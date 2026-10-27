"""Bazaar-style benchmark (PLAN §14).

Runs the platform exactly as a user would (``steps=25``, full verification),
matches findings to the fixture's ground truth by **normalized signature**, and
writes ``benchmark/results/{timestamp}.json``.

    python benchmark/run_bench.py --fixture bazaar --runs 3
    mqa bench --fixture bazaar --runs 3

Scoring, straight from PLAN §14:

* ``recall`` = confirmed B1-B4 / 4;
* ``B5`` must be ``flaky`` when observed (a confirmed B5 is a false positive);
  ``N/A`` when the random trigger never fired is the honest answer;
* ``false_positives`` = confirmed findings that match no B1-B4 entry;
* ``determinism`` = runs with recall 4/4.

Nothing here changes the platform or the ground truth: a low score is reported,
never repaired.
"""

from __future__ import annotations

import argparse
import json
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EXPECTED_PATH = REPO_ROOT / "benchmark" / "expected_findings.json"
RESULTS_DIR = REPO_ROOT / "benchmark" / "results"
MOCK_API_HEALTH_TIMEOUT_S = 25.0

#: Kinds that are compared by their own signature shape.
SIGNATURE_KINDS = ("crash", "anr", "http_5xx", "http_4xx", "deadtap", "ui_broken")


# -- signatures ------------------------------------------------------------
def normalize_signature(kind: str, signature: str) -> str:
    """Canonical form for matching a finding to the ground truth."""
    sig = (signature or "").strip()
    if kind in {"http_5xx", "http_4xx"}:
        # ``500 POST /api/checkout`` - status, upper-case method, path without query.
        parts = sig.split()
        if len(parts) >= 3 and parts[0].isdigit():
            return f"{parts[0]} {parts[1].upper()} {parts[2].split('?', 1)[0]}"
        return sig.upper()
    if kind == "deadtap":
        return sig.rsplit(":", 1)[-1]
    if kind == "ui_broken":
        # ``overlap:tv_product_title,tv_product_price`` - rid pair is order-free.
        head, _, rest = sig.partition(":")
        if head == "overlap":
            return "overlap:" + ",".join(sorted(part.strip() for part in rest.split(",")))
        return sig
    return sig


def signatures_match(kind: str, expected: str, actual: str) -> bool:
    """Match a finding to one ground-truth signature.

    Crash signatures carry the exception and the message token; an extra frame
    on either side must not lose the match, so a substring in either direction
    counts once the exception class agrees.
    """
    left = normalize_signature(kind, expected)
    right = normalize_signature(kind, actual)
    if not left or not right:
        return False
    if left == right:
        return True
    if kind in {"crash", "anr"}:
        exception = left.split(":", 1)[0]
        if exception and exception not in right:
            return False
        return left in right or right in left
    return False


