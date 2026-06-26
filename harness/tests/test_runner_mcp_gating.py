"""Unit tests for the MCP server gating helpers (Slice C of #1028).

Tests the three pure helpers that decide which MCP servers get wired
into a run's ``ClaudeAgentOptions``. The full ``run_explore_phase``
pipeline is integration-tested elsewhere (test_setup_actions covers
the runner-integration boundary); these tests stay focused on the
gating policy itself so they're fast + deterministic.
"""

from __future__ import annotations

import logging

import pytest

from qa_agents.runner import (
    _allowed_tools_for,
    _mcp_servers_for,
    _resolve_enabled_mcp_servers,
    _resolve_openapi_spec_url,
)


# ---------------------------------------------------------------------------
# _resolve_enabled_mcp_servers — policy core.
# ---------------------------------------------------------------------------
def test_empty_choice_falls_back_to_catalog_defaults():
    """No operator selection → every default_enabled server is wired."""
    enabled = _resolve_enabled_mcp_servers(())
    # Baseline catalog: playwright + email + findings + identity (#1023)
    # + a11y (#1021), all default_enabled.
    assert enabled == frozenset(
        {"playwright", "email", "findings", "identity", "a11y"}
    )


def test_empty_list_treated_same_as_empty_tuple():
    """The TriggerRunRequest carries a list; the env-parser carries a
    tuple. Both shapes must produce the same outcome."""
    assert _resolve_enabled_mcp_servers([]) == _resolve_enabled_mcp_servers(())


def test_explicit_selection_narrows_to_chosen():
    """A non-empty operator selection is the exact opt-in set."""
    enabled = _resolve_enabled_mcp_servers(("playwright",))
    assert enabled == frozenset({"playwright"})


def test_explicit_selection_can_include_default_off_servers():
    """When more servers land in the catalog with default_enabled=False
    (e.g. Sentry MCP from #1022), the operator can still opt them IN
    explicitly. We don't have such servers today, so this test simulates
    the shape with playwright only — the contract is "non-empty list
    is the exact set, period."""
    enabled = _resolve_enabled_mcp_servers(("playwright", "findings"))
    assert enabled == frozenset({"playwright", "findings"})
    assert "email" not in enabled


def test_unknown_server_id_is_dropped_with_warning(caplog):
    """A stale env value referencing a removed server must not crash —
    log a warning, drop the bad id, proceed with the rest."""
    caplog.set_level(logging.WARNING, logger="qa_agents.runner")
    enabled = _resolve_enabled_mcp_servers(("playwright", "not-a-real-server"))
    assert enabled == frozenset({"playwright"})
    assert any(
        "unknown server id" in r.message.lower() for r in caplog.records
    )
    assert any("not-a-real-server" in r.message for r in caplog.records)


def test_all_unknown_ids_returns_empty_set(caplog):
    """If every id is unknown the result is an empty set — caller will
    likely produce a useless run, but that's the operator's choice; we
    don't second-guess them by silently filling in defaults."""
    caplog.set_level(logging.WARNING, logger="qa_agents.runner")
    enabled = _resolve_enabled_mcp_servers(("ghost", "phantom"))
    assert enabled == frozenset()


# ---------------------------------------------------------------------------
# _allowed_tools_for — the allowed-tools list reflects the enabled set.
# ---------------------------------------------------------------------------
def test_allowed_tools_includes_playwright_when_enabled():
    tools = _allowed_tools_for(
        frozenset({"playwright"}),
        email_tools=["send_email"],
        findings_tools=["note_finding"],
        identity_tools=["generate_identity"],
    )
    # The Playwright tool list is the module-level constant; every
    # playwright tool must be allowed when the server is enabled.
    assert any(t.startswith("mcp__playwright__") for t in tools)
    # No email/findings/identity tools when those servers are disabled.
    assert not any(t.startswith("mcp__email__") for t in tools)
    assert not any(t.startswith("mcp__findings__") for t in tools)
    assert not any(t.startswith("mcp__identity__") for t in tools)


def test_allowed_tools_includes_email_when_enabled():
    tools = _allowed_tools_for(
        frozenset({"email"}),
        email_tools=["send_email", "wait_for_email", "get_email"],
        findings_tools=["note_finding"],
        identity_tools=["generate_identity"],
    )
    assert "mcp__email__send_email" in tools
    assert "mcp__email__wait_for_email" in tools
    assert "mcp__email__get_email" in tools
    assert not any(t.startswith("mcp__findings__") for t in tools)
    assert not any(t.startswith("mcp__identity__") for t in tools)


