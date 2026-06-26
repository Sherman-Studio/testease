"""Tests for apply_cross_run_dedup_for_run (Slice 2.1 of #1104).

The data plane helpers from Slice 2.0 (find_prior_finding,
bump_finding_recurring) are tested in test_finding_dedup.py. This file
tests the integration helper that the harness's report sink calls
after add_persona_result writes a run's findings.
"""

from __future__ import annotations

import mongomock
import pytest

from qa_store.schema import (
    Store,
    add_persona_result,
    apply_cross_run_dedup_for_run,
    create_run,
)


@pytest.fixture
def store() -> Store:
    client = mongomock.MongoClient()
    s = Store(client=client, db_name="slyreply_qa_test")
    s.runs.create_index("run_id", unique=True)
    s.findings.create_index("finding_id", unique=True)
    s.findings.create_index(
        [("persona", 1), ("category", 1), ("title_hash", 1), ("created_at", -1)],
    )
    return s


def _file(
    store, *,
    run_id, persona="maya", title="Login broken",
    category="bug", severity="major", body="",
):
    create_run(store, run_id, [persona])
    return add_persona_result(
        store, run_id, persona,
        review_markdown="...", verdict="explored",
        accounting={"cost_usd": 0.0},
        findings=[{
            "title": title, "category": category,
            "severity": severity, "body": body,
        }],
    )


# ---------------------------------------------------------------------------
# Basic counts + return shape
# ---------------------------------------------------------------------------
def test_no_priors_no_changes(store):
    """Run 1 has no priors; recurring_count stays at 1, is_regression False."""
    _file(store, run_id="run-1")
    counts = apply_cross_run_dedup_for_run(store, "run-1")
    assert counts == {"matched_priors": 0, "regressions": 0}
    doc = store.findings.find_one({"finding_id": "run-1:maya:1"})
    assert doc["recurring_count"] == 1
    assert doc["is_regression"] is False


def test_second_sighting_sets_recurring_count_to_2(store):
    _file(store, run_id="run-1", title="Login broken")
    apply_cross_run_dedup_for_run(store, "run-1")
    _file(store, run_id="run-2", title="Login broken")
    counts = apply_cross_run_dedup_for_run(store, "run-2")
    assert counts["matched_priors"] == 1
    doc = store.findings.find_one({"finding_id": "run-2:maya:1"})
    assert doc["recurring_count"] == 2


def test_third_sighting_sets_recurring_count_to_3(store):
    """The running tally lives on the MOST RECENT row."""
    from datetime import UTC, datetime, timedelta
    _file(store, run_id="run-1", title="Login broken")
    apply_cross_run_dedup_for_run(store, "run-1")
    _file(store, run_id="run-2", title="Login broken")
    # Push run-2's created_at forward so find_prior sort is deterministic.
    store.findings.update_one(
        {"finding_id": "run-2:maya:1"},
        {"$set": {"created_at": datetime.now(UTC) + timedelta(hours=1)}},
    )
    apply_cross_run_dedup_for_run(store, "run-2")
    _file(store, run_id="run-3", title="Login broken")
    store.findings.update_one(
        {"finding_id": "run-3:maya:1"},
        {"$set": {"created_at": datetime.now(UTC) + timedelta(hours=2)}},
    )
    counts = apply_cross_run_dedup_for_run(store, "run-3")
    assert counts["matched_priors"] == 1
    doc = store.findings.find_one({"finding_id": "run-3:maya:1"})
    assert doc["recurring_count"] == 3


def test_prior_row_is_not_mutated(store):
    """The new row carries the running tally; prior rows are frozen at
    the value they had when their own dedup pass ran."""
    _file(store, run_id="run-1", title="Login broken")
    apply_cross_run_dedup_for_run(store, "run-1")
    _file(store, run_id="run-2", title="Login broken")
    apply_cross_run_dedup_for_run(store, "run-2")
    r1 = store.findings.find_one({"finding_id": "run-1:maya:1"})
    assert r1["recurring_count"] == 1  # untouched


# ---------------------------------------------------------------------------
# Regression detection — the memory-backed signal retired with the
# persona-memory subsystem, so a recurring finding never flips
# is_regression any more. recurring_count is the surviving signal.
# ---------------------------------------------------------------------------
def test_recurring_finding_is_not_a_regression(store):
    """A bug seen again across runs bumps recurring_count but is NOT a
    regression — the memory-backed 'was fixed, now back' signal is gone."""
    _file(store, run_id="run-1", title="Login broken")
    apply_cross_run_dedup_for_run(store, "run-1")
    _file(store, run_id="run-2", title="Login broken")
    counts = apply_cross_run_dedup_for_run(store, "run-2")
    assert counts["regressions"] == 0
    doc = store.findings.find_one({"finding_id": "run-2:maya:1"})
    assert doc["is_regression"] is False
    assert doc["recurring_count"] == 2


# ---------------------------------------------------------------------------
# Cross-persona / cross-category isolation
# ---------------------------------------------------------------------------
def test_different_personas_dont_match(store):
    """Two personas filing the same title are independent streams."""
    _file(store, run_id="run-1", persona="maya",   title="Login broken")
    apply_cross_run_dedup_for_run(store, "run-1")
    _file(store, run_id="run-2", persona="jordan", title="Login broken")
    counts = apply_cross_run_dedup_for_run(store, "run-2")
    assert counts["matched_priors"] == 0
    doc = store.findings.find_one({"finding_id": "run-2:jordan:1"})
    assert doc["recurring_count"] == 1


def test_different_categories_dont_match(store):
    """A 'bug' and a 'confusion' with the same title are different artefacts."""
    _file(store, run_id="run-1", title="Checkout widget jank", category="bug")
    apply_cross_run_dedup_for_run(store, "run-1")
    _file(store, run_id="run-2", title="Checkout widget jank", category="confusion")
    counts = apply_cross_run_dedup_for_run(store, "run-2")
    assert counts["matched_priors"] == 0


def test_punctuation_variants_DO_match(store):
    """The whole point of title_hash — semantically-identical titles
    with cosmetic differences should match."""
    _file(store, run_id="run-1", title="Login broken")
    apply_cross_run_dedup_for_run(store, "run-1")
    _file(store, run_id="run-2", title="Login broken!")
    counts = apply_cross_run_dedup_for_run(store, "run-2")
    assert counts["matched_priors"] == 1


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------
def test_empty_run_is_a_no_op(store):
    """An unknown run_id (or one with no findings) returns zero counts."""
    counts = apply_cross_run_dedup_for_run(store, "no-such-run")
    assert counts == {"matched_priors": 0, "regressions": 0}


def test_findings_without_title_hash_are_skipped(store):
    """A pre-Slice-2.0 finding row without title_hash skips cleanly."""
    _file(store, run_id="run-1", title="Login broken")
    store.findings.update_one(
        {"finding_id": "run-1:maya:1"}, {"$unset": {"title_hash": 1}},
    )
    counts = apply_cross_run_dedup_for_run(store, "run-1")
    assert counts == {"matched_priors": 0, "regressions": 0}
