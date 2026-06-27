"""Site Model access functions — per-(tenant, target) site knowledge as DATA.

The product (Test Ease) points at any site for any tenant. Everything the agent
knows about a target — its map (``site_surfaces``), its test plan
(``test_flows``), and the by-design / known-issue notes (``site_knowledge``)
that used to be HARDCODED in ``personas.py`` — lives as rows keyed by
``(tenant_id, target_id)``, not code. ``site_targets`` is the registry that
replaces ``QA_WEB_BASE_URL`` + the hardcoded admin creds.

Same conventions as ``schema.py``: pymongo + plain dicts, idempotent upserts
(``$setOnInsert`` preserves ``created_at`` across re-runs), ``_strip_id`` on
read. The collection name constants, ``Store`` accessors and index creation all
live in ``schema.py``; this module is just the access layer over them.

Single-tenant for now (``DEFAULT_TENANT``); the ``tenant_id`` column is in place
so multi-tenant is a data change, not a schema one.
"""

from __future__ import annotations

import hashlib
from typing import Any

from pymongo import ASCENDING

from qa_store.schema import DEFAULT_TENANT, Store, _now, _strip_id

# The onboarding lifecycle a target moves through (roadmap §1–2). The explorer
# and the questionnaire UI drive these transitions; kept as a flat set rather
# than a strict FSM so an operator can re-explore or jump back without fighting
# the tool. A freshly-registered target starts at ``registered``.
LIFECYCLE_STATES = (
    "registered",        # known to the tool; nothing discovered yet
    "exploring",         # the explorer is probing for affordances
    "awaiting-answers",  # a questionnaire exists; waiting on the operator
    "configured",        # answers in; personas can be configured from them
    "testing",           # persona runs are happening
    "re-explore",        # operator asked for another discovery pass
)
DEFAULT_LIFECYCLE = "registered"


def content_sha(text: str) -> str:
    """sha256 hex of an embeddable field's text — the fingerprint stored in
    ``embedded_body_sha`` (site_knowledge) / ``embedded_sha`` (site_surfaces)
    so the reconciler can detect new/edited content. Mirrors Sherman's
    ``body_sha`` so the change-detection logic generalises across both."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _upsert(coll: Any, key: dict, fields: dict) -> dict:
    """Idempotent upsert that preserves ``created_at`` and bumps
    ``updated_at``. Returns the stored doc (``_id`` stripped)."""
    now = _now()
    coll.update_one(
        key,
        {
            "$set": {**fields, "updated_at": now},
            "$setOnInsert": {**key, "created_at": now},
        },
        upsert=True,
    )
    return _strip_id(coll.find_one(key))


# ── site_targets ─────────────────────────────────────────────────────────
def upsert_site_target(
    store: Store,
    *,
    target_id: str,
    base_url: str,
    tenant_id: str = DEFAULT_TENANT,
    display_name: str = "",
    auth: dict | None = None,
    scope: dict | None = None,
    ownership: dict | None = None,
    status: str = "active",
) -> dict:
    """Create/replace a target. ``auth.credential_ref`` is a vault/secret
    POINTER string — never a raw secret.

    The onboarding ``lifecycle`` is set on insert only (``$setOnInsert``) so a
    re-upsert of an existing target never resets its onboarding progress;
    advance it with :func:`set_target_lifecycle`."""
    now = _now()
    key = {"tenant_id": tenant_id, "target_id": target_id}
    store.site_targets.update_one(
        key,
        {
            "$set": {
                "base_url": str(base_url).strip(),
                "display_name": str(display_name or target_id).strip(),
                "auth": dict(auth or {"method": "none", "credential_ref": None}),
                "scope": dict(
                    scope
                    or {
                        "allow_globs": [],
                        "deny_globs": [],
                        "max_depth": 3,
                        "rate_limit": None,
                    },
                ),
                "ownership": dict(
                    ownership or {"method": None, "status": "unverified"},
                ),
                "status": status,
                "updated_at": now,
            },
            "$setOnInsert": {**key, "lifecycle": DEFAULT_LIFECYCLE, "created_at": now},
        },
        upsert=True,
    )
    return _strip_id(store.site_targets.find_one(key))


def set_target_lifecycle(
    store: Store, tenant_id: str, target_id: str, lifecycle: str,
) -> dict | None:
    """Move a target to a new onboarding ``lifecycle`` state.

    Validates against :data:`LIFECYCLE_STATES` (any state → any state; the
    explorer/UI own the ordering). Returns the updated target, or ``None`` if
    the target doesn't exist."""
    if lifecycle not in LIFECYCLE_STATES:
        raise ValueError(
            f"unknown lifecycle {lifecycle!r}; expected one of "
            f"{', '.join(LIFECYCLE_STATES)}",
        )
    if get_site_target(store, tenant_id, target_id) is None:
        return None
    return update_site_target(store, tenant_id, target_id, lifecycle=lifecycle)


def get_site_target(
    store: Store, tenant_id: str, target_id: str,
) -> dict | None:
    return _strip_id(
        store.site_targets.find_one(
            {"tenant_id": tenant_id, "target_id": target_id},
        ),
    )


def list_site_targets(
    store: Store, tenant_id: str = DEFAULT_TENANT,
) -> list[dict]:
    cur = store.site_targets.find({"tenant_id": tenant_id}).sort(
        "created_at", ASCENDING,
    )
    return [_strip_id(d) for d in cur]


def update_site_target(
    store: Store, tenant_id: str, target_id: str, **fields: Any,
) -> dict | None:
    if fields:
        store.site_targets.update_one(
            {"tenant_id": tenant_id, "target_id": target_id},
            {"$set": {**fields, "updated_at": _now()}},
        )
    return get_site_target(store, tenant_id, target_id)


