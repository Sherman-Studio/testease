"""Tests for the per-persona step recorder (#860).

Substitutes a minimal in-memory ``Store`` + ``FakeBucket`` so the recorder's
write paths are exercised without a real Mongo / Atlas connection. The
SDK message types are duck-typed via ``SimpleNamespace`` — the recorder
only reads attributes, so a namespace stand-in is faithful enough.

For end-to-end recorder ↔ live SDK coverage, the harness writes on every
real run and the Transcript tab renders the result; a unit fake catches
the structural bugs (off-by-one step_n, missed text-buffer drain, etc.)
that would otherwise corrupt the transcript silently.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from bson import ObjectId
from qa_store import screenshots
from qa_store.schema import Store, list_steps_for_persona

from qa_agents.run_recorder import RunRecorder


@dataclass
class _FakeBucket:
    files: dict = field(default_factory=dict)

    def upload_from_stream(self, filename, data, metadata=None):
        oid = ObjectId()
        self.files[oid] = {"filename": filename, "data": data,
                           "metadata": metadata or {}}
        return oid


@pytest.fixture
def store(monkeypatch) -> Store:
    """Mongomock-backed Store with the GridFS bucket factory stubbed."""
    import mongomock
    client = mongomock.MongoClient()
    s = Store(client=client, db_name="slyreply_qa_test")
    s.steps.create_index(
        [("run_id", 1), ("persona_id", 1), ("step_n", 1)], unique=True
    )
    fake = _FakeBucket()
    monkeypatch.setattr(screenshots, "_bucket", lambda _store: fake)
    s._fake_bucket = fake  # type: ignore[attr-defined]
    return s


def _tool_use(name: str, input_args: dict, tool_id: str | None = None):
    """SDK-shaped ToolUseBlock stand-in."""
    return SimpleNamespace(
        name=name, input=input_args, id=tool_id, text=None,
    )


def _text(text: str):
    return SimpleNamespace(name=None, input=None, text=text)


def _tool_result(tool_use_id: str, content: list):
    """SDK-shaped ToolResultBlock stand-in (lives inside a UserMessage)."""
    return SimpleNamespace(
        type="tool_result", tool_use_id=tool_use_id, content=content,
    )


def _image_block(data_b64: str) -> dict:
    """Playwright-MCP-shaped image content block."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": data_b64,
        },
    }


# ---------------------------------------------------------------------------
# step recording
# ---------------------------------------------------------------------------
class TestStepRecording:
    def test_records_one_step_per_tool_use(self, store):
        r = RunRecorder(store, "run-1", "first-impression-critic")
        r.on_assistant_message([
            _tool_use("mcp__playwright__browser_navigate",
                      {"url": "http://frontend/"}, tool_id="t1"),
        ])
        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        assert len(steps) == 1
        assert steps[0]["tool_name"] == "mcp__playwright__browser_navigate"
        assert steps[0]["step_n"] == 1

    def test_step_n_increments_per_tool_use_across_messages(self, store):
        r = RunRecorder(store, "run-1", "first-impression-critic")
        r.on_assistant_message([_tool_use("a", {}, tool_id="t1")])
        r.on_assistant_message([_tool_use("b", {}, tool_id="t2")])
        r.on_assistant_message([
            _tool_use("c", {}, tool_id="t3"),
            _tool_use("d", {}, tool_id="t4"),
        ])
        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        assert [s["step_n"] for s in steps] == [1, 2, 3, 4]
        assert [s["tool_name"] for s in steps] == ["a", "b", "c", "d"]

    def test_text_buffer_drains_into_next_step(self, store):
        r = RunRecorder(store, "run-1", "first-impression-critic")
        r.on_assistant_message([
            _text("I'm going to open the homepage to see what this is."),
            _text("Looks like a normal site."),
            _tool_use("mcp__playwright__browser_navigate",
                      {"url": "http://frontend/"}, tool_id="t1"),
        ])
        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        text = steps[0]["text_from_persona"]
        assert "I'm going to open the homepage" in text
        assert "normal site" in text

    def test_text_drains_only_into_first_subsequent_tool(self, store):
        """The buffered prose should belong to the NEXT step, not all
        following steps. Prevents a "stuck preamble" pattern where the
        first sentence appears on every tool until a new text block
        arrives."""
        r = RunRecorder(store, "run-1", "first-impression-critic")
        r.on_assistant_message([
            _text("Opening the homepage."),
            _tool_use("nav", {"url": "/"}, tool_id="t1"),
            _tool_use("click", {"ref": "x"}, tool_id="t2"),
        ])
        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        assert "Opening" in steps[0]["text_from_persona"]
        assert steps[1]["text_from_persona"] == ""

    def test_summarize_args_callback_runs(self, store):
        r = RunRecorder(
            store, "run-1", "first-impression-critic",
            summarize_args=lambda name, args: f"args_for_{name}",
        )
        r.on_assistant_message([
            _tool_use("nav", {"url": "/"}, tool_id="t1"),
        ])
        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        assert steps[0]["args_summary"] == "args_for_nav"


