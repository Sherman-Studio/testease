"""Tests for the qa-store schema + access functions, against a mock Mongo."""

from __future__ import annotations

import mongomock
import pytest

from qa_store.schema import (
    Store,
    add_persona_result,
    all_personas_reviewed,
    attach_finding_to_step,
    attach_screenshot_to_step,
    claim_run_finish,
    create_run,
    finding_id,
    finish_run,
    get_run,
    list_runs,
    list_steps_for_persona,
    mark_run_filed,
    record_step,
    set_finding_status,
)


@pytest.fixture
def store() -> Store:
    """A Store backed by mongomock — no real Mongo, no indexes side effects."""
    client = mongomock.MongoClient()
    s = Store(client=client, db_name="slyreply_qa_test")
    # mongomock supports create_index; mirror connect()'s indexes.
    s.runs.create_index("run_id", unique=True)
    s.findings.create_index("finding_id", unique=True)
    s.steps.create_index(
        [("run_id", 1), ("persona_id", 1), ("step_n", 1)], unique=True
    )
    return s


_FINDINGS = [
    {"category": "confusion", "severity": "major", "title": "What is a UID?", "body": "unclear"},
    {"category": "worry", "severity": "blocker", "title": "Privacy", "body": "scary"},
]


# ---------------------------------------------------------------------------
# create_run
# ---------------------------------------------------------------------------
def test_create_run_creates_doc(store):
    run = create_run(store, "run-1", ["first-impression-critic", "desktop-evaluator"])
    assert run["run_id"] == "run-1"
    assert run["status"] == "new"
    assert run["personas"] == ["first-impression-critic", "desktop-evaluator"]
    assert run["reviews"] == []
    assert run["gh_issue_url"] is None
    assert "_id" not in run


def test_create_run_is_idempotent_and_merges_personas(store):
    create_run(store, "run-1", ["first-impression-critic"])
    run = create_run(store, "run-1", ["first-impression-critic", "desktop-evaluator"])
    assert run["personas"] == ["first-impression-critic", "desktop-evaluator"]
    assert store.runs.count_documents({"run_id": "run-1"}) == 1


def test_create_run_does_not_clobber_existing_reviews(store):
    create_run(store, "run-1", ["first-impression-critic"])
    add_persona_result(store, "run-1", "first-impression-critic", "review md", "ok", {}, [])
    run = create_run(store, "run-1", ["desktop-evaluator"])
    assert len(run["reviews"]) == 1


# ---------------------------------------------------------------------------
# create_run — #858 run_notes + config_snapshot
# ---------------------------------------------------------------------------
def test_create_run_persists_run_notes_and_config_snapshot(store):
    run = create_run(
        store, "run-1", ["first-impression-critic"],
        run_notes="smoke test before billing migration #998",
        config_snapshot={
            "max_turns": 75,
            "concurrency": 2,
            "explore_model": "claude-haiku-4-5",
            "report_model": "claude-opus-4-7",
        },
    )
    assert run["run_notes"] == "smoke test before billing migration #998"
    assert run["config_snapshot"]["max_turns"] == 75
    assert run["config_snapshot"]["explore_model"] == "claude-haiku-4-5"


def test_create_run_defaults_to_empty_notes_and_snapshot(store):
    # Back-compat: callers that don't pass the new fields get sensible
    # empties (not missing keys — the review UI's renderer expects them).
    run = create_run(store, "run-1", ["first-impression-critic"])
    assert run["run_notes"] == ""
    assert run["config_snapshot"] == {}


def test_create_run_run_notes_is_sticky_across_idempotent_recalls(store):
    """First persona sets the operator's notes; later personas don't clobber.

    Mirrors the real-world flow — the orchestrator calls create_run once per
    persona, but only the FIRST call carries the operator's intent. The
    later calls reach the sink with the same notes value (it's a Config
    field, not per-persona) but the sticky merge is what makes them safe
    even if a future change emits empty notes from later callers."""
    create_run(store, "run-1", ["first-impression-critic"], run_notes="reproducing #861")
    # Second call passes empty notes — original must survive.
    create_run(store, "run-1", ["desktop-evaluator"], run_notes="")
    run = get_run(store, "run-1")
    assert run["run_notes"] == "reproducing #861"