def delete_site_target(
    store: Store, tenant_id: str, target_id: str,
) -> bool:
    res = store.site_targets.delete_one(
        {"tenant_id": tenant_id, "target_id": target_id},
    )
    return res.deleted_count == 1


# ── site_surfaces ────────────────────────────────────────────────────────
def upsert_site_surface(
    store: Store,
    *,
    target_id: str,
    surface_id: str,
    kind: str,
    tenant_id: str = DEFAULT_TENANT,
    path: str = "",
    title: str = "",
    description: str = "",
    forms: list | None = None,
    links: list | None = None,
    detected_auth: Any = None,
    source: str = "crawl",
) -> dict:
    return _upsert(
        store.site_surfaces,
        {"tenant_id": tenant_id, "target_id": target_id, "surface_id": surface_id},
        {
            "kind": kind,
            "path": path,
            "title": title,
            "description": description,
            "forms": list(forms or []),
            "links": list(links or []),
            "detected_auth": detected_auth,
            "source": source,
            "discovered_at": _now(),
            # Vector layer (wired in a later PR): embedding of `description`.
            "description_embedding": None,
            "embedded_sha": None,
        },
    )


def get_site_surface(
    store: Store, tenant_id: str, target_id: str, surface_id: str,
) -> dict | None:
    return _strip_id(
        store.site_surfaces.find_one(
            {
                "tenant_id": tenant_id,
                "target_id": target_id,
                "surface_id": surface_id,
            },
        ),
    )


def list_surfaces_by_target(
    store: Store, tenant_id: str, target_id: str,
) -> list[dict]:
    cur = store.site_surfaces.find(
        {"tenant_id": tenant_id, "target_id": target_id},
    ).sort("path", ASCENDING)
    return [_strip_id(d) for d in cur]


def delete_site_surface(
    store: Store, tenant_id: str, target_id: str, surface_id: str,
) -> bool:
    res = store.site_surfaces.delete_one(
        {
            "tenant_id": tenant_id,
            "target_id": target_id,
            "surface_id": surface_id,
        },
    )
    return res.deleted_count == 1


# ── test_flows ───────────────────────────────────────────────────────────
def upsert_test_flow(
    store: Store,
    *,
    target_id: str,
    flow_id: str,
    tenant_id: str = DEFAULT_TENANT,
    area: str = "",
    user_story: str = "",
    steps: list | None = None,
    priority: str = "normal",
    persona_archetype: str | None = None,
    generated_from: str = "operator",
    enabled: bool = True,
) -> dict:
    return _upsert(
        store.test_flows,
        {"tenant_id": tenant_id, "target_id": target_id, "flow_id": flow_id},
        {
            "area": area,
            "user_story": user_story,
            "steps": list(steps or []),
            "priority": priority,
            "persona_archetype": persona_archetype,
            "generated_from": generated_from,
            "enabled": bool(enabled),
        },
    )


def get_test_flow(
    store: Store, tenant_id: str, target_id: str, flow_id: str,
) -> dict | None:
    return _strip_id(
        store.test_flows.find_one(
            {"tenant_id": tenant_id, "target_id": target_id, "flow_id": flow_id},
        ),
    )


def list_flows_by_target(
    store: Store, tenant_id: str, target_id: str,
) -> list[dict]:
    cur = store.test_flows.find(
        {"tenant_id": tenant_id, "target_id": target_id},
    ).sort("flow_id", ASCENDING)
    return [_strip_id(d) for d in cur]


def delete_test_flow(
    store: Store, tenant_id: str, target_id: str, flow_id: str,
) -> bool:
    res = store.test_flows.delete_one(
        {"tenant_id": tenant_id, "target_id": target_id, "flow_id": flow_id},
    )
    return res.deleted_count == 1


# ── site_knowledge (mirrors Sherman's knowledge_base shape) ──────────────
def upsert_site_knowledge(
    store: Store,
    *,
    target_id: str,
    entry_id: str,
    kind: str,
    body: str,
    tenant_id: str = DEFAULT_TENANT,
    applies_to: list | None = None,
    authored_by: str = "operator",
) -> dict:
    return _upsert(
        store.site_knowledge,
        {"tenant_id": tenant_id, "target_id": target_id, "entry_id": entry_id},
        {
            "kind": kind,
            "body": body,
            "applies_to": list(applies_to or []),
            "authored_by": authored_by,
            # Vector layer (wired in a later PR): same shape as Sherman's
            # knowledge_base so the same reconciler/retriever generalise.
            "body_embedding": None,
            "embedded_body_sha": None,
        },
    )


def get_site_knowledge(
    store: Store, tenant_id: str, target_id: str, entry_id: str,
) -> dict | None:
    return _strip_id(
        store.site_knowledge.find_one(
            {"tenant_id": tenant_id, "target_id": target_id, "entry_id": entry_id},
        ),
    )


def list_knowledge_by_target(
    store: Store, tenant_id: str, target_id: str,
) -> list[dict]:
    cur = store.site_knowledge.find(
        {"tenant_id": tenant_id, "target_id": target_id},
    ).sort("entry_id", ASCENDING)
    return [_strip_id(d) for d in cur]


def delete_site_knowledge(
    store: Store, tenant_id: str, target_id: str, entry_id: str,
) -> bool:
    res = store.site_knowledge.delete_one(
        {"tenant_id": tenant_id, "target_id": target_id, "entry_id": entry_id},
    )
    return res.deleted_count == 1
