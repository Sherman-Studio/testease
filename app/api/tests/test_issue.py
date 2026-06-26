"""Tests for the GitHub issue composer (no network)."""

from __future__ import annotations

from qa_review_api.issue import compose_issue


def _run(findings=None, reviews=None) -> dict:
    return {
        "run_id": "qa-run-9",
        "totals": {"input_tokens": 1234, "output_tokens": 567, "cache_tokens": 0, "cost_usd": 2.5},
        "reviews": reviews
        if reviews is not None
        else [
            {"persona": "first-impression-critic", "verdict": "Yes.", "review_markdown": "## Review\n\nLiked it."},
        ],
        "findings": findings if findings is not None else [],
    }


def test_compose_issue_title():
    title, _ = compose_issue(_run())
    assert title == "QA review — run qa-run-9"


def test_compose_issue_includes_totals_and_reviews():
    _, body = compose_issue(_run())
    assert "Run totals" in body
    assert "Approx cost: $2.5000" in body
    assert "### first-impression-critic — Yes." in body
    assert "Liked it." in body


def test_compose_issue_groups_included_findings_by_severity():
    findings = [
        {"persona": "first-impression-critic", "category": "worry", "severity": "blocker",
         "title": "Privacy", "body": "scary", "status": "included"},
        {"persona": "desktop-evaluator", "category": "copy", "severity": "minor",
         "title": "Jargon", "body": "", "status": "included"},
        {"persona": "desktop-evaluator", "category": "bug", "severity": "major",
         "title": "Broken link", "body": "", "status": "dismissed"},
    ]
    _, body = compose_issue(_run(findings=findings))
    assert "### Blocker (1)" in body
    assert "### Minor (1)" in body
    # Dismissed finding is excluded.
    assert "Broken link" not in body
    # Blocker section precedes minor.
    assert body.index("### Blocker") < body.index("### Minor")


def test_compose_issue_notes_no_included_findings():
    _, body = compose_issue(_run(findings=[]))
    assert "No findings were marked" in body


def test_compose_issue_handles_no_reviews():
    _, body = compose_issue(_run(reviews=[]))
    assert "No persona reviews" in body


# ---------------------------------------------------------------------------
# #882 / #1822 — Max-billed runs render a billing-attribution line instead
# of any dollar figure (the per-run cost computation was retired; runs bill
# the operator's flat-rate Claude Code Max subscription).
# ---------------------------------------------------------------------------
def test_compose_issue_marks_max_billed_runs():
    run = _run()
    run["totals"]["backend"] = "claude-code"
    # Even a legacy Max-mode doc that still stores the retired estimate
    # renders the attribution line, never a dollar figure.
    run["totals"]["cost_usd"] = 3.5
    _, body = compose_issue(run)
    assert "Claude Code Max" in body, (
        "must explicitly attribute Max-billed runs so a reader "
        "doesn't think the run spent API credits"
    )
    assert "no per-run API charge" in body
    assert "$3.5000" not in body, "#1822 — no dollar figures for Max runs"


def test_compose_issue_new_run_doc_without_cost_key():
    """#1822 — NEW run docs carry no ``cost_usd`` at all. The composer must
    not KeyError and must render token totals + the Max billing line."""
    run = _run()
    run["totals"] = {
        "input_tokens": 1234, "output_tokens": 567, "cache_tokens": 0,
        "backend": "claude-code",
    }
    _, body = compose_issue(run)
    assert "Input tokens: 1,234" in body
    assert "Claude Code Max" in body
    assert "$" not in body


def test_compose_issue_api_doc_without_cost_key_omits_cost_line():
    """An api-backend doc with no stored cost renders no cost line (and
    doesn't KeyError) — nothing is recomputed."""
    run = _run()
    run["totals"] = {"input_tokens": 1, "output_tokens": 2, "cache_tokens": 3}
    _, body = compose_issue(run)
    assert "Approx cost" not in body
    assert "Cache tokens: 3" in body


def test_compose_issue_keeps_existing_format_for_api_mode():
    """Legacy API-mode docs (no ``backend`` key, stored ``cost_usd``) must
    still render the 'Approx cost: $X' line verbatim — pass-through of
    whatever is stored, never recomputed."""
    _, body = compose_issue(_run())  # no backend key, cost_usd stored
    assert "Approx cost: $2.5000" in body
    assert "Claude Code Max" not in body