def test_create_run_config_snapshot_is_sticky_across_idempotent_recalls(store):
    """Same sticky-merge contract as run_notes."""
    create_run(
        store, "run-1", ["first-impression-critic"],
        config_snapshot={"max_turns": 50, "concurrency": 1},
    )
    create_run(store, "run-1", ["desktop-evaluator"], config_snapshot=None)
    run = get_run(store, "run-1")
    assert run["config_snapshot"] == {"max_turns": 50, "concurrency": 1}


# ---------------------------------------------------------------------------
# add_persona_result
# ---------------------------------------------------------------------------
def test_add_persona_result_attaches_review_and_findings(store):
    create_run(store, "run-1", ["first-impression-critic"])
    add_persona_result(
        store, "run-1", "first-impression-critic", "## review", "Would use it.",
        {"total_cost_usd": 0.4}, _FINDINGS,
    )
    run = get_run(store, "run-1")
    assert len(run["reviews"]) == 1
    assert run["reviews"][0]["persona"] == "first-impression-critic"
    assert run["reviews"][0]["verdict"] == "Would use it."
    assert len(run["findings"]) == 2
    assert all(f["status"] == "open" for f in run["findings"])
    assert run["findings"][0]["finding_id"] == finding_id("run-1", "first-impression-critic", 1)


def test_add_persona_result_auto_creates_run(store):
    add_persona_result(store, "run-x", "desktop-evaluator", "md", "v", {}, [])
    run = get_run(store, "run-x")
    assert run is not None
    assert "desktop-evaluator" in run["personas"]


def test_add_persona_result_is_idempotent_per_persona(store):
    create_run(store, "run-1", ["first-impression-critic"])
    add_persona_result(store, "run-1", "first-impression-critic", "v1", "verdict1", {}, _FINDINGS)
    add_persona_result(store, "run-1", "first-impression-critic", "v2", "verdict2", {}, _FINDINGS[:1])
    run = get_run(store, "run-1")
    # Review slice replaced, not duplicated.
    assert len(run["reviews"]) == 1
    assert run["reviews"][0]["review_markdown"] == "v2"
    # Findings re-upserted; stale second finding cleared.
    assert len(run["findings"]) == 1


def test_add_persona_result_keeps_personas_separate(store):
    create_run(store, "run-1", ["first-impression-critic", "desktop-evaluator"])
    add_persona_result(store, "run-1", "first-impression-critic", "m", "mv", {}, _FINDINGS)
    add_persona_result(store, "run-1", "desktop-evaluator", "d", "dv", {}, _FINDINGS[:1])
    run = get_run(store, "run-1")
    assert len(run["reviews"]) == 2
    assert len(run["findings"]) == 3


# ---------------------------------------------------------------------------
# finish_run
# ---------------------------------------------------------------------------
def test_finish_run_sets_totals_and_status(store):
    create_run(store, "run-1", ["first-impression-critic"])
    run = finish_run(
        store, "run-1",
        {"input_tokens": 100, "output_tokens": 50, "cache_tokens": 10},
    )
    assert run["status"] == "reviewed"
    assert run["finished_at"] is not None
    assert run["totals"]["input_tokens"] == 100
    assert run["totals"]["output_tokens"] == 50
    assert run["totals"]["cache_tokens"] == 10


def test_finish_run_unknown_raises(store):
    with pytest.raises(KeyError):
        finish_run(store, "nope", {})


def test_finish_run_does_not_downgrade_filed_status(store):
    create_run(store, "run-1", ["first-impression-critic"])
    mark_run_filed(store, "run-1", "https://github.com/x/y/issues/1")
    run = finish_run(store, "run-1", {"input_tokens": 1})
    assert run["status"] == "filed"


# ---------------------------------------------------------------------------
# #882 — backend round-tripping. #1822 — the cost_usd / real_cost_usd
# dollar fields were retired: finish_run writes token counts + backend
# only, and silently DROPS any cost figure a stale caller still passes.
# Old run docs that carry the dollar keys are passed through untouched
# by the readers (list_runs / get_run never project fields away).
# ---------------------------------------------------------------------------
def test_finish_run_defaults_backend_to_api(store):
    """A caller that doesn't pass ``backend`` gets the historical 'api'
    default so the UI's isMaxBilled check treats the run as API-mode."""
    create_run(store, "run-back-compat", ["first-impression-critic"])
    run = finish_run(
        store, "run-back-compat",
        {"input_tokens": 100, "output_tokens": 50, "cache_tokens": 10},
    )
    assert run["totals"]["backend"] == "api"


