"""Forward-only schema migrations for the state file.

Each migration is a function ``vN_to_vN_plus_1(raw: dict) -> dict``. We
upgrade in steps so old state files (any prior schema_version) work without
manual intervention.
"""

from __future__ import annotations

from typing import Any, Callable

from pgforge.errors import StateError
from pgforge.state.schema import SCHEMA_VERSION

Migration = Callable[[dict[str, Any]], dict[str, Any]]

# Map of (from_version) -> migration function producing version+1.
_MIGRATIONS: dict[int, Migration] = {
    # 0 -> 1: state files predating versioning had no top-level wrapper.
    # 0: lambda raw: {"schema_version": 1, "instances": raw.get("instances", {})},
}


def migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Upgrade ``raw`` in place until it matches :data:`SCHEMA_VERSION`."""
    version = int(raw.get("schema_version", 0))
    while version < SCHEMA_VERSION:
        migration = _MIGRATIONS.get(version)
        if migration is None:
            raise StateError(
                f"no migration from state schema_version={version} to {SCHEMA_VERSION}"
            )
        raw = migration(raw)
        raw["schema_version"] = version + 1
        version += 1
    if version > SCHEMA_VERSION:
        raise StateError(
            f"state file schema_version={version} is newer than this pgforge "
            f"(supports up to {SCHEMA_VERSION}). Upgrade pgforge."
        )
    return raw
