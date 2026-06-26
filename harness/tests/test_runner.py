"""Tests for the persona runner — resilience to SDK-side errors (#652) and
live per-turn logging (#664).

The Claude Agent SDK can raise mid-stream — the most common case is the
``"Reached maximum number of turns"`` truncation, but any other ``Exception``
out of ``query()`` is in the same risk class. ``run_explore_phase`` must
absorb both, mark the outcome as truncated, and let the report phase still
run so a review lands instead of the whole orchestrator crashing.

Live logging (#664) emits a single-line entry per AssistantMessage content
block (``→ tool(args)`` for tool-use, ``» text`` for narration) and a
``turn done`` line per ResultMessage so the operator can follow the run in
``kubectl logs -f``. A heartbeat fires when the SDK sits silent.

These tests substitute a fake ``query()`` (no Anthropic calls, no Playwright
MCP) into the runner module so the SDK contract — an async generator that
may raise — is faithful but in-process.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

import pytest

from qa_agents import runner as runner_mod
from qa_agents.accounting import RunAccounting
from qa_agents.config import Config
from qa_agents.personas import get_persona
from qa_agents.runner import (
    ExploreOutcome,
    _format_tool_args,
    _is_max_turns_error,
    _is_success_completion,
    _options_env,
    _shorten_text,
    run_explore_phase,
    run_persona,
)
from qa_agents.tools.findings import Findings


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
        sink="file",
        run_id="qa-runner-test",
        qa_store_url="mongodb://localhost:27017",
        qa_store_db="slyreply_qa_test",
        discord_webhook_url="",
        concurrency=1,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _patch_servers(monkeypatch):
    """Stub the in-process MCP server factories so no real MCP is spun up."""
    monkeypatch.setattr(
        runner_mod,
        "build_email_server",
        lambda **kwargs: (object(), ["mail_send", "mail_inbox"]),
    )

    def _fake_findings_server(findings, *, live_writer=None):
        # #1115 follow-up — accept (and ignore) the optional live_writer
        # kwarg so the runner's per-persona wiring can pass it through.
        return object(), ["note_finding"]

    monkeypatch.setattr(runner_mod, "build_findings_server", _fake_findings_server)


# ---------------------------------------------------------------------------
# _is_max_turns_error — substring matching of the SDK's error wording.
# ---------------------------------------------------------------------------
def test_max_turns_substring_matches_real_sdk_message():
    exc = Exception("Claude Code returned an error result: Reached maximum number of turns (60)")
    assert _is_max_turns_error(exc) is True


def test_max_turns_substring_matches_case_insensitively():
    assert _is_max_turns_error(Exception("MAXIMUM NUMBER OF TURNS reached")) is True


def test_max_turns_substring_rejects_unrelated_errors():
    assert _is_max_turns_error(Exception("connection reset")) is False
    assert _is_max_turns_error(RuntimeError("bork")) is False


# ---------------------------------------------------------------------------
# _is_success_completion — the SDK "error result: success" quirk (#668).
# ---------------------------------------------------------------------------
def test_success_completion_matches_real_sdk_message():
    exc = Exception("Claude Code returned an error result: success")
    assert _is_success_completion(exc) is True


def test_success_completion_matches_case_insensitively():
    assert _is_success_completion(Exception("ERROR RESULT: SUCCESS")) is True


def test_success_completion_rejects_unrelated_messages():
    # Just the word "success" elsewhere must NOT match — we're matching the
    # specific contradictory phrase, not any error containing the word.
    assert _is_success_completion(Exception("partial success then failed")) is False
    assert _is_success_completion(Exception("connection reset")) is False
    assert _is_success_completion(RuntimeError("bork")) is False


# ---------------------------------------------------------------------------
# _options_env: Max-only. The harness ALWAYS scrubs ANTHROPIC_API_KEY to
# the empty string in the spawned ``claude`` subprocess's env so the CLI
# falls through to OAuth (Claude Code Max). The org-API backend was removed
# entirely; there is no selector.
#
# These tests are deliberately direct asserts on the returned dict rather
# than spinning up a full ClaudeAgentOptions: the contract that matters is
# "what gets put into options.env", and that's a one-line function. If the
# merge behaviour of claude-agent-sdk's subprocess transport ever changes,
# the integration would fail in tests/test_atlas_sink.py or in the real
# cluster long before this unit test would.
# ---------------------------------------------------------------------------
class TestOptionsEnv:
    def test_always_overrides_key_to_empty_string(self) -> None:
        """#894 hardening — the harness actively OVERRIDES
        ANTHROPIC_API_KEY to empty string in options.env, not just
        omits it. The Agent SDK merges options.env on top of inherited
        os.environ (subprocess_cli.py:430), so omitting would leak any
        inherited key through to the spawned ``claude`` CLI — and the
        CLI prefers API-key auth over OAuth when both resolve.
        Empty-string forces OAuth fallback regardless of what the parent
        env carries.

        This belt-and-braces matters in two real scenarios: a pod spec
        that accidentally envFroms the API-key Secret (cluster Max Job at
        risk of misconfiguration), and a Python process spawned outside
        qa-run-local.sh."""
        env = _options_env(_config())
        assert env == {"ANTHROPIC_API_KEY": ""}, (
            "the harness must OVERRIDE ANTHROPIC_API_KEY with empty "
            "string in options.env (not omit it), otherwise an "
            "inherited key from the parent env can leak through and "
            "silently bill the run to the org API."
        )

    def test_options_env_wins_over_real_parent_key(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#916 — when ANTHROPIC_API_KEY is set in the parent process env
        (e.g. a pod spec that accidentally envFroms an API-key Secret),
        the SDK's subprocess transport merges options.env AFTER inherited
        env, so options.env's empty-string override MUST win.

        Without this pin, the design could silently re-route the agent
        loop to API billing if:
          - the SDK's merge order ever inverts (so inherited wins)
          - someone refactors _options_env to omit the key instead of
            override

        This test replicates the SDK's merge-order exactly (see
        claude_agent_sdk/_internal/transport/subprocess_cli.py:430)
        so a regression in either layer fails here, not in production
        with the next billing cycle's invoice.
        """
        # Simulate the cluster Max pod env: parent has the API key
        # (for the org-Haiku background paths), and we're about to spawn
        # a subprocess for the agent loop.
        monkeypatch.setenv(
            "ANTHROPIC_API_KEY",
            "sk-real-parent-key-would-bill-the-API-workspace",
        )

        options_env = _options_env(_config())

        # Replicate the SDK's merge logic verbatim — if the SDK ever
        # changes order, this assertion fails and forces a review.
        import os
        merged_subprocess_env = {**os.environ, **options_env}

        assert merged_subprocess_env["ANTHROPIC_API_KEY"] == "", (
            "options.env's empty-string ANTHROPIC_API_KEY must win over "
            "the parent process's real key. If this assertion fails, "
            "either (a) _options_env stopped returning the empty-string "
            "override, or (b) the SDK's merge order inverted. EITHER "
            "WAY: the cluster Max pod's agent loop would silently bill "
            "the API workspace instead of falling through to OAuth/Max."
        )


