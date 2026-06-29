"""Capabilities — what the operator can *grant* Test Ease to test a site deeper.

The more access a site grants, the more like an insider Test Ease tests, and the
fewer false alarms it raises (a *symptom* — "the page errored" — becomes a
*fact* — "500 at OrderService:42, request id abc"). See docs/CAPABILITIES.md.

This is the questionnaire generalised: a grant is usually a secret → the
**vault** (pointer only); a granted capability lights up an MCP tool for the
harness. The CATALOG is seeded baseline data, extensible by operators (the open
tail). GRANTS are per-(tenant, target). A derived **depth score** drives the
"level up your testing" UX.
"""

from __future__ import annotations

from qa_store.schema import DEFAULT_TENANT, Store, _now, _strip_id
from qa_store.vault import delete_secret, put_secret

# The access ladder — index == capability `level`.
DEPTH_LEVELS = (
    "Black-box",            # 0 — just a URL
    "Authenticated",        # 1 — test logins / read context
    "Instrumented inputs",  # 2 — sandbox email, test cards
    "Observability",        # 3 — logs, errors, metrics (read)
    "State verification",   # 4 — read-only DB, admin/read API
    "Environment control",  # 5 — kube/exec, flags, seed/reset
)
CAPABILITY_STATUSES = ("proposed", "granted", "declined", "not_applicable")
RISK_CLASSES = ("none", "sandbox-only", "read-only", "prod-read", "write-control")
GRANT_KINDS = ("none", "secret", "url", "connection", "file")
CAPABILITY_CATEGORIES = (
    "identity", "email", "payments", "api", "observability",
    "data", "environment", "context", "custom",
)

# Baseline catalog (id, title, category, level, risk_class, grant_kind, unlocks,
# proposed_when). `proposed_when` is the heuristic tag the explorer matches.
def _c(cid, title, category, level, risk, kind, unlocks, proposed_when=None):
    return {
        "capability_id": cid, "title": title, "category": category,
        "level": level, "risk_class": risk, "grant_kind": kind,
        "unlocks": unlocks, "proposed_when": proposed_when, "baseline": True,
    }


