"""Direct Flow API endpoints — for manual operations outside the queue."""
import asyncio
import base64
import logging
import mimetypes
import os
from pathlib import Path
import time
from typing import Literal, Optional
from urllib.parse import quote
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from agent.config import (
    BASE_DIR,
    OUTPUT_DIR,
    USE_BATCH_RPC, FLOW_PROJECT_ID, FLOW_ALLOW_DEGRADED,
    FLOW_GENERATION_MIN_INTERVAL_S, FLOW_GENERATION_MAX_CONCURRENT,
    FLOW_UNUSUAL_ACTIVITY_COOLDOWN_S,
)
from agent.services.flow_client import get_flow_client
from agent.services.browser_session import (
    debit_cached_flow_credits,
    ensure_flow_session,
    inspect_flow_credits,
    inspect_flow_session,
    inspect_google_account,
)
from agent.services.flow_credits import credit_response, estimate_video_generation_cost
from agent.services.flow_payload_drift import payload_drift_status
from agent.services.flow_project_session import current_session_project, ensure_session_project
from agent.services.flow_ui_generation import cache_uploaded_media_bytes, mark_uploaded_media_for_ui_refresh
from agent.services.image_capabilities import image_capabilities
from agent.services.omni_flash import (
    check_omni_flash_status,
    generate_omni_flash_first_frame_video,
    generate_omni_flash_first_last_video,
    generate_omni_flash_text_video,
    generate_omni_flash_video,
)
from agent.services.watermark_remover import (
    download_media_file,
    run_delogo,
    poll_media_download_url,
    get_media_dimensions,
)

from agent.studio.profile_manager import profile_manager

logger = logging.getLogger(__name__)

TAG_IMAGE = "🎨 Tạo & Xử Lý Ảnh (Image Generation)"
TAG_VIDEO = "🎬 Tạo Video & Upscale (Video Generation)"
TAG_WATERMARK = "💧 Xóa Watermark (Watermark Removal)"
TAG_STATUS = "💳 Trạng Thái & Credits (Status & Credits)"

FLOW_OUTPUT_DIR = OUTPUT_DIR / "flow"
FLOW_IMAGES_DIR = FLOW_OUTPUT_DIR / "images"
FLOW_VIDEOS_DIR = FLOW_OUTPUT_DIR / "videos"
FLOW_UPLOADS_DIR = FLOW_OUTPUT_DIR / "uploads"

router = APIRouter(prefix="/flow")

_flow_round_robin_counter = 0


