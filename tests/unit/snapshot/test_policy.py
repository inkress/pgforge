"""Retention policy parsing + pruning."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pgforge.errors import ConfigError
from pgforge.snapshot.policy import parse_retention, prune_with_policy


def test_parse_basic():
    p = parse_retention("7d,4w,3m")
    assert p.daily == 7
    assert p.weekly == 4
    assert p.monthly == 3
    assert p.yearly == 0
    assert p.hourly == 0


def test_parse_empty():
    p = parse_retention("")
    assert p.total_buckets() == 0


def test_parse_rejects_garbage():
    with pytest.raises(ConfigError):
        parse_retention("7q")
    with pytest.raises(ConfigError):
        parse_retention("not even close")


def test_prune_keeps_at_least_one():
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    snaps = [(f"s-{i}", now - timedelta(days=i)) for i in range(30)]
    p = parse_retention("7d")
    pruned = prune_with_policy(snaps, p, now=now)
    # Should keep ~7 + minimum_keep floor; not all of them.
    assert len(pruned) > 0
    assert len(snaps) - len(pruned) >= 1


def test_prune_nothing_when_no_policy():
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    snaps = [(f"s-{i}", now - timedelta(days=i)) for i in range(5)]
    p = parse_retention("")
    pruned = prune_with_policy(snaps, p, now=now)
    assert pruned == []


def test_prune_keeps_newest_in_each_bucket():
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    snaps = [
        ("today-am", now - timedelta(hours=6)),
        ("today-pm", now - timedelta(hours=1)),
        ("yesterday", now - timedelta(days=1, hours=2)),
        ("two-days", now - timedelta(days=2, hours=2)),
    ]
    p = parse_retention("3d")
    pruned = prune_with_policy(snaps, p, now=now)
    kept = {sid for sid, _ in snaps} - set(pruned)
    # We should keep the newest in each day-window.
    assert "today-pm" in kept
    assert "yesterday" in kept
    assert "two-days" in kept
