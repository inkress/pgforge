"""Azure provider (Compute + Managed Disks).

Shells out to ``az`` v2. Notable Azure quirks:

* **Resource Groups.** Every resource lives in an RG. pgforge accepts an RG
  via ``--location`` shaped as ``<rg>/<region>`` (e.g.
  ``my-rg/eastus2``), or falls back to ``AZURE_RESOURCE_GROUP`` and
  ``AZURE_LOCATION`` env vars. Volumes and the server must share an RG.

* **Device paths.** Linux VMs map data disks to
  ``/dev/disk/azure/scsi1/lun<N>`` where N is the LUN (0-based, set at attach
  time). pgforge picks the next free LUN.

* **Snapshots are top-level resources** in their own RG; we keep them in the
  same RG as the source disk.

* **Identity-based credentials.** Preferred path: enable a system-assigned
  managed identity on the VM and grant ``Microsoft.Compute/snapshots/write``
  on the RG. Fallback: service principal client_id/client_secret.
"""

from __future__ import annotations

import os
import re
import secrets
import uuid
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

AZ = "az"
MIN_VERSION = "2.50.0"


def _split_location(loc: str | None, default_rg: str | None, default_region: str | None) -> tuple[str, str]:
    if loc and "/" in loc:
        rg, region = loc.split("/", 1)
        return rg, region
    rg = default_rg or os.environ.get("AZURE_RESOURCE_GROUP") or ""
    region = loc or default_region or os.environ.get("AZURE_LOCATION") or ""
    return rg, region


