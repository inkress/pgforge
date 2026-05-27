"""Collect filesystem + Postgres usage over SSH (provider-independent half of capacity)."""

from __future__ import annotations

import re
import shlex
from datetime import datetime, timezone

from pgforge.logging import get_logger
from pgforge.metrics.schema import FilesystemUsage, PostgresUsage
from pgforge.remote.ssh import RemoteHost

log = get_logger(__name__)


def collect_filesystem(host: RemoteHost, mount_point: str) -> FilesystemUsage:
    """Run ``df`` and ``df -i`` on the server to populate FS usage."""
    rc, out, _ = host.run(
        f"df -B1 --output=size,used,avail {shlex.quote(mount_point)}",
        check=False,
        log_command=False,
    )
    rc2, out_i, _ = host.run(
        f"df -i --output=itotal,iused {shlex.quote(mount_point)}",
        check=False,
        log_command=False,
    )
    usage = FilesystemUsage()
    if rc == 0:
        for line in out.splitlines():
            nums = re.findall(r"\d+", line)
            if len(nums) >= 3:
                usage.total_bytes = int(nums[0])
                usage.used_bytes = int(nums[1])
                usage.free_bytes = int(nums[2])
                break
    if rc2 == 0:
        for line in out_i.splitlines():
            nums = re.findall(r"\d+", line)
            if len(nums) >= 2:
                usage.inodes_total = int(nums[0])
                usage.inodes_used = int(nums[1])
                break
    return usage


def collect_postgres(host: RemoteHost, container_name: str) -> PostgresUsage:
    """Query inside the postgres container for DB + WAL sizes."""
    usage = PostgresUsage()
    rc, out, _ = host.run(
        f"docker exec {shlex.quote(container_name)} psql -U postgres -tAc "
        f"\"SELECT COALESCE(SUM(pg_database_size(datname)),0) FROM pg_database;\"",
        check=False,
        log_command=False,
    )
    if rc == 0 and out.strip().isdigit():
        usage.database_size_bytes = int(out.strip())
    rc, out, _ = host.run(
        f"docker exec {shlex.quote(container_name)} bash -c "
        f"\"du -sb /var/lib/postgresql/data/pgdata/pg_wal 2>/dev/null | awk '{{print \\$1}}'\"",
        check=False,
        log_command=False,
    )
    if rc == 0 and out.strip().isdigit():
        usage.wal_size_bytes = int(out.strip())
    return usage


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
