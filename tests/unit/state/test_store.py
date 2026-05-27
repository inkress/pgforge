"""State store: round-trip, atomic write, lock semantics, concurrency."""

from __future__ import annotations

import json
import threading

import pytest

from pgforge.errors import StateConflict, StateLocked, StateNotFound
from pgforge.state.schema import (
    InstanceState,
    KMSRef,
    Phase,
    PostgresInfo,
    ProviderResources,
    SSHInfo,
)
from pgforge.state.store import instance_lock


def _make_instance(name: str = "demo") -> InstanceState:
    return InstanceState(
        name=name,
        provider="mock",
        provider_resources=ProviderResources(
            server_id="srv-1",
            server_name="mock-host-1",
            volume_id="vol-7",
            device_path="/dev/disk/by-id/mock-volume-7",
            location="mock-1",
        ),
        kms=KMSRef(backend="local", key_id="/tmp/key"),
        postgres=PostgresInfo(container_name=f"pg-{name}"),
        ssh=SSHInfo(host="203.0.113.10"),
        phase=Phase.READY,
    )


def test_round_trip(state_store):
    state_store.add_instance(_make_instance("a"))
    got = state_store.get_instance("a")
    assert got.name == "a"
    assert got.provider == "mock"
    assert got.phase == Phase.READY


def test_duplicate_name_rejected(state_store):
    state_store.add_instance(_make_instance("a"))
    with pytest.raises(StateConflict):
        state_store.add_instance(_make_instance("a"))


def test_missing_name_raises(state_store):
    with pytest.raises(StateNotFound):
        state_store.get_instance("nope")


def test_state_file_is_valid_json(state_store):
    state_store.add_instance(_make_instance("a"))
    raw = json.loads(state_store.paths.state_file.read_text())
    assert raw["schema_version"] == 1
    assert "a" in raw["instances"]


def test_atomic_write_keeps_old_on_crash(state_store, monkeypatch):
    """Simulating a crash mid-write: temp file shouldn't replace good file."""
    state_store.add_instance(_make_instance("a"))
    good = state_store.paths.state_file.read_text()
    # Force os.replace to fail.
    import os

    def boom(*args, **kwargs):
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        state_store.add_instance(_make_instance("b"))
    # Original file untouched.
    assert state_store.paths.state_file.read_text() == good


def test_instance_lock_blocks_re_entry(isolated_home):
    with instance_lock("foo", isolated_home):
        with pytest.raises(StateLocked):
            with instance_lock("foo", isolated_home):
                pass


def test_instance_lock_allows_different_names(isolated_home):
    with instance_lock("a", isolated_home):
        with instance_lock("b", isolated_home):
            pass


def test_concurrent_writes_serialize(state_store):
    """Two threads each adding a different instance must both succeed."""
    errors: list[Exception] = []

    def worker(name: str) -> None:
        try:
            state_store.add_instance(_make_instance(name))
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=worker, args=("x",))
    t2 = threading.Thread(target=worker, args=("y",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors, errors
    names = {i.name for i in state_store.list_instances()}
    assert names == {"x", "y"}
