"""DigitalOcean provider via ``doctl``.

DigitalOcean's CLI is the simplest of the bunch, but has two quirks pgforge
must respect:

* **No instance roles.** Server-side cron always needs a Personal Access
  Token on disk. We use the operator's ``DIGITALOCEAN_ACCESS_TOKEN`` and
  store it (with the last-4 metadata) — DO doesn't allow API-driven token
  minting, so pgforge can never narrow the blast radius below "the whole
  account". Documented loudly.

* **Device path uses the volume *name*,** not the volume id, in
  ``/dev/disk/by-id/scsi-0DO_Volume_<name>``. DigitalOcean restricts
  volume names to lowercase letters, digits and dashes, ≤64 chars; pgforge
  reuses the volume's name verbatim.
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

DOCTL = "doctl"
MIN_VERSION = "1.100.0"


class DigitalOceanProvider(Provider):
    name: ClassVar[str] = "digitalocean"
    cli_binary: ClassVar[str] = DOCTL
    min_cli_version: ClassVar[str] = MIN_VERSION

    def __init__(self) -> None:
        self._binary = require_binary(DOCTL)

    # ---- preflight ----

    def check_cli(self) -> CLIInfo:
        ver = run([self._binary, "version"], check=False)
        m = re.search(r"doctl version (\S+)", ver.stdout) or re.search(r"doctl version (\S+)", ver.stderr)
        version_text = m.group(1) if m else ver.stdout.strip()
        check_min_version(version_text, MIN_VERSION, name="doctl")
        acct = run([self._binary, "account", "get", "-o", "json"], check=False, parse_json=True)
        return CLIInfo(
            binary=self._binary,
            version=version_text,
            authenticated=acct.returncode == 0,
            account=((acct.parsed or [{}])[0] if isinstance(acct.parsed, list) else (acct.parsed or {})).get("email"),
            raw=acct.parsed if isinstance(acct.parsed, dict) else {},
        )

    def list_locations(self) -> list[Location]:
        res = run([self._binary, "compute", "region", "list", "-o", "json"], parse_json=True)
        return [Location(name=r["slug"], description=r.get("name"), raw=r) for r in (res.parsed or [])]

    # ---- server ----

    def get_server(self, server_id_or_name: str) -> Server:
        argv = [self._binary, "compute", "droplet", "get", server_id_or_name, "-o", "json"]
        res = run(argv, check=False, parse_json=True)
        if res.returncode != 0:
            raise ProviderResourceNotFound(f"DO droplet {server_id_or_name!r} not found: {res.stderr.strip()}")
        parsed = res.parsed if isinstance(res.parsed, list) else [res.parsed]
        raw = parsed[0] or {}
        ipv4 = next((n.get("ip_address") for n in (raw.get("networks", {}).get("v4") or []) if n.get("type") == "public"), None)
        ipv6 = next((n.get("ip_address") for n in (raw.get("networks", {}).get("v6") or [])), None)
        return Server(
            id=str(raw["id"]),
            name=raw.get("name", ""),
            ipv4=ipv4,
            ipv6=ipv6,
            location=(raw.get("region") or {}).get("slug", ""),
            raw=raw,
        )

    def server_device_path(self, server: Server, volume: Volume) -> str:
        # DO uses the volume *name*, not the id.
        return f"/dev/disk/by-id/scsi-0DO_Volume_{volume.name}"

    # ---- volumes ----

    def create_volume(self, spec: VolumeSpec) -> Volume:
        argv = [
            self._binary, "compute", "volume", "create", spec.name,
            "--size", f"{spec.size_gb}GiB",
            "--region", spec.location,
            "--fs-type", "",  # we format ourselves via LUKS
            "-o", "json",
        ]
        if spec.labels:
            argv += ["--tag", ",".join(f"{k}-{v}" for k, v in spec.labels.items())]
        res = run(argv, parse_json=True)
        parsed = res.parsed if isinstance(res.parsed, list) else [res.parsed]
        return self._volume_from_raw(parsed[0])

    def attach_volume(self, volume_id: str, server_id: str) -> AttachedVolume:
        argv = [self._binary, "compute", "volume-action", "attach", volume_id, server_id, "--wait", "-o", "json"]
        res = run(argv, check=False, parse_json=True)
        if res.returncode != 0 and "already" not in res.stderr.lower():
            raise ProviderAPIError(f"doctl attach failed: {res.stderr.strip()}")
        vol = self.get_volume(volume_id)
        srv = self.get_server(server_id)
        return AttachedVolume(volume=vol, server_id=server_id, device_path=self.server_device_path(srv, vol))

    def detach_volume(self, volume_id: str) -> None:
        vol = self.get_volume(volume_id)
        if not vol.attached_server_id:
            return
        run(
            [self._binary, "compute", "volume-action", "detach", volume_id, vol.attached_server_id, "--wait"],
            check=False,
        )

    def delete_volume(self, volume_id: str) -> None:
        run([self._binary, "compute", "volume", "delete", volume_id, "--force"], check=False)

    def get_volume(self, volume_id: str) -> Volume:
        argv = [self._binary, "compute", "volume", "get", volume_id, "-o", "json"]
        res = run(argv, check=False, parse_json=True)
        if res.returncode != 0:
            raise ProviderResourceNotFound(f"DO volume {volume_id} not found: {res.stderr.strip()}")
        parsed = res.parsed if isinstance(res.parsed, list) else [res.parsed]
        return self._volume_from_raw(parsed[0])

    def list_volumes(self) -> list[Volume]:
        res = run([self._binary, "compute", "volume", "list", "-o", "json"], parse_json=True)
        return [self._volume_from_raw(v) for v in (res.parsed or [])]

    # ---- snapshots ----

    def create_snapshot(
        self, volume_id: str, name: str, labels: dict[str, str] | None = None
    ) -> Snapshot:
        # doctl tags snapshots, but labels-as-tags are a noisy fit; only the
        # name is preserved. We embed the pgforge label as a colon-separated
        # tag for `list_snapshots` filtering.
        tag_parts = [f"pgforge-instance-{labels['pgforge-instance']}"] if labels and "pgforge-instance" in labels else []
        argv = [
            self._binary, "compute", "volume", "snapshot", volume_id,
            "--snapshot-name", name,
            "-o", "json",
        ]
        if tag_parts:
            argv += ["--tag", ",".join(tag_parts)]
        res = run(argv, parse_json=True)
        parsed = res.parsed if isinstance(res.parsed, list) else [res.parsed]
        return self._snapshot_from_raw(parsed[0], volume_id)

    def list_snapshots(self, volume_id: str | None = None) -> list[Snapshot]:
        argv = [self._binary, "compute", "snapshot", "list", "--resource", "volume", "-o", "json"]
        res = run(argv, parse_json=True)
        out: list[Snapshot] = []
        for s in res.parsed or []:
            src = (s.get("resource_id") or "").strip()
            if volume_id and src != str(volume_id):
                continue
            out.append(self._snapshot_from_raw(s, volume_id or src))
        return out

    def delete_snapshot(self, snapshot_id: str) -> None:
        run([self._binary, "compute", "snapshot", "delete", snapshot_id, "--force"], check=False)

    def restore_snapshot(self, snapshot_id: str, target_spec: VolumeSpec) -> Volume:
        argv = [
            self._binary, "compute", "volume", "create", target_spec.name,
            "--snapshot", snapshot_id,
            "--region", target_spec.location,
            "-o", "json",
        ]
        if target_spec.size_gb:
            argv += ["--size", f"{target_spec.size_gb}GiB"]
        res = run(argv, parse_json=True)
        parsed = res.parsed if isinstance(res.parsed, list) else [res.parsed]
        return self._volume_from_raw(parsed[0])

    # ---- metrics & capacity ----

    def volume_metrics(self, volume_id: str, window: TimeWindow) -> ProviderMetrics:
        reason = (
            "DigitalOcean does not expose per-volume IOPS or throughput via "
            "doctl; install node_exporter on the VM for in-guest metrics."
        )
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
        token = os.environ.get("PGFORGE_DIGITALOCEAN_SNAPSHOT_TOKEN") or os.environ.get("DIGITALOCEAN_ACCESS_TOKEN")
        if not token:
            raise ProviderAPIError(
                "DigitalOcean cannot mint per-instance tokens via API. Set "
                "DIGITALOCEAN_ACCESS_TOKEN (or PGFORGE_DIGITALOCEAN_SNAPSHOT_TOKEN "
                "to use a separate token) before scheduling snapshots."
            )
        body = f"export DIGITALOCEAN_ACCESS_TOKEN={token!r}\n"
        return RemoteCredential(
            credential_id=f"do-{secrets.token_hex(4)}",
            mode="file_credential",
            files={f"/root/.pgforge/cred-{scope.instance_name}.env": body},
            last4=token[-4:],
        )

    def revoke_snapshot_credential(self, credential_id: str) -> None:
        log.warning(
            "DigitalOcean credentials are operator-managed; revoke %s in the "
            "Cloud Console (API → Tokens).", credential_id
        )

    # ---- internals ----

    def _volume_from_raw(self, raw: dict[str, Any]) -> Volume:
        attached_id = None
        droplet_ids = raw.get("droplet_ids") or []
        if droplet_ids:
            attached_id = str(droplet_ids[0])
        return Volume(
            id=str(raw.get("id", "")),
            name=raw.get("name", ""),
            size_gb=int(raw.get("size_gigabytes", 0)),
            location=(raw.get("region") or {}).get("slug", "") if isinstance(raw.get("region"), dict) else str(raw.get("region", "")),
            attached_server_id=attached_id,
            labels={t: "" for t in raw.get("tags") or []},
            raw=raw,
        )

    def _snapshot_from_raw(self, raw: dict[str, Any], volume_id: str) -> Snapshot:
        return Snapshot(
            id=str(raw.get("id", "")),
            name=raw.get("name", ""),
            source_volume_id=str(raw.get("resource_id") or volume_id),
            size_gb=int(raw.get("size_gigabytes", 0) or raw.get("min_disk_size", 0)),
            created_at=_parse_ts(raw.get("created_at")),
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
