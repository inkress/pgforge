"""`pgforge key ls|rotate|export` — KMS handle operations."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.table import Table

from pgforge.commands._common import emit_json, is_json, store, warn
from pgforge.errors import ConfigError, PgforgeError
from pgforge.logging import out_console

app = typer.Typer(help="Manage KMS handles tracked by pgforge.")


@app.command("ls")
def list_keys(ctx: typer.Context) -> None:
    """List all KMS handles referenced by instances."""
    rows = []
    for inst in store(ctx).list_instances():
        rows.append(
            {
                "instance": inst.name,
                "backend": inst.kms.backend,
                "key_id": inst.kms.key_id,
                "unlock_mode": inst.kms.unlock_mode.value,
                "envelope": bool(inst.kms.envelope_ciphertext_b64),
            }
        )
    if is_json(ctx):
        emit_json(rows); return
    t = Table(title="KMS handles", header_style="bold")
    for c in ["instance", "backend", "key_id", "unlock_mode", "envelope"]:
        t.add_column(c)
    for r in rows:
        t.add_row(*[str(r[c]) for c in ["instance", "backend", "key_id", "unlock_mode", "envelope"]])
    out_console.print(t)


@app.command("rotate")
def rotate(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    yes: bool = typer.Option(False, "--yes", help="Skip the typed-name confirmation."),
) -> None:
    """Rotate the LUKS key for an instance.

    Transactional: pgforge generates a new key in the same KMS backend, adds
    it as a new LUKS keyslot, verifies the new slot unlocks the device, then
    removes the old slot and swaps the canonical keyfile on the server. The
    old key handle is destroyed last; if any step fails before that, the old
    key is still usable.
    """
    from pgforge.commands._common import confirm_destructive
    from pgforge.kms.base import KeyHandle
    from pgforge.kms.registry import get_backend as get_kms
    from pgforge.remote.bootstrap import render_script
    from pgforge.remote.ssh import RemoteHost
    from pgforge.state.store import instance_lock

    s = store(ctx)
    inst = s.get_instance(name)
    confirm_destructive(
        name, force=yes,
        prompt=f"This will rotate the LUKS key for {name!r}.",
    )

    kms_backend = get_kms(inst.kms.backend)
    old_handle = KeyHandle(
        backend=inst.kms.backend,
        key_id=inst.kms.key_id,
        envelope_ciphertext_b64=inst.kms.envelope_ciphertext_b64,
        unlock_mode=inst.kms.unlock_mode,
        metadata=inst.kms.metadata,
    )
    # 1. Generate the new key locally.
    new_handle = kms_backend.rotate(old_handle)
    new_bytes = kms_backend.fetch_material(new_handle)

    keyfile_remote = f"/root/.pgforge/keys/{name}.key"
    keyfile_remote_new = f"/root/.pgforge/keys/{name}.key.new"

    with instance_lock(name) as _:
        with RemoteHost(host=inst.ssh.host, user=inst.ssh.user, port=inst.ssh.port) as rh:
            rh.upload(new_bytes, keyfile_remote_new, mode=0o400)
            rh.run(
                render_script(
                    "luks_rotate.sh.j2",
                    device=inst.provider_resources.device_path,
                    keyfile=keyfile_remote,
                    new_keyfile=keyfile_remote_new,
                ),
                check=True,
            )

        # 2. Persist new handle in state.
        with s.transaction() as state:
            inst2 = state.instances[name]
            inst2.kms.key_id = new_handle.key_id
            inst2.kms.envelope_ciphertext_b64 = new_handle.envelope_ciphertext_b64
            inst2.kms.unlock_mode = new_handle.unlock_mode
            inst2.kms.metadata = {**new_handle.metadata, "rotated_at": str(_now())}
            inst2.touch()

        # 3. Delete the old key from KMS.
        try:
            kms_backend.delete(old_handle)
        except Exception as e:
            warn(f"old KMS key delete failed (server-side rotation already complete): {e}")

    out_console.print(f"[green]rotated[/green] {name}: new key {new_handle.key_id}")


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


@app.command("export")
def export(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    out: Path = typer.Option(..., "--to", help="Where to write the key file."),
    yes: bool = typer.Option(False, "--yes", help="Skip the warning prompt."),
) -> None:
    """Export the LUKS key for an instance. **Local backend only.**

    Useful for emergency recovery (you can luksOpen by hand). The exported
    file is mode 0600. Treat it like a backup of the data itself.
    """
    inst = store(ctx).get_instance(name)
    if inst.kms.backend != "local":
        raise ConfigError(
            f"export only works for the local KMS backend; instance {name!r} uses "
            f"{inst.kms.backend!r}. Use the backend's own recovery flow."
        )
    src = Path(inst.kms.key_id)
    if not src.is_file():
        raise PgforgeError(f"local key file missing: {src}")
    if not yes:
        warn(
            f"You are about to copy the LUKS key for {name!r} to {out}. "
            f"Anyone with this file can decrypt your data. Re-run with --yes once you understand."
        )
        raise PgforgeError("aborted (pass --yes to proceed)")
    shutil.copy(src, out)
    out.chmod(0o600)
    out_console.print(f"[green]wrote[/green] {out}")