# ---------------------------------------------------------------------------
# screenshot capture
# ---------------------------------------------------------------------------
class TestScreenshotCapture:
    def test_screenshot_is_stored_and_linked_to_step(self, store):
        r = RunRecorder(store, "run-1", "first-impression-critic")
        png_b64 = base64.b64encode(b"PNG_BYTES_HERE").decode()
        r.on_assistant_message([
            _tool_use("mcp__playwright__browser_take_screenshot", {},
                      tool_id="screenshot-1"),
        ])
        r.on_user_message([
            _tool_result("screenshot-1", [_image_block(png_b64)]),
        ])
        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        assert len(steps) == 1
        assert steps[0]["screenshot_id"] is not None
        # The bytes round-tripped via the fake bucket.
        stored = list(store._fake_bucket.files.values())  # type: ignore[attr-defined]
        assert len(stored) == 1
        assert stored[0]["data"] == b"PNG_BYTES_HERE"

    def test_unrelated_tool_result_does_not_create_screenshot(self, store):
        r = RunRecorder(store, "run-1", "first-impression-critic")
        r.on_assistant_message([
            _tool_use("mcp__playwright__browser_navigate",
                      {"url": "/"}, tool_id="nav-1"),
        ])
        r.on_user_message([
            _tool_result("nav-1", [{"type": "text", "text": "ok"}]),
        ])
        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        assert steps[0]["screenshot_id"] is None
        assert store._fake_bucket.files == {}  # type: ignore[attr-defined]

    def test_screenshot_missing_image_block_is_quiet(self, store):
        """If the Playwright MCP response doesn't carry an image block
        (rare but possible — error path?), we don't crash and we don't
        attach a non-existent oid."""
        r = RunRecorder(store, "run-1", "first-impression-critic")
        r.on_assistant_message([
            _tool_use("mcp__playwright__browser_take_screenshot", {},
                      tool_id="ss-broken"),
        ])
        r.on_user_message([
            _tool_result("ss-broken", [
                {"type": "text", "text": "screenshot failed: page closed"},
            ]),
        ])
        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        assert steps[0]["screenshot_id"] is None

    def test_screenshot_tolerates_flat_data_shape(self, store):
        """Some MCP transports flatten the image block — data at top level
        rather than nested under source. Tolerate both."""
        r = RunRecorder(store, "run-1", "first-impression-critic")
        png_b64 = base64.b64encode(b"PNG2").decode()
        r.on_assistant_message([
            _tool_use("mcp__playwright__browser_take_screenshot", {},
                      tool_id="ss-flat"),
        ])
        r.on_user_message([
            _tool_result("ss-flat", [{"type": "image", "data": png_b64}]),
        ])
        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        assert steps[0]["screenshot_id"] is not None


