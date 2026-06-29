"""Tests for the review UI FastAPI app.

The qa-store is mocked with mongomock; the GitHub call in the file-issue path
is monkeypatched. No real network, no real Mongo.
"""

from __future__ import annotations

import mongomock
import pytest
from fastapi.testclient import TestClient
from qa_store import add_persona_result, create_run, finish_run
from qa_store.schema import Store

from qa_review_api.app import create_app
from qa_review_api.runs import ClusterUnavailable, RunAlreadyActive, RunLimitExceeded
from qa_review_api.settings import Settings


@pytest.fixture
def store() -> Store:
    client = mongomock.MongoClient()
    s = Store(client=client, db_name="slyreply_qa_test")
    s.runs.create_index("run_id", unique=True)
    s.findings.create_index("finding_id", unique=True)
    # #860 — the Transcript-tab endpoints query this collection. Same
    # compound index that connect() ensures in production.
    s.steps.create_index(
        [("run_id", 1), ("persona_id", 1), ("step_n", 1)], unique=True
    )
    # #862 — scenarios are routed by `id`; the 409-on-duplicate test
    # depends on this index existing (mongomock honours unique
    # constraints, but only when the index is explicitly created).
    s.scenarios.create_index("id", unique=True)
    # #988 / #1000 — qa_personas. The TestPersonasAPI duplicate-409 test
    # depends on the unique index; same mongomock caveat as scenarios.
    s.personas.create_index("persona_id", unique=True)
    return s


@pytest.fixture
def seeded(store) -> Store:
    """A store with one finished run, two personas, four findings."""
    create_run(store, "qa-run-1", ["first-impression-critic", "desktop-evaluator"])
    add_persona_result(
        store, "qa-run-1", "first-impression-critic",
        review_markdown="## First impressions\n\nNervous but curious.",
        verdict="Cautiously yes.",
        accounting={"total_cost_usd": 0.5, "total_input_tokens": 1000},
        findings=[
            {"category": "confusion", "severity": "major", "title": "UID?", "body": "unclear"},
            {"category": "worry", "severity": "blocker", "title": "Privacy", "body": "scary"},
        ],
    )
    add_persona_result(
        store, "qa-run-1", "desktop-evaluator",
        review_markdown="## Why email\n\nI wanted a chat box.",
        verdict="Not for me.",
        accounting={"total_cost_usd": 0.7, "total_input_tokens": 2000},
        findings=[
            {"category": "copy", "severity": "minor", "title": "Jargon", "body": "the word UID"},
            {"category": "surprise", "severity": "nit", "title": "No dark mode", "body": ""},
        ],
    )
    finish_run(
        store, "qa-run-1",
        {"input_tokens": 3000, "output_tokens": 1500, "cache_tokens": 0},
    )
    return store


def _client(
    store, *, token="ghp_testtoken", run_control=None, seed_personas=False,
) -> TestClient:
    """Build a TestClient for one test.

    ``seed_personas`` defaults to False so tests start with a clean
    ``qa_personas`` collection — tests that DO want the seed pass
    ``seed_personas=True`` explicitly. Production startup keeps the seed
    on; this is just to keep test fixtures deterministic.
    """
    settings = Settings(
        qa_store_url="mongodb://x",
        qa_store_db="slyreply_qa_test",
        github_token=token,
        github_repo="mccullya/slyreply",
    )
    return TestClient(
        create_app(
            settings=settings,
            store=store,
            run_control=run_control,
            seed_personas=seed_personas,
        )
    )


# ---------------------------------------------------------------------------
# GET /api/runs
# ---------------------------------------------------------------------------
def test_list_runs_empty(store):
    resp = _client(store).get("/api/runs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_runs_returns_run_with_counts(seeded):
    resp = _client(seeded).get("/api/runs")
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) == 1
    run = runs[0]
    assert run["run_id"] == "qa-run-1"
    assert run["status"] == "reviewed"
    assert sorted(run["personas"]) == ["desktop-evaluator", "first-impression-critic"]
    # #1822 — new runs carry token totals only; no dollar fields.
    assert run["totals"]["input_tokens"] == 3000
    assert "cost_usd" not in run["totals"]
    assert run["finding_counts"]["blocker"] == 1
    assert run["finding_counts"]["major"] == 1
    assert run["finding_counts"]["minor"] == 1
    assert run["finding_counts"]["nit"] == 1


# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}
# ---------------------------------------------------------------------------
def test_get_run_full(seeded):
    resp = _client(seeded).get("/api/runs/qa-run-1")
    assert resp.status_code == 200
    run = resp.json()
    assert len(run["reviews"]) == 2
    assert len(run["findings"]) == 4
    first_impression_critic = next(r for r in run["reviews"] if r["persona"] == "first-impression-critic")
    assert "Nervous but curious." in first_impression_critic["review_markdown"]
    assert first_impression_critic["verdict"] == "Cautiously yes."


def test_get_run_404(store):
    resp = _client(store).get("/api/runs/nope")
    assert resp.status_code == 404