CAPABILITY_CATALOG = [
    # Identity (L1)
    _c("test-account", "Test account login(s)", "identity", 1, "sandbox-only", "secret",
       "Get past auth and test real signed-in flows.", "login_form"),
    _c("sso-sandbox", "SSO sandbox app", "identity", 1, "sandbox-only", "connection",
       "Test federated / single sign-on login.", "sso"),
    _c("sandbox-tenant", "Sandbox org / workspace", "identity", 1, "sandbox-only", "connection",
       "A throwaway space to test in without touching real data."),
    _c("api-token", "API token", "identity", 1, "sandbox-only", "secret",
       "Authenticate API calls as a real user.", "api"),
    # Email & messaging (L2)
    _c("sandbox-inbox", "Sandbox email inbox", "email", 2, "sandbox-only", "connection",
       "Read verification codes, reset links, and transactional email.", "email"),
    _c("sandbox-sending", "Sandbox email sending", "email", 2, "sandbox-only", "connection",
       "Trigger and inspect outbound mail."),
    _c("sms-sandbox", "SMS / OTP sandbox", "email", 2, "sandbox-only", "secret",
       "Receive OTP / 2FA codes over SMS.", "otp"),
    _c("webhook-capture", "Webhook capture", "email", 2, "sandbox-only", "url",
       "Inspect outbound webhooks the site fires.", "webhook"),
    # Payments (L2)
    _c("payments-sandbox", "Payments sandbox + test cards", "payments", 2,
       "sandbox-only", "connection",
       "Run checkout with test cards — no real charges.", "payments"),
    _c("test-clock", "Billing test clock", "payments", 2, "sandbox-only", "connection",
       "Advance subscription/billing time to test lifecycle states.", "payments"),
    _c("entitlement-seed", "Entitlement seeding", "payments", 2, "sandbox-only", "url",
       "Seed coupons / plans / entitlements for tests."),
    # API & contracts (L2-3)
    _c("openapi-spec", "API schema (OpenAPI / GraphQL)", "api", 2, "read-only", "url",
       "Exercise and contract-test the API.", "api"),
    _c("api-access", "Direct API access", "api", 3, "prod-read", "secret",
       "Drive endpoints directly and cross-check the UI against the API.", "api"),
    # Observability (L3, read)
    _c("app-logs", "Application logs (read)", "observability", 3, "read-only", "secret",
       "Turn 'the page errored' into a stack trace + request id.", "health"),
    _c("error-tracking", "Error tracking (Sentry…)", "observability", 3, "read-only", "secret",
       "Attach real stack traces to findings."),
    _c("apm-metrics", "Metrics / APM (read)", "observability", 3, "read-only", "secret",
       "Catch performance regressions with real numbers.", "metrics"),
    _c("request-tracing", "Request tracing", "observability", 3, "read-only", "connection",
       "Correlate a persona action to the server-side trace."),
    # Data & state (L4, read)
    _c("readonly-db", "Read-only database", "data", 4, "prod-read", "secret",
       "Verify backend state and catch silent data corruption."),
    _c("admin-read-api", "Admin / read API", "data", 4, "prod-read", "secret",
       "Query domain objects to verify effects.", "admin"),
    _c("object-store-read", "Object storage (read)", "data", 4, "prod-read", "secret",
       "Verify uploads and generated files."),
    _c("search-index-read", "Search index (read)", "data", 4, "read-only", "secret",
       "Verify indexing and search results."),
    # Environment & control (L5, write-control)
    _c("kube-exec", "Kubernetes access (read + exec)", "environment", 5,
       "write-control", "connection",
       "Read pods/logs and exec for runtime inspection."),
    _c("feature-flags", "Feature flags", "environment", 5, "write-control", "secret",
       "Toggle flags to test variants and gated features."),
    _c("seed-reset", "Seed / reset endpoints", "environment", 5, "write-control", "url",
       "Set preconditions and reset state between runs."),
    _c("time-control", "Time control", "environment", 5, "write-control", "url",
       "Drive a test clock to test time-dependent behaviour."),
    _c("preview-deploys", "Preview deploys", "environment", 5, "write-control", "connection",
       "Test per-PR / preview environments."),
    # Context (L1, read — no creds, big payoff)
    _c("repo-read", "Source code (read)", "context", 1, "read-only", "connection",
       "Read code to learn intent → fewer by-design false flags."),
    _c("internal-docs", "Internal docs", "context", 1, "read-only", "connection",
       "Runbooks / specs the explorer reads for context."),
    _c("issue-tracker-read", "Issue tracker (read)", "context", 1, "read-only", "secret",
       "Known issues — so testers don't re-file them."),
    _c("product-analytics", "Product analytics", "context", 1, "read-only", "secret",
       "Which flows real users take → prioritise testing."),
]


# ── Catalog (global) ───────────────────────────────────────────────────────
def seed_capability_catalog(store: Store) -> int:
    """Upsert the baseline catalog. Idempotent (insert-only on the metadata so
    an operator's edits survive); returns how many rows it touched."""
    n = 0
    for entry in CAPABILITY_CATALOG:
        store.capability_catalog.update_one(
            {"capability_id": entry["capability_id"]},
            {"$setOnInsert": {**entry, "created_at": _now()}},
            upsert=True,
        )
        n += 1
    return n


def list_capabilities(store: Store) -> list[dict]:
    """Every catalog entry (baseline + custom), ordered by (level, category)."""
    cur = store.capability_catalog.find().sort(
        [("level", 1), ("category", 1), ("title", 1)],
    )
    return [_strip_id(c) for c in cur]


def get_capability(store: Store, capability_id: str) -> dict | None:
    return _strip_id(store.capability_catalog.find_one({"capability_id": capability_id}))


def upsert_capability(
    store: Store,
    *,
    capability_id: str,
    title: str,
    unlocks: str = "",
    category: str = "custom",
    level: int = 1,
    risk_class: str = "read-only",
    grant_kind: str = "connection",
) -> dict:
    """Add/replace a (usually custom) catalog entry — the open-tail escape hatch."""
    store.capability_catalog.update_one(
        {"capability_id": capability_id},
        {
            "$set": {
                "title": title, "unlocks": unlocks, "category": category,
                "level": int(level), "risk_class": risk_class,
                "grant_kind": grant_kind, "updated_at": _now(),
            },
            "$setOnInsert": {
                "capability_id": capability_id, "baseline": False,
                "proposed_when": None, "created_at": _now(),
            },
        },
        upsert=True,
    )
    return get_capability(store, capability_id)


# ── Grants (per tenant, target) ────────────────────────────────────────────
def list_site_capabilities(
    store: Store, tenant_id: str, target_id: str,
) -> list[dict]:
    cur = store.site_capabilities.find({"tenant_id": tenant_id, "target_id": target_id})
    return [_strip_id(g) for g in cur]


