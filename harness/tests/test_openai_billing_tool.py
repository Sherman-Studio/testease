"""Tests for the OpenAI organization billing MCP server.

No real network: ``_fetch`` is the single I/O boundary and is exercised
against a mocked ``httpx.AsyncClient`` (mirroring test_openapi_tools'
``_mock_async_client``). The pure summarisers + the YYYY-MM-DD → unix
conversion are tested directly. asyncio_mode=auto, so async tests need no
decorator.
"""

from __future__ import annotations

import httpx

from qa_agents.tools.openai_billing import (
    _date_to_unix,
    _fetch,
    build_openai_billing_server,
    summarise_costs,
    summarise_usage,
)

# --------------------------------------------------------------------------
# Sample OpenAI org-API payloads (bucketed shape: data[].results[]).
# --------------------------------------------------------------------------
COSTS_JSON = {
    "object": "page",
    "data": [
        {
            "object": "bucket",
            "results": [
                {
                    "object": "organization.costs.result",
                    "amount": {"value": 12.50, "currency": "usd"},
                    "line_item": "gpt-4o, completions",
                },
                {
                    "object": "organization.costs.result",
                    "amount": {"value": 3.25, "currency": "usd"},
                    "line_item": "image generation",
                },
            ],
        }
    ],
    "next_page": None,
}

USAGE_JSON = {
    "object": "page",
    "data": [
        {
            "object": "bucket",
            "results": [
                {
                    "object": "organization.usage.completions.result",
                    "input_tokens": 1000,
                    "output_tokens": 250,
                    "model": "gpt-4o",
                },
                {
                    "object": "organization.usage.completions.result",
                    "input_tokens": 400,
                    "output_tokens": 100,
                    "model": "gpt-4o-mini",
                },
            ],
        }
    ],
    "next_page": None,
}


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _mock_async_client(monkeypatch, handler):
    """Patch httpx.AsyncClient so _fetch hits an in-memory handler."""
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs.pop("follow_redirects", None)
        kwargs["transport"] = transport
        return real_async_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


# --------------------------------------------------------------------------
# YYYY-MM-DD → unix conversion.
# --------------------------------------------------------------------------
def test_date_to_unix_known_date_is_utc_midnight():
    # 2026-06-15 00:00:00 UTC == 1781481600 (verified independently).
    assert _date_to_unix("2026-06-15") == 1781481600


def test_date_to_unix_tolerates_surrounding_whitespace():
    assert _date_to_unix("  2026-06-15  ") == _date_to_unix("2026-06-15")


def test_date_to_unix_epoch():
    assert _date_to_unix("1970-01-01") == 0


# --------------------------------------------------------------------------
# Summarisers — pure, no I/O.
# --------------------------------------------------------------------------
def test_summarise_costs_includes_total_and_line_items():
    out = summarise_costs(COSTS_JSON)
    # 12.50 + 3.25 = 15.75 total.
    assert "15.7500 USD" in out
    assert "gpt-4o, completions" in out
    assert "image generation" in out


def test_summarise_costs_empty_says_zero():
    out = summarise_costs({"data": [], "next_page": None})
    assert "0.00" in out
    assert "no cost rows" in out.lower()


def test_summarise_costs_notes_truncation_when_next_page_present():
    body = dict(COSTS_JSON, next_page="page_2_cursor")
    out = summarise_costs(body)
    assert "TRUNCATED" in out


def test_summarise_usage_includes_token_totals_and_models():
    out = summarise_usage(USAGE_JSON)
    # 1000 + 400 = 1400 input, 250 + 100 = 350 output, 1750 total.
    assert "1400 input tokens" in out
    assert "350 output tokens" in out
    assert "1750 total" in out
    assert "gpt-4o" in out
    assert "gpt-4o-mini" in out


def test_summarise_usage_empty_says_zero():
    out = summarise_usage({"data": [], "next_page": None})
    assert "input tokens: 0" in out
    assert "output tokens: 0" in out


