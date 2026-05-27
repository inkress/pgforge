"""Normalized output dataclasses for ``pgforge metrics`` and ``pgforge capacity``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FilesystemUsage:
    total_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    inodes_total: int | None = None
    inodes_used: int | None = None


@dataclass
class PostgresUsage:
    database_size_bytes: int | None = None
    wal_size_bytes: int | None = None


@dataclass
class CapacityReport:
    """Result of ``pgforge capacity <instance>``."""

    instance: str
    provider: str
    volume_id: str
    provider_reported_size_bytes: int | None
    filesystem: FilesystemUsage = field(default_factory=FilesystemUsage)
    postgres: PostgresUsage = field(default_factory=PostgresUsage)
    collected_at: datetime | None = None
    notes: list[str] = field(default_factory=list)
    """Free-form notes shown after the table (e.g., 'Hetzner does not expose IOPS')."""
