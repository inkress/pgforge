"""Cloud-provider abstraction layer.

Concrete providers shell out to the provider's official CLI (``hcloud``,
``aws``, ``gcloud``, ``az``, ``doctl``, ``linode-cli``) — pgforge never
imports a cloud SDK directly. See ``providers/base.py`` for the contract.
"""

from pgforge.providers.base import (
    AttachedVolume,
    CapacityInfo,
    CLIInfo,
    Location,
    Provider,
    ProviderMetrics,
    RemoteCredential,
    Server,
    Snapshot,
    SnapshotScope,
    SSHEndpoint,
    TimeWindow,
    Volume,
    VolumeSpec,
)
from pgforge.providers.registry import get_provider, list_providers, register

__all__ = [
    "AttachedVolume",
    "CLIInfo",
    "CapacityInfo",
    "Location",
    "Provider",
    "ProviderMetrics",
    "RemoteCredential",
    "SSHEndpoint",
    "Server",
    "Snapshot",
    "SnapshotScope",
    "TimeWindow",
    "Volume",
    "VolumeSpec",
    "get_provider",
    "list_providers",
    "register",
]
