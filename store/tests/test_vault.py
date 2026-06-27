"""Tests for the secrets vault (qa_store.vault).

The vault is the encrypted store the Site Model points into via opaque
``credential_ref`` strings. These cover the round-trip, the pointer format,
that the inventory never leaks values, real Fernet encryption when a key is
configured, and the graceful-None paths on bad pointers.
"""

from __future__ import annotations

import mongomock
import pytest

from qa_store.schema import Store
from qa_store.vault import (
    delete_secret,
    get_secret,
    list_secret_refs,
    make_credential_ref,
    parse_credential_ref,
    put_secret,
    secret_exists,
)


def _store() -> Store:
    return Store(client=mongomock.MongoClient(), db_name="testdb")


# ── credential_ref format ────────────────────────────────────────────────
def test_make_and_parse_round_trip():
    ref = make_credential_ref("default", "acme", "admin-pw")
    assert ref == "vault://default/acme/admin-pw"
    assert parse_credential_ref(ref) == ("default", "acme", "admin-pw")


@pytest.mark.parametrize(
    "bad",
    [
        "admin-pw",                      # no scheme
        "vault://default/acme",          # too few parts
        "vault://default/acme/a/b",      # too many parts
        "vault://default//admin-pw",     # empty component
        "https://default/acme/admin-pw", # wrong scheme
    ],
)
def test_parse_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_credential_ref(bad)


# ── put / get round-trip (no key → dev plaintext path) ───────────────────
def test_put_returns_pointer_and_get_round_trips(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    ref = put_secret(store, target_id="acme", value="hunter2", ref="admin-pw")
    assert ref == "vault://default/acme/admin-pw"
    assert get_secret(store, ref) == "hunter2"


def test_generated_ref_is_unique_and_resolvable(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    r1 = put_secret(store, target_id="acme", value="a")
    r2 = put_secret(store, target_id="acme", value="b")
    assert r1 != r2
    assert get_secret(store, r1) == "a"
    assert get_secret(store, r2) == "b"


def test_re_put_same_ref_replaces_value_and_keeps_created_at(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    ref = put_secret(store, target_id="acme", value="old", ref="k")
    created = store.site_secrets.find_one({"ref": "k"})["created_at"]
    put_secret(store, target_id="acme", value="new", ref="k")
    assert get_secret(store, ref) == "new"
    assert store.site_secrets.find_one({"ref": "k"})["created_at"] == created
    assert store.site_secrets.count_documents({"ref": "k"}) == 1


def test_put_rejects_ref_with_slash(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    with pytest.raises(ValueError):
        put_secret(store, target_id="acme", value="x", ref="a/b")


# ── encryption at rest (key configured) ──────────────────────────────────
def test_value_is_encrypted_at_rest_when_key_set(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("QA_CREDENTIAL_KEY", Fernet.generate_key().decode())
    store = _store()
    ref = put_secret(store, target_id="acme", value="s3cr3t", ref="k")
    raw = store.site_secrets.find_one({"ref": "k"})["secret"]
    assert raw["is_encrypted"] is True
    assert raw["value"] != "s3cr3t"          # ciphertext on disk, not plaintext
    assert get_secret(store, ref) == "s3cr3t"  # decrypts on read


# ── inventory never leaks values ─────────────────────────────────────────
def test_list_secret_refs_is_metadata_only(monkeypatch):
    monkeypatch.setenv("QA_CREDENTIAL_KEY", _key())
    store = _store()
    put_secret(store, target_id="acme", value="topsecret", ref="k", label="admin password")
    rows = list_secret_refs(store, target_id="acme")
    assert len(rows) == 1
    row = rows[0]
    assert row["credential_ref"] == "vault://default/acme/k"
    assert row["label"] == "admin password"
    assert row["is_encrypted"] is True
    # The value must NOT appear anywhere in the inventory row.
    assert "topsecret" not in str(row)
    assert "value" not in row and "secret" not in row


def test_list_scoped_by_target(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    put_secret(store, target_id="acme", value="a", ref="k1")
    put_secret(store, target_id="other", value="b", ref="k2")
    assert {r["ref"] for r in list_secret_refs(store, target_id="acme")} == {"k1"}
    assert {r["ref"] for r in list_secret_refs(store)} == {"k1", "k2"}  # tenant-wide


# ── exists / delete / bad pointers ───────────────────────────────────────
def test_secret_exists_and_delete(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    ref = put_secret(store, target_id="acme", value="x", ref="k")
    assert secret_exists(store, ref) is True
    assert delete_secret(store, ref) is True
    assert secret_exists(store, ref) is False
    assert get_secret(store, ref) is None
    assert delete_secret(store, ref) is False  # already gone


def test_unknown_or_malformed_pointer_is_none_not_raise(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    assert get_secret(store, "vault://default/acme/nope") is None
    assert get_secret(store, "garbage") is None
    assert secret_exists(store, "garbage") is False
    assert delete_secret(store, "garbage") is False


def _key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()