def test_finish_run_records_claude_code_backend(store):
    create_run(store, "run-max", ["first-impression-critic"])
    run = finish_run(
        store, "run-max",
        {
            "input_tokens": 1_000_000,
            "output_tokens": 200_000,
            "cache_tokens": 0,
            "backend": "claude-code",
        },
    )
    assert run["totals"]["backend"] == "claude-code"


def test_finish_run_drops_cost_fields_from_stale_callers(store):
    """#1822 — a stale caller still passing the retired dollar fields must
    not get them persisted on a NEW run document."""
    create_run(store, "run-stale-cost", ["first-impression-critic"])
    run = finish_run(
        store, "run-stale-cost",
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_tokens": 0,
            "cost_usd": 3.5,
            "real_cost_usd": 0.0,
            "backend": "claude-code",
        },
    )
    assert "cost_usd" not in run["totals"]
    assert "real_cost_usd" not in run["totals"]
    assert run["totals"]["input_tokens"] == 100


def test_old_run_doc_with_cost_usd_passes_through_readers(store):
    """BACK-COMPAT (#1822) — a pre-#1822 run document that still carries
    ``totals.cost_usd`` / ``totals.real_cost_usd`` is returned verbatim by
    get_run / list_runs (no KeyError, no stripping)."""
    create_run(store, "run-legacy", ["first-impression-critic"])
    store.runs.update_one(
        {"run_id": "run-legacy"},
        {"$set": {
            "totals": {
                "input_tokens": 10, "output_tokens": 5, "cache_tokens": 0,
                "cost_usd": 1.25, "real_cost_usd": 0.0,
                "backend": "claude-code",
            },
            "status": "reviewed",
        }},
    )
    run = get_run(store, "run-legacy")
    assert run["totals"]["cost_usd"] == 1.25
    assert run["totals"]["real_cost_usd"] == 0.0
    listed = list_runs(store)
    assert listed[0]["totals"]["cost_usd"] == 1.25


# ---------------------------------------------------------------------------
# mark_run_filed
# ---------------------------------------------------------------------------
def test_mark_run_filed(store):
    create_run(store, "run-1", ["first-impression-critic"])
    run = mark_run_filed(store, "run-1", "https://github.com/mccullya/slyreply/issues/9")
    assert run["status"] == "filed"
    assert run["gh_issue_url"].endswith("/issues/9")


def test_mark_run_filed_unknown_raises(store):
    with pytest.raises(KeyError):
        mark_run_filed(store, "nope", "url")


# ---------------------------------------------------------------------------
# list_runs / get_run
# ---------------------------------------------------------------------------
def test_list_runs_newest_first_with_counts(store):
    from datetime import UTC, datetime

    create_run(store, "run-1", ["first-impression-critic"])
    create_run(store, "run-2", ["desktop-evaluator"])
    # create_run stamps started_at from the wall clock; in a fast test the two
    # inserts tie. Force distinct timestamps so the sort is exercised.
    store.runs.update_one(
        {"run_id": "run-1"},
        {"$set": {"started_at": datetime(2026, 5, 19, 10, 0, 0, tzinfo=UTC)}},
    )
    store.runs.update_one(
        {"run_id": "run-2"},
        {"$set": {"started_at": datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)}},
    )
    add_persona_result(store, "run-2", "desktop-evaluator", "md", "v", {}, _FINDINGS)
    runs = list_runs(store)
    assert [r["run_id"] for r in runs] == ["run-2", "run-1"]
    counts = {r["run_id"]: r["finding_counts"] for r in runs}
    assert counts["run-2"]["blocker"] == 1
    assert counts["run-2"]["major"] == 1
    assert counts["run-1"]["blocker"] == 0


def test_list_runs_respects_limit(store):
    for i in range(5):
        create_run(store, f"run-{i}", ["first-impression-critic"])
    assert len(list_runs(store, limit=2)) == 2


def test_get_run_unknown_returns_none(store):
    assert get_run(store, "nope") is None


# ---------------------------------------------------------------------------
# set_finding_status
# ---------------------------------------------------------------------------
def test_set_finding_status(store):
    create_run(store, "run-1", ["first-impression-critic"])
    add_persona_result(store, "run-1", "first-impression-critic", "md", "v", {}, _FINDINGS)
    fid = finding_id("run-1", "first-impression-critic", 1)
    updated = set_finding_status(store, fid, "included")
    assert updated["status"] == "included"
    assert get_run(store, "run-1")["findings"][0]["status"] == "included"


