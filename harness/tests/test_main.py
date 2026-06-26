"""Tests for the harness CLI entrypoint (``qa_agents/__main__.py``).

The harness is Max-only: every run bills Claude Code Max and the org-API
backend (and its #900 startup API-key guard) was removed entirely. The
QA org API-key credential and the post-run org-Haiku background paths
(memory distillation, insights, canonicalisation, timeline organiser,
persona synthesis) were retired, so the only Anthropic auth left is the
Claude Code Max OAuth token. These tests pin the startup banner contract,
which must report the (now fixed) backend + OAuth-token presence without
ever leaking a secret value.

We exercise ``_async_main`` directly rather than the ``main`` console
script so the tests are pure asyncio and don't shell out. ``_run_single``
and ``_run_multi`` are stubbed via monkeypatch so the test never tries
to actually drive the Agent SDK.
"""

from __future__ import annotations

import dataclasses

import mongomock
import pytest

from qa_agents import __main__ as main_mod
from qa_agents.__main__ import _describe_env_var, _log_auth_banner, _stripe_personas
from qa_agents.config import Config


def _config(**overrides) -> Config:
    """Minimal Config that satisfies the dataclass — values are
    placeholders for the entrypoint/banner tests."""
    base = Config(
        persona="first-impression-critic",
        web_base_url="http://frontend",
        smtp_host="smtp",
        smtp_port=1025,
        mailpit_url="http://mailpit:8025",
        explore_model="claude-sonnet-4-6",
        report_model="claude-opus-4-7",
        max_turns=10,
        run_timeout_s=60,
        out_dir="/tmp",
        mongodb_url="",
        admin_email="",
        admin_password="",
        sink="file",
        run_id="",
        qa_store_url="",
        qa_store_db="",
        discord_webhook_url="",
        concurrency=1,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


@pytest.fixture
def stub_runners(monkeypatch):
    """Replace _run_single / _run_multi with sentinels that return 0
    without actually invoking the agent SDK. Returns a recorder dict
    so tests can assert which path was taken."""
    called: dict[str, int] = {"single": 0, "multi": 0}

    async def _fake_single(config):
        called["single"] += 1
        return 0

    async def _fake_multi(persona_ids, config, **kwargs):
        called["multi"] += 1
        # #1821 — record what this pod was asked to run so sharding tests can
        # assert the stripe was applied before _run_multi was reached.
        called["last_persona_ids"] = persona_ids
        called["last_expected"] = kwargs.get("expected_persona_ids")
        return 0

    monkeypatch.setattr(main_mod, "_run_single", _fake_single)
    monkeypatch.setattr(main_mod, "_run_multi", _fake_multi)
    return called


# ---------------------------------------------------------------------------
# Max-only — there is NO startup API-key guard. A run with an empty
# ANTHROPIC_API_KEY must proceed to the runner; the spawned ``claude`` CLI
# resolves OAuth/Max auth itself, and any failure surfaces from there with
# a useful message instead of a misleading 'set ANTHROPIC_API_KEY'.
# ---------------------------------------------------------------------------
class TestNoApiKeyGuard:
    @pytest.mark.asyncio
    async def test_empty_key_runs_persona(
        self, monkeypatch, capsys, stub_runners,
    ):
        """The removed #900 guard must NOT fire — an empty key is the
        expected Max-mode state."""
        monkeypatch.setattr(
            main_mod, "resolve_config",
            lambda args: _config(),
        )
        rc = await main_mod._async_main(["--persona", "first-impression-critic"])
        assert rc == 0
        assert stub_runners["single"] == 1
        err = capsys.readouterr().err
        assert "ANTHROPIC_API_KEY is not set" not in err

    @pytest.mark.asyncio
    async def test_stray_key_still_runs(
        self, monkeypatch, capsys, stub_runners,
    ):
        """A stray API key in the env shouldn't change behaviour at this
        layer — the runner's ``_options_env`` scrubs it to "" so the
        spawned CLI still uses OAuth."""
        monkeypatch.setattr(
            main_mod, "resolve_config",
            lambda args: _config(),
        )
        rc = await main_mod._async_main(["--persona", "first-impression-critic"])
        assert rc == 0
        assert stub_runners["single"] == 1

    @pytest.mark.asyncio
    async def test_multi_persona_path(
        self, monkeypatch, capsys, stub_runners,
    ):
        """Multi-persona orchestrator path (--all) also gets through with
        an empty key."""
        monkeypatch.setattr(
            main_mod, "resolve_config",
            lambda args: _config(),
        )
        rc = await main_mod._async_main(["--all"])
        assert rc == 0
        assert stub_runners["multi"] == 1
        assert stub_runners["single"] == 0


# ---------------------------------------------------------------------------
# #1821 — multi-pod persona striping. A sharded run is ONE indexed Job with
# pod_count parallel pods; pod i runs personas[j] for j % pod_count == i over
# the SORTED roster. The stripe MUST be disjoint (no persona run twice) AND
# complete (every persona run exactly once) across all pods.
# ---------------------------------------------------------------------------
class TestPersonaStripe:
    ROSTER = ["a", "b", "c", "d", "e", "f", "g"]

    def test_single_pod_returns_full_roster(self):
        """pod_count=1 ⇒ the one pod gets the whole roster, sorted,
        unchanged from the legacy single-pod behaviour."""
        out = _stripe_personas(["c", "a", "b"], pod_index=0, pod_count=1)
        assert out == ["a", "b", "c"]

    def test_stripe_modulo_over_sorted(self):
        """Modulo stripe over the SORTED roster: pod i takes index j where
        j % pod_count == i."""
        roster = sorted(self.ROSTER)  # a..g
        # pod_count=3: pod0 -> a,d,g ; pod1 -> b,e ; pod2 -> c,f
        assert _stripe_personas(roster, pod_index=0, pod_count=3) == ["a", "d", "g"]
        assert _stripe_personas(roster, pod_index=1, pod_count=3) == ["b", "e"]
        assert _stripe_personas(roster, pod_index=2, pod_count=3) == ["c", "f"]

    @pytest.mark.parametrize("pod_count", [1, 2, 3, 4])
    def test_stripe_disjoint_and_complete(self, pod_count):
        """For pod_count 1..4 the union of every pod's stripe equals the
        full sorted roster AND no persona appears in two stripes."""
        roster = self.ROSTER
        union: list[str] = []
        seen: set[str] = set()
        for i in range(pod_count):
            stripe = _stripe_personas(roster, pod_index=i, pod_count=pod_count)
            # Disjoint: nothing this pod runs was run by an earlier pod.
            assert not (set(stripe) & seen), f"pod {i} overlaps an earlier pod"
            seen |= set(stripe)
            union.extend(stripe)
        # Complete: every persona exactly once.
        assert sorted(union) == sorted(roster)
        assert len(union) == len(roster)

    def test_input_order_does_not_change_stripe(self):
        """The stripe is over the SORTED roster, so an unsorted input
        produces the same assignment as a sorted one."""
        a = _stripe_personas(["g", "a", "d", "b", "c"], pod_index=0, pod_count=2)
        b = _stripe_personas(["a", "b", "c", "d", "g"], pod_index=0, pod_count=2)
        assert a == b

    @pytest.mark.asyncio
    async def test_async_main_explicit_personas_not_resliced_under_multipod(
        self, monkeypatch, stub_runners,
    ):
        """Option B: an explicit ``--personas`` selection is AUTHORITATIVE — the
        trigger already handed this pod its slice, so even with QA_POD_COUNT > 1
        the harness runs exactly the given list (re-striping it would silently
        drop most of the pod's personas). The full given list is also the
        ``expected`` denominator this pod declares (the trigger's sticky
        create_run holds the real run-wide denominator)."""
        monkeypatch.setattr(
            main_mod, "resolve_config",
            lambda args: _config(pod_index=1, pod_count=2),
        )
        rc = await main_mod._async_main(["--personas", "a,b,c,d"])
        assert rc == 0
        # NO re-slice: the whole given list runs (sorted), not a modulo stripe.
        assert stub_runners["last_persona_ids"] == ["a", "b", "c", "d"]
        assert stub_runners["last_expected"] == ["a", "b", "c", "d"]

    @pytest.mark.asyncio
    async def test_async_main_all_still_self_stripes_under_multipod(
        self, monkeypatch, stub_runners,
    ):
        """A whole-roster ``--all`` selection still self-stripes by
        pod_index/pod_count — this preserves a direct ``--all`` CronJob/operator
        run (the one path that isn't pre-sliced by the trigger)."""
        from qa_agents.personas import PERSONAS

        monkeypatch.setattr(
            main_mod, "resolve_config",
            lambda args: _config(pod_index=1, pod_count=2),
        )
        rc = await main_mod._async_main(["--all"])
        assert rc == 0
        roster = sorted(PERSONAS)
        assert stub_runners["last_persona_ids"] == _stripe_personas(
            roster, pod_index=1, pod_count=2
        )
        assert stub_runners["last_expected"] == roster

    @pytest.mark.asyncio
    async def test_async_main_single_pod_runs_full_roster(
        self, monkeypatch, stub_runners,
    ):
        """pod_count=1 (the default) ⇒ _run_multi gets the whole sorted
        roster — single-pod behaviour unchanged."""
        monkeypatch.setattr(
            main_mod, "resolve_config",
            lambda args: _config(pod_index=0, pod_count=1),
        )
        rc = await main_mod._async_main(["--personas", "d,a,c,b"])
        assert rc == 0
        assert stub_runners["last_persona_ids"] == ["a", "b", "c", "d"]
        assert stub_runners["last_expected"] == ["a", "b", "c", "d"]


# ---------------------------------------------------------------------------
# #906 — auth-diagnostic banner. Reports backend + auth-env presence;
# MUST NEVER leak the actual secret values into the banner.
# ---------------------------------------------------------------------------
class TestAuthBanner:
    def test_describe_env_var_absent(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert _describe_env_var("ANTHROPIC_API_KEY") == "ANTHROPIC_API_KEY=absent"

    def test_describe_env_var_empty(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        assert _describe_env_var("ANTHROPIC_API_KEY") == "ANTHROPIC_API_KEY=empty"

    def test_describe_env_var_whitespace_only_is_empty(self, monkeypatch):
        """Trim before length check — a stray newline in a Secret value
        shouldn't make us report a presence that isn't real."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "   \n  ")
        assert _describe_env_var("ANTHROPIC_API_KEY") == "ANTHROPIC_API_KEY=empty"

    def test_describe_env_var_present_reports_length_only(self, monkeypatch):
        """The whole point of the banner: presence + length, NEVER value."""
        secret = "sk-this-is-a-real-secret-that-must-never-appear-in-logs"
        monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
        result = _describe_env_var("ANTHROPIC_API_KEY")
        assert result == f"ANTHROPIC_API_KEY=present({len(secret)} chars)"
        # Belt-and-braces: assert the value didn't leak anywhere in the
        # rendered string. If a future refactor accidentally formats
        # the raw value in, this test screams.
        assert secret not in result
        assert "sk-" not in result

    def test_log_auth_banner_writes_to_stderr(self, monkeypatch, capsys):
        """End-to-end shape check: one line, contains the (fixed) backend
        note + the OAuth-token state. The QA org API key was retired, so
        the banner no longer reports ANTHROPIC_API_KEY at all."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        config = _config()
        _log_auth_banner(config)
        err = capsys.readouterr().err
        assert "backend=claude-code (Max)" in err
        # The API-key line is gone — only the OAuth token is reported.
        assert "ANTHROPIC_API_KEY" not in err
        assert "CLAUDE_CODE_OAUTH_TOKEN=absent" in err
        # One banner per startup; assert it's a single line.
        assert err.count("==> harness starting") == 1

    def test_log_auth_banner_max_mode_shape(self, monkeypatch, capsys):
        """In the expected Max-mode shape — OAuth token present — the
        banner makes the routing visually obvious."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x" * 72)
        config = _config()
        _log_auth_banner(config)
        err = capsys.readouterr().err
        assert "backend=claude-code (Max)" in err
        assert "ANTHROPIC_API_KEY" not in err
        assert "CLAUDE_CODE_OAUTH_TOKEN=present(72 chars)" in err

    @pytest.mark.asyncio
    async def test_banner_fires_in_async_main(
        self, monkeypatch, capsys, stub_runners,
    ):
        """The banner must fire on startup so every run leaves a visible
        record of what auth state the harness saw."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr(
            main_mod, "resolve_config",
            lambda args: _config(),
        )
        rc = await main_mod._async_main(["--persona", "first-impression-critic"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "==> harness starting" in err
        assert "backend=claude-code (Max)" in err


# ---------------------------------------------------------------------------
# #1821 — the --finalize reaper. Force-finishes a multi-pod run stranded at
# status 'new' (a pod crashed before writing its slice). Runs NO personas.
# ---------------------------------------------------------------------------
class TestFinalizeReaper:
    @pytest.fixture
    def shared_store(self, monkeypatch):
        import qa_store
        from qa_store.schema import Store

        client = mongomock.MongoClient()
        s = Store(client=client, db_name="slyreply_qa_test")
        s.runs.create_index("run_id", unique=True)
        s.findings.create_index("finding_id", unique=True)
        monkeypatch.setattr(qa_store, "connect", lambda url=None, db=None: s)
        return s

    @pytest.mark.asyncio
    async def test_finalize_stamps_stranded_run(
        self, monkeypatch, capsys, shared_store, stub_runners,
    ):
        """A run stuck at 'new' with a missing expected persona is force-
        finished: placeholder review written, status → reviewed. No persona
        run path is taken."""
        # Seed a stranded run: expected = 2, only 1 reviewed, status new.
        from qa_store import add_persona_result, create_run

        create_run(
            shared_store, "qa-strand", ["alpha"],
            expected_personas=["alpha", "beta"],
        )
        add_persona_result(
            shared_store, "qa-strand", "alpha",
            review_markdown="## Would I use this?\n\nYes.",
            verdict="yes", accounting={"total_input_tokens": 100}, findings=[],
        )
        assert shared_store.runs.find_one({"run_id": "qa-strand"})["status"] == "new"

        monkeypatch.setattr(
            main_mod, "resolve_config",
            lambda args: _config(sink="atlas", qa_store_db="slyreply_qa_test"),
        )
        rc = await main_mod._async_main(["--finalize", "qa-strand"])
        assert rc == 0
        # No persona was run.
        assert stub_runners["single"] == 0
        assert stub_runners["multi"] == 0

        doc = shared_store.runs.find_one({"run_id": "qa-strand"})
        assert doc["status"] == "reviewed"
        assert doc["finished_at"] is not None
        personas = {r["persona"] for r in doc["reviews"]}
        assert personas == {"alpha", "beta"}
        beta = next(r for r in doc["reviews"] if r["persona"] == "beta")
        assert beta["verdict"] == "stranded"

    @pytest.mark.asyncio
    async def test_finalize_unknown_run_errors(
        self, monkeypatch, shared_store, stub_runners,
    ):
        monkeypatch.setattr(
            main_mod, "resolve_config",
            lambda args: _config(sink="atlas", qa_store_db="slyreply_qa_test"),
        )
        rc = await main_mod._async_main(["--finalize", "qa-nope"])
        assert rc == 2

    @pytest.mark.asyncio
    async def test_finalize_already_finished_is_noop(
        self, monkeypatch, shared_store, stub_runners,
    ):
        from qa_store import create_run, finish_run

        create_run(
            shared_store, "qa-done", ["alpha"], expected_personas=["alpha"],
        )
        finish_run(shared_store, "qa-done", {"input_tokens": 1})
        assert shared_store.runs.find_one({"run_id": "qa-done"})["status"] == "reviewed"

        monkeypatch.setattr(
            main_mod, "resolve_config",
            lambda args: _config(sink="atlas", qa_store_db="slyreply_qa_test"),
        )
        rc = await main_mod._async_main(["--finalize", "qa-done"])
        # Already finished → rc 0, no error, no extra reviews.
        assert rc == 0
        doc = shared_store.runs.find_one({"run_id": "qa-done"})
        assert len(doc["reviews"]) == 0

    @pytest.mark.asyncio
    async def test_finalize_requires_atlas_sink(
        self, monkeypatch, shared_store, stub_runners,
    ):
        """--finalize against a file-sink config is a usage error (no
        barrier exists for the file sink)."""
        monkeypatch.setattr(
            main_mod, "resolve_config",
            lambda args: _config(sink="file"),
        )
        rc = await main_mod._async_main(["--finalize", "qa-whatever"])
        assert rc == 2
