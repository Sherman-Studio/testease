"""Tests for the discovered_* CRUD + distillation module (Slice 1 of #1002).

Two tiers:
  * TestDiscoveredCRUD — pure pymongo writes/reads against mongomock.
    Exercises the upsert+list helpers for actions/tools/branches plus
    the clear-for-persona-run sweep.
  * TestDistillation — the orchestration helper that fetches logs,
    calls Anthropic (injected fake here), and writes through to the
    three collections. Exercises the kill switch, the no-logs case,
    the malformed-row defensiveness, and the idempotency contract.
"""

from __future__ import annotations

import mongomock
import pytest

from qa_store.distillation import (
    distill_persona_run,
    format_transcript,
    is_enabled,
)
from qa_store.schema import (
    Store,
    append_run_log,
    clear_discovered_for_persona_run,
    list_discovered_actions,
    list_discovered_branches,
    list_discovered_tools,
    upsert_discovered_action,
    upsert_discovered_branch,
    upsert_discovered_tool,
)


@pytest.fixture
def store() -> Store:
    client = mongomock.MongoClient()
    s = Store(client=client, db_name="slyreply_qa_test")
    # Mirror the production unique indexes — the upsert tests don't
    # depend on these, but they catch any future regression that
    # tries to insert duplicate doc_ids.
    s.discovered_actions.create_index("doc_id", unique=True)
    s.discovered_tools.create_index("doc_id", unique=True)
    s.discovered_branches.create_index("doc_id", unique=True)
    return s


# ---------------------------------------------------------------------------
# CRUD — upsert + list + clear
# ---------------------------------------------------------------------------
class TestDiscoveredCRUD:
    def test_upsert_action_inserts(self, store):
        upsert_discovered_action(
            store,
            run_id="run-1", persona_id="first-impression-critic",
            action_id="auth.signup", category="auth",
            human_description="Sign up for a new account",
            url_seen="/signup",
            evidence="User clicked Create account",
            branches_noticed=["Resend code not tried"],
        )
        rows = list_discovered_actions(store)
        assert len(rows) == 1
        assert rows[0]["action_id"] == "auth.signup"
        assert rows[0]["branches_noticed"] == ["Resend code not tried"]
        assert rows[0]["doc_id"] == "run-1:first-impression-critic:auth.signup"
        assert rows[0]["source"] == "distilled-v1"

    def test_upsert_action_replaces_in_place(self, store):
        """Re-distillation overwrites — same doc_id, no duplicate row."""
        for desc in ("first", "second"):
            upsert_discovered_action(
                store,
                run_id="run-1", persona_id="first-impression-critic",
                action_id="auth.signup", category="auth",
                human_description=desc,
            )
        rows = list_discovered_actions(store)
        assert len(rows) == 1
        assert rows[0]["human_description"] == "second"

    def test_upsert_action_unknown_category_buckets_to_other(self, store):
        """Forward-compat: future model emits a new category → 'other'."""
        upsert_discovered_action(
            store,
            run_id="run-1", persona_id="first-impression-critic",
            action_id="x.y", category="not-a-real-category",
            human_description="hi",
        )
        assert list_discovered_actions(store)[0]["category"] == "other"

    def test_list_actions_filters_by_run(self, store):
        upsert_discovered_action(
            store, run_id="run-1", persona_id="first-impression-critic",
            action_id="a.b", category="auth", human_description="x",
        )
        upsert_discovered_action(
            store, run_id="run-2", persona_id="first-impression-critic",
            action_id="a.c", category="auth", human_description="y",
        )
        assert len(list_discovered_actions(store, run_id="run-1")) == 1
        assert len(list_discovered_actions(store, run_id="run-2")) == 1
        assert len(list_discovered_actions(store)) == 2

    def test_list_actions_filters_by_persona(self, store):
        upsert_discovered_action(
            store, run_id="run-1", persona_id="first-impression-critic",
            action_id="a.b", category="auth", human_description="x",
        )
        upsert_discovered_action(
            store, run_id="run-1", persona_id="desktop-evaluator",
            action_id="a.c", category="auth", human_description="y",
        )
        rows = list_discovered_actions(store, persona_id="first-impression-critic")
        assert len(rows) == 1
        assert rows[0]["persona_id"] == "first-impression-critic"

    def test_list_actions_filters_by_category(self, store):
        for cat in ("auth", "billing"):
            upsert_discovered_action(
                store, run_id="run-1", persona_id="first-impression-critic",
                action_id=f"{cat}.x", category=cat, human_description="x",
            )
        assert len(list_discovered_actions(store, category="auth")) == 1
        assert len(list_discovered_actions(store, category="billing")) == 1

    def test_list_actions_limit_clamped(self, store):
        for i in range(20):
            upsert_discovered_action(
                store, run_id="run-1", persona_id="first-impression-critic",
                action_id=f"a.x{i}", category="auth", human_description="x",
            )
        assert len(list_discovered_actions(store, limit=5)) == 5
        # Lower bound clamp: <=0 becomes 1
        assert len(list_discovered_actions(store, limit=0)) == 1

    def test_upsert_tool_and_list(self, store):
        upsert_discovered_tool(
            store, run_id="run-1", persona_id="first-impression-critic",
            name="mailpit", purpose="check verification email",
        )
        rows = list_discovered_tools(store)
        assert len(rows) == 1
        assert rows[0]["name"] == "mailpit"
        assert rows[0]["doc_id"] == "run-1:first-impression-critic:mailpit"

    def test_upsert_branch_and_list(self, store):
        for i, desc in enumerate(["b-one", "b-two"], start=1):
            upsert_discovered_branch(
                store, run_id="run-1", persona_id="first-impression-critic",
                ordinal=i, description=desc,
            )
        rows = list_discovered_branches(store)
        assert len(rows) == 2
        # Round-trip preserves both ordinals + descriptions. The
        # (distilled_at DESC, ordinal ASC) sort is exercised by the
        # API test_branches_preserves_ordinal_order which seeds inside
        # one route call so timestamps collide deterministically.
        assert {r["ordinal"] for r in rows} == {1, 2}
        assert {r["description"] for r in rows} == {"b-one", "b-two"}

    def test_clear_removes_only_targeted_pair(self, store):
        """The clear-before-write contract — re-distillation wipes the
        target persona-run's rows without touching anyone else's."""
        upsert_discovered_action(
            store, run_id="run-1", persona_id="first-impression-critic",
            action_id="a.x", category="auth", human_description="x",
        )
        upsert_discovered_tool(
            store, run_id="run-1", persona_id="first-impression-critic",
            name="t1",
        )
        upsert_discovered_branch(
            store, run_id="run-1", persona_id="first-impression-critic",
            ordinal=1, description="b",
        )
        # Sibling persona in same run — must survive the clear
        upsert_discovered_action(
            store, run_id="run-1", persona_id="desktop-evaluator",
            action_id="a.y", category="auth", human_description="y",
        )
        # Same persona in a different run — must survive too
        upsert_discovered_action(
            store, run_id="run-2", persona_id="first-impression-critic",
            action_id="a.z", category="auth", human_description="z",
        )

        counts = clear_discovered_for_persona_run(store, "run-1", "first-impression-critic")
        assert counts == {"actions": 1, "tools": 1, "branches": 1}

        remaining_actions = list_discovered_actions(store)
        assert len(remaining_actions) == 2
        assert {r["persona_id"] for r in remaining_actions} == {"desktop-evaluator", "first-impression-critic"}
        assert {r["run_id"] for r in remaining_actions} == {"run-1", "run-2"}


