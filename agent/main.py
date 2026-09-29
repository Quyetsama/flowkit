"""Flow Kit — FastAPI + WebSocket server entry point."""
import asyncio
import json
import logging
import signal
from contextlib import asynccontextmanager

import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

from agent.config import API_HOST, API_PORT, WS_HOST, WS_PORT
from agent.db.schema import init_db, close_db
from agent.api.characters import router as characters_router
from agent.api.projects import router as projects_router
from agent.api.videos import router as videos_router
from agent.api.scenes import router as scenes_router
from agent.api.requests import router as requests_router
from agent.api.flow import router as flow_router
from agent.api.upscale_status import router as upscale_status_router
from agent.api.reviews import router as reviews_router
from agent.api.tts import router as tts_router
from agent.api.materials import router as materials_router
from agent.api.music import router as music_router
from agent.api.models import router as models_router
from agent.api.providers import router as providers_router
from agent.api.active_project import router as active_project_router
from agent.studio.routes import router as studio_router
from agent.worker.processor import get_worker_controller
from agent.services.flow_client import get_flow_client
from agent.services.event_bus import event_bus
from agent.sdk import init_sdk

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


# ─── WebSocket Server for Extension ─────────────────────────

async def ws_handler(websocket):
    """Handle a Chrome extension WebSocket connection."""
    client = get_flow_client()
    client.set_extension(websocket)
    logger.info("Extension connected from %s", websocket.remote_address)

    # Send callback secret so extension can authenticate HTTP callbacks
    await websocket.send(json.dumps({"type": "callback_secret", "secret": _CALLBACK_SECRET}))

    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
                await client.handle_message(data, websocket)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from extension")
            except Exception as e:
                logger.exception("Error handling extension message: %s", e)
    except websockets.ConnectionClosed:
        pass
    finally:
        client.clear_extension(websocket)
        logger.info("Extension disconnected")


async def run_ws_server():
    """Run WebSocket server for extension connections."""
    async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
        logger.info("WebSocket server listening on ws://%s:%d", WS_HOST, WS_PORT)
        await asyncio.Future()  # run forever


# ─── FastAPI App ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # Load custom materials from DB into in-memory registry
    from agent.db.crud import list_materials as db_list_materials
    from agent.materials import register_material, _BUILTIN_IDS
    try:
        custom_materials = await db_list_materials()
        for m in custom_materials:
            if m["id"] not in _BUILTIN_IDS:
                register_material(m)
                logger.info("Loaded custom material from DB: %s", m["id"])
    except Exception as e:
        logger.warning("Failed to load custom materials: %s", e)

    ops = init_sdk(get_flow_client())
    logger.info("SDK initialized (OperationService ready)")
    logger.info("Flow Kit starting on %s:%d", API_HOST, API_PORT)

    controller = get_worker_controller()

    # SIGTERM handler for graceful shutdown (Unix only)
    try:
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, controller.request_shutdown)
    except (NotImplementedError, AttributeError):
        pass

    # Start background tasks
    ws_task = asyncio.create_task(run_ws_server())
    worker_task = asyncio.create_task(controller.start())
    logger.info("WS server + worker started")

    yield

    controller.request_shutdown()
    await controller.drain()
    ws_task.cancel()
    worker_task.cancel()
    await close_db()
    logger.info("Flow Kit stopped")


OPENAPI_TAGS = [
    {
        "name": "🎨 Tạo & Xử Lý Ảnh (Image Generation)",
        "description": "Tạo ảnh mới với Nano Banana 2 / Pro, chỉnh sửa ảnh (Inpainting), upload file và lấy danh sách cấu hình hỗ trợ.",
    },
    {
        "name": "🎬 Tạo Video & Upscale (Video Generation)",
        "description": "Tạo video AI từ văn bản (Omni Flash Text-to-Video), từ ảnh khởi đầu (Image-to-Video), First+Last frames, reference ảnh, kiểm tra trạng thái render và nâng cấp 4K.",
    },
    {
        "name": "⚡ Hàng Đợi Tạo Hàng Loạt (Batch Queue)",
        "description": "Gửi nhiều prompt tạo ảnh/video cùng lúc vào hàng đợi tự động phân bổ và xử lý nền.",
    },
    {
        "name": "💧 Xóa Watermark (Watermark Removal)",
        "description": "Thuật toán giải mã ngược ma trận alpha (Lossless Reverse Alpha Blending) bóc tách và xóa sạch 100% watermark Google trên ảnh và video mà không làm mờ, không suy giảm chi tiết.",
    },
    {
        "name": "💳 Trạng Thái & Credits (Status & Credits)",
        "description": "Kiểm tra số dư credit Google Flow, tình trạng kết nối Extension/Chrome CDP và điều khiển cơ chế cooldown bảo vệ tài khoản.",
    },
]