# ---------------------------------------------------------------------------
# Findings.add wrap — finding ↔ step linkback
# ---------------------------------------------------------------------------
class TestFindingLinkback:
    def test_note_finding_step_gets_finding_ordinal(self, store):
        from qa_agents.tools.findings import Findings

        findings = Findings()
        r = RunRecorder(store, "run-1", "first-impression-critic", findings=findings)
        # Persona issues a note_finding tool call.
        r.on_assistant_message([
            _tool_use("mcp__findings__note_finding",
                      {"category": "confusion", "severity": "minor",
                       "title": "What is a UID?", "body": "unclear"},
                      tool_id="nf-1"),
        ])
        # Simulate the SDK invoking the tool body: it calls findings.add(...)
        # synchronously between SDK messages. The wrapped add records the
        # backlink.
        findings.add(category="confusion", severity="minor",
                     title="What is a UID?", body="unclear")

        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        assert len(steps) == 1
        assert steps[0]["tool_name"] == "mcp__findings__note_finding"
        # Ordinal is 1-based (matches qa_store.finding_id semantics).
        assert steps[0]["finding_ordinals"] == [1]

    def test_non_finding_tools_do_not_leak_into_finding_ordinals(self, store):
        """A subsequent non-finding tool must NOT accidentally have a
        finding ordinal attached (current_finding_step must reset)."""
        from qa_agents.tools.findings import Findings

        findings = Findings()
        r = RunRecorder(store, "run-1", "first-impression-critic", findings=findings)
        r.on_assistant_message([
            _tool_use("mcp__findings__note_finding",
                      {"category": "bug", "severity": "minor",
                       "title": "x", "body": ""}, tool_id="nf-1"),
            _tool_use("mcp__playwright__browser_navigate",
                      {"url": "/"}, tool_id="nav-1"),
        ])
        # Tool body runs once per ToolUseBlock; only the note_finding one
        # calls findings.add.
        findings.add(category="bug", severity="minor", title="x", body="")

        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        assert steps[0]["finding_ordinals"] == [1]  # note_finding step
        assert steps[1]["finding_ordinals"] == []   # browser_navigate step

    def test_wrap_is_idempotent(self, store):
        """Re-instantiating the recorder against the same Findings (e.g.
        in a test fixture re-use) must not double-wrap and double-record."""
        from qa_agents.tools.findings import Findings

        findings = Findings()
        RunRecorder(store, "run-1", "first-impression-critic", findings=findings)
        RunRecorder(store, "run-1", "first-impression-critic", findings=findings)
        # Calling add should still only attach ONCE to the current step.
        # No current step set (no note_finding ToolUseBlock processed),
        # so the wrap is a no-op — but it must NOT raise.
        findings.add(category="bug", severity="minor", title="t", body="")
        # Verify the wrap is still in place by introspecting the marker.
        assert getattr(findings.add, "__qa_recorder_wrapped__", False)


# ---------------------------------------------------------------------------
# Safety — recorder errors must NOT crash the persona's run.
# ---------------------------------------------------------------------------
class TestSafetyContract:
    def test_record_step_swallows_store_exceptions(self, monkeypatch, store):
        """A flaky Mongo connection during step write should log + drop,
        not raise — losing one transcript row is preferable to crashing
        a 90-minute persona run."""
        from qa_agents import run_recorder

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated Mongo timeout")

        monkeypatch.setattr(run_recorder, "record_step", _boom)

        r = RunRecorder(store, "run-1", "first-impression-critic")
        # Must not raise.
        r.on_assistant_message([_tool_use("nav", {}, tool_id="t1")])

    def test_screenshot_store_failure_swallowed(self, monkeypatch, store):
        from qa_agents import run_recorder

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated GridFS write timeout")

        monkeypatch.setattr(run_recorder, "store_screenshot", _boom)

        r = RunRecorder(store, "run-1", "first-impression-critic")
        png_b64 = base64.b64encode(b"x").decode()
        r.on_assistant_message([
            _tool_use("mcp__playwright__browser_take_screenshot", {},
                      tool_id="ss-1"),
        ])
        # Must not raise even though store_screenshot fails.
        r.on_user_message([
            _tool_result("ss-1", [_image_block(png_b64)]),
        ])
        # Step record exists (it was written before the screenshot tried),
        # screenshot_id stays None.
        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        assert steps[0]["screenshot_id"] is None