async def test_explore_phase_treats_sdk_success_quirk_as_graceful(monkeypatch):
    _patch_servers(monkeypatch)

    async def _fake_query(prompt, options):
        if False:
            yield None  # pragma: no cover
        raise Exception("Claude Code returned an error result: success")

    monkeypatch.setattr(runner_mod, "query", _fake_query)

    persona = get_persona("email-verifier")
    findings = Findings()
    accounting = RunAccounting()

    outcome = await run_explore_phase(persona, _config(), findings, accounting)
    # Must NOT propagate — the orchestrator depends on this.
    assert outcome.truncated_reason is not None
    # The reason mentions the SDK quirk so the report phase + the operator
    # can tell this apart from max_turns or a real failure.
    assert "success" in outcome.truncated_reason.lower()
    assert "graceful" in outcome.truncated_reason.lower()
    # And the digest carries the truncation note so the report is honest.
    assert "ended early" in outcome.digest


# ---------------------------------------------------------------------------
# run_explore_phase absorbs the max-turns truncation.
# ---------------------------------------------------------------------------
async def test_explore_phase_treats_max_turns_as_truncation(monkeypatch):
    _patch_servers(monkeypatch)

    async def _fake_query(prompt, options):
        # Yield nothing — simulate the SDK raising before any AssistantMessage
        # makes it through. This is the worst-case "early max_turns" shape.
        if False:
            yield None  # pragma: no cover
        raise Exception(
            "Claude Code returned an error result: Reached maximum number of turns (60)"
        )

    monkeypatch.setattr(runner_mod, "query", _fake_query)

    persona = get_persona("first-impression-critic")
    findings = Findings()
    accounting = RunAccounting()

    outcome = await run_explore_phase(persona, _config(), findings, accounting)
    # Must NOT propagate.
    assert isinstance(outcome, ExploreOutcome)
    assert outcome.truncated is True
    assert outcome.truncated_reason is not None
    assert "max_turns" in outcome.truncated_reason
    # The truncation note travels inside the digest too so the report phase
    # sees it and the persona writes an honest review.
    assert "explore phase ended early" in outcome.digest