# ---------------------------------------------------------------------------
# Distillation runner
# ---------------------------------------------------------------------------

# Canned model output — the shape the prompt produces (proved in the spike).
_FAKE_HAIKU_RESULT = {
    "discovered_actions": [
        {
            "action_id": "auth.signup",
            "category": "auth",
            "human_description": "Sign up for a new account",
            "url_seen": "/signup",
            "evidence": "Persona filled signup form",
            "branches_noticed": [
                "Decline T&C checkbox not tried",
                "Pro tier upgrade not tried",
            ],
        },
        {
            "action_id": "billing.upgrade",
            "category": "billing",
            "human_description": "Upgrade to a paid plan via Revolut",
            "url_seen": "/billing",
            "evidence": "Persona clicked Upgrade to Pro",
            "branches_noticed": ["Power tier upgrade not tried"],
        },
    ],
    "tools_used": [
        {"name": "mailpit", "purpose": "Verify signup email arrived"},
    ],
    "unexplored_branches": [
        "Persona saw a Cancel button but didn't click it",
        "Persona saw a Reset password link but didn't try it",
    ],
}


def _fake_call(_transcript: str) -> dict:
    """A drop-in for the real Anthropic call — returns the canned result
    deterministically. Use via ``call_anthropic=_fake_call`` injection."""
    return _FAKE_HAIKU_RESULT


def _seed_logs(store: Store, run_id: str, persona_id: str, n: int = 3) -> None:
    """Drop some qa_run_logs rows the distillation can hand to the model."""
    for seq in range(1, n + 1):
        append_run_log(
            store, run_id=run_id, persona_id=persona_id,
            seq=seq, kind="explore",
            content=f"step {seq} narration",
        )