def test_runs_readers_pass_through_legacy_cost_fields(store):
    """BACK-COMPAT (#1822) — pre-#1822 run docs still carry
    ``totals.cost_usd`` / ``totals.real_cost_usd``. Both GET /api/runs and
    GET /api/runs/{id} must pass the stored values through verbatim (no
    KeyError, no stripping, no recomputation)."""
    create_run(store, "qa-run-legacy", ["first-impression-critic"])
    store.runs.update_one(
        {"run_id": "qa-run-legacy"},
        {"$set": {
            "status": "reviewed",
            "totals": {
                "input_tokens": 100, "output_tokens": 50, "cache_tokens": 0,
                "cost_usd": 1.25, "real_cost_usd": 0.0,
                "backend": "claude-code",
            },
        }},
    )
    client = _client(store)
    listed = client.get("/api/runs").json()
    assert listed[0]["totals"]["cost_usd"] == 1.25
    one = client.get("/api/runs/qa-run-legacy").json()
    assert one["totals"]["cost_usd"] == 1.25
    assert one["totals"]["real_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# #1030 — Slice B of the MCP visibility epic. The catalog is a static
# module in qa_agents.mcp_catalog; the API just serialises it.
# ---------------------------------------------------------------------------
def test_list_mcp_servers_returns_baseline_catalog(store):
    resp = _client(store).get("/api/mcp-servers")
    assert resp.status_code == 200
    servers = resp.json()["servers"]
    ids = [s["id"] for s in servers]
    # Baseline catalog ships playwright + email + findings (#1010);
    # #1019 children will append more.
    assert "playwright" in ids
    assert "email" in ids
    assert "findings" in ids


def test_list_mcp_servers_includes_all_metadata_fields(store):
    resp = _client(store).get("/api/mcp-servers")
    servers = resp.json()["servers"]
    # Pick the first entry and verify the full field set the UI needs.
    s = servers[0]
    assert isinstance(s["id"], str) and s["id"]
    assert isinstance(s["display_name"], str) and s["display_name"]
    assert isinstance(s["description"], str) and s["description"]
    assert isinstance(s["default_enabled"], bool)
    assert isinstance(s["persona_compat"], list)
    assert isinstance(s["tool_count"], int) and s["tool_count"] >= 0


def test_list_mcp_servers_preserves_catalog_order(store):
    """The catalog is curated 'most-used first' — Playwright before
    email before findings. Any sort the UI applies should be on top
    of the catalog's authoritative order, not replacing it."""
    resp = _client(store).get("/api/mcp-servers")
    ids = [s["id"] for s in resp.json()["servers"]]
    assert ids.index("playwright") < ids.index("email")
    assert ids.index("email") < ids.index("findings")


# #1029 — Slice A of the MCP visibility epic. GET /api/runs/{id} now
# includes mcp_servers_used aggregated from qa_run_steps.
def test_get_run_includes_mcp_servers_used(store):
    from qa_store.schema import create_run, record_step
    create_run(store, "run-mcp", ["first-impression-critic"])
    record_step(store, "run-mcp", "first-impression-critic", 1,
                tool_name="mcp__playwright__browser_navigate")
    record_step(store, "run-mcp", "first-impression-critic", 2,
                tool_name="mcp__playwright__browser_click")
    record_step(store, "run-mcp", "first-impression-critic", 3,
                tool_name="mcp__email__send_email")
    resp = _client(store).get("/api/runs/run-mcp")
    assert resp.status_code == 200
    assert resp.json()["mcp_servers_used"] == [
        {"server": "playwright", "calls": 2},
        {"server": "email", "calls": 1},
    ]


def test_get_run_mcp_servers_used_empty_when_no_steps(store):
    """A run with zero recorded steps still surfaces the field — empty
    list — so the frontend chip-list can render conditionally without
    a separate \"field present?\" check."""
    from qa_store.schema import create_run
    create_run(store, "run-bare", ["first-impression-critic"])
    resp = _client(store).get("/api/runs/run-bare")
    assert resp.status_code == 200
    assert resp.json()["mcp_servers_used"] == []


# ---------------------------------------------------------------------------
# PATCH /api/findings/{finding_id}
# ---------------------------------------------------------------------------
def test_patch_finding_status(seeded):
    client = _client(seeded)
    run = client.get("/api/runs/qa-run-1").json()
    fid = run["findings"][0]["finding_id"]
    resp = client.patch(f"/api/findings/{fid}", json={"status": "included"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "included"
    # Reflected in a subsequent read.
    again = client.get("/api/runs/qa-run-1").json()
    updated = next(f for f in again["findings"] if f["finding_id"] == fid)
    assert updated["status"] == "included"


def test_patch_finding_bad_status(seeded):
    client = _client(seeded)
    fid = client.get("/api/runs/qa-run-1").json()["findings"][0]["finding_id"]
    resp = client.patch(f"/api/findings/{fid}", json={"status": "bogus"})
    assert resp.status_code == 422


def test_patch_finding_unknown(store):
    resp = _client(store).patch("/api/findings/nope", json={"status": "open"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/runs/{run_id}/file-issue
# ---------------------------------------------------------------------------
def test_file_issue_creates_one_issue(seeded, monkeypatch):
    captured: dict = {}

    def _fake_create(repo, token, title, body, *, label="qa-agent-report"):
        captured.update(repo=repo, token=token, title=title, body=body, label=label)
        return "https://github.com/mccullya/slyreply/issues/777"

    monkeypatch.setattr("qa_review_api.app.create_github_issue", _fake_create)

    client = _client(seeded)
    # Mark two findings as included so they land in the issue body.
    run = client.get("/api/runs/qa-run-1").json()
    for f in run["findings"][:2]:
        client.patch(f"/api/findings/{f['finding_id']}", json={"status": "included"})

    resp = client.post("/api/runs/qa-run-1/file-issue")
    assert resp.status_code == 200
    assert resp.json()["gh_issue_url"].endswith("/issues/777")

    # One issue, correct repo + label.
    assert captured["repo"] == "mccullya/slyreply"
    assert captured["label"] == "qa-agent-report"
    assert captured["title"] == "QA review — run qa-run-1"
    # Body carries reviews + included findings.
    assert "Persona reviews" in captured["body"]
    assert "Nervous but curious." in captured["body"]
    assert "Included findings" in captured["body"]

    # Run is now marked filed with the url.
    after = client.get("/api/runs/qa-run-1").json()
    assert after["status"] == "filed"
    assert after["gh_issue_url"].endswith("/issues/777")


def test_file_issue_allowed_with_no_included_findings(seeded, monkeypatch):
    bodies: list[str] = []

    def _fake_create(repo, token, title, body, *, label="qa-agent-report"):
        bodies.append(body)
        return "https://github.com/mccullya/slyreply/issues/778"

    monkeypatch.setattr("qa_review_api.app.create_github_issue", _fake_create)

    resp = _client(seeded).post("/api/runs/qa-run-1/file-issue")
    assert resp.status_code == 200
    # Body notes the absence of included findings.
    assert "No findings were marked" in bodies[0]


def test_file_issue_unknown_run(store):
    resp = _client(store).post("/api/runs/nope/file-issue")
    assert resp.status_code == 404


def test_file_issue_without_github_token(seeded):
    resp = _client(seeded, token="").post("/api/runs/qa-run-1/file-issue")
    assert resp.status_code == 503


def test_file_issue_github_error(seeded, monkeypatch):
    import httpx

    def _boom(repo, token, title, body, *, label="qa-agent-report"):
        request = httpx.Request("POST", "https://api.github.com/x")
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    monkeypatch.setattr("qa_review_api.app.create_github_issue", _boom)
    resp = _client(seeded).post("/api/runs/qa-run-1/file-issue")
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Run control — GET /api/runs/personas|active, POST /api/runs/trigger,
# GET /api/runs/active/logs. The K8sRunControl is faked: no real cluster.
# ---------------------------------------------------------------------------
class FakeRunControl:
    """Stands in for K8sRunControl — records triggers, scripts the responses."""

    def __init__(
        self,
        *,
        active=None,
        log_lines=None,
        trigger_error=None,
        secret_exists=True,
    ):
        self._active = active
        self._log_lines = log_lines or []
        self._trigger_error = trigger_error
        # Controls what secret_exists() returns. Default True so existing
        # trigger tests pass the unconditional Max OAuth-token pre-check.
        self._secret_exists = secret_exists
        self.triggered: list[list[str]] = []
        self.concurrencies: list[int | None] = []
        self.explore_models: list[str | None] = []
        self.report_models: list[str | None] = []
        self.max_turns_list: list[int | None] = []
        # #1115 — record per-trigger run-duration overrides. None ==
        # use the harness's 7200s default.
        self.run_duration_s_list: list[int | None] = []
        self.run_notes_list: list[str | None] = []
        self.mandatory_action_ids_list: list[list[str]] = []
        # #1018 — record per-trigger target URL override. None == use the
        # CronJob template's baked-in QA_WEB_BASE_URL (today: the
        # in-cluster SlyReply sandbox).
        self.target_urls: list[str | None] = []
        # #1031 — record per-trigger MCP server selection (Slice C of
        # #1028). None / empty list == catalog defaults (the harness's
        # _resolve_enabled_mcp_servers falls through). Non-empty == the
        # exact opt-in.
        self.enabled_mcp_servers_list: list[list[str] | None] = []
        # P4 — record per-trigger capability-derived credentials (vaulted
        # creds injected as harness env vars). None == no target / no grants.
        self.capability_envs: list[dict | None] = []
        # Record secret-existence pre-check calls. The Max OAuth-token
        # Secret is pre-checked unconditionally now (every run is Max).
        self.secret_checks: list[str] = []
        # #1821 — record per-trigger pod_count (multi-pod fan-out). The
        # endpoint forwards 1 when pod_count is omitted (PR-4 default None →
        # single-pod Job); an explicit value passes straight through.
        self.pod_counts: list[int] = []
        # #1821 — record whether the endpoint handed us a store handle so we
        # could write expected_personas. The real trigger writes the run doc;
        # the fake just notes that it was offered the store.
        self.stores: list[object] = []

    def active_run(self):
        if isinstance(self._active, Exception):
            raise self._active
        return self._active

    def secret_exists(self, name):
        """#894 — return the canned answer for the pre-check."""
        self.secret_checks.append(name)
        if isinstance(self._secret_exists, Exception):
            raise self._secret_exists
        return self._secret_exists

    def trigger(
        self,
        personas,
        concurrency=None,
        explore_model=None,
        report_model=None,
        max_turns=None,
        run_duration_s=None,
        run_notes=None,
        mandatory_action_ids=None,
        target_url=None,
        enabled_mcp_servers=None,
        capability_env=None,
        pod_count=1,
        store=None,
    ):
        if self._trigger_error is not None:
            raise self._trigger_error
        self.triggered.append(list(personas))
        self.concurrencies.append(concurrency)
        self.pod_counts.append(pod_count)
        self.stores.append(store)
        self.explore_models.append(explore_model)
        self.report_models.append(report_model)
        self.max_turns_list.append(max_turns)
        self.run_duration_s_list.append(run_duration_s)
        self.run_notes_list.append(run_notes)
        self.target_urls.append(target_url)
        self.enabled_mcp_servers_list.append(
            list(enabled_mcp_servers) if enabled_mcp_servers else None
        )
        self.capability_envs.append(dict(capability_env) if capability_env else None)
        self.mandatory_action_ids_list.append(
            list(mandatory_action_ids) if mandatory_action_ids else []
        )
        self._active = {
            "job_name": "qa-ui-123",
            "pod_name": "qa-ui-123-abc",
            "phase": "Pending",
            "started_at": None,
        }
        return {
            "job_name": "qa-ui-123",
            "pod_count": pod_count,
            "personas": list(personas) or ["all"],
        }

    def stream_logs(self):
        yield from self._log_lines


def test_personas_endpoint_lists_the_full_catalog(store):
    """#1009 — the legacy 12-persona check became a >=25-persona check.
    The endpoint reads ``KNOWN_PERSONAS`` which since #1009 is derived
    from ``qa_agents.personas.PERSONAS`` at import time. We assert on
    the SIZE-floor + presence-of-known-ids rather than the full exact
    list because the catalog grows freely — pinning an exact size
    means every ``personas.py`` addition rots this test (``==25``
    broke on 2026-05-28 when PIA + ASHA landed in #1140, then again
    when CATALINA + AURORA landed in #1115)."""
    resp = _client(store, run_control=FakeRunControl()).get("/api/runs/personas")
    assert resp.status_code == 200
    personas = resp.json()["personas"]
    assert len(personas) >= 25
    # #1047 — personas now carry metadata, not bare ids. The trigger UI
    # renders {display_name, archetype, region, language} as cards; this
    # check pins the shape so a future regression that drops e.g.
    # `archetype` (the visible per-card subtitle) doesn't silently ship.
    ids = {p["id"] for p in personas}
    for pid in (
        "mobile-signup-visitor", "happy-path-signup", "privacy-skeptic",
        "adversarial-tester", "first-impression-critic",
    ):
        assert pid in ids
    for p in personas:
        assert {"id", "display_name", "archetype", "region", "language",
                "registered_email"} <= set(p)
        assert p["id"] and p["display_name"], f"empty id/name on {p}"


def test_active_run_none(store):
    resp = _client(store, run_control=FakeRunControl(active=None)).get("/api/runs/active")
    assert resp.status_code == 200
    assert resp.json() == {"active": None}


def test_active_run_present(store):
    active = {"job_name": "qa-ui-9", "pod_name": "qa-ui-9-x", "phase": "Running",
              "started_at": "2026-05-22T00:00:00+00:00"}
    resp = _client(store, run_control=FakeRunControl(active=active)).get("/api/runs/active")
    assert resp.status_code == 200
    assert resp.json()["active"]["job_name"] == "qa-ui-9"


def test_active_run_cluster_unavailable(store):
    rc = FakeRunControl(active=ClusterUnavailable("no kube config"))
    resp = _client(store, run_control=rc).get("/api/runs/active")
    assert resp.status_code == 503


def test_run_availability_true_when_cluster_reachable(store):
    resp = _client(store, run_control=FakeRunControl(active=None)).get("/api/runs/availability")
    assert resp.status_code == 200
    assert resp.json() == {"available": True, "reason": None}


def test_run_availability_false_without_cluster(store):
    rc = FakeRunControl(active=ClusterUnavailable("no kube config"))
    body = _client(store, run_control=rc).get("/api/runs/availability").json()
    assert body["available"] is False
    # A plain-language reason, not the raw kube-config error.
    assert "Kubernetes" in body["reason"]
    assert "kube-config" not in body["reason"]


def test_trigger_without_cluster_leads_with_plain_guidance(store):
    # The newcomer who completes the funnel and clicks Launch gets plain-language
    # guidance up front; the raw cluster cause is appended for operators.
    rc = FakeRunControl(secret_exists=ClusterUnavailable("Invalid kube-config file"))
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"]},
    )
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail.startswith("Persona runs execute on a Kubernetes cluster")
    assert "Invalid kube-config file" in detail  # raw cause preserved for debugging


def test_trigger_run_with_chosen_personas(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic", "email-verifier"]}
    )
    assert resp.status_code == 200
    assert resp.json()["job_name"] == "qa-ui-123"
    assert rc.triggered == [["first-impression-critic", "email-verifier"]]


def test_trigger_auto_enables_granted_capability_servers(store, monkeypatch):
    """P4 — a run for a target auto-enables the MCP servers that target has
    *granted* capabilities for, and injects their vaulted creds as env, without
    dropping the default-on servers."""
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    from qa_store.capabilities import set_capability_status
    set_capability_status(
        store, target_id="acme", capability_id="openapi-spec",
        status="granted", token="https://acme.test/openapi.json",
    )
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "target_id": "acme"},
    )
    assert resp.status_code == 200
    enabled = rc.enabled_mcp_servers_list[0]
    assert "openapi" in enabled            # granted capability lit its server
    assert "playwright" in enabled and "findings" in enabled  # defaults preserved
    # The vaulted spec URL reaches the run as the env var the harness reads.
    assert rc.capability_envs[0] == {"QA_OPENAPI_URL": "https://acme.test/openapi.json"}


def test_trigger_without_target_id_leaves_mcp_untouched(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"]},
    )
    assert resp.status_code == 200
    assert rc.enabled_mcp_servers_list[0] is None  # catalog defaults, unchanged
    assert rc.capability_envs[0] is None


def test_trigger_target_with_no_grants_is_a_plain_run(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "target_id": "acme"},
    )
    assert resp.status_code == 200
    assert rc.enabled_mcp_servers_list[0] is None
    assert rc.capability_envs[0] is None


def test_trigger_run_empty_means_active_set(store):
    """#1009 — pre-relaunch empty-personas meant "run all". Now it means
    "run the currently-active set". Seed one active persona and verify
    the trigger picks it up."""
    from qa_store.schema import upsert_persona
    upsert_persona(store, {
        "persona_id": "first-impression-critic",
        "display_name": "First-impression critic",
        "is_active": True, "is_default": True, "hidden": False,
        "registered_email": "fic@example.com", "color_token": "teal",
        "explore_system_prompt": "x", "report_system_prompt": "y",
        "flows": [], "uses_admin_login": False,
    })
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": []},
    )
    assert resp.status_code == 200
    assert rc.triggered == [["first-impression-critic"]]


def test_trigger_run_empty_with_no_active_personas_422s(store):
    """#1009 — empty personas + no active personas in the store = 422."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": []},
    )
    assert resp.status_code == 422
    assert "no personas are active" in resp.json()["detail"]


def test_trigger_run_rejects_unknown_persona(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic", "bob"]}
    )
    assert resp.status_code == 422
    assert "bob" in resp.json()["detail"]
    # Nothing was triggered.
    assert rc.triggered == []


def test_trigger_run_409_when_already_active(store):
    rc = FakeRunControl(trigger_error=RunAlreadyActive("a QA run is already in progress"))
    resp = _client(store, run_control=rc).post("/api/runs/trigger", json={"personas": ["first-impression-critic"]})
    assert resp.status_code == 409


def test_trigger_run_503_when_cluster_unavailable(store):
    rc = FakeRunControl(trigger_error=ClusterUnavailable("no kube config"))
    resp = _client(store, run_control=rc).post("/api/runs/trigger", json={"personas": ["first-impression-critic"]})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# #1821 — multi-pod pod_count: passthrough, bounds, store handoff, and the
# cost-ceiling 422 mapping.
# ---------------------------------------------------------------------------
def test_trigger_run_uses_pod_count_default(store):
    """No pod_count in body → single-pod (1) reaches run_control.

    #1821 PR-4 — pod_count defaults to None (field omitted), and the
    endpoint forwards 1 in that case so the spawned Job is the pre-#1821
    single-pod (non-Indexed) shape, byte-for-byte. The multi-pod fan-out
    is strictly opt-in via an explicit pod_count > 1."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"]}
    )
    assert resp.status_code == 200
    assert rc.pod_counts == [1]


def test_trigger_run_passes_pod_count_through(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "pod_count": 2, "concurrency": 2},
    )
    assert resp.status_code == 200
    assert rc.pod_counts == [2]


def test_trigger_run_accepts_pod_count_at_bounds(store):
    rc = FakeRunControl()
    for n in (1, 4):
        resp = _client(store, run_control=rc).post(
            "/api/runs/trigger",
            json={"personas": ["first-impression-critic"], "pod_count": n, "concurrency": 1},
        )
        assert resp.status_code == 200, resp.text
        rc._active = None
    assert rc.pod_counts == [1, 4]


@pytest.mark.parametrize("bad", [0, 5, -1, 100])
def test_trigger_run_rejects_out_of_range_pod_count(store, bad):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "pod_count": bad},
    )
    assert resp.status_code == 422
    assert rc.triggered == []


