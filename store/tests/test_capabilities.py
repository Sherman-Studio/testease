"""Tests for the capabilities store layer (catalog, grants, depth score)."""

from __future__ import annotations

import mongomock
import pytest

from qa_store.capabilities import (
    CAPABILITY_CATALOG,
    DEPTH_LEVELS,
    capability_depth,
    delete_site_capability,
    get_capability_token,
    list_capabilities,
    list_site_capabilities,
    seed_capability_catalog,
    set_capability_status,
    upsert_capability,
)
from qa_store.schema import Store


def _store() -> Store:
    s = Store(client=mongomock.MongoClient(), db_name="testdb")
    s.capability_catalog.create_index([("capability_id", 1)], unique=True)
    s.site_capabilities.create_index(
        [("tenant_id", 1), ("target_id", 1), ("capability_id", 1)], unique=True,
    )
    return s


# ── catalog ──
def test_seed_is_idempotent_and_ordered():
    store = _store()
    n1 = seed_capability_catalog(store)
    n2 = seed_capability_catalog(store)
    assert n1 == n2 == len(CAPABILITY_CATALOG)
    caps = list_capabilities(store)
    assert len(caps) == len(CAPABILITY_CATALOG)  # no duplicates
    assert [c["level"] for c in caps] == sorted(c["level"] for c in caps)  # level-ordered
    # Sanity: the examples that motivated this all exist as baseline entries.
    ids = {c["capability_id"] for c in caps}
    assert {"kube-exec", "app-logs", "sandbox-inbox", "readonly-db", "admin-read-api"} <= ids


def test_custom_capability_added():
    store = _store()
    seed_capability_catalog(store)
    c = upsert_capability(store, capability_id="god-mode", title="God-mode console",
                          unlocks="Drive any internal action.", level=5)
    assert c["category"] == "custom" and c["baseline"] is False
    assert "god-mode" in {x["capability_id"] for x in list_capabilities(store)}


# ── grants + vault ──
def test_grant_with_secret_is_vaulted(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    seed_capability_catalog(store)
    g = set_capability_status(store, target_id="acme", capability_id="readonly-db",
                              status="granted", token="postgres://secret")
    assert g["status"] == "granted"
    assert g["credential_ref"] == "vault://default/acme/cap-readonly-db"
    raw = store.site_capabilities.find_one({"capability_id": "readonly-db"})
    assert "postgres://secret" not in str(raw)            # not inline
    assert get_capability_token(store, "default", "acme", "readonly-db") == "postgres://secret"


def test_propose_decline_na_flow():
    store = _store()
    seed_capability_catalog(store)
    set_capability_status(store, target_id="acme", capability_id="app-logs", status="proposed", proposed_by="explorer")
    set_capability_status(store, target_id="acme", capability_id="kube-exec", status="declined")
    rows = {r["capability_id"]: r for r in list_site_capabilities(store, "default", "acme")}
    assert rows["app-logs"]["status"] == "proposed" and rows["app-logs"]["proposed_by"] == "explorer"
    assert rows["kube-exec"]["status"] == "declined"


def test_set_rejects_bad_status():
    store = _store()
    seed_capability_catalog(store)
    with pytest.raises(ValueError):
        set_capability_status(store, target_id="acme", capability_id="app-logs", status="maybe")


def test_revoke_drops_vaulted_secret(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    seed_capability_catalog(store)
    set_capability_status(store, target_id="acme", capability_id="app-logs", status="granted", token="logkey")
    assert delete_site_capability(store, "default", "acme", "app-logs") is True
    assert get_capability_token(store, "default", "acme", "app-logs") is None
    assert delete_site_capability(store, "default", "acme", "app-logs") is False


# ── depth score ──
def test_depth_starts_black_box():
    store = _store()
    seed_capability_catalog(store)
    d = capability_depth(store, "default", "acme")
    assert d["depth_level"] == 0
    assert d["depth_label"] == DEPTH_LEVELS[0] == "Black-box"
    # Next unlock is the lowest rung available (an L1 identity/context capability).
    assert d["next_unlock"] is not None and d["next_unlock"]["level"] == 1


def test_depth_rises_to_highest_granted_rung():
    store = _store()
    seed_capability_catalog(store)
    set_capability_status(store, target_id="acme", capability_id="test-account", status="granted")  # L1
    set_capability_status(store, target_id="acme", capability_id="app-logs", status="granted")      # L3
    d = capability_depth(store, "default", "acme")
    assert d["depth_level"] == 3
    assert d["depth_label"] == "Observability"
    assert d["granted_count"] == 2
    # Next unlock points above the current rung.
    assert d["next_unlock"]["level"] > 3
