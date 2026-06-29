"""Resolve a target's GRANTED capabilities into the MCP servers + credentials
its runs should use — the "a granted capability lights up an MCP tool" bridge.

Reads the per-``(tenant, target)`` ``site_capabilities`` grants, maps each
granted capability to the catalog server(s) it unlocks (``unlocked_by`` in
:mod:`qa_agents.mcp_catalog`), and resolves the vaulted credential for the
capabilities that feed a server env var (``CAPABILITY_ENV``). The harness
already reads those env vars (``QA_OPENAPI_URL`` / ``QA_MAILPIT_URL`` …) into its
Config and the ``build_*_server`` factories, so injecting the vaulted value as
the matching env var is all it takes — no runner change.

Import-light on purpose (only :mod:`qa_store` + the catalog, no SDK/runner) so
the review-ui API container — which installs the harness ``--no-deps`` — can
import it at request time, exactly like :mod:`qa_agents.site_knowledge`.

Graceful: any error (storeless / unreachable / unexpected shape) yields the
empty result so a run/trigger proceeds exactly as before.
"""

from __future__ import annotations

import logging

from .mcp_catalog import (
    CAPABILITY_ENV,
    get_server,
    server_ids_for_capabilities,
)

log = logging.getLogger(__name__)


def _empty() -> dict:
    return {"server_ids": [], "env": {}, "servers": []}


def resolve_target_mcp(
    store: object, target_id: str, *, tenant_id: str | None = None,
) -> dict:
    """Resolve the MCP wiring a target has *earned* through granted capabilities.

    Returns ``{"server_ids": [...], "env": {VAR: value}, "servers": [...]}``:

    - ``server_ids`` — catalog server ids unlocked by the granted capabilities
      (catalog order, de-duplicated); union these into the run's enabled set.
    - ``env`` — resolved (raw) credentials keyed by the harness env var they
      populate; inject these into the run container. **Never** surface to the UI.
    - ``servers`` — display metadata ``{server_id, display_name, capabilities}``
      (no secrets) for "powered by" UI.
    """
    try:
        from qa_store.capabilities import (  # noqa: PLC0415
            get_capability_token,
            list_site_capabilities,
        )
        from qa_store.schema import DEFAULT_TENANT  # noqa: PLC0415

        tenant = tenant_id or DEFAULT_TENANT
        grants = list_site_capabilities(store, tenant, target_id)
    except Exception:  # noqa: BLE001 — never break a run/trigger over enrichment
        log.warning("target MCP resolution failed for %r", target_id, exc_info=True)
        return _empty()

    granted = sorted(
        g["capability_id"] for g in grants if g.get("status") == "granted"
    )
    if not granted:
        return _empty()

    granted_set = set(granted)
    server_ids = list(server_ids_for_capabilities(granted))

    # Per-server display metadata: which granted capabilities power each server.
    servers: list[dict] = []
    for sid in server_ids:
        srv = get_server(sid)
        caps = sorted(granted_set.intersection(srv.unlocked_by))
        servers.append({
            "server_id": sid,
            "display_name": srv.display_name,
            "friendly_name": srv.friendly,
            "capabilities": caps,
        })

    # Resolve the vaulted credential for the capabilities that feed a server env.
    env: dict[str, str] = {}
    for cid in granted:
        env_var = CAPABILITY_ENV.get(cid)
        if not env_var:
            continue
        try:
            token = get_capability_token(store, tenant, target_id, cid)
        except Exception:  # noqa: BLE001 — a single bad cred shouldn't drop the rest
            log.warning("could not resolve credential for capability %r", cid, exc_info=True)
            token = None
        if token:
            env[env_var] = token

    return {"server_ids": server_ids, "env": env, "servers": servers}