def test_trigger_run_hands_store_to_run_control(store):
    """The endpoint hands its store to trigger() so it can write the run doc's
    expected_personas (the finish-barrier denominator)."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"]}
    )
    assert resp.status_code == 200
    assert rc.stores == [store]


def test_trigger_run_422_when_cost_ceiling_exceeded(store):
    """#1821 — pod_count × concurrency over the simultaneous-persona ceiling
    surfaces as 422 (RunLimitExceeded mapped, same shape as the other
    request-shape rejections)."""
    rc = FakeRunControl(
        trigger_error=RunLimitExceeded("pod_count (4) × concurrency (3) = 12 exceeds")
    )
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "pod_count": 4, "concurrency": 3},
    )
    assert resp.status_code == 422
    assert "exceeds" in resp.json()["detail"]


def test_trigger_run_api_layer_rejects_over_ceiling_before_dispatch(store):
    """#1821 PR-4 — the API mirrors the pod_count × concurrency ceiling and
    422s BEFORE any run_control call, the same way the per-field bounds do.

    A 4×3=12 shape passes both per-field pydantic bounds (pod_count ≤ 4,
    concurrency ≤ 6) yet must be refused — the product is what loads the
    single Max subscription. The plain FakeRunControl scripts NO error, so a
    422 here proves the endpoint rejected the shape itself rather than relying
    on the K8s-layer guard. Nothing reaches run_control."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "pod_count": 4, "concurrency": 3},
    )
    assert resp.status_code == 422
    assert "exceeds" in resp.json()["detail"]
    # The over-budget shape never reached the cluster layer — no Job, and the
    # Max-token pre-check was never even consulted.
    assert rc.triggered == []
    assert rc.pod_counts == []
    assert rc.secret_checks == []


def test_trigger_run_api_layer_allows_product_at_ceiling(store):
    """#1821 PR-4 — pod_count × concurrency exactly AT the ceiling (4×2=8) is
    allowed; only strictly-over is refused."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "pod_count": 4, "concurrency": 2},
    )
    assert resp.status_code == 200, resp.text
    assert rc.pod_counts == [4]
    assert rc.concurrencies == [2]


# ---------------------------------------------------------------------------
# Per-trigger concurrency override (#826).
# ---------------------------------------------------------------------------
def test_trigger_run_uses_concurrency_default(store):
    """#1821 — the in-pod concurrency default is now 2 (was: omit/None).

    With the multi-pod fan-out, the run's worst-case simultaneous-persona
    count is ``pod_count × concurrency``; the operator-facing defaults
    (pod_count 3 × concurrency 2 = 6) sit under the 8-persona ceiling. So an
    empty body now resolves to concurrency=2 rather than "omit and let the
    pod-spec default win"."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"]}
    )
    assert resp.status_code == 200
    assert rc.concurrencies == [2]


def test_trigger_run_passes_concurrency_through(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"], "concurrency": 3}
    )
    assert resp.status_code == 200
    assert rc.triggered == [["first-impression-critic"]]
    assert rc.concurrencies == [3]


def test_trigger_run_accepts_concurrency_at_bounds(store):
    rc = FakeRunControl()
    for n in (1, 6):
        resp = _client(store, run_control=rc).post(
            "/api/runs/trigger", json={"personas": ["first-impression-critic"], "concurrency": n}
        )
        assert resp.status_code == 200, resp.text
        # FakeRunControl flips itself to active after each trigger; reset.
        rc._active = None
    assert rc.concurrencies == [1, 6]


@pytest.mark.parametrize("bad", [0, 7, -1, 100])
def test_trigger_run_rejects_out_of_range_concurrency(store, bad):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"], "concurrency": bad}
    )
    assert resp.status_code == 422
    # Nothing made it to run_control.
    assert rc.triggered == []
    assert rc.concurrencies == []


# ---------------------------------------------------------------------------
# Per-trigger model overrides (#836). The allowlist is the three model ids in
# active use across the project — see TriggerRunRequest in app.py for the
# pattern and the rationale.
# ---------------------------------------------------------------------------
def test_trigger_run_omits_models_by_default(store):
    """No model fields in body → run_control gets ``None`` for both →
    the spawned Job inherits whatever the CronJob template already sets
    (the qa_agent_*_model TF variables) and the harness keeps the default
    Sonnet-explore / Opus-report split."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"]}
    )
    assert resp.status_code == 200
    assert rc.explore_models == [None]
    assert rc.report_models == [None]


def test_trigger_run_passes_explore_model_through(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "explore_model": "claude-haiku-4-5"},
    )
    assert resp.status_code == 200
    assert rc.explore_models == ["claude-haiku-4-5"]
    # The report model still uses its baked-in default when omitted.
    assert rc.report_models == [None]


def test_trigger_run_passes_report_model_through(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "report_model": "claude-sonnet-4-6"},
    )
    assert resp.status_code == 200
    assert rc.explore_models == [None]
    assert rc.report_models == ["claude-sonnet-4-6"]


def test_trigger_run_passes_both_models_through(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={
            "personas": ["first-impression-critic"],
            "explore_model": "claude-haiku-4-5",
            "report_model": "claude-opus-4-7",
        },
    )
    assert resp.status_code == 200
    assert rc.explore_models == ["claude-haiku-4-5"]
    assert rc.report_models == ["claude-opus-4-7"]


# #1018 — target_url plumbing (Slice 1 of the agnostic-tenant epic).
def test_trigger_run_omits_target_url_by_default(store):
    """No target_url in body → run_control gets ``None`` → the spawned
    Job inherits the CronJob template's baked-in QA_WEB_BASE_URL. This
    preserves the pre-#1018 single-tenant default behaviour."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"]},
    )
    assert resp.status_code == 200, resp.text
    assert rc.target_urls == [None]


def test_trigger_run_passes_target_url_through(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={
            "personas": ["first-impression-critic"],
            "target_url": "https://staging.example.com",
        },
    )
    assert resp.status_code == 200, resp.text
    assert rc.target_urls == ["https://staging.example.com"]


@pytest.mark.parametrize(
    "good",
    [
        "https://staging.example.com",
        "http://localhost:5173",
        "http://frontend",                       # in-cluster service name
        "https://app-staging.tenant.co.uk/path",  # path component allowed
        "https://example.com:8443",              # explicit port allowed
    ],
)
def test_trigger_run_accepts_good_target_urls(store, good):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "target_url": good},
    )
    assert resp.status_code == 200, resp.text
    assert rc.target_urls == [good]


@pytest.mark.parametrize(
    "bad",
    [
        "staging.example.com",       # no scheme
        "ftp://staging.example.com", # wrong scheme
        "javascript:alert(1)",       # would-be injection
        "//cdn.example.com",         # protocol-relative
        "",                          # empty string (use null/omit instead)
        "https://example.com hax",   # embedded whitespace
    ],
)
def test_trigger_run_rejects_bad_target_urls(store, bad):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "target_url": bad},
    )
    assert resp.status_code == 422, resp.text


def test_trigger_run_rejects_target_url_over_500_chars(store):
    """#1018 — Pydantic caps target_url at max_length=500. A URL longer
    than that is almost always a logged-in deep-link to a specific
    session rather than a site entry point — operator misuse."""
    rc = FakeRunControl()
    too_long = "https://staging.example.com/" + ("x" * 500)
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "target_url": too_long},
    )
    assert resp.status_code == 422, resp.text


# #1031 — Slice C of the MCP visibility epic. Per-run MCP server
# selection. None / omitted = catalog defaults; non-empty list is the
# exact opt-in, validated against qa_agents.mcp_catalog.
def test_trigger_run_omits_enabled_mcp_servers_by_default(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"]},
    )
    assert resp.status_code == 200, resp.text
    # None reaches run_control.trigger → harness falls back to catalog
    # defaults (the pre-#1031 behaviour preservation contract).
    assert rc.enabled_mcp_servers_list == [None]


def test_trigger_run_passes_enabled_mcp_servers_through(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={
            "personas": ["first-impression-critic"],
            "enabled_mcp_servers": ["playwright", "findings"],
        },
    )
    assert resp.status_code == 200, resp.text
    assert rc.enabled_mcp_servers_list == [["playwright", "findings"]]


def test_trigger_run_rejects_unknown_mcp_server_id(store):
    """A typo in the operator's selection 422s before the Job is built —
    same fail-fast contract as the mandatory_action_ids validator."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={
            "personas": ["first-impression-critic"],
            "enabled_mcp_servers": ["playwright", "not-a-real-server"],
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "not-a-real-server" in detail
    assert "/api/mcp-servers" in detail


def test_trigger_run_accepts_empty_enabled_mcp_servers_list(store):
    """Empty list is equivalent to None — both signal 'use catalog
    defaults'. The harness reads QA_ENABLED_MCPS as the source of
    truth; an empty list parses to an empty tuple, which the
    _resolve_enabled_mcp_servers helper falls back from."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={
            "personas": ["first-impression-critic"],
            "enabled_mcp_servers": [],
        },
    )
    assert resp.status_code == 200, resp.text
    # Empty list reaches the FakeRunControl as None (the truthy check in
    # the FakeRunControl.trigger maps empty-list to None for assertion
    # convenience). Either way, no env override is emitted.
    assert rc.enabled_mcp_servers_list == [None]


def test_trigger_run_rejects_enabled_mcp_servers_over_20(store):
    """Pydantic caps at 20; longer is operator misuse or a bug."""
    rc = FakeRunControl()
    too_many = [f"server-{i}" for i in range(21)]
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={
            "personas": ["first-impression-critic"],
            "enabled_mcp_servers": too_many,
        },
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "model",
    ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"],
)
def test_trigger_run_accepts_each_allowlisted_model(store, model):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "explore_model": model, "report_model": model},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "bad",
    [
        "gpt-4",
        "claude-3-5-sonnet",
        "claude-sonnet-4-5",  # off-by-one
        "claude-haiku-4-5; rm -rf /",  # would-be injection
        "",
        "claude-opus-4-7-20260101",  # dated id not in the allowlist
    ],
)
def test_trigger_run_rejects_unknown_explore_model(store, bad):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"], "explore_model": bad}
    )
    assert resp.status_code == 422
    assert rc.triggered == []
    assert rc.explore_models == []


@pytest.mark.parametrize(
    "bad",
    ["gpt-4", "claude-3-5-sonnet", "claude-sonnet-4-5", "", "haiku"],
)
def test_trigger_run_rejects_unknown_report_model(store, bad):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"], "report_model": bad}
    )
    assert resp.status_code == 422
    assert rc.triggered == []
    assert rc.report_models == []


