"""Tests for the Slice 2.0 of #1104 finding dedup data plane.

Covers:
- ``normalised_title_hash`` collision behaviour + stability
- ``add_persona_result`` now stamps title_hash + recurring_count
  + last_verified_run_id + is_regression on every inserted finding
- ``find_prior_finding`` lookup + exclude_run_id
- ``bump_finding_recurring`` increments + preserves is_regression flag
"""

from __future__ import annotations

import mongomock
import pytest

from qa_store.schema import (
    Store,
    add_persona_result,
    bump_finding_recurring,
    create_run,
    find_prior_finding,
    normalised_title_hash,
)


@pytest.fixture
def store() -> Store:
    client = mongomock.MongoClient()
    s = Store(client=client, db_name="slyreply_qa_test")
    s.runs.create_index("run_id", unique=True)
    s.findings.create_index("finding_id", unique=True)
    s.findings.create_index([("run_id", 1)])
    s.findings.create_index(
        [
            ("persona", 1),
            ("category", 1),
            ("title_hash", 1),
            ("created_at", -1),
        ],
    )
    return s


def _file_finding(
    store, *,
    run_id="run-1", persona="maya",
    title="Login button doesn't render",
    category="bug", severity="major", body="Repro: load /login on Safari",
):
    create_run(store, run_id, [persona])
    return add_persona_result(
        store, run_id, persona,
        review_markdown="...",
        verdict="explored",
        accounting={"cost_usd": 0.0},
        findings=[{
            "title": title, "category": category,
            "severity": severity, "body": body,
        }],
    )


# ---------------------------------------------------------------------------
# normalised_title_hash
# ---------------------------------------------------------------------------
def test_hash_is_stable():
    h1 = normalised_title_hash("Login button broken")
    h2 = normalised_title_hash("Login button broken")
    assert h1 == h2


def test_hash_collides_on_punctuation_variance():
    """The dedup contract: cosmetic punctuation differences in the
    title (a stray exclamation, a trailing question mark) should not
    create separate findings."""
    assert normalised_title_hash("Login button broken!") == \
           normalised_title_hash("Login button broken")
    assert normalised_title_hash("Login broken?") == \
           normalised_title_hash("login broken")


def test_hash_collides_on_case():
    assert normalised_title_hash("LOGIN BUTTON BROKEN") == \
           normalised_title_hash("login button broken")


def test_hash_collides_on_whitespace_variance():
    """Internal whitespace runs collapse to one space — distillation
    sometimes emits double spaces or trailing whitespace."""
    assert normalised_title_hash("Login  button   broken") == \
           normalised_title_hash("Login button broken")
    assert normalised_title_hash("  Login button broken  ") == \
           normalised_title_hash("Login button broken")


def test_hash_differs_on_real_content_change():
    """Different actual bugs must NOT collide. The hash isn't perfect
    semantic clustering — it's a cheap dedup that the operator can
    override if needed."""
    assert normalised_title_hash("Login button broken") != \
           normalised_title_hash("Signup button broken")


def test_hash_handles_empty_and_none():
    """Distillation could produce a title-less finding (defensive).
    The hash should not throw."""
    assert isinstance(normalised_title_hash(""), str)
    assert isinstance(normalised_title_hash(None), str)
    # Empty and None collide — they're both "no title" findings.
    assert normalised_title_hash("") == normalised_title_hash(None)


def test_hash_is_16_hex_chars():
    """Storage form is a 16-char SHA-1 prefix. The compound index on
    title_hash relies on a fixed-width string for B-tree depth."""
    h = normalised_title_hash("Anything")
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# add_persona_result stamps the new fields
# ---------------------------------------------------------------------------
def test_add_persona_result_stamps_title_hash(store):
    _file_finding(store, title="Login button doesn't render")
    doc = store.findings.find_one({"finding_id": "run-1:maya:1"})
    expected_hash = normalised_title_hash("Login button doesn't render")
    assert doc["title_hash"] == expected_hash


