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
import signal
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
import websockets

from agent.studio.models import ProfileConfig

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
PROFILES_FILE = ROOT_DIR / "profiles.json"
MAX_AUTO_ACTIVE_PROFILES = 5


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
        self._launch_lock: asyncio.Lock | None = None
        self.load()

    @property
    def launch_lock(self) -> asyncio.Lock:
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()
        return self._launch_lock

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
        """Return profiles currently connected via CDP and not quarantined."""
        now = time.time()
        if (now - self._last_check_time > max_cache_age_s) or not any(p.is_connected for p in self._profiles.values()):
            await self.check_all_profiles()

        active = []
        for p in self._profiles.values():
            if not p.is_connected or not p.is_running:
                continue
            if p.quarantine_until and now < p.quarantine_until:
                # Quarantined due to error or quota
                continue
            active.append(p)
        return active

    def launch_chrome(self, profile_id: str, project_id: str | None = None) -> dict[str, Any]:
        """Launch Google Chrome process configured for this profile."""
        profile = self.get_profile(profile_id)
        if not profile:
            return {"success": False, "error": f"Không tìm thấy profile '{profile_id}'"}

        # Clear previous quarantine and failure states on explicit launch
        profile.consecutive_failures = 0
        profile.quarantine_until = 0.0
        profile.disabled_reason = None
        profile.error_message = None

        chrome_path = find_chrome_executable()
        if not chrome_path:
            return {
                "success": False,
                "error": "Không tìm thấy trình duyệt Google Chrome trên hệ điều hành này. Vui lòng cài đặt Chrome.",
            }

        os.makedirs(profile.user_data_dir, exist_ok=True)

        target_pid = project_id or profile.active_project_id
        if target_pid:
            url = f"https://labs.google/fx/tools/flow/project/{target_pid}"
            profile.active_project_id = target_pid
            self.save()
        else:
            url = "https://labs.google/fx/tools/flow"

        cmd = [
            chrome_path,
            f"--remote-debugging-port={profile.cdp_port}",
            f"--user-data-dir={profile.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
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

            logger.info("Launched Chrome for profile %s on port %s (URL: %s)", profile.id, profile.cdp_port, url)
            return {
                "success": True,
                "profile_id": profile.id,
                "cdp_port": profile.cdp_port,
                "chrome_path": chrome_path,
                "url": url,
            }
        except Exception as exc:
            logger.error("Failed to launch Chrome for %s: %s", profile.id, exc)
            return {"success": False, "error": str(exc)}

    async def wait_until_ready(self, profile: ProfileConfig, timeout_s: float = 12.0) -> bool:
        """Poll loopback CDP /json until Chrome is up and responsive."""
        deadline = time.time() + timeout_s
        cdp_url = f"http://127.0.0.1:{profile.cdp_port}"

        while time.time() < deadline:
            try:
                def _check():
                    req = urllib.request.Request(f"{cdp_url}/json", method="GET")
                    with urllib.request.urlopen(req, timeout=1.0) as resp:
                        return json.loads(resp.read().decode("utf-8"))

                tabs = await asyncio.to_thread(_check)
                if isinstance(tabs, list) and len(tabs) > 0:
                    profile.is_running = True
                    profile.is_connected = True
                    profile.error_message = None
                    for t in tabs:
                        tab_url = t.get("url", "")
                        if "/project/" in tab_url:
                            pid = tab_url.split("/project/")[-1].split("?")[0].split("/")[0]
                            if pid:
                                profile.active_project_id = pid
                                self.save()
                                break
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)

        logger.warning("Profile %s (port %s) did not become ready within %.1fs", profile.id, profile.cdp_port, timeout_s)
        return False

    async def navigate_profile_to_project(self, profile: ProfileConfig, project_id: str) -> bool:
        """Navigate an active Chrome profile tab to a specific Google Flow project."""
        if not project_id or profile.active_project_id == project_id:
            return True

        cdp_url = f"http://127.0.0.1:{profile.cdp_port}"
        target_url = f"https://labs.google/fx/tools/flow/project/{project_id}"

        try:
            def _get_tabs():
                req = urllib.request.Request(f"{cdp_url}/json", method="GET")
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            tabs = await asyncio.to_thread(_get_tabs)
            if not isinstance(tabs, list):
                return False

            flow_tab = None
            for t in tabs:
                if t.get("type") == "page":
                    tab_url = t.get("url", "")
                    if "flow.google.com" in tab_url or "labs.google/fx" in tab_url:
                        flow_tab = t
                        break
            if not flow_tab:
                for t in tabs:
                    if t.get("type") == "page":
                        flow_tab = t
                        break

            if flow_tab and flow_tab.get("webSocketDebuggerUrl"):
                ws_url = flow_tab["webSocketDebuggerUrl"]
                req_id = int(time.time() * 1000) & 0x7FFFFFFF
                async with websockets.connect(ws_url, open_timeout=3, close_timeout=2) as ws:
                    await ws.send(json.dumps({
                        "id": req_id,
                        "method": "Page.navigate",
                        "params": {"url": target_url},
                    }))
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=3.0)
                    except Exception:
                        pass
                profile.active_project_id = project_id
                self.save()
                return True
            else:
                def _open_new():
                    encoded_url = urllib.parse.quote(target_url, safe="")
                    req = urllib.request.Request(f"{cdp_url}/json/new?{encoded_url}", method="PUT")
                    with urllib.request.urlopen(req, timeout=2.0) as resp:
                        return json.loads(resp.read().decode("utf-8"))

                await asyncio.to_thread(_open_new)
                profile.active_project_id = project_id
                self.save()
                return True
        except Exception as exc:
            logger.warning("Could not navigate profile %s to project %s: %s", profile.id, project_id, exc)
            return False

    async def auto_launch_profiles(
        self,
        count: int = 1,
        project_id: str | None = None,
        preferred_profile_id: str | None = None,
    ) -> list[ProfileConfig]:
        """Auto-launch eligible unquarantined Chrome profiles on demand up to MAX_AUTO_ACTIVE_PROFILES (5)."""
        async with self.launch_lock:
            # 1. Check currently active connected profiles
            connected = await self.get_active_connected_profiles(max_cache_age_s=2.0)
            currently_active = len(connected)

            if preferred_profile_id:
                target_p = self.get_profile(preferred_profile_id)
                if target_p:
                    if target_p.is_connected:
                        if project_id and target_p.active_project_id != project_id:
                            await self.navigate_profile_to_project(target_p, project_id)
                        return [target_p]
                    if target_p.quarantine_until <= time.time():
                        logger.info("[Auto-Launch] Launching requested profile %s on demand", target_p.id)
                        self.launch_chrome(target_p.id, project_id=project_id)
                        ready = await self.wait_until_ready(target_p)
                        if ready:
                            self._last_check_time = 0.0
                            return [target_p]

            max_can_launch = max(0, MAX_AUTO_ACTIVE_PROFILES - currently_active)
            if max_can_launch <= 0:
                logger.info(
                    "[Auto-Launch] Max active profiles reached (%d/%d). Using existing active pool.",
                    currently_active,
                    MAX_AUTO_ACTIVE_PROFILES,
                )
                return connected[:count]

            needed = min(count, max_can_launch)
            now = time.time()
            all_list = self.list_profiles()

            # Filter candidates: not connected and NOT quarantined
            candidates = [
                p for p in all_list
                if not p.is_connected and (p.quarantine_until <= now)
            ]

            if not candidates:
                logger.warning("[Auto-Launch] No unquarantined offline profiles available to launch.")
                return connected[:count]

            # Prioritize candidates:
            # 1. Matching project_id
            # 2. Fewer consecutive failures
            def _rank(p: ProfileConfig) -> tuple[int, int]:
                match_proj = 0 if (project_id and p.active_project_id == project_id) else 1
                return (match_proj, p.consecutive_failures)

            candidates.sort(key=_rank)
            to_launch = candidates[:needed]

            logger.info(
                "[Auto-Launch] Launching %d profile(s) on-demand: %s",
                len(to_launch),
                [p.id for p in to_launch],
            )

            # Launch processes
            for p in to_launch:
                target_pid = project_id or p.active_project_id
                self.launch_chrome(p.id, project_id=target_pid)

            # Wait for all launched profiles to be ready
            results = await asyncio.gather(*(self.wait_until_ready(p) for p in to_launch))

            successfully_launched = [p for p, ok in zip(to_launch, results) if ok]
            logger.info(
                "[Auto-Launch] Successfully launched %d/%d profile(s)",
                len(successfully_launched),
                len(to_launch),
            )

            self._last_check_time = 0.0
            return successfully_launched or connected[:count]

    def _kill_chrome_process(self, profile: ProfileConfig) -> None:
        """Kill Chrome process listening on the profile's CDP port to immediately free RAM."""
        port = profile.cdp_port
        sys_name = platform.system()
        if sys_name in ("Darwin", "Linux"):
            # 1. Kill by port listener via lsof
            try:
                res = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True)
                pids = [p.strip() for p in res.stdout.split() if p.strip()]
                for pid in pids:
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except Exception:
                        pass
                if pids:
                    time.sleep(0.3)
                    for pid in pids:
                        try:
                            os.kill(int(pid), signal.SIGKILL)
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("lsof kill error on port %d: %s", port, e)

            # 2. Kill any lingering process matching the port argument
            try:
                subprocess.run(["pkill", "-9", "-f", f"--remote-debugging-port={port}"], capture_output=True)
            except Exception:
                pass
        elif sys_name == "Windows":
            try:
                res = subprocess.run(f"netstat -ano | findstr :{port}", shell=True, capture_output=True, text=True)
                for line in res.stdout.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 5 and "LISTENING" in parts:
                        pid = parts[-1]
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                subprocess.run(
                    f'wmic process where "commandline like \'%--remote-debugging-port={port}%\'" call terminate',
                    shell=True,
                    capture_output=True,
                )
            except Exception as e:
                logger.debug("Windows process kill error on port %d: %s", port, e)

    async def stop_chrome(self, profile_id: str) -> dict[str, Any]:
        """Gracefully close or terminate Chrome for this profile to free system RAM."""
        profile = self.get_profile(profile_id)
        if not profile:
            return {"success": False, "error": f"Không tìm thấy profile '{profile_id}'"}

        cdp_url = f"http://127.0.0.1:{profile.cdp_port}"
        closed_via_cdp = False

        # Attempt graceful CDP Browser.close first
        try:
            def _get_browser_ws():
                req = urllib.request.Request(f"{cdp_url}/json/version", method="GET")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    return json.loads(resp.read().decode("utf-8")).get("webSocketDebuggerUrl")

            ws_url = await asyncio.to_thread(_get_browser_ws)
            if ws_url:
                import websockets
                async with websockets.connect(ws_url, open_timeout=1.5, close_timeout=1.5) as ws:
                    await ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                await asyncio.sleep(0.5)
                closed_via_cdp = True
                logger.info("Gracefully closed Chrome via CDP Browser.close for %s", profile.id)
        except Exception:
            pass

        # Force terminate process if still running
        await asyncio.to_thread(self._kill_chrome_process, profile)

        profile.is_running = False
        profile.is_connected = False
        self.save()
        logger.info("[Resource Manager] Chrome stopped for profile %s (RAM freed)", profile.id)
        return {"success": True, "profile_id": profile.id, "closed_via_cdp": closed_via_cdp}

    def report_profile_success(self, profile_id: str | None) -> None:
        """Called when a generation operation succeeds on this profile."""
        if not profile_id:
            return
        p = self.get_profile(profile_id)
        if p:
            p.consecutive_failures = 0
            if not p.disabled_reason:
                p.error_message = None

    async def report_profile_failure(self, profile_id: str | None, error: str, auto_stop: bool = True) -> bool:
        """Record failure and trigger auto-stop (Circuit Breaker) if error is fatal or continuous.
        Returns True if profile was automatically shut down to free RAM."""
        if not profile_id:
            return False
        p = self.get_profile(profile_id)
        if not p:
            return False

        err_low = str(error or "").lower()
        fatal = False
        reason = ""
        quarantine_s = 600.0  # default 10 minutes

        # Check fatal categories
        if any(kw in err_low for kw in ("public_error_user_quota_reached", "user_quota_reached", "quota exceeded", "quota reached", "hết credit", "out of credits")):
            fatal = True
            reason = "Tài khoản hết Quota hôm nay (User Quota Reached)"
            quarantine_s = 12 * 3600  # 12 hours
        elif any(kw in err_low for kw in ("public_error_unusual_activity", "unusual activity", "extension_hijack_detected")):
            fatal = True
            reason = "Google phát hiện hoạt động bất thường (Unusual Activity)"
            quarantine_s = 1800  # 30 mins
        elif any(kw in err_low for kw in ("flow_session_unavailable", "no_flow_key", "no_at_token", "signed out", "session expired")):
            fatal = True
            reason = "Phiên đăng nhập Google bị văng hoặc hết hạn (Signed Out)"
            quarantine_s = 3600  # 1 hour
        else:
            p.consecutive_failures += 1
            if p.consecutive_failures >= 3:
                fatal = True
                reason = f"Gặp lỗi liên tiếp {p.consecutive_failures} lần ({error[:70]})"
                quarantine_s = 900  # 15 mins

        if fatal:
            p.disabled_reason = reason
            p.quarantine_until = time.time() + quarantine_s
            p.error_message = f"Tự động tắt Chrome: {reason}"
            if auto_stop:
                logger.warning(
                    "[Circuit Breaker] Auto-stopping Chrome for profile %s: %s (Terminating process to free RAM)",
                    p.id,
                    reason,
                )
                await self.stop_chrome(p.id)
                return True

        return False


profile_manager = ProfileManager()
