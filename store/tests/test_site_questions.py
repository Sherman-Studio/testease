"""Tests for the explorer questionnaire (qa_store.site_questions).

Covers the CRUD, the re-explore idempotency (refresh wording, keep the answer),
ordering/scoping, and — the important one — that ``secret`` answers route to the
vault and never persist the raw value on the question row.
"""

from __future__ import annotations

import mongomock
import pytest

from qa_store.schema import Store
from qa_store.site_questions import (
    answer_site_question,
    delete_site_question,
    get_site_question,
    list_questions_by_target,
    questionnaire_status,
    skip_site_question,
    upsert_site_question,
)
from qa_store.vault import get_secret, secret_exists


def _store() -> Store:
    return Store(client=mongomock.MongoClient(), db_name="testdb")


def _q(store, qid="q1", *, kind="free_text", **kw):
    return upsert_site_question(
        store, target_id="acme", question_id=qid, text=kw.pop("text", "A question?"),
        kind=kind, **kw,
    )


# ── upsert / defaults / validation ───────────────────────────────────────
def test_new_question_defaults_open_unanswered():
    store = _store()
    q = _q(store, category="auth", rationale="need it", required=True, order=2)
    assert q["status"] == "open"
    assert q["answer"] is None and q["credential_ref"] is None
    assert q["category"] == "auth" and q["required"] is True and q["order"] == 2
    assert q["generated_by"] == "explorer"


def test_upsert_rejects_bad_kind_and_bad_id():
    store = _store()
    with pytest.raises(ValueError):
        _q(store, kind="nope")
    with pytest.raises(ValueError):
        _q(store, qid="a/b")


def test_re_explore_refreshes_text_but_keeps_answer():
    store = _store()
    _q(store, qid="q1", text="Old wording?")
    answer_site_question(store, target_id="acme", question_id="q1", answer="42")
    # The explorer re-runs and re-asks the same question with new wording.
    again = upsert_site_question(
        store, target_id="acme", question_id="q1", text="New wording?",
    )
    assert again["text"] == "New wording?"      # metadata refreshed
    assert again["status"] == "answered"        # answer preserved
    assert again["answer"] == "42"


# ── list: ordering, scoping, status filter ───────────────────────────────
def test_list_ordered_by_order_then_id():
    store = _store()
    _q(store, qid="b", order=1)
    _q(store, qid="a", order=1)
    _q(store, qid="z", order=0)
    ids = [q["question_id"] for q in list_questions_by_target(store, "default", "acme")]
    assert ids == ["z", "a", "b"]


def test_list_status_filter_and_scoping():
    store = _store()
    _q(store, qid="q1")
    _q(store, qid="q2")
    upsert_site_question(store, target_id="other", question_id="q3", text="x")
    answer_site_question(store, target_id="acme", question_id="q1", answer="y")
    open_ = list_questions_by_target(store, "default", "acme", status="open")
    assert [q["question_id"] for q in open_] == ["q2"]
    # 'other' target's question doesn't leak into acme's list.
    assert {q["question_id"] for q in list_questions_by_target(store, "default", "acme")} == {"q1", "q2"}


# ── answering: non-secret inline ─────────────────────────────────────────
def test_answer_non_secret_stored_inline():
    store = _store()
    _q(store, qid="q1", kind="free_text")
    q = answer_site_question(store, target_id="acme", question_id="q1", answer="blue")
    assert q["status"] == "answered" and q["answer"] == "blue"
    assert q["credential_ref"] is None
    assert q["answered_at"] is not None


def test_answer_unknown_question_returns_none():
    store = _store()
    assert answer_site_question(store, target_id="acme", question_id="ghost", answer="x") is None


# ── answering: secret routes to the vault, never inline ──────────────────
def test_secret_answer_goes_to_vault_not_the_row(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    _q(store, qid="admin-pw", kind="secret")
    q = answer_site_question(
        store, target_id="acme", question_id="admin-pw", answer="hunter2",
        label="admin password",
    )
    assert q["status"] == "answered"
    assert q["answer"] is None                       # raw value NOT on the row
    assert q["credential_ref"] == "vault://default/acme/q-admin-pw"
    # The value is retrievable only through the vault pointer.
    assert get_secret(store, q["credential_ref"]) == "hunter2"
    # And it isn't sitting in the question doc anywhere.
    raw = store.site_questions.find_one({"question_id": "admin-pw"})
    assert "hunter2" not in str(raw)


def test_re_answering_secret_replaces_vault_value(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    _q(store, qid="k", kind="secret")
    answer_site_question(store, target_id="acme", question_id="k", answer="old")
    q = answer_site_question(store, target_id="acme", question_id="k", answer="new")
    assert get_secret(store, q["credential_ref"]) == "new"


# ── skip / delete (delete cleans up the vaulted secret) ──────────────────
def test_skip():
    store = _store()
    _q(store, qid="q1")
    q = skip_site_question(store, target_id="acme", question_id="q1")
    assert q["status"] == "skipped" and q["answer"] is None


def test_delete_also_drops_the_vaulted_secret(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    _q(store, qid="k", kind="secret")
    q = answer_site_question(store, target_id="acme", question_id="k", answer="s")
    ref = q["credential_ref"]
    assert secret_exists(store, ref) is True
    assert delete_site_question(store, "default", "acme", "k") is True
    assert get_site_question(store, "default", "acme", "k") is None
    assert secret_exists(store, ref) is False    # no orphan left in the vault
    assert delete_site_question(store, "default", "acme", "k") is False


# ── roll-up for the lifecycle/UI ─────────────────────────────────────────
def test_questionnaire_status_counts():
    store = _store()
    _q(store, qid="q1", required=True)
    _q(store, qid="q2", required=True)
    _q(store, qid="q3")
    answer_site_question(store, target_id="acme", question_id="q1", answer="a")
    skip_site_question(store, target_id="acme", question_id="q3")
    st = questionnaire_status(store, "default", "acme")
    assert st == {"total": 3, "answered": 1, "open": 1, "skipped": 1, "required_open": 1}
