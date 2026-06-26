"""Unit tests for the in-process OpenAPI surface-discovery MCP server.

Covers the pure spec helpers (list/get/search/ref-inlining/parse) and the
lazy cached SpecProvider + tool handlers. The handlers are exercised
directly via make_openapi_handlers (the SDK doesn't surface handlers on the
server object — same idiom as test_findings_live_writer).
"""

from __future__ import annotations

import json

import httpx
import pytest

from qa_agents.tools.openapi import (
    SpecProvider,
    _inline_refs,
    _iter_operations,
    _resolve_ref,
    build_openapi_server,
    get_endpoint,
    list_endpoints,
    make_openapi_handlers,
    parse_spec,
    search_endpoints,
)

# A small but representative spec: a couple of UI-visible endpoints, an
# admin-shaped one the UI wouldn't surface, $ref-ed request/response bodies,
# and a self-referential schema to exercise the cycle guard.
SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Demo API", "version": "1.0.0"},
    "paths": {
        "/login": {
            "post": {
                "summary": "Log in",
                "operationId": "login",
                "tags": ["auth"],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Login"}
                        }
                    }
                },
            },
            "parameters": [{"name": "ignored", "in": "query"}],
        },
        "/admin/export": {
            "get": {
                "summary": "Export everything",
                "operationId": "adminExport",
                "tags": ["admin"],
                "description": "Dumps the whole database as CSV.",
            }
        },
        "/profile": {
            "get": {"summary": "My profile"},
            "$ref": "#/components/notAnOperation",
        },
    },
    "components": {
        "schemas": {
            "Login": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "next": {"$ref": "#/components/schemas/Login"},
                },
            }
        }
    },
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_iter_operations_skips_non_operation_keys():
    ops = _iter_operations(SPEC)
    pairs = {(m, p) for m, p, _ in ops}
    assert pairs == {("POST", "/login"), ("GET", "/admin/export"), ("GET", "/profile")}
    # 'parameters' and '$ref' under a path item are not operations.
    assert not any(m == "PARAMETERS" for m, _, _ in ops)


def test_iter_operations_tolerates_malformed_spec():
    assert _iter_operations({}) == []
    assert _iter_operations({"paths": None}) == []
    assert _iter_operations({"paths": {"/x": "nonsense"}}) == []


def test_list_endpoints_uses_summary_then_operation_id():
    rows = list_endpoints(SPEC)
    by_path = {(r["method"], r["path"]): r["summary"] for r in rows}
    assert by_path[("POST", "/login")] == "Log in"
    assert by_path[("GET", "/admin/export")] == "Export everything"
    # /profile has only a summary.
    assert by_path[("GET", "/profile")] == "My profile"


def test_resolve_ref_local_and_external():
    assert _resolve_ref(SPEC, "#/components/schemas/Login")["type"] == "object"
    # External / unresolvable refs return None.
    assert _resolve_ref(SPEC, "https://other/spec#/x") is None
    assert _resolve_ref(SPEC, "#/components/schemas/Missing") is None


def test_get_endpoint_inlines_refs_and_guards_cycles():
    op = get_endpoint(SPEC, "post", "/login")
    schema = op["requestBody"]["content"]["application/json"]["schema"]
    # First-level ref inlined to the object schema.
    assert schema["type"] == "object"
    assert "email" in schema["properties"]
    # The self-referential 'next' ref is left as a pointer (cycle guard),
    # not expanded infinitely.
    next_node = schema["properties"]["next"]
    assert next_node == {"$ref": "#/components/schemas/Login"}


def test_get_endpoint_method_case_insensitive_and_missing():
    assert get_endpoint(SPEC, "POST", "/login") is not None
    assert get_endpoint(SPEC, "get", "/login") is None  # wrong method
    assert get_endpoint(SPEC, "get", "/nope") is None  # wrong path


def test_inline_refs_depth_budget_stops_expanding():
    # At depth 0 the ref is returned verbatim rather than resolved.
    node = {"$ref": "#/components/schemas/Login"}
    assert _inline_refs(SPEC, node, depth=0) == node


def test_search_matches_path_summary_description_and_tags():
    # By tag.
    assert {r["path"] for r in search_endpoints(SPEC, "admin")} == {"/admin/export"}
    # By description substring.
    assert {r["path"] for r in search_endpoints(SPEC, "database")} == {"/admin/export"}
    # By path.
    assert {r["path"] for r in search_endpoints(SPEC, "login")} == {"/login"}
    # Case-insensitive.
    assert search_endpoints(SPEC, "EXPORT")
    # Empty query → no matches.
    assert search_endpoints(SPEC, "   ") == []


