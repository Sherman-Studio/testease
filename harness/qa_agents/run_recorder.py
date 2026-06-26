"""Per-persona step recorder + Playwright-screenshot capture (#860, Slice 3).

The harness already SEES every tool call (runner.py iterates the SDK's
message stream and renders each ``ToolUseBlock`` into the log). This module
does the same iteration but persists the structured record:

* one ``qa_run_steps`` doc per tool call,
* the prose text the persona said BEFORE each call,
* the bytes from each ``mcp__playwright__browser_take_screenshot`` result
  (stored as GridFS blobs; the step doc carries the ObjectId),
* a backlink from each ``note_finding`` call to the resulting finding
  ordinal, so the review UI can render finding ↔ step navigation.

The review UI's Transcript tab reads these docs verbatim. Slice 4 (#861)
adds the coverage-matrix highlighting on top of the same data.

Design contract — best-effort writes
  The recorder MUST never raise into the persona's run. A flaky qa-store
  connection, a malformed tool result, an unexpected block shape — any of
  these should log + drop, not crash the orchestrator. The persona's
  findings + review markdown are the irreplaceable outputs of a run; the
  transcript is a luxury. ``_safe`` wraps every write to honour this.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from qa_store import (
    append_run_log,
    attach_finding_to_step,
    attach_screenshot_to_step,
    record_step,
    store_screenshot,
)

if TYPE_CHECKING:
    from qa_store.schema import Store

    from qa_agents.tools.findings import Findings

logger = logging.getLogger(__name__)

# Tool name the Playwright MCP server registers for screenshots. Hard-coded
# (not derived) so a future MCP renaming becomes a focused one-line update
# here rather than a silent capture-loss.
_SCREENSHOT_TOOL = "mcp__playwright__browser_take_screenshot"
# Findings tool — backlink target.
_NOTE_FINDING_TOOL = "mcp__findings__note_finding"
# #1078 — sentinel emitted by ``runner._format_tool_args`` for tool
# calls whose timeline row carries zero operator value (snapshots,
# console-message polls, payload-less unknowns). We keep the literal
# in sync with ``runner._SKIP_LOG_ROW`` rather than importing it to
# avoid a circular import — the recorder receives the summariser as
# a callable, not a module reference.
_SKIP_LOG_ROW = "__SKIP__"


def _should_skip_log_row(args_summary: str) -> bool:
    """Return True when ``args_summary`` is the skip-row sentinel.

    The recorder advances the step ordinal regardless (findings +
    screenshots are attached by step number), but suppresses the
    parallel ``qa_run_logs`` mirror so the run-detail v2 timeline
    doesn't render a noise row.
    """
    return args_summary == _SKIP_LOG_ROW


def _safe(label: str, fn, *args, **kwargs) -> Any:
    """Call ``fn`` and swallow every exception with a logged warning.

    The persona's run takes precedence over the transcript; a recorder
    failure must NOT crash the orchestrator. ``label`` shows up in the
    warning so the operator can grep which write went wrong.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - intentional
        logger.warning("run_recorder: %s failed: %s: %s",
                       label, type(exc).__name__, exc)
        return None


def _extract_screenshot_bytes(tool_result_content: Any) -> bytes | None:
    """Pull PNG bytes from a Playwright ``browser_take_screenshot`` result.

    The Playwright MCP server returns a list of content blocks; one of them
    is the screenshot, typically:

        {"type": "image", "source": {"type": "base64",
         "media_type": "image/png", "data": "iVBORw0K..."}}

    Different MCP transports may shape this slightly differently (some
    expose ``data`` at the top level rather than nested under ``source``).
    We tolerate both shapes — anything that yields a base64-decodable
    payload wins. Returns ``None`` if no image block is found OR the bytes
    fail to decode — the caller treats this as "no screenshot for this
    step", not an error.
    """
    if not isinstance(tool_result_content, list):
        return None
    for block in tool_result_content:
        block_type = _block_field(block, "type")
        if block_type != "image":
            continue
        source = _block_field(block, "source")
        if isinstance(source, dict):
            data = source.get("data")
        else:
            data = _block_field(block, "data")  # flat-shape variant
        if not data:
            continue
        try:
            return base64.b64decode(data)
        except (ValueError, TypeError):
            logger.warning(
                "run_recorder: screenshot block had a non-base64 data field"
            )
            return None
    return None


def _block_field(block: Any, key: str) -> Any:
    """Read ``key`` from a content block whether it's a dict or an SDK object.

    The Claude Agent SDK + Playwright MCP can deliver tool-result content
    blocks as either dicts (most common) or SDK content-block objects that
    expose the same fields as attributes. One accessor handles both, and
    living at module level (rather than as a closure inside the loop)
    keeps ruff's B023 — "function definition does not bind loop variable"
    — happy.
    """
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