def test_allowed_tools_includes_identity_when_enabled():
    """#1023 — Faker identity server. One tool today: generate_identity."""
    tools = _allowed_tools_for(
        frozenset({"identity"}),
        email_tools=["send_email"],
        findings_tools=["note_finding"],
        identity_tools=["generate_identity"],
    )
    assert "mcp__identity__generate_identity" in tools
    assert not any(t.startswith("mcp__playwright__") for t in tools)
    assert not any(t.startswith("mcp__email__") for t in tools)
    assert not any(t.startswith("mcp__findings__") for t in tools)


def test_allowed_tools_includes_chrome_devtools_when_enabled():
    """#1024 — Chrome DevTools MCP. Perf tools for slow-connection +
    perf-budget-evaluator personas. The server is default_enabled=False
    so operators must explicitly opt in via QA_ENABLED_MCPS."""
    tools = _allowed_tools_for(
        frozenset({"chrome_devtools"}),
        email_tools=["send_email"],
        findings_tools=["note_finding"],
        identity_tools=["generate_identity"],
    )
    assert "mcp__chrome_devtools__emulate_network" in tools
    assert "mcp__chrome_devtools__emulate_cpu" in tools
    assert "mcp__chrome_devtools__performance_start_trace" in tools
    assert not any(t.startswith("mcp__playwright__") for t in tools)
    assert not any(t.startswith("mcp__email__") for t in tools)


def test_allowed_tools_includes_a11y_when_enabled():
    """#1021 — a11y/axe MCP. Used by keyboard-only (Iris) + screen-reader
    (Solomon) personas to produce WCAG-citation findings."""
    tools = _allowed_tools_for(
        frozenset({"a11y"}),
        email_tools=["send_email"],
        findings_tools=["note_finding"],
        identity_tools=["generate_identity"],
    )
    assert "mcp__a11y__audit" in tools
    assert not any(t.startswith("mcp__playwright__") for t in tools)
    assert not any(t.startswith("mcp__identity__") for t in tools)


def test_allowed_tools_includes_internal_servers_when_enabled():
    """Internal-group servers (loadgen / cost / openai_billing) qualify
    their in-process tool names like the other in-process servers."""
    tools = _allowed_tools_for(
        frozenset({"loadgen", "cost", "openai_billing"}),
        email_tools=["send_email"],
        findings_tools=["note_finding"],
        identity_tools=["generate_identity"],
        loadgen_tools=["blast"],
        cost_tools=["cost_report", "usage_summary"],
        openai_billing_tools=["openai_costs", "openai_usage"],
    )
    assert "mcp__loadgen__blast" in tools
    assert "mcp__cost__cost_report" in tools
    assert "mcp__cost__usage_summary" in tools
    assert "mcp__openai_billing__openai_costs" in tools
    assert "mcp__openai_billing__openai_usage" in tools


def test_internal_persona_auto_unions_its_required_servers():
    """The internal-load-economist persona literally cannot do its job
    without the load generator + cost readers, so they auto-union in via
    persona_compat even on a default (empty-choice) run — the same
    mechanism api-poker relies on for openapi."""
    enabled = _resolve_enabled_mcp_servers((), persona_id="internal-load-economist")
    assert {"loadgen", "cost", "openai_billing"} <= enabled


def test_allowed_tools_empty_when_no_servers_enabled():
    tools = _allowed_tools_for(
        frozenset(),
        email_tools=["send_email"],
        findings_tools=["note_finding"],
        identity_tools=["generate_identity"],
    )
    assert tools == []


def test_allowed_tools_returns_a_new_list_each_call():
    """Callers may sort / append for logging — a shared internal list
    would poison the next call. Defensive test."""
    a = _allowed_tools_for(
        frozenset({"findings"}), [], ["note_finding"], []
    )
    b = _allowed_tools_for(
        frozenset({"findings"}), [], ["note_finding"], []
    )
    assert a == b
    a.append("hacked")
    assert "hacked" not in b


# ---------------------------------------------------------------------------
# _mcp_servers_for — the SDK ``mcp_servers`` dict reflects the enabled set.
# ---------------------------------------------------------------------------
def test_mcp_servers_for_full_baseline():
    servers = _mcp_servers_for(
        frozenset({"playwright", "email", "findings", "identity"}),
        persona_browser_locale="en-GB",
        email_server=object(),
        findings_server=object(),
        identity_server=object(),
    )
    assert set(servers) == {"playwright", "email", "findings", "identity"}


