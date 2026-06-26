"""Tests for the harness-side credentials cache (#1105 Slice 1.1).

The qa-store layer's CRUD is pinned at the data-plane level (#1110);
this module covers:
  - the in-process cache populates on first read + skips re-read on
    second
  - save_after_signup warms the cache so subsequent reads see the
    bundle without touching qa-store again
  - failure paths are silent (return None / log warning, don't raise)
  - reset_run_cache wipes the cache between personas
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import mongomock
import pytest
from qa_store import create_persona, set_persona_credentials
from qa_store.schema import Store, _ensure_indexes

from qa_agents import credentials as creds


@pytest.fixture
def store():
    client = mongomock.MongoClient()
    s = Store(client=client, db_name="slyreply_qa_test")
    _ensure_indexes(s)
    create_persona(s, {
        "persona_id": "maya",
        "display_name": "Maya",
        "registered_email": "maya@x.com",
        "explore_system_prompt": "x",
        "report_system_prompt": "x",
        "flows": [],
        "uses_admin_login": False,
        "setup_actions": None,
        "browser_locale": None,
        "color_token": "teal",
        "avatar_seed": "maya",
        "is_default": True,
        "hidden": False,
        "is_active": True,
    })
    return s


@pytest.fixture(autouse=True)
def _reset_cache():
    """The cache is module-global; clear it before + after every test
    so cases can't leak state into each other."""
    creds.reset_run_cache()
    yield
    creds.reset_run_cache()


# ---------------------------------------------------------------------------
# load_for_persona — populates cache, handles missing creds, swallows errors
# ---------------------------------------------------------------------------
def test_load_returns_none_when_persona_has_no_credentials(store):
    assert creds.load_for_persona(store, "maya") is None


def test_load_returns_bundle_when_credentials_exist(store):
    set_persona_credentials(
        store, "maya", email="maya+r1@x.com", password_plain="pw",
    )
    bundle = creds.load_for_persona(store, "maya")
    assert bundle is not None
    assert bundle.email == "maya+r1@x.com"
    assert bundle.password == "pw"
    assert bundle.verified is False
    assert bundle.last_rotation_n == 0


def test_load_caches_after_first_call(store):
    """Second read should hit the cache, not qa-store. We patch the
    qa-store helper between calls to prove the second one doesn't
    re-query."""
    set_persona_credentials(store, "maya", email="m@x.com", password_plain="pw")
    first = creds.load_for_persona(store, "maya")
    assert first is not None and first.email == "m@x.com"
    # Mutate the underlying doc behind the cache's back; cached read
    # should still return the original value.
    set_persona_credentials(store, "maya", email="changed@x.com", password_plain="pw2")
    cached = creds.load_for_persona(store, "maya")
    assert cached.email == "m@x.com"
    # force_refresh bypasses the cache.
    fresh = creds.load_for_persona(store, "maya", force_refresh=True)
    assert fresh.email == "changed@x.com"


