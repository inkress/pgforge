"""HashiCorp Vault backend.

Uses Vault's **transit secrets engine** for envelope encryption (the cleanest
fit for our model — Vault never exposes the KEK, but encrypt/decrypt of the
DEK is fast). Config:

* ``mount`` — transit mount path (default: ``transit``)
* ``key`` — transit key name (default: ``pgforge``)
* ``addr`` / ``token`` — read from ``VAULT_ADDR`` and ``VAULT_TOKEN`` if unset
  (or any other Vault auth method already configured in the operator's
  environment — pgforge does not log in for you).

Falls back to the ``vault`` CLI; no Python Vault SDK dependency.
"""

from __future__ import annotations

import base64
import os
from typing import ClassVar

from pgforge.errors import ConfigError, KMSDecryptionFailed
from pgforge.kms._envelope import EnvelopeKMSBackend
from pgforge.kms.base import KeyHandle, KeySpec
from pgforge.providers._shell import require_binary, run

NAME = "vault"


class VaultKMS(EnvelopeKMSBackend):
    name: ClassVar[str] = NAME

    def __init__(
        self,
        *,
        mount: str | None = None,
        key: str | None = None,
        addr: str | None = None,
        token: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._binary = require_binary("vault")
        self._mount = mount or os.environ.get("PGFORGE_VAULT_MOUNT") or "transit"
        self._key = key or os.environ.get("PGFORGE_VAULT_KEY") or "pgforge"
        self._addr = addr or os.environ.get("VAULT_ADDR")
        self._token = token or os.environ.get("VAULT_TOKEN")
        if not self._addr:
            raise ConfigError(
                "vault backend requires VAULT_ADDR (or --kms-config addr=...). "
                "VAULT_TOKEN (or another configured auth method) is also required."
            )

    def _env(self) -> dict[str, str]:
        env = {"VAULT_ADDR": self._addr or ""}
        if self._token:
            env["VAULT_TOKEN"] = self._token
        return env

    def encrypt(self, plaintext: bytes, spec: KeySpec) -> bytes:
        b64_pt = base64.b64encode(plaintext).decode("ascii")
        res = run(
            [
                self._binary, "write", "-format=json",
                f"{self._mount}/encrypt/{self._key}",
                f"plaintext={b64_pt}",
            ],
            env=self._env(), parse_json=True,
        )
        ciphertext_str = ((res.parsed or {}).get("data") or {}).get("ciphertext", "")
        if not ciphertext_str:
            raise KMSDecryptionFailed("vault transit encrypt returned no ciphertext")
        # Vault returns "vault:v1:<base64>" form. We store it verbatim.
        return ciphertext_str.encode("ascii")

    def decrypt(self, ciphertext: bytes, handle: KeyHandle) -> bytes:
        ct_str = ciphertext.decode("ascii")
        res = run(
            [
                self._binary, "write", "-format=json",
                f"{self._mount}/decrypt/{self._key}",
                f"ciphertext={ct_str}",
            ],
            env=self._env(), check=False, parse_json=True,
        )
        if res.returncode != 0:
            raise KMSDecryptionFailed(f"vault transit decrypt failed: {res.stderr.strip()}")
        b64_pt = ((res.parsed or {}).get("data") or {}).get("plaintext", "")
        return base64.b64decode(b64_pt)

    def _build_key_id(self, spec: KeySpec, *, suffix: str) -> str:
        return f"vault:{self._mount}/{self._key}:{_safe(spec.label)}-{suffix}"


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s) or "pgforge"
