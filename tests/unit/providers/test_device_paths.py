"""Per-provider device path conventions.

Each cloud names attached block devices differently. The Provider abstraction
hides this — these tests pin the actual strings so a regression on any of
them is immediately visible.
"""

from __future__ import annotations

import pytest

from pgforge.providers.base import Server, Volume


def _stub_volume(vid: str, name: str) -> Volume:
    return Volume(id=vid, name=name, size_gb=10, location="", attached_server_id=None)


def _stub_server() -> Server:
    return Server(id="srv-1", name="host", ipv4="203.0.113.10", ipv6=None, location="")


@pytest.fixture(autouse=True)
def _stub_cli_lookup(monkeypatch):
    """Pretend every provider CLI exists at /usr/bin/<name>, so the providers
    instantiate without trying to actually run a binary."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")


def test_hetzner_device_path():
    from pgforge.providers.hetzner import HetznerProvider

    p = HetznerProvider()
    assert (
        p.server_device_path(_stub_server(), _stub_volume("42", "vol42"))
        == "/dev/disk/by-id/scsi-0HC_Volume_42"
    )


def test_aws_nitro_device_path():
    from pgforge.providers.aws import AWSProvider

    p = AWSProvider()
    assert (
        p.server_device_path(_stub_server(), _stub_volume("vol-0123abcd", "ebs-name"))
        == "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol0123abcd"
    )


def test_gcp_device_path():
    from pgforge.providers.gcp import GCPProvider

    p = GCPProvider(project="my-proj", zone="us-central1-a")
    assert (
        p.server_device_path(_stub_server(), _stub_volume("disk-7", "my-disk"))
        == "/dev/disk/by-id/google-my-disk"
    )


def test_azure_device_path():
    from pgforge.providers.azure import AzureProvider

    p = AzureProvider(resource_group="rg", region="eastus2")
    vol = _stub_volume("dsk", "dsk-name")
    vol.raw = {"lun": 3}
    assert p.server_device_path(_stub_server(), vol) == "/dev/disk/azure/scsi1/lun3"


def test_digitalocean_device_path():
    from pgforge.providers.digitalocean import DigitalOceanProvider

    p = DigitalOceanProvider()
    assert (
        p.server_device_path(_stub_server(), _stub_volume("123", "my-volume"))
        == "/dev/disk/by-id/scsi-0DO_Volume_my-volume"
    )


def test_linode_device_path():
    from pgforge.providers.linode import LinodeProvider

    p = LinodeProvider()
    assert (
        p.server_device_path(_stub_server(), _stub_volume("1234", "vol-label"))
        == "/dev/disk/by-id/scsi-0Linode_Volume_vol-label"
    )
