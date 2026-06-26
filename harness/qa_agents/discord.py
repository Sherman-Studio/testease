"""Discord run-summary alerting for the QA persona harness — Slice 5 (#622).

After a multi-persona run finishes, the orchestrator posts ONE alert to a
Discord webhook so a maintainer knows a run is ready to triage. The alert
carries the shared run id, a one-line verdict per persona, the combined
token totals block, and a pointer to the review UI.

It deliberately does NOT open a GitHub issue — filing an issue is the human
action taken in the review UI (#626). This module only announces the run.

Two concerns, kept apart so the formatting is unit-testable with no network:

1. ``format_run_alert`` — pure: turns a list of per-persona summaries plus a
   totals dict into the Discord message string. No I/O.
2. ``post_run_alert`` — the thin POST. A no-op when the webhook URL is empty,
   so a local ``--all`` run needs no Discord credential.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

# The review UI has no ingress; access is via port-forward. This is the exact
# command the alert tells a reader to run.
_REVIEW_UI_HINT = "kubectl -n slyreply-qa port-forward svc/qa-review 8000:8000"


@dataclass(frozen=True)
class PersonaAlertLine:
    """One persona's contribution to the run alert."""

    persona_id: str
    verdict: str
    findings_count: int


def _verdict_text(verdict: str) -> str:
    """A persona's verdict, trimmed to one line and never empty."""
    line = (verdict or "").strip().splitlines()[0].strip() if verdict else ""
    if not line:
        return "_(no verdict captured)_"
    if len(line) > 240:
        line = line[:239].rstrip() + "…"
    return line


def _totals_block(totals: dict) -> str:
    """Render the run's combined token totals as a fenced code block.

    Token counts only — the dollar line was retired in #1822 (runs bill
    the operator's flat-rate Claude Code Max subscription).
    """
    inp = int(totals.get("input_tokens", 0) or 0)
    out = int(totals.get("output_tokens", 0) or 0)
    cache = int(totals.get("cache_tokens", 0) or 0)
    lines = [
        "```",
        f"input tokens : {inp:,}",
        f"output tokens: {out:,}",
        f"cache tokens : {cache:,}",
        f"total tokens : {inp + out + cache:,}",
        "```",
    ]
    return "\n".join(lines)


def _breakdown_line(personas: list[PersonaAlertLine]) -> str:
    """Render the clean/truncated/failed breakdown line.

    A persona's ``verdict`` carries a free-text line from the review markdown.
    The orchestrator stamps `"failed"` for personas whose ``run_persona``
    raised, and the explore-phase truncation marker shows up as a
    `"_(explore phase ended early…)_"` line in the review body — but the
    one signal that survives into ``PersonaAlertLine`` is the verdict text,
    so the heuristic is: verdict containing ``"failed"`` (case-insensitive)
    is a failure, ``"ended early"`` / ``"truncated"`` / ``"max_turns"`` is a
    truncation, everything else is clean. Keeps the wire format simple while
    still surfacing the three states the maintainer cares about (#652).
    """
    failed = 0
    truncated = 0
    for p in personas:
        v = (p.verdict or "").lower()
        if "failed" in v:
            failed += 1
        elif "ended early" in v or "truncated" in v or "max_turns" in v:
            truncated += 1
    clean = len(personas) - failed - truncated
    return f"{clean} clean · {truncated} truncated · {failed} failed"


def format_run_alert(
    run_id: str,
    personas: list[PersonaAlertLine],
    totals: dict,
) -> str:
    """Build the Discord run-summary message — pure, no I/O.

    Layout: a header with the run id, a clean/truncated/failed breakdown line,
    one verdict line per persona, the token totals block, then the
    review-UI pointer.
    """
    lines: list[str] = [
        f"**QA persona run `{run_id}` finished**",
    ]
    if personas:
        lines += [
            f"_{_breakdown_line(personas)}_",
            "",
        ]
        for p in personas:
            lines.append(
                f"• **{p.persona_id}** ({p.findings_count} findings) — "
                f"{_verdict_text(p.verdict)}"
            )
    else:
        lines += ["", "_(no personas ran)_"]
    lines += [
        "",
        "**Run totals**",
        _totals_block(totals),
        "",
        f"Review it in the UI: `{_REVIEW_UI_HINT}`",
    ]
    return "\n".join(lines)


def post_run_alert(
    webhook_url: str,
    run_id: str,
    personas: list[PersonaAlertLine],
    totals: dict,
    *,
    timeout: float = 10.0,
) -> bool:
    """POST the run-summary alert to a Discord webhook.

    Returns ``True`` if the alert was sent, ``False`` if it was skipped
    because no webhook URL is configured. Network/HTTP errors propagate —
    the orchestrator catches them so a Discord outage never fails a run.
    """
    if not (webhook_url or "").strip():
        return False
    content = format_run_alert(run_id, personas, totals)
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(webhook_url, json={"content": content})
        resp.raise_for_status()
    return True
