"""API tests for the explorer questionnaire + target lifecycle endpoints.

Mirrors test_site_model_api.py: a mongomock-backed Store with the prod unique
indexes, an injected-store TestClient, no persona seed.
"""

from __future__ import annotations

import mongomock
import pytest
from fastapi.testclient import TestClient
from qa_store.schema import Store
from qa_store.site_model import upsert_site_target

from qa_review_api.app import create_app
from qa_review_api.settings import Settings


@pytest.fixture
def store() -> Store:
    s = Store(client=mongomock.MongoClient(), db_name="testease_test")
    s.site_targets.create_index([("tenant_id", 1), ("target_id", 1)], unique=True)
    s.site_questions.create_index(
        [("tenant_id", 1), ("target_id", 1), ("question_id", 1)], unique=True,
    )
    s.site_secrets.create_index(
        [("tenant_id", 1), ("target_id", 1), ("ref", 1)], unique=True,
    )
    return s


def _client(store) -> TestClient:
    settings = Settings(
        qa_store_url="mongodb://x", qa_store_db="testease_test",
        github_token="", github_repo="",
    )
    return TestClient(create_app(settings=settings, store=store, seed_personas=False))


def _mk_target(store, target_id="acme"):
    upsert_site_target(store, target_id=target_id, base_url="https://app.acme.example")


def _mk_question(c, target_id="acme", **kw):
    payload = {"text": "A question?", "question_id": "q1", **kw}
    return c.post(f"/api/site/targets/{target_id}/questions", json=payload)


# ── list + rollup + lifecycle ────────────────────────────────────────────
def test_list_empty_includes_status_and_lifecycle(store):
    _mk_target(store)
    r = _client(store).get("/api/site/targets/acme/questions")
    assert r.status_code == 200
    body = r.json()
    assert body["questions"] == []
    assert body["status"]["total"] == 0
    assert body["lifecycle"] == "registered"
    assert "exploring" in body["lifecycle_states"]


# ── create ───────────────────────────────────────────────────────────────
def test_create_question(store):
    c = _client(store)
    r = _mk_question(c, kind="choice", category="auth", options=["a", "b"], required=True)
    assert r.status_code == 200
    q = r.json()
    assert q["question_id"] == "q1" and q["status"] == "open"
    assert q["kind"] == "choice" and q["required"] is True
    assert q["generated_by"] == "operator"
    listed = c.get("/api/site/targets/acme/questions").json()["questions"]
    assert [x["question_id"] for x in listed] == ["q1"]


def test_create_rejects_bad_kind(store):
    r = _mk_question(_client(store), kind="nonsense")
    assert r.status_code == 422


def test_create_duplicate_is_409(store):
    c = _client(store)
    assert _mk_question(c).status_code == 200
    assert _mk_question(c).status_code == 409


# ── answer: non-secret inline ────────────────────────────────────────────
def test_answer_non_secret(store):
    c = _client(store)
    _mk_question(c, kind="free_text")
    r = c.post("/api/site/targets/acme/questions/q1/answer", json={"answer": "blue"})
    assert r.status_code == 200
    q = r.json()
    assert q["status"] == "answered" and q["answer"] == "blue"


def test_answer_unknown_is_404(store):
    r = _client(store).post(
        "/api/site/targets/acme/questions/ghost/answer", json={"answer": "x"},
    )
    assert r.status_code == 404


# ── answer: secret never echoed, lands in the vault ──────────────────────
def test_secret_answer_is_vaulted_not_returned(store, monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    c = _client(store)
    _mk_question(c, kind="secret")
    r = c.post(
        "/api/site/targets/acme/questions/q1/answer",
        json={"answer": "hunter2", "label": "admin pw"},
    )
    assert r.status_code == 200
    q = r.json()
    assert q["status"] == "answered"
    assert q["answer"] is None                       # value NOT echoed
    assert q["credential_ref"] == "vault://default/acme/q-q1"
    assert "hunter2" not in r.text                    # nowhere in the response
    # And the value really is retrievable from the vault.
    from qa_store.vault import get_secret
    assert get_secret(store, q["credential_ref"]) == "hunter2"


# ── skip / delete ────────────────────────────────────────────────────────
def test_skip(store):
    c = _client(store)
    _mk_question(c)
    r = c.post("/api/site/targets/acme/questions/q1/skip")
    assert r.status_code == 200 and r.json()["status"] == "skipped"


def test_delete_and_404(store):
    c = _client(store)
    _mk_question(c)
    assert c.delete("/api/site/targets/acme/questions/q1").status_code == 200
    assert c.delete("/api/site/targets/acme/questions/q1").status_code == 404


# ── lifecycle ────────────────────────────────────────────────────────────
def test_set_lifecycle(store):
    _mk_target(store)
    c = _client(store)
    r = c.post("/api/site/targets/acme/lifecycle", json={"lifecycle": "exploring"})
    assert r.status_code == 200 and r.json()["lifecycle"] == "exploring"


def test_set_lifecycle_bad_state_422(store):
    _mk_target(store)
    r = _client(store).post("/api/site/targets/acme/lifecycle", json={"lifecycle": "bogus"})
    assert r.status_code == 422


def test_set_lifecycle_unknown_target_404(store):
    r = _client(store).post("/api/site/targets/ghost/lifecycle", json={"lifecycle": "exploring"})
    assert r.status_code == 404


# ── rollup reflects answers ──────────────────────────────────────────────
def test_status_rollup_updates(store):
    c = _client(store)
    _mk_question(c, question_id="q1", required=True)
    c.post("/api/site/targets/acme/questions", json={"text": "Q2?", "question_id": "q2"})
    c.post("/api/site/targets/acme/questions/q1/answer", json={"answer": "a"})
    st = c.get("/api/site/targets/acme/questions").json()["status"]
    assert st == {"total": 2, "answered": 1, "open": 1, "skipped": 0, "required_open": 0}
