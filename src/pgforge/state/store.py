"""File-locked JSON store for instance state.

Two locks live here:

1. **State-file lock** (``state.json.lock`` sibling, ``fcntl.flock``): held
   only during read-modify-write of the state file itself. Brief.

2. **Per-instance lock** (``~/.config/pgforge/locks/<name>.lock``): held for
   the duration of a long-running operation (provision/destroy/snapshot).
   Prevents two operators from racing on the same instance.

All writes are atomic: write to ``state.json.tmp`` and ``os.replace`` onto
``state.json``. A crash mid-write leaves the prior good copy in place.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import portalocker

from pgforge.config.paths import Paths, ensure_dirs, get_paths
from pgforge.errors import StateConflict, StateError, StateLocked, StateNotFound
from pgforge.logging import get_logger
from pgforge.state.migrate import migrate
from pgforge.state.schema import InstanceState, StateFile

log = get_logger(__name__)


class StateStore:
    """Thin wrapper around the on-disk state file.

    Use as a short-lived object per command. The ``transaction()`` context
    manager is the only safe way to mutate state — it holds the file lock,
    re-reads the latest content, hands you a :class:`StateFile`, and writes
    atomically on exit.
    """

    def __init__(self, paths: Paths | None = None):
        self.paths = paths or get_paths()
        self._lock_file = self.paths.state_file.with_suffix(
            self.paths.state_file.suffix + ".lock"
        )

    # ---- read ----

    def read(self) -> StateFile:
        """Read the state file. Returns an empty :class:`StateFile` if missing."""
        if not self.paths.state_file.is_file():
            return StateFile.empty()
        try:
            raw = json.loads(self.paths.state_file.read_text())
        except json.JSONDecodeError as e:
            raise StateError(f"state file is not valid JSON: {e}") from e
        raw = migrate(raw)
        return StateFile.model_validate(raw)

    def get_instance(self, name: str) -> InstanceState:
        state = self.read()
        if name not in state.instances:
            raise StateNotFound(f"unknown instance: {name!r}")
        return state.instances[name]

    def list_instances(self) -> list[InstanceState]:
        return list(self.read().instances.values())

    # ---- write (always inside transaction) ----

    @contextlib.contextmanager
    def transaction(self) -> Iterator[StateFile]:
        """Acquire the file lock, yield a fresh :class:`StateFile`, write on exit.

        The caller mutates ``state.instances[...]`` and we serialize on exit.
        Atomic via ``os.replace``.
        """
        ensure_dirs(self.paths)
        with self._file_lock() as _:
            state = self.read()
            yield state
            self._write_atomic(state)

    def add_instance(self, instance: InstanceState) -> None:
        """Convenience: add a fresh instance, error if the name exists."""
        with self.transaction() as state:
            if instance.name in state.instances:
                raise StateConflict(f"instance {instance.name!r} already exists")
            state.instances[instance.name] = instance

    def update_instance(self, instance: InstanceState) -> None:
        """Convenience: replace an existing instance's record."""
        with self.transaction() as state:
            if instance.name not in state.instances:
                raise StateNotFound(f"unknown instance: {instance.name!r}")
            instance.touch()
            state.instances[instance.name] = instance

    def delete_instance(self, name: str) -> None:
        with self.transaction() as state:
            if name not in state.instances:
                raise StateNotFound(f"unknown instance: {name!r}")
            del state.instances[name]

    # ---- internals ----

    @contextlib.contextmanager
    def _file_lock(self) -> Iterator[IO[bytes]]:
        ensure_dirs(self.paths)
        with open(self._lock_file, "ab+") as fh:
            # Try non-blocking first so we can give a fast, clear error if
            # another pgforge is already mutating the file. If that fails we
            # fall through to a blocking acquire — the contender will usually
            # release within milliseconds.
            try:
                portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
            except portalocker.exceptions.LockException:
                portalocker.lock(fh, portalocker.LOCK_EX)
            try:
                yield fh
            finally:
                portalocker.unlock(fh)

    def _write_atomic(self, state: StateFile) -> None:
        ensure_dirs(self.paths)
        payload = state.model_dump_json(indent=2, exclude_none=False)
        tmp = self.paths.state_file.with_suffix(self.paths.state_file.suffix + ".tmp")
        tmp.write_text(payload)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.paths.state_file)


# ---- per-instance lock ---------------------------------------------------

@contextlib.contextmanager
def instance_lock(
    name: str,
    paths: Paths | None = None,
    *,
    timeout: float = 0,
) -> Iterator[Path]:
    """Hold an exclusive advisory lock on ``locks/<name>.lock`` for the duration
    of a long operation.

    ``timeout=0`` is non-blocking: raises :class:`StateLocked` immediately if
    another pgforge process holds the lock.
    """
    paths = paths or get_paths()
    ensure_dirs(paths)
    lock_path = paths.locks_dir / f"{name}.lock"
    with open(lock_path, "ab+") as fh:
        try:
            if timeout > 0:
                # Crude busy-wait loop because portalocker.lock() doesn't
                # accept a timeout argument in all backends.
                import time
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
                        break
                    except portalocker.exceptions.LockException:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.25)
            else:
                portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.exceptions.LockException as e:
            raise StateLocked(
                f"instance {name!r} is busy (lock held: {lock_path}). "
                f"Wait or pass --force-unlock once you're sure no other pgforge is running."
            ) from e
        # Write our pid so the user can see who's holding it.
        try:
            fh.truncate(0)
            fh.write(str(os.getpid()).encode())
            fh.flush()
        except OSError:
            pass
        try:
            yield lock_path
        finally:
            portalocker.unlock(fh)
