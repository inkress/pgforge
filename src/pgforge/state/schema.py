"""Pydantic models for the on-disk state file.

The state file is forward-compatible: extra keys at the top level are tolerated
by Pydantic with ``extra="ignore"``, and ``schema_version`` is bumped only on
shape-breaking changes. See ``state/migrate.py`` for upgrade logic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pgforge.kms.base import UnlockMode

SCHEMA_VERSION = 1


class Phase(str, Enum):
    """Resumable provision/destroy phase. Forward-only progression."""

    PENDING = "pending"
    VOLUME_CREATED = "volume_created"
    VOLUME_ATTACHED = "volume_attached"
    LUKS_FORMATTED = "luks_formatted"
    LUKS_OPENED = "luks_opened"
    FILESYSTEM_CREATED = "filesystem_created"
    MOUNTED = "mounted"
    CRYPTTAB_WRITTEN = "crypttab_written"
    POSTGRES_RUNNING = "postgres_running"
    CRON_INSTALLED = "cron_installed"
    READY = "ready"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"

    @classmethod
    def order(cls) -> list["Phase"]:
        """Forward provision order (used by :func:`is_at_or_past`)."""
        return [
            cls.PENDING,
            cls.VOLUME_CREATED,
            cls.VOLUME_ATTACHED,
            cls.LUKS_FORMATTED,
            cls.LUKS_OPENED,
            cls.FILESYSTEM_CREATED,
            cls.MOUNTED,
            cls.CRYPTTAB_WRITTEN,
            cls.POSTGRES_RUNNING,
            cls.CRON_INSTALLED,
            cls.READY,
        ]

    def is_at_or_past(self, other: "Phase") -> bool:
        order = self.order()
        try:
            return order.index(self) >= order.index(other)
        except ValueError:
            # destroy phases aren't comparable to provision phases
            return False


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class ProviderResources(_Base):
    server_id: str
    server_name: str | None = None
    volume_id: str
    device_path: str
    location: str


class LUKSInfo(_Base):
    name: str = "pgdata"
    uuid: str | None = None
    cipher: str = "aes-xts-plain64"
    key_size: int = 512


class KMSRef(_Base):
    """Pointer into the KMS layer. Never contains plaintext key material."""

    backend: str
    """Backend name: ``local``, ``aws-kms``, ``gcp-kms``, ``azure-kv``, ``vault``."""

    key_id: str
    """Backend-specific identifier."""

    envelope_ciphertext_b64: str | None = None
    """For cloud-KMS envelope encryption: the KMS-encrypted DEK. Empty for ``local``."""

    unlock_mode: UnlockMode = UnlockMode.STATIC
    metadata: dict[str, Any] = Field(default_factory=dict)


class MountInfo(_Base):
    path: str = "/mnt/pg"
    filesystem: str = "ext4"


class PostgresInfo(_Base):
    container_name: str
    image: str = "postgres:16"
    port: int = 5432
    bind: str = "127.0.0.1"
    data_subdir: str = "data"


class SSHInfo(_Base):
    host: str
    port: int = 22
    user: str = "root"
    key_fingerprint: str | None = None


class SnapshotSchedule(_Base):
    enabled: bool = False
    cron: str | None = None
    retention: str | None = None
    """Retention string parsed by ``snapshot/policy.py`` (e.g., ``7d,4w,3m``)."""

    credential_id: str | None = None
    last_run: datetime | None = None
    installed_unit: str | None = None
    """Path to the cron file on the server, e.g. ``/etc/cron.d/pgforge-myapp``."""


class CredentialMeta(_Base):
    """What we remember about a server-side credential, never the secret itself."""

    credential_id: str
    last4: str
    created_at: datetime
    provider: str


class InstanceState(_Base):
    name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    provider: str
    provider_resources: ProviderResources
    luks: LUKSInfo = Field(default_factory=LUKSInfo)
    kms: KMSRef
    mount: MountInfo = Field(default_factory=MountInfo)
    postgres: PostgresInfo
    ssh: SSHInfo
    snapshot_schedule: SnapshotSchedule = Field(default_factory=SnapshotSchedule)
    credentials: dict[str, CredentialMeta] = Field(default_factory=dict)
    """Server-side credentials minted for this instance, keyed by credential_id."""

    labels: dict[str, str] = Field(default_factory=dict)
    phase: Phase = Phase.PENDING

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)


class StateFile(_Base):
    schema_version: int = SCHEMA_VERSION
    instances: dict[str, InstanceState] = Field(default_factory=dict)

    @classmethod
    def empty(cls) -> "StateFile":
        return cls()
