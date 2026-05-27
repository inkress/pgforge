"""Local-keyfile KMS backend.

Keys live at ``~/.config/pgforge/keys/<label>-<short-id>.key`` mode 0600.
This matches the security model of the bash baseline: the operator holds the
key on disk and uploads a copy to the server so the volume auto-unlocks at
boot.

Recommended for: development, single-operator setups, anywhere a HashiCorp
Vault or cloud KMS would be overkill.

**Not** recommended for: multi-operator teams (no central recovery story),
or threat models where "operator's laptop stolen" is a concern. Use Vault
or cloud KMS for those.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from pgforge.config.paths import Paths, ensure_dirs, get_paths
from pgforge.errors import KMSKeyNotFound
from pgforge.kms.base import KeyHandle, KeySpec, KMSBackend, UnlockMode
from pgforge.logging import get_logger

log = get_logger(__name__)

NAME = "local"


class LocalKMS(KMSBackend):
    name = NAME

    def __init__(self, paths: Paths | None = None):
        self.paths = paths or get_paths()
        ensure_dirs(self.paths)

    # ---- abstract methods ----

    def generate_key(self, spec: KeySpec) -> KeyHandle:
        if spec.unlock_mode == UnlockMode.RUNTIME:
            raise NotImplementedError(
                "runtime unlock mode is not implemented yet (Phase 5). "
                "Use unlock_mode=static."
            )
        # ``secrets.token_bytes`` uses the OS CSPRNG.
        material = secrets.token_bytes(spec.size_bytes)
        # Short suffix keeps key files distinguishable when a label is reused.
        suffix = secrets.token_hex(4)
        key_path = self.paths.keys_dir / f"{_sanitize(spec.label)}-{suffix}.key"
        # umask-tight write: open with O_CREAT|O_EXCL so we never overwrite an
        # existing file, then chmod to 0600.
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, material)
        finally:
            os.close(fd)
        log.info("generated local key %s (%d bytes)", key_path, spec.size_bytes)
        return KeyHandle(
            backend=NAME,
            key_id=str(key_path),
            envelope_ciphertext_b64=None,
            unlock_mode=spec.unlock_mode,
            metadata={"label": spec.label, "size_bytes": spec.size_bytes},
        )

    def fetch_material(self, handle: KeyHandle) -> bytes:
        if handle.backend != NAME:
            raise ValueError(f"handle is for backend {handle.backend!r}, not {NAME!r}")
        path = Path(handle.key_id)
        if not path.is_file():
            raise KMSKeyNotFound(
                f"local key {path} is missing. If you deleted it, the data is "
                f"unrecoverable unless a copy exists on the server."
            )
        return path.read_bytes()

    def delete(self, handle: KeyHandle) -> None:
        if handle.backend != NAME:
            return
        path = Path(handle.key_id)
        if path.is_file():
            try:
                # Best-effort overwrite before unlink. Not a guaranteed wipe
                # on modern flash, but a step above plain unlink.
                size = path.stat().st_size
                with open(path, "r+b") as fh:
                    fh.write(b"\x00" * size)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError:
                pass
            try:
                path.unlink()
                log.info("deleted local key %s", path)
            except FileNotFoundError:
                pass

    def rotate(self, handle: KeyHandle) -> KeyHandle:
        size = int(handle.metadata.get("size_bytes", 64))
        label = handle.metadata.get("label", "rotated")
        return self.generate_key(KeySpec(label=str(label), size_bytes=size))


def _sanitize(label: str) -> str:
    out = []
    for ch in label:
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "pgforge"
