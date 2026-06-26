"""Unit tests for ``K8sRunControl`` groundwork toward multi-pod runs (#1821).

Two backward-compatible slices, both cluster-free (driven against fake
``kubernetes`` modules):

* **active_run run-grouping** — ``active_run`` historically returned the
  *first* Pending/Running pod with no ``run_id`` key, so App.vue's live-badge
  link (``active?.run_id``) was always null. It now groups the active pods by
  their owning Job (the ``job-name`` label = one run) and surfaces a ``run_id``
  plus ``pod_count``, keeping single-flight semantics and the existing keys
  (``job_name`` / ``pod_name`` / ``phase`` / ``started_at``).

* **shared-run-id plumbing** — ``trigger`` historically minted a ``job_name``
  but never set ``QA_RUN_ID`` on the spawned Job, so each harness pod
  self-assigned its own ``qa-<ts>`` id and a multi-pod run would scatter across
  several ``qa_runs`` documents. It now generates ONE shared run id at trigger
  time and injects it as ``QA_RUN_ID`` (same strip-then-append env shape as
  every other override), returning it as ``run_id``. Single-pod behaviour is
  unchanged; this is the prerequisite for multi-pod runs landing in one doc.
"""

from __future__ import annotations

import datetime as _dt
import re
import types
from unittest.mock import MagicMock

import mongomock
import pytest
from kubernetes.client import V1EnvVar, V1Job, V1ObjectMeta
from kubernetes.client.exceptions import ApiException
from qa_store import get_run
from qa_store.schema import Store

from qa_review_api.runs import (
    MAX_SIMULTANEOUS_PERSONAS,
    ClusterUnavailable,
    K8sRunControl,
    RunLimitExceeded,
)

# The pod-template label trigger() stamps on a multi-pod run; active_run()
# groups by it. Kept in sync with runs._QA_RUN_ID_LABEL.
_QA_RUN_ID_LABEL = "slyreply.ai/qa-run-id"

# ---------------------------------------------------------------------------
# active_run() run-grouping
# ---------------------------------------------------------------------------

# -- minimal fakes mirroring the kubernetes-python surface active_run touches -


class _FakePodStatus:
    def __init__(self, phase, start_time=None):
        self.phase = phase
        self.start_time = start_time


class _FakePodMeta:
    def __init__(self, name, labels):
        self.name = name
        self.labels = labels


class _FakePod:
    def __init__(self, name, phase, *, job_name=None, start_time=None, extra_labels=None):
        labels = dict(extra_labels or {})
        if job_name is not None:
            labels["job-name"] = job_name
        self.metadata = _FakePodMeta(name, labels)
        self.status = _FakePodStatus(phase, start_time)


class _FakePodList:
    def __init__(self, pods):
        self.items = list(pods)


