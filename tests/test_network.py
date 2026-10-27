"""Network detector: the B2 signature and every false-positive guard."""

from __future__ import annotations

import json

from litmus.detectors.network import (
    HTTP_4XX,
    HTTP_5XX,
    NETWORK_ERROR,
    PINNING,
    Flow,
    NetworkDetector,
    app_flows,
    backend_reachable,
    load_flows,
    parse_flows,
    split_hostport,
    unreachable_hosts,
)

BAZAAR_HOST = "10.0.2.2:8100"


def flow(status=500, method="POST", path="/api/checkout", **kwargs) -> Flow:
    return Flow(
        status=status,
        method=method,
        path=path,
        host=kwargs.pop("host", "10.0.2.2"),
        port=kwargs.pop("port", 8100),
        **kwargs,
    )


def detector(**kwargs) -> NetworkDetector:
    return NetworkDetector(allowed_hosts=[BAZAAR_HOST], **kwargs)


# -- parsing ---------------------------------------------------------------
def test_parse_flows_ignores_torn_and_malformed_lines():
    text = "\n".join(
        [
            json.dumps({"status": 500, "method": "POST", "path": "/api/checkout",
                        "host": "10.0.2.2", "port": 8100}),
            '{"status": 200, "method": "GET", "path": "/api/prod',
            "not json at all",
            json.dumps({"status": 200, "method": "GET", "path": "/api/products?page=2",
                        "host": "10.0.2.2", "port": 8100}),
        ]
    )
    flows = parse_flows(text)
    assert [flow.status for flow in flows] == [500, 200]
    assert flows[1].clean_path == "/api/products"


def test_load_flows_missing_file_is_empty(tmp_path):
    assert load_flows(tmp_path / "nope.jsonl") == []


# -- B2 --------------------------------------------------------------------
def test_b2_500_is_a_hard_finding_with_the_ground_truth_signature():
    events = detector().detect([flow(500, "POST", "/api/checkout")])
    assert len(events) == 1
    event = events[0]
    assert event.kind == HTTP_5XX
    assert event.confidence == "hard"
    assert event.signature == "500 POST /api/checkout"


def test_one_signature_across_replays_is_one_finding():
    flows = [flow() for _ in range(3)] + [flow(503, "GET", "/api/x")]
    events = detector().detect(flows)
    assert [event.signature for event in events] == ["500 POST /api/checkout", "503 GET /api/x"]


def test_2xx_never_produces_a_finding():
    flows = [flow(200, "POST", "/api/login"), flow(200, "GET", "/api/products"),
             flow(302, "GET", "/redirect")]
    assert detector().detect(flows) == []


# -- false-positive guards -------------------------------------------------
def test_transport_failure_is_a_warning_not_a_5xx():
    # BAZAAR_NET: 0 - the app could not reach the mock API at all.
    events = detector().detect([flow(status=0)])
    assert [event.kind for event in events] == [NETWORK_ERROR]
    assert not events[0].is_finding


def test_missing_status_is_transport():
    assert detector().detect([flow(status=None)]).__len__() == 1
    assert detector().detect([flow(status=None)])[0].kind == NETWORK_ERROR


def test_mitmproxy_generated_502_is_transport_not_a_bug():
    # mitmproxy answers a dead upstream itself; that is harness noise.
    events = detector().detect([flow(502, proxy_generated=True)])
    assert events[0].kind == NETWORK_ERROR
    assert not events[0].is_finding


def test_flow_error_with_a_status_is_still_transport():
    events = detector().detect([flow(500, error="ConnectionRefusedError")])
    assert events[0].kind == NETWORK_ERROR


def test_degraded_host_5xx_is_harness_noise():
    events = detector().detect([flow(500)], degraded_hosts={BAZAAR_HOST})
    assert events[0].kind == NETWORK_ERROR
    assert "unreachable" in events[0].detail


def test_other_hosts_are_out_of_scope():
    flows = [flow(500, host="connectivitycheck.gstatic.com", port=80,
                  path="/generate_204")]
    assert detector().detect(flows) == []


def test_generic_profile_without_allowed_hosts_sees_everything():
    events = NetworkDetector().detect([flow(500, host="api.example.com", port=443)])
    assert events[0].signature == "500 POST /api/checkout"


def test_app_flows_drop_the_emulator_system_traffic():
    """A dead tap must not be suppressed by a probe the tap cannot have caused."""
    probe = flow(204, "GET", "/generate_204", host="connectivitycheck.gstatic.com", port=80)
    foreign_tls = Flow(host="play.googleapis.com", port=443, tls_error=True)
    app = flow(500)

    assert app_flows([probe, foreign_tls, app], detector()) == [app]
    assert app_flows([probe, foreign_tls], detector()) == []
    # Without a detector every flow is kept (a generic profile has no scope).
    assert app_flows([probe, app]) == [probe, app]


def test_tls_failure_is_filtered_pinning():
    events = detector().detect([flow(status=None, tls_error=True,
                                     error="client rejected the proxy certificate")])
    assert events[0].kind == PINNING
    assert not events[0].is_finding


# -- 4xx -------------------------------------------------------------------
def test_4xx_is_off_by_default():
    assert detector().detect([flow(404, "GET", "/api/products")]) == []


def test_4xx_only_fires_on_api_paths_when_enabled():
    det = detector(http_4xx=True)
    assert [event.kind for event in det.detect([flow(404, "GET", "/api/products")])] == [
        HTTP_4XX
    ]
    assert det.detect([flow(404, "GET", "/favicon.ico")]) == []
    assert det.detect([flow(400, "POST", "/api/login")]) == []


# -- findings --------------------------------------------------------------
def test_finding_carries_the_flow_as_evidence():
    event = detector().detect([flow()])[0]
    finding = detector().to_finding(event, local_id="F2", step_idx=5)
    assert finding.kind == "http_5xx"
    assert finding.confidence == "hard"
    assert finding.step_idx == 5
    assert finding.evidence["flows.json"]["status"] == 500
    assert json.loads(finding.log_text)["path"] == "/api/checkout"


# -- reachability ----------------------------------------------------------
def test_split_hostport():
    assert split_hostport("10.0.2.2:8100") == ("10.0.2.2", 8100)
    assert split_hostport("host") == ("host", 0)


def test_backend_reachable_uses_host_loopback_for_the_emulator_alias():
    assert backend_reachable("10.0.2.2:9", timeout_s=0.2) is False


def test_unreachable_hosts_reports_only_dead_ones():
    assert unreachable_hosts(["10.0.2.2:9"]) == {"10.0.2.2:9"}