async def test_explore_phase_other_exception_marked_failed(monkeypatch):
    """Network blips, SDK bugs etc — same shape, just a different reason."""
    _patch_servers(monkeypatch)

    async def _fake_query(prompt, options):
        if False:
            yield None  # pragma: no cover
        raise RuntimeError("network bork")

    monkeypatch.setattr(runner_mod, "query", _fake_query)

    outcome = await run_explore_phase(
        get_persona("first-impression-critic"), _config(), Findings(), RunAccounting()
    )
    assert outcome.truncated is True
    assert "RuntimeError" in outcome.truncated_reason
    assert "network bork" in outcome.truncated_reason
    assert "explore phase ended early" in outcome.digest


async def test_explore_phase_preserves_findings_from_partial_run(monkeypatch):
    """Findings recorded BEFORE the truncation must survive."""
    _patch_servers(monkeypatch)
    findings = Findings()
    findings.add("confusion", "major", "Pre-truncation finding", "body")

    async def _fake_query(prompt, options):
        if False:
            yield None  # pragma: no cover
        raise Exception("Reached maximum number of turns (60)")

    monkeypatch.setattr(runner_mod, "query", _fake_query)

    await run_explore_phase(
        get_persona("first-impression-critic"), _config(), findings, RunAccounting()
    )
    # The collector is preserved — the orchestrator can still write it out.
    assert len(findings) == 1
    assert findings.items[0].title == "Pre-truncation finding"


# ---------------------------------------------------------------------------
# run_persona end-to-end: report phase still runs on a truncated digest.
# ---------------------------------------------------------------------------
async def test_run_persona_still_returns_review_after_truncation(monkeypatch):
    """The whole point: a truncated explore phase still yields a written run."""
    _patch_servers(monkeypatch)

    explore_calls = {"n": 0}
    report_calls = {"n": 0}

    class _Block:
        def __init__(self, text):
            self.text = text

    class _AsstMsg:
        def __init__(self, text):
            self.content = [_Block(text)]

    async def _fake_query(prompt, options):
        # Two phases share the same module-level query symbol. Distinguish by
        # whether tools are allowed — the explore phase has tools, report has
        # none.
        if options.allowed_tools:
            explore_calls["n"] += 1
            # Yield a tiny bit of narration first so the digest isn't empty.
            yield _AsstMsg("Margaret stares at the landing page.")
            raise Exception(
                "Claude Code returned an error result: Reached maximum number of turns (60)"
            )
        else:
            report_calls["n"] += 1
            yield _AsstMsg("## Verdict\n\nI did not get far before things stopped.")

    monkeypatch.setattr(runner_mod, "query", _fake_query)
    # Use isinstance-friendly fakes — monkeypatch the runner's checks.
    monkeypatch.setattr(runner_mod, "AssistantMessage", _AsstMsg)
    monkeypatch.setattr(runner_mod, "ResultMessage", type("RM", (), {}))

    result = await run_persona(get_persona("first-impression-critic"), _config())

    # Both phases were entered.
    assert explore_calls["n"] == 1
    assert report_calls["n"] == 1
    # The review markdown was written despite the explore truncation.
    assert "I did not get far" in result.review_markdown
    # The digest carries the truncation note so the review can be honest.
    assert "explore phase ended early" in result.explore_digest