def test_mcp_servers_for_omits_disabled_keys():
    """The SDK's ``mcp_servers`` dict is the only way the loop discovers
    a server; omitting the key prevents construction AND keeps the model
    from ever seeing the tools (combined with _allowed_tools_for)."""
    servers = _mcp_servers_for(
        frozenset({"playwright"}),
        persona_browser_locale=None,
        email_server=object(),
        findings_server=object(),
        identity_server=object(),
    )
    assert "playwright" in servers
    assert "email" not in servers
    assert "findings" not in servers
    assert "identity" not in servers


def test_mcp_servers_for_chrome_devtools_when_enabled():
    """#1024 — chrome_devtools server is wired when in the enabled set.
    Its config is built fresh per-call (it's stdio) so we just check the
    key is present and points at a dict-shaped config."""
    servers = _mcp_servers_for(
        frozenset({"chrome_devtools"}),
        persona_browser_locale=None,
        email_server=object(),
        findings_server=object(),
        identity_server=object(),
    )
    assert "chrome_devtools" in servers
    cfg = servers["chrome_devtools"]
    assert isinstance(cfg, dict)
    assert cfg.get("type") == "stdio"
    assert cfg.get("command")
    assert isinstance(cfg.get("args"), list)


def test_mcp_servers_for_identity_when_enabled():
    """#1023 — identity server is wired when in the enabled set."""
    sentinel = object()
    servers = _mcp_servers_for(
        frozenset({"identity"}),
        persona_browser_locale=None,
        email_server=object(),
        findings_server=object(),
        identity_server=sentinel,
    )
    assert servers == {"identity": sentinel}


def test_mcp_servers_for_a11y_when_enabled():
    """#1021 — a11y server config is built per-call (stdio)."""
    servers = _mcp_servers_for(
        frozenset({"a11y"}),
        persona_browser_locale=None,
        email_server=object(),
        findings_server=object(),
        identity_server=object(),
    )
    assert "a11y" in servers
    cfg = servers["a11y"]
    assert isinstance(cfg, dict)
    assert cfg.get("type") == "stdio"
    assert cfg.get("command")


def test_mcp_servers_for_openapi_wires_passed_in_server():
    """The openapi server is now an in-process SDK server built by the
    runner and passed in (not a stdio config built here). When enabled,
    _mcp_servers_for wires exactly that object under the 'openapi' key."""
    sentinel = object()
    servers = _mcp_servers_for(
        frozenset({"openapi"}),
        persona_browser_locale=None,
        email_server=object(),
        findings_server=object(),
        identity_server=object(),
        openapi_server=sentinel,
    )
    assert servers["openapi"] is sentinel


def test_mcp_servers_for_internal_servers_wire_passed_in_objects():
    """loadgen / cost / openai_billing are in-process SDK servers built by
    the runner and passed in; when enabled, _mcp_servers_for wires exactly
    those objects under their keys."""
    s_loadgen, s_cost, s_openai = object(), object(), object()
    servers = _mcp_servers_for(
        frozenset({"loadgen", "cost", "openai_billing"}),
        persona_browser_locale=None,
        email_server=object(),
        findings_server=object(),
        identity_server=object(),
        loadgen_server=s_loadgen,
        cost_server=s_cost,
        openai_billing_server=s_openai,
    )
    assert servers["loadgen"] is s_loadgen
    assert servers["cost"] is s_cost
    assert servers["openai_billing"] is s_openai


def test_mcp_servers_for_internal_servers_omitted_when_not_enabled():
    """Even with the objects passed, they're not wired unless enabled."""
    servers = _mcp_servers_for(
        frozenset({"playwright"}),
        persona_browser_locale=None,
        email_server=object(),
        findings_server=object(),
        identity_server=object(),
        loadgen_server=object(),
        cost_server=object(),
        openai_billing_server=object(),
    )
    assert "loadgen" not in servers
    assert "cost" not in servers
    assert "openai_billing" not in servers


def test_mcp_servers_for_openapi_omitted_when_not_enabled():
    """Even with an openapi_server passed, it's not wired unless 'openapi'
    is in the enabled set."""
    servers = _mcp_servers_for(
        frozenset({"playwright"}),
        persona_browser_locale=None,
        email_server=object(),
        findings_server=object(),
        identity_server=object(),
        openapi_server=object(),
    )
    assert "openapi" not in servers


