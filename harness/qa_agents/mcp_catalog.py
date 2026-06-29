"""MCP server catalog — the source-of-truth for which servers Test Ease
has wired (Slice B of #1028).

This module is a *static* catalog. Editing the catalog requires a deploy
because each entry represents a Python integration that lives in the
harness image. When the catalog grows beyond ~10 entries OR operators
need enable/disable without deploys, migrate to a DB-backed
``qa_mcp_servers`` collection — same pattern as ``qa_personas`` (see
#1028 acceptance criteria).

Why a separate module instead of inline in :mod:`qa_agents.runner`:
the review-ui API needs to read this catalog at request time to serve
``GET /api/mcp-servers``. The harness Dockerfile uses
``pip install --no-deps -e ../harness`` to pull just the lightweight
modules (``qa_agents.personas`` was the first; this is the second) —
both must stay free of heavy SDK imports so the API container can
import them without inheriting Anthropic / Playwright / MCP-server
dependencies.

Slice C (#1031) will read this catalog to know which servers to gate
when ``QA_ENABLED_MCPS`` is set — the only env-aware code is in
:mod:`qa_agents.runner`, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MCPServer:
    """One entry in the MCP server catalog.

    ``id`` is the short slug used in tool prefixes (``mcp__<id>__*``)
    and in :func:`qa_store.summarise_mcp_servers_used`'s ``server``
    field. It is the canonical identity.

    ``display_name`` is what the operator sees in the catalog UI and
    on Slice A's chip list once Slice A picks up the catalog
    (currently rendering raw ids).

    ``description`` is a one-line operator-facing summary. Aim for the
    "what would I write on a sticky note?" voice — concrete capability,
    not marketing copy.

    ``default_enabled`` is the default state on the trigger-form
    checkbox list (Slice C). True = on by default; False = off.
    Reflects "is this server cheap/safe enough to always have on?".

    ``persona_compat`` lists persona ids where this server is
    *meaningful*. An empty list means "all personas" (the default for
    universally-applicable servers like Playwright). The trigger UI
    can use this to grey out an irrelevant server when the operator
    picks specific personas.

    ``tool_count`` is the number of tools the server exposes. Used in
    the catalog UI as a hint of capability surface area.

    ``unlocked_by`` lists the ``capability_id``s (see
    :mod:`qa_store.capabilities`) that, when *granted* for a target,
    light this server up for that target's runs — the realisation of
    the capabilities docstring's "a granted capability lights up an MCP
    tool". Empty = not gated on any capability (availability follows the
    ``default_enabled`` / ``persona_compat`` rules alone). See
    :func:`server_ids_for_capabilities`.
    """

    id: str
    display_name: str
    description: str
    default_enabled: bool = True
    persona_compat: list[str] = field(default_factory=list)
    tool_count: int = 0
    unlocked_by: tuple[str, ...] = ()
    # A plain, benefit-oriented name for non-technical operators (the
    # ``display_name`` is deliberately developer-facing — "Mailpit", "OpenAPI").
    # Surfaced wherever a capability advertises the tool it lights up. Falls
    # back to ``display_name`` via :meth:`friendly` when unset.
    friendly_name: str = ""

    @property
    def friendly(self) -> str:
        return self.friendly_name or self.display_name


# ---------------------------------------------------------------------------
# Catalog — the three servers Test Ease ships with today (#1010 baseline).
# #1019 child PRs (#1020..#1026) will append new entries as integrations
# land. Order is "most-used first" — controls the default sort on the
# catalog UI before any operator filter.
# ---------------------------------------------------------------------------
CATALOG: tuple[MCPServer, ...] = (
    MCPServer(
        id="playwright",
        display_name="Playwright (browser automation)",
        description=(
            "The persona's eyes and hands in the browser. Navigate, click, "
            "type, take screenshots, run a11y snapshots. Used by every "
            "persona that interacts with a web UI."
        ),
        default_enabled=True,
        persona_compat=[],
        tool_count=22,
    ),
    MCPServer(
        id="email",
        display_name="Email (Mailpit read + SMTP send)",
        description=(
            "Read the tenant's dev inbox (Mailpit API today; IMAP/Mailosaur "
            "later per the agnostic-tenant epic) to grab verification "
            "links; send mail FROM the persona's address to test "
            "email-receiving features."
        ),
        default_enabled=True,
        persona_compat=[],
        tool_count=3,
        unlocked_by=("sandbox-inbox", "sandbox-sending"),
        friendly_name="email inbox tool",
    ),
    MCPServer(
        id="findings",
        display_name="Findings recorder",
        description=(
            "The persona's mechanism for filing observations during the "
            "run — categorised + severity-tagged, stored in "
            "qa_findings. Without this the persona has nowhere to record "
            "what they saw."
        ),
        default_enabled=True,
        persona_compat=[],
        tool_count=1,
    ),
    MCPServer(
        id="identity",
        display_name="Faker (per-run persona identity)",
        description=(
            "Generates a locale-appropriate name, email, phone and "
            "address per run so signup-shaped personas don't reuse a "
            "hardcoded email (which collides cross-run) or English "
            "placeholders on a Japanese tenant. The generated email "
            "reuses the persona's inbox domain so wait_for_email still "
            "sees verification mail."
        ),
        default_enabled=True,
        persona_compat=[],
        tool_count=1,
    ),
    MCPServer(
        id="openapi",
        display_name="OpenAPI surface explorer (API surface discovery)",
        description=(
            "In-process server exposing list_endpoints / get_endpoint / "
            "search tools over the tenant's OpenAPI spec so the api-poker "
            "persona can read the API surface (including endpoints not "
            "surfaced in the UI) and then probe them. default_enabled=False "
            "because only the api-poker persona uses it. The spec URL is "
            "supplied per-run via QA_OPENAPI_URL, or derived from "
            "{web_base_url}/openapi.json for FastAPI tenants."
        ),
        default_enabled=False,
        persona_compat=["api-poker"],
        tool_count=3,
        unlocked_by=("openapi-spec", "api-access"),
        friendly_name="API probing tool",
    ),
    MCPServer(
        id="chrome_devtools",
        display_name="Chrome DevTools (perf throttling + traces)",
        description=(
            "Real network + CPU throttling and Lighthouse-style trace "
            "capture via the Chrome DevTools Protocol. Used by the "
            "slow-connection and perf-budget-evaluator personas. "
            "default_enabled=False because it launches its own Chrome "
            "instance separate from Playwright's — only opt in for "
            "perf-focused runs."
        ),
        default_enabled=False,
        persona_compat=["slow-connection", "perf-budget-evaluator"],
        tool_count=5,
    ),
    MCPServer(
        id="a11y",
        display_name="Axe / a11y-mcp (WCAG audits)",
        description=(
            "Runs axe-core against a target URL and returns a list of "
            "WCAG violations with rule id, severity, selector, and "
            "criterion. Used by keyboard-only (Iris) and screen-reader "
            "(Solomon) personas to produce citation-grade findings "
            "instead of qualitative ones. Cheap enough to leave "
            "default-enabled — the audit runs in a short-lived "
            "headless browser inside the MCP server."
        ),
        default_enabled=True,
        persona_compat=["keyboard-only", "screen-reader"],
        tool_count=2,
    ),
    MCPServer(
        id="loadgen",
        display_name="Load generator (bulk real-pipeline send)",
        description=(
            "Fires a BATCH of emails at one UID address through the real "
            "inbound pipeline (the same SMTP send path as the email tool), "
            "so volume actually reaches process_inbound, costs real provider "
            "money, and counts against fair-use. The internal-load-economist "
            "persona's primary volume lever — a browser alone can't generate "
            "real load. default_enabled=False and insider-only."
        ),
        default_enabled=False,
        persona_compat=["internal-load-economist"],
        tool_count=1,
    ),
    MCPServer(
        id="cost",
        display_name="Cost reader (SlyReply admin cost API)",
        description=(
            "Reads SlyReply's OWN cost/usage data — GET /api/admin/costs "
            "(per-provider / per-model / per-agent spend, MTD, and the "
            "internal-vs-external reconciliation) and GET /api/usage/summary "
            "(fair-use standing). Admin-only; authenticates with the "
            "QA_ADMIN_TOKEN bearer. Used by the internal-load-economist "
            "persona to judge unit economics. default_enabled=False."
        ),
        default_enabled=False,
        persona_compat=["internal-load-economist"],
        tool_count=2,
    ),
    MCPServer(
        id="openai_billing",
        display_name="OpenAI billing (org usage/costs API)",
        description=(
            "Reads OpenAI's organization Costs + Usage API (requires an "
            "sk-admin-* key in QA_OPENAI_ADMIN_KEY) — an INDEPENDENT external "
            "read to cross-check SlyReply's internal cost estimate. Used by "
            "the internal-load-economist persona. OpenAI's billing API lags "
            "and is coarse; treat it as a sanity check, not ground truth. "
            "default_enabled=False."
        ),
        default_enabled=False,
        persona_compat=["internal-load-economist"],
        tool_count=2,
    ),
)


def list_servers() -> list[MCPServer]:
    """Return every catalog entry as a list (for API serialisation)."""
    return list(CATALOG)


def get_server(server_id: str) -> MCPServer:
    """Look up one server by id. Raises ``KeyError`` with a clear
    message listing valid ids, mirroring the
    :func:`qa_agents.personas.get_persona` contract."""
    for entry in CATALOG:
        if entry.id == server_id:
            return entry
    valid = ", ".join(s.id for s in CATALOG) or "(none)"
    raise KeyError(f"Unknown MCP server {server_id!r}. Available: {valid}.")


def server_ids() -> tuple[str, ...]:
    """Just the ids — useful for env-var validation in Slice C."""
    return tuple(s.id for s in CATALOG)


# ---------------------------------------------------------------------------
# Capability → MCP wiring (the "granted capability lights up a tool" bridge).
#
# ``CAPABILITY_ENV`` maps a granted ``capability_id`` to the harness env var its
# vaulted credential populates. The harness ALREADY reads these env vars into
# its Config and the build_*_server factories (qa_agents/config.py +
# qa_agents/runner.py), so injecting the vaulted value as the matching env var
# is all it takes to feed a capability's credential to its server — no runner
# change needed. Only capabilities whose credential is a concrete URL/token the
# server consumes appear here; a capability can unlock a server (``unlocked_by``)
# without supplying an env credential (e.g. a sandbox the harness derives).
# ---------------------------------------------------------------------------
CAPABILITY_ENV: dict[str, str] = {
    "openapi-spec": "QA_OPENAPI_URL",
    "sandbox-inbox": "QA_MAILPIT_URL",
}


def server_ids_for_capabilities(capability_ids: object) -> tuple[str, ...]:
    """The catalog server ids unlocked by the given granted ``capability_ids``
    (any iterable of strings), in catalog order, de-duplicated."""
    granted = set(capability_ids or ())
    return tuple(
        s.id for s in CATALOG if granted.intersection(s.unlocked_by)
    )


def servers_unlocked_by(capability_id: str) -> tuple[MCPServer, ...]:
    """Every catalog server a single capability lights up (for the 'Powers …'
    UI on a capability card)."""
    return tuple(s for s in CATALOG if capability_id in s.unlocked_by)
