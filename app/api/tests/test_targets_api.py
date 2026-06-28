"""API tests for registering a target (the onboarding front door)."""

from __future__ import annotations

import mongomock
import pytest
from fastapi.testclient import TestClient
from qa_store.schema import Store

from qa_review_api.app import create_app
from qa_review_api.settings import Settings


@pytest.fixture
def store() -> Store:
    s = Store(client=mongomock.MongoClient(), db_name="testease_test")
    s.site_targets.create_index([("tenant_id", 1), ("target_id", 1)], unique=True)
    return s


def _client(store) -> TestClient:
    settings = Settings(
        qa_store_url="mongodb://x", qa_store_db="testease_test",
        github_token="", github_repo="",
    )
    return TestClient(create_app(settings=settings, store=store, seed_personas=False))


def test_create_target_starts_registered(store):
    c = _client(store)
    r = c.post("/api/site/targets", json={"base_url": "https://app.acme.com", "display_name": "Acme"})
    assert r.status_code == 200
    t = r.json()
    assert t["base_url"] == "https://app.acme.com"
    assert t["display_name"] == "Acme"
    assert t["lifecycle"] == "registered"
    assert t["target_id"] == "acme"  # slug from display name
    # Appears in the list.
    assert "acme" in [x["target_id"] for x in c.get("/api/site/targets").json()["targets"]]


def test_slug_from_host_when_no_display_name(store):
    r = _client(store).post("/api/site/targets", json={"base_url": "https://www.SauceDemo.com/"})
    assert r.status_code == 200
    assert r.json()["target_id"] == "saucedemo"  # www. dropped, lowercased


def test_slug_dedupes_with_suffix(store):
    c = _client(store)
    a = c.post("/api/site/targets", json={"base_url": "https://acme.com", "display_name": "Acme"})
    b = c.post("/api/site/targets", json={"base_url": "https://acme.io", "display_name": "Acme"})
    assert a.json()["target_id"] == "acme"
    assert b.json()["target_id"] == "acme-2"  # auto-suffixed, not a collision


def test_explicit_duplicate_id_is_409(store):
    c = _client(store)
    c.post("/api/site/targets", json={"base_url": "https://acme.com", "target_id": "acme"})
    r = c.post("/api/site/targets", json={"base_url": "https://acme.io", "target_id": "acme"})
    assert r.status_code == 409


@pytest.mark.parametrize("bad", ["acme.com", "ftp://acme.com", "not a url", "https://nodot", ""])
def test_bad_url_is_422(store, bad):
    r = _client(store).post("/api/site/targets", json={"base_url": bad})
    assert r.status_code == 422