def test_set_finding_status_rejects_bad_status(store):
    create_run(store, "run-1", ["first-impression-critic"])
    add_persona_result(store, "run-1", "first-impression-critic", "md", "v", {}, _FINDINGS)
    with pytest.raises(ValueError):
        set_finding_status(store, finding_id("run-1", "first-impression-critic", 1), "bogus")


def test_set_finding_status_unknown_raises(store):
    with pytest.raises(KeyError):
        set_finding_status(store, "nope", "open")


# ---------------------------------------------------------------------------
# #860 — run_steps (Transcript tab data).
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# #1029 — Slice A of the MCP visibility epic. Aggregate run steps by the
# MCP server prefix in tool_name so the run-overview can chip-list them.
# ---------------------------------------------------------------------------
def test_summarise_mcp_servers_used_groups_by_server_prefix(store):
    from qa_store import summarise_mcp_servers_used
    create_run(store, "run-mcp", ["first-impression-critic"])
    # 5 playwright + 2 email + 1 findings call from the persona.
    for n, tool in enumerate([
        "mcp__playwright__browser_navigate",
        "mcp__playwright__browser_click",
        "mcp__playwright__browser_take_screenshot",
        "mcp__playwright__browser_snapshot",
        "mcp__playwright__browser_take_screenshot",
        "mcp__email__send_email",
        "mcp__email__wait_for_email",
        "mcp__findings__note_finding",
    ], start=1):
        record_step(store, "run-mcp", "first-impression-critic", n, tool_name=tool)
    result = summarise_mcp_servers_used(store, "run-mcp")
    assert result == [
        {"server": "playwright", "calls": 5},
        {"server": "email", "calls": 2},
        {"server": "findings", "calls": 1},
    ]


def test_summarise_mcp_servers_used_returns_empty_for_unknown_run(store):
    from qa_store import summarise_mcp_servers_used
    assert summarise_mcp_servers_used(store, "nope") == []


def test_summarise_mcp_servers_used_returns_empty_when_no_mcp_calls(store):
    """A run with only non-MCP tool_names — e.g. a debug run that
    pre-dates the recorder wiring — should produce an empty list, not
    crash on the prefix split."""
    from qa_store import summarise_mcp_servers_used
    create_run(store, "run-no-mcp", ["first-impression-critic"])
    record_step(store, "run-no-mcp", "first-impression-critic", 1, tool_name="raw_text")
    record_step(store, "run-no-mcp", "first-impression-critic", 2, tool_name="other")
    assert summarise_mcp_servers_used(store, "run-no-mcp") == []


def test_summarise_mcp_servers_used_aggregates_across_personas(store):
    """Multiple personas in one run all contribute to the same server
    counts — the chip list is run-scoped, not persona-scoped (Slice A's
    contract; per-persona breakdown is a possible future slice)."""
    from qa_store import summarise_mcp_servers_used
    create_run(store, "run-multi", ["first-impression-critic", "desktop-evaluator"])
    record_step(store, "run-multi", "first-impression-critic", 1,
                tool_name="mcp__playwright__browser_navigate")
    record_step(store, "run-multi", "desktop-evaluator", 1,
                tool_name="mcp__playwright__browser_navigate")
    record_step(store, "run-multi", "desktop-evaluator", 2,
                tool_name="mcp__email__send_email")
    result = summarise_mcp_servers_used(store, "run-multi")
    assert result == [
        {"server": "playwright", "calls": 2},
        {"server": "email", "calls": 1},
    ]


def test_summarise_mcp_servers_used_stable_tiebreak_alphabetical(store):
    """Two servers with the same call count tie-break alphabetically
    by server name so the chip ordering is deterministic across runs."""
    from qa_store import summarise_mcp_servers_used
    create_run(store, "run-tie", ["first-impression-critic"])
    record_step(store, "run-tie", "first-impression-critic", 1, tool_name="mcp__zulu__x")
    record_step(store, "run-tie", "first-impression-critic", 2, tool_name="mcp__alpha__y")
    record_step(store, "run-tie", "first-impression-critic", 3, tool_name="mcp__beta__z")
    result = summarise_mcp_servers_used(store, "run-tie")
    # All have calls=1 → alphabetical.
    assert [r["server"] for r in result] == ["alpha", "beta", "zulu"]


