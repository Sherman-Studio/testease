"""Rendering + output for a persona run.

Two responsibilities, deliberately separated so Slice 5/6 can swap the sink:

1. Pure rendering — ``build_run_summary`` and ``render_review_markdown`` turn
   a finished run into a JSON-able dict and a markdown string. No I/O.
2. The ``ReportSink`` interface — where those artifacts go. ``FileReportSink``
   writes them to a directory; Slice 5/6 adds a MongoDB Atlas sink that
   implements the same interface. The runner only knows the interface.
"""

from __future__ import annotations

import abc
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from .accounting import RunAccounting
from .tools.findings import Findings

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    """Everything a finished persona run produces, ready to render or store."""

    run_id: str
    persona_id: str
    persona_display_name: str
    started_at: str
    finished_at: str
    accounting: RunAccounting
    findings: Findings
    review_markdown: str
    explore_digest: str = ""

    @property
    def num_turns(self) -> int:
        return self.accounting.total_turns


# ---------------------------------------------------------------------------
# Pure rendering.
# ---------------------------------------------------------------------------
def build_run_summary(result: RunResult) -> dict:
    """Build the ``run-summary.json`` payload — a stable, JSON-able dict."""
    return {
        "run_id": result.run_id,
        "persona": result.persona_id,
        "persona_display_name": result.persona_display_name,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "num_turns": result.num_turns,
        "accounting": result.accounting.as_dict(),
        "findings": result.findings.as_list(),
        "findings_count": len(result.findings),
        "findings_by_severity": result.findings.counts_by_severity(),
        "findings_by_category": result.findings.counts_by_category(),
    }


def _accounting_table(acc: RunAccounting) -> str:
    """Render the per-phase token block as a markdown table.

    Token counts only — the per-run dollar column was retired in #1822
    (runs bill the operator's flat-rate Claude Code Max subscription).
    """
    lines = [
        "| Phase | Model | Input | Output | Cache | Turns | Total tokens |",
        "|---|---|--:|--:|--:|--:|--:|",
    ]
    for p in acc.phases:
        cache = p.cache_creation_input_tokens + p.cache_read_input_tokens
        lines.append(
            f"| {p.phase} | {p.model} | {p.input_tokens:,} | "
            f"{p.output_tokens:,} | {cache:,} | {p.num_turns} | "
            f"{p.total_tokens:,} |"
        )
    lines.append(
        f"| **Run total** | | {acc.total_input_tokens:,} | "
        f"{acc.total_output_tokens:,} | {acc.total_cache_tokens:,} | "
        f"{acc.total_turns} | **{acc.total_tokens:,}** |"
    )
    return "\n".join(lines)


def _findings_appendix(findings: Findings) -> str:
    """Render the structured findings list for triage."""
    if not len(findings):
        return "_No findings were recorded during this run._"
    lines: list[str] = []
    for i, f in enumerate(findings, 1):
        lines.append(f"{i}. **[{f.category}/{f.severity}]** {f.title}")
        if f.body:
            for body_line in f.body.splitlines():
                lines.append(f"   {body_line}")
    return "\n".join(lines)