# ─────────────────────────────────────────────────────────────────────
# #902 / #903 — qa_run_logs narrative archive. log_emit() is the new
# public method; _record_tool_use() now ALSO appends a tool_use row.
# ─────────────────────────────────────────────────────────────────────
class TestLogEmit:
    def test_appends_row_with_monotonic_seq(self, store):
        r = RunRecorder(store, "run-1", "first-impression-critic")
        r.log_emit("text", "hello")
        r.log_emit("text", "world")
        rows = list(store.run_logs.find().sort("seq", 1))
        assert [row["seq"] for row in rows] == [1, 2]
        assert [row["content"] for row in rows] == ["hello", "world"]

    def test_persists_kind_phase_turn_metadata(self, store):
        r = RunRecorder(store, "run-1", "desktop-evaluator")
        r.log_emit(
            "result", "turn done",
            turn=4, phase="explore",
            metadata={"cost": 0.01, "usage": {"input_tokens": 100}},
        )
        row = store.run_logs.find_one()
        assert row["kind"] == "result"
        assert row["phase"] == "explore"
        assert row["turn"] == 4
        assert row["metadata"]["cost"] == 0.01

    def test_silent_noop_when_run_id_empty(self, store):
        """A local-dev / test runner without a configured run_id must
        get a clean no-op (not a crash, not a stray doc with empty
        run_id polluting the collection)."""
        r = RunRecorder(store, "", "first-impression-critic")
        r.log_emit("text", "x")
        assert store.run_logs.count_documents({}) == 0

    def test_silent_noop_when_store_is_none(self, store):
        r = RunRecorder(None, "run-1", "first-impression-critic")
        # Must not raise.
        r.log_emit("text", "x")

    def test_persist_failure_swallowed(self, monkeypatch, store):
        """A Mongo blip during the append MUST NOT crash the persona run.
        Same contract as record_step / screenshot store."""
        from qa_agents import run_recorder

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated mongo write timeout")

        monkeypatch.setattr(run_recorder, "append_run_log", _boom)
        r = RunRecorder(store, "run-1", "first-impression-critic")
        # Must not raise.
        r.log_emit("text", "this would have logged")
        # Seq still advances — the recorder's bookkeeping is independent
        # of the persist success/failure, so a later success-path log
        # doesn't reuse seq=1.
        assert r._log_seq == 1


class TestToolUseAlsoLogs:
    def test_record_tool_use_appends_run_log(self, store):
        """A ToolUseBlock should land in BOTH qa_run_steps (existing)
        AND qa_run_logs (#903 mirror)."""
        r = RunRecorder(store, "run-1", "first-impression-critic")
        r.on_assistant_message([
            _tool_use(
                "mcp__playwright__browser_navigate",
                {"url": "https://x.test"},
                tool_id="t1",
            ),
        ])
        # qa_run_steps untouched in semantic (existing test path).
        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        assert len(steps) == 1
        # And the parallel narrative row.
        log_rows = list(store.run_logs.find())
        assert len(log_rows) == 1
        assert log_rows[0]["kind"] == "tool_use"
        assert log_rows[0]["metadata"]["step_n"] == 1
        assert (
            log_rows[0]["metadata"]["tool_name"]
            == "mcp__playwright__browser_navigate"
        )


