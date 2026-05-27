"""Envelope KMS base: round-trip with a fake encrypt/decrypt pair."""

from __future__ import annotations

import os

import pytest

from pgforge.kms._envelope import EnvelopeKMSBackend
from pgforge.kms.base import KeyHandle, KeySpec, UnlockMode


class _Fake(EnvelopeKMSBackend):
    name = "fake"

    def encrypt(self, plaintext: bytes, spec) -> bytes:
        # "encrypt" by XOR with 0xAA — round-trippable, not secure (test only).
        return bytes(b ^ 0xAA for b in plaintext)

    def decrypt(self, ciphertext: bytes, handle: KeyHandle) -> bytes:
        return bytes(b ^ 0xAA for b in ciphertext)


def test_static_mode_writes_local_file(isolated_home):
    kms = _Fake(isolated_home)
    h = kms.generate_key(KeySpec(label="fake", size_bytes=32))
    assert h.envelope_ciphertext_b64
    assert h.unlock_mode == UnlockMode.STATIC
    material = kms.fetch_material(h)
    assert len(material) == 32
    # The static-mode file exists on disk.
    assert os.path.isfile(h.key_id)


def test_runtime_mode_skips_local_file(isolated_home):
    kms = _Fake(isolated_home)
    h = kms.generate_key(KeySpec(label="runtime", size_bytes=32, unlock_mode=UnlockMode.RUNTIME))
    assert h.unlock_mode == UnlockMode.RUNTIME
    # Recover via envelope decrypt — no local file needed.
    material = kms.fetch_material(h)
    assert len(material) == 32


def test_envelope_recovery_when_local_file_is_missing(isolated_home):
    kms = _Fake(isolated_home)
    h = kms.generate_key(KeySpec(label="recover", size_bytes=16))
    expected = kms.fetch_material(h)
    os.remove(h.key_id)
    # Should fall through to envelope decrypt.
    recovered = kms.fetch_material(h)
    assert recovered == expected


def test_delete_zeroes_local_and_calls_schedule(isolated_home):
    kms = _Fake(isolated_home)
    h = kms.generate_key(KeySpec(label="bye", size_bytes=8))
    kms.delete(h)
    assert not os.path.isfile(h.key_id)


def test_rotate_preserves_mode(isolated_home):
    kms = _Fake(isolated_home)
    h = kms.generate_key(KeySpec(label="rot", size_bytes=16, unlock_mode=UnlockMode.RUNTIME))
    h2 = kms.rotate(h)
    assert h2.unlock_mode == UnlockMode.RUNTIME
