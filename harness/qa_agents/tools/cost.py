"""Cost / usage MCP server — lets the internal QA persona (the load
economist) read SlyReply's own cost and fair-use data via its admin API.

Two tools, in the same in-process shape as the ``email`` / ``identity`` /
``openapi`` servers — each owns its tool contract, fetches with ``httpx``,
and is unit-testable with no subprocess:

  - ``cost_report(period)`` — GET ``/api/admin/costs`` for one window and
    pretty-print the totals, per-provider / per-model breakdowns, the top
    by-agent rows, and the internal-vs-external reconciliation block.
  - ``usage_summary()``     — GET ``/api/usage/summary`` and print the
    per-user fair-use standing + per-agent breakdown.

Both calls send ``Authorization: Bearer {admin_token}``. The token may be
empty (e.g. in tests) — the server still constructs fine; the call just
fails at request time and the tool returns a graceful error text so the
persona files it as ONE observation rather than crashing the run. No tool
ever raises out into the agent loop.
"""

from __future__ import annotations

from typing import Any

import httpx

# A window longer than any single admin page should ever need, but short
# enough that a hung/unreachable backend fails the tool rather than the run.
_TIMEOUT_SECONDS = 30.0

_VALID_PERIODS = ("day", "week", "month", "all")


# ---------------------------------------------------------------------------
# Pure formatting helpers — no I/O, unit-tested directly.
# ---------------------------------------------------------------------------
def _fmt_usd(value: Any) -> str:
    """Render a dollar amount as ``$1.2345`` (4dp), tolerant of None/junk."""
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return "$?"


