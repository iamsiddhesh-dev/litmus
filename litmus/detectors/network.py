"""Network detector (PLAN §7, contract §3.1, §12.3).

mitmproxy records one JSON line per flow; this module classifies those lines.
Bazaar is plain HTTP on purpose, so no CA is installed: a 500 on
``POST /api/checkout`` is observed directly.

False-positive guards (all of them are load-bearing):

* ``BAZAAR_NET: 0`` / connection refused is a **transport failure**, not a
  5xx. mitmproxy answers a dead upstream with a *generated* 502, so a flow that
  carries an error or a ``Server: mitmproxy`` header is transport, never a bug.
* Only hosts the profile allows are classified (``allowed_hosts``), so emulator
  system traffic cannot become a finding.
* One ``(status, method, path)`` is one finding, however many flows repeat it.
* 2xx/3xx never produce a finding.
* TLS failures are ``pinning``: filtered, never a bug.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from litmus.detectors.base import Finding

NETWORK_ERROR = "network_error"
PINNING = "pinning"
HTTP_5XX = "http_5xx"
HTTP_4XX = "http_4xx"

#: Emulator alias for the host loopback (contract §6).
EMULATOR_HOST_ALIAS = "10.0.2.2"


@dataclass(slots=True)
class Flow:
    """One request/response pair as written by the proxy addon."""

    method: str = ""
    path: str = ""
    host: str = ""
    port: int = 0
    scheme: str = "http"
    status: int | None = None
    error: str = ""
    tls_error: bool = False
    proxy_generated: bool = False
    ts: float = 0.0

    @property
    def hostport(self) -> str:
        return f"{self.host}:{self.port}" if self.port else self.host

    @property
    def clean_path(self) -> str:
        return (self.path or "").split("?", 1)[0]

    @property
    def signature(self) -> str:
        return f"{self.status} {self.method} {self.clean_path}"

    @property
    def transport_failure(self) -> bool:
        """True when no HTTP status was observed (harness/network problem)."""
        if self.tls_error:
            return False
        if self.error:
            return True
        if self.proxy_generated:
            return True
        return self.status in (None, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "method": self.method,
            "path": self.clean_path,
            "host": self.host,
            "port": self.port,
            "scheme": self.scheme,
            "error": self.error,
            "tls_error": self.tls_error,
            "proxy_generated": self.proxy_generated,
            "ts": self.ts,
        }


def app_flows(flows: list[Flow], detector: NetworkDetector | None = None) -> list[Flow]:
    """Only the flows a tap could plausibly have caused (PLAN §7).

    The proxy also records emulator system traffic: connectivity probes to
    ``/generate_204``, TLS handshakes from other processes that reject our CA,
    Play services, and so on. None of those can be an *effect of a tap*, so
    counting them would suppress a genuine dead tap (B3) at random.
    """
    if detector is None:
        return list(flows)
    return [flow for flow in flows if detector.in_scope(flow)]


def parse_flows(text: str) -> list[Flow]:
    """Parse a ``flows.jsonl`` document, ignoring torn/json-invalid lines."""
    flows: list[Flow] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        status = record.get("status")
        flows.append(
            Flow(
                method=str(record.get("method") or ""),
                path=str(record.get("path") or ""),
                host=str(record.get("host") or ""),
                port=int(record.get("port") or 0),
                scheme=str(record.get("scheme") or "http"),
                status=int(status) if isinstance(status, int) else None,
                error=str(record.get("error") or ""),
                tls_error=bool(record.get("tls_error")),
                proxy_generated=bool(record.get("proxy_generated")),
                ts=float(record.get("ts") or 0.0),
            )
        )
    return flows


def load_flows(path: Path) -> list[Flow]:
    if not path.is_file():
        return []
    return parse_flows(path.read_text(encoding="utf-8", errors="replace"))


@dataclass(slots=True)
class NetworkEvent:
    """A classified flow.

    ``kind == network_error`` is a warning, not a finding: the harness failed to
    reach the backend, so nothing about the app can be concluded.
    """

    kind: str
    signature: str
    fingerprint: str
    confidence: str
    detail: str
    flow: Flow = field(default_factory=Flow)

    @property
    def is_finding(self) -> bool:
        return self.kind in {HTTP_5XX, HTTP_4XX}

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "signature": self.signature,
            "confidence": self.confidence,
            "detail": self.detail,
            "flow": self.flow.to_dict(),
        }


class NetworkDetector:
    """Classifies proxy flows for one app."""

    name = "network"

    def __init__(
        self,
        allowed_hosts: list[str] | None = None,
        http_4xx: bool = False,
        http_4xx_paths: list[str] | None = None,
        http_4xx_statuses: list[int] | None = None,
    ) -> None:
        self.allowed_hosts = [host.strip() for host in (allowed_hosts or []) if host.strip()]
        self.http_4xx = http_4xx
        self.http_4xx_paths = http_4xx_paths or ["/api/"]
        self.http_4xx_statuses = http_4xx_statuses or [404]

    # -- scoping ----------------------------------------------------------
    def in_scope(self, flow: Flow) -> bool:
        if not self.allowed_hosts:
            return True
        return flow.hostport in self.allowed_hosts

    # -- classification ---------------------------------------------------
    def classify(self, flow: Flow, degraded_hosts: set[str] | None = None) -> NetworkEvent | None:
        if not self.in_scope(flow):
            return None

        if flow.tls_error:
            return NetworkEvent(
                kind=PINNING,
                signature=f"pinning:{flow.hostport}",
                fingerprint=f"pinning:{flow.hostport}",
                confidence="heuristic",
                detail=f"TLS handshake failed for {flow.hostport}: {flow.error}",
                flow=flow,
            )

        if flow.transport_failure:
            return NetworkEvent(
                kind=NETWORK_ERROR,
                signature=f"network_error:{flow.hostport}{flow.clean_path}",
                fingerprint=f"network_error:{flow.hostport}{flow.clean_path}",
                confidence="hard",
                detail=(
                    f"{flow.method} {flow.hostport}{flow.clean_path} failed in transport"
                    + (f" ({flow.error})" if flow.error else "")
                    + " - harness/network issue, not an HTTP status"
                ),
                flow=flow,
            )

        status = int(flow.status or 0)
        if status >= 500:
            if degraded_hosts and flow.hostport in degraded_hosts:
                # The backend is not up; a 5xx cannot be attributed to the app.
                return NetworkEvent(
                    kind=NETWORK_ERROR,
                    signature=f"network_error:{flow.hostport}",
                    fingerprint=f"network_error:{flow.hostport}",
                    confidence="hard",
                    detail=(
                        f"{flow.hostport} was unreachable before the run; "
                        f"{flow.signature} is harness noise"
                    ),
                    flow=flow,
                )
            return NetworkEvent(
                kind=HTTP_5XX,
                signature=flow.signature,
                fingerprint=flow.signature,
                confidence="hard",
                detail=f"server error {flow.signature}",
                flow=flow,
            )

        if 400 <= status < 500:
            if not self.http_4xx:
                return None
            if status not in self.http_4xx_statuses:
                return None
            if not any(flow.clean_path.startswith(prefix) for prefix in self.http_4xx_paths):
                return None
            return NetworkEvent(
                kind=HTTP_4XX,
                signature=flow.signature,
                fingerprint=flow.signature,
                confidence="heuristic",
                detail=f"client error {flow.signature}",
                flow=flow,
            )

        return None

    def detect(
        self, flows: list[Flow], degraded_hosts: set[str] | None = None
    ) -> list[NetworkEvent]:
        """Classify every flow, de-duplicated by ``(kind, signature)``."""
        seen: set[tuple[str, str]] = set()
        events: list[NetworkEvent] = []
        for flow in flows:
            event = self.classify(flow, degraded_hosts)
            if event is None:
                continue
            key = (event.kind, event.signature)
            if key in seen:
                continue
            seen.add(key)
            events.append(event)
        return events

    # -- findings ---------------------------------------------------------
    def to_finding(
        self, event: NetworkEvent, local_id: str, step_idx: int | None = None
    ) -> Finding:
        return Finding(
            local_id=local_id,
            kind=event.kind,
            signature=event.signature,
            fingerprint=event.fingerprint,
            confidence=event.confidence,
            step_idx=step_idx,
            detail=event.detail,
            log_text=json.dumps(event.flow.to_dict(), indent=2),
            probe={"signature": event.signature, "flow": event.flow.to_dict()},
            evidence={"flows.json": event.flow.to_dict()},
        )


# -- backend reachability -------------------------------------------------
def host_loopback(host: str) -> str:
    """Map the emulator's host alias to the host loopback for a pre-flight check."""
    return "127.0.0.1" if host == EMULATOR_HOST_ALIAS else host


def split_hostport(hostport: str) -> tuple[str, int]:
    host, _, port = hostport.rpartition(":")
    if not host:
        return hostport, 0
    try:
        return host, int(port)
    except ValueError:
        return hostport, 0


def backend_reachable(hostport: str, timeout_s: float = 1.5) -> bool:
    """True when the backend behind a profile host answers a TCP connect."""
    host, port = split_hostport(hostport)
    if not host or not port:
        return True
    try:
        with socket.create_connection((host_loopback(host), port), timeout=timeout_s):
            return True
    except OSError:
        return False


def unreachable_hosts(hosts: list[str]) -> set[str]:
    """Hosts from the profile that are not listening on this machine."""
    return {host for host in hosts if not backend_reachable(host)}
