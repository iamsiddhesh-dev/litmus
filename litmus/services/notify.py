"""Slack notification on ``done`` (PLAN §15).

Two rules, both load-bearing:

* **No webhook, no work.** An unset ``SLACK_WEBHOOK_URL`` means the function
  returns ``False`` immediately: no socket, no log noise, no exception.
* **The verdict comes first.** The run has already been written to the database
  and announced on the event stream before this is called, and the call runs in
  a daemon thread with a 3 s timeout, so a slow or dead Slack can never delay or
  fail a run. The *content* of the message (counts and report URL) is pure, so
  it is unit-tested without a network.

The payload is deliberately small: a Slack incoming webhook only needs ``text``,
and a block gives the counts one line each.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Any

from litmus.config import Settings

logger = logging.getLogger(__name__)

TIMEOUT_S = 3.0

#: Emoji per verdict, so the channel reads at a glance.
VERDICT_ICON = {"confirmed": "🐞", "flaky": "🌀", "filtered": "🧪"}


def report_url(settings: Settings, run_id: str) -> str:
    """Absolute dashboard URL for a run (the API is loopback-only by default)."""
    return f"http://{settings.api_host}:{settings.api_port}/runs/{run_id}"


def slack_payload(run: dict[str, Any], url: str = "") -> dict[str, Any]:
    """Build the incoming-webhook body for one finished run."""
    run_id = str(run.get("run_id") or "")
    confirmed = int(run.get("confirmed") or 0)
    flaky = int(run.get("flaky") or 0)
    filtered = int(run.get("filtered") or 0)
    duration = float(run.get("duration_s") or 0.0)
    status = str(run.get("status") or "done")
    package = str(run.get("package") or "")
    kinds = ", ".join(str(kind) for kind in (run.get("kinds") or []))

    headline = f"{confirmed} confirmed"
    if flaky:
        headline += f", {flaky} flaky"
    if filtered:
        headline += f", {filtered} filtered"
    text = f"{VERDICT_ICON['confirmed']} *Litmus {status}* — {headline} in {duration:.0f}s"
    if package:
        text += f" · `{package}`"

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"run `{run_id}`" + (f" · {kinds}" if kinds else ""),
                }
            ],
        },
    ]
    if url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open report"},
                        "url": url,
                    }
                ],
            }
        )
    return {"text": text, "blocks": blocks}


def notify_done(
    settings: Settings,
    run: dict[str, Any],
    url: str = "",
    timeout_s: float = TIMEOUT_S,
) -> bool:
    """POST the run summary to Slack. Returns ``True`` when Slack accepted it.

    Never raises: an integration failure is logged at debug level and the run
    is unaffected.
    """
    webhook = (getattr(settings, "slack_webhook_url", "") or "").strip()
    if not webhook:
        return False
    body = json.dumps(slack_payload(run, url or report_url(settings, str(run.get("run_id")))))
    request = urllib.request.Request(  # noqa: S310 - the URL is an operator setting
        webhook,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            return 200 <= int(response.status) < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:  # pragma: no cover - network
        logger.debug("slack notify failed: %s", exc)
        return False


def notify_done_async(
    settings: Settings,
    run: dict[str, Any],
    url: str = "",
    timeout_s: float = TIMEOUT_S,
) -> threading.Thread | None:
    """Fire :func:`notify_done` on a daemon thread so a run never waits for it."""
    if not (getattr(settings, "slack_webhook_url", "") or "").strip():
        return None
    thread = threading.Thread(
        target=notify_done,
        args=(settings, run, url, timeout_s),
        name="litmus-slack",
        daemon=True,
    )
    thread.start()
    return thread