def test_allowed_tools_includes_openapi_when_enabled():
    """OpenAPI tool names (qualified from the in-process server's bare
    names) surface for the api-poker persona."""
    tools = _allowed_tools_for(
        frozenset({"openapi"}),
        email_tools=["send_email"],
        findings_tools=["note_finding"],
        identity_tools=["generate_identity"],
        openapi_tools=["list_endpoints", "get_endpoint", "search"],
    )
    assert "mcp__openapi__list_endpoints" in tools
    assert "mcp__openapi__get_endpoint" in tools
    assert "mcp__openapi__search" in tools
    assert not any(t.startswith("mcp__playwright__") for t in tools)


def test_allowed_tools_openapi_omitted_when_not_enabled():
    """Disabled openapi → its tools never enter the allow-list, even if
    the names are passed in."""
    tools = _allowed_tools_for(
        frozenset({"email"}),
        email_tools=["send_email"],
        findings_tools=["note_finding"],
        identity_tools=["generate_identity"],
        openapi_tools=["list_endpoints", "get_endpoint", "search"],
    )
    assert not any(t.startswith("mcp__openapi__") for t in tools)


def test_mcp_servers_for_empty_set_returns_empty_dict():
    """The harness MUST tolerate an empty dict (e.g. an experiment where
    the operator disabled everything to see what the model does with no
    tools). The SDK is happy with mcp_servers={}; the persona simply
    can't do anything tool-like."""
    servers = _mcp_servers_for(
        frozenset(),
        persona_browser_locale=None,
        email_server=object(),
        findings_server=object(),
        identity_server=object(),
    )
    assert servers == {}


def test_mcp_servers_for_unknown_id_is_silently_ignored():
    """The dict-build code only checks for known ids; an unknown id in
    the enabled set produces no entry. This is the defence-in-depth
    that _resolve_enabled_mcp_servers' warn-and-drop sits on top of."""
    servers = _mcp_servers_for(
        frozenset({"playwright", "not-a-thing"}),
        persona_browser_locale=None,
        email_server=object(),
        findings_server=object(),
        identity_server=object(),
    )
    assert set(servers) == {"playwright"}


# ---------------------------------------------------------------------------
# #1354 — persona-compat auto-enable. A persona's persona_compat MCPs are
# *required* for that persona to function (api-poker without the OpenAPI
# MCP can't do its job); auto-include them so a forgetful operator
# triggering an api-poker run gets the OpenAPI tools without ticking the
# MCP picker. The MCPs are not opt-out from the persona side — if the
# operator wants to test missing-MCP behaviour they pick a different
# persona.
# ---------------------------------------------------------------------------
def test_persona_compat_mcps_auto_enable_with_empty_choice(caplog):
    """api-poker run with no explicit MCP selection picks up openapi
    (default_enabled=False) on top of the catalog defaults."""
    caplog.set_level(logging.INFO, logger="qa_agents.runner")
    enabled = _resolve_enabled_mcp_servers((), persona_id="api-poker")
    assert "openapi" in enabled
    # Catalog defaults still in (regression pin against accidentally
    # narrowing instead of unioning).
    assert {"playwright", "email", "findings", "identity", "a11y"} <= enabled
    # We log the auto-enable for operator observability.
    assert any(
        "auto-enabling" in r.message and "openapi" in r.message
        for r in caplog.records
    )


def test_persona_compat_mcps_auto_enable_overrides_explicit_omission():
    """Even when the operator narrows the explicit list to
    {playwright}, the api-poker persona still gets openapi auto-
    unioned. Required tools are non-negotiable — pick a different
    persona to test the missing-MCP path."""
    enabled = _resolve_enabled_mcp_servers(
        ("playwright",), persona_id="api-poker",
    )
    assert enabled == frozenset({"playwright", "openapi"})


def test_persona_with_no_compat_mcps_unchanged():
    """A persona that doesn't appear in any catalog persona_compat
    list (e.g. desktop-evaluator) produces the same result with or
    without persona_id — the union is a no-op."""
    without = _resolve_enabled_mcp_servers(())
    with_id = _resolve_enabled_mcp_servers(
        (), persona_id="desktop-evaluator",
    )
    assert without == with_id


def test_persona_id_none_is_legacy_shape():
    """Pre-#1354 callers (no persona_id kwarg) get the original
    behaviour. Regression pin so we don't break older tooling."""
    legacy = _resolve_enabled_mcp_servers(())
    new_default = _resolve_enabled_mcp_servers((), persona_id=None)
    assert legacy == new_default


def test_persona_compat_with_unknown_persona_is_noop():
    """A persona id that no catalog server lists in persona_compat
    (typo, removed persona, etc.) doesn't crash and doesn't widen
    the enabled set — just falls through to the operator-choice or
    catalog-default behaviour."""
    enabled = _resolve_enabled_mcp_servers((), persona_id="not-a-persona")
    catalog_defaults = _resolve_enabled_mcp_servers(())
    assert enabled == catalog_defaults


