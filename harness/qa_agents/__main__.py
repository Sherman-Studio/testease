"""CLI entrypoint for the QA persona harness.

    python -m qa_agents --persona margaret [--out DIR]   # one persona
    python -m qa_agents --all                            # every persona
    python -m qa_agents --personas margaret,priya        # a subset

Single-persona mode runs one persona and writes its review through the report
sink (file by default, Atlas when ``QA_SINK=atlas``).

Multi-persona mode (``--all`` / ``--personas``) is Slice 5's orchestrator: it
generates ONE shared ``run_id``, builds ONE ``AtlasReportSink``, runs every
selected persona's two-phase loop in sequence against that single sink, calls
the sink's ``finish()`` exactly once at the end, then posts a Discord alert.
The sandbox CronJob invokes ``python -m qa_agents --all``.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys

from .config import Config
from .orchestrator import run_personas
from .personas import PERSONAS, get_persona, personas_in_group
from .report import AtlasReportSink, build_sink, write_run
from .runner import run_persona
from .site_knowledge import load_by_design_block


def _describe_env_var(name: str) -> str:
    """Render a single auth-env-var presence indicator for the startup
    banner. Reports presence + length only — NEVER the value.

    #906 — when the cluster Max-billed Job started accidentally billing
    the org's API workspace instead of the operator's Max account
    (because CLAUDE_CODE_OAUTH_TOKEN turned out to be an API-workspace
    token in disguise), the only way to tell from `kubectl logs` what
    auth shape the harness had inherited was to add this banner.
    The next time something is off, the wrong shape is visible without
    needing console-side forensics.
    """
    raw = os.environ.get(name)
    if raw is None:
        return f"{name}=absent"
    raw = raw.strip()
    if not raw:
        return f"{name}=empty"
    return f"{name}=present({len(raw)} chars)"


def _log_auth_banner(config: Config) -> None:
    """Emit one line summarising which auth env vars are visible to the
    spawned ``claude`` CLI. Goes to stderr so it lands in the same stream
    as the rest of the harness narration.

    Never prints the value of any secret, only presence + length —
    safe to leave on permanently in production.
    """
    parts = [
        "backend=claude-code (Max)",
        _describe_env_var("CLAUDE_CODE_OAUTH_TOKEN"),
    ]
    print(f"==> harness starting · {' · '.join(parts)}", file=sys.stderr)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="qa_agents",
        description=(
            "Run SlyReply QA personas: autonomous Claude agents that play "
            "fictional users, walk the app, and write reviews. Run one "
            "persona, a named subset, or all of them under one shared run."
        ),
    )
    parser.add_argument(
        "--persona",
        help="Single persona id to run (default: $QA_PERSONA or 'margaret'). "
        f"Implemented: {', '.join(sorted(PERSONAS))}.",
    )
    parser.add_argument(
        "--personas",
        help="Comma-separated list of persona ids to run together under one "
        "shared run id (e.g. 'margaret,priya').",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run every implemented persona together under one shared run id. "
        "This is what the sandbox CronJob invokes.",
    )
    parser.add_argument(
        "--group",
        choices=("target", "core", "technical"),
        help="Run every persona in one group under a shared run id (#616). "
        "'target' = the not-so-savvy / small-business users; 'core' = "
        "lifecycle/billing journeys; 'technical' = the opt-in a11y/perf/"
        "security/i18n/api sweep. Recommended default: 'target'.",
    )
    parser.add_argument(
        "--out",
        help="Output directory for the review + run-summary (default: "
        "$QA_OUT_DIR or ./qa-runs). Used by the file sink only.",
    )
    parser.add_argument(
        "--finalize",
        metavar="RUN_ID",
        help="Reaper mode (#1821): force-finish a multi-pod run whose doc "
        "is still 'new' — e.g. a pod crashed/was evicted before writing its "
        "slice, so no pod ever won the finish barrier. Writes a placeholder "
        "review for every still-missing expected persona, then stamps the "
        "run finished with whatever totals are present. No personas are run.",
    )
    return parser.parse_args(argv)


def _stripe_personas(
    persona_ids: list[str], *, pod_index: int, pod_count: int
) -> list[str]:
    """Select THIS pod's stripe of a multi-pod run's persona roster (#1821).

    The roster is sorted first so the assignment is deterministic and
    independent of the caller's input order (``--all`` already sorts, but
    ``--personas a,b`` does not). Pod ``i`` of ``pod_count`` runs
    ``roster[j]`` for every ``j`` where ``j % pod_count == i``.

    Modulo-stripe (rather than contiguous chunks) so heavy and light
    personas spread evenly across pods — a contiguous split would land all
    the alphabetically-clustered heavy personas on one pod. The stripe is
    disjoint and complete: every persona is run by exactly one pod.

    ``pod_count <= 1`` is the single-pod path: the whole sorted roster, so
    behaviour is identical to the pre-#1821 orchestrator.
    """
    roster = sorted(persona_ids)
    if pod_count <= 1:
        return roster
    return [p for j, p in enumerate(roster) if j % pod_count == pod_index]


def resolve_config(args: argparse.Namespace) -> Config:
    """Build the run config from env, applying CLI overrides."""
    config = Config.from_env()
    overrides: dict = {}
    if args.persona:
        overrides["persona"] = args.persona
    if args.out:
        overrides["out_dir"] = args.out
    if overrides:
        config = dataclasses.replace(config, **overrides)
    return config


def _selected_personas(args: argparse.Namespace) -> list[str] | None:
    """Resolve the multi-persona selection, or None for single-persona mode.

    ``--all`` wins over ``--group``, which wins over ``--personas``; all three
    are multi-persona. Returns ``None`` when none is given (the caller then
    runs the single-persona path).
    """
    if args.all:
        return sorted(PERSONAS)
    if getattr(args, "group", None):
        return personas_in_group(args.group)
    if args.personas:
        ids = [p.strip() for p in args.personas.split(",") if p.strip()]
        if not ids:
            raise ValueError("--personas was given but listed no persona ids")
        return ids
    return None


async def _run_single(config: Config) -> int:
    """Run one persona — the original single-persona path."""
    try:
        persona = get_persona(config.persona)
    except KeyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(
        f"==> qa-agents: running persona {persona.id!r} "
        f"({persona.display_name})\n"
        f"    web:     {config.web_base_url}\n"
        f"    smtp:    {config.smtp_host}:{config.smtp_port}\n"
        f"    mailpit: {config.mailpit_url}\n"
        f"    explore model: {config.explore_model}\n"
        f"    report model:  {config.report_model}\n"
        f"    max turns: {config.max_turns}  timeout: {config.run_timeout_s}s",
        file=sys.stderr,
    )

    # Load this target's by-design knowledge once (graceful → "" if no store).
    by_design_block = load_by_design_block(config)
    result = await run_persona(persona, config, by_design_block=by_design_block)

    # When the harness runs a single persona per process, finish() is called
    # here so the run's totals are stamped. The multi-persona orchestrator
    # (--all / --personas) instead shares one sink and calls finish() once.
    sink = build_sink(config)
    locators = write_run(result, sink)
    if isinstance(sink, AtlasReportSink):
        sink.finish()
        print(f"    atlas run: {sink.run_id}", file=sys.stderr)

    acc = result.accounting
    print(
        f"==> done: {len(result.findings)} findings, "
        f"{acc.total_turns} turns, "
        f"{acc.total_tokens:,} tokens\n"
        f"    review:  {locators['review']}\n"
        f"    summary: {locators['summary']}",
        file=sys.stderr,
    )
    return 0


async def _run_multi(
    persona_ids: list[str],
    config: Config,
    *,
    expected_persona_ids: list[str] | None = None,
) -> int:
    """Run several personas under one shared run — the Slice 5 orchestrator.

    ``persona_ids`` is THIS pod's slice (the full roster for a single-pod
    run; a stripe for a sharded one). ``expected_persona_ids`` (#1821) is
    the COMPLETE roster the whole run will cover — the finish-barrier
    denominator. ``None`` (single-pod) falls back to ``persona_ids``.
    """
    try:
        # Validate every id up front for a clear error before any work.
        for pid in persona_ids:
            get_persona(pid)
    except KeyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    shard_note = ""
    if config.pod_count > 1:
        shard_note = (
            f"    shard:   pod {config.pod_index} of {config.pod_count} "
            f"(stripe of {len(expected_persona_ids or persona_ids)} total)\n"
        )
    print(
        f"==> qa-agents: orchestrated run of {len(persona_ids)} personas: "
        f"{', '.join(persona_ids)}\n"
        f"{shard_note}"
        f"    web:     {config.web_base_url}\n"
        f"    smtp:    {config.smtp_host}:{config.smtp_port}\n"
        f"    mailpit: {config.mailpit_url}\n"
        f"    sink:    {config.sink}\n"
        f"    explore model: {config.explore_model}\n"
        f"    report model:  {config.report_model}\n"
        f"    concurrency:   {config.concurrency}",
        file=sys.stderr,
    )

    run = await run_personas(
        persona_ids, config, expected_persona_ids=expected_persona_ids
    )

    t = run.totals
    print(
        f"==> orchestrated run {run.run_id} complete: "
        f"{len(run.persona_ids)} personas\n"
        f"    totals: {t['input_tokens']:,} in / {t['output_tokens']:,} out / "
        f"{t['cache_tokens']:,} cache\n"
        f"    discord alert: {'posted' if run.discord_posted else 'skipped'}",
        file=sys.stderr,
    )
    return 0


async def _async_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    config = resolve_config(args)

    # #906 — log the resolved backend + auth-env-var presence before
    # anything that could swallow the info into a long traceback. If a
    # future run accidentally hits the wrong workspace (the bug this
    # banner exists for), the first three lines of `kubectl logs` will
    # make the cause obvious instead of requiring console-side billing
    # forensics.
    _log_auth_banner(config)

    # Reaper mode (#1821) — force-finish a stranded multi-pod run. Runs no
    # personas; handled before persona selection so it composes with neither.
    if getattr(args, "finalize", None):
        return _finalize_run(args.finalize, config)

    try:
        persona_ids = _selected_personas(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if persona_ids is not None:
        expected_persona_ids = sorted(persona_ids)
        # #1821 (Option B): persona slicing is the trigger's job. A multi-pod
        # run is fanned out as N separate Jobs, each handed an explicit
        # ``--personas <shard>`` slice, so an explicit selection is
        # AUTHORITATIVE and must NEVER be re-sliced here — re-striping a slice
        # (with JOB_COMPLETION_INDEX unset, i.e. pod_index 0) would silently
        # drop most of this pod's personas. The run's finish-barrier denominator
        # is the full roster, written once by the trigger's create_run (sticky),
        # so this pod declaring only its slice as ``expected`` can't shrink it.
        #
        # Only a whole-roster selection (``--all`` / ``--group``) self-stripes,
        # which preserves a direct ``--all`` CronJob/operator run and is a no-op
        # at the single-pod default (pod_count == 1).
        if args.all or getattr(args, "group", None):
            my_personas = _stripe_personas(
                persona_ids,
                pod_index=config.pod_index,
                pod_count=config.pod_count,
            )
        else:
            my_personas = sorted(persona_ids)
        return await _run_multi(
            my_personas, config, expected_persona_ids=expected_persona_ids
        )
    return await _run_single(config)


def _finalize_run(run_id: str, config: Config) -> int:
    """Reaper: force-finish a multi-pod run whose doc is still ``new`` (#1821).

    The multi-pod finish barrier hands the run-level finalisation to whichever
    pod writes the LAST expected persona's review. If a pod crashed or was
    evicted before writing its slice, that last write never happens, no pod
    ever wins the claim, and the run sits at status ``new`` forever. This
    reaper is the out-of-band recovery: it writes a placeholder review for
    every still-missing ``expected_personas`` entry, then stamps the run
    finished with whatever per-persona accounting is already present.

    Idempotent-ish: a run already past ``new`` (someone finished it) is left
    untouched and reported. Only ``QA_SINK=atlas`` runs have a finish barrier;
    the reaper requires the Atlas sink.
    """
    sink = build_sink(config)
    if not isinstance(sink, AtlasReportSink):
        print(
            "ERROR: --finalize requires QA_SINK=atlas (the finish barrier "
            f"only exists for the Atlas sink); got sink={config.sink!r}",
            file=sys.stderr,
        )
        return 2

    store = sink._ensure_store()
    run = store.runs.find_one({"run_id": run_id})
    if run is None:
        print(f"ERROR: unknown run_id {run_id!r}", file=sys.stderr)
        return 2
    if run.get("status") != "new":
        print(
            f"==> finalize: run {run_id} is already "
            f"{run.get('status')!r} — nothing to do",
            file=sys.stderr,
        )
        return 0

    sink.finalize_stranded(run_id)
    print(f"==> finalize: stamped stranded run {run_id} reviewed", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Console-script entrypoint."""
    argv = sys.argv[1:] if argv is None else argv
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