def set_capability_status(
    store: Store,
    *,
    target_id: str,
    capability_id: str,
    status: str,
    tenant_id: str = DEFAULT_TENANT,
    token: str | None = None,
    config: dict | None = None,
    proposed_by: str | None = None,
) -> dict:
    """Propose / grant / decline / mark-n-a a capability for a target. A secret
    ``token`` is vaulted and only its ``credential_ref`` pointer is kept on the
    grant. Returns the grant row (never the token)."""
    if status not in CAPABILITY_STATUSES:
        raise ValueError(
            f"unknown status {status!r}; expected one of {', '.join(CAPABILITY_STATUSES)}",
        )
    key = {"tenant_id": tenant_id, "target_id": target_id, "capability_id": capability_id}
    existing = store.site_capabilities.find_one(key) or {}
    credential_ref = existing.get("credential_ref")
    if token:
        credential_ref = put_secret(
            store, tenant_id=tenant_id, target_id=target_id, value=token,
            ref=f"cap-{capability_id}", label=f"{capability_id} credential",
        )
    now = _now()
    store.site_capabilities.update_one(
        key,
        {
            "$set": {
                "status": status,
                "credential_ref": credential_ref,
                "config": dict(config) if config is not None else existing.get("config", {}),
                "proposed_by": proposed_by or existing.get("proposed_by") or "operator",
                "updated_at": now,
            },
            "$setOnInsert": {**key, "created_at": now},
        },
        upsert=True,
    )
    return _strip_id(store.site_capabilities.find_one(key))


def get_capability_token(
    store: Store, tenant_id: str, target_id: str, capability_id: str,
) -> str | None:
    """The plaintext credential a granted capability points at (vault), or None.
    The single read path for the raw value — for the harness to use."""
    from qa_store.vault import get_secret  # noqa: PLC0415

    g = store.site_capabilities.find_one(
        {"tenant_id": tenant_id, "target_id": target_id, "capability_id": capability_id},
    )
    ref = (g or {}).get("credential_ref")
    return get_secret(store, ref) if ref else None


def delete_site_capability(
    store: Store, tenant_id: str, target_id: str, capability_id: str,
) -> bool:
    """Revoke a grant (and drop any vaulted credential)."""
    g = store.site_capabilities.find_one(
        {"tenant_id": tenant_id, "target_id": target_id, "capability_id": capability_id},
    )
    if g is None:
        return False
    if g.get("credential_ref"):
        delete_secret(store, g["credential_ref"])
    res = store.site_capabilities.delete_one(
        {"tenant_id": tenant_id, "target_id": target_id, "capability_id": capability_id},
    )
    return res.deleted_count == 1


# ── Depth score ────────────────────────────────────────────────────────────
def capability_depth(store: Store, tenant_id: str, target_id: str) -> dict:
    """The target's testing-depth roll-up: the highest granted rung, a label,
    a count, and the next capability worth granting (the CTA)."""
    catalog = {c["capability_id"]: c for c in list_capabilities(store)}
    grants = {g["capability_id"]: g for g in list_site_capabilities(store, tenant_id, target_id)}

    granted_levels = [
        catalog[cid]["level"]
        for cid, g in grants.items()
        if g["status"] == "granted" and cid in catalog
    ]
    depth_level = max(granted_levels) if granted_levels else 0

    def _open(cid: str) -> bool:
        return grants.get(cid, {}).get("status") not in ("granted", "declined", "not_applicable")

    candidates = [
        c for c in catalog.values() if c.get("category") != "custom" and _open(c["capability_id"])
    ]
    above = sorted(
        (c for c in candidates if c["level"] > depth_level),
        key=lambda c: (c["level"], c["title"]),
    )
    fallback = sorted(candidates, key=lambda c: (c["level"], c["title"]))
    nxt = above[0] if above else (fallback[0] if fallback else None)

    return {
        "depth_level": depth_level,
        "depth_label": DEPTH_LEVELS[depth_level],
        "levels": list(DEPTH_LEVELS),
        "granted_count": sum(1 for g in grants.values() if g["status"] == "granted"),
        "next_unlock": (
            {
                "capability_id": nxt["capability_id"], "title": nxt["title"],
                "unlocks": nxt["unlocks"], "level": nxt["level"],
            }
            if nxt else None
        ),
    }
