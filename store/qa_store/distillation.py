"""Discovery distillation — Slice 1 of #1002 (follows the spike in #1000).

Reads ``qa_run_logs`` for one persona-run pair, sends the chronological
transcript to Haiku, asks for a structured list of {actions, tools,
unexplored branches}, persists each into its own collection.

The hard work (designing the prompt, validating the output quality) was
done in /tmp/testease-spike/. This module is the productionised version
of distill.py from that spike — the prompt is verbatim, but the runner
writes to MongoDB rather than printing.

What this module is NOT:
  * Canonicalization. Each persona-run produces its own rows; merging
    across runs into a canonical catalog is Slice 2's job.
  * Variant generation. Slice 3.
  * Findings extraction. The prompt explicitly skips findings — those
    keep flowing through the qa_findings pipeline.

Cost: roughly $0.05 per persona × ~12 personas = ~$0.60 per run on
Haiku 4.5. Env-var kill switch ``QA_DISTILLATION_ENABLED=0`` disables
the runner entirely (callers check the flag).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .schema import (
    Store,
    clear_discovered_for_persona_run,
    list_run_logs_for_persona,
    upsert_discovered_action,
    upsert_discovered_branch,
    upsert_discovered_tool,
)

log = logging.getLogger(__name__)

# Tuned in the spike — Haiku 4.5 produces high-quality structured
# output at ~$0.05 per persona run. Sonnet would be marginal value at
# 3× the cost; Opus is unjustifiable for a batch task.
DISTILLATION_MODEL = "claude-haiku-4-5"

# The system prompt is verbatim from the spike (distill.py). The
# JSON-output shape MUST stay aligned with how we map fields into the
# qa_store schema below — change one, change both.
# The prompt is verbatim from the spike; one schema line below is wider
# than the 100-char limit on purpose (line-wrapping it would change what
# the model sees). The `noqa: E501` is on that single offending line.
_SYSTEM_PROMPT = """\
You are analysing a QA persona's exploration log to extract a coverage
catalog — the testable actions the persona discovered the website could
perform.

Output STRICT JSON with this shape:
{
  "discovered_actions": [
    {
      "action_id": "category.kebab-case-slug",
      "category": one of {auth, billing, agents, playground, account, contact, docs, admin, other},
      "human_description": "concrete, ~10-word description of what this action does",
      "url_seen": "the URL path where the action was discovered (or null)",
      "evidence": "1-sentence quote from the log proving the persona saw or tried this",
      "branches_noticed": ["one-line description per branch the persona noticed but did not try"]
    }
  ],
  "tools_used": [
    {"name": "tool-name", "purpose": "what the persona used it for"}
  ],
  "unexplored_branches": [
    "free-text observation of something the persona noticed but did not attempt"
  ]
}

Rules:
- Only include actions the persona ACTUALLY interacted with or clearly identified.
  "User would click Submit" is not enough; "User clicked Submit" or "User saw
  a Submit button" both qualify.
- action_id is a stable, deterministic slug — same action across runs MUST
  produce the same id. Use category.verb-noun form (e.g. auth.signup-with-existing-email).
- Skip purely navigational actions (e.g. "went to home page") unless they
  exposed something interesting.
- Skip findings/bugs/errors — those are tracked separately in qa_findings.
  Focus on COVERAGE — what could be tested.
- Categories: auth=login/signup/password, billing=checkout/subscriptions/prices,
  agents=UID/persona configuration, playground=the playground UI, account=
  profile/settings, admin=admin panels, contact=support/contact forms,
  docs=help/documentation, other=anything that doesn't fit.
- If a persona discovered MANY variants of one action, prioritise the
  distinct action over its variants. Example: one "billing.submit-card-form"
  action; "what if the card is declined?" goes in branches_noticed, NOT as a
  separate action.

Return JSON only — no commentary before or after, no code fences.
"""

_MAX_TRANSCRIPT_BYTES = 200_000
"""Hard ceiling on how much transcript we send the model.

