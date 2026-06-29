"""The FastAPI application for the Persona QA review UI.

JSON endpoints under ``/api`` plus a static-file mount that serves the built
Vue SPA at ``/``:

* ``GET   /api/runs``                     — runs newest-first, with finding counts.
* ``GET   /api/runs/personas``            — the persona ids a run can be scoped to.
* ``GET   /api/runs/active``              — the harness run in progress, if any.
* ``POST  /api/runs/trigger``             — start a harness run for chosen personas.
* ``GET   /api/runs/active/logs``         — SSE stream of the active run's logs.
* ``GET   /api/runs/{run_id}``            — one run: reviews + findings.
* ``PATCH /api/findings/{finding_id}``    — set a finding's triage status.
* ``POST  /api/runs/{run_id}/file-issue`` — compose + create ONE GitHub issue.

The store handle is created once at startup and shared. ``qa-store`` is pymongo
only, so this app stays light — no Agent SDK in the review-ui image. Run
control reaches the cluster lazily (see ``runs.py``).
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from qa_store import (
    CATALOG,
    CATEGORIES,
    DISCOVERED_ACTION_CATEGORIES,
    FINDING_STATUSES,
    clear_persona_credentials,
    connect,
    create_persona,
    create_scenario,
    delete_persona,
    delete_scenario,
    ensure_vector_indexes,
    fetch_screenshot,
    get_finding,
    get_persona,
    get_persona_credentials_status,
    get_run,
    get_scenario,
    list_admin_wipes,
    list_discovered_actions,
    list_discovered_branches,
    list_discovered_tools,
    list_personas,
    list_run_logs_for_persona,
    list_runs,
    list_scenarios,
    list_steps_for_persona,
    mark_finding_filed,
    mark_run_filed,
    record_admin_wipe,
    search_run_logs,
    seed_default_personas,
    set_finding_status,
    update_finding_gh_state,
    update_persona,
    update_scenario,
    wipe_for_relaunch,
)

# Site Model DAOs live in qa_store.site_model (not re-exported from the package
# root). DEFAULT_TENANT scopes every read/write — single-tenant for now.
from qa_store.app_config import (
    LLM_BACKEND_ENV,
    LLM_BACKENDS,
    clear_llm_token,
    get_llm_config,
    get_llm_token,
    set_llm_config,
)
from qa_store.capabilities import (
    CAPABILITY_STATUSES,
    capability_depth,
    delete_site_capability,
    get_capability,
    list_capabilities,
    list_site_capabilities,
    seed_capability_catalog,
    set_capability_status,
    upsert_capability,
)
from qa_store.embeddings import embedding_dim_for
from qa_store.schema import DEFAULT_TENANT, SITE_QUESTION_KINDS
from qa_store.site_model import (
    LIFECYCLE_STATES,
    delete_site_knowledge,
    get_site_knowledge,
    get_site_target,
    list_flows_by_target,
    list_knowledge_by_target,
    list_site_targets,
    list_surfaces_by_target,
    set_target_lifecycle,
    upsert_site_knowledge,
    upsert_site_target,
)
from qa_store.site_questions import (
    answer_site_question,
    delete_site_question,
    get_site_question,
    list_questions_by_target,
    questionnaire_status,
    skip_site_question,
    upsert_site_question,
)

from .issue import (
    compose_finding_issue,
    compose_issue,
    create_github_issue,
    create_github_issue_full,
    fetch_github_issue_state,
)
from .runs import (
    KNOWN_PERSONAS,
    MAX_SIMULTANEOUS_PERSONAS,
    ClusterUnavailable,
    K8sRunControl,
    RunAlreadyActive,
    RunControlError,
    RunLimitExceeded,
)
from .settings import Settings

# Where the built Vue SPA lands. The Dockerfile copies web/dist here; in local
# dev it may be absent (run the SPA via `npm run dev` instead) — handled below.
_SPA_DIR = os.environ.get(
    "QA_REVIEW_SPA_DIR",
    os.path.join(os.path.dirname(__file__), "static"),
)


# ---------------------------------------------------------------------------
# Request models.
# ---------------------------------------------------------------------------
class FindingStatusUpdate(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# #1146 — nuclear-button admin wipe request.
#
# The endpoint requires a literal ``confirm="WIPE"`` token in the body
# — typed by the operator on the /admin page — as a deliberate friction
# layer. A clicked button without a typed confirmation can't drop the
# slyreply_qa database by accident.
#
# ``requester_note`` is the operator-typed reason ("Validating Slice 3",
# "Hetzner cutover prep"). Persisted to qa_admin_audit so the /admin
# "Recent wipes" list can attribute resets months later.
# ---------------------------------------------------------------------------
class AdminWipeRequest(BaseModel):
    confirm: str = Field(..., min_length=1, max_length=16)
    requester_note: str = Field(default="", max_length=200)
    # #1108 — opt-in Mailpit content wipe. False by default because
    # Mailpit's PVC carries cross-run persona inbox history (the
    # "Maya forwards last week's verification email" lifecycle case
    # in #1104). When the operator wants a TRUE full-reset they tick
    # the modal checkbox; the API hits Mailpit's admin DELETE
    # /api/v1/messages over the cross-namespace Service URL. The PVC
    # itself is never deleted by this endpoint — only its contents.
    wipe_mailpit: bool = False


# ---------------------------------------------------------------------------
# #862 — saved scenarios request models.
#
# A scenario is a named {persona + mandatory-action-ids} preset. The
# bounds here mirror the trigger-page constraints (≤50 mandatory ids,
# valid catalog ids), and the id-pattern is a strict lowercase slug so
# the URL path stays stable + obviously not a free-text field. Validation
# of persona_id-in-KNOWN_PERSONAS and action-ids-in-CATALOG happens at
# the endpoint level (the create / update handlers do the lookup) rather
# than in the pydantic schema, because both lists are imported at module
# scope and a pydantic validator would duplicate the import dance.
# ---------------------------------------------------------------------------
_SCENARIO_ID_PATTERN = r"^[a-z][a-z0-9-]*$"

# Plain-language stand-in for the raw kube-config error when this deployment
# can't reach a cluster to dispatch runs (e.g. the local-first docker stack).
# Everything else in the control room works without a cluster.
_RUNS_UNAVAILABLE_MSG = (
    "Persona runs execute on a Kubernetes cluster, which this deployment can't "
    "reach. Exploring sites, the Site Model, capabilities, and the rest of the "
    "control room all work without it — but launching a run needs a cluster "
    "(or run the harness directly with `docker compose --profile run`)."
)


class ScenarioCreateRequest(BaseModel):
    """Body for ``POST /api/scenarios``."""

    id: str = Field(min_length=1, max_length=64, pattern=_SCENARIO_ID_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    persona_id: str = Field(min_length=1, max_length=32)
    mandatory_action_ids: list[str] = Field(default_factory=list, max_length=50)


class ScenarioUpdateRequest(BaseModel):
    """Body for ``PATCH /api/scenarios/{id}``. Every field optional.

    A field omitted from the request body means "leave this alone". An
    explicit empty list for ``mandatory_action_ids`` clears the field
    (distinct from omission — see qa_store.update_scenario for the
    same contract).
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    persona_id: str | None = Field(default=None, min_length=1, max_length=32)
    mandatory_action_ids: list[str] | None = Field(
        default=None, max_length=50,
    )


class PersonaCreateRequest(BaseModel):
    """Body for ``POST /api/personas`` — create a new (non-default) persona."""

    persona_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    display_name: str = Field(min_length=1, max_length=120)
    registered_email: str = Field(min_length=1, max_length=254)
    explore_system_prompt: str = Field(min_length=1, max_length=20000)
    report_system_prompt: str = Field(min_length=1, max_length=20000)
    flows: list[str] = Field(default_factory=list)
    uses_admin_login: bool = False
    setup_actions: str | None = Field(default=None, max_length=4000)
    browser_locale: str | None = Field(default=None, max_length=16)
    color_token: str = Field(default="slate", max_length=32)
    avatar_seed: str | None = None


class PersonaUpdateRequest(BaseModel):
    """Body for ``PATCH /api/personas/{persona_id}``. All fields optional."""

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    archetype: str | None = Field(default=None, max_length=120)
    registered_email: str | None = Field(default=None, min_length=1, max_length=254)
    explore_system_prompt: str | None = Field(default=None, min_length=1, max_length=20000)
    report_system_prompt: str | None = Field(default=None, min_length=1, max_length=20000)
    flows: list[str] | None = None
    uses_admin_login: bool | None = None
    setup_actions: str | None = None
    # #1009 — region + language replace browser_locale on new clients but
    # browser_locale is still accepted for back-compat. The Persona
    # dataclass derives browser_locale from region + language at run
    # time, so writing one OR the other is fine.
    region: str | None = Field(default=None, max_length=8)
    language: str | None = Field(default=None, max_length=8)
    browser_locale: str | None = None
    color_token: str | None = Field(default=None, max_length=32)
    avatar_seed: str | None = None
    hidden: bool | None = None
    # #1009 — the activation toggle. is_active controls whether this
    # persona shows up in trigger-time default runs. Operators flip this
    # from the Personas page to enable/disable per-tenant.
    is_active: bool | None = None


