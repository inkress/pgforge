"""MockProvider contract test.

Doubles as the contract test other providers will inherit from in Phase 2.
"""

from __future__ import annotations

from datetime import timedelta

from pgforge.providers.base import SnapshotScope, TimeWindow, VolumeSpec
from pgforge.providers.mock import MockProvider


def test_volume_create_attach_delete():
    p = MockProvider()
    srv = p.get_server("srv-1")
    vol = p.create_volume(VolumeSpec(name="t1", size_gb=10, location="mock-1", server_id=srv.id))
    assert vol.id
    attached = p.attach_volume(vol.id, srv.id)
    assert attached.server_id == srv.id
    assert attached.device_path.startswith("/dev/disk/by-id/")

    # round-trip
    again = p.get_volume(vol.id)
    assert again.id == vol.id

    p.detach_volume(vol.id)
    p.delete_volume(vol.id)


def test_snapshot_lifecycle():
    p = MockProvider()
    srv = p.get_server("srv-1")
    vol = p.create_volume(VolumeSpec(name="snap-src", size_gb=10, location="mock-1", server_id=srv.id))
    snap = p.create_snapshot(vol.id, "pgforge-test", labels={"pgforge-instance": "t"})
    assert snap.source_volume_id == vol.id
    listed = p.list_snapshots(vol.id)
    assert snap.id in {s.id for s in listed}
    p.delete_snapshot(snap.id)
    assert p.list_snapshots(vol.id) == []


def test_metrics_has_unavailable_reasons():
    p = MockProvider()
    srv = p.get_server("srv-1")
    vol = p.create_volume(VolumeSpec(name="m", size_gb=10, location="mock-1", server_id=srv.id))
    metrics = p.volume_metrics(vol.id, TimeWindow.last(timedelta(hours=1)))
    assert metrics.iops_read is not None
    assert metrics.throughput_read_bps and metrics.throughput_read_bps.unavailable_reason


def test_mint_credential_returns_last4():
    p = MockProvider()
    srv = p.get_server("srv-1")
    vol = p.create_volume(VolumeSpec(name="c", size_gb=10, location="mock-1", server_id=srv.id))
    cred = p.mint_snapshot_credential(
        SnapshotScope(instance_name="c", volume_id=vol.id, server_id=srv.id, location="mock-1")
    )
    assert cred.mode == "file_credential"
    assert len(cred.last4) == 4
