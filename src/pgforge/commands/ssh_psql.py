"""`pgforge ssh <name>` and `pgforge psql <name>` convenience commands."""

from __future__ import annotations

import os
import shlex

import typer

from pgforge.commands._common import store
from pgforge.errors import PgforgeError


def ssh_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
) -> None:
    """SSH to the server hosting the given instance."""
    inst = store(ctx).get_instance(name)
    host = inst.ssh.host
    target = f"{inst.ssh.user}@{host}"
    args = ["ssh", "-p", str(inst.ssh.port), target]
    os.execvp("ssh", args)


def psql_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
) -> None:
    """Open psql inside the Postgres container on the target server."""
    inst = store(ctx).get_instance(name)
    remote = f"docker exec -it {shlex.quote(inst.postgres.container_name)} psql -U postgres"
    args = ["ssh", "-t", "-p", str(inst.ssh.port), f"{inst.ssh.user}@{inst.ssh.host}", remote]
    try:
        os.execvp("ssh", args)
    except OSError as e:
        raise PgforgeError(f"could not exec ssh: {e}") from e