class TriggerRunRequest(BaseModel):
    """Body for ``POST /api/runs/trigger``. Empty ``personas`` = every persona."""

    personas: list[str] = []
    # Optional per-trigger override of QA_HARNESS_CONCURRENCY (#824). When
    # omitted, the harness uses the pod-spec default. Capped at 6: the pod's
    # resource limits are sized for ~4 concurrent personas with realistic-peak
    # headroom to 6, so accepting higher values from the UI would silently OOM
    # the pod — raising the ceiling needs a deliberate code + pod-limits
    # change, not a number typed into a form. 1 = fully sequential, which is
    # a legitimate debugging mode.
    concurrency: int | None = Field(default=2, ge=1, le=6)
    # #1821 — number of harness pods to fan the run across. ``pod_count is
    # None`` (the default — the field omitted from the body) reproduces today's
    # single-pod Job byte-for-byte: the endpoint forwards ``pod_count=1`` so a
    # single plain Job is created, exactly the pre-#1821 shape. An explicit
    # value >1 fans the run out as N SEPARATE labelled Jobs (Option B), one per
    # pod, each handed an explicit ``--personas`` slice of the roster so the
    # personas shard across pods. Bounds 1..4: the
    # cost ceiling (pod_count × concurrency ≤ 8 simultaneous personas, mirrored
    # at this API layer as a 422 below AND defended in K8sRunControl.trigger)
    # is the real budget guard; the 1..4 rail just keeps the fan-out within the
    # sandbox node's pod headroom.
    pod_count: int | None = Field(default=None, ge=1, le=4)
    # Optional per-trigger overrides of QA_EXPLORE_MODEL / QA_REPORT_MODEL
    # (#836). The allowlist is the three model ids in active use across the
    # project (see infra/main.tf defaults and harness/qa_agents/config.py) —
    # anything else is rejected with 422, so an operator typo or a model id
    # the harness doesn't know about can't slip through. Letting
    # the explore phase drop to Haiku is the main lever here: the 2026-05-23
    # qa-20260523T100324Z run was $8.24 for two personas and 93% of that was
    # Sonnet explore tokens.
    explore_model: str | None = Field(
        default=None,
        pattern=r"^(claude-haiku-4-5|claude-sonnet-4-6|claude-opus-4-7)$",
    )
    report_model: str | None = Field(
        default=None,
        pattern=r"^(claude-haiku-4-5|claude-sonnet-4-6|claude-opus-4-7)$",
    )
    # Optional per-trigger override of QA_MAX_TURNS (#858, ceiling lifted
    # in #1115). The harness default is 200 (qa_agents/config.py); bounds
    # 10..5000 are sanity rails — under 10 makes the explore phase
    # pointless, over 5000 has no realistic use case (the wall-clock
    # run_duration_s below caps a run long before 5000 turns is reachable).
    # A sniff test wants 20-30; a deep regression run bumps to 1000+. The
    # pre-#1115 cap was 400 and a few personas (notably the attachment-
    # aggressor and image-aggressor) want to push further.
    max_turns: int | None = Field(default=None, ge=10, le=5000)
    # #1115 — wall-clock budget for the whole run, in seconds. The harness
    # default is 7200 (2 h, qa_agents/config.py:207). Bounds 300..7200:
    # 300s (5 min) is the floor for a useful explore phase (below that
    # the persona barely makes it past signup); 7200s is the ceiling
    # because the K8s Job's activeDeadlineSeconds is 12h and we want
    # headroom for the report phase + the next persona in the queue.
    run_duration_s: int | None = Field(default=None, ge=300, le=7200)
    # Free-text "what was this run about" label, persisted on the run doc
    # so a future operator can answer "why did we kick off run X?" months
    # later without grepping logs. 500-char cap is enough for a sentence
    # plus a GitHub issue reference; longer would invite an essay nobody
    # reads.
    run_notes: str | None = Field(default=None, max_length=500)
    # Operator-selected mandatory coverage-action ids (#861, slice 4 of
    # epic #857). Each id must be present in qa_store.CATALOG; unknown
    # ids 422 below before the K8s Job is ever created (the harness
    # would tolerate them with a warning, but failing fast at the API
    # gives the operator a clean error in the UI instead of a silent
    # drop they only notice when reviewing the run later). Capped at 50:
    # more than 50 mandatory actions per run is operator misuse — the
    # whole point of mandatory items is "MUST attempt", and 50 already
    # eats most of a persona's max_turns budget. Empty list = pure
    # free-rein run (the original behaviour, unchanged).
    mandatory_action_ids: list[str] = Field(default_factory=list, max_length=50)
    # #1018 — per-trigger override of QA_WEB_BASE_URL (Slice 1 of #1006,
    # the agnostic-tenant epic). Pre-#1018 the CronJob template hardcoded
    # the SlyReply sandbox at http://frontend so every run pointed at the
    # same site; that was the dominant blocker to using Test Ease against
    # any other tenant. When omitted, the harness keeps using whatever
    # QA_WEB_BASE_URL the pod-spec template ships (today: the in-cluster
    # sandbox). When set, it must be an absolute http(s) URL; we don't
    # accept paths or scheme-relative URLs because the harness uses this
    # as the .format() value for {base_url} in every persona prompt and a
    # malformed value would corrupt the prompt without any visible error.
    # Max length 500 — same rationale as run_notes; longer is operator
    # misuse and a URL that long is almost always a logged-in deep-link
    # to a specific session rather than the site's entry point.
    target_url: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^https?://[^\s]+$",
    )
    # #1031 — Slice C of the MCP visibility epic. Per-run MCP server
    # selection. Empty list / None = "use catalog defaults" (every
    # default_enabled=True server in qa_agents.mcp_catalog); a non-empty
    # list is the exact opt-in. Validated against the catalog below
    # (422 with a clear detail on unknown ids) so an operator typo
    # doesn't reach the harness as a stale env value. Capped at 20 —
    # the catalog is curated and the maximum reasonable selection is
    # the full catalog; longer would mean a bug or operator misuse.
    enabled_mcp_servers: list[str] | None = Field(default=None, max_length=20)
    # P4 — the registered target this run is for (the New Run form sends it once
    # it defaults the URL from a site). When present, the run auto-enables the
    # MCP servers that target has *granted* capabilities for, and injects their
    # vaulted credentials as env. Omitted ⇒ a plain URL-only run, unchanged.
    target_id: str | None = Field(default=None, max_length=200)


# ---------------------------------------------------------------------------
# Site Model — request models + response shaping.
# ---------------------------------------------------------------------------
_SITE_KNOWLEDGE_KINDS = ("by_design", "known_issue", "guidance", "glossary")

# Vector fields the UI never needs — populated by the reconciler (#2100) and
# hundreds of floats each. Stripped from every response (mirrors the #2094
# admin_sherman projection so we don't ship vectors to the browser).
_SITE_EMBED_FIELDS = (
    "body_embedding", "embedded_body_sha",
    "description_embedding", "embedded_sha",
)


def _strip_site_embeddings(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in _SITE_EMBED_FIELDS}


class SiteKnowledgeCreate(BaseModel):
    body: str = Field(min_length=1, max_length=20_000)
    kind: str = "by_design"
    applies_to: list[str] = Field(default_factory=list)
    # Optional stable id; generated when omitted so the operator can just type
    # a body and save.
    entry_id: str | None = Field(default=None, max_length=200)


class SiteKnowledgePatch(BaseModel):
    # The (tenant, target, entry_id) key needs the target; the PATCH path only
    # carries entry_id, so the editor sends target_id in the body.
    target_id: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=20_000)
    kind: str | None = None
    applies_to: list[str] | None = None


# ── Explorer questionnaire (site_questions) + target lifecycle ──────────────
class QuestionCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4_000)
    kind: str = "free_text"
    category: str = Field(default="general", max_length=100)
    rationale: str = Field(default="", max_length=4_000)
    options: list[str] = Field(default_factory=list)
    required: bool = False
    order: int = 0
    # Optional stable slug; generated when omitted.
    question_id: str | None = Field(default=None, max_length=200)


class QuestionAnswer(BaseModel):
    # For a ``secret`` question the value is vaulted server-side and never
    # echoed back; for every other kind it's stored inline.
    answer: str = Field(min_length=1, max_length=20_000)
    label: str = Field(default="", max_length=200)


class LifecycleSet(BaseModel):
    lifecycle: str = Field(min_length=1, max_length=50)


class TargetCreate(BaseModel):
    # Operator registers a site to test. base_url is the only required field;
    # target_id is slugified from the host/display_name when omitted.
    base_url: str = Field(min_length=1, max_length=2_000)
    display_name: str = Field(default="", max_length=200)
    target_id: str | None = Field(default=None, max_length=200)


class LLMConfigSet(BaseModel):
    # BYOK: the backend choice + (optionally) a new token. The token is vaulted
    # server-side and never returned; omit it to change only the backend.
    backend: str = Field(min_length=1, max_length=50)
    token: str | None = Field(default=None, max_length=4_000)


class CapabilityGrant(BaseModel):
    # status: granted / declined / not_applicable / proposed. A `token` (secret
    # credential) is vaulted server-side and never echoed; `config` is non-secret.
    status: str = Field(min_length=1, max_length=30)
    token: str | None = Field(default=None, max_length=8_000)
    config: dict | None = None


