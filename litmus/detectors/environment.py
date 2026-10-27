"""Environment signals that must never become bugs (PLAN §7).

``unsupported_abi`` (an install/link failure on this device) and ``pinning``
(a TLS failure the proxy cannot read through) describe the environment, not the
app. They are reported in the **filtered** lane so the evidence is visible, and
they never count towards recall or false positives.
"""

from __future__ import annotations

from litmus.detectors.base import Finding

UNSUPPORTED_ABI = "unsupported_abi"
PINNING = "pinning"

ABI_MARKERS = (
    "INSTALL_FAILED_NO_MATCHING_ABIS",
    "INSTALL_FAILED_NO_MATCHING_ABI",
    "UnsatisfiedLinkError",
    "couldn't find \"lib",
)
PINNING_MARKERS = (
    "SSLPeerUnverifiedException",
    "CertificatePinner",
    "Trust anchor for certification path not found",
    "CertPathValidatorException",
    "TLS handshake failed",
    "SSLHandshakeException",
)


def classify_environment(text: str) -> str | None:
    """Return the environment kind a log/install text describes, if any."""
    blob = text or ""
    if any(marker in blob for marker in ABI_MARKERS):
        return UNSUPPORTED_ABI
    if any(marker in blob for marker in PINNING_MARKERS):
        return PINNING
    return None


def find_environment_signal(logcat: str, package: str) -> tuple[str, str] | None:
    """Scan a logcat dump for an environment signal tied to ``package``.

    Lines from other processes are ignored: a system app's TLS failure says
    nothing about the app under test.
    """
    for line in (logcat or "").splitlines():
        if package and package not in line:
            continue
        kind = classify_environment(line)
        if kind:
            return kind, line.strip()
    return None


def filtered_finding(
    kind: str,
    signature: str,
    local_id: str,
    detail: str = "",
    step_idx: int | None = None,
) -> Finding:
    """Build a finding that is visible in the filtered lane and never confirms."""
    return Finding(
        local_id=local_id,
        kind=kind,
        signature=signature,
        fingerprint=signature,
        confidence="heuristic",
        step_idx=step_idx,
        detail=detail,
        observed_once=True,
        verdict="filtered",
        probe={"filtered": True},
    )
