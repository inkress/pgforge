"""`pgforge metrics` and `pgforge capacity`."""

from __future__ import annotations

from datetime import timedelta

import typer
from rich.table import Table

from pgforge.commands._common import emit_json, is_json, store
from pgforge.errors import PgforgeError
from pgforge.logging import out_console
from pgforge.metrics.collector import collect_filesystem, collect_postgres, now_utc
from pgforge.metrics.schema import CapacityReport
from pgforge.providers.base import TimeWindow
from pgforge.providers.registry import get_provider
from pgforge.remote.ssh import RemoteHost


def _parse_window(text: str) -> TimeWindow:
    units = {"h": 1, "d": 24, "w": 7 * 24}
    text = text.strip().lower()
    if not text or text[-1] not in units:
        raise PgforgeError(f"bad --window {text!r}; expected forms like 1h, 24h, 7d")
    n = int(text[:-1])
    return TimeWindow.last(timedelta(hours=n * units[text[-1]]))


def metrics(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    window: str = typer.Option("24h", "--window", help="Window: 1h, 24h, 7d, ..."),
) -> None:
    """Show provider-side disk metrics for the encrypted volume."""
    inst = store(ctx).get_instance(name)
    provider = get_provider(inst.provider)
    pm = provider.volume_metrics(inst.provider_resources.volume_id, _parse_window(window))

    if is_json(ctx):
        emit_json(
            {
                "instance": name,
                "volume_id": pm.volume_id,
                "window": {"start": pm.window.start.isoformat(), "end": pm.window.end.isoformat()},
                "series": {
                    k: ({"points": [(t.isoformat(), v) for t, v in s.points], "unit": s.unit}
                        if s and not s.unavailable_reason
                        else {"unavailable_reason": s.unavailable_reason if s else "not implemented"})
                    for k, s in {
                        "iops_read": pm.iops_read,
                        "iops_write": pm.iops_write,
                        "throughput_read_bps": pm.throughput_read_bps,
                        "throughput_write_bps": pm.throughput_write_bps,
                        "queue_depth": pm.queue_depth,
                        "latency_ms": pm.latency_ms,
                    }.items()
                },
                "provider_native": pm.provider_native,
            }
        )
        return

    t = Table(title=f"metrics: {name}", header_style="bold")
    t.add_column("series"); t.add_column("status"); t.add_column("samples"); t.add_column("notes")
    for label, s in {
        "iops_read": pm.iops_read,
        "iops_write": pm.iops_write,
        "throughput_read_bps": pm.throughput_read_bps,
        "throughput_write_bps": pm.throughput_write_bps,
        "queue_depth": pm.queue_depth,
        "latency_ms": pm.latency_ms,
    }.items():
        if s is None or s.unavailable_reason:
            t.add_row(label, "[yellow]n/a[/yellow]", "0", (s.unavailable_reason if s else "not implemented"))
        else:
            t.add_row(label, "[green]ok[/green]", str(len(s.points)), s.unit)
    out_console.print(t)


def capacity(
    ctx: typer.Context,
    name: str | None = typer.Argument(None, help="Instance name; omit with --all."),
    all_: bool = typer.Option(False, "--all", help="Report capacity for every instance."),
) -> None:
    """Filesystem + provider capacity for an instance."""
    if not name and not all_:
        raise PgforgeError("provide an instance name or pass --all")
    s = store(ctx)
    instances = list(s.list_instances()) if all_ else [s.get_instance(name)]  # type: ignore[arg-type]

    reports: list[CapacityReport] = []
    for inst in instances:
        provider = get_provider(inst.provider)
        info = provider.volume_capacity(inst.provider_resources.volume_id)
        report = CapacityReport(
            instance=inst.name,
            provider=inst.provider,
            volume_id=inst.provider_resources.volume_id,
            provider_reported_size_bytes=info.provider_reported_size_bytes,
            collected_at=now_utc(),
        )
        try:
            with RemoteHost(host=inst.ssh.host, user=inst.ssh.user, port=inst.ssh.port) as rh:
                report.filesystem = collect_filesystem(rh, inst.mount.path)
                report.postgres = collect_postgres(rh, inst.postgres.container_name)
        except Exception as e:
            report.notes.append(f"ssh probe failed: {e}")
        reports.append(report)

    if is_json(ctx):
        emit_json([r.__dict__ for r in reports]); return

    for r in reports:
        t = Table(title=f"capacity: {r.instance} ({r.provider})", header_style="bold")
        t.add_column("field"); t.add_column("bytes"); t.add_column("note")
        t.add_row("provider_reported_size", str(r.provider_reported_size_bytes or "?"), "")
        fs = r.filesystem
        t.add_row("fs_total", str(fs.total_bytes or "?"), "")
        t.add_row("fs_used", str(fs.used_bytes or "?"), "")
        t.add_row("fs_free", str(fs.free_bytes or "?"), "")
        t.add_row("inodes_total", str(fs.inodes_total or "?"), "")
        t.add_row("inodes_used", str(fs.inodes_used or "?"), "")
        t.add_row("postgres_db_size", str(r.postgres.database_size_bytes or "?"), "")
        t.add_row("postgres_wal_size", str(r.postgres.wal_size_bytes or "?"), "")
        out_console.print(t)
        for note in r.notes:
            out_console.print(f"  [dim]{note}[/dim]")
