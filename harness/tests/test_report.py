"""Tests for report rendering — run-summary.json shape and review markdown."""

from __future__ import annotations

import json

import pytest

from qa_agents.accounting import RunAccounting
from qa_agents.report import (
    FileReportSink,
    RunResult,
    build_run_summary,
    new_run_id,
    render_review_markdown,
    write_run,
)
from qa_agents.tools.findings import Findings


@pytest.fixture
def result() -> RunResult:
    acc = RunAccounting()
    acc.record(
        "explore",
        "claude-sonnet-4-6",
        {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 200},
        num_turns=15,
    )
    acc.record(
        "report",
        "claude-opus-4-7",
        {"input_tokens": 3000, "output_tokens": 1200},
        num_turns=1,
    )
    findings = Findings()
    findings.add("confusion", "major", "What is a UID?", 'The page said "UID".')
    findings.add("worry", "blocker", "Privacy unclear", "Can it read my email?")
    return RunResult(
        run_id="first-impression-critic-20260519T100000Z",
        persona_id="first-impression-critic",
        persona_display_name='Margaret "Marg" Doyle',
        started_at="2026-05-19T10:00:00+00:00",
        finished_at="2026-05-19T10:20:00+00:00",
        accounting=acc,
        findings=findings,
        review_markdown="## First impressions\n\nI was nervous.",
        explore_digest="navigated to landing page",
    )


# --------------------------------------------------------------------------
# run-summary.json shape.
# --------------------------------------------------------------------------
def test_build_run_summary_shape(result):
    summary = build_run_summary(result)
    assert summary["run_id"] == "first-impression-critic-20260519T100000Z"
    assert summary["persona"] == "first-impression-critic"
    assert summary["num_turns"] == 16  # 15 explore + 1 report
    assert summary["findings_count"] == 2
    assert summary["findings_by_severity"]["blocker"] == 1
    assert summary["findings_by_severity"]["major"] == 1
    assert summary["findings_by_category"]["worry"] == 1
    assert summary["findings_by_category"]["confusion"] == 1
    assert len(summary["findings"]) == 2
    # #1822 — no dollar fields in the accounting payload any more.
    assert "total_cost_usd" not in summary["accounting"]
    assert "real_cost_usd" not in summary["accounting"]
    assert summary["accounting"]["total_tokens"] == 5900
    assert len(summary["accounting"]["phases"]) == 2


def test_run_summary_is_json_serialisable(result):
    # The whole payload must round-trip through json with no custom encoder.
    text = json.dumps(build_run_summary(result))
    reparsed = json.loads(text)
    assert reparsed["persona"] == "first-impression-critic"


# --------------------------------------------------------------------------
# Markdown rendering.
# --------------------------------------------------------------------------
def test_render_review_markdown_contains_key_sections(result):
    md = render_review_markdown(result)
    assert "# QA persona review — Margaret" in md
    assert "## Run accounting" in md
    assert "## Review" in md
    assert "## Findings appendix" in md
    # The persona-voiced review body is embedded verbatim.
    assert "I was nervous." in md
    # The accounting table totals row — token counts, no dollar column
    # (#1822: the per-run cost computation was retired).
    assert "Run total" in md
    assert "**5,900**" in md
    assert "$" not in md.split("## Review")[0]
    # The findings appendix lists each finding with its tags.
    assert "[confusion/major]" in md
    assert "[worry/blocker]" in md
    # The findings summary line in the header.
    assert "1 blocker" in md


def test_render_review_markdown_no_findings():
    acc = RunAccounting()
    acc.record("explore", "claude-sonnet-4-6", {})
    res = RunResult(
        run_id="margaret-x",
        persona_id="first-impression-critic",
        persona_display_name="Margaret",
        started_at="a",
        finished_at="b",
        accounting=acc,
        findings=Findings(),
        review_markdown="ok",
    )
    md = render_review_markdown(res)
    assert "No findings were recorded" in md


# --------------------------------------------------------------------------
# FileReportSink writes the two artifacts.
# --------------------------------------------------------------------------
def test_file_report_sink_writes_both_files(result, tmp_path):
    sink = FileReportSink(str(tmp_path))
    locators = write_run(result, sink)

    review_path = tmp_path / "first-impression-critic-review.md"
    summary_path = tmp_path / "run-summary.json"
    assert review_path.exists()
    assert summary_path.exists()
    assert locators["review"] == str(review_path)
    assert locators["summary"] == str(summary_path)

    review_text = review_path.read_text(encoding="utf-8")
    assert "## Review" in review_text

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["persona"] == "first-impression-critic"
    assert summary["findings_count"] == 2


def test_file_report_sink_creates_missing_directory(result, tmp_path):
    nested = tmp_path / "deep" / "qa-runs"
    sink = FileReportSink(str(nested))
    write_run(result, sink)
    assert (nested / "run-summary.json").exists()


# --------------------------------------------------------------------------
# Run id.
# --------------------------------------------------------------------------
def test_new_run_id_format():
    from datetime import UTC, datetime

    rid = new_run_id("first-impression-critic", now=datetime(2026, 5, 19, 10, 0, 0, tzinfo=UTC))
    assert rid == "first-impression-critic-20260519T100000Z"
