"""The ``slyreply_qa`` schema and its access functions.

Three collections:

* ``qa_runs``      — one doc per orchestrated harness run (all personas of one
  job share a ``run_id``). Carries the per-persona reviews, the run totals,
  and the lifecycle ``status``.
* ``qa_findings``  — one doc per structured finding, joined to a run by
  ``run_id``. Each finding has its own triage ``status``.
* ``qa_run_steps`` (#860) — one doc per tool call the persona issued during
  the explore phase. Carries the tool name + a compact args summary, any
  prose text the persona said BEFORE the call, an optional GridFS oid
  pointing at a captured Playwright screenshot (the related blob lives in
  ``qa_screenshots`` via GridFS), and the ordinals of any findings filed
  AT this step (so the review UI can render a finding↔step linkback).

Plus a GridFS bucket ``qa_screenshots`` carrying the actual PNG bytes (see
``qa_store.screenshots``). Storing screenshots in GridFS rather than the
``qa_run_steps`` doc keeps individual step docs small (Mongo's 16 MB doc
cap is plenty for metadata but laughable for 30 PNGs per persona).

Everything here is pymongo and stdlib only — no Agent SDK, no FastAPI. Both
the harness's ``AtlasReportSink`` and the review UI API import this module.

Write functions are idempotent where it is sensible: ``create_run`` upserts on
``run_id``; ``add_persona_result`` replaces the persona's slice (review entry +
that persona's findings) rather than appending duplicates, so a re-run of one
persona is safe.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from pymongo import ASCENDING, DESCENDING, MongoClient

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------
RUN_STATUSES = ("new", "reviewed", "filed", "dismissed")
FINDING_STATUSES = ("open", "included", "dismissed")

_RUNS = "qa_runs"
_FINDINGS = "qa_findings"
# #860 — per-tool-call records that back the review UI's Transcript tab.
# Separate collection (not embedded in qa_runs) because a run can have
# hundreds of steps per persona × 12 personas × ~50 runs/month — embedded
# arrays would push qa_runs docs toward the 16 MB cap, and the access
# pattern (paginate steps by persona) is naturally a separate collection.
_STEPS = "qa_run_steps"
# #862 — saved scenarios. One doc per named {persona + mandatory-action
# selection} preset the operator can reload + retrigger from the UI.
# Tiny collection (~tens of docs lifetime); separate from qa_runs because
# scenarios are run-shape templates, not records of past runs.
_SCENARIOS = "qa_scenarios"

# #902/#903 — per-emit narrative archive. Every _emit_* call from
# runner.py (text + tool_use + tool_result + result + heartbeat +
# system) lands one doc here, alongside the per-tool-call records in
# qa_run_steps. qa_run_steps tells you *what* the persona did;
# qa_run_logs tells you *why* (the LLM narration between calls).
# Today that narration only exists in `kubectl logs` and dies with
# the Job — slice 1 of the QA-insights epic starts persisting it so
# slice 2 (search UI) + slice 3 (analyzer) have substrate.
# TTL 180d — long enough to support a quarterly cross-run analysis
# pass, short enough that the volume (one doc per ~5 turns per
# persona × ~12 personas × ~50 runs/month → ~30k docs/month) stays
# bounded.
_RUN_LOGS = "qa_run_logs"
RUN_LOG_TTL_SECONDS = 180 * 86400
RUN_LOG_KINDS = ("text", "tool_use", "tool_result", "result", "heartbeat", "system")

# QA Studio redesign — qa_personas is the editable persona library that
# becomes the runtime source of truth in place of the hardcoded PERSONAS
# dict in harness/qa_agents/personas.py. The 12 default personas still
# ship in code; on first boot the FastAPI app seeds them into this
# collection, and from there the UI can edit / clone / create new
# personas without redeploying. ``is_default`` flags the seeded set —
# those rows can be reset back to factory defaults but not hard-
# deleted (a ``hidden`` flag handles the equivalent).
_PERSONAS = "qa_personas"

# #1146 — admin-audit collection. Records every wipe via the
# operator's nuclear-button flow so the /admin "Recent wipes" list
# can attribute resets across time. DELIBERATELY
# excluded from ``wipe_for_relaunch``'s drop list (qa_store/wipe.py)
# so audit history survives across resets. Shape:
#   {wipe_id, wiped_at, dropped_counts: {col: N, ...},
#    requester_note: str, requester: str}
# Append-only from operator action; no edit/delete API.
_ADMIN_AUDIT = "qa_admin_audit"

# Cosmetic tokens the persona cards + avatars draw from. Open by design:
# the UI uses a known palette but a custom value falls back to a
# neutral. Pinned at 12 to match the seeded persona count so each
# default persona can have a unique colour out of the box.
PERSONA_COLOR_TOKENS = (
    "teal", "amber", "rose", "indigo", "emerald", "violet",
    "sky", "fuchsia", "lime", "orange", "cyan", "slate",
)

# Slice 1 of the Test Ease discovery loop (#1002, follows the spike
# in #1000). Three collections capture distillation output PER persona-
# run row — NOT canonical-merged yet. Slice 2 (#TBD) will fold these
# into a canonical catalog with the cross-run dedup pass the spike
# already validated produces good output.
#
# Why three collections vs one schemaless: queries diverge. The UI
# needs separate lists of "actions discovered" (the coverage corpus),
# "tools used" (the fixture catalog seed), and "branches noticed"
# (the variant-generator seed). Each gets its own surface.
#
# All three keep ``doc_id`` as the unique key in
# ``<run_id>:<persona_id>:<artifact_id>`` form, so a re-distillation
# of the same persona's run overwrites in place rather than
# duplicating — same upsert pattern as every other write here.
_DISCOVERED_ACTIONS = "qa_discovered_actions"
_DISCOVERED_TOOLS = "qa_discovered_tools"
_DISCOVERED_BRANCHES = "qa_discovered_branches"
# Single source of truth for the action categories the distillation
# emits. Exported so the API can validate query-string filters; kept
# narrow on purpose (the model is asked to map everything to one of
# these or "other", which forces consistency across runs).
DISCOVERED_ACTION_CATEGORIES = (
    "auth", "billing", "agents", "playground",
    "account", "contact", "docs", "admin", "other",
)

_DEFAULT_URL = "mongodb://localhost:27017"
_DEFAULT_DB = "slyreply_qa"

# ── Site Model (per-(tenant, target) site knowledge as DATA) ──────────────
# The product (Test Ease) points at any site for any tenant; everything the
# agent knows about a target — its map, its test plan, and the "by design /
# don't worry about X" suppressions we used to HARDCODE in personas.py —
# lives here as rows keyed by (tenant_id, target_id), not code. Single-tenant
# for now via DEFAULT_TENANT; the column is in place so multi-tenant is a data
# change, not a schema one.
DEFAULT_TENANT = "default"
_SITE_TARGETS = "site_targets"       # one row per (tenant, target): url/auth/scope
_SITE_SURFACES = "site_surfaces"     # discovered pages/forms/flows/apis/entities
_TEST_FLOWS = "test_flows"           # the test plan (replaces hardcoded `flows`)
_SITE_KNOWLEDGE = "site_knowledge"   # by-design/known-issue/guidance (mirrors
                                     # Sherman's knowledge_base shape so the same
                                     # reconciler/retriever generalise later)
_SITE_SECRETS = "site_secrets"       # the VAULT: encrypted per-target secret
                                     # values. The Site Model (auth.credential_ref,
                                     # site_questions) stores only POINTERS into
                                     # this; raw secrets never live in the model.
_SITE_QUESTIONS = "site_questions"   # the explorer's per-target human
                                     # questionnaire — one row per question;
                                     # secret answers are vaulted (credential_ref),
                                     # never stored inline.
_QA_CONFIG = "qa_config"             # instance-level config (one doc per
                                     # (tenant, key)): the LLM backend choice +
                                     # a vault POINTER to the BYOK token. See
                                     # qa_store.app_config.
# Allowed enum-ish values, kept narrow on purpose (consistency across rows).
SITE_AUTH_METHODS = ("none", "form", "magic_link", "oauth")
SITE_SURFACE_KINDS = ("page", "form", "auth_flow", "api", "entity")
SITE_KNOWLEDGE_KINDS = ("by_design", "known_issue", "guidance", "glossary")
# Questionnaire answer types + per-question status (see qa_store.site_questions).
SITE_QUESTION_KINDS = ("free_text", "secret", "choice", "boolean", "url", "number")
SITE_QUESTION_STATUSES = ("open", "answered", "skipped")


# ---------------------------------------------------------------------------
# Connection handle.
# ---------------------------------------------------------------------------
@dataclass
class Store:
    """A thin handle bundling the client and the two collections.

    Every access function takes a ``Store`` as its first argument, so callers
    (the harness sink, the FastAPI app) connect once and pass it around.
    """

    client: MongoClient
    db_name: str

    @property
    def db(self):
        return self.client[self.db_name]

    @property
    def runs(self):
        return self.db[_RUNS]

    @property
    def findings(self):
        return self.db[_FINDINGS]

    @property
    def steps(self):
        """The ``qa_run_steps`` collection (#860 — Transcript tab data)."""
        return self.db[_STEPS]

    @property
    def scenarios(self):
        """The ``qa_scenarios`` collection (#862 — saved run-shape presets)."""
        return self.db[_SCENARIOS]

    @property
    def run_logs(self):
        """The ``qa_run_logs`` collection (#902/#903 — per-emit
        narrative archive feeding the QA-insights epic's search +
        cross-run analyzer)."""
        return self.db[_RUN_LOGS]

    @property
    def personas(self):
        """The ``qa_personas`` collection — editable persona library
        seeded from the hardcoded PERSONAS dict on first boot."""
        return self.db[_PERSONAS]

    @property
    def discovered_actions(self):
        """The ``qa_discovered_actions`` collection (Slice 1 of #1002).
        Per persona-run rows — one document per (run, persona, action)."""
        return self.db[_DISCOVERED_ACTIONS]

    @property
    def discovered_tools(self):
        """The ``qa_discovered_tools`` collection (Slice 1 of #1002).
        Per persona-run rows — what tools the persona identified at
        its disposal (mailpit, revolut sandbox, etc.)."""
        return self.db[_DISCOVERED_TOOLS]

    @property
    def discovered_branches(self):
        """The ``qa_discovered_branches`` collection (Slice 1 of #1002).
        Free-text "things the persona noticed but didn't try" — feed
        for the future variant generator."""
        return self.db[_DISCOVERED_BRANCHES]

    @property
    def admin_audit(self):
        """The ``qa_admin_audit`` collection (#1146).
        Append-only log of operator-triggered wipes. Survives wipes
        deliberately so the /admin "Recent wipes" list can show a chain
        of resets across time."""
        return self.db[_ADMIN_AUDIT]

    # ── Site Model collections ──
    @property
    def site_targets(self):
        """``site_targets`` — one row per (tenant, target): base_url, auth,
        scope, ownership. Replaces QA_WEB_BASE_URL + hardcoded admin creds."""
        return self.db[_SITE_TARGETS]

    @property
    def site_surfaces(self):
        """``site_surfaces`` — the discovered map of a target (pages, forms,
        auth flows, apis, entities). Populated by the crawler / personas."""
        return self.db[_SITE_SURFACES]

    @property
    def test_flows(self):
        """``test_flows`` — the per-target test plan. Replaces the hardcoded
        ``flows`` lists on each persona."""
        return self.db[_TEST_FLOWS]

    @property
    def site_knowledge(self):
        """``site_knowledge`` — by-design / known-issue / guidance entries
        scoped to a target. Replaces the code-level BY-DESIGN suppressions;
        mirrors Sherman's knowledge_base shape (body + body_embedding)."""
        return self.db[_SITE_KNOWLEDGE]

    @property
    def site_secrets(self):
        """``site_secrets`` — the VAULT. One row per stored secret, the value
        encrypted via ``qa_store.crypto``. Everything else in the Site Model
        references a secret by a ``credential_ref`` POINTER, never inline;
        ``qa_store.vault`` is the only module that reads/writes raw values."""
        return self.db[_SITE_SECRETS]

    @property
    def site_questions(self):
        """``site_questions`` — the explorer's per-target questionnaire. One row
        per question; secret answers are vaulted (the row carries a
        ``credential_ref``, never the raw value). See ``qa_store.site_questions``."""
        return self.db[_SITE_QUESTIONS]

    @property
    def qa_config(self):
        """``qa_config`` — instance-level config (LLM backend + a vault pointer
        to the BYOK token). See ``qa_store.app_config``."""
        return self.db[_QA_CONFIG]

    def close(self) -> None:
        self.client.close()


def connect(url: str | None = None, db: str | None = None) -> Store:
    """Connect to the ``slyreply_qa`` store and ensure its indexes.

    ``url`` / ``db`` default to ``QA_STORE_URL`` / ``QA_STORE_DB`` from the
    environment (and then to a localhost / ``slyreply_qa`` fallback), so a
    bare ``connect()`` works both in the sandbox Job and a local dev shell.
    """
    url = url or os.environ.get("QA_STORE_URL", _DEFAULT_URL)
    db = db or os.environ.get("QA_STORE_DB", _DEFAULT_DB)
    client: MongoClient = MongoClient(url)
    store = Store(client=client, db_name=db)
    _ensure_indexes(store)
    # NB: the Site Model $vectorSearch indexes are NOT ensured here — connect()
    # is on a hot path (runner.py opens several stores per run) and search-index
    # admin round-trips don't belong there. They're ensured once at real boot
    # points instead: the control-room app startup (create_app) and the
    # deterministic `python -m qa_store.init_vector_indexes` one-shot. See
    # ensure_vector_indexes.
    return store


def _ensure_indexes(store: Store) -> None:
    """Create the indexes the access patterns rely on. Idempotent."""
    store.runs.create_index([("run_id", ASCENDING)], unique=True, name="run_id_unique")
    store.runs.create_index([("started_at", DESCENDING)], name="started_at_desc")
    store.findings.create_index(
        [("finding_id", ASCENDING)], unique=True, name="finding_id_unique"
    )
    store.findings.create_index([("run_id", ASCENDING)], name="run_id")
    # Slice 2 of #1104 — cross-run dedup key on findings. The active
    # access pattern is find_prior_finding(persona, category, title_hash)
    # ordered by created_at DESC. mongomock supports compound secondary
    # indexes; the compound here gives both the equality filter and the
    # ordering hint a single index can satisfy.
    store.findings.create_index(
        [
            ("persona", ASCENDING),
            ("category", ASCENDING),
            ("title_hash", ASCENDING),
            ("created_at", DESCENDING),
        ],
        name="persona_category_title_hash_created",
    )
    # #860 — Transcript tab. Unique on (run_id, persona_id, step_n) so a
    # re-played persona overwrites in place rather than duplicating; the
    # compound also doubles as the natural read index (list_steps_for_persona
    # filters by run_id+persona_id and sorts by step_n).
    store.steps.create_index(
        [("run_id", ASCENDING), ("persona_id", ASCENDING), ("step_n", ASCENDING)],
        unique=True,
        name="run_persona_step_unique",
    )
    # #862 — saved scenarios. Unique on id (the stable slug the UI uses
    # for routing + the lookup key for every CRUD endpoint). Secondary
    # index on updated_at to power a "most recently edited" list view in
    # a future iteration — the dashboard today just iterates everything,
    # but the secondary index is cheap and futureproofs that pattern.
    store.scenarios.create_index(
        [("id", ASCENDING)], unique=True, name="scenario_id_unique",
    )
    store.scenarios.create_index(
        [("updated_at", DESCENDING)], name="scenario_updated_at_desc",
    )
    # #902/#903 — narrative emits archive. Three indexes for the three
    # known access patterns:
    #   1. (run_id, persona_id, seq) — replay a single persona's run in
    #      order. Slice 2's Transcript Search and slice 3's analyzer
    #      both stream by this key.
    #   2. (persona_id, ts) — cross-run queries like "every margaret
    #      emit in the last 7 days" without scanning every run_id.
    #   3. TTL on ts at RUN_LOG_TTL_SECONDS — caps storage growth
    #      without needing a separate prune job.
    store.run_logs.create_index(
        [("run_id", ASCENDING), ("persona_id", ASCENDING), ("seq", ASCENDING)],
        name="run_persona_seq",
    )
    store.run_logs.create_index(
        [("persona_id", ASCENDING), ("ts", DESCENDING)],
        name="persona_recent",
    )
    store.run_logs.create_index(
        "ts", expireAfterSeconds=RUN_LOG_TTL_SECONDS, name="ts_ttl",
    )
    # QA Studio — qa_personas. Two indexes:
    #   1. persona_id unique — stable slug used for routing, FK in other
    #      collections, and the seed-upsert key.
    #   2. is_default flag + display_name — lets the UI list seeded
    #      personas first, then user-created ones, without a sort-scan.
    store.personas.create_index(
        [("persona_id", ASCENDING)], unique=True, name="persona_id_unique",
    )
    store.personas.create_index(
        [("is_default", DESCENDING), ("display_name", ASCENDING)],
        name="default_name",
    )
    # Slice 1 of #1002 — qa_discovered_* trio. Each collection has the
    # same index shape because they share the same access pattern:
    #
    #   1. doc_id unique — the upsert key. Same composite-id pattern as
    #      memory_id/finding_id; re-distilling a run overwrites in place.
    #   2. (run_id, persona_id) — the run-detail "Discovered" tab and
    #      the per-persona drill-down both filter on this pair.
    #   3. distilled_at DESC — the corpus-wide "/discovered" page sorts
    #      newest-first.
    #   4. (only on actions) category — the corpus page's category
    #      filter; mongomock honours single-field secondary indexes,
    #      so the test suite exercises this path too.
    for coll in (
        store.discovered_actions,
        store.discovered_tools,
        store.discovered_branches,
    ):
        coll.create_index([("doc_id", ASCENDING)], unique=True, name="doc_id_unique")
        coll.create_index(
            [("run_id", ASCENDING), ("persona_id", ASCENDING)],
            name="run_persona",
        )
        coll.create_index([("distilled_at", DESCENDING)], name="distilled_at_desc")
    store.discovered_actions.create_index(
        [("category", ASCENDING)], name="category",
    )
    # #1146 — admin-audit. Unique on wipe_id (the operator-supplied
    # stable id from the API layer); secondary index on wiped_at for
    # the "recent wipes, newest first" list view.
    store.admin_audit.create_index(
        [("wipe_id", ASCENDING)], unique=True, name="wipe_id_unique",
    )
    store.admin_audit.create_index(
        [("wiped_at", DESCENDING)], name="wiped_at_desc",
    )

    # ── Site Model — every doc keyed by (tenant_id, target_id) ──
    # Compound (tenant_id, target_id) backs list_by_target on all four; a
    # unique (tenant_id, target_id, <entity>_id) enforces one row per entity
    # within a target and makes the upserts collision-safe.
    store.site_targets.create_index(
        [("tenant_id", ASCENDING), ("target_id", ASCENDING)],
        unique=True, name="tenant_target_unique",
    )
    store.site_surfaces.create_index(
        [("tenant_id", ASCENDING), ("target_id", ASCENDING)],
        name="tenant_target",
    )
    store.site_surfaces.create_index(
        [("tenant_id", ASCENDING), ("target_id", ASCENDING), ("surface_id", ASCENDING)],
        unique=True, name="tenant_target_surface_unique",
    )
    store.test_flows.create_index(
        [("tenant_id", ASCENDING), ("target_id", ASCENDING)],
        name="tenant_target",
    )
    store.test_flows.create_index(
        [("tenant_id", ASCENDING), ("target_id", ASCENDING), ("flow_id", ASCENDING)],
        unique=True, name="tenant_target_flow_unique",
    )
    store.site_knowledge.create_index(
        [("tenant_id", ASCENDING), ("target_id", ASCENDING)],
        name="tenant_target",
    )
    store.site_knowledge.create_index(
        [("tenant_id", ASCENDING), ("target_id", ASCENDING), ("entry_id", ASCENDING)],
        unique=True, name="tenant_target_entry_unique",
    )
    # ── Vault (site_secrets) — one row per (tenant, target, ref). The unique
    # compound is the credential_ref's natural key; list_secret_refs reads by
    # (tenant, target).
    store.site_secrets.create_index(
        [("tenant_id", ASCENDING), ("target_id", ASCENDING)],
        name="tenant_target",
    )
    store.site_secrets.create_index(
        [("tenant_id", ASCENDING), ("target_id", ASCENDING), ("ref", ASCENDING)],
        unique=True, name="tenant_target_ref_unique",
    )
    # ── Questionnaire (site_questions) — list_questions_by_target reads by
    # (tenant, target) ordered by (order, question_id); unique question_id
    # within a target makes the explorer's re-upserts collision-safe.
    store.site_questions.create_index(
        [("tenant_id", ASCENDING), ("target_id", ASCENDING)],
        name="tenant_target",
    )
    store.site_questions.create_index(
        [
            ("tenant_id", ASCENDING),
            ("target_id", ASCENDING),
            ("question_id", ASCENDING),
        ],
        unique=True, name="tenant_target_question_unique",
    )
    # ── Instance config (qa_config) — one doc per (tenant, key).
    store.qa_config.create_index(
        [("tenant_id", ASCENDING), ("key", ASCENDING)],
        unique=True, name="tenant_key_unique",
    )
# ---------------------------------------------------------------------------
# Atlas Search vector indexes — distinct from the compound indexes above, which
# plain mongod creates but can't power $vectorSearch. mongodb-atlas-local
# bundles mongot, so these run locally with no cloud Atlas.
#
# The (collection, index-name, embedded-field) triples MUST stay in lockstep
# with qa_store.site_retriever (KB_INDEX_NAME / SURFACES_INDEX_NAME +
# *_EMBEDDED_FIELD). They're duplicated as literals here rather than imported to
# avoid a schema ←→ site_retriever import cycle (site_retriever imports Store
# from this module).
_VECTOR_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("site_knowledge", "site_knowledge_vector", "body_embedding"),
    ("site_surfaces", "site_surfaces_vector", "description_embedding"),
)
# Filter fields both indexes declare so $vectorSearch can scope per
# (tenant, target[, kind]) DURING the ANN search — site_retriever builds
# exactly this filter.
_VECTOR_FILTER_FIELDS: tuple[str, ...] = ("tenant_id", "target_id", "kind")
# numDimensions of the DEFAULT (local) embedding model — bge-small is 384-d.
# Selecting the OpenAI provider (1536-d) means recreating these indexes at the
# new dimension and re-embedding; see qa_store.embeddings.DEFAULT_EMBEDDING_DIM.
DEFAULT_VECTOR_DIM = 384