# -- fixtures --------------------------------------------------------------
def load_fixture(name: str) -> dict:
    path = REPO_ROOT / "fixtures" / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"fixture not found: {path}")
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_expected(path: Path = EXPECTED_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_apk(fixture: dict) -> Path:
    """Local path first (relative to the repo root), then the release URL."""
    apk = fixture.get("apk") or {}
    local = apk.get("local")
    if local:
        candidate = Path(local)
        if not candidate.is_absolute():
            candidate = (REPO_ROOT / candidate).resolve()
        if candidate.is_file():
            return candidate
    url = str(apk.get("url") or "")
    if url.startswith("http"):
        import httpx

        cache = REPO_ROOT / "artifacts" / f"{fixture.get('name', 'target')}.apk"
        cache.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
            response.raise_for_status()
            with cache.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        return cache
    raise FileNotFoundError(f"no APK for fixture (local={local!r}, url={url!r})")


def mock_api_command(fixture: dict) -> list[str] | None:
    raw = str((fixture.get("mock_api") or {}).get("cmd") or "").strip()
    if not raw:
        return None
    parts = shlex.split(raw)
    if parts and parts[0] in {"python", "python3", "py"}:
        parts[0] = sys.executable
    return parts


def mock_api_port(fixture: dict) -> int:
    return int((fixture.get("mock_api") or {}).get("port") or 0)


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_mock_api(fixture: dict, log_path: Path) -> subprocess.Popen | None:
    """Start the fixture's mock backend when it is not already listening."""
    port = mock_api_port(fixture)
    if not port:
        return None
    if port_open(port):
        print(f"mock API   already up on :{port}")
        return None
    command = mock_api_command(fixture)
    if not command:
        print(f"mock API   NOT running on :{port} and the fixture has no command", file=sys.stderr)
        return None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8", errors="replace")
    print(f"mock API   starting {' '.join(command)}")
    process = subprocess.Popen(  # noqa: S603 - fixture command, fixed argv
        command,
        cwd=str(REPO_ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    log.close()
    deadline = time.monotonic() + MOCK_API_HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if port_open(port):
            return process
        if process.poll() is not None:
            print(f"mock API   exited with {process.returncode}", file=sys.stderr)
            return process
        time.sleep(0.3)
    print(f"mock API   did not listen on :{port} in time", file=sys.stderr)
    return process


# -- scoring ---------------------------------------------------------------
@dataclass(slots=True)
class RunScore:
    index: int
    run_id: str = ""
    status: str = ""
    recall: int = 0
    total: int = 4
    confirmed: list[dict] = field(default_factory=list)
    flaky: list[dict] = field(default_factory=list)
    filtered: list[dict] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    false_positives: list[dict] = field(default_factory=list)
    b5: str = "N/A"
    duration_s: float = 0.0
    steps: int = 0
    error: str = ""

    @property
    def line(self) -> str:
        b5 = self.b5 if self.b5 in {"flaky", "N/A"} else self.b5
        return (
            f"run {self.index}  confirmed {self.recall}/{self.total}  "
            f"B5 {b5}  FP {len(self.false_positives)}"
        )

    def to_dict(self) -> dict:
        return asdict(self)


B5_ID = "B5"


def _match_expected(expected: dict, finding: dict) -> bool:
    return signatures_match(
        str(expected.get("kind") or ""),
        str(expected.get("signature") or ""),
        str(finding.get("signature") or ""),
    )


def score_run(index: int, expected: dict, findings: list[dict], **extra) -> RunScore:
    """Score one run's findings against the ground truth (pure function)."""
    entries = expected.get("expected") or []
    recall_entries = [entry for entry in entries if entry.get("id") != B5_ID]
    b5_entry = next((entry for entry in entries if entry.get("id") == B5_ID), None)
    score = RunScore(index=index, total=len(recall_entries))

    confirmed = [finding for finding in findings if finding.get("verdict") == "confirmed"]
    score.confirmed = [_summary(finding) for finding in confirmed]
    score.flaky = [_summary(finding) for finding in findings if finding.get("verdict") == "flaky"]
    score.filtered = [
        _summary(finding) for finding in findings if finding.get("verdict") == "filtered"
    ]

    claimed: set[int] = set()
    for entry in recall_entries:
        hit = next(
            (
                finding
                for position, finding in enumerate(confirmed)
                if position not in claimed and _match_expected(entry, finding)
            ),
            None,
        )
        if hit is None:
            score.missed.append(str(entry.get("id")))
            continue
        claimed.add(confirmed.index(hit))
        score.matched.append(str(entry.get("id")))
    score.recall = len(score.matched)

    score.false_positives = [
        _summary(finding)
        for position, finding in enumerate(confirmed)
        if position not in claimed
    ]

    if b5_entry is not None:
        observed = next(
            (finding for finding in findings if _match_expected(b5_entry, finding)), None
        )
        if observed is None:
            score.b5 = "N/A"
        else:
            verdict = str(observed.get("verdict") or "")
            score.b5 = verdict if verdict else "N/A"

    for key, value in extra.items():
        if hasattr(score, key):
            setattr(score, key, value)
    return score


def _summary(finding: dict) -> dict:
    return {
        "kind": finding.get("kind"),
        "signature": finding.get("signature"),
        "confidence": finding.get("confidence"),
        "verdict": finding.get("verdict"),
        "screen": finding.get("screen"),
        "step_idx": finding.get("step_idx"),
    }


# -- execution -------------------------------------------------------------
def run_once(
    apk: Path,
    profile: str,
    steps: int,
    run_id: str,
    artifacts_dir: Path | None = None,
    serial: str | None = None,
    quiet: bool = False,
):
    from litmus.worker.lifecycle import Lifecycle, RunOptions

    def emit(kind: str, message: str) -> None:
        if quiet:
            return
        if kind in {"status", "detect", "verdict", "proxy", "warn", "error", "recover"}:
            print(f"    {kind:<8} {message}")

    options = RunOptions(
        apk=apk,
        profile=profile,
        steps=steps,
        serial=serial,
        artifacts_dir=artifacts_dir,
        run_id=run_id,
    )
    return Lifecycle(options, on_event=emit).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score Litmus against a fixture's ground truth")
    parser.add_argument("--fixture", default="bazaar", help="fixture name under fixtures/")
    parser.add_argument("--runs", type=int, default=3, help="number of runs")
    parser.add_argument("--steps", type=int, default=25, help="explore step budget per run")
    parser.add_argument("--apk", default=None, help="override the fixture APK path")
    parser.add_argument("--serial", default=None, help="adb serial")
    parser.add_argument("--quiet", action="store_true", help="only print the score lines")
    parser.add_argument(
        "--expected",
        default=str(EXPECTED_PATH),
        help="ground-truth JSON (defaults to benchmark/expected_findings.json)",
    )
    args = parser.parse_args(argv)

    fixture = load_fixture(args.fixture)
    expected = load_expected(Path(args.expected))
    apk = Path(args.apk) if args.apk else resolve_apk(fixture)
    profile = str(fixture.get("profile") or f"profiles/{args.fixture}.yaml")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = RESULTS_DIR / f"{stamp}-mock-api.log"
    api_process = ensure_mock_api(fixture, log_path)

    print(f"benchmark  {args.fixture}  {expected.get('package', '?')}  apk {apk}")
    print(f"            {args.runs} runs x {args.steps} steps, proxy on, replay 3/3")

    scores: list[RunScore] = []
    try:
        for index in range(1, args.runs + 1):
            run_id = f"bench-{stamp}-{index}"
            print(f"\n--- run {index}/{args.runs}  {run_id}")
            result = run_once(
                apk=apk,
                profile=profile,
                steps=args.steps,
                run_id=run_id,
                serial=args.serial,
                quiet=args.quiet,
            )
            findings = [finding.to_dict() for finding in result.findings]
            score = score_run(
                index,
                expected,
                findings,
                run_id=run_id,
                status=result.status,
                duration_s=result.duration_s,
                steps=len(result.steps),
                error=result.error,
            )
            scores.append(score)
            print(score.line)
            for entry in score.false_positives:
                print(f"    FALSE POSITIVE  {entry['kind']}  {entry['signature']}")
            for missed in score.missed:
                print(f"    missed          {missed}")
    finally:
        if api_process is not None:
            api_process.terminate()
            try:
                api_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                api_process.kill()

    total = scores[0].total if scores else 4
    clean_runs = sum(
        1 for score in scores if score.recall == score.total and not score.false_positives
    )
    payload = {
        "fixture": args.fixture,
        "package": expected.get("package"),
        "version": expected.get("version"),
        "expected": args.expected,
        "started_at": stamp,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": {"steps": args.steps, "apk": str(apk), "profile": profile},
        "runs": [score.to_dict() for score in scores],
        "summary": {
            "runs": len(scores),
            "determinism": f"{clean_runs}/{len(scores)}",
            "recall": f"{min((score.recall for score in scores), default=0)}/{total}",
            "false_positives": sum(len(score.false_positives) for score in scores),
            "b5": [score.b5 for score in scores],
        },
    }
    results_path = RESULTS_DIR / f"{stamp}.json"
    results_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    summary = payload["summary"]
    print(
        f"\nbenchmark  {summary['determinism']} runs at {summary['recall']}  "
        f"FP {summary['false_positives']}  B5 {','.join(summary['b5'])}"
    )
    print(f"results    {results_path.relative_to(REPO_ROOT)}")

    clean = all(score.recall == score.total and not score.false_positives for score in scores)
    return 0 if clean and scores else 1


if __name__ == "__main__":
    raise SystemExit(main())
