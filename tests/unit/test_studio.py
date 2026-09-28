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

