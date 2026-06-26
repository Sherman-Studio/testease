"""Multi-persona orchestration for the QA harness — Slice 5 (#622).

The single-persona path (``__main__`` ``--persona``) runs one persona and, if
the sink is Atlas, calls ``finish()`` itself. An orchestrated run is different:
*several* personas must land in **one** run document, so the orchestrator owns
the shared sink and the ``finish()`` call.

``run_personas`` does exactly that:

* generate ONE ``run_id`` (or honour ``QA_RUN_ID`` from config) and build ONE
  ``AtlasReportSink`` for it — every persona writes through the same sink, so
  ``AtlasReportSink`` groups them under one ``qa_runs`` document;
* run each selected persona's two-phase loop in sequence against that sink;
* call ``sink.finish()`` exactly once, after the last persona, so the run's
  combined totals are stamped;
* post one Discord run-summary alert.

The k8s CronJob (``k8s/sandbox/qa-agents-cronjob.yaml``) invokes this via
``python -m qa_agents --all``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from .accounting import RunAccounting
from .config import Config
from .discord import PersonaAlertLine, post_run_alert
from .personas import Persona, get_persona
from .report import (
    AtlasReportSink,
    ReportSink,
    RunResult,
    _extract_verdict,
    build_sink,
    new_run_id,
    write_run,
)
from .runner import run_persona
from .site_knowledge import load_by_design_block
from .tools.findings import Findings

# #1253 — production hostnames the harness is NEVER allowed to seed
# persistent persona accounts on. ``signup_or_login`` (and the other
# credential-saving setup_actions variants) uses ``_TEST_PASSWORD``, a
# constant baked into source — running those personas against
# production would create real accounts with a publicly-known
# password. Sandbox runs are fine; non-persistent personas (default
# ``setup_actions=None``) on prod are fine (they sign up with
# AI-invented passwords that decay naturally).
#
# The payment-processor cleanup cron sweeps ``@testease.example.com`` accounts
# off production within minutes, so a misconfigured run wouldn't be
# disastrous — but the guard refuses ahead of time so the operator
# never sees that few-minute exposure window.
_PROD_HOSTS = frozenset({
    "slyreply.ai",
    "www.slyreply.ai",
    "app.slyreply.ai",
})

# setup_actions variants that PERSIST credentials to qa-store. Listed
# explicitly rather than inverted from None so a future addition (e.g.
# signup_then_team) is added intentionally to one set or the other.
_PERSISTENT_SETUP_ACTIONS = frozenset({
    "signup",
    "signup_or_login",
    "signup_then_pro",
    "signup_then_power",
    "clear_credentials_then_signup",
})


def _refuse_prod_persistent_runs(
    *,
    web_base_url: str,
    personas: list[Persona],
) -> None:
    """Refuse to start a run that would create persistent persona
    accounts on the production hostname.

    The harness signs every persona up with a fixed test password
    (``setup_actions._TEST_PASSWORD``). On a sandbox URL that's fine;
    on production, that constant is a publicly-known login secret.
    Refusing here keeps the harness from ever leaving such an account
    behind, even briefly.

    Raises ``RuntimeError`` so the orchestrator's exception path
    surfaces a clear operator-friendly message. Override via
    ``QA_ALLOW_PROD_PERSISTENCE=1`` for the rare case the operator
    genuinely needs to seed prod (e.g. one-time staging migration).
    No-op when no persistent personas are in the run or when the
    URL doesn't resolve to a known production host.
    """
    host = (urlparse(web_base_url).hostname or "").lower()
    if host not in _PROD_HOSTS:
        return
    persistent = [
        p for p in personas if p.setup_actions in _PERSISTENT_SETUP_ACTIONS
    ]
    if not persistent:
        return
    if os.environ.get("QA_ALLOW_PROD_PERSISTENCE", "") == "1":
        # Operator opted in. Log but allow. Sink-level audit is enough
        # of a paper trail; we don't need to print() because every run
        # is logged.
        return
    pids = ", ".join(p.id for p in persistent)
    raise RuntimeError(
        f"Refusing to start run: target_url points at production host "
        f"{host!r} AND personas with credential-persisting setup_actions "
        f"are included ({pids}). The harness uses a fixed test password "
        f"that would land as a known-credential account on production. "
        f"Either run against a sandbox URL, remove the persistent "
        f"personas, or set QA_ALLOW_PROD_PERSISTENCE=1 if you "
        f"deliberately want this."
    )


@dataclass
class OrchestratedRun:
    """The result of an orchestrated multi-persona run."""

    run_id: str
    persona_ids: list[str]
    # Per-persona (persona_id -> locators dict from write_run).
    locators: dict[str, dict[str, str]]
    # The combined token totals for the whole run.
    totals: dict
    discord_posted: bool
    # Persona ids whose run_persona() raised — they still landed in the run
    # document via a placeholder review, but the maintainer should know which.
    failed_persona_ids: list[str] = dataclasses.field(default_factory=list)


def _placeholder_failed_result(
    persona: Persona, run_id: str, exc: BaseException, *, started_at: datetime
) -> RunResult:
    """Build a placeholder ``RunResult`` for a persona whose run crashed.

    Used when ``run_persona`` raised before producing a real result (#652).
    The placeholder still travels through the same sink interface so the
    review UI / qa_runs.reviews has a row for the failed persona with an
    honest "**Persona failed.**" markdown body and a ``failed`` verdict line —
    not a silent gap. Token accounting is empty because we cannot recover
    the partial spend from outside ``run_persona``; whatever was accrued
    inside that call is lost when its accounting object is garbage-collected.
    The shared ``Findings`` collector is NOT visible at this layer, so the
    placeholder carries an empty findings list — anything the persona noted
    before crashing lives only inside the lost ``run_persona`` frame. (A
    future refactor could move ``Findings`` ownership up here so they survive
    a crash; for now the orchestrator's job is just to not lose the run.)
    """
    finished_at = datetime.now(UTC)
    review_md = (
        "## Verdict\n\nfailed\n\n"
        "## Would I use this?\n\n"
        f"**Persona failed.** `{type(exc).__name__}: {exc}`\n\n"
        "The harness raised an unexpected error before this persona finished "
        "her run, so there is no first-person review. Triage the exception "
        "in the harness Job logs."
    )
    return RunResult(
        run_id=run_id,
        persona_id=persona.id,
        persona_display_name=persona.display_name,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        accounting=RunAccounting(),
        findings=Findings(),
        review_markdown=review_md,
        explore_digest=f"(persona run crashed: {type(exc).__name__}: {exc})",
    )


def _shared_run_id(config: Config, sink: ReportSink) -> str:
    """The id every persona of this orchestrated run is grouped under.

    The Atlas sink resolves a shared id at construction (from ``QA_RUN_ID`` or
    a generated ``qa-<ts>``); the file sink has no such notion, so fall back to
    ``config.run_id`` or a literal so the Discord alert still has something.
    """
    if isinstance(sink, AtlasReportSink):
        return sink.run_id
    return config.run_id or "qa-local"


def _totals_from_personas(results: list) -> dict:
    """Sum every persona's RunAccounting into one run-totals dict.

    Mirrors ``AtlasReportSink.finish``'s arithmetic so the Discord alert shows
    the same numbers the stored run document carries. Token counts only —
    the per-run dollar conversion was retired in #1822 (runs bill the
    operator's flat-rate Claude Code Max subscription).
    """
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_tokens": 0,
    }
    for result in results:
        acc = result.accounting
        totals["input_tokens"] += acc.total_input_tokens
        totals["output_tokens"] += acc.total_output_tokens
        totals["cache_tokens"] += acc.total_cache_tokens
    return totals


async def run_personas(
    persona_ids: list[str],
    config: Config,
    *,
    expected_persona_ids: list[str] | None = None,
) -> OrchestratedRun:
    """Run every selected persona under one shared run.

    ``persona_ids`` is THIS pod's slice of the roster — the whole roster for
    a single-pod run, or one modulo stripe for a sharded (#1821) run.
    ``expected_persona_ids`` is the COMPLETE roster the whole run covers; it
    becomes the run document's ``expected_personas`` (the finish-barrier
    denominator). ``None`` (single-pod / legacy) lets the store fall back to
    the incremental persona list, so single-pod behaviour is unchanged.

    When ``config.pod_count > 1`` the orchestrator does NOT unconditionally
    finalise the run: it calls ``AtlasReportSink.finish_if_last()`` after
    writing its stripe, and only the pod that wins the finish-barrier claim
    runs ``finish_run`` + posts the Discord alert. Single-pod runs
    (``pod_count <= 1``) keep the original unconditional ``finish()`` + alert.

    All personas share ONE sink — for ``QA_SINK=atlas`` that means one
    ``qa_runs`` document, with ``finish()`` called exactly once at the end.

    Personas execute concurrently under an ``asyncio.Semaphore`` capped at
    ``config.concurrency`` (#824). Each ``run_persona`` call already builds
    its own browser session, Mailpit/SMTP client, and Anthropic SDK client —
    no shared mutable state — and per-persona mailbox isolation is guaranteed
    because every persona has a distinct ``@example.com`` registered address
    that the email tools filter on. ``concurrency=1`` is a clean fallback to
    the original sequential loop.

    A ``write_run`` lock serialises sink writes across the concurrent tasks.
    The qa-store layer is pymongo (synchronous) and ``create_run`` /
    ``add_persona_result`` are read-modify-write sequences (``find_one``
    followed by ``update_one``) — two concurrent calls touching the run doc
    at the same instant could race on the persona list or the reviews array.
    An ``asyncio.Lock`` is cheap and removes the entire class of race; given
    the sink writes are short (~tens of ms) compared with a 75-min persona
    explore phase, the serialisation cost is negligible.
    """
    # Resolve personas up front so a bad id fails before any expensive work.
    personas = [get_persona(pid) for pid in persona_ids]

    # #1253 — production-target safety check. Refuses runs that would
    # plant persistent-credential persona accounts on the prod hostname.
    # Raises RuntimeError if the combination is unsafe; no-ops otherwise.
    _refuse_prod_persistent_runs(
        web_base_url=config.web_base_url,
        personas=personas,
    )

    # ONE sink for the whole run. The Atlas sink fixes its shared run id here,
    # at construction, so every persona's write_summary lands in one document.
    # #1821 — seed the finish-barrier denominator (expected_personas) on the
    # sink so the FIRST create_run writes the COMPLETE roster, not just this
    # pod's stripe. None falls back to the incremental persona list.
    sink = build_sink(config, expected_personas=expected_persona_ids)
    run_id = _shared_run_id(config, sink)

    # Load the target's by-design site_knowledge ONCE for the whole run (#2097);
    # every persona's explore prompt reuses it. Graceful → "" if no store.
    by_design_block = load_by_design_block(config)

    # Bounded concurrency. Clamp to at least 1 so a misconfigured 0/-1 from
    # the env can never deadlock the run (a 0-permit semaphore would block
    # forever the moment we try to enter it).
    concurrency = max(1, int(config.concurrency or 1))
    semaphore = asyncio.Semaphore(concurrency)
    # Serialise sink writes — see the function docstring.
    sink_lock = asyncio.Lock()

    print(
        f"==> orchestrator: launching {len(personas)} personas "
        f"under shared run {run_id} (concurrency cap: {concurrency})",
        file=sys.stderr,
    )

    # Per-task containers keyed by persona id; assembling them once up front
    # lets us preserve the INPUT order in the OrchestratedRun result, even
    # though tasks may finish out of order.
    results_by_id: dict[str, RunResult] = {}
    locators: dict[str, dict[str, str]] = {}
    failed_persona_ids: list[str] = []
    # We append failed ids from concurrent tasks; use a lock so the list
    # mutation is well-defined under arbitrary scheduling. The orchestrator
    # later sorts this back into input order anyway.
    failed_lock = asyncio.Lock()

    async def _run_one(persona: Persona) -> None:
        async with semaphore:
            print(
                f"==> orchestrator: persona {persona.id!r} "
                f"({persona.display_name})  run {run_id}",
                file=sys.stderr,
            )
            # Pin every persona's process to the shared run id so a
            # single-process orchestrated run is consistent even for the
            # file sink.
            persona_config = dataclasses.replace(
                config, persona=persona.id, run_id=run_id
            )
            started_at = datetime.now(UTC)
            try:
                result = await run_persona(
                    persona, persona_config, by_design_block=by_design_block,
                )
            except Exception as exc:  # noqa: BLE001 - a single persona must not lose the whole run (#652)
                print(
                    f"ERROR: persona {persona.id!r} raised "
                    f"{type(exc).__name__}: {exc!r}; recording a "
                    "placeholder review and continuing with the "
                    "remaining personas",
                    file=sys.stderr,
                )
                result = _placeholder_failed_result(
                    persona,
                    # The placeholder's per-persona run_id is informational
                    # only; the Atlas sink groups it under the SHARED run
                    # id from the sink itself. We mint a sortable id for
                    # the file sink's row.
                    new_run_id(persona.id, now=started_at),
                    exc,
                    started_at=started_at,
                )
                async with failed_lock:
                    failed_persona_ids.append(persona.id)
            results_by_id[persona.id] = result
        # Sink writes happen OUTSIDE the persona-concurrency semaphore but
        # INSIDE a dedicated lock so they never overlap. Keeping the sink
        # lock orthogonal to the persona semaphore means a slow sink write
        # cannot starve a finished persona's slot in the gather.
        async with sink_lock:
            # write_run goes through the SAME sink interface — so a failed
            # persona still lands in qa_runs.reviews and the review UI
            # shows it.
            locators[persona.id] = write_run(result, sink)

    tasks = [asyncio.create_task(_run_one(p)) for p in personas]
    # return_exceptions=True is defensive — _run_one already catches the
    # persona's own Exception and writes a placeholder, but a programming
    # error (e.g. write_run raising) shouldn't take the whole gather down
    # silently. Any task-level exception is re-raised below.
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            # Programming error inside _run_one itself (NOT a persona crash —
            # those are already caught and turned into placeholder results).
            # Surface it so the caller sees the failure rather than silently
            # writing an under-populated run document.
            raise outcome

    # Preserve INPUT order for the results list — the orchestrator's outputs
    # (Discord alert, persona_ids, totals iteration) all expect this stable
    # ordering even though tasks may have finished in any order.
    results = [results_by_id[p.id] for p in personas]
    # Same for the failed-ids list — sorted by INPUT order, not finish order.
    failed_persona_ids = [p.id for p in personas if p.id in failed_persona_ids]

    # Finalisation. Two shapes:
    #
    #  * Single-pod (pod_count <= 1, the default / legacy path): this one
    #    process wrote every persona, so it unconditionally owns the finish.
    #    ALWAYS call finish() even if some personas failed, so the run doc is
    #    stamped and totals recomputed from the (possibly partial) reviews.
    #    The Discord alert is then posted unconditionally below — unchanged.
    #
    #  * Multi-pod (pod_count > 1, #1821): N pods each wrote their stripe.
    #    finish_if_last() opens the finish barrier — only the pod that writes
    #    the last expected persona AND wins the atomic claim runs finish_run.
    #    The Discord alert is gated on winning that claim so it fires exactly
    #    once across the whole run, not once per pod.
    won_finish = True
    if isinstance(sink, AtlasReportSink):
        if config.pod_count > 1:
            won_finish = sink.finish_if_last()
            if won_finish:
                print(
                    f"==> orchestrator: pod {config.pod_index} won the finish "
                    f"barrier for {run_id} — finalising",
                    file=sys.stderr,
                )
            else:
                print(
                    f"==> orchestrator: pod {config.pod_index} is not the last "
                    f"pod for {run_id} (or lost the claim) — standing down, "
                    "no finalisation/alert",
                    file=sys.stderr,
                )
        else:
            sink.finish()

    totals = _totals_from_personas(results)

    # Post the Discord run-summary alert. ALWAYS attempted, even if every
    # persona failed — a maintainer needs to know the Job finished and how
    # it broke down (clean / truncated / failed). A Discord outage itself
    # must never fail an otherwise-good run, so HTTP errors are caught and
    # logged, not raised.
    alert_lines = []
    for r in results:
        verdict = _extract_verdict(r.review_markdown)
        # Mark a failed persona explicitly in the Discord line so the
        # maintainer can see at-a-glance which one needs triage.
        if r.persona_id in failed_persona_ids and not verdict:
            verdict = "failed"
        alert_lines.append(
            PersonaAlertLine(
                persona_id=r.persona_id,
                verdict=verdict,
                findings_count=len(r.findings),
            )
        )
    clean = sum(
        1
        for r in results
        if r.persona_id not in failed_persona_ids
        and "explore phase ended early" not in (r.explore_digest or "")
    )
    truncated = sum(
        1
        for r in results
        if r.persona_id not in failed_persona_ids
        and "explore phase ended early" in (r.explore_digest or "")
    )
    failed = len(failed_persona_ids)
    print(
        f"==> orchestrator: run {run_id} breakdown — {clean} clean, "
        f"{truncated} truncated, {failed} failed (of {len(results)} total)",
        file=sys.stderr,
    )
    discord_posted = False
    # #1821 — a multi-pod loser pod (one that didn't win the finish barrier)
    # posts NO Discord alert: the alert is the run-level "we're done" signal
    # and must fire exactly once, from the finalising pod. ``won_finish`` is
    # True for every single-pod run, so this gate is a no-op there.
    if not won_finish:
        print(
            f"==> orchestrator: pod {config.pod_index} did not finalise "
            f"{run_id} — skipping Discord alert (the winning pod posts it)",
            file=sys.stderr,
        )
    try:
        if won_finish:
            discord_posted = post_run_alert(
                config.discord_webhook_url, run_id, alert_lines, totals
            )
        if discord_posted:
            print(f"==> orchestrator: posted Discord alert for {run_id}", file=sys.stderr)
        elif won_finish:
            print(
                "==> orchestrator: QA_DISCORD_WEBHOOK_URL unset — "
                "skipping Discord alert",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 - a Discord blip must not fail the run
        print(
            f"WARNING: Discord alert for {run_id} failed: {exc!r} "
            "(the run itself is unaffected)",
            file=sys.stderr,
        )

    return OrchestratedRun(
        run_id=run_id,
        persona_ids=[p.id for p in personas],
        locators=locators,
        totals=totals,
        discord_posted=discord_posted,
        failed_persona_ids=failed_persona_ids,
    )
