"""Tests for the multi-persona orchestrator — Slice 5 (#622).

Focus: every persona of one ``run_personas`` call shares ONE run id, ONE
sink, and ``AtlasReportSink.finish()`` is called exactly once. ``run_persona``
is mocked (no Anthropic calls) and the qa-store is mongomock-backed (no
network). The Discord POST is mocked too.
"""

from __future__ import annotations

import asyncio
import dataclasses

import mongomock
import pytest

from qa_agents.accounting import RunAccounting
from qa_agents.config import Config
from qa_agents.orchestrator import run_personas
from qa_agents.report import RunResult
from qa_agents.tools.findings import Findings


# ---------------------------------------------------------------------------
# Fixtures — mongomock store, mocked run_persona, captured Discord posts.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _mock_mongo(monkeypatch):
    """qa_store.connect() → a mongomock-backed Store."""
    import qa_store
    from qa_store.schema import Store

    def _fake_connect(url=None, db=None):
        client = mongomock.MongoClient()
        s = Store(client=client, db_name=db or "slyreply_qa_test")
        s.runs.create_index("run_id", unique=True)
        s.findings.create_index("finding_id", unique=True)
        return s

    monkeypatch.setattr(qa_store, "connect", _fake_connect)
    # By-design knowledge injection is exercised in test_by_design_injection;
    # here it's irrelevant, so stub it to "" (keeps these tests off a real
    # qa-store and fast — the production loader opens its own MongoClient).
    monkeypatch.setattr(
        "qa_agents.orchestrator.load_by_design_block", lambda config: "",
    )
    return _fake_connect


@pytest.fixture
def discord_posts(monkeypatch):
    """Capture every Discord POST instead of making a network call."""
    posts: list[dict] = []

    def _fake_post(webhook_url, run_id, personas, totals, **kwargs):
        if not (webhook_url or "").strip():
            return False
        posts.append(
            {
                "url": webhook_url,
                "run_id": run_id,
                "personas": personas,
                "totals": totals,
            }
        )
        return True

    monkeypatch.setattr("qa_agents.orchestrator.post_run_alert", _fake_post)
    return posts


