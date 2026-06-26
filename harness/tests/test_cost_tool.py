"""Unit tests for the in-process cost/usage MCP server.

No real HTTP: the pure formatters are tested directly, and the ``CostClient``
(the HTTP layer the tools delegate to) is exercised against a mocked
``httpx`` transport so no network is hit.
"""

from __future__ import annotations

import httpx

from qa_agents.tools.cost import (
    CostClient,
    build_cost_server,
    format_cost_report,
    format_usage_summary,
)

# A representative /api/admin/costs payload — the REAL CostResponse shape:
# models are NESTED under each ``providers[]`` section (not a top-level
# ``by_model``), and there is no top-level ``by_provider``.
SAMPLE_COSTS = {
    "total_cost_usd": 12.3456,
    "ai_cost_usd": 11.0,
    "delivery_cost_usd": 1.3456,
    "total_replies": 4200,
    "avg_cost_per_reply": 0.00294,
    "providers": [
        {
            "provider": "anthropic",
            "cost_usd": 9.0,
            "mtd_usd": 150.5,
            "balance_usd": None,
            "models": [
                {
                    "model": "claude-opus-4-8",
                    "replies": 3000,
                    "cost_usd": 8.5,
                    "input_tokens": 1234567,
                    "output_tokens": 89012,
                    "image_count": 0,
                },
            ],
        },
        {"provider": "openai", "cost_usd": 2.0, "mtd_usd": 40.0, "models": []},
    ],
    "by_agent": [
        {"uid": "customerservice", "uid_id": "a1", "replies": 2500, "cost_usd": 7.5},
        {"uid": "support", "uid_id": "a2", "replies": 1700, "cost_usd": 4.8},
    ],
    "by_user": [
        {"name": "Acme Ltd", "email": "ops@acme.test", "replies": 4200, "cost_usd": 12.3},
    ],
    "reconciliation": {
        "available": True,
        "internal_usd": 12.3456,
        "external_usd": 12.40,
        "drift_pct": 0.44,
    },
}

# Real /api/usage/summary shape: nested ``standing`` + by_agent[agent_name].
SAMPLE_USAGE = {
    "reply_count": 312,
    "period": "2026-06",
    "standing": {
        "status": "steady",
        "reply_count": 312,
        "reply_limit": 10000,
        "image_count": 4,
        "image_limit": 150,
        "cooldown_until": None,
        "period_reset_at": "2026-07-01T00:00:00Z",
    },
    "by_agent": [
        {"agent_name": "customerservice", "reply_count": 200, "is_catalog": False},
        {"agent_name": "support", "reply_count": 112, "is_catalog": True},
    ],
}


# ---------------------------------------------------------------------------
# Pure formatters.
# ---------------------------------------------------------------------------
def test_format_cost_report_includes_totals_model_row_and_drift():
    out = format_cost_report("day", SAMPLE_COSTS)
    # Window + totals.
    assert "period: day" in out
    assert "$12.3456" in out  # total cost
    assert "4,200" in out  # total replies, thousands-grouped
    # Per-provider breakdown (top-level ``providers``).
    assert "anthropic" in out
    assert "MTD $150.5000" in out
    # A NESTED model row with token counts + image count.
    assert "claude-opus-4-8" in out
    assert "1,234,567 tok" in out
    assert "images 0" in out
    # Top by-agent (uid label) + by-user.
    assert "customerservice" in out
    assert "Acme Ltd" in out
    # Reconciliation drift.
    assert "drift 0.44%" in out
    assert "internal $12.3456" in out


def test_format_cost_report_regression_reads_nested_models_not_flat():
    # Guard against the iter3 bug: a non-empty providers/models payload must
    # NOT render "(none)" for the provider/model breakdown. (The old code
    # read by_provider/by_model, which don't exist, so it always showed
    # "(none)" despite real data.)
    out = format_cost_report("day", SAMPLE_COSTS)
    assert "By provider / model:" in out
    # The provider+model section must contain the model, not the (none) stub.
    section = out.split("By provider / model:")[1].split("Top by agent:")[0]
    assert "claude-opus-4-8" in section
    assert "(none)" not in section


def test_format_cost_report_tolerates_missing_sections():
    # A backend that only fills the totals must not crash the formatter —
    # missing breakdowns render as "(none)" rather than raising.
    out = format_cost_report("week", {"total_cost_usd": 1.0})
    assert "period: week" in out
    assert "$1.0000" in out
    assert "(none)" in out
    # Unknown reconciliation degrades gracefully too.
    assert "Reconciliation:" in out


def test_format_cost_report_reconciliation_unavailable():
    data = dict(SAMPLE_COSTS)
    data["reconciliation"] = {"available": False}
    out = format_cost_report("month", data)
    assert "available: no" in out


def test_format_usage_summary_includes_standing_and_agent_rows():
    out = format_usage_summary(SAMPLE_USAGE)
    # Nested standing fields (status word + replies/images vs limits).
    assert "status:" in out
    assert "steady" in out
    assert "312" in out
    assert "10,000" in out  # reply_limit, thousands-grouped
    # by_agent uses agent_name + reply_count; catalog flag surfaced.
    assert "customerservice: replies 200" in out
    assert "(catalog)" in out
    assert "(none)" not in out.split("By agent:")[1]


