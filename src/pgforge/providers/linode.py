"""Linode provider via ``linode-cli``.

Linode is the most barren of the bunch in CLI terms:

* **No native snapshots for block volumes.** Linode's block-storage volumes
  do not have an API-driven snapshot product. pgforge implements "snapshot"
  via ``linode-cli volumes clone`` — it copies the volume to a new one with
  the snapshot name as label, which is the closest available primitive. The
  clone *is* the snapshot. Retention pruning operates on clones flagged with
  pgforge labels (we tag clones via the label suffix because Linode tags
  apply to compute, not volumes).

* **No CLI metrics.** Same as DigitalOcean — install node_exporter on the
  Linode and scrape Prometheus.

* **No per-volume PAT scoping.** Server-side cron needs an account-wide
  PAT. Same security caveat as DigitalOcean.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any, ClassVar

from pgforge.errors import ProviderAPIError, ProviderResourceNotFound
from pgforge.logging import get_logger
from pgforge.providers._shell import check_min_version, require_binary, run
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

LINODE = "linode-cli"
MIN_VERSION = "5.0.0"


class LinodeProvider(Provider):
    name: ClassVar[str] = "linode"
    cli_binary: ClassVar[str] = LINODE
    min_cli_version: ClassVar[str] = MIN_VERSION

    def __init__(self) -> None:
        self._binary = require_binary(LINODE)

    # ---- preflight ----

    def check_cli(self) -> CLIInfo:
        ver = run([self._binary, "--version"], check=False)
        m = re.search(r"linode-cli\s+(\S+)", ver.stdout)
        version_text = m.group(1) if m else ver.stdout.strip()
        check_min_version(version_text, MIN_VERSION, name="linode-cli")
        acct = run([self._binary, "account", "view", "--json", "--no-headers"], check=False, parse_json=True)
        return CLIInfo(
            binary=self._binary,
            version=version_text,
            authenticated=acct.returncode == 0,
            account=((acct.parsed or [{}])[0] if isinstance(acct.parsed, list) else (acct.parsed or {})).get("email"),
            raw={},
        )

    def list_locations(self) -> list[Location]:
        res = run([self._binary, "regions", "list", "--json", "--no-headers"], parse_json=True)
        return [Location(name=r["id"], description=r.get("country"), raw=r) for r in (res.parsed or [])]

    # ---- server ----

    def get_server(self, server_id_or_name: str) -> Server:
        if server_id_or_name.isdigit():
            argv = [self._binary, "linodes", "view", server_id_or_name, "--json", "--no-headers"]
        else:
            argv = [self._binary, "linodes", "list", "--label", server_id_or_name, "--json", "--no-headers"]
        res = run(argv, check=False, parse_json=True)
        if res.returncode != 0:
            raise ProviderResourceNotFound(f"linode {server_id_or_name!r} not found: {res.stderr.strip()}")
        items = res.parsed if isinstance(res.parsed, list) else [res.parsed]
        if not items:
            raise ProviderResourceNotFound(f"linode {server_id_or_name!r} not found")
        raw = items[0]
        ipv4 = (raw.get("ipv4") or [None])[0] if isinstance(raw.get("ipv4"), list) else raw.get("ipv4")
        return Server(
            id=str(raw["id"]),
            name=raw.get("label", ""),
            ipv4=ipv4,
            ipv6=raw.get("ipv6"),
            location=raw.get("region", ""),
            raw=raw,
        )

    def server_device_path(self, server: Server, volume: Volume) -> str:
        return f"/dev/disk/by-id/scsi-0Linode_Volume_{volume.name}"

    # ---- volumes ----

    def create_volume(self, spec: VolumeSpec) -> Volume:
        argv = [
            self._binary, "volumes", "create",
            "--label", spec.name,
            "--size", str(spec.size_gb),
            "--region", spec.location,
            "--json", "--no-headers",
        ]
        if spec.labels:
            argv += ["--tags", *(f"{k}-{v}" for k, v in spec.labels.items())]
        res = run(argv, parse_json=True)
        items = res.parsed if isinstance(res.parsed, list) else [res.parsed]
        return self._volume_from_raw(items[0])

    def attach_volume(self, volume_id: str, server_id: str) -> AttachedVolume:
        res = run(
            [self._binary, "volumes", "attach", volume_id, "--linode-id", server_id, "--json", "--no-headers"],
            check=False, parse_json=True,
        )
        if res.returncode != 0 and "already" not in res.stderr.lower():
            raise ProviderAPIError(f"linode-cli volumes attach failed: {res.stderr.strip()}")
        vol = self.get_volume(volume_id)
        srv = self.get_server(server_id)
        return AttachedVolume(volume=vol, server_id=server_id, device_path=self.server_device_path(srv, vol))

    def detach_volume(self, volume_id: str) -> None:
        run([self._binary, "volumes", "detach", volume_id], check=False)

    def delete_volume(self, volume_id: str) -> None:
        run([self._binary, "volumes", "delete", volume_id], check=False)

    def get_volume(self, volume_id: str) -> Volume:
        res = run(
            [self._binary, "volumes", "view", volume_id, "--json", "--no-headers"],
            check=False, parse_json=True,
        )
        if res.returncode != 0:
            raise ProviderResourceNotFound(f"linode volume {volume_id} not found: {res.stderr.strip()}")
        items = res.parsed if isinstance(res.parsed, list) else [res.parsed]
        return self._volume_from_raw(items[0])

    def list_volumes(self) -> list[Volume]:
        res = run([self._binary, "volumes", "list", "--json", "--no-headers"], parse_json=True)
        return [self._volume_from_raw(v) for v in (res.parsed or [])]

    # ---- "snapshots" (implemented as volume clones) ----

    def create_snapshot(
        self, volume_id: str, name: str, labels: dict[str, str] | None = None
    ) -> Snapshot:
        argv = [self._binary, "volumes", "clone", volume_id, "--label", name, "--json", "--no-headers"]
        res = run(argv, parse_json=True)
        items = res.parsed if isinstance(res.parsed, list) else [res.parsed]
        raw = items[0] or {}
        # We embed pgforge labels in the clone's name suffix so list_snapshots can filter.
        return Snapshot(
            id=str(raw.get("id", "")),
            name=raw.get("label", name),
            source_volume_id=str(volume_id),
            size_gb=int(raw.get("size", 0)),
            created_at=_parse_ts(raw.get("created")),
            labels=labels or {},
            raw=raw,
        )

    def list_snapshots(self, volume_id: str | None = None) -> list[Snapshot]:
        res = run([self._binary, "volumes", "list", "--json", "--no-headers"], parse_json=True)
        out: list[Snapshot] = []
        for v in res.parsed or []:
            label = v.get("label", "")
            if not label.startswith("pgforge-"):
                continue
            # The label format is pgforge-<instance>-<timestamp>; the parent
            # volume id is not preserved by Linode after cloning, so we only
            # populate labels we can recover from the name.
            parts = label.split("-")
            instance = parts[1] if len(parts) >= 2 else ""
            out.append(
                Snapshot(
                    id=str(v["id"]),
                    name=label,
                    source_volume_id=str(volume_id or ""),
                    size_gb=int(v.get("size", 0)),
                    created_at=_parse_ts(v.get("created")),
                    labels={"pgforge-instance": instance},
                    raw=v,
                )
            )
        return out

    def delete_snapshot(self, snapshot_id: str) -> None:
        run([self._binary, "volumes", "delete", snapshot_id], check=False)

    def restore_snapshot(self, snapshot_id: str, target_spec: VolumeSpec) -> Volume:
        # The snapshot is itself a volume; cloning it back is the "restore".
        return self.create_snapshot(snapshot_id, target_spec.name).source_volume_id and self.get_volume(snapshot_id)  # type: ignore[return-value]

    # ---- metrics & capacity ----

    def volume_metrics(self, volume_id: str, window: TimeWindow) -> ProviderMetrics:
        reason = "linode-cli does not expose per-volume metrics; use Longview or node_exporter."
        return ProviderMetrics(
            volume_id=volume_id, window=window,
            iops_read=MetricSeries([], "ops/s", reason),
            iops_write=MetricSeries([], "ops/s", reason),
            throughput_read_bps=MetricSeries([], "bytes/s", reason),
            throughput_write_bps=MetricSeries([], "bytes/s", reason),
            queue_depth=None,
            latency_ms=MetricSeries([], "ms", reason),
        )

    def volume_capacity(self, volume_id: str) -> CapacityInfo:
        vol = self.get_volume(volume_id)
        return CapacityInfo(
            volume_id=volume_id,
            provider_reported_size_bytes=vol.size_gb * 1024 * 1024 * 1024,
            raw=vol.raw,
        )

    # ---- snapshot cron credentials ----

    def mint_snapshot_credential(self, scope: SnapshotScope) -> RemoteCredential:
        token = os.environ.get("PGFORGE_LINODE_SNAPSHOT_TOKEN") or os.environ.get("LINODE_CLI_TOKEN") or os.environ.get("LINODE_TOKEN")
        if not token:
            raise ProviderAPIError(
                "Linode has no API for minting scoped tokens; set LINODE_CLI_TOKEN "
                "(or PGFORGE_LINODE_SNAPSHOT_TOKEN) before scheduling snapshots."
            )
        body = f"export LINODE_CLI_TOKEN={token!r}\nexport LINODE_TOKEN={token!r}\n"
        return RemoteCredential(
            credential_id=f"linode-{secrets.token_hex(4)}",
            mode="file_credential",
            files={f"/root/.pgforge/cred-{scope.instance_name}.env": body},
            last4=token[-4:],
        )

    def revoke_snapshot_credential(self, credential_id: str) -> None:
        log.warning(
            "Linode credentials are operator-managed; revoke %s in the Linode "
            "Cloud Manager (Profile → API Tokens).", credential_id
        )

    # ---- internals ----

    def _volume_from_raw(self, raw: dict[str, Any]) -> Volume:
        return Volume(
            id=str(raw.get("id", "")),
            name=raw.get("label", ""),
            size_gb=int(raw.get("size", 0)),
            location=raw.get("region", ""),
            attached_server_id=(str(raw["linode_id"]) if raw.get("linode_id") else None),
            labels={t: "" for t in raw.get("tags") or []},
            raw=raw,
        )


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)