def ensure_vector_indexes(store: Store, *, dim: int = DEFAULT_VECTOR_DIM) -> list[str]:
    """Create the Site Model ``$vectorSearch`` indexes if they're missing.

    Idempotent and **best-effort**: returns the names it actually created (empty
    if all already exist, or if this deployment has no Atlas Search engine).
    Safe to call on every boot.

    Requires a deployment with Atlas Search — ``mongodb-atlas-local`` (which
    bundles mongot) or cloud Atlas. On a plain ``mongod`` or ``mongomock`` the
    search-index API is absent or rejected; we log and skip rather than raise,
    because retrieval is enrichment (``site_retriever`` returns ``[]`` on a
    missing index). The same tolerance covers the window after mongod is
    healthy but before mongot is ready to accept index creation.
    """
    from pymongo.operations import SearchIndexModel  # pymongo ≥4.7 (vectorSearch type)

    def _index_names(coll) -> set[str]:
        return {ix["name"] for ix in coll.list_search_indexes()}

    created: list[str] = []
    for attr, index_name, path in _VECTOR_INDEXES:
        coll = getattr(store, attr)
        try:
            existing = _index_names(coll)
        except Exception:  # noqa: BLE001 — no Atlas Search here / mongot not ready
            log.info(
                "vector-index ensure skipped: Atlas Search unavailable "
                "(plain mongod, mongomock, or mongot not yet ready)"
            )
            return created
        if index_name in existing:
            continue
        model = SearchIndexModel(
            name=index_name,
            type="vectorSearch",
            definition={
                "fields": [
                    {
                        "type": "vector",
                        "path": path,
                        "numDimensions": dim,
                        "similarity": "cosine",
                    },
                    *(
                        {"type": "filter", "path": field}
                        for field in _VECTOR_FILTER_FIELDS
                    ),
                ],
            },
        )
        try:
            coll.create_search_index(model)
            created.append(index_name)
            log.info("created vector index %s on %s (dim=%d)", index_name, attr, dim)
        except Exception:  # noqa: BLE001
            # A concurrent ensurer (e.g. app startup + the init one-shot both
            # booting on a cold stack) may have created it between our list and
            # our create — that's a benign idempotent win, not a failure. Only
            # warn if the index genuinely isn't there afterwards.
            try:
                present = index_name in _index_names(coll)
            except Exception:  # noqa: BLE001
                present = False
            if present:
                log.debug("vector index %s on %s already created by a peer", index_name, attr)
            else:
                log.warning(
                    "failed to create vector index %s on %s", index_name, attr,
                    exc_info=True,
                )
    return created


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(UTC)