def _config(**overrides) -> Config:
    base = Config(
        persona="first-impression-critic",
        web_base_url="http://frontend",
        smtp_host="smtp-inbound",
        smtp_port=1025,
        mailpit_url="http://mailpit:8025",
        explore_model="claude-sonnet-4-6",
        report_model="claude-opus-4-7",
        max_turns=60,
        run_timeout_s=1800,
        out_dir="./qa-runs",
        mongodb_url="mongodb://mongodb/slyreply",
        admin_email="admin@x",
        admin_password="pw",
        sink="atlas",
        run_id="",
        qa_store_url="mongodb://localhost:27017",
        qa_store_db="slyreply_qa_test",
        discord_webhook_url="https://discord.test/wh",
        # Default the orchestrator tests to concurrency=1 so any test that
        # cares about ordering or sequential semantics behaves the same as
        # before #824. The concurrency-specific tests below override this.
        concurrency=1,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _fake_run_persona_factory():
    """Build a stub run_persona that records the configs it was called with.

    The stub returns a deterministic RunResult and crucially echoes the
    config's ``run_id`` into the result so a test can assert sharing.
    """
    seen: list[Config] = []

    async def _fake_run_persona(persona, config, *, by_design_block=""):
        seen.append(config)
        acc = RunAccounting()
        acc.record(
            "explore", "claude-sonnet-4-6",
            {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 200},
            num_turns=10,
        )
        acc.record(
            "report", "claude-opus-4-7",
            {"input_tokens": 2000, "output_tokens": 800},
            num_turns=1,
        )
        findings = Findings()
        findings.add("confusion", "major", "Test finding", "body")
        return RunResult(
            # run_id echoes the config so the test can see what was passed.
            run_id=config.run_id or "unset",
            persona_id=persona.id,
            persona_display_name=persona.display_name,
            started_at="2026-05-19T10:00:00+00:00",
            finished_at="2026-05-19T10:10:00+00:00",
            accounting=acc,
            findings=findings,
            review_markdown="## Would I use this?\n\nYes.",
        )

    return _fake_run_persona, seen


# ---------------------------------------------------------------------------
# Run-id sharing — the core guarantee.
# ---------------------------------------------------------------------------
async def test_all_personas_share_one_run_id(monkeypatch, discord_posts):
    fake_run_persona, seen = _fake_run_persona_factory()
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    run = await run_personas(["first-impression-critic", "email-verifier", "upgrade-buyer"], _config())

    # Every persona got the SAME run id in its config.
    run_ids = {c.run_id for c in seen}
    assert len(run_ids) == 1
    assert run.run_id in run_ids
    # And it is the one the OrchestratedRun reports.
    assert run.persona_ids == ["first-impression-critic", "email-verifier", "upgrade-buyer"]


async def test_explicit_qa_run_id_is_honoured(monkeypatch, discord_posts):
    fake_run_persona, seen = _fake_run_persona_factory()
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    run = await run_personas(["first-impression-critic", "desktop-evaluator"], _config(run_id="qa-fixed-123"))

    assert run.run_id == "qa-fixed-123"
    assert all(c.run_id == "qa-fixed-123" for c in seen)


async def test_one_shared_run_document_in_store(monkeypatch, discord_posts):
    """All personas land in ONE qa_runs document keyed by the shared id."""
    fake_run_persona, _ = _fake_run_persona_factory()
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    import qa_store

    store = qa_store.connect(None, "slyreply_qa_test")
    # Re-point connect at this exact store so we can inspect it afterwards.
    monkeypatch.setattr(qa_store, "connect", lambda url=None, db=None: store)

    run = await run_personas(["first-impression-critic", "email-verifier"], _config())

    docs = list(store.runs.find({}))
    assert len(docs) == 1
    doc = docs[0]
    assert doc["run_id"] == run.run_id
    # Both personas appended their review to the one document.
    assert sorted(doc["personas"]) == ["email-verifier", "first-impression-critic"]
    assert len(doc.get("reviews") or []) == 2


async def test_finish_called_once_stamps_run_totals(monkeypatch, discord_posts):
    """finish() runs exactly once — the run doc ends up status=new + totals."""
    fake_run_persona, _ = _fake_run_persona_factory()
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    import qa_store

    store = qa_store.connect(None, "slyreply_qa_test")
    monkeypatch.setattr(qa_store, "connect", lambda url=None, db=None: store)

    run = await run_personas(["first-impression-critic", "email-verifier"], _config())

    doc = store.runs.find_one({"run_id": run.run_id})
    # finish_run stamps a totals block and a finished timestamp.
    assert doc.get("totals") is not None
    # Two personas × (1000 + 2000) input tokens. #1822 — token counts only;
    # the dollar fields are no longer written for new runs.
    assert doc["totals"]["input_tokens"] == 6000
    assert "cost_usd" not in doc["totals"]
    assert "real_cost_usd" not in doc["totals"]


async def test_orchestrator_totals_sum_every_persona(monkeypatch, discord_posts):
    fake_run_persona, _ = _fake_run_persona_factory()
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    run = await run_personas(["first-impression-critic", "email-verifier", "upgrade-buyer"], _config())

    # 3 personas × (1000+2000) input = 9000; × (500+800) output = 3900.
    assert run.totals["input_tokens"] == 9000
    assert run.totals["output_tokens"] == 3900
    # #1822 — token counts only; no dollar fields in the run totals.
    assert "cost_usd" not in run.totals
    assert "cost_is_estimated" not in run.totals


# ---------------------------------------------------------------------------
# Discord wiring.
# ---------------------------------------------------------------------------
async def test_discord_alert_posted_with_shared_run_id(monkeypatch, discord_posts):
    fake_run_persona, _ = _fake_run_persona_factory()
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    run = await run_personas(["first-impression-critic", "email-verifier"], _config())

    assert run.discord_posted is True
    assert len(discord_posts) == 1
    post = discord_posts[0]
    assert post["run_id"] == run.run_id
    assert [p.persona_id for p in post["personas"]] == ["first-impression-critic", "email-verifier"]


async def test_discord_skipped_when_no_webhook(monkeypatch, discord_posts):
    fake_run_persona, _ = _fake_run_persona_factory()
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    run = await run_personas(["first-impression-critic"], _config(discord_webhook_url=""))

    assert run.discord_posted is False
    assert discord_posts == []


async def test_discord_failure_does_not_fail_the_run(monkeypatch):
    """A Discord outage is logged, not raised — the run still succeeds."""
    fake_run_persona, _ = _fake_run_persona_factory()
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    def _boom(*args, **kwargs):
        raise RuntimeError("discord is down")

    monkeypatch.setattr("qa_agents.orchestrator.post_run_alert", _boom)

    run = await run_personas(["first-impression-critic"], _config())
    # The run completed; only the alert failed.
    assert run.discord_posted is False
    assert run.persona_ids == ["first-impression-critic"]


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------
async def test_unknown_persona_raises_before_any_run(monkeypatch, discord_posts):
    fake_run_persona, seen = _fake_run_persona_factory()
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    with pytest.raises(KeyError):
        await run_personas(["first-impression-critic", "nobody"], _config())
    # Failed before running any persona.
    assert seen == []


# ---------------------------------------------------------------------------
# File sink still works for a local --personas run.
# ---------------------------------------------------------------------------
async def test_file_sink_orchestrated_run(monkeypatch, discord_posts, tmp_path):
    fake_run_persona, seen = _fake_run_persona_factory()
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    cfg = _config(sink="file", out_dir=str(tmp_path), discord_webhook_url="")
    run = await run_personas(["first-impression-critic", "desktop-evaluator"], cfg)

    # File sink has no shared-id machinery; the orchestrator still pins a
    # single run id across the personas.
    assert len({c.run_id for c in seen}) == 1
    assert run.run_id == "qa-local"


# ---------------------------------------------------------------------------
# Per-persona crash resilience (#652) — a single persona failure must NEVER
# lose the whole run. The orchestrator records a placeholder review through
# the same sink, calls finish() exactly once, and still posts the Discord
# alert so a maintainer knows the Job finished.
# ---------------------------------------------------------------------------
def _factory_with_failures(failing_ids: set[str]):
    """Like the clean factory but personas in ``failing_ids`` raise."""
    from qa_agents.accounting import RunAccounting
    from qa_agents.tools.findings import Findings as _Findings

    seen: list[Config] = []
    finish_calls = {"n": 0}

    async def _fake_run_persona(persona, config, *, by_design_block=""):
        seen.append(config)
        if persona.id in failing_ids:
            raise RuntimeError(f"boom: {persona.id} crashed")
        acc = RunAccounting()
        acc.record(
            "explore", "claude-sonnet-4-6",
            {"input_tokens": 1000, "output_tokens": 500},
            num_turns=10,
        )
        f = _Findings()
        f.add("confusion", "minor", "ok", "ok")
        from qa_agents.report import RunResult
        return RunResult(
            run_id=config.run_id or "unset",
            persona_id=persona.id,
            persona_display_name=persona.display_name,
            started_at="2026-05-19T10:00:00+00:00",
            finished_at="2026-05-19T10:10:00+00:00",
            accounting=acc,
            findings=f,
            review_markdown="## Would I use this?\n\nYes.",
        )

    return _fake_run_persona, seen, finish_calls


async def test_first_persona_failure_still_records_all_reviews(monkeypatch, discord_posts):
    """The first persona raises; the rest run; every persona has a review row."""
    fake_run_persona, seen, _ = _factory_with_failures({"first-impression-critic"})
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    import qa_store

    from qa_agents.report import AtlasReportSink

    store = qa_store.connect(None, "slyreply_qa_test")
    monkeypatch.setattr(qa_store, "connect", lambda url=None, db=None: store)

    # Spy on finish() — it must be called exactly once.
    finish_calls = {"n": 0}
    original_finish = AtlasReportSink.finish

    def _counted_finish(self):
        finish_calls["n"] += 1
        return original_finish(self)

    monkeypatch.setattr(AtlasReportSink, "finish", _counted_finish)

    run = await run_personas(["first-impression-critic", "email-verifier", "upgrade-buyer"], _config())

    # All THREE personas were attempted.
    assert [c.persona for c in seen] == ["first-impression-critic", "email-verifier", "upgrade-buyer"]
    # The failed persona is reported.
    assert run.failed_persona_ids == ["first-impression-critic"]
    # The run document has all three reviews — the failed one as a placeholder.
    doc = store.runs.find_one({"run_id": run.run_id})
    assert doc is not None
    reviews_by_persona = {r["persona"]: r for r in doc.get("reviews") or []}
    assert set(reviews_by_persona) == {"first-impression-critic", "email-verifier", "upgrade-buyer"}
    # Placeholder review is honest about the failure.
    margaret_md = reviews_by_persona["first-impression-critic"]["review_markdown"]
    assert "Persona failed" in margaret_md
    assert "RuntimeError" in margaret_md
    assert "boom: first-impression-critic crashed" in margaret_md
    # finish() called exactly once.
    assert finish_calls["n"] == 1
    # Discord posted exactly once.
    assert len(discord_posts) == 1
    assert run.discord_posted is True


async def test_all_personas_failing_still_finishes_run(monkeypatch, discord_posts):
    """When every persona crashes, the run still finishes and alerts."""
    fake_run_persona, seen, _ = _factory_with_failures(
        {"first-impression-critic", "email-verifier", "upgrade-buyer", "desktop-evaluator"}
    )
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    import qa_store

    from qa_agents.report import AtlasReportSink

    store = qa_store.connect(None, "slyreply_qa_test")
    monkeypatch.setattr(qa_store, "connect", lambda url=None, db=None: store)

    finish_calls = {"n": 0}
    original_finish = AtlasReportSink.finish

    def _counted_finish(self):
        finish_calls["n"] += 1
        return original_finish(self)

    monkeypatch.setattr(AtlasReportSink, "finish", _counted_finish)

    run = await run_personas(
        ["first-impression-critic", "email-verifier", "upgrade-buyer", "desktop-evaluator"], _config()
    )

    # Every persona is failed but every persona has a placeholder review.
    assert set(run.failed_persona_ids) == {"first-impression-critic", "email-verifier", "upgrade-buyer", "desktop-evaluator"}
    doc = store.runs.find_one({"run_id": run.run_id})
    reviews_by_persona = {r["persona"]: r for r in doc.get("reviews") or []}
    assert set(reviews_by_persona) == {"first-impression-critic", "email-verifier", "upgrade-buyer", "desktop-evaluator"}
    for md in (r["review_markdown"] for r in reviews_by_persona.values()):
        assert "Persona failed" in md
    # finish() and Discord still fired exactly once.
    assert finish_calls["n"] == 1
    assert len(discord_posts) == 1
    # The Discord alert lines reflect the failures.
    post = discord_posts[0]
    assert {p.persona_id for p in post["personas"]} == {
        "first-impression-critic", "email-verifier", "upgrade-buyer", "desktop-evaluator"
    }


async def test_placeholder_review_has_failed_verdict(monkeypatch, discord_posts):
    """The placeholder's verdict says 'failed' so the Discord alert and the
    runs table both surface the crash."""
    fake_run_persona, _, _ = _factory_with_failures({"email-verifier"})
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    run = await run_personas(["first-impression-critic", "email-verifier"], _config())

    # Find email-verifier's alert line — the verdict surfaces the failure.
    post = discord_posts[0]
    by_id = {p.persona_id: p for p in post["personas"]}
    assert "failed" in by_id["email-verifier"].verdict.lower()
    assert "RuntimeError" in by_id["email-verifier"].verdict or "boom" in by_id["email-verifier"].verdict
    # first-impression-critic was fine.
    assert "yes" in by_id["first-impression-critic"].verdict.lower()
    # The exposed list shows email-verifier as the failure.
    assert run.failed_persona_ids == ["email-verifier"]


# ---------------------------------------------------------------------------
# Bounded concurrency (#824). The orchestrator runs personas in parallel
# under an asyncio.Semaphore so a 12-persona run drops from ~16h to ~4h.
# These tests pin the contract: (a) the cap is respected, (b) concurrency=1
# is a clean sequential fallback, (c) a single persona's crash still does
# not poison its siblings when they are running concurrently.
# ---------------------------------------------------------------------------
def _instrumented_run_persona_factory(
    *,
    failing_ids: set[str] | None = None,
    sleep_s: float = 0.05,
):
    """Build a run_persona stub that tracks concurrent invocations.

    Each invocation increments ``state['active']`` on entry, sleeps a
    little so concurrent calls can actually overlap, then decrements on
    exit. ``state['peak']`` records the high-water mark — that is what
    the concurrency-cap tests assert on.
    """
    failing_ids = failing_ids or set()
    state = {"active": 0, "peak": 0, "calls": []}

    async def _fake_run_persona(persona, config, *, by_design_block=""):
        state["calls"].append(persona.id)
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            # Hold the slot long enough for a sibling task scheduled in the
            # same gather() to enter — otherwise a fast stub finishes before
            # the next coroutine is scheduled and we under-measure peak.
            await asyncio.sleep(sleep_s)
            if persona.id in failing_ids:
                raise RuntimeError(f"boom: {persona.id} crashed")
            acc = RunAccounting()
            acc.record(
                "explore",
                "claude-sonnet-4-6",
                {"input_tokens": 1000, "output_tokens": 500},
                num_turns=10,
            )
            findings = Findings()
            findings.add("confusion", "minor", "ok", "ok")
            return RunResult(
                run_id=config.run_id or "unset",
                persona_id=persona.id,
                persona_display_name=persona.display_name,
                started_at="2026-05-23T10:00:00+00:00",
                finished_at="2026-05-23T10:10:00+00:00",
                accounting=acc,
                findings=findings,
                review_markdown="## Would I use this?\n\nYes.",
            )
        finally:
            state["active"] -= 1

    return _fake_run_persona, state


async def test_concurrency_cap_is_respected(monkeypatch, discord_posts):
    """With concurrency=3 and 8 personas, peak overlap never exceeds 3."""
    fake_run_persona, state = _instrumented_run_persona_factory()
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    # 8 personas is well over the cap of 3 so the semaphore actually clamps.
    cfg = _config(concurrency=3)
    persona_ids = ["first-impression-critic", "desktop-evaluator", "email-verifier", "upgrade-buyer", "privacy-skeptic", "long-input-tester", "mobile-signup-visitor"]
    # Use whatever personas exist in the registry — six is plenty to overrun.
    run = await run_personas(persona_ids[: min(len(persona_ids), 7)], cfg)

    assert state["peak"] <= 3
    # And we genuinely ran several concurrently — otherwise the test would
    # pass even with a broken (still-sequential) implementation.
    assert state["peak"] >= 2
    assert len(run.persona_ids) == len(persona_ids[: min(len(persona_ids), 7)])


async def test_concurrency_one_is_sequential(monkeypatch, discord_posts):
    """concurrency=1 → peak active is 1 (regression: never break sequential)."""
    fake_run_persona, state = _instrumented_run_persona_factory()
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    cfg = _config(concurrency=1)
    run = await run_personas(["first-impression-critic", "desktop-evaluator", "email-verifier"], cfg)

    assert state["peak"] == 1
    assert run.persona_ids == ["first-impression-critic", "desktop-evaluator", "email-verifier"]


async def test_concurrency_default_from_config(monkeypatch, discord_posts):
    """The orchestrator reads concurrency from Config, not a hardcoded const."""
    fake_run_persona, state = _instrumented_run_persona_factory()
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    cfg = _config(concurrency=2)
    await run_personas(["first-impression-critic", "desktop-evaluator", "email-verifier", "upgrade-buyer"], cfg)

    assert state["peak"] <= 2
    assert state["peak"] >= 2  # 4 personas at cap 2 will overlap.


async def test_failed_persona_does_not_sink_siblings_concurrent(
    monkeypatch, discord_posts
):
    """A crash in one persona still leaves siblings with real reviews."""
    fake_run_persona, _state = _instrumented_run_persona_factory(
        failing_ids={"email-verifier"}
    )
    monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

    import qa_store

    store = qa_store.connect(None, "slyreply_qa_test")
    monkeypatch.setattr(qa_store, "connect", lambda url=None, db=None: store)

    cfg = _config(concurrency=3)
    run = await run_personas(["first-impression-critic", "desktop-evaluator", "email-verifier", "upgrade-buyer"], cfg)

    assert run.failed_persona_ids == ["email-verifier"]
    doc = store.runs.find_one({"run_id": run.run_id})
    reviews_by_persona = {r["persona"]: r for r in doc.get("reviews") or []}
    assert set(reviews_by_persona) == {"first-impression-critic", "desktop-evaluator", "email-verifier", "upgrade-buyer"}
    # Siblings carry the real review, email-verifier the placeholder.
    assert "Persona failed" in reviews_by_persona["email-verifier"]["review_markdown"]
    for sibling in ("first-impression-critic", "desktop-evaluator", "upgrade-buyer"):
        assert "Persona failed" not in reviews_by_persona[sibling]["review_markdown"]


async def test_results_preserve_input_persona_order(monkeypatch, discord_posts):
    """Concurrent execution may interleave, but the OrchestratedRun's
    persona_ids list (and the sink's reviews) must remain in the input order
    so a maintainer reading the Discord alert sees a stable order.
    """
    # Make later personas finish faster than earlier ones — if the code is
    # naively appending in completion order this assertion will catch it.
    state = {"counter": 0}

    async def _fake_run_persona(persona, config, *, by_design_block=""):
        # Earlier personas sleep longer; reverse order finishes first.
        delays = {"first-impression-critic": 0.12, "desktop-evaluator": 0.08, "email-verifier": 0.04, "upgrade-buyer": 0.01}
        await asyncio.sleep(delays.get(persona.id, 0.05))
        state["counter"] += 1
        acc = RunAccounting()
        return RunResult(
            run_id=config.run_id or "unset",
            persona_id=persona.id,
            persona_display_name=persona.display_name,
            started_at="2026-05-23T10:00:00+00:00",
            finished_at="2026-05-23T10:10:00+00:00",
            accounting=acc,
            findings=Findings(),
            review_markdown="## Would I use this?\n\nYes.",
        )

    monkeypatch.setattr("qa_agents.orchestrator.run_persona", _fake_run_persona)

    cfg = _config(concurrency=4)
    run = await run_personas(["first-impression-critic", "desktop-evaluator", "email-verifier", "upgrade-buyer"], cfg)

    assert run.persona_ids == ["first-impression-critic", "desktop-evaluator", "email-verifier", "upgrade-buyer"]


# ---------------------------------------------------------------------------
# #1253 — production-target safety check.
# ---------------------------------------------------------------------------
class TestRefuseProdPersistentRuns:
    """The harness refuses to seed persistent persona accounts on
    the production hostname. ``signup_or_login`` (and the other
    credential-saving variants) uses a fixed test password that's
    public in source — landing such an account on prod is a known-
    credential exposure window we don't accept.

    See _PROD_HOSTS / _PERSISTENT_SETUP_ACTIONS in orchestrator.py.
    """

    def _persona(self, persona_id="jordan", *, setup_actions=None):
        from qa_agents.personas import Persona
        return Persona(
            id=persona_id,
            display_name=f"{persona_id} display",
            archetype="test",
            registered_email=f"{persona_id}@testease.example.com",
            explore_system_prompt="x",
            report_system_prompt="x",
            setup_actions=setup_actions,
        )

    def test_sandbox_url_with_persistent_persona_passes(self):
        from qa_agents.orchestrator import _refuse_prod_persistent_runs
        # Sandbox is the normal happy path — no error regardless of
        # setup_actions configuration.
        _refuse_prod_persistent_runs(
            web_base_url="https://sandbox.slyreply.ai",
            personas=[self._persona(setup_actions="signup_or_login")],
        )

    def test_prod_url_without_persistent_persona_passes(self):
        from qa_agents.orchestrator import _refuse_prod_persistent_runs
        # Hitting prod with non-persistent personas (AI invents
        # passwords that decay) is the operator's existing risk
        # surface; the guard doesn't expand it.
        _refuse_prod_persistent_runs(
            web_base_url="https://slyreply.ai",
            personas=[self._persona(setup_actions=None)],
        )

    def test_prod_url_with_persistent_persona_raises(self):
        from qa_agents.orchestrator import _refuse_prod_persistent_runs
        with pytest.raises(RuntimeError) as exc_info:
            _refuse_prod_persistent_runs(
                web_base_url="https://slyreply.ai",
                personas=[
                    self._persona("jordan", setup_actions="signup_or_login"),
                ],
            )
        # The error message MUST tell the operator both what triggered
        # it AND how to override — frustrating to be blocked without
        # being told the escape hatch.
        msg = str(exc_info.value)
        assert "production" in msg
        assert "jordan" in msg
        assert "QA_ALLOW_PROD_PERSISTENCE" in msg

    def test_prod_url_with_persistent_persona_allowed_with_override(
        self, monkeypatch,
    ):
        from qa_agents.orchestrator import _refuse_prod_persistent_runs
        # Operator explicitly opted in. The guard logs but allows.
        monkeypatch.setenv("QA_ALLOW_PROD_PERSISTENCE", "1")
        _refuse_prod_persistent_runs(
            web_base_url="https://slyreply.ai",
            personas=[self._persona(setup_actions="signup_or_login")],
        )

    def test_override_value_other_than_1_does_not_unlock(self, monkeypatch):
        # "true" / "yes" / random truthy strings DON'T unlock — we want
        # the operator to make a deliberate, exact choice. Anything but
        # "1" is rejected so a stray "QA_ALLOW_PROD_PERSISTENCE=" in
        # a parent shell doesn't accidentally disarm the guard.
        from qa_agents.orchestrator import _refuse_prod_persistent_runs
        for value in ("true", "yes", "on", "0", ""):
            monkeypatch.setenv("QA_ALLOW_PROD_PERSISTENCE", value)
            with pytest.raises(RuntimeError):
                _refuse_prod_persistent_runs(
                    web_base_url="https://slyreply.ai",
                    personas=[self._persona(setup_actions="signup_or_login")],
                )

    def test_each_prod_host_variant_is_blocked(self):
        # Bare apex, www, and app subdomains all count as production.
        # Sandbox + testease are intentionally separate sub-domains.
        from qa_agents.orchestrator import _refuse_prod_persistent_runs
        for host in ("slyreply.ai", "www.slyreply.ai", "app.slyreply.ai"):
            with pytest.raises(RuntimeError):
                _refuse_prod_persistent_runs(
                    web_base_url=f"https://{host}",
                    personas=[self._persona(setup_actions="signup_or_login")],
                )

    def test_signup_then_pro_also_blocked(self):
        # All credential-persisting variants are covered, not just
        # signup_or_login.
        from qa_agents.orchestrator import _refuse_prod_persistent_runs
        for action in ("signup", "signup_or_login", "signup_then_pro",
                       "signup_then_power", "clear_credentials_then_signup"):
            with pytest.raises(RuntimeError):
                _refuse_prod_persistent_runs(
                    web_base_url="https://slyreply.ai",
                    personas=[self._persona(setup_actions=action)],
                )

    def test_signup_fresh_is_allowed_on_prod(self):
        # signup_fresh deliberately doesn't save credentials — it's the
        # "test the signup flow itself" mode. Operator may legitimately
        # want to drive a smoke signup on prod (and clean up after).
        from qa_agents.orchestrator import _refuse_prod_persistent_runs
        _refuse_prod_persistent_runs(
            web_base_url="https://slyreply.ai",
            personas=[self._persona(setup_actions="signup_fresh")],
        )

    def test_unparseable_url_does_not_crash(self):
        # Defensive: a malformed config URL must not turn into an
        # AttributeError during the safety check. No host → no match,
        # so the guard silently passes (the bad URL fails later
        # anyway during the first navigate call).
        from qa_agents.orchestrator import _refuse_prod_persistent_runs
        for bad in ("", "not-a-url", "://broken", "file:///etc/passwd"):
            _refuse_prod_persistent_runs(
                web_base_url=bad,
                personas=[self._persona(setup_actions="signup_or_login")],
            )


# ---------------------------------------------------------------------------
# Multi-pod sharding (#1821). pod_count > 1 means N pods each run a stripe
# of the roster under one shared run_id; expected_persona_ids seeds the
# finish-barrier denominator; only the LAST pod finalises + posts Discord.
# ---------------------------------------------------------------------------
class TestMultiPodOrchestration:
    @pytest.fixture
    def shared_store(self, monkeypatch):
        """One mongomock store every pod's run_personas call shares."""
        import qa_store
        from qa_store.schema import Store

        client = mongomock.MongoClient()
        s = Store(client=client, db_name="slyreply_qa_test")
        s.runs.create_index("run_id", unique=True)
        s.findings.create_index("finding_id", unique=True)
        monkeypatch.setattr(
            qa_store, "connect", lambda url=None, db=None: s
        )
        return s

    async def test_first_pod_does_not_post_discord(
        self, monkeypatch, discord_posts, shared_store
    ):
        """Pod 0 of 2 runs its stripe but is NOT last — it stamps nothing
        and posts NO Discord alert."""
        fake_run_persona, _ = _fake_run_persona_factory()
        monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

        run = await run_personas(
            ["first-impression-critic"],
            _config(run_id="qa-2pod", pod_index=0, pod_count=2),
            expected_persona_ids=["desktop-evaluator", "first-impression-critic"],
        )

        assert run.discord_posted is False
        assert discord_posts == []
        doc = shared_store.runs.find_one({"run_id": "qa-2pod"})
        assert doc["status"] == "new"
        # The denominator is the FULL roster, not just this pod's stripe.
        assert sorted(doc["expected_personas"]) == [
            "desktop-evaluator", "first-impression-critic",
        ]

    async def test_last_pod_finishes_and_posts_exactly_once(
        self, monkeypatch, discord_posts, shared_store
    ):
        """Across a simulated 2-pod sequence finish fires exactly once and
        only the last pod posts Discord."""
        fake_run_persona, _ = _fake_run_persona_factory()
        monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

        expected = ["desktop-evaluator", "first-impression-critic"]

        # Pod 0 runs first-impression-critic.
        run_a = await run_personas(
            ["first-impression-critic"],
            _config(run_id="qa-2pod", pod_index=0, pod_count=2),
            expected_persona_ids=expected,
        )
        # Pod 1 runs desktop-evaluator — this write completes the roster.
        run_b = await run_personas(
            ["desktop-evaluator"],
            _config(run_id="qa-2pod", pod_index=1, pod_count=2),
            expected_persona_ids=expected,
        )

        # Exactly one pod posted Discord — the last.
        assert run_a.discord_posted is False
        assert run_b.discord_posted is True
        assert len(discord_posts) == 1
        assert discord_posts[0]["run_id"] == "qa-2pod"

        doc = shared_store.runs.find_one({"run_id": "qa-2pod"})
        assert doc["status"] == "reviewed"
        assert doc["finished_at"] is not None
        # Totals summed across BOTH pods' personas (each 3000 in / 1300 out).
        assert doc["totals"]["input_tokens"] == 6000
        assert doc["totals"]["output_tokens"] == 2600

    async def test_single_pod_path_unchanged(
        self, monkeypatch, discord_posts, shared_store
    ):
        """pod_count=1 (default) keeps the unconditional finish + alert."""
        fake_run_persona, _ = _fake_run_persona_factory()
        monkeypatch.setattr("qa_agents.orchestrator.run_persona", fake_run_persona)

        run = await run_personas(
            ["first-impression-critic", "desktop-evaluator"],
            _config(run_id="qa-1pod", pod_index=0, pod_count=1),
            expected_persona_ids=["desktop-evaluator", "first-impression-critic"],
        )

        assert run.discord_posted is True
        assert len(discord_posts) == 1
        doc = shared_store.runs.find_one({"run_id": "qa-1pod"})
        assert doc["status"] == "reviewed"
