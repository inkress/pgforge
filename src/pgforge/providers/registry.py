"""Provider registry.

Implementations register themselves at import time so the CLI can resolve
``--provider hetzner`` to a class without hard-coding the import map. Each
provider module is imported on demand to keep ``pgforge --help`` fast.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from pgforge.errors import ProviderUnsupported
from pgforge.providers.base import Provider

# Lazy: maps name -> import string. Avoids importing all providers (and their
# CLI version probes) on every invocation.
_LAZY: dict[str, str] = {
    "hetzner": "pgforge.providers.hetzner:HetznerProvider",
    "aws": "pgforge.providers.aws:AWSProvider",
    "gcp": "pgforge.providers.gcp:GCPProvider",
    "azure": "pgforge.providers.azure:AzureProvider",
    "digitalocean": "pgforge.providers.digitalocean:DigitalOceanProvider",
    "linode": "pgforge.providers.linode:LinodeProvider",
    "mock": "pgforge.providers.mock:MockProvider",
}

# Implemented as of this phase. Entries not in here raise ProviderUnsupported
# at instantiation time with a clear roadmap message.
_IMPLEMENTED: set[str] = {
    "hetzner",
    "aws",
    "gcp",
    "azure",
    "digitalocean",
    "linode",
    "mock",
}

# Eager registry (override _LAZY for testing / for the mock provider).
_REGISTRY: dict[str, Callable[[], Provider]] = {}


def register(name: str, factory: Callable[[], Provider]) -> None:
    """Register a provider factory under ``name``."""
    _REGISTRY[name] = factory
    _IMPLEMENTED.add(name)


def list_providers() -> list[tuple[str, bool]]:
    """Return ``[(name, implemented), ...]`` for every known provider."""
    names = sorted(set(_LAZY) | set(_REGISTRY) | _IMPLEMENTED)
    return [(n, n in _IMPLEMENTED) for n in names]


def get_provider(name: str) -> Provider:
    """Instantiate the provider named ``name``.

    Raises :class:`ProviderUnsupported` for known-but-not-yet-implemented
    providers, and ``KeyError`` for unknown names.
    """
    if name in _REGISTRY:
        return _REGISTRY[name]()
    if name not in _LAZY:
        raise KeyError(f"unknown provider: {name!r}")
    if name not in _IMPLEMENTED:
        raise ProviderUnsupported(
            f"{name!r} is on the roadmap but not yet implemented. "
            f"Currently supported: {', '.join(sorted(_IMPLEMENTED))}."
        )
    target = _LAZY[name]
    module, attr = target.split(":")
    import importlib

    cls = getattr(importlib.import_module(module), attr)
    instance = cls()
    return instance


def iter_implemented() -> Iterator[str]:
    return iter(sorted(_IMPLEMENTED))
