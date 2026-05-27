"""Typer CLI entry point.

Run ``pgforge --help`` to see all commands. The handlers themselves live in
``pgforge.commands.*`` — this file is the wiring.
"""

from __future__ import annotations

import sys
from typing import Optional

import typer

from pgforge import __version__
from pgforge.errors import PgforgeError, UserAbort
from pgforge.logging import configure as configure_logging
from pgforge.logging import err_console
from pgforge.providers._shell import set_dry_run

app = typer.Typer(
    name="pgforge",
    help="Multi-cloud CLI for encrypted-at-rest Postgres (LUKS + Docker).",
    no_args_is_help=True,
    rich_markup_mode="rich",
    add_completion=True,
)


# ---------------------------------------------------------------------------
# Global flags via top-level callback
# ---------------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pgforge {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    verbose: int = typer.Option(
        0, "-v", "--verbose", count=True, help="Increase log verbosity. Repeat for DEBUG."
    ),
    quiet: bool = typer.Option(False, "-q", "--quiet", help="Suppress info logs."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print actions without performing them."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON to stdout (where supported)."
    ),
    state_file: Optional[str] = typer.Option(
        None,
        "--state-file",
        envvar="PGFORGE_STATE_FILE",
        help="Override the path to state.json.",
    ),
    version: Optional[bool] = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """Global flags applied to every subcommand."""
    configure_logging(verbose=verbose, quiet=quiet)
    set_dry_run(dry_run)
    ctx.obj = {
        "verbose": verbose,
        "quiet": quiet,
        "dry_run": dry_run,
        "json": json_out,
        "state_file": state_file,
    }


# ---------------------------------------------------------------------------
# Subcommand registration
# ---------------------------------------------------------------------------


def _register_commands() -> None:
    """Import command modules lazily so ``pgforge --help`` stays fast."""
    from pgforge.commands import (
        destroy as destroy_cmd,
        doctor as doctor_cmd,
        key as key_cmd,
        metrics as metrics_cmd,
        provision as provision_cmd,
        snapshot as snapshot_cmd,
        ssh_psql as ssh_psql_cmd,
        state_cmd,
        status as status_cmd,
    )

    app.command("provision")(provision_cmd.provision)
    app.command("destroy")(destroy_cmd.destroy)
    app.command("ls")(status_cmd.ls)
    app.command("show")(status_cmd.show)
    app.command("doctor")(doctor_cmd.doctor)
    app.command("metrics")(metrics_cmd.metrics)
    app.command("capacity")(metrics_cmd.capacity)
    app.command("ssh")(ssh_psql_cmd.ssh_cmd)
    app.command("psql")(ssh_psql_cmd.psql_cmd)

    app.add_typer(snapshot_cmd.app, name="snapshot")
    app.add_typer(key_cmd.app, name="key")
    app.add_typer(state_cmd.app, name="state")


_register_commands()


# ---------------------------------------------------------------------------
# Top-level error handler
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        app(standalone_mode=False)
    except UserAbort as e:
        err_console.print(f"[yellow]aborted:[/yellow] {e}")
        sys.exit(e.exit_code)
    except PgforgeError as e:
        err_console.print(f"[red]error:[/red] {e}")
        sys.exit(e.exit_code)
    except typer.Exit as e:
        sys.exit(e.exit_code)
    except KeyboardInterrupt:
        err_console.print("[yellow]interrupted[/yellow]")
        sys.exit(130)
    except Exception:
        err_console.print_exception(show_locals=False)
        sys.exit(1)
