"""Data models for FlowKit Studio multi-profile and batch management."""

from __future__ import annotations

import time
import uuid
from typing import Literal
from pydantic import BaseModel, Field

TaskStatus = Literal[
    "queued",
    "submitting",
    "generating",
    "downloading",
    "delogo",
    "completed",
    "failed",
    "cancelled",
]


class ProfileConfig(BaseModel):
    id: str = Field(default_factory=lambda: f"profile_{uuid.uuid4().hex[:6]}")
    name: str = "Google Account"
    cdp_port: int = 9224
    user_data_dir: str = ""
    is_connected: bool = False
    is_running: bool = False
    active_project_id: str | None = None
    credits_balance: int | None = None
    plan: str | None = None
    error_message: str | None = None
    consecutive_failures: int = 0
    quarantine_until: float = 0.0
    disabled_reason: str | None = None


class BatchTask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    index: int = 0
    prompt: str = ""
    task_type: Literal["video", "image"] = "video"
    duration_s: int = 8
    resolution: str = "720p"
    aspect_ratio: str = "VIDEO_ASPECT_RATIO_LANDSCAPE"
    image_model: str = "IMAGE_MODEL_NANO_BANANA_2"
    auto_delogo: bool = True
    output_filename: str = ""
    status: TaskStatus = "queued"
    profile_id: str | None = None
    media_id: str | None = None
    workflow_name: str | None = None
    download_url: str | None = None
    file_path: str | None = None
    clean_file_path: str | None = None
    error: str | None = None
    progress_percent: int = 0
    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None


class BatchJobConfig(BaseModel):
    prompts: list[str] = Field(default_factory=list)
    task_type: Literal["video", "image"] = "video"
    duration_s: int = 8
    resolution: str = "720p"
    aspect_ratio: str = "VIDEO_ASPECT_RATIO_LANDSCAPE"
    image_model: str = "IMAGE_MODEL_NANO_BANANA_2"
    auto_delogo: bool = True
    output_dir: str = "output/studio"
    selected_profiles: list[str] = Field(default_factory=list)
    max_concurrent_per_profile: int = 1
    cooldown_seconds: float = 6.0
