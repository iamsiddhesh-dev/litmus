"""``mqa`` - the Litmus command line entry point."""

from __future__ import annotations

import typer

from litmus.config import get_settings

_HELP = (
    "Litmus - a litmus test for mobile bugs. Explore, detect, replay 3x, "
    "report what survives."
)

app = typer.Typer(
    name="mqa",
    help=_HELP,
    no_args_is_help=True,
    add_completion=False,
)

_STYLE = {
    "PASS": "green",
    "FAIL": "red",
    "WARN": "yellow",
    "INFO": "cyan",
}


@app.command()
def doctor() -> None:
    """Check that the local environment can run Litmus."""
    from litmus.doctor import Status, doctor_exit_code, run_doctor_sync

    settings = get_settings()
    checks = run_doctor_sync(settings)

    width = max(len(check.name) for check in checks)
    for check in checks:
        style = _STYLE.get(check.status.value, "white")
        typer.secho(
            f"{check.status.value:<4}  {check.name:<{width}}  {check.detail}",
            fg=style,
        )

    failed = [
        check.name for check in checks if check.status is Status.FAIL and check.required
    ]
    if failed:
        typer.secho(f"\ndoctor failed: {', '.join(failed)}", fg="red", err=True)
        raise typer.Exit(code=doctor_exit_code(checks))
    typer.secho("\ndoctor ok: environment is ready", fg="green")


@app.command()
def run(
    apk: str = typer.Option(..., "--apk", help="Path to the APK under test."),
    profile: str = typer.Option("generic", "--profile", help="Profile name or path."),
    steps: int = typer.Option(25, "--steps", help="Explore step budget."),
    serial: str = typer.Option(None, "--serial", help="adb serial (default: attached emulator)."),
    artifacts_dir: str = typer.Option(
        None, "--artifacts-dir", help="Where run evidence is written (default: ./artifacts)."
    ),
) -> None:
    """Explore an APK, detect, replay 3x, and write evidence."""
    from pathlib import Path

    from litmus.worker.lifecycle import Lifecycle, RunOptions

    apk_path = Path(apk)
    if not apk_path.is_file():
        typer.secho(f"FAIL  apk not found: {apk_path}", fg="red", err=True)
        raise typer.Exit(code=2)

    _COLORS = {
        "run": "cyan",
        "status": "cyan",
        "device": "cyan",
        "install": "cyan",
        "launch": "cyan",
        "proxy": "cyan",
        "step": "white",
        "detect": "magenta",
        "crash": "red",
        "recover": "yellow",
        "verify": "cyan",
        "attempt": "white",
        "verdict": "green",
        "evidence": "green",
        "db": "green",
        "warn": "yellow",
        "error": "red",
    }

    def emit(kind: str, message: str) -> None:
        style = _COLORS.get(kind, "white")
        typer.secho(f"{kind:<7} {message}", fg=style)

    options = RunOptions(
        apk=apk_path,
        profile=profile,
        steps=steps,
        serial=serial,
        artifacts_dir=Path(artifacts_dir) if artifacts_dir else None,
    )
    result = Lifecycle(options, on_event=emit).run()

    typer.echo("")
    if result.error:
        typer.secho(f"run failed: {result.error}", fg="red", err=True)
    for verification, finding in zip(result.verifications, result.findings, strict=False):
        style = {
            "confirmed": "green",
            "flaky": "yellow",
            "filtered": "yellow",
        }.get(verification.verdict, "red")
        typer.secho(
            f"{verification.verdict} {verification.confirmations}/{verification.valid_attempts}"
            f"  {finding.local_id}  {finding.kind}  {finding.signature}",
            fg=style,
            bold=True,
        )
        if finding.evidence_dir:
            typer.echo(f"        evidence {finding.evidence_dir}")
    if not result.findings:
        typer.secho("no findings", fg="yellow")

    typer.echo(
        f"\nrun {result.run_id}  {result.status}  {result.duration_s:.1f}s  "
        f"{len(result.steps)} steps  ->  {result.run_dir}"
    )
    raise typer.Exit(code=result.exit_code)