def _fmt_num(value: Any) -> str:
    """Render an integer-ish count, tolerant of None/junk."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value if value is not None else "?")


def format_cost_report(period: str, data: dict) -> str:
    """Pretty-print an ``/api/admin/costs`` response as readable text.

    Tolerant of a sparse / partial payload: a missing section is rendered
    as a short "(none)" line rather than raising, so a backend that only
    fills some fields still produces something the persona can act on.
    """
    if not isinstance(data, dict):
        return f"Cost report ({period}): unexpected response shape: {data!r}"

    lines: list[str] = [f"Cost report — period: {period}", ""]

    lines.append("Totals:")
    lines.append(f"  total cost:       {_fmt_usd(data.get('total_cost_usd'))}")
    lines.append(f"  AI cost:          {_fmt_usd(data.get('ai_cost_usd'))}")
    lines.append(
        f"  delivery cost:    {_fmt_usd(data.get('delivery_cost_usd'))}"
    )
    lines.append(f"  total replies:    {_fmt_num(data.get('total_replies'))}")
    lines.append(
        f"  avg cost/reply:   {_fmt_usd(data.get('avg_cost_per_reply'))}"
    )

    # /api/admin/costs nests models UNDER each provider section: the
    # top-level field is ``providers`` (CostProviderSection), each with a
    # nested ``models`` list (CostModelBreakdown: model/replies/cost_usd/
    # input_tokens/output_tokens/image_count). There is NO top-level
    # ``by_provider``/``by_model`` — reading those rendered "(none)" for
    # every breakdown regardless of data (the bug Nadia's iter3 surfaced).
    providers = data.get("providers") or []
    lines.append("")
    lines.append("By provider / model:")
    if isinstance(providers, list) and providers:
        for p in providers:
            if not isinstance(p, dict):
                continue
            bal = p.get("balance_usd")
            bal_txt = f", balance {_fmt_usd(bal)}" if bal is not None else ""
            lines.append(
                f"  {p.get('provider', '?')}: "
                f"cost {_fmt_usd(p.get('cost_usd'))}, "
                f"MTD {_fmt_usd(p.get('mtd_usd'))}{bal_txt}"
            )
            for m in (p.get("models") or []):
                if not isinstance(m, dict):
                    continue
                lines.append(
                    f"    - {m.get('model', '?')}: "
                    f"replies {_fmt_num(m.get('replies'))}, "
                    f"cost {_fmt_usd(m.get('cost_usd'))}, "
                    f"in {_fmt_num(m.get('input_tokens'))} tok, "
                    f"out {_fmt_num(m.get('output_tokens'))} tok, "
                    f"images {_fmt_num(m.get('image_count'))}"
                )
    else:
        lines.append("  (none)")

    agents = data.get("by_agent") or []
    lines.append("")
    lines.append("Top by agent:")
    if isinstance(agents, list) and agents:
        for row in agents:
            if not isinstance(row, dict):
                continue
            label = row.get("uid") or row.get("uid_id") or "(unattributed)"
            lines.append(
                f"  {label}: "
                f"replies {_fmt_num(row.get('replies'))}, "
                f"cost {_fmt_usd(row.get('cost_usd'))}"
            )
    else:
        lines.append("  (none)")

    users = data.get("by_user") or []
    if isinstance(users, list) and users:
        lines.append("")
        lines.append("Top by user:")
        for row in users:
            if not isinstance(row, dict):
                continue
            label = row.get("name") or row.get("email") or row.get("user_id") or "?"
            lines.append(
                f"  {label}: "
                f"replies {_fmt_num(row.get('replies'))}, "
                f"cost {_fmt_usd(row.get('cost_usd'))}"
            )

    recon = data.get("reconciliation")
    lines.append("")
    lines.append("Reconciliation:")
    if isinstance(recon, dict):
        if recon.get("available"):
            lines.append(
                f"  available: yes — "
                f"internal {_fmt_usd(recon.get('internal_usd'))}, "
                f"external {_fmt_usd(recon.get('external_usd'))}, "
                f"drift {recon.get('drift_pct')}%"
            )
        else:
            lines.append("  available: no (no external feed to reconcile against)")
    else:
        lines.append("  (none)")

    return "\n".join(lines)


def format_usage_summary(data: dict) -> str:
    """Pretty-print an ``/api/usage/summary`` response as readable text."""
    if not isinstance(data, dict):
        return f"Usage summary: unexpected response shape: {data!r}"

    lines: list[str] = ["Usage summary", ""]

    # /api/usage/summary shape: top-level reply_count + period, a nested
    # ``standing`` (FairUseStanding: status/reply_count/reply_limit/
    # image_count/image_limit/cooldown_until/period_reset_at), and
    # ``by_agent`` rows of {agent_name, reply_count, is_catalog}. (The old
    # code read flat tier/replies_used/replies_limit + by_agent.agent —
    # none of which exist, so it printed an empty summary.)
    standing = data.get("standing") or {}
    lines.append("Fair-use standing:")
    if isinstance(standing, dict) and standing:
        lines.append(f"  status:            {standing.get('status')}")
        lines.append(
            f"  replies:           {_fmt_num(standing.get('reply_count'))}"
            f" / {_fmt_num(standing.get('reply_limit'))}"
        )
        lines.append(
            f"  images:            {_fmt_num(standing.get('image_count'))}"
            f" / {_fmt_num(standing.get('image_limit'))}"
        )
        if standing.get("cooldown_until"):
            lines.append(f"  cooldown until:    {standing.get('cooldown_until')}")
        if standing.get("period_reset_at"):
            lines.append(f"  period resets:     {standing.get('period_reset_at')}")
    else:
        lines.append("  (none)")
    if data.get("period"):
        lines.append(f"  period:            {data.get('period')}")

    agents = data.get("by_agent") or []
    lines.append("")
    lines.append("By agent:")
    if isinstance(agents, list) and agents:
        for row in agents:
            if not isinstance(row, dict):
                continue
            label = row.get("agent_name") or "?"
            catalog = " (catalog)" if row.get("is_catalog") else ""
            lines.append(
                f"  {label}: replies {_fmt_num(row.get('reply_count'))}{catalog}"
            )
    else:
        lines.append("  (none)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP client — testable without the SDK, mirrors openapi._fetch_spec.
# ---------------------------------------------------------------------------
class CostClient:
    """Thin async client over SlyReply's admin cost/usage endpoints.

    Never raises out of its public methods: on any transport error or
    non-200 status it returns a persona-facing error string instead of the
    parsed JSON, so the tool layer can hand it straight to the agent.
    """

    def __init__(
        self,
        web_base_url: str,
        admin_token: str,
        admin_email: str = "",
        admin_password: str = "",
    ) -> None:
        self._base = (web_base_url or "").rstrip("/")
        self._token = admin_token or ""
        # Self-login fallback: when no static token is supplied, the client
        # mints its own JWT from the admin credentials the harness already
        # holds (QA_ADMIN_EMAIL / QA_ADMIN_PASSWORD, defaulting to the
        # sandbox admin). This is more robust than a static expiring token
        # and avoids depending on the admin UI dashboard, which can flake
        # under the load this persona drives.
        self._email = admin_email or ""
        self._password = admin_password or ""
        self._login_attempted = False

    async def _ensure_token(self) -> None:
        """Mint a bearer token via /api/auth/login if we don't have one.

        One-shot and non-fatal: a missing-creds or failed login leaves the
        token empty, the request goes out unauthenticated, and the 401/403
        surfaces through the normal graceful-error path. We never raise.
        """
        if self._token or self._login_attempted:
            return
        self._login_attempted = True
        if not (self._email and self._password):
            return
        url = f"{self._base}/api/auth/login"
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                resp = await client.post(
                    url, json={"email": self._email, "password": self._password}
                )
            if resp.status_code == 200:
                self._token = (resp.json() or {}).get("access_token", "") or ""
        except Exception:  # noqa: BLE001 - login failure is non-fatal
            pass

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def _get(self, path: str, params: dict | None = None) -> Any:
        """GET ``path``; return parsed JSON, or an error string on failure."""
        await self._ensure_token()
        url = f"{self._base}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                resp = await client.get(
                    url, params=params, headers=self._headers()
                )
        except Exception as exc:  # noqa: BLE001 - never crash the agent loop
            return (
                f"Could not reach {url}: {type(exc).__name__}: {exc}. "
                "File ONE observation noting the cost endpoint was "
                "unreachable."
            )
        if resp.status_code != 200:
            body = resp.text
            if len(body) > 2000:
                body = body[:2000] + "… (truncated)"
            return (
                f"GET {url} returned HTTP {resp.status_code}. Body: {body}. "
                "File ONE observation with the status + body."
            )
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            return (
                f"GET {url} returned HTTP 200 but the body was not JSON "
                f"({type(exc).__name__}). First 500 chars: {resp.text[:500]}"
            )

    async def costs(self, period: str) -> Any:
        return await self._get(
            "/api/admin/costs",
            params={
                "period": period,
                "workspace": "slyreply",
                "traffic_class": "all",
            },
        )

    async def usage_summary(self) -> Any:
        return await self._get("/api/usage/summary")


def _text_result(text: str) -> dict:
    """Wrap a string in the MCP tool ``content`` envelope."""
    return {"content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# SDK tool factory — mirrors build_openapi_server / build_identity_server.
# ---------------------------------------------------------------------------
def build_cost_server(
    *,
    web_base_url: str,
    admin_token: str,
    admin_email: str = "",
    admin_password: str = "",
):
    """Build the in-process MCP server exposing the two cost/usage tools.

    Returns ``(server, tool_names)`` matching the other ``build_*_server``
    helpers; the runner qualifies the names as ``mcp__cost__<name>``.

    Auth precedence: a non-empty ``admin_token`` is used as-is; otherwise,
    if ``admin_email`` + ``admin_password`` are supplied, the client logs in
    once to mint its own JWT (see ``CostClient._ensure_token``). With
    neither, the server still constructs and the HTTP call surfaces a
    graceful 401 the persona files as one observation.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    client = CostClient(
        web_base_url=web_base_url,
        admin_token=admin_token,
        admin_email=admin_email,
        admin_password=admin_password,
    )

    @tool(
        "cost_report",
        "Read SlyReply's own AI + delivery cost report for a time window "
        "from the admin API. period is one of day|week|month|all. Returns "
        "totals (total/ai/delivery cost, reply count, avg cost per reply), "
        "per-provider and per-model breakdowns, the top agents by spend, "
        "and the internal-vs-external reconciliation drift. Use this to "
        "check whether load you generate moves the cost numbers as expected.",
        {"period": str},
    )
    async def cost_report_tool(args: dict) -> dict:
        period = (args.get("period") or "").strip().lower()
        if period not in _VALID_PERIODS:
            return _text_result(
                f"Invalid period {period!r}. Must be one of: "
                f"{', '.join(_VALID_PERIODS)}."
            )
        result = await client.costs(period)
        if isinstance(result, str):
            # An error string from the client — hand it straight to the agent.
            return _text_result(result)
        return _text_result(format_cost_report(period, result))

    @tool(
        "usage_summary",
        "Read the per-user fair-use standing + per-agent reply breakdown "
        "from SlyReply's usage API. Shows the subscription tier, replies "
        "used vs the fair-use limit for the current period, any active "
        "cooldown, and a per-agent reply count. Use this to see how close "
        "the account is to its fair-use backstop.",
        {},
    )
    async def usage_summary_tool(_args: dict) -> dict:
        result = await client.usage_summary()
        if isinstance(result, str):
            return _text_result(result)
        return _text_result(format_usage_summary(result))

    server = create_sdk_mcp_server(
        name="cost",
        tools=[cost_report_tool, usage_summary_tool],
    )
    return server, ["cost_report", "usage_summary"]
