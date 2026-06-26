"""Tests for the credential crypto module (#1105).

The crypto module ships in two modes:
  - Key configured: real Fernet encryption.
  - Key missing or invalid: plaintext fallback + warning log.

Both modes need explicit test coverage so a future key-rotation
change doesn't silently shift behaviour.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet

from qa_store.crypto import _ENV_KEY, EncryptedField, decrypt, encrypt


# ---------------------------------------------------------------------------
# Plaintext fallback (no key configured)
# ---------------------------------------------------------------------------
def test_encrypt_without_key_returns_plaintext_with_warning(
    monkeypatch, caplog,
):
    monkeypatch.delenv(_ENV_KEY, raising=False)
    caplog.set_level(logging.WARNING, logger="qa_store.crypto")
    field = encrypt("hunter22")
    assert field.is_encrypted is False
    assert field.value == "hunter22"
    assert any(
        "not configured" in r.getMessage() for r in caplog.records
    ), "missing-key warning should land at WARNING level"


def test_decrypt_plaintext_field_passes_through(monkeypatch):
    monkeypatch.delenv(_ENV_KEY, raising=False)
    field = EncryptedField(value="hunter22", is_encrypted=False)
    assert decrypt(field) == "hunter22"


# ---------------------------------------------------------------------------
# Encrypted round-trip (key configured)
# ---------------------------------------------------------------------------
def test_encrypt_with_key_returns_ciphertext(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(_ENV_KEY, key)
    field = encrypt("hunter22")
    assert field.is_encrypted is True
    assert field.value != "hunter22"  # actually encrypted


def test_encrypt_decrypt_roundtrip(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(_ENV_KEY, key)
    field = encrypt("hunter22")
    assert decrypt(field) == "hunter22"


def test_decrypt_returns_none_when_key_missing_for_encrypted_field(
    monkeypatch, caplog,
):
    """Key rotation-without-re-encryption case: existing rows have
    is_encrypted=True but the running pod has no key. Decrypt fails
    softly — returns None so the caller falls back to signup."""
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(_ENV_KEY, key)
    field = encrypt("hunter22")
    # Now strip the key and try again.
    monkeypatch.delenv(_ENV_KEY, raising=False)
    caplog.set_level(logging.ERROR, logger="qa_store.crypto")
    assert decrypt(field) is None
    assert any(
        "cannot decrypt" in r.getMessage() for r in caplog.records
    )


def test_decrypt_returns_none_on_corrupt_ciphertext(monkeypatch, caplog):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(_ENV_KEY, key)
    caplog.set_level(logging.ERROR, logger="qa_store.crypto")
    corrupt = EncryptedField(value="not-a-valid-token", is_encrypted=True)
    assert decrypt(corrupt) is None
    assert any(
        "decrypt failed" in r.getMessage() for r in caplog.records
    )


def test_decrypt_none_field_returns_none():
    assert decrypt(None) is None


def test_encrypt_with_malformed_key_falls_back_to_plaintext(
    monkeypatch, caplog,
):
    """Invalid key string (not a valid Fernet key) should NOT crash
    the persona-save path; log + fall back to plaintext is the
    dev-friendly behaviour."""
    monkeypatch.setenv(_ENV_KEY, "not-a-real-key")
    caplog.set_level(logging.WARNING, logger="qa_store.crypto")
    field = encrypt("hunter22")
    # Falls back to plaintext; the warning is about the bad key being
    # logged via the lazy-load path.
    assert field.is_encrypted is False
    assert field.value == "hunter22"


# ---------------------------------------------------------------------------
# EncryptedField <-> Mongo doc round-trip
# ---------------------------------------------------------------------------
def test_encrypted_field_to_from_mongo_roundtrip():
    field = EncryptedField(value="abc", is_encrypted=True)
    as_doc = field.to_mongo()
    assert as_doc == {"value": "abc", "is_encrypted": True}
    back = EncryptedField.from_mongo(as_doc)
    assert back == field


def test_encrypted_field_from_mongo_none_returns_none():
    """Pre-#1105 persona docs have no credentials sub-doc at all;
    from_mongo(None) must not raise."""
    assert EncryptedField.from_mongo(None) is None