def test_get_run_includes_mcp_servers_used(store):
    """The aggregation lands in the run document returned by get_run so
    the API doesn't have to make a second call. Belt-and-braces: the run
    must have steps recorded for the field to be non-empty."""
    create_run(store, "run-with-mcp", ["first-impression-critic"])
    record_step(store, "run-with-mcp", "first-impression-critic", 1,
                tool_name="mcp__playwright__browser_navigate")
    run = get_run(store, "run-with-mcp")
    assert run["mcp_servers_used"] == [{"server": "playwright", "calls": 1}]


def test_get_run_mcp_servers_used_empty_when_no_steps(store):
    """A freshly-created run with no steps yet still returns the field —
    empty list — so the frontend can treat the field as always-present."""
    create_run(store, "run-empty", ["first-impression-critic"])
    run = get_run(store, "run-empty")
    assert run["mcp_servers_used"] == []


def test_record_step_persists_a_single_step(store):
    step = record_step(
        store, "run-1", "first-impression-critic", 1,
        tool_name="mcp__playwright__browser_navigate",
        args_summary="url=http://frontend/",
        text_from_persona="Opening the homepage to see what SlyReply is about.",
    )
    assert step["run_id"] == "run-1"
    assert step["persona_id"] == "first-impression-critic"
    assert step["step_n"] == 1
    assert step["tool_name"] == "mcp__playwright__browser_navigate"
    assert step["args_summary"] == "url=http://frontend/"
    assert "Opening the homepage" in step["text_from_persona"]
    assert step["screenshot_id"] is None
    assert step["finding_ordinals"] == []


def test_record_step_upserts_in_place_on_replay(store):
    """A replayed persona must overwrite its step, not duplicate."""
    record_step(store, "run-1", "first-impression-critic", 1, tool_name="x", args_summary="v1")
    record_step(store, "run-1", "first-impression-critic", 1, tool_name="x", args_summary="v2")
    steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
    assert len(steps) == 1
    assert steps[0]["args_summary"] == "v2"


def test_list_steps_for_persona_orders_by_step_n(store):
    # Insert OUT OF ORDER on purpose — the index sort must rescue us.
    for n in [3, 1, 2]:
        record_step(
            store, "run-1", "first-impression-critic", n,
            tool_name=f"tool_{n}",
        )
    steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
    assert [s["step_n"] for s in steps] == [1, 2, 3]


def test_list_steps_for_persona_isolates_personas_within_a_run(store):
    record_step(store, "run-1", "first-impression-critic", 1, tool_name="m1")
    record_step(store, "run-1", "desktop-evaluator", 1, tool_name="d1")
    record_step(store, "run-1", "desktop-evaluator", 2, tool_name="d2")
    margaret_steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
    daniel_steps = list_steps_for_persona(store, "run-1", "desktop-evaluator")
    assert [s["tool_name"] for s in margaret_steps] == ["m1"]
    assert [s["tool_name"] for s in daniel_steps] == ["d1", "d2"]


def test_list_steps_for_persona_returns_empty_when_unrecorded(store):
    """The harness might be running pre-#860 (no recorder wired). Empty
    transcript is a valid display state — not an error."""
    create_run(store, "run-1", ["first-impression-critic"])
    assert list_steps_for_persona(store, "run-1", "first-impression-critic") == []


def test_attach_screenshot_to_step_patches_existing(store):
    record_step(store, "run-1", "first-impression-critic", 1, tool_name="browser_take_screenshot")
    attach_screenshot_to_step(store, "run-1", "first-impression-critic", 1, "fake-oid-123")
    steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
    assert steps[0]["screenshot_id"] == "fake-oid-123"


def test_attach_screenshot_to_step_is_noop_when_step_missing(store):
    """Late screenshot writes mustn't crash the recorder. The transcript
    just loses one image instead of the whole run."""
    attach_screenshot_to_step(store, "run-1", "first-impression-critic", 99, "fake-oid-x")
    # No exception, no row created.
    assert list_steps_for_persona(store, "run-1", "first-impression-critic") == []


def test_attach_finding_to_step_adds_ordinal(store):
    record_step(store, "run-1", "first-impression-critic", 1, tool_name="note_finding")
    attach_finding_to_step(store, "run-1", "first-impression-critic", 1, 1)
    attach_finding_to_step(store, "run-1", "first-impression-critic", 1, 2)
    steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
    assert steps[0]["finding_ordinals"] == [1, 2]


