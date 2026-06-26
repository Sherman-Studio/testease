"""Tests for the persona credentials sub-doc (#1105 Slice 1).

Round-trips set / get / status / clear / record_session. Encryption
behaviour is covered separately in test_crypto.py — these tests assume
the crypto module's plaintext fallback (no QA_CREDENTIAL_KEY env) so
the test environment doesn't need a key set.
"""

from __future__ import annotations

from datetime import UTC, datetime

import mongomock
import pytest

from qa_store import (
    clear_persona_credentials,
    create_persona,
    get_persona,
    get_persona_credentials,
    get_persona_credentials_status,
    get_persona_resume_token,
    record_persona_resume_token,
    record_persona_session,
    set_persona_credentials,
)
from qa_store.schema import Store, _ensure_indexes


@pytest.fixture
def store():
    client = mongomock.MongoClient()
    s = Store(client=client, db_name="slyreply_qa_test")
    _ensure_indexes(s)
    return s


@pytest.fixture
def seeded_persona(store):
    create_persona(store, {
        "persona_id": "maya",
        "display_name": "Maya",
        "registered_email": "maya@testease.example.com",
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


# ---------------------------------------------------------------------------
# set_persona_credentials
# ---------------------------------------------------------------------------
def test_set_credentials_persists_email_and_encrypted_password(store, seeded_persona):
    set_persona_credentials(
        store, "maya",
        email="maya+r1@testease.example.com",
        password_plain="hunter22",
        verified=True,
    )
    doc = get_persona(store, "maya")
    creds = doc["credentials"]
    assert creds["email"] == "maya+r1@testease.example.com"
    # Password stored as the {value, is_encrypted} sub-doc shape.
    assert "value" in creds["password"]
    assert "is_encrypted" in creds["password"]
    assert creds["verified"] is True
    # rotation_n starts at 0; bumps only happen on email-changing re-saves.
    assert creds["last_rotation_n"] == 0
    assert isinstance(creds["registered_at"], datetime)


def test_set_credentials_unknown_persona_raises(store):
    with pytest.raises(KeyError):
        set_persona_credentials(
            store, "no-such-persona",
            email="x@y.com", password_plain="pw",
        )


def test_set_credentials_email_rotation_bumps_counter(store, seeded_persona):
    set_persona_credentials(
        store, "maya",
        email="maya+r1@testease.example.com",
        password_plain="pw1",
    )
    set_persona_credentials(
        store, "maya",
        email="maya+r2@testease.example.com",
        password_plain="pw2",
    )
    creds = get_persona(store, "maya")["credentials"]
    assert creds["last_rotation_n"] == 1


def test_set_credentials_password_update_same_email_preserves_registered_at(
    store, seeded_persona,
):
    set_persona_credentials(
        store, "maya", email="maya+r1@testease.example.com", password_plain="pw1",
    )
    first = get_persona(store, "maya")["credentials"]["registered_at"]
    set_persona_credentials(
        store, "maya", email="maya+r1@testease.example.com", password_plain="pw2",
    )
    second = get_persona(store, "maya")["credentials"]["registered_at"]
    assert first == second


# ---------------------------------------------------------------------------
# get_persona_credentials
# ---------------------------------------------------------------------------
def test_get_credentials_returns_plaintext_password(store, seeded_persona):
    set_persona_credentials(
        store, "maya", email="m@x.com", password_plain="topsecret",
    )
    creds = get_persona_credentials(store, "maya")
    assert creds["email"] == "m@x.com"
    assert creds["password_plain"] == "topsecret"
    # The encrypted-storage shape MUST NOT leak to callers.
    assert "password" not in creds


def test_get_credentials_unknown_persona_returns_none(store):
    assert get_persona_credentials(store, "ghost") is None


def test_get_credentials_persona_without_credentials_returns_none(
    store, seeded_persona,
):
    assert get_persona_credentials(store, "maya") is None


# ---------------------------------------------------------------------------
# get_persona_credentials_status
# ---------------------------------------------------------------------------
def test_status_persona_without_credentials_returns_stable_shape(
    store, seeded_persona,
):
    status = get_persona_credentials_status(store, "maya")
    assert status == {"has_credentials": False}


def test_status_unknown_persona_raises(store):
    with pytest.raises(KeyError):
        get_persona_credentials_status(store, "no-such-persona")


def test_status_never_includes_password(store, seeded_persona):
    """Critical security property: the operator-visible status endpoint
    MUST NOT leak the password under any shape (plain, encrypted, or
    placeholder)."""
    set_persona_credentials(
        store, "maya", email="m@x.com", password_plain="secretXYZ",
    )
    status = get_persona_credentials_status(store, "maya")
    assert status["has_credentials"] is True
    assert status["email"] == "m@x.com"
    # Stable shape — every key explicitly enumerated so a future change
    # adding the password by accident trips this immediately.
    assert set(status.keys()) == {
        "has_credentials",
        "email",
        "registered_at",
        "verified",
        "last_rotation_n",
        "has_session_jwt",
        "jwt_expires_at",
    }
    # And the value never appears anywhere in the serialised payload.
    assert "secretXYZ" not in str(status)


# ---------------------------------------------------------------------------
# clear_persona_credentials
# ---------------------------------------------------------------------------
def test_clear_unsets_credentials_and_bumps_rotation_log(
    store, seeded_persona,
):
    set_persona_credentials(
        store, "maya", email="m@x.com", password_plain="pw",
    )
    clear_persona_credentials(store, "maya")
    doc = get_persona(store, "maya")
    assert "credentials" not in doc
    # last_credential_rotation is the audit-side bump so the operator
    # can see "this persona's been reset N times" over the persona
    # lifetime even after the sub-doc is gone.
    assert doc.get("last_credential_rotation", 0) == 1


def test_clear_unknown_persona_raises(store):
    with pytest.raises(KeyError):
        clear_persona_credentials(store, "ghost")


def test_clear_then_set_resets_rotation_n_but_keeps_audit_count(
    store, seeded_persona,
):
    """Re-saving after a clear starts with rotation_n=0 (since the new
    save is the "first" credential for this rotation). The audit count
    on the persona's top-level field tracks resets independently."""
    set_persona_credentials(store, "maya", email="m1@x.com", password_plain="pw1")
    clear_persona_credentials(store, "maya")
    set_persona_credentials(store, "maya", email="m2@x.com", password_plain="pw2")
    doc = get_persona(store, "maya")
    assert doc["credentials"]["last_rotation_n"] == 0
    assert doc["last_credential_rotation"] == 1


# ---------------------------------------------------------------------------
# record_persona_session
# ---------------------------------------------------------------------------
def test_record_session_updates_only_jwt_fields(store, seeded_persona):
    set_persona_credentials(
        store, "maya",
        email="m@x.com", password_plain="pw",
    )
    expires = datetime(2026, 12, 31, tzinfo=UTC)
    record_persona_session(store, "maya", jwt="eyJhbGc...", jwt_expires_at=expires)
    creds = get_persona_credentials(store, "maya")
    assert creds["session_jwt"] == "eyJhbGc..."
    # Existing fields untouched.
    assert creds["email"] == "m@x.com"
    assert creds["password_plain"] == "pw"


def test_record_session_without_credentials_raises(store, seeded_persona):
    """Can't refresh a session you never started — caller should call
    set_persona_credentials first. KeyError flags this clearly instead
    of silently no-oping."""
    with pytest.raises(KeyError):
        record_persona_session(store, "maya", jwt="x")


def test_record_session_unknown_persona_raises(store):
    with pytest.raises(KeyError):
        record_persona_session(store, "ghost", jwt="x")


# ---------------------------------------------------------------------------
# record_persona_resume_token + get_persona_resume_token (#1257 slice 2)
# ---------------------------------------------------------------------------
def test_record_resume_token_round_trip(store, seeded_persona):
    set_persona_credentials(
        store, "maya", email="m@x.com", password_plain="pw",
    )
    expires = datetime(2026, 12, 31, tzinfo=UTC)
    record_persona_resume_token(
        store, "maya", token="tok-abc", expires_at=expires,
    )
    result = get_persona_resume_token(store, "maya")
    assert result is not None
    assert result["resume_token"] == "tok-abc"
    assert result["expires_at"] == expires


def test_record_resume_token_preserves_other_fields(store, seeded_persona):
    """Setting a resume token must NOT touch password / email / session_jwt."""
    set_persona_credentials(
        store, "maya",
        email="m@x.com", password_plain="pw",
        session_jwt="prior-jwt",
    )
    record_persona_resume_token(
        store, "maya", token="tok-1",
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    creds = get_persona_credentials(store, "maya")
    assert creds["email"] == "m@x.com"
    assert creds["password_plain"] == "pw"
    assert creds["session_jwt"] == "prior-jwt"
    assert creds["resume_token"] == "tok-1"


def test_record_resume_token_without_credentials_raises(store, seeded_persona):
    with pytest.raises(KeyError):
        record_persona_resume_token(store, "maya", token="x")


def test_record_resume_token_unknown_persona_raises(store):
    with pytest.raises(KeyError):
        record_persona_resume_token(store, "ghost", token="x")


def test_get_resume_token_returns_none_when_no_persona(store):
    assert get_persona_resume_token(store, "ghost") is None


def test_get_resume_token_returns_none_when_no_credentials(store, seeded_persona):
    assert get_persona_resume_token(store, "maya") is None


def test_get_resume_token_returns_none_when_no_token_saved(store, seeded_persona):
    set_persona_credentials(
        store, "maya", email="m@x.com", password_plain="pw",
    )
    # No resume token has been recorded yet.
    assert get_persona_resume_token(store, "maya") is None


def test_get_resume_token_returns_none_when_expired(store, seeded_persona):
    """Expired tokens collapse to None so the caller doesn't have to
    do its own clock arithmetic. The consume endpoint would 410 anyway
    but skipping the network round-trip is cheaper."""
    set_persona_credentials(
        store, "maya", email="m@x.com", password_plain="pw",
    )
    # Past expiry.
    record_persona_resume_token(
        store, "maya", token="expired-tok",
        expires_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert get_persona_resume_token(store, "maya") is None


def test_get_resume_token_naive_datetime_treated_as_utc(store, seeded_persona):
    """Some legacy paths store naive datetimes. Treat them as UTC at
    read time so the expiry check doesn't crash on a tzinfo mismatch."""
    set_persona_credentials(
        store, "maya", email="m@x.com", password_plain="pw",
    )
    # Naive future datetime.
    future_naive = datetime(2099, 1, 1)
    record_persona_resume_token(
        store, "maya", token="still-good", expires_at=future_naive,
    )
    result = get_persona_resume_token(store, "maya")
    assert result is not None
    assert result["resume_token"] == "still-good"
