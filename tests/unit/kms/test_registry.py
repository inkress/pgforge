"""KMS registry: all five backends are listed; cloud backends require config."""

from __future__ import annotations

import pytest

from pgforge.errors import ConfigError
from pgforge.kms.registry import get_backend, list_backends


def test_all_five_kms_implemented():
    backends = dict(list_backends())
    for name in ("local", "aws-kms", "gcp-kms", "azure-kv", "vault"):
        assert backends[name] is True


def test_aws_kms_requires_key_id(monkeypatch):
    monkeypatch.delenv("PGFORGE_AWS_KMS_KEY_ID", raising=False)
    monkeypatch.setattr("pgforge.providers._shell.require_binary", lambda _: "/usr/bin/fake")
    with pytest.raises(ConfigError):
        get_backend("aws-kms")


def test_gcp_kms_requires_key(monkeypatch):
    monkeypatch.delenv("PGFORGE_GCP_KMS_KEY", raising=False)
    monkeypatch.setattr("pgforge.providers._shell.require_binary", lambda _: "/usr/bin/fake")
    with pytest.raises(ConfigError):
        get_backend("gcp-kms")


def test_azure_kv_requires_vault_and_key(monkeypatch):
    monkeypatch.delenv("PGFORGE_AZURE_KV_VAULT", raising=False)
    monkeypatch.delenv("PGFORGE_AZURE_KV_KEY", raising=False)
    monkeypatch.setattr("pgforge.providers._shell.require_binary", lambda _: "/usr/bin/fake")
    with pytest.raises(ConfigError):
        get_backend("azure-kv")


def test_vault_requires_addr(monkeypatch):
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("PGFORGE_VAULT_MOUNT", raising=False)
    monkeypatch.delenv("PGFORGE_VAULT_KEY", raising=False)
    monkeypatch.setattr("pgforge.providers._shell.require_binary", lambda _: "/usr/bin/fake")
    with pytest.raises(ConfigError):
        get_backend("vault")