class TestDistillation:
    def test_writes_all_three_collections(self, store):
        _seed_logs(store, "run-1", "first-impression-critic")
        counts = distill_persona_run(
            store, "run-1", "first-impression-critic", call_anthropic=_fake_call,
        )
        assert counts == {"actions": 2, "tools": 1, "branches": 2}
        assert len(list_discovered_actions(store)) == 2
        assert len(list_discovered_tools(store)) == 1
        assert len(list_discovered_branches(store)) == 2

    def test_idempotent_clear_before_write(self, store):
        """Re-distillation clears the old batch first — no stale rows."""
        _seed_logs(store, "run-1", "first-impression-critic")
        distill_persona_run(store, "run-1", "first-impression-critic", call_anthropic=_fake_call)

        # Mutate the canned output: same call returns different actions.
        mutated = {
            "discovered_actions": [
                {
                    "action_id": "auth.login",
                    "category": "auth",
                    "human_description": "Sign in",
                },
            ],
            "tools_used": [],
            "unexplored_branches": [],
        }
        distill_persona_run(
            store, "run-1", "first-impression-critic",
            call_anthropic=lambda _: mutated,
        )

        actions = list_discovered_actions(store)
        # Old auth.signup + billing.upgrade are gone; only the new one remains.
        assert len(actions) == 1
        assert actions[0]["action_id"] == "auth.login"
        # Tools + branches got cleared too — second run had none.
        assert list_discovered_tools(store) == []
        assert list_discovered_branches(store) == []

    def test_no_logs_returns_zero_counts(self, store):
        """A persona-run pair with no qa_run_logs (pre-#903 run, or a
        persona that never emitted) is a valid empty case, not an error."""
        counts = distill_persona_run(
            store, "run-empty", "first-impression-critic", call_anthropic=_fake_call,
        )
        assert counts == {"actions": 0, "tools": 0, "branches": 0}
        assert list_discovered_actions(store) == []

    def test_kill_switch_short_circuits(self, store, monkeypatch):
        """QA_DISTILLATION_ENABLED=0 → no model call, no writes."""
        _seed_logs(store, "run-1", "first-impression-critic")
        monkeypatch.setenv("QA_DISTILLATION_ENABLED", "0")

        # If the fake gets called the test still passes (returns canned
        # result), so make absolutely sure it isn't — assert by inspecting
        # writes after.
        calls = []
        def _spy(_t):
            calls.append(1)
            return _FAKE_HAIKU_RESULT
        counts = distill_persona_run(
            store, "run-1", "first-impression-critic", call_anthropic=_spy,
        )
        assert counts == {"actions": 0, "tools": 0, "branches": 0}
        assert calls == []
        assert list_discovered_actions(store) == []

    def test_kill_switch_default_on(self, monkeypatch):
        """No env var set → enabled."""
        monkeypatch.delenv("QA_DISTILLATION_ENABLED", raising=False)
        assert is_enabled() is True

    @pytest.mark.parametrize("falsey", ["0", "false", "False"])
    def test_kill_switch_recognises_falsy_values(self, monkeypatch, falsey):
        monkeypatch.setenv("QA_DISTILLATION_ENABLED", falsey)
        assert is_enabled() is False

    def test_malformed_action_is_skipped_not_fatal(self, store):
        """One bad row shouldn't take the whole batch down."""
        _seed_logs(store, "run-1", "first-impression-critic")
        bad_then_good = {
            "discovered_actions": [
                {"category": "auth", "human_description": "missing action_id"},
                {"action_id": "auth.ok", "category": "auth", "human_description": "ok"},
            ],
            "tools_used": [],
            "unexplored_branches": [],
        }
        distill_persona_run(
            store, "run-1", "first-impression-critic",
            call_anthropic=lambda _: bad_then_good,
        )
        rows = list_discovered_actions(store)
        # The good row went through, the malformed one was skipped.
        assert len(rows) == 1
        assert rows[0]["action_id"] == "auth.ok"

    def test_model_call_failure_is_non_fatal(self, store):
        """A model error returns zero counts rather than raising."""
        _seed_logs(store, "run-1", "first-impression-critic")
        def _raises(_t):
            raise RuntimeError("simulated model 500")
        counts = distill_persona_run(
            store, "run-1", "first-impression-critic", call_anthropic=_raises,
        )
        assert counts == {"actions": 0, "tools": 0, "branches": 0}


# ---------------------------------------------------------------------------
# format_transcript
# ---------------------------------------------------------------------------
class TestFormatTranscript:
    def test_drops_empty_content_rows(self):
        logs = [
            {"ts": "2026-05-25T12:00:00Z", "kind": "explore", "content": "first"},
            {"ts": "2026-05-25T12:00:01Z", "kind": "explore", "content": ""},
            {"ts": "2026-05-25T12:00:02Z", "kind": "explore", "content": None},
            {"ts": "2026-05-25T12:00:03Z", "kind": "explore", "content": "second"},
        ]
        out = format_transcript(logs)
        assert "first" in out
        assert "second" in out
        # The empty rows didn't add stray bracketed lines
        assert out.count("\n") == 1

    def test_renders_datetime_ts_as_isoformat(self):
        from datetime import UTC, datetime
        logs = [
            {"ts": datetime(2026, 5, 25, 12, 0, tzinfo=UTC),
             "kind": "explore", "content": "hi"},
        ]
        out = format_transcript(logs)
        assert "2026-05-25T12:00:00" in out

    def test_truncates_to_tail_when_over_cap(self):
        # Force enormous input; head should be dropped, tail kept.
        from qa_store.distillation import _MAX_TRANSCRIPT_BYTES

        many = [
            {"ts": "2026-05-25T12:00:00Z", "kind": "explore",
             "content": f"line {i}: " + "x" * 200}
            for i in range(_MAX_TRANSCRIPT_BYTES // 100)
        ]
        out = format_transcript(many)
        assert out.startswith("[... transcript truncated")
        assert len(out) <= _MAX_TRANSCRIPT_BYTES + 200  # the truncation banner adds a bit
