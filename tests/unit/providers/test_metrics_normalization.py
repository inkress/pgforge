"""Providers without rich metrics return ``unavailable_reason`` rather than lying."""

from __future__ import annotations

from datetime import timedelta

import pytest

from pgforge.providers.base import TimeWindow


def _window():
    return TimeWindow.last(timedelta(hours=1))


@pytest.fixture(autouse=True)
def _stub_cli_lookup(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")


def test_hetzner_metrics_all_unavailable():
    from pgforge.providers.hetzner import HetznerProvider

    m = HetznerProvider().volume_metrics("vol-1", _window())
    for series in (m.iops_read, m.iops_write, m.throughput_read_bps, m.throughput_write_bps, m.queue_depth, m.latency_ms):
        if series is None:
            continue
        assert series.unavailable_reason


def test_digitalocean_metrics_unavailable_with_hint():
    from pgforge.providers.digitalocean import DigitalOceanProvider

    m = DigitalOceanProvider().volume_metrics("100", _window())
    assert m.iops_read and "doctl" in (m.iops_read.unavailable_reason or "")


def test_linode_metrics_unavailable_with_hint():
    from pgforge.providers.linode import LinodeProvider

    m = LinodeProvider().volume_metrics("1", _window())
    assert m.iops_read and "linode-cli" in (m.iops_read.unavailable_reason or "")
