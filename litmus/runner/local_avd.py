"""Local AVD runner (PLAN §12.1) - the dev-time device provider.

One emulator per worker, no Docker on this laptop. State is reset with
``pm clear`` (~1.3 s) instead of a cold boot (~82 s); a cold boot is reserved
for the first run of a benchmark.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from litmus.config import Settings, get_settings
from litmus.runner.driver import AdbError, Driver


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """What the evidence manifest records about the device."""

    serial: str
    api_level: str = ""
    model: str = ""
    release: str = ""
    screen_size: tuple[int, int] = (0, 0)
    density: str = ""

    def to_dict(self) -> dict:
        return {
            "serial": self.serial,
            "api_level": self.api_level,
            "model": self.model,
            "release": self.release,
            "screen_size": list(self.screen_size),
            "density": self.density,
        }


def _adb_path(settings: Settings) -> str:
    import shutil

    found = shutil.which("adb")
    if found:
        return found
    for root in (settings.android_home, settings.android_sdk_root):
        if root:
            candidate = Path(root) / "platform-tools" / "adb.exe"
            if candidate.exists():
                return str(candidate)
    return "adb"


def _emulator_path(settings: Settings) -> str | None:
    import shutil

    found = shutil.which("emulator")
    if found:
        return found
    for root in (settings.android_home, settings.android_sdk_root):
        if root:
            candidate = Path(root) / "emulator" / "emulator.exe"
            if candidate.exists():
                return str(candidate)
    return None


def list_devices(adb: str) -> list[str]:
    """Serials currently in the ``device`` state."""
    try:
        result = subprocess.run(
            [adb, "devices"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    serials: list[str] = []
    for line in (result.stdout or "").splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


class LocalAvd:
    """Owns the emulator: start it, prepare it, install the APK, reset state."""

    def __init__(
        self,
        settings: Settings | None = None,
        serial: str | None = None,
        start_timeout: int = 240,
    ) -> None:
        self.settings = settings or get_settings()
        self.adb = _adb_path(self.settings)
        self.requested_serial = serial
        self.start_timeout = start_timeout
        self.driver: Driver | None = None
        self._started_emulator = False

    # -- lifecycle --------------------------------------------------------
    def ensure_device(self) -> Driver:
        """Return a driver for a booted device, starting the AVD if needed."""
        serial = self.requested_serial or self._pick_existing_serial()
        if serial is None:
            serial = self._start_emulator()
        driver = Driver(serial=serial, adb=self.adb)
        self._wait_for_boot(driver)
        self.driver = driver
        return driver

    def _pick_existing_serial(self) -> str | None:
        serials = list_devices(self.adb)
        if not serials:
            return None
        # Prefer an emulator, but any attached device is acceptable.
        for serial in serials:
            if serial.startswith("emulator-"):
                return serial
        return serials[0]

    def _start_emulator(self) -> str:
        """Boot the configured AVD. Never quickboot - snapshots crashed twice."""
        emulator = _emulator_path(self.settings)
        if not emulator:
            raise AdbError("emulator binary not found; set ANDROID_HOME")
        avd = self.settings.litmus_avd
        cmd = [
            emulator,
            "-avd",
            avd,
            "-no-snapshot",
            "-no-boot-anim",
            "-no-audio",
            "-gpu",
            "swiftshader_indirect",
        ]
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._started_emulator = True
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            serial = self._pick_existing_serial()
            if serial:
                return serial
            time.sleep(2)
        raise AdbError(f"emulator {avd} did not appear in adb within {self.start_timeout}s")

    def _wait_for_boot(self, driver: Driver) -> None:
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if driver.is_booted():
                return
            time.sleep(2)
        raise AdbError("device did not finish booting")

    def prepare(self, driver: Driver | None = None) -> None:
        """Make the device deterministic: no animations, screen awake, unlocked."""
        driver = driver or self.driver
        if driver is None:
            raise AdbError("no device attached")
        for setting in (
            "window_animation_scale",
            "transition_animation_scale",
            "animator_duration_scale",
        ):
            driver.shell("settings", "put", "global", setting, "0", check=False)
        driver.shell("settings", "put", "global", "stay_on_while_plugged_in", "3", check=False)
        # Suppress the "you are in immersive mode" overlay: it covers the app
        # window and is not part of any run.
        driver.shell(
            "settings", "put", "secure", "immersive_mode_confirmations", "confirmed", check=False
        )
        driver.shell("input", "keyevent", "224", check=False)  # screen on
        driver.shell("input", "keyevent", "82", check=False)  # dismiss keyguard

    # -- app state --------------------------------------------------------
    def install(self, driver: Driver, apk: Path) -> str:
        return driver.install(apk)

    def clear(self, driver: Driver, package: str) -> None:
        driver.clear_app(package)

    def app_info(self, driver: Driver, package: str) -> dict:
        """Read versionName / versionCode from ``dumpsys package``."""
        info: dict[str, str] = {"package": package}
        try:
            out = driver.shell("dumpsys", "package", package, timeout=30)
        except AdbError:
            return info
        match = re.search(r"versionName=(\S+)", out)
        if match:
            info["version_name"] = match.group(1)
        match = re.search(r"versionCode=(\d+)", out)
        if match:
            info["version_code"] = match.group(1)
        match = re.search(r"minSdk=(\d+)", out)
        if match:
            info["min_sdk"] = match.group(1)
        return info

    def device_info(self, driver: Driver | None = None) -> DeviceInfo:
        driver = driver or self.driver
        if driver is None:
            raise AdbError("no device attached")
        density = ""
        try:
            match = re.search(r"Physical density:\s*(\d+)", driver.shell("wm", "density"))
            density = match.group(1) if match else ""
        except AdbError:
            pass
        return DeviceInfo(
            serial=driver.serial,
            api_level=driver.getprop("ro.build.version.sdk"),
            model=driver.getprop("ro.product.model"),
            release=driver.getprop("ro.build.version.release"),
            screen_size=driver.screen_size(),
            density=density,
        )

    def launch(self, driver: Driver, package: str) -> str:
        """Start the app and return the component that was started."""
        component = driver.resolve_launcher(package)
        driver.launch(component)
        return component