# ---------------------------------------------------------------------------
# #1354 — QA_OPENAPI_URL fallback derived from QA_WEB_BASE_URL. The
# api-poker persona running against the default sandbox shouldn't need
# the operator to also remember a separate env. FastAPI publishes the
# spec at /openapi.json by default.
# ---------------------------------------------------------------------------
def test_openapi_spec_url_derived_from_web_base_url(monkeypatch):
    """When QA_OPENAPI_URL is unset but web_base_url is passed, the spec
    URL is derived as {web_base_url}/openapi.json."""
    monkeypatch.delenv("QA_OPENAPI_URL", raising=False)
    assert (
        _resolve_openapi_spec_url("https://sandbox.slyreply.ai")
        == "https://sandbox.slyreply.ai/openapi.json"
    )


def test_openapi_spec_url_env_overrides_derived(monkeypatch):
    """QA_OPENAPI_URL takes precedence over the derived web-base
    fallback. Operators targeting a non-FastAPI tenant or a versioned
    spec path use the env var; the fallback is just a convenience for the
    common case."""
    monkeypatch.setenv("QA_OPENAPI_URL", "https://other.example/v2/openapi.json")
    assert (
        _resolve_openapi_spec_url("https://sandbox.slyreply.ai")
        == "https://other.example/v2/openapi.json"
    )


def test_openapi_spec_url_trailing_slash_stripped(monkeypatch):
    """A trailing slash on web_base_url must not become a double slash in
    the derived spec URL."""
    monkeypatch.delenv("QA_OPENAPI_URL", raising=False)
    assert (
        _resolve_openapi_spec_url("https://sandbox.slyreply.ai/")
        == "https://sandbox.slyreply.ai/openapi.json"
    )


def test_openapi_spec_url_env_whitespace_stripped(monkeypatch):
    """A stray-whitespace QA_OPENAPI_URL is trimmed, not treated as a
    URL with spaces."""
    monkeypatch.setenv("QA_OPENAPI_URL", "  https://x.example/spec.json  ")
    assert _resolve_openapi_spec_url(None) == "https://x.example/spec.json"


def test_openapi_no_url_at_all_returns_none(monkeypatch):
    """When neither QA_OPENAPI_URL nor web_base_url is provided, the
    resolver returns None — the in-process server then exposes the tools
    but each returns a file-a-finding message."""
    monkeypatch.delenv("QA_OPENAPI_URL", raising=False)
    assert _resolve_openapi_spec_url(None) is None
    assert _resolve_openapi_spec_url("") is None


# ---------------------------------------------------------------------------
# Config wiring (sanity) — QA_ENABLED_MCPS parses into the expected tuple.
# ---------------------------------------------------------------------------
def test_config_parses_qa_enabled_mcps_env(monkeypatch):
    from qa_agents.config import Config
    monkeypatch.setenv("QA_ENABLED_MCPS", "playwright, email")
    # Other required env vars need defaults — Config.from_env() is built
    # to tolerate missing values for everything except a few keys we set
    # below as part of the test isolation.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    cfg = Config.from_env()
    # Whitespace around the comma must be stripped.
    assert cfg.enabled_mcp_servers == ("playwright", "email")


def test_config_empty_qa_enabled_mcps_parses_to_empty_tuple(monkeypatch):
    from qa_agents.config import Config
    monkeypatch.setenv("QA_ENABLED_MCPS", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    cfg = Config.from_env()
    assert cfg.enabled_mcp_servers == ()


def test_config_unset_qa_enabled_mcps_parses_to_empty_tuple(monkeypatch):
    from qa_agents.config import Config
    monkeypatch.delenv("QA_ENABLED_MCPS", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    cfg = Config.from_env()
    assert cfg.enabled_mcp_servers == ()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("playwright", ("playwright",)),
        ("playwright,email,findings", ("playwright", "email", "findings")),
        # whitespace tolerance — matches the mandatory_action_ids parser.
        ("  playwright , email ", ("playwright", "email")),
        # trailing/leading commas produce no empty-string entries.
        (",playwright,,email,", ("playwright", "email")),
    ],
)
def test_config_qa_enabled_mcps_parse_shapes(monkeypatch, raw, expected):
    from qa_agents.config import Config
    monkeypatch.setenv("QA_ENABLED_MCPS", raw)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    cfg = Config.from_env()
    assert cfg.enabled_mcp_servers == expected
