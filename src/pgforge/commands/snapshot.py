"""`pgforge snapshot` subcommands."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import typer
from rich.table import Table

from pgforge import __version__
from pgforge.commands._common import (
    confirm_destructive,
    emit_json,
    is_json,
    store,
    warn,
)
from pgforge.errors import ConfigError, PgforgeError, SnapshotError
from pgforge.logging import get_logger, out_console
from pgforge.providers.registry import get_provider
from pgforge.remote.ssh import RemoteHost
from pgforge.snapshot.policy import parse_retention, prune_with_policy
from pgforge.snapshot.scheduler import (
    cron_file_path,
    install_schedule,
    remove_schedule,
    runner_path,
)

log = get_logger(__name__)

app = typer.Typer(help="Snapshot management.")


def _parse_labels(label: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for kv in label or []:
        if "=" not in kv:
            raise ConfigError(f"label must be key=value, got {kv!r}")
        k, v = kv.split("=", 1)
        out[k] = v
    return out


@app.command("create")
def create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    label: list[str] = typer.Option(None, "--label", help="Extra label, key=value. Repeatable."),
    quiesce: bool = typer.Option(
        False,
        "--quiesce",
        help=(
            "Wrap the snapshot in pg_backup_start/stop for a SQL-consistent capture. "
            "Adds ~1s of overhead per snapshot."
        ),
    ),
) -> None:
    """Take an ad-hoc snapshot now."""
    inst = store(ctx).get_instance(name)
    provider = get_provider(inst.provider)
    labels = {
        "pgforge-instance": inst.name,
        "pgforge-luks-uuid": inst.luks.uuid or "",
        "pgforge-version": __version__,
        "pgforge-quiesce": "true" if quiesce else "false",
    }
    labels.update(_parse_labels(label))
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    snap_name = f"pgforge-{name}-{ts}"

    if quiesce:
        # SQL-consistent: pg_backup_start before, pg_backup_stop after. The
        # CHECKPOINT inside pg_backup_start flushes WAL; pg_backup_stop emits
        # a backup_label file that restore picks up on recovery.
        with RemoteHost(host=inst.ssh.host, user=inst.ssh.user, port=inst.ssh.port) as rh:
            _pg_backup_start(rh, inst.postgres.container_name, name)
            try:
                snap = provider.create_snapshot(inst.provider_resources.volume_id, snap_name, labels)
            finally:
                _pg_backup_stop(rh, inst.postgres.container_name, name)
    else:
        snap = provider.create_snapshot(inst.provider_resources.volume_id, snap_name, labels)

    if is_json(ctx):
        emit_json({"id": snap.id, "name": snap.name, "size_gb": snap.size_gb, "created_at": str(snap.created_at), "quiesce": quiesce})
        return
    out_console.print(
        f"[green]created[/green] {snap.id} ({snap.name})"
        + (" [dim](SQL-consistent)[/dim]" if quiesce else "")
    )


def _pg_backup_start(rh: RemoteHost, container: str, name: str) -> None:
    import shlex

    label = f"pgforge-{name}"
    rh.run(
        f"docker exec {shlex.quote(container)} psql -U postgres -tAc "
        f"\"SELECT pg_backup_start('{label}', true);\"",
        check=True,
    )


def _pg_backup_stop(rh: RemoteHost, container: str, name: str) -> None:
    import shlex

    rh.run(
        f"docker exec {shlex.quote(container)} psql -U postgres -tAc "
        f"\"SELECT pg_backup_stop();\"",
        check=False,  # best-effort; if start failed we still want stop attempted
    )


@app.command("ls")
def list_snapshots(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Instance name; omit with --all."),
    all_: bool = typer.Option(False, "--all", help="List snapshots across all instances."),
) -> None:
    """List snapshots for one instance or all of them."""
    if not name and not all_:
        raise ConfigError("provide an instance name or pass --all")
    s = store(ctx)
    rows = []
    instances = list(s.list_instances()) if all_ else [s.get_instance(name)]  # type: ignore[arg-type]
    for inst in instances:
        provider = get_provider(inst.provider)
        for snap in provider.list_snapshots(inst.provider_resources.volume_id):
            rows.append(
                {
                    "instance": inst.name,
                    "id": snap.id,
                    "name": snap.name,
                    "size_gb": snap.size_gb,
                    "created_at": snap.created_at.isoformat(),
                }
            )
    if is_json(ctx):
        emit_json(rows); return
    t = Table(title="snapshots", header_style="bold")
    for c in ["instance", "id", "name", "size_gb", "created_at"]:
        t.add_column(c)
    for r in rows:
        t.add_row(*[str(r[c]) for c in ["instance", "id", "name", "size_gb", "created_at"]])
    out_console.print(t)


@app.command("delete")
def delete(
    ctx: typer.Context,
    snapshot_id: str = typer.Argument(..., help="Snapshot id from `pgforge snapshot ls`."),
    instance: str = typer.Option(..., "--instance", help="Instance the snapshot belongs to."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation."),
) -> None:
    """Delete one snapshot."""
    inst = store(ctx).get_instance(instance)
    confirm_destructive(snapshot_id, force=force, prompt=f"This will delete snapshot {snapshot_id}.")
    get_provider(inst.provider).delete_snapshot(snapshot_id)
    out_console.print(f"[green]deleted[/green] {snapshot_id}")


@app.command("restore")
def restore(
    ctx: typer.Context,
    snapshot_id: str = typer.Argument(..., help="Snapshot id."),
    instance: str = typer.Option(..., "--instance", help="Source instance (for provider lookup)."),
    new_name: str = typer.Option(..., "--to", help="Name for the new restored instance."),
    target_server: Optional[str] = typer.Option(
        None, "--server", help="Server to attach the restored volume to (defaults to source server)."
    ),
    port: int = typer.Option(5433, "--postgres-port", help="Host port for the restored container."),
    container: Optional[str] = typer.Option(None, "--container-name", help="Docker container name."),
) -> None:
    """Restore a snapshot into a new volume + container."""
    from pgforge.commands._restore import run_restore

    run_restore(
        ctx,
        snapshot_id=snapshot_id,
        source_instance=instance,
        new_name=new_name,
        target_server=target_server,
        postgres_port=port,
        container_name=container,
    )


@app.command("prune")
def prune(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    retention: Optional[str] = typer.Option(
        None,
        "--retain",
        help="Retention spec (defaults to the schedule's --retain).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print would-delete list and exit."),
) -> None:
    """Apply the retention policy and delete expired snapshots.

    The cron runner on the DB server creates snapshots only — pruning is
    centralized here so the operator can audit deletions. Pruning is also a
    safe operation to run from a CI cron or external scheduler.
    """
    inst = store(ctx).get_instance(name)
    policy_str = retention or (inst.snapshot_schedule.retention or "")
    if not policy_str:
        raise ConfigError(
            f"{name!r} has no retention policy. Pass --retain or attach one via "
            f"`pgforge snapshot schedule {name} --cron ... --retain ...`."
        )
    policy = parse_retention(policy_str)
    provider = get_provider(inst.provider)
    snaps = provider.list_snapshots(inst.provider_resources.volume_id)
    snaps = [s for s in snaps if s.labels.get("pgforge-instance") == name]
    to_prune = prune_with_policy([(s.id, s.created_at) for s in snaps], policy)

    if is_json(ctx):
        emit_json({"instance": name, "retention": policy_str, "to_delete": to_prune, "dry_run": dry_run})
    else:
        out_console.print(
            f"retention={policy_str}; have {len(snaps)} snapshot(s); would delete {len(to_prune)}"
        )
        for sid in to_prune:
            out_console.print(f"  - {sid}")

    if dry_run:
        return
    for sid in to_prune:
        try:
            provider.delete_snapshot(sid)
        except PgforgeError as e:
            warn(f"could not delete {sid}: {e}")


@app.command("schedule")
def schedule(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    cron: Optional[str] = typer.Option(None, "--cron", help="Cron expression (5 fields, UTC)."),
    retention: Optional[str] = typer.Option(None, "--retain", help="Retention spec, e.g. 7d,4w,3m."),
    disable: bool = typer.Option(False, "--disable", help="Remove the schedule."),
    show_: bool = typer.Option(False, "--show", help="Print the active schedule."),
    quiesce: bool = typer.Option(
        False, "--quiesce",
        help="Wrap each scheduled snapshot in pg_backup_start/stop (SQL-consistent).",
    ),
) -> None:
    """Install/update/remove the server-side snapshot cron entry."""
    s = store(ctx)
    inst = s.get_instance(name)

    if show_:
        sch = inst.snapshot_schedule
        if is_json(ctx):
            emit_json(sch.model_dump(mode="json")); return
        out_console.print(sch.model_dump(mode="json"))
        return

    if disable:
        with RemoteHost(host=inst.ssh.host, user=inst.ssh.user, port=inst.ssh.port) as rh:
            remove_schedule(rh, name)
        with s.transaction() as state:
            inst2 = state.instances[name]
            inst2.snapshot_schedule.enabled = False
            inst2.snapshot_schedule.installed_unit = None
            inst2.touch()
        out_console.print(f"[green]disabled[/green] snapshot schedule for {name}")
        return

    if not cron or not retention:
        raise ConfigError("provide both --cron and --retain to install a schedule")
    parse_retention(retention)  # validate
    provider = get_provider(inst.provider)
    # Mint or refresh credential on the server.
    from pgforge.providers.base import SnapshotScope

    cred = provider.mint_snapshot_credential(
        SnapshotScope(
            instance_name=inst.name,
            volume_id=inst.provider_resources.volume_id,
            server_id=inst.provider_resources.server_id,
            location=inst.provider_resources.location,
        )
    )
    credential_content: Optional[str] = None
    if cred.mode == "file_credential" and cred.files:
        first_path, first_body = next(iter(cred.files.items()))
        credential_content = first_body
        log.debug("credential file body length: %d", len(first_body))
        _ = first_path
    elif cred.env_exports:
        lines = [f'export {k}={v!r}' for k, v in cred.env_exports.items()]
        credential_content = "\n".join(lines) + "\n"
    else:
        warn(
            f"provider {inst.provider} returned no file credential — assuming an "
            f"instance role is attached server-side."
        )

    with RemoteHost(host=inst.ssh.host, user=inst.ssh.user, port=inst.ssh.port) as rh:
        cron_p, runner_p = install_schedule(
            rh,
            instance_name=inst.name,
            provider=inst.provider,
            volume_id=inst.provider_resources.volume_id,
            luks_uuid=inst.luks.uuid or "",
            container_name=inst.postgres.container_name,
            cron_expression=cron,
            retention=retention,
            credential_content=credential_content,
            quiesce=quiesce,
        )

    with s.transaction() as state:
        inst2 = state.instances[name]
        inst2.snapshot_schedule.enabled = True
        inst2.snapshot_schedule.cron = cron
        inst2.snapshot_schedule.retention = retention
        inst2.snapshot_schedule.installed_unit = cron_p
        inst2.snapshot_schedule.credential_id = cred.credential_id
        from pgforge.state.schema import CredentialMeta

        inst2.credentials[cred.credential_id] = CredentialMeta(
            credential_id=cred.credential_id,
            last4=cred.last4,
            created_at=datetime.now(timezone.utc),
            provider=inst.provider,
        )
        inst2.touch()
    out_console.print(
        f"[green]installed[/green] cron at {cron_p} running {runner_p} on {inst.ssh.host}"
    )


@app.command("health")
def health(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    grace_minutes: int = typer.Option(60, "--grace", help="Tolerance beyond schedule interval."),
) -> None:
    """Check whether the most recent snapshot is fresh enough.

    Compares ``max(created_at over snapshots labelled pgforge-instance=<name>)``
    against now. Returns exit code 0 if fresh, non-zero if stale — so external
    monitoring can shell-test the staleness.
    """
    inst = store(ctx).get_instance(name)
    if not inst.snapshot_schedule.enabled or not inst.snapshot_schedule.cron:
        raise PgforgeError(f"{name!r} has no active schedule (nothing to check)")
    provider = get_provider(inst.provider)
    snaps = provider.list_snapshots(inst.provider_resources.volume_id)
    snaps = [s for s in snaps if s.labels.get("pgforge-instance") == name]
    if not snaps:
        raise PgforgeError(f"{name!r}: no pgforge snapshots found")
    snaps.sort(key=lambda s: s.created_at, reverse=True)
    newest = snaps[0]
    age_min = int((datetime.now(timezone.utc) - newest.created_at).total_seconds() // 60)
    # We don't try to parse cron expressions here — just enforce: newest snapshot
    # must be younger than 24h + grace. Honest crude default for MVP.
    threshold = 24 * 60 + grace_minutes
    payload = {
        "instance": name,
        "newest_snapshot_id": newest.id,
        "newest_snapshot_age_minutes": age_min,
        "threshold_minutes": threshold,
        "fresh": age_min <= threshold,
    }
    if is_json(ctx):
        emit_json(payload); return
    if payload["fresh"]:
        out_console.print(f"[green]ok[/green]  {name}: newest snapshot is {age_min}m old (<= {threshold}m)")
    else:
        out_console.print(f"[red]stale[/red]  {name}: newest snapshot is {age_min}m old (> {threshold}m)")
        raise PgforgeError(f"snapshot for {name} is stale ({age_min} min)")


# Internal helpers used by the cron prune path elsewhere.
def apply_retention_locally(
    *,
    snapshots: list[tuple[str, datetime]],
    retention: str,
) -> list[str]:
    return prune_with_policy(snapshots, parse_retention(retention))


# Used to silence unused-import warnings when this module is reexported.
_ = (cron_file_path, runner_path)
