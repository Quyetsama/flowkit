"""Unit tests for FlowKit Studio (Profile Manager and Batch Engine)."""

import pytest
from pathlib import Path
from agent.studio.models import BatchJobConfig, ProfileConfig
from agent.studio.profile_manager import ProfileManager, find_chrome_executable
from agent.studio.batch_engine import BatchEngine, slugify


def test_slugify():
    assert slugify("0-3s: Cinematic shot of a cat") == "0-3s_cinematic_shot_of_a_cat"
    assert slugify("!!! Hello world ???") == "hello_world"
    assert slugify("") == "item"


def test_profile_manager_lifecycle(tmp_path):
    pfile = tmp_path / "profiles_test.json"
    mgr = ProfileManager(file_path=pfile)

    profiles = mgr.list_profiles()
    assert len(profiles) >= 1
    assert profiles[0].id == "profile_1"

    # Add profile
    p2 = mgr.add_profile("Test Account 2", port=9225)
    assert p2.name == "Test Account 2"
    assert p2.cdp_port == 9225
    assert len(mgr.list_profiles()) == 2

    # Delete profile
    deleted = mgr.delete_profile(p2.id)
    assert deleted is True
    assert len(mgr.list_profiles()) == 1


def test_find_chrome_executable():
    chrome = find_chrome_executable()
    # On macOS developer machine, Chrome should be found
    if chrome:
        assert Path(chrome).exists()


def test_batch_engine_status():
    engine = BatchEngine()
    status = engine.get_status()
    assert status["is_running"] is False
    assert status["total"] == 0
    assert status["progress_percent"] == 0


def test_batch_task_config():
    config = BatchJobConfig(
        prompts=["con mèo dễ thương", "con chó vui nhộn"],
        task_type="image",
        aspect_ratio="16:9",
    )
    assert len(config.prompts) == 2
    assert config.task_type == "image"


@pytest.mark.asyncio
async def test_batch_engine_profile_isolation(monkeypatch, tmp_path):
    """Verify each profile worker routes generation strictly to its own CDP port & project."""
    import agent.studio.batch_engine as engine_module
    from agent.studio.models import BatchTask

    calls = []

    async def fake_video(**kwargs):
        calls.append(("video", kwargs))
        return {
            "status": 200,
            "data": {
                "media": [{"name": "fake-media-1"}],
                "workflows": [{"name": "fake-wf-1", "primary_media_id": "fake-media-1"}],
            },
        }

    async def fake_images(**kwargs):
        calls.append(("image", kwargs))
        return {
            "media": [{"name": "fake-img-1", "fifeUrl": "http://example.com/img.jpg"}],
        }

    monkeypatch.setattr(engine_module, "generate_omni_flash_text_video", fake_video)

    engine = BatchEngine()
    engine.is_running = True
    config = BatchJobConfig(prompts=["test prompt"], task_type="video", output_dir=str(tmp_path))

    profile_2 = ProfileConfig(
        id="profile_2",
        name="Profile 2",
        cdp_port=9225,
        user_data_dir=str(tmp_path / "p2"),
        active_project_id="pid-profile-2-uuid",
    )

    queue = engine_module.asyncio.Queue()
    task = BatchTask(
        index=1,
        prompt="test prompt",
        task_type="video",
        output_filename="001_test.mp4",
        file_path=str(tmp_path / "001_test.mp4"),
    )
    queue.put_nowait(task)

    # Monkeypatch client.get_media to return completed download url immediately
    async def fake_get_media(mid, cdp_endpoint=None):
        return {"status": 200, "data": {"url": "http://example.com/video.mp4"}}

    fake_client = engine_module.get_flow_client()
    monkeypatch.setattr(fake_client, "get_media", fake_get_media)
    monkeypatch.setattr(engine, "_download_file", lambda url, path: None)

    await engine._profile_worker(profile_2, queue, config)

    assert len(calls) == 1
    call_type, kwargs = calls[0]
    assert call_type == "video"
    assert kwargs["cdp_endpoint"] == "http://127.0.0.1:9225"
    assert kwargs["project_id"] == "pid-profile-2-uuid"


