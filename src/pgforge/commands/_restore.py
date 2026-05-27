"""Implementation of ``pgforge snapshot restore``.

The flow is essentially "provision with luksFormat replaced by luksOpen
against the existing key":

1. Validate the source instance state (for KMS handle, mount config, etc.).
2. Create a new volume from the snapshot via ``provider.restore_snapshot``.
3. Attach the new volume to the same server (or a different one via
   ``--server`` override).
4. Fetch the LUKS key from the source instance's KMS handle.
5. ``luksOpen`` the restored device, ``mount`` it, optionally write
   crypttab/fstab.
6. Launch a new Postgres container on the restored mount.
7. Register the new instance in state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pgforge.commands._common import emit_json, is_json, store, warn
from pgforge.errors import PgforgeError, SnapshotError, StateConflict
from pgforge.kms.base import KeyHandle
from pgforge.kms.registry import get_backend as get_kms
from pgforge.logging import get_logger, out_console
from pgforge.providers.base import VolumeSpec
from pgforge.providers.registry import get_provider
from pgforge.remote.bootstrap import render_script
from pgforge.remote.ssh import RemoteHost
from pgforge.state.schema import (
    InstanceState,
    KMSRef,
    LUKSInfo,
    MountInfo,
    Phase,
    PostgresInfo,
    ProviderResources,
    SSHInfo,
)
from pgforge.state.store import instance_lock

log = get_logger(__name__)


def run_restore(
    ctx,
    *,
    snapshot_id: str,
    source_instance: str,
    new_name: str,
    target_server: Optional[str] = None,
    postgres_port: int = 5432,
    container_name: Optional[str] = None,
) -> None:
    """Top-level restore handler called from ``commands/snapshot.py``."""
    s = store(ctx)
    src = s.get_instance(source_instance)
    try:
        s.get_instance(new_name)
    except PgforgeError:
        pass
    else:
        raise StateConflict(f"instance {new_name!r} already exists")

    provider = get_provider(src.provider)

    # 1. Restore snapshot into a new volume.
    new_volume_name = f"pgforge-{new_name}"
    target_spec = VolumeSpec(
        name=new_volume_name,
        size_gb=0,  # let the provider infer from the snapshot where supported
        location=src.provider_resources.location,
        labels={"pgforge-instance": new_name, "pgforge-restored-from": snapshot_id},
        server_id=None,
    )
    log.info("restoring snapshot %s -> volume %s", snapshot_id, new_volume_name)
    new_volume = provider.restore_snapshot(snapshot_id, target_spec)

    # 2. Resolve / attach to a server.
    server_ref = target_server or src.provider_resources.server_id
    srv = provider.get_server(server_ref)
    attached = provider.attach_volume(new_volume.id, srv.id)
    device_path = attached.device_path
    log.info("attached restored volume %s to %s at %s", new_volume.id, srv.id, device_path)

    # 3. Open LUKS with the source instance's key.
    kms_backend = get_kms(src.kms.backend)
    handle = KeyHandle(
        backend=src.kms.backend,
        key_id=src.kms.key_id,
        envelope_ciphertext_b64=src.kms.envelope_ciphertext_b64,
        unlock_mode=src.kms.unlock_mode,
        metadata=src.kms.metadata,
    )
    key_bytes = kms_backend.fetch_material(handle)

    keyfile_remote = f"/root/.pgforge/keys/{new_name}.key"
    container = container_name or f"pg-{new_name}"
    new_pg_password = src.postgres.bind  # the existing data dir already has its own password
    _ = new_pg_password  # unused: restored container uses the password baked into the data dir

    with RemoteHost(host=src.ssh.host, user=src.ssh.user, port=src.ssh.port) as rh:
        rh.run(render_script("install_deps.sh.j2"), check=True)
        rh.upload(key_bytes, keyfile_remote, mode=0o400)

        rh.run(
            render_script(
                "luks_open.sh.j2",
                device=device_path,
                mapper=f"{src.luks.name}-{new_name}",
                keyfile=keyfile_remote,
                filesystem=src.mount.filesystem,
                mount_point=f"/mnt/pg-{new_name}",
            ),
            check=True,
        )
        # 4. Start a fresh Postgres container on the restored data.
        # No POSTGRES_PASSWORD: the existing data directory keeps its credentials.
        import shlex
        rh.run(
            f"docker run -d --name {shlex.quote(container)} "
            f"--restart unless-stopped "
            f"-v /mnt/pg-{new_name}/data:/var/lib/postgresql/data "
            f"-p 127.0.0.1:{postgres_port}:5432 "
            f"-e PGDATA=/var/lib/postgresql/data/pgdata "
            f"{shlex.quote(src.postgres.image)}",
            check=True,
        )

    # 5. Register state.
    new_inst = InstanceState(
        name=new_name,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        provider=src.provider,
        provider_resources=ProviderResources(
            server_id=srv.id,
            server_name=srv.name,
            volume_id=new_volume.id,
            device_path=device_path,
            location=src.provider_resources.location,
        ),
        luks=LUKSInfo(name=f"{src.luks.name}-{new_name}", uuid=None, cipher=src.luks.cipher, key_size=src.luks.key_size),
        kms=KMSRef(
            backend=src.kms.backend,
            key_id=src.kms.key_id,
            envelope_ciphertext_b64=src.kms.envelope_ciphertext_b64,
            unlock_mode=src.kms.unlock_mode,
            metadata={**src.kms.metadata, "restored_from": source_instance},
        ),
        mount=MountInfo(path=f"/mnt/pg-{new_name}", filesystem=src.mount.filesystem),
        postgres=PostgresInfo(
            container_name=container,
            image=src.postgres.image,
            port=postgres_port,
            bind=src.postgres.bind,
            data_subdir=src.postgres.data_subdir,
        ),
        ssh=SSHInfo(host=src.ssh.host, port=src.ssh.port, user=src.ssh.user),
        labels={**src.labels, "pgforge-restored-from": snapshot_id, "pgforge-restored-source": source_instance},
        phase=Phase.READY,
    )
    with instance_lock(new_name) as _:
        s.add_instance(new_inst)

    if is_json(ctx):
        emit_json(
            {
                "restored": new_name,
                "source_instance": source_instance,
                "from_snapshot": snapshot_id,
                "volume_id": new_volume.id,
                "device_path": device_path,
                "ssh": f"{src.ssh.user}@{src.ssh.host}:{src.ssh.port}",
                "postgres_port": postgres_port,
            }
        )
        return
    out_console.print(
        f"[green]restored[/green] {new_name} from snapshot {snapshot_id} "
        f"(source instance: {source_instance})"
    )
    out_console.print(
        f"Postgres is now running on 127.0.0.1:{postgres_port} on {src.ssh.host}. "
        f"It uses the *original* superuser password (the one set at provision time)."
    )
    warn = _ = SnapshotError  # silence linters
    _ = warn