app = FastAPI(
    title="FlowKit — Google Flow Automation & REST API",
    description=(
        "Hệ thống API tự động hóa Google Flow (Nano Banana, Gemini Omni Flash, Veo 2).\n\n"
        "👉 **Trang Swagger chuyên biệt cho Tạo Ảnh & Video: [/swagger](/swagger)**"
    ),
    version="1.3.0",
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_GENERATION_PATHS = {
    "/api/flow/generate-image",
    "/api/flow/generate-video",
    "/api/flow/generate-video-refs",
    "/api/flow/generate-video-omni",
    "/api/flow/generate-video-omni-text",
    "/api/flow/edit-image",
}


@app.middleware("http")
async def flow_caller_observability(request: Request, call_next):
    """Attribute generation submits without logging prompts, media or secrets."""
    response = await call_next(request)
    if request.method == "POST" and request.url.path in _GENERATION_PATHS:
        caller = (request.headers.get("x-flowkit-caller") or "unknown")[:80]
        logger.info(
            "Flow generation request caller=%s path=%s status=%s",
            caller,
            request.url.path,
            response.status_code,
        )
    return response


app.include_router(characters_router, prefix="/api")
app.include_router(projects_router, prefix="/api")
app.include_router(videos_router, prefix="/api")
app.include_router(scenes_router, prefix="/api")
app.include_router(requests_router, prefix="/api")
app.include_router(flow_router, prefix="/api")
app.include_router(upscale_status_router, prefix="/api")
app.include_router(reviews_router, prefix="/api")
app.include_router(tts_router, prefix="/api")
app.include_router(materials_router, prefix="/api")
app.include_router(music_router, prefix="/api")
app.include_router(models_router)
app.include_router(providers_router)
app.include_router(active_project_router)
app.include_router(studio_router)


import secrets as _secrets
_CALLBACK_SECRET = _secrets.token_urlsafe(32)


@app.post("/api/ext/callback")
async def ext_callback(request: Request):
    """HTTP callback for extension to deliver API responses.

    Replaces ws.send() for response delivery — immune to WS disconnect.
    Extension POSTs {id, status, data, error} here instead of sending via WS.
    Requires X-Callback-Secret header matching the secret sent to extension on WS connect.
    """
    data = await request.json()
    client = get_flow_client()
    req_id = data.get("id")
    logger.info("ext/callback: id=%s pending=%d match=%s",
                str(req_id)[:8] if req_id else "none",
                len(client._pending),
                "yes" if req_id and req_id in client._pending else "no")
    if req_id and req_id in client._pending:
        future = client._pending[req_id]
        try:
            future.set_result(data)
        except asyncio.InvalidStateError:
            pass
        return {"ok": True}
    return {"ok": False, "reason": "no matching pending request"}


@app.get("/health")
async def health():
    client = get_flow_client()
    return {
        "status": "ok",
        "version": "0.2.0",
        "extension_connected": client.connected,
        "ws": client.ws_stats,
    }


@app.get("/studio")
async def studio_redirect():
    return RedirectResponse("/api/studio/ui")


# ─── Swagger Generation UI ───────────────────────────────────

@app.get("/openapi-generation.json", include_in_schema=False)
async def openapi_generation_schema():
    """Return a focused OpenAPI schema containing only Image and Video Generation APIs."""
    full_schema = app.openapi()

    generation_prefixes = (
        "/api/flow/generate-",
        "/api/flow/edit-image",
        "/api/flow/upload-image",
        "/api/flow/export-",
        "/api/flow/image-capabilities",
        "/api/flow/media/",
        "/api/flow/check-",
        "/api/flow/upscale-video",
        "/api/flow/refresh-urls/",
        "/api/flow/remove-watermark",
        "/api/flow/file",
        "/api/flow/credits",
        "/api/flow/status",
        "/api/flow/clear-hijack",
        "/api/requests/batch",
        "/health",
    )

    filtered_paths = {}
    used_tags = set()
    for path, methods in full_schema.get("paths", {}).items():
        if any(path.startswith(p) for p in generation_prefixes):
            filtered_paths[path] = methods
            for op in methods.values():
                if isinstance(op, dict) and "tags" in op:
                    used_tags.update(op["tags"])

    filtered_tags = [
        t for t in full_schema.get("tags", [])
        if t.get("name") in used_tags
    ]

    return {
        "openapi": full_schema.get("openapi", "3.1.0"),
        "info": {
            "title": "FlowKit — Swagger API Tạo Ảnh & Video & Xóa Watermark (Google Flow)",
            "description": (
                "## 🎨, 🎬 & 💧 FlowKit Generation & Watermark Removal API Documentation\n\n"
                "Giao diện Swagger tương tác trực tiếp để kiểm thử và tích hợp các API tạo hình ảnh, video AI của **Google Flow** (Nano Banana, Gemini Omni Flash, Veo 2) kèm theo **quy trình tự động xóa sạch 100% Watermark Google** bằng thuật toán giải mã ngược ma trận alpha kênh (Lossless Reverse Alpha Blending).\n\n"
                "### 🚀 Quy trình trọn gói (Full Pipeline):\n"
                "- **1. Tạo ảnh sạch (Nano Banana)**: Dùng `POST /api/flow/generate-image` (mặc định `auto_delogo: true`, tự động xóa watermark và trả link `clean_url`).\n"
                "- **2. Tạo video trọn gói (Full Pipeline: Generate -> Poll -> Delogo -> Clean Video)**: Dùng `POST /api/flow/generate-video-full`. API tự động submit prompt, chờ Google Flow render xong, tải video về và bóc tách watermark, trả link `clean_url` sẵn sàng sử dụng!\n"
                "- **3. Tạo video từ chữ (Omni Flash Text-to-Video)**: Dùng `POST /api/flow/generate-video-omni-text` (chọn duration 4/6/8/10s).\n"
                "- **4. Tạo video từ ảnh (First frame / Image-to-Video)**: Upload ảnh qua `POST /api/flow/upload-image-file` lấy `media_id`, sau đó gọi `POST /api/flow/generate-video`.\n"
                "- **5. Xóa Watermark độc lập**: Dùng `POST /api/flow/remove-watermark-file` để upload file trực tiếp từ máy tính, hoặc `POST /api/flow/remove-watermark` để xử lý theo `media_id`, `url`, hoặc `file_path`.\n"
                "- **6. Xem & tải file sạch**: Truy cập trực tiếp qua `GET /api/flow/file?path=...`.\n"
                "- **7. Hàng đợi tạo hàng loạt**: Dùng `POST /api/requests/batch` và kiểm tra qua `GET /api/requests/batch-status`.\n"
                "- **8. Kiểm tra Credit**: Xem số dư tại `GET /api/flow/credits`.\n"
            ),
            "version": "1.3.0",
        },
        "tags": filtered_tags or OPENAPI_TAGS,
        "paths": filtered_paths,
        "components": full_schema.get("components", {}),
    }


@app.get("/swagger", include_in_schema=False)
@app.get("/docs/generation", include_in_schema=False)
async def swagger_generation_ui():
    """Trang Swagger UI chuyên biệt dành riêng cho API Tạo Ảnh & Video."""
    return get_swagger_ui_html(
        openapi_url="/openapi-generation.json",
        title="FlowKit — API Tạo Ảnh & Video (Swagger UI)",
        swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
        swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        swagger_favicon_url="https://fastapi.tiangolo.com/img/favicon.png",
    )


# ─── Dashboard WebSocket ──────────────────────────────────────

@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket):
    """WebSocket endpoint for dashboard clients (Chrome extension side panel)."""
    # Reject cross-origin connections (only allow localhost)
    origin = (websocket.headers.get("origin") or "").lower()
    if origin and not any(origin.startswith(p) for p in (
        "http://127.0.0.1", "http://localhost", "chrome-extension://",
    )):
        await websocket.close(code=4003, reason="Origin not allowed")
        return
    await websocket.accept()

    q = event_bus.subscribe()
    try:
        # Send initial snapshot
        client = get_flow_client()
        controller = get_worker_controller()
        from agent.db import crud
        pending_requests = await crud.list_requests(status="PENDING")
        processing_requests = await crud.list_requests(status="PROCESSING")
        snapshot = {
            "type": "snapshot",
            "health": {
                "status": "ok",
                "extension_connected": client.connected,
            },
            "requests": pending_requests + processing_requests,
            "worker": {
                "active": controller.active_count,
                "slots": max(0, 5 - controller.active_count),
            },
        }
        await websocket.send_text(json.dumps(snapshot))

        # Forward events from event_bus to this client
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
                await websocket.send_text(msg)
            except asyncio.TimeoutError:
                # Send keepalive ping
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("Dashboard WS client disconnected: %s", e)
    finally:
        event_bus.unsubscribe(q)


if __name__ == "__main__":
    import os
    import uvicorn
    reload_enabled = os.environ.get("GLA_RELOAD", "0") == "1"
    uvicorn.run(
        "agent.main:app",
        host=API_HOST,
        port=API_PORT,
        reload=reload_enabled,
        reload_excludes=["*.db", "*.db-wal", "*.db-shm", "output/*"],
    )
