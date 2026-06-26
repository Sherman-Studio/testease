"""Site Model API tests — read endpoints (target-scoped, embeddings excluded)
+ site_knowledge curation (POST/PATCH/DELETE)."""

from __future__ import annotations

import mongomock
import pytest
from fastapi.testclient import TestClient
from qa_store.schema import Store
from qa_store.site_model import (
    upsert_site_knowledge,
    upsert_site_surface,
    upsert_site_target,
    upsert_test_flow,
)

from qa_review_api.app import create_app
from qa_review_api.settings import Settings


@pytest.fixture
def store() -> Store:
    s = Store(client=mongomock.MongoClient(), db_name="slyreply_qa_test")
    s.site_targets.create_index(
        [("tenant_id", 1), ("target_id", 1)], unique=True,
    )
    s.site_surfaces.create_index(
        [("tenant_id", 1), ("target_id", 1), ("surface_id", 1)], unique=True,
    )
    s.test_flows.create_index(
        [("tenant_id", 1), ("target_id", 1), ("flow_id", 1)], unique=True,
    )
    s.site_knowledge.create_index(
        [("tenant_id", 1), ("target_id", 1), ("entry_id", 1)], unique=True,
    )
    return s


@pytest.fixture
def seeded(store) -> Store:
    upsert_site_target(
        store, target_id="slyreply", base_url="https://slyreply.ai",
        display_name="SlyReply",
    )
    upsert_site_target(store, target_id="other", base_url="https://other.example")
    upsert_test_flow(
        store, target_id="slyreply", flow_id="signup", area="auth",
        persona_archetype="newcomer",
    )
    upsert_site_surface(
        store, target_id="slyreply", surface_id="s1", kind="page",
        path="/pricing", description="the pricing page",
    )
    upsert_site_knowledge(
        store, target_id="slyreply", entry_id="k1", kind="by_design",
        body="BY DESIGN — /verify-pending is intentional.",
    )
    # Cross-target row that must NOT leak into slyreply's lists.
    upsert_test_flow(store, target_id="other", flow_id="login", area="auth")
    return store


def _client(store) -> TestClient:
    settings = Settings(
        qa_store_url="mongodb://x", qa_store_db="slyreply_qa_test",
        github_token="ghp_t", github_repo="mccullya/slyreply",
    )
    return TestClient(create_app(settings=settings, store=store, seed_personas=False))


# ── reads ──
def test_list_targets(seeded):
    r = _client(seeded).get("/api/site/targets")
    assert r.status_code == 200
    ids = {t["target_id"] for t in r.json()["targets"]}
    assert {"slyreply", "other"} <= ids


def test_get_target_and_404(seeded):
    c = _client(seeded)
    assert c.get("/api/site/targets/slyreply").json()["base_url"] == "https://slyreply.ai"
    assert c.get("/api/site/targets/nope").status_code == 404


def test_flows_and_surfaces_scoped_to_target(seeded):
    c = _client(seeded)
    flows = c.get("/api/site/targets/slyreply/flows").json()["flows"]
    assert [f["flow_id"] for f in flows] == ["signup"]  # 'other'.login excluded
    surfaces = c.get("/api/site/targets/slyreply/surfaces").json()["surfaces"]
    assert [s["surface_id"] for s in surfaces] == ["s1"]


def test_knowledge_excludes_embedding_vectors(seeded):
    # Simulate the reconciler having embedded the row.
    seeded.site_knowledge.update_one(
        {"entry_id": "k1"},
        {"$set": {"body_embedding": [0.1] * 384, "embedded_body_sha": "abc"}},
    )
    rows = _client(seeded).get("/api/site/targets/slyreply/knowledge").json()["knowledge"]
    assert rows and rows[0]["body"].startswith("BY DESIGN")
    assert "body_embedding" not in rows[0]
    assert "embedded_body_sha" not in rows[0]


def test_surfaces_exclude_embedding_vectors(seeded):
    seeded.site_surfaces.update_one(
        {"surface_id": "s1"},
        {"$set": {"description_embedding": [0.1] * 384, "embedded_sha": "x"}},
    )
    rows = _client(seeded).get("/api/site/targets/slyreply/surfaces").json()["surfaces"]
    assert "description_embedding" not in rows[0]
    assert "embedded_sha" not in rows[0]


# ── curation ──
def test_create_knowledge(store):
    c = _client(store)
    r = c.post(
        "/api/site/targets/slyreply/knowledge",
        json={"body": "# new note", "kind": "guidance", "applies_to": ["s1"]},
    )
    assert r.status_code == 200
    assert r.json()["body"] == "# new note"
    assert r.json()["kind"] == "guidance"
    assert "body_embedding" not in r.json()
    # Persisted + listable.
    listed = c.get("/api/site/targets/slyreply/knowledge").json()["knowledge"]
    assert [k["body"] for k in listed] == ["# new note"]


def test_create_knowledge_rejects_bad_kind(store):
    r = _client(store).post(
        "/api/site/targets/slyreply/knowledge",
        json={"body": "x", "kind": "nonsense"},
    )
    assert r.status_code == 422


def test_patch_knowledge(seeded):
    r = _client(seeded).patch(
        "/api/site/knowledge/k1",
        json={"target_id": "slyreply", "body": "edited body", "kind": "known_issue"},
    )
    assert r.status_code == 200
    assert r.json()["body"] == "edited body"
    assert r.json()["kind"] == "known_issue"


def test_patch_unknown_404(seeded):
    r = _client(seeded).patch(
        "/api/site/knowledge/nope", json={"target_id": "slyreply", "body": "x"},
    )
    assert r.status_code == 404


def test_delete_knowledge(seeded):
    c = _client(seeded)
    r = c.delete("/api/site/knowledge/k1", params={"target_id": "slyreply"})
    assert r.status_code == 200
    assert c.get("/api/site/targets/slyreply/knowledge").json()["knowledge"] == []


def test_delete_unknown_404(seeded):
    r = _client(seeded).delete(
        "/api/site/knowledge/nope", params={"target_id": "slyreply"},
    )
    assert r.status_code == 404