def test_add_persona_result_initial_recurring_count_is_1(store):
    _file_finding(store)
    doc = store.findings.find_one({"finding_id": "run-1:maya:1"})
    assert doc["recurring_count"] == 1


def test_add_persona_result_initial_last_verified_run_id_is_self(store):
    """The first sighting IS the last verified sighting at insert time —
    subsequent witnesses bump this via bump_finding_recurring."""
    _file_finding(store)
    doc = store.findings.find_one({"finding_id": "run-1:maya:1"})
    assert doc["last_verified_run_id"] == "run-1"


def test_add_persona_result_initial_is_regression_is_false(store):
    _file_finding(store)
    doc = store.findings.find_one({"finding_id": "run-1:maya:1"})
    assert doc["is_regression"] is False


def test_add_persona_result_re_upsert_preserves_dedup_shape(store):
    """The same run + persona re-running should NOT spuriously bump
    recurring_count or flip is_regression — those are the
    cross-run dedup helpers' job. ``add_persona_result`` is idempotent
    per (run, persona)."""
    _file_finding(store, title="Login button broken")
    _file_finding(store, title="Login button broken")  # re-run
    doc = store.findings.find_one({"finding_id": "run-1:maya:1"})
    assert doc["recurring_count"] == 1
    assert doc["is_regression"] is False


# ---------------------------------------------------------------------------
# find_prior_finding
# ---------------------------------------------------------------------------
def test_find_prior_returns_none_for_no_match(store):
    result = find_prior_finding(
        store, persona="maya", category="bug",
        title_hash=normalised_title_hash("Anything"),
    )
    assert result is None


def test_find_prior_finds_by_dedup_key(store):
    _file_finding(
        store, run_id="run-1", persona="maya",
        title="Login button broken", category="bug",
    )
    prior = find_prior_finding(
        store, persona="maya", category="bug",
        title_hash=normalised_title_hash("Login button broken"),
    )
    assert prior is not None
    assert prior["finding_id"] == "run-1:maya:1"


def test_find_prior_filters_by_persona(store):
    """Two personas can independently file the same title — they're
    separate streams, not duplicates."""
    _file_finding(store, run_id="run-1", persona="maya",   title="Login broken")
    _file_finding(store, run_id="run-1", persona="jordan", title="Login broken")
    prior = find_prior_finding(
        store, persona="jordan", category="bug",
        title_hash=normalised_title_hash("Login broken"),
    )
    assert prior["persona"] == "jordan"


def test_find_prior_filters_by_category(store):
    """A `bug` and a `confusion` with the same title are different
    artefacts — the persona's classification matters."""
    _file_finding(
        store, run_id="run-1", title="Checkout widget jank",
        category="bug",
    )
    _file_finding(
        store, run_id="run-2", title="Checkout widget jank",
        category="confusion",
    )
    prior = find_prior_finding(
        store, persona="maya", category="confusion",
        title_hash=normalised_title_hash("Checkout widget jank"),
    )
    assert prior["category"] == "confusion"
    assert prior["run_id"] == "run-2"


def test_find_prior_returns_most_recent_match(store):
    """Multiple matches: return the freshest by created_at.

    Real-world cross-run dedup spans different runs hours-to-days apart,
    so production created_at values are always distinct. The test
    inserts back-to-back and then explicitly advances run-2's
    created_at so the sort has something to bite on.
    """
    from datetime import UTC, datetime, timedelta

    _file_finding(store, run_id="run-1", title="Login broken")
    _file_finding(store, run_id="run-2", title="Login broken")
    future = datetime.now(UTC) + timedelta(hours=1)
    store.findings.update_one(
        {"finding_id": "run-2:maya:1"},
        {"$set": {"created_at": future}},
    )
    prior = find_prior_finding(
        store, persona="maya", category="bug",
        title_hash=normalised_title_hash("Login broken"),
    )
    assert prior["run_id"] == "run-2"


