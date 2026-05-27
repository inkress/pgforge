"""AWS EC2 + EBS provider.

Shells out to ``aws`` v2. The most divergent provider in the lineup:

* **Device paths.** Modern AWS instances are on Nitro and expose volumes as
  ``/dev/nvme*n1``. The kernel's persistent ID for an EBS volume is
  ``/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_<volid-without-dash>``.
  We use that — the legacy ``/dev/xvdf``-style hints are unreliable.

* **Metrics.** CloudWatch exposes ``VolumeReadOps``, ``VolumeWriteOps``,
  ``VolumeReadBytes``, ``VolumeWriteBytes``, ``VolumeQueueLength``,
  ``VolumeIdleTime`` — pgforge fetches a small subset and normalizes.

* **Credentials for cron.** When the operator has IAM-management permissions
  pgforge mints a per-instance IAM user + access key + policy
  ``ec2:CreateSnapshot/DeleteSnapshot/DescribeSnapshots`` scoped to the
  volume ARN. Otherwise we fall back to whatever credentials the operator
  has in their shell, which gets installed as a file credential on the
  server — with a loud warning.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
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

AWSCLI = "aws"
MIN_VERSION = "2.0.0"


class AWSProvider(Provider):
    name: ClassVar[str] = "aws"
    cli_binary: ClassVar[str] = AWSCLI
    min_cli_version: ClassVar[str] = MIN_VERSION

    def __init__(self, *, region: str | None = None) -> None:
        self._binary = require_binary(AWSCLI)
        self._region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        self._account_id: str | None = None

    # ---- preflight ----

    def check_cli(self) -> CLIInfo:
        ver = run([self._binary, "--version"], check=False)
        m = re.search(r"aws-cli/(\S+)", ver.stdout) or re.search(r"aws-cli/(\S+)", ver.stderr)
        version_text = m.group(1) if m else (ver.stdout or "").strip()
        check_min_version(version_text, MIN_VERSION, name="aws")
        # `aws sts get-caller-identity` is the canonical authentication probe.
        sts = run(
            [self._binary, "sts", "get-caller-identity", "--output", "json"],
            check=False, parse_json=True,
        )
        authenticated = sts.returncode == 0
        account = None
        if authenticated and sts.parsed:
            account = sts.parsed.get("Account")
            self._account_id = account
        return CLIInfo(
            binary=self._binary,
            version=version_text,
            authenticated=authenticated,
            account=account,
            raw=sts.parsed or {},
        )

    def list_locations(self) -> list[Location]:
        argv = [self._binary, "ec2", "describe-regions", "--output", "json"]
        if self._region:
            argv += ["--region", self._region]
        res = run(argv, parse_json=True)
        out = []
        for region in (res.parsed or {}).get("Regions", []):
            out.append(Location(name=region["RegionName"], description=region.get("OptInStatus"), raw=region))
        return out

    # ---- server ----

    def get_server(self, server_id_or_name: str) -> Server:
        # Accept either an instance id (i-...) or a Name tag.
        filters: list[str]
        if server_id_or_name.startswith("i-"):
            filters = ["--instance-ids", server_id_or_name]
        else:
            filters = ["--filters", f"Name=tag:Name,Values={server_id_or_name}"]
        argv = [self._binary, "ec2", "describe-instances", *filters, "--output", "json"]
        if self._region:
            argv += ["--region", self._region]
        res = run(argv, parse_json=True, check=False)
        if res.returncode != 0:
            raise ProviderResourceNotFound(
                f"aws describe-instances failed for {server_id_or_name!r}: {res.stderr.strip()}"
            )
        instances = []
        for r in (res.parsed or {}).get("Reservations", []):
            instances.extend(r.get("Instances", []))
        if not instances:
            raise ProviderResourceNotFound(f"no EC2 instance matches {server_id_or_name!r}")
        inst = instances[0]
        name = ""
        for tag in inst.get("Tags") or []:
            if tag.get("Key") == "Name":
                name = tag.get("Value", "")
                break
        return Server(
            id=inst["InstanceId"],
            name=name or inst["InstanceId"],
            ipv4=inst.get("PublicIpAddress") or inst.get("PrivateIpAddress"),
            ipv6=inst.get("Ipv6Address"),
            location=inst.get("Placement", {}).get("AvailabilityZone", ""),
            raw=inst,
        )

    def server_device_path(self, server: Server, volume: Volume) -> str:
        # Nitro instances expose EBS volumes via NVMe; the persistent device id
        # is the volume id with the dash dropped, prefixed with 'vol'.
        vid = volume.id.replace("-", "")
        return f"/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_{vid}"

    # ---- volumes ----

    def create_volume(self, spec: VolumeSpec) -> Volume:
        # AWS needs an AZ, not a region; if location looks like a region, fail loudly.
        az = spec.location
        if re.match(r"^[a-z]{2}-[a-z]+-\d+$", az):
            raise ProviderAPIError(
                f"AWS volumes need an availability zone (e.g. us-east-1a), got region {az!r}"
            )
        tag_specs = "ResourceType=volume,Tags=[" + ",".join(
            [f"{{Key=Name,Value={spec.name}}}"]
            + [f"{{Key={k},Value={v}}}" for k, v in spec.labels.items()]
        ) + "]"
        argv = [
            self._binary, "ec2", "create-volume",
            "--availability-zone", az,
            "--size", str(spec.size_gb),
            "--volume-type", "gp3",
            "--tag-specifications", tag_specs,
            "--output", "json",
        ]
        if self._region:
            argv += ["--region", self._region]
        res = run(argv, parse_json=True)
        raw = res.parsed or {}
        vol = self._volume_from_raw(raw)
        # Wait for "available".
        self._wait_volume_state(vol.id, "available")
        return self.get_volume(vol.id)

    def attach_volume(self, volume_id: str, server_id: str) -> AttachedVolume:
        # Pick the next free /dev/sdX letter (Nitro re-maps to /dev/nvme*n1).
        device = "/dev/sdf"
        argv = [
            self._binary, "ec2", "attach-volume",
            "--volume-id", volume_id,
            "--instance-id", server_id,
            "--device", device,
            "--output", "json",
        ]
        if self._region:
            argv += ["--region", self._region]
        res = run(argv, check=False, parse_json=True)
        if res.returncode != 0 and "already attached" not in res.stderr.lower():
            raise ProviderAPIError(f"aws attach-volume failed: {res.stderr.strip()}")
        self._wait_volume_state(volume_id, "in-use")
        vol = self.get_volume(volume_id)
        srv = self.get_server(server_id)
        return AttachedVolume(volume=vol, server_id=server_id, device_path=self.server_device_path(srv, vol))

    def detach_volume(self, volume_id: str) -> None:
        argv = [self._binary, "ec2", "detach-volume", "--volume-id", volume_id, "--output", "json"]
        if self._region:
            argv += ["--region", self._region]
        run(argv, check=False)
        self._wait_volume_state(volume_id, "available", missing_ok=True)

    def delete_volume(self, volume_id: str) -> None:
        argv = [self._binary, "ec2", "delete-volume", "--volume-id", volume_id]
        if self._region:
            argv += ["--region", self._region]
        run(argv, check=False)

    def get_volume(self, volume_id: str) -> Volume:
        argv = [
            self._binary, "ec2", "describe-volumes",
            "--volume-ids", volume_id,
            "--output", "json",
        ]
        if self._region:
            argv += ["--region", self._region]
        res = run(argv, parse_json=True, check=False)
        if res.returncode != 0:
            raise ProviderResourceNotFound(f"aws volume {volume_id} not found: {res.stderr.strip()}")
        vols = (res.parsed or {}).get("Volumes", [])
        if not vols:
            raise ProviderResourceNotFound(f"aws volume {volume_id} not found")
        return self._volume_from_raw(vols[0])

    def list_volumes(self) -> list[Volume]:
        argv = [self._binary, "ec2", "describe-volumes", "--output", "json"]
        if self._region:
            argv += ["--region", self._region]
        res = run(argv, parse_json=True)
        return [self._volume_from_raw(v) for v in (res.parsed or {}).get("Volumes", [])]

    # ---- snapshots ----

    def create_snapshot(
        self, volume_id: str, name: str, labels: dict[str, str] | None = None
    ) -> Snapshot:
        tag_pairs = [f"{{Key=Name,Value={name}}}"]
        for k, v in (labels or {}).items():
            tag_pairs.append(f"{{Key={k},Value={v}}}")
        tag_specs = "ResourceType=snapshot,Tags=[" + ",".join(tag_pairs) + "]"
        argv = [
            self._binary, "ec2", "create-snapshot",
            "--volume-id", volume_id,
            "--description", name,
            "--tag-specifications", tag_specs,
            "--output", "json",
        ]
        if self._region:
            argv += ["--region", self._region]
        res = run(argv, parse_json=True)
        return self._snapshot_from_raw(res.parsed or {}, volume_id)

    def list_snapshots(self, volume_id: str | None = None) -> list[Snapshot]:
        argv = [
            self._binary, "ec2", "describe-snapshots",
            "--owner-ids", "self",
            "--output", "json",
        ]
        if volume_id:
            argv += ["--filters", f"Name=volume-id,Values={volume_id}"]
        if self._region:
            argv += ["--region", self._region]
        res = run(argv, parse_json=True)
        return [
            self._snapshot_from_raw(s, s.get("VolumeId", volume_id or ""))
            for s in (res.parsed or {}).get("Snapshots", [])
        ]

    def delete_snapshot(self, snapshot_id: str) -> None:
        argv = [self._binary, "ec2", "delete-snapshot", "--snapshot-id", snapshot_id]
        if self._region:
            argv += ["--region", self._region]
        run(argv, check=False)

    def restore_snapshot(self, snapshot_id: str, target_spec: VolumeSpec) -> Volume:
        tag_pairs = [f"{{Key=Name,Value={target_spec.name}}}"]
        for k, v in target_spec.labels.items():
            tag_pairs.append(f"{{Key={k},Value={v}}}")
        tag_specs = "ResourceType=volume,Tags=[" + ",".join(tag_pairs) + "]"
        argv = [
            self._binary, "ec2", "create-volume",
            "--snapshot-id", snapshot_id,
            "--availability-zone", target_spec.location,
            "--volume-type", "gp3",
            "--tag-specifications", tag_specs,
            "--output", "json",
        ]
        if target_spec.size_gb:
            argv += ["--size", str(target_spec.size_gb)]
        if self._region:
            argv += ["--region", self._region]
        res = run(argv, parse_json=True)
        vol = self._volume_from_raw(res.parsed or {})
        self._wait_volume_state(vol.id, "available")
        return self.get_volume(vol.id)

    # ---- metrics & capacity ----

    def volume_metrics(self, volume_id: str, window: TimeWindow) -> ProviderMetrics:
        def fetch(metric: str, statistic: str, unit: str, divisor: float = 1.0) -> MetricSeries:
            argv = [
                self._binary, "cloudwatch", "get-metric-statistics",
                "--namespace", "AWS/EBS",
                "--metric-name", metric,
                "--dimensions", f"Name=VolumeId,Value={volume_id}",
                "--start-time", window.start.isoformat(),
                "--end-time", window.end.isoformat(),
                "--period", "300",
                "--statistics", statistic,
                "--output", "json",
            ]
            if self._region:
                argv += ["--region", self._region]
            res = run(argv, check=False, parse_json=True)
            if res.returncode != 0 or not res.parsed:
                return MetricSeries(points=[], unit=unit, unavailable_reason="cloudwatch error")
            dps = sorted(
                ((dp["Timestamp"], float(dp[statistic]) / divisor) for dp in res.parsed.get("Datapoints", [])),
                key=lambda p: p[0],
            )
            return MetricSeries(
                points=[(_parse_ts(t), v) for t, v in dps],
                unit=unit,
            )

        return ProviderMetrics(
            volume_id=volume_id,
            window=window,
            iops_read=fetch("VolumeReadOps", "Sum", "ops/s", divisor=300.0),
            iops_write=fetch("VolumeWriteOps", "Sum", "ops/s", divisor=300.0),
            throughput_read_bps=fetch("VolumeReadBytes", "Sum", "bytes/s", divisor=300.0),
            throughput_write_bps=fetch("VolumeWriteBytes", "Sum", "bytes/s", divisor=300.0),
            queue_depth=fetch("VolumeQueueLength", "Average", "ops"),
            latency_ms=MetricSeries(
                points=[], unit="ms",
                unavailable_reason="AWS exposes idle time; derive latency externally",
            ),
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
        """Mint an IAM user + access key scoped to one volume's snapshot ops.

        Falls back to file-credential mode using the operator's own AWS keys if
        IAM management isn't available (typically because the operator's role
        doesn't have iam:CreateUser).
        """
        if not self._account_id:
            self.check_cli()
        suffix = uuid.uuid4().hex[:8]
        user = f"pgforge-{scope.instance_name}-{suffix}"
        policy_doc = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "ec2:CreateSnapshot",
                            "ec2:DeleteSnapshot",
                            "ec2:DescribeSnapshots",
                            "ec2:CreateTags",
                        ],
                        "Resource": "*",
                        "Condition": {
                            "StringEquals": {
                                "ec2:SourceVolumeArn": (
                                    f"arn:aws:ec2:{(self._region or '*')}:"
                                    f"{self._account_id or '*'}:volume/{scope.volume_id}"
                                )
                            }
                        },
                    },
                ],
            }
        )

        # Try the privileged path first.
        try:
            run([self._binary, "iam", "create-user", "--user-name", user, "--output", "json"], parse_json=True)
            run(
                [
                    self._binary, "iam", "put-user-policy",
                    "--user-name", user,
                    "--policy-name", "pgforge-snapshot",
                    "--policy-document", policy_doc,
                ],
                check=True,
            )
            key = run(
                [self._binary, "iam", "create-access-key", "--user-name", user, "--output", "json"],
                parse_json=True,
            )
            ak = (key.parsed or {}).get("AccessKey", {})
            access_id = ak["AccessKeyId"]
            secret = ak["SecretAccessKey"]
            body = (
                f'export AWS_ACCESS_KEY_ID={access_id!r}\n'
                f'export AWS_SECRET_ACCESS_KEY={secret!r}\n'
                f'export AWS_DEFAULT_REGION={(self._region or "")!r}\n'
            )
            return RemoteCredential(
                credential_id=user,
                mode="file_credential",
                files={f"/root/.pgforge/cred-{scope.instance_name}.env": body},
                last4=access_id[-4:],
            )
        except Exception as e:
            log.warning("IAM mint failed (%s); falling back to operator credentials", e)
            access_id = os.environ.get("AWS_ACCESS_KEY_ID", "")
            secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
            if not access_id or not secret:
                raise ProviderAPIError(
                    "could not mint scoped IAM user, and AWS_ACCESS_KEY_ID / "
                    "AWS_SECRET_ACCESS_KEY are not set. Set them or attach an "
                    "EC2 instance role with ec2:CreateSnapshot to the server."
                ) from e
            body = (
                f'export AWS_ACCESS_KEY_ID={access_id!r}\n'
                f'export AWS_SECRET_ACCESS_KEY={secret!r}\n'
                f'export AWS_DEFAULT_REGION={(self._region or "")!r}\n'
            )
            return RemoteCredential(
                credential_id=f"aws-fallback-{secrets.token_hex(4)}",
                mode="file_credential",
                files={f"/root/.pgforge/cred-{scope.instance_name}.env": body},
                last4=access_id[-4:],
            )

    def revoke_snapshot_credential(self, credential_id: str) -> None:
        if not credential_id.startswith("pgforge-"):
            log.info("nothing to revoke for credential id %s", credential_id)
            return
        # Delete access keys, inline policy, then the user. Best-effort.
        keys = run(
            [self._binary, "iam", "list-access-keys", "--user-name", credential_id, "--output", "json"],
            check=False, parse_json=True,
        )
        for ak in (keys.parsed or {}).get("AccessKeyMetadata", []):
            run(
                [self._binary, "iam", "delete-access-key", "--user-name", credential_id, "--access-key-id", ak["AccessKeyId"]],
                check=False,
            )
        run([self._binary, "iam", "delete-user-policy", "--user-name", credential_id, "--policy-name", "pgforge-snapshot"], check=False)
        run([self._binary, "iam", "delete-user", "--user-name", credential_id], check=False)

    # ---- internals ----

    def _volume_from_raw(self, raw: dict[str, Any]) -> Volume:
        labels: dict[str, str] = {}
        for tag in raw.get("Tags") or []:
            labels[tag["Key"]] = tag.get("Value", "")
        attached_server_id = None
        for att in raw.get("Attachments") or []:
            attached_server_id = att.get("InstanceId")
        return Volume(
            id=raw["VolumeId"],
            name=labels.get("Name", raw["VolumeId"]),
            size_gb=int(raw.get("Size", 0)),
            location=raw.get("AvailabilityZone", ""),
            attached_server_id=attached_server_id,
            labels=labels,
            raw=raw,
        )

    def _snapshot_from_raw(self, raw: dict[str, Any], volume_id: str) -> Snapshot:
        labels: dict[str, str] = {}
        for tag in raw.get("Tags") or []:
            labels[tag["Key"]] = tag.get("Value", "")
        return Snapshot(
            id=raw["SnapshotId"],
            name=labels.get("Name", raw.get("Description", "")),
            source_volume_id=raw.get("VolumeId", volume_id),
            size_gb=int(raw.get("VolumeSize", 0)),
            created_at=_parse_ts(raw.get("StartTime")),
            labels=labels,
            raw=raw,
        )

    def _wait_volume_state(
        self, volume_id: str, target: str, *, timeout: float = 180, missing_ok: bool = False
    ) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                vol = self.get_volume(volume_id)
            except ProviderResourceNotFound:
                if missing_ok:
                    return
                raise
            state = (vol.raw or {}).get("State", "")
            if state == target:
                return
            time.sleep(3)
        raise ProviderAPIError(f"volume {volume_id} did not reach state {target!r} within {timeout}s")


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)