def test_attach_finding_to_step_dedups_via_addToSet(store):
    """Repeated attachment of the same ordinal must not duplicate."""
    record_step(store, "run-1", "first-impression-critic", 1, tool_name="note_finding")
    attach_finding_to_step(store, "run-1", "first-impression-critic", 1, 1)
    attach_finding_to_step(store, "run-1", "first-impression-critic", 1, 1)
    steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
    assert steps[0]["finding_ordinals"] == [1]


# ---------------------------------------------------------------------------
# #1115 follow-up — live-streaming finding writer tests.
# ---------------------------------------------------------------------------
class TestUpsertLiveFinding:
    """``upsert_live_finding`` is the per-note_finding writer that lets
    the run-detail UI see findings appear seconds after each tool call
    instead of in waves at persona-end. See the schema module's
    docstring above ``upsert_live_finding`` for the contract."""

    def test_eager_creates_the_run_doc(self, store):
        from qa_store.schema import upsert_live_finding
        assert store.runs.find_one({"run_id": "qa-live-1"}) is None
        upsert_live_finding(
            store, "qa-live-1", "first-impression-critic", 1,
            {"category": "bug", "severity": "minor", "title": "T", "body": "B"},
        )
        run = store.runs.find_one({"run_id": "qa-live-1"})
        assert run is not None
        assert run["personas"] == ["first-impression-critic"]
        assert run["status"] == "new"

    def test_writes_finding_with_deterministic_id(self, store):
        from qa_store.schema import upsert_live_finding
        result = upsert_live_finding(
            store, "qa-live-2", "first-impression-critic", 1,
            {
                "kind": "praise",
                "category": "surprise",
                "severity": "minor",
                "title": "Pricing clarity",
                "body": "really clear",
            },
        )
        # Same id shape used by add_persona_result, so the two paths
        # converge on the same doc.
        assert result["finding_id"] == finding_id(
            "qa-live-2", "first-impression-critic", 1,
        )
        assert result["kind"] == "praise"
        assert result["title"] == "Pricing clarity"
        # Fresh insert leaves gh_issue_* at None — what add_persona_result
        # expects to find when reconciling.
        assert result["gh_issue_url"] is None
        assert result["gh_issue_number"] is None

    def test_repeated_calls_refresh_live_fields_not_dedup_metadata(self, store):
        """Persona may correct their own finding mid-run by re-calling
        note_finding with the same ordinal (e.g. sharper wording). Live
        fields refresh; created_at + recurring_count stay put so the
        Slice-2.0 dedup ladder isn't perturbed."""
        from qa_store.schema import upsert_live_finding
        upsert_live_finding(
            store, "qa-live-3", "first-impression-critic", 1,
            {"category": "bug", "severity": "minor", "title": "v1", "body": ""},
        )
        first = store.findings.find_one({
            "finding_id": finding_id("qa-live-3", "first-impression-critic", 1),
        })
        upsert_live_finding(
            store, "qa-live-3", "first-impression-critic", 1,
            {"category": "bug", "severity": "blocker", "title": "v2 — sharper", "body": "B"},
        )
        second = store.findings.find_one({
            "finding_id": finding_id("qa-live-3", "first-impression-critic", 1),
        })
        # Live fields rewrote.
        assert second["title"] == "v2 — sharper"
        assert second["severity"] == "blocker"
        assert second["body"] == "B"
        # $setOnInsert fields preserved across the refresh.
        assert second["created_at"] == first["created_at"]
        assert second["recurring_count"] == 1
        assert second["last_verified_run_id"] == "qa-live-3"

    def test_persona_end_reconciliation_preserves_mid_run_gh_issue(self, store):
        """The critical end-to-end: persona files a finding live, operator
        clicks "File issue" on it mid-run (gh_issue_url populated), then
        the persona finishes and add_persona_result runs. The filing must
        survive — pre-this-slice the delete_many + replace_one wiped it."""
        from qa_store.schema import upsert_live_finding
        upsert_live_finding(
            store, "qa-live-4", "first-impression-critic", 1,
            {"category": "bug", "severity": "blocker", "title": "broken thing", "body": ""},
        )
        upsert_live_finding(
            store, "qa-live-4", "first-impression-critic", 2,
            {"category": "bug", "severity": "minor", "title": "small thing", "body": ""},
        )
        fid1 = finding_id("qa-live-4", "first-impression-critic", 1)
        store.findings.update_one(
            {"finding_id": fid1},
            {"$set": {
                "gh_issue_url": "https://github.com/mccullya/slyreply/issues/9999",
                "gh_issue_number": 9999,
            }},
        )

        add_persona_result(
            store, "qa-live-4", "first-impression-critic",
            review_markdown="## Verdict\n\npass",
            verdict="pass",
            accounting={
                "total_input_tokens": 0, "total_output_tokens": 0,
                "total_cache_tokens": 0, "total_cost_usd": 0.0,
                "cost_is_estimated": False,
            },
            findings=[
                {"category": "bug", "severity": "blocker", "title": "broken thing", "body": ""},
                {"category": "bug", "severity": "minor", "title": "small thing", "body": ""},
            ],
        )

        f1 = store.findings.find_one({"finding_id": fid1})
        assert f1["gh_issue_url"] == "https://github.com/mccullya/slyreply/issues/9999"
        assert f1["gh_issue_number"] == 9999

    def test_orphan_without_gh_issue_is_deleted_on_reconcile(self, store):
        """A live-written ordinal not in the final list (e.g. shorter
        re-run) gets cleaned up — UNLESS it carries a GH issue."""
        from qa_store.schema import upsert_live_finding
        upsert_live_finding(
            store, "qa-live-5", "first-impression-critic", 1,
            {"category": "bug", "severity": "minor", "title": "keep me", "body": ""},
        )
        upsert_live_finding(
            store, "qa-live-5", "first-impression-critic", 2,
            {"category": "bug", "severity": "minor", "title": "ditch me", "body": ""},
        )
        add_persona_result(
            store, "qa-live-5", "first-impression-critic",
            review_markdown="## Verdict\n\npass",
            verdict="pass",
            accounting={
                "total_input_tokens": 0, "total_output_tokens": 0,
                "total_cache_tokens": 0, "total_cost_usd": 0.0,
                "cost_is_estimated": False,
            },
            findings=[
                {"category": "bug", "severity": "minor", "title": "keep me", "body": ""},
            ],
        )
        assert store.findings.find_one({
            "finding_id": finding_id("qa-live-5", "first-impression-critic", 1),
        }) is not None
        assert store.findings.find_one({
            "finding_id": finding_id("qa-live-5", "first-impression-critic", 2),
        }) is None

    def test_orphan_with_gh_issue_is_preserved(self, store):
        """The other half: a live orphan with a filed issue stays put on
        reconciliation. Mid-run filing must outlive a shorter re-run."""
        from qa_store.schema import upsert_live_finding
        upsert_live_finding(
            store, "qa-live-6", "first-impression-critic", 1,
            {"category": "bug", "severity": "minor", "title": "kept", "body": ""},
        )
        upsert_live_finding(
            store, "qa-live-6", "first-impression-critic", 2,
            {"category": "bug", "severity": "minor", "title": "filed-orphan", "body": ""},
        )
        # Ordinal 2 gets a GH issue mid-run.
        fid2 = finding_id("qa-live-6", "first-impression-critic", 2)
        store.findings.update_one(
            {"finding_id": fid2},
            {"$set": {
                "gh_issue_url": "https://github.com/mccullya/slyreply/issues/8888",
                "gh_issue_number": 8888,
            }},
        )
        # Re-run with a shorter list — ordinal 2 is now an orphan.
        add_persona_result(
            store, "qa-live-6", "first-impression-critic",
            review_markdown="## Verdict\n\npass",
            verdict="pass",
            accounting={
                "total_input_tokens": 0, "total_output_tokens": 0,
                "total_cache_tokens": 0, "total_cost_usd": 0.0,
                "cost_is_estimated": False,
            },
            findings=[
                {"category": "bug", "severity": "minor", "title": "kept", "body": ""},
            ],
        )
        # Both findings still exist; the filed orphan was NOT deleted.
        assert store.findings.find_one({"finding_id": fid2}) is not None
        assert store.findings.find_one({"finding_id": fid2})["gh_issue_url"] == (
            "https://github.com/mccullya/slyreply/issues/8888"
        )