# ─────────────────────────────────────────────────────────────────────
# #1078 Slice 1 — known-noise tools (browser_snapshot, console_messages,
# payload-less unknowns) come back from the summariser as the literal
# sentinel ``__SKIP__``. The recorder must:
#   * still advance ``_step_n`` and write the qa_run_steps doc
#     (findings + screenshots key on step_n; ordinals must keep ticking)
#   * persist a BLANK args_summary in qa_run_steps (not the sentinel)
#   * suppress the parallel qa_run_logs mirror so the v2 timeline drops
#     the row instead of rendering a bare tool name
# ─────────────────────────────────────────────────────────────────────
class TestSkipSentinelHandling:
    def _skip_summariser(self, tool_name, args):
        """Stand-in for ``runner._format_tool_args`` — returns the
        sentinel for snapshots and the console-message poller; raw
        ``url=…`` for navigates; empty string otherwise."""
        if tool_name in (
            "mcp__playwright__browser_snapshot",
            "mcp__playwright__browser_console_messages",
        ):
            return "__SKIP__"
        if tool_name == "mcp__playwright__browser_navigate":
            return f"url={args.get('url', '')}"
        return ""

    def test_snapshot_advances_step_but_skips_log_emit(self, store):
        r = RunRecorder(
            store, "run-1", "first-impression-critic",
            summarize_args=self._skip_summariser,
        )
        r.on_assistant_message([
            _tool_use("mcp__playwright__browser_snapshot", {}, tool_id="t1"),
        ])

        # Step ordinal MUST advance — findings + screenshots key on it.
        assert r._step_n == 1
        # qa_run_steps still gets a row, with a BLANK args_summary
        # (NOT the literal sentinel — operator-facing).
        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        assert len(steps) == 1
        assert steps[0]["tool_name"] == "mcp__playwright__browser_snapshot"
        assert steps[0]["args_summary"] == ""
        # qa_run_logs mirror MUST be empty — that's how the v2 timeline
        # drops the row.
        assert list(store.run_logs.find()) == []
        assert r._log_seq == 0

    def test_console_messages_advances_step_but_skips_log_emit(self, store):
        r = RunRecorder(
            store, "run-1", "first-impression-critic",
            summarize_args=self._skip_summariser,
        )
        r.on_assistant_message([
            _tool_use(
                "mcp__playwright__browser_console_messages", {}, tool_id="t1",
            ),
        ])
        assert r._step_n == 1
        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        assert len(steps) == 1
        assert steps[0]["args_summary"] == ""
        assert list(store.run_logs.find()) == []

    def test_log_emit_call_count_via_mock(self, store, monkeypatch):
        """Direct assertion: a snapshot ToolUseBlock must NOT call
        log_emit. Uses a mock to count invocations exactly."""
        r = RunRecorder(
            store, "run-1", "first-impression-critic",
            summarize_args=self._skip_summariser,
        )
        calls: list[tuple[str, str]] = []
        original_log_emit = r.log_emit

        def _spy(kind, content, **kwargs):
            calls.append((kind, content))
            return original_log_emit(kind, content, **kwargs)

        monkeypatch.setattr(r, "log_emit", _spy)

        r.on_assistant_message([
            _tool_use("mcp__playwright__browser_snapshot", {}, tool_id="t1"),
            _tool_use(
                "mcp__playwright__browser_navigate",
                {"url": "http://x"},
                tool_id="t2",
            ),
        ])

        # Only the navigate call emitted into qa_run_logs.
        kinds = [k for k, _ in calls]
        assert kinds == ["tool_use"]
        assert "url=http://x" in calls[0][1]
        # And the step ordinals match: 2 steps, only 1 log_emit.
        assert r._step_n == 2

    def test_step_n_keeps_ticking_around_a_skip(self, store):
        """A skip in the middle of a real flow must not break the
        ordinal sequence — findings + screenshots reference step_n."""
        r = RunRecorder(
            store, "run-1", "first-impression-critic",
            summarize_args=self._skip_summariser,
        )
        r.on_assistant_message([
            _tool_use(
                "mcp__playwright__browser_navigate",
                {"url": "http://x"},
                tool_id="t1",
            ),
            _tool_use(
                "mcp__playwright__browser_snapshot", {}, tool_id="t2",
            ),
            _tool_use(
                "mcp__playwright__browser_navigate",
                {"url": "http://y"},
                tool_id="t3",
            ),
        ])
        steps = list_steps_for_persona(store, "run-1", "first-impression-critic")
        # All three steps land in qa_run_steps, ordinals 1-2-3.
        assert [s["step_n"] for s in steps] == [1, 2, 3]
        # The middle row has the blanked args_summary; the other two
        # retain their rendered url.
        assert steps[1]["args_summary"] == ""
        assert "url=http://x" in steps[0]["args_summary"]
        assert "url=http://y" in steps[2]["args_summary"]
        # And only the two navigates show up in qa_run_logs.
        log_rows = list(store.run_logs.find().sort("seq", 1))
        assert len(log_rows) == 2
        assert all(row["kind"] == "tool_use" for row in log_rows)


class TestShouldSkipLogRowHelper:
    """The skip predicate is a one-liner but lives at module level so
    Slice 2's transcript-replay code (which reads qa_run_logs) can
    share the same constant if it ever needs to."""

    def test_sentinel_matches(self):
        from qa_agents.run_recorder import _should_skip_log_row
        assert _should_skip_log_row("__SKIP__") is True

    def test_empty_string_does_not_match(self):
        from qa_agents.run_recorder import _should_skip_log_row
        # Empty string is a LEGAL args_summary for tools that genuinely
        # have no payload — the recorder still mirrors them (the v1
        # fallback in slice 2's UI prints the tool name).
        assert _should_skip_log_row("") is False

    def test_prose_does_not_match(self):
        from qa_agents.run_recorder import _should_skip_log_row
        assert _should_skip_log_row("Clicked btn-23") is False
