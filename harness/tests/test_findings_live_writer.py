"""Tests for the live-streaming finding writer wiring (#1115 follow-up).

The contract: when ``build_findings_server`` is given a ``live_writer``
callable, every successful ``note_finding`` tool call invokes the writer
with ``(ordinal, finding_dict)``. Errors from the writer are swallowed —
the in-memory append always succeeds and the persona-end reconciliation
remains the safety net.

We exercise ``make_note_finding_handler`` directly (the inner async
handler) rather than reaching through the MCP server returned by
``build_findings_server`` — the SDK doesn't expose the handler on the
server object in a stable way across versions. Both paths share the
same handler so the wiring is equivalent.
"""

from __future__ import annotations

import logging

import pytest

from qa_agents.tools.findings import (
    Findings,
    make_note_finding_handler,
)


@pytest.fixture
def findings() -> Findings:
    return Findings()


async def test_no_live_writer_keeps_pre_followup_behaviour(findings):
    """No live_writer = nothing writes to qa-store mid-call. The finding
    still lands in the in-memory collector (the pre-this-slice behaviour
    that the persona-end add_persona_result reconciliation relies on)."""
    handler = make_note_finding_handler(findings)
    await handler({
        "kind": "bug", "category": "bug", "severity": "minor",
        "title": "t", "body": "b",
    })
    assert len(findings) == 1


async def test_live_writer_invoked_with_ordinal_and_dict(findings):
    """Writer is called with (ordinal, finding_dict). Ordinal is 1-based
    and matches the in-memory collector's length at call time."""
    calls: list[tuple[int, dict]] = []

    def writer(ordinal: int, finding_dict: dict) -> None:
        calls.append((ordinal, finding_dict))

    handler = make_note_finding_handler(findings, live_writer=writer)
    await handler({
        "kind": "praise", "category": "surprise", "severity": "minor",
        "title": "works well", "body": "",
    })
    await handler({
        "kind": "bug", "category": "bug", "severity": "blocker",
        "title": "broken", "body": "quote: 'oops'",
    })
    assert [c[0] for c in calls] == [1, 2]
    assert calls[0][1]["kind"] == "praise"
    assert calls[0][1]["title"] == "works well"
    assert calls[1][1]["kind"] == "bug"
    assert calls[1][1]["severity"] == "blocker"


async def test_live_writer_failure_does_not_crash_the_tool(findings, caplog):
    """A transient Mongo blip raised by the writer must not surface to
    the model — the in-memory append always succeeds, and the
    persona-end reconciliation is the safety net."""

    def writer(ordinal: int, finding_dict: dict) -> None:
        raise RuntimeError("Mongo unavailable")

    handler = make_note_finding_handler(findings, live_writer=writer)
    with caplog.at_level(logging.WARNING, logger="qa_agents.tools.findings"):
        result = await handler({
            "kind": "bug", "category": "bug", "severity": "minor",
            "title": "x", "body": "",
        })

    # Tool result still came back successfully — the persona reads
    # this ACK and moves on.
    assert "Noted finding #1" in result["content"][0]["text"]
    # In-memory append survived.
    assert len(findings) == 1
    # And we logged the writer failure for the operator to grep.
    assert any("live_writer failed" in r.message for r in caplog.records)


async def test_live_writer_sees_normalised_finding_dict(findings):
    """The writer receives the same shape the Finding dataclass
    serialises to. Unknown kind/severity coerce BEFORE the writer
    sees them, so the writer never has to defend against junk."""
    seen: list[dict] = []

    def writer(ordinal: int, finding_dict: dict) -> None:
        seen.append(finding_dict)

    handler = make_note_finding_handler(findings, live_writer=writer)
    await handler({
        "kind": "not-a-real-kind", "category": "bug",
        "severity": "not-a-severity", "title": "x", "body": "",
    })
    assert len(seen) == 1
    assert seen[0]["kind"] == "bug"     # coerced
    assert seen[0]["severity"] == "minor"  # coerced
    assert seen[0]["title"] == "x"
    assert "recorded_at" in seen[0]


async def test_in_memory_append_and_live_write_share_the_ordinal(findings):
    """The handler reports the ordinal in the ACK and passes the same
    ordinal to the writer — so a future cross-reference between the
    transcript step's ``finding_ordinals`` and the qa-store row's
    finding_id (which encodes the ordinal) lines up."""
    seen: list[int] = []

    def writer(ordinal: int, finding_dict: dict) -> None:
        seen.append(ordinal)

    handler = make_note_finding_handler(findings, live_writer=writer)
    r1 = await handler({"kind": "bug", "category": "bug", "severity": "minor",
                         "title": "a", "body": ""})
    r2 = await handler({"kind": "bug", "category": "bug", "severity": "minor",
                         "title": "b", "body": ""})
    assert "Noted finding #1" in r1["content"][0]["text"]
    assert "Noted finding #2" in r2["content"][0]["text"]
    assert seen == [1, 2]