class RunRecorder:
    """Per-persona step recorder.

    One instance per persona run. The runner constructs it once, hands it
    every SDK message via :meth:`on_assistant_message` /
    :meth:`on_user_message`, and discards it when the persona finishes.

    Wraps ``Findings.add`` with a callback so ``note_finding`` calls link
    the new finding's ordinal back to the step that produced it — done via
    monkey-patch rather than a constructor argument to keep
    ``tools/findings.py`` agnostic of the recorder. (Test: the wrap is
    idempotent; recording without findings doesn't break.)
    """

    def __init__(
        self,
        store: Store,
        run_id: str,
        persona_id: str,
        findings: Findings | None = None,
        *,
        summarize_args=None,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._persona_id = persona_id
        self._step_n = 0
        # #902/#903 — monotonic sequence number for the qa_run_logs
        # narrative archive. Incremented PER (run_id, persona_id) so
        # slice 2's transcript replay can stream rows in the order they
        # were emitted. Independent of _step_n because tool calls are
        # only one of six kinds we log (text / tool_use / tool_result /
        # result / heartbeat / system).
        self._log_seq = 0
        # Drained into the NEXT step's text_from_persona on the next tool use.
        # If the persona ends with a text-only message (no trailing tool),
        # we never emit that prose as a step — accept the small loss; the
        # review markdown captures the final summary anyway.
        self._text_buffer: list[str] = []
        # tool_use_id → step_n for in-flight screenshot calls. The tool
        # result lands in a later UserMessage with the same id.
        self._screenshot_pending: dict[str, int] = {}
        # FIFO queue of step_ns awaiting their finding ordinal. Pushed when
        # we process a ``note_finding`` ToolUseBlock; popped by the wrapped
        # Findings.add when the tool body eventually runs and the new
        # ordinal becomes known. Queue (not single slot) because the SDK
        # can emit multiple ToolUseBlocks in one AssistantMessage and run
        # their bodies AFTER we've finished iterating the message — a
        # single-slot pointer would get overwritten by the last tool in
        # the message instead of preserving the per-tool pairing.
        self._pending_finding_steps: list[int] = []
        # Caller passes the runner's own arg-summariser so the recorder
        # produces the same human-readable summary the log line shows.
        # Falls back to a no-op if not provided.
        self._summarize_args = summarize_args or (lambda _n, _a: "")

        if findings is not None:
            self._wrap_findings_add(findings)

    # -- narrative-emit archive (#902/#903) -------------------------------
    def log_emit(
        self,
        kind: str,
        content: str,
        *,
        turn: int | None = None,
        phase: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Append one narrative-emit row into ``qa_run_logs``.

        Called by the runner's ``_emit_*`` helpers alongside the existing
        ``logger.info`` so the kubectl-logs view stays unchanged AND the
        text-between-tool-calls survives Job termination. Best-effort —
        a Mongo blip MUST NOT crash the persona run (same contract as
        every other write through this recorder).

        ``kind`` is one of ``RUN_LOG_KINDS`` (text / tool_use /
        tool_result / result / heartbeat / system). The recorder is
        forgiving: an unknown ``kind`` is still persisted (a future
        runner edit shouldn't lose log lines while the schema catches
        up). Validation is documentation, not gatekeeping.

        ``store`` may be None when the runner is invoked without a
        configured backing store — common in tests + local dev. Drop
        silently in that case.
        """
        if self._store is None or not self._run_id:
            return
        self._log_seq += 1
        _safe(
            "append_run_log",
            append_run_log,
            self._store,
            run_id=self._run_id,
            persona_id=self._persona_id,
            seq=self._log_seq,
            kind=kind,
            content=content,
            turn=turn,
            phase=phase,
            metadata=metadata,
        )

    # -- public message handlers ------------------------------------------
    def on_assistant_message(self, content_blocks: list[Any]) -> None:
        """Process one ``AssistantMessage``'s content blocks in order.

        Walks each block; text blocks buffer into the next step's prose,
        tool-use blocks emit a step (draining the buffer).
        """
        for block in content_blocks or []:
            block_name = getattr(block, "name", None)
            block_input = getattr(block, "input", None) or {}
            block_text = getattr(block, "text", None)
            block_id = getattr(block, "id", None)

            if block_name is not None and block_input is not None and block_text is None:
                self._record_tool_use(block_name, block_input, block_id)
            elif block_text:
                self._text_buffer.append(str(block_text))

    def on_user_message(self, content_blocks: list[Any]) -> None:
        """Process one ``UserMessage`` — looking for screenshot tool results.

        Most user messages just carry tool-result text the harness ignores;
        we only care about ``browser_take_screenshot`` results, which arrive
        with ``tool_use_id`` matching an earlier ToolUseBlock we tracked.

        Detection (#1009 fix): claude-agent-sdk delivers ``ToolResultBlock``
        instances on UserMessage.content. They DON'T expose a ``.type``
        attribute — the discriminator is the class name. The previous
        implementation checked ``getattr(block, "type", None) == "tool_result"``
        which returns None for the SDK class and skipped every block — so
        ZERO screenshots were ever captured. Duck-typing on ``tool_use_id``
        is the most robust check across SDK versions + raw dict shape.
        """
        for block in content_blocks or []:
            # Detect tool_result blocks across three known shapes:
            #   1. SDK-typed ``ToolResultBlock`` (class-name discriminator)
            #   2. dict with ``"type": "tool_result"`` (raw API shape)
            #   3. anthropic SDK Pydantic model with ``.type`` attribute
            tool_use_id = None
            content = None
            if isinstance(block, dict):
                if block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id")
                    content = block.get("content")
            else:
                cls_name = block.__class__.__name__
                explicit_type = getattr(block, "type", None)
                if cls_name == "ToolResultBlock" or explicit_type == "tool_result":
                    tool_use_id = getattr(block, "tool_use_id", None)
                    content = getattr(block, "content", None)
                # Pure duck-type fallback: if the block has a tool_use_id
                # attribute, treat it as a tool result. Catches any future
                # SDK rename without us having to chase the class name.
                elif hasattr(block, "tool_use_id"):
                    tool_use_id = getattr(block, "tool_use_id", None)
                    content = getattr(block, "content", None)

            if tool_use_id is None:
                continue
            if tool_use_id not in self._screenshot_pending:
                continue

            step_n = self._screenshot_pending.pop(tool_use_id)
            data = _extract_screenshot_bytes(content)
            if data is None:
                continue
            oid = _safe(
                "store_screenshot",
                store_screenshot,
                self._store, data,
                run_id=self._run_id,
                persona_id=self._persona_id,
                step_n=step_n,
            )
            if oid is not None:
                _safe(
                    "attach_screenshot_to_step",
                    attach_screenshot_to_step,
                    self._store, self._run_id, self._persona_id, step_n, oid,
                )

    # -- internals --------------------------------------------------------
    def _record_tool_use(
        self,
        tool_name: str,
        tool_input: dict,
        tool_use_id: str | None,
    ) -> None:
        self._step_n += 1
        text_from_persona = "\n".join(self._text_buffer).strip()
        self._text_buffer.clear()
        args_summary = self._summarize_args(tool_name, tool_input)

        # #1078 — known-noise tools (browser_snapshot, console_messages,
        # payload-less unknowns) come back with the skip sentinel. The
        # step ordinal must still tick (findings + screenshots key on
        # step_n), but we want a clean blank in qa_run_steps.args_summary
        # and we want to drop the qa_run_logs mirror so slice 2's
        # timeline doesn't render a noise row.
        skip_log_row = _should_skip_log_row(args_summary)
        persisted_args_summary = "" if skip_log_row else args_summary

        _safe(
            "record_step",
            record_step,
            self._store, self._run_id, self._persona_id, self._step_n,
            tool_name=tool_name,
            args_summary=persisted_args_summary,
            text_from_persona=text_from_persona,
            ts=datetime.now(UTC),
        )

        # #902/#903 — mirror the same tool call into the narrative
        # archive so slice 2's transcript replay + slice 3's analyzer
        # see the WHOLE stream, not just text + result. The qa_run_steps
        # doc above stays the primary record for the Transcript tab
        # (it carries the screenshot oid + finding linkback); this
        # parallel row is for cross-run analysis where ordering by seq
        # alongside text/result matters more than the per-tool detail.
        #
        # #1078 — skip the mirror entirely for known-noise tools so the
        # run-detail v2 timeline (which reads from qa_run_logs in slice
        # 2) drops the row instead of rendering a bare tool name.
        if not skip_log_row:
            self.log_emit(
                "tool_use",
                persisted_args_summary or tool_name,
                metadata={
                    "tool_name": tool_name,
                    "step_n": self._step_n,
                },
            )

        if tool_name == _SCREENSHOT_TOOL and tool_use_id is not None:
            # Bind the in-flight screenshot to its step so on_user_message
            # can attach the GridFS oid when the result arrives.
            self._screenshot_pending[tool_use_id] = self._step_n

        if tool_name == _NOTE_FINDING_TOOL:
            # Push this step onto the pending-findings FIFO. The wrapped
            # Findings.add will pop it when the tool body runs (which
            # happens AFTER we've finished iterating this AssistantMessage,
            # so a single-slot pointer would be overwritten if the
            # message also contained later tool uses).
            self._pending_finding_steps.append(self._step_n)

    def _wrap_findings_add(self, findings: Findings) -> None:
        """Monkey-patch ``findings.add`` to backlink ordinals into steps.

        ``Findings.add`` (tools/findings.py) appends to ``_items`` and
        returns the new Finding. We replace the bound method with a
        wrapper that, after the original runs, attaches
        ``len(findings)`` (the new ordinal) to the most recent
        ``note_finding`` step. Idempotent — re-wrapping does nothing
        because the wrapper checks for an attribute marker.
        """
        if getattr(findings.add, "__qa_recorder_wrapped__", False):
            return
        original_add = findings.add

        def _wrapped_add(*args, **kwargs):
            finding = original_add(*args, **kwargs)
            if self._pending_finding_steps:
                step = self._pending_finding_steps.pop(0)
                ordinal = len(findings)
                _safe(
                    "attach_finding_to_step",
                    attach_finding_to_step,
                    self._store, self._run_id, self._persona_id, step, ordinal,
                )
            return finding

        _wrapped_add.__qa_recorder_wrapped__ = True  # type: ignore[attr-defined]
        findings.add = _wrapped_add  # type: ignore[method-assign]
