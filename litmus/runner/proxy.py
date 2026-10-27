"""One mitmproxy instance per run (PLAN §12.3).

``mitmdump`` runs as a child process with the addon in
``litmus/runner/proxy_addon.py``, writing ``flows.jsonl`` under the run
directory. The emulator is pointed at it with the global ``http_proxy``
setting, which Android applies to app processes started afterwards - the
lifecycle always launches the app after the proxy is up.

The proxy is **always** cleared, including when startup fails or the run
raises: :meth:`MitmProxy.stop` puts the emulator back to ``:0`` in a
``finally``-safe way, because a global proxy left behind silently breaks every
later run on that device.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from litmus.detectors.network import Flow, load_flows, parse_flows

DEFAULT_PORT = 8080
PORT_SCAN = 20
START_TIMEOUT_S = 25.0

#: How the emulator reaches the host loopback (contract §6).
EMULATOR_HOST = "10.0.2.2"

CLEAR_PROXY = ":0"


class ProxyError(RuntimeError):
    """The proxy could not be started."""


def _mitmdump_path() -> str | None:
    """Prefer the console script of the interpreter running Litmus."""
    candidate = Path(sys.executable).with_name("mitmdump.exe")
    if candidate.is_file():
        return str(candidate)
    candidate = Path(sys.executable).with_name("mitmdump")
    if candidate.is_file():
        return str(candidate)
    import shutil

    return shutil.which("mitmdump")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(start: int = DEFAULT_PORT, attempts: int = PORT_SCAN) -> int:
    for port in range(start, start + attempts):
        if _port_free(port):
            return port
    raise ProxyError(f"no free proxy port in {start}..{start + attempts - 1}")


def _port_open(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


class MitmProxy:
    """Supervises one ``mitmdump`` and the emulator's global proxy setting."""

    def __init__(
        self,
        driver,
        workdir: Path,
        port: int = DEFAULT_PORT,
        start_timeout_s: float = START_TIMEOUT_S,
    ) -> None:
        self.driver = driver
        self.workdir = Path(workdir)
        self.requested_port = port
        self.start_timeout_s = start_timeout_s
        self.port = 0
        self.flows_path = self.workdir / "flows.jsonl"
        self.log_path = self.workdir / "proxy.log"
        self.process: subprocess.Popen | None = None
        self.started = False
        self.error = ""

    # -- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        """Start mitmdump, wait for its port, then point the device at it."""
        if self.started:
            return True
        self.workdir.mkdir(parents=True, exist_ok=True)
        if self.flows_path.exists():
            self.flows_path.unlink()

        mitmdump = _mitmdump_path()
        if not mitmdump:
            self.error = "mitmdump not found - install the proxy extra"
            return False

        try:
            self.port = pick_port(self.requested_port)
        except ProxyError as exc:
            self.error = str(exc)
            return False

        addon = Path(__file__).with_name("proxy_addon.py")
        confdir = self.workdir / "mitm-conf"
        confdir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["LITMUS_FLOWS_PATH"] = str(self.flows_path)
        cmd = [
            mitmdump,
            "--listen-host",
            "0.0.0.0",
            "--listen-port",
            str(self.port),
            "-s",
            str(addon),
            "--set",
            f"confdir={confdir}",
            "--set",
            "block_global=false",
            "--set",
            "flow_detail=0",
            "-q",
        ]
        log = self.log_path.open("w", encoding="utf-8", errors="replace")
        try:
            self.process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self.error = f"could not start mitmdump: {exc}"
            return False
        finally:
            log.close()

        deadline = time.monotonic() + self.start_timeout_s
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.error = f"mitmdump exited with {self.process.returncode}"
                self._terminate()
                return False
            if _port_open(self.port):
                break
            time.sleep(0.2)
        else:
            self.error = "mitmdump did not listen in time"
            self._terminate()
            return False

        self._set_device_proxy(f"{EMULATOR_HOST}:{self.port}")
        self.started = True
        return True

    def stop(self) -> None:
        """Clear the device proxy (always) and stop mitmdump."""
        try:
            self.clear_device_proxy()
        finally:
            self._terminate()
            self.started = False

    def _terminate(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - OS wedged
                pass

    # -- device proxy -----------------------------------------------------
    def _set_device_proxy(self, value: str) -> None:
        try:
            self.driver.shell("settings", "put", "global", "http_proxy", value, check=False)
        except Exception:  # noqa: BLE001 - the device may be gone; stop() retries
            pass

    def clear_device_proxy(self) -> str:
        """Put the global proxy back to ``:0``; returns what the device reports."""
        driver = self.driver
        if driver is None:
            return ""
        try:
            driver.shell("settings", "put", "global", "http_proxy", CLEAR_PROXY, check=False)
            current = driver.shell("settings", "get", "global", "http_proxy", check=False) or ""
        except Exception:  # noqa: BLE001 - never let cleanup fail a run
            return ""
        current = current.strip()
        if current not in {"", CLEAR_PROXY, "null"}:
            try:
                driver.shell("settings", "delete", "global", "http_proxy", check=False)
                current = driver.shell(
                    "settings", "get", "global", "http_proxy", check=False
                ) or ""
            except Exception:  # noqa: BLE001
                pass
        return current.strip()

    # -- flows ------------------------------------------------------------
    def offset(self) -> int:
        try:
            return self.flows_path.stat().st_size
        except OSError:
            return 0

    def flows_since(self, offset: int) -> list[Flow]:
        """Flows appended after ``offset`` (a byte marker taken before a tap)."""
        try:
            size = self.flows_path.stat().st_size
        except OSError:
            return []
        if size <= offset:
            return []
        try:
            with self.flows_path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
        except OSError:
            return []
        return parse_flows(data.decode("utf-8", errors="replace"))

    def all_flows(self) -> list[Flow]:
        return load_flows(self.flows_path)

    def status_line(self) -> str:
        if self.started:
            return f"mitmproxy :{self.port} -> {self.flows_path.name}"
        return f"mitmproxy off ({self.error or 'not started'})"
