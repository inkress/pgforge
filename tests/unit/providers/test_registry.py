"""Provider registry: all six clouds + mock register, instantiate, and conform."""

from __future__ import annotations

import pytest

from pgforge.providers.registry import get_provider, iter_implemented, list_providers


def test_all_six_clouds_plus_mock_implemented():
    implemented = set(iter_implemented())
    assert {"hetzner", "aws", "gcp", "azure", "digitalocean", "linode", "mock"} <= implemented


def test_list_providers_returns_roadmap_status():
    rows = dict(list_providers())
    for name in ("hetzner", "aws", "gcp", "azure", "digitalocean", "linode", "mock"):
        assert rows[name] is True


def test_unknown_provider_raises():
    with pytest.raises(KeyError):
        get_provider("not-a-cloud")


@pytest.mark.parametrize(
    "name",
    ["hetzner", "aws", "gcp", "azure", "digitalocean", "linode"],
)
def test_provider_raises_cli_missing_when_binary_absent(name, monkeypatch):
    """Each provider should raise ProviderCLIMissing when its binary isn't on PATH."""
    import shutil

    from pgforge.errors import ProviderCLIMissing

    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ProviderCLIMissing):
        get_provider(name)