# --------------------------------------------------------------------------
# _fetch — the I/O boundary, graceful on every failure mode.
# --------------------------------------------------------------------------
async def test_fetch_happy_path_returns_body(monkeypatch):
    def handler(request):
        # The date range is rendered into start_time / end_time params.
        assert request.url.params["start_time"] == str(_date_to_unix("2026-06-01"))
        assert request.url.params["end_time"] == str(_date_to_unix("2026-06-15"))
        assert request.headers["Authorization"] == "Bearer sk-admin-test"
        return httpx.Response(200, json=COSTS_JSON)

    _mock_async_client(monkeypatch, handler)
    body, error = await _fetch(
        "https://api.openai.com/v1/organization/costs",
        admin_key="sk-admin-test",
        start_date="2026-06-01",
        end_date="2026-06-15",
    )
    assert error is None
    assert body == COSTS_JSON


async def test_fetch_scopes_to_project_when_project_id_set(monkeypatch):
    def handler(request):
        # project_ids is sent as the API's array filter (repeated param).
        assert request.url.params.get_list("project_ids") == ["proj_sandbox"]
        return httpx.Response(200, json=COSTS_JSON)

    _mock_async_client(monkeypatch, handler)
    body, error = await _fetch(
        "https://api.openai.com/v1/organization/costs",
        admin_key="sk-admin-test",
        start_date="2026-06-01",
        end_date="2026-06-15",
        project_id="proj_sandbox",
    )
    assert error is None


async def test_fetch_omits_project_filter_when_unset(monkeypatch):
    def handler(request):
        # No project scope → org-wide; the param must be absent entirely.
        assert "project_ids" not in request.url.params
        return httpx.Response(200, json=COSTS_JSON)

    _mock_async_client(monkeypatch, handler)
    body, error = await _fetch(
        "https://api.openai.com/v1/organization/costs",
        admin_key="sk-admin-test",
        start_date="2026-06-01",
        end_date="2026-06-15",
    )
    assert error is None


async def test_fetch_non_200_returns_graceful_error_text(monkeypatch):
    def handler(request):
        return httpx.Response(401, text='{"error": "invalid admin key"}')

    _mock_async_client(monkeypatch, handler)
    body, error = await _fetch(
        "https://api.openai.com/v1/organization/costs",
        admin_key="sk-admin-bad",
        start_date="2026-06-01",
        end_date="2026-06-15",
    )
    assert body is None
    assert "HTTP 401" in error
    assert "invalid admin key" in error


async def test_fetch_empty_admin_key_returns_finding_message():
    body, error = await _fetch(
        "https://api.openai.com/v1/organization/costs",
        admin_key="",
        start_date="2026-06-01",
        end_date="2026-06-15",
    )
    assert body is None
    assert "admin key" in error.lower()
    assert "file one finding" in error.lower()


async def test_fetch_bad_date_does_not_raise():
    body, error = await _fetch(
        "https://api.openai.com/v1/organization/costs",
        admin_key="sk-admin-test",
        start_date="not-a-date",
        end_date="2026-06-15",
    )
    assert body is None
    assert "YYYY-MM-DD" in error


async def test_fetch_transport_error_does_not_raise(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    _mock_async_client(monkeypatch, handler)
    body, error = await _fetch(
        "https://api.openai.com/v1/organization/costs",
        admin_key="sk-admin-test",
        start_date="2026-06-01",
        end_date="2026-06-15",
    )
    assert body is None
    assert "failed" in error.lower()


# --------------------------------------------------------------------------
# build_openai_billing_server — public contract.
# --------------------------------------------------------------------------
def test_build_server_returns_expected_name_and_tool_names():
    server, names = build_openai_billing_server(admin_key="sk-admin-test")
    assert names == ["openai_costs", "openai_usage"]
    assert server is not None
    assert server["name"] == "openai_billing"


def test_build_server_with_empty_admin_key_still_builds():
    # An empty key must not crash the factory — the tools degrade at call
    # time with a "no admin key configured" message.
    server, names = build_openai_billing_server(admin_key="")
    assert server["name"] == "openai_billing"
    assert names == ["openai_costs", "openai_usage"]