# ---------------------------------------------------------------------------
# #1821 — multi-pod finish barrier (expected_personas + claim_run_finish +
# all_personas_reviewed).
# ---------------------------------------------------------------------------
def _review(store, run_id, persona):
    """Minimal helper: file an empty-but-valid review for one persona."""
    add_persona_result(
        store, run_id, persona,
        review_markdown="ok", verdict="pass", accounting={}, findings=[],
    )


def test_create_run_persists_expected_personas(store):
    run = create_run(
        store, "run-1", ["first-impression-critic"],
        expected_personas=["first-impression-critic", "desktop-evaluator", "upgrade-buyer"],
    )
    assert run["expected_personas"] == [
        "first-impression-critic", "desktop-evaluator", "upgrade-buyer",
    ]
    # finish_claimed starts False — nobody has finalised yet.
    assert run["finish_claimed"] is False


def test_create_run_expected_personas_defaults_to_personas_arg(store):
    # Single-pod / legacy callers: no expected_personas → denominator is the
    # full personas list passed on the one and only create_run call.
    run = create_run(store, "run-1", ["first-impression-critic", "desktop-evaluator"])
    assert run["expected_personas"] == ["first-impression-critic", "desktop-evaluator"]


def test_create_run_expected_personas_is_sticky_across_reupsert(store):
    """Unlike the incremental ``personas`` list, expected_personas is set ONCE.

    A later create_run re-upsert (e.g. a per-pod add_persona_result
    auto-create, or a second pod's create_run carrying its own narrower
    view) must NOT clobber or grow the denominator the first writer set.
    """
    create_run(
        store, "run-1", ["first-impression-critic"],
        expected_personas=["first-impression-critic", "desktop-evaluator", "upgrade-buyer"],
    )
    # Re-upsert with a DIFFERENT (and smaller) expected set — must be ignored.
    create_run(
        store, "run-1", ["desktop-evaluator"],
        expected_personas=["desktop-evaluator"],
    )
    # And a re-upsert that omits it entirely (the common per-pod path).
    create_run(store, "run-1", ["upgrade-buyer"])
    run = get_run(store, "run-1")
    assert run["expected_personas"] == [
        "first-impression-critic", "desktop-evaluator", "upgrade-buyer",
    ]
    # The incremental personas list, by contrast, DID union-merge.
    assert run["personas"] == [
        "first-impression-critic", "desktop-evaluator", "upgrade-buyer",
    ]