@app.command()
def bench(
    fixture: str = typer.Option("bazaar", "--fixture", help="Fixture name (see fixtures/)."),
    runs: int = typer.Option(3, "--runs", help="Number of benchmark runs."),
    steps: int = typer.Option(25, "--steps", help="Explore step budget per run."),
    apk: str = typer.Option(None, "--apk", help="Override the fixture APK path."),
    serial: str = typer.Option(None, "--serial", help="adb serial."),
) -> None:
    """Score the platform against a fixture's ground truth (PLAN §14)."""
    import importlib.util
    import sys
    from pathlib import Path

    from litmus.config import REPO_ROOT

    script = REPO_ROOT / "benchmark" / "run_bench.py"
    if not script.is_file():
        typer.secho(f"FAIL  benchmark runner missing: {script}", fg="red", err=True)
        raise typer.Exit(code=2)
    spec = importlib.util.spec_from_file_location("litmus_bench", script)
    if spec is None or spec.loader is None:
        typer.secho(f"FAIL  cannot load {script}", fg="red", err=True)
        raise typer.Exit(code=2)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    argv = ["--fixture", fixture, "--runs", str(runs), "--steps", str(steps)]
    if apk:
        if not Path(apk).is_file():
            typer.secho(f"FAIL  apk not found: {apk}", fg="red", err=True)
            raise typer.Exit(code=2)
        argv += ["--apk", apk]
    if serial:
        argv += ["--serial", serial]
    raise typer.Exit(code=module.main(argv))


@app.command()
def verify_fix(
    finding: str = typer.Option(
        ...,
        "--finding",
        help="Finding to re-verify: F2, or <run_id>:F2.",
    ),
    apk: str = typer.Option(..., "--apk", help="Path to the fixed APK."),
    run: str = typer.Option(
        None, "--run", help="Run id (or run.json path) the finding came from."
    ),
    attempts: int = typer.Option(3, "--attempts", help="Clean replays required."),
    serial: str = typer.Option(None, "--serial", help="adb serial."),
    json_out: bool = typer.Option(False, "--json", help="Emit the verdict as JSON."),
) -> None:
    """Prove a fix: replay a saved finding against a new APK (PLAN §5.6).

    ``fixed`` (exit 0) means every required replay was valid and none of them
    reproduced the recorded signature. ``still reproduces`` (exit 1) is a
    verdict too. An *invalid* replay exits 2: an unreplayable trace is an
    error, never a fix.
    """
    import json as jsonlib
    from pathlib import Path

    from litmus.worker.verify_fix import VerifyFixError, run_verify_fix

    apk_path = Path(apk)
    if not apk_path.is_file():
        typer.secho(f"FAIL  apk not found: {apk}", fg="red", err=True)
        raise typer.Exit(code=2)

    def emit(kind: str, message: str) -> None:
        if not json_out:
            typer.echo(f"    {kind:<8} {message}")

    try:
        result = run_verify_fix(
            finding=finding,
            apk=apk_path,
            run=run,
            attempts=attempts,
            serial=serial,
            on_event=emit,
        )
    except VerifyFixError as exc:
        typer.secho(f"FAIL  {exc}", fg="red", err=True)
        raise typer.Exit(code=2) from exc

    if json_out:
        typer.echo(jsonlib.dumps(result.to_dict(), indent=2))
    else:
        style = _STYLE["PASS"] if result.verdict == "fixed" else (
            _STYLE["FAIL"] if result.verdict == "still reproduces" else _STYLE["WARN"]
        )
        typer.secho(f"\n{result.verdict}", fg=style, bold=True)
        typer.echo(f"  {result.reason}")
        typer.echo(
            f"  {result.finding_ref}  {result.kind}  {result.signature}  ({result.duration_s:.1f}s)"
        )
    raise typer.Exit(code=result.exit_code)


def main() -> None:  # pragma: no cover - console script entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