def _strip_id(doc: dict | None) -> dict | None:
    """Drop the Mongo ``_id`` so docs round-trip cleanly as JSON."""
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


def finding_id(run_id: str, persona: str, ordinal: int) -> str:
    """Build a stable, deterministic finding id.

    Deterministic so re-inserting a persona's findings (``add_persona_result``
    called twice) upserts in place rather than duplicating.
    """
    return f"{run_id}:{persona}:{ordinal}"


def normalised_title_hash(title: str) -> str:
    """Stable hash used to dedup findings across runs (#1106).

    The cross-run dedup key for findings is ``(persona, category,
    title_hash)``. Two slightly different but semantically identical
    titles from different runs ("Login button doesn't render" vs "Login
    button doesn't render!") should collide on this hash so the second
    sighting bumps the prior's ``recurring_count`` instead of creating
    a duplicate row.

    Normalisation steps (in order):
      1. lowercase
      2. strip leading/trailing whitespace
      3. drop all non-alphanumeric, non-whitespace characters
         (punctuation, trailing-question-mark variance, em-dashes)
      4. collapse runs of whitespace to one space

    A 16-hex-char SHA-1 prefix is the storage form. Full SHA-1 would
    also work; the truncation saves a few bytes per finding and the
    collision domain is per ``(persona, category)`` pair where any
    practical persona produces low thousands of findings over the
    project's lifetime — collision probability is effectively zero.
    """
    import hashlib
    import re
    cleaned = (title or "").lower().strip()
    cleaned = re.sub(r"[^\w\s]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Run lifecycle.
# ---------------------------------------------------------------------------
def create_run(
    store: Store,
    run_id: str,
    personas: list[str],
    *,
    run_notes: str = "",
    config_snapshot: dict | None = None,
    expected_personas: list[str] | None = None,
) -> dict:
    """Create (or upsert) the run document. Idempotent on ``run_id``.

    A re-call merges the persona list (union, order-preserving) and never
    clobbers reviews/totals already written for the run.

    ``run_notes`` (#858) — the operator-facing "what was this run about"
    label set by the review-UI trigger. Empty string is "no notes" and
    renders as an empty cell in the runs list. Once set, it is NOT
    overwritten by subsequent ``create_run`` calls for the same run_id
    (re-call with empty notes preserves the original label).

    ``config_snapshot`` (#858) — dict of the run's resolved config knobs
    (max_turns, concurrency, explore_model, report_model). Persisted so a
    future operator can answer "what knobs did this run use?" months
    later. Like ``run_notes``, sticky on re-call. ``None`` means "don't
    write a snapshot" (preserves whatever's already there); ``{}`` is
    valid (records that no overrides were used).

    ``expected_personas`` (#1821) — the COMPLETE set of personas the run
    will cover, known up front by the orchestrator. UNLIKE the incremental
    ``personas`` list (which unions as each pod/persona checks in), this is
    the run's *denominator* for the multi-pod finish barrier: a run is only
    "everyone's done" when the distinct reviewed personas ⊇ this set (see
    :func:`all_personas_reviewed`). Because it's the denominator, it is set
    ONCE at run creation and is STICKY — a later ``create_run`` re-upsert
    (e.g. from a per-pod ``add_persona_result`` auto-create) must NOT
    clobber or grow it. ``None`` (the default, single-pod/legacy callers)
    falls back to the ``personas`` argument so single-pod behaviour is
    unchanged — the run that declares its full persona list as ``personas``
    on the one and only ``create_run`` call gets the same set as its
    expected denominator for free.
    """
    existing = store.runs.find_one({"run_id": run_id})
    if existing is not None:
        merged = list(existing.get("personas") or [])
        for p in personas:
            if p not in merged:
                merged.append(p)
        update: dict = {"personas": merged}
        # Sticky merge: only write notes if the new value is non-empty AND
        # the existing doc doesn't already have notes set. Lets the first
        # writer's value win even if a later create_run call lacks it.
        if run_notes and not (existing.get("run_notes") or ""):
            update["run_notes"] = run_notes
        if config_snapshot is not None and not existing.get("config_snapshot"):
            update["config_snapshot"] = dict(config_snapshot)
        # Sticky, set-once: only seed expected_personas if the existing doc
        # never got one (e.g. an add_persona_result auto-create raced ahead
        # of the orchestrator's create_run). Never union-merge it — the
        # first writer that supplies the full set is the denominator.
        if expected_personas is not None and not existing.get("expected_personas"):
            update["expected_personas"] = list(expected_personas)
        store.runs.update_one({"run_id": run_id}, {"$set": update})
        return _strip_id(store.runs.find_one({"run_id": run_id}))

    doc = {
        "run_id": run_id,
        "started_at": _now(),
        "finished_at": None,
        "status": "new",
        "personas": list(personas),
        # The finish-barrier denominator (#1821). Set once here; never
        # union-merged on re-upsert. Defaults to the incremental persona
        # list so single-pod callers (one create_run with the full set)
        # behave exactly as before.
        "expected_personas": list(
            expected_personas if expected_personas is not None else personas
        ),
        # Multi-pod finish barrier claim flag (#1821). Flipped exactly once
        # by the winning caller of claim_run_finish; absent/False means
        # "nobody has run the finalisation yet".
        "finish_claimed": False,
        "reviews": [],
        # #1822 — token counts only. The per-run dollar conversion was
        # retired (runs bill the operator's flat-rate Claude Code Max
        # subscription); pre-#1822 documents may still carry ``cost_usd``
        # / ``real_cost_usd`` keys, which readers pass through untouched.
        "totals": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_tokens": 0,
        },
        "gh_issue_url": None,
        "discord_url": None,
        # Sticky after first write; empty string is fine and means "no
        # operator label". list_runs returns whatever's stored verbatim.
        "run_notes": run_notes,
        # Empty dict if the caller didn't pass anything — keeps the field
        # shape stable for the review UI's renderer.
        "config_snapshot": dict(config_snapshot) if config_snapshot else {},
    }
    store.runs.insert_one(doc)
    return _strip_id(store.runs.find_one({"run_id": run_id}))


def add_persona_result(
    store: Store,
    run_id: str,
    persona: str,
    review_markdown: str,
    verdict: str,
    accounting: dict,
    findings: list[dict],
) -> dict:
    """Attach one persona's result to a run: its review entry + its findings.

    Idempotent per persona — calling it twice for the same ``(run_id, persona)``
    replaces that persona's review slice and re-upserts that persona's findings
    (the finding ids are deterministic) instead of duplicating anything.

    The run is auto-created if it does not exist yet, so the sink does not have
    to order ``create_run`` before the first persona.
    """
    run = store.runs.find_one({"run_id": run_id})
    if run is None:
        create_run(store, run_id, [persona])
        run = store.runs.find_one({"run_id": run_id})

    # Ensure the persona is listed on the run.
    if persona not in (run.get("personas") or []):
        store.runs.update_one(
            {"run_id": run_id}, {"$push": {"personas": persona}}
        )

    review_entry = {
        "persona": persona,
        "review_markdown": review_markdown,
        "verdict": verdict,
        "accounting": accounting,
    }
    # Drop any prior review slice for this persona, then append the fresh one.
    store.runs.update_one(
        {"run_id": run_id},
        {"$pull": {"reviews": {"persona": persona}}},
    )
    store.runs.update_one(
        {"run_id": run_id},
        {"$push": {"reviews": review_entry}},
    )

    # Re-upsert this persona's findings. Deterministic ids keep it
    # idempotent; we also clear any stale extras from a previous longer
    # run.
    #
    # #1115 follow-up — pre-fix this did ``delete_many({run_id, persona})``
    # then ``replace_one`` for each, which obliterated ``gh_issue_*`` on
    # any finding the operator filed mid-run while the harness was still
    # streaming findings live (the ``upsert_live_finding`` writer below
    # is the live path). The new pattern preserves the filed state:
    #
    #   1. For each finding in the current list, upsert with ``$set`` on
    #      live fields and ``$setOnInsert`` on ``gh_issue_*`` + the
    #      Slice-2.0-of-#1104 dedup fields (created_at, recurring_count,
    #      last_verified_run_id, is_regression).
    #   2. Orphan-delete any (run_id, persona) finding whose id is NOT
    #      in the new list — but ONLY when it has no ``gh_issue_url``.
    #      Conservative cleanup so a mid-run filing is never lost.
    #
    # Slice 2.0 of #1104 adds four fields to each finding doc:
    #
    #   - title_hash: cross-run dedup key (see ``normalised_title_hash``).
    #     Set on every insert; never modified.
    #   - recurring_count: 1 on initial insert. Cross-run dedup (Slice
    #     2.1) calls ``bump_finding_recurring`` on the PRIOR finding
    #     instead of re-inserting; that's where increments happen.
    #   - last_verified_run_id: set to ``run_id`` on insert; reset by
    #     ``bump_finding_recurring`` on subsequent witnesses so the UI
    #     can show "last seen in run X".
    #   - is_regression: ``False`` on insert. Flipped by Slice 2.1 when
    #     a memory that was previously transitioned to ``fixed`` is
    #     observed broken again and produces a new finding.
    inserted: list[dict] = []
    new_ids: set[str] = set()
    for ordinal, f in enumerate(findings, 1):
        fid = finding_id(run_id, persona, ordinal)
        new_ids.add(fid)
        title = f.get("title", "(untitled)")
        # Live fields — the persona owns these, reapplied on every
        # reconciliation.
        set_fields = {
            "finding_id": fid,
            "run_id": run_id,
            "persona": persona,
            "category": f.get("category", "confusion"),
            "severity": f.get("severity", "minor"),
            "kind": f.get("kind", "bug"),
            "title": title,
            "title_hash": normalised_title_hash(title),
            "body": f.get("body", ""),
        }
        # Only set when the doc is new — preserves mid-run GH issue
        # filings and the Slice-2.0 dedup metadata across re-runs.
        set_on_insert = {
            "status": "open",
            "created_at": _now(),
            "recurring_count": 1,
            "last_verified_run_id": run_id,
            "is_regression": False,
            "gh_issue_url": None,
            "gh_issue_number": None,
            "gh_issue_state": None,
            "gh_issue_state_synced_at": None,
        }
        store.findings.update_one(
            {"finding_id": fid},
            {"$set": set_fields, "$setOnInsert": set_on_insert},
            upsert=True,
        )
        doc = store.findings.find_one({"finding_id": fid})
        inserted.append(_strip_id(doc))

    # Conservative orphan cleanup — delete only docs the operator
    # hasn't filed. ``Findings.add`` only appends in memory so ordinals
    # can't shrink within one persona's run; orphans realistically only
    # appear when a re-run produces a shorter list than a prior run
    # with the same (run_id, persona).
    store.findings.delete_many({
        "run_id": run_id,
        "persona": persona,
        "finding_id": {"$nin": list(new_ids)},
        "$or": [
            {"gh_issue_url": None},
            {"gh_issue_url": {"$exists": False}},
        ],
    })
    return {"review": review_entry, "findings": inserted}


