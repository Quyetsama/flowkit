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