def test_load_swallows_qastore_failures(caplog):
    """A Mongo blip during load_for_persona must return None and log,
    not crash the run."""
    bad_store = MagicMock()
    bad_store.personas.find_one.side_effect = RuntimeError("mongo down")
    caplog.set_level(logging.ERROR, logger="qa_agents.credentials")
    result = creds.load_for_persona(bad_store, "maya")
    assert result is None
    assert any("load failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# save_after_signup — persists + warms cache + swallows failures
# ---------------------------------------------------------------------------
def test_save_after_signup_persists_and_warms_cache(store):
    creds.save_after_signup(
        store, "maya",
        email="maya+r1@x.com",
        password="hunter22",
        verified=True,
    )
    # Cache returns the new bundle without a fresh qa-store read.
    bundle = creds.load_for_persona(store, "maya")
    assert bundle.email == "maya+r1@x.com"
    assert bundle.password == "hunter22"
    assert bundle.verified is True


def test_save_after_signup_swallows_qastore_failures(caplog):
    """A failed save must not crash the persona's exploration phase;
    the persona just won't have persisted credentials going forward."""
    bad_store = MagicMock()
    bad_store.personas.find_one.side_effect = RuntimeError("mongo down")
    caplog.set_level(logging.ERROR, logger="qa_agents.credentials")
    creds.save_after_signup(
        bad_store, "maya", email="m@x.com", password="pw",
    )
    assert any("save failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# clear_for_persona — clears qa-store + cache, idempotent on unknown
# ---------------------------------------------------------------------------
def test_clear_wipes_credentials_and_cache(store):
    set_persona_credentials(store, "maya", email="m@x.com", password_plain="pw")
    # Prime the cache.
    creds.load_for_persona(store, "maya")
    creds.clear_for_persona(store, "maya")
    # Subsequent load returns None — both qa-store AND cache are empty.
    assert creds.load_for_persona(store, "maya") is None


def test_clear_idempotent_on_persona_without_credentials(store):
    """Clearing an already-empty credentials sub-doc is a no-op, not
    an error. The setup-action DSL's clear_credentials_then_signup
    fires this unconditionally."""
    creds.clear_for_persona(store, "maya")  # no credentials → no raise


def test_clear_idempotent_on_unknown_persona(store):
    """Unknown persona is also a no-op — the harness might race
    against a persona being deleted; we don't want to crash."""
    creds.clear_for_persona(store, "ghost")


# ---------------------------------------------------------------------------
# reset_run_cache
# ---------------------------------------------------------------------------
def test_reset_run_cache_clears_in_memory_state(store):
    set_persona_credentials(store, "maya", email="m@x.com", password_plain="pw")
    creds.load_for_persona(store, "maya")
    # Behind the cache's back, swap qa-store state.
    set_persona_credentials(store, "maya", email="new@x.com", password_plain="pw2")
    # Without reset, cache returns stale.
    assert creds.load_for_persona(store, "maya").email == "m@x.com"
    creds.reset_run_cache()
    # Post-reset, the next read goes to qa-store and picks up the new value.
    assert creds.load_for_persona(store, "maya").email == "new@x.com"


# ---------------------------------------------------------------------------
# record_session — refreshes JWT only, doesn't touch password
# ---------------------------------------------------------------------------
def test_record_session_updates_jwt_and_cache(store):
    set_persona_credentials(store, "maya", email="m@x.com", password_plain="pw")
    creds.load_for_persona(store, "maya")  # warm cache
    creds.record_session(store, "maya", jwt="eyJhbGc...")
    bundle = creds.load_for_persona(store, "maya")
    assert bundle.session_jwt == "eyJhbGc..."
    assert bundle.password == "pw"  # unchanged


def test_record_session_logs_warning_when_no_credentials(store, caplog):
    """Can't refresh a session you never started — the harness logs
    a warning and continues."""
    caplog.set_level(logging.WARNING, logger="qa_agents.credentials")
    creds.record_session(store, "maya", jwt="x")
    assert any(
        "has no saved credentials" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# save_resume_token + load_resume_token (#1257 slice 2)
# ---------------------------------------------------------------------------
def test_save_resume_token_round_trip(store):
    from datetime import UTC, datetime
    set_persona_credentials(store, "maya", email="m@x.com", password_plain="pw")
    expires = datetime(2026, 12, 31, tzinfo=UTC)
    creds.save_resume_token(store, "maya", token="tok-1", expires_at=expires)
    result = creds.load_resume_token(store, "maya")
    assert result is not None
    assert result["resume_token"] == "tok-1"
    assert result["expires_at"] == expires


def test_save_resume_token_without_credentials_logs_warning(store, caplog):
    """Same shape as record_session: a save called before any signup
    is a soft warning, not a crash. Next signup will populate creds
    and a subsequent save will work."""
    caplog.set_level(logging.WARNING, logger="qa_agents.credentials")
    creds.save_resume_token(store, "maya", token="tok-1")
    assert any(
        "has no saved credentials" in r.getMessage()
        for r in caplog.records
    )


def test_load_resume_token_returns_none_for_unknown_persona(store):
    assert creds.load_resume_token(store, "ghost") is None


def test_load_resume_token_returns_none_when_never_saved(store):
    set_persona_credentials(store, "maya", email="m@x.com", password_plain="pw")
    assert creds.load_resume_token(store, "maya") is None


def test_load_resume_token_returns_none_when_expired(store):
    """Past expiry → None. Caller falls back to the UI login path
    without burning a network round-trip on a token we already know
    is dead."""
    from datetime import UTC, datetime
    set_persona_credentials(store, "maya", email="m@x.com", password_plain="pw")
    creds.save_resume_token(
        store, "maya", token="expired",
        expires_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert creds.load_resume_token(store, "maya") is None


def test_save_resume_token_swallows_unexpected_exceptions(caplog):
    """A failing qa-store should not crash the persona's setup phase.
    Mock the store to raise an unrelated exception and confirm the
    helper logs + returns cleanly."""
    caplog.set_level(logging.ERROR, logger="qa_agents.credentials")
    bad_store = MagicMock()
    bad_store.personas.find_one.side_effect = RuntimeError("mongo blip")
    # No assertions on the return value — save_resume_token returns None.
    creds.save_resume_token(bad_store, "maya", token="x")
    assert any(
        "save_resume_token failed" in r.getMessage()
        for r in caplog.records
    )