def test_delogo_filter_calculation():
    engine = BatchEngine()
    # 720p 16:9 video
    assert engine._get_delogo_filter(1280, 720, is_video=True) == "delogo=x=1130:y=575:w=65:h=65"
    # 1080p 16:9 video
    assert engine._get_delogo_filter(1920, 1080, is_video=True) == "delogo=x=1700:y=865:w=95:h=95"
    # 720x1280 9:16 vertical video
    assert engine._get_delogo_filter(720, 1280, is_video=True) == "delogo=x=570:y=1135:w=65:h=65"
    # 1024x1024 image
    assert engine._get_delogo_filter(1024, 1024, is_video=False) == "delogo=x=899:y=899:w=72:h=72"
    # Bound clamping (small image)
    filt = engine._get_delogo_filter(60, 60, is_video=False)
    assert "w=50:h=50" in filt
    assert "x=0" in filt and "y=0" in filt


@pytest.mark.asyncio
async def test_batch_engine_image_delogo(monkeypatch, tmp_path):
    """Verify image task with auto_delogo=True invokes delogo and sets clean_file_path."""
    import agent.studio.batch_engine as engine_module
    from agent.studio.models import BatchTask

    delogo_calls = []

    def fake_delogo(src, dst, is_video=True):
        delogo_calls.append((src, dst, is_video))
        return True

    engine = BatchEngine()
    engine.is_running = True
    monkeypatch.setattr(engine, "_run_delogo", fake_delogo)
    monkeypatch.setattr(engine, "_download_file", lambda url, path: None)

    async def fake_images(**kwargs):
        return {
            "media": [{"name": "fake-img-1", "fifeUrl": "http://example.com/img.jpg"}],
        }

    fake_client = engine_module.get_flow_client()
    monkeypatch.setattr(fake_client, "generate_images", fake_images)

    config = BatchJobConfig(prompts=["test image prompt"], task_type="image", output_dir=str(tmp_path), auto_delogo=True)

    profile = ProfileConfig(
        id="profile_1",
        name="Profile 1",
        cdp_port=9224,
        user_data_dir=str(tmp_path / "p1"),
        active_project_id="pid-p1",
    )

    queue = engine_module.asyncio.Queue()
    task = BatchTask(
        index=1,
        prompt="test image prompt",
        task_type="image",
        output_filename="001_test.jpg",
        file_path=str(tmp_path / "001_test.jpg"),
        auto_delogo=True,
    )
    queue.put_nowait(task)

    await engine._profile_worker(profile, queue, config)

    assert len(delogo_calls) == 1
    src, dst, is_video = delogo_calls[0]
    assert src == str(tmp_path / "001_test.jpg")
    assert dst == str(tmp_path / "001_test_clean.jpg")
    assert is_video is False
    assert task.clean_file_path == str(tmp_path / "001_test_clean.jpg")
    assert task.status == "completed"