# ---------------------------------------------------------------------------
# Live-streaming finding writer (#1115 follow-up).
#
# Background: pre-this-slice every ``note_finding`` call appended to an
# in-memory ``Findings`` collector in the harness, and the whole list
# flushed to qa-store at persona-end via :func:`add_persona_result`. The
# review UI's auto-refresh loop (4s) saw the findings "in waves" — one
# wave per persona completion. Operators wanted findings to appear
# seconds after each ``note_finding`` call instead.
#
# ``upsert_live_finding`` is the per-call writer. The harness's
# ``tools/findings.Findings.add`` returns the new ordinal; the MCP tool
# wrapper calls this with ``(run_id, persona, ordinal, finding_dict)``.
#
# Contract:
#   * Eager-creates the run document via :func:`create_run` — idempotent
#     with the later call from ``report.AtlasReportSink.write_summary``
#     so the persona-end reconciliation just unions cleanly.
#   * Upserts the finding doc using the same ``$set`` / ``$setOnInsert``
#     pattern as ``add_persona_result`` above, so the persona-end
#     reconciliation never undoes a mid-run GH issue filing.
#   * Safe to call multiple times for the same (run_id, persona,
#     ordinal) — live fields refresh, ``gh_issue_*`` stays put.
# ---------------------------------------------------------------------------
def upsert_live_finding(
    store: Store,
    run_id: str,
    persona: str,
    ordinal: int,
    finding: dict,
) -> dict:
    """Write one live finding to qa-store mid-persona.

    See the module-level comment above for the full contract. Returns
    the upserted doc (with ``_id`` stripped) — useful for tests; the
    harness caller can ignore it.
    """
    existing_run = store.runs.find_one({"run_id": run_id}, {"personas": 1})
    if existing_run is None:
        create_run(store, run_id, [persona])
    elif persona not in (existing_run.get("personas") or []):
        store.runs.update_one(
            {"run_id": run_id}, {"$push": {"personas": persona}}
        )

    fid = finding_id(run_id, persona, ordinal)
    title = finding.get("title", "(untitled)")
    set_fields = {
        "finding_id": fid,
        "run_id": run_id,
        "persona": persona,
        "category": finding.get("category", "confusion"),
        "severity": finding.get("severity", "minor"),
        "kind": finding.get("kind", "bug"),
        "title": title,
        "title_hash": normalised_title_hash(title),
        "body": finding.get("body", ""),
    }
    set_on_insert = {
        "status": "open",
        "created_at": _now(),
        "recurring_count": 1,
        "last_verified_run_id": run_id,
        "is_regression": False,
        "gh_issue_url": None,
        "gh_issue_number": None,
        "gh_issue_state": None,
        "gh_issue_state_synced_at": None,
    }
    store.findings.update_one(
        {"finding_id": fid},
        {"$set": set_fields, "$setOnInsert": set_on_insert},
        upsert=True,
    )
    return _strip_id(store.findings.find_one({"finding_id": fid}))


def finish_run(store: Store, run_id: str, totals: dict) -> dict:
    """Mark a run finished: stamp ``finished_at``, store ``totals``.

    Status is advanced ``new`` → ``reviewed`` (a finished run is ready for a
    human to look at); a run already past ``reviewed`` is left alone.
    """
    run = store.runs.find_one({"run_id": run_id})
    if run is None:
        raise KeyError(f"unknown run_id {run_id!r}")

    update: dict = {
        "finished_at": _now(),
        # #1822 — token counts only. The ``cost_usd`` / ``real_cost_usd``
        # dollar fields are no longer written for new runs (every run bills
        # the operator's flat-rate Claude Code Max subscription, so the
        # figure was a vanity number). Old run docs that carry them are
        # passed through by the readers untouched; a cost value passed in
        # ``totals`` by a stale caller is deliberately dropped here.
        "totals": {
            "input_tokens": int(totals.get("input_tokens", 0) or 0),
            "output_tokens": int(totals.get("output_tokens", 0) or 0),
            "cache_tokens": int(totals.get("cache_tokens", 0) or 0),
            # #882 — which LLM backend produced this run.
            # ``api`` (default) → org-API-billed; ``claude-code`` →
            # operator-Max-billed. Mixed runs aren't possible by
            # construction (one orchestrator run = one config = one
            # backend). Runs created before #882 lack this key —
            # readers should treat absence as ``api``.
            "backend": str(totals.get("backend", "api")),
        },
    }
    if run.get("status") == "new":
        update["status"] = "reviewed"
    store.runs.update_one({"run_id": run_id}, {"$set": update})
    return _strip_id(store.runs.find_one({"run_id": run_id}))


# ---------------------------------------------------------------------------
# Multi-pod finish barrier (#1821).
#
# When a QA run is sharded across N concurrent pods (one persona per pod),
# each pod writes its own persona's result via add_persona_result, then the
# LAST pod to finish is responsible for the run-level finalisation
# (finish_run + Discord/GH notification). "Last pod" is decided
# cooperatively at the data layer, not orchestrated out-of-band:
#
#   1. all_personas_reviewed(store, run_id) — has every expected persona
#      filed its review yet? (the denominator is expected_personas).
#   2. claim_run_finish(store, run_id) — an atomic compare-and-set so
#      exactly ONE of the N pods that observe (1) becomes True wins the
#      right to run the finalisation. The others get False and stand down.
#
# Single-pod runs (pod_count=1) still work unchanged: the one pod sees all
# personas reviewed, claims the finish, and finalises — identical to the
# pre-#1821 single-process flow.
# ---------------------------------------------------------------------------
def claim_run_finish(store: Store, run_id: str) -> bool:
    """Atomically claim the right to finalise a run. Win-once semantics.

    A compare-and-set on the ``finish_claimed`` flag:
    ``update_one({run_id, finish_claimed != True}, {$set: finish_claimed=True})``.
    Mongo guarantees the match-and-update is a single atomic operation, so
    across N concurrent callers EXACTLY ONE sees ``modified_count == 1`` and
    wins; every later caller matches zero docs (the flag is already True)
    and gets ``False``.

    Returns ``True`` iff THIS caller flipped the flag (i.e. won the claim),
    ``False`` otherwise — including when ``run_id`` is unknown (no doc to
    claim is "you didn't win", not an error: the caller's contract is "only
    finalise if you won").
    """
    result = store.runs.update_one(
        {"run_id": run_id, "finish_claimed": {"$ne": True}},
        {"$set": {"finish_claimed": True}},
    )
    return result.modified_count == 1


def all_personas_reviewed(store: Store, run_id: str) -> bool:
    """True iff every ``expected_personas`` entry has filed a review.

    The denominator is the run's ``expected_personas`` (set once at
    creation); the numerator is the distinct persona ids present in the
    run's ``reviews`` array (written by :func:`add_persona_result`). The
    barrier opens when the reviewed set is a SUPERSET of the expected set
    (superset, not equality, so an unexpected extra reviewer — a persona
    added mid-run — never blocks the finish).

    Returns ``False`` for an unknown ``run_id``. A run with an empty
    ``expected_personas`` is vacuously complete (returns ``True``), but in
    practice every run is created with at least one expected persona.
    """
    run = store.runs.find_one(
        {"run_id": run_id}, {"expected_personas": 1, "reviews": 1}
    )
    if run is None:
        return False
    expected = set(run.get("expected_personas") or [])
    reviewed = {r.get("persona") for r in (run.get("reviews") or [])}
    return expected <= reviewed


def mark_run_filed(store: Store, run_id: str, issue_url: str) -> dict:
    """Record that a human filed the GitHub issue for this run."""
    res = store.runs.update_one(
        {"run_id": run_id},
        {"$set": {"status": "filed", "gh_issue_url": issue_url}},
    )
    if res.matched_count == 0:
        raise KeyError(f"unknown run_id {run_id!r}")
    return _strip_id(store.runs.find_one({"run_id": run_id}))


# ---------------------------------------------------------------------------
# Per-finding reads + file-issue writes (#1115).
# ---------------------------------------------------------------------------
def get_finding(store: Store, finding_id: str) -> dict | None:
    """Return one finding by id, or None if it doesn't exist (#1115)."""
    doc = store.findings.find_one({"finding_id": finding_id})
    return _strip_id(doc) if doc else None


def mark_finding_filed(
    store: Store,
    finding_id: str,
    *,
    issue_url: str,
    issue_number: int,
) -> dict:
    """Record that the operator filed a GitHub issue for this finding (#1115).

    Mirrors :func:`mark_insight_filed`. Sets ``gh_issue_url`` +
    ``gh_issue_number`` on the finding. Raises ``KeyError`` if no such
    finding exists — the caller should know the id is valid.

    Does NOT change the finding's ``status`` (findings already carry their
    own open/included/dismissed lifecycle for the per-run review flow;
    filing an issue is orthogonal to that triage axis).
    """
    result = store.findings.update_one(
        {"finding_id": finding_id},
        {"$set": {
            "gh_issue_url": issue_url,
            "gh_issue_number": int(issue_number),
        }},
    )
    if result.matched_count == 0:
        raise KeyError(f"unknown finding_id {finding_id!r}")
    return _strip_id(store.findings.find_one({"finding_id": finding_id}))


def update_finding_gh_state(
    store: Store,
    finding_id: str,
    *,
    state: str | None,
    synced_at: datetime | None = None,
) -> dict:
    """Record the live GitHub state (open/closed) of a finding's linked
    issue (#1115). Mirrors :func:`update_insight_gh_state`. Bumps
    ``gh_issue_state_synced_at`` even on a failed fetch so the lazy
    refresh cache window holds."""
    when = synced_at or _now()
    result = store.findings.update_one(
        {"finding_id": finding_id},
        {"$set": {
            "gh_issue_state": state,
            "gh_issue_state_synced_at": when,
        }},
    )
    if result.matched_count == 0:
        raise KeyError(f"unknown finding_id {finding_id!r}")
    return _strip_id(store.findings.find_one({"finding_id": finding_id}))


