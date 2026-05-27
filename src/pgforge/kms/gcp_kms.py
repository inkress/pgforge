"""GCP KMS backend (envelope encryption via ``gcloud kms encrypt`` / ``decrypt``).

Config:

* ``key`` — full resource name of the key version target. Example::

    projects/<project>/locations/global/keyRings/<keyring>/cryptoKeys/<key>

  Or pass it pre-resolved via ``PGFORGE_GCP_KMS_KEY``.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import ClassVar

from pgforge.errors import ConfigError, KMSDecryptionFailed
from pgforge.kms._envelope import EnvelopeKMSBackend
from pgforge.kms.base import KeyHandle, KeySpec
from pgforge.providers._shell import require_binary, run

NAME = "gcp-kms"


class GCPKMS(EnvelopeKMSBackend):
    name: ClassVar[str] = NAME

    def __init__(self, *, key: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self._binary = require_binary("gcloud")
        self._key = key or os.environ.get("PGFORGE_GCP_KMS_KEY")
        if not self._key:
            raise ConfigError(
                "gcp-kms backend requires --kms-config key=<resource-name> or "
                "PGFORGE_GCP_KMS_KEY (e.g. projects/p/locations/global/keyRings/r/cryptoKeys/k)."
            )

    def encrypt(self, plaintext: bytes, spec: KeySpec) -> bytes:
        # gcloud kms encrypt only takes file paths, not stdin. Use temp files.
        with tempfile.NamedTemporaryFile(delete=False) as pt, tempfile.NamedTemporaryFile(delete=False) as ct:
            pt.write(plaintext)
            pt.flush()
            pt_path, ct_path = pt.name, ct.name
        try:
            res = run(
                [
                    self._binary, "kms", "encrypt",
                    "--key", self._key,
                    "--plaintext-file", pt_path,
                    "--ciphertext-file", ct_path,
                ],
                check=False,
            )
            if res.returncode != 0:
                raise KMSDecryptionFailed(f"gcloud kms encrypt failed: {res.stderr.strip()}")
            with open(ct_path, "rb") as fh:
                return fh.read()
        finally:
            for p in (pt_path, ct_path):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def decrypt(self, ciphertext: bytes, handle: KeyHandle) -> bytes:
        with tempfile.NamedTemporaryFile(delete=False) as ct, tempfile.NamedTemporaryFile(delete=False) as pt:
            ct.write(ciphertext)
            ct.flush()
            ct_path, pt_path = ct.name, pt.name
        try:
            res = subprocess.run(
                [
                    self._binary, "kms", "decrypt",
                    "--key", self._key,
                    "--ciphertext-file", ct_path,
                    "--plaintext-file", pt_path,
                ],
                capture_output=True, text=True,
            )
            if res.returncode != 0:
                raise KMSDecryptionFailed(f"gcloud kms decrypt failed: {res.stderr.strip()}")
            with open(pt_path, "rb") as fh:
                return fh.read()
        finally:
            for p in (ct_path, pt_path):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _build_key_id(self, spec: KeySpec, *, suffix: str) -> str:
        return f"gcp-kms:{self._key}:{_safe(spec.label)}-{suffix}"


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s) or "pgforge"
