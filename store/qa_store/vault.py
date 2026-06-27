"""Secrets vault — the encrypted per-target store the Site Model points into.

The product points at any site and elicits secrets from the operator through
the explorer's questionnaire (test-card numbers, sandbox logins, API keys,
read-only DB URLs). Those raw values NEVER live in the Site Model: the model
(``site_targets.auth.credential_ref``, and later ``site_questions``) stores only
a ``credential_ref`` POINTER, and this module is the single place that turns a
pointer back into a value. Values are encrypted at rest via ``qa_store.crypto``
(Fernet, ``QA_CREDENTIAL_KEY``; same dev-friendly plaintext fallback + threat
model as persona credentials).

This is the consent/secrets boundary the roadmap says to build *before* storing
any secret: discovery proposes a capability, the operator grants it by handing
over a value, the value lands here, and only an opaque pointer travels onward.

A ``credential_ref`` is an opaque, operator-shareable string of the form
``vault://<tenant>/<target>/<ref>``. Callers treat it as opaque — only this
module parses it.
"""

from __future__ import annotations

import uuid

from pymongo import ASCENDING

from qa_store.crypto import EncryptedField, decrypt, encrypt
from qa_store.schema import DEFAULT_TENANT, Store, _now

CREDENTIAL_REF_SCHEME = "vault"


def make_credential_ref(tenant_id: str, target_id: str, ref: str) -> str:
    """Build the opaque pointer the Site Model stores."""
    return f"{CREDENTIAL_REF_SCHEME}://{tenant_id}/{target_id}/{ref}"


def parse_credential_ref(credential_ref: str) -> tuple[str, str, str]:
    """``(tenant_id, target_id, ref)`` from a ``vault://t/target/ref`` pointer.

    Raises :class:`ValueError` on a malformed pointer (wrong scheme, wrong
    arity, or an empty/slash-bearing component).
    """
    prefix = f"{CREDENTIAL_REF_SCHEME}://"
    if not isinstance(credential_ref, str) or not credential_ref.startswith(prefix):
        raise ValueError(f"not a vault credential_ref: {credential_ref!r}")
    parts = credential_ref[len(prefix):].split("/")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"malformed credential_ref: {credential_ref!r}")
    return parts[0], parts[1], parts[2]


def put_secret(
    store: Store,
    *,
    target_id: str,
    value: str,
    ref: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
    label: str = "",
) -> str:
    """Encrypt + store ``value`` and return its ``credential_ref`` pointer.

    ``ref`` is the stable slug within ``(tenant, target)``; omitted → a fresh
    uuid. Re-putting the same ref replaces the value (idempotent on the
    pointer, ``created_at`` preserved). ``label`` is operator-facing, non-secret
    metadata ("admin password", "Visa test card") for the UI inventory — never
    the value itself.
    """
    ref = ref or uuid.uuid4().hex
    if "/" in ref or not ref:
        raise ValueError(f"vault ref must be a non-empty slug without '/': {ref!r}")
    enc = encrypt(value)
    now = _now()
    key = {"tenant_id": tenant_id, "target_id": target_id, "ref": ref}
    store.site_secrets.update_one(
        key,
        {
            "$set": {"secret": enc.to_mongo(), "label": str(label or ""), "updated_at": now},
            "$setOnInsert": {**key, "created_at": now},
        },
        upsert=True,
    )
    return make_credential_ref(tenant_id, target_id, ref)


def get_secret(store: Store, credential_ref: str) -> str | None:
    """Decrypt + return the plaintext a pointer references, or ``None`` if the
    pointer is unknown / malformed / undecryptable.

    The ONLY path that returns a raw secret — callers hand the result straight
    to the thing that needs it (the harness logging in, an API client) and never
    persist it back into the Site Model.
    """
    try:
        tenant_id, target_id, ref = parse_credential_ref(credential_ref)
    except ValueError:
        return None
    doc = store.site_secrets.find_one(
        {"tenant_id": tenant_id, "target_id": target_id, "ref": ref},
    )
    if doc is None:
        return None
    return decrypt(EncryptedField.from_mongo(doc.get("secret")))


def secret_exists(store: Store, credential_ref: str) -> bool:
    """True if the pointer resolves to a stored secret (no decrypt, no value)."""
    try:
        tenant_id, target_id, ref = parse_credential_ref(credential_ref)
    except ValueError:
        return False
    return store.site_secrets.find_one(
        {"tenant_id": tenant_id, "target_id": target_id, "ref": ref},
        projection={"_id": 1},
    ) is not None


def list_secret_refs(
    store: Store,
    tenant_id: str = DEFAULT_TENANT,
    target_id: str | None = None,
) -> list[dict]:
    """Operator-facing secret INVENTORY — ref, label, credential_ref, the
    encrypted flag, and timestamps. NEVER the value. Scoped to a tenant,
    optionally narrowed to one target."""
    query: dict = {"tenant_id": tenant_id}
    if target_id is not None:
        query["target_id"] = target_id
    out: list[dict] = []
    for d in store.site_secrets.find(query).sort("created_at", ASCENDING):
        secret = d.get("secret") or {}
        out.append(
            {
                "tenant_id": d["tenant_id"],
                "target_id": d["target_id"],
                "ref": d["ref"],
                "credential_ref": make_credential_ref(
                    d["tenant_id"], d["target_id"], d["ref"],
                ),
                "label": d.get("label", ""),
                "is_encrypted": bool(secret.get("is_encrypted", False)),
                "created_at": d.get("created_at"),
                "updated_at": d.get("updated_at"),
            }
        )
    return out


def delete_secret(store: Store, credential_ref: str) -> bool:
    """Drop the secret a pointer references. Returns whether a row was removed."""
    try:
        tenant_id, target_id, ref = parse_credential_ref(credential_ref)
    except ValueError:
        return False
    res = store.site_secrets.delete_one(
        {"tenant_id": tenant_id, "target_id": target_id, "ref": ref},
    )
    return res.deleted_count == 1
