"""Trigger persona QA runs and tail their logs from the review UI.

The review UI runs in the ``slyreply-qa`` namespace; the QA harness Jobs run
in ``slyreply-sandbox``. This module is the thin Kubernetes-API layer that
lets an operator start a run — for one, several, or all personas — and watch
it live, without leaving the browser for ``kubectl``.

``K8sRunControl`` is deliberately injectable into ``create_app`` so tests pass
a fake and need no real cluster. The real implementation reaches the cluster
through the pod's ServiceAccount (see ``k8s/sandbox/qa-review-rbac.yaml`` for
the cross-namespace Role + RoleBinding that grants it).
"""

from __future__ import annotations

import copy
import time
from collections.abc import Iterator
from datetime import UTC, datetime


# The twelve QA personas. Kept in sync BY HAND with the ``PERSONAS`` registry
# in ``qa_agents/personas.py``. Since #990 the review-ui image installs
# the harness package with ``--no-deps`` (the personas module is pure
# stdlib), so we can import the catalog directly rather than maintaining
# a duplicate list. Falls back to an empty tuple when the harness isn't
# importable (some unit-test contexts) — the trigger UI handles that
# gracefully with an empty-state.
def _load_known_personas() -> tuple[str, ...]:
    try:
        from qa_agents.personas import PERSONAS  # type: ignore[import]
    except ImportError:
        return ()
    return tuple(PERSONAS)


KNOWN_PERSONAS: tuple[str, ...] = _load_known_personas()

# The label every QA harness pod carries (set by the CronJob's pod template,
# k8s/sandbox/qa-agents-cronjob.yaml). Used to find an in-progress run.
_QA_POD_SELECTOR = "app=qa-agents"
# The harness container inside the QA Job pod — the one whose logs matter
# (the other is the short-lived wipe-and-seed init container).
_HARNESS_CONTAINER = "harness"

# Pod-template label carrying the minted run id on a multi-pod run (#1821).
# Every pod of a run's N fan-out Jobs (Option B) inherits it from the pod
# template, so ``active_run`` can collapse N pods of ONE run into a single
# active row by this label rather than by ``job-name`` (which now DIFFERS per
# Job — ``qa-ui-max-<ts>-p{i}`` — making this shared label the only reliable
# run identity, and the one that matches the ``QA_RUN_ID`` the harness writes
# to the qa_runs document).
_QA_RUN_ID_LABEL = "slyreply.ai/qa-run-id"


def _stripe_roster(roster: list[str], pod_count: int) -> list[list[str]]:
    """Split ``roster`` into ``pod_count`` disjoint modulo stripes (#1821, Option B).

    Pod ``i`` gets ``sorted(roster)[j]`` for every ``j`` where
    ``j % pod_count == i``. Modulo (not contiguous chunks) so heavy and light
    personas spread evenly across pods — a contiguous split would land all the
    alphabetically-clustered heavy personas on one pod. The split is exhaustive
    and pairwise disjoint: every persona lands in exactly one pod's slice.
    Slices can be empty when ``pod_count`` exceeds the roster size; the caller
    drops those. Slicing here at trigger time (rather than letting each pod
    self-stripe by ``JOB_COMPLETION_INDEX``) is the heart of Option B: a pod's
    workload is then explicit in its own ``--personas`` arg.
    """
    ordered = sorted(roster)
    return [
        [p for j, p in enumerate(ordered) if j % pod_count == i]
        for i in range(pod_count)
    ]

# Cost ceiling for a single multi-pod run (#1821). A run fans out across
# ``pod_count`` pods, each running ``concurrency`` personas in parallel, so
# the worst-case number of personas hammering one personal Claude Code Max
# subscription *simultaneously* is ``pod_count × concurrency``. Max's rolling
# session cap can't absorb an unbounded fan-out, so the trigger refuses any
# combination whose product exceeds this constant. 8 = the empirically-safe
# ceiling (the default 3×2=6 sits comfortably under it, and 4×2=8 is the most
# aggressive shape the UI bounds — pod_count≤4, concurrency≤6 — that still
# fits). Raising this is a deliberate "are we sure Max can take it?" decision,
# not a number typed into a form.
MAX_SIMULTANEOUS_PERSONAS = 8