def test_find_prior_exclude_run_id_skips_specified_run(store):
    """Slice 2.1's distillation calls this from inside the currently-
    running distil pass. exclude_run_id lets the caller say "don't
    find ourselves — only return TRULY prior sightings."""
    _file_finding(store, run_id="run-1", title="Login broken")
    _file_finding(store, run_id="run-2", title="Login broken")
    prior = find_prior_finding(
        store, persona="maya", category="bug",
        title_hash=normalised_title_hash("Login broken"),
        exclude_run_id="run-2",
    )
    assert prior["run_id"] == "run-1"


def test_find_prior_exclude_run_id_with_only_self_returns_none(store):
    _file_finding(store, run_id="run-1", title="Login broken")
    prior = find_prior_finding(
        store, persona="maya", category="bug",
        title_hash=normalised_title_hash("Login broken"),
        exclude_run_id="run-1",
    )
    assert prior is None


# ---------------------------------------------------------------------------
# bump_finding_recurring
# ---------------------------------------------------------------------------
def test_bump_increments_recurring_count(store):
    _file_finding(store, run_id="run-1")
    bump_finding_recurring(store, "run-1:maya:1", run_id="run-2")
    doc = store.findings.find_one({"finding_id": "run-1:maya:1"})
    assert doc["recurring_count"] == 2


def test_bump_updates_last_verified_run_id(store):
    _file_finding(store, run_id="run-1")
    bump_finding_recurring(store, "run-1:maya:1", run_id="run-5")
    doc = store.findings.find_one({"finding_id": "run-1:maya:1"})
    assert doc["last_verified_run_id"] == "run-5"


def test_bump_default_does_not_flip_is_regression(store):
    _file_finding(store, run_id="run-1")
    bump_finding_recurring(store, "run-1:maya:1", run_id="run-2")
    doc = store.findings.find_one({"finding_id": "run-1:maya:1"})
    assert doc["is_regression"] is False


def test_bump_explicit_flag_sets_is_regression(store):
    """The Slice 2.1 caller flips this when the bug had previously
    transitioned to memory.verification_status='fixed' but a new run
    saw it broken again."""
    _file_finding(store, run_id="run-1")
    bump_finding_recurring(
        store, "run-1:maya:1", run_id="run-2", is_regression=True,
    )
    doc = store.findings.find_one({"finding_id": "run-1:maya:1"})
    assert doc["is_regression"] is True


def test_bump_does_not_clear_existing_regression(store):
    """Once is_regression=True is set, a subsequent bump without the
    flag must NOT clear it. The loud signal stays until an operator
    explicitly acknowledges via the cockpit."""
    _file_finding(store, run_id="run-1")
    bump_finding_recurring(
        store, "run-1:maya:1", run_id="run-2", is_regression=True,
    )
    bump_finding_recurring(
        store, "run-1:maya:1", run_id="run-3",  # no flag this time
    )
    doc = store.findings.find_one({"finding_id": "run-1:maya:1"})
    assert doc["is_regression"] is True


def test_bump_does_not_modify_status(store):
    """A bump on an operator-dismissed finding must NOT silently
    re-open it. Status changes go through set_finding_status; recurring
    bumps stay at the dedup layer."""
    _file_finding(store, run_id="run-1")
    store.findings.update_one(
        {"finding_id": "run-1:maya:1"}, {"$set": {"status": "dismissed"}},
    )
    bump_finding_recurring(store, "run-1:maya:1", run_id="run-2")
    doc = store.findings.find_one({"finding_id": "run-1:maya:1"})
    assert doc["status"] == "dismissed"
    assert doc["recurring_count"] == 2


def test_bump_returns_none_for_unknown_finding(store):
    result = bump_finding_recurring(
        store, "no-such-finding:x:1", run_id="run-2",
    )
    assert result is None


def test_bump_returns_updated_doc(store):
    """Return value carries the post-bump state so the caller doesn't
    need a separate re-read."""
    _file_finding(store, run_id="run-1")
    doc = bump_finding_recurring(store, "run-1:maya:1", run_id="run-2")
    assert doc is not None
    assert doc["recurring_count"] == 2
    assert doc["last_verified_run_id"] == "run-2"