def test_claim_run_finish_exactly_one_winner_across_concurrent_callers(store):
    """The atomic compare-and-set: of N callers, exactly one wins."""
    create_run(store, "run-1", ["a", "b"], expected_personas=["a", "b"])
    # Two "concurrent" claims — mongomock serialises them, but the
    # compare-and-set guarantees the second matches zero docs.
    first = claim_run_finish(store, "run-1")
    second = claim_run_finish(store, "run-1")
    assert [first, second].count(True) == 1
    assert first is True
    assert second is False
    # The flag is durably set.
    assert store.runs.find_one({"run_id": "run-1"})["finish_claimed"] is True


def test_claim_run_finish_unknown_run_does_not_win(store):
    assert claim_run_finish(store, "nope") is False


def test_all_personas_reviewed_false_on_partial(store):
    create_run(
        store, "run-1", ["a", "b", "c"],
        expected_personas=["a", "b", "c"],
    )
    _review(store, "run-1", "a")
    _review(store, "run-1", "b")
    # c hasn't filed yet → barrier stays shut.
    assert all_personas_reviewed(store, "run-1") is False


def test_all_personas_reviewed_true_when_complete(store):
    create_run(
        store, "run-1", ["a", "b", "c"],
        expected_personas=["a", "b", "c"],
    )
    _review(store, "run-1", "a")
    _review(store, "run-1", "b")
    _review(store, "run-1", "c")
    assert all_personas_reviewed(store, "run-1") is True


def test_all_personas_reviewed_true_with_extra_unexpected_reviewer(store):
    # Superset, not equality: an unexpected extra reviewer never blocks.
    create_run(store, "run-1", ["a", "b"], expected_personas=["a", "b"])
    _review(store, "run-1", "a")
    _review(store, "run-1", "b")
    _review(store, "run-1", "surprise-persona")
    assert all_personas_reviewed(store, "run-1") is True


def test_all_personas_reviewed_unknown_run_is_false(store):
    assert all_personas_reviewed(store, "nope") is False