def test_remove_image_watermark_lossless(tmp_path):
    """Test mathematical reverse alpha blending on synthetic watermarked image."""
    import cv2
    import numpy as np
    from agent.studio.batch_engine import BatchEngine, load_watermark_alpha_48

    engine = BatchEngine()

    # Create synthetic 1024x1024 image with green background and sharp black line
    img = np.full((1024, 1024, 3), (120, 170, 140), dtype=np.uint8)
    cv2.line(img, (890, 890), (940, 940), (0, 0, 0), 3)

    alpha = load_watermark_alpha_48()
    assert alpha is not None
    assert alpha.shape == (48, 48)

    # Blend watermark at (898, 898)
    patch = img[898:898+48, 898:898+48].astype(np.float32)
    a = (alpha * 0.64)[:, :, np.newaxis]
    watermarked_patch = patch * (1.0 - a) + 255.0 * a
    img[898:898+48, 898:898+48] = np.clip(watermarked_patch, 0, 255).astype(np.uint8)

    src = str(tmp_path / "synthetic.jpg")
    dst = str(tmp_path / "synthetic_clean.jpg")
    cv2.imwrite(src, img)

    success = engine._remove_image_watermark_lossless(src, dst)
    assert success is True
    assert Path(dst).exists()

    clean_img = cv2.imread(dst)
    # Verify watermark is removed:
    # 1. Line pixel is preserved as sharp black (not blurred into green)
    line_val = clean_img[898 + 24, 898 + 24]
    assert int(line_val[0]) < 15
    assert int(line_val[1]) < 15
    assert int(line_val[2]) < 15

    # 2. Background pixel is restored to green background (120, 170, 140)
    bg_val = clean_img[898 + 20, 898 + 28]
    assert abs(int(bg_val[0]) - 120) < 15
    assert abs(int(bg_val[1]) - 170) < 15
    assert abs(int(bg_val[2]) - 140) < 15


def test_remove_image_watermark_clean_skipped(tmp_path):
    """Test that an image without watermark is preserved untouched."""
    import cv2
    import numpy as np
    from agent.studio.batch_engine import BatchEngine

    engine = BatchEngine()
    # Pristine off-white image without any watermark
    img = np.full((1024, 1024, 3), (250, 248, 248), dtype=np.uint8)

    src = str(tmp_path / "pristine.jpg")
    dst = str(tmp_path / "pristine_clean.jpg")
    cv2.imwrite(src, img)

    success = engine._remove_image_watermark_lossless(src, dst)
    assert success is True
    assert Path(dst).exists()

    clean_img = cv2.imread(dst)
    # Must remain identical without any ghost watermark artifact
    assert np.array_equal(clean_img, img)


def test_remove_video_watermark_lossless(tmp_path):
    """Test video watermark removal using reverse alpha blending."""
    import cv2
    import numpy as np
    from agent.studio.batch_engine import BatchEngine, load_watermark_alpha_48

    engine = BatchEngine()
    alpha = load_watermark_alpha_48()
    assert alpha is not None

    w, h = 1280, 720
    fps = 24.0
    src = str(tmp_path / "synthetic_vid.mp4")
    dst = str(tmp_path / "synthetic_vid_clean.mp4")

    # Create a small 5-frame 1280x720 video
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(src, fourcc, fps, (w, h))

    cx = w - 144
    cy = h - 144
    a = (alpha * 0.60)[:, :, np.newaxis]

    for _ in range(5):
        frame = np.full((h, w, 3), (120, 170, 140), dtype=np.uint8)
        cv2.line(frame, (cx - 10, cy - 10), (cx + 58, cy + 58), (0, 0, 0), 3)
        patch = frame[cy : cy + 48, cx : cx + 48].astype(np.float32)
        frame[cy : cy + 48, cx : cx + 48] = np.clip(patch * (1.0 - a) + 255.0 * a, 0, 255).astype(np.uint8)
        writer.write(frame)
    writer.release()

    success = engine._remove_video_watermark_lossless(src, dst)
    assert success is True
    assert Path(dst).exists()
    assert Path(dst).stat().st_size > 0

    cap = cv2.VideoCapture(dst)
    assert cap.isOpened()
    ret, clean_frame = cap.read()
    cap.release()
    assert ret is True
    assert clean_frame.shape == (h, w, 3)

    # 1. Line pixel is preserved as sharp black
    line_val = clean_frame[cy + 10, cx + 10]
    assert int(line_val[0]) < 15
    assert int(line_val[1]) < 15
    assert int(line_val[2]) < 15

    # 2. Background pixel is restored near (120, 170, 140)
    bg_val = clean_frame[cy + 30, cx + 10]
    assert abs(int(bg_val[0]) - 120) < 20
    assert abs(int(bg_val[1]) - 170) < 20
    assert abs(int(bg_val[2]) - 140) < 20




