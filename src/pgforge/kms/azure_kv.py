"""Azure Key Vault backend (envelope encryption via ``az keyvault key encrypt``).

Config:

* ``vault`` — Key Vault name.
* ``key`` — Key name inside the vault.
* (optional) ``algorithm`` — defaults to ``RSA-OAEP-256``.
"""

from __future__ import annotations

import base64
import os
from typing import ClassVar

from pgforge.errors import ConfigError, KMSDecryptionFailed
from pgforge.kms._envelope import EnvelopeKMSBackend
from pgforge.kms.base import KeyHandle, KeySpec
from pgforge.providers._shell import require_binary, run

NAME = "azure-kv"


class AzureKeyVault(EnvelopeKMSBackend):
    name: ClassVar[str] = NAME

    def __init__(self, *, vault: str | None = None, key: str | None = None, algorithm: str = "RSA-OAEP-256", **kwargs):
        super().__init__(**kwargs)
        self._binary = require_binary("az")
        self._vault = vault or os.environ.get("PGFORGE_AZURE_KV_VAULT")
        self._key = key or os.environ.get("PGFORGE_AZURE_KV_KEY")
        self._algorithm = algorithm
        if not self._vault or not self._key:
            raise ConfigError(
                "azure-kv backend requires --kms-config vault=<name>,key=<name> "
                "(or PGFORGE_AZURE_KV_VAULT / PGFORGE_AZURE_KV_KEY env vars)."
            )

    def encrypt(self, plaintext: bytes, spec: KeySpec) -> bytes:
        # az keyvault key encrypt expects --value base64-encoded.
        value = base64.b64encode(plaintext).decode("ascii")
        res = run(
            [
                self._binary, "keyvault", "key", "encrypt",
                "--vault-name", self._vault,
                "--name", self._key,
                "--algorithm", self._algorithm,
                "--value", value,
                "--data-type", "base64",
                "-o", "json",
            ],
            parse_json=True,
        )
        result = (res.parsed or {}).get("result", "")
        return base64.b64decode(result)

    def decrypt(self, ciphertext: bytes, handle: KeyHandle) -> bytes:
        value = base64.b64encode(ciphertext).decode("ascii")
        res = run(
            [
                self._binary, "keyvault", "key", "decrypt",
                "--vault-name", self._vault,
                "--name", self._key,
                "--algorithm", self._algorithm,
                "--value", value,
                "--data-type", "base64",
                "-o", "json",
            ],
            check=False, parse_json=True,
        )
        if res.returncode != 0:
            raise KMSDecryptionFailed(f"az keyvault key decrypt failed: {res.stderr.strip()}")
        result = (res.parsed or {}).get("result", "")
        return base64.b64decode(result)

    def _build_key_id(self, spec: KeySpec, *, suffix: str) -> str:
        return f"azure-kv:{self._vault}/{self._key}:{_safe(spec.label)}-{suffix}"


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s) or "pgforge"
