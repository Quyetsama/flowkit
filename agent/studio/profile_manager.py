"""Profile Manager for FlowKit Studio.

Handles multiple Chrome profiles with isolated remote debugging ports and user data dirs,
supporting both Windows and macOS automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from agent.studio.models import ProfileConfig

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
PROFILES_FILE = ROOT_DIR / "profiles.json"


def find_chrome_executable() -> str | None:
    """Auto-detect Google Chrome path across macOS, Windows, and Linux."""
    sys_name = platform.system()

    if sys_name == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
        for c in candidates:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c

    elif sys_name == "Windows":
        candidates = [
            os.path.join(os.environ.get("PROGRAMFILES", "C:\\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c

    else:  # Linux / Unix
        for bin_name in ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]:
            found = shutil.which(bin_name)
            if found:
                return found

    return shutil.which("chrome") or shutil.which("google-chrome")


def get_default_profile_dir(profile_id: str) -> str:
    """Return default OS-appropriate directory for profile storage."""
    sys_name = platform.system()
    if sys_name == "Darwin":
        if profile_id == "profile_1":
            legacy_dir = Path.home() / "Library" / "Application Support" / "FlowkitChrome"
            if legacy_dir.exists():
                return str(legacy_dir)
        return str(Path.home() / "Library" / "Application Support" / "FlowkitProfiles" / profile_id)
    elif sys_name == "Windows":
        local_app = os.environ.get("LOCALAPPDATA", str(Path.home()))
        return os.path.join(local_app, "FlowkitProfiles", profile_id)
    else:
        return str(Path.home() / ".config" / "flowkit_profiles" / profile_id)


class ProfileManager:
    """Manages profile definitions, Chrome process launching, and CDP status."""

    def __init__(self, file_path: Path = PROFILES_FILE):
        self.file_path = file_path
        self._profiles: dict[str, ProfileConfig] = {}
        self._last_check_time: float = 0.0
        self.load()

    def load(self) -> None:
        """Load profiles from disk or create defaults."""
        if not self.file_path.exists():
            # Create default Profile 1
            default_p = ProfileConfig(
                id="profile_1",
                name="Profile 1 (Chính)",
                cdp_port=9224,
                user_data_dir=get_default_profile_dir("profile_1"),
            )
            self._profiles = {default_p.id: default_p}
            self.save()
            return

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            loaded = {}
            for item in data.get("profiles", []):
                # Runtime connectivity must be verified rather than blindly trusted from disk
                item["is_connected"] = False
                item["is_running"] = False
                p = ProfileConfig(**item)
                loaded[p.id] = p
            self._profiles = loaded
        except Exception as exc:
            logger.error("Failed to load profiles.json: %s. Reinitializing defaults.", exc)
            default_p = ProfileConfig(
                id="profile_1",
                name="Profile 1 (Chính)",
                cdp_port=9224,
                user_data_dir=get_default_profile_dir("profile_1"),
            )
            self._profiles = {default_p.id: default_p}
            self.save()

    def save(self) -> None:
        """Persist profiles to disk."""
        data = {
            "profiles": [p.model_dump() for p in self._profiles.values()]
        }
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def list_profiles(self) -> list[ProfileConfig]:
        return list(self._profiles.values())

    def get_profile(self, profile_id: str) -> ProfileConfig | None:
        return self._profiles.get(profile_id)

    def add_profile(self, name: str, port: int | None = None, user_data_dir: str | None = None) -> ProfileConfig:
        idx = len(self._profiles) + 1
        used_ports = {p.cdp_port for p in self._profiles.values()}
        allocated_port = port or 9224
        while allocated_port in used_ports:
            allocated_port += 1

        pid = f"profile_{idx}"
        p = ProfileConfig(
            id=pid,
            name=name or f"Profile {idx}",
            cdp_port=allocated_port,
            user_data_dir=user_data_dir or get_default_profile_dir(pid),
        )
        self._profiles[p.id] = p
        self.save()
        return p

    def _close_chrome_instance(self, profile: ProfileConfig) -> None:
        """Attempt to gracefully close Chrome on this profile's CDP port before deleting data."""
        if not profile.cdp_port:
            return
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{profile.cdp_port}/json/version", method="GET")
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                vdata = json.loads(resp.read().decode("utf-8"))
            ws_url = vdata.get("webSocketDebuggerUrl")
            if ws_url:
                import websockets.sync.client
                with websockets.sync.client.connect(ws_url, close_timeout=1) as ws:
                    ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                import time
                time.sleep(0.5)
        except Exception as exc:
            logger.debug("Could not close Chrome on port %s via CDP: %s", profile.cdp_port, exc)

    def _delete_profile_data_dir(self, user_data_dir: str | None) -> None:
        """Safely delete user data directory containing all cookies, cache, and session tokens."""
        if not user_data_dir:
            return

        try:
            target_path = Path(user_data_dir).expanduser().resolve()
            home = Path.home().resolve()
            root = Path("/").resolve()

            if target_path == home or target_path == root:
                logger.warning("Refusing to delete unsafe root or home path: %s", target_path)
                return

            if not target_path.exists() or not target_path.is_dir():
                return

            path_str = str(target_path)
            is_recognized_profile_dir = (
                "FlowkitProfiles" in path_str
                or "flowkit_profiles" in path_str
                or "profile_" in target_path.name.lower()
            )
            if not is_recognized_profile_dir:
                try:
                    target_path.relative_to(home)
                    if len(target_path.parts) <= len(home.parts) + 1:
                        logger.warning("Refusing to delete shallow directory directly under home: %s", target_path)
                        return
                except ValueError:
                    import tempfile
                    temp_dir = Path(tempfile.gettempdir()).resolve()
                    try:
                        target_path.relative_to(temp_dir)
                    except ValueError:
                        logger.warning("Refusing to delete path outside home and temp: %s", target_path)
                        return

            logger.info("Purging profile user data directory (cookies & sessions): %s", target_path)
            shutil.rmtree(target_path, ignore_errors=True)
        except Exception as exc:
            logger.error("Failed to delete profile data directory %s: %s", user_data_dir, exc)

    def delete_profile(self, profile_id: str, delete_data: bool = True) -> bool:
        """Delete profile configuration and optionally purge all cookies and data on disk."""
        if profile_id not in self._profiles:
            return False

        profile = self._profiles[profile_id]

        if delete_data:
            self._close_chrome_instance(profile)
            self._delete_profile_data_dir(profile.user_data_dir)

        del self._profiles[profile_id]
        self.save()
        return True

    async def check_profile_status(self, profile: ProfileConfig) -> ProfileConfig:
        """Query Chrome loopback CDP to see if this profile is active and has Flow open."""
        cdp_url = f"http://127.0.0.1:{profile.cdp_port}"
        try:
            def _query_json():
                req = urllib.request.Request(f"{cdp_url}/json", method="GET")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            tabs = await asyncio.to_thread(_query_json)
            profile.is_running = True
            profile.is_connected = True
            profile.error_message = None

            # Look for flow tab with active project
            active_pid = None
            for t in tabs:
                if t.get("type") != "page":
                    continue
                u = str(t.get("url") or "")
                if ("flow.google.com" in u or "labs.google/fx" in u) and "/project/" in u:
                    parts = u.split("/project/")
                    if len(parts) > 1:
                        cand = parts[1].split("?")[0].split("/")[0].strip()
                        if cand and cand != "404":
                            active_pid = cand
                            break

            if active_pid:
                profile.active_project_id = active_pid
                self.save()

        except Exception as exc:
            profile.is_running = False
            profile.is_connected = False
            profile.error_message = "Chưa kết nối Chrome (CDP port đóng)"

        return profile

    async def check_all_profiles(self) -> list[ProfileConfig]:
        tasks = [self.check_profile_status(p) for p in self._profiles.values()]
        results = await asyncio.gather(*tasks)
        self._last_check_time = time.time()
        return results

    async def get_active_connected_profiles(self, max_cache_age_s: float = 5.0) -> list[ProfileConfig]:
        """Return profiles currently connected via CDP, refreshing if cache is older than max_cache_age_s."""
        now = time.time()
        if (now - self._last_check_time > max_cache_age_s) or not any(p.is_connected for p in self._profiles.values()):
            await self.check_all_profiles()
        return [p for p in self._profiles.values() if p.is_connected]

    def launch_chrome(self, profile_id: str) -> dict[str, Any]:
        """Launch Google Chrome process configured for this profile."""
        profile = self.get_profile(profile_id)
        if not profile:
            return {"success": False, "error": f"Không tìm thấy profile '{profile_id}'"}

        chrome_path = find_chrome_executable()
        if not chrome_path:
            return {
                "success": False,
                "error": "Không tìm thấy trình duyệt Google Chrome trên hệ điều hành này. Vui lòng cài đặt Chrome.",
            }

        os.makedirs(profile.user_data_dir, exist_ok=True)

        cmd = [
            chrome_path,
            f"--remote-debugging-port={profile.cdp_port}",
            f"--user-data-dir={profile.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://flow.google.com/",
        ]

        try:
            # Spawn detached process
            if platform.system() == "Windows":
                DETACHED_PROCESS = 0x00000008
                subprocess.Popen(
                    cmd,
                    creationflags=DETACHED_PROCESS,
                    close_fds=True,
                )
            else:
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setpgrp,
                )

            logger.info("Launched Chrome for profile %s on port %s", profile.id, profile.cdp_port)
            return {
                "success": True,
                "profile_id": profile.id,
                "cdp_port": profile.cdp_port,
                "chrome_path": chrome_path,
            }
        except Exception as exc:
            logger.error("Failed to launch Chrome for %s: %s", profile.id, exc)
            return {"success": False, "error": str(exc)}


profile_manager = ProfileManager()
