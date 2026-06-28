"""Instance-level config — the LLM backend choice + its BYOK credential.

Test Ease runs on the operator's own **Claude Code Max subscription** (flat
price): the harness runs the ``claude`` CLI on a ``CLAUDE_CODE_OAUTH_TOKEN`` and
scrubs ``ANTHROPIC_API_KEY``. The backend is selectable — ``claude-code`` (the
default and the point of the product) or ``api`` (per-token ``ANTHROPIC_API_KEY``).

This module stores the operator's *choice* and a **vault POINTER** to the token;
the raw token lives only in :mod:`qa_store.vault`, never inline. Environment
variables remain a valid source (the cluster Max-Job pattern) — the API layer
reports env presence as a fallback. ``get_llm_token`` here resolves the
vault-stored token only; callers combine it with the env fallback.
"""

from __future__ import annotations

from qa_store.schema import DEFAULT_TENANT, Store, _now
from qa_store.vault import delete_secret, get_secret, put_secret

LLM_BACKENDS = ("claude-code", "api")
DEFAULT_LLM_BACKEND = "claude-code"

# The env var each backend's token is conventionally provided as (the cluster
# pattern + what the API checks for the "configured via environment" status).
LLM_BACKEND_ENV = {
    "claude-code": "CLAUDE_CODE_OAUTH_TOKEN",
    "api": "ANTHROPIC_API_KEY",
}

# Where an instance-level (not per-site) vault secret lives. ``_config`` is a
# reserved pseudo-target — it never appears in the Sites list (site_targets).
_CONFIG_TARGET = "_config"
_LLM_TOKEN_REF = "llm-token"
_LLM_KEY = "llm"


def get_llm_config(store: Store, tenant_id: str = DEFAULT_TENANT) -> dict:
    """The stored ``{backend, credential_ref}`` (defaults when unset). Never
    returns the token itself."""
    doc = store.qa_config.find_one({"tenant_id": tenant_id, "key": _LLM_KEY})
    if doc is None:
        return {"backend": DEFAULT_LLM_BACKEND, "credential_ref": None}
    return {
        "backend": doc.get("backend", DEFAULT_LLM_BACKEND),
        "credential_ref": doc.get("credential_ref"),
    }


def set_llm_config(
    store: Store,
    *,
    backend: str,
    token: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> dict:
    """Set the backend; if a ``token`` is given, vault it and keep only the
    pointer. Validates ``backend``. Returns ``{backend, credential_ref}`` (no
    token)."""
    if backend not in LLM_BACKENDS:
        raise ValueError(
            f"unknown backend {backend!r}; expected one of {', '.join(LLM_BACKENDS)}",
        )
    credential_ref = get_llm_config(store, tenant_id)["credential_ref"]
    if token:
        credential_ref = put_secret(
            store, tenant_id=tenant_id, target_id=_CONFIG_TARGET,
            value=token, ref=_LLM_TOKEN_REF, label=f"{backend} token",
        )
    now = _now()
    store.qa_config.update_one(
        {"tenant_id": tenant_id, "key": _LLM_KEY},
        {
            "$set": {"backend": backend, "credential_ref": credential_ref, "updated_at": now},
            "$setOnInsert": {"tenant_id": tenant_id, "key": _LLM_KEY, "created_at": now},
        },
        upsert=True,
    )
    return get_llm_config(store, tenant_id)


def get_llm_token(store: Store, tenant_id: str = DEFAULT_TENANT) -> str | None:
    """The plaintext BYOK token from the vault, or ``None`` if none is stored.
    The single read path for the raw value — callers (the harness) fall back to
    the env var when this is ``None``."""
    ref = get_llm_config(store, tenant_id)["credential_ref"]
    return get_secret(store, ref) if ref else None


def clear_llm_token(store: Store, tenant_id: str = DEFAULT_TENANT) -> dict:
    """Drop the vaulted token + its pointer (keeps the backend choice)."""
    cfg = get_llm_config(store, tenant_id)
    if cfg["credential_ref"]:
        delete_secret(store, cfg["credential_ref"])
    store.qa_config.update_one(
        {"tenant_id": tenant_id, "key": _LLM_KEY},
        {"$set": {"credential_ref": None, "updated_at": _now()}},
    )
    return get_llm_config(store, tenant_id)
