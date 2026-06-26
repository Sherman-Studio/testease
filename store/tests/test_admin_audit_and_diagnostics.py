"""Tests for the nuclear-button data plane (#1146).

Covers:
- ``record_admin_wipe`` / ``list_admin_wipes`` round-trip
- The audit collection SURVIVES ``wipe_for_relaunch`` (the whole point
  of carving it out — operators want history across resets)
"""

from __future__ import annotations

import mongomock
import pytest

from qa_store.schema import (
    Store,
    create_run,
    list_admin_wipes,
    record_admin_wipe,
)
from qa_store.wipe import wipe_for_relaunch


@pytest.fixture
def store() -> Store:
    client = mongomock.MongoClient()
    s = Store(client=client, db_name="slyreply_qa_test")
    # Mirror the prod indexes the schema sets up.
    s.runs.create_index("run_id", unique=True)
    s.admin_audit.create_index("wipe_id", unique=True)
    s.admin_audit.create_index([("wiped_at", -1)])
    return s


# ---------------------------------------------------------------------------
# record_admin_wipe / list_admin_wipes
# ---------------------------------------------------------------------------
def test_record_inserts_a_row(store):
    record_admin_wipe(
        store, wipe_id="abc123",
        dropped_counts={"qa_runs": 5, "qa_findings": 12},
        requester_note="Validating Slice 3",
    )
    rows = list_admin_wipes(store)
    assert len(rows) == 1
    assert rows[0]["wipe_id"] == "abc123"
    assert rows[0]["dropped_counts"] == {"qa_runs": 5, "qa_findings": 12}
    assert rows[0]["dropped_total"] == 17
    assert rows[0]["requester_note"] == "Validating Slice 3"


def test_record_sums_total_across_collections(store):
    record_admin_wipe(
        store, wipe_id="x",
        dropped_counts={"a": 3, "b": 0, "c": 10},
    )
    assert list_admin_wipes(store)[0]["dropped_total"] == 13


def test_record_strips_whitespace_from_note(store):
    record_admin_wipe(
        store, wipe_id="x", dropped_counts={},
        requester_note="  trimmed  ",
    )
    assert list_admin_wipes(store)[0]["requester_note"] == "trimmed"


def test_record_strips_whitespace_from_empty_note(store):
    """A whitespace-only note should normalise to empty string so the
    UI's optional-note treatment works."""
    record_admin_wipe(
        store, wipe_id="x", dropped_counts={},
        requester_note="   ",
    )
    assert list_admin_wipes(store)[0]["requester_note"] == ""


def test_list_returns_newest_first(store):
    """The /admin page shows newest first; the sort is wiped_at DESC."""
    from datetime import UTC, datetime, timedelta
    record_admin_wipe(store, wipe_id="old", dropped_counts={})
    record_admin_wipe(store, wipe_id="new", dropped_counts={})
    # Backdate the "old" row so the sort has something to bite on.
    store.admin_audit.update_one(
        {"wipe_id": "old"},
        {"$set": {"wiped_at": datetime.now(UTC) - timedelta(hours=1)}},
    )
    rows = list_admin_wipes(store)
    assert [r["wipe_id"] for r in rows] == ["new", "old"]


def test_list_respects_limit(store):
    for i in range(25):
        record_admin_wipe(store, wipe_id=f"w{i}", dropped_counts={})
    assert len(list_admin_wipes(store, limit=5)) == 5


def test_list_returns_empty_on_fresh_store(store):
    assert list_admin_wipes(store) == []


# ---------------------------------------------------------------------------
# Audit survives wipe_for_relaunch
# ---------------------------------------------------------------------------
def test_audit_survives_wipe(store):
    """The whole reason qa_admin_audit was carved out — the history
    must persist across resets so the operator can see chains of
    'wipe → run 1 → run 2 → wipe → ...' across many resets."""
    # Seed some run data + an audit row from a previous wipe.
    create_run(store, "r-prior", ["persona-a"])
    record_admin_wipe(
        store, wipe_id="prior-wipe", dropped_counts={"qa_runs": 1},
    )
    # Now wipe.
    dropped = wipe_for_relaunch(store)
    # qa_runs should be gone, qa_admin_audit should NOT be in the
    # drop list at all.
    assert "qa_runs" in dropped
    assert "qa_admin_audit" not in dropped
    # And the audit row survived.
    rows = list_admin_wipes(store)
    assert len(rows) == 1
    assert rows[0]["wipe_id"] == "prior-wipe"