def _bridge_bearer_token_api_key(configuration_cls) -> None:
    """Work around kubernetes-python #2284: in-cluster token never sent.

    ``InClusterConfigLoader._set_config`` writes the ServiceAccount token to
    ``Configuration.api_key['authorization']``, but the OpenAPI-generated
    ``Configuration.auth_settings()`` only exposes the token to the rest client
    when it lives at ``api_key['BearerToken']``. The mismatch means every
    request goes out with no ``Authorization`` header and the API server
    returns 401 — which the run-trigger UI surfaces as ``listing QA pods
    failed: 401`` (issue #809). Mirror the value across both keys, and wrap
    the rotation hook so refreshed tokens stay mirrored too. A no-op on
    versions where ``BearerToken`` is already present.
    """
    config = configuration_cls.get_default_copy()
    if "BearerToken" in config.api_key or "authorization" not in config.api_key:
        return
    config.api_key["BearerToken"] = config.api_key["authorization"]
    original_hook = config.refresh_api_key_hook
    if original_hook is not None:
        def _mirrored_refresh(cfg, _original=original_hook):
            _original(cfg)
            if "authorization" in cfg.api_key:
                cfg.api_key["BearerToken"] = cfg.api_key["authorization"]
        config.refresh_api_key_hook = _mirrored_refresh
    configuration_cls.set_default(config)


class RunControlError(RuntimeError):
    """Base: something went wrong driving a QA run on the cluster."""


class ClusterUnavailable(RunControlError):
    """The Kubernetes API could not be reached or configured.

    Raised when the process is not running in-cluster and has no local
    kube-config, or when an API call fails outright. The endpoints map this
    to HTTP 503 so the UI can say "run control is unavailable" plainly.
    """


class RunAlreadyActive(RunControlError):
    """A QA run is already in progress — refuse to start a second one.

    A QA run is a long, real-money job; the CronJob's own ``concurrencyPolicy:
    Forbid`` does not apply to Jobs created outside the CronJob, so this guard
    is what stops the UI from stacking runs.
    """


class RunLimitExceeded(RunControlError):
    """The requested fan-out would exceed the simultaneous-persona ceiling.

    Raised when ``pod_count × concurrency > MAX_SIMULTANEOUS_PERSONAS`` (#1821).
    A multi-pod run multiplies the apparent load on the single personal Claude
    Code Max subscription, so the trigger refuses an over-budget shape BEFORE
    any Job is created. The endpoint maps this to HTTP 422 (the same shape it
    already uses for bad persona / model / action ids) so the operator sees a
    clean form error, not a half-started run that throttles Max mid-flight.
    """