def render_review_markdown(result: RunResult) -> str:
    """Render the full persona review markdown document.

    The persona-voiced review (from the report phase) leads; the accounting
    block and the structured findings appendix follow it for the maintainer.
    """
    sev = result.findings.counts_by_severity()
    summary_line = (
        f"{len(result.findings)} findings "
        f"({sev['blocker']} blocker, {sev['major']} major, "
        f"{sev['minor']} minor, {sev['nit']} nit)"
    )
    parts = [
        f"# QA persona review — {result.persona_display_name}",
        "",
        f"- **Persona:** `{result.persona_id}`",
        f"- **Run id:** `{result.run_id}`",
        f"- **Started:** {result.started_at}",
        f"- **Finished:** {result.finished_at}",
        f"- **Findings:** {summary_line}",
        "",
        "## Run accounting",
        "",
        _accounting_table(result.accounting),
        "",
        "## Review",
        "",
        result.review_markdown.strip(),
        "",
        "## Findings appendix",
        "",
        _findings_appendix(result.findings),
        "",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Output sink interface — swappable in Slice 5/6.
# ---------------------------------------------------------------------------
class ReportSink(abc.ABC):
    """Where a finished run's artifacts go.

    ``FileReportSink`` writes to disk today; a future ``MongoReportSink`` will
    implement the same two methods. The runner depends only on this interface.
    """

    @abc.abstractmethod
    def write_review(self, result: RunResult, markdown: str) -> str:
        """Persist the markdown review; return a locator (path / id / url)."""

    @abc.abstractmethod
    def write_summary(self, result: RunResult, summary: dict) -> str:
        """Persist the run-summary payload; return a locator."""


class FileReportSink(ReportSink):
    """Writes ``<persona>-review.md`` and ``run-summary.json`` into a dir."""

    def __init__(self, out_dir: str) -> None:
        self.out_dir = out_dir

    def _ensure_dir(self) -> None:
        os.makedirs(self.out_dir, exist_ok=True)

    def write_review(self, result: RunResult, markdown: str) -> str:
        self._ensure_dir()
        path = os.path.join(self.out_dir, f"{result.persona_id}-review.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        return path

    def write_summary(self, result: RunResult, summary: dict) -> str:
        self._ensure_dir()
        path = os.path.join(self.out_dir, "run-summary.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, sort_keys=True)
            fh.write("\n")
        return path


def _extract_verdict(review_markdown: str) -> str:
    """Best-effort one-line verdict pulled from the persona review markdown.

    The review template ends with a "Would I use this?" section; we grab the
    first non-empty prose line under whichever final ``## …`` heading mentions
    a verdict. This is purely cosmetic (the runs table shows it) — if nothing
    matches we return an empty string and the UI just shows the run status.
    """
    lines = review_markdown.splitlines()
    verdict_idx: int | None = None
    for i, line in enumerate(lines):
        low = line.lower()
        if low.startswith("#") and ("would i" in low or "verdict" in low):
            verdict_idx = i
    if verdict_idx is None:
        return ""
    for line in lines[verdict_idx + 1 :]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped.lstrip("*_ ").strip()
    return ""


class AtlasReportSink(ReportSink):
    """Writes a persona's result into the shared ``slyreply_qa`` Atlas store.

    Implements the same ``ReportSink`` interface as ``FileReportSink`` so the
    runner is sink-agnostic. Unlike the file sink, this one groups every
    persona of one orchestrated job under a **single shared run document**:

    * The shared ``run_id`` is read from ``config.run_id`` (``QA_RUN_ID``,
      which Slice 5's orchestration sets once per Job). If it is unset, the
      sink generates one — ``qa-<UTC timestamp>`` — and reuses it for the
      lifetime of the sink instance, so personas run in the same process still
      land in one run.
    * ``write_summary`` is where the work happens: it upserts the run doc
      (idempotent ``create_run``), then ``add_persona_result`` appends this
      persona's review + accounting + findings. ``write_review`` is a no-op
      locator — the markdown travels inside the summary's ``review_markdown``.

    The store handle is created lazily on first write so importing this module
    (e.g. in the harness tests) never opens a Mongo connection.
    """

    def __init__(self, config, expected_personas=None) -> None:  # noqa: ANN001 - avoids a Config import cycle
        self._config = config
        self._store = None
        # Resolve the shared run id once, here, so all personas share it.
        self._run_id = config.run_id or self._generate_run_id()
        # #1821 — the COMPLETE persona roster the whole (possibly sharded)
        # run will cover. This is the finish-barrier denominator passed to
        # create_run as expected_personas; it is set ONCE and sticky in the
        # store. ``None`` (single-pod / legacy callers) lets create_run fall
        # back to the incremental persona list — so a single pod that writes
        # every persona still gets the full set as its denominator for free.
        self._expected_personas = (
            list(expected_personas) if expected_personas is not None else None
        )

    @staticmethod
    def _generate_run_id() -> str:
        return f"qa-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"

    @property
    def run_id(self) -> str:
        """The shared run id every persona of this job is grouped under."""
        return self._run_id

    def _ensure_store(self):
        """Connect to the qa-store lazily; reuse the handle across personas."""
        if self._store is None:
            from qa_store import connect

            self._store = connect(
                self._config.qa_store_url, self._config.qa_store_db
            )
        return self._store

    def write_review(self, result: RunResult, markdown: str) -> str:
        """No-op for Atlas — the review markdown is stored via write_summary.

        Returns a store locator string so the runner still has something to
        log. The interface contract (return a locator) is honoured.
        """
        return f"atlas://{self._config.qa_store_db}/qa_runs/{self._run_id}"

    def write_summary(self, result: RunResult, summary: dict) -> str:
        """Upsert the shared run doc and append this persona's result.

        Idempotent: ``create_run`` upserts; ``add_persona_result`` replaces the
        persona's slice. A re-run of one persona overwrites cleanly.
        """
        from qa_store import add_persona_result, create_run

        store = self._ensure_store()
        # Idempotent upsert — every persona calls this; the first wins, the
        # rest just merge the persona list.
        #
        # #858 — pass the operator-facing run_notes + a config_snapshot of
        # the run's actual resolved knobs through to create_run. The store's
        # sticky-merge keeps the FIRST persona's notes/snapshot, so an
        # operator triggering "smoke test before billing migration" doesn't
        # have those notes silently overwritten by a later persona's call.
        create_run(
            store,
            self._run_id,
            [result.persona_id],
            run_notes=self._config.run_notes,
            # #1821 — the finish-barrier denominator. Sticky/set-once in the
            # store, so passing it on every persona's write is harmless: the
            # first create_run for this run fixes it. None ⇒ create_run falls
            # back to the persona list (single-pod legacy behaviour).
            expected_personas=self._expected_personas,
            config_snapshot={
                "max_turns": self._config.max_turns,
                "concurrency": self._config.concurrency,
                "explore_model": self._config.explore_model,
                "report_model": self._config.report_model,
                # #861 — record what the operator selected as mandatory
                # at trigger time so the run-detail page can render the
                # per-persona attempted/not-attempted checklist months
                # later (the harness env QA_MANDATORY_ACTIONS is gone
                # once the Job ends; this is the only persisted trace).
                "mandatory_action_ids": list(self._config.mandatory_action_ids),
            },
        )
        markdown = render_review_markdown(result)
        add_persona_result(
            store,
            self._run_id,
            result.persona_id,
            review_markdown=markdown,
            verdict=_extract_verdict(result.review_markdown),
            accounting=result.accounting.as_dict(),
            findings=result.findings.as_list(),
        )
        # Slice 2.1 of #1104 — cross-run finding dedup. For each finding
        # just written, look up the most recent prior with the same
        # (persona, category, title_hash) and update this row's
        # recurring_count + is_regression. Best-effort: a dedup error
        # must not void the write that's already landed.
        try:
            from qa_store import apply_cross_run_dedup_for_run  # noqa: PLC0415

            dedup_counts = apply_cross_run_dedup_for_run(store, self._run_id)
            if dedup_counts["matched_priors"] or dedup_counts["regressions"]:
                logger.info(
                    "finding dedup: %s/%s — matched_priors=%d regressions=%d",
                    self._run_id, result.persona_id,
                    dedup_counts["matched_priors"], dedup_counts["regressions"],
                )
        except Exception as exc:  # noqa: BLE001 — non-fatal by design
            logger.warning(
                "finding dedup failed for %s/%s (non-fatal): %s",
                self._run_id, result.persona_id, exc,
            )
        # Slice 1 of #1002 — discovery distillation. Fires per persona,
        # non-fatal on any error (the run already has its review +
        # findings written by the call above; distillation failure
        # shouldn't void that). Disable with QA_DISTILLATION_ENABLED=0
        # in the harness env if cost spikes.
        try:
            from qa_store import distill_persona_run  # noqa: PLC0415

            counts = distill_persona_run(store, self._run_id, result.persona_id)
            if counts["actions"] or counts["tools"] or counts["branches"]:
                logger.info(
                    "qa_distillation: persisted %d actions / %d tools / "
                    "%d branches for %s/%s",
                    counts["actions"], counts["tools"], counts["branches"],
                    self._run_id, result.persona_id,
                )
        except Exception as exc:  # noqa: BLE001  — non-fatal by design
            logger.warning(
                "qa_distillation failed for %s/%s (non-fatal): %s",
                self._run_id, result.persona_id, exc,
            )
        return f"atlas://{self._config.qa_store_db}/qa_runs/{self._run_id}"

    @staticmethod
    def _totals_from_run(run: dict) -> dict:
        """Sum the run document's per-persona ``reviews`` accounting into the
        run-level totals dict ``finish_run`` expects.

        Pure over the doc — no store access — so the single-pod ``finish()``,
        the multi-pod ``finish_if_last()`` winner, and the ``finalize_stranded``
        reaper all stamp identical totals.
        """
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_tokens": 0,
            # #882 — backend defaults to ``api`` so a run with no
            # reviews (zero-persona edge case) still gets a sensible
            # value. Overwritten from the first review's accounting
            # below — one orchestrator run uses one backend by
            # construction.
            "backend": "api",
        }
        backend_seen: str | None = None
        for review in run.get("reviews") or []:
            acc = review.get("accounting") or {}
            totals["input_tokens"] += int(acc.get("total_input_tokens", 0) or 0)
            totals["output_tokens"] += int(acc.get("total_output_tokens", 0) or 0)
            totals["cache_tokens"] += int(acc.get("total_cache_tokens", 0) or 0)
            backend_seen = acc.get("backend", backend_seen) or backend_seen
        if backend_seen is not None:
            totals["backend"] = backend_seen
        return totals

    def finish(self) -> None:
        """Stamp the shared run finished, summing all personas' accounting.

        Called once by the orchestrator after every persona has been written
        (the per-persona sink interface has no natural "all done" hook). Safe
        to call even if no persona was written — it is a no-op then.

        This is the SINGLE-POD / ``--finalize``-free path: the single process
        wrote every persona, so it unconditionally owns the finalisation.
        Multi-pod runs go through ``finish_if_last()`` instead.
        """
        from qa_store import finish_run

        store = self._ensure_store()
        run = store.runs.find_one({"run_id": self._run_id})
        if run is None:
            return
        finish_run(store, self._run_id, self._totals_from_run(run))

    def finish_if_last(self) -> bool:
        """Multi-pod finish barrier (#1821): finalise iff this is the last pod.

        Each pod calls this after writing its stripe of personas. The barrier:

        1. Re-read the run doc and check ``all_personas_reviewed`` — has every
           ``expected_personas`` entry filed a review yet? If not, this pod
           isn't last; return ``False`` and stand down.
        2. Otherwise attempt ``claim_run_finish`` — an atomic compare-and-set
           so exactly ONE of the pods that observe (1) wins. The winner runs
           the existing totals computation + ``finish_run`` and returns
           ``True`` (the caller gates its Discord alert on this). Every loser
           gets ``False`` and posts nothing.

        Returns ``True`` iff THIS call performed the finalisation (won the
        claim). A two-pod race ends with exactly one ``True`` across all
        pods, so ``finish_run`` + the Discord alert fire exactly once.
        """
        from qa_store import all_personas_reviewed, claim_run_finish, finish_run

        store = self._ensure_store()
        if not all_personas_reviewed(store, self._run_id):
            return False
        if not claim_run_finish(store, self._run_id):
            return False
        # We won the claim — we're the one and only finaliser. Re-read so the
        # totals include every pod's just-written review.
        run = store.runs.find_one({"run_id": self._run_id})
        if run is None:  # pragma: no cover - claim succeeded ⇒ doc exists
            return False
        finish_run(store, self._run_id, self._totals_from_run(run))
        return True

    def finalize_stranded(self, run_id: str | None = None) -> None:
        """Reaper (#1821): force-finish a run stranded at status ``new``.

        Used by ``python -m qa_agents --finalize <run_id>`` when a pod crashed
        or was evicted before writing its slice, so the last-expected-persona
        review never landed and no pod ever won the finish barrier. Writes a
        placeholder review for every still-missing ``expected_personas`` entry
        (so the run document is shape-complete for the review UI), then stamps
        the run finished with whatever per-persona totals are present.

        Defaults ``run_id`` to this sink's own run id so it composes with the
        construction-time run id, but accepts an explicit id for the CLI path.
        """
        from qa_store import add_persona_result, finish_run

        rid = run_id or self._run_id
        store = self._ensure_store()
        run = store.runs.find_one({"run_id": rid})
        if run is None:
            return
        expected = list(run.get("expected_personas") or [])
        reviewed = {r.get("persona") for r in (run.get("reviews") or [])}
        missing = [p for p in expected if p not in reviewed]
        for persona_id in missing:
            add_persona_result(
                store,
                rid,
                persona_id,
                review_markdown=(
                    "## Verdict\n\nstranded\n\n"
                    "## Would I use this?\n\n"
                    "**Persona never reported.** The pod assigned this persona "
                    "crashed or was evicted before writing its review, and the "
                    "run was force-finished by the `--finalize` reaper. There "
                    "is no first-person review — triage the pod's Job logs."
                ),
                verdict="stranded",
                accounting={},
                findings=[],
            )
        # Re-read so the placeholder rows are included in the totals scan, then
        # stamp finished with whatever's present.
        run = store.runs.find_one({"run_id": rid})
        finish_run(store, rid, self._totals_from_run(run))


def write_run(result: RunResult, sink: ReportSink) -> dict[str, str]:
    """Render + persist a finished run through a sink. Returns the locators."""
    summary = build_run_summary(result)
    markdown = render_review_markdown(result)
    review_loc = sink.write_review(result, markdown)
    summary_loc = sink.write_summary(result, summary)
    return {"review": review_loc, "summary": summary_loc}


def build_sink(config, expected_personas=None) -> ReportSink:  # noqa: ANN001 - avoids a Config import cycle
    """Select the report sink from ``config.sink`` (``QA_SINK``).

    ``file`` → ``FileReportSink`` (writes into ``config.out_dir``).
    ``atlas`` → ``AtlasReportSink`` (upserts into the ``slyreply_qa`` store).
    The runner / CLI calls this so the sink choice is one env var, not code.

    ``expected_personas`` (#1821) is only meaningful for the Atlas sink — it
    seeds the run document's finish-barrier denominator. ``None`` (every
    single-persona caller, the file sink) preserves the legacy behaviour.
    """
    sink = (config.sink or "file").strip().lower()
    if sink == "atlas":
        return AtlasReportSink(config, expected_personas=expected_personas)
    if sink == "file":
        return FileReportSink(config.out_dir)
    raise ValueError(f"QA_SINK must be 'file' or 'atlas', got {config.sink!r}")


def new_run_id(persona_id: str, *, now: datetime | None = None) -> str:
    """Build a sortable, human-readable run id for a persona."""
    now = now or datetime.now(UTC)
    return f"{persona_id}-{now.strftime('%Y%m%dT%H%M%SZ')}"
