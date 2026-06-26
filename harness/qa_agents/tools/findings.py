"""The note_finding tool + the run-scoped Findings collector.

A *finding* is one structured observation the persona makes mid-run: a
moment of confusion, a worrying bit of copy, a suspected bug, something the
product does well. The agent calls ``note_finding`` whenever it reacts;
the harness keeps every call in a plain ``Findings`` collector that the
runner owns and later feeds to the report phase and into
``run-summary.json``.

#1115 — added ``kind`` axis to separate positives from negatives. Pre-#1115
the schema was (category, severity) only, and personas were hacking
positives into ``severity="nit"`` + body-tag-as-"(positive security)".
That made the run UI count praise alongside bugs in the same "X findings"
badge, which buried real fixables under compliments. With ``kind``, the
UI splits the Findings tab into three sections:

  - ``bug`` / ``gap`` / ``risk`` / ``nit`` → 🐞 Fix list (file-as-issue)
  - ``praise`` → ✓ Working well (no issue, no severity)
  - ``observation`` → 🔎 neutral context (no issue, no severity)

``severity`` only carries meaning for the fixable kinds; praise + observation
ignore whatever the persona passed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# #1115 follow-up — type alias for the live writer callable the
# in-process MCP tool invokes after every successful add. The runner
# constructs one of these closing over (store, run_id, persona_id) and
# passes it into build_findings_server.
#
# Signature: ``(ordinal, finding_dict) -> None``. Errors raised by the
# writer are caught and logged here — a Mongo blip should never crash
# the agent loop or lose the in-memory finding (the persona-end
# reconciliation will still flush it).
LiveFindingWriter = Callable[[int, dict], None]

# Legacy taxonomy — kept for back-compat. New code reads ``kind`` instead.
# ``category`` was the pre-#1115 axis and is still optional on note_finding
# calls; if a persona ships only ``kind`` we synthesise a category for the
# legacy run-summary.json schema (and old runs keep rendering).
CATEGORIES = ("bug", "confusion", "copy", "missing-feature", "worry", "surprise")
SEVERITIES = ("blocker", "major", "minor", "nit")

# #1115 — the orthogonal axis. Three "fix me" kinds, two "no fix needed".
# Order matters: it's the priority the UI bucketises by within the
# 🐞 Fix list.
KINDS = ("bug", "gap", "risk", "nit", "praise", "observation")
FIXABLE_KINDS = frozenset({"bug", "gap", "risk", "nit"})
POSITIVE_KINDS = frozenset({"praise"})
NEUTRAL_KINDS = frozenset({"observation"})


@dataclass
class Finding:
    """One structured observation recorded during the explore phase."""

    category: str
    severity: str
    title: str
    body: str
    # #1115 — orthogonal sentiment axis. Default ``bug`` preserves the
    # pre-#1115 semantics on docs that don't carry a kind (every legacy
    # finding was implicitly a "fix me" item).
    kind: str = "bug"
    recorded_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def is_fixable(self) -> bool:
        """True if this finding belongs in the 🐞 Fix list."""
        return self.kind in FIXABLE_KINDS


class Findings:
    """Run-scoped, ordered collection of findings.

    Owned by the runner and passed into the tool factory so the tool appends
    here. Categories / severities / kinds are normalised; unknown values are
    coerced to a safe default rather than rejected, so a slightly-off agent
    call still records a usable finding.
    """

    def __init__(self) -> None:
        self._items: list[Finding] = []

    def add(
        self,
        category: str,
        severity: str,
        title: str,
        body: str,
        kind: str = "",
    ) -> Finding:
        cat = (category or "").strip().lower()
        if cat not in CATEGORIES:
            cat = "confusion"
        sev = (severity or "").strip().lower()
        if sev not in SEVERITIES:
            sev = "minor"
        k = (kind or "").strip().lower()
        if k not in KINDS:
            # #1115 — defensive default. A persona that forgets ``kind`` keeps
            # the pre-#1115 semantics: everything was a fixable. We do NOT
            # try to infer praise from the body text — too brittle.
            k = "bug"
        finding = Finding(
            category=cat,
            severity=sev,
            kind=k,
            title=(title or "").strip() or "(untitled)",
            body=(body or "").strip(),
        )
        self._items.append(finding)
        return finding

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    @property
    def items(self) -> list[Finding]:
        return list(self._items)

    def as_list(self) -> list[dict]:
        return [f.as_dict() for f in self._items]

    def counts_by_severity(self) -> dict[str, int]:
        counts = dict.fromkeys(SEVERITIES, 0)
        for f in self._items:
            counts[f.severity] += 1
        return counts

    def counts_by_category(self) -> dict[str, int]:
        counts = dict.fromkeys(CATEGORIES, 0)
        for f in self._items:
            counts[f.category] += 1
        return counts

    def counts_by_kind(self) -> dict[str, int]:
        counts = dict.fromkeys(KINDS, 0)
        for f in self._items:
            counts[f.kind] += 1
        return counts


_NOTE_FINDING_DESCRIPTION = (
    "Record a structured observation about the product. Call this EVERY "
    "time you have a reaction — broken, confused, worried, surprised, OR "
    "something the product does well. The operator needs both kinds to "
    "review the run.\n"
    "\n"
    "REQUIRED: ``kind`` — pick one:\n"
    "  - ``bug``         — something is broken / wrong / doesn't work as designed\n"
    "  - ``gap``         — expected feature is missing\n"
    "  - ``risk``        — legal / compliance / security / privacy concern\n"
    "  - ``nit``         — small fixable annoyance (copy, spacing, colour)\n"
    "  - ``praise``      — the product does this WELL; worth keeping\n"
    "  - ``observation`` — neutral context with no judgement\n"
    "\n"
    f"``severity`` ({'/'.join(SEVERITIES)}) only matters when kind is "
    "bug/gap/risk/nit. For praise + observation pass anything; the UI "
    "ignores it. ``category`` is the legacy axis — keep using "
    f"({'/'.join(CATEGORIES)}) for back-compat. Quote the exact on-screen "
    "wording in the body whenever copy is involved."
)


def make_note_finding_handler(
    findings: Findings,
    live_writer: LiveFindingWriter | None = None,
):
    """Return the bare async ``note_finding`` handler.

    Exposed for direct unit testing — the MCP server tuple returned by
    ``build_findings_server`` doesn't surface the inner handler in a
    stable way across claude-agent-sdk versions, so tests call this
    factory instead and exercise the handler with a plain
    ``await handler({...})``. Production code uses
    ``build_findings_server`` (which wraps this handler with the SDK's
    ``@tool`` decorator).
    """

    async def note_finding(args: dict) -> dict:
        finding = findings.add(
            kind=str(args.get("kind", "")),
            category=str(args.get("category", "")),
            severity=str(args.get("severity", "")),
            title=str(args.get("title", "")),
            body=str(args.get("body", "")),
        )
        ordinal = len(findings)
        # #1115 follow-up — fire the live writer if configured. Wrapped
        # in a try/except so a transient Mongo error doesn't crash the
        # whole tool call — the in-memory append above has already
        # succeeded, and the persona-end reconciliation will flush the
        # finding eventually.
        if live_writer is not None:
            try:
                live_writer(ordinal, finding.as_dict())
            except Exception as exc:  # noqa: BLE001 — explicitly non-fatal
                logger.warning(
                    "live_writer failed for finding #%d (non-fatal — "
                    "persona-end reconciliation will flush it): %r",
                    ordinal, exc,
                )

        # Render the ACK so the agent sees both axes — if it picked the
        # wrong kind we want the mistake visible immediately.
        tail = (
            finding.severity
            if finding.kind in FIXABLE_KINDS
            else "—"
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Noted finding #{ordinal} "
                        f"[{finding.kind}/{tail}/{finding.category}]: "
                        f"{finding.title}"
                    ),
                }
            ]
        }

    return note_finding


def build_findings_server(
    findings: Findings,
    *,
    live_writer: LiveFindingWriter | None = None,
):
    """Build the in-process MCP server exposing ``note_finding``.

    The runner creates one ``Findings`` per run and passes it here so
    every tool call lands in the same collector.

    ``live_writer`` (#1115 follow-up) is the per-call qa-store writer.
    When set, every successful ``note_finding`` call also writes the
    finding to qa-store (via ``upsert_live_finding``) so the review-UI's
    auto-refresh loop can render it within ~4s — instead of waiting for
    the persona-end batch flush in ``add_persona_result``. ``None``
    keeps the pre-#1115-follow-up behaviour (in-memory only; flushed at
    persona-end).

    Errors raised by the writer are caught and logged inside the
    handler — a Mongo blip should never crash the agent loop or lose
    the in-memory finding. The persona-end reconciliation in
    ``add_persona_result`` will still flush anything the live writer
    missed.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    handler = make_note_finding_handler(findings, live_writer=live_writer)
    decorated = tool(
        "note_finding",
        _NOTE_FINDING_DESCRIPTION,
        {
            "kind": str,
            "category": str,
            "severity": str,
            "title": str,
            "body": str,
        },
    )(handler)
    server = create_sdk_mcp_server(name="findings", tools=[decorated])
    return server, ["note_finding"]
