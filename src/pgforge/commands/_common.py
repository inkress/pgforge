"""Shared helpers for command modules."""

from __future__ import annotations

import json
import sys
from typing import Any

import typer
from rich.table import Table

from pgforge.config.paths import get_paths
from pgforge.logging import err_console, out_console
from pgforge.state.store import StateStore


def store(ctx: typer.Context | None = None) -> StateStore:
    """Build a StateStore honoring ``--state-file`` from the Typer context."""
    override = None
    if ctx is not None and ctx.obj:
        override = ctx.obj.get("state_file")
    paths = get_paths(state_file_override=override) if override else get_paths()
    return StateStore(paths)


def is_json(ctx: typer.Context | None) -> bool:
    if ctx is None or not ctx.obj:
        return False
    return bool(ctx.obj.get("json"))


def emit_json(payload: Any) -> None:
    """Print machine-readable JSON to stdout."""
    print(json.dumps(payload, indent=2, default=str), file=sys.stdout)


def confirm_destructive(name: str, *, force: bool, prompt: str) -> None:
    """Require typed-name confirmation unless ``force`` or env override."""
    import os

    if force or os.environ.get("PGFORGE_ASSUME_YES") == "1":
        return
    answer = typer.prompt(
        f"{prompt} Type the instance name to confirm",
        default="",
        show_default=False,
    )
    if answer.strip() != name:
        from pgforge.errors import UserAbort

        raise UserAbort(f"confirmation did not match {name!r}; nothing was changed")


def instance_table(rows: list[dict[str, Any]], *, title: str = "instances") -> Table:
    table = Table(title=title, show_lines=False, header_style="bold")
    cols = ["name", "provider", "phase", "size_gb", "location", "server", "kms"]
    for c in cols:
        table.add_column(c)
    for r in rows:
        table.add_row(*[str(r.get(c, "")) for c in cols])
    return table


def print_console(value: Any, *, json_out: bool, table: Table | None = None) -> None:
    if json_out:
        emit_json(value)
    elif table is not None:
        out_console.print(table)
    else:
        out_console.print(value)


def warn(msg: str) -> None:
    err_console.print(f"[yellow]warning:[/yellow] {msg}")