A typical run is ~30-50 KB of logs; a verbose abuse-tester run can hit
~150 KB. The 200 KB cap protects against runaway runs that would blow
through Haiku's context limit AND inflate the cost meaningfully. When
truncation is needed, we keep the TAIL — the end of the log is where
the persona's final discoveries land, which is the higher-signal
window. The head is mostly setup chatter.
"""


def is_enabled() -> bool:
    """Distillation is on by default. Set ``QA_DISTILLATION_ENABLED=0``
    in the harness env to disable (e.g. emergency cost-cap)."""
    return os.environ.get("QA_DISTILLATION_ENABLED", "1") not in ("0", "false", "False")


def format_transcript(logs: list[dict]) -> str:
    """Compact qa_run_log docs into a chronological line-oriented feed.

    Drops empty content rows and the metadata-only fields the model
    doesn't need. Caps the result at ``_MAX_TRANSCRIPT_BYTES`` (tail
    preserved — see constant docstring).
    """
    lines = []
    for entry in logs:
        ts = entry.get("ts", "")
        # Datetimes come back from Mongo as bson datetime objects; render
        # them ISO-formatted so the model sees consistent timestamps.
        if hasattr(ts, "isoformat"):
            ts = ts.isoformat()
        kind = entry.get("kind", "?")
        content = (entry.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"[{ts}] [{kind}] {content}")
    text = "\n".join(lines)
    if len(text) > _MAX_TRANSCRIPT_BYTES:
        truncated = text[-_MAX_TRANSCRIPT_BYTES:]
        # Don't start mid-line — find the next newline and trim to it.
        nl = truncated.find("\n")
        if nl >= 0:
            truncated = truncated[nl + 1 :]
        text = (
            "[... transcript truncated, showing tail only ...]\n" + truncated
        )
    return text


def _call_anthropic(transcript: str) -> dict:
    """Issue a single ``messages.create`` to Haiku and return parsed JSON.

    The Anthropic SDK is imported lazily so qa-store importers without
    an Anthropic key (the review-ui API server, for example) don't
    drag the SDK in for no reason. The harness, which DOES write
    distillations, already depends on anthropic for memory distillation
    (#872) so this is a free dependency for the only caller that
    actually invokes the function.
    """
    from anthropic import Anthropic  # noqa: PLC0415

    client = Anthropic()
    response = client.messages.create(
        model=DISTILLATION_MODEL,
        max_tokens=4000,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    "Here is the persona's exploration log. Extract the "
                    "discovered actions per the system prompt.\n\n"
                    f"<transcript>\n{transcript}\n</transcript>"
                ),
            }
        ],
    )
    text = response.content[0].text.strip()
    # Defensive — strip fenced-code wrap if the model added one despite
    # the prompt asking not to.
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    if text.startswith("json\n"):
        text = text[5:]
    return json.loads(text)


def distill_persona_run(
    store: Store,
    run_id: str,
    persona_id: str,
    *,
    call_anthropic=_call_anthropic,
) -> dict[str, int]:
    """Distill one (run_id, persona_id) into the discovered_* collections.

    Returns a counts dict ``{"actions": N, "tools": N, "branches": N}``
    for the caller to log + surface in the run summary.

    Idempotent: re-running on the same pair clears the previous batch
    BEFORE writing the new one, so a re-distillation doesn't leave
    stale rows whose action_id is no longer emitted. The clear+write
    is NOT in a transaction — a transient failure mid-write could
    leave a partial result. Acceptable for Slice 1; Slice 2's
    canonicalization run will mop up via merge semantics anyway.

    ``call_anthropic`` is injectable for tests — pass a fake that
    returns canned JSON to exercise the persistence path without an
    API call. The Anthropic SDK import only fires when the real
    helper is used.
    """
    if not is_enabled():
        log.info(
            "qa_distillation: skipped %s/%s (QA_DISTILLATION_ENABLED=0)",
            run_id, persona_id,
        )
        return {"actions": 0, "tools": 0, "branches": 0}

    logs = list_run_logs_for_persona(store, run_id, persona_id)
    if not logs:
        log.info(
            "qa_distillation: no logs for %s/%s — pre-#903 run, skipping",
            run_id, persona_id,
        )
        return {"actions": 0, "tools": 0, "branches": 0}

    transcript = format_transcript(logs)
    if not transcript.strip():
        return {"actions": 0, "tools": 0, "branches": 0}

    try:
        result = call_anthropic(transcript)
    except Exception as exc:
        log.warning(
            "qa_distillation: model call failed for %s/%s: %s",
            run_id, persona_id, exc,
        )
        return {"actions": 0, "tools": 0, "branches": 0}

    # Clear any previous batch BEFORE writing the new one so a re-
    # distillation produces a clean view rather than a merged history.
    clear_discovered_for_persona_run(store, run_id, persona_id)

    actions = result.get("discovered_actions") or []
    tools = result.get("tools_used") or []
    branches = result.get("unexplored_branches") or []

    for a in actions:
        try:
            upsert_discovered_action(
                store,
                run_id=run_id,
                persona_id=persona_id,
                action_id=a["action_id"],
                category=a.get("category", "other"),
                human_description=a.get("human_description", ""),
                url_seen=a.get("url_seen"),
                evidence=a.get("evidence", ""),
                branches_noticed=a.get("branches_noticed", []),
            )
        except (KeyError, TypeError) as exc:
            log.warning(
                "qa_distillation: skipping malformed action in %s/%s: %s (%r)",
                run_id, persona_id, exc, a,
            )

    for t in tools:
        try:
            upsert_discovered_tool(
                store,
                run_id=run_id,
                persona_id=persona_id,
                name=t["name"],
                purpose=t.get("purpose", ""),
            )
        except (KeyError, TypeError) as exc:
            log.warning(
                "qa_distillation: skipping malformed tool in %s/%s: %s (%r)",
                run_id, persona_id, exc, t,
            )

    for ordinal, b in enumerate(branches, start=1):
        # Branches are free-text strings, no schema beyond that.
        description = b if isinstance(b, str) else json.dumps(b)
        upsert_discovered_branch(
            store,
            run_id=run_id,
            persona_id=persona_id,
            ordinal=ordinal,
            description=description,
        )

    counts: dict[str, Any] = {
        "actions": len(actions),
        "tools": len(tools),
        "branches": len(branches),
    }
    log.info(
        "qa_distillation: %s/%s — %d actions, %d tools, %d branches",
        run_id, persona_id, counts["actions"], counts["tools"], counts["branches"],
    )
    return counts