# ---------------------------------------------------------------------------
# Max-only billing — every trigger pre-checks the Max OAuth-token Secret
# unconditionally; there is no backend selector.
# ---------------------------------------------------------------------------
class TestMaxOnlyBilling:
    def test_trigger_pre_checks_secret_unconditionally(self, store):
        """No backend field exists — every run is Max-billed, so the
        OAuth-token Secret pre-check fires on every trigger before any
        Job is created."""
        rc = FakeRunControl(secret_exists=True)
        resp = _client(store, run_control=rc).post(
            "/api/runs/trigger", json={"personas": ["first-impression-critic"]}
        )
        assert resp.status_code == 200
        assert rc.secret_checks == ["qa-claude-code-credentials"]

    def test_missing_secret_returns_422(self, store):
        """If the OAuth token Secret hasn't been provisioned yet, the
        trigger must fail loud with a 422 BEFORE creating any Job —
        half-creating a Job that then 401s at pod-start is a worse UX
        (operator has to clean it up + read pod logs to figure out what
        went wrong)."""
        rc = FakeRunControl(secret_exists=False)
        resp = _client(store, run_control=rc).post(
            "/api/runs/trigger",
            json={"personas": ["first-impression-critic"]},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        # Error message must name the missing Secret and tell the
        # operator how to provision it.
        assert "qa-claude-code-credentials" in detail
        assert "make qa-claude-token" in detail
        assert "infra-apply" in detail
        # No Job created.
        assert rc.triggered == []

    def test_pre_check_cluster_unavailable_returns_503(self, store):
        """A real Kubernetes error during the pre-check (RBAC rolled
        back, API server down) should surface as 503 with the
        underlying message, not 422 — 422 is reserved for 'token not
        provisioned', the actionable case."""
        rc = FakeRunControl(
            secret_exists=ClusterUnavailable("apiserver unreachable")
        )
        resp = _client(store, run_control=rc).post(
            "/api/runs/trigger",
            json={"personas": ["first-impression-critic"]},
        )
        assert resp.status_code == 503
        assert "apiserver unreachable" in resp.json()["detail"]
        assert rc.triggered == []

    def test_stray_backend_field_is_ignored(self, store):
        """The body no longer has a backend field; a stray one from an
        old client is silently ignored (Pydantic drops unknown fields)
        and the run proceeds as Max-billed."""
        rc = FakeRunControl(secret_exists=True)
        resp = _client(store, run_control=rc).post(
            "/api/runs/trigger",
            json={"personas": ["first-impression-critic"], "backend": "api"},
        )
        assert resp.status_code == 200
        assert rc.triggered == [["first-impression-critic"]]
        assert rc.secret_checks == ["qa-claude-code-credentials"]


# ---------------------------------------------------------------------------
# K8sRunControl.trigger — verify the spawned Job spec carries (or doesn't
# carry) the QA_HARNESS_CONCURRENCY env var on the harness container.
# Drives the real method with a stubbed kubernetes module so we exercise the
# Job-spec mutation path end-to-end (no live cluster).
# ---------------------------------------------------------------------------
class _StubV1EnvVar:
    def __init__(self, name, value=None, value_from=None):
        self.name = name
        self.value = value
        self.value_from = value_from


class _StubContainer:
    def __init__(self, name, env=None, args=None):
        self.name = name
        self.env = env if env is not None else []
        self.args = args


class _StubPodSpec:
    def __init__(self, containers):
        self.containers = containers


class _StubPodTemplate:
    def __init__(self, spec):
        self.spec = spec


class _StubJobSpec:
    def __init__(self, template):
        self.template = template


class _StubJobTemplate:
    def __init__(self, spec):
        self.spec = spec


class _StubCronSpec:
    def __init__(self, job_template):
        self.job_template = job_template


class _StubCron:
    def __init__(self, spec):
        self.spec = spec


class _StubBatchV1Api:
    last_created_job = None  # captured for assertions

    def __init__(self, cron):
        self._cron = cron

    def read_namespaced_cron_job(self, name, namespace):  # noqa: ARG002
        return self._cron

    def create_namespaced_job(self, namespace, job):  # noqa: ARG002
        type(self).last_created_job = job


class _StubCoreV1Api:
    """Returns no pods → active_run() reports no active run."""

    class _PodList:
        items = []

    def list_namespaced_pod(self, namespace, label_selector=None):  # noqa: ARG002
        return self._PodList()


def _fake_kubernetes_with_cron():
    """Build a fake ``kubernetes`` module exposing the surface runs.py uses."""
    import types

    harness = _StubContainer(
        name="harness",
        env=[_StubV1EnvVar(name="QA_SINK", value="atlas")],
        args=["--all"],
    )
    cron = _StubCron(
        spec=_StubCronSpec(
            job_template=_StubJobTemplate(
                spec=_StubJobSpec(
                    template=_StubPodTemplate(spec=_StubPodSpec(containers=[harness]))
                )
            )
        )
    )
    batch = _StubBatchV1Api(cron)

    fake = types.SimpleNamespace()
    fake.client = types.SimpleNamespace(
        V1EnvVar=_StubV1EnvVar,
        V1Job=lambda metadata, spec: types.SimpleNamespace(metadata=metadata, spec=spec),
        V1ObjectMeta=lambda **kw: types.SimpleNamespace(**kw),
        BatchV1Api=lambda: batch,
        CoreV1Api=_StubCoreV1Api,
        ApiException=Exception,
        Configuration=type(
            "Cfg",
            (),
            {
                "get_default_copy": classmethod(lambda cls: None),
                "set_default": classmethod(lambda cls, _v: None),
            },
        ),
    )
    return fake, batch


def _make_run_control(monkeypatch):
    from qa_review_api.runs import K8sRunControl

    fake, batch = _fake_kubernetes_with_cron()
    rc = K8sRunControl(namespace="slyreply-sandbox", cronjob_name="qa-agents")
    # Bypass config loading.
    rc._configured = True
    monkeypatch.setattr(rc, "_kubernetes", lambda: fake)
    return rc, batch


def test_k8s_trigger_without_concurrency_does_not_inject_env(monkeypatch):
    rc, batch = _make_run_control(monkeypatch)
    rc.trigger(["first-impression-critic"])
    job = type(batch).last_created_job
    harness = next(c for c in job.spec.template.spec.containers if c.name == "harness")
    names = [e.name for e in harness.env]
    assert "QA_HARNESS_CONCURRENCY" not in names
    # The pre-existing env entry is preserved.
    assert "QA_SINK" in names
    # And the persona arg made it through.
    assert harness.args == ["--personas", "first-impression-critic"]


def test_k8s_trigger_with_concurrency_injects_env_on_harness(monkeypatch):
    rc, batch = _make_run_control(monkeypatch)
    rc.trigger([], concurrency=3)
    job = type(batch).last_created_job
    harness = next(c for c in job.spec.template.spec.containers if c.name == "harness")
    overrides = [e for e in harness.env if e.name == "QA_HARNESS_CONCURRENCY"]
    assert len(overrides) == 1
    assert overrides[0].value == "3"
    # No pre-existing entry was lost.
    assert any(e.name == "QA_SINK" for e in harness.env)
    # --all when no personas selected.
    assert harness.args == ["--all"]


def test_k8s_trigger_with_concurrency_overrides_existing_entry(monkeypatch):
    """If the CronJob template itself already sets QA_HARNESS_CONCURRENCY
    (which it does from #824 onward — value "4"), a per-trigger value
    REPLACES the entry rather than duplicating it."""
    import types

    from qa_review_api.runs import K8sRunControl

    harness = _StubContainer(
        name="harness",
        env=[
            _StubV1EnvVar(name="QA_SINK", value="atlas"),
            _StubV1EnvVar(name="QA_HARNESS_CONCURRENCY", value="4"),
        ],
        args=["--all"],
    )
    cron = _StubCron(
        spec=_StubCronSpec(
            job_template=_StubJobTemplate(
                spec=_StubJobSpec(
                    template=_StubPodTemplate(spec=_StubPodSpec(containers=[harness]))
                )
            )
        )
    )
    batch = _StubBatchV1Api(cron)
    fake = types.SimpleNamespace()
    fake.client = types.SimpleNamespace(
        V1EnvVar=_StubV1EnvVar,
        V1Job=lambda metadata, spec: types.SimpleNamespace(metadata=metadata, spec=spec),
        V1ObjectMeta=lambda **kw: types.SimpleNamespace(**kw),
        BatchV1Api=lambda: batch,
        CoreV1Api=_StubCoreV1Api,
        ApiException=Exception,
    )

    rc = K8sRunControl(namespace="slyreply-sandbox", cronjob_name="qa-agents")
    rc._configured = True
    monkeypatch.setattr(rc, "_kubernetes", lambda: fake)

    rc.trigger([], concurrency=2)
    job = type(batch).last_created_job
    harness_out = next(c for c in job.spec.template.spec.containers if c.name == "harness")
    overrides = [e for e in harness_out.env if e.name == "QA_HARNESS_CONCURRENCY"]
    assert len(overrides) == 1, [e.name for e in harness_out.env]
    assert overrides[0].value == "2"


# ---------------------------------------------------------------------------
# K8sRunControl.trigger — same env-injection contract for the model overrides
# (#836). The harness reads QA_EXPLORE_MODEL / QA_REPORT_MODEL (see
# harness/qa_agents/config.py), so those are the names we inject.
# ---------------------------------------------------------------------------
def test_k8s_trigger_without_models_does_not_inject_env(monkeypatch):
    rc, batch = _make_run_control(monkeypatch)
    rc.trigger(["first-impression-critic"])
    job = type(batch).last_created_job
    harness = next(c for c in job.spec.template.spec.containers if c.name == "harness")
    names = [e.name for e in harness.env]
    assert "QA_EXPLORE_MODEL" not in names
    assert "QA_REPORT_MODEL" not in names
    # The pre-existing entry survives.
    assert "QA_SINK" in names


def test_k8s_trigger_with_explore_model_injects_env_on_harness(monkeypatch):
    rc, batch = _make_run_control(monkeypatch)
    rc.trigger([], explore_model="claude-haiku-4-5")
    job = type(batch).last_created_job
    harness = next(c for c in job.spec.template.spec.containers if c.name == "harness")
    overrides = [e for e in harness.env if e.name == "QA_EXPLORE_MODEL"]
    assert len(overrides) == 1
    assert overrides[0].value == "claude-haiku-4-5"
    # Setting only explore_model leaves report alone.
    assert not any(e.name == "QA_REPORT_MODEL" for e in harness.env)
    # And does not disturb the pre-existing env.
    assert any(e.name == "QA_SINK" for e in harness.env)


def test_k8s_trigger_with_report_model_injects_env_on_harness(monkeypatch):
    rc, batch = _make_run_control(monkeypatch)
    rc.trigger([], report_model="claude-sonnet-4-6")
    job = type(batch).last_created_job
    harness = next(c for c in job.spec.template.spec.containers if c.name == "harness")
    overrides = [e for e in harness.env if e.name == "QA_REPORT_MODEL"]
    assert len(overrides) == 1
    assert overrides[0].value == "claude-sonnet-4-6"
    assert not any(e.name == "QA_EXPLORE_MODEL" for e in harness.env)


def test_k8s_trigger_with_both_models_injects_both(monkeypatch):
    rc, batch = _make_run_control(monkeypatch)
    rc.trigger(
        [],
        explore_model="claude-haiku-4-5",
        report_model="claude-opus-4-7",
    )
    job = type(batch).last_created_job
    harness = next(c for c in job.spec.template.spec.containers if c.name == "harness")
    explore = [e for e in harness.env if e.name == "QA_EXPLORE_MODEL"]
    report = [e for e in harness.env if e.name == "QA_REPORT_MODEL"]
    assert len(explore) == 1 and explore[0].value == "claude-haiku-4-5"
    assert len(report) == 1 and report[0].value == "claude-opus-4-7"


def test_k8s_trigger_with_explore_model_overrides_existing_entry(monkeypatch):
    """If the CronJob template itself already sets QA_EXPLORE_MODEL (which it
    does — the value comes from the qa_agent_explore_model TF variable), a
    per-trigger value REPLACES the entry rather than duplicating it. The
    first-match-wins getenv semantics on Linux mean a duplicate would let
    the stale template value silently win."""
    import types

    from qa_review_api.runs import K8sRunControl

    harness = _StubContainer(
        name="harness",
        env=[
            _StubV1EnvVar(name="QA_SINK", value="atlas"),
            _StubV1EnvVar(name="QA_EXPLORE_MODEL", value="claude-sonnet-4-6"),
            _StubV1EnvVar(name="QA_REPORT_MODEL", value="claude-opus-4-7"),
        ],
        args=["--all"],
    )
    cron = _StubCron(
        spec=_StubCronSpec(
            job_template=_StubJobTemplate(
                spec=_StubJobSpec(
                    template=_StubPodTemplate(spec=_StubPodSpec(containers=[harness]))
                )
            )
        )
    )
    batch = _StubBatchV1Api(cron)
    fake = types.SimpleNamespace()
    fake.client = types.SimpleNamespace(
        V1EnvVar=_StubV1EnvVar,
        V1Job=lambda metadata, spec: types.SimpleNamespace(metadata=metadata, spec=spec),
        V1ObjectMeta=lambda **kw: types.SimpleNamespace(**kw),
        BatchV1Api=lambda: batch,
        CoreV1Api=_StubCoreV1Api,
        ApiException=Exception,
    )

    rc = K8sRunControl(namespace="slyreply-sandbox", cronjob_name="qa-agents")
    rc._configured = True
    monkeypatch.setattr(rc, "_kubernetes", lambda: fake)

    rc.trigger(
        [],
        explore_model="claude-haiku-4-5",
        report_model="claude-haiku-4-5",
    )
    job = type(batch).last_created_job
    harness_out = next(c for c in job.spec.template.spec.containers if c.name == "harness")
    explore = [e for e in harness_out.env if e.name == "QA_EXPLORE_MODEL"]
    report = [e for e in harness_out.env if e.name == "QA_REPORT_MODEL"]
    assert len(explore) == 1, [e.name for e in harness_out.env]
    assert explore[0].value == "claude-haiku-4-5"
    assert len(report) == 1
    assert report[0].value == "claude-haiku-4-5"
    # Untouched env entries are still there.
    assert any(e.name == "QA_SINK" for e in harness_out.env)


def test_active_run_logs_streams_sse(store):
    rc = FakeRunControl(log_lines=["==> run started", "[desktop-evaluator t=1] hello"])
    resp = _client(store, run_control=rc).get("/api/runs/active/logs")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert "data: ==> run started" in body
    assert "data: [desktop-evaluator t=1] hello" in body
    # Terminated with an explicit end event.
    assert "event: end" in body


# ---------------------------------------------------------------------------
# Regression for #809 — kubernetes==36 InClusterConfigLoader writes the SA
# token to api_key['authorization'], but auth_settings() only exposes it from
# api_key['BearerToken'], so every request leaves with no Authorization header
# and the API returns 401. _bridge_bearer_token_api_key() mirrors the value.
# ---------------------------------------------------------------------------
class _FakeConfiguration:
    """Mimics the kubernetes==36 quirk: api_key has only 'authorization'."""

    _default = None

    def __init__(self):
        self.api_key: dict[str, str] = {}
        self.refresh_api_key_hook = None

    @classmethod
    def get_default_copy(cls):
        return cls._default

    @classmethod
    def set_default(cls, cfg):
        cls._default = cfg


def test_bridge_bearer_token_api_key_mirrors_token():
    from qa_review_api.runs import _bridge_bearer_token_api_key

    cfg = _FakeConfiguration()
    cfg.api_key["authorization"] = "bearer token-v1"
    _FakeConfiguration._default = cfg

    _bridge_bearer_token_api_key(_FakeConfiguration)

    assert cfg.api_key["BearerToken"] == "bearer token-v1"
    assert _FakeConfiguration._default is cfg


def test_bridge_bearer_token_api_key_propagates_rotated_token():
    from qa_review_api.runs import _bridge_bearer_token_api_key

    cfg = _FakeConfiguration()
    cfg.api_key["authorization"] = "bearer token-v1"

    def _rotate(c):
        c.api_key["authorization"] = "bearer token-v2"

    cfg.refresh_api_key_hook = _rotate
    _FakeConfiguration._default = cfg

    _bridge_bearer_token_api_key(_FakeConfiguration)
    cfg.refresh_api_key_hook(cfg)

    assert cfg.api_key["authorization"] == "bearer token-v2"
    assert cfg.api_key["BearerToken"] == "bearer token-v2"


def test_bridge_bearer_token_api_key_is_noop_when_already_correct():
    from qa_review_api.runs import _bridge_bearer_token_api_key

    cfg = _FakeConfiguration()
    cfg.api_key["BearerToken"] = "bearer fixed-upstream"
    _FakeConfiguration._default = cfg

    _bridge_bearer_token_api_key(_FakeConfiguration)

    assert cfg.api_key == {"BearerToken": "bearer fixed-upstream"}


# ---------------------------------------------------------------------------
# #858 — max_turns + run_notes per-trigger overrides.
# ---------------------------------------------------------------------------
def test_trigger_run_omits_max_turns_and_run_notes_by_default(store):
    """No fields → both arrive as None at the run-control layer, so the
    harness uses its baked-in QA_MAX_TURNS default (200) and writes no
    run_notes to the qa-store run doc."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"]}
    )
    assert resp.status_code == 200
    assert rc.max_turns_list == [None]
    assert rc.run_notes_list == [None]


def test_trigger_run_passes_max_turns_through(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "max_turns": 50},
    )
    assert resp.status_code == 200
    assert rc.max_turns_list == [50]
    # The other knobs still arrive at their defaults when omitted — sanity
    # that the new field hasn't accidentally swallowed an existing one.
    # concurrency defaults to 2 since #1821 (the multi-pod cost-ceiling
    # default), the rest stay None.
    assert rc.concurrencies == [2]
    assert rc.run_notes_list == [None]


def test_trigger_run_passes_run_notes_through(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={
            "personas": ["first-impression-critic"],
            "run_notes": "smoke test before the billing migration #998",
        },
    )
    assert resp.status_code == 200
    assert rc.run_notes_list == ["smoke test before the billing migration #998"]
    assert rc.max_turns_list == [None]


@pytest.mark.parametrize("bound", [10, 200, 1000, 5000])
def test_trigger_run_accepts_max_turns_at_bounds(store, bound):
    """10..5000 are the documented sanity rails (ceiling lifted in
    #1115). Both endpoints inclusive."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"], "max_turns": bound}
    )
    assert resp.status_code == 200, resp.text
    assert rc.max_turns_list == [bound]


@pytest.mark.parametrize("bad", [0, 1, 9, 5001, 10000, -5])
def test_trigger_run_rejects_out_of_range_max_turns(store, bad):
    """A typo'd 50000 must 422 at pydantic, NOT silently launch a
    50000-turn real-money job. #1115 lifted the ceiling 400 → 5000;
    above that is misuse."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"], "max_turns": bad}
    )
    assert resp.status_code == 422
    assert rc.triggered == []


# #1115 — run duration slider bounds (5 min..2 h).
@pytest.mark.parametrize("bound", [300, 1800, 7200])
def test_trigger_run_accepts_run_duration_s_at_bounds(store, bound):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "run_duration_s": bound},
    )
    assert resp.status_code == 200, resp.text
    assert rc.run_duration_s_list == [bound]


@pytest.mark.parametrize("bad", [0, 60, 299, 7201, 86400, -1])
def test_trigger_run_rejects_out_of_range_run_duration_s(store, bad):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "run_duration_s": bad},
    )
    assert resp.status_code == 422
    assert rc.triggered == []


def test_trigger_run_rejects_overlong_run_notes(store):
    """500-char cap. 501 must 422 — defends against an operator pasting a
    whole essay or, accidentally, a sensitive log dump."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "run_notes": "x" * 501},
    )
    assert resp.status_code == 422
    assert rc.triggered == []


