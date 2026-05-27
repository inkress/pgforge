"""Shared envelope-encryption helpers for cloud KMS backends.

All four cloud KMS backends (AWS KMS, GCP KMS, Azure Key Vault, Vault transit)
follow the same recipe:

1. Generate a random 64-byte data-encryption key (DEK) locally.
2. Encrypt the DEK with the cloud's customer-managed key (CMK).
3. Store the resulting ciphertext in :class:`KeyHandle.envelope_ciphertext_b64`
   so pgforge can recover the DEK later.
4. Also write the plaintext DEK to a local file (mode 0600), because the LUKS
   crypttab on the server needs a plaintext keyfile at boot.

When fetching, the local file is the fast path. If it's missing we fall back
to ``decrypt(envelope_ciphertext_b64)`` to recover.

This module factors out the local-file plumbing so the four backends only
have to implement ``encrypt`` / ``decrypt``.
"""

from __future__ import annotations

import base64
import os
import secrets
from abc import abstractmethod
from pathlib import Path
from typing import ClassVar

from pgforge.config.paths import Paths, ensure_dirs, get_paths
from pgforge.errors import KMSDecryptionFailed, KMSKeyNotFound
from pgforge.kms.base import KeyHandle, KeySpec, KMSBackend, UnlockMode
from pgforge.logging import get_logger

log = get_logger(__name__)


class EnvelopeKMSBackend(KMSBackend):
    """Shared base for cloud KMS backends using envelope encryption.

    Subclasses must set ``name`` and implement :meth:`encrypt` / :meth:`decrypt`,
    plus :meth:`schedule_key_deletion` for delete bookkeeping (optional —
    default no-op).
    """

    name: ClassVar[str]

    def __init__(self, paths: Paths | None = None):
        self.paths = paths or get_paths()
        ensure_dirs(self.paths)

    # ---- KMSBackend impls ----

    def generate_key(self, spec: KeySpec) -> KeyHandle:
        if spec.unlock_mode == UnlockMode.RUNTIME:
            # Runtime mode: don't persist DEK plaintext locally. The server-side
            # boot agent fetches and decrypts. The handle still records the
            # envelope ciphertext.
            material = secrets.token_bytes(spec.size_bytes)
            ct = self.encrypt(material, spec)
            key_id = self._build_key_id(spec, suffix=secrets.token_hex(4))
            log.info("generated envelope key %s (runtime mode)", key_id)
            return KeyHandle(
                backend=self.name,
                key_id=key_id,
                envelope_ciphertext_b64=base64.b64encode(ct).decode("ascii"),
                unlock_mode=UnlockMode.RUNTIME,
                metadata={"label": spec.label, "size_bytes": spec.size_bytes},
            )

        material = secrets.token_bytes(spec.size_bytes)
        ct = self.encrypt(material, spec)
        suffix = secrets.token_hex(4)
        key_path = self.paths.keys_dir / f"{_sanitize(spec.label)}-{suffix}.key"
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, material)
        finally:
            os.close(fd)
        log.info("generated envelope key %s (%d bytes; static mode)", key_path, spec.size_bytes)
        return KeyHandle(
            backend=self.name,
            key_id=str(key_path),
            envelope_ciphertext_b64=base64.b64encode(ct).decode("ascii"),
            unlock_mode=UnlockMode.STATIC,
            metadata={"label": spec.label, "size_bytes": spec.size_bytes},
        )

    def fetch_material(self, handle: KeyHandle) -> bytes:
        if handle.unlock_mode == UnlockMode.STATIC:
            path = Path(handle.key_id)
            if path.is_file():
                return path.read_bytes()
        # Fall through: recover from envelope ciphertext.
        if not handle.envelope_ciphertext_b64:
            raise KMSKeyNotFound(
                f"handle {handle.key_id} has no plaintext DEK on disk and no "
                f"envelope ciphertext to recover from."
            )
        ct = base64.b64decode(handle.envelope_ciphertext_b64)
        try:
            return self.decrypt(ct, handle)
        except Exception as e:  # noqa: BLE001
            raise KMSDecryptionFailed(f"could not decrypt envelope: {e}") from e

    def delete(self, handle: KeyHandle) -> None:
        if handle.unlock_mode == UnlockMode.STATIC:
            path = Path(handle.key_id)
            if path.is_file():
                try:
                    size = path.stat().st_size
                    with open(path, "r+b") as fh:
                        fh.write(b"\x00" * size)
                        fh.flush()
                        os.fsync(fh.fileno())
                except OSError:
                    pass
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        self.schedule_key_deletion(handle)

    def rotate(self, handle: KeyHandle) -> KeyHandle:
        size = int(handle.metadata.get("size_bytes", 64))
        label = str(handle.metadata.get("label", "rotated"))
        return self.generate_key(KeySpec(label=label, size_bytes=size, unlock_mode=handle.unlock_mode))

    # ---- per-backend extension points ----

    @abstractmethod
    def encrypt(self, plaintext: bytes, spec: KeySpec) -> bytes:
        """Encrypt the DEK with the backend's KEK. Returns ciphertext blob."""

    @abstractmethod
    def decrypt(self, ciphertext: bytes, handle: KeyHandle) -> bytes:
        """Reverse of :meth:`encrypt`."""

    def schedule_key_deletion(self, handle: KeyHandle) -> None:
        """Optional: schedule deletion of the KEK in the cloud KMS itself.

        Default no-op — the KEK is a long-lived account-level resource, and
        most operators want to keep it for other instances. Subclasses can
        opt in if a handle owns its own KEK.
        """
        return None

    def _build_key_id(self, spec: KeySpec, *, suffix: str) -> str:
        # Default: opaque identifier. Subclasses may override (e.g. to encode
        # the KMS KeyId / CMK arn).
        return f"{self.name}:{_sanitize(spec.label)}-{suffix}"


def _sanitize(label: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in label) or "pgforge"
