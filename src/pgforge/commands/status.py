"""`pgforge ls` and `pgforge show`."""

from __future__ import annotations

from typing import Optional

import typer
from rich.panel import Panel
from rich.pretty import Pretty

from pgforge.commands._common import (
    emit_json,
    instance_table,
    is_json,
    print_console,
    store,
)
from pgforge.logging import out_console


def ls(
    ctx: typer.Context,
    provider: Optional[str] = typer.Option(None, "--provider", help="Filter by provider name."),
) -> None:
    """List all instances pgforge knows about (from local state)."""
    instances = store(ctx).list_instances()
    if provider:
        instances = [i for i in instances if i.provider == provider]
    rows = []
    for i in instances:
        rows.append(
            {
                "name": i.name,
                "provider": i.provider,
                "phase": i.phase.value,
                "size_gb": "?",  # we don't store this; pgforge show queries the cloud
                "location": i.provider_resources.location,
                "server": i.provider_resources.server_name or i.provider_resources.server_id,
                "kms": i.kms.backend,
            }
        )
    if is_json(ctx):
        emit_json(rows)
        return
    if not rows:
        out_console.print("[dim]no instances yet. Provision one with `pgforge provision`.[/dim]")
        return
    print_console(rows, json_out=False, table=instance_table(rows))


def show(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
) -> None:
    """Show full details for one instance."""
    inst = store(ctx).get_instance(name)
    if is_json(ctx):
        emit_json(inst.model_dump(mode="json"))
        return
    out_console.print(Panel.fit(Pretty(inst.model_dump(mode="json"), expand_all=True), title=name))
