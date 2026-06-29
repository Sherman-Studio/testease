"""API tests for the capabilities endpoints."""

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
    s.capability_catalog.create_index([("capability_id", 1)], unique=True)
    s.site_capabilities.create_index(
        [("tenant_id", 1), ("target_id", 1), ("capability_id", 1)], unique=True,
    )
    s.site_secrets.create_index(
        [("tenant_id", 1), ("target_id", 1), ("ref", 1)], unique=True,
    )
    return s


def _client(store) -> TestClient:
    # create_app seeds the catalog on boot.
    settings = Settings(
        qa_store_url="mongodb://x", qa_store_db="testease_test",
        github_token="", github_repo="",
    )
    return TestClient(create_app(settings=settings, store=store, seed_personas=False))


def _target(store):
    upsert_site_target(store, target_id="acme", base_url="https://acme.example")


def test_catalog_seeded_on_boot(store):
    c = _client(store)
    ids = {x["capability_id"] for x in c.get("/api/capabilities").json()["capabilities"]}
    assert {"kube-exec", "app-logs", "readonly-db", "sandbox-inbox"} <= ids


def test_site_capabilities_starts_black_box(store):
    _target(store)
    body = _client(store).get("/api/site/targets/acme/capabilities").json()
    assert body["depth"]["depth_level"] == 0
    assert body["depth"]["depth_label"] == "Black-box"
    # Every catalog entry shows up with status "available" (no grant yet).
    assert all(c["status"] == "available" for c in body["capabilities"])


def test_grant_raises_depth(store):
    _target(store)
    c = _client(store)
    r = c.put("/api/site/targets/acme/capabilities/app-logs", json={"status": "granted", "token": "k"})
    assert r.status_code == 200
    assert r.json()["depth"]["depth_level"] == 3  # observability
    caps = {x["capability_id"]: x for x in r.json()["capabilities"]}
    assert caps["app-logs"]["status"] == "granted"


def test_granted_secret_is_never_echoed(store):
    _target(store)
    c = _client(store)
    r = c.put(
        "/api/site/targets/acme/capabilities/readonly-db",
        json={"status": "granted", "token": "postgres://supersecret"},
    )
    assert "supersecret" not in r.text
    caps = {x["capability_id"]: x for x in r.json()["capabilities"]}
    assert caps["readonly-db"]["credential_ref"] == "vault://default/acme/cap-readonly-db"


def test_decline_and_bad_status(store):
    _target(store)
    c = _client(store)
    assert c.put("/api/site/targets/acme/capabilities/kube-exec", json={"status": "declined"}).status_code == 200
    assert c.put("/api/site/targets/acme/capabilities/kube-exec", json={"status": "nope"}).status_code == 422
    assert c.put("/api/site/targets/acme/capabilities/ghost-cap", json={"status": "granted"}).status_code == 404


def test_custom_capability(store):
    _target(store)
    c = _client(store)
    r = c.post(
        "/api/site/targets/acme/capabilities",
        json={"title": "God-mode console", "unlocks": "Drive any internal action.", "level": 5, "token": "x"},
    )
    assert r.status_code == 200
    customs = [x for x in r.json()["capabilities"] if x["category"] == "custom"]
    assert len(customs) == 1 and customs[0]["status"] == "granted"
    assert r.json()["depth"]["depth_level"] == 5


def test_revoke(store):
    _target(store)
    c = _client(store)
    c.put("/api/site/targets/acme/capabilities/app-logs", json={"status": "granted", "token": "k"})
    r = c.delete("/api/site/targets/acme/capabilities/app-logs")
    caps = {x["capability_id"]: x for x in r.json()["capabilities"]}
    assert caps["app-logs"]["status"] == "available"
    assert r.json()["depth"]["depth_level"] == 0


def test_unknown_target_404(store):
    assert _client(store).get("/api/site/targets/ghost/capabilities").status_code == 404


# ── P4: capability → MCP wiring surfaced in the view + a resolved endpoint ──
def test_capabilities_view_includes_powers(store):
    _target(store)
    caps = {
        x["capability_id"]: x
        for x in _client(store).get("/api/site/targets/acme/capabilities").json()["capabilities"]
    }
    # A mapped capability advertises the MCP tool it powers…
    powers = caps["openapi-spec"]["powers"]
    assert any(p["server_id"] == "openapi" for p in powers)
    # …and an unmapped one powers nothing.
    assert caps["test-account"]["powers"] == []


def test_target_mcp_endpoint_lists_granted_servers(store):
    _target(store)
    c = _client(store)
    # Nothing granted yet → no servers.
    assert c.get("/api/site/targets/acme/mcp").json()["server_ids"] == []
    # Grant openapi-spec → the openapi server lights up (names only, no secret).
    c.put(
        "/api/site/targets/acme/capabilities/openapi-spec",
        json={"status": "granted", "token": "https://acme.example/openapi.json"},
    )
    body = c.get("/api/site/targets/acme/mcp").json()
    assert body["server_ids"] == ["openapi"]
    assert body["servers"][0]["capabilities"] == ["openapi-spec"]
    assert "openapi.json" not in str(body)  # credential never echoed


def test_target_mcp_endpoint_404_unknown(store):
    assert _client(store).get("/api/site/targets/ghost/mcp").status_code == 404
