"""Composing and filing the per-run GitHub issue.

Slice 6's design: the harness writes findings to Atlas + Discord but does NOT
auto-create GitHub issues. A human reviews a run in the UI, marks the findings
worth keeping as ``included``, and presses "File GitHub issue" — which calls
``POST /api/runs/{run_id}/file-issue``. That endpoint composes ONE issue: every
persona's review, followed by the run's ``included`` findings grouped by
severity, and creates it via the GitHub REST API.
"""

from __future__ import annotations

import httpx

_SEVERITY_ORDER = ("blocker", "major", "minor", "nit")
_ISSUE_LABEL = "qa-agent-report"
# #1089 — insights filed from the /insights page carry a distinct
# label so a triager can tell run-issues from insight-issues at a
# glance without opening the body.
_INSIGHT_ISSUE_LABEL = "from-insights-analyzer"
# #1115 — finding-level filing label. Distinct from the run-level
# label so a triager can tell at-a-glance "one persona flagged this"
# from "the whole run was filed as one issue".
_FINDING_ISSUE_LABEL = "qa-finding"

# #1115 — how each finding ``kind`` maps to a GitHub triage label tag in
# the issue body. The label list on the API call is kept short
# (_FINDING_ISSUE_LABEL only) so we don't proliferate labels in the repo;
# the kind is surfaced in the body header instead.
_KIND_HEADER = {
    "bug": "🐞 Bug",
    "gap": "🧩 Missing feature",
    "risk": "⚠ Risk (legal / compliance / security)",
    "nit": "✏ Nit",
    "praise": "✓ Praise (filed manually — usually no action needed)",
    "observation": "🔎 Observation (filed manually — context only)",
}


def compose_issue(run: dict) -> tuple[str, str]:
    """Build the ``(title, body)`` for a run's GitHub issue.

    ``run`` is a full run doc (as returned by ``qa_store.get_run`` — it carries
    ``reviews`` and ``findings``). The body leads with the run totals, then each
    persona's full review, then the ``included`` findings grouped by severity.
    """
    run_id = run["run_id"]
    title = f"QA review — run {run_id}"

    totals = run.get("totals") or {}
    # #882 — distinguish Max-billed runs in the GitHub-issue body. Pre-#882
    # docs have no ``backend`` key in totals; treat absence as ``api`` (the
    # historical default).
    #
    # #1822 — the per-run dollar computation was retired (every run bills
    # the operator's flat-rate Claude Code Max subscription), so new run
    # docs carry token totals only. Max-billed runs get a billing
    # attribution line; legacy API-mode docs that still STORE a
    # ``cost_usd`` keep their "Approx cost" line verbatim (pass-through of
    # whatever is stored — never recomputed).
    backend = totals.get("backend", "api")
    if backend == "claude-code":
        billing_line = (
            "- Billing: **operator's Claude Code Max plan** "
            "(no per-run API charge)"
        )
    elif "cost_usd" in totals:
        billing_line = f"- Approx cost: ${float(totals.get('cost_usd', 0.0)):.4f}"
    else:
        billing_line = None
    parts: list[str] = [
        f"_Filed from the QA review UI for run `{run_id}`._",
        "",
        "## Run totals",
        "",
        f"- Input tokens: {totals.get('input_tokens', 0):,}",
        f"- Output tokens: {totals.get('output_tokens', 0):,}",
        f"- Cache tokens: {totals.get('cache_tokens', 0):,}",
    ]
    if billing_line is not None:
        parts.append(billing_line)
    parts.append("")

    # Per-persona reviews.
    parts.append("## Persona reviews")
    parts.append("")
    reviews = run.get("reviews") or []
    if not reviews:
        parts.append("_No persona reviews were recorded for this run._")
        parts.append("")
    for review in reviews:
        verdict = review.get("verdict") or ""
        header = f"### {review.get('persona', 'unknown')}"
        if verdict:
            header += f" — {verdict}"
        parts.append(header)
        parts.append("")
        parts.append((review.get("review_markdown") or "").strip())
        parts.append("")

    # Included findings, grouped by severity.
    included = [
        f for f in (run.get("findings") or []) if f.get("status") == "included"
    ]
    parts.append("## Included findings")
    parts.append("")
    if not included:
        parts.append(
            "_No findings were marked **included** for this run — this issue "
            "records the persona reviews only._"
        )
        parts.append("")
    else:
        by_sev: dict[str, list[dict]] = {s: [] for s in _SEVERITY_ORDER}
        for f in included:
            by_sev.setdefault(f.get("severity", "minor"), []).append(f)
        for sev in _SEVERITY_ORDER:
            group = by_sev.get(sev) or []
            if not group:
                continue
            parts.append(f"### {sev.title()} ({len(group)})")
            parts.append("")
            for f in group:
                parts.append(
                    f"- **[{f.get('category', '?')}]** {f.get('title', '(untitled)')} "
                    f"— _{f.get('persona', '?')}_"
                )
                body = (f.get("body") or "").strip()
                if body:
                    for line in body.splitlines():
                        parts.append(f"  {line}")
            parts.append("")

    return title, "\n".join(parts).rstrip() + "\n"


