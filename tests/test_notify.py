"""Slack notification: payload shape, silent no-op, and never delaying a run."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from litmus.config import get_settings
from litmus.services.notify import (
    notify_done,
    notify_done_async,
    report_url,
    slack_payload,
)

RUN = {
    "run_id": "20260101-000000-abcdef",
    "status": "done",
    "package": "com.bazaar.demo",
    "duration_s": 620.4,
    "confirmed": 4,
    "flaky": 1,
    "filtered": 0,
    "kinds": ["crash", "deadtap", "http_5xx", "ui_broken"],
}


def settings(**overrides):
    return get_settings().model_copy(update=overrides)


class _Capture(BaseHTTPRequestHandler):
    """Minimal webhook receiver: records the body, answers 200."""

    bodies: list[dict] = []
    status = 200

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            type(self).bodies.append(json.loads(raw))
        except json.JSONDecodeError:  # pragma: no cover - defensive
            type(self).bodies.append({"raw": raw.decode("utf-8", "replace")})
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:  # silence the default stderr line
        return None


def webhook() -> tuple[str, HTTPServer, threading.Thread]:
    _Capture.bodies = []
    _Capture.status = 200
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return f"http://{host}:{port}/services/T000/B000/xyz", server, thread


# -- payload ---------------------------------------------------------------
def test_payload_counts_the_verdicts_and_links_the_report():
    payload = slack_payload(RUN, "http://127.0.0.1:8000/runs/20260101-000000-abcdef")

    assert "4 confirmed" in payload["text"]
    assert "1 flaky" in payload["text"]
    assert "com.bazaar.demo" in payload["text"]
    assert payload["blocks"][0]["text"]["text"].startswith("🐞")
    context = payload["blocks"][1]["elements"][0]["text"]
    assert "20260101-000000-abcdef" in context
    assert "deadtap" in context
    button = payload["blocks"][2]["elements"][0]
    assert button["url"].endswith("/runs/20260101-000000-abcdef")


def test_payload_without_flakes_stays_short():
    payload = slack_payload({**RUN, "flaky": 0, "filtered": 0}, "")
    assert "flaky" not in payload["text"]
    assert "filtered" not in payload["text"]
    assert len(payload["blocks"]) == 2


def test_report_url_is_the_dashboard_page():
    settings_ = settings(api_host="127.0.0.1", api_port=8123)
    assert report_url(settings_, "run_1") == "http://127.0.0.1:8123/runs/run_1"


# -- delivery --------------------------------------------------------------
def test_no_webhook_is_a_silent_no_op():
    assert notify_done(settings(slack_webhook_url=""), RUN) is False


def test_a_configured_webhook_receives_the_summary():
    url, server, thread = webhook()
    try:
        delivered = notify_done(settings(slack_webhook_url=url), RUN, timeout_s=2.0)
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert delivered is True
    assert len(_Capture.bodies) == 1
    body = _Capture.bodies[0]
    assert "4 confirmed" in body["text"]
    assert body["blocks"][0]["type"] == "section"


def test_a_dead_webhook_is_swallowed():
    # Port 9 on loopback refuses immediately; the run must not care.
    ok = notify_done(settings(slack_webhook_url="http://127.0.0.1:9/hook"), RUN, timeout_s=0.5)
    assert ok is False


def test_async_notification_never_blocks_the_caller():
    url, server, thread = webhook()
    try:
        worker = notify_done_async(settings(slack_webhook_url=url), RUN, timeout_s=2.0)
        assert worker is not None
        worker.join(timeout=3)
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert len(_Capture.bodies) == 1


def test_async_notification_without_a_webhook_does_nothing():
    assert notify_done_async(settings(slack_webhook_url=""), RUN) is None