class CustomCapability(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    unlocks: str = Field(default="", max_length=2_000)
    level: int = Field(default=1, ge=0, le=5)
    grant_kind: str = "connection"
    token: str | None = Field(default=None, max_length=8_000)
    config: dict | None = None


_LLM_BACKEND_META = {
    "claude-code": {
        "label": "Claude Code subscription (flat price)",
        "hint": "Runs on your Claude Code Max OAuth token — mint one with `claude setup-token`.",
        "recommended": True,
    },
    "api": {
        "label": "Anthropic API key (per-token billing)",
        "hint": "Uses an ANTHROPIC_API_KEY; billed per token.",
        "recommended": False,
    },
}


def _slugify(text: str) -> str:
    """Lowercase, collapse non-alphanumerics to hyphens, trim. Empty → ''."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _target_host(base_url: str) -> str:
    """Best-effort host slug from a URL, for a default target_id."""
    from urllib.parse import urlparse  # noqa: PLC0415 — local, cheap

    host = urlparse(base_url).hostname or ""
    # Drop a leading www. and the trailing TLD label so https://www.acme.com
    # → "acme" (a friendly slug; display_name carries the real name).
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    if len(parts) >= 2:
        host = ".".join(parts[:-1])
    return _slugify(host)


# ---------------------------------------------------------------------------
# App factory.
# ---------------------------------------------------------------------------
def create_app(
    settings: Settings | None = None,
    store=None,
    run_control=None,
    *,
    seed_personas: bool = True,
) -> FastAPI:
    """Build the FastAPI app.

    ``settings`` / ``store`` / ``run_control`` are injectable so tests can pass
    a mongomock-backed store, a fixed config and a fake run controller; the
    production ``create_app()`` resolves all three from the environment.
    """
    settings = settings or Settings.from_env()
    if store is None:
        store = connect(settings.qa_store_url, settings.qa_store_db)
    if run_control is None:
        run_control = K8sRunControl(
            namespace=settings.sandbox_namespace,
            cronjob_name=settings.qa_cronjob_name,
        )

    app = FastAPI(
        title="Test Ease",
        description=(
            "Persona QA workbench — manage personas, build scenarios, watch "
            "live workflow timelines, triage findings, and file GitHub issues. "
            "Originally built for SlyReply; tracked for spinout as a "
            "standalone product.\n\n"
            "## Authentication\n\n"
            "**No application-level auth.** The API is intentionally "
            "unauthenticated at the HTTP layer; access is gated by network "
            "policy:\n\n"
            "- In-cluster: the testease Service is reachable only from "
            "the `slyreply-qa` namespace by default; egress is blocked by "
            "NetworkPolicy.\n"
            "- Public origin (testease.slyreply.ai): protected by "
            "Cloudflare WAF + Tailscale-tunnel restrictions per the "
            "infra Terraform.\n"
            "- The harness pod authenticates to GitHub via a separate "
            "service-account token (see `qa-claude-code-credentials`); "
            "that token is **never** read from inbound requests.\n\n"
            "If you fork this API for a multi-tenant deployment, you "
            "need to add auth here (e.g. a `Depends(verify_token)` on "
            "every route) — the unauthenticated default is correct only "
            "while a single-tenant trust boundary exists outside the app."
        ),
        version="0.3.0",
        openapi_tags=[
            {
                "name": "Runs",
                "description": "Trigger, list, fetch and stream live harness runs.",
            },
            {
                "name": "Findings",
                "description": (
                    "Per-finding triage status (open / included / dismissed)."
                ),
            },
            {
                "name": "Transcripts",
                "description": (
                    "Substring search across the persona-narration archive."
                ),
            },
            {
                "name": "Personas",
                "description": (
                    "CRUD on personas (defaults can be hidden but not deleted)."
                ),
            },
            {
                "name": "Scenarios",
                "description": (
                    "Named {persona + mandatory-actions} presets for "
                    "re-triggering."
                ),
            },
            {
                "name": "System",
                "description": "Healthcheck + service-level endpoints.",
            },
            {
                "name": "Admin",
                "description": (
                    "Destructive operator endpoints. Wipe + audit — "
                    "reset the QA store to a clean slate and record who "
                    "did it and why."
                ),
            },
        ],
    )
    app.state.settings = settings
    app.state.store = store

    # Seed default personas on every startup (idempotent — insert-only,
    # so operator UI edits to a default persona survive subsequent boots).
    # The harness package may not be on the path in unit tests or a
    # standalone API deploy — in that case the seed is silently skipped
    # and the UI reads whatever is already in the collection. DB errors
    # propagate (a Mongo blip at startup should fail loudly, not hide).
    import logging as _seed_log  # noqa: PLC0415
    if seed_personas:
        try:
            _n = seed_default_personas(store)
            if _n:
                _seed_log.getLogger(__name__).info(
                    "qa_personas: inserted %d default personas (first-boot)", _n,
                )
            else:
                _seed_log.getLogger(__name__).info(
                    "qa_personas: all defaults already present, no new rows",
                )
        except ImportError:
            _seed_log.getLogger(__name__).info(
                "qa_personas seed skipped: harness package not on path",
            )

    # Seed the capability catalog (idempotent, insert-only — operator edits +
    # custom entries survive). The menu of grantable capabilities.
    try:
        seed_capability_catalog(store)
    except Exception:  # noqa: BLE001 — never block boot on the seed
        _seed_log.getLogger(__name__).warning("capability catalog seed failed", exc_info=True)

    # Ensure the Site Model $vectorSearch indexes once at startup (idempotent,
    # best-effort), sized to the selected embedding provider
    # (QA_EMBEDDING_PROVIDER → embedding_dim_for). On a cold atlas-local boot
    # mongot may not be ready yet, in which case this no-ops and the
    # `vector-init` one-shot (which polls for readiness) creates them; on a warm
    # deployment this is the only step needed. Mongomock / plain mongod degrade
    # quietly inside the call.
    _created_vix = ensure_vector_indexes(store, dim=embedding_dim_for())
    if _created_vix:
        _seed_log.getLogger(__name__).info(
            "vector indexes created at startup: %s", ", ".join(_created_vix),
        )

    # -- API ---------------------------------------------------------------
    @app.get(
        "/api/runs",
        tags=["Runs"],
        summary="List runs newest-first, each carrying finding_counts by severity.",
    )
    def api_list_runs(limit: int = 50) -> list[dict]:
        """Runs newest-first. Each run carries ``finding_counts`` by severity."""
        return list_runs(store, limit=limit)

    # -- Run control -------------------------------------------------------
    # These GET routes are declared BEFORE ``/api/runs/{run_id}`` on purpose:
    # registered after it, "personas"/"active" would match as a run id.
    @app.get(
        "/api/runs/personas",
        tags=["Runs"],
        summary="List personas (id + display metadata) a run can be scoped to.",
    )
    def api_run_personas() -> dict:
        """Persona catalogue for the trigger-page picker.

        Pre-#1047 this returned a flat ``["mobile-signup-visitor", ...]``
        list of ids and the UI looked up a (stale, hardcoded) label map
        for descriptions — which silently rotted when the persona roster
        was rebuilt for #1010, leaving every card blank in production.
        Returning the metadata from the registry directly keeps the UI
        in lockstep with the harness's catalogue with no second source
        of truth.

        Each entry: ``{id, display_name, archetype, region, language,
        registered_email}``. Fields absent on a given persona come back
        as ``null`` rather than being omitted so the UI's templating
        doesn't have to special-case missing keys.
        """
        try:
            from qa_agents.personas import PERSONAS  # noqa: PLC0415
        except ImportError:
            return {"personas": [
                {"id": pid, "display_name": pid, "archetype": None,
                 "region": None, "language": None, "registered_email": None}
                for pid in KNOWN_PERSONAS
            ]}
        return {
            "personas": [
                {
                    "id": p.id,
                    "display_name": p.display_name,
                    "archetype": p.archetype,
                    "region": p.region,
                    "language": p.language,
                    "registered_email": p.registered_email,
                }
                for p in PERSONAS.values()
            ]
        }

    @app.get(
        "/api/runs/coverage-catalog",
        tags=["Runs"],
        summary="Return the full coverage-action catalog the trigger-page checklist renders from.",
    )
    def api_coverage_catalog() -> dict:
        """The full coverage catalog the trigger-page checklist renders from.

        Returns ``{"categories": [...], "actions": [...]}`` where every
        action carries id / category / human_description / persona_compat /
        requires_auth / expected_outcome. The shape mirrors
        :class:`qa_store.CoverageAction` exactly; the Vue client just maps
        over it.

        Catalog is static — the SPA can cache the response for the page
        session (sent once with default Cache-Control; if traffic shape
        ever demands aggressive caching, switch to ``public, max-age=3600``
        and bump the cache-busting on catalog changes by deploy version).
        """
        return {
            "categories": list(CATEGORIES),
            "actions": [
                {
                    "id": a.id,
                    "category": a.category,
                    "human_description": a.human_description,
                    "persona_compat": list(a.persona_compat),
                    "requires_auth": a.requires_auth,
                    "expected_outcome": a.expected_outcome,
                }
                for a in CATALOG
            ],
        }

    @app.get(
        "/api/runs/active",
        tags=["Runs"],
        summary="Return the harness run currently in progress (or null when nothing is running).",
    )
    def api_active_run() -> dict:
        """The QA run in progress, or ``{"active": null}`` if none."""
        try:
            return {"active": run_control.active_run()}
        except ClusterUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get(
        "/api/runs/availability",
        tags=["Runs"],
        summary="Whether this deployment can actually dispatch persona runs.",
    )
    def api_run_availability() -> dict:
        """Persona runs execute as Kubernetes Jobs; the local-first
        ``docker compose`` stack has no cluster, so they can't launch here. The
        UI calls this to explain that **up front** rather than letting the
        operator complete the whole funnel and hit a cryptic error at Launch."""
        try:
            run_control.active_run()  # cheapest probe that exercises cluster access
            return {"available": True, "reason": None}
        except ClusterUnavailable:
            return {"available": False, "reason": _RUNS_UNAVAILABLE_MSG}

    @app.post(
        "/api/runs/trigger",
        tags=["Runs"],
        summary="Trigger a new harness run for the selected personas.",
    )
    def api_trigger_run(req: TriggerRunRequest) -> dict:
        """Start a QA harness run for the requested personas.

        Empty ``req.personas`` means "use the currently-active set" (#1009).
        Pre-relaunch this meant "every persona in the harness catalog";
        post-relaunch the operator activates a subset on the Personas
        page and the trigger defaults to that. If nothing is active,
        we 422 rather than running silently with zero personas.

        409 if a run is already active, 422 for an unknown persona id, 503 if
        the cluster is unreachable. The UI gates this behind a confirm step;
        the active-run guard here is the authoritative check.
        """
        # Resolve persona list — explicit body OR active set from DB.
        if req.personas:
            personas_to_run = list(req.personas)
        else:
            active = list_personas(store, active_only=True)
            personas_to_run = [p["persona_id"] for p in active]
            if not personas_to_run:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "no personas are active — activate at least one on the "
                        "Personas page, or pass an explicit `personas` list "
                        "in the trigger request."
                    ),
                )

        unknown = sorted(p for p in personas_to_run if p not in KNOWN_PERSONAS)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown persona(s): {', '.join(unknown)} — "
                    f"valid ids are {', '.join(KNOWN_PERSONAS)}"
                ),
            )
        # #861 — validate mandatory coverage-action ids against the catalog
        # BEFORE the K8s Job is created. The harness's prompt renderer
        # tolerates unknown ids with a warning-and-drop (a defence against
        # a stale CronJob env), but the trigger UI should fail fast on a
        # typo so the operator sees a clean error instead of an unexpectedly
        # empty mandatory block in the resulting run.
        if req.mandatory_action_ids:
            catalog_ids = {a.id for a in CATALOG}
            unknown_actions = sorted(
                aid for aid in req.mandatory_action_ids if aid not in catalog_ids
            )
            if unknown_actions:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"unknown mandatory action(s): {', '.join(unknown_actions)} "
                        "— see /api/runs/coverage-catalog for the valid ids"
                    ),
                )
        # #1031 — Slice C of the MCP visibility epic. Same fail-fast
        # shape as mandatory_action_ids above. The harness's gating
        # helper (_resolve_enabled_mcp_servers) tolerates unknown ids
        # with a log warning, but the trigger UI should 422 on a typo
        # so the operator sees the error before the run is dispatched.
        # Catalog is the harness's qa_agents.mcp_catalog source-of-truth.
        if req.enabled_mcp_servers:
            try:
                from qa_agents.mcp_catalog import server_ids  # noqa: PLC0415
                mcp_catalog_ids = set(server_ids())
            except ImportError:
                mcp_catalog_ids = set()
            unknown_mcps = sorted(
                m for m in req.enabled_mcp_servers if m not in mcp_catalog_ids
            )
            if unknown_mcps:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"unknown MCP server(s): {', '.join(unknown_mcps)} "
                        "— see /api/mcp-servers for the valid ids"
                    ),
                )
        # #1821 — mirror the pod_count × concurrency ≤ MAX_SIMULTANEOUS_PERSONAS
        # cost ceiling at the API layer, the same way the per-field bounds
        # (concurrency ≤ 6, pod_count ≤ 4) are enforced as request-shape 422s:
        # reject an over-budget fan-out BEFORE any cluster call. The product —
        # not either field alone — is what loads the single personal Claude
        # Code Max subscription, so a 4×3=12 shape passes both per-field bounds
        # yet must be refused. K8sRunControl.trigger defends the same ceiling
        # (so a non-UI caller can't bypass it), but checking here gives the
        # operator a clean form error before the Max-token pre-check + CronJob
        # read. Resolve the same effective defaults the K8s layer uses: an
        # omitted pod_count is the single-pod default (1); an omitted
        # concurrency is the conservative pod-spec floor (1).
        effective_pod_count = req.pod_count if req.pod_count is not None else 1
        effective_concurrency = req.concurrency if req.concurrency is not None else 1
        if effective_pod_count * effective_concurrency > MAX_SIMULTANEOUS_PERSONAS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"pod_count ({effective_pod_count}) × concurrency "
                    f"({effective_concurrency}) = "
                    f"{effective_pod_count * effective_concurrency} exceeds the "
                    f"{MAX_SIMULTANEOUS_PERSONAS}-persona simultaneous ceiling"
                ),
            )
        # Every run is Max-billed: the single qa-agents CronJob scrubs
        # ANTHROPIC_API_KEY so the harness always authenticates with the
        # operator's Claude Code Max OAuth token (Secret
        # qa-claude-code-credentials). Pre-check that Secret unconditionally
        # so the operator sees a clean 422 instead of a half-created Job
        # that 401s at pod-start.
        try:
            if not run_control.secret_exists(settings.qa_claude_code_secret_name):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Secret {settings.qa_claude_code_secret_name!r} not "
                        "found in the sandbox namespace — the Max-billed "
                        "Job has no OAuth token to authenticate with. Run "
                        "`make qa-claude-token && make infra-apply` to "
                        "provision the token, then retry."
                    ),
                )
        except ClusterUnavailable as exc:
            raise HTTPException(
                status_code=503, detail=f"{_RUNS_UNAVAILABLE_MSG} (cluster: {exc})",
            ) from exc

        # P4 — light up the MCP servers this target has *granted* capabilities
        # for, and inject their vaulted credentials as the env vars the harness
        # already reads. Best-effort: a resolution hiccup must never block a run.
        enabled_servers = req.enabled_mcp_servers
        capability_env: dict[str, str] | None = None
        if req.target_id:
            try:
                from qa_agents.mcp_catalog import list_servers  # noqa: PLC0415
                from qa_agents.target_mcp import resolve_target_mcp  # noqa: PLC0415
                resolved = resolve_target_mcp(store, req.target_id)
            except Exception:  # noqa: BLE001 — enrichment, never fatal
                resolved = {"server_ids": [], "env": {}}
            derived = resolved.get("server_ids") or []
            if derived:
                # Augment, never shrink: a non-empty enabled list is an exclusive
                # opt-in to the harness, so union the derived servers onto the
                # operator's choice (or onto the default-on set when they left it
                # to defaults) — otherwise auto-adding one server would silently
                # drop playwright/findings.
                base = list(req.enabled_mcp_servers) if req.enabled_mcp_servers else [
                    s.id for s in list_servers() if s.default_enabled
                ]
                enabled_servers = base + [s for s in derived if s not in base]
            capability_env = resolved.get("env") or None
        try:
            return run_control.trigger(
                personas_to_run,
                concurrency=req.concurrency,
                explore_model=req.explore_model,
                report_model=req.report_model,
                max_turns=req.max_turns,
                run_duration_s=req.run_duration_s,
                run_notes=req.run_notes,
                mandatory_action_ids=req.mandatory_action_ids,
                target_url=req.target_url,
                enabled_mcp_servers=enabled_servers,
                capability_env=capability_env,
                # #1821 — an omitted pod_count forwards as 1 (single-pod, the
                # pre-#1821 Job shape, byte-for-byte). An explicit value passes
                # straight through to drive the N-Jobs fan-out (Option B).
                pod_count=effective_pod_count,
                # #1821 — hand trigger the store so it can write the run doc's
                # expected_personas (the finish-barrier denominator) up front.
                store=store,
            )
        except RunLimitExceeded as exc:
            # #1821 — pod_count × concurrency over the simultaneous-persona
            # ceiling. 422 matches the other request-shape rejections above.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RunAlreadyActive as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ClusterUnavailable as exc:
            raise HTTPException(
                status_code=503, detail=f"{_RUNS_UNAVAILABLE_MSG} (cluster: {exc})",
            ) from exc
        except RunControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get(
        "/api/runs/active/logs",
        tags=["Runs"],
        summary="Server-Sent-Events stream of the live run's narration.",
    )
    def api_active_run_logs() -> StreamingResponse:
        """Server-Sent-Events stream of the active run's harness logs."""

        def _events():
            try:
                for line in run_control.stream_logs():
                    yield f"data: {line}\n\n"
            except RunControlError as exc:
                yield f"data: (run-control error: {exc})\n\n"
            except Exception as exc:  # noqa: BLE001 - #1822, see below
                # Last-ditch guard: ANY unexpected exception escaping the
                # log stream must not abort the StreamingResponse mid-frame
                # (the browser would see an HTTP/2 protocol error and the
                # EventSource would reconnect-loop). Emit one in-band error
                # line, then fall through to the ``event: end`` terminator
                # below so the client closes cleanly. GeneratorExit (client
                # disconnect) is BaseException and still propagates.
                reason = " ".join(f"{type(exc).__name__}: {exc}".split())[:200]
                yield f"data: (log stream error: {reason})\n\n"
            # EVERY exit path ends with the explicit terminator — the SPA's
            # EventSource closes on it instead of auto-reconnecting forever.
            yield "event: end\ndata: done\n\n"

        return StreamingResponse(
            _events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/api/runs/{run_id}",
        tags=["Runs"],
        summary="Fetch a single run with its findings + per-persona reviews.",
    )
    def api_get_run(run_id: str) -> dict:
        """One run in full — its per-persona reviews and all its findings."""
        run = get_run(store, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
        return run

    # #860 — Transcript tab data + screenshot byte streaming.
    #
    # Declared BEFORE /api/runs/{run_id}/file-issue and the /findings PATCH
    # so the longer paths win the route match (FastAPI matches in
    # registration order — a shorter prefix earlier would shadow these).
    @app.get(
        "/api/runs/{run_id}/personas/{persona_id}/transcript",
        tags=["Runs"],
        summary="Return one persona's full transcript for a run.",
    )
    def api_get_transcript(run_id: str, persona_id: str) -> dict:
        """Per-persona step-by-step transcript for the Transcript tab.

        Always returns a 200 with ``{"steps": [...]}``. An empty list is a
        valid state — the harness might be running an older image without
        recorder wiring, or a brand-new persona may not have taken any
        steps yet. Clients render "(no steps recorded)" on empty rather
        than treating it as an error.

        Step record shape — see qa_store.schema.record_step for the full
        contract. Notably ``screenshot_id`` is a Mongo ObjectId (serialised
        as a string by the JSON layer when present); the client fetches
        the bytes via /api/runs/{run_id}/screenshots/{oid} below.
        """
        steps = list_steps_for_persona(store, run_id, persona_id)
        # JSON-serialise the screenshot_id if it's an ObjectId — FastAPI's
        # default encoder doesn't know about bson types.
        for step in steps:
            sid = step.get("screenshot_id")
            if sid is not None:
                step["screenshot_id"] = str(sid)
        return {"steps": steps}

    # #902 / #904 — narrative-emit replay. Same idea as the
    # /transcript endpoint above (per-persona, chronological) but
    # pulls from qa_run_logs (added in slice 1, #903) rather than
    # qa_run_steps. The two collections are complementary: steps tell
    # you WHAT the persona did (one row per tool call, with
    # screenshot oid + finding linkback); logs tell you WHY (the
    # LLM's narration between calls + the per-turn result accounting).
    # The Transcripts page in the review UI consumes this for the
    # full-replay view.
    @app.get(
        "/api/runs/{run_id}/personas/{persona_id}/logs",
        tags=["Runs"],
        summary="Return one persona's structured logs for a run.",
    )
    def api_get_persona_logs(run_id: str, persona_id: str) -> dict:
        """Per-persona emit replay, ordered by ``seq`` ascending.

        Always returns 200 with ``{"logs": [...]}``. Empty list is a
        valid state (pre-#903 runs have no logs; brand-new persona
        may not have emitted yet). Client renders an empty-state
        rather than treating it as an error.
        """
        logs = list_run_logs_for_persona(store, run_id, persona_id)
        return {"logs": logs}

    # QA Studio — merged timeline for the live workflow view. Combines
    # qa_run_steps (screenshots, tool calls) and qa_run_logs (narrative
    # emits) for all personas in one sorted response so the UI can render
    # the "what's happening right now" panel without two round-trips per
    # persona. Each event carries a ``kind`` discriminator:
    #   "step"  — from qa_run_steps (screenshot, tool action)
    #   "log"   — from qa_run_logs (narrative emit)
    @app.get(
        "/api/runs/{run_id}/timeline",
        tags=["Runs"],
        summary="Return the ordered timeline of steps + log lines for a run.",
    )
    def api_run_timeline(run_id: str) -> dict:
        """Merged step + log events for every persona in one run, sorted by time.

        Returns ``{"events": [...], "run_id": run_id}``. Each event has:
          ``kind``        "step" | "log"
          ``persona_id``  str
          ``ts``          ISO-8601 timestamp
          plus all fields from the underlying step / log document.

        Empty list on a pre-Studio run that has no steps or logs — valid
        display state, client renders an empty-state message.
        """
        run = get_run(store, run_id)
        if run is None:
            raise HTTPException(404, detail=f"run {run_id!r} not found")

        # Sentinel for rows missing a timestamp — keeps the sort total-ordering
        # even on legacy step rows that pre-date the recorder. ``datetime.min``
        # comes from stdlib; both step.recorded_at and log.ts are aware
        # datetimes, so the unaware ``datetime.min`` would mix and TypeError
        # the sort — use ``datetime.min.replace(tzinfo=UTC)`` instead.
        from datetime import UTC as _UTC  # noqa: PLC0415
        from datetime import datetime as _dt  # noqa: PLC0415
        _EPOCH = _dt.min.replace(tzinfo=_UTC)

        events: list[dict] = []
        for persona_id in run.get("personas") or []:
            for step in list_steps_for_persona(store, run_id, persona_id):
                sid = step.get("screenshot_id")
                if sid is not None:
                    step["screenshot_id"] = str(sid)
                step["kind"] = "step"
                # Steps use ``recorded_at``; mirror it into ``ts`` so the
                # client only needs to read one field across both event kinds.
                if "ts" not in step:
                    step["ts"] = step.get("recorded_at") or _EPOCH
                events.append(step)
            for log in list_run_logs_for_persona(store, run_id, persona_id):
                log["kind"] = "log"
                # ``append_run_log`` always writes ts — guard is for forward
                # compat / mongomock test doubles, not a real expected case.
                log.setdefault("ts", _EPOCH)
                events.append(log)

        events.sort(key=lambda e: e.get("ts") or _EPOCH)
        return {"events": events, "run_id": run_id}

    # ── Slice 1 of #1002 — discovered_* read endpoints ─────────────────
    # Three GETs over the new collections written by the harness's
    # post-run distillation hook. All read-only; Slice 2 adds the
    # canonicalization writes (approval queue, merge etc).
    #
    # The query string is flat — run_id + persona_id + category (where
    # applicable) all optional; combine to get the natural views:
    #   ?run_id=X            → this run's discoveries (RunDetail tab)
    #   ?persona_id=margaret → margaret's cumulative discoveries
    #   ?category=billing    → corpus-wide "everything billing"
    #   (no params)          → newest-first feed across all runs
    @app.get("/api/discovered-actions")
    def api_list_discovered_actions(
        run_id: str | None = None,
        persona_id: str | None = None,
        category: str | None = None,
        limit: int = 500,
    ) -> dict:
        """Coverage corpus — what personas have learned the site can do.

        ``category`` is validated against ``DISCOVERED_ACTION_CATEGORIES``
        so a typo'd filter 422s rather than silently returning nothing.
        ``run_id`` / ``persona_id`` are not pre-validated — an unknown
        id just yields an empty list, matching how ``/api/transcripts/
        search`` handles unknown filters.
        """
        if category is not None and category not in DISCOVERED_ACTION_CATEGORIES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown category {category!r} — valid: "
                    f"{', '.join(DISCOVERED_ACTION_CATEGORIES)}"
                ),
            )
        rows = list_discovered_actions(
            store,
            run_id=run_id,
            persona_id=persona_id,
            category=category,
            limit=int(limit),
        )
        return {"actions": rows, "count": len(rows)}

    @app.get("/api/discovered-tools")
    def api_list_discovered_tools(
        run_id: str | None = None,
        persona_id: str | None = None,
        limit: int = 500,
    ) -> dict:
        """Fixture catalog seed — what tools the personas identified.

        Returns the raw names (often ``mcp__playwright__*``,
        ``mcp__email__*`` etc.) until the variant-generator slice gives
        them friendly identifiers. The UI groups by tool name for the
        per-run summary.
        """
        rows = list_discovered_tools(
            store, run_id=run_id, persona_id=persona_id, limit=int(limit),
        )
        return {"tools": rows, "count": len(rows)}

    @app.get("/api/discovered-branches")
    def api_list_discovered_branches(
        run_id: str | None = None,
        persona_id: str | None = None,
        limit: int = 500,
    ) -> dict:
        """Variant-generator seed — things the persona noticed but didn't try.

        Free-text observations. Sorted (distilled_at DESC, ordinal ASC) so
        within a distillation the branches keep the model's emit order
        (which mirrors the persona's chronological observation).
        """
        rows = list_discovered_branches(
            store, run_id=run_id, persona_id=persona_id, limit=int(limit),
        )
        return {"branches": rows, "count": len(rows)}

    # #902 / #904 — cross-run search. Hand-driven pattern discovery
    # before any analyzer agent is built (slice 3 will add semantic
    # search via Atlas Vector). Today this is a case-insensitive
    # regex on `content` — cheap, scales fine to the ~30k docs/month
    # the substrate accumulates.
    #
    # Validation:
    #   - persona is checked against KNOWN_PERSONAS so a typo'd value
    #     is a 422 (matches how the trigger endpoint validates persona ids)
    #   - kind is open-ended — the recorder accepts unknowns by
    #     design, so the API does too. Filtering on a typo'd kind
    #     just returns nothing.
    #   - q is unrestricted; the qa-store helper escapes regex metas.
    #   - since/until are ISO-8601, parsed by FastAPI's datetime type.
    @app.get(
        "/api/transcripts/search",
        tags=["Transcripts"],
        summary="Substring-search across every persona's narration archive.",
    )
    def api_search_transcripts(
        q: str | None = None,
        persona: str | None = None,
        kind: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 200,
    ) -> dict:
        """Newest-first cross-run match against qa_run_logs.

        ``q`` — case-insensitive substring (regex metas escaped).
        ``persona`` — exact match; must be a known persona id when set.
        ``kind`` — exact match (no validation; unknowns silently match
        nothing, same forgiveness the recorder uses).
        ``since`` / ``until`` — inclusive ts window.
        ``limit`` — capped at 1000 by the qa-store helper.
        """
        if persona is not None and persona not in KNOWN_PERSONAS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown persona {persona!r}",
            )
        results = search_run_logs(
            store,
            q=q,
            persona_id=persona,
            kind=kind,
            since=since,
            until=until,
            limit=int(limit),
        )
        return {
            "results": results,
            "count": len(results),
            "query": {
                "q": q, "persona": persona, "kind": kind,
                "since": since.isoformat() if since else None,
                "until": until.isoformat() if until else None,
                "limit": int(limit),
            },
        }

    @app.get(
        "/api/runs/{run_id}/screenshots/{oid}",
        tags=["Runs"],
        summary="Stream a screenshot (GridFS blob) captured during a run.",
    )
    def api_get_screenshot(run_id: str, oid: str) -> StreamingResponse:
        """Stream a captured Playwright screenshot.

        The ``run_id`` path param exists for two reasons even though the
        GridFS lookup keys only on ``oid``: it documents the URL's intent
        (this screenshot belongs to this run) and gives a future
        authorisation layer something to enforce against (per-run access
        control would check it here). Today the review UI is operator-only
        and behind an in-cluster port-forward, so we don't authorise per
        request.

        404 when the oid doesn't exist (``gridfs.NoFile``); 500 only on
        unexpected errors — Mongo connectivity blips bubble up to the
        FastAPI exception handler.
        """
        from gridfs.errors import NoFile

        try:
            data = fetch_screenshot(store, oid)
        except NoFile as exc:
            raise HTTPException(
                status_code=404, detail=f"screenshot {oid!r} not found"
            ) from exc

        return StreamingResponse(
            iter([data]),
            media_type="image/png",
            headers={
                # Aggressive cache — screenshot bytes are immutable (the
                # ObjectId pins a specific blob), so a long browser cache
                # is safe and reduces repeat GridFS pulls when the
                # Transcript tab is re-rendered.
                "Cache-Control": "private, max-age=86400, immutable",
            },
        )

    @app.patch(
        "/api/findings/{finding_id}",
        tags=["Findings"],
        summary="Set a finding's triage status (open / included / dismissed).",
    )
    def api_patch_finding(finding_id: str, update: FindingStatusUpdate) -> dict:
        """Set a finding's triage status (open | included | dismissed)."""
        if update.status not in FINDING_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"status must be one of {list(FINDING_STATUSES)}",
            )
        try:
            return set_finding_status(store, finding_id, update.status)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"unknown finding {finding_id!r}"
            ) from None

    @app.post(
        "/api/runs/{run_id}/file-issue",
        tags=["Runs"],
        summary="Compose a GitHub issue from the run's findings and post it.",
    )
    def api_file_issue(run_id: str) -> dict:
        """Compose ONE GitHub issue for the run and create it.

        Body = each persona's review + the run's ``included`` findings grouped
        by severity. A run with no ``included`` findings is still fileable —
        the issue notes that and records the reviews only. On success the run
        is marked ``filed`` and the issue url is stored + returned.
        """
        run = get_run(store, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
        if not settings.github_token:
            raise HTTPException(
                status_code=503,
                detail="GITHUB_TOKEN is not configured on the API server.",
            )

        title, body = compose_issue(run)
        try:
            issue_url = create_github_issue(
                settings.github_repo, settings.github_token, title, body
            )
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"GitHub rejected the issue: {exc.response.status_code}",
            ) from exc
        except httpx.HTTPError as exc:  # network / timeout
            raise HTTPException(
                status_code=502, detail=f"could not reach GitHub: {exc}"
            ) from exc

        mark_run_filed(store, run_id, issue_url)
        return {"gh_issue_url": issue_url}

    # -----------------------------------------------------------------
    # Saved scenarios (#862, Slice 5).
    #
    # CRUD on the qa_scenarios collection. Validates persona_id against
    # KNOWN_PERSONAS and mandatory_action_ids against CATALOG on every
    # write — keeping the validation here (not in qa_store) lets the
    # store stay agnostic of harness-side constants.
    # -----------------------------------------------------------------
    def _validate_scenario_refs(
        persona_id: str | None,
        mandatory_action_ids: list[str] | None,
    ) -> None:
        """Raise 422 if persona_id or any action id is unknown.

        Both args are optional (``None`` skips that check) so the same
        helper serves create (both required) and patch (either may be
        absent from the body). Mirrors the trigger-endpoint validation
        pattern in api_trigger_run.
        """
        if persona_id is not None and persona_id not in KNOWN_PERSONAS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown persona_id {persona_id!r} — valid ids are "
                    f"{', '.join(KNOWN_PERSONAS)}"
                ),
            )
        if mandatory_action_ids:
            catalog_ids = {a.id for a in CATALOG}
            unknown = sorted(
                aid for aid in mandatory_action_ids if aid not in catalog_ids
            )
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"unknown mandatory action(s): {', '.join(unknown)} "
                        "— see /api/runs/coverage-catalog for the valid ids"
                    ),
                )

    # -- Persona library (QA Studio) ----------------------------------------
    @app.get(
        "/api/personas",
        tags=["Personas"],
        summary="List personas (optionally including hidden defaults).",
    )
    def api_list_personas(
        include_hidden: bool = False,
        active: bool = False,
    ) -> dict:
        """All personas, default rows first then alphabetical.

        Returns ``{"personas": [...]}``. Filters:
          - ``?include_hidden=true`` includes soft-deleted defaults
          - ``?active=true`` (#1009) restricts to operator-activated
            personas — the trigger UI's "Active" tab uses this.
        """
        return {
            "personas": list_personas(
                store,
                include_hidden=include_hidden,
                active_only=active,
            ),
        }

    # -- MCP server catalog (#1030) -----------------------------------------
    # The catalog lives in qa_agents.mcp_catalog (the harness package,
    # installed --no-deps in the review-ui image — same pattern as
    # qa_agents.personas reads in runs.py per #990). This endpoint
    # serialises it for the SPA's /mcp-tools view (Slice B) and the
    # trigger-form checkbox list (Slice C).
    @app.get(
        "/api/mcp-servers",
        tags=["MCP"],
        summary="List the MCP servers Test Ease has wired (static catalog).",
    )
    def api_list_mcp_servers() -> dict:
        """Return every MCP server in the catalog.

        Each entry: ``{id, display_name, description, default_enabled,
        persona_compat, tool_count}``. The catalog is a static module
        in the harness image — editing it requires a deploy (per the
        #1028 epic's source-of-truth decision; migrate to DB-backed
        when the catalog grows past ~10 entries).
        """
        try:
            from qa_agents.mcp_catalog import list_servers  # noqa: PLC0415
        except ImportError:
            # Same defensive fall-through pattern as runs.py
            # KNOWN_PERSONAS — if the harness package isn't installed
            # (a test context that skipped `pip install qa-agents`),
            # return an empty catalog rather than 500.
            return {"servers": []}
        return {
            "servers": [
                {
                    "id": s.id,
                    "display_name": s.display_name,
                    "description": s.description,
                    "default_enabled": s.default_enabled,
                    "persona_compat": list(s.persona_compat),
                    "tool_count": s.tool_count,
                }
                for s in list_servers()
            ],
        }

    @app.post(
        "/api/personas",
        status_code=201,
        tags=["Personas"],
        summary="Create a new (non-default) persona.",
    )
    def api_create_persona(req: PersonaCreateRequest) -> dict:
        """Create a new user-defined persona.

        409 if ``persona_id`` is already taken (the unique index on
        ``persona_id`` is the source of truth — the route catches the
        DuplicateKeyError rather than doing a check-then-insert, which
        would race with a concurrent create).
        """
        from pymongo.errors import DuplicateKeyError  # noqa: PLC0415

        doc = req.model_dump()
        doc["is_default"] = False
        doc["hidden"] = False
        if doc.get("avatar_seed") is None:
            doc["avatar_seed"] = req.persona_id
        try:
            return create_persona(store, doc)
        except DuplicateKeyError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"persona_id {req.persona_id!r} already exists",
            ) from exc

    @app.get("/api/personas/{persona_id}", tags=["Personas"], summary="Fetch one persona by id.")
    def api_get_persona(persona_id: str) -> dict:
        """Return one persona by id. 404 if not found."""
        doc = get_persona(store, persona_id)
        if doc is None:
            raise HTTPException(404, detail=f"persona {persona_id!r} not found")
        return doc

    @app.patch(
        "/api/personas/{persona_id}",
        tags=["Personas"],
        summary="Update fields on a persona (all fields optional).",
    )
    def api_update_persona(persona_id: str, req: PersonaUpdateRequest) -> dict:
        """Partial-update a persona. Returns the updated document.

        For default personas, ``hidden=True`` is the soft-delete path.
        Hard-delete is only allowed for non-default personas (via DELETE).

        Uses ``exclude_unset=True`` so a JSON body distinguishes "field
        omitted" (no change) from "field explicitly set to null" (clear it).
        Only ``setup_actions``, ``browser_locale``, and ``avatar_seed`` are
        nullable — the other null-shaped fields are tightened in the model.
        """
        patch = req.model_dump(exclude_unset=True)
        if not patch:
            raise HTTPException(422, detail="request body is empty — nothing to update")
        try:
            return update_persona(store, persona_id, patch)
        except KeyError as exc:
            raise HTTPException(404, detail=str(exc)) from exc

    @app.delete(
        "/api/personas/{persona_id}",
        status_code=204,
        tags=["Personas"],
        summary="Delete a non-default persona (defaults can only be hidden).",
    )
    def api_delete_persona(persona_id: str):
        """Hard-delete a user-created persona.

        Raises 422 if called on a default persona (use hidden=True instead).
        Raises 404 if the persona doesn't exist.

        Return annotation intentionally omitted — under FastAPI 0.104 (our
        pinned version) ``-> None`` plus ``status_code=204`` trips
        ``Status code 204 must not have a response body`` at route
        registration. The other 204 routes here predate that pin and
        happened to be registered before the offending interaction, so
        they slip through; this route trips it consistently. Drop the
        annotation and FastAPI infers "no response body" from the bare
        signature.
        """
        try:
            deleted = delete_persona(store, persona_id)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(404, detail=f"persona {persona_id!r} not found")

    # #1105 Slice 1 — persistent persona credentials. The status endpoint
    # surfaces whether a persona has a saved login (no password ever
    # leaves the server). DELETE is the "reset login" operator action
    # used by the Personas page's reset button + the SIGNUP_FRESH
    # setup-action DSL. The password-setting path is operator-internal
    # for Slice 1.0 — the harness's recorder hook (Slice 1.1) will
    # populate credentials after a successful signup; operators don't
    # type passwords into Test Ease.
    @app.get(
        "/api/personas/{persona_id}/credentials/status",
        tags=["Personas"],
        summary="Whether this persona has saved login credentials.",
    )
    def api_get_persona_credentials_status(persona_id: str) -> dict:
        """Return ``{has_credentials, email?, registered_at?, verified?,
        last_rotation_n?, has_session_jwt?, jwt_expires_at?}``.

        Never returns the password under any shape — that's the
        security contract pinned by ``test_status_never_includes_password``
        in the qa-store layer.
        """
        try:
            return get_persona_credentials_status(store, persona_id)
        except KeyError as exc:
            raise HTTPException(404, detail=str(exc)) from exc

    @app.delete(
        "/api/personas/{persona_id}/credentials",
        status_code=204,
        tags=["Personas"],
        summary="Clear a persona's saved login credentials.",
    )
    def api_clear_persona_credentials(persona_id: str):
        """Reset the persona's saved login. Next run will fall through
        to a fresh signup. Bumps the persona's
        ``last_credential_rotation`` audit counter.

        Idempotent: clearing an already-empty credentials sub-doc
        returns 204 without error (only 404 when the persona itself
        doesn't exist). Return annotation omitted for the same reason
        as DELETE /api/personas/{id} above — FastAPI 0.104 + 204 +
        ``-> None`` trips a registration assertion.
        """
        try:
            clear_persona_credentials(store, persona_id)
        except KeyError as exc:
            raise HTTPException(404, detail=str(exc)) from exc

    @app.get(
        "/api/scenarios",
        tags=["Scenarios"],
        summary="List saved scenarios (persona + mandatory-actions presets).",
    )
    def api_list_scenarios() -> dict:
        """All saved scenarios, newest-edited first.

        Returns ``{"scenarios": [...]}`` — never errors on an empty
        collection (returns an empty list, which is the normal state
        on a fresh deploy).
        """
        return {"scenarios": list_scenarios(store)}

    @app.post("/api/scenarios", status_code=201, tags=["Scenarios"], summary="Save a new scenario.")
    def api_create_scenario(req: ScenarioCreateRequest) -> dict:
        """Create a new scenario.

        409 if the id is already taken; 422 for an unknown persona_id
        or an unknown coverage-action id. The slug-pattern + length
        bounds on ``id`` / ``name`` are enforced by pydantic before this
        body even runs.
        """
        _validate_scenario_refs(req.persona_id, req.mandatory_action_ids)
        from pymongo.errors import DuplicateKeyError

        try:
            return create_scenario(
                store,
                id=req.id,
                name=req.name,
                description=req.description,
                persona_id=req.persona_id,
                mandatory_action_ids=req.mandatory_action_ids,
            )
        except DuplicateKeyError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"scenario id {req.id!r} already exists",
            ) from exc

    @app.patch(
        "/api/scenarios/{scenario_id}",
        tags=["Scenarios"],
        summary="Update an existing scenario.",
    )
    def api_update_scenario(
        scenario_id: str, req: ScenarioUpdateRequest,
    ) -> dict:
        """Partial-update a scenario.

        404 if the id doesn't exist. 422 for an unknown persona_id or
        coverage-action id in the body. Omitted fields preserve; an
        explicit empty mandatory_action_ids list clears the field (see
        the schema docstring for the omission-vs-clear contract).
        """
        _validate_scenario_refs(req.persona_id, req.mandatory_action_ids)
        result = update_scenario(
            store,
            scenario_id,
            name=req.name,
            description=req.description,
            persona_id=req.persona_id,
            mandatory_action_ids=req.mandatory_action_ids,
        )
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown scenario {scenario_id!r}",
            )
        return result

    @app.delete(
        "/api/scenarios/{scenario_id}",
        status_code=204,
        tags=["Scenarios"],
        summary="Delete a scenario.",
    )
    def api_delete_scenario(scenario_id: str):
        """Hard-delete a scenario. 404 if it didn't exist.

        Returns 204 No Content on success — the SPA reloads its list
        from /api/scenarios afterwards, so an empty response body keeps
        the transport tight.
        """
        if not delete_scenario(store, scenario_id):
            raise HTTPException(
                status_code=404,
                detail=f"unknown scenario {scenario_id!r}",
            )

    @app.get(
        "/api/scenarios/{scenario_id}",
        tags=["Scenarios"],
        summary="Fetch one scenario by id.",
    )
    def api_get_scenario(scenario_id: str) -> dict:
        """Fetch one scenario by id. 404 if missing.

        Declared AFTER the collection-level routes above so e.g. a
        path like /api/scenarios doesn't match this {id}-templated
        route.
        """
        doc = get_scenario(store, scenario_id)
        if doc is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown scenario {scenario_id!r}",
            )
        return doc

    # #1115 — per-finding file-issue. Mirrors POST /api/insights/{id}/
    # file-issue. The run-level button (POST /runs/{run_id}/file-issue)
    # still exists for bundling the entire run; this endpoint files ONE
    # finding so the operator can triage row-by-row from the run-detail
    # Findings tab.
    @app.post(
        "/api/findings/{finding_id}/file-issue",
        tags=["Runs"],
        summary="File a GitHub issue for one finding (server-side).",
    )
    def api_file_finding_issue(finding_id: str) -> dict:
        """Create a GitHub issue for ``finding_id`` and link it back.

        On success the finding's ``gh_issue_url`` + ``gh_issue_number``
        are persisted. Returns ``{gh_issue_url, gh_issue_number}``.

        Errors:
          * 404 if the finding (or its run) doesn't exist
          * 409 if it's already been filed
          * 503 if GITHUB_TOKEN isn't configured
          * 502 if GitHub rejects the call or the network breaks
        """
        finding = get_finding(store, finding_id)
        if finding is None:
            raise HTTPException(
                status_code=404, detail=f"finding {finding_id!r} not found",
            )
        if finding.get("gh_issue_url"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"finding already filed as {finding['gh_issue_url']}"
                ),
            )
        run = get_run(store, finding.get("run_id"))
        if run is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"finding's run {finding.get('run_id')!r} not found "
                    "(orphan finding doc?)"
                ),
            )
        if not settings.github_token:
            raise HTTPException(
                status_code=503,
                detail="GITHUB_TOKEN is not configured on the API server.",
            )

        title, body = compose_finding_issue(finding, run)
        try:
            created = create_github_issue_full(
                settings.github_repo,
                settings.github_token,
                title,
                body,
                label="qa-finding",
            )
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"GitHub rejected the issue: {exc.response.status_code}",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"could not reach GitHub: {exc}",
            ) from exc

        updated = mark_finding_filed(
            store,
            finding_id,
            issue_url=created["html_url"],
            issue_number=created["number"],
        )
        return {
            "gh_issue_url": updated["gh_issue_url"],
            "gh_issue_number": updated["gh_issue_number"],
        }

    # #1115 — live GitHub state sync for a finding's linked issue.
    # Same cache-bypass semantics as the insight equivalent.
    @app.post(
        "/api/findings/{finding_id}/sync-gh-state",
        tags=["Runs"],
        summary="Force-refresh a finding's linked GitHub issue state.",
    )
    def api_sync_finding_gh_state(finding_id: str) -> dict:
        """Force-fetch the live open/closed state of the finding's
        linked issue and persist it. Returns ``{gh_issue_state,
        gh_issue_state_synced_at}``."""
        finding = get_finding(store, finding_id)
        if finding is None:
            raise HTTPException(
                status_code=404, detail=f"finding {finding_id!r} not found",
            )
        if not finding.get("gh_issue_url") or not finding.get("gh_issue_number"):
            raise HTTPException(
                status_code=422,
                detail=(
                    "finding has no linked GitHub issue — file one first "
                    "via POST /api/findings/{id}/file-issue"
                ),
            )
        if not settings.github_token:
            raise HTTPException(
                status_code=503,
                detail="GITHUB_TOKEN is not configured on the API server.",
            )
        new_state = fetch_github_issue_state(
            settings.github_repo,
            settings.github_token,
            finding["gh_issue_number"],
        )
        updated = update_finding_gh_state(
            store, finding_id, state=new_state,
        )
        return {
            "gh_issue_state": updated.get("gh_issue_state"),
            "gh_issue_state_synced_at": updated.get("gh_issue_state_synced_at"),
        }

    # ───────────────────────────────────────────────────────────────
    # #1146 — Admin / nuclear-button endpoints.
    #
    # The "wipe and re-prove" workflow: operator clicks /admin's
    # Wipe button → endpoint drops every per-run + per-persona
    # collection AND re-seeds the persona catalog → audit row
    # logged with the operator's reason → fresh runs start
    # populating from zero.
    # ───────────────────────────────────────────────────────────────
    @app.post(
        "/api/admin/wipe",
        tags=["Admin"],
        summary="Drop every per-run + per-persona collection. Requires confirm='WIPE'.",
    )
    def api_admin_wipe(req: AdminWipeRequest) -> dict:
        """Drop every per-run + per-persona collection and re-seed.

        Two-step destructive confirmation: the operator types ``WIPE``
        into the modal's confirm box. The literal-string match (NOT
        case-insensitive, NOT prefix-tolerant) is deliberate friction
        — a clicked button without a typed confirmation can't drop
        the slyreply_qa database by accident.

        After ``wipe_for_relaunch`` returns, ``seed_default_personas``
        immediately re-seeds the persona catalog so the UI isn't empty
        on the next render. Then a row goes into qa_admin_audit (the
        ONLY collection wipe_for_relaunch leaves alone) recording the
        wipe + the operator-supplied note.

        #1108 — when the operator opts into ``wipe_mailpit`` the Mailpit
        admin DELETE /api/v1/messages also fires AFTER the Mongo wipe.
        The PVC itself is NEVER deleted by this endpoint — only the
        message contents. The Mailpit step is best-effort: a failure
        there does NOT roll back the Mongo wipe (already irreversible)
        and the audit row records what was attempted via
        ``mailpit_wiped`` / ``mailpit_error``.

        Returns the per-collection dropped counts AND the audit row
        the UI can render in its history list, plus the Mailpit
        outcome when the toggle was on.
        """
        import uuid  # noqa: PLC0415 — only used here

        if req.confirm != "WIPE":
            raise HTTPException(
                status_code=422,
                detail="confirm must be the literal string 'WIPE'",
            )
        dropped = wipe_for_relaunch(store)
        # Re-seed the catalog so the UI lands on a non-empty Personas page.
        try:
            seed_default_personas(store)
        except Exception:  # noqa: BLE001 — seeding failure is non-fatal here
            # The next qa-review pod restart will re-seed via the
            # startup hook regardless; the wipe itself succeeded.
            pass
        # #1108 — opt-in Mailpit content wipe. Done AFTER the Mongo wipe
        # so an unreachable Mailpit can't block the primary destructive
        # path the operator confirmed.
        mailpit_wiped = False
        mailpit_error: str | None = None
        if req.wipe_mailpit:
            try:
                resp = httpx.delete(
                    f"{settings.mailpit_admin_url}/api/v1/messages",
                    # CRITICAL — empty body, not `{"ids":[]}`. Mailpit
                    # treats DELETE /api/v1/messages with no body as
                    # "wipe everything" which is exactly the
                    # operator-confirmed semantic here.
                    timeout=10.0,
                )
                resp.raise_for_status()
                mailpit_wiped = True
            except Exception as exc:  # noqa: BLE001
                # Best-effort — the Mongo wipe already landed. Surface
                # the failure on the audit row so the operator can
                # re-run the per-run wipe-and-seed init container if
                # needed.
                mailpit_error = f"{type(exc).__name__}: {exc}"
        wipe_id = uuid.uuid4().hex[:16]
        audit = record_admin_wipe(
            store,
            wipe_id=wipe_id,
            dropped_counts=dropped,
            requester_note=req.requester_note,
        )
        # Attach the Mailpit outcome to the response so the UI can
        # confirm or surface the error. The audit row itself stays
        # focused on the Mongo wipe — Mailpit content is ephemeral by
        # design and not part of the destructive-history accounting.
        audit["mailpit_wiped"] = mailpit_wiped
        if mailpit_error is not None:
            audit["mailpit_error"] = mailpit_error
        return {"audit": audit, "dropped": dropped}

    @app.get(
        "/api/admin/wipes",
        tags=["Admin"],
        summary="List recent operator-triggered wipes (newest first).",
    )
    def api_admin_list_wipes(limit: int = 20) -> dict:
        """Return up to ``limit`` recent wipe audit rows.

        Drives the /admin page's "Recent wipes" list. Always 200 with a
        possibly-empty list — a fresh cluster has no wipes yet.
        """
        return {"wipes": list_admin_wipes(store, limit=int(limit))}

    @app.get("/health", tags=["System"], summary="Healthcheck endpoint for the API container.")
    def health() -> dict:
        """Liveness/readiness probe target — see k8s/qa/review-ui.yaml.

        Declared before the SPA catch-all so it always wins the route match.
        """
        return {"status": "ok"}

    # -- Site Model -------------------------------------------------------
    # Read the per-(tenant, target) Site Model (#2097) and CURATE site_knowledge
    # — the human pass over the heuristic by-design migration (91/94 entries).
    # All scoped to DEFAULT_TENANT; embeddings stripped from every response.
    @app.get("/api/site/targets", tags=["Site Model"])
    def api_site_targets() -> dict:
        return {
            "targets": [
                _strip_site_embeddings(t)
                for t in list_site_targets(store, DEFAULT_TENANT)
            ],
        }

    @app.post("/api/site/targets", tags=["Site Model"])
    def api_create_target(body: TargetCreate) -> dict:
        """Register a new site to test. The front door of the onboarding flow:
        creates the target at lifecycle ``registered``. base_url is required;
        target_id is slugified from the display name / host when omitted and
        de-duplicated with a numeric suffix."""
        base_url = body.base_url.strip()
        if not re.match(r"^https?://[^\s.]+\.[^\s]+", base_url):
            raise HTTPException(
                status_code=422,
                detail="base_url must be a full http(s):// URL, e.g. https://app.example.com",
            )
        requested = (body.target_id or "").strip()
        if requested:
            slug = _slugify(requested)
            if not slug:
                raise HTTPException(status_code=422, detail="target_id must be a slug")
            if get_site_target(store, DEFAULT_TENANT, slug) is not None:
                raise HTTPException(status_code=409, detail=f"target {slug!r} already exists")
        else:
            base = _slugify(body.display_name) or _target_host(base_url) or "site"
            slug = base
            n = 2
            while get_site_target(store, DEFAULT_TENANT, slug) is not None:
                slug = f"{base}-{n}"
                n += 1
        saved = upsert_site_target(
            store, tenant_id=DEFAULT_TENANT, target_id=slug, base_url=base_url,
            display_name=body.display_name or slug,
        )
        return _strip_site_embeddings(saved)

    @app.get("/api/site/targets/{target_id}", tags=["Site Model"])
    def api_site_target(target_id: str) -> dict:
        t = get_site_target(store, DEFAULT_TENANT, target_id)
        if t is None:
            raise HTTPException(status_code=404, detail=f"unknown target {target_id!r}")
        return _strip_site_embeddings(t)

    @app.get("/api/site/targets/{target_id}/surfaces", tags=["Site Model"])
    def api_site_surfaces(target_id: str) -> dict:
        rows = list_surfaces_by_target(store, DEFAULT_TENANT, target_id)
        return {"surfaces": [_strip_site_embeddings(r) for r in rows]}

    @app.get("/api/site/targets/{target_id}/flows", tags=["Site Model"])
    def api_site_flows(target_id: str) -> dict:
        rows = list_flows_by_target(store, DEFAULT_TENANT, target_id)
        return {"flows": [_strip_site_embeddings(r) for r in rows]}

    @app.get("/api/site/targets/{target_id}/knowledge", tags=["Site Model"])
    def api_site_knowledge(target_id: str) -> dict:
        rows = list_knowledge_by_target(store, DEFAULT_TENANT, target_id)
        return {"knowledge": [_strip_site_embeddings(r) for r in rows]}

    @app.post("/api/site/targets/{target_id}/knowledge", tags=["Site Model"])
    def api_create_knowledge(target_id: str, body: SiteKnowledgeCreate) -> dict:
        if body.kind not in _SITE_KNOWLEDGE_KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"kind must be one of {list(_SITE_KNOWLEDGE_KINDS)}",
            )
        entry_id = (body.entry_id or "").strip() or f"manual-{uuid.uuid4().hex[:12]}"
        if get_site_knowledge(store, DEFAULT_TENANT, target_id, entry_id) is not None:
            raise HTTPException(
                status_code=409, detail=f"entry {entry_id!r} already exists",
            )
        saved = upsert_site_knowledge(
            store, tenant_id=DEFAULT_TENANT, target_id=target_id,
            entry_id=entry_id, kind=body.kind, body=body.body,
            applies_to=body.applies_to, authored_by="operator",
        )
        return _strip_site_embeddings(saved)

    @app.patch("/api/site/knowledge/{entry_id}", tags=["Site Model"])
    def api_patch_knowledge(entry_id: str, body: SiteKnowledgePatch) -> dict:
        existing = get_site_knowledge(store, DEFAULT_TENANT, body.target_id, entry_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"unknown entry {entry_id!r}")
        kind = body.kind if body.kind is not None else existing.get("kind", "by_design")
        if kind not in _SITE_KNOWLEDGE_KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"kind must be one of {list(_SITE_KNOWLEDGE_KINDS)}",
            )
        new_body = body.body if body.body is not None else existing.get("body", "")
        applies = (
            body.applies_to if body.applies_to is not None
            else existing.get("applies_to", [])
        )
        # upsert replaces the doc and nulls the embedding fields, so an edit
        # forces the reconciler to re-embed (correct when the body changed).
        saved = upsert_site_knowledge(
            store, tenant_id=DEFAULT_TENANT, target_id=body.target_id,
            entry_id=entry_id, kind=kind, body=new_body, applies_to=applies,
            authored_by=existing.get("authored_by", "operator"),
        )
        return _strip_site_embeddings(saved)

    @app.delete("/api/site/knowledge/{entry_id}", tags=["Site Model"])
    def api_delete_knowledge(entry_id: str, target_id: str) -> dict:
        if not delete_site_knowledge(store, DEFAULT_TENANT, target_id, entry_id):
            raise HTTPException(status_code=404, detail=f"unknown entry {entry_id!r}")
        return {"deleted": True, "entry_id": entry_id}

    # -- Explorer questionnaire + lifecycle --------------------------------
    @app.get("/api/site/targets/{target_id}/questions", tags=["Site Model"])
    def api_site_questions(target_id: str) -> dict:
        """The target's questionnaire + its roll-up + onboarding lifecycle.
        Secret answers come back as a ``credential_ref`` pointer, never a
        value."""
        target = get_site_target(store, DEFAULT_TENANT, target_id)
        return {
            "questions": list_questions_by_target(store, DEFAULT_TENANT, target_id),
            "status": questionnaire_status(store, DEFAULT_TENANT, target_id),
            "lifecycle": (target or {}).get("lifecycle"),
            "lifecycle_states": list(LIFECYCLE_STATES),
        }

    @app.post("/api/site/targets/{target_id}/questions", tags=["Site Model"])
    def api_create_question(target_id: str, body: QuestionCreate) -> dict:
        if body.kind not in SITE_QUESTION_KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"kind must be one of {list(SITE_QUESTION_KINDS)}",
            )
        qid = (body.question_id or "").strip() or f"q-{uuid.uuid4().hex[:12]}"
        if "/" in qid:
            raise HTTPException(status_code=422, detail="question_id must not contain '/'")
        if get_site_question(store, DEFAULT_TENANT, target_id, qid) is not None:
            raise HTTPException(status_code=409, detail=f"question {qid!r} already exists")
        return upsert_site_question(
            store, target_id=target_id, question_id=qid, text=body.text,
            kind=body.kind, category=body.category, rationale=body.rationale,
            options=body.options, required=body.required, order=body.order,
            generated_by="operator",
        )

    def _advance_if_ready(target_id: str) -> None:
        """Once no required questions remain open, move an ``awaiting-answers``
        target to ``configured`` — the stepper progresses and the "Run the
        personas" CTA unlocks without the operator hunting the lifecycle menu.
        Forward-only and idempotent; a power user can still set it by hand."""
        target = get_site_target(store, DEFAULT_TENANT, target_id)
        if (target or {}).get("lifecycle") != "awaiting-answers":
            return
        st = questionnaire_status(store, DEFAULT_TENANT, target_id)
        if st.get("total", 0) > 0 and st.get("required_open", 0) == 0:
            set_target_lifecycle(store, DEFAULT_TENANT, target_id, "configured")

    @app.post(
        "/api/site/targets/{target_id}/questions/{question_id}/answer",
        tags=["Site Model"],
    )
    def api_answer_question(
        target_id: str, question_id: str, body: QuestionAnswer,
    ) -> dict:
        updated = answer_site_question(
            store, target_id=target_id, question_id=question_id,
            answer=body.answer, label=body.label,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail=f"unknown question {question_id!r}")
        _advance_if_ready(target_id)
        return updated

    @app.post(
        "/api/site/targets/{target_id}/questions/{question_id}/skip",
        tags=["Site Model"],
    )
    def api_skip_question(target_id: str, question_id: str) -> dict:
        updated = skip_site_question(
            store, target_id=target_id, question_id=question_id,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail=f"unknown question {question_id!r}")
        _advance_if_ready(target_id)
        return updated

    @app.delete(
        "/api/site/targets/{target_id}/questions/{question_id}",
        tags=["Site Model"],
    )
    def api_delete_question(target_id: str, question_id: str) -> dict:
        if not delete_site_question(store, DEFAULT_TENANT, target_id, question_id):
            raise HTTPException(status_code=404, detail=f"unknown question {question_id!r}")
        return {"deleted": True, "question_id": question_id}

    @app.post("/api/site/targets/{target_id}/lifecycle", tags=["Site Model"])
    def api_set_lifecycle(target_id: str, body: LifecycleSet) -> dict:
        if body.lifecycle not in LIFECYCLE_STATES:
            raise HTTPException(
                status_code=422,
                detail=f"lifecycle must be one of {list(LIFECYCLE_STATES)}",
            )
        updated = set_target_lifecycle(store, DEFAULT_TENANT, target_id, body.lifecycle)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"unknown target {target_id!r}")
        return updated

    @app.post("/api/site/targets/{target_id}/explore", tags=["Site Model"])
    def api_explore_target(target_id: str) -> dict:
        """Run the heuristic explorer: bootstrap the site model + questionnaire
        from the target's homepage and advance the lifecycle to
        ``awaiting-answers``. (v1 is GET-only HTML discovery — no token needed;
        the LLM/browser agent is a later layer.)"""
        from .explorer import explore_target  # noqa: PLC0415 — httpx only when used

        summary = explore_target(store, target_id, tenant_id=DEFAULT_TENANT)
        if summary is None:
            raise HTTPException(status_code=404, detail=f"unknown target {target_id!r}")
        return summary

    # -- Capabilities (grant deeper access) --------------------------------
    def _powers_for(capability_id: str) -> list[dict]:
        """The MCP server(s) a capability lights up when granted — for the
        "Powers the … tool" UI on a capability card. Names only, no secrets."""
        try:
            from qa_agents.mcp_catalog import servers_unlocked_by  # noqa: PLC0415
        except ImportError:
            return []
        return [
            {"server_id": s.id, "display_name": s.display_name, "friendly_name": s.friendly}
            for s in servers_unlocked_by(capability_id)
        ]

    def _capabilities_view(target_id: str) -> dict:
        """Catalog merged with this target's grant status + the depth roll-up.
        Secrets are pointers only — values are never returned."""
        grants = {
            g["capability_id"]: g
            for g in list_site_capabilities(store, DEFAULT_TENANT, target_id)
        }
        merged = []
        for cap in list_capabilities(store):
            g = grants.get(cap["capability_id"])
            merged.append({
                **cap,
                "status": g["status"] if g else "available",
                "credential_ref": (g or {}).get("credential_ref"),
                "config": (g or {}).get("config", {}),
                "proposed_by": (g or {}).get("proposed_by"),
                "powers": _powers_for(cap["capability_id"]),
            })
        return {
            "depth": capability_depth(store, DEFAULT_TENANT, target_id),
            "capabilities": merged,
        }

    @app.get("/api/capabilities", tags=["Capabilities"])
    def api_capability_catalog() -> dict:
        return {"capabilities": list_capabilities(store)}

    @app.get("/api/site/targets/{target_id}/capabilities", tags=["Capabilities"])
    def api_site_capabilities(target_id: str) -> dict:
        if get_site_target(store, DEFAULT_TENANT, target_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown target {target_id!r}")
        return _capabilities_view(target_id)

    @app.get("/api/site/targets/{target_id}/mcp", tags=["Capabilities"])
    def api_target_mcp(target_id: str) -> dict:
        """The MCP servers a target's *granted* capabilities light up for its
        runs (names only — credentials are never returned). Drives the New Run
        "auto-enabled from this site" hint."""
        if get_site_target(store, DEFAULT_TENANT, target_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown target {target_id!r}")
        try:
            from qa_agents.target_mcp import resolve_target_mcp  # noqa: PLC0415
            resolved = resolve_target_mcp(store, target_id)
        except ImportError:
            resolved = {"server_ids": [], "servers": []}
        return {"server_ids": resolved.get("server_ids", []),
                "servers": resolved.get("servers", [])}

    @app.put(
        "/api/site/targets/{target_id}/capabilities/{capability_id}",
        tags=["Capabilities"],
    )
    def api_set_capability(target_id: str, capability_id: str, body: CapabilityGrant) -> dict:
        if body.status not in CAPABILITY_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"status must be one of {list(CAPABILITY_STATUSES)}",
            )
        if get_capability(store, capability_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown capability {capability_id!r}")
        token = (body.token or "").strip() or None
        set_capability_status(
            store, target_id=target_id, capability_id=capability_id,
            status=body.status, token=token, config=body.config,
        )
        return _capabilities_view(target_id)

    @app.post("/api/site/targets/{target_id}/capabilities", tags=["Capabilities"])
    def api_custom_capability(target_id: str, body: CustomCapability) -> dict:
        """Connect a bespoke capability (the open-tail escape hatch): add a
        custom catalog entry and grant it for this target."""
        cap_id = f"custom-{uuid.uuid4().hex[:10]}"
        upsert_capability(
            store, capability_id=cap_id, title=body.title, unlocks=body.unlocks,
            category="custom", level=body.level, grant_kind=body.grant_kind,
        )
        token = (body.token or "").strip() or None
        set_capability_status(
            store, target_id=target_id, capability_id=cap_id, status="granted",
            token=token, config=body.config,
        )
        return _capabilities_view(target_id)

    @app.delete(
        "/api/site/targets/{target_id}/capabilities/{capability_id}",
        tags=["Capabilities"],
    )
    def api_revoke_capability(target_id: str, capability_id: str) -> dict:
        delete_site_capability(store, DEFAULT_TENANT, target_id, capability_id)
        return _capabilities_view(target_id)

    # -- BYOK: LLM backend config ------------------------------------------
    def _llm_status() -> dict:
        """Status only — never the token. A token counts as configured if it's
        vaulted (saved here) OR present in the environment (the cluster
        pattern)."""
        cfg = get_llm_config(store, DEFAULT_TENANT)
        backend = cfg["backend"]
        env_var = LLM_BACKEND_ENV.get(backend, "")
        vaulted = bool(get_llm_token(store, DEFAULT_TENANT))
        env_present = bool(os.environ.get(env_var, "").strip())
        source = "vault" if vaulted else ("env" if env_present else None)
        return {
            "backend": backend,
            "env_var": env_var,
            "token_configured": vaulted or env_present,
            "token_source": source,
            "backends": [
                {"id": b, "env": LLM_BACKEND_ENV.get(b, ""), **_LLM_BACKEND_META.get(b, {})}
                for b in LLM_BACKENDS
            ],
        }

    @app.get("/api/config/llm", tags=["Config"])
    def api_get_llm_config() -> dict:
        return _llm_status()

    @app.put("/api/config/llm", tags=["Config"])
    def api_set_llm_config(body: LLMConfigSet) -> dict:
        if body.backend not in LLM_BACKENDS:
            raise HTTPException(
                status_code=422,
                detail=f"backend must be one of {list(LLM_BACKENDS)}",
            )
        token = (body.token or "").strip() or None
        set_llm_config(store, backend=body.backend, token=token, tenant_id=DEFAULT_TENANT)
        return _llm_status()

    @app.delete("/api/config/llm/token", tags=["Config"])
    def api_clear_llm_token() -> dict:
        clear_llm_token(store, DEFAULT_TENANT)
        return _llm_status()

    # -- SPA static files --------------------------------------------------
    # Mounted last so /api/* always wins. If the dist dir is absent (local dev
    # without a build), the mount is skipped and only the API is served.
    if os.path.isdir(_SPA_DIR):
        app.mount(
            "/assets",
            StaticFiles(directory=os.path.join(_SPA_DIR, "assets")),
            name="assets",
        )

        @app.get("/{full_path:path}")
        def spa(full_path: str):  # noqa: ARG001 - path captured for SPA routing
            """Serve the SPA shell for any non-API path (client-side routing)."""
            index = os.path.join(_SPA_DIR, "index.html")
            return FileResponse(index)

    return app


def _lazy_app() -> FastAPI:
    """Build the production app from the environment.

    Kept behind a function so merely *importing* this module never opens a
    Mongo connection — ``connect()`` builds indexes eagerly, which would fail
    at import time in a test process with no Mongo. ``uvicorn`` calls the
    factory; tests call ``create_app(store=...)`` directly.
    """
    return create_app()


# For ``uvicorn qa_review_api.app:app --factory``.
app = _lazy_app

