"""`pgforge provision` — the end-to-end provisioning flow.

This command is the heart of pgforge. It mirrors the bash baseline:

1. Resolve / validate the target server via the provider.
2. Create (or adopt) the volume of the requested size.
3. Attach the volume to the server.
4. Generate a LUKS key in the chosen KMS backend.
5. Upload deps + key to the server.
6. luksFormat (if not already LUKS); luksOpen; mkfs; mount.
7. Write /etc/crypttab and /etc/fstab.
8. Launch (or restart) the Postgres container on the encrypted mount.
9. Record the LUKS UUID and write everything to state.

Each step records a :class:`Phase` so re-running the command after a failure
resumes from the last completed step. ``--resume`` reuses an existing state
record; without it, the command refuses to provision a name that already
exists.
"""

from __future__ import annotations

import secrets
import shlex
import string
from datetime import datetime, timezone
from typing import Optional

import typer

from pgforge import __version__
from pgforge.commands._common import emit_json, is_json, store
from pgforge.errors import (
    ConfigError,
    PgforgeError,
    ProvisionError,
    StateConflict,
    StateNotFound,
)
from pgforge.kms.base import KeySpec, UnlockMode
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


def _random_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(24))


def _parse_kv(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for kv in items or []:
        if "=" not in kv:
            raise ConfigError(f"expected key=value, got {kv!r}")
        k, v = kv.split("=", 1)
        out[k] = v
    return out


def provision(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Name for the new instance."),
    provider_name: str = typer.Option(..., "--provider", help="Cloud provider."),
    server: str = typer.Option(..., "--server", help="Server id or name to attach the volume to."),
    size: int = typer.Option(..., "--size", help="Volume size in GB (provider minimums apply)."),
    location: Optional[str] = typer.Option(
        None, "--location", help="Cloud location (only required when the provider can't infer)."
    ),
    kms: str = typer.Option("local", "--kms", help="KMS backend (local|aws-kms|gcp-kms|azure-kv|vault)."),
    kms_config: list[str] = typer.Option(
        None, "--kms-config", help="Backend-specific config key=value. Repeatable."
    ),
    postgres_version: str = typer.Option("16", "--postgres-version", help="Postgres major version tag."),
    postgres_password: Optional[str] = typer.Option(
        None, "--postgres-password", envvar="PGFORGE_POSTGRES_PASSWORD",
        help="Initial postgres password (random if unset).",
    ),
    filesystem: str = typer.Option("ext4", "--filesystem", help="ext4 or xfs."),
    mount_point: str = typer.Option("/mnt/pg", "--mount-point", help="Mount path on the server."),
    container_name: Optional[str] = typer.Option(
        None, "--container-name", help="Docker container name (defaults to pg-<name>)."
    ),
    port: int = typer.Option(5432, "--postgres-port", help="Host-side port on 127.0.0.1."),
    ssh_user: str = typer.Option("root", "--ssh-user", help="SSH user for remote orchestration."),
    ssh_key: Optional[str] = typer.Option(None, "--ssh-key", help="Path to SSH private key."),
    label: list[str] = typer.Option(None, "--label", help="Instance label, key=value. Repeatable."),
    no_cron: bool = typer.Option(False, "--no-cron", help="Skip snapshot cron bootstrap."),
    resume: bool = typer.Option(False, "--resume", help="Continue a partial provision."),
    unlock_mode: str = typer.Option(
        "static", "--unlock-mode",
        help=(
            "static = key file on server (matches the bash baseline); "
            "runtime = boot-time agent decrypts via cloud KMS into tmpfs (cloud KMS only)."
        ),
    ),
) -> None:
    """Provision an encrypted Postgres instance on the chosen cloud."""
    if filesystem not in ("ext4", "xfs"):
        raise ConfigError("--filesystem must be ext4 or xfs")
    if unlock_mode not in ("static", "runtime"):
        raise ConfigError("--unlock-mode must be 'static' or 'runtime'")
    if unlock_mode == "runtime" and kms == "local":
        raise ConfigError(
            "--unlock-mode=runtime requires a cloud KMS backend (aws-kms / gcp-kms / "
            "azure-kv / vault). The 'local' backend has nothing to decrypt at boot."
        )

    s = store(ctx)
    _ = _parse_kv(kms_config)  # currently unused but validated

    # Determine whether this is a fresh provision or a resume.
    try:
        existing = s.get_instance(name)
    except StateNotFound:
        existing = None

    if existing is not None and not resume:
        raise StateConflict(
            f"instance {name!r} already exists in state (phase={existing.phase.value}). "
            f"Pass --resume to continue, or `pgforge destroy {name}` first."
        )

    container = container_name or f"pg-{name}"
    pg_password = postgres_password or _random_password()

    with instance_lock(name) as _:
        if existing is None:
            # Phase 0: resolve server, compute resources, write a PENDING record.
            provider = get_provider(provider_name)
            srv = provider.get_server(server)
            loc = location or srv.location
            inst = _make_pending_state(
                name=name,
                provider_name=provider_name,
                kms_backend=kms,
                server=srv,
                container=container,
                postgres_version=postgres_version,
                port=port,
                ssh_user=ssh_user,
                mount_point=mount_point,
                filesystem=filesystem,
                location=loc,
                labels=_parse_kv(label),
            )
            s.add_instance(inst)
        else:
            inst = existing

        provider = get_provider(inst.provider)
        srv = provider.get_server(inst.provider_resources.server_id or server)
        log.info("server: %s (ipv4=%s ipv6=%s loc=%s)", srv.name, srv.ipv4, srv.ipv6, srv.location)

        # ---- Phase 1: volume created
        if not inst.phase.is_at_or_past(Phase.VOLUME_CREATED):
            existing_vol = provider.find_volume_by_name(_volume_name(name))
            if existing_vol is None:
                volume = provider.create_volume(
                    VolumeSpec(
                        name=_volume_name(name),
                        size_gb=size,
                        location=inst.provider_resources.location,
                        labels={"pgforge-instance": name, **_parse_kv(label)},
                        server_id=srv.id,
                    )
                )
            else:
                volume = existing_vol
            inst.provider_resources.volume_id = volume.id
            inst.phase = Phase.VOLUME_CREATED
            inst.touch()
            s.update_instance(inst)
        else:
            volume = provider.get_volume(inst.provider_resources.volume_id)

        # ---- Phase 2: volume attached
        if not inst.phase.is_at_or_past(Phase.VOLUME_ATTACHED):
            attached = provider.attach_volume(volume.id, srv.id)
            inst.provider_resources.device_path = attached.device_path
            inst.phase = Phase.VOLUME_ATTACHED
            inst.touch()
            s.update_instance(inst)
        device_path = inst.provider_resources.device_path

        # ---- KMS key (idempotent: reuse if already present in state)
        kms_backend = get_kms(inst.kms.backend)
        chosen_unlock = UnlockMode.RUNTIME if unlock_mode == "runtime" else UnlockMode.STATIC
        if not inst.kms.key_id:
            handle = kms_backend.generate_key(
                KeySpec(label=f"pgforge-{name}", size_bytes=64, unlock_mode=chosen_unlock)
            )
            inst.kms.key_id = handle.key_id
            inst.kms.envelope_ciphertext_b64 = handle.envelope_ciphertext_b64
            inst.kms.unlock_mode = handle.unlock_mode
            inst.kms.metadata = handle.metadata
            inst.touch()
            s.update_instance(inst)
        else:
            handle = _handle_from_state(inst)
        keyfile_local = inst.kms.key_id
        # In runtime mode the server has no plaintext key file on disk; the
        # boot-time agent decrypts the envelope into tmpfs every boot. We
        # still need plaintext briefly during initial provisioning to drive
        # the very first luksFormat, so we fetch it (which the boot agent
        # will recover later by calling KMS).
        key_bytes = kms_backend.fetch_material(handle)

        # ---- SSH endpoint
        endpoint = provider.server_ssh_endpoint(srv)
        inst.ssh = SSHInfo(host=endpoint.host, port=endpoint.port, user=ssh_user)
        s.update_instance(inst)

        keyfile_remote = f"/root/.pgforge/keys/{name}.key"

        with RemoteHost(host=endpoint.host, user=ssh_user, port=endpoint.port, key_filename=ssh_key) as rh:
            # Deps + key upload.
            rh.run(render_script("install_deps.sh.j2"), check=True)
            if chosen_unlock == UnlockMode.STATIC:
                rh.upload(key_bytes, keyfile_remote, mode=0o400)
                log.info("uploaded LUKS key to %s", keyfile_remote)
            else:
                # Runtime mode: stage to tmpfs path for initial luksFormat, then
                # install the boot-time agent so future boots can re-derive.
                rh.run("mkdir -p /run/pgforge/keys && chmod 700 /run/pgforge && chmod 700 /run/pgforge/keys", check=True)
                tmpfs_key = f"/run/pgforge/keys/{name}.key"
                rh.upload(key_bytes, tmpfs_key, mode=0o400)
                # Install the cloud CLI for the chosen KMS provider.
                kms_provider_hint = {
                    "aws-kms": "aws",
                    "gcp-kms": "gcp",
                    "azure-kv": "azure",
                    "vault": "vault",
                }.get(inst.kms.backend, "")
                if kms_provider_hint and kms_provider_hint != "vault":
                    rh.run(render_script("install_cloud_cli.sh.j2", provider=kms_provider_hint), check=True)
                # Drop the agent + systemd unit.
                agent = render_script(
                    "runtime_unlock_agent.sh.j2",
                    instance_name=name,
                    provider=kms_provider_hint or inst.kms.backend.split("-")[0],
                    envelope_b64=inst.kms.envelope_ciphertext_b64 or "",
                    kms_key_ref=_kms_key_ref_for_runtime(inst.kms),
                    target_keyfile=tmpfs_key,
                )
                agent_path = f"/usr/local/sbin/pgforge-runtime-unlock-{name}"
                rh.upload(agent, agent_path, mode=0o700)
                unit = render_script("runtime_unlock.service.j2", instance_name=name)
                unit_path = f"/etc/systemd/system/pgforge-runtime-unlock-{name}.service"
                rh.upload(unit, unit_path, mode=0o644)
                rh.run("systemctl daemon-reload", check=False)
                rh.run(f"systemctl enable pgforge-runtime-unlock-{name}.service", check=False)
                # Use the staged tmpfs key for the rest of provisioning.
                keyfile_remote = tmpfs_key

            # ---- Phase 3: luks formatted
            if not inst.phase.is_at_or_past(Phase.LUKS_FORMATTED):
                rh.run(
                    render_script(
                        "luks_format.sh.j2",
                        device=device_path,
                        keyfile=keyfile_remote,
                        cipher=inst.luks.cipher,
                        key_size=inst.luks.key_size,
                    ),
                    check=True,
                )
                inst.phase = Phase.LUKS_FORMATTED
                inst.touch()
                s.update_instance(inst)

            # ---- Phase 4 & 5 & 6: luks opened / mkfs / mounted
            if not inst.phase.is_at_or_past(Phase.MOUNTED):
                rh.run(
                    render_script(
                        "luks_open.sh.j2",
                        device=device_path,
                        mapper=inst.luks.name,
                        keyfile=keyfile_remote,
                        filesystem=filesystem,
                        mount_point=mount_point,
                    ),
                    check=True,
                )
                # Capture LUKS UUID for crypttab and state.
                rc, out_text, _ = rh.run(
                    f"cryptsetup luksUUID {shlex.quote(device_path)}",
                    check=False,
                    log_command=False,
                )
                if rc == 0:
                    inst.luks.uuid = out_text.strip()
                inst.phase = Phase.MOUNTED
                inst.touch()
                s.update_instance(inst)

            # ---- Phase 7: crypttab/fstab written
            if not inst.phase.is_at_or_past(Phase.CRYPTTAB_WRITTEN):
                rh.run(
                    render_script(
                        "crypttab_fstab.sh.j2",
                        device=device_path,
                        mapper=inst.luks.name,
                        keyfile=keyfile_remote,
                        mount_point=mount_point,
                        filesystem=filesystem,
                    ),
                    check=True,
                )
                inst.phase = Phase.CRYPTTAB_WRITTEN
                inst.touch()
                s.update_instance(inst)

            # ---- Phase 8: postgres running
            if not inst.phase.is_at_or_past(Phase.POSTGRES_RUNNING):
                rh.run(
                    render_script(
                        "postgres_docker.sh.j2",
                        container_name=container,
                        image=f"postgres:{postgres_version}",
                        mount_point=mount_point,
                        data_subdir="data",
                        port=port,
                        pg_password=pg_password,
                    ),
                    check=True,
                )
                inst.phase = Phase.POSTGRES_RUNNING
                inst.touch()
                s.update_instance(inst)

            # ---- Phase 9 (optional): cron installed via `pgforge snapshot schedule`
            # We don't auto-install a schedule on provision — the user picks the
            # cadence and retention with `pgforge snapshot schedule`. We DO
            # install the provider CLI on the server so a later `schedule` is
            # a one-shot upload of the cron file.
            if not no_cron and not inst.phase.is_at_or_past(Phase.CRON_INSTALLED):
                rh.run(render_script("install_cloud_cli.sh.j2", provider=inst.provider), check=True)
                inst.phase = Phase.CRON_INSTALLED
                inst.touch()
                s.update_instance(inst)

        inst.phase = Phase.READY
        inst.touch()
        s.update_instance(inst)

    summary = {
        "name": name,
        "provider": provider_name,
        "server": srv.name,
        "ssh": f"{ssh_user}@{endpoint.host}:{endpoint.port}",
        "volume_id": inst.provider_resources.volume_id,
        "device_path": device_path,
        "mount_point": mount_point,
        "container": container,
        "postgres_image": f"postgres:{postgres_version}",
        "postgres_port": f"127.0.0.1:{port}",
        "postgres_password": pg_password,
        "luks_uuid": inst.luks.uuid,
        "kms": {"backend": inst.kms.backend, "key_id": inst.kms.key_id},
    }
    if is_json(ctx):
        emit_json(summary); return

    out_console.print(
        f"[green]ready[/green] {name}: encrypted Postgres on {endpoint.host}:{port} "
        f"(volume={inst.provider_resources.volume_id}, container={container})"
    )
    out_console.print(
        "Connect via SSH tunnel:\n"
        f"  ssh -L {port}:127.0.0.1:{port} {ssh_user}@{endpoint.host}\n"
        f"  psql 'postgresql://postgres:{pg_password}@127.0.0.1:{port}/postgres'\n"
    )
    out_console.print(
        "[dim]Postgres password printed above is the only place pgforge will show it. "
        "Store it in your secrets manager.[/dim]"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _volume_name(instance_name: str) -> str:
    # Volume names must be unique per project on most clouds. Prefix with
    # pgforge- so it's discoverable.
    return f"pgforge-{instance_name}"


def _make_pending_state(
    *,
    name: str,
    provider_name: str,
    kms_backend: str,
    server,
    container: str,
    postgres_version: str,
    port: int,
    ssh_user: str,
    mount_point: str,
    filesystem: str,
    location: str,
    labels: dict[str, str],
) -> InstanceState:
    now = datetime.now(timezone.utc)
    return InstanceState(
        name=name,
        created_at=now,
        updated_at=now,
        provider=provider_name,
        provider_resources=ProviderResources(
            server_id=server.id,
            server_name=server.name,
            volume_id="",
            device_path="",
            location=location,
        ),
        luks=LUKSInfo(),
        kms=KMSRef(backend=kms_backend, key_id="", envelope_ciphertext_b64=None),
        mount=MountInfo(path=mount_point, filesystem=filesystem),
        postgres=PostgresInfo(
            container_name=container,
            image=f"postgres:{postgres_version}",
            port=port,
        ),
        ssh=SSHInfo(host=server.ipv4 or server.ipv6 or "", user=ssh_user),
        labels=labels,
        phase=Phase.PENDING,
    )


def _handle_from_state(inst: InstanceState):
    from pgforge.kms.base import KeyHandle

    return KeyHandle(
        backend=inst.kms.backend,
        key_id=inst.kms.key_id,
        envelope_ciphertext_b64=inst.kms.envelope_ciphertext_b64,
        unlock_mode=inst.kms.unlock_mode,
        metadata=inst.kms.metadata,
    )


def _kms_key_ref_for_runtime(kms_ref) -> str:
    """Extract the backend-specific key reference for the runtime unlock agent."""
    # KeyId formats embed the backend ref. See each backend's _build_key_id().
    kid = kms_ref.key_id or ""
    # Stored shape examples:
    #   aws-kms:arn:aws:kms:us-east-1:1234:key/abc:label-deadbeef
    #   gcp-kms:projects/p/locations/.../cryptoKeys/k:label-deadbeef
    #   azure-kv:vault/key:label-deadbeef
    #   vault:transit/pgforge:label-deadbeef
    parts = kid.split(":", 2)
    if len(parts) >= 2:
        return parts[1]
    return ""


# Silence unused-import warning
_ = ProvisionError
_ = PgforgeError
