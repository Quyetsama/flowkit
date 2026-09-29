import io
from pathlib import Path
import pytest
from fastapi import UploadFile

from agent.api import flow as flow_api
from agent.services.watermark_remover import (
    load_watermark_alpha_48,
    get_media_dimensions,
    get_delogo_filter,
    run_delogo,
    download_media_file,
)


def test_watermark_alpha_and_filter():
    alpha = load_watermark_alpha_48()
    assert alpha is not None
    assert alpha.shape == (48, 48)

    filt_vid = get_delogo_filter(1280, 720, is_video=True)
    assert "delogo=" in filt_vid
    assert filt_vid == "delogo=x=1130:y=575:w=65:h=65"

    filt_img = get_delogo_filter(1024, 1024, is_video=False)
    assert filt_img == "delogo=x=899:y=899:w=72:h=72"


def test_run_delogo_on_stickman(tmp_path):
    stickman = Path("frame_stickman.png")
    if not stickman.exists():
        pytest.skip("frame_stickman.png not found")
    out_file = tmp_path / "stickman_clean.png"
    success = run_delogo(str(stickman), str(out_file), is_video=False)
    assert success is True
    assert out_file.exists()
    assert out_file.stat().st_size > 0


class FakeFlowClientForPipeline:
    connected = True

    def __init__(self):
        self.requested_images = []
        self.requested_videos = []

    async def generate_images(self, **kwargs):
        self.requested_images.append(kwargs)
        # Return mock media with local stickman file URL
        return {
            "status": 200,
            "data": {
                "media": [
                    {
                        "name": "11111111-2222-3333-4444-555555555555",
                        "image": {
                            "generatedImage": {
                                "mediaId": "11111111-2222-3333-4444-555555555555",
                                "fifeUrl": "file://" + str(Path("frame_stickman.png").resolve()),
                            }
                        },
                    }
                ]
            },
        }

    async def get_media(self, media_id, cdp_endpoint=None):
        return {
            "status": 200,
            "data": {
                "mediaId": media_id,
                "url": "file://" + str(Path("frame_stickman.png").resolve()),
            },
        }


@pytest.mark.asyncio
async def test_generate_image_endpoint_auto_delogo(monkeypatch, tmp_path):
    client = FakeFlowClientForPipeline()
    monkeypatch.setattr(flow_api, "get_flow_client", lambda: client)
    monkeypatch.setattr(flow_api, "FLOW_IMAGES_DIR", tmp_path)

    # Mock download_media_file to copy local file directly
    def mock_download(url, target):
        import shutil
        src = url.replace("file://", "")
        shutil.copyfile(src, target)

    monkeypatch.setattr(flow_api, "download_media_file", mock_download)

    req = flow_api.GenerateImageRequest(
        prompt="A beautiful sunrise over mountains",
        project_id="test-project",
        auto_delogo=True,
    )
    res = await flow_api.generate_image(req)
    assert "clean_url" in res
    assert res.get("watermark_removed") is True
    assert "clean_file_path" in res
    assert Path(res["clean_file_path"]).exists()


@pytest.mark.asyncio
async def test_remove_watermark_endpoint_file_path(monkeypatch, tmp_path):
    stickman = Path("frame_stickman.png").resolve()
    if not stickman.exists():
        pytest.skip("frame_stickman.png not found")

    req = flow_api.RemoveWatermarkRequest(file_path=str(stickman), is_video=False)
    res = await flow_api.remove_watermark(req)
    assert res["status"] == "COMPLETED"
    assert res["watermark_removed"] is True
    assert "clean_url" in res
    assert Path(res["clean_file_path"]).exists()


@pytest.mark.asyncio
async def test_remove_watermark_file_upload_endpoint(tmp_path):
    stickman = Path("frame_stickman.png")
    if not stickman.exists():
        pytest.skip("frame_stickman.png not found")

    content = stickman.read_bytes()
    upload = UploadFile(filename="stickman.png", file=io.BytesIO(content))

    response = await flow_api.remove_watermark_file(upload, is_video=False)
    assert response.headers["X-Watermark-Removed"] == "true"
    assert Path(response.path).exists()


@pytest.mark.asyncio
async def test_serve_flow_file(tmp_path):
    test_img = tmp_path / "test.jpg"
    test_img.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 50)

    res = await flow_api.serve_flow_file(str(test_img), download=False)
    assert res.media_type == "image/jpeg"
    assert 'inline; filename="test.jpg"' in res.headers["Content-Disposition"]


@pytest.mark.asyncio
async def test_generate_video_full_endpoint(monkeypatch, tmp_path):
    client = FakeFlowClientForPipeline()
    monkeypatch.setattr(flow_api, "get_flow_client", lambda: client)
    monkeypatch.setattr(flow_api, "FLOW_VIDEOS_DIR", tmp_path)

    async def mock_submit_omni_text(**kwargs):
        return {
            "status": 200,
            "data": {
                "media": [{"name": "test-video-uuid-123"}],
            },
        }

    monkeypatch.setattr(flow_api, "generate_omni_flash_text_video", mock_submit_omni_text)

    async def mock_get_media(media_id, cdp_endpoint=None):
        return {
            "status": 200,
            "data": {
                "mediaId": media_id,
                "video": {"fifeUrl": "http://mock-flow.google/video.mp4"},
            },
        }

    monkeypatch.setattr(client, "get_media", mock_get_media)

    def mock_download(url, target):
        Path(target).write_bytes(b"fake video data")

    monkeypatch.setattr(flow_api, "download_media_file", mock_download)

    def mock_run_delogo(src, dst, is_video=True):
        Path(dst).write_bytes(b"clean fake video data")
        return True

    monkeypatch.setattr(flow_api, "run_delogo", mock_run_delogo)

    req = flow_api.GenerateVideoFullRequest(
        prompt="Cyberpunk drone flight across neon skyscrapers",
        project_id="test-pid",
        duration_s=8,
        auto_delogo=True,
        timeout_seconds=30,
    )
    res = await flow_api.generate_video_full(req)
    assert res["status"] == "COMPLETED"
    assert res["media_id"] == "test-video-uuid-123"
    assert res["watermark_removed"] is True
    assert "clean_url" in res
    assert Path(res["clean_file_path"]).exists()

