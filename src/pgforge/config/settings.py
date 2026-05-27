"""Optional ``~/.config/pgforge/config.toml`` providing user defaults.

The CLI works with no config file. The file just lets the user set defaults
for ``--provider``, ``--ssh-user``, ``--kms``, etc. so they don't have to
retype them on every ``pgforge provision``.

Example::

    [defaults]
    provider = "hetzner"
    ssh_user = "root"
    kms = "local"
    postgres_version = "16"
    filesystem = "ext4"
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - fallback for 3.10
    import tomli as tomllib  # type: ignore[import-not-found]


def load(config_file: Path) -> dict[str, Any]:
    """Return the ``[defaults]`` table, or ``{}`` if no config exists."""
    if not config_file.is_file():
        return {}
    with config_file.open("rb") as f:
        data = tomllib.load(f)
    return data.get("defaults", {})
