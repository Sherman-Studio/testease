"""Encryption helpers for persona credentials (#1105).

Persona passwords are persisted alongside the persona doc so the harness
can re-authenticate on subsequent runs instead of signing up fresh. The
password is the only field that NEEDS encryption — email is operator-
visible by design, and JWT cookies are short-lived enough that plaintext
storage is acceptable for the operator-tier threat model.

Key source: ``QA_CREDENTIAL_KEY`` env var, a 32-byte url-safe base64
string (i.e. a ``Fernet.generate_key()`` value). Missing key is the
DEV-FRIENDLY path: passwords get persisted in plaintext with a loud
warning so a fresh checkout still works. Production wires the key via
``infra/qa-agents.tf`` + ``infra/qa-review.tf`` into the same Secret
both pods mount.

Rotation policy:
- Generate a new key, set it as ``QA_CREDENTIAL_KEY``, run a one-shot
  re-encryption script (TODO follow-up). Existing rows decrypt with
  the OLD key first, fall back to the new on miss.
- For now (pre-launch), losing the key just means everyone signs up
  fresh on next run — annoying but not data loss.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

_ENV_KEY = "QA_CREDENTIAL_KEY"


@dataclass(frozen=True)
class EncryptedField:
    """A persisted field's two halves — the cipher bytes + a flag the
    reader uses to decide whether to decrypt or trust the value as-is.

    Stored on the Mongo doc as a small object so both halves travel
    together. Pre-#1105 docs (no field at all) read back as None and
    the caller handles them explicitly."""
    value: str
    is_encrypted: bool

    def to_mongo(self) -> dict:
        return {"value": self.value, "is_encrypted": self.is_encrypted}

    @classmethod
    def from_mongo(cls, doc: dict | None) -> EncryptedField | None:
        if not doc:
            return None
        return cls(
            value=doc.get("value", ""),
            is_encrypted=bool(doc.get("is_encrypted", False)),
        )


def _load_fernet():
    """Return a Fernet instance if the key is configured, else None.

    Import is lazy so a process that never touches credentials doesn't
    pay the ``cryptography`` import cost (it's a heavy native dep).
    """
    key = os.environ.get(_ENV_KEY, "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet  # noqa: PLC0415
        return Fernet(key.encode("utf-8"))
    except Exception:  # noqa: BLE001 — malformed key
        log.exception(
            "crypto: %s is set but invalid — falling back to plaintext",
            _ENV_KEY,
        )
        return None


def encrypt(plain: str) -> EncryptedField:
    """Encrypt a plaintext value for persistence.

    When ``QA_CREDENTIAL_KEY`` is set + valid: returns an
    EncryptedField with ``is_encrypted=True`` and Fernet ciphertext as
    the value (url-safe base64 string).

    When the key is missing or malformed: returns an EncryptedField
    with ``is_encrypted=False`` and the plaintext as the value, AND
    logs a warning. This is the dev-friendly path; production should
    never hit it.
    """
    fernet = _load_fernet()
    if fernet is None:
        log.warning(
            "crypto: %s not configured; persisting credential field "
            "as plaintext. Set the env var for production deploys.",
            _ENV_KEY,
        )
        return EncryptedField(value=plain, is_encrypted=False)
    token = fernet.encrypt(plain.encode("utf-8")).decode("ascii")
    return EncryptedField(value=token, is_encrypted=True)


def decrypt(field: EncryptedField | None) -> str | None:
    """Inverse of :func:`encrypt`.

    Returns the plaintext for both encrypted + plaintext fields (the
    latter is just passthrough). Returns None for a missing field. If
    decryption fails (key rotated without re-encryption, corrupted
    ciphertext) logs an error and returns None — the caller treats it
    as "credentials lost; fall back to signup".
    """
    if field is None:
        return None
    if not field.is_encrypted:
        return field.value
    fernet = _load_fernet()
    if fernet is None:
        log.error(
            "crypto: cannot decrypt; %s is unset but the field is "
            "marked is_encrypted=True. Persona will fall back to signup.",
            _ENV_KEY,
        )
        return None
    try:
        return fernet.decrypt(field.value.encode("ascii")).decode("utf-8")
    except Exception:  # noqa: BLE001
        log.exception("crypto: decrypt failed; treating field as lost")
        return None
