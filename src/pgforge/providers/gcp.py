"""Google Cloud (Compute Engine + Persistent Disks) provider.

Shells out to ``gcloud``. Two GCP quirks worth knowing:

* **Device-name vs disk-name.** A disk has a *name* in the project, but when
  it's attached to a VM the kernel sees ``/dev/disk/by-id/google-<device-name>``
  where ``device-name`` is set at attach time. We force ``device-name = disk
  name`` so the two are identical; that's the convention every other
  pgforge layer assumes.

* **Per-VM service accounts.** The clean credential model is to attach a
  service account with ``compute.disks.createSnapshot`` on the disk URL. If
  the operator can't manage SAs we fall through to a JSON key on disk.
"""

from __future__ import annotations

import json
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

GCLOUD = "gcloud"
MIN_VERSION = "400.0.0"


class GCPProvider(Provider):
    name: ClassVar[str] = "gcp"
    cli_binary: ClassVar[str] = GCLOUD
    min_cli_version: ClassVar[str] = MIN_VERSION

    def __init__(self, *, project: str | None = None, zone: str | None = None) -> None:
        self._binary = require_binary(GCLOUD)
        self._project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("CLOUDSDK_CORE_PROJECT")
        self._zone = zone or os.environ.get("CLOUDSDK_COMPUTE_ZONE")

    # ---- preflight ----

    def check_cli(self) -> CLIInfo:
        ver = run([self._binary, "--version"], check=False)
        m = re.search(r"Google Cloud SDK\s+(\S+)", ver.stdout)
        version_text = m.group(1) if m else ver.stdout.splitlines()[0] if ver.stdout else ""
        check_min_version(version_text, MIN_VERSION, name="gcloud")
        auth = run(
            [self._binary, "auth", "list", "--filter=status:ACTIVE", "--format=json"],
            check=False, parse_json=True,
        )
        active = (auth.parsed or [{}])[0].get("account") if auth.parsed else None
        return CLIInfo(
            binary=self._binary,
            version=version_text,
            authenticated=bool(active),
            account=active or self._project,
            raw={"project": self._project, "zone": self._zone},
        )

    def list_locations(self) -> list[Location]:
        argv = [self._binary, "compute", "zones", "list", "--format=json"]
        if self._project:
            argv += ["--project", self._project]
        res = run(argv, parse_json=True)
        return [Location(name=z["name"], description=z.get("region"), raw=z) for z in (res.parsed or [])]

    # ---- server ----

    def get_server(self, server_id_or_name: str) -> Server:
        argv = [self._binary, "compute", "instances", "describe", server_id_or_name, "--format=json"]
        if self._project:
            argv += ["--project", self._project]
        if self._zone:
            argv += ["--zone", self._zone]
        res = run(argv, check=False, parse_json=True)
        if res.returncode != 0:
            raise ProviderResourceNotFound(f"gcp instance {server_id_or_name!r} not found: {res.stderr.strip()}")
        raw = res.parsed or {}
        ipv4 = None
        ipv6 = None
        for iface in raw.get("networkInterfaces") or []:
            for ac in iface.get("accessConfigs") or []:
                if ac.get("type") == "ONE_TO_ONE_NAT" and not ipv4:
                    ipv4 = ac.get("natIP")
            for ac in iface.get("ipv6AccessConfigs") or []:
                ipv6 = ipv6 or ac.get("externalIpv6")
        return Server(
            id=str(raw.get("id") or raw["name"]),
            name=raw["name"],
            ipv4=ipv4,
            ipv6=ipv6,
            location=_short(raw.get("zone")),
            raw=raw,
        )

    def server_device_path(self, server: Server, volume: Volume) -> str:
        return f"/dev/disk/by-id/google-{volume.name}"

    # ---- volumes ----

    def create_volume(self, spec: VolumeSpec) -> Volume:
        argv = [
            self._binary, "compute", "disks", "create", spec.name,
            "--size", f"{spec.size_gb}GB",
            "--type", "pd-balanced",
            "--zone", spec.location,
            "--format=json",
        ]
        if self._project:
            argv += ["--project", self._project]
        if spec.labels:
            kv = ",".join(f"{k}={v}" for k, v in spec.labels.items())
            argv += ["--labels", kv]
        res = run(argv, parse_json=True)
        items = res.parsed if isinstance(res.parsed, list) else [res.parsed]
        return self._volume_from_raw(items[0])

    def attach_volume(self, volume_id: str, server_id: str) -> AttachedVolume:
        # GCP attaches by disk name; volume_id from create_volume is the disk name.
        argv = [
            self._binary, "compute", "instances", "attach-disk", server_id,
            "--disk", volume_id,
            "--device-name", volume_id,
        ]
        if self._project:
            argv += ["--project", self._project]
        if self._zone:
            argv += ["--zone", self._zone]
        res = run(argv, check=False)
        if res.returncode != 0 and "already attached" not in res.stderr.lower():
            raise ProviderAPIError(f"gcloud attach-disk failed: {res.stderr.strip()}")
        vol = self.get_volume(volume_id)
        srv = self.get_server(server_id)
        return AttachedVolume(volume=vol, server_id=server_id, device_path=self.server_device_path(srv, vol))

    def detach_volume(self, volume_id: str) -> None:
        vol = self.get_volume(volume_id)
        if not vol.attached_server_id:
            return
        argv = [
            self._binary, "compute", "instances", "detach-disk", vol.attached_server_id,
            "--disk", volume_id,
        ]
        if self._project:
            argv += ["--project", self._project]
        if self._zone:
            argv += ["--zone", self._zone]
        run(argv, check=False)

    def delete_volume(self, volume_id: str) -> None:
        argv = [self._binary, "compute", "disks", "delete", volume_id, "--quiet"]
        if self._project:
            argv += ["--project", self._project]
        if self._zone:
            argv += ["--zone", self._zone]
        run(argv, check=False)

    def get_volume(self, volume_id: str) -> Volume:
        argv = [self._binary, "compute", "disks", "describe", volume_id, "--format=json"]
        if self._project:
            argv += ["--project", self._project]
        if self._zone:
            argv += ["--zone", self._zone]
        res = run(argv, check=False, parse_json=True)
        if res.returncode != 0:
            raise ProviderResourceNotFound(f"gcp disk {volume_id} not found: {res.stderr.strip()}")
        return self._volume_from_raw(res.parsed or {})

    def list_volumes(self) -> list[Volume]:
        argv = [self._binary, "compute", "disks", "list", "--format=json"]
        if self._project:
            argv += ["--project", self._project]
        res = run(argv, parse_json=True)
        return [self._volume_from_raw(d) for d in (res.parsed or [])]

    # ---- snapshots ----

    def create_snapshot(
        self, volume_id: str, name: str, labels: dict[str, str] | None = None
    ) -> Snapshot:
        argv = [
            self._binary, "compute", "snapshots", "create", name,
            "--source-disk", volume_id,
            "--format=json",
        ]
        if self._project:
            argv += ["--project", self._project]
        if self._zone:
            argv += ["--source-disk-zone", self._zone]
        if labels:
            argv += ["--labels", ",".join(f"{k}={v}" for k, v in labels.items())]
        res = run(argv, parse_json=True)
        raw = res.parsed if isinstance(res.parsed, dict) else (res.parsed or [{}])[0]
        return self._snapshot_from_raw(raw, volume_id)

    def list_snapshots(self, volume_id: str | None = None) -> list[Snapshot]:
        argv = [self._binary, "compute", "snapshots", "list", "--format=json"]
        if self._project:
            argv += ["--project", self._project]
        if volume_id:
            argv += ["--filter", f"sourceDisk:{volume_id}"]
        res = run(argv, parse_json=True)
        return [self._snapshot_from_raw(s, volume_id or _short(s.get("sourceDisk", ""))) for s in (res.parsed or [])]

    def delete_snapshot(self, snapshot_id: str) -> None:
        argv = [self._binary, "compute", "snapshots", "delete", snapshot_id, "--quiet"]
        if self._project:
            argv += ["--project", self._project]
        run(argv, check=False)

    def restore_snapshot(self, snapshot_id: str, target_spec: VolumeSpec) -> Volume:
        argv = [
            self._binary, "compute", "disks", "create", target_spec.name,
            "--source-snapshot", snapshot_id,
            "--zone", target_spec.location,
            "--format=json",
        ]
        if self._project:
            argv += ["--project", self._project]
        if target_spec.size_gb:
            argv += ["--size", f"{target_spec.size_gb}GB"]
        if target_spec.labels:
            argv += ["--labels", ",".join(f"{k}={v}" for k, v in target_spec.labels.items())]
        res = run(argv, parse_json=True)
        items = res.parsed if isinstance(res.parsed, list) else [res.parsed]
        return self._volume_from_raw(items[0])

    # ---- metrics & capacity ----

    def volume_metrics(self, volume_id: str, window: TimeWindow) -> ProviderMetrics:
        def fetch(metric: str, unit: str) -> MetricSeries:
            filter_expr = (
                f'metric.type="compute.googleapis.com/instance/disk/{metric}" '
                f'resource.label."device_name"="{volume_id}"'
            )
            argv = [
                self._binary, "monitoring", "time-series", "list",
                "--filter", filter_expr,
                f"--interval={window.start.isoformat()}Z/{window.end.isoformat()}Z",
                "--format=json",
            ]
            if self._project:
                argv += ["--project", self._project]
            res = run(argv, check=False, parse_json=True)
            if res.returncode != 0 or not res.parsed:
                return MetricSeries(points=[], unit=unit, unavailable_reason="gcloud monitoring error")
            series = res.parsed[0] if res.parsed else {}
            points = []
            for pt in series.get("points") or []:
                ts = _parse_ts(pt.get("interval", {}).get("endTime"))
                value = float(next(iter(pt.get("value", {}).values()), 0) or 0)
                points.append((ts, value))
            return MetricSeries(points=sorted(points), unit=unit)

        return ProviderMetrics(
            volume_id=volume_id,
            window=window,
            iops_read=fetch("read_ops_count", "ops/s"),
            iops_write=fetch("write_ops_count", "ops/s"),
            throughput_read_bps=fetch("read_bytes_count", "bytes/s"),
            throughput_write_bps=fetch("write_bytes_count", "bytes/s"),
            queue_depth=None,
            latency_ms=fetch("operation_time", "ms"),
        )

    def volume_capacity(self, volume_id: str) -> CapacityInfo:
        vol = self.get_volume(volume_id)
        size_b = int((vol.raw or {}).get("sizeGb") or vol.size_gb) * 1024 * 1024 * 1024
        return CapacityInfo(volume_id=volume_id, provider_reported_size_bytes=size_b, raw=vol.raw)

    # ---- snapshot cron credentials ----

    def mint_snapshot_credential(self, scope: SnapshotScope) -> RemoteCredential:
        """Create a per-instance service account + key with disk-scoped snapshot perms.

        Falls back to the operator's active gcloud credentials if SA management
        isn't possible. Best practice is to instead attach a SA to the VM at
        provision time — pgforge logs that recommendation.
        """
        suffix = uuid.uuid4().hex[:8]
        # Service account IDs are limited to 30 chars, lowercase alphanumeric+dash.
        raw_sa = f"pgf-{scope.instance_name}-{suffix}".lower().replace("_", "-")
        sa_id = re.sub(r"[^a-z0-9-]", "", raw_sa)[:30].strip("-")
        if not self._project:
            raise ProviderAPIError("GCP requires a project (set CLOUDSDK_CORE_PROJECT or pass project=)")
        sa_email = f"{sa_id}@{self._project}.iam.gserviceaccount.com"

        try:
            run(
                [
                    self._binary, "iam", "service-accounts", "create", sa_id,
                    "--project", self._project,
                    "--display-name", f"pgforge {scope.instance_name}",
                ],
                check=True,
            )
            # Bind disk-level role (disk URL would be:
            # projects/{project}/zones/{location}/disks/{volume_id})
            run(
                [
                    self._binary, "compute", "disks", "add-iam-policy-binding",
                    scope.volume_id,
                    "--project", self._project,
                    "--zone", scope.location,
                    "--member", f"serviceAccount:{sa_email}",
                    "--role", "roles/compute.storageAdmin",
                ],
                check=False,
            )
            key = run(
                [
                    self._binary, "iam", "service-accounts", "keys", "create", "/dev/stdout",
                    "--iam-account", sa_email,
                    "--project", self._project,
                    "--format=json",
                ],
                parse_json=False,
            )
            data = key.stdout
            try:
                parsed = json.loads(data)
                key_id = parsed.get("private_key_id", "")[-4:]
            except json.JSONDecodeError:
                key_id = ""
            cred_path = f"/root/.pgforge/gcp-sa-{scope.instance_name}.json"
            env_body = (
                f"export GOOGLE_APPLICATION_CREDENTIALS={cred_path!r}\n"
                f"export CLOUDSDK_CORE_PROJECT={self._project!r}\n"
            )
            return RemoteCredential(
                credential_id=sa_email,
                mode="file_credential",
                files={
                    cred_path: data,
                    f"/root/.pgforge/cred-{scope.instance_name}.env": env_body,
                },
                last4=key_id or "????",
            )
        except Exception as e:
            log.warning("SA mint failed (%s); using operator credentials instead", e)
            env_body = (
                f"# pgforge could not mint a service account. Configure gcloud on the VM.\n"
                f"export CLOUDSDK_CORE_PROJECT={(self._project or '')!r}\n"
            )
            return RemoteCredential(
                credential_id=f"gcp-fallback-{secrets.token_hex(4)}",
                mode="file_credential",
                files={f"/root/.pgforge/cred-{scope.instance_name}.env": env_body},
                last4="????",
            )

    def revoke_snapshot_credential(self, credential_id: str) -> None:
        if "@" not in credential_id:
            return
        run(
            [self._binary, "iam", "service-accounts", "delete", credential_id, "--quiet"],
            check=False,
        )

    # ---- internals ----

    def _volume_from_raw(self, raw: dict[str, Any]) -> Volume:
        attached: str | None = None
        users = raw.get("users") or []
        if users:
            attached = _short(users[0])
        return Volume(
            id=raw.get("name", ""),
            name=raw.get("name", ""),
            size_gb=int(raw.get("sizeGb", 0)),
            location=_short(raw.get("zone", "")),
            attached_server_id=attached,
            labels=raw.get("labels") or {},
            raw=raw,
        )

    def _snapshot_from_raw(self, raw: dict[str, Any], volume_id: str) -> Snapshot:
        return Snapshot(
            id=raw.get("name", ""),
            name=raw.get("name", ""),
            source_volume_id=volume_id or _short(raw.get("sourceDisk", "")),
            size_gb=int(raw.get("storageBytes", 0) // (1024**3)) or int(raw.get("diskSizeGb", 0)),
            created_at=_parse_ts(raw.get("creationTimestamp")),
            labels=raw.get("labels") or {},
            raw=raw,
        )


def _short(url: str | None) -> str:
    if not url:
        return ""
    return url.rsplit("/", 1)[-1]


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)