class _FakeApiException(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


class _FakeCoreV1Api:
    def __init__(self, pods, *, raise_status=None):
        self._pods = pods
        self._raise_status = raise_status

    def list_namespaced_pod(self, namespace, label_selector=None):  # noqa: ARG002
        if self._raise_status is not None:
            raise _FakeApiException(self._raise_status)
        return _FakePodList(self._pods)


def _make_control(pods, *, raise_status=None) -> K8sRunControl:
    """Build a ``K8sRunControl`` whose ``_kubernetes`` is stubbed out."""
    core = _FakeCoreV1Api(pods, raise_status=raise_status)
    fake = types.SimpleNamespace()
    fake.client = types.SimpleNamespace(
        CoreV1Api=lambda: core,
        ApiException=_FakeApiException,
    )
    rc = K8sRunControl(namespace="slyreply-sandbox", cronjob_name="qa-agents")
    rc._kubernetes = lambda: fake  # type: ignore[method-assign]
    rc._configured = True
    return rc


# -- tests --------------------------------------------------------------------


def test_no_pods_means_no_active_run():
    assert _make_control([]).active_run() is None


def test_only_terminal_pods_means_no_active_run():
    pods = [
        _FakePod("p1", "Succeeded", job_name="qa-ui-max-1"),
        _FakePod("p2", "Failed", job_name="qa-ui-max-1"),
    ]
    assert _make_control(pods).active_run() is None


def test_single_running_pod_reports_run_id_and_count():
    started = _dt.datetime(2026, 6, 8, 12, 0, 0, tzinfo=_dt.UTC)
    pods = [_FakePod("qa-ui-max-99-abc", "Running", job_name="qa-ui-max-99",
                     start_time=started)]
    active = _make_control(pods).active_run()
    assert active is not None
    # New keys (#1821)
    assert active["run_id"] == "qa-ui-max-99"
    assert active["pod_count"] == 1
    # Backward-compatible keys preserved
    assert active["job_name"] == "qa-ui-max-99"
    assert active["pod_name"] == "qa-ui-max-99-abc"
    assert active["phase"] == "Running"
    assert active["started_at"] == started.isoformat()


def test_multi_pod_run_reports_as_one_active_run():
    """Several harness pods of the SAME run group into one active run with
    a pod_count > 1 -- the single-flight contract is per-run, not per-pod."""
    started = _dt.datetime(2026, 6, 8, 12, 0, 0, tzinfo=_dt.UTC)
    later = _dt.datetime(2026, 6, 8, 12, 0, 5, tzinfo=_dt.UTC)
    pods = [
        _FakePod("qa-ui-max-7-a", "Running", job_name="qa-ui-max-7", start_time=started),
        _FakePod("qa-ui-max-7-b", "Pending", job_name="qa-ui-max-7", start_time=later),
        _FakePod("qa-ui-max-7-c", "Running", job_name="qa-ui-max-7", start_time=later),
    ]
    active = _make_control(pods).active_run()
    assert active is not None
    assert active["run_id"] == "qa-ui-max-7"
    assert active["pod_count"] == 3


def test_started_at_is_earliest_pod_start_in_run():
    """The run's started_at reflects when the run began (earliest pod), not
    whichever pod happens to be listed first."""
    earlier = _dt.datetime(2026, 6, 8, 12, 0, 0, tzinfo=_dt.UTC)
    later = _dt.datetime(2026, 6, 8, 12, 5, 0, tzinfo=_dt.UTC)
    pods = [
        _FakePod("qa-ui-max-5-late", "Running", job_name="qa-ui-max-5", start_time=later),
        _FakePod("qa-ui-max-5-early", "Running", job_name="qa-ui-max-5", start_time=earlier),
    ]
    active = _make_control(pods).active_run()
    assert active is not None
    assert active["started_at"] == earlier.isoformat()


def test_representative_pod_name_is_a_pod_in_the_run():
    pods = [
        _FakePod("qa-ui-max-3-x", "Running", job_name="qa-ui-max-3"),
        _FakePod("qa-ui-max-3-y", "Running", job_name="qa-ui-max-3"),
    ]
    active = _make_control(pods).active_run()
    assert active is not None
    assert active["pod_name"] in {"qa-ui-max-3-x", "qa-ui-max-3-y"}


def test_first_active_run_wins_when_multiple_runs_present():
    """Single-flight: if two distinct runs somehow overlap, report exactly
    one (the run of the first active pod encountered) -- never a blend."""
    pods = [
        _FakePod("qa-ui-max-1-a", "Running", job_name="qa-ui-max-1"),
        _FakePod("qa-ui-max-2-a", "Running", job_name="qa-ui-max-2"),
    ]
    active = _make_control(pods).active_run()
    assert active is not None
    assert active["run_id"] == "qa-ui-max-1"
    # Only pods of the reported run are counted, not the other run's pods.
    assert active["pod_count"] == 1


def test_missing_job_name_label_falls_back_to_empty_run_id():
    """A pod with no job-name label still reports (back-compat with the old
    ``labels.get("job-name", "")`` behaviour) rather than crashing."""
    pods = [_FakePod("loose-pod", "Running", job_name=None)]
    active = _make_control(pods).active_run()
    assert active is not None
    assert active["run_id"] == ""
    assert active["job_name"] == ""
    assert active["pod_name"] == "loose-pod"
    assert active["pod_count"] == 1


def test_api_exception_raises_cluster_unavailable():
    rc = _make_control([], raise_status=503)
    with pytest.raises(ClusterUnavailable):
        rc.active_run()


# ---------------------------------------------------------------------------
# shared-run-id plumbing in trigger()
# ---------------------------------------------------------------------------

_HARNESS = "harness"
# ``qa-YYYYMMDDTHHMMSSZ`` — must match the AtlasReportSink's own generated id
# format (report.py) so an injected id is indistinguishable from a self-assigned
# one downstream.
_RUN_ID_RE = re.compile(r"^qa-\d{8}T\d{6}Z$")


def _cronjob_with_env(harness_env):
    """A fake CronJob whose harness container starts with ``harness_env``.

    Only the attributes ``trigger`` touches are modelled. A second, non-harness
    container is included to prove the injection only lands on the harness one.
    """
    harness = types.SimpleNamespace(name=_HARNESS, args=None, env=list(harness_env))
    seed = types.SimpleNamespace(name="wipe-and-seed", args=None, env=[])
    pod_spec = types.SimpleNamespace(containers=[harness, seed])
    template = types.SimpleNamespace(spec=pod_spec)
    job_spec = types.SimpleNamespace(template=template)
    job_template = types.SimpleNamespace(spec=job_spec)
    cron_spec = types.SimpleNamespace(job_template=job_template)
    return types.SimpleNamespace(spec=cron_spec), harness


@pytest.fixture
def trigger_env(monkeypatch):
    """(rc, batch_mock, harness_container) wired so ``trigger`` runs offline.

    ``active_run`` is short-circuited to ``None`` (no run in progress) so the
    guard passes; the real ``V1EnvVar``/``V1Job``/``V1ObjectMeta`` are used so
    the strip-by-name logic sees genuine ``.name`` attributes.
    """
    cron, harness = _cronjob_with_env(
        [V1EnvVar(name="QA_WEB_BASE_URL", value="http://frontend")]
    )
    batch = MagicMock(name="BatchV1Api")
    batch.read_namespaced_cron_job.return_value = cron

    client_mod = types.SimpleNamespace(
        BatchV1Api=lambda: batch,
        ApiException=ApiException,
        V1EnvVar=V1EnvVar,
        V1Job=V1Job,
        V1ObjectMeta=V1ObjectMeta,
    )
    k8s_stub = types.SimpleNamespace(client=client_mod)

    rc = K8sRunControl(namespace="slyreply-sandbox", cronjob_name="qa-agents")
    rc._configured = True
    rc._kubernetes = lambda: k8s_stub  # type: ignore[method-assign]
    rc.active_run = lambda: None  # type: ignore[method-assign]
    return rc, batch, harness


def _harness_env_of_created_job(batch) -> dict[str, str]:
    """The harness container's env (name→value) on the Job actually created."""
    (_ns, job), _ = batch.create_namespaced_job.call_args
    assert isinstance(job, V1Job)
    harness = next(c for c in job.spec.template.spec.containers if c.name == _HARNESS)
    return {e.name: e.value for e in (harness.env or [])}


def test_trigger_injects_qa_run_id_env(trigger_env):
    rc, batch, _harness = trigger_env

    rc.trigger(["first-impression-critic"])

    env = _harness_env_of_created_job(batch)
    assert "QA_RUN_ID" in env
    assert _RUN_ID_RE.match(env["QA_RUN_ID"]), env["QA_RUN_ID"]
    # The pre-existing override is untouched — we only add QA_RUN_ID.
    assert env["QA_WEB_BASE_URL"] == "http://frontend"


def test_trigger_returns_shared_run_id_matching_injected_env(trigger_env):
    rc, batch, _harness = trigger_env

    result = rc.trigger([])

    assert _RUN_ID_RE.match(result["run_id"]), result["run_id"]
    env = _harness_env_of_created_job(batch)
    # The id the caller sees is exactly the id baked into the Job's env, so the
    # review UI can correlate the trigger response with the stored run doc.
    assert result["run_id"] == env["QA_RUN_ID"]
    # job_name is still returned unchanged (distinct from the run id).
    assert result["job_name"].startswith("qa-ui-max-")


def test_trigger_strips_stale_qa_run_id_before_injecting(monkeypatch):
    """A QA_RUN_ID already on the template is replaced, not duplicated.

    Linux ``getenv`` returns the FIRST matching entry and Kubernetes does not
    de-duplicate env vars, so a stale template entry would silently win unless
    we strip-then-append (the contract every other override in trigger holds).
    """
    cron, harness = _cronjob_with_env(
        [V1EnvVar(name="QA_RUN_ID", value="qa-stale-from-template")]
    )
    batch = MagicMock(name="BatchV1Api")
    batch.read_namespaced_cron_job.return_value = cron
    client_mod = types.SimpleNamespace(
        BatchV1Api=lambda: batch,
        ApiException=ApiException,
        V1EnvVar=V1EnvVar,
        V1Job=V1Job,
        V1ObjectMeta=V1ObjectMeta,
    )
    k8s_stub = types.SimpleNamespace(client=client_mod)
    rc = K8sRunControl(namespace="slyreply-sandbox", cronjob_name="qa-agents")
    rc._configured = True
    rc._kubernetes = lambda: k8s_stub  # type: ignore[method-assign]
    rc.active_run = lambda: None  # type: ignore[method-assign]

    result = rc.trigger(["first-impression-critic"])

    (_ns, job), _ = batch.create_namespaced_job.call_args
    harness_created = next(
        c for c in job.spec.template.spec.containers if c.name == _HARNESS
    )
    run_id_entries = [e for e in harness_created.env if e.name == "QA_RUN_ID"]
    assert len(run_id_entries) == 1, "exactly one QA_RUN_ID entry, no duplicate"
    assert run_id_entries[0].value == result["run_id"]
    assert run_id_entries[0].value != "qa-stale-from-template"


def test_trigger_does_not_touch_non_harness_container(trigger_env):
    rc, batch, _harness = trigger_env

    rc.trigger(["first-impression-critic"])

    (_ns, job), _ = batch.create_namespaced_job.call_args
    seed = next(
        c for c in job.spec.template.spec.containers if c.name == "wipe-and-seed"
    )
    assert all(e.name != "QA_RUN_ID" for e in (seed.env or []))


# ---------------------------------------------------------------------------
# Multi-pod RUN-CONTROL (#1821, Option B) — N separate labelled Jobs (one per
# pod, each with an explicit ``--personas`` slice), QA_POD_COUNT, shared run-id
# pod label, cost ceiling, expected_personas, active_run grouping.
# ---------------------------------------------------------------------------

# The cronjob fixture above models the harness container only; for the per-pod
# assertions we also need the pod-template + job-spec to be mutable objects.
# ``_cronjob_with_env`` already builds them as ``types.SimpleNamespace``
# (arbitrary-attribute), and trigger() deep-copies the spec once per pod, so
# each created Job gets an independent ``--personas`` arg.


def _make_multipod_control(harness_env=None, *, store=None):
    """A trigger-capable control whose ``active_run`` reports no run.

    Returns ``(rc, batch, harness_container)``. Uses the real ``V1*`` types so
    the strip-by-name + label logic sees genuine attributes. The pod template
    starts with NO ``metadata`` (mirroring a CronJob jobTemplate that ships no
    pod labels of its own) to prove trigger() creates one for the run-id label.
    """
    if harness_env is None:
        harness_env = [V1EnvVar(name="QA_SINK", value="atlas")]
    harness = types.SimpleNamespace(name=_HARNESS, args=None, env=list(harness_env))
    pod_spec = types.SimpleNamespace(containers=[harness])
    template = types.SimpleNamespace(spec=pod_spec)  # NB: no metadata attr
    job_spec = types.SimpleNamespace(template=template)
    job_template = types.SimpleNamespace(spec=job_spec)
    cron_spec = types.SimpleNamespace(job_template=job_template)
    cron = types.SimpleNamespace(spec=cron_spec)

    batch = MagicMock(name="BatchV1Api")
    batch.read_namespaced_cron_job.return_value = cron
    client_mod = types.SimpleNamespace(
        BatchV1Api=lambda: batch,
        ApiException=ApiException,
        V1EnvVar=V1EnvVar,
        V1Job=V1Job,
        V1ObjectMeta=V1ObjectMeta,
    )
    k8s_stub = types.SimpleNamespace(client=client_mod)
    rc = K8sRunControl(namespace="slyreply-sandbox", cronjob_name="qa-agents")
    rc._configured = True
    rc._kubernetes = lambda: k8s_stub  # type: ignore[method-assign]
    rc.active_run = lambda: None  # type: ignore[method-assign]
    return rc, batch, harness


def _created_job(batch) -> V1Job:
    (_ns, job), _ = batch.create_namespaced_job.call_args
    return job


def _mongomock_store() -> Store:
    s = Store(client=mongomock.MongoClient(), db_name="slyreply_qa_test")
    s.runs.create_index("run_id", unique=True)
    return s


# -- pod_count == 1 reproduces today's single-pod Job EXACTLY -----------------


def test_single_pod_job_is_not_indexed(trigger_env):
    """pod_count defaults to 1 → a plain, non-Indexed Job: no completion_mode,
    no parallelism/completions, no QA_POD_COUNT env."""
    rc, batch, _harness = trigger_env
    rc.trigger(["first-impression-critic"])
    job = _created_job(batch)
    spec = job.spec
    assert getattr(spec, "completion_mode", None) in (None, "NonIndexed")
    assert getattr(spec, "parallelism", None) is None
    assert getattr(spec, "completions", None) is None
    # No QA_POD_COUNT on the harness container.
    env = _harness_env_of_created_job(batch)
    assert "QA_POD_COUNT" not in env


def test_single_pod_job_carries_run_id_pod_label(trigger_env):
    """#1822 follow-up regression: the run-id pod-template label must be set
    for EVERY run, not just multi-pod ones. #1821 only labelled the
    pod_count > 1 branch, so active_run() fell back to the JOB name
    (qa-ui-max-<epoch>) for single-pod runs and the UI's "open live run"
    links 404'd — the qa_runs doc lives under the qa-<UTC>Z shared run id."""
    rc, batch, _harness = trigger_env
    result = rc.trigger(["first-impression-critic"])
    spec = _created_job(batch).spec
    meta = getattr(spec.template, "metadata", None)
    labels = getattr(meta, "labels", None) or {} if meta else {}
    assert labels[_QA_RUN_ID_LABEL] == result["run_id"]
    # And the label matches the QA_RUN_ID env the harness writes the doc under.
    env = _harness_env_of_created_job(batch)
    assert labels[_QA_RUN_ID_LABEL] == env["QA_RUN_ID"]


def test_single_pod_return_reports_pod_count_one(trigger_env):
    rc, _batch, _harness = trigger_env
    result = rc.trigger(["first-impression-critic"])
    assert result["pod_count"] == 1


# -- pod_count > 1 → N separate labelled Jobs, each with its --personas slice --


def _created_jobs(batch) -> list[V1Job]:
    """Every Job passed to create_namespaced_job, in creation order."""
    return [c.args[1] for c in batch.create_namespaced_job.call_args_list]


def _slice_of(job: V1Job) -> list[str]:
    """The persona slice the harness container of ``job`` was handed."""
    harness = next(
        c for c in job.spec.template.spec.containers if c.name == _HARNESS
    )
    assert harness.args[0] == "--personas", harness.args
    return harness.args[1].split(",")


def test_multi_pod_creates_n_separate_jobs_not_indexed():
    """Option B: pod_count > 1 fans out to N distinct Jobs — NOT one Indexed
    Job. None of them carries completion_mode/parallelism/completions."""
    rc, batch, _harness = _make_multipod_control()
    rc.trigger([], concurrency=2, pod_count=3)
    jobs = _created_jobs(batch)
    assert len(jobs) == 3
    for job in jobs:
        spec = job.spec
        assert getattr(spec, "completion_mode", None) in (None, "NonIndexed")
        assert getattr(spec, "parallelism", None) is None
        assert getattr(spec, "completions", None) is None
    # Distinct, per-pod-suffixed names off one shared base.
    names = [j.metadata.name for j in jobs]
    assert len(set(names)) == 3
    assert all("-p" in n for n in names)


def test_multi_pod_personas_are_sliced_disjointly_across_jobs():
    """The roster is split into disjoint, exhaustive modulo stripes — every
    persona is run by exactly one pod, and no pod's slice is empty."""
    from qa_review_api.runs import KNOWN_PERSONAS

    rc, batch, _harness = _make_multipod_control()
    rc.trigger([], concurrency=2, pod_count=3)
    slices = [_slice_of(j) for j in _created_jobs(batch)]
    flat = [p for s in slices for p in s]
    assert sorted(flat) == sorted(KNOWN_PERSONAS)  # exhaustive
    assert len(flat) == len(set(flat))  # pairwise disjoint
    assert all(s for s in slices)  # no empty slice


def test_multi_pod_return_reports_job_names_and_actual_pod_count():
    rc, _batch, _harness = _make_multipod_control()
    result = rc.trigger([], concurrency=2, pod_count=3)
    assert len(result["job_names"]) == 3
    assert result["pod_count"] == 3
    # job_name (singular) stays back-compat = the first fan-out Job.
    assert result["job_name"] == result["job_names"][0]


def test_multi_pod_drops_empty_slices_when_more_pods_than_personas():
    """More pods than personas → empty stripes are dropped, so we never emit a
    ``--personas`` with no ids and pod_count reflects the ACTUAL pods."""
    rc, batch, _harness = _make_multipod_control()
    # 2 personas × pod_count 4 (× concurrency 2 = 8, at the ceiling) → 2 Jobs.
    result = rc.trigger(
        ["first-impression-critic", "email-verifier"],
        concurrency=2,
        pod_count=4,
    )
    assert batch.create_namespaced_job.call_count == 2
    assert result["pod_count"] == 2
    for job in _created_jobs(batch):
        assert _slice_of(job)  # each non-empty


def test_multi_pod_injects_qa_pod_count_env():
    rc, batch, _harness = _make_multipod_control()
    rc.trigger([], concurrency=2, pod_count=4)
    env = _harness_env_of_created_job(batch)
    assert env["QA_POD_COUNT"] == "4"
    # QA_RUN_ID is still injected alongside (PR-2 plumbing intact).
    assert _RUN_ID_RE.match(env["QA_RUN_ID"]), env["QA_RUN_ID"]
    # Pre-existing env survives.
    assert env["QA_SINK"] == "atlas"


def test_multi_pod_sets_run_id_pod_label_to_minted_run_id():
    rc, batch, _harness = _make_multipod_control()
    result = rc.trigger(["first-impression-critic"], concurrency=2, pod_count=3)
    job = _created_job(batch)
    labels = job.spec.template.metadata.labels
    assert labels[_QA_RUN_ID_LABEL] == result["run_id"]
    # The label value matches the QA_RUN_ID env the pods read.
    env = _harness_env_of_created_job(batch)
    assert labels[_QA_RUN_ID_LABEL] == env["QA_RUN_ID"]


def test_multi_pod_run_id_label_does_not_clobber_existing_pod_labels():
    """If the jobTemplate ships pod labels, the run-id label is added, not
    a wholesale replace."""
    rc, batch, _harness = _make_multipod_control()
    # Give the pod template pre-existing metadata/labels.
    cron = batch.read_namespaced_cron_job.return_value
    cron.spec.job_template.spec.template.metadata = V1ObjectMeta(
        labels={"app": "qa-agents", "qa-billing": "max"}
    )
    rc.trigger([], concurrency=2, pod_count=2)
    labels = _created_job(batch).spec.template.metadata.labels
    assert labels["app"] == "qa-agents"
    assert labels["qa-billing"] == "max"
    assert _QA_RUN_ID_LABEL in labels


def test_multi_pod_does_not_touch_non_harness_container():
    harness = types.SimpleNamespace(name=_HARNESS, args=None, env=[])
    seed = types.SimpleNamespace(name="wipe-and-seed", args=None, env=[])
    pod_spec = types.SimpleNamespace(containers=[harness, seed])
    template = types.SimpleNamespace(spec=pod_spec)
    job_spec = types.SimpleNamespace(template=template)
    cron = types.SimpleNamespace(
        spec=types.SimpleNamespace(
            job_template=types.SimpleNamespace(spec=job_spec)
        )
    )
    batch = MagicMock(name="BatchV1Api")
    batch.read_namespaced_cron_job.return_value = cron
    client_mod = types.SimpleNamespace(
        BatchV1Api=lambda: batch, ApiException=ApiException,
        V1EnvVar=V1EnvVar, V1Job=V1Job, V1ObjectMeta=V1ObjectMeta,
    )
    rc = K8sRunControl(namespace="slyreply-sandbox", cronjob_name="qa-agents")
    rc._configured = True
    rc._kubernetes = lambda: types.SimpleNamespace(client=client_mod)
    rc.active_run = lambda: None
    rc.trigger([], concurrency=2, pod_count=3)
    seed_c = next(c for c in _created_job(batch).spec.template.spec.containers
                  if c.name == "wipe-and-seed")
    assert all(e.name != "QA_POD_COUNT" for e in (seed_c.env or []))


# -- cost ceiling: pod_count × concurrency ≤ MAX_SIMULTANEOUS_PERSONAS --------


def test_cost_ceiling_rejects_over_budget_and_creates_no_job():
    rc, batch, _harness = _make_multipod_control()
    # 4 pods × 3 concurrency = 12 > 8.
    with pytest.raises(RunLimitExceeded):
        rc.trigger(["first-impression-critic"], concurrency=3, pod_count=4)
    batch.create_namespaced_job.assert_not_called()
    # And it didn't even read the CronJob — the guard is before any cluster call.
    batch.read_namespaced_cron_job.assert_not_called()


def test_cost_ceiling_allows_exactly_at_the_limit():
    rc, batch, _harness = _make_multipod_control()
    # 4 × 2 = 8 == ceiling → allowed.
    assert MAX_SIMULTANEOUS_PERSONAS == 8
    rc.trigger([], concurrency=2, pod_count=4)
    assert batch.create_namespaced_job.called


def test_cost_ceiling_default_shape_is_under_limit():
    """The operator-facing defaults (pod_count 3 × concurrency 2 = 6) pass."""
    rc, batch, _harness = _make_multipod_control()
    rc.trigger([], concurrency=2, pod_count=3)
    assert batch.create_namespaced_job.called


def test_cost_ceiling_treats_none_concurrency_as_one():
    """concurrency=None means 'use the pod-spec default' (1 in the template),
    so even the max pod_count (4) × 1 = 4 is under the ceiling and allowed."""
    rc, batch, _harness = _make_multipod_control()
    rc.trigger([], concurrency=None, pod_count=4)
    assert batch.create_namespaced_job.called


# -- expected_personas written via create_run before Job creation ------------


def test_trigger_writes_expected_personas_for_explicit_list():
    store = _mongomock_store()
    rc, _batch, _harness = _make_multipod_control()
    result = rc.trigger(
        ["first-impression-critic", "email-verifier"],
        concurrency=2,
        pod_count=2,
        store=store,
    )
    doc = get_run(store, result["run_id"])
    assert doc is not None
    assert set(doc["expected_personas"]) == {
        "first-impression-critic", "email-verifier",
    }


def test_trigger_writes_full_roster_as_expected_personas_when_empty():
    """Empty personas → the FULL KNOWN_PERSONAS roster is the denominator."""
    from qa_review_api.runs import KNOWN_PERSONAS

    store = _mongomock_store()
    rc, _batch, _harness = _make_multipod_control()
    result = rc.trigger([], concurrency=2, pod_count=2, store=store)
    doc = get_run(store, result["run_id"])
    assert set(doc["expected_personas"]) == set(KNOWN_PERSONAS)


def test_trigger_writes_expected_personas_for_single_pod_too():
    """The denominator is written on single-pod runs as well (pod_count=1)."""
    store = _mongomock_store()
    rc, _batch, _harness = _make_multipod_control()
    result = rc.trigger(["first-impression-critic"], store=store)
    doc = get_run(store, result["run_id"])
    assert doc["expected_personas"] == ["first-impression-critic"]


def test_trigger_without_store_creates_no_run_doc():
    """No store injected → trigger still creates the Job, just no run doc."""
    rc, batch, _harness = _make_multipod_control()
    rc.trigger(["first-impression-critic"], concurrency=2, pod_count=2)
    assert batch.create_namespaced_job.called


# -- active_run: N pods of ONE multi-pod run report as ONE active run ---------


def test_active_run_groups_pods_by_run_id_label():
    """N pods of one run (now N separate Jobs, Option B) all carry the same
    run-id label → ONE active run with pod_count == N, even though k8s gives
    each pod — and each Job — a distinct name."""
    started = _dt.datetime(2026, 6, 8, 12, 0, 0, tzinfo=_dt.UTC)
    later = _dt.datetime(2026, 6, 8, 12, 0, 3, tzinfo=_dt.UTC)
    run_label = {_QA_RUN_ID_LABEL: "qa-20260608T120000Z"}
    pods = [
        _FakePod("qa-ui-max-7-0", "Running", job_name="qa-ui-max-7",
                 start_time=started, extra_labels=run_label),
        _FakePod("qa-ui-max-7-1", "Running", job_name="qa-ui-max-7",
                 start_time=later, extra_labels=run_label),
        _FakePod("qa-ui-max-7-2", "Pending", job_name="qa-ui-max-7",
                 start_time=later, extra_labels=run_label),
    ]
    active = _make_control(pods).active_run()
    assert active is not None
    assert active["run_id"] == "qa-20260608T120000Z"
    assert active["pod_count"] == 3
    assert active["started_at"] == started.isoformat()


def test_active_run_label_groups_even_with_distinct_job_names():
    """Grouping is by the run-id label, so pods sharing the label collapse
    into one run even if (pathologically) their job-name labels differ."""
    run_label = {_QA_RUN_ID_LABEL: "qa-20260608T120000Z"}
    pods = [
        _FakePod("p-a", "Running", job_name="job-a", extra_labels=run_label),
        _FakePod("p-b", "Running", job_name="job-b", extra_labels=run_label),
    ]
    active = _make_control(pods).active_run()
    assert active is not None
    assert active["run_id"] == "qa-20260608T120000Z"
    assert active["pod_count"] == 2


def test_active_run_guard_blocks_a_second_distinct_run():
    """Two DIFFERENT runs overlapping → single-flight reports exactly one
    (the first), so the trigger guard blocks a second run — but never the
    2nd..Nth pod of the SAME run (covered above)."""
    pods = [
        _FakePod("run1-0", "Running", job_name="job-1",
                 extra_labels={_QA_RUN_ID_LABEL: "qa-run-1"}),
        _FakePod("run2-0", "Running", job_name="job-2",
                 extra_labels={_QA_RUN_ID_LABEL: "qa-run-2"}),
    ]
    active = _make_control(pods).active_run()
    assert active is not None
    assert active["run_id"] == "qa-run-1"
    # Only the first run's single pod is counted, not the other run's.
    assert active["pod_count"] == 1
