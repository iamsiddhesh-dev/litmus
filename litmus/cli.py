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
) -> None:
    """Run an explore + detect + verify cycle against an APK (available from M1)."""
    typer.secho(
        "mqa run is not implemented yet - the runner lands in M1.\n"
        f"  requested: apk={apk} profile={profile} steps={steps}",
        fg="yellow",
    )
    raise typer.Exit(code=2)


@app.command()
def bench(
    fixture: str = typer.Option(..., "--fixture", help="Fixture name (see fixtures/)."),
    runs: int = typer.Option(3, "--runs", help="Number of benchmark runs."),
) -> None:
    """Score the platform against a fixture's ground truth (available from M3)."""
    typer.secho(
        f"mqa bench is not implemented yet - fixture={fixture} runs={runs}. It lands in M3.",
        fg="yellow",
    )
    raise typer.Exit(code=2)


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
