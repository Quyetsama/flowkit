"""FastAPI routes for FlowKit Studio."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from agent.studio.batch_engine import batch_engine
from agent.studio.models import BatchJobConfig, ProfileConfig
from agent.studio.profile_manager import profile_manager

router = APIRouter(prefix="/api/studio", tags=["studio"])
WEB_DIR = Path(__file__).resolve().parent / "web"


class CreateProfileRequest(BaseModel):
    name: str = "Tài khoản mới"
    port: int | None = None
    user_data_dir: str | None = None


class OpenFolderRequest(BaseModel):
    path: str


@router.get("/profiles")
async def list_profiles(check: bool = False):
    """List all profiles with their connectivity and active project status."""
    if check:
        await profile_manager.check_all_profiles()
    profiles = profile_manager.list_profiles()
    return {"profiles": [p.model_dump() for p in profiles]}


@router.post("/profiles")
async def create_profile(body: CreateProfileRequest):
    p = profile_manager.add_profile(
        name=body.name,
        port=body.port,
        user_data_dir=body.user_data_dir,
    )
    return {"profile": p.model_dump()}


@router.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: str):
    success = profile_manager.delete_profile(profile_id)
    if not success:
        raise HTTPException(404, f"Không tìm thấy profile '{profile_id}'")
    return {"success": True}


@router.post("/profiles/{profile_id}/launch")
async def launch_chrome_profile(profile_id: str):
    """1-Click launch Chrome with the appropriate OS path and CDP flags."""
    res = profile_manager.launch_chrome(profile_id)
    if not res.get("success"):
        raise HTTPException(400, res.get("error", "Lỗi khởi động Chrome"))
    # Wait a moment then check status
    await asyncio.sleep(1.5)
    p = profile_manager.get_profile(profile_id)
    if p:
        await profile_manager.check_profile_status(p)
        return {"success": True, "profile": p.model_dump()}
    return res


@router.post("/profiles/{profile_id}/check")
async def check_profile(profile_id: str):
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(404, "Profile not found")
    await profile_manager.check_profile_status(p)
    return {"profile": p.model_dump()}


@router.post("/batch/start")
async def start_batch(config: BatchJobConfig):
    res = await batch_engine.start_batch(config)
    if not res.get("success"):
        raise HTTPException(400, res.get("error", "Không thể bắt đầu tác vụ"))
    return res


@router.get("/batch/status")
async def get_batch_status():
    return batch_engine.get_status()


@router.post("/batch/pause")
async def pause_batch():
    return await batch_engine.pause_batch()


@router.post("/batch/resume")
async def resume_batch():
    return await batch_engine.resume_batch()


@router.post("/batch/cancel")
async def cancel_batch():
    return await batch_engine.cancel_batch()


@router.post("/open-folder")
async def open_folder(body: OpenFolderRequest):
    """Open folder in macOS Finder or Windows File Explorer."""
    folder = Path(body.path).resolve()
    if not folder.exists():
        raise HTTPException(404, f"Thư mục không tồn tại: {folder}")

    sys_name = platform.system()
    try:
        if sys_name == "Darwin":
            subprocess.run(["open", str(folder)], check=True)
        elif sys_name == "Windows":
            subprocess.run(["explorer", str(folder)], check=True)
        else:
            subprocess.run(["xdg-open", str(folder)], check=True)
        return {"success": True, "path": str(folder)}
    except Exception as exc:
        raise HTTPException(500, f"Không thể mở thư mục: {exc}")


@router.get("/events")
async def studio_events(request: Request):
    """Server-Sent Events (SSE) for real-time progress updates in the UI."""
    q = batch_engine.subscribe()

    async def event_generator():
        try:
            # Send initial snapshot
            init_data = json.dumps({"type": "init", "data": batch_engine.get_status()})
            yield f"data: {init_data}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Keep-alive heartbeat
                    yield ": ping\n\n"
        finally:
            batch_engine.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# Standalone Web UI View
@router.get("/ui", response_class=HTMLResponse)
@router.get("", response_class=HTMLResponse)
async def serve_studio_ui():
    index_file = WEB_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(404, "Studio Web UI chưa được cài đặt")
    return index_file.read_text(encoding="utf-8")


@router.get("/video-stream")
async def video_stream(path: str):
    file_path = Path(path).resolve()
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "File video không tồn tại")
    return FileResponse(file_path, media_type="video/mp4")

