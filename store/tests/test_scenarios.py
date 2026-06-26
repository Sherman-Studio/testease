"""Tests for the saved-scenarios CRUD helpers (#862, Slice 5)."""

from __future__ import annotations

import time

import mongomock
import pytest
from pymongo.errors import DuplicateKeyError

from qa_store.schema import (
    Store,
    create_scenario,
    delete_scenario,
    get_scenario,
    list_scenarios,
    update_scenario,
)


@pytest.fixture
def store() -> Store:
    client = mongomock.MongoClient()
    s = Store(client=client, db_name="slyreply_qa_test")
    # Mirror the production unique-index so collision tests cover the
    # real path; mongomock honours unique constraints.
    s.scenarios.create_index("id", unique=True)
    return s


# ---------------------------------------------------------------------------
# create_scenario
# ---------------------------------------------------------------------------
def test_create_scenario_persists_all_fields(store):
    doc = create_scenario(
        store,
        id="smoke-billing",
        name="Smoke billing",
        description="Quick check on the upgrade-to-Pro flow.",
        persona_id="desktop-evaluator",
        mandatory_action_ids=[
            "billing.view_pricing_page",
            "billing.upgrade_to_pro",
        ],
    )
    assert doc["id"] == "smoke-billing"
    assert doc["name"] == "Smoke billing"
    assert "upgrade-to-Pro" in doc["description"]
    assert doc["persona_id"] == "desktop-evaluator"
    assert doc["mandatory_action_ids"] == [
        "billing.view_pricing_page",
        "billing.upgrade_to_pro",
    ]
    assert "created_at" in doc and "updated_at" in doc
    # No raw _id leaks through to JSON callers.
    assert "_id" not in doc


def test_create_scenario_strips_whitespace_on_string_fields(store):
    doc = create_scenario(
        store,
        id="  smoke-billing  ",
        name="  Smoke billing  ",
        description="  Quick check  ",
        persona_id="  desktop-evaluator  ",
        mandatory_action_ids=[],
    )
    assert doc["id"] == "smoke-billing"
    assert doc["name"] == "Smoke billing"
    assert doc["description"] == "Quick check"
    assert doc["persona_id"] == "desktop-evaluator"


def test_create_scenario_defaults_empty_description_and_actions(store):
    doc = create_scenario(
        store, id="x", name="X", persona_id="first-impression-critic",
    )
    assert doc["description"] == ""
    assert doc["mandatory_action_ids"] == []


def test_create_scenario_duplicate_id_raises(store):
    create_scenario(
        store, id="dup", name="One", persona_id="first-impression-critic",
    )
    with pytest.raises(DuplicateKeyError):
        create_scenario(
            store, id="dup", name="Two", persona_id="desktop-evaluator",
        )


# ---------------------------------------------------------------------------
# list_scenarios
# ---------------------------------------------------------------------------
def test_list_scenarios_returns_empty_when_none(store):
    assert list_scenarios(store) == []


def test_list_scenarios_orders_newest_first(store):
    create_scenario(store, id="a", name="First", persona_id="first-impression-critic")
    # Force a timestamp gap that mongomock will respect on the index sort.
    time.sleep(0.01)
    create_scenario(store, id="b", name="Second", persona_id="desktop-evaluator")
    docs = list_scenarios(store)
    assert [d["id"] for d in docs] == ["b", "a"]


# ---------------------------------------------------------------------------
# get_scenario
# ---------------------------------------------------------------------------
def test_get_scenario_returns_doc_for_known_id(store):
    create_scenario(store, id="x", name="X", persona_id="first-impression-critic")
    doc = get_scenario(store, "x")
    assert doc is not None
    assert doc["id"] == "x"
    assert "_id" not in doc


def test_get_scenario_returns_none_for_unknown_id(store):
    """Missing-is-not-an-error — the API translates to 404."""
    assert get_scenario(store, "no-such-id") is None


# ---------------------------------------------------------------------------
# update_scenario
# ---------------------------------------------------------------------------
def test_update_scenario_patches_only_provided_fields(store):
    create_scenario(
        store, id="x", name="Original", description="orig",
        persona_id="first-impression-critic",
        mandatory_action_ids=["auth.register_new_account"],
    )
    updated = update_scenario(store, "x", name="Renamed")
    assert updated["name"] == "Renamed"
    # Untouched fields preserve.
    assert updated["description"] == "orig"
    assert updated["persona_id"] == "first-impression-critic"
    assert updated["mandatory_action_ids"] == ["auth.register_new_account"]


def test_update_scenario_touches_updated_at(store):
    create_scenario(store, id="x", name="X", persona_id="first-impression-critic")
    before = get_scenario(store, "x")["updated_at"]
    time.sleep(0.01)
    after = update_scenario(store, "x", name="Y")["updated_at"]
    assert after > before


def test_update_scenario_can_change_mandatory_actions_to_empty(store):
    """Passing an explicit empty list MUST clear the field — distinct
    semantics from passing None (which leaves it alone)."""
    create_scenario(
        store, id="x", name="X", persona_id="first-impression-critic",
        mandatory_action_ids=["a", "b"],
    )
    updated = update_scenario(store, "x", mandatory_action_ids=[])
    assert updated["mandatory_action_ids"] == []


def test_update_scenario_returns_none_for_unknown_id(store):
    assert update_scenario(store, "no-such", name="x") is None


# ---------------------------------------------------------------------------
# delete_scenario
# ---------------------------------------------------------------------------
def test_delete_scenario_removes_existing(store):
    create_scenario(store, id="x", name="X", persona_id="first-impression-critic")
    assert delete_scenario(store, "x") is True
    assert get_scenario(store, "x") is None


def test_delete_scenario_returns_false_for_unknown_id(store):
    assert delete_scenario(store, "no-such") is False