def test_trigger_run_passes_all_knobs_together(store):
    """All five overrides at once — proves no interaction bugs between the
    new fields and the existing concurrency/model overrides."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={
            "personas": ["first-impression-critic"],
            "concurrency": 2,
            "explore_model": "claude-haiku-4-5",
            "report_model": "claude-opus-4-7",
            "max_turns": 75,
            "run_notes": "reproducing the #861 regression",
        },
    )
    assert resp.status_code == 200
    assert rc.concurrencies == [2]
    assert rc.explore_models == ["claude-haiku-4-5"]
    assert rc.report_models == ["claude-opus-4-7"]
    assert rc.max_turns_list == [75]
    assert rc.run_notes_list == ["reproducing the #861 regression"]


# ---------------------------------------------------------------------------
# #860 — Transcript tab data + screenshot streaming endpoints.
# ---------------------------------------------------------------------------
def test_get_transcript_returns_empty_when_no_steps(seeded):
    """A run that finished BEFORE #860 wired the recorder has no steps —
    the endpoint must return an empty list, not 404."""
    resp = _client(seeded).get("/api/runs/qa-run-1/personas/first-impression-critic/transcript")
    assert resp.status_code == 200
    assert resp.json() == {"steps": []}


def test_get_transcript_returns_persona_steps_in_order(seeded):
    from qa_store import record_step
    # Insert OUT OF ORDER on purpose — the API must trust the sort, not the
    # insertion order.
    for step_n in [3, 1, 2]:
        record_step(
            seeded, "qa-run-1", "first-impression-critic", step_n,
            tool_name=f"tool_{step_n}",
            args_summary=f"args_{step_n}",
        )
    resp = _client(seeded).get("/api/runs/qa-run-1/personas/first-impression-critic/transcript")
    assert resp.status_code == 200
    steps = resp.json()["steps"]
    assert [s["step_n"] for s in steps] == [1, 2, 3]
    assert [s["tool_name"] for s in steps] == ["tool_1", "tool_2", "tool_3"]


def test_get_transcript_isolates_personas(seeded):
    """A second persona's steps must not bleed into the first's transcript."""
    from qa_store import record_step
    record_step(seeded, "qa-run-1", "first-impression-critic", 1, tool_name="m1")
    record_step(seeded, "qa-run-1", "desktop-evaluator", 1, tool_name="d1")
    record_step(seeded, "qa-run-1", "desktop-evaluator", 2, tool_name="d2")
    margaret_resp = _client(seeded).get(
        "/api/runs/qa-run-1/personas/first-impression-critic/transcript"
    )
    daniel_resp = _client(seeded).get(
        "/api/runs/qa-run-1/personas/desktop-evaluator/transcript"
    )
    assert [s["tool_name"] for s in margaret_resp.json()["steps"]] == ["m1"]
    assert [s["tool_name"] for s in daniel_resp.json()["steps"]] == ["d1", "d2"]


def test_get_transcript_stringifies_screenshot_oid(seeded):
    """ObjectId is not JSON-serialisable by FastAPI's default encoder —
    the endpoint must coerce to string so the client can use it as a URL
    fragment in the /screenshots/{oid} call."""
    from bson import ObjectId
    from qa_store import record_step
    oid = ObjectId()
    record_step(
        seeded, "qa-run-1", "first-impression-critic", 1,
        tool_name="mcp__playwright__browser_take_screenshot",
        screenshot_id=oid,
    )
    resp = _client(seeded).get("/api/runs/qa-run-1/personas/first-impression-critic/transcript")
    assert resp.status_code == 200
    step = resp.json()["steps"][0]
    assert step["screenshot_id"] == str(oid)


def test_get_screenshot_streams_png_bytes(seeded, monkeypatch):
    """Endpoint fetches via qa_store.fetch_screenshot and streams the bytes
    with image/png. We patch fetch_screenshot rather than going through
    GridFS so the test doesn't depend on a real Mongo."""
    monkeypatch.setattr(
        "qa_review_api.app.fetch_screenshot",
        lambda _store, _oid: b"FAKE_PNG_BYTES",
    )
    resp = _client(seeded).get(
        "/api/runs/qa-run-1/screenshots/507f1f77bcf86cd799439011"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == b"FAKE_PNG_BYTES"
    # Aggressive cache — bytes are immutable per the oid.
    assert "immutable" in resp.headers.get("cache-control", "")


def test_get_screenshot_404s_on_missing_oid(seeded, monkeypatch):
    """gridfs.NoFile → 404, NOT a 500. The Transcript tab might link a
    screenshot whose blob was later cleaned up — graceful failure on the
    client beats a crashed endpoint."""
    from gridfs.errors import NoFile

    def _raise_nofile(_store, _oid):
        raise NoFile("missing")

    monkeypatch.setattr("qa_review_api.app.fetch_screenshot", _raise_nofile)
    resp = _client(seeded).get(
        "/api/runs/qa-run-1/screenshots/507f1f77bcf86cd799439011"
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# #861 — GET /api/runs/coverage-catalog (Slice 4 trigger-page checklist data).
# ---------------------------------------------------------------------------
def test_get_coverage_catalog_returns_categories_and_actions(store):
    resp = _client(store, run_control=FakeRunControl()).get(
        "/api/runs/coverage-catalog"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "categories" in body
    assert "actions" in body
    # 8 categories per qa_store.CATEGORIES (auth, agents, playground,
    # billing, account, contact, docs, admin).
    assert len(body["categories"]) == 8
    assert "auth" in body["categories"]
    # At least 40 actions per the catalog's documented size bounds.
    assert len(body["actions"]) >= 40
    # Every action carries the documented shape.
    sample = body["actions"][0]
    for required in (
        "id", "category", "human_description",
        "persona_compat", "requires_auth", "expected_outcome",
    ):
        assert required in sample, f"action missing {required}"
    # persona_compat is a JSON-friendly list, not a tuple.
    assert isinstance(sample["persona_compat"], list)


def test_get_coverage_catalog_actions_are_sorted_by_category(store):
    """Source order in CATEGORIES groups entries — the trigger UI relies on
    iterating in source order to render categories together without extra
    sorting. Verify the API preserves that order."""
    resp = _client(store, run_control=FakeRunControl()).get(
        "/api/runs/coverage-catalog"
    )
    categories = resp.json()["categories"]
    actions = resp.json()["actions"]
    # Find the index of each action's category in the categories list,
    # and verify the sequence is non-decreasing (entries grouped by cat).
    cat_index = {c: i for i, c in enumerate(categories)}
    indices = [cat_index.get(a["category"], -1) for a in actions]
    assert indices == sorted(indices), (
        "actions are not grouped by category in source order"
    )


# ---------------------------------------------------------------------------
# #861 — mandatory_action_ids on POST /api/runs/trigger.
# ---------------------------------------------------------------------------
def test_trigger_run_omits_mandatory_action_ids_by_default(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger", json={"personas": ["first-impression-critic"]}
    )
    assert resp.status_code == 200
    # No ids supplied → empty list at the run-control layer → no env emitted.
    assert rc.mandatory_action_ids_list == [[]]


def test_trigger_run_passes_mandatory_action_ids_through(store):
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={
            "personas": ["first-impression-critic"],
            "mandatory_action_ids": [
                "auth.register_new_account",
                "auth.complete_email_verification",
            ],
        },
    )
    assert resp.status_code == 200
    assert rc.mandatory_action_ids_list == [[
        "auth.register_new_account",
        "auth.complete_email_verification",
    ]]


def test_trigger_run_rejects_unknown_mandatory_action_id(store):
    """A typo'd id must 422 at the API BEFORE the K8s Job is created — the
    harness would tolerate it with a warn-and-drop, but failing fast at
    the trigger lets the operator fix the typo immediately."""
    rc = FakeRunControl()
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={
            "personas": ["first-impression-critic"],
            "mandatory_action_ids": [
                "auth.register_new_account",
                "made.up_action_id",  # typo
            ],
        },
    )
    assert resp.status_code == 422
    assert "made.up_action_id" in resp.json()["detail"]
    assert "coverage-catalog" in resp.json()["detail"]
    # Nothing was triggered.
    assert rc.triggered == []


def test_trigger_run_rejects_too_many_mandatory_ids(store):
    """Pydantic max_length=50 catches operator misuse (a mandatory list
    longer than the catalog itself is nonsense)."""
    rc = FakeRunControl()
    too_many = [f"auth.action_{i}" for i in range(51)]
    resp = _client(store, run_control=rc).post(
        "/api/runs/trigger",
        json={"personas": ["first-impression-critic"], "mandatory_action_ids": too_many},
    )
    assert resp.status_code == 422
    assert rc.triggered == []


# ---------------------------------------------------------------------------
# #862 — saved scenarios (Slice 5).
# ---------------------------------------------------------------------------
def _scenario_payload(**overrides):
    """Default body for POST /api/scenarios; tests override fields."""
    base = {
        "id": "smoke-billing",
        "name": "Smoke billing",
        "description": "Quick check on the upgrade-to-Pro flow.",
        "persona_id": "desktop-evaluator",
        "mandatory_action_ids": [
            "billing.view_pricing_page",
            "billing.upgrade_to_pro",
        ],
    }
    base.update(overrides)
    return base


# --- list ---
def test_list_scenarios_empty_when_none(store):
    resp = _client(store).get("/api/scenarios")
    assert resp.status_code == 200
    assert resp.json() == {"scenarios": []}


def test_list_scenarios_returns_created(store):
    c = _client(store)
    c.post("/api/scenarios", json=_scenario_payload())
    resp = c.get("/api/scenarios")
    assert resp.status_code == 200
    docs = resp.json()["scenarios"]
    assert len(docs) == 1
    assert docs[0]["id"] == "smoke-billing"


# --- create ---
def test_create_scenario_201_with_full_doc(store):
    resp = _client(store).post("/api/scenarios", json=_scenario_payload())
    assert resp.status_code == 201
    doc = resp.json()
    assert doc["id"] == "smoke-billing"
    assert doc["persona_id"] == "desktop-evaluator"
    assert "billing.upgrade_to_pro" in doc["mandatory_action_ids"]
    assert "created_at" in doc and "updated_at" in doc


def test_create_scenario_409_on_duplicate_id(store):
    c = _client(store)
    c.post("/api/scenarios", json=_scenario_payload())
    resp = c.post("/api/scenarios", json=_scenario_payload())
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


def test_create_scenario_422_on_unknown_persona_id(store):
    resp = _client(store).post(
        "/api/scenarios",
        json=_scenario_payload(persona_id="ghost-persona"),
    )
    assert resp.status_code == 422
    assert "ghost-persona" in resp.json()["detail"]


def test_create_scenario_422_on_unknown_action_id(store):
    resp = _client(store).post(
        "/api/scenarios",
        json=_scenario_payload(
            mandatory_action_ids=["billing.view_pricing_page", "made.up_id"],
        ),
    )
    assert resp.status_code == 422
    assert "made.up_id" in resp.json()["detail"]
    assert "coverage-catalog" in resp.json()["detail"]


@pytest.mark.parametrize("bad_id", [
    "UPPERCASE",      # uppercase rejected
    "with_under",     # underscore rejected — slug uses hyphens
    "with space",     # whitespace rejected
    "1starts-digit",  # must start with a letter
    "",               # min_length=1
])
def test_create_scenario_422_on_bad_slug(store, bad_id):
    resp = _client(store).post(
        "/api/scenarios", json=_scenario_payload(id=bad_id),
    )
    assert resp.status_code == 422


# --- get ---
def test_get_scenario_404_when_missing(store):
    resp = _client(store).get("/api/scenarios/no-such")
    assert resp.status_code == 404


def test_get_scenario_returns_existing(store):
    c = _client(store)
    c.post("/api/scenarios", json=_scenario_payload())
    resp = c.get("/api/scenarios/smoke-billing")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Smoke billing"


# --- update (PATCH) ---
def test_patch_scenario_updates_subset(store):
    c = _client(store)
    c.post("/api/scenarios", json=_scenario_payload())
    resp = c.patch(
        "/api/scenarios/smoke-billing",
        json={"name": "Renamed", "description": "new desc"},
    )
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["name"] == "Renamed"
    assert doc["description"] == "new desc"
    # Untouched.
    assert doc["persona_id"] == "desktop-evaluator"


def test_patch_scenario_clears_mandatory_actions_via_empty_list(store):
    c = _client(store)
    c.post("/api/scenarios", json=_scenario_payload())
    resp = c.patch(
        "/api/scenarios/smoke-billing",
        json={"mandatory_action_ids": []},
    )
    assert resp.status_code == 200
    assert resp.json()["mandatory_action_ids"] == []


def test_patch_scenario_404_when_missing(store):
    resp = _client(store).patch(
        "/api/scenarios/no-such", json={"name": "X"},
    )
    assert resp.status_code == 404


def test_patch_scenario_422_on_unknown_persona_id(store):
    c = _client(store)
    c.post("/api/scenarios", json=_scenario_payload())
    resp = c.patch(
        "/api/scenarios/smoke-billing",
        json={"persona_id": "ghost"},
    )
    assert resp.status_code == 422


def test_patch_scenario_422_on_unknown_action_id(store):
    c = _client(store)
    c.post("/api/scenarios", json=_scenario_payload())
    resp = c.patch(
        "/api/scenarios/smoke-billing",
        json={"mandatory_action_ids": ["made.up"]},
    )
    assert resp.status_code == 422


# --- delete ---
def test_delete_scenario_204_on_success(store):
    c = _client(store)
    c.post("/api/scenarios", json=_scenario_payload())
    resp = c.delete("/api/scenarios/smoke-billing")
    assert resp.status_code == 204
    # Gone.
    assert c.get("/api/scenarios/smoke-billing").status_code == 404


def test_delete_scenario_404_when_missing(store):
    resp = _client(store).delete("/api/scenarios/no-such")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# #902 / #904 — Transcript Search slice. Two endpoints:
#   * GET /api/runs/{run_id}/personas/{persona_id}/logs — per-persona replay
#     from qa_run_logs (added in #903)
#   * GET /api/transcripts/search — cross-run regex search
# ---------------------------------------------------------------------------
def _seed_log(store, *, run_id="r1", persona_id="first-impression-critic", seq=1,
              kind="text", content="hello", **extra):
    from qa_store import append_run_log
    append_run_log(
        store, run_id=run_id, persona_id=persona_id, seq=seq,
        kind=kind, content=content, **extra,
    )


class TestGetPersonaLogs:
    def test_empty_when_no_logs(self, store):
        resp = _client(store).get("/api/runs/qa-run-x/personas/first-impression-critic/logs")
        assert resp.status_code == 200
        assert resp.json() == {"logs": []}

    def test_orders_by_seq_ascending(self, store):
        for seq in (3, 1, 2):
            _seed_log(store, seq=seq, content=f"line {seq}")
        resp = _client(store).get("/api/runs/r1/personas/first-impression-critic/logs")
        assert resp.status_code == 200
        logs = resp.json()["logs"]
        assert [r["seq"] for r in logs] == [1, 2, 3]
        assert [r["content"] for r in logs] == ["line 1", "line 2", "line 3"]

    def test_isolates_personas(self, store):
        _seed_log(store, persona_id="first-impression-critic", seq=1, content="m")
        _seed_log(store, persona_id="desktop-evaluator", seq=1, content="d")
        m = _client(store).get("/api/runs/r1/personas/first-impression-critic/logs").json()
        d = _client(store).get("/api/runs/r1/personas/desktop-evaluator/logs").json()
        assert [r["content"] for r in m["logs"]] == ["m"]
        assert [r["content"] for r in d["logs"]] == ["d"]


class TestSearchTranscripts:
    def test_returns_results_and_count(self, store):
        _seed_log(store, content="hello world")
        _seed_log(store, seq=2, content="totally different")
        resp = _client(store).get("/api/transcripts/search?q=hello")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["results"][0]["content"] == "hello world"
        # The query echo is useful for the UI's "you searched: ..." caption.
        assert body["query"]["q"] == "hello"

    def test_case_insensitive(self, store):
        _seed_log(store, content="ERROR connecting to upstream")
        resp = _client(store).get("/api/transcripts/search?q=error")
        assert resp.json()["count"] == 1

    def test_regex_metas_escaped(self, store):
        """A literal dot must not become wildcard — `foo.bar` matches
        only `foo.bar`, not `fooxbar`."""
        _seed_log(store, content="touched foo.bar today")
        _seed_log(store, seq=2, content="this is fooXbar instead")
        resp = _client(store).get("/api/transcripts/search?q=foo.bar")
        body = resp.json()
        assert body["count"] == 1
        assert "foo.bar" in body["results"][0]["content"]

    def test_persona_filter(self, store):
        _seed_log(store, persona_id="first-impression-critic", content="hit")
        _seed_log(store, persona_id="desktop-evaluator", seq=2, content="hit")
        resp = _client(store).get(
            "/api/transcripts/search?q=hit&persona=first-impression-critic"
        )
        body = resp.json()
        assert body["count"] == 1
        assert body["results"][0]["persona_id"] == "first-impression-critic"

    def test_kind_filter(self, store):
        _seed_log(store, kind="text", content="rate-limit reached")
        _seed_log(
            store, kind="result", seq=2, content="rate-limit observed in result",
        )
        resp = _client(store).get(
            "/api/transcripts/search?q=rate-limit&kind=text"
        )
        body = resp.json()
        assert body["count"] == 1
        assert body["results"][0]["kind"] == "text"

    def test_persona_unknown_returns_422(self, store):
        # Same validation shape as the trigger endpoint — typo'd
        # persona id is a 422, not a silent zero-match.
        resp = _client(store).get(
            "/api/transcripts/search?q=anything&persona=ghost"
        )
        assert resp.status_code == 422

    def test_empty_q_returns_filtered_recent(self, store):
        """A blank q with persona/kind filters is a valid query — the UI
        uses it to list recent emits for one persona without text gate."""
        _seed_log(store, persona_id="first-impression-critic", content="a")
        _seed_log(store, persona_id="first-impression-critic", seq=2, content="b")
        _seed_log(store, persona_id="desktop-evaluator", seq=3, content="c")
        resp = _client(store).get("/api/transcripts/search?persona=first-impression-critic")
        body = resp.json()
        assert body["count"] == 2
        assert all(r["persona_id"] == "first-impression-critic" for r in body["results"])

    def test_limit_clamp(self, store):
        for i in range(30):
            _seed_log(store, seq=i + 1, content=f"hit {i}")
        # limit=5 → exactly 5 returned
        resp = _client(store).get("/api/transcripts/search?q=hit&limit=5")
        assert resp.json()["count"] == 5

    def test_results_newest_first(self, store):
        from datetime import UTC, datetime, timedelta
        now = datetime.now(UTC)
        for i, offset in enumerate([5, 0, 10]):
            _seed_log(
                store, seq=i + 1, content=f"hit-{i}",
                ts=now - timedelta(minutes=offset),
            )
        resp = _client(store).get("/api/transcripts/search?q=hit")
        results = resp.json()["results"]
        # offsets 0, 5, 10 → newest-first means content order hit-1, hit-0, hit-2
        assert [r["content"] for r in results] == ["hit-1", "hit-0", "hit-2"]

# ===========================================================================
# /api/personas — Test Ease persona library (#988 + #1000)
# ===========================================================================
class TestPersonasAPI:
    """Route-level tests for the persona library introduced in #988 and
    expanded in #1000. Covers create / list / get / update / delete plus the
    edge cases that are easy to regress (409, 422, 404, exclude_unset)."""

    def _doc(self, **overrides) -> dict:
        base = {
            "persona_id": "alice",
            "display_name": "Alice Tester",
            "registered_email": "alice@example.com",
            "explore_system_prompt": "explore",
            "report_system_prompt": "report",
            "flows": ["signup"],
            "uses_admin_login": False,
            "setup_actions": None,
            "browser_locale": None,
            "color_token": "teal",
            "avatar_seed": "alice",
        }
        base.update(overrides)
        return base

    def test_list_empty(self, store):
        resp = _client(store).get("/api/personas")
        assert resp.status_code == 200
        assert resp.json() == {"personas": []}

    def test_create_201(self, store):
        resp = _client(store).post("/api/personas", json=self._doc())
        assert resp.status_code == 201
        body = resp.json()
        assert body["persona_id"] == "alice"
        # is_default is forced to False at the route level
        assert body["is_default"] is False
        assert body["hidden"] is False

    def test_create_duplicate_409(self, store):
        c = _client(store)
        c.post("/api/personas", json=self._doc())
        resp = c.post("/api/personas", json=self._doc())
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    def test_create_invalid_persona_id_422(self, store):
        """Pydantic pattern rejects upper-case + leading digits."""
        resp = _client(store).post("/api/personas", json=self._doc(persona_id="Alice"))
        assert resp.status_code == 422

    def test_create_defaults_avatar_seed_to_persona_id(self, store):
        """avatar_seed=null means 'use persona_id' — exercise the path."""
        doc = self._doc(avatar_seed=None)
        resp = _client(store).post("/api/personas", json=doc)
        assert resp.status_code == 201
        assert resp.json()["avatar_seed"] == "alice"

    def test_get_404(self, store):
        resp = _client(store).get("/api/personas/ghost")
        assert resp.status_code == 404

    def test_get_returns_doc(self, store):
        c = _client(store)
        c.post("/api/personas", json=self._doc())
        resp = c.get("/api/personas/alice")
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Alice Tester"

    def test_patch_partial(self, store):
        c = _client(store)
        c.post("/api/personas", json=self._doc())
        resp = c.patch("/api/personas/alice", json={"display_name": "Alice Edited"})
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Alice Edited"
        # Other fields preserved
        assert resp.json()["registered_email"] == "alice@example.com"

    def test_patch_empty_body_422(self, store):
        c = _client(store)
        c.post("/api/personas", json=self._doc())
        resp = c.patch("/api/personas/alice", json={})
        assert resp.status_code == 422

    def test_patch_unknown_id_404(self, store):
        resp = _client(store).patch("/api/personas/ghost", json={"display_name": "X"})
        assert resp.status_code == 404

    def test_patch_explicit_null_clears_field(self, store):
        """exclude_unset semantics — null distinguishes from omitted."""
        c = _client(store)
        c.post("/api/personas", json=self._doc(browser_locale="en-GB"))
        resp = c.patch("/api/personas/alice", json={"browser_locale": None})
        assert resp.status_code == 200
        assert resp.json()["browser_locale"] is None

    def test_delete_non_default_204(self, store):
        c = _client(store)
        c.post("/api/personas", json=self._doc())
        resp = c.delete("/api/personas/alice")
        assert resp.status_code == 204
        # Re-fetch is 404
        assert c.get("/api/personas/alice").status_code == 404

    def test_delete_unknown_404(self, store):
        resp = _client(store).delete("/api/personas/ghost")
        assert resp.status_code == 404

    def test_delete_default_422(self, store):
        """Default personas must be hidden, not hard-deleted."""
        # Seed a default row directly via the store so the route's
        # is_default=False forcing on create doesn't get in the way.
        from qa_store.schema import create_persona as _create
        doc = self._doc(persona_id="def-alice")
        doc.update({"is_default": True, "hidden": False})
        _create(store, doc)

        resp = _client(store).delete("/api/personas/def-alice")
        assert resp.status_code == 422
        assert "default" in resp.json()["detail"]

    def test_list_default_first_then_alpha(self, store):
        """Default rows sort to the top; alphabetical within each group."""
        from qa_store.schema import create_persona as _create
        for pid, name, is_def in [
            ("zoe", "Zoe", False),
            ("bob", "Bob", True),
            ("amy", "Amy", True),
        ]:
            d = self._doc(persona_id=pid, display_name=name)
            d.update({"is_default": is_def, "hidden": False})
            _create(store, d)

        resp = _client(store).get("/api/personas")
        names = [p["display_name"] for p in resp.json()["personas"]]
        assert names == ["Amy", "Bob", "Zoe"]

    def test_list_hidden_excluded_by_default(self, store):
        from qa_store.schema import create_persona as _create
        for pid, hidden in [("alice", False), ("bob", True)]:
            d = self._doc(persona_id=pid)
            d.update({"is_default": True, "hidden": hidden})
            _create(store, d)

        ids = {p["persona_id"] for p in _client(store).get("/api/personas").json()["personas"]}
        assert ids == {"alice"}

    def test_list_include_hidden(self, store):
        from qa_store.schema import create_persona as _create
        for pid, hidden in [("alice", False), ("bob", True)]:
            d = self._doc(persona_id=pid)
            d.update({"is_default": True, "hidden": hidden})
            _create(store, d)

        resp = _client(store).get("/api/personas?include_hidden=true")
        ids = {p["persona_id"] for p in resp.json()["personas"]}
        assert ids == {"alice", "bob"}


# ===========================================================================
# /api/runs/{run_id}/timeline — merged step + log feed (#988)
# ===========================================================================
class TestRunTimeline:
    """The Timeline endpoint that powers the new RunDetail Timeline tab."""

    def test_unknown_run_404(self, store):
        resp = _client(store).get("/api/runs/ghost/timeline")
        assert resp.status_code == 404

    def test_empty_run_returns_empty_events(self, seeded):
        """qa-run-1 from the fixture has no qa_run_steps or qa_run_logs rows
        — timeline is empty, not an error."""
        resp = _client(seeded).get("/api/runs/qa-run-1/timeline")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == "qa-run-1"
        assert body["events"] == []

    def test_merges_steps_and_logs_sorted_by_ts(self, seeded):
        """Two steps + two logs across two timestamps, the merged feed must be
        in ts-ascending order with the 'kind' discriminator on each row."""
        from datetime import UTC, datetime, timedelta

        from qa_store.schema import append_run_log, record_step

        t0 = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
        # Order written ≠ order returned — timeline sorts by ts.
        record_step(seeded, run_id="qa-run-1", persona_id="first-impression-critic", step_n=1,
                    tool_name="navigate", ts=t0)
        append_run_log(seeded, run_id="qa-run-1", persona_id="first-impression-critic",
                       seq=1, kind="explore", content="thinking",
                       ts=t0 + timedelta(seconds=1))
        record_step(seeded, run_id="qa-run-1", persona_id="first-impression-critic", step_n=2,
                    tool_name="click", ts=t0 + timedelta(seconds=2))
        append_run_log(seeded, run_id="qa-run-1", persona_id="first-impression-critic",
                       seq=2, kind="explore", content="more",
                       ts=t0 + timedelta(seconds=3))

        resp = _client(seeded).get("/api/runs/qa-run-1/timeline")
        events = resp.json()["events"]
        assert len(events) == 4
        # ts strictly ascending
        timestamps = [e["ts"] for e in events]
        assert timestamps == sorted(timestamps)
        # Kinds interleave in time order
        kinds = [e["kind"] for e in events]
        assert kinds == ["step", "log", "step", "log"]

    def test_step_screenshot_id_serialised_as_string(self, seeded):
        """ObjectId-typed screenshot_id must become a str for JSON safety."""
        from datetime import UTC, datetime

        from bson import ObjectId
        from qa_store.schema import record_step

        oid = ObjectId()
        record_step(seeded, run_id="qa-run-1", persona_id="first-impression-critic", step_n=1,
                    tool_name="screenshot", ts=datetime.now(UTC),
                    screenshot_id=oid)

        resp = _client(seeded).get("/api/runs/qa-run-1/timeline")
        step = next(e for e in resp.json()["events"] if e["kind"] == "step")
        assert isinstance(step["screenshot_id"], str)
        assert step["screenshot_id"] == str(oid)


# ===========================================================================
# /api/discovered-* — Slice 1 of #1002
# ===========================================================================
class TestDiscoveredAPI:
    """Route-level tests for the three new GETs that read the
    qa_discovered_* collections written by the harness's distillation
    hook. Each helper seeds the store directly (no model calls in the
    test path) and then drives the route via TestClient."""

    def _seed_action(self, store, **overrides):
        from qa_store.schema import upsert_discovered_action
        defaults = dict(
            run_id="run-1", persona_id="first-impression-critic",
            action_id="auth.signup", category="auth",
            human_description="Sign up", url_seen="/signup",
            evidence="evidence", branches_noticed=[],
        )
        defaults.update(overrides)
        upsert_discovered_action(store, **defaults)

    def _seed_tool(self, store, **overrides):
        from qa_store.schema import upsert_discovered_tool
        defaults = dict(
            run_id="run-1", persona_id="first-impression-critic",
            name="mailpit", purpose="verify",
        )
        defaults.update(overrides)
        upsert_discovered_tool(store, **defaults)

    def _seed_branch(self, store, **overrides):
        from qa_store.schema import upsert_discovered_branch
        defaults = dict(
            run_id="run-1", persona_id="first-impression-critic",
            ordinal=1, description="unexplored thing",
        )
        defaults.update(overrides)
        upsert_discovered_branch(store, **defaults)

    # ── /api/discovered-actions ────────────────────────────────────────
    def test_actions_empty_returns_empty_list(self, store):
        resp = _client(store).get("/api/discovered-actions")
        assert resp.status_code == 200
        assert resp.json() == {"actions": [], "count": 0}

    def test_actions_returns_seeded_rows(self, store):
        self._seed_action(store)
        resp = _client(store).get("/api/discovered-actions")
        body = resp.json()
        assert body["count"] == 1
        assert body["actions"][0]["action_id"] == "auth.signup"

    def test_actions_filters_by_run(self, store):
        self._seed_action(store, run_id="run-1", action_id="auth.signup")
        self._seed_action(store, run_id="run-2", action_id="auth.login")
        resp = _client(store).get("/api/discovered-actions?run_id=run-2")
        assert resp.json()["count"] == 1
        assert resp.json()["actions"][0]["action_id"] == "auth.login"

    def test_actions_filters_by_persona(self, store):
        self._seed_action(store, persona_id="first-impression-critic", action_id="auth.signup")
        self._seed_action(store, persona_id="desktop-evaluator", action_id="auth.login")
        resp = _client(store).get("/api/discovered-actions?persona_id=first-impression-critic")
        assert resp.json()["count"] == 1

    def test_actions_filters_by_category(self, store):
        self._seed_action(store, category="auth", action_id="auth.x")
        self._seed_action(store, category="billing", action_id="billing.x")
        resp = _client(store).get("/api/discovered-actions?category=billing")
        assert resp.json()["count"] == 1
        assert resp.json()["actions"][0]["category"] == "billing"

    def test_actions_unknown_category_422(self, store):
        """Pre-validate the filter against the static category list so
        a typo doesn't silently return nothing."""
        resp = _client(store).get("/api/discovered-actions?category=bogus")
        assert resp.status_code == 422
        assert "unknown category" in resp.json()["detail"]

    def test_actions_unknown_run_id_returns_empty_not_404(self, store):
        """Mirrors how /api/transcripts/search handles unknown filters
        — an empty match isn't an error."""
        self._seed_action(store)
        resp = _client(store).get("/api/discovered-actions?run_id=nope")
        assert resp.status_code == 200
        assert resp.json() == {"actions": [], "count": 0}

    # ── /api/discovered-tools ──────────────────────────────────────────
    def test_tools_empty(self, store):
        resp = _client(store).get("/api/discovered-tools")
        assert resp.status_code == 200
        assert resp.json() == {"tools": [], "count": 0}

    def test_tools_returns_seeded(self, store):
        self._seed_tool(store)
        resp = _client(store).get("/api/discovered-tools")
        assert resp.json()["count"] == 1
        assert resp.json()["tools"][0]["name"] == "mailpit"

    def test_tools_filters_by_run(self, store):
        self._seed_tool(store, run_id="run-1", name="mailpit")
        self._seed_tool(store, run_id="run-2", name="revolut-test")
        resp = _client(store).get("/api/discovered-tools?run_id=run-2")
        assert resp.json()["count"] == 1

    # ── /api/discovered-branches ───────────────────────────────────────
    def test_branches_empty(self, store):
        resp = _client(store).get("/api/discovered-branches")
        assert resp.status_code == 200
        assert resp.json() == {"branches": [], "count": 0}

    def test_branches_returns_seeded(self, store):
        self._seed_branch(store)
        resp = _client(store).get("/api/discovered-branches")
        assert resp.json()["count"] == 1
        assert resp.json()["branches"][0]["description"] == "unexplored thing"

    def test_branches_round_trip_via_route(self, store):
        """Distinct ordinals + descriptions round-trip through the route.
        Set comparison — the cross-row sort depends on microsecond-level
        timestamps that drift between inserts; the within-distillation
        ordering contract is tested at the schema layer where the test
        can control distilled_at to collide deterministically."""
        for i, d in enumerate(["first", "second", "third"], start=1):
            self._seed_branch(store, ordinal=i, description=d)
        resp = _client(store).get("/api/discovered-branches?run_id=run-1")
        branches = resp.json()["branches"]
        assert len(branches) == 3
        assert {b["ordinal"] for b in branches} == {1, 2, 3}
        assert {b["description"] for b in branches} == {"first", "second", "third"}


# ===========================================================================
# #1105 Slice 1 — persona credentials API
# ===========================================================================
class TestPersonaCredentialsAPI:
    """Route-level coverage for the credentials endpoints. The qa-store
    layer already pins the contract that the password never appears in
    the status response; these tests confirm the API surface mirrors
    that and that the operator-facing reset path is wired correctly."""

    def _seed(self, store, persona_id="maya"):
        from qa_store import create_persona
        create_persona(store, {
            "persona_id": persona_id,
            "display_name": "Maya",
            "registered_email": f"{persona_id}@x.com",
            "explore_system_prompt": "x",
            "report_system_prompt": "x",
            "flows": [],
            "uses_admin_login": False,
            "setup_actions": None,
            "browser_locale": None,
            "color_token": "teal",
            "avatar_seed": persona_id,
            "is_default": True,
            "hidden": False,
            "is_active": True,
        })

    def test_status_no_credentials_returns_has_credentials_false(self, store):
        self._seed(store)
        resp = _client(store).get("/api/personas/maya/credentials/status")
        assert resp.status_code == 200
        assert resp.json() == {"has_credentials": False}

    def test_status_with_credentials_returns_metadata_no_password(self, store):
        from qa_store import set_persona_credentials
        self._seed(store)
        set_persona_credentials(
            store, "maya",
            email="maya+r1@x.com",
            password_plain="secretXYZ",
            verified=True,
        )
        resp = _client(store).get("/api/personas/maya/credentials/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_credentials"] is True
        assert body["email"] == "maya+r1@x.com"
        assert body["verified"] is True
        assert body["last_rotation_n"] == 0
        # Security property — the password value MUST NOT appear in
        # the serialised payload under any key shape.
        assert "secretXYZ" not in resp.text
        assert "password" not in body

    def test_status_unknown_persona_404s(self, store):
        resp = _client(store).get("/api/personas/ghost/credentials/status")
        assert resp.status_code == 404

    def test_delete_clears_credentials_returns_204(self, store):
        from qa_store import get_persona, set_persona_credentials
        self._seed(store)
        set_persona_credentials(
            store, "maya", email="m@x.com", password_plain="pw",
        )
        resp = _client(store).delete("/api/personas/maya/credentials")
        assert resp.status_code == 204
        # Persona doc round-trip confirms the sub-doc is gone.
        assert "credentials" not in get_persona(store, "maya")

    def test_delete_idempotent_on_empty_credentials(self, store):
        """Clearing an already-empty credentials sub-doc returns 204,
        not an error — the operator may double-click Reset, the SPA
        may retry on transient network, and the SIGNUP_FRESH directive
        always clears first regardless of prior state."""
        self._seed(store)
        resp = _client(store).delete("/api/personas/maya/credentials")
        assert resp.status_code == 204

    def test_delete_unknown_persona_404s(self, store):
        resp = _client(store).delete("/api/personas/ghost/credentials")
        assert resp.status_code == 404


# -- #1146 — admin / nuclear-button -------------------------------------
class TestAdminWipe:
    """POST /api/admin/wipe drops every per-run + per-persona collection
    after a literal-string 'WIPE' confirmation. The audit row endpoint
    round-trips what the UI needs to render the page.
    """

    def _seed_run_data(self, store):
        """Seed enough state across collections so the dropped counts
        in the response are non-zero — proves the wipe actually
        touched something."""
        from qa_store import (
            add_persona_result,
            create_persona,
            create_run,
        )
        create_persona(store, {
            "persona_id": "maya", "display_name": "Maya",
            "is_active": True, "is_default": False, "hidden": False,
        })
        create_run(store, "r-1", ["maya"])
        add_persona_result(
            store, "r-1", "maya",
            review_markdown="...", verdict="explored",
            accounting={"cost_usd": 0.0},
            findings=[{
                "title": "Bug A", "category": "bug",
                "severity": "major", "body": "...",
            }],
        )

    def test_missing_confirm_422s(self, store):
        resp = _client(store).post(
            "/api/admin/wipe", json={"requester_note": "no confirm"},
        )
        assert resp.status_code == 422

    def test_wrong_confirm_422s(self, store):
        resp = _client(store).post(
            "/api/admin/wipe", json={"confirm": "wipe"},
        )
        assert resp.status_code == 422

    def test_correct_confirm_drops_collections(self, store):
        self._seed_run_data(store)
        resp = _client(store).post(
            "/api/admin/wipe",
            json={"confirm": "WIPE", "requester_note": "test"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dropped"]["qa_runs"] >= 1
        assert body["dropped"]["qa_findings"] >= 1
        assert body["audit"]["dropped_total"] >= 2
        # qa_admin_audit is NOT in the drop list — survives the wipe.
        assert "qa_admin_audit" not in body["dropped"]

    def test_audit_includes_requester_note(self, store):
        self._seed_run_data(store)
        resp = _client(store).post(
            "/api/admin/wipe",
            json={"confirm": "WIPE", "requester_note": "Validating #1146"},
        )
        assert resp.status_code == 200
        assert resp.json()["audit"]["requester_note"] == "Validating #1146"

    def test_wipe_attempts_reseed(self, store):
        """After a wipe the endpoint attempts seed_default_personas.

        The seed is best-effort: if qa_agents isn't on the path (the
        review-ui-api test venv doesn't install it), the seed silently
        skips and the next pod restart re-seeds via the startup hook.
        Production has qa-agents installed so the in-band seed
        succeeds; this test only verifies the wipe + audit path
        completed without crashing when seeding can't happen."""
        self._seed_run_data(store)
        resp = _client(store).post(
            "/api/admin/wipe",
            json={"confirm": "WIPE", "requester_note": "seed-attempted"},
        )
        # Wipe itself succeeded — the audit row landed even though the
        # in-band seed couldn't run in this test env.
        assert resp.status_code == 200
        assert resp.json()["audit"]["requester_note"] == "seed-attempted"

    def test_audit_list_includes_recent_wipes(self, store):
        c = _client(store)
        c.post("/api/admin/wipe", json={"confirm": "WIPE", "requester_note": "first"})
        c.post("/api/admin/wipe", json={"confirm": "WIPE", "requester_note": "second"})
        wipes = c.get("/api/admin/wipes").json()["wipes"]
        assert len(wipes) == 2
        notes = [w["requester_note"] for w in wipes]
        assert "first" in notes and "second" in notes

    def test_audit_list_respects_limit(self, store):
        c = _client(store)
        for i in range(5):
            c.post("/api/admin/wipe", json={"confirm": "WIPE", "requester_note": f"#{i}"})
        wipes = c.get("/api/admin/wipes?limit=2").json()["wipes"]
        assert len(wipes) == 2

    def test_audit_list_empty_on_fresh_store(self, store):
        assert _client(store).get("/api/admin/wipes").json() == {"wipes": []}

    # ---------------------------------------------------------------
    # #1108 — opt-in Mailpit content wipe on the nuclear button.
    # ---------------------------------------------------------------
    def _ok_response(self):
        """Build a 200 httpx.Response with a request attached.

        httpx.Response.raise_for_status() needs `.request` set, so a
        bare ``httpx.Response(200)`` blows up — and the handler's
        broad except would silently mark the wipe as failed. Attach a
        no-op request so the test exercises the success path.
        """
        import httpx as _h
        req = _h.Request("DELETE", "http://mailpit/mailpit/api/v1/messages")
        return _h.Response(200, request=req)

    def test_wipe_mailpit_defaults_to_false(self, store, monkeypatch):
        """Without wipe_mailpit in the body, the handler does NOT call
        the Mailpit admin API. The persona-lifecycle epic relies on
        inbox continuity across runs — the default must be 'leave
        Mailpit alone'."""
        called: list[str] = []

        def _spy_delete(url, **_kw):
            called.append(url)
            return self._ok_response()

        monkeypatch.setattr("qa_review_api.app.httpx.delete", _spy_delete)

        self._seed_run_data(store)
        resp = _client(store).post(
            "/api/admin/wipe",
            json={"confirm": "WIPE", "requester_note": "default"},
        )
        assert resp.status_code == 200
        assert called == []
        # Audit row carries the "did not wipe" signal so the UI can
        # render the correct success message.
        assert resp.json()["audit"]["mailpit_wiped"] is False
        assert "mailpit_error" not in resp.json()["audit"]

    def test_wipe_mailpit_true_calls_mailpit_admin_api(self, store, monkeypatch):
        """When the operator ticks the checkbox the handler fires the
        Mailpit admin DELETE against the configured base URL. The PVC
        itself is NEVER deleted — only message contents."""
        captured: dict = {}

        def _fake_delete(url, **kw):
            captured["url"] = url
            captured["kwargs"] = kw
            return self._ok_response()

        monkeypatch.setattr("qa_review_api.app.httpx.delete", _fake_delete)

        self._seed_run_data(store)
        resp = _client(store).post(
            "/api/admin/wipe",
            json={
                "confirm": "WIPE",
                "requester_note": "true-reset",
                "wipe_mailpit": True,
            },
        )
        assert resp.status_code == 200
        # Hits the default cross-namespace URL under the /mailpit
        # webroot (#979).
        assert captured["url"].endswith("/mailpit/api/v1/messages")
        assert resp.json()["audit"]["mailpit_wiped"] is True

    def test_wipe_mailpit_failure_does_not_block_mongo_wipe(
        self, store, monkeypatch
    ):
        """A Mailpit blip MUST NOT roll back the Mongo wipe (which is
        already irreversible). The audit row carries the error so the
        operator can retry via the per-run init container."""
        def _raise(*_a, **_kw):
            raise RuntimeError("simulated mailpit unreachable")

        monkeypatch.setattr("qa_review_api.app.httpx.delete", _raise)

        self._seed_run_data(store)
        resp = _client(store).post(
            "/api/admin/wipe",
            json={
                "confirm": "WIPE",
                "requester_note": "mailpit-down",
                "wipe_mailpit": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # Mongo dropped counts still landed.
        assert body["dropped"]["qa_runs"] >= 1
        # Mailpit failure surfaced on the audit row.
        assert body["audit"]["mailpit_wiped"] is False
        assert "simulated mailpit unreachable" in body["audit"]["mailpit_error"]
