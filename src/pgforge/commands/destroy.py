"""`pgforge destroy <name>` — tear down an instance.

Order of operations (each best-effort, idempotent):

1. Remove the cron schedule on the server (uninstall.sh.j2 handles it).
2. Stop + remove the Postgres container.
3. Unmount the encrypted filesystem.
4. cryptsetup luksClose.
5. Strip /etc/crypttab + /etc/fstab entries.
6. Wipe and remove server-side keyfile + credential file.
7. Detach + delete the cloud volume (skipped with --keep-volume).
8. Delete pgforge snapshots for this instance (skipped with --keep-snapshots).
9. Delete the local KMS handle (skipped unless --purge-key).
10. Drop the instance record from state.
"""

from __future__ import annotations

import typer

from pgforge.commands._common import (
    confirm_destructive,
    emit_json,
    is_json,
    store,
    warn,
)
from pgforge.errors import PgforgeError
from pgforge.kms.registry import get_backend as get_kms
from pgforge.logging import get_logger, out_console
from pgforge.providers.registry import get_provider
from pgforge.remote.bootstrap import render_script
from pgforge.remote.ssh import RemoteHost
from pgforge.snapshot.scheduler import (
    credential_path,
    cron_file_path,
    runner_path,
)
from pgforge.state.store import instance_lock

log = get_logger(__name__)


def destroy(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name."),
    keep_snapshots: bool = typer.Option(False, "--keep-snapshots", help="Don't delete snapshots."),
    keep_volume: bool = typer.Option(False, "--keep-volume", help="Detach but keep the volume."),
    purge_key: bool = typer.Option(False, "--purge-key", help="Also destroy the KMS key handle."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation."),
) -> None:
    """Tear down an instance. Idempotent; safe to re-run after partial failures."""
    s = store(ctx)
    inst = s.get_instance(name)

    confirm_destructive(
        name,
        force=force,
        prompt=(
            f"This will permanently remove instance {name!r}. "
            f"Volume: {'KEPT' if keep_volume else 'DELETED'}. "
            f"Snapshots: {'KEPT' if keep_snapshots else 'DELETED'}. "
            f"KMS key: {'PURGED' if purge_key else 'KEPT'}."
        ),
    )

    with instance_lock(name) as _:
        provider = get_provider(inst.provider)
        kms_backend = get_kms(inst.kms.backend)

        # ---- Remote teardown
        try:
            with RemoteHost(host=inst.ssh.host, user=inst.ssh.user, port=inst.ssh.port) as rh:
                rh.run(
                    render_script(
                        "uninstall.sh.j2",
                        container_name=inst.postgres.container_name,
                        mapper=inst.luks.name,
                        mount_point=inst.mount.path,
                        keyfile=f"/root/.pgforge/keys/{name}.key",
                        credential_path=credential_path(name),
                        cron_file=cron_file_path(name),
                    ),
                    check=False,
                )
                # also clean up the snapshot runner script
                import shlex
                rh.run(f"rm -f {shlex.quote(runner_path(name))}", check=False)
        except PgforgeError as e:
            warn(f"remote teardown failed for {name}: {e}")

        # ---- Detach + delete cloud volume
        try:
            if inst.provider_resources.volume_id:
                provider.detach_volume(inst.provider_resources.volume_id)
                if not keep_volume:
                    provider.delete_volume(inst.provider_resources.volume_id)
        except PgforgeError as e:
            warn(f"volume teardown error: {e}")

        # ---- Snapshots
        if not keep_snapshots and inst.provider_resources.volume_id:
            try:
                snaps = provider.list_snapshots(inst.provider_resources.volume_id)
                for snap in snaps:
                    if snap.labels.get("pgforge-instance") == name:
                        provider.delete_snapshot(snap.id)
            except PgforgeError as e:
                warn(f"snapshot cleanup error: {e}")

        # ---- KMS key
        if purge_key:
            from pgforge.kms.base import KeyHandle

            handle = KeyHandle(
                backend=inst.kms.backend,
                key_id=inst.kms.key_id,
                envelope_ciphertext_b64=inst.kms.envelope_ciphertext_b64,
                unlock_mode=inst.kms.unlock_mode,
                metadata=inst.kms.metadata,
            )
            try:
                kms_backend.delete(handle)
            except PgforgeError as e:
                warn(f"KMS delete error: {e}")

        # ---- Provider-side snapshot credential
        for cred_id in list(inst.credentials.keys()):
            try:
                provider.revoke_snapshot_credential(cred_id)
            except PgforgeError as e:
                warn(f"credential revoke error ({cred_id}): {e}")

        # ---- Drop state record
        s.delete_instance(name)

    payload = {"destroyed": name, "kept_volume": keep_volume, "kept_snapshots": keep_snapshots, "purged_key": purge_key}
    if is_json(ctx):
        emit_json(payload); return
    out_console.print(f"[green]destroyed[/green] {name}")
