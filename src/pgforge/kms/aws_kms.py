"""AWS KMS backend (envelope encryption via ``aws kms encrypt`` / ``decrypt``).

Required config (via constructor kwargs or env):

* ``key_id`` — the KMS key ARN or alias (``alias/pgforge``). Read from
  ``--kms-config key_id=...`` or ``PGFORGE_AWS_KMS_KEY_ID``.
* ``region`` — optional, defaults to ``AWS_REGION`` / ``AWS_DEFAULT_REGION``.

The CMK can encrypt arbitrary blobs up to 4 KB — well over our 64-byte DEKs.
"""

from __future__ import annotations

import base64
import os
from typing import ClassVar

from pgforge.errors import ConfigError, KMSDecryptionFailed
from pgforge.kms._envelope import EnvelopeKMSBackend
from pgforge.kms.base import KeyHandle, KeySpec
from pgforge.providers._shell import require_binary, run

NAME = "aws-kms"


class AWSKMS(EnvelopeKMSBackend):
    name: ClassVar[str] = NAME

    def __init__(self, *, key_id: str | None = None, region: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self._binary = require_binary("aws")
        self._key_id = key_id or os.environ.get("PGFORGE_AWS_KMS_KEY_ID")
        self._region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not self._key_id:
            raise ConfigError(
                "aws-kms backend requires --kms-config key_id=<arn|alias> or "
                "PGFORGE_AWS_KMS_KEY_ID."
            )

    def encrypt(self, plaintext: bytes, spec: KeySpec) -> bytes:
        argv = [
            self._binary, "kms", "encrypt",
            "--key-id", self._key_id,
            "--plaintext", "fileb:///dev/stdin",
            "--output", "json",
            "--query", "CiphertextBlob",
            "--encryption-context", f"pgforge-label={spec.label}",
        ]
        if self._region:
            argv += ["--region", self._region]
        res = run(argv, input=base64.b64encode(plaintext).decode("ascii"), parse_json=False)
        # aws --query strips surrounding quotes when output=text; with output=json
        # we get a JSON-encoded string. Strip whitespace + quotes.
        body = res.stdout.strip().strip('"')
        return base64.b64decode(body)

    def decrypt(self, ciphertext: bytes, handle: KeyHandle) -> bytes:
        argv = [
            self._binary, "kms", "decrypt",
            "--ciphertext-blob", "fileb:///dev/stdin",
            "--output", "json",
            "--query", "Plaintext",
            "--encryption-context", f"pgforge-label={handle.metadata.get('label', '')}",
        ]
        if self._region:
            argv += ["--region", self._region]
        res = run(argv, input=base64.b64encode(ciphertext).decode("ascii"), check=False)
        if res.returncode != 0:
            raise KMSDecryptionFailed(f"aws kms decrypt failed: {res.stderr.strip()}")
        body = res.stdout.strip().strip('"')
        return base64.b64decode(body)

    def _build_key_id(self, spec: KeySpec, *, suffix: str) -> str:
        # Carry both the CMK arn and the local suffix so we can fetch later.
        return f"aws-kms:{self._key_id}:{_safe(spec.label)}-{suffix}"


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s) or "pgforge"
