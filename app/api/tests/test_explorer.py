"""Tests for the heuristic site explorer (qa_review_api.explorer)."""

from __future__ import annotations

import mongomock
import pytest
from qa_store.schema import Store
from qa_store.site_model import (
    get_site_target,
    list_flows_by_target,
    list_surfaces_by_target,
    upsert_site_target,
)
from qa_store.site_questions import list_questions_by_target

from qa_review_api import explorer

_RICH_HTML = """
<html><head><title>Acme — the best widgets</title></head><body>
  <a href="/signup">Sign up</a>
  <a href="/login">Log in</a>
  <a href="/pricing">Pricing</a>
  <a href="/api/docs">API reference</a>
  <form action="/login"><input type="text" name="email"><input type="password" name="pw"></form>
</body></html>
"""


@pytest.fixture
def store() -> Store:
    return Store(client=mongomock.MongoClient(), db_name="testdb")


def _target(store, tid="acme", url="https://acme.example.com"):
    upsert_site_target(store, target_id=tid, base_url=url)


def test_rich_page_populates_model_and_questionnaire(store, monkeypatch):
    monkeypatch.setattr(explorer, "_fetch", lambda url: _RICH_HTML)
    _target(store)
    out = explorer.explore_target(store, "acme")
    assert out["lifecycle"] == "awaiting-answers"
    assert out["fetched"] is True
    assert set(out["detected"]) == {"signup", "login", "checkout", "api"}
    # Lifecycle advanced on the target.
    assert get_site_target(store, "default", "acme")["lifecycle"] == "awaiting-answers"
    # Surfaces: homepage + the login form.
    surfaces = list_surfaces_by_target(store, "default", "acme")
    assert any(s["surface_id"] == "home" for s in surfaces)
    assert any(s["kind"] == "auth_flow" for s in surfaces)
    # Flows from the detected affordances.
    flows = {f["flow_id"] for f in list_flows_by_target(store, "default", "acme")}
    assert {"signup", "login", "checkout", "api"} <= flows
    # Questionnaire: auth basics + the detected extras.
    qs = {q["question_id"]: q for q in list_questions_by_target(store, "default", "acme")}
    assert {"login-username", "login-password", "payment-mode", "api-base", "avoid-paths"} <= set(qs)
    assert qs["login-password"]["kind"] == "secret"
    assert qs["payment-mode"]["kind"] == "choice"


def test_minimal_page_gets_a_baseline(store, monkeypatch):
    monkeypatch.setattr(explorer, "_fetch", lambda url: "<html><title>Hi</title><body>nothing</body></html>")
    _target(store)
    out = explorer.explore_target(store, "acme")
    assert out["detected"] == []
    flows = {f["flow_id"] for f in list_flows_by_target(store, "default", "acme")}
    assert flows == {"first-impression"}  # never empty
    qs = {q["question_id"] for q in list_questions_by_target(store, "default", "acme")}
    assert {"login-username", "login-password", "avoid-paths"} <= qs
    assert out["lifecycle"] == "awaiting-answers"


def test_fetch_failure_still_advances(store, monkeypatch):
    monkeypatch.setattr(explorer, "_fetch", lambda url: None)
    _target(store)
    out = explorer.explore_target(store, "acme")
    assert out["fetched"] is False
    assert out["lifecycle"] == "awaiting-answers"
    # A homepage surface + the explorer knowledge note are still written.
    assert list_surfaces_by_target(store, "default", "acme")
    assert list_questions_by_target(store, "default", "acme")


def test_explore_proposes_relevant_capabilities(store, monkeypatch):
    from qa_store.capabilities import list_site_capabilities, seed_capability_catalog
    seed_capability_catalog(store)
    monkeypatch.setattr(explorer, "_fetch", lambda url: _RICH_HTML)
    _target(store)
    out = explorer.explore_target(store, "acme")
    assert out["counts"]["capabilities_proposed"] > 0
    proposed = {
        g["capability_id"]
        for g in list_site_capabilities(store, "default", "acme")
        if g["status"] == "proposed"
    }
    # The lighter, relevant rungs are suggested…
    assert {"test-account", "payments-sandbox", "api-token"} <= proposed
    # …but sensitive infra (L4/L5) is NEVER auto-proposed (earn-trust).
    assert "readonly-db" not in proposed and "kube-exec" not in proposed


def test_re_explore_does_not_clobber_a_grant(store, monkeypatch):
    from qa_store.capabilities import (
        list_site_capabilities,
        seed_capability_catalog,
        set_capability_status,
    )
    seed_capability_catalog(store)
    monkeypatch.setattr(explorer, "_fetch", lambda url: _RICH_HTML)
    _target(store)
    set_capability_status(store, target_id="acme", capability_id="test-account", status="granted")
    explorer.explore_target(store, "acme")  # re-explore
    rows = {g["capability_id"]: g for g in list_site_capabilities(store, "default", "acme")}
    assert rows["test-account"]["status"] == "granted"  # preserved, not re-proposed


def test_unknown_target_returns_none(store):
    assert explorer.explore_target(store, "ghost") is None


def test_re_explore_is_idempotent(store, monkeypatch):
    monkeypatch.setattr(explorer, "_fetch", lambda url: _RICH_HTML)
    _target(store)
    explorer.explore_target(store, "acme")
    n1 = len(list_questions_by_target(store, "default", "acme"))
    explorer.explore_target(store, "acme")  # again
    n2 = len(list_questions_by_target(store, "default", "acme"))
    assert n1 == n2  # upserts, no duplicates


# ── API endpoint ──────────────────────────────────────────────────────────
def _client(store):
    from fastapi.testclient import TestClient

    from qa_review_api.app import create_app
    from qa_review_api.settings import Settings

    settings = Settings(
        qa_store_url="mongodb://x", qa_store_db="testdb",
        github_token="", github_repo="",
    )
    return TestClient(create_app(settings=settings, store=store, seed_personas=False))


def test_explore_endpoint(store, monkeypatch):
    monkeypatch.setattr(explorer, "_fetch", lambda url: _RICH_HTML)
    _target(store)
    r = _client(store).post("/api/site/targets/acme/explore")
    assert r.status_code == 200
    body = r.json()
    assert body["lifecycle"] == "awaiting-answers"
    assert set(body["detected"]) == {"signup", "login", "checkout", "api"}
    assert body["counts"]["questions"] >= 3


def test_explore_unknown_target_404(store):
    assert _client(store).post("/api/site/targets/ghost/explore").status_code == 404
