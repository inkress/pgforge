"""KMS backend ABC.

``generate_key`` returns a :class:`KeyHandle` — never raw key material.
:meth:`fetch_material` is the only path to plaintext bytes and must only be
called by code that's about to hand the bytes to ``cryptsetup``.

The :class:`UnlockMode` field on a handle records how the volume is unlocked
on the server:

* ``static`` — the plaintext key sits on the server in
  ``/root/.pgforge/keys/<instance>.key`` and is referenced from
  ``/etc/crypttab``. This matches the bash baseline. KMS gates *operator*
  recovery only.
* ``runtime`` — a boot-time agent on the server fetches the key into
  tmpfs using an instance role / VM identity / Vault auth. The key never
  persists on disk across reboots. Phase 5.

MVP only implements ``static``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar


class UnlockMode(str, Enum):
    STATIC = "static"
    RUNTIME = "runtime"


@dataclass
class KeySpec:
    """Caller's intent for ``generate_key``."""

    label: str
    """Human-readable label baked into the key id (e.g., ``pgforge-myapp``)."""

    size_bytes: int = 64
    """LUKS supports up to 8192-bit keys; we default to 512-bit."""

    unlock_mode: UnlockMode = UnlockMode.STATIC


@dataclass
class KeyHandle:
    backend: str
    key_id: str
    """Backend-specific identifier (path for local; ARN for AWS KMS; URL for
    Vault; etc.). Stored verbatim in the state file."""

    envelope_ciphertext_b64: str | None = None
    """For cloud-KMS backends using envelope encryption: the KMS-encrypted DEK."""

    unlock_mode: UnlockMode = UnlockMode.STATIC
    metadata: dict[str, Any] = field(default_factory=dict)


class KMSBackend(ABC):
    name: ClassVar[str]

    @abstractmethod
    def generate_key(self, spec: KeySpec) -> KeyHandle:
        """Create a new key, return a handle. Implementations must persist
        enough information that :meth:`fetch_material` can later retrieve the
        plaintext bytes given the handle alone."""

    @abstractmethod
    def fetch_material(self, handle: KeyHandle) -> bytes:
        """Return the plaintext key bytes. Callers must zeroize the buffer
        as soon as they've passed it to ``cryptsetup``."""

    @abstractmethod
    def delete(self, handle: KeyHandle) -> None:
        """Best-effort destruction. Local backend ``os.remove``s the file;
        cloud KMS backends schedule key deletion via the provider."""

    @abstractmethod
    def rotate(self, handle: KeyHandle) -> KeyHandle:
        """Create a new key alongside the old one. Caller is responsible for
        re-keying the LUKS slot and then calling :meth:`delete` on the old
        handle once the new slot is verified."""

    def supports_server_side_unlock(self) -> bool:
        """True if the backend can drive an automatic boot-time unlock.

        The local backend uploads a keyfile to the server (auto-unlock via
        crypttab). Envelope cloud KMS backends do the same — the *operator*
        view of recovery differs but the *server* sees the same plaintext key
        on disk. Future runtime-unlock mode flips this story.
        """
        return True
