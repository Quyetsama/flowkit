"""Batch Engine for FlowKit Studio.

Handles multi-threaded queue processing across multiple Chrome profiles,
automatic polling, streaming download, and optional watermark removal via FFmpeg.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from agent.studio.models import BatchJobConfig, BatchTask, ProfileConfig, TaskStatus
from agent.studio.profile_manager import profile_manager
from agent.services.flow_client import get_flow_client
from agent.services.omni_flash import generate_omni_flash_text_video
from agent.config import GOOGLE_API_KEY, GOOGLE_FLOW_API

logger = logging.getLogger(__name__)


def slugify(text: str, max_words: int = 6) -> str:
    """Create a safe filesystem slug from a prompt."""
    clean = re.sub(r"[^\w\s-]", "", text.strip()).strip()
    words = clean.split()[:max_words]
    slug = "_".join(words).lower()
    return slug or "item"


ALPHA_48_PATH = Path(__file__).parent / "assets" / "alpha_48.npy"


def load_watermark_alpha_48():
    """Load the pre-calibrated 48x48 alpha channel map for Google watermark."""
    try:
        import numpy as np

        if ALPHA_48_PATH.exists():
            return np.load(str(ALPHA_48_PATH))
    except Exception as exc:
        logger.warning("Could not load alpha_48.npy: %s", exc)
    return None


class BatchEngine:
    """Coordinates batch tasks, profile workers, downloads, and post-processing."""

    def __init__(self):
        self.tasks: list[BatchTask] = []
        self.config: BatchJobConfig | None = None
        self.is_running: bool = False
        self.is_paused: bool = False
        self._cancel_requested: bool = False
        self._worker_tasks: list[asyncio.Task] = []
        self._subscribers: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Broadcast event to UI via Server-Sent Events / WebSockets."""
        msg = {"type": event_type, "timestamp": time.time(), "data": data}
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for d in dead:
            self.unsubscribe(d)

    def get_status(self) -> dict[str, Any]:
        """Summary of current batch state."""
        counts = {
            "queued": 0,
            "submitting": 0,
            "generating": 0,
            "downloading": 0,
            "delogo": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        }
        for t in self.tasks:
            counts[t.status] = counts.get(t.status, 0) + 1

        total = len(self.tasks)
        finished = counts["completed"] + counts["failed"] + counts["cancelled"]
        percent = int((finished / total * 100)) if total > 0 else 0

        return {
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "total": total,
            "completed": counts["completed"],
            "failed": counts["failed"],
            "pending": total - finished,
            "progress_percent": percent,
            "counts": counts,
            "output_dir": self.config.output_dir if self.config else "",
            "tasks": [t.model_dump() for t in self.tasks],
        }

    async def start_batch(self, config: BatchJobConfig) -> dict[str, Any]:
        """Initialize and start a batch job across connected profiles."""
        async with self._lock:
            if self.is_running:
                return {"success": False, "error": "Một tác vụ hàng loạt đang chạy!"}

            # Filter valid prompts
            raw_prompts = [p.strip() for p in config.prompts if p.strip()]
            if not raw_prompts:
                return {"success": False, "error": "Danh sách prompt trống!"}

            # Refresh profiles status
            await profile_manager.check_all_profiles()
            all_profiles = profile_manager.list_profiles()

            # Select active profiles
            target_pids = set(config.selected_profiles) if config.selected_profiles else None
            available_profiles = [
                p for p in all_profiles
                if (target_pids is None or p.id in target_pids) and p.is_connected
            ]

            if not available_profiles:
                # If target specified but none connected, or none connected at all
                connected_names = [p.name for p in all_profiles if p.is_connected]
                return {
                    "success": False,
                    "error": (
                        "Không tìm thấy Profile nào đang mở Chrome và sẵn sàng! "
                        f"(Đã kiểm tra: {len(all_profiles)} profiles, đang kết nối: {len(connected_names)}). "
                        "Vui lòng bấm 'Mở Chrome' ở tab Profiles và đăng nhập Google Flow trước."
                    ),
                }

            # Setup output dir
            out_path = Path(config.output_dir)
            if not out_path.is_absolute():
                out_path = Path.cwd() / out_path
            out_path.mkdir(parents=True, exist_ok=True)
            config.output_dir = str(out_path)

            self.config = config
            self.is_running = True
            self.is_paused = False
            self._cancel_requested = False
            self.tasks.clear()

            # Build Task list
            for i, prompt in enumerate(raw_prompts, start=1):
                slug = slugify(prompt)
                ext = "mp4" if config.task_type == "video" else "jpg"
                filename = f"{i:03d}_{slug}.{ext}"
                task = BatchTask(
                    index=i,
                    prompt=prompt,
                    task_type=config.task_type,
                    duration_s=config.duration_s,
                    resolution=config.resolution,
                    aspect_ratio=config.aspect_ratio,
                    image_model=config.image_model,
                    auto_delogo=config.auto_delogo,
                    output_filename=filename,
                    file_path=str(out_path / filename),
                )
                self.tasks.append(task)

            # Start worker loops
            task_queue: asyncio.Queue[BatchTask] = asyncio.Queue()
            for t in self.tasks:
                task_queue.put_nowait(t)

            self._worker_tasks = [
                asyncio.create_task(self._profile_worker(p, task_queue, config))
                for p in available_profiles
            ]

            logger.info(
                "Started batch with %d tasks on %d workers (profiles=%s)",
                len(self.tasks),
                len(available_profiles),
                [p.id for p in available_profiles],
            )
            await self.emit_event("batch_started", {"total": len(self.tasks)})
            return {"success": True, "total": len(self.tasks), "workers": len(available_profiles)}

    async def pause_batch(self) -> dict[str, Any]:
        self.is_paused = True
        await self.emit_event("batch_paused", {})
        return {"success": True, "is_paused": True}

    async def resume_batch(self) -> dict[str, Any]:
        self.is_paused = False
        await self.emit_event("batch_resumed", {})
        return {"success": True, "is_paused": False}

    async def cancel_batch(self) -> dict[str, Any]:
        self._cancel_requested = True
        self.is_running = False
        for t in self.tasks:
            if t.status in ("queued", "submitting"):
                t.status = "cancelled"
        for wt in self._worker_tasks:
            wt.cancel()
        await self.emit_event("batch_cancelled", {})
        return {"success": True, "is_running": False}

    async def _profile_worker(
        self,
        profile: ProfileConfig,
        queue: asyncio.Queue[BatchTask],
        config: BatchJobConfig,
    ) -> None:
        """Worker loop bound to a single Google Chrome profile."""
        cdp_url = f"http://127.0.0.1:{profile.cdp_port}"
        client = get_flow_client()

        # Auto-detect profile's active project if missing
        if not profile.active_project_id:
            try:
                await profile_manager.check_profile_status(profile)
            except Exception as exc:
                logger.warning("Could not auto-detect project for profile %s: %s", profile.id, exc)

        while self.is_running and not self._cancel_requested:
            # Handle pause
            while self.is_paused and not self._cancel_requested:
                await asyncio.sleep(1)

            try:
                task = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            task.profile_id = profile.id
            task.started_at = time.time()
            task.status = "submitting"
            task.progress_percent = 10
            await self.emit_event("task_updated", task.model_dump())

            try:
                # 1. Submit Generation
                project_id = profile.active_project_id or ""

                if task.task_type == "video":
                    v_aspect = task.aspect_ratio
                    if "PORTRAIT" in v_aspect or "9:16" in v_aspect:
                        v_aspect = "VIDEO_ASPECT_RATIO_PORTRAIT"
                    else:
                        v_aspect = "VIDEO_ASPECT_RATIO_LANDSCAPE"

                    submit_res = await generate_omni_flash_text_video(
                        prompt=task.prompt,
                        project_id=project_id,
                        duration_s=task.duration_s,
                        resolution=task.resolution,
                        aspect_ratio=v_aspect,
                        cdp_endpoint=cdp_url,
                    )
                else:  # Image
                    i_aspect = task.aspect_ratio
                    if "PORTRAIT" in i_aspect or "9:16" in i_aspect:
                        i_aspect = "IMAGE_ASPECT_RATIO_PORTRAIT"
                    elif "SQUARE" in i_aspect or "1:1" in i_aspect:
                        i_aspect = "IMAGE_ASPECT_RATIO_SQUARE"
                    elif "FOUR_THREE" in i_aspect or "4:3" in i_aspect:
                        i_aspect = "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE"
                    elif "THREE_FOUR" in i_aspect or "3:4" in i_aspect:
                        i_aspect = "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"
                    else:
                        i_aspect = "IMAGE_ASPECT_RATIO_LANDSCAPE"

                    img_data = {
                        "prompt": task.prompt,
                        "project_id": project_id,
                        "aspect_ratio": i_aspect,
                        "image_model": task.image_model,
                        "count": 1,
                        "cdp_endpoint": cdp_url,
                    }
                    submit_res = await client.generate_images(**img_data)

                if submit_res.get("error"):
                    raise RuntimeError(submit_res["error"])

                if not profile.active_project_id:
                    res_pid = submit_res.get("data", {}).get("flowkitPolling", {}).get("project_id")
                    if res_pid:
                        profile.active_project_id = res_pid
                        profile_manager.save()

                # Extract media_id
                media_list = submit_res.get("media") or submit_res.get("data", {}).get("media") or []
                if not media_list and isinstance(submit_res.get("data"), dict):
                    media_list = submit_res["data"].get("media", [])

                if media_list and isinstance(media_list[0], dict):
                    task.media_id = media_list[0].get("name")

                workflows = submit_res.get("workflows") or submit_res.get("data", {}).get("workflows") or []
                if workflows and isinstance(workflows[0], dict):
                    task.workflow_name = workflows[0].get("name")
                    if not task.media_id:
                        task.media_id = workflows[0].get("primary_media_id")

                if not task.media_id:
                    raise RuntimeError(f"Không nhận được media_id từ phản hồi: {submit_res}")

                # Check if image returned immediate download url (Nano Banana)
                download_url = None
                if task.task_type == "image" and media_list and isinstance(media_list[0], dict):
                    first_media = media_list[0]
                    download_url = (
                        first_media.get("image", {}).get("generatedImage", {}).get("fifeUrl")
                        or first_media.get("image", {}).get("fifeUrl")
                        or first_media.get("fifeUrl")
                        or first_media.get("url")
                    )

                # 2. Polling for Completion (if not already downloaded directly)
                if not download_url:
                    task.status = "generating"
                    task.progress_percent = 30
                    await self.emit_event("task_updated", task.model_dump())

                    poll_attempts = 0
                    max_polls = 120  # ~8 minutes max
                    while poll_attempts < max_polls and self.is_running and not self._cancel_requested:
                        await asyncio.sleep(4)
                        poll_attempts += 1
                        task.progress_percent = min(75, 30 + int(poll_attempts * 0.7))
                        await self.emit_event("task_updated", task.model_dump())

                        try:
                            media_res = await client.get_media(task.media_id, cdp_endpoint=cdp_url)
                            data = media_res.get("data", media_res) if isinstance(media_res, dict) else {}
                            v_url = (data.get("video", {}) or {}).get("fifeUrl")
                            i_url = (data.get("image", {}) or {}).get("fifeUrl")
                            download_url = data.get("url") or v_url or i_url
                            if download_url:
                                break
                        except Exception as poll_err:
                            logger.debug("Polling %s: %s", task.media_id, poll_err)

                if not download_url:
                    raise TimeoutError("Quá thời gian chờ render từ Google Flow (Timeout > 8 phút)")

                task.download_url = download_url

                # 3. Download File
                task.status = "downloading"
                task.progress_percent = 80
                await self.emit_event("task_updated", task.model_dump())

                await asyncio.to_thread(self._download_file, download_url, task.file_path)

                # 4. Optional Post-Processing (Delogo)
                if task.auto_delogo and shutil.which("ffmpeg"):
                    task.status = "delogo"
                    task.progress_percent = 90
                    await self.emit_event("task_updated", task.model_dump())

                    orig_path = Path(task.file_path)
                    ext = orig_path.suffix or (".mp4" if task.task_type == "video" else ".jpg")
                    clean_file = str(orig_path.with_name(f"{orig_path.stem}_clean{ext}"))
                    is_video = (task.task_type == "video")
                    success = await asyncio.to_thread(self._run_delogo, task.file_path, clean_file, is_video)
                    if success:
                        task.clean_file_path = clean_file

                task.status = "completed"
                task.progress_percent = 100
                task.completed_at = time.time()
                await self.emit_event("task_updated", task.model_dump())

            except Exception as exc:
                logger.error("Task %d failed on profile %s: %s", task.index, profile.id, exc)
                task.status = "failed"
                task.error = str(exc)
                task.completed_at = time.time()
                await self.emit_event("task_updated", task.model_dump())

            finally:
                queue.task_done()
                self._save_results()

            # Cooldown to stay safe with Google rate limits
            if not queue.empty() and config.cooldown_seconds > 0:
                await asyncio.sleep(config.cooldown_seconds)

        # Check if all completed
        if queue.empty():
            all_done = all(t.status in ("completed", "failed", "cancelled") for t in self.tasks)
            if all_done:
                self.is_running = False
                await self.emit_event("batch_completed", self.get_status())

    def _download_file(self, url: str, target_path: str) -> None:
        """Download remote url with stream to target file."""
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp, open(target_path, "wb") as out:
            shutil.copyfileobj(resp, out)
    def _remove_image_watermark_lossless(self, src: str, dst: str) -> bool:
        """Mathematically reconstruct original pixels using Reverse Alpha Blending (lossless, zero blur)."""
        try:
            import base64
            import cv2
            import numpy as np
        except ImportError:
            return False

        try:
            img = cv2.imread(src)
            if img is None:
                return False
            h, w, _ = img.shape

            alpha_48 = load_watermark_alpha_48()
            if alpha_48 is None:
                return False

            scale_factor = 2 if (h >= 1500 and w >= 1500) else 1
            box_size = 48 * scale_factor
            template = (
                cv2.resize(alpha_48, (box_size, box_size), interpolation=cv2.INTER_LINEAR)
                if scale_factor != 1
                else alpha_48.copy()
            )

            expected_x = w - 126 * scale_factor
            expected_y = h - 126 * scale_factor

            # Broad search in bottom-right corner
            corner_dim = 160 * scale_factor
            roi_x1 = max(0, w - corner_dim)
            roi_y1 = max(0, h - corner_dim)

            gray_corner = cv2.cvtColor(img[roi_y1:h, roi_x1:w], cv2.COLOR_BGR2GRAY).astype(np.float32)
            template_norm = (template / template.max() * 255.0).astype(np.float32)

            res = cv2.matchTemplate(gray_corner, template_norm, cv2.TM_CCOEFF_NORMED)
            _, max_v, _, max_l = cv2.minMaxLoc(res)

            if max_v >= 0.18:
                best_x = roi_x1 + max_l[0]
                best_y = roi_y1 + max_l[1]
            else:
                best_x = expected_x
                best_y = expected_y

            best_x = max(0, min(w - box_size, best_x))
            best_y = max(0, min(h - box_size, best_y))

            patch = img[best_y : best_y + box_size, best_x : best_x + box_size].astype(np.float32)
            a = np.clip(template * 0.64, 0.0, 0.95)[:, :, np.newaxis]
            unblended = (patch - 255.0 * a) / (1.0 - a)
            unblended = np.clip(unblended, 0, 255).astype(np.uint8)

            img[best_y : best_y + box_size, best_x : best_x + box_size] = unblended

            ext = Path(dst).suffix.lower()
            if ext in (".jpg", ".jpeg"):
                return cv2.imwrite(dst, img, [cv2.IMWRITE_JPEG_QUALITY, 96])
            elif ext == ".png":
                return cv2.imwrite(dst, img, [cv2.IMWRITE_PNG_COMPRESSION, 3])
            elif ext == ".webp":
                return cv2.imwrite(dst, img, [cv2.IMWRITE_WEBP_QUALITY, 96])
            return cv2.imwrite(dst, img)
        except Exception as exc:
            logger.warning("Lossless watermark removal failed, falling back: %s", exc)
            return False

    def _get_media_dimensions(self, file_path: str) -> tuple[int, int] | None:
        """Extract width and height using ffprobe if available."""
        if not shutil.which("ffprobe"):
            return None
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            file_path,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0 and res.stdout.strip():
                parts = res.stdout.strip().split("x")
                if len(parts) >= 2:
                    return int(parts[0]), int(parts[1])
        except Exception as exc:
            logger.debug("Failed to probe dimensions for %s: %s", file_path, exc)
        return None

    def _get_delogo_filter(self, width: int, height: int, is_video: bool = True) -> str:
        """Calculate delogo filter parameters matching Google watermark placement."""
        if is_video:
            max_dim = max(width, height)
            if max_dim >= 1800:
                w, h = 95, 95
                offset_x, offset_y = 220, 215
            elif max_dim >= 1100:
                w, h = 65, 65
                offset_x, offset_y = 150, 145
            else:
                w, h = 45, 45
                offset_x, offset_y = 90, 85
        else:
            min_dim = min(width, height)
            if min_dim >= 1500:
                w, h = 100, 100
                offset_x, offset_y = 175, 175
            elif min_dim >= 700:
                w, h = 72, 72
                offset_x, offset_y = 125, 125
            else:
                w, h = 50, 50
                offset_x, offset_y = 90, 90

        x = max(0, min(width - w, width - offset_x))
        y = max(0, min(height - h, height - offset_y))
        return f"delogo=x={x}:y={y}:w={w}:h={h}"

    def _run_delogo(self, src: str, dst: str, is_video: bool = True) -> bool:
        """Execute delogo to remove bottom-right Google watermark."""
        if not os.path.exists(src):
            logger.warning("Source file not found for delogo: %s", src)
            return False

        if not is_video:
            if self._remove_image_watermark_lossless(src, dst):
                return True
            logger.info("Reverse alpha blending skipped, falling back to FFmpeg delogo")

        dims = self._get_media_dimensions(src)
        if dims:
            width, height = dims
        else:
            width, height = (1280, 720) if is_video else (1024, 1024)

        delogo_vf = self._get_delogo_filter(width, height, is_video=is_video)

        if is_video:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                src,
                "-vf",
                delogo_vf,
                "-c:a",
                "copy",
                dst,
            ]
        else:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                src,
                "-vf",
                delogo_vf,
                "-q:v",
                "2",
                dst,
            ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            return res.returncode == 0 and os.path.exists(dst)
        except Exception as exc:
            logger.warning("FFmpeg delogo failed for %s: %s", src, exc)
            return False

    def _save_results(self) -> None:
        """Export results.json and results.csv into output directory."""
        if not self.config or not self.config.output_dir:
            return
        out_dir = Path(self.config.output_dir)
        try:
            # JSON
            with open(out_dir / "results.json", "w", encoding="utf-8") as f:
                json.dump([t.model_dump() for t in self.tasks], f, indent=2, ensure_ascii=False)

            # CSV
            with open(out_dir / "results.csv", "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Index", "Prompt", "Status", "Profile", "File", "Clean File", "Error"])
                for t in self.tasks:
                    writer.writerow([
                        t.index,
                        t.prompt,
                        t.status,
                        t.profile_id or "",
                        t.file_path or "",
                        t.clean_file_path or "",
                        t.error or "",
                    ])
        except Exception as e:
            logger.warning("Failed to save batch results: %s", e)


batch_engine = BatchEngine()
