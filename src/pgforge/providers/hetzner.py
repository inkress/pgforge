"""Hetzner Cloud provider implementation.

Shells out to the official ``hcloud`` CLI for every action. JSON output is
selected with ``-o json``. Authentication is whatever ``hcloud`` itself is
configured with — pgforge does not store the token.

This is the reference implementation: the bash baseline targeted Hetzner, so
porting fidelity matters most here. Other providers measure their behavior
against this one's.
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from pgforge.errors import ProviderAPIError, ProviderResourceNotFound
from pgforge.logging import get_logger
from pgforge.providers._shell import (
    CommandResult,
    check_min_version,
    require_binary,
    run,
)
from pgforge.providers.base import (
    AttachedVolume,
    CapacityInfo,
    CLIInfo,
    Location,
    MetricSeries,
    Provider,
    ProviderMetrics,
    RemoteCredential,
    Server,
    Snapshot,
    SnapshotScope,
    TimeWindow,
    Volume,
    VolumeSpec,
)

log = get_logger(__name__)

HCLOUD = "hcloud"
MIN_VERSION = "1.40.0"


class HetznerProvider(Provider):
    name = "hetzner"
    cli_binary = HCLOUD
    min_cli_version = MIN_VERSION

    def __init__(self) -> None:
        self._binary = require_binary(HCLOUD)

    # ---- preflight ----

    def check_cli(self) -> CLIInfo:
        ver = run([self._binary, "version"], check=False)
        version_text = ver.stdout.strip().splitlines()[0] if ver.stdout else ""
        check_min_version(version_text, MIN_VERSION, name="hcloud")
        # ``hcloud context active`` prints the active context name or empty.
        ctx = run([self._binary, "context", "active"], check=False)
        active = ctx.stdout.strip()
        # Authenticated if we can list at least one server. (`servers list` always
        # works even on empty projects, returning [].)
        ls = run([self._binary, "server", "list", "-o", "json"], check=False, parse_json=False)
        authenticated = ls.returncode == 0
        return CLIInfo(
            binary=self._binary,
            version=version_text,
            authenticated=authenticated,
            account=active or None,
            raw={"context": active},
        )

    def list_locations(self) -> list[Location]:
        res = run([self._binary, "location", "list", "-o", "json"], parse_json=True)
        raw = res.parsed or []
        return [
            Location(
                name=loc["name"],
                description=loc.get("description"),
                country=loc.get("country"),
                raw=loc,
            )
            for loc in raw
        ]

    # ---- server ----

    def get_server(self, server_id_or_name: str) -> Server:
        res = run(
            [self._binary, "server", "describe", server_id_or_name, "-o", "json"],
            parse_json=True,
            check=False,
        )
        if res.returncode != 0:
            raise ProviderResourceNotFound(
                f"hetzner server {server_id_or_name!r} not found: {res.stderr.strip()}"
            )
        raw = res.parsed
        return Server(
            id=str(raw["id"]),
            name=raw.get("name", ""),
            ipv4=(raw.get("public_net") or {}).get("ipv4", {}).get("ip"),
            ipv6=(raw.get("public_net") or {}).get("ipv6", {}).get("ip"),
            location=((raw.get("datacenter") or {}).get("location") or {}).get("name", ""),
            raw=raw,
        )

    def server_device_path(self, server: Server, volume: Volume) -> str:
        return f"/dev/disk/by-id/scsi-0HC_Volume_{volume.id}"

    # ---- volumes ----

    def create_volume(self, spec: VolumeSpec) -> Volume:
        argv = [
            self._binary, "volume", "create",
            "--name", spec.name,
            "--size", str(spec.size_gb),
            "--automount=false",
            "--format=",
            "-o", "json",
        ]
        if spec.server_id:
            argv += ["--server", spec.server_id]
        else:
            argv += ["--location", spec.location]
        for k, v in spec.labels.items():
            argv += ["--label", f"{k}={v}"]
        res = run(argv, parse_json=True)
        raw = (res.parsed or {}).get("volume") or res.parsed
        return self._volume_from_raw(raw)

    def attach_volume(self, volume_id: str, server_id: str) -> AttachedVolume:
        # hcloud's attach is idempotent (returns error if already attached, but
        # we tolerate that and re-describe).
        res = run(
            [self._binary, "volume", "attach", volume_id, "--server", server_id, "--automount=false"],
            check=False,
        )
        if res.returncode != 0 and "already attached" not in res.stderr.lower():
            raise ProviderAPIError(f"hcloud volume attach failed: {res.stderr.strip()}")
        vol = self.get_volume(volume_id)
        # Wait for "attached" state.
        for _ in range(30):
            if vol.attached_server_id == server_id:
                break
            time.sleep(2)
            vol = self.get_volume(volume_id)
        device_path = self.server_device_path(self.get_server(server_id), vol)
        return AttachedVolume(volume=vol, server_id=server_id, device_path=device_path)

    def detach_volume(self, volume_id: str) -> None:
        run([self._binary, "volume", "detach", volume_id], check=False)

    def delete_volume(self, volume_id: str) -> None:
        run([self._binary, "volume", "delete", volume_id], check=False)

    def get_volume(self, volume_id: str) -> Volume:
        res = run(
            [self._binary, "volume", "describe", volume_id, "-o", "json"],
            parse_json=True,
            check=False,
        )
        if res.returncode != 0:
            raise ProviderResourceNotFound(
                f"hetzner volume {volume_id!r} not found: {res.stderr.strip()}"
            )
        return self._volume_from_raw(res.parsed)

    def list_volumes(self) -> list[Volume]:
        res = run([self._binary, "volume", "list", "-o", "json"], parse_json=True)
        return [self._volume_from_raw(v) for v in (res.parsed or [])]

    # ---- snapshots ----
    #
    # Hetzner exposes volumes as backups distinct from server snapshots.
    # The ``hcloud volume create-snapshot`` subcommand wraps this. NB: at the
    # time of writing Hetzner does not expose volume snapshots through the
    # public ``hcloud`` CLI on all account types — for accounts where this
    # isn't available, this method will surface a clear error.

    def create_snapshot(
        self, volume_id: str, name: str, labels: dict[str, str] | None = None
    ) -> Snapshot:
        argv = [
            self._binary, "volume", "create-snapshot", volume_id,
            "--description", name,
            "-o", "json",
        ]
        for k, v in (labels or {}).items():
            argv += ["--label", f"{k}={v}"]
        res = run(argv, parse_json=True, check=False)
        if res.returncode != 0:
            raise ProviderAPIError(
                f"hcloud snapshot create failed: {res.stderr.strip()}. "
                f"Volume snapshots may not be enabled on your Hetzner project."
            )
        raw = (res.parsed or {}).get("snapshot") or res.parsed
        return self._snapshot_from_raw(raw, volume_id)

    def list_snapshots(self, volume_id: str | None = None) -> list[Snapshot]:
        # Hetzner does not have a dedicated `volume snapshots` command;
        # filter the global snapshot list by description label.
        res = run([self._binary, "image", "list", "--type", "snapshot", "-o", "json"], parse_json=True)
        out = []
        for raw in res.parsed or []:
            labels = raw.get("labels") or {}
            if volume_id and str(labels.get("pgforge-volume-id", "")) != str(volume_id):
                if not labels:
                    continue
            out.append(self._snapshot_from_raw(raw, volume_id or labels.get("pgforge-volume-id", "")))
        return out

    def delete_snapshot(self, snapshot_id: str) -> None:
        run([self._binary, "image", "delete", snapshot_id], check=False)

    def restore_snapshot(self, snapshot_id: str, target_spec: VolumeSpec) -> Volume:
        raise ProviderAPIError(
            "restore_snapshot for Hetzner requires creating a new volume from an image, "
            "then luksOpen + mount via SSH. Implemented in Phase 2."
        )

    # ---- metrics & capacity ----

    def volume_metrics(self, volume_id: str, window: TimeWindow) -> ProviderMetrics:
        # Hetzner does not expose per-volume IOPS / throughput / latency via
        # the API. We return a fully ``unavailable`` payload with a helpful
        # reason instead of silently lying.
        reason = "Hetzner does not expose per-volume IOPS/throughput/latency via hcloud."
        return ProviderMetrics(
            volume_id=volume_id,
            window=window,
            iops_read=MetricSeries(points=[], unit="ops/s", unavailable_reason=reason),
            iops_write=MetricSeries(points=[], unit="ops/s", unavailable_reason=reason),
            throughput_read_bps=MetricSeries(points=[], unit="bytes/s", unavailable_reason=reason),
            throughput_write_bps=MetricSeries(points=[], unit="bytes/s", unavailable_reason=reason),
            queue_depth=MetricSeries(points=[], unit="ops", unavailable_reason=reason),
            latency_ms=MetricSeries(points=[], unit="ms", unavailable_reason=reason),
        )

    def volume_capacity(self, volume_id: str) -> CapacityInfo:
        vol = self.get_volume(volume_id)
        return CapacityInfo(
            volume_id=vol.id,
            provider_reported_size_bytes=vol.size_gb * 1024 * 1024 * 1024,
            raw=vol.raw,
        )

    # ---- snapshot cron credentials ----

    def mint_snapshot_credential(self, scope: SnapshotScope) -> RemoteCredential:
        """For Hetzner, server-side cron needs an ``HCLOUD_TOKEN``.

        We don't mint a new project token via the API (Hetzner doesn't expose
        token creation). Instead, the operator either:

        1. Sets ``HCLOUD_TOKEN`` in their environment before running pgforge;
           we forward the value to a file on the server. (Quick, broad-scope.)
        2. Provides ``PGFORGE_HETZNER_SNAPSHOT_TOKEN`` separately for the
           server side, so the operator-side token isn't shipped onto the box.

        Either way the resulting credential is project-scoped — Hetzner does
        not support per-volume tokens. Document that loudly.
        """
        token = os.environ.get("PGFORGE_HETZNER_SNAPSHOT_TOKEN") or os.environ.get("HCLOUD_TOKEN")
        if not token:
            raise ProviderAPIError(
                "Hetzner has no per-instance token mint. Set HCLOUD_TOKEN (or "
                "PGFORGE_HETZNER_SNAPSHOT_TOKEN for a separate snapshot-only "
                "token) before running `pgforge snapshot schedule`."
            )
        last4 = token[-4:]
        cred_id = f"hetzner-{scope.instance_name}-{uuid.uuid4().hex[:8]}"
        body = f'export HCLOUD_TOKEN={token!r}\n'
        return RemoteCredential(
            credential_id=cred_id,
            mode="file_credential",
            files={f"/root/.pgforge/cred-{scope.instance_name}.env": body},
            last4=last4,
        )

    def revoke_snapshot_credential(self, credential_id: str) -> None:
        # No-op: Hetzner doesn't allow revoking arbitrary tokens via CLI.
        # The operator must rotate via the Hetzner Cloud Console.
        log.warning(
            "Hetzner credential %s cannot be revoked via API. Rotate it in the "
            "Hetzner Cloud Console.", credential_id
        )

    # ---- internals ----

    def _volume_from_raw(self, raw: dict[str, Any]) -> Volume:
        return Volume(
            id=str(raw["id"]),
            name=raw.get("name", ""),
            size_gb=int(raw.get("size", 0)),
            location=((raw.get("location") or {}).get("name") or ""),
            attached_server_id=(str(raw["server"]) if raw.get("server") else None),
            labels=raw.get("labels") or {},
            raw=raw,
        )

    def _snapshot_from_raw(self, raw: dict[str, Any], source_volume_id: str) -> Snapshot:
        return Snapshot(
            id=str(raw.get("id") or raw.get("snapshot", {}).get("id")),
            name=raw.get("description") or raw.get("name", ""),
            source_volume_id=str(source_volume_id),
            size_gb=int(raw.get("disk_size") or raw.get("size") or 0),
            created_at=_parse_ts(raw.get("created")),
            labels=raw.get("labels") or {},
            raw=raw,
        )


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


# Silence unused-import warning
_ = (secrets, CommandResult)
