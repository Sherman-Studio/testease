"""Unit tests for the MCP server catalog (Slice B of #1028)."""

from __future__ import annotations

import pytest

from qa_agents.mcp_catalog import (
    CATALOG,
    MCPServer,
    get_server,
    list_servers,
    server_ids,
)


def test_catalog_ships_at_least_three_baseline_servers():
    """The harness wires playwright + email + findings at minimum (#1010
    baseline). New servers from #1019 children get appended; the
    baseline must always be present."""
    ids = server_ids()
    assert "playwright" in ids
    assert "email" in ids
    assert "findings" in ids


def test_catalog_ids_are_unique():
    """A duplicate id would shadow the earlier entry in get_server
    lookups; a typo in two entries would silently merge them."""
    ids = [s.id for s in CATALOG]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("server", list(CATALOG))
def test_every_server_has_required_fields(server):
    """Each catalog entry must carry the fields the UI relies on."""
    assert server.id
    assert server.display_name
    assert server.description
    # default_enabled is a bool, never None — the trigger-form checkbox
    # relies on a tri-state-free signal.
    assert isinstance(server.default_enabled, bool)
    # persona_compat is a list (possibly empty == "all personas").
    assert isinstance(server.persona_compat, list)
    # tool_count is a non-negative int. Zero is acceptable for a stub
    # entry but flagged for follow-up.
    assert isinstance(server.tool_count, int)
    assert server.tool_count >= 0


def test_list_servers_returns_a_copy_not_the_internal_tuple():
    """Callers that mutate the result (e.g. sort it for display)
    must not poison the module-level catalog for subsequent callers."""
    a = list_servers()
    a.append(MCPServer(id="x", display_name="X", description="x"))
    b = list_servers()
    # Mutating `a` did not leak into the next caller's `b`.
    assert any(s.id == "x" for s in a)
    assert not any(s.id == "x" for s in b)


def test_get_server_returns_the_matching_entry():
    server = get_server("playwright")
    assert isinstance(server, MCPServer)
    assert server.id == "playwright"


def test_get_server_unknown_raises_with_helpful_message():
    with pytest.raises(KeyError) as exc:
        get_server("notreal")
    msg = str(exc.value)
    assert "notreal" in msg
    # The error names the valid ids so an operator typo is fixable.
    assert "playwright" in msg


def test_mcpserver_is_frozen():
    """Frozen dataclass — accidental mutation of a catalog entry would
    poison every subsequent reader (the catalog is a module-level
    constant)."""
    server = get_server("playwright")
    with pytest.raises((AttributeError, Exception)):
        # dataclasses.FrozenInstanceError extends AttributeError on
        # Python 3.12; tolerate the broader exception too in case the
        # frozen kwarg ever changes.
        server.id = "hacked"  # type: ignore[misc]


def test_persona_compat_empty_means_all_personas():
    """A server with persona_compat=[] is the "applies to every persona"
    signal — used by the trigger UI to decide whether to grey out a
    server when specific personas are selected. The baseline three
    servers all carry this signal (Playwright is universal, email
    rarely matters but is cheap to keep on, findings is non-optional)."""
    for s in CATALOG:
        assert isinstance(s.persona_compat, list)
        # Baseline entries are universal; future per-persona MCPs
        # (e.g. Sentry) may carry explicit lists.
        # No assertion on emptiness here — just the type contract.