async def resolve_profile_target(
    profile_id: Optional[str] = None,
    cdp_endpoint: Optional[str] = None,
    project_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolves (cdp_endpoint, active_project_id, profile_id).

    If explicit cdp_endpoint is provided, routes to that Chrome profile.
    If explicit profile_id is provided, routes to that profile (auto-launching if offline and unquarantined).
    If omitted:
      1. Checks connected active profiles and round-robins.
      2. If NO active profiles are connected, auto-launches an unquarantined profile on-demand (max 5 active).
      3. Automatically ensures the profile navigates to the requested project_id if provided.
    """
    global _flow_round_robin_counter

    # 1. Explicit CDP endpoint
    if cdp_endpoint and cdp_endpoint.strip():
        ep = cdp_endpoint.strip().rstrip("/")
        for p in profile_manager.list_profiles():
            if f":{p.cdp_port}" in ep:
                if project_id and p.active_project_id != project_id:
                    await profile_manager.navigate_profile_to_project(p, project_id)
                return ep, p.active_project_id, p.id
        return ep, None, None

    # 2. Explicit profile_id
    if profile_id and profile_id.strip():
        p = profile_manager.get_profile(profile_id.strip())
        if p:
            if not p.is_connected:
                # If unquarantined, auto-launch on demand
                launched = await profile_manager.auto_launch_profiles(
                    count=1,
                    project_id=project_id,
                    preferred_profile_id=p.id,
                )
                if launched:
                    p = launched[0]
            elif project_id and p.active_project_id != project_id:
                await profile_manager.navigate_profile_to_project(p, project_id)

            if p.is_connected:
                ep = f"http://127.0.0.1:{p.cdp_port}"
                return ep, p.active_project_id, p.id

    # 3. Auto Load-Balancing: check active connected profiles
    connected = await profile_manager.get_active_connected_profiles()
    if not connected:
        # No profile is active! Auto-launch on-demand up to 1 profile for this request (respecting max 5 active cap)
        logger.info("[FlowKit MultiProfile] No active profiles found. Auto-launching profile on-demand...")
        launched = await profile_manager.auto_launch_profiles(count=1, project_id=project_id)
        if launched:
            connected = launched

    if connected:
        selected = connected[_flow_round_robin_counter % len(connected)]
        _flow_round_robin_counter += 1
        if project_id and selected.active_project_id != project_id:
            await profile_manager.navigate_profile_to_project(selected, project_id)

        ep = f"http://127.0.0.1:{selected.cdp_port}"
        logger.info(
            "[FlowKit MultiProfile] Auto-routing request to profile %s (%s) on %s (active: %d)",
            selected.id,
            selected.name,
            ep,
            len(connected),
        )
        return ep, selected.active_project_id, selected.id

    return None, None, None


class GenerateImageRequest(BaseModel):
    prompt: str = Field(
        ...,
        description="Mô tả chi tiết hình ảnh cần tạo.",
        examples=["Chân dung điện ảnh chiến binh cyberpunk giữa đường phố Tokyo đêm mưa, ánh đèn neon phản chiếu, chi tiết 8k"],
    )
    project_id: str = Field(
        default="",
        description="ID dự án Flow (để trống để tự động cấp phát session project).",
    )
    aspect_ratio: str = Field(
        default="IMAGE_ASPECT_RATIO_PORTRAIT",
        description="Tỉ lệ ảnh: IMAGE_ASPECT_RATIO_LANDSCAPE (16:9), IMAGE_ASPECT_RATIO_PORTRAIT (9:16), IMAGE_ASPECT_RATIO_SQUARE (1:1)",
        examples=["IMAGE_ASPECT_RATIO_LANDSCAPE"],
    )
    user_paygate_tier: str = Field(
        default="PAYGATE_TIER_ONE",
        description="Gói dịch vụ: PAYGATE_TIER_ONE hoặc PAYGATE_TIER_TWO.",
    )
    image_model: Optional[str] = Field(
        default=None,
        description="Model tạo ảnh: None (mặc định Nano Banana Pro), hoặc chỉ định tên model.",
    )
    count: int = Field(
        default=1,
        ge=1,
        le=4,
        description="Số lượng ảnh tạo ra (1 đến 4 ảnh).",
        examples=[1],
    )
    seed: Optional[int] = Field(
        default=None,
        ge=1,
        le=1_000_000_000,
        description="Seed ngẫu nhiên để cố định phong cách (tùy chọn).",
    )
    reference_media_ids: Optional[list[str]] = Field(
        default=None,
        description="Danh sách UUID các ảnh tham chiếu (nhân vật, phong cách).",
        examples=[["00000000-0000-0000-0000-000000000000"]],
    )
    character_media_ids: Optional[list[str]] = Field(
        default=None,
        description="Alias cũ cho reference_media_ids.",
    )
    auto_delogo: bool = Field(
        default=True,
        description="Tự động tải ảnh về và xóa watermark Google bằng thuật toán Lossless Reverse Alpha Blending.",
        examples=[True],
    )
    profile_id: Optional[str] = Field(
        default=None,
        description="ID profile Google Flow cụ thể (vd: 'profile_1', 'profile_2'). Nếu để trống, server sẽ tự động xoay vòng qua các profile đang kết nối.",
    )
    cdp_endpoint: Optional[str] = Field(
        default=None,
        description="CDP endpoint URL cụ thể (vd: 'http://127.0.0.1:9224').",
    )


class GeneratedImageItem(BaseModel):
    model_config = {"extra": "allow"}

    name: str = Field(
        ...,
        description="UUID định danh ảnh (Media ID).",
        examples=["0bb78ef5-0959-45e0-8270-b75fcf9f2bf8"],
    )
    image: Optional[dict] = Field(
        default=None,
        description="Dữ liệu ảnh từ Google Flow (chứa fifeUrl ảnh gốc).",
    )
    clean_url: Optional[str] = Field(
        default=None,
        description="Đường dẫn xem trực tiếp hoặc tải ảnh sạch đã xóa 100% watermark Google.",
        examples=["/api/flow/file?path=output%2Fflow%2Fimages%2F0bb78ef5-0959-45e0-8270-b75fcf9f2bf8_clean.jpg"],
    )
    clean_file_path: Optional[str] = Field(
        default=None,
        description="Đường dẫn file ảnh sạch trên máy chủ.",
        examples=["/Users/quyetnguyen/Documents/Me/Flowkit/output/flow/images/0bb78ef5-0959-45e0-8270-b75fcf9f2bf8_clean.jpg"],
    )
    original_file_path: Optional[str] = Field(
        default=None,
        description="Đường dẫn file ảnh gốc tải về từ Google Flow.",
    )
    watermark_removed: Optional[bool] = Field(
        default=False,
        description="Trạng thái xác nhận đã xóa watermark thành công.",
        examples=[True],
    )


class GenerateImageResponse(BaseModel):
    model_config = {"extra": "allow"}

    media: list[GeneratedImageItem] = Field(
        default_factory=list,
        description="Danh sách các biến thể ảnh được tạo ra.",
    )
    clean_url: Optional[str] = Field(
        default=None,
        description="Đường dẫn truy cập ảnh sạch của biến thể đầu tiên để dùng ngay.",
        examples=["/api/flow/file?path=output%2Fflow%2Fimages%2F0bb78ef5-0959-45e0-8270-b75fcf9f2bf8_clean.jpg"],
    )
    clean_file_path: Optional[str] = Field(
        default=None,
        description="Đường dẫn file ảnh sạch đầu tiên trên máy chủ.",
        examples=["/Users/quyetnguyen/Documents/Me/Flowkit/output/flow/images/0bb78ef5-0959-45e0-8270-b75fcf9f2bf8_clean.jpg"],
    )
    watermark_removed: Optional[bool] = Field(
        default=False,
        description="Xác nhận ảnh đã được xóa watermark thành công.",
        examples=[True],
    )
    requested_count: Optional[int] = Field(
        default=1,
        description="Số lượng ảnh yêu cầu tạo.",
        examples=[1],
    )
    generated_count: Optional[int] = Field(
        default=1,
        description="Số lượng ảnh thực tế đã tạo thành công.",
        examples=[1],
    )
    complete: Optional[bool] = Field(
        default=True,
        description="Cờ báo toàn bộ số lượng ảnh yêu cầu đã hoàn tất.",
        examples=[True],
    )


class GenerateVideoRequest(BaseModel):
    start_image_media_id: str = Field(
        ...,
        description="UUID ảnh khởi đầu (First Frame / Image-to-Video).",
        examples=["00000000-0000-0000-0000-000000000000"],
    )
    prompt: str = Field(
        ...,
        description="Mô tả chuyển động và diễn biến cảnh từ ảnh khởi đầu.",
        examples=["Nhân vật mỉm cười nhẹ nhàng và bước về phía máy quay, tuyết rơi chậm xung quanh."],
    )
    project_id: str = Field(
        default="",
        description="ID dự án Flow (để trống để tự động cấp phát).",
    )
    scene_id: str = Field(
        default="",
        description="ID phân cảnh (tùy chọn).",
    )
    aspect_ratio: str = Field(
        default="VIDEO_ASPECT_RATIO_PORTRAIT",
        description="Tỉ lệ video: VIDEO_ASPECT_RATIO_PORTRAIT (9:16) hoặc VIDEO_ASPECT_RATIO_LANDSCAPE (16:9).",
        examples=["VIDEO_ASPECT_RATIO_PORTRAIT"],
    )
    end_image_media_id: Optional[str] = Field(
        default=None,
        description="UUID ảnh kết thúc (Last Frame - tùy chọn để tạo chuyển cảnh mượt mà giữa 2 frame).",
    )
    user_paygate_tier: str = Field(
        default="PAYGATE_TIER_ONE",
    )
    model_family: Literal["veo", "omni_flash"] = Field(
        default="omni_flash",
        description="Mô hình video: omni_flash (khuyên dùng) hoặc veo.",
        examples=["omni_flash"],
    )
    duration_s: int = Field(
        default=8,
        description="Thời lượng video: 4, 6, 8, hoặc 10 giây.",
        examples=[8],
    )
    resolution: Literal["360p", "720p"] = Field(
        default="720p",
        description="Độ phân giải video ban đầu (720p hoặc 360p).",
        examples=["720p"],
    )
    profile_id: Optional[str] = Field(
        default=None,
        description="ID profile Google Flow cụ thể (vd: 'profile_1', 'profile_2'). Nếu để trống, server sẽ tự động xoay vòng qua các profile đang kết nối.",
    )
    cdp_endpoint: Optional[str] = Field(
        default=None,
        description="CDP endpoint URL cụ thể (vd: 'http://127.0.0.1:9224').",
    )


class GenerateVideoRefsRequest(BaseModel):
    reference_media_ids: list[str] = Field(
        ...,
        description="Danh sách UUID các ảnh tham chiếu nhân vật/bối cảnh cần xuất hiện trong video.",
        examples=[["00000000-0000-0000-0000-000000000000"]],
    )
    prompt: str = Field(
        ...,
        description="Mô tả hành động của nhân vật trong video.",
        examples=["Nhân vật bước đi tự tin trong khu phố ánh sáng sầm uất."],
    )
    project_id: str = Field(
        default="",
    )
    scene_id: str = Field(
        default="",
    )
    aspect_ratio: str = Field(
        default="VIDEO_ASPECT_RATIO_PORTRAIT",
        description="Tỉ lệ video: VIDEO_ASPECT_RATIO_PORTRAIT (9:16) hoặc VIDEO_ASPECT_RATIO_LANDSCAPE (16:9).",
        examples=["VIDEO_ASPECT_RATIO_LANDSCAPE"],
    )
    user_paygate_tier: str = Field(
        default="PAYGATE_TIER_ONE",
    )
    model_family: Literal["veo", "omni_flash"] = Field(
        default="omni_flash",
        examples=["omni_flash"],
    )
    duration_s: int = Field(
        default=8,
        examples=[8],
    )
    resolution: Literal["360p", "720p"] = Field(
        default="720p",
        examples=["720p"],
    )
    profile_id: Optional[str] = Field(
        default=None,
        description="ID profile Google Flow cụ thể (vd: 'profile_1', 'profile_2'). Nếu để trống, server sẽ tự động xoay vòng qua các profile đang kết nối.",
    )
    cdp_endpoint: Optional[str] = Field(
        default=None,
        description="CDP endpoint URL cụ thể (vd: 'http://127.0.0.1:9224').",
    )


class GenerateOmniFlashVideoRequest(BaseModel):
    reference_media_ids: list[str] = Field(
        ...,
        description="Danh sách UUID các ảnh tham chiếu.",
    )
    prompt: str = Field(
        ...,
        description="Prompt mô tả video.",
    )
    project_id: str = Field(
        default="",
    )
    scene_id: str = Field(
        default="",
    )
    duration_s: int = Field(
        default=8,
        examples=[8],
    )
    resolution: Literal["360p", "720p"] = Field(
        default="720p",
        examples=["720p"],
    )
    aspect_ratio: str = Field(
        default="VIDEO_ASPECT_RATIO_PORTRAIT",
        examples=["VIDEO_ASPECT_RATIO_PORTRAIT"],
    )
    user_paygate_tier: str = Field(
        default="PAYGATE_TIER_ONE",
    )
    profile_id: Optional[str] = Field(
        default=None,
        description="ID profile Google Flow cụ thể (vd: 'profile_1', 'profile_2'). Nếu để trống, server sẽ tự động xoay vòng qua các profile đang kết nối.",
    )
    cdp_endpoint: Optional[str] = Field(
        default=None,
        description="CDP endpoint URL cụ thể (vd: 'http://127.0.0.1:9224').",
    )


class GenerateOmniFlashTextVideoRequest(BaseModel):
    prompt: str = Field(
        ...,
        description="Prompt kịch bản hành động cho video (khuyên dùng cấu trúc thời gian: 0-3s: [hành động]. 3-6s: [hành động]. 6-8s: [kết thúc]).",
        examples=["0-3s: Siêu xe thể thao màu đỏ tăng tốc lao qua đại lộ ánh sáng ban đêm. 3-8s: Góc máy quay lia từ trên cao xuống toàn cảnh thành phố tương lai."],
    )
    project_id: str = Field(
        default="",
        description="ID dự án Flow (để trống để tự động cấp phát).",
    )
    scene_id: str = Field(
        default="",
    )
    duration_s: int = Field(
        default=8,
        description="Thời lượng video (4, 6, 8, hoặc 10 giây).",
        examples=[8],
    )
    resolution: Literal["360p", "720p"] = Field(
        default="720p",
        description="Độ phân giải: 720p hoặc 360p.",
        examples=["720p"],
    )
    aspect_ratio: str = Field(
        default="VIDEO_ASPECT_RATIO_PORTRAIT",
        description="Tỉ lệ khung hình: VIDEO_ASPECT_RATIO_PORTRAIT (9:16) hoặc VIDEO_ASPECT_RATIO_LANDSCAPE (16:9).",
        examples=["VIDEO_ASPECT_RATIO_LANDSCAPE"],
    )
    user_paygate_tier: str = Field(
        default="PAYGATE_TIER_ONE",
    )
    profile_id: Optional[str] = Field(
        default=None,
        description="ID profile Google Flow cụ thể (vd: 'profile_1', 'profile_2'). Nếu để trống, server sẽ tự động xoay vòng qua các profile đang kết nối.",
    )
    cdp_endpoint: Optional[str] = Field(
        default=None,
        description="CDP endpoint URL cụ thể (vd: 'http://127.0.0.1:9224').",
    )


class UpscaleVideoRequest(BaseModel):
    media_id: str = Field(
        ...,
        description="UUID của video cần nâng cấp độ phân giải.",
        examples=["00000000-0000-0000-0000-000000000000"],
    )
    scene_id: str = Field(
        default="",
    )
    aspect_ratio: str = Field(
        default="VIDEO_ASPECT_RATIO_PORTRAIT",
    )
    resolution: str = Field(
        default="VIDEO_RESOLUTION_4K",
        description="Độ phân giải mục tiêu: VIDEO_RESOLUTION_4K hoặc VIDEO_RESOLUTION_1080P.",
        examples=["VIDEO_RESOLUTION_4K"],
    )
    project_id: Optional[str] = None


class GenerateVideoFullRequest(BaseModel):
    prompt: str = Field(
        ...,
        description="Mô tả chi tiết nội dung và chuyển động của video (khuyên dùng cấu trúc thời gian: 0-3s: [hành động], 3-6s: [hành động]).",
        examples=["0-3s: Chiến binh bước đi giữa thành phố cyberpunk mưa rơi neon. 3-8s: Máy quay xoay quanh nhân vật rồi lia lên bầu trời tương lai."],
    )
    start_image_media_id: Optional[str] = Field(
        default=None,
        description="UUID ảnh khởi đầu (First frame / Image-to-Video). Để trống để tạo trực tiếp từ chữ (Text-to-Video).",
        examples=["00000000-0000-0000-0000-000000000000"],
    )
    end_image_media_id: Optional[str] = Field(
        default=None,
        description="UUID ảnh kết thúc (Last frame - tùy chọn để morph chuyển cảnh giữa 2 ảnh).",
    )
    reference_media_ids: Optional[list[str]] = Field(
        default=None,
        description="Danh sách UUID các ảnh tham chiếu nhân vật/bối cảnh (Reference-to-Video).",
    )
    project_id: str = Field(
        default="",
        description="ID dự án Flow (để trống để tự động cấp phát session project).",
    )
    scene_id: str = Field(
        default="",
        description="ID phân cảnh (tùy chọn).",
    )
    aspect_ratio: str = Field(
        default="VIDEO_ASPECT_RATIO_PORTRAIT",
        description="Tỉ lệ video: VIDEO_ASPECT_RATIO_PORTRAIT (9:16) hoặc VIDEO_ASPECT_RATIO_LANDSCAPE (16:9).",
        examples=["VIDEO_ASPECT_RATIO_PORTRAIT"],
    )
    duration_s: int = Field(
        default=8,
        description="Thời lượng video (4, 6, 8, hoặc 10 giây).",
        examples=[8],
    )
    resolution: Literal["360p", "720p"] = Field(
        default="720p",
        description="Độ phân giải video ban đầu (720p hoặc 360p).",
        examples=["720p"],
    )
    model_family: Literal["omni_flash", "veo"] = Field(
        default="omni_flash",
        description="Mô hình video: omni_flash (khuyên dùng, nhanh và đẹp) hoặc veo.",
        examples=["omni_flash"],
    )
    user_paygate_tier: str = Field(
        default="PAYGATE_TIER_ONE",
    )
    auto_delogo: bool = Field(
        default=True,
        description="Tự động tải video về và xóa watermark Google bằng thuật toán Lossless Reverse Alpha Blending.",
        examples=[True],
    )
    timeout_seconds: int = Field(
        default=480,
        ge=30,
        le=1200,
        description="Thời gian tối đa chờ render hoàn tất (giây).",
        examples=[480],
    )
    profile_id: Optional[str] = Field(
        default=None,
        description="ID profile Google Flow cụ thể (vd: 'profile_1', 'profile_2').",
    )
    cdp_endpoint: Optional[str] = Field(
        default=None,
        description="CDP endpoint URL cụ thể (vd: 'http://127.0.0.1:9224').",
    )


class GenerateVideoFullResponse(BaseModel):
    model_config = {"extra": "allow"}

    status: str = Field(
        description="Trạng thái hoàn thành: COMPLETED hoặc FAILED.",
        examples=["COMPLETED"],
    )
    media_id: Optional[str] = Field(
        default=None,
        description="UUID video được tạo bởi Google Flow.",
        examples=["9592f75a-38c6-47b7-872e-c534440c4ec3"],
    )
    clean_url: str = Field(
        description="URL xem trực tiếp hoặc tải video sạch đã xóa sạch 100% watermark.",
        examples=["/api/flow/file?path=output%2Fflow%2Fvideos%2F9592f75a-38c6-47b7-872e-c534440c4ec3_clean.mp4"],
    )
    clean_file_path: str = Field(
        description="Đường dẫn file video sạch lưu trên ổ đĩa máy chủ.",
        examples=["/Users/quyetnguyen/Documents/Me/Flowkit/output/flow/videos/9592f75a-38c6-47b7-872e-c534440c4ec3_clean.mp4"],
    )
    original_url: Optional[str] = Field(
        default=None,
        description="URL video gốc trực tiếp từ CDN của Google Flow.",
        examples=["https://flow-content.google/..."],
    )
    original_file_path: str = Field(
        description="Đường dẫn file video gốc đã tải về máy chủ.",
        examples=["/Users/quyetnguyen/Documents/Me/Flowkit/output/flow/videos/9592f75a-38c6-47b7-872e-c534440c4ec3.mp4"],
    )
    watermark_removed: bool = Field(
        description="Trạng thái xác nhận đã xóa watermark bằng Reverse Alpha Blending.",
        examples=[True],
    )
    elapsed_seconds: float = Field(
        description="Tổng thời gian thực thi toàn bộ pipeline từ lúc gửi prompt tới khi có video sạch (giây).",
        examples=[68.2],
    )
    duration_s: int = Field(
        description="Thời lượng video (giây).",
        examples=[8],
    )
    aspect_ratio: str = Field(
        description="Tỉ lệ khung hình của video.",
        examples=["VIDEO_ASPECT_RATIO_PORTRAIT"],
    )
    resolution: str = Field(
        description="Độ phân giải video.",
        examples=["720p"],
    )


class RemoveWatermarkRequest(BaseModel):
    file_path: Optional[str] = Field(
        default=None,
        description="Đường dẫn file media trên server (ảnh hoặc video).",
        examples=["output/flow/videos/sample.mp4"],
    )
    url: Optional[str] = Field(
        default=None,
        description="URL tải media (Google Flow FifeUrl hoặc direct URL) để server tự động tải về và xóa watermark.",
    )
    media_id: Optional[str] = Field(
        default=None,
        description="UUID media trên Google Flow để tự động lấy link tải và xóa watermark.",
    )
    is_video: Optional[bool] = Field(
        default=None,
        description="Chỉ định rõ media là video (True) hay ảnh (False). Để trống để tự nhận diện theo phần mở rộng.",
    )


class RemoveWatermarkResponse(BaseModel):
    model_config = {"extra": "allow"}

    status: str = Field(
        description="Trạng thái hoàn thành: COMPLETED hoặc FAILED.",
        examples=["COMPLETED"],
    )
    clean_url: str = Field(
        description="URL xem trực tiếp hoặc tải file media sạch sau khi xóa watermark.",
        examples=["/api/flow/file?path=output%2Fflow%2Fvideos%2Fsample_clean.mp4"],
    )
    clean_file_path: str = Field(
        description="Đường dẫn file sạch lưu trên ổ đĩa máy chủ.",
        examples=["/Users/quyetnguyen/Documents/Me/Flowkit/output/flow/videos/sample_clean.mp4"],
    )
    original_file_path: str = Field(
        description="Đường dẫn file gốc trước khi xử lý xóa watermark.",
        examples=["/Users/quyetnguyen/Documents/Me/Flowkit/output/flow/videos/sample.mp4"],
    )
    watermark_removed: bool = Field(
        description="Xác nhận đã xóa watermark thành công.",
        examples=[True],
    )
    is_video: bool = Field(
        description="Loại media: true nếu là video, false nếu là ảnh.",
        examples=[True],
    )


class UploadImageRequest(BaseModel):
    image_base64: Optional[str] = Field(
        default=None,
        description=(
            "Recommended for external/API callers. Base64-encoded image bytes; "
            "avoids filesystem namespace and permission issues."
        ),
    )
    file_path: Optional[str] = Field(
        default=None,
        description=(
            "Server-local convenience mode only. The path is opened by the FlowKit "
            "service user and must be visible/readable inside its systemd namespace. "
            "Caller-local /tmp and protected home paths may not be accessible."
        ),
    )
    mime_type: Optional[str] = Field(
        default=None,
        description="Optional MIME override; otherwise inferred from file_name/file_path.",
    )
    project_id: str = Field(
        default="",
        description="Existing Flow project id, or empty to use/create the session project.",
    )
    file_name: str = Field(default="image.png", description="Filename sent to Google Flow.")
    profile_id: Optional[str] = Field(
        default=None,
        description="ID profile Google Flow cụ thể (vd: 'profile_1', 'profile_2').",
    )
    cdp_endpoint: Optional[str] = Field(
        default=None,
        description="CDP endpoint URL cụ thể (vd: 'http://127.0.0.1:9224').",
    )


class CheckStatusRequest(BaseModel):
    operations: list[dict] = []
    # Omni/workflow-mode callers should pass workflow descriptors instead of
    # operation handles. If workflows is set, /check-status automatically uses
    # authenticated Flow project polling.
    workflows: Optional[list[dict]] = None
    project_id: str = ""
    include_encoded_video: bool = False


class CheckOmniStatusRequest(BaseModel):
    workflows: list[dict]
    project_id: str = ""
    include_encoded_video: bool = False


class EditImageRequest(BaseModel):
    prompt: str
    source_media_id: str
    project_id: str
    aspect_ratio: str = "IMAGE_ASPECT_RATIO_PORTRAIT"
    user_paygate_tier: str = "PAYGATE_TIER_ONE"
    image_model: Optional[str] = None
    count: int = Field(default=1, ge=1, le=4)
    seed: Optional[int] = Field(default=None, ge=1, le=1_000_000_000)
    reference_media_ids: Optional[list[str]] = None


class UpscaleImageRequest(BaseModel):
    media_id: str
    project_id: str
    quality: Literal["2k", "4k"] = "2k"


def _attach_credit_estimate(data: object, snapshot: dict, cost: int | None):
    if isinstance(data, dict):
        data["credits"] = credit_response(snapshot, cost)
    debit_cached_flow_credits(cost)
    return data


async def _resolve_direct_project(client, project_id: str) -> str:
    """Use an explicit project, otherwise fall back to FLOW_PROJECT_ID, otherwise reuse/create session project."""
    pid = str(project_id or "").strip()
    if pid:
        return pid
    if FLOW_PROJECT_ID:
        client._batch_active_project = FLOW_PROJECT_ID
        return FLOW_PROJECT_ID
    try:
        session = await ensure_session_project(client)
    except Exception as exc:
        raise HTTPException(502, f"Could not create Flow session project: {exc}") from exc
    pid = str(session.get("project_id") or "")
    if not pid:
        raise HTTPException(502, "Flow session project did not return an id")
    return pid


@router.get("/status", tags=[TAG_STATUS], summary="Kiểm tra trạng thái kết nối Extension & Chrome")
async def extension_status():
    """Report transport state and the real signed-in Flow browser session."""
    client = get_flow_client()
    session = await inspect_flow_session() if client.connected else {}
    signed_in = bool(session.get("signedIn")) if isinstance(session, dict) else False
    return {
        "connected": client.connected,
        "transport": "batch" if USE_BATCH_RPC else "legacy_rest",
        "flow_project_id": FLOW_PROJECT_ID or None,
        "allow_degraded": FLOW_ALLOW_DEGRADED,
        "authenticated": signed_in,
        "auth_state": "AUTHENTICATED" if signed_in else session.get("state", "UNKNOWN"),
        "flow_tab_present": bool(session.get("flowTabPresent")),
        "at_token_present": bool(session.get("atTokenPresent")),
        "session_url": session.get("url"),
        "flow_key_present": client._flow_key is not None,
        "legacy_flow_key_authoritative": not USE_BATCH_RPC,
        "generation_throttle": {
            "min_interval_s": FLOW_GENERATION_MIN_INTERVAL_S,
            "max_concurrent": FLOW_GENERATION_MAX_CONCURRENT,
            "unusual_activity_cooldown_s": FLOW_UNUSUAL_ACTIVITY_COOLDOWN_S,
            **client.generation_guard_status,
        },
        "session_project": current_session_project(),
        "payload_drift": payload_drift_status(),
    }


@router.post("/clear-hijack", tags=[TAG_STATUS], summary="Xóa cooldown hijack để tiếp tục tạo ngay")
async def clear_hijack_cooldown():
    """Reset the generation cooldown triggered by extension_hijack_detected.

    Call this after deploying the bypass fix to immediately resume generation
    without waiting for the cooldown to expire.
    """
    import time as _time
    client = get_flow_client()
    old_until = client._generation_unusual_until
    client._generation_unusual_until = 0.0
    was_active = old_until > 0.0 and old_until > _time.monotonic()
    return {
        "cleared": True,
        "was_active": was_active,
    }


@router.post("/ensure-session", tags=[TAG_STATUS], summary="Phục hồi phiên đăng nhập tài khoản Flow")
async def ensure_session():
    """Reopen/reload Flow in the persistent Google profile and re-check login."""
    client = get_flow_client()
    if not client.connected:
        raise HTTPException(503, "Extension not connected")
    session = await ensure_flow_session()
    return {
        "authenticated": bool(session.get("signedIn")),
        "auth_state": session.get("state", "UNKNOWN"),
        "recovered": bool(session.get("recovered")),
        "flow_tab_present": bool(session.get("flowTabPresent")),
        "at_token_present": bool(session.get("atTokenPresent")),
        "session_url": session.get("url"),
        "interactive_login_required": session.get("state") == "INTERACTIVE_LOGIN_REQUIRED",
    }


@router.get("/account", tags=[TAG_STATUS], summary="Lấy thông tin tài khoản Google đang đăng nhập")
async def get_account():
    """Return the signed-in Flow Google account name/email, never auth secrets."""
    client = get_flow_client()
    if not client.connected:
        raise HTTPException(503, "Extension not connected")
    return await inspect_google_account()


@router.get("/credits", tags=[TAG_STATUS], summary="Kiểm tra số dư credit Google Flow")
async def get_credits(refresh: bool = False):
    """Get the real visible Google Flow credit balance."""
    client = get_flow_client()
    if not client.connected:
        raise HTTPException(503, "Extension not connected")
    result = await client.get_credits(refresh=refresh)
    if result.get("error"):
        raise HTTPException(502, result["error"])
    return result.get("data", result)


@router.get("/image-capabilities", tags=[TAG_IMAGE], summary="Tra cứu danh sách model ảnh và tỉ lệ khung hình")
async def get_image_capabilities(refresh: bool = False):
    """List current image models, aspect ratios, count and upscale targets."""
    return await image_capabilities(refresh=refresh)


@router.post(
    "/generate-image",
    response_model=GenerateImageResponse,
    tags=[TAG_IMAGE],
    summary="Tạo ảnh mới với AI (Nano Banana 2 / Pro)",
)
async def generate_image(body: GenerateImageRequest):
    """Generate 1-4 images with an explicit or dynamically discovered model.

    When auto_delogo=True (default), downloads the generated image(s) and
    removes the Google watermark using Lossless Reverse Alpha Blending.
    """
    client = get_flow_client()
    target_cdp, target_pid, profile_id = await resolve_profile_target(body.profile_id, body.cdp_endpoint, body.project_id)
    if not client.connected and not target_cdp:
        raise HTTPException(503, "Không có Chrome profile hoặc extension nào đang kết nối. Vui lòng mở Chrome hoặc kiểm tra lại tab Profiles.")
    project_id = await _resolve_direct_project(client, body.project_id or target_pid or "")
    data = body.model_dump(exclude={"reference_media_ids", "auto_delogo", "profile_id", "cdp_endpoint"})
    data["project_id"] = project_id
    if target_cdp:
        data["cdp_endpoint"] = target_cdp
    refs = list(dict.fromkeys((body.reference_media_ids or []) + (body.character_media_ids or [])))
    data["character_media_ids"] = refs or None
    result = await client.generate_images(**data)
    if result.get("error") or (isinstance(result.get("status"), int) and result["status"] >= 400):
        err_msg = str(result.get("error", result.get("data")))
        await profile_manager.report_profile_failure(profile_id, err_msg, auto_stop=True)
        raise HTTPException(result.get("status", 502), err_msg)
    profile_manager.report_profile_success(profile_id)
    res_data = result.get("data", result)

    if body.auto_delogo and isinstance(res_data, dict):
        media_list = res_data.get("media") or []
        FLOW_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        for item in media_list:
            if not isinstance(item, dict):
                continue
            media_id = item.get("name")
            gen_img = item.get("image", {}).get("generatedImage") if isinstance(item.get("image"), dict) else {}
            download_url = gen_img.get("fifeUrl") or item.get("fifeUrl") or item.get("url")
            if not download_url and media_id:
                download_url = await poll_media_download_url(client, media_id, max_wait_seconds=15, poll_interval=2.0)

            if download_url and media_id:
                orig_file = FLOW_IMAGES_DIR / f"{media_id}.jpg"
                clean_file = FLOW_IMAGES_DIR / f"{media_id}_clean.jpg"
                try:
                    await asyncio.to_thread(download_media_file, download_url, str(orig_file))
                    cleaned = await asyncio.to_thread(run_delogo, str(orig_file), str(clean_file), False)
                    target_file = clean_file if cleaned else orig_file
                    item["clean_file_path"] = str(target_file)
                    item["clean_url"] = f"/api/flow/file?path={quote(str(target_file))}"
                    item["watermark_removed"] = bool(cleaned)
                    item["original_file_path"] = str(orig_file)
                except Exception as exc:
                    logger.warning("Auto delogo failed for image %s: %s", media_id, exc)
                    item["watermark_removed"] = False

        if media_list and isinstance(media_list[0], dict):
            first = media_list[0]
            if "clean_url" in first:
                res_data["clean_url"] = first["clean_url"]
                res_data["clean_file_path"] = first.get("clean_file_path")
                res_data["watermark_removed"] = first.get("watermark_removed", False)

    return res_data


@router.post("/generate-video", tags=[TAG_VIDEO], summary="Tạo video từ ảnh (First frame / First+Last frames)")
async def generate_video(body: GenerateVideoRequest):
    """Submit frame-conditioned video generation using Veo or Omni Flash.

    Existing callers default to Veo. For Omni set ``model_family=omni_flash``
    and ``duration_s`` to 4/6/8/10. With only ``start_image_media_id`` the
    request uses Omni First frame. When ``end_image_media_id`` is also present,
    it uses Omni First+Last frames.

    On the migrated batch transport, Omni frame-conditioned responses return
    ``flowkitPolling.mode=batch_operation`` and are polled through ``/check-status``.
    """
    client = get_flow_client()
    target_cdp, target_pid, profile_id = await resolve_profile_target(body.profile_id, body.cdp_endpoint, body.project_id)
    if not client.connected and not target_cdp:
        raise HTTPException(503, "Không có Chrome profile hoặc extension nào đang kết nối. Vui lòng mở Chrome hoặc kiểm tra lại tab Profiles.")
    project_id = await _resolve_direct_project(client, body.project_id or target_pid or "")
    credit_snapshot = await inspect_flow_credits()

    if body.model_family == "omni_flash":
        try:
            common = dict(
                start_image_media_id=body.start_image_media_id,
                prompt=body.prompt,
                project_id=project_id,
                scene_id=body.scene_id,
                duration_s=body.duration_s,
                resolution=body.resolution,
                aspect_ratio=body.aspect_ratio,
                user_paygate_tier=body.user_paygate_tier,
                cdp_endpoint=target_cdp,
            )
            if body.end_image_media_id:
                result = await generate_omni_flash_first_last_video(
                    end_image_media_id=body.end_image_media_id,
                    **common,
                )
            else:
                result = await generate_omni_flash_first_frame_video(**common)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        payload = body.model_dump(
            exclude={"model_family", "duration_s", "resolution", "profile_id", "cdp_endpoint"}, exclude_none=True
        )
        payload["project_id"] = project_id
        if target_cdp:
            payload["cdp_endpoint"] = target_cdp
        result = await client.generate_video(**payload)

    if result.get("error") or (isinstance(result.get("status"), int) and result["status"] >= 400):
        err_msg = str(result.get("error", result.get("data")))
        await profile_manager.report_profile_failure(profile_id, err_msg, auto_stop=True)
        raise HTTPException(result.get("status", 502), err_msg)
    profile_manager.report_profile_success(profile_id)

    data = result.get("data", result)
    veo_model = None
    if body.model_family == "veo":
        gen_type = "start_end_frame_2_video" if body.end_image_media_id else "frame_2_video"
        veo_model = client._batch_video_model(
            body.user_paygate_tier, gen_type, body.aspect_ratio
        )
    cost = estimate_video_generation_cost(
        model_family=body.model_family,
        duration_s=body.duration_s,
        resolution=body.resolution,
        model_key=veo_model,
        plan=credit_snapshot.get("plan"),
    )
    return _attach_credit_estimate(data, credit_snapshot, cost)


@router.post("/generate-video-refs", tags=[TAG_VIDEO], summary="Tạo video từ ảnh tham chiếu (Reference-to-Video)")
async def generate_video_refs(body: GenerateVideoRefsRequest):
    """Submit reference-to-video generation using Veo or Gemini Omni Flash.

    Existing requests default to ``model_family=veo``. Set
    ``model_family=omni_flash`` and ``duration_s`` to 4/6/8/10 to use Omni.
    Migrated Omni Ingredients/R2V returns ``flowkitPolling.mode=batch_operation``;
    poll its operations through ``/check-status``.
    """
    client = get_flow_client()
    target_cdp, target_pid, profile_id = await resolve_profile_target(body.profile_id, body.cdp_endpoint, body.project_id)
    if not client.connected and not target_cdp:
        raise HTTPException(503, "Không có Chrome profile hoặc extension nào đang kết nối. Vui lòng mở Chrome hoặc kiểm tra lại tab Profiles.")
    project_id = await _resolve_direct_project(client, body.project_id or target_pid or "")
    credit_snapshot = await inspect_flow_credits()

    if body.model_family == "omni_flash":
        try:
            result = await generate_omni_flash_video(
                reference_media_ids=body.reference_media_ids,
                prompt=body.prompt,
                project_id=project_id,
                scene_id=body.scene_id,
                duration_s=body.duration_s,
                resolution=body.resolution,
                aspect_ratio=body.aspect_ratio,
                user_paygate_tier=body.user_paygate_tier,
                cdp_endpoint=target_cdp,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        payload = body.model_dump(exclude={"model_family", "duration_s", "resolution", "profile_id", "cdp_endpoint"})
        payload["project_id"] = project_id
        if target_cdp:
            payload["cdp_endpoint"] = target_cdp
        result = await client.generate_video_from_references(**payload)

    if result.get("error") or (isinstance(result.get("status"), int) and result["status"] >= 400):
        err_msg = str(result.get("error", result.get("data")))
        await profile_manager.report_profile_failure(profile_id, err_msg, auto_stop=True)
        raise HTTPException(result.get("status", 502), err_msg)
    profile_manager.report_profile_success(profile_id)

    data = result.get("data", result)
    veo_model = None
    if body.model_family == "veo" and body.reference_media_ids:
        veo_model = client._batch_video_model(
            body.user_paygate_tier, "reference_frame_2_video", body.aspect_ratio
        )
    cost = estimate_video_generation_cost(
        model_family=body.model_family,
        duration_s=body.duration_s,
        resolution=body.resolution,
        model_key=veo_model,
        plan=credit_snapshot.get("plan"),
    )
    return _attach_credit_estimate(data, credit_snapshot, cost)


@router.post("/generate-video-omni-text", tags=[TAG_VIDEO], summary="Tạo video từ văn bản (Omni Flash Text-to-Video 4s-10s)")
async def generate_video_omni_text(body: GenerateOmniFlashTextVideoRequest):
    """Submit Omni 1.1 Flash text-to-video on flow.google.com.

    Durations 4/6/8/10 seconds map to Flow's ``abra_t2v_<N>s`` models.
    """
    client = get_flow_client()
    target_cdp, target_pid, profile_id = await resolve_profile_target(body.profile_id, body.cdp_endpoint, body.project_id)
    if not client.connected and not target_cdp:
        raise HTTPException(503, "Không có Chrome profile hoặc extension nào đang kết nối. Vui lòng mở Chrome hoặc kiểm tra lại tab Profiles.")
    project_id = await _resolve_direct_project(client, body.project_id or target_pid or "")
    credit_snapshot = await inspect_flow_credits()
    try:
        payload = body.model_dump(exclude={"profile_id", "cdp_endpoint"})
        payload["project_id"] = project_id
        if target_cdp:
            payload["cdp_endpoint"] = target_cdp
        result = await generate_omni_flash_text_video(**payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if result.get("error") or (
        isinstance(result.get("status"), int) and result["status"] >= 400
    ):
        err_msg = str(result.get("error", result.get("data")))
        await profile_manager.report_profile_failure(profile_id, err_msg, auto_stop=True)
        raise HTTPException(
            result.get("status", 502),
            err_msg,
        )
    profile_manager.report_profile_success(profile_id)
    data = result.get("data", result)
    cost = estimate_video_generation_cost(
        model_family="omni_flash",
        duration_s=body.duration_s,
        resolution="720p",
        plan=credit_snapshot.get("plan"),
    )
    return _attach_credit_estimate(data, credit_snapshot, cost)


@router.post("/generate-video-omni", tags=[TAG_VIDEO], summary="Tạo video Gemini Omni Flash")
async def generate_video_omni(body: GenerateOmniFlashVideoRequest):
    """Submit Gemini Omni Flash reference-to-video generation.

    The response includes ``flowkitPolling.workflows`` for the correct
    workflow/media polling path.
    """
    client = get_flow_client()
    target_cdp, target_pid, profile_id = await resolve_profile_target(body.profile_id, body.cdp_endpoint, body.project_id)
    if not client.connected and not target_cdp:
        raise HTTPException(503, "Không có Chrome profile hoặc extension nào đang kết nối. Vui lòng mở Chrome hoặc kiểm tra lại tab Profiles.")
    project_id = await _resolve_direct_project(client, body.project_id or target_pid or "")
    credit_snapshot = await inspect_flow_credits()
    try:
        payload = body.model_dump(exclude={"profile_id", "cdp_endpoint"})
        payload["project_id"] = project_id
        if target_cdp:
            payload["cdp_endpoint"] = target_cdp
        result = await generate_omni_flash_video(**payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if result.get("error") or (isinstance(result.get("status"), int) and result["status"] >= 400):
        err_msg = str(result.get("error", result.get("data")))
        await profile_manager.report_profile_failure(profile_id, err_msg, auto_stop=True)
        raise HTTPException(result.get("status", 502), err_msg)
    profile_manager.report_profile_success(profile_id)
    data = result.get("data", result)
    cost = estimate_video_generation_cost(
        model_family="omni_flash",
        duration_s=body.duration_s,
        resolution=body.resolution,
        plan=credit_snapshot.get("plan"),
    )
    return _attach_credit_estimate(data, credit_snapshot, cost)


@router.post("/upscale-video", tags=[TAG_VIDEO], summary="Nâng cấp độ phân giải video (1080p / 4K)")
async def upscale_video(body: UpscaleVideoRequest):
    """Submit video upscale (returns operations for polling)."""
    client = get_flow_client()
    if not client.connected:
        raise HTTPException(503, "Extension not connected")
    result = await client.upscale_video(**body.model_dump())
    if result.get("error") or (isinstance(result.get("status"), int) and result["status"] >= 400):
        raise HTTPException(result.get("status", 502), result.get("error", result.get("data")))
    return result.get("data", result)


@router.post("/check-status", tags=[TAG_VIDEO], summary="Kiểm tra trạng thái render video/ảnh (Polling)")
async def check_status(body: CheckStatusRequest):
    """Check Veo operation status or Omni workflow/media status.

    Veo: pass ``operations``.
    Omni Flash: pass ``workflows`` from submit ``flowkitPolling.workflows``.
    """
    client = get_flow_client()
    if not client.connected:
        raise HTTPException(503, "Extension not connected")

    if body.workflows:
        try:
            return await check_omni_flash_status(
                body.workflows,
                include_encoded_video=body.include_encoded_video,
                project_id=body.project_id,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc

    if not body.operations:
        raise HTTPException(400, "Provide operations for Veo or workflows for Omni Flash")

    result = await client.check_video_status(body.operations)
    if result.get("error"):
        raise HTTPException(502, result["error"])
    if isinstance(result.get("status"), int) and result["status"] >= 400:
        raise HTTPException(result["status"], result.get("data", "Flow polling failed"))
    return result.get("data", result)


@router.post("/check-omni-status", tags=[TAG_VIDEO], summary="Kiểm tra trạng thái job Omni Flash")
async def check_omni_status(body: CheckOmniStatusRequest):
    """Poll Gemini Omni Flash jobs via workflow primary media IDs."""
    client = get_flow_client()
    if not client.connected:
        raise HTTPException(503, "Extension not connected")
    try:
        return await check_omni_flash_status(
            body.workflows,
            include_encoded_video=body.include_encoded_video,
            project_id=body.project_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/refresh-urls/{project_id}", tags=[TAG_IMAGE, TAG_VIDEO], summary="Làm mới URL tải toàn bộ media trong project")
async def refresh_project_urls(project_id: str):
    """Bulk refresh all media URLs for a project via per-media get_media calls.

    Provider-neutral no-op: when every stored URL is local (file://), there is
    nothing to re-sign and the extension is not required.
    """
    from agent.db import crud

    urls: list[str] = []
    for video in await crud.list_videos(project_id):
        for scene in await crud.list_scenes(video["id"]):
            for k, v in scene.items():
                if k.endswith("_url") and v:
                    urls.append(v)
    for char in await crud.get_project_characters(project_id):
        for k, v in char.items():
            if k.endswith("_url") and v:
                urls.append(v)

    if not any(u.startswith("http") for u in urls):
        return {"refreshed": 0, "found": len(urls),
                "skipped": "no remote media URLs (all local or none); nothing to refresh"}

    client = get_flow_client()
    if not client.connected:
        raise HTTPException(503, "Extension not connected")
    result = await client.refresh_project_urls(project_id)
    if result.get("error"):
        raise HTTPException(502, result["error"])
    return result


@router.get("/media/{media_id}", tags=[TAG_IMAGE, TAG_VIDEO], summary="Lấy metadata và URL tải media theo UUID")
async def get_media(media_id: str):
    """Get media metadata + fresh signed URL from Google Flow.

    Returns the raw response which may contain ``video.encodedVideo`` for
    workflow-backed video generations.
    """
    client = get_flow_client()
    if not client.connected:
        raise HTTPException(503, "Extension not connected")
    result = await client.get_media(media_id)
    if result.get("error"):
        raise HTTPException(502, result["error"])
    status = result.get("status", 200)
    if isinstance(status, int) and status >= 400:
        raise HTTPException(status, result.get("data", "Media not found"))
    data = result.get("data", result)
    if isinstance(data, dict):
        video_url = data.get("video", {}).get("fifeUrl") if isinstance(data.get("video"), dict) else None
        image_url = data.get("image", {}).get("fifeUrl") if isinstance(data.get("image"), dict) else None
        data.setdefault("url", video_url or image_url)
    return data


@router.post("/edit-image", tags=[TAG_IMAGE], summary="Chỉnh sửa ảnh có sẵn (Inpainting / Edits)")
async def edit_image(body: EditImageRequest):
    """Edit an existing image using the current Flow BASE_IMAGE wire input."""
    client = get_flow_client()
    if not client.connected:
        raise HTTPException(503, "Extension not connected")
    result = await client.edit_image(
        body.prompt,
        body.source_media_id,
        body.project_id,
        aspect_ratio=body.aspect_ratio,
        user_paygate_tier=body.user_paygate_tier,
        character_media_ids=body.reference_media_ids,
        image_model=body.image_model,
        count=body.count,
        seed=body.seed,
    )
    if result.get("error") or (isinstance(result.get("status"), int) and result["status"] >= 400):
        raise HTTPException(result.get("status", 502), result.get("error", result.get("data")))
    return result.get("data", result)


@router.post("/export-image", tags=[TAG_IMAGE], summary="Xuất và tải ảnh chất lượng cao 2K / 4K")
@router.post("/upscale-image", include_in_schema=False)
async def export_image(body: UpscaleImageRequest):
    """Download a generated Flow image at 2K (or plan-gated 4K)."""
    import base64
    import binascii

    client = get_flow_client()
    if not client.connected:
        raise HTTPException(503, "Extension not connected")
    result = await client.upscale_image(
        body.media_id,
        body.project_id,
        resolution=body.quality.upper(),
    )
    if result.get("error") or (isinstance(result.get("status"), int) and result["status"] >= 400):
        raise HTTPException(result.get("status", 502), result.get("error", result.get("data")))
    data = result.get("data", result)
    try:
        content = base64.b64decode(data["encodedImage"], validate=True)
    except (KeyError, TypeError, binascii.Error) as exc:
        raise HTTPException(502, "Flow image upscale returned invalid image data") from exc
    quality = body.quality.lower()
    return Response(
        content=content,
        media_type=data.get("contentType", "image/jpeg"),
        headers={
            "Content-Disposition": f'attachment; filename="flow-{body.media_id}-{quality}.jpg"',
            "X-Flow-Image-Quality": quality,
        },
    )


async def _upload_image_bytes(
    client,
    image_bytes: bytes,
    *,
    project_id: str,
    mime_type: str,
    file_name: str,
    cdp_endpoint: Optional[str] = None,
) -> dict:
    """Upload bytes through the shared Flow path and return the public response."""
    if not image_bytes:
        raise HTTPException(422, "image payload is empty")
    resolved_project_id = await _resolve_direct_project(client, project_id)
    b64 = base64.b64encode(image_bytes).decode()
    result = await client.upload_image(
        b64,
        mime_type=mime_type,
        project_id=resolved_project_id,
        file_name=file_name,
        cdp_endpoint=cdp_endpoint,
    )
    if result.get("error") or (
        isinstance(result.get("status"), int) and result["status"] >= 400
    ):
        raise HTTPException(
            result.get("status", 502),
            result.get("error", result.get("data")),
        )
    media_id = result.get("_mediaId")
    if media_id:
        cached = cache_uploaded_media_bytes(
            media_id,
            image_bytes,
            mime_type=mime_type,
            file_name=file_name,
        )
        if cached is None:
            # Fallback for cache write failures: the backend media is still valid,
            # but the already-open Flow gallery may need one refresh to see it.
            mark_uploaded_media_for_ui_refresh(resolved_project_id, media_id)
    return {
        "media_id": media_id,
        "project_id": resolved_project_id,
        "raw": result.get("data", result),
    }


def _read_server_local_image(file_path: str) -> bytes:
    """Read a path from FlowKit's own service namespace with useful API errors."""
    try:
        with open(file_path, "rb") as f:
            return f.read()
    except FileNotFoundError as exc:
        raise HTTPException(
            404,
            (
                "Server-local file is not visible to the FlowKit service: "
                f"{file_path}. External callers should use image_base64 or "
                "/api/flow/upload-image-file; caller-local /tmp paths may be hidden "
                "by systemd PrivateTmp."
            ),
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            403,
            (
                "File is not readable by FlowKit service: "
                f"{file_path}. The path must be readable by the service user; "
                "external callers should use image_base64 or /api/flow/upload-image-file."
            ),
        ) from exc
    except IsADirectoryError as exc:
        raise HTTPException(422, f"file_path is a directory, not an image file: {file_path}") from exc
    except OSError as exc:
        raise HTTPException(422, f"Could not read server-local file {file_path}: {exc}") from exc


@router.post(
    "/upload-image",
    tags=[TAG_IMAGE],
    summary="Upload ảnh qua Base64 hoặc server-local path",
    description=(
        "JSON upload endpoint. External/API callers should send image_base64. "
        "file_path is a server-local convenience mode only: the path is opened by "
        "the FlowKit service user and must be visible inside its systemd namespace."
    ),
)
async def upload_image(body: UploadImageRequest):
    """Upload image bytes to Google Flow; prefer image_base64 for external callers."""
    client = get_flow_client()
    if not client.connected:
        raise HTTPException(503, "Extension not connected")

    target_cdp, target_pid, _ = await resolve_profile_target(body.profile_id, body.cdp_endpoint)

    if body.image_base64:
        try:
            image_bytes = base64.b64decode(body.image_base64, validate=True)
        except Exception as exc:
            raise HTTPException(422, "image_base64 is not valid base64") from exc
        if not image_bytes:
            raise HTTPException(422, "image_base64 is empty")
        mime = body.mime_type or mimetypes.guess_type(body.file_name)[0] or "image/png"
    elif body.file_path:
        image_bytes = _read_server_local_image(body.file_path)
        if not image_bytes:
            raise HTTPException(422, f"Server-local image is empty: {body.file_path}")
        mime = body.mime_type or mimetypes.guess_type(body.file_path)[0] or "image/png"
    else:
        raise HTTPException(
            422,
            "image_base64 is recommended; alternatively provide server-local file_path",
        )

    return await _upload_image_bytes(
        client,
        image_bytes,
        project_id=body.project_id or target_pid or "",
        mime_type=mime,
        file_name=body.file_name,
        cdp_endpoint=target_cdp,
    )


@router.post(
    "/upload-image-file",
    tags=[TAG_IMAGE],
    summary="Upload file ảnh trực tiếp (multipart/form-data)",
    description=(
        "Recommended direct-file endpoint for external callers. The uploaded bytes are "
        "read from the HTTP request, so the caller does not need to share a filesystem "
        "namespace with the FlowKit service. Leave project_id empty to use/create the "
        "session project."
    ),
)
async def upload_image_file(
    file: UploadFile = File(..., description="Image file bytes from the caller."),
    project_id: str = Form(
        default="",
        description="Existing Flow project id, or empty to use/create the session project.",
    ),
    file_name: Optional[str] = Form(
        default=None,
        description="Optional filename override sent to Google Flow.",
    ),
    mime_type: Optional[str] = Form(
        default=None,
        description="Optional MIME override; defaults to upload Content-Type or filename inference.",
    ),
):
    """Upload a multipart file without requiring server-local filesystem access."""
    client = get_flow_client()
    if not client.connected:
        raise HTTPException(503, "Extension not connected")

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(422, "uploaded image file is empty")
    resolved_name = file_name or file.filename or "image.png"
    resolved_mime = (
        mime_type
        or file.content_type
        or mimetypes.guess_type(resolved_name)[0]
        or "image/png"
    )
    return await _upload_image_bytes(
        client,
        image_bytes,
        project_id=project_id,
        mime_type=resolved_mime,
        file_name=resolved_name,
    )


@router.post(
    "/generate-video-full",
    response_model=GenerateVideoFullResponse,
    tags=[TAG_VIDEO],
    summary="Tạo Video hoàn chỉnh & Tự động xóa Watermark (Full Pipeline: Generate -> Poll -> Delogo -> Clean Video)",
    description=(
        "Chạy toàn bộ quy trình tạo video từ A-Z trong một lần gọi API:\n"
        "1. Gửi lệnh tạo video lên Google Flow (Text-to-Video, Image-to-Video hoặc Reference-to-Video).\n"
        "2. Tự động thăm dò (polling) đến khi Google Flow hoàn tất render video (hỗ trợ cả Omni Flash và Veo).\n"
        "3. Tải file video chất lượng cao về server cục bộ.\n"
        "4. Tự động áp dụng thuật toán Lossless Reverse Alpha Blending để bóc tách và xóa sạch Watermark Google ở góc phải từng frame video.\n"
        "5. Trả về link video sạch (`clean_url`), đường dẫn lưu trữ cục bộ (`clean_file_path`), và trạng thái hoàn thành."
    ),
)
async def generate_video_full(body: GenerateVideoFullRequest):
    """Full-pipeline video generation: Submit -> Poll -> Download -> Delogo -> Return clean media."""
    client = get_flow_client()
    target_cdp, target_pid, profile_id = await resolve_profile_target(body.profile_id, body.cdp_endpoint, body.project_id)
    if not client.connected and not target_cdp:
        raise HTTPException(503, "Không có Chrome profile hoặc extension nào đang kết nối. Vui lòng mở Chrome hoặc kiểm tra lại tab Profiles.")

    project_id = await _resolve_direct_project(client, body.project_id or target_pid or "")
    start_time = time.time()

    # 1. Submit request based on input parameters
    submit_res = None
    if body.start_image_media_id:
        if body.model_family == "omni_flash":
            if body.end_image_media_id:
                submit_res = await generate_omni_flash_first_last_video(
                    start_image_media_id=body.start_image_media_id,
                    end_image_media_id=body.end_image_media_id,
                    prompt=body.prompt,
                    project_id=project_id,
                    scene_id=body.scene_id,
                    duration_s=body.duration_s,
                    resolution=body.resolution,
                    aspect_ratio=body.aspect_ratio,
                    user_paygate_tier=body.user_paygate_tier,
                    cdp_endpoint=target_cdp,
                )
            else:
                submit_res = await generate_omni_flash_first_frame_video(
                    start_image_media_id=body.start_image_media_id,
                    prompt=body.prompt,
                    project_id=project_id,
                    scene_id=body.scene_id,
                    duration_s=body.duration_s,
                    resolution=body.resolution,
                    aspect_ratio=body.aspect_ratio,
                    user_paygate_tier=body.user_paygate_tier,
                    cdp_endpoint=target_cdp,
                )
        else:
            payload = {
                "start_image_media_id": body.start_image_media_id,
                "end_image_media_id": body.end_image_media_id,
                "prompt": body.prompt,
                "project_id": project_id,
                "scene_id": body.scene_id,
                "aspect_ratio": body.aspect_ratio,
                "user_paygate_tier": body.user_paygate_tier,
                "cdp_endpoint": target_cdp,
            }
            submit_res = await client.generate_video(**{k: v for k, v in payload.items() if v is not None})
    elif body.reference_media_ids:
        if body.model_family == "omni_flash":
            submit_res = await generate_omni_flash_video(
                reference_media_ids=body.reference_media_ids,
                prompt=body.prompt,
                project_id=project_id,
                scene_id=body.scene_id,
                duration_s=body.duration_s,
                resolution=body.resolution,
                aspect_ratio=body.aspect_ratio,
                user_paygate_tier=body.user_paygate_tier,
                cdp_endpoint=target_cdp,
            )
        else:
            submit_res = await client.generate_video_from_references(
                reference_media_ids=body.reference_media_ids,
                prompt=body.prompt,
                project_id=project_id,
                scene_id=body.scene_id,
                aspect_ratio=body.aspect_ratio,
                user_paygate_tier=body.user_paygate_tier,
                cdp_endpoint=target_cdp,
            )
    else:
        # Default Text-to-Video via Omni Flash
        submit_res = await generate_omni_flash_text_video(
            prompt=body.prompt,
            project_id=project_id,
            scene_id=body.scene_id,
            duration_s=body.duration_s,
            resolution=body.resolution,
            aspect_ratio=body.aspect_ratio,
            user_paygate_tier=body.user_paygate_tier,
            cdp_endpoint=target_cdp,
        )

    if not submit_res or submit_res.get("error") or (isinstance(submit_res.get("status"), int) and submit_res["status"] >= 400):
        err_msg = submit_res.get("error", submit_res.get("data", "Submit failed")) if isinstance(submit_res, dict) else "Submit failed"
        await profile_manager.report_profile_failure(profile_id, str(err_msg), auto_stop=True)
        raise HTTPException(submit_res.get("status", 502) if isinstance(submit_res, dict) else 502, err_msg)
    profile_manager.report_profile_success(profile_id)

    submit_data = submit_res.get("data", submit_res)

    # 2. Extract media identifiers
    media_id = None
    media_list = submit_res.get("media") or (submit_data.get("media") if isinstance(submit_data, dict) else [])
    if media_list and isinstance(media_list[0], dict):
        media_id = media_list[0].get("name")

    workflows = submit_res.get("workflows") or (submit_data.get("workflows") if isinstance(submit_data, dict) else [])
    if workflows and isinstance(workflows[0], dict) and not media_id:
        media_id = workflows[0].get("primary_media_id")

    operations = submit_res.get("operations") or (submit_data.get("operations") if isinstance(submit_data, dict) else [])
    if not operations and isinstance(submit_data, dict):
        operations = submit_data.get("flowkitPolling", {}).get("operations", [])

    # 3. Polling loop
    download_url = None
    deadline = time.time() + body.timeout_seconds
    while time.time() < deadline:
        await asyncio.sleep(4)

        if media_id:
            try:
                media_res = await client.get_media(media_id)
                mdata = media_res.get("data", media_res) if isinstance(media_res, dict) else {}
                v_url = (mdata.get("video", {}) or {}).get("fifeUrl")
                i_url = (mdata.get("image", {}) or {}).get("fifeUrl")
                download_url = mdata.get("url") or v_url or i_url
                if download_url:
                    break
            except Exception as e:
                logger.debug("get_media poll for %s: %s", media_id, e)

        if workflows and not download_url:
            try:
                omni_status = await check_omni_flash_status(workflows, project_id=project_id)
                wf_list = omni_status.get("workflows") or []
                if wf_list and isinstance(wf_list[0], dict):
                    w0 = wf_list[0]
                    if w0.get("status") == "MEDIA_GENERATION_STATUS_SUCCESSFUL" or w0.get("done"):
                        download_url = w0.get("media", {}).get("url")
                        media_id = w0.get("primary_media_id") or media_id
                        if download_url:
                            break
                    elif w0.get("status") == "FAILED":
                        raise HTTPException(502, f"Omni Flash render thất bại: {w0.get('error')}")
            except HTTPException:
                raise
            except Exception as e:
                logger.debug("check_omni_flash_status poll: %s", e)

        if operations and not download_url:
            try:
                op_status = await client.check_video_status(operations)
                ops_list = op_status.get("data", {}).get("operations", [])
                if ops_list and isinstance(ops_list[0], dict):
                    op0 = ops_list[0]
                    if op0.get("status") == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                        meta_vid = op0.get("operation", {}).get("metadata", {}).get("video", {})
                        download_url = meta_vid.get("fifeUrl")
                        media_id = meta_vid.get("mediaId") or media_id
                        if download_url:
                            break
                    elif op0.get("status") == "MEDIA_GENERATION_STATUS_FAILED":
                        raise HTTPException(502, f"Veo render thất bại: {op0.get('error')}")
            except HTTPException:
                raise
            except Exception as e:
                logger.debug("check_video_status poll: %s", e)

    if not download_url:
        raise HTTPException(504, f"Quá thời gian chờ render video từ Google Flow (> {body.timeout_seconds}s)")

    # 4. Download file
    FLOW_VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    file_prefix = media_id or f"video_{int(time.time())}"
    orig_path = FLOW_VIDEOS_DIR / f"{file_prefix}.mp4"
    clean_path = FLOW_VIDEOS_DIR / f"{file_prefix}_clean.mp4"

    await asyncio.to_thread(download_media_file, download_url, str(orig_path))

    # 5. Delogo
    watermark_removed = False
    if body.auto_delogo:
        watermark_removed = await asyncio.to_thread(run_delogo, str(orig_path), str(clean_path), True)

    target_file = clean_path if watermark_removed else orig_path

    return {
        "status": "COMPLETED",
        "media_id": media_id,
        "clean_url": f"/api/flow/file?path={quote(str(target_file))}",
        "clean_file_path": str(target_file),
        "original_url": download_url,
        "original_file_path": str(orig_path),
        "watermark_removed": watermark_removed,
        "elapsed_seconds": round(time.time() - start_time, 2),
        "duration_s": body.duration_s,
        "aspect_ratio": body.aspect_ratio,
        "resolution": body.resolution,
    }


@router.post(
    "/remove-watermark",
    response_model=RemoveWatermarkResponse,
    tags=[TAG_WATERMARK],
    summary="Xóa Watermark từ file, URL hoặc media_id (Lossless)",
    description=(
        "Loại bỏ watermark Google bằng thuật toán Lossless Reverse Alpha Blending từ:\n"
        "- `file_path`: Đường dẫn file ảnh/video trên máy chủ.\n"
        "- `url`: Đường dẫn URL tải trực tiếp (FifeUrl hoặc web URL).\n"
        "- `media_id`: UUID media của Google Flow."
    ),
)
async def remove_watermark(body: RemoveWatermarkRequest):
    """Remove watermark from file_path, direct URL, or Google Flow media_id."""
    client = get_flow_client()
    src_path = None
    media_id = body.media_id
    is_video = body.is_video

    FLOW_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if body.file_path:
        src_p = Path(body.file_path).resolve()
        if not src_p.exists() or not src_p.is_file():
            raise HTTPException(404, f"File không tồn tại: {body.file_path}")
        src_path = src_p
        if is_video is None:
            is_video = src_p.suffix.lower() in (".mp4", ".webm", ".mov", ".mkv")

    elif body.url:
        dl_url = body.url
        if is_video is None:
            is_video = ("/video/" in dl_url) or any(ext in dl_url.lower() for ext in (".mp4", ".webm"))
        ext = ".mp4" if is_video else ".jpg"
        src_path = FLOW_OUTPUT_DIR / f"dl_{uuid.uuid4().hex[:12]}{ext}"
        await asyncio.to_thread(download_media_file, dl_url, str(src_path))

    elif body.media_id:
        if not client.connected:
            raise HTTPException(503, "Extension not connected để tra cứu media_id")
        media_res = await client.get_media(body.media_id)
        mdata = media_res.get("data", media_res) if isinstance(media_res, dict) else {}
        v_url = (mdata.get("video", {}) or {}).get("fifeUrl")
        i_url = (mdata.get("image", {}) or {}).get("fifeUrl")
        dl_url = mdata.get("url") or v_url or i_url
        if not dl_url:
            raise HTTPException(404, f"Không tìm thấy URL tải cho media_id: {body.media_id}")
        if is_video is None:
            is_video = bool(v_url) or ("/video/" in dl_url)
        ext = ".mp4" if is_video else ".jpg"
        src_path = FLOW_OUTPUT_DIR / f"{body.media_id}{ext}"
        await asyncio.to_thread(download_media_file, dl_url, str(src_path))

    else:
        raise HTTPException(400, "Vui lòng cung cấp ít nhất một trong các trường: file_path, url, hoặc media_id")

    ext = src_path.suffix or (".mp4" if is_video else ".jpg")
    clean_path = src_path.with_name(f"{src_path.stem}_clean{ext}")
    success = await asyncio.to_thread(run_delogo, str(src_path), str(clean_path), bool(is_video))

    target_path = clean_path if success else src_path
    return {
        "status": "COMPLETED",
        "clean_url": f"/api/flow/file?path={quote(str(target_path))}",
        "clean_file_path": str(target_path),
        "original_file_path": str(src_path),
        "watermark_removed": success,
        "is_video": bool(is_video),
    }


@router.post(
    "/remove-watermark-file",
    tags=[TAG_WATERMARK],
    summary="Upload file media để xóa Watermark (Trả về File sạch)",
    description=(
        "Upload trực tiếp 1 file ảnh (JPG/PNG/WebP) hoặc video (MP4/WebM) từ máy tính của bạn.\n"
        "Hệ thống sẽ chạy thuật toán Reverse Alpha Blending để bóc tách watermark và trả về file sạch nguyên bản."
    ),
)
async def remove_watermark_file(
    file: UploadFile = File(..., description="File ảnh (JPG/PNG/WebP) hoặc video (MP4/WebM) cần xóa watermark."),
    is_video: Optional[bool] = Form(None, description="Tùy chọn: True nếu là video, False nếu là ảnh. Mặc định tự nhận diện theo file."),
):
    """Upload multipart media file, remove watermark losslessly, and return clean file directly."""
    FLOW_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    orig_name = file.filename or "media"
    suffix = Path(orig_name).suffix.lower()

    if is_video is None:
        is_video = suffix in (".mp4", ".webm", ".mov", ".mkv") or (file.content_type and "video" in file.content_type)

    if not suffix:
        suffix = ".mp4" if is_video else ".jpg"

    file_stem = f"{uuid.uuid4().hex[:10]}_{Path(orig_name).stem}"
    orig_file = FLOW_UPLOADS_DIR / f"{file_stem}{suffix}"
    clean_file = FLOW_UPLOADS_DIR / f"{file_stem}_clean{suffix}"

    content = await file.read()
    if not content:
        raise HTTPException(422, "File tải lên rỗng")
    with open(orig_file, "wb") as f:
        f.write(content)

    success = await asyncio.to_thread(run_delogo, str(orig_file), str(clean_file), bool(is_video))
    target_file = clean_file if success else orig_file

    media_type = file.content_type
    if not media_type:
        media_type = "video/mp4" if is_video else "image/jpeg"

    clean_filename = f"clean_{orig_name}"
    return FileResponse(
        target_file,
        media_type=media_type,
        filename=clean_filename,
        headers={
            "X-Watermark-Removed": str(success).lower(),
            "Content-Disposition": f'inline; filename="{clean_filename}"',
        },
    )


@router.get(
    "/file",
    tags=[TAG_IMAGE, TAG_VIDEO, TAG_WATERMARK],
    summary="Tải hoặc xem trực tiếp file media đã tạo",
    description="Stream trực tiếp hoặc tải về file media (ảnh hoặc video) tạo bởi FlowKit.",
)
async def serve_flow_file(path: str, download: bool = False):
    """Stream or download a local generated or cleaned media file."""
    resolved = Path(path).resolve()
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(404, "File media không tồn tại")

    import tempfile
    allowed_dirs = [
        BASE_DIR.resolve(),
        OUTPUT_DIR.resolve(),
        Path("/tmp").resolve(),
        Path(tempfile.gettempdir()).resolve(),
    ]
    if not any(str(resolved).startswith(str(d)) for d in allowed_dirs):
        raise HTTPException(403, "Đường dẫn file không nằm trong danh mục cho phép truy cập")

    suffix = resolved.suffix.lower()
    media_type = "application/octet-stream"
    if suffix in (".jpg", ".jpeg"):
        media_type = "image/jpeg"
    elif suffix == ".png":
        media_type = "image/png"
    elif suffix == ".webp":
        media_type = "image/webp"
    elif suffix == ".mp4":
        media_type = "video/mp4"
    elif suffix == ".webm":
        media_type = "video/webm"

    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{resolved.name}"'
    else:
        headers["Content-Disposition"] = f'inline; filename="{resolved.name}"'

    return FileResponse(resolved, media_type=media_type, headers=headers)
