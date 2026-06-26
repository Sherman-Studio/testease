"""Tests for the Discord run-summary alert — Slice 5 (#622).

Two halves: the pure ``format_run_alert`` formatter, and ``post_run_alert``
with the webhook POST mocked. No live network and no Anthropic calls.
"""

from __future__ import annotations

import pytest

from qa_agents.discord import (
    PersonaAlertLine,
    format_run_alert,
    post_run_alert,
)

_TOTALS = {
    "input_tokens": 120_000,
    "output_tokens": 8_500,
    "cache_tokens": 40_000,
}

_PERSONAS = [
    PersonaAlertLine("margaret", "Yes, cautiously — but the jargon worried me.", 7),
    PersonaAlertLine("priya", "I would not build on this until fair use is documented.", 12),
]


# ---------------------------------------------------------------------------
# format_run_alert — pure formatting.
# ---------------------------------------------------------------------------
def test_alert_contains_run_id():
    msg = format_run_alert("qa-20260519T100000Z", _PERSONAS, _TOTALS)
    assert "qa-20260519T100000Z" in msg


def test_alert_has_one_verdict_line_per_persona():
    msg = format_run_alert("qa-run", _PERSONAS, _TOTALS)
    assert "margaret" in msg
    assert "priya" in msg
    assert "Yes, cautiously" in msg
    assert "would not build on this" in msg
    # One bullet per persona.
    assert msg.count("• **") == 2


def test_alert_includes_findings_count():
    msg = format_run_alert("qa-run", _PERSONAS, _TOTALS)
    assert "7 findings" in msg
    assert "12 findings" in msg


def test_alert_includes_totals_block():
    msg = format_run_alert("qa-run", _PERSONAS, _TOTALS)
    assert "120,000" in msg  # input tokens, comma-grouped
    assert "8,500" in msg
    assert "40,000" in msg
    # Combined total: 120,000 + 8,500 + 40,000.
    assert "168,500" in msg


def test_alert_has_no_dollar_figure():
    """#1822 — the per-run cost line was retired (runs bill the operator's
    flat-rate Claude Code Max plan); the totals block is tokens only."""
    msg = format_run_alert("qa-run", _PERSONAS, _TOTALS)
    assert "$" not in msg
    # Legacy totals dicts that still carry cost keys are ignored, not
    # rendered.
    legacy = {**_TOTALS, "cost_usd": 2.3456, "cost_is_estimated": True}
    msg = format_run_alert("qa-run", _PERSONAS, legacy)
    assert "$" not in msg


def test_alert_points_at_review_ui_port_forward():
    msg = format_run_alert("qa-run", _PERSONAS, _TOTALS)
    assert "kubectl -n slyreply-qa port-forward svc/qa-review 8000:8000" in msg


def test_alert_does_not_mention_github_issue():
    # Filing an issue is the human action in the review UI, not the alert's.
    msg = format_run_alert("qa-run", _PERSONAS, _TOTALS).lower()
    assert "issue" not in msg


def test_alert_handles_empty_verdict():
    personas = [PersonaAlertLine("daniel", "", 3)]
    msg = format_run_alert("qa-run", personas, _TOTALS)
    assert "no verdict captured" in msg


def test_alert_verdict_uses_only_first_line():
    personas = [PersonaAlertLine("tomas", "Solid console.\nBut filters are missing.", 5)]
    msg = format_run_alert("qa-run", personas, _TOTALS)
    assert "Solid console." in msg
    assert "But filters are missing." not in msg


def test_alert_long_verdict_truncated():
    personas = [PersonaAlertLine("margaret", "x" * 500, 1)]
    msg = format_run_alert("qa-run", personas, _TOTALS)
    assert "…" in msg


def test_alert_handles_no_personas():
    msg = format_run_alert("qa-run", [], _TOTALS)
    assert "no personas ran" in msg


def test_alert_handles_missing_totals_keys():
    # Defensive: an empty totals dict must not raise.
    msg = format_run_alert("qa-run", _PERSONAS, {})
    assert "input tokens : 0" in msg


# ---------------------------------------------------------------------------
# post_run_alert — the POST, mocked.
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code: int = 204) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Stand-in for httpx.Client capturing the single POST."""

    posted: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def post(self, url, json=None):
        _FakeClient.posted.append({"url": url, "json": json})
        return _FakeResponse(204)


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeClient.posted = []
    yield
    _FakeClient.posted = []


def test_post_is_noop_when_webhook_empty(monkeypatch):
    monkeypatch.setattr("qa_agents.discord.httpx.Client", _FakeClient)
    sent = post_run_alert("", "qa-run", _PERSONAS, _TOTALS)
    assert sent is False
    assert _FakeClient.posted == []


def test_post_is_noop_when_webhook_whitespace(monkeypatch):
    monkeypatch.setattr("qa_agents.discord.httpx.Client", _FakeClient)
    assert post_run_alert("   ", "qa-run", _PERSONAS, _TOTALS) is False
    assert _FakeClient.posted == []


def test_post_sends_formatted_payload(monkeypatch):
    monkeypatch.setattr("qa_agents.discord.httpx.Client", _FakeClient)
    sent = post_run_alert(
        "https://discord.test/webhook/abc", "qa-20260519", _PERSONAS, _TOTALS
    )
    assert sent is True
    assert len(_FakeClient.posted) == 1
    call = _FakeClient.posted[0]
    assert call["url"] == "https://discord.test/webhook/abc"
    # The payload is Discord's webhook shape: {"content": "..."}.
    assert set(call["json"]) == {"content"}
    content = call["json"]["content"]
    assert "qa-20260519" in content
    assert "margaret" in content
    assert content == format_run_alert("qa-20260519", _PERSONAS, _TOTALS)


def test_post_raises_on_http_error(monkeypatch):
    class _ErrClient(_FakeClient):
        def post(self, url, json=None):
            return _FakeResponse(500)

    monkeypatch.setattr("qa_agents.discord.httpx.Client", _ErrClient)
    with pytest.raises(RuntimeError):
        post_run_alert("https://discord.test/wh", "qa-run", _PERSONAS, _TOTALS)