# ---------------------------------------------------------------------------
# #1146 — admin audit (nuclear-button history).
# ---------------------------------------------------------------------------
def record_admin_wipe(
    store: Store,
    *,
    wipe_id: str,
    dropped_counts: dict[str, int],
    requester_note: str = "",
    requester: str = "",
) -> dict:
    """Append a wipe record to the audit collection.

    Called by ``api_admin_wipe`` AFTER ``wipe_for_relaunch`` completes
    — the audit collection is excluded from the wipe drop list, so
    the row persists across resets and the operator can see a chain
    of "wipe → run 1 → run 2 → wipe → run 1 → ..." sequences.

    ``wipe_id`` is a caller-supplied stable id (the API uses a UUID4
    hex prefix). ``requester_note`` is the operator-typed reason —
    "Validating Slice 3", "Hetzner cutover prep", etc. ``requester``
    captures whoever clicked the button if the UI surfaces operator
    identity (currently always blank — the qa-review UI has no auth).
    """
    doc = {
        "wipe_id": wipe_id,
        "wiped_at": _now(),
        "dropped_counts": {str(k): int(v) for k, v in (dropped_counts or {}).items()},
        "dropped_total": sum(int(v) for v in (dropped_counts or {}).values()),
        "requester_note": (requester_note or "").strip(),
        "requester": (requester or "").strip(),
    }
    store.admin_audit.insert_one(doc)
    return _strip_id(store.admin_audit.find_one({"wipe_id": wipe_id}))


def list_admin_wipes(store: Store, limit: int = 20) -> list[dict]:
    """Return recent wipes, newest first.

    Empty list when nothing has been logged — the /admin view treats
    that as "no wipes yet" and renders an empty state.
    """
    cursor = (
        store.admin_audit.find()
        .sort("wiped_at", DESCENDING)
        .limit(int(limit))
    )
    return [_strip_id(doc) for doc in cursor]


# ---------------------------------------------------------------------------
# Reads.
# ---------------------------------------------------------------------------
def list_runs(store: Store, limit: int = 50) -> list[dict]:
    """Return runs newest-first (by ``started_at``), with finding counts.

    Each run gets a ``finding_counts`` block (counts by severity) so the runs
    table can render at-a-glance triage signal without a second round-trip.
    """
    cursor = store.runs.find().sort("started_at", DESCENDING).limit(int(limit))
    runs = [_strip_id(doc) for doc in cursor]
    for run in runs:
        run["finding_counts"] = _finding_counts(store, run["run_id"])
    return runs


def get_run(store: Store, run_id: str) -> dict | None:
    """Return one run plus its findings, or ``None`` if it does not exist."""
    run = store.runs.find_one({"run_id": run_id})
    if run is None:
        return None
    run = _strip_id(run)
    findings = [
        _strip_id(doc)
        for doc in store.findings.find({"run_id": run_id}).sort("created_at", ASCENDING)
    ]
    run["findings"] = findings
    run["finding_counts"] = _count_by_severity(findings)
    # #1115 — kind bucket so the run-detail Findings tab can split 🐞 Fix
    # / ✓ Working well / 🔎 Observations without recomputing client-side.
    run["finding_counts"].update(
        {f"kind_{k}": v for k, v in _count_by_kind(findings).items()}
    )
    # #1029 — surface which MCP servers the persona exercised. Operators
    # want the answer at-a-glance on the run-overview chip list.
    run["mcp_servers_used"] = summarise_mcp_servers_used(store, run_id)
    return run


def summarise_mcp_servers_used(store: Store, run_id: str) -> list[dict]:
    """Aggregate ``qa_run_steps`` by MCP server prefix.

    Every step's ``tool_name`` is shaped like ``mcp__<server>__<tool>``
    (e.g. ``mcp__playwright__browser_navigate``). We split on ``__``,
    take the segment after the ``mcp`` prefix as the server id, and
    count how many calls landed on each.

    Returns a list of ``{"server": str, "calls": int}`` sorted by call
    count descending (most active server first), then by server name
    for stable tie-breaks. Empty list if the run has no MCP-shaped
    tool calls (a raw text-only run, or a run that hasn't started).

    Slice A of #1028 — the display layer renders these as chips on
    the run-detail overview. Slice B will introduce a catalog to map
    server ids to human display names; today's chip layer renders the
    raw ids unchanged.
    """
    pipeline = [
        {"$match": {"run_id": run_id, "tool_name": {"$regex": "^mcp__"}}},
        {"$project": {
            "server": {"$arrayElemAt": [{"$split": ["$tool_name", "__"]}, 1]}
        }},
        {"$group": {"_id": "$server", "calls": {"$sum": 1}}},
        {"$project": {"_id": 0, "server": "$_id", "calls": 1}},
        {"$sort": {"calls": -1, "server": 1}},
    ]
    return list(store.steps.aggregate(pipeline))


def set_finding_status(store: Store, finding_id: str, status: str) -> dict:
    """Update one finding's triage status. Returns the updated finding."""
    if status not in FINDING_STATUSES:
        raise ValueError(
            f"status must be one of {FINDING_STATUSES}, got {status!r}"
        )
    res = store.findings.update_one(
        {"finding_id": finding_id}, {"$set": {"status": status}}
    )
    if res.matched_count == 0:
        raise KeyError(f"unknown finding_id {finding_id!r}")
    return _strip_id(store.findings.find_one({"finding_id": finding_id}))


# ---------------------------------------------------------------------------
# Slice 2 of #1104 — cross-run finding dedup helpers.
#
# add_persona_result tags every new finding with a ``title_hash``;
# Slice 2.1's distillation calls find_prior_finding to look up earlier
# sightings, and bump_finding_recurring to increment ``recurring_count``
# and (optionally) flip ``is_regression`` on the prior row. The data
# plane lives here; the behavioural integration is Slice 2.1.
# ---------------------------------------------------------------------------
def find_prior_finding(
    store: Store,
    *,
    persona: str,
    category: str,
    title_hash: str,
    exclude_run_id: str | None = None,
) -> dict | None:
    """Return the most recent prior finding matching the dedup key.

    Dedup key is ``(persona, category, title_hash)`` per the epic spec.
    The same persona reporting the same category of bug with a
    semantically-identical title is treated as the same underlying
    issue; ``recurring_count`` on the prior finding is the correct
    surface, not a duplicate row.

    ``exclude_run_id`` (optional) — when supplied, skip findings from
    that run. Slice 2.1 uses this from the distillation path so a
    persona's first sighting in the run-currently-being-processed isn't
    mistaken for its own prior. ``None`` (default) returns the most
    recent matching finding from any run including the active one.

    Returns ``None`` if no match.
    """
    query: dict = {
        "persona": persona,
        "category": category,
        "title_hash": title_hash,
    }
    if exclude_run_id is not None:
        query["run_id"] = {"$ne": exclude_run_id}
    doc = store.findings.find_one(query, sort=[("created_at", DESCENDING)])
    return _strip_id(doc)


def bump_finding_recurring(
    store: Store,
    finding_id: str,
    *,
    run_id: str,
    is_regression: bool = False,
) -> dict | None:
    """Increment ``recurring_count`` and record the witnessing run.

    Called by Slice 2.1's distillation when a new persona-run sighting
    matches a prior finding's dedup key. Effects on the matched row:

      - ``recurring_count`` += 1
      - ``last_verified_run_id`` ← ``run_id``
      - ``is_regression`` ← True if explicitly passed; else preserved
        (don't accidentally clear an existing regression flag).

    ``status`` is intentionally NOT modified: if an operator dismissed
    the prior, a fresh sighting should NOT silently re-open it; the
    review UI will instead surface "this dismissed finding was seen
    again in run X" via the recurring_count badge.

    Returns the refreshed doc, or ``None`` if the finding_id is
    unknown (missing-is-not-an-error contract, same as
    ``get_memory_by_id``).
    """
    update: dict = {
        "$inc": {"recurring_count": 1},
        "$set": {"last_verified_run_id": run_id},
    }
    if is_regression:
        # Only ever flip is_regression to True via this path. Clearing
        # it requires explicit operator action (a future cockpit
        # affordance) — automatic recovery would erase the loud signal
        # before the operator had a chance to triage it.
        update["$set"]["is_regression"] = True
    result = store.findings.update_one({"finding_id": finding_id}, update)
    if result.matched_count == 0:
        return None
    return _strip_id(store.findings.find_one({"finding_id": finding_id}))


def apply_cross_run_dedup_for_run(store: Store, run_id: str) -> dict[str, int]:
    """Walk this run's findings, set ``recurring_count`` + ``is_regression``
    based on what prior runs already filed.

    Slice 2.1 of #1104. Called by the harness's report sink right after
    ``add_persona_result`` writes the run's findings. Iterates each
    finding F filed in this run and, for each:

      1. Looks up the most recent prior F_prior with the same dedup key
         (persona, category, title_hash), EXCLUDING this run.
      2. If F_prior exists:
           - sets F.recurring_count = F_prior.recurring_count + 1 (the
             running tally across runs lives on the most recent row).

    ``recurring_count`` is finding-derived and survives; the older
    "is_regression via a fixed persona-memory" signal was removed when
    the persona-memory subsystem was retired, so this pass no longer
    flips ``is_regression`` (a stored ``True`` is left untouched).

    Returns a counts dict ``{"matched_priors": N, "regressions": N}``
    for caller-side logging (``regressions`` stays 0 now that the
    memory-backed signal is gone). Best-effort: any per-finding error is
    logged + swallowed so a bad row can't void the rest of the run.

    The new row's recurring_count is set TO the new tally (not
    incremented via ``bump_finding_recurring``) — at insert time
    ``add_persona_result`` already wrote ``recurring_count=1``, and this
    function REPLACES that value rather than adding to it. The prior
    row is NOT mutated here; future runs will find this latest row as
    their "prior" and continue the chain.
    """
    counts = {"matched_priors": 0, "regressions": 0}
    findings = list(store.findings.find({"run_id": run_id}))
    for f in findings:
        fid = f.get("finding_id")
        persona = f.get("persona")
        category = f.get("category")
        title_hash = f.get("title_hash")
        if not (fid and persona and category and title_hash):
            # Pre-Slice-2.0 rows without a title_hash skip cleanly — the
            # contract is "best effort"; missing dedup data means we
            # leave the row as-is rather than computing a partial state.
            continue
        prior = find_prior_finding(
            store,
            persona=persona,
            category=category,
            title_hash=title_hash,
            exclude_run_id=run_id,
        )
        if prior is None:
            continue
        counts["matched_priors"] += 1
        new_count = int(prior.get("recurring_count", 1) or 1) + 1
        update_fields: dict = {"recurring_count": new_count}

        # The memory-backed regression signal ("a prior finding that a
        # persona memory marked verification_status='fixed' was seen
        # again") retired with the persona-memory subsystem. recurring_
        # count above is the surviving cross-run signal.
        store.findings.update_one(
            {"finding_id": fid}, {"$set": update_fields},
        )
    return counts


# ---------------------------------------------------------------------------
# Internal count helpers.
# ---------------------------------------------------------------------------
def _count_by_severity(findings: list[dict]) -> dict[str, int]:
    counts = {"blocker": 0, "major": 0, "minor": 0, "nit": 0}
    for f in findings:
        sev = f.get("severity")
        if sev in counts:
            counts[sev] += 1
    return counts


