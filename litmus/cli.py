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
    finding: str = typer.Option(..., "--finding", help="Finding id to re-verify."),
    apk: str = typer.Option(..., "--apk", help="Path to the fixed APK."),
) -> None:
    """Replay a saved finding trace against a fixed APK (available from M4)."""
    typer.secho(
        f"mqa verify-fix is not implemented yet - finding={finding} apk={apk}. It lands in M4.",
        fg="yellow",
    )
    raise typer.Exit(code=2)


def main() -> None:  # pragma: no cover - console script entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
