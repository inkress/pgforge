"""XDG-aware path resolution.

Everything pgforge writes lives under a single base dir, by default
``$XDG_CONFIG_HOME/pgforge`` (or ``~/.config/pgforge`` if XDG is unset).
Override with ``PGFORGE_HOME`` for tests or alternate installs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    base: Path
    """Root directory for all pgforge state on the operator machine."""

    state_file: Path
    """JSON state file."""

    keys_dir: Path
    """Local KMS backend stores keyfiles here, mode 0700."""

    locks_dir: Path
    """Per-instance lock files for serializing long ops."""

    config_file: Path
    """Optional ~/.config/pgforge/config.toml with defaults."""

    log_dir: Path
    """Operator-side logs (separate from server-side /var/log/pgforge)."""


def _base_dir() -> Path:
    override = os.environ.get("PGFORGE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return (root / "pgforge").resolve()


def get_paths(*, state_file_override: str | os.PathLike[str] | None = None) -> Paths:
    """Compute paths, honoring env overrides. Does NOT create directories."""
    base = _base_dir()
    state_file = (
        Path(state_file_override).expanduser().resolve()
        if state_file_override
        else (Path(os.environ["PGFORGE_STATE_FILE"]).expanduser().resolve()
              if os.environ.get("PGFORGE_STATE_FILE")
              else base / "state.json")
    )
    return Paths(
        base=base,
        state_file=state_file,
        keys_dir=base / "keys",
        locks_dir=base / "locks",
        config_file=base / "config.toml",
        log_dir=base / "logs",
    )


def ensure_dirs(paths: Paths) -> None:
    """Create the directories pgforge needs, with restrictive permissions.

    Called lazily by code paths that actually write — keeps ``--help`` and
    ``--version`` from touching the filesystem.
    """
    paths.base.mkdir(parents=True, exist_ok=True)
    paths.keys_dir.mkdir(parents=True, exist_ok=True)
    paths.locks_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    # Keys directory must not be world-readable.
    try:
        os.chmod(paths.keys_dir, 0o700)
        os.chmod(paths.base, 0o700)
    except OSError:
        # Non-POSIX filesystems may not support chmod; ignore.
        pass