class K8sRunControl:
    """Start and observe QA harness runs via the Kubernetes API."""

    def __init__(self, *, namespace: str, cronjob_name: str) -> None:
        self.namespace = namespace
        self.cronjob_name = cronjob_name
        self._configured = False

    # -- cluster wiring ----------------------------------------------------
    def _kubernetes(self):
        """Return the ``kubernetes`` module with config loaded (lazily).

        Loading is lazy and on first use so merely constructing the object —
        which ``create_app`` does at startup — never needs a cluster, and an
        environment with no kube access only fails when a run endpoint is
        actually called.
        """
        import kubernetes  # noqa: PLC0415 - heavy + optional; import on first use

        if not self._configured:
            try:
                kubernetes.config.load_incluster_config()
            except kubernetes.config.ConfigException as incluster_exc:
                try:
                    kubernetes.config.load_kube_config()
                except Exception as local_exc:  # noqa: BLE001 - any load failure
                    raise ClusterUnavailable(
                        "no in-cluster ServiceAccount and no local kube-config "
                        f"({incluster_exc}; {local_exc})"
                    ) from local_exc
            _bridge_bearer_token_api_key(kubernetes.client.Configuration)
            self._configured = True
        return kubernetes

    # -- queries -----------------------------------------------------------
    def active_run(self) -> dict | None:
        """The QA run currently in progress, or ``None``.

        A run counts as active while a harness pod is ``Pending`` or
        ``Running``. Listing pods (rather than Jobs) catches runs started any
        way — by this UI, by the CronJob, or by a hand-run ``kubectl``.

        Pods are grouped into ONE run so that the N pods of a multi-pod
        run report as a single active run, never as N runs and never
        blocking the 2nd..Nth pod of the same run (#1821). Under Option B the N
        pods belong to N separate Jobs (distinct ``job-name`` labels), so the
        shared ``slyreply.ai/qa-run-id`` label is the ONLY thing that groups
        them — which is exactly what the grouping below keys on. The grouping key is
        the ``slyreply.ai/qa-run-id`` pod-template label when present (the
        explicit, operator-set run identity that matches the ``QA_RUN_ID`` the
        harness writes to the qa_runs document), falling back to the
        ``job-name`` label for legacy / single-pod pods that carry no run-id
        label (#1824's behaviour, unchanged). The first active pod encountered
        fixes which run we report (single-flight: we never blend two
        overlapping runs); every active pod sharing that grouping key then
        folds into the same row, contributing to ``pod_count`` and pulling
        ``started_at`` back to the earliest pod start. The legacy keys
        (``job_name`` / ``pod_name`` / ``phase`` / ``started_at``) stay
        populated for backward compatibility, with ``run_id`` and
        ``pod_count`` added on top so App.vue's live badge can link to the run.
        """
        k8s = self._kubernetes()
        core = k8s.client.CoreV1Api()
        try:
            pods = core.list_namespaced_pod(
                self.namespace, label_selector=_QA_POD_SELECTOR
            )
        except k8s.client.ApiException as exc:
            raise ClusterUnavailable(f"listing QA pods failed: {exc.status}") from exc

        group_key: str | None = None
        result: dict | None = None
        for pod in pods.items:
            phase = pod.status.phase if pod.status else None
            if phase not in ("Pending", "Running"):
                continue
            labels = pod.metadata.labels or {}
            job_name = labels.get("job-name", "")
            # Prefer the explicit run-id label (multi-pod runs); fall back to
            # the job-name label for label-less single-pod / legacy pods so
            # #1824's grouping is preserved unchanged.
            pod_group = labels.get(_QA_RUN_ID_LABEL) or job_name
            # The reported run_id is the run-id label when set, else the
            # job-name (the pre-#1821 value), so the existing keys never
            # regress for a single-pod run.
            pod_run_id = labels.get(_QA_RUN_ID_LABEL) or job_name
            started = pod.status.start_time if pod.status else None
            started_iso = started.isoformat() if started else None

            if result is None:
                # First active pod fixes the run we report (single-flight).
                group_key = pod_group
                result = {
                    "run_id": pod_run_id,
                    "job_name": job_name,
                    "pod_name": pod.metadata.name,
                    "phase": phase,
                    "started_at": started_iso,
                    "pod_count": 1,
                }
            elif pod_group == group_key:
                # Another pod of the same run — fold it in.
                result["pod_count"] += 1
                if started_iso is not None and (
                    result["started_at"] is None or started_iso < result["started_at"]
                ):
                    result["started_at"] = started_iso
        return result

    # -- queries (continued) -----------------------------------------------
    def secret_exists(self, name: str) -> bool:
        """#894 — does a named Secret exist in the harness namespace?

        Used by the trigger endpoint to pre-check the Max-billed
        CronJob's CLAUDE_CODE_OAUTH_TOKEN Secret before creating a
        Job, so the operator sees a clean 422 ("token not
        provisioned — run `make qa-claude-token && make infra-apply`")
        instead of a half-created Job that pod-starts and 401s.

        Returns False on both "not found" (404) and "forbidden by
        RBAC" (403) — the latter only happens if the namespace's
        RBAC grant was rolled back, in which case the trigger would
        also fail later for the same reason; failing closed here
        with a misleading "not found" is preferable to crashing the
        endpoint.
        """
        k8s = self._kubernetes()
        core = k8s.client.CoreV1Api()
        try:
            core.read_namespaced_secret(name, self.namespace)
            return True
        except k8s.client.ApiException as exc:
            if exc.status in (403, 404):
                return False
            raise ClusterUnavailable(
                f"checking Secret {name!r} failed: {exc.status}"
            ) from exc

    # -- mutations ---------------------------------------------------------
    def trigger(
        self,
        personas: list[str],
        concurrency: int | None = None,
        explore_model: str | None = None,
        report_model: str | None = None,
        max_turns: int | None = None,
        run_duration_s: int | None = None,
        run_notes: str | None = None,
        mandatory_action_ids: list[str] | None = None,
        target_url: str | None = None,
        enabled_mcp_servers: list[str] | None = None,
        pod_count: int = 1,
        store=None,
    ) -> dict:
        """Start a QA run for ``personas`` (empty list = every persona).

        The Job is built from the ``qa-agents`` CronJob's ``jobTemplate`` so it
        always matches the deployed run shape; only the harness container's
        args (and optionally ``QA_HARNESS_CONCURRENCY`` / ``QA_EXPLORE_MODEL``
        / ``QA_REPORT_MODEL`` / ``QA_MAX_TURNS`` / ``QA_RUN_NOTES`` /
        ``QA_MANDATORY_ACTIONS``) are overridden per trigger. When any of
        these is ``None`` (or empty list) the harness uses the pod-spec
        default baked into the CronJob template (#824 for concurrency;
        #836 for the models; #858 for max_turns and run_notes; #861 for
        mandatory_action_ids — defaults are sonnet for explore, opus for
        report, 200 turns, no notes, no mandatory actions; see
        infra/main.tf and harness/qa_agents/config.py).

        ``run_notes`` is an operator-facing label persisted on the run
        document (the harness writes it through to qa-store when it creates
        the run doc). Lets a future operator answer "why did we kick off
        run X?" without grepping logs.

        ``mandatory_action_ids`` (#861) is the comma-joined list of
        coverage-catalog ids the persona MUST attempt this session. The
        harness's prompt renderer resolves these against
        ``qa_store.CATALOG`` and prepends a "MANDATORY THIS SESSION" block
        to the persona prompt. Empty list ⇒ no env emitted ⇒ pure
        free-rein run (the original behaviour, unchanged).

        ``pod_count`` (#1821) is the number of harness pods to fan the run
        across. ``pod_count == 1`` (the default) reproduces today's single-pod
        Job EXACTLY — a plain Job with the unchanged args. ``pod_count > 1``
        fans the run out as N SEPARATE Jobs (Option B), one per pod, each handed
        an explicit ``--personas <shard>`` slice of the roster computed here via
        :func:`_stripe_roster` (disjoint modulo stripes over the sorted roster).
        Every Job shares the one minted ``slyreply.ai/qa-run-id`` pod label +
        ``QA_RUN_ID`` env, and carries ``QA_POD_COUNT`` so each pod gates the
        cross-pod finish barrier; ``active_run`` folds the N pods into ONE run
        by the shared label. There is NO Indexed Job and no implicit
        ``JOB_COMPLETION_INDEX`` striping — a pod's persona slice is explicit in
        its own Job spec and ``kubectl logs``. Empty slices (``pod_count`` >
        persona count) are dropped, so the run uses
        ``min(pod_count, len(roster))`` pods.

        ``store`` (#1821), when provided, is the qa_store handle used to write
        the run document's ``expected_personas`` (the COMPLETE resolved persona
        roster) up front — the denominator the multi-pod finish barrier needs
        before any pod files a review. It is written for single- and multi-pod
        runs alike (sticky/idempotent: ``create_run`` only seeds it once, so a
        later harness ``create_run`` won't clobber it). ``None`` skips the write
        (the k8s-only unit tests that don't exercise the store).

        Refuses with :class:`RunLimitExceeded` (mapped to 422 by the endpoint)
        when ``pod_count × effective_concurrency`` would exceed
        :data:`MAX_SIMULTANEOUS_PERSONAS` — no Job is created in that case.
        """
        # COST CEILING (#1821): bound the worst-case simultaneous personas
        # against the single personal Claude Code Max subscription BEFORE any
        # cluster call, so an over-budget shape is rejected with no Job created
        # and no read of the CronJob. ``concurrency is None`` means "use the
        # pod-spec default", which is 1 in the deployed CronJob template, so we
        # treat None as 1 for the product (the conservative floor — the real
        # default is never higher than what the operator could pass).
        effective_concurrency = concurrency if concurrency is not None else 1
        if pod_count * effective_concurrency > MAX_SIMULTANEOUS_PERSONAS:
            raise RunLimitExceeded(
                f"pod_count ({pod_count}) × concurrency ({effective_concurrency}) "
                f"= {pod_count * effective_concurrency} exceeds the "
                f"{MAX_SIMULTANEOUS_PERSONAS}-persona simultaneous ceiling"
            )

        k8s = self._kubernetes()
        if self.active_run() is not None:
            raise RunAlreadyActive("a QA run is already in progress")

        # Single Max-only CronJob (the harness always bills Claude Code
        # Max). The Job is built from its jobTemplate so it always matches
        # the deployed run shape.
        effective_cronjob = self.cronjob_name

        batch = k8s.client.BatchV1Api()
        try:
            cron = batch.read_namespaced_cron_job(effective_cronjob, self.namespace)
        except k8s.client.ApiException as exc:
            raise ClusterUnavailable(
                f"reading CronJob {effective_cronjob!r} failed: {exc.status}"
            ) from exc

        job_spec = cron.spec.job_template.spec
        args = ["--all"] if not personas else ["--personas", ",".join(personas)]
        for container in job_spec.template.spec.containers:
            if container.name == _HARNESS_CONTAINER:
                container.args = args
                if concurrency is not None:
                    # Per-trigger override of the pod-spec default. The
                    # harness reads QA_HARNESS_CONCURRENCY at startup (#824);
                    # strip any existing entry with the same name first so
                    # the override is unambiguous (Kubernetes does not
                    # de-duplicate env entries and getenv() returns the
                    # first match, so a stale earlier entry would win).
                    container.env = [
                        e for e in (container.env or [])
                        if getattr(e, "name", None) != "QA_HARNESS_CONCURRENCY"
                    ] + [
                        k8s.client.V1EnvVar(
                            name="QA_HARNESS_CONCURRENCY", value=str(concurrency)
                        )
                    ]
                if explore_model is not None:
                    # Per-trigger override of QA_EXPLORE_MODEL (#836). Same
                    # strip-then-append shape as concurrency above: the
                    # CronJob template already sets this env var from the
                    # qa_agent_explore_model TF variable, so we MUST replace
                    # it (first-match-wins under Linux getenv) rather than
                    # appending — a duplicate entry would let the older
                    # template value silently win.
                    container.env = [
                        e for e in (container.env or [])
                        if getattr(e, "name", None) != "QA_EXPLORE_MODEL"
                    ] + [
                        k8s.client.V1EnvVar(
                            name="QA_EXPLORE_MODEL", value=explore_model
                        )
                    ]
                if report_model is not None:
                    # Per-trigger override of QA_REPORT_MODEL (#836). Same
                    # reasoning as explore_model above.
                    container.env = [
                        e for e in (container.env or [])
                        if getattr(e, "name", None) != "QA_REPORT_MODEL"
                    ] + [
                        k8s.client.V1EnvVar(
                            name="QA_REPORT_MODEL", value=report_model
                        )
                    ]
                if max_turns is not None:
                    # Per-trigger override of QA_MAX_TURNS (#858). Same
                    # strip-then-append pattern as the model overrides — a
                    # duplicate env entry would let the older template
                    # value silently win under Linux getenv().
                    container.env = [
                        e for e in (container.env or [])
                        if getattr(e, "name", None) != "QA_MAX_TURNS"
                    ] + [
                        k8s.client.V1EnvVar(
                            name="QA_MAX_TURNS", value=str(max_turns)
                        )
                    ]
                if run_duration_s is not None:
                    # #1115 — per-trigger override of QA_RUN_TIMEOUT_S, the
                    # wall-clock wrapper around the whole run (asyncio.
                    # wait_for in runner.py). Same strip-then-append pattern
                    # as the others. Empty / None leaves the env unset and
                    # the harness uses its 7200s default.
                    container.env = [
                        e for e in (container.env or [])
                        if getattr(e, "name", None) != "QA_RUN_TIMEOUT_S"
                    ] + [
                        k8s.client.V1EnvVar(
                            name="QA_RUN_TIMEOUT_S", value=str(run_duration_s)
                        )
                    ]
                if run_notes is not None:
                    # Per-trigger free-text label (#858). The harness reads
                    # QA_RUN_NOTES and persists it onto the run document
                    # when it calls create_run, so the review UI can show
                    # "why did we kick off this run?" on the runs list.
                    container.env = [
                        e for e in (container.env or [])
                        if getattr(e, "name", None) != "QA_RUN_NOTES"
                    ] + [
                        k8s.client.V1EnvVar(
                            name="QA_RUN_NOTES", value=run_notes
                        )
                    ]
                if mandatory_action_ids:
                    # Per-trigger mandatory coverage actions (#861).
                    # Comma-joined for env transport; the harness's
                    # Config.from_env parses it with the same split-and-
                    # strip logic that produced this string. The env is
                    # emitted ONLY when at least one id is selected — an
                    # empty list keeps the env var unset and the harness
                    # treats that as "no mandatory items" (today's
                    # default). Same strip-then-append pattern as the
                    # other overrides to guard against first-match-wins
                    # under Linux getenv().
                    container.env = [
                        e for e in (container.env or [])
                        if getattr(e, "name", None) != "QA_MANDATORY_ACTIONS"
                    ] + [
                        k8s.client.V1EnvVar(
                            name="QA_MANDATORY_ACTIONS",
                            value=",".join(mandatory_action_ids),
                        )
                    ]
                if target_url is not None:
                    # #1018 — per-trigger override of QA_WEB_BASE_URL (Slice 1
                    # of #1006 agnostic-tenant epic). Pre-#1018 the CronJob
                    # template hardcoded http://frontend, so every run pointed
                    # at the SlyReply sandbox — the gating blocker to testing
                    # any other tenant. Operator-provided URL goes straight
                    # through; the API has already validated it's http(s).
                    # Same strip-then-append shape as the other overrides;
                    # the env name lives unchanged in qa_agents/config.py
                    # which reads QA_WEB_BASE_URL.
                    container.env = [
                        e for e in (container.env or [])
                        if getattr(e, "name", None) != "QA_WEB_BASE_URL"
                    ] + [
                        k8s.client.V1EnvVar(
                            name="QA_WEB_BASE_URL", value=target_url,
                        )
                    ]
                if enabled_mcp_servers:
                    # #1031 — Slice C of the MCP visibility epic. Comma-
                    # joined list of MCP server ids the operator opted
                    # IN for this run. Empty list / None ⇒ env stays
                    # unset and the harness's _resolve_enabled_mcp_servers
                    # falls back to catalog defaults (the pre-Slice-C
                    # behaviour preservation contract). Same strip-then-
                    # append shape as every other override — first-match-
                    # wins under Linux getenv makes the strip mandatory.
                    # The API has already validated each id against the
                    # catalog above, so an unknown id can't reach here.
                    container.env = [
                        e for e in (container.env or [])
                        if getattr(e, "name", None) != "QA_ENABLED_MCPS"
                    ] + [
                        k8s.client.V1EnvVar(
                            name="QA_ENABLED_MCPS",
                            value=",".join(enabled_mcp_servers),
                        )
                    ]

        # #1821 — mint ONE shared run id and inject it as QA_RUN_ID across
        # every harness container, so a multi-pod run lands in a single
        # qa_runs document. The orchestrator already honours QA_RUN_ID via
        # _shared_run_id (orchestrator.py); the Atlas sink generates its own
        # ``qa-<UTC timestamp>`` id when the env is unset, so for a single
        # pod the behaviour is unchanged — we just make the grouping id
        # explicit and operator-visible at trigger time rather than letting
        # the pod self-assign one. Same strip-then-append env shape as the
        # other overrides above (first-match-wins under Linux getenv makes
        # the strip mandatory) and matches the sink's own id format so a
        # generated-vs-injected id is indistinguishable downstream.
        shared_run_id = f"qa-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        for container in job_spec.template.spec.containers:
            if container.name == _HARNESS_CONTAINER:
                container.env = [
                    e for e in (container.env or [])
                    if getattr(e, "name", None) != "QA_RUN_ID"
                ] + [
                    k8s.client.V1EnvVar(name="QA_RUN_ID", value=shared_run_id)
                ]
                if pod_count > 1:
                    # #1821 (Option B) — QA_POD_COUNT > 1 tells each pod it's
                    # part of a multi-pod run, so it uses the cross-pod finish
                    # barrier (``finish_if_last``) instead of finishing the run
                    # solo. It no longer drives persona sharding — each pod is
                    # handed an explicit ``--personas`` slice below — so its
                    # exact value only needs to be > 1. Only emitted for a
                    # genuine multi-pod run so the single-pod env stays exactly
                    # as it is today (QA_POD_COUNT unset ⇒ harness default 1).
                    # Same strip-then-append shape as every other override.
                    container.env = [
                        e for e in (container.env or [])
                        if getattr(e, "name", None) != "QA_POD_COUNT"
                    ] + [
                        k8s.client.V1EnvVar(
                            name="QA_POD_COUNT", value=str(pod_count)
                        )
                    ]

        # The run-id pod-template label is what active_run() reports as the
        # run_id (and what groups a multi-pod run's N pods into one row).
        # #1822 follow-up: #1821 only set it on the pod_count > 1 branch, so
        # a single-pod run's pod carried no label, active_run() fell back to
        # the JOB name (qa-ui-max-<epoch>) and every "open live run" link
        # 404'd — the qa_runs document lives under ``shared_run_id``
        # (qa-<UTC>Z), never the job name. Label EVERY run's pods.
        pod_template = job_spec.template
        meta = getattr(pod_template, "metadata", None)
        if meta is None:
            meta = k8s.client.V1ObjectMeta(labels={})
            pod_template.metadata = meta
        if getattr(meta, "labels", None) is None:
            meta.labels = {}
        meta.labels[_QA_RUN_ID_LABEL] = shared_run_id

        # #1821 — write the finish-barrier denominator (expected_personas: the
        # COMPLETE resolved roster) onto the run doc BEFORE any Job is created,
        # so the harness barrier always has its denominator regardless of which
        # pod's create_run lands first. Sticky/idempotent in create_run, so a
        # later (per-pod) create_run won't clobber or grow it — critical under
        # Option B, where each pod's own create_run declares only its slice.
        # Skipped when no store is injected (the k8s-only unit tests).
        resolved_personas = list(personas) if personas else list(KNOWN_PERSONAS)
        if store is not None:
            from qa_store import create_run  # noqa: PLC0415 - optional, lazy

            create_run(
                store,
                shared_run_id,
                resolved_personas,
                run_notes=run_notes or "",
                expected_personas=resolved_personas,
            )

        # Every run is Max-billed now (the single qa-agents CronJob scrubs
        # ANTHROPIC_API_KEY so the harness always uses Claude Code Max).
        # Keep the "max" job-name suffix + qa-billing label so existing
        # `kubectl get jobs` selectors and the review-UI Max pill (which
        # reads the accounting backend field) stay consistent.
        base_name = f"qa-ui-max-{int(time.time())}"
        labels = {
            "app": "qa-agents",
            "slyreply.ai/triggered-by": "review-ui",
            "qa-billing": "max",
        }

        # #1821 (Option B) — fan a multi-pod run out across N SEPARATE labelled
        # Jobs rather than one Indexed Job. Each Job runs an explicit
        # ``--personas <shard>`` slice of the roster, so a pod's workload is
        # visible in its own Job spec and ``kubectl logs`` — no implicit
        # JOB_COMPLETION_INDEX striping. All N Jobs deep-copy the one fully
        # configured ``job_spec`` (so they share every env override, the
        # QA_RUN_ID / QA_POD_COUNT envs, and the ``slyreply.ai/qa-run-id`` pod
        # label stamped above) and differ ONLY in the harness ``--personas``
        # arg and the per-pod job name. pod_count == 1 stays a single plain Job
        # whose args are unchanged — byte-for-byte today's single-pod shape.
        if pod_count <= 1:
            planned = [(base_name, job_spec)]
        else:
            # Disjoint modulo stripes over the sorted roster; drop empty slices
            # (operator asked for more pods than personas) so we never emit a
            # ``--personas`` with no ids, which the harness rejects.
            stripes = [s for s in _stripe_roster(resolved_personas, pod_count) if s]
            planned = []
            for i, stripe in enumerate(stripes):
                spec_i = copy.deepcopy(job_spec)
                for container in spec_i.template.spec.containers:
                    if container.name == _HARNESS_CONTAINER:
                        container.args = ["--personas", ",".join(stripe)]
                planned.append((f"{base_name}-p{i}", spec_i))

        created: list[str] = []
        for name, spec in planned:
            job = k8s.client.V1Job(
                metadata=k8s.client.V1ObjectMeta(name=name, labels=labels),
                spec=spec,
            )
            try:
                batch.create_namespaced_job(self.namespace, job)
            except k8s.client.ApiException as exc:
                # Best-effort teardown of any sibling Jobs already created for
                # this run, so a mid-fan-out failure doesn't strand half a run
                # (all share the run-id label, but we delete by the names we
                # know we created — no label-list round-trip needed).
                for done in created:
                    try:
                        batch.delete_namespaced_job(
                            done, self.namespace, propagation_policy="Background"
                        )
                    except k8s.client.ApiException:
                        pass
                raise RunControlError(
                    f"creating Job {name!r} failed: {exc.status} {exc.reason}"
                ) from exc
            created.append(name)

        return {
            # ``job_name`` stays the first (or only) Job for back-compat with
            # callers/tests that read a single name; ``job_names`` is the full
            # fan-out set, and ``pod_count`` is the ACTUAL pod count after empty
            # slices were dropped (≤ the requested pod_count).
            "job_name": created[0],
            "job_names": created,
            "run_id": shared_run_id,
            "pod_count": len(created),
            "personas": resolved_personas,
        }

    # -- log streaming -----------------------------------------------------
    def stream_logs(self) -> Iterator[str]:
        """Yield log lines from the active run's harness container.

        Waits out the short-lived wipe-and-seed init container, then follows
        the harness container line by line. Ends when the pod terminates (the
        run finished) or when no run is active.
        """
        k8s = self._kubernetes()
        core = k8s.client.CoreV1Api()

        active = self.active_run()
        if active is None:
            yield "(no QA run is currently active)"
            return
        pod = active["pod_name"]

        # The harness container only exists once wipe-and-seed has finished;
        # until then read_namespaced_pod_log 400s. Retry for ~2 minutes.
        #
        # NB: do NOT wrap this in ``watch.Watch().stream(...)``. ``Watch.stream``
        # is for watching API *resources* (pod lifecycle events) and forces
        # ``watch=True`` onto the wrapped call — but ``read_namespaced_pod_log``
        # only accepts ``follow``. kubernetes-python <36 silently ignored the
        # stray kwarg; 36.0.0 is strict and raises ``ApiTypeError`` (#849),
        # which 500s every live-log request. The documented log-streaming
        # pattern is ``_preload_content=False`` + ``response.stream()``.
        for _ in range(60):
            try:
                response = core.read_namespaced_pod_log(
                    name=pod,
                    namespace=self.namespace,
                    container=_HARNESS_CONTAINER,
                    follow=True,
                    _preload_content=False,
                )
            except k8s.client.ApiException as exc:
                if exc.status == 400:  # container still waiting on the init step
                    yield "(waiting for the run container to start…)"
                    time.sleep(2)
                    continue
                yield f"(log stream error: HTTP {exc.status})"
                return

            try:
                for chunk in response.stream(decode_content=False):
                    yield chunk.decode("utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001 - see below
                # #1822 — mid-stream failures (the K8s API server resetting
                # the connection, urllib3 ProtocolError, etc.) used to
                # propagate out of this generator, killing the SSE
                # StreamingResponse mid-flight; the browser saw
                # net::ERR_HTTP2_PROTOCOL_ERROR and EventSource entered an
                # auto-reconnect loop. Convert to one final in-band error
                # line and end the stream cleanly instead. Deliberately
                # ``Exception`` (not BaseException) so GeneratorExit — the
                # client disconnecting — still propagates naturally.
                # Collapse whitespace: a multi-line repr would corrupt the
                # caller's ``data: <line>`` SSE framing.
                reason = " ".join(
                    f"{type(exc).__name__}: {exc}".split()
                )[:200]
                yield f"(log stream error: {reason})"
            finally:
                response.release_conn()
            return  # stream ended — the run pod has terminated
        yield "(timed out waiting for the run container to start)"
