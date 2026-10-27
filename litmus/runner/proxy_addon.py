"""mitmproxy addon: append one JSON line per observed flow.

Loaded by ``mitmdump -s``. The output path arrives in ``LITMUS_FLOWS_PATH``
(an environment variable) so the addon needs no Litmus imports and can run in
mitmproxy's own interpreter.

Only classification inputs are recorded - status, method, host, path, and the
transport error - never bodies or headers, so the file stays small and free of
secrets.
"""

from __future__ import annotations

import json
import os
import time

FLOWS_PATH = os.environ.get("LITMUS_FLOWS_PATH", "")

#: ``10.0.2.2`` is the emulator's alias for the host loopback. It resolves
#: inside the guest, but not on the host where mitmproxy dials the upstream, so
#: the addon rewrites it to ``127.0.0.1`` and keeps the original in the log for
#: profile scoping (``allowed_hosts: 10.0.2.2:8100``).
EMULATOR_HOST = "10.0.2.2"
_ORIGINAL_HOSTS: dict[str, str] = {}


def _write(record: dict) -> None:
    if not FLOWS_PATH:
        return
    record["ts"] = round(time.time(), 3)
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    # One write per flow: a reader tails the file by byte offset, and a torn
    # last line is dropped by the parser.
    with open(FLOWS_PATH, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()


def _base(flow) -> dict:
    request = getattr(flow, "request", None)
    dialed = getattr(request, "host", "") or ""
    original = _ORIGINAL_HOSTS.get(getattr(flow, "id", ""), dialed)
    return {
        "method": getattr(request, "method", "") or "",
        "scheme": getattr(request, "scheme", "http") or "http",
        "host": original or dialed,
        "port": int(getattr(request, "port", 0) or 0),
        "path": getattr(request, "path", "") or "",
        "status": None,
        "error": "",
        "tls_error": False,
        "proxy_generated": False,
    }


def request(flow) -> None:
    """Dial the host loopback for the emulator alias, keeping the guest name."""
    host = getattr(flow.request, "host", "") or ""
    _ORIGINAL_HOSTS[flow.id] = host
    if host == EMULATOR_HOST:
        flow.request.host = "127.0.0.1"


def _error_text(flow) -> str:
    error = getattr(flow, "error", None)
    if not error:
        return ""
    return str(error).strip()


def _looks_like_tls(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("tls", "ssl", "certificate", "handshake"))


def response(flow) -> None:
    record = _base(flow)
    response_ = getattr(flow, "response", None)
    if response_ is not None:
        record["status"] = int(getattr(response_, "status_code", 0) or 0)
        headers = getattr(response_, "headers", {}) or {}
        server = ""
        try:
            server = str(headers.get("Server", "") or "").lower()
        except Exception:  # noqa: BLE001 - header access must never break a flow
            server = ""
        if server == "mitmproxy":
            # mitmproxy generated this response; the upstream never answered.
            record["proxy_generated"] = True
    error = _error_text(flow)
    if error:
        record["error"] = error
        record["proxy_generated"] = True
    _write(record)


def error(flow) -> None:
    record = _base(flow)
    text = _error_text(flow) or "connection error"
    record["error"] = text
    record["tls_error"] = _looks_like_tls(text)
    _write(record)


def tls_failed_server(flow) -> None:
    record = _base(flow)
    record["error"] = "upstream TLS handshake failed"
    record["tls_error"] = True
    _write(record)


def tls_failed_client(flow) -> None:
    record = _base(flow)
    record["error"] = "client rejected the proxy certificate (pinning?)"
    record["tls_error"] = True
    _write(record)