class AzureProvider(Provider):
    name: ClassVar[str] = "azure"
    cli_binary: ClassVar[str] = AZ
    min_cli_version: ClassVar[str] = MIN_VERSION

    def __init__(self, *, resource_group: str | None = None, region: str | None = None) -> None:
        self._binary = require_binary(AZ)
        self._rg = resource_group
        self._region = region
        self._sub: str | None = None

    # ---- preflight ----

    def check_cli(self) -> CLIInfo:
        ver = run([self._binary, "version", "-o", "json"], check=False, parse_json=True)
        cli_ver = (ver.parsed or {}).get("azure-cli", "") if ver.parsed else ""
        check_min_version(cli_ver, MIN_VERSION, name="az")
        acct = run([self._binary, "account", "show", "-o", "json"], check=False, parse_json=True)
        authenticated = acct.returncode == 0
        account = None
        if authenticated and acct.parsed:
            account = acct.parsed.get("user", {}).get("name") or acct.parsed.get("name")
            self._sub = acct.parsed.get("id")
        return CLIInfo(
            binary=self._binary,
            version=cli_ver,
            authenticated=authenticated,
            account=account,
            raw=acct.parsed or {},
        )

    def list_locations(self) -> list[Location]:
        res = run([self._binary, "account", "list-locations", "-o", "json"], parse_json=True)
        return [Location(name=r["name"], description=r.get("displayName"), raw=r) for r in (res.parsed or [])]

    # ---- server ----

    def get_server(self, server_id_or_name: str) -> Server:
        rg, _ = _split_location(None, self._rg, self._region)
        argv = [self._binary, "vm", "show", "--name", server_id_or_name, "-o", "json"]
        if rg:
            argv += ["--resource-group", rg]
        argv += ["--show-details"]
        res = run(argv, check=False, parse_json=True)
        if res.returncode != 0:
            raise ProviderResourceNotFound(f"azure VM {server_id_or_name!r} not found: {res.stderr.strip()}")
        raw = res.parsed or {}
        return Server(
            id=raw.get("vmId") or raw.get("id") or server_id_or_name,
            name=raw.get("name", server_id_or_name),
            ipv4=raw.get("publicIps") or raw.get("privateIps"),
            ipv6=None,
            location=f"{raw.get('resourceGroup', rg)}/{raw.get('location', '')}",
            raw=raw,
        )

    def server_device_path(self, server: Server, volume: Volume) -> str:
        # We attach at LUN N — see attach_volume. Best-effort: read the LUN
        # from the volume's raw payload if present.
        lun = (volume.raw or {}).get("managedBy", "")
        m = re.search(r"lun(\d+)", lun) if isinstance(lun, str) else None
        if not m:
            # Look at attachments in the volume raw.
            attached_lun = (volume.raw or {}).get("lun")
            if attached_lun is not None:
                return f"/dev/disk/azure/scsi1/lun{attached_lun}"
        return "/dev/disk/azure/scsi1/lun0"

    # ---- volumes ----

    def create_volume(self, spec: VolumeSpec) -> Volume:
        rg, region = _split_location(spec.location, self._rg, self._region)
        if not rg or not region:
            raise ProviderAPIError(
                "azure --location must be 'resource-group/region' (got " f"{spec.location!r})"
            )
        argv = [
            self._binary, "disk", "create",
            "--resource-group", rg,
            "--name", spec.name,
            "--size-gb", str(spec.size_gb),
            "--location", region,
            "--sku", "Premium_LRS",
            "-o", "json",
        ]
        if spec.labels:
            argv += ["--tags", *(f"{k}={v}" for k, v in spec.labels.items())]
        res = run(argv, parse_json=True)
        raw = res.parsed or {}
        raw["resourceGroup"] = rg
        return self._volume_from_raw(raw)

    def attach_volume(self, volume_id: str, server_id: str) -> AttachedVolume:
        rg, _ = _split_location(None, self._rg, self._region)
        # Find next free LUN.
        srv_raw = run([self._binary, "vm", "show", "-g", rg, "-n", server_id, "-o", "json"], parse_json=True).parsed or {}
        used = {d.get("lun") for d in srv_raw.get("storageProfile", {}).get("dataDisks", [])}
        lun = next(i for i in range(64) if i not in used)
        argv = [
            self._binary, "vm", "disk", "attach",
            "-g", rg,
            "--vm-name", server_id,
            "--name", volume_id,
            "--lun", str(lun),
            "-o", "json",
        ]
        res = run(argv, check=False, parse_json=True)
        if res.returncode != 0 and "already attached" not in res.stderr.lower():
            raise ProviderAPIError(f"az vm disk attach failed: {res.stderr.strip()}")
        vol = self.get_volume(volume_id)
        vol.raw["lun"] = lun  # remembered for device_path
        return AttachedVolume(volume=vol, server_id=server_id, device_path=f"/dev/disk/azure/scsi1/lun{lun}")

    def detach_volume(self, volume_id: str) -> None:
        vol = self.get_volume(volume_id)
        if not vol.attached_server_id:
            return
        rg, _ = _split_location(None, self._rg, self._region)
        run(
            [
                self._binary, "vm", "disk", "detach",
                "-g", rg,
                "--vm-name", vol.attached_server_id,
                "--name", volume_id,
            ],
            check=False,
        )

    def delete_volume(self, volume_id: str) -> None:
        rg, _ = _split_location(None, self._rg, self._region)
        run(
            [self._binary, "disk", "delete", "-g", rg, "-n", volume_id, "--yes"],
            check=False,
        )

    def get_volume(self, volume_id: str) -> Volume:
        rg, _ = _split_location(None, self._rg, self._region)
        res = run(
            [self._binary, "disk", "show", "-g", rg, "-n", volume_id, "-o", "json"],
            check=False, parse_json=True,
        )
        if res.returncode != 0:
            raise ProviderResourceNotFound(f"azure disk {volume_id} not found: {res.stderr.strip()}")
        raw = res.parsed or {}
        raw["resourceGroup"] = rg
        return self._volume_from_raw(raw)

    def list_volumes(self) -> list[Volume]:
        argv = [self._binary, "disk", "list", "-o", "json"]
        rg, _ = _split_location(None, self._rg, self._region)
        if rg:
            argv += ["-g", rg]
        res = run(argv, parse_json=True)
        return [self._volume_from_raw(d) for d in (res.parsed or [])]

    # ---- snapshots ----

    def create_snapshot(
        self, volume_id: str, name: str, labels: dict[str, str] | None = None
    ) -> Snapshot:
        rg, region = _split_location(None, self._rg, self._region)
        vol = self.get_volume(volume_id)
        src_id = (vol.raw or {}).get("id")
        argv = [
            self._binary, "snapshot", "create",
            "-g", rg, "-n", name,
            "--source", src_id or volume_id,
            "--location", region,
            "-o", "json",
        ]
        if labels:
            argv += ["--tags", *(f"{k}={v}" for k, v in labels.items())]
        res = run(argv, parse_json=True)
        return self._snapshot_from_raw(res.parsed or {}, volume_id)

    def list_snapshots(self, volume_id: str | None = None) -> list[Snapshot]:
        rg, _ = _split_location(None, self._rg, self._region)
        argv = [self._binary, "snapshot", "list", "-o", "json"]
        if rg:
            argv += ["-g", rg]
        res = run(argv, parse_json=True)
        out = []
        for s in res.parsed or []:
            if volume_id and not (s.get("creationData", {}).get("sourceResourceId", "")).endswith(f"/{volume_id}"):
                continue
            out.append(self._snapshot_from_raw(s, volume_id or ""))
        return out

    def delete_snapshot(self, snapshot_id: str) -> None:
        rg, _ = _split_location(None, self._rg, self._region)
        run([self._binary, "snapshot", "delete", "-g", rg, "-n", snapshot_id], check=False)

    def restore_snapshot(self, snapshot_id: str, target_spec: VolumeSpec) -> Volume:
        rg, region = _split_location(target_spec.location, self._rg, self._region)
        snap = run(
            [self._binary, "snapshot", "show", "-g", rg, "-n", snapshot_id, "-o", "json"],
            parse_json=True,
        )
        src_id = (snap.parsed or {}).get("id")
        argv = [
            self._binary, "disk", "create",
            "-g", rg, "-n", target_spec.name,
            "--source", src_id or snapshot_id,
            "--location", region,
            "-o", "json",
        ]
        res = run(argv, parse_json=True)
        raw = res.parsed or {}
        raw["resourceGroup"] = rg
        return self._volume_from_raw(raw)

    # ---- metrics & capacity ----

    def volume_metrics(self, volume_id: str, window: TimeWindow) -> ProviderMetrics:
        vol = self.get_volume(volume_id)
        rid = (vol.raw or {}).get("id")
        if not rid:
            reason = "azure disk id unknown"
            return ProviderMetrics(
                volume_id=volume_id, window=window,
                iops_read=MetricSeries([], "ops/s", reason),
                iops_write=MetricSeries([], "ops/s", reason),
                throughput_read_bps=MetricSeries([], "bytes/s", reason),
                throughput_write_bps=MetricSeries([], "bytes/s", reason),
                queue_depth=None,
                latency_ms=MetricSeries([], "ms", reason),
            )

        def fetch(metric: str, unit: str) -> MetricSeries:
            argv = [
                self._binary, "monitor", "metrics", "list",
                "--resource", rid,
                "--metric", metric,
                "--start-time", window.start.isoformat(),
                "--end-time", window.end.isoformat(),
                "--interval", "PT5M",
                "-o", "json",
            ]
            res = run(argv, check=False, parse_json=True)
            if res.returncode != 0 or not res.parsed:
                return MetricSeries(points=[], unit=unit, unavailable_reason="azure monitor error")
            series = (res.parsed.get("value") or [{}])[0]
            points = []
            for s in series.get("timeseries") or []:
                for d in s.get("data") or []:
                    v = d.get("average") or d.get("total")
                    if v is None:
                        continue
                    points.append((_parse_ts(d.get("timeStamp")), float(v)))
            return MetricSeries(points=sorted(points), unit=unit)

        return ProviderMetrics(
            volume_id=volume_id, window=window,
            iops_read=fetch("Composite Disk Read Operations/sec", "ops/s"),
            iops_write=fetch("Composite Disk Write Operations/sec", "ops/s"),
            throughput_read_bps=fetch("Composite Disk Read Bytes/sec", "bytes/s"),
            throughput_write_bps=fetch("Composite Disk Write Bytes/sec", "bytes/s"),
            queue_depth=None,
            latency_ms=fetch("Disk Read Latency", "ms"),
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
        """Create a service principal scoped to snapshot ops on the RG.

        Returns a file credential containing the SP client id/secret. For the
        recommended path (system-assigned managed identity on the VM), the
        operator should enable it at provision time and pgforge will skip the
        credential file when ``AZURE_USE_MANAGED_IDENTITY=1``.
        """
        if os.environ.get("AZURE_USE_MANAGED_IDENTITY") == "1":
            env_body = "# pgforge: relying on system-assigned managed identity on the VM.\n"
            return RemoteCredential(
                credential_id="azure-managed-identity",
                mode="instance_role",
                files={f"/root/.pgforge/cred-{scope.instance_name}.env": env_body},
                last4="MI",
            )

        rg, _ = _split_location(scope.location, self._rg, self._region)
        if not rg or not self._sub:
            self.check_cli()
        sp_name = f"pgforge-{scope.instance_name}-{uuid.uuid4().hex[:8]}"
        scope_arn = f"/subscriptions/{self._sub}/resourceGroups/{rg}"
        try:
            res = run(
                [
                    self._binary, "ad", "sp", "create-for-rbac",
                    "--name", sp_name,
                    "--role", "Disk Snapshot Contributor",
                    "--scopes", scope_arn,
                    "-o", "json",
                ],
                parse_json=True,
            )
            sp = res.parsed or {}
            client_id = sp.get("appId", "")
            secret = sp.get("password", "")
            tenant = sp.get("tenant", "")
            body = (
                f"export AZURE_CLIENT_ID={client_id!r}\n"
                f"export AZURE_CLIENT_SECRET={secret!r}\n"
                f"export AZURE_TENANT_ID={tenant!r}\n"
                f"export AZURE_SUBSCRIPTION_ID={(self._sub or '')!r}\n"
            )
            return RemoteCredential(
                credential_id=client_id,
                mode="file_credential",
                files={f"/root/.pgforge/cred-{scope.instance_name}.env": body},
                last4=client_id[-4:],
            )
        except Exception as e:
            log.warning("SP mint failed (%s); falling back to ambient credentials", e)
            return RemoteCredential(
                credential_id=f"azure-fallback-{secrets.token_hex(4)}",
                mode="file_credential",
                files={f"/root/.pgforge/cred-{scope.instance_name}.env": "# pgforge could not mint a service principal.\n"},
                last4="????",
            )

    def revoke_snapshot_credential(self, credential_id: str) -> None:
        if credential_id == "azure-managed-identity" or credential_id.startswith("azure-fallback-"):
            return
        run([self._binary, "ad", "sp", "delete", "--id", credential_id], check=False)

    # ---- internals ----

    def _volume_from_raw(self, raw: dict[str, Any]) -> Volume:
        attached = (raw.get("managedBy") or "").rsplit("/", 1)[-1] or None
        return Volume(
            id=raw.get("name", ""),
            name=raw.get("name", ""),
            size_gb=int(raw.get("diskSizeGB") or raw.get("diskSizeGb", 0)),
            location=f"{raw.get('resourceGroup', '')}/{raw.get('location', '')}",
            attached_server_id=attached,
            labels=raw.get("tags") or {},
            raw=raw,
        )

    def _snapshot_from_raw(self, raw: dict[str, Any], volume_id: str) -> Snapshot:
        src = (raw.get("creationData") or {}).get("sourceResourceId", "")
        return Snapshot(
            id=raw.get("name", ""),
            name=raw.get("name", ""),
            source_volume_id=volume_id or src.rsplit("/", 1)[-1],
            size_gb=int(raw.get("diskSizeGB") or raw.get("diskSizeGb", 0)),
            created_at=_parse_ts(raw.get("timeCreated")),
            labels=raw.get("tags") or {},
            raw=raw,
        )


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)