def _count_by_kind(findings: list[dict]) -> dict[str, int]:
    """Counts per #1115 ``kind`` axis (bug / gap / risk / nit / praise /
    observation). Missing kind on an old finding doc defaults to ``bug``,
    which matches the in-store default and pre-#1115 semantics."""
    counts = {
        "bug": 0,
        "gap": 0,
        "risk": 0,
        "nit": 0,
        "praise": 0,
        "observation": 0,
    }
    for f in findings:
        k = f.get("kind", "bug")
        if k in counts:
            counts[k] += 1
        else:
            # Unknown kind from a future writer / hand-edited doc — bucket
            # into ``bug`` so we don't silently swallow it.
            counts["bug"] += 1
    return counts


def _finding_counts(store: Store, run_id: str) -> dict[str, int]:
    findings = list(
        store.findings.find(
            {"run_id": run_id},
            {"severity": 1, "kind": 1, "_id": 0},
        )
    )
    counts = _count_by_severity(findings)
    # #1115 — fold the kind bucket counts in. Caller (list_runs) renders
    # both ladders; kept under the same dict so existing readers that only
    # touch severity keys keep working.
    counts.update({f"kind_{k}": v for k, v in _count_by_kind(findings).items()})
    return counts


# ---------------------------------------------------------------------------
# Run steps (#860 — Transcript tab data).
#
# One doc per tool call. The harness's RunRecorder is the sole writer; the
# review UI is read-only on this collection. Idempotent on
# (run_id, persona_id, step_n) so a replayed persona overwrites rather than
# duplicating.
# ---------------------------------------------------------------------------
def record_step(
    store: Store,
    run_id: str,
    persona_id: str,
    step_n: int,
    *,
    tool_name: str,
    args_summary: str = "",
    text_from_persona: str = "",
    screenshot_id: object | None = None,
    finding_ordinals: list[int] | None = None,
    ts: datetime | None = None,
) -> dict:
    """Upsert one step record.

    ``screenshot_id`` is the ObjectId returned by
    :func:`qa_store.screenshots.store_screenshot` when the tool call was
    ``mcp__playwright__browser_take_screenshot``; ``None`` otherwise. The
    review UI's ``GET /api/runs/.../screenshots/{oid}`` fetches the blob
    back via GridFS.

    ``finding_ordinals`` is the list of per-persona ordinal numbers (see
    :func:`finding_id`) for any findings the persona filed AT this step.
    The review UI uses this to render a finding↔step linkback. Most steps
    have an empty list; ``note_finding`` calls land here.

    Upsert semantics (matches the access-pattern of every other writer in
    this module): a re-record with the same ``(run_id, persona_id, step_n)``
    replaces the doc — a replayed persona's transcript is rewritten
    cleanly, not appended.
    """
    doc = {
        "run_id": run_id,
        "persona_id": persona_id,
        "step_n": int(step_n),
        "ts": ts or _now(),
        "tool_name": tool_name,
        "args_summary": args_summary,
        "text_from_persona": text_from_persona,
        "screenshot_id": screenshot_id,
        "finding_ordinals": list(finding_ordinals or []),
    }
    store.steps.update_one(
        {"run_id": run_id, "persona_id": persona_id, "step_n": int(step_n)},
        {"$set": doc},
        upsert=True,
    )
    return _strip_id(
        store.steps.find_one(
            {"run_id": run_id, "persona_id": persona_id, "step_n": int(step_n)}
        )
    )


def list_steps_for_persona(
    store: Store, run_id: str, persona_id: str
) -> list[dict]:
    """All steps for one persona's run, ordered by ``step_n`` ascending.

    The Transcript tab in the review UI renders this verbatim. Returns an
    empty list when no steps were recorded (the harness might be running
    an older image without #860's recorder wired up — empty transcript is
    a valid display state, not an error).
    """
    cursor = store.steps.find(
        {"run_id": run_id, "persona_id": persona_id}
    ).sort("step_n", ASCENDING)
    return [_strip_id(d) for d in cursor]


def attach_screenshot_to_step(
    store: Store,
    run_id: str,
    persona_id: str,
    step_n: int,
    screenshot_id: object,
) -> None:
    """Update an existing step with a freshly-captured screenshot oid.

    The Claude Agent SDK streams ``ToolUseBlock`` (the call) and
    ``ToolResultBlock`` (the response — where Playwright's screenshot
    bytes live) as separate messages with assistant text potentially in
    between. The recorder writes the step on the ToolUseBlock and patches
    the screenshot in when the ToolResultBlock arrives, so this helper is
    the gap-bridging update path.

    No-op if no such step exists — better to drop a late screenshot than
    crash a still-streaming persona on a transcript race.
    """
    store.steps.update_one(
        {"run_id": run_id, "persona_id": persona_id, "step_n": int(step_n)},
        {"$set": {"screenshot_id": screenshot_id}},
    )


def attach_finding_to_step(
    store: Store,
    run_id: str,
    persona_id: str,
    step_n: int,
    ordinal: int,
) -> None:
    """Append a finding ordinal to a step's ``finding_ordinals`` list.

    Called when the persona issues ``mcp__findings__note_finding`` and the
    recorder needs to link the resulting finding back to the step that
    produced it. ``$addToSet`` so repeated calls don't double-count.

    No-op if the step doesn't exist (same reasoning as
    :func:`attach_screenshot_to_step` — late writes are tolerated).
    """
    store.steps.update_one(
        {"run_id": run_id, "persona_id": persona_id, "step_n": int(step_n)},
        {"$addToSet": {"finding_ordinals": int(ordinal)}},
    )


# ---------------------------------------------------------------------------
# Run logs (#902/#903 — narrative-emit archive feeding the QA-insights epic).
#
# One doc per ``runner._emit_*`` call. Strictly append-only — the
# RunRecorder generates a monotonic ``seq`` per (run_id, persona_id)
# and never overwrites. Replayed personas don't produce duplicates
# in practice (a re-play gets a new run_id by construction), so we
# don't bother with an upsert key here.
# ---------------------------------------------------------------------------
def append_run_log(
    store: Store,
    *,
    run_id: str,
    persona_id: str,
    seq: int,
    kind: str,
    content: str,
    phase: str = "",
    turn: int | None = None,
    metadata: dict | None = None,
    ts: datetime | None = None,
) -> None:
    """Insert one narrative-emit row.

    ``kind`` is one of :data:`RUN_LOG_KINDS`. Unknown kinds are still
    persisted (the validation is informational, not gatekeeping —
    keeping the recorder forgiving means a future runner edit that
    emits a new kind doesn't lose log lines while the schema catches
    up). ``content`` is the textual payload; ``metadata`` is the
    open-ended bag (e.g. tool args, ResultMessage cost/usage).
    """
    store.run_logs.insert_one({
        "run_id": run_id,
        "persona_id": persona_id,
        "seq": int(seq),
        "phase": phase or "",
        "turn": int(turn) if turn is not None else None,
        "kind": kind,
        "content": content,
        "metadata": dict(metadata) if metadata else {},
        "ts": ts or _now(),
    })


def list_run_logs_for_persona(
    store: Store,
    run_id: str,
    persona_id: str,
    *,
    limit: int = 5000,
) -> list[dict]:
    """All log rows for one persona's run, ordered by ``seq`` ascending.

    Slice 2's Transcript Search consumes this for the per-run replay
    view. Default limit (5000) is well above the steady-state max per
    persona run (~500 emits) but bounded so a runaway never returns
    an unbounded result set.
    """
    cursor = (
        store.run_logs.find(
            {"run_id": run_id, "persona_id": persona_id}
        )
        .sort("seq", ASCENDING)
        .limit(int(limit))
    )
    return [_strip_id(d) for d in cursor]


