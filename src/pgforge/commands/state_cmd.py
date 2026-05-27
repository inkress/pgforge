"""`pgforge state validate|repair|import|export`."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from pgforge.commands._common import emit_json, is_json, store
from pgforge.errors import PgforgeError
from pgforge.logging import out_console
from pgforge.providers.registry import get_provider

app = typer.Typer(help="Manage the local pgforge state file.")


@app.command("validate")
def validate(ctx: typer.Context) -> None:
    """Parse the state file and confirm it matches the current schema."""
    state = store(ctx).read()
    out_console.print(
        f"[green]ok[/green]  schema_version={state.schema_version}  instances={len(state.instances)}"
    )


@app.command("repair")
def repair(
    ctx: typer.Context,
    reconcile: bool = typer.Option(False, "--reconcile", help="Verify each instance against the cloud."),
) -> None:
    """Diff state vs. cloud reality. Reports drift; does not auto-delete."""
    s = store(ctx)
    state = s.read()
    issues: list[dict] = []
    for name, inst in state.instances.items():
        if not reconcile:
            issues.append({"instance": name, "check": "skipped", "note": "pass --reconcile to query the provider"})
            continue
        try:
            provider = get_provider(inst.provider)
            try:
                provider.get_volume(inst.provider_resources.volume_id)
                vol_ok = True
            except PgforgeError as e:
                vol_ok = False
                issues.append({"instance": name, "check": "volume", "note": str(e)})
            try:
                provider.get_server(inst.provider_resources.server_id)
                srv_ok = True
            except PgforgeError as e:
                srv_ok = False
                issues.append({"instance": name, "check": "server", "note": str(e)})
            if vol_ok and srv_ok:
                issues.append({"instance": name, "check": "ok", "note": "volume + server present"})
        except PgforgeError as e:
            issues.append({"instance": name, "check": "provider", "note": str(e)})
    if is_json(ctx):
        emit_json(issues); return
    for i in issues:
        color = "green" if i["check"] == "ok" else ("yellow" if i["check"] == "skipped" else "red")
        out_console.print(f"[{color}]{i['check']:<8}[/{color}] {i['instance']:<20} {i['note']}")


@app.command("export")
def export_state(
    ctx: typer.Context,
    out: Path = typer.Option(..., "--to", help="Destination path."),
) -> None:
    """Write the current state file to ``--to`` (verbatim copy)."""
    state = store(ctx).read()
    out.write_text(state.model_dump_json(indent=2))
    out.chmod(0o600)
    out_console.print(f"[green]wrote[/green] {out}")


@app.command("import")
def import_state(
    ctx: typer.Context,
    src: Path = typer.Option(..., "--from", help="Source path."),
    merge: bool = typer.Option(False, "--merge", help="Merge into existing state instead of replacing."),
) -> None:
    """Read a state file from ``--from`` into pgforge's state."""
    from pgforge.state.schema import StateFile

    raw = json.loads(src.read_text())
    incoming = StateFile.model_validate(raw)
    s = store(ctx)
    with s.transaction() as state:
        if merge:
            for k, v in incoming.instances.items():
                state.instances[k] = v
        else:
            state.instances = incoming.instances
            state.schema_version = incoming.schema_version
    out_console.print(f"[green]imported[/green] {len(incoming.instances)} instance(s) from {src}")


@app.command("rebuild")
def rebuild(
    ctx: typer.Context,
    from_cloud: bool = typer.Option(False, "--from-cloud", help="Re-derive instances from cloud snapshot labels."),
    provider: list[str] = typer.Option(
        None, "--provider", help="Limit to one or more provider names (repeatable)."
    ),
    apply_: bool = typer.Option(
        False, "--apply",
        help="Write the discovered instances to state. Without this flag, prints a dry-run report.",
    ),
) -> None:
    """Reconstruct state from cloud-side metadata.

    Use case: the operator's laptop is gone, but the cloud account still has
    all the volumes and snapshots tagged ``pgforge-instance=<name>``. This
    command scans the named providers, groups snapshots by instance, and
    re-creates the state file entries. It cannot recover server SSH details
    or the KMS handle on its own — those still need to be passed back in via
    ``--state-file`` or rotated.
    """
    if not from_cloud:
        raise PgforgeError("only --from-cloud is supported (other rebuild modes are roadmap items)")

    from pgforge.providers.registry import iter_implemented

    targets = list(provider) if provider else [p for p in iter_implemented() if p != "mock"]
    findings: list[dict] = []

    for prov_name in targets:
        try:
            p = get_provider(prov_name)
        except PgforgeError as e:
            findings.append({"provider": prov_name, "error": str(e)})
            continue
        try:
            snaps = p.list_snapshots(None)
        except PgforgeError as e:
            findings.append({"provider": prov_name, "error": str(e)})
            continue
        by_instance: dict[str, list] = {}
        for s in snaps:
            inst_name = s.labels.get("pgforge-instance") if s.labels else None
            if not inst_name:
                continue
            by_instance.setdefault(inst_name, []).append(s)
        for inst_name, group in by_instance.items():
            group.sort(key=lambda x: x.created_at, reverse=True)
            newest = group[0]
            findings.append({
                "provider": prov_name,
                "instance": inst_name,
                "snapshot_count": len(group),
                "newest_snapshot": newest.id,
                "newest_created_at": newest.created_at.isoformat(),
                "luks_uuid": newest.labels.get("pgforge-luks-uuid", ""),
                "source_volume_id": newest.source_volume_id,
            })

    if is_json(ctx):
        emit_json(findings); return
    for f in findings:
        if "error" in f:
            out_console.print(f"[red]error[/red]  {f['provider']}: {f['error']}")
            continue
        out_console.print(
            f"{f['provider']}/{f['instance']}: {f['snapshot_count']} snapshot(s); "
            f"newest {f['newest_snapshot']} @ {f['newest_created_at']} (luks_uuid={f['luks_uuid']})"
        )

    if not apply_:
        out_console.print("[dim](dry run; pass --apply to write to state)[/dim]")
        return

    # Write minimal state entries for discovered instances.
    # NOTE: we cannot recover SSH/Postgres/KMS-key data from snapshot labels
    # alone. The operator must update those by other means (kms key handle
    # restore, ssh re-configure, then `pgforge state validate`).
    raise PgforgeError(
        "automatic state apply isn't safe without SSH + KMS context. "
        "Use the report to populate `--state-file --import-from <hand-edited.json>`."
    )
