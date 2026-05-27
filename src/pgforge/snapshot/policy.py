"""Retention policy parsing and grandfathered pruning.

A retention string is a comma-separated list of buckets::

    7d,4w,3m,2y

means: keep one snapshot per day for the last 7 days, plus one per week for
the last 4 weeks, plus one per month for the last 3 months, plus one per
year for the last 2 years. Snapshots that don't satisfy any bucket are
pruned.

The classic GFS scheme. ``parse_retention`` builds the policy;
``prune_with_policy`` applies it to a list of snapshots and returns the IDs
to delete.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from pgforge.errors import ConfigError

_BUCKET = re.compile(r"^\s*(\d+)\s*([hdwmy])\s*$", re.IGNORECASE)


@dataclass
class RetentionPolicy:
    hourly: int = 0
    daily: int = 0
    weekly: int = 0
    monthly: int = 0
    yearly: int = 0
    minimum_keep: int = 1
    """Refuse to prune below this many snapshots no matter what the policy says.
    Guards against accidental wipeouts from a misconfigured cron."""

    raw: str = ""

    def total_buckets(self) -> int:
        return self.hourly + self.daily + self.weekly + self.monthly + self.yearly


def parse_retention(text: str) -> RetentionPolicy:
    """Parse ``"7d,4w,3m"`` -> RetentionPolicy. Empty or whitespace -> no-op policy."""
    text = (text or "").strip()
    if not text:
        return RetentionPolicy(raw="")
    policy = RetentionPolicy(raw=text)
    for chunk in text.split(","):
        m = _BUCKET.match(chunk)
        if not m:
            raise ConfigError(
                f"invalid retention bucket {chunk!r}. Expected forms like "
                f"'24h', '7d', '4w', '3m', '2y'."
            )
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit == "h":
            policy.hourly = n
        elif unit == "d":
            policy.daily = n
        elif unit == "w":
            policy.weekly = n
        elif unit == "m":
            policy.monthly = n
        elif unit == "y":
            policy.yearly = n
    return policy


@dataclass
class _Snap:
    id: str
    ts: datetime


def prune_with_policy(
    snapshots: Iterable[tuple[str, datetime]],
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Decide which snapshot IDs to delete under ``policy``.

    The bucket assignment uses calendar boundaries (UTC). For each bucket
    (hour/day/week/month/year), we keep the *newest* snapshot whose timestamp
    falls inside the bucket window. Anything not picked by any bucket is
    pruned, but never below ``policy.minimum_keep``.
    """
    snaps = sorted(
        (_Snap(id=sid, ts=_to_utc(ts)) for sid, ts in snapshots),
        key=lambda s: s.ts,
        reverse=True,
    )
    if not snaps or policy.total_buckets() == 0:
        return []
    now = _to_utc(now or datetime.now(timezone.utc))

    keep: set[str] = set()
    keep |= _keep_per_window(snaps, now, count=policy.hourly, step=timedelta(hours=1))
    keep |= _keep_per_window(snaps, now, count=policy.daily, step=timedelta(days=1))
    keep |= _keep_per_window(snaps, now, count=policy.weekly, step=timedelta(weeks=1))
    keep |= _keep_per_window(snaps, now, count=policy.monthly, step=timedelta(days=30))
    keep |= _keep_per_window(snaps, now, count=policy.yearly, step=timedelta(days=365))

    pruned: list[str] = [s.id for s in snaps if s.id not in keep]

    # Floor at minimum_keep.
    if len(snaps) - len(pruned) < policy.minimum_keep:
        # walk from newest and add to keep until floor reached
        for s in snaps:
            if s.id not in keep:
                keep.add(s.id)
                if len(snaps) - sum(1 for sid in pruned if sid not in keep) >= policy.minimum_keep:
                    break
        pruned = [s.id for s in snaps if s.id not in keep]

    return pruned


def _keep_per_window(
    snaps: list[_Snap], now: datetime, *, count: int, step: timedelta
) -> set[str]:
    if count <= 0 or not snaps:
        return set()
    kept: set[str] = set()
    # For each of the last ``count`` windows, pick the newest snapshot inside.
    for w in range(count):
        end = now - step * w
        start = end - step
        candidate: _Snap | None = None
        for s in snaps:
            if start <= s.ts <= end and (candidate is None or s.ts > candidate.ts):
                candidate = s
        if candidate is not None:
            kept.add(candidate.id)
    return kept


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
