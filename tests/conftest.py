"""Shared pytest fixtures."""

from __future__ import annotations

import os

import pytest

from pgforge.config.paths import Paths, get_paths


@pytest.fixture
def isolated_home(tmp_path, monkeypatch) -> Paths:
    """Point PGFORGE_HOME at a fresh temp dir for the duration of the test.

    Ensures unit tests never touch the real ``~/.config/pgforge``.
    """
    monkeypatch.setenv("PGFORGE_HOME", str(tmp_path))
    monkeypatch.delenv("PGFORGE_STATE_FILE", raising=False)
    return get_paths()


@pytest.fixture
def state_store(isolated_home):
    from pgforge.state.store import StateStore

    return StateStore(isolated_home)
