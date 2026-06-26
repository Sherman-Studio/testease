"""Tests for the Site Model access layer (per-(tenant, target) site knowledge).

Uses mongomock + an arbitrary db_name (the access layer is DB-name-agnostic —
it operates on whatever DB the Store is configured for, never a literal).
"""

from __future__ import annotations

import mongomock
import pytest

from qa_store.schema import DEFAULT_TENANT, Store
from qa_store.site_model import (
    delete_test_flow,
    get_site_knowledge,
    get_site_target,
    get_test_flow,
    list_flows_by_target,
    list_knowledge_by_target,
    list_site_targets,
    list_surfaces_by_target,
    update_site_target,
    upsert_site_knowledge,
    upsert_site_surface,
    upsert_site_target,
    upsert_test_flow,
)


@pytest.fixture
def store() -> Store:
    client = mongomock.MongoClient()
    # Deliberately NOT "slyreply_qa" — proves the layer is db-name-agnostic.
    s = Store(client=client, db_name="testease_test")
    # Mirror the prod unique compound indexes so collision/scoping behaviour
    # is the real path (mongomock honours unique constraints).
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


# ── site_targets ──
def test_upsert_and_get_site_target(store):
    upsert_site_target(
        store, target_id="slyreply", base_url="https://slyreply.ai",
        display_name="SlyReply",
        auth={"method": "form", "credential_ref": "vault:slyreply-admin"},
    )
    t = get_site_target(store, DEFAULT_TENANT, "slyreply")
    assert t["base_url"] == "https://slyreply.ai"
    assert t["tenant_id"] == DEFAULT_TENANT
    assert t["auth"]["credential_ref"] == "vault:slyreply-admin"  # a pointer
    assert t["status"] == "active"
    assert "created_at" in t and "updated_at" in t


def test_upsert_is_idempotent_and_preserves_created_at(store):
    first = upsert_site_target(
        store, target_id="t1", base_url="https://a.example",
    )
    again = upsert_site_target(
        store, target_id="t1", base_url="https://a-renamed.example",
    )
    # One row, created_at pinned, fields updated.
    assert len(list_site_targets(store)) == 1
    assert again["created_at"] == first["created_at"]
    assert again["base_url"] == "https://a-renamed.example"


def test_update_and_default_auth(store):
    upsert_site_target(store, target_id="t1", base_url="https://a.example")
    t = get_site_target(store, DEFAULT_TENANT, "t1")
    assert t["auth"] == {"method": "none", "credential_ref": None}
    updated = update_site_target(store, DEFAULT_TENANT, "t1", status="paused")
    assert updated["status"] == "paused"


# ── (tenant, target) scoping isolation ──
def test_flows_are_scoped_per_target(store):
    upsert_test_flow(store, target_id="A", flow_id="signup")
    upsert_test_flow(store, target_id="A", flow_id="login")
    upsert_test_flow(store, target_id="B", flow_id="checkout")

    a = {f["flow_id"] for f in list_flows_by_target(store, DEFAULT_TENANT, "A")}
    b = {f["flow_id"] for f in list_flows_by_target(store, DEFAULT_TENANT, "B")}
    assert a == {"signup", "login"}
    assert b == {"checkout"}  # A's rows don't leak into B's list


def test_rows_are_scoped_per_tenant(store):
    upsert_site_knowledge(
        store, tenant_id="acme", target_id="A", entry_id="k1",
        kind="by_design", body="acme note",
    )
    upsert_site_knowledge(
        store, tenant_id="globex", target_id="A", entry_id="k1",
        kind="by_design", body="globex note",
    )
    acme = list_knowledge_by_target(store, "acme", "A")
    globex = list_knowledge_by_target(store, "globex", "A")
    assert [k["body"] for k in acme] == ["acme note"]
    assert [k["body"] for k in globex] == ["globex note"]


# ── per-entity CRUD + vector fields default to None ──
def test_test_flow_crud(store):
    upsert_test_flow(
        store, target_id="A", flow_id="signup", area="auth",
        user_story="sign up", persona_archetype="newcomer",
        generated_from="template",
    )
    f = get_test_flow(store, DEFAULT_TENANT, "A", "signup")
    assert f["persona_archetype"] == "newcomer"
    assert f["generated_from"] == "template"
    assert f["enabled"] is True
    assert delete_test_flow(store, DEFAULT_TENANT, "A", "signup") is True
    assert get_test_flow(store, DEFAULT_TENANT, "A", "signup") is None


def test_site_knowledge_vector_fields_default_none(store):
    upsert_site_knowledge(
        store, target_id="A", entry_id="k1", kind="by_design", body="# note",
    )
    k = get_site_knowledge(store, DEFAULT_TENANT, "A", "k1")
    assert k["body_embedding"] is None
    assert k["embedded_body_sha"] is None  # filled by the future reconciler


def test_site_surface_crud_and_vector_fields(store):
    upsert_site_surface(
        store, target_id="A", surface_id="s1", kind="page",
        path="/pricing", title="Pricing", description="the pricing page",
    )
    surfaces = list_surfaces_by_target(store, DEFAULT_TENANT, "A")
    assert len(surfaces) == 1
    assert surfaces[0]["description_embedding"] is None
    assert surfaces[0]["embedded_sha"] is None
