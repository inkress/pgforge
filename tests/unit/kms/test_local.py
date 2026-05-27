"""LocalKMS: generate/fetch/delete/rotate round-trip."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pgforge.errors import KMSKeyNotFound
from pgforge.kms.base import KeySpec, UnlockMode
from pgforge.kms.local import LocalKMS


def test_generate_and_fetch_round_trip(isolated_home):
    kms = LocalKMS(isolated_home)
    handle = kms.generate_key(KeySpec(label="round-trip", size_bytes=64))
    assert handle.backend == "local"
    assert handle.unlock_mode == UnlockMode.STATIC
    material = kms.fetch_material(handle)
    assert len(material) == 64
    # File permissions are restrictive.
    mode = os.stat(handle.key_id).st_mode & 0o777
    assert mode == 0o600


def test_delete_removes_file(isolated_home):
    kms = LocalKMS(isolated_home)
    h = kms.generate_key(KeySpec(label="goodbye", size_bytes=32))
    assert Path(h.key_id).is_file()
    kms.delete(h)
    assert not Path(h.key_id).is_file()


def test_fetch_missing_raises(isolated_home):
    kms = LocalKMS(isolated_home)
    h = kms.generate_key(KeySpec(label="vanish", size_bytes=16))
    os.remove(h.key_id)
    with pytest.raises(KMSKeyNotFound):
        kms.fetch_material(h)


def test_rotate_creates_distinct_handle(isolated_home):
    kms = LocalKMS(isolated_home)
    h1 = kms.generate_key(KeySpec(label="rot", size_bytes=64))
    h2 = kms.rotate(h1)
    assert h1.key_id != h2.key_id
    assert kms.fetch_material(h1) != kms.fetch_material(h2)


def test_runtime_mode_unsupported(isolated_home):
    kms = LocalKMS(isolated_home)
    with pytest.raises(NotImplementedError):
        kms.generate_key(KeySpec(label="rt", size_bytes=32, unlock_mode=UnlockMode.RUNTIME))
