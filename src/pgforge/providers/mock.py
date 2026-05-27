"""Mock provider — in-memory for unit tests, optional file-backed for CLI smoke.

Register with::

    from pgforge.providers.mock import MockProvider, register_mock
    register_mock()

By default the world is purely in-memory. If the environment variable
``PGFORGE_MOCK_STATE`` is set to a path, the mock persists its world to JSON
there so subsequent CLI invocations see the same volumes/snapshots — useful
for end-to-end CLI smoke tests without a real cloud account.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

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
from pgforge.providers.registry import register


class MockProvider(Provider):
    name: ClassVar[str] = "mock"
    cli_binary: ClassVar[str] = "true"
    min_cli_version: ClassVar[str] = "0.0.0"

    def __init__(self) -> None:
        self._servers: dict[str, Server] = {
            "srv-1": Server(
                id="srv-1", name="mock-host-1", ipv4="203.0.113.10", ipv6=None, location="mock-1"
            ),
        }
        self._volumes: dict[str, Volume] = {}
        self._snapshots: dict[str, Snapshot] = {}
        self._next_id = 1
        self._state_path: Path | None = None
        path = os.environ.get("PGFORGE_MOCK_STATE")
        if path:
            self._state_path = Path(path)
            self._load_persistent()

    # ---- persistence ----

    def _load_persistent(self) -> None:
        if self._state_path is None or not self._state_path.is_file():
            return
        raw = json.loads(self._state_path.read_text())
        self._next_id = raw.get("next_id", 1)
        for v in raw.get("volumes", []):
            self._volumes[v["id"]] = Volume(
                id=v["id"], name=v["name"], size_gb=v["size_gb"],
                location=v["location"], attached_server_id=v.get("attached_server_id"),
                labels=v.get("labels", {}), raw=v.get("raw", {}),
            )
        for s in raw.get("snapshots", []):
            self._snapshots[s["id"]] = Snapshot(
                id=s["id"], name=s["name"], source_volume_id=s["source_volume_id"],
                size_gb=s["size_gb"],
                created_at=datetime.fromisoformat(s["created_at"]),
                labels=s.get("labels", {}), raw=s.get("raw", {}),
            )

    def _save_persistent(self) -> None:
        if self._state_path is None:
            return
        payload = {
            "next_id": self._next_id,
            "volumes": [asdict(v) for v in self._volumes.values()],
            "snapshots": [
                {**asdict(s), "created_at": s.created_at.isoformat()}
                for s in self._snapshots.values()
            ],
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(payload, default=str, indent=2))

    def _new_id(self, prefix: str) -> str:
        self._next_id += 1
        new = f"{prefix}-{self._next_id}"
        self._save_persistent()
        return new

    # ---- preflight ----

    def check_cli(self) -> CLIInfo:
        return CLIInfo(binary=self.cli_binary, version="mock", authenticated=True, account="mock-account")

    def list_locations(self) -> list[Location]:
        return [Location(name="mock-1", country="ZZ")]

    # ---- server ----

    def get_server(self, server_id_or_name: str) -> Server:
        from pgforge.errors import ProviderResourceNotFound

        for srv in self._servers.values():
            if server_id_or_name in (srv.id, srv.name):
                return srv
        raise ProviderResourceNotFound(f"mock: no server {server_id_or_name!r}")

    def server_device_path(self, server: Server, volume: Volume) -> str:
        return f"/dev/disk/by-id/mock-volume-{volume.id}"

    # ---- volumes ----

    def create_volume(self, spec: VolumeSpec) -> Volume:
        vol = Volume(
            id=self._new_id("vol"),
            name=spec.name,
            size_gb=spec.size_gb,
            location=spec.location,
            attached_server_id=spec.server_id,
            labels=dict(spec.labels),
            raw={"mock": True},
        )
        self._volumes[vol.id] = vol
        self._save_persistent()
        return vol

    def attach_volume(self, volume_id: str, server_id: str) -> AttachedVolume:
        vol = self.get_volume(volume_id)
        vol.attached_server_id = server_id
        srv = self.get_server(server_id)
        return AttachedVolume(volume=vol, server_id=server_id, device_path=self.server_device_path(srv, vol))

    def detach_volume(self, volume_id: str) -> None:
        if volume_id in self._volumes:
            self._volumes[volume_id].attached_server_id = None

    def delete_volume(self, volume_id: str) -> None:
        self._volumes.pop(volume_id, None)
        self._save_persistent()

    def get_volume(self, volume_id: str) -> Volume:
        from pgforge.errors import ProviderResourceNotFound

        if volume_id not in self._volumes:
            raise ProviderResourceNotFound(f"mock: no volume {volume_id!r}")
        return self._volumes[volume_id]

    def list_volumes(self) -> list[Volume]:
        return list(self._volumes.values())

    # ---- snapshots ----

    def create_snapshot(self, volume_id: str, name: str, labels: dict[str, str] | None = None) -> Snapshot:
        size_gb = self._volumes[volume_id].size_gb if volume_id in self._volumes else 0
        snap = Snapshot(
            id=self._new_id("snap"),
            name=name,
            source_volume_id=volume_id,
            size_gb=size_gb,
            created_at=datetime.now(timezone.utc),
            labels=dict(labels or {}),
            raw={"mock": True},
        )
        self._snapshots[snap.id] = snap
        self._save_persistent()
        return snap

    def list_snapshots(self, volume_id: str | None = None) -> list[Snapshot]:
        return [s for s in self._snapshots.values() if volume_id is None or s.source_volume_id == volume_id]

    def delete_snapshot(self, snapshot_id: str) -> None:
        self._snapshots.pop(snapshot_id, None)
        self._save_persistent()

    def restore_snapshot(self, snapshot_id: str, target_spec: VolumeSpec) -> Volume:
        src = self._snapshots[snapshot_id]
        spec = VolumeSpec(name=target_spec.name, size_gb=src.size_gb, location=target_spec.location)
        return self.create_volume(spec)

    # ---- metrics & capacity ----

    def volume_metrics(self, volume_id: str, window: TimeWindow) -> ProviderMetrics:
        reason = "mock provider returns synthetic series"
        return ProviderMetrics(
            volume_id=volume_id,
            window=window,
            iops_read=MetricSeries(points=[(window.end, 42.0)], unit="ops/s"),
            iops_write=MetricSeries(points=[(window.end, 17.0)], unit="ops/s"),
            throughput_read_bps=MetricSeries(points=[], unit="bytes/s", unavailable_reason=reason),
            throughput_write_bps=MetricSeries(points=[], unit="bytes/s", unavailable_reason=reason),
            queue_depth=None,
            latency_ms=MetricSeries(points=[(window.end, 1.2)], unit="ms"),
        )

    def volume_capacity(self, volume_id: str) -> CapacityInfo:
        vol = self.get_volume(volume_id)
        return CapacityInfo(
            volume_id=volume_id,
            provider_reported_size_bytes=vol.size_gb * 1024 * 1024 * 1024,
        )

    # ---- snapshot cron credentials ----

    def mint_snapshot_credential(self, scope: SnapshotScope) -> RemoteCredential:
        token = secrets.token_hex(8)
        return RemoteCredential(
            credential_id=f"mock-{scope.instance_name}-{int(time.time())}",
            mode="file_credential",
            files={f"/root/.pgforge/cred-{scope.instance_name}.env": f"MOCK_TOKEN={token}\n"},
            last4=token[-4:],
        )

    def revoke_snapshot_credential(self, credential_id: str) -> None:
        pass


def register_mock() -> None:
    register("mock", MockProvider)
