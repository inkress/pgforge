"""`pgforge doctor` — preflight checks."""

from __future__ import annotations

from typing import Optional

import typer
from rich.table import Table

from pgforge.commands._common import emit_json, is_json
from pgforge.errors import PgforgeError
from pgforge.kms.registry import list_backends
from pgforge.logging import out_console
from pgforge.providers.registry import get_provider, iter_implemented, list_providers


def doctor(
    ctx: typer.Context,
    provider: Optional[str] = typer.Option(None, "--provider", help="Check only this provider."),
) -> None:
    """Check that the required cloud CLIs are installed and authenticated."""
    results = []
    targets = [provider] if provider else list(iter_implemented())
    targets = [t for t in targets if t != "mock"]

    for name in targets:
        row = {"provider": name, "status": "unknown", "cli": "", "version": "", "auth": "", "detail": ""}
        try:
            p = get_provider(name)
            info = p.check_cli()
            row["cli"] = info.binary
            row["version"] = info.version
            row["auth"] = "ok" if info.authenticated else "missing"
            row["status"] = "ok" if info.authenticated else "warn"
            row["detail"] = info.account or ""
        except PgforgeError as e:
            row["status"] = "error"
            row["detail"] = str(e)
        results.append(row)

    backends = list_backends()
    kms_rows = [{"kms": n, "implemented": impl} for n, impl in backends]
    providers_meta = [
        {"provider": n, "implemented": impl} for n, impl in list_providers()
    ]

    if is_json(ctx):
        emit_json({"providers": results, "providers_meta": providers_meta, "kms": kms_rows})
        return

    t = Table(title="providers", show_lines=False, header_style="bold")
    for col in ["provider", "status", "cli", "version", "auth", "detail"]:
        t.add_column(col)
    for r in results:
        style = {"ok": "green", "warn": "yellow", "error": "red"}.get(r["status"], "")
        t.add_row(*[f"[{style}]{r[c]}[/{style}]" if c == "status" else str(r[c]) for c in ["provider", "status", "cli", "version", "auth", "detail"]])
    out_console.print(t)

    meta_t = Table(title="roadmap", show_lines=False, header_style="bold")
    meta_t.add_column("provider"); meta_t.add_column("implemented")
    for m in providers_meta:
        meta_t.add_row(m["provider"], "yes" if m["implemented"] else "no")
    out_console.print(meta_t)

    kms_t = Table(title="KMS backends", show_lines=False, header_style="bold")
    kms_t.add_column("backend"); kms_t.add_column("implemented")
    for k in kms_rows:
        kms_t.add_row(k["kms"], "yes" if k["implemented"] else "no")
    out_console.print(kms_t)