# ---------------------------------------------------------------------------
# CostClient against a mocked transport — no network.
# ---------------------------------------------------------------------------
def _mock_async_client(monkeypatch, handler):
    """Patch httpx.AsyncClient so CostClient hits an in-memory handler."""
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        transport = httpx.MockTransport(handler)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_cost_client_costs_sends_auth_and_query_params(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=SAMPLE_COSTS)

    _mock_async_client(monkeypatch, handler)
    client = CostClient("http://web:8000/", "secret-token")
    result = await client.costs("day")

    assert result == SAMPLE_COSTS
    assert seen["path"] == "/api/admin/costs"
    assert seen["params"] == {
        "period": "day",
        "workspace": "slyreply",
        "traffic_class": "all",
    }
    assert seen["auth"] == "Bearer secret-token"


async def test_cost_client_usage_summary_hits_right_path(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/usage/summary"
        return httpx.Response(200, json=SAMPLE_USAGE)

    _mock_async_client(monkeypatch, handler)
    client = CostClient("http://web:8000", "tok")
    assert await client.usage_summary() == SAMPLE_USAGE


async def test_cost_client_non_200_returns_graceful_error_string(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden: admin only")

    _mock_async_client(monkeypatch, handler)
    client = CostClient("http://web:8000", "")
    result = await client.costs("month")
    # A string (not a dict) signals an error the tool hands to the persona.
    assert isinstance(result, str)
    assert "403" in result
    assert "forbidden: admin only" in result
    assert "observation" in result.lower()


async def test_cost_client_transport_error_returns_graceful_string(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _mock_async_client(monkeypatch, handler)
    client = CostClient("http://web:8000", "tok")
    result = await client.usage_summary()
    assert isinstance(result, str)
    assert "Could not reach" in result
    assert "ConnectError" in result


async def test_cost_client_200_non_json_returns_graceful_string(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    _mock_async_client(monkeypatch, handler)
    client = CostClient("http://web:8000", "tok")
    result = await client.costs("all")
    assert isinstance(result, str)
    assert "not JSON" in result


# ---------------------------------------------------------------------------
# Self-login: mint a JWT from admin creds when no static token is given.
# ---------------------------------------------------------------------------
async def test_cost_client_self_logs_in_when_no_static_token(monkeypatch):
    seen = {"login": 0, "auth_on_costs": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            seen["login"] += 1
            return httpx.Response(200, json={"access_token": "minted-jwt", "token_type": "bearer"})
        seen["auth_on_costs"] = request.headers.get("Authorization")
        return httpx.Response(200, json=SAMPLE_COSTS)

    _mock_async_client(monkeypatch, handler)
    client = CostClient("http://web:8000", "", admin_email="admin@x", admin_password="pw")
    result = await client.costs("day")

    assert result == SAMPLE_COSTS
    assert seen["login"] == 1
    assert seen["auth_on_costs"] == "Bearer minted-jwt"


async def test_cost_client_static_token_skips_login(monkeypatch):
    seen = {"login": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            seen["login"] += 1
            return httpx.Response(200, json={"access_token": "should-not-be-used"})
        assert request.headers.get("Authorization") == "Bearer static"
        return httpx.Response(200, json=SAMPLE_USAGE)

    _mock_async_client(monkeypatch, handler)
    client = CostClient("http://web:8000", "static", admin_email="admin@x", admin_password="pw")
    await client.usage_summary()
    assert seen["login"] == 0  # static token wins; never logs in


async def test_cost_client_login_only_attempted_once(monkeypatch):
    seen = {"login": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            seen["login"] += 1
            return httpx.Response(500, text="boom")  # login fails
        # No token minted → request goes out unauthenticated → 401.
        assert request.headers.get("Authorization") is None
        return httpx.Response(401, text="unauthorized")

    _mock_async_client(monkeypatch, handler)
    client = CostClient("http://web:8000", "", admin_email="admin@x", admin_password="pw")
    r1 = await client.costs("day")
    r2 = await client.usage_summary()
    # Login failure is non-fatal and one-shot; both calls degrade gracefully.
    assert isinstance(r1, str) and "401" in r1
    assert isinstance(r2, str) and "401" in r2
    assert seen["login"] == 1  # not retried on the second call


async def test_cost_client_no_creds_proceeds_unauthenticated(monkeypatch):
    seen = {"login": 0, "auth": "unset"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            seen["login"] += 1
            return httpx.Response(200, json={"access_token": "x"})
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(403, text="admin only")

    _mock_async_client(monkeypatch, handler)
    client = CostClient("http://web:8000", "")  # no token, no creds
    result = await client.costs("day")
    assert seen["login"] == 0  # no creds → no login attempt
    assert seen["auth"] is None  # no Authorization header sent
    assert isinstance(result, str) and "403" in result


# ---------------------------------------------------------------------------
# build_cost_server — public contract.
# ---------------------------------------------------------------------------
def test_build_cost_server_returns_named_server_and_tool_names():
    server, names = build_cost_server(
        web_base_url="http://web:8000", admin_token="tok"
    )
    assert names == ["cost_report", "usage_summary"]
    assert server is not None
    # create_sdk_mcp_server returns a dict carrying the server name.
    assert server["name"] == "cost"


def test_build_cost_server_constructs_fine_with_empty_token():
    # admin_token may be empty in tests / unconfigured runs — the server
    # must still build (the call just fails at request time).
    server, names = build_cost_server(web_base_url="http://web", admin_token="")
    assert server["name"] == "cost"
    assert names == ["cost_report", "usage_summary"]
