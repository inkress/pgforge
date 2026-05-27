"""KMS backend registry."""

from __future__ import annotations

from collections.abc import Callable

from pgforge.errors import ConfigError
from pgforge.kms.base import KMSBackend

_LAZY: dict[str, str] = {
    "local": "pgforge.kms.local:LocalKMS",
    "aws-kms": "pgforge.kms.aws_kms:AWSKMS",
    "gcp-kms": "pgforge.kms.gcp_kms:GCPKMS",
    "azure-kv": "pgforge.kms.azure_kv:AzureKeyVault",
    "vault": "pgforge.kms.vault:VaultKMS",
}

# Backends implemented as of this phase.
_IMPLEMENTED: set[str] = {"local", "aws-kms", "gcp-kms", "azure-kv", "vault"}

_REGISTRY: dict[str, Callable[..., KMSBackend]] = {}


def register(name: str, factory: Callable[..., KMSBackend]) -> None:
    _REGISTRY[name] = factory
    _IMPLEMENTED.add(name)


def list_backends() -> list[tuple[str, bool]]:
    names = sorted(set(_LAZY) | set(_REGISTRY) | _IMPLEMENTED)
    return [(n, n in _IMPLEMENTED) for n in names]


def get_backend(name: str, **kwargs) -> KMSBackend:
    if name in _REGISTRY:
        return _REGISTRY[name](**kwargs)
    if name not in _LAZY:
        raise KeyError(f"unknown KMS backend: {name!r}")
    if name not in _IMPLEMENTED:
        raise ConfigError(
            f"KMS backend {name!r} is on the roadmap but not yet implemented. "
            f"Supported: {', '.join(sorted(_IMPLEMENTED))}."
        )
    target = _LAZY[name]
    module, attr = target.split(":")
    import importlib

    cls = getattr(importlib.import_module(module), attr)
    return cls(**kwargs)