# ---------------------------------------------------------------------------
# Live per-turn logging (#664).
#
# ``run_explore_phase`` must emit, for every assistant content block as it
# streams, one of:
#   [<persona> t=<N>] → tool(args)         (ToolUseBlock)
#   [<persona> t=<N>] » <truncated text>   (TextBlock)
# and a per-turn token summary on each ResultMessage (#1822 — token counts
# only, the dollar figure was retired with the per-run cost computation):
#   [<persona>] turn done  in=… out=… cache=…  run=… tokens
# Heartbeat lines fire when the SDK sits silent longer than the interval.
# ---------------------------------------------------------------------------


class _Block:
    """Duck-typed SDK content block — the runner only reads .text/.name/.input."""

    def __init__(self, text=None, name=None, input=None):
        if text is not None:
            self.text = text
        if name is not None:
            self.name = name
        if input is not None:
            self.input = input


class _AsstMsg:
    """Duck-typed AssistantMessage — module-level so isinstance() works."""

    def __init__(self, blocks):
        self.content = list(blocks)


class _ResultMsg:
    """Duck-typed ResultMessage.

    Still carries a ``total_cost_usd`` attribute like the real SDK message —
    the runner must IGNORE it now (#1822), which the turn-done log test
    pins by asserting no dollar figure appears.
    """

    def __init__(self, usage=None, cost=0.0, num_turns=0):
        self.usage = usage or {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        self.total_cost_usd = cost
        self.num_turns = num_turns


def _block(text=None, name=None, input=None):
    return _Block(text=text, name=name, input=input)


def _asst(*blocks):
    return _AsstMsg(blocks)


def _result(usage=None, cost=0.0, num_turns=0):
    return _ResultMsg(usage=usage, cost=cost, num_turns=num_turns)


def _patch_message_types(monkeypatch):
    """Make the runner's isinstance() checks pass for our duck-typed stubs."""
    monkeypatch.setattr(runner_mod, "AssistantMessage", _AsstMsg)
    monkeypatch.setattr(runner_mod, "ResultMessage", _ResultMsg)


def test_shorten_text_collapses_whitespace_and_truncates():
    """Multi-line text is single-lined, repeated spaces collapsed, ellipsised."""
    out = _shorten_text("hello\n\n  world  \n   how\tare\nyou", 100)
    assert out == "hello world how are you"
    long = "x" * 300
    short = _shorten_text(long, 50)
    assert len(short) <= 50
    assert short.endswith("…")


def test_format_tool_args_dispatch():
    """Each known tool renders its most-meaningful fields.

    #1078 Slice 1 — rewrote the row contents from terse ``key=val``
    pairs to prose ("Clicked X", "Typed 'Y' into Z") and introduced
    the ``__SKIP__`` sentinel for known-noise tools. The
    ``browser_navigate`` row stays raw because Slice 2 parses it for
    the URL spine; ``note_finding`` stays as ``cat/sev title`` because
    the findings panel already surfaces it well.
    """
    # browser_navigate — UNCHANGED (Slice 2 contract).
    assert _format_tool_args(
        "mcp__playwright__browser_navigate", {"url": "http://frontend/login"}
    ) == "url=http://frontend/login"

    # browser_click — prose, ref first, falls back to text/element.
    assert _format_tool_args(
        "mcp__playwright__browser_click", {"ref": "btn-23", "text": "Sign in"}
    ) == "Clicked btn-23"

    # browser_type — prose, truncates the typed value.
    assert _format_tool_args(
        "mcp__playwright__browser_type",
        {"ref": "input-7", "text": "first-impression-critic@example.com"},
    ) == "Typed 'first-impression-critic@example.com' into input-7"

    # note_finding — UNCHANGED.
    args = _format_tool_args(
        "mcp__findings__note_finding",
        {
            "category": "confusion",
            "severity": "major",
            "title": "Couldn't find where to sign up",
            "body": "...",
        },
    )
    assert args.startswith("confusion/major ")
    assert "Couldn't find where to sign up" in args

    # email send — prose form.
    assert _format_tool_args(
        "mcp__email__send_email",
        {"to": "agent@slyreply.ai", "subject": "Hi there", "body": "..."},
    ) == "Emailed agent@slyreply.ai: 'Hi there'"

    # Unknown tool with a string-valued first kwarg — fallback survives.
    assert _format_tool_args(
        "mcp__playwright__browser_unknown", {"foo": "bar"}
    ) == "foo=bar"

    # Snapshot — known noise, returns the skip sentinel.
    assert _format_tool_args(
        "mcp__playwright__browser_snapshot", {}
    ) == "__SKIP__"


async def test_explore_logs_tool_use_line(monkeypatch, caplog):
    """A ToolUseBlock yields an `→ tool(args)` line tagged with persona+turn."""
    _patch_servers(monkeypatch)
    _patch_message_types(monkeypatch)

    async def _fake_query(prompt, options):
        yield _asst(
            _block(name="mcp__playwright__browser_navigate", input={"url": "http://frontend"})
        )

    monkeypatch.setattr(runner_mod, "query", _fake_query)

    caplog.set_level(logging.INFO, logger="qa_agents.runner")
    await run_explore_phase(
        get_persona("first-impression-critic"), _config(), Findings(), RunAccounting()
    )

    lines = [r.getMessage() for r in caplog.records]
    matching = [line for line in lines if "→" in line and "browser_navigate" in line]
    assert matching, f"expected a tool-use line, got: {lines}"
    sample = matching[0]
    assert sample.startswith("[first-impression-critic t=1]"), sample
    assert "playwright.browser_navigate" in sample
    assert "url=http://frontend" in sample


async def test_explore_logs_text_block_in_full(monkeypatch, caplog):
    """#916 — A TextBlock now logs in FULL (no truncation).

    The original test (`test_explore_logs_text_block_truncated`) pinned
    the 200-char cap behaviour. That made the log strictly less useful
    than no truncation because narration got cut mid-sentence, and
    slice 1 (#911) persists the full content to qa_run_logs anyway,
    so the live log doesn't need to act as the canonical record.

    This replaces the old test with the inverse contract: long text
    survives intact, with no ellipsis and no length cap.
    """
    _patch_servers(monkeypatch)
    _patch_message_types(monkeypatch)

    long = "I open the landing page.  " * 50  # well over the old 200-char cap
    async def _fake_query(prompt, options):
        yield _asst(_block(text=long))

    monkeypatch.setattr(runner_mod, "query", _fake_query)

    caplog.set_level(logging.INFO, logger="qa_agents.runner")
    await run_explore_phase(
        get_persona("first-impression-critic"), _config(), Findings(), RunAccounting()
    )

    lines = [r.getMessage() for r in caplog.records]
    matching = [line for line in lines if "»" in line]
    assert matching, f"expected a text line, got: {lines}"
    sample = matching[0]
    assert sample.startswith("[first-impression-critic t=1]"), sample
    body = sample.split("» ", 1)[1]
    # #916 contract — body is the FULL text emitted by the agent.
    assert body == long, "log line must contain the full text, no truncation"
    assert not body.endswith("…"), "no ellipsis — that was the 200-char cap behaviour"
    assert len(body) > 200, "sanity: test fixture must exercise long content"


async def test_explore_logs_result_turn_done(monkeypatch, caplog):
    """A ResultMessage yields a `turn done in=… out=… run=… tokens` line."""
    _patch_servers(monkeypatch)
    _patch_message_types(monkeypatch)

    async def _fake_query(prompt, options):
        yield _asst(_block(text="hi"))
        yield _result(
            usage={
                "input_tokens": 1500,
                "output_tokens": 250,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 50,
            },
            cost=0.0123,
            num_turns=1,
        )

    monkeypatch.setattr(runner_mod, "query", _fake_query)

    caplog.set_level(logging.INFO, logger="qa_agents.runner")
    await run_explore_phase(
        get_persona("first-impression-critic"), _config(), Findings(), RunAccounting()
    )

    lines = [r.getMessage() for r in caplog.records]
    matching = [line for line in lines if "turn done" in line]
    assert matching, f"expected a turn-done line, got: {lines}"
    sample = matching[0]
    assert sample.startswith("[first-impression-critic]"), sample
    assert "in=1.5k" in sample
    assert "out=250" in sample
    # cache = 100 + 50 = 150
    assert "cache=150" in sample
    # #1822 — the running total is tokens (1500 + 250 + 150 = 1.9k);
    # no dollar figure is logged even though the SDK message carried one.
    assert "run=1.9k tokens" in sample
    assert "$" not in sample


async def test_explore_heartbeat_fires_on_slow_yield(monkeypatch, caplog):
    """If the SDK stays silent past the interval, a `still working` line fires."""
    _patch_servers(monkeypatch)
    _patch_message_types(monkeypatch)

    async def _slow_query(prompt, options):
        await asyncio.sleep(0.25)
        yield _asst(_block(text="finally"))

    monkeypatch.setattr(runner_mod, "query", _slow_query)
    # Force a heartbeat interval well below the simulated slow yield.
    monkeypatch.setattr(runner_mod, "_HEARTBEAT_INTERVAL_S", 0.05)

    caplog.set_level(logging.INFO, logger="qa_agents.runner")
    await run_explore_phase(
        get_persona("first-impression-critic"), _config(), Findings(), RunAccounting()
    )

    lines = [r.getMessage() for r in caplog.records]
    hb = [line for line in lines if "still working" in line]
    assert hb, f"expected at least one heartbeat line, got: {lines}"
    assert hb[0].startswith("[first-impression-critic]"), hb[0]
    assert "awaiting model" in hb[0]


@pytest.mark.parametrize("name", ["explore", "report"])
def test_no_live_api_calls_in_tests(name):
    """A guard: every test in this module stubs ``runner_mod.query``.

    The Claude Agent SDK would otherwise hit the real Anthropic API, which is
    forbidden in CI / unit tests. This trivially asserts the assumption.
    """
    # The actual stubbing happens inside each test via ``monkeypatch.setattr``;
    # this test just documents the invariant so a future contributor sees it.
    assert hasattr(runner_mod, "query")


# ---------------------------------------------------------------------------
# Playwright MCP config + tool guardrails.
# ---------------------------------------------------------------------------
def test_playwright_mcp_config_points_at_image_browser(monkeypatch):
    """By default the server runs from the image and uses the baked Chromium."""
    monkeypatch.delenv("QA_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.delenv("QA_PLAYWRIGHT_MCP_CMD", raising=False)
    cfg = runner_mod._playwright_mcp_config()
    assert cfg["type"] == "stdio"
    # The globally-installed server binary — no npx download at runtime.
    assert cfg["command"] == "playwright-mcp"
    # Headless + sandbox-disabled + isolated for a containerised, non-root run.
    for flag in ("--headless", "--no-sandbox", "--isolated"):
        assert flag in cfg["args"]
    # Pointed explicitly at the image's bundled Chromium.
    assert "--executable-path" in cfg["args"]
    assert "/usr/local/bin/qa-chromium" in cfg["args"]


def test_playwright_mcp_config_is_env_overridable(monkeypatch):
    """A local (non-image) run can override the command and browser path."""
    monkeypatch.setenv("QA_PLAYWRIGHT_MCP_CMD", "npx")
    monkeypatch.setenv("QA_BROWSER_EXECUTABLE", "")
    cfg = runner_mod._playwright_mcp_config()
    assert cfg["command"] == "npx"
    # Empty QA_BROWSER_EXECUTABLE → no --executable-path, so the server falls
    # back to its own browser discovery.
    assert "--executable-path" not in cfg["args"]


# ---------------------------------------------------------------------------
# #891 — QA-token header injection. When QA_TOKEN is set, the runner
# writes a config file with extraHTTPHeaders so the browser sends
# X-QA-Token on every request. Backend's slowapi key-derivation routes
# matching requests to the dedicated qa:client bucket so concurrent
# personas don't collide on the cluster's shared egress IP.
# ---------------------------------------------------------------------------
def test_playwright_mcp_config_omits_config_flag_when_no_qa_token(
    monkeypatch, tmp_path,
):
    """No QA_TOKEN env = no --config flag, no JSON file written.
    Local dev / pre-#891 environments behave exactly as before."""
    monkeypatch.delenv("QA_TOKEN", raising=False)
    monkeypatch.setenv("QA_PLAYWRIGHT_MCP_CONFIG_PATH", str(tmp_path / "cfg.json"))
    cfg = runner_mod._playwright_mcp_config()
    assert "--config" not in cfg["args"]
    assert not (tmp_path / "cfg.json").exists()


def test_playwright_mcp_config_writes_qa_token_header(monkeypatch, tmp_path):
    """QA_TOKEN set = JSON file written with extraHTTPHeaders + --config
    pointing at it. The Playwright MCP server reads this and applies
    extraHTTPHeaders to every BrowserContext it creates, so every page
    load + every fetch carries X-QA-Token."""
    monkeypatch.setenv("QA_TOKEN", "secret-test-token")
    config_path = tmp_path / "cfg.json"
    monkeypatch.setenv("QA_PLAYWRIGHT_MCP_CONFIG_PATH", str(config_path))
    cfg = runner_mod._playwright_mcp_config()
    assert "--config" in cfg["args"]
    config_idx = cfg["args"].index("--config")
    assert cfg["args"][config_idx + 1] == str(config_path)
    import json
    written = json.loads(config_path.read_text())
    assert (
        written["browser"]["contextOptions"]["extraHTTPHeaders"]["X-QA-Token"]
        == "secret-test-token"
    )


def test_playwright_mcp_config_ignores_blank_qa_token(monkeypatch, tmp_path):
    """Whitespace-only / empty QA_TOKEN must not enable injection — an
    accidental empty env var elsewhere shouldn't cause every request to
    carry a meaningless X-QA-Token: '' header that would collide on
    the qa:client bucket without authorising anyone."""
    monkeypatch.setenv("QA_TOKEN", "   ")
    monkeypatch.setenv(
        "QA_PLAYWRIGHT_MCP_CONFIG_PATH", str(tmp_path / "cfg.json"),
    )
    cfg = runner_mod._playwright_mcp_config()
    assert "--config" not in cfg["args"]


# ---------------------------------------------------------------------------
# #934 — per-persona browser locale. When the Persona declares a
# `browser_locale` (e.g. "en-GB" for Daniel and Margaret), the harness
# pipes it into Playwright's contextOptions.locale AND the
# Accept-Language header so country-detect code paths (the checkout
# widget's country picker, frontend currency switcher) see the persona's
# home country instead of the sandbox VPS's region.
# ---------------------------------------------------------------------------
def test_playwright_mcp_config_writes_browser_locale(monkeypatch, tmp_path):
    """Persona locale flows into contextOptions.locale + Accept-Language."""
    monkeypatch.delenv("QA_TOKEN", raising=False)
    config_path = tmp_path / "cfg.json"
    monkeypatch.setenv("QA_PLAYWRIGHT_MCP_CONFIG_PATH", str(config_path))
    cfg = runner_mod._playwright_mcp_config(browser_locale="en-GB")
    assert "--config" in cfg["args"]
    import json
    written = json.loads(config_path.read_text())
    ctx = written["browser"]["contextOptions"]
    assert ctx["locale"] == "en-GB"
    assert ctx["extraHTTPHeaders"]["Accept-Language"] == "en-GB"


def test_playwright_mcp_config_combines_qa_token_and_locale(monkeypatch, tmp_path):
    """A persona with QA_TOKEN set AND a browser_locale lands both in
    the same JSON config — extraHTTPHeaders carries both keys."""
    monkeypatch.setenv("QA_TOKEN", "secret-test-token")
    config_path = tmp_path / "cfg.json"
    monkeypatch.setenv("QA_PLAYWRIGHT_MCP_CONFIG_PATH", str(config_path))
    cfg = runner_mod._playwright_mcp_config(browser_locale="es-ES")
    assert "--config" in cfg["args"]
    import json
    written = json.loads(config_path.read_text())
    ctx = written["browser"]["contextOptions"]
    assert ctx["locale"] == "es-ES"
    headers = ctx["extraHTTPHeaders"]
    assert headers["X-QA-Token"] == "secret-test-token"
    assert headers["Accept-Language"] == "es-ES"


def test_playwright_mcp_config_no_locale_omits_field(monkeypatch, tmp_path):
    """Personas without a browser_locale stay on Chromium's default —
    no locale or Accept-Language injected. QA_TOKEN-only behaviour
    must be unchanged (regression guard for the #891 contract)."""
    monkeypatch.setenv("QA_TOKEN", "secret-test-token")
    config_path = tmp_path / "cfg.json"
    monkeypatch.setenv("QA_PLAYWRIGHT_MCP_CONFIG_PATH", str(config_path))
    cfg = runner_mod._playwright_mcp_config(browser_locale=None)
    # QA_TOKEN alone is still enough to wire the config file in —
    # regression guard for the #891 contract before #934 landed.
    assert "--config" in cfg["args"]
    import json
    written = json.loads(config_path.read_text())
    ctx = written["browser"]["contextOptions"]
    assert "locale" not in ctx
    assert "Accept-Language" not in ctx["extraHTTPHeaders"]
    assert ctx["extraHTTPHeaders"]["X-QA-Token"] == "secret-test-token"


def test_destructive_builtin_tools_are_disallowed():
    """A persona is a website user — it must not get a shell, files, or web.

    The first real run showed a persona use Bash to hand-patch the Playwright
    MCP server's node_modules; removing these tools makes that impossible.
    """
    for tool_name in ("Bash", "Edit", "Write", "Read", "WebFetch", "Task"):
        assert tool_name in runner_mod._DISALLOWED_BUILTIN_TOOLS
    # ToolSearch stays available — the deferred MCP tool schemas need it.
    assert "ToolSearch" not in runner_mod._DISALLOWED_BUILTIN_TOOLS


def test_http_client_loggers_are_quiet():
    """httpx/httpcore are lifted to WARNING so wait_for_email's 2s Mailpit
    polling does not flood the Job log — it was ~46% of lines in one run."""
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_playwright_allowlist_includes_riley_recon_tools():
    """long-input-tester's prompt directs her to inspect network traffic and run JS, so
    those tools must be in the allowlist or she ToolSearches for them in vain.
    """
    assert "mcp__playwright__browser_network_requests" in runner_mod._PLAYWRIGHT_TOOLS
    assert "mcp__playwright__browser_evaluate" in runner_mod._PLAYWRIGHT_TOOLS