def search_run_logs(
    store: Store,
    *,
    q: str | None = None,
    persona_id: str | None = None,
    kind: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[dict]:
    """Filter qa_run_logs across runs. Returns newest-first.

    Slice 2 (#904) — the hand-driven search surface in the review UI.
    Today the text match is a case-insensitive substring (Mongo
    ``$regex``) — cheap, no extra index needed beyond the existing
    compound on (persona_id, ts). Slice 3 (#905) swaps in Atlas
    Vector Search for semantic match; this regex path stays as the
    fallback / exact-string drill.

    ``q`` — case-insensitive substring on ``content``. ``None`` /
    empty returns rows matching the other filters with no text gate.
    The regex special chars in ``q`` are escaped so an accidental
    parenthesis or dot doesn't either crash the query or silently
    over-match.

    ``persona_id``, ``kind`` — exact match. ``None`` skips that filter.

    ``since`` / ``until`` — inclusive window on ``ts``.

    ``limit`` — caller-clamped at the API layer; 1000 max returned
    here as a defensive cap so a misconfigured client can't pull the
    full 30k-doc/month volume in one request.
    """
    import re as _re

    query: dict = {}
    if persona_id:
        query["persona_id"] = persona_id
    if kind:
        query["kind"] = kind
    if since or until:
        ts_q: dict = {}
        if since:
            ts_q["$gte"] = since
        if until:
            ts_q["$lte"] = until
        query["ts"] = ts_q
    if q:
        # Escape every regex meta-char so "foo.bar" matches the literal
        # "foo.bar", not "foo" + any-char + "bar". `re.escape` is the
        # standard fix.
        query["content"] = {
            "$regex": _re.escape(q),
            "$options": "i",
        }

    capped = min(int(limit), 1000)
    cursor = (
        store.run_logs.find(query)
        .sort("ts", DESCENDING)
        .limit(capped)
    )
    return [_strip_id(d) for d in cursor]


# ---------------------------------------------------------------------------
# Saved scenarios (#862 — named {persona + mandatory-action-ids} presets).
#
# A scenario is a re-runnable preset, not a record of a past run. The
# operator picks a persona, ticks mandatory coverage actions on the
# trigger UI, then optionally clicks "Save as scenario" to persist the
# choice as a named preset for one-click reload next time.
#
# The store side is intentionally minimal: id-uniqueness, sane string
# field strips, an updated_at touch on update — nothing fancy. Catalog +
# persona-id VALIDATION lives in the review-ui API layer (where the
# CATALOG + KNOWN_PERSONAS constants are imported), not here, because
# qa-store is meant to stay agnostic of harness-side specifics.
# ---------------------------------------------------------------------------
def create_scenario(
    store: Store,
    *,
    id: str,
    name: str,
    description: str = "",
    persona_id: str,
    mandatory_action_ids: list[str] | None = None,
) -> dict:
    """Insert a new scenario. Raises if ``id`` already exists.

    ``id`` must be a stable slug — short, lower-kebab-case, never changed
    after creation because it's the URL path the review UI routes on.
    The store enforces uniqueness via the index; a duplicate insert
    raises ``pymongo.errors.DuplicateKeyError`` which the API layer
    translates to HTTP 409.
    """
    now = _now()
    doc = {
        "id": str(id).strip(),
        "name": str(name).strip(),
        "description": str(description or "").strip(),
        "persona_id": str(persona_id).strip(),
        "mandatory_action_ids": list(mandatory_action_ids or []),
        "created_at": now,
        "updated_at": now,
    }
    store.scenarios.insert_one(doc)
    return _strip_id(store.scenarios.find_one({"id": doc["id"]}))


def list_scenarios(store: Store) -> list[dict]:
    """All scenarios, newest-edited first.

    Returns ``[]`` when none exist (this is a brand-new feature; most
    deployments will start empty). The review UI lists everything on
    one page — no pagination today because the realistic ceiling is
    tens of scenarios per team, not thousands.
    """
    cursor = store.scenarios.find().sort("updated_at", DESCENDING)
    return [_strip_id(d) for d in cursor]


def get_scenario(store: Store, scenario_id: str) -> dict | None:
    """Fetch one scenario by ``id``, or ``None`` if missing.

    The API translates a ``None`` return into HTTP 404; the store does
    NOT raise here because a missing scenario is a normal state during
    UI navigation (e.g. the operator just deleted it in another tab and
    clicked an old link).
    """
    return _strip_id(store.scenarios.find_one({"id": scenario_id}))


def update_scenario(
    store: Store,
    scenario_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    persona_id: str | None = None,
    mandatory_action_ids: list[str] | None = None,
) -> dict | None:
    """Partial-update one scenario; touch ``updated_at`` on any change.

    Any ``None`` argument means "leave that field alone" — letting the
    API pass through whichever PATCH fields the client sent without
    needing a per-field handler at the FastAPI layer. Returns the
    refreshed doc, or ``None`` if no scenario with that id existed
    (mirrors get_scenario's missing-is-not-an-error contract).

    ``id`` is deliberately NOT updateable — it's the routing key, and
    a "rename the id" semantic would be confusing alongside the "name"
    field that the UI does let the operator change freely.
    """
    set_fields: dict = {"updated_at": _now()}
    if name is not None:
        set_fields["name"] = str(name).strip()
    if description is not None:
        set_fields["description"] = str(description).strip()
    if persona_id is not None:
        set_fields["persona_id"] = str(persona_id).strip()
    if mandatory_action_ids is not None:
        set_fields["mandatory_action_ids"] = list(mandatory_action_ids)
    result = store.scenarios.update_one(
        {"id": scenario_id}, {"$set": set_fields}
    )
    if result.matched_count == 0:
        return None
    return _strip_id(store.scenarios.find_one({"id": scenario_id}))


def delete_scenario(store: Store, scenario_id: str) -> bool:
    """Hard-delete one scenario. Returns ``True`` if it existed.

    No soft-delete on this collection — scenarios are operator-owned
    presets, not audit material; if the operator clicks Delete they
    mean it. ``False`` return tells the API to return 404, matching
    every other read/update path's missing-is-not-an-error contract.
    """
    result = store.scenarios.delete_one({"id": scenario_id})
    return result.deleted_count > 0


# ---------------------------------------------------------------------------
# Persona library (QA Studio redesign).
#
# Schema (all fields):
#   persona_id          str   — stable slug (e.g. "margaret")
#   display_name        str   — human label ("Margaret Chen")
#   registered_email    str   — SMTP from address for this persona
#   explore_system_prompt  str
#   report_system_prompt   str
#   flows               list[str]   — action ids or free-text flow tags
#   uses_admin_login    bool
#   setup_actions       str | None
#   browser_locale      str | None
#   color_token         str   — one of PERSONA_COLOR_TOKENS
#   avatar_seed         str   — seed passed to the Open Peeps generator
#   is_default          bool  — seeded from personas.py; UI can hide but
#                               not hard-delete
#   hidden              bool  — operator-set soft-delete for default rows
#   created_at          datetime
#   updated_at          datetime
# ---------------------------------------------------------------------------

def create_persona(store: Store, doc: dict) -> dict:
    """Strict insert — raises ``DuplicateKeyError`` if ``persona_id`` is taken.

    Use this from the UI's create path so a duplicate id surfaces as a
    409 (the route catches DuplicateKeyError) without a check-then-insert
    TOCTOU window. For seeding (where the desired behaviour is "rewrite
    existing"), use ``upsert_persona`` instead.
    """
    now = _now()
    doc = {**doc, "created_at": now, "updated_at": now}
    store.personas.insert_one(doc)
    return _strip_id(store.personas.find_one({"persona_id": doc["persona_id"]}))


def upsert_persona(store: Store, doc: dict) -> dict:
    """Insert or replace a persona by ``persona_id``.

    Used by the seed path (idempotent — re-running seed rewrites the
    default rows without touching user-created ones) and by the UI's
    create/update endpoints.
    """
    now = _now()
    persona_id = doc["persona_id"]
    existing = store.personas.find_one({"persona_id": persona_id})
    if existing is None:
        doc = {**doc, "created_at": now, "updated_at": now}
        store.personas.insert_one(doc)
    else:
        update_doc = {**doc, "updated_at": now}
        update_doc.pop("created_at", None)
        store.personas.update_one(
            {"persona_id": persona_id},
            {"$set": update_doc},
        )
    return _strip_id(store.personas.find_one({"persona_id": persona_id}))


def list_personas(
    store: Store,
    *,
    include_hidden: bool = False,
    active_only: bool = False,
) -> list[dict]:
    """All personas, default rows first then alphabetical.

    Hidden rows (soft-deleted defaults) are excluded unless
    ``include_hidden=True``. ``active_only=True`` (Slice of #1009)
    restricts to personas the operator has activated — the trigger
    flow's default. Combining is fine: ``active_only=True,
    include_hidden=False`` is "active and visible."
    """
    query: dict = {} if include_hidden else {"hidden": {"$ne": True}}
    if active_only:
        query["is_active"] = True
    cursor = (
        store.personas
        .find(query)
        .sort([("is_default", DESCENDING), ("display_name", ASCENDING)])
    )
    return [_strip_id(d) for d in cursor]


def toggle_persona_active(
    store: Store, persona_id: str, *, active: bool,
) -> dict:
    """Convenience wrapper for the activation toggle (Slice of #1009).

    Equivalent to ``update_persona(store, persona_id, {"is_active": active})``
    but with a clearer signature — this is the most common write the UI
    makes against the qa_personas collection now that the seeded catalog
    starts inactive.
    """
    return update_persona(store, persona_id, {"is_active": bool(active)})


def get_persona(store: Store, persona_id: str) -> dict | None:
    """Return one persona by id, or None if it doesn't exist / is hidden."""
    doc = store.personas.find_one({"persona_id": persona_id})
    return _strip_id(doc) if doc else None


def update_persona(store: Store, persona_id: str, patch: dict) -> dict:
    """Partial-update a persona. ``patch`` may contain any mutable fields;
    ``persona_id``, ``is_default``, and ``created_at`` are always ignored.

    Raises ``KeyError`` if the persona doesn't exist.
    """
    patch = {k: v for k, v in patch.items()
             if k not in ("persona_id", "is_default", "created_at", "_id")}
    patch["updated_at"] = _now()
    result = store.personas.update_one(
        {"persona_id": persona_id},
        {"$set": patch},
    )
    if result.matched_count == 0:
        raise KeyError(f"unknown persona_id {persona_id!r}")
    return _strip_id(store.personas.find_one({"persona_id": persona_id}))


def delete_persona(store: Store, persona_id: str) -> bool:
    """Hard-delete a user-created persona (``is_default=False``).

    For default personas use ``update_persona(..., {"hidden": True})``
    instead — this function raises ``ValueError`` if called on a default
    row to prevent accidental data loss.

    Returns ``True`` if the row was deleted, ``False`` if it didn't exist.
    """
    doc = store.personas.find_one({"persona_id": persona_id})
    if doc is None:
        return False
    if doc.get("is_default"):
        raise ValueError(
            f"persona {persona_id!r} is a default persona — use hidden=True to hide it"
        )
    result = store.personas.delete_one({"persona_id": persona_id})
    return result.deleted_count == 1


# ---------------------------------------------------------------------------
# #1105 — persistent persona credentials.
#
# Stored as a sub-doc on the persona row, NOT a separate collection —
# every read of a persona's identity already fetches the persona doc, so
# inlining keeps the harness's setup-phase to one round trip. Password
# is encrypted via qa_store.crypto.Fernet (key sourced from
# QA_CREDENTIAL_KEY env); email + dates stay plaintext.
# ---------------------------------------------------------------------------
def set_persona_credentials(
    store: Store,
    persona_id: str,
    *,
    email: str,
    password_plain: str,
    verified: bool = False,
    session_jwt: str | None = None,
    jwt_expires_at: datetime | None = None,
) -> dict:
    """Persist credentials for a persona. Password is encrypted via
    :mod:`qa_store.crypto`; the operator-visible status endpoint reads
    everything EXCEPT the password.

    Raises ``KeyError`` if the persona doesn't exist. ``email`` and
    ``password_plain`` are required for an initial save; the JWT pair
    is optional (used by :func:`record_persona_session` for the fast-
    path cookie reuse).

    Idempotent: re-saving with the same email replaces the password
    in place but preserves ``registered_at`` from the existing row.
    Changing the email is allowed (e.g. rotating to a new +rN local
    part); when it changes, ``last_rotation_n`` increments.
    """
    from .crypto import encrypt  # noqa: PLC0415 — local to avoid import-time cost
    existing = store.personas.find_one({"persona_id": persona_id})
    if existing is None:
        raise KeyError(f"unknown persona_id {persona_id!r}")
    prior = existing.get("credentials") or {}
    rotation_n = int(prior.get("last_rotation_n", 0))
    if prior.get("email") and prior.get("email") != email:
        rotation_n += 1
    registered_at = prior.get("registered_at") or _now()
    encrypted = encrypt(password_plain)
    creds = {
        "email": email,
        "password": encrypted.to_mongo(),
        "registered_at": registered_at,
        "verified": bool(verified),
        "session_jwt": session_jwt,
        "jwt_expires_at": jwt_expires_at,
        "last_rotation_n": rotation_n,
        "updated_at": _now(),
    }
    store.personas.update_one(
        {"persona_id": persona_id},
        {"$set": {"credentials": creds}},
    )
    return _strip_id(store.personas.find_one({"persona_id": persona_id}))


def get_persona_credentials(
    store: Store, persona_id: str,
) -> dict | None:
    """Return the credentials sub-doc with password decrypted.

    Returns ``None`` if the persona doesn't exist OR has never been
    given credentials. The returned dict contains ``password_plain``
    (decrypted, suitable for harness reuse) instead of the encrypted
    storage shape. Other fields pass through unchanged.

    Decryption failures (rotated key without re-encryption, corrupt
    ciphertext) come back as ``password_plain=None`` so the caller
    can fall back to a fresh signup cleanly.
    """
    from .crypto import EncryptedField, decrypt  # noqa: PLC0415
    doc = store.personas.find_one({"persona_id": persona_id})
    if doc is None or not doc.get("credentials"):
        return None
    creds = dict(doc["credentials"])
    pw_field = EncryptedField.from_mongo(creds.get("password"))
    creds["password_plain"] = decrypt(pw_field)
    # Don't bubble the raw encrypted shape outward — callers should not
    # see (or care about) the storage format.
    creds.pop("password", None)
    return creds


def get_persona_credentials_status(
    store: Store, persona_id: str,
) -> dict:
    """Operator-visible credential status — same fields as
    :func:`get_persona_credentials` but NEVER includes the password.

    Used by the API's ``GET /api/personas/{id}/credentials/status``
    endpoint. Returns ``{has_credentials: False}`` when the persona
    has no saved login (instead of None, so the caller can branch on
    a stable shape).

    Raises ``KeyError`` if the persona doesn't exist.
    """
    doc = store.personas.find_one({"persona_id": persona_id})
    if doc is None:
        raise KeyError(f"unknown persona_id {persona_id!r}")
    creds = doc.get("credentials")
    if not creds:
        return {"has_credentials": False}
    return {
        "has_credentials": True,
        "email": creds.get("email"),
        "registered_at": creds.get("registered_at"),
        "verified": bool(creds.get("verified", False)),
        "last_rotation_n": int(creds.get("last_rotation_n", 0)),
        "has_session_jwt": bool(creds.get("session_jwt")),
        "jwt_expires_at": creds.get("jwt_expires_at"),
    }


def clear_persona_credentials(store: Store, persona_id: str) -> dict:
    """Wipe credentials for a persona — used by the "reset login"
    operator action and by the ``SIGNUP_FRESH`` setup-action DSL.

    Bumps ``last_rotation_n`` on the OLD credentials shape before
    deletion is committed (so a re-save under a new email surfaces
    the right rotation count); but since we're clearing entirely,
    the bump is stored on the persona's top-level
    ``last_credential_rotation`` field for audit/log purposes.

    Raises ``KeyError`` if the persona doesn't exist. Returns the
    persona doc after the clear.
    """
    doc = store.personas.find_one({"persona_id": persona_id})
    if doc is None:
        raise KeyError(f"unknown persona_id {persona_id!r}")
    prior_rotation = int(
        (doc.get("credentials") or {}).get("last_rotation_n", 0)
    )
    store.personas.update_one(
        {"persona_id": persona_id},
        {
            "$unset": {"credentials": ""},
            "$set": {
                "last_credential_rotation": prior_rotation + 1,
                "updated_at": _now(),
            },
        },
    )
    return _strip_id(store.personas.find_one({"persona_id": persona_id}))


def record_persona_session(
    store: Store,
    persona_id: str,
    *,
    jwt: str,
    jwt_expires_at: datetime | None = None,
) -> dict:
    """Refresh just the session-token half of the credentials sub-doc.

    Called by the harness after a successful login (or on cookie
    refresh). Touches ONLY ``session_jwt`` + ``jwt_expires_at`` so
    the password + email + registered_at stay stable.

    Raises ``KeyError`` if the persona doesn't exist OR has no
    credentials yet (you can't refresh a session you haven't started).
    """
    doc = store.personas.find_one({"persona_id": persona_id})
    if doc is None:
        raise KeyError(f"unknown persona_id {persona_id!r}")
    if not doc.get("credentials"):
        raise KeyError(
            f"persona {persona_id!r} has no credentials; "
            "call set_persona_credentials first"
        )
    store.personas.update_one(
        {"persona_id": persona_id},
        {"$set": {
            "credentials.session_jwt": jwt,
            "credentials.jwt_expires_at": jwt_expires_at,
            "credentials.updated_at": _now(),
        }},
    )
    return _strip_id(store.personas.find_one({"persona_id": persona_id}))


# ---------------------------------------------------------------------------
# #1257 — single-use resume tokens. Distinct from ``session_jwt``
# (which carries a session-cookie JWT) — the resume token is a
# magic-link one-shot the backend exchanges for a fresh session when
# the persona's browser visits ``/auth/restore?token=…`` as its
# first action. Persisted on the same credentials sub-doc so the
# whole persona-identity surface lives in one row.
# ---------------------------------------------------------------------------
def record_persona_resume_token(
    store: Store,
    persona_id: str,
    *,
    token: str,
    expires_at: datetime | None = None,
) -> dict:
    """Save a fresh resume token on the persona's credentials sub-doc.

    Called by the harness right after a successful scripted login.
    Touches ONLY ``resume_token`` + ``resume_token_expires_at`` so the
    password, email, registered_at, and any existing session_jwt stay
    stable.

    Raises ``KeyError`` if the persona doesn't exist OR has no
    credentials yet (you can't issue a resume token for an account
    that never signed up).
    """
    doc = store.personas.find_one({"persona_id": persona_id})
    if doc is None:
        raise KeyError(f"unknown persona_id {persona_id!r}")
    if not doc.get("credentials"):
        raise KeyError(
            f"persona {persona_id!r} has no credentials; "
            "call set_persona_credentials first"
        )
    store.personas.update_one(
        {"persona_id": persona_id},
        {"$set": {
            "credentials.resume_token": token,
            "credentials.resume_token_expires_at": expires_at,
            "credentials.updated_at": _now(),
        }},
    )
    return _strip_id(store.personas.find_one({"persona_id": persona_id}))


def get_persona_resume_token(
    store: Store, persona_id: str,
) -> dict | None:
    """Return the persona's current resume token + expiry, or None.

    None means: no persona, no credentials, no token, or the token
    has expired. The caller treats all four the same way — fall back
    to driving the UI login form with email + password.
    """
    doc = store.personas.find_one({"persona_id": persona_id})
    if doc is None:
        return None
    creds = doc.get("credentials") or {}
    token = creds.get("resume_token")
    expires_at = creds.get("resume_token_expires_at")
    if not token:
        return None
    # Expired tokens collapse to "no token" so the caller doesn't have
    # to do its own clock arithmetic; the consume endpoint would 410
    # anyway but skipping the network round-trip is cheaper.
    if isinstance(expires_at, datetime):
        # The store may have an aware datetime (insertions go through
        # _now()) or naive (some legacy paths). Treat naive as UTC.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            return None
    return {
        "resume_token": token,
        "expires_at": expires_at,
    }


# ---------------------------------------------------------------------------
# Slice 1 of #1002 — discovered_* writes/reads.
#
# All three collections are written by the same caller (the harness
# orchestrator's post-run hook, via qa_store.distillation) and read by
# the review-ui API. The per-collection CRUD is intentionally
# narrow — just upsert + filtered list — because Slice 1 is read-mostly:
# operators browse what the personas found, and don't yet edit it.
# Slice 2 will add the canonicalization writes (merge across runs).
# ---------------------------------------------------------------------------

def _discovered_doc_id(run_id: str, persona_id: str, artifact_id: str) -> str:
    """Stable composite key: same shape as memory_id/finding_id.

    A re-distillation of the same persona's run overwrites in place
    rather than duplicating — replayed personas get rewritten
    transcripts and replayed distillations get rewritten artifacts.
    """
    return f"{run_id}:{persona_id}:{artifact_id}"


def upsert_discovered_action(
    store: Store,
    *,
    run_id: str,
    persona_id: str,
    action_id: str,
    category: str,
    human_description: str,
    url_seen: str | None = None,
    evidence: str = "",
    branches_noticed: list[str] | None = None,
) -> dict:
    """Insert or replace a discovered-action row.

    ``category`` is validated against :data:`DISCOVERED_ACTION_CATEGORIES`
    — unknown values bucket to ``"other"`` rather than raising, so a
    future model update that emits a new category doesn't take the
    whole run down.
    """
    if category not in DISCOVERED_ACTION_CATEGORIES:
        category = "other"
    doc = {
        "doc_id": _discovered_doc_id(run_id, persona_id, action_id),
        "run_id": run_id,
        "persona_id": persona_id,
        "action_id": action_id,
        "category": category,
        "human_description": human_description,
        "url_seen": url_seen,
        "evidence": evidence,
        "branches_noticed": list(branches_noticed or []),
        "source": "distilled-v1",
        "distilled_at": _now(),
    }
    store.discovered_actions.update_one(
        {"doc_id": doc["doc_id"]},
        {"$set": doc},
        upsert=True,
    )
    return _strip_id(store.discovered_actions.find_one({"doc_id": doc["doc_id"]}))


def upsert_discovered_tool(
    store: Store,
    *,
    run_id: str,
    persona_id: str,
    name: str,
    purpose: str = "",
) -> dict:
    """Insert or replace a discovered-tool row.

    The ``artifact_id`` portion of the doc_id is the tool name (a
    slug-shaped identifier the persona used or saw — e.g. ``mailpit``,
    ``revolut-sandbox``).
    """
    doc = {
        "doc_id": _discovered_doc_id(run_id, persona_id, name),
        "run_id": run_id,
        "persona_id": persona_id,
        "name": name,
        "purpose": purpose,
        "source": "distilled-v1",
        "distilled_at": _now(),
    }
    store.discovered_tools.update_one(
        {"doc_id": doc["doc_id"]},
        {"$set": doc},
        upsert=True,
    )
    return _strip_id(store.discovered_tools.find_one({"doc_id": doc["doc_id"]}))


def upsert_discovered_branch(
    store: Store,
    *,
    run_id: str,
    persona_id: str,
    ordinal: int,
    description: str,
) -> dict:
    """Insert or replace a discovered-branch row.

    Branches are free-text, so we key by ``ordinal`` (their position in
    the distillation output) rather than trying to derive a stable
    slug from the description. A re-distillation overwrites all
    branches for the persona-run pair cleanly.
    """
    doc = {
        "doc_id": _discovered_doc_id(run_id, persona_id, f"branch-{ordinal}"),
        "run_id": run_id,
        "persona_id": persona_id,
        "ordinal": int(ordinal),
        "description": description,
        "source": "distilled-v1",
        "distilled_at": _now(),
    }
    store.discovered_branches.update_one(
        {"doc_id": doc["doc_id"]},
        {"$set": doc},
        upsert=True,
    )
    return _strip_id(store.discovered_branches.find_one({"doc_id": doc["doc_id"]}))


def list_discovered_actions(
    store: Store,
    *,
    run_id: str | None = None,
    persona_id: str | None = None,
    category: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """List discovered-action rows, newest-distilled first.

    ``limit`` is hard-clamped to [1, 5000] — the page-wide view
    streams a lot at once but we don't want a misconfigured client
    asking for unbounded results.
    """
    limit = max(1, min(int(limit), 5000))
    query: dict = {}
    if run_id:
        query["run_id"] = run_id
    if persona_id:
        query["persona_id"] = persona_id
    if category:
        query["category"] = category
    cursor = (
        store.discovered_actions
        .find(query)
        .sort("distilled_at", DESCENDING)
        .limit(limit)
    )
    return [_strip_id(d) for d in cursor]


def list_discovered_tools(
    store: Store,
    *,
    run_id: str | None = None,
    persona_id: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """List discovered-tool rows, newest-distilled first. Same shape as
    list_discovered_actions but without the category filter (tools
    aren't categorised — they're just names + purposes)."""
    limit = max(1, min(int(limit), 5000))
    query: dict = {}
    if run_id:
        query["run_id"] = run_id
    if persona_id:
        query["persona_id"] = persona_id
    cursor = (
        store.discovered_tools
        .find(query)
        .sort("distilled_at", DESCENDING)
        .limit(limit)
    )
    return [_strip_id(d) for d in cursor]


def list_discovered_branches(
    store: Store,
    *,
    run_id: str | None = None,
    persona_id: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """List discovered-branch rows, newest-distilled first.

    Branches sort by ``(distilled_at DESC, ordinal ASC)`` so within a
    single distillation, the branches appear in the order the model
    emitted them — which is the order the persona observed them
    chronologically.
    """
    limit = max(1, min(int(limit), 5000))
    query: dict = {}
    if run_id:
        query["run_id"] = run_id
    if persona_id:
        query["persona_id"] = persona_id
    cursor = (
        store.discovered_branches
        .find(query)
        .sort([("distilled_at", DESCENDING), ("ordinal", ASCENDING)])
        .limit(limit)
    )
    return [_strip_id(d) for d in cursor]


def clear_discovered_for_persona_run(
    store: Store, run_id: str, persona_id: str,
) -> dict:
    """Delete every discovered-* row for one persona-run pair.

    Used by the distillation runner BEFORE writing a fresh batch — a
    re-distillation should produce N actions, not N + leftovers from
    the previous attempt. The upserts replace in place but they don't
    delete rows whose action_id is no longer emitted by the model.

    Returns a ``{actions, tools, branches}`` dict of deleted counts.
    """
    return {
        "actions": store.discovered_actions.delete_many(
            {"run_id": run_id, "persona_id": persona_id}
        ).deleted_count,
        "tools": store.discovered_tools.delete_many(
            {"run_id": run_id, "persona_id": persona_id}
        ).deleted_count,
        "branches": store.discovered_branches.delete_many(
            {"run_id": run_id, "persona_id": persona_id}
        ).deleted_count,
    }