def compose_insight_issue(insight: dict) -> tuple[str, str]:
    """Build the ``(title, body)`` for an insight's GitHub issue (#1089).

    ``insight`` is the qa_insights doc as returned by ``get_insight``.
    Body leads with provenance + severity/category metadata, the
    insight body, then evidence rows so the issue is self-contained
    (a triager doesn't need to log into Test Ease to read it).
    """
    insight_id = insight.get("insight_id", "")
    title = f"[testease/insight] {insight.get('title', '(untitled insight)')}"[:120]

    generated_at = insight.get("generated_at")
    if hasattr(generated_at, "isoformat"):
        gen_repr = generated_at.isoformat()
    else:
        gen_repr = str(generated_at or "")

    parts: list[str] = [
        f"_Generated by the Test Ease cross-run insights analyzer "
        f"({gen_repr}) — `/insights/{insight_id}`._",
        "",
        f"**Severity:** {insight.get('severity', '?')} · "
        f"**Category:** {insight.get('category', '?')}",
        "",
        (insight.get("body") or "_(empty insight body)_").rstrip(),
        "",
    ]

    evidence = insight.get("evidence") or []
    if evidence:
        parts.append("## Evidence")
        parts.append("")
        for e in evidence:
            run_id = e.get("run_id") or "?"
            persona = e.get("persona") or e.get("persona_id") or ""
            snippet = (e.get("snippet") or "").strip()
            line = f"- `{run_id}`"
            if persona:
                line += f" · {persona}"
            parts.append(line)
            if snippet:
                # Indent so each snippet renders as a sub-block under
                # the bullet (GitHub markdown collapses single-newline
                # continuations into the same list item).
                for sl in snippet.splitlines():
                    parts.append(f"  > {sl}")
        parts.append("")

    return title, "\n".join(parts).rstrip() + "\n"


def compose_finding_issue(finding: dict, run: dict) -> tuple[str, str]:
    """Build the ``(title, body)`` for a single finding's GitHub issue (#1115).

    The per-finding equivalent of :func:`compose_issue`. Body opens with the
    finding kind + severity, then the finding's own copy, then provenance
    (which run, which persona, which step) so a triager landing on the
    issue from GitHub alone can jump back into Test Ease for the screenshot
    timeline.

    ``run`` is the same shape ``qa_store.get_run`` returns — used for the
    run id, the persona display name, and the LLM backend (so the cost
    line matches what the run-level issue would say).
    """
    title_text = finding.get("title", "(untitled finding)")
    kind = (finding.get("kind") or "bug").lower()
    severity = (finding.get("severity") or "minor").lower()
    persona = finding.get("persona") or "?"
    run_id = run.get("run_id", finding.get("run_id", "?"))
    finding_id = finding.get("finding_id", "?")

    # Title shape mirrors the insight composer so a triager grepping
    # `[testease/...]` finds both run-issues and finding-issues in one
    # query. Severity is in the title for fixables; absent for
    # praise/observation since those have no meaningful severity.
    if kind in ("bug", "gap", "risk", "nit"):
        title = f"[testease/{kind}/{severity}] {title_text}"[:120]
    else:
        title = f"[testease/{kind}] {title_text}"[:120]

    kind_header = _KIND_HEADER.get(kind, _KIND_HEADER["bug"])
    category = finding.get("category") or "confusion"

    parts: list[str] = [
        f"_Filed from the QA review UI for run "
        f"[`{run_id}`](/runs/{run_id}) · finding `{finding_id}`._",
        "",
        f"**Kind:** {kind_header}",
    ]
    if kind in ("bug", "gap", "risk", "nit"):
        parts.append(f"**Severity:** {severity}  ·  **Category:** {category}")
    else:
        parts.append(f"**Category:** {category}")
    parts.append(f"**Persona:** {persona}")
    parts.append("")
    body_text = (finding.get("body") or "").strip()
    parts.append(
        body_text
        or "_(empty body — see the run's timeline for screenshots and "
        "surrounding context.)_"
    )
    parts.append("")
    parts.append("---")
    parts.append("")
    parts.append(
        f"_View the run's full timeline and screenshots: `/runs/{run_id}`._"
    )

    return title, "\n".join(parts).rstrip() + "\n"


def create_github_issue(
    repo: str, token: str, title: str, body: str, *, label: str = _ISSUE_LABEL
) -> str:
    """Create a GitHub issue via the REST API; return its html_url.

    Raises ``httpx.HTTPStatusError`` on a non-2xx response so the route can
    surface a clean error. Network is only ever touched here — tests mock it.

    Kept for backward compatibility with the runs file-issue path which
    only stores the URL. New callers needing the issue number should
    use :func:`create_github_issue_full`.
    """
    return create_github_issue_full(repo, token, title, body, label=label)["html_url"]


def fetch_github_issue_state(
    repo: str, token: str, issue_number: int,
) -> str | None:
    """Return the live ``"open"``/``"closed"`` state of an issue (#1089 B).

    GitHub's issues API also exposes ``state_reason`` and, for issues
    linked to merged PRs, the ``pull_request`` block — but the
    "merged via linked PR" detection is an order-of-magnitude bigger
    lift (timeline events API). Slice B exposes just open/closed; the
    merged-vs-just-closed precision is a follow-up if needed.

    Returns ``None`` on any failure so the caller can persist the
    sync-attempt timestamp without overwriting the previous good
    state. Network is only ever touched here — tests mock it.
    """
    url = f"https://api.github.com/repos/{repo}/issues/{int(issue_number)}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    data = resp.json()
    state = data.get("state")
    if state in ("open", "closed"):
        return state
    return None


def create_github_issue_full(
    repo: str, token: str, title: str, body: str, *, label: str = _ISSUE_LABEL,
) -> dict:
    """Create a GitHub issue and return both ``html_url`` and ``number``.

    The insight file-issue path (#1089) needs the issue number for the
    Slice B state-sync follow-up; the runs path doesn't, and uses the
    thinner :func:`create_github_issue` wrapper.
    """
    url = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"title": title, "body": body, "labels": [label]}
    resp = httpx.post(url, json=payload, headers=headers, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    return {"html_url": data["html_url"], "number": int(data["number"])}