def test_parse_spec_json():
    assert parse_spec(json.dumps(SPEC))["info"]["title"] == "Demo API"


def test_parse_spec_yaml_fallback():
    pytest.importorskip("yaml")
    yaml_text = "openapi: '3.0.0'\npaths:\n  /x:\n    get:\n      summary: hi\n"
    spec = parse_spec(yaml_text)
    assert list_endpoints(spec) == [{"method": "GET", "path": "/x", "summary": "hi"}]


def test_parse_spec_rejects_garbage():
    with pytest.raises((ValueError, Exception)):
        parse_spec("::: not json :: not yaml mapping :::\n\t- [}{")


# ---------------------------------------------------------------------------
# SpecProvider — lazy, cached, never crashes.
# ---------------------------------------------------------------------------
def _mock_async_client(monkeypatch, handler):
    """Patch httpx.AsyncClient so _fetch_spec hits an in-memory handler."""
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs.pop("follow_redirects", None)
        kwargs["transport"] = transport
        return real_async_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_provider_fetches_and_caches(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=SPEC)

    _mock_async_client(monkeypatch, handler)
    provider = SpecProvider("https://tenant.example/openapi.json")
    first = await provider.load()
    second = await provider.load()
    assert first is second  # cached
    assert calls["n"] == 1  # fetched once
    assert provider.error is None


async def test_provider_none_url_yields_error_message():
    provider = SpecProvider(None)
    assert await provider.load() is None
    assert "didn't supply a spec URL" in provider.error


async def test_provider_http_error_is_caught(monkeypatch):
    def handler(request):
        return httpx.Response(404, text="not found")

    _mock_async_client(monkeypatch, handler)
    provider = SpecProvider("https://tenant.example/openapi.json")
    assert await provider.load() is None
    assert "Could not load the OpenAPI spec" in provider.error
    # A second load doesn't re-fetch / re-raise — error is sticky.
    assert await provider.load() is None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------
def _text(result: dict) -> str:
    return result["content"][0]["text"]


async def test_handlers_happy_path(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=SPEC)

    _mock_async_client(monkeypatch, handler)
    handlers = make_openapi_handlers(SpecProvider("https://t/openapi.json"))

    listed = json.loads(_text(await handlers["list_endpoints"]({})))
    assert listed["count"] == 3
    assert {"method": "GET", "path": "/admin/export", "summary": "Export everything"} in listed["endpoints"]

    got = json.loads(_text(await handlers["get_endpoint"]({"method": "post", "path": "/login"})))
    assert got["method"] == "POST"
    assert got["operation"]["requestBody"]["content"]["application/json"]["schema"]["type"] == "object"

    found = json.loads(_text(await handlers["search"]({"query": "admin"})))
    assert found["count"] == 1
    assert found["matches"][0]["path"] == "/admin/export"


async def test_handlers_report_missing_endpoint(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=SPEC)

    _mock_async_client(monkeypatch, handler)
    handlers = make_openapi_handlers(SpecProvider("https://t/openapi.json"))
    msg = _text(await handlers["get_endpoint"]({"method": "delete", "path": "/login"}))
    assert "No operation DELETE /login" in msg
    assert "list_endpoints" in msg


async def test_handlers_no_spec_url_tell_persona_to_file_finding():
    handlers = make_openapi_handlers(SpecProvider(None))
    for name in ("list_endpoints", "get_endpoint", "search"):
        msg = _text(await handlers[name]({"method": "get", "path": "/x", "query": "q"}))
        assert "file ONE finding" in msg.lower() or "File ONE finding" in msg


async def test_handlers_empty_spec_paths(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"openapi": "3.0.0", "paths": {}})

    _mock_async_client(monkeypatch, handler)
    handlers = make_openapi_handlers(SpecProvider("https://t/openapi.json"))
    msg = _text(await handlers["list_endpoints"]({}))
    assert "no paths" in msg.lower()


# ---------------------------------------------------------------------------
# build_openapi_server — public contract.
# ---------------------------------------------------------------------------
def test_build_openapi_server_returns_three_tool_names():
    server, names = build_openapi_server(spec_url=None)
    assert names == ["list_endpoints", "get_endpoint", "search"]
    assert server is not None
