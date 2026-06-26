"""Environment-driven configuration for the QA persona harness.

Every knob is read from the environment with a sane default so the harness can
run locally (against the spike's dev stack) or in the sandbox k8s Job with no
code change. Slice 5 turns the model knobs into Terraform variables that
render into these same env vars — so keep the names stable.

#1822 — the ``PriceTable`` (``QA_PRICE_*`` envs) was retired along with the
per-run dollar conversion: every run bills the operator's flat-rate Claude
Code Max subscription, so the harness now tracks raw token totals only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    """Resolved harness configuration for a single run."""

    persona: str
    web_base_url: str
    smtp_host: str
    smtp_port: int
    mailpit_url: str
    explore_model: str
    report_model: str
    max_turns: int
    run_timeout_s: int
    out_dir: str
    mongodb_url: str
    admin_email: str
    admin_password: str
    # Report sink selection (Slice 6). ``file`` writes markdown + JSON to a
    # directory; ``atlas`` upserts the run into the shared ``slyreply_qa`` store
    # so the review UI can show it. Both sinks stay valid.
    sink: str
    # The shared run id grouping every persona of one orchestrated job. Slice
    # 5's orchestration sets QA_RUN_ID once per Job; if unset, the Atlas sink
    # generates one (see report.AtlasReportSink).
    run_id: str
    qa_store_url: str
    qa_store_db: str
    # Slice 5 — Discord alerting. The orchestrator POSTs a run-summary alert to
    # this webhook after every persona has finished. Empty → the post is a
    # no-op (so a local `--all` run needs no Discord credential).
    discord_webhook_url: str
    # #824 — bounded persona concurrency. The orchestrator runs at most
    # ``concurrency`` personas at once under an asyncio.Semaphore. 4 was
    # picked as a balance between wall-clock speedup (12 personas in ~4h
    # instead of ~16h) and Anthropic 429 headroom; the operator can override
    # via QA_HARNESS_CONCURRENCY without a code change. ``concurrency=1`` is
    # a clean fallback to the pre-#824 sequential loop.
    concurrency: int
    # #858 — operator-facing "what was this run about" label, set by the
    # review-UI trigger path as QA_RUN_NOTES. The Atlas report sink writes
    # it onto the run document when it calls ``create_run``. Empty string
    # means "no notes" — the run still works; the UI just won't render a
    # notes line on the runs list. CronJob-scheduled runs always have
    # empty notes (nothing on the CronJob template sets the env).
    run_notes: str = ""
    # Site Model target this run is exercising (qa-store site_targets /
    # site_knowledge are keyed by tenant + target_id). The harness loads this
    # target's by-design knowledge and injects it into persona prompts so they
    # stop re-flagging intentional behaviour. Defaults to "slyreply" (our
    # dogfood target); set QA_TARGET_ID per target once multi-target lands.
    target_id: str = "slyreply"
    # #861 — operator-selected mandatory coverage-action ids (qa_store.
    # coverage_catalog) the persona MUST attempt this session. Empty
    # list = pure free-rein run (the original behaviour). Set via
    # QA_MANDATORY_ACTIONS env, comma-separated. Unknown ids are
    # warned-and-dropped at prompt-render time rather than crashing the
    # run (the review-UI 422s typos before the env ever gets set, so
    # the only way an unknown id reaches here is a stale snapshot).
    mandatory_action_ids: tuple[str, ...] = ()
    # #1031 — operator-selected MCP servers for THIS run (Slice C of the
    # MCP visibility epic, #1028). Empty tuple = "use catalog defaults"
    # (every default_enabled=True server in qa_agents.mcp_catalog). Set
    # via env QA_ENABLED_MCPS as a comma-joined list of server ids.
    # Unknown ids are dropped with a warning at runner-build time rather
    # than crashing the run (the trigger UI validates against the
    # catalog before the env is ever set; an unknown id reaching here
    # would be a stale-snapshot edge case). Honoured by runner.py's
    # mcp_servers construction — disabled servers are simply not built,
    # and their tool prefixes are removed from allowed_tools so the
    # model never sees them.
    enabled_mcp_servers: tuple[str, ...] = ()
    # #1821 — multi-pod sharding. A sharded QA run is ONE k8s indexed Job
    # with ``pod_count`` parallel pods; each pod runs a disjoint stripe of
    # the persona roster and the LAST pod to finish runs the run-level
    # finalisation (finish_run + Discord). ``pod_index`` is the pod's
    # ordinal (k8s sets JOB_COMPLETION_INDEX on indexed Jobs); ``pod_count``
    # is the parallelism (operator sets QA_POD_COUNT). Both default to the
    # single-pod values (index 0, count 1) so a non-sharded run — every
    # existing run — behaves exactly as before: one pod owns the whole
    # roster and finishes the run itself.
    pod_index: int = 0
    pod_count: int = 1
    # Internal-group personas (the `internal-load-economist` / Nadia) read
    # SlyReply's own cost API via the cost MCP. That endpoint is admin-only
    # and takes a bearer token; the operator supplies it as QA_ADMIN_TOKEN
    # for the QA tenant. Empty → the cost tool degrades gracefully (it
    # reports it has no credentials and the persona files one observation).
    admin_api_token: str = ""
    # OpenAI organization usage/costs API key (sk-admin-*) for the
    # openai_billing MCP — an independent external read to cross-check the
    # internal cost estimate. Empty → that tool degrades gracefully too.
    openai_admin_key: str = ""
    # OpenAI project id to scope the billing read to (the sandbox project).
    # Empty → org-wide. Set so the cross-check sees ONLY sandbox spend and
    # the report never surfaces prod figures, even though the admin key is
    # org-wide.
    openai_project_id: str = ""

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            persona=_env("QA_PERSONA", "margaret"),
            web_base_url=_env("QA_WEB_BASE_URL", "http://localhost:5173"),
            smtp_host=_env("QA_SMTP_HOST", "localhost"),
            smtp_port=_env_int("QA_SMTP_PORT", 1025),
            mailpit_url=_env("QA_MAILPIT_URL", "http://localhost:8025"),
            explore_model=_env("QA_EXPLORE_MODEL", "claude-sonnet-4-6"),
            report_model=_env("QA_REPORT_MODEL", "claude-opus-4-7"),
            # 200 turns sized for a full persona journey: ~100–150 Playwright
            # browser tool calls + ~50 assistant/model turns. The Slice 5 spike
            # at 60 hit the SDK's truncation guard mid-run (#652). Still env
            # overridable via QA_MAX_TURNS — set it lower for a sniff test.
            max_turns=_env_int("QA_MAX_TURNS", 200),
            # 7200s (2h) per persona — sized so the explore phase (up to
            # max_turns=200 at ~30s/turn ≈ 100 min) AND the report phase fit
            # inside the asyncio.wait_for guard. The first real run (#668)
            # used 1800s and the report phase got cancelled when daniel and
            # margaret hit max_turns inside the explore phase, leaving no
            # synthesised review. Still env-overridable.
            run_timeout_s=_env_int("QA_RUN_TIMEOUT_S", 7200),
            out_dir=_env("QA_OUT_DIR", "./qa-runs"),
            # Sandbox MongoDB — used by the fair-use override utility
            # (qa_agents.fair_use_override), NOT by the agent loop itself.
            mongodb_url=_env("QA_MONGODB_URL", "mongodb://mongodb:27017/slyreply"),
            # Admin credentials for the `tomas` persona. He LOGS IN as the
            # seeded sandbox admin rather than signing up fresh. The defaults
            # match the belt-and-braces admin that k8s/sandbox's
            # seed-extra-configmap.yaml inserts when no admin exists
            # (admin@sandbox.slyreply.ai / "testpass123"). The Slice 2 sandbox
            # seed MUST guarantee a loginable admin with these exact values
            # (or these env vars must be overridden to match the real seed) —
            # see README "Persona admin credentials (tomas)".
            admin_email=_env("QA_ADMIN_EMAIL", "admin@sandbox.slyreply.ai"),
            admin_password=_env("QA_ADMIN_PASSWORD", "testpass123"),
            # Slice 6 — report sink. Defaults to the file sink so a local run
            # needs no MongoDB; the sandbox Job sets QA_SINK=atlas.
            sink=_env("QA_SINK", "file"),
            run_id=_env("QA_RUN_ID", ""),
            qa_store_url=_env("QA_STORE_URL", "mongodb://localhost:27017"),
            qa_store_db=_env("QA_STORE_DB", "slyreply_qa"),
            target_id=_env("QA_TARGET_ID", "slyreply"),
            # Discord run-summary webhook. Carried in qa-agents-secrets in the
            # sandbox; unset locally → discord.post_run_alert is a no-op.
            discord_webhook_url=_env("QA_DISCORD_WEBHOOK_URL", ""),
            # Bounded persona concurrency for the orchestrator (#824). 4 is
            # the default — see the field docstring above.
            concurrency=_env_int("QA_HARNESS_CONCURRENCY", 4),
            # Operator-facing run label, set by the review-UI trigger path
            # (#858). Empty string is "no label" — the runs list just won't
            # render a notes line for this run. CronJob runs have no
            # QA_RUN_NOTES on their template, so this stays "" for them.
            run_notes=_env("QA_RUN_NOTES", ""),
            # Mandatory coverage-action ids the persona MUST attempt this
            # session (#861). Comma-separated; whitespace tolerated; empty
            # parses to (). The trigger UI validates ids against the
            # catalog server-side before the env ever gets set, so unknown
            # ids reaching here are a stale-snapshot edge case the
            # prompt renderer drops with a warning.
            mandatory_action_ids=tuple(
                s.strip()
                for s in _env("QA_MANDATORY_ACTIONS", "").split(",")
                if s.strip()
            ),
            # #1031 — per-run MCP-server selection (Slice C of #1028).
            # Same comma-split-and-strip shape as mandatory_action_ids;
            # empty string ⇒ empty tuple ⇒ "use catalog defaults". The
            # runner reads this to gate which servers it constructs.
            enabled_mcp_servers=tuple(
                s.strip()
                for s in _env("QA_ENABLED_MCPS", "").split(",")
                if s.strip()
            ),
            # #1821 — multi-pod sharding. k8s indexed Jobs expose the pod
            # ordinal as JOB_COMPLETION_INDEX; the operator sets
            # QA_POD_COUNT to the Job's parallelism. Both default to the
            # single-pod values so a plain (non-indexed) Job runs the whole
            # roster in one pod, unchanged.
            pod_index=_env_int("JOB_COMPLETION_INDEX", 0),
            pod_count=_env_int("QA_POD_COUNT", 1),
            # Internal-persona cost/billing credentials (Nadia). Both empty
            # by default so customer-persona runs need neither; the QA
            # tenant's secret sets them for the internal-load-economist run.
            admin_api_token=_env("QA_ADMIN_TOKEN", ""),
            openai_admin_key=_env("QA_OPENAI_ADMIN_KEY", ""),
            openai_project_id=_env("QA_OPENAI_PROJECT_ID", ""),
        )
