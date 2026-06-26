"""OpenAI organization billing MCP server — in-process.

In-process MCP server exposing two TOOLS the internal QA cost persona
(Nadia, the load-economist) uses to cross-check SlyReply's *internal*
per-call cost estimate against OpenAI's *external* organization bill:

  - ``openai_costs`` — the org Costs API (dollar amounts, grouped by
                       line item / bucket) for a date range.
  - ``openai_usage`` — the org completions Usage API (input/output
                       tokens, broken down by model) for a date range.

Same shape as the ``email`` / ``findings`` / ``identity`` / ``openapi``
servers: it owns its own tool contract, fetches with ``httpx``, and is
unit-testable with no subprocess. The persona prompt names these exact
two tools, qualified by the runner as ``mcp__openai_billing__*``.

Auth + scope notes
------------------
OpenAI's organization usage/costs endpoints require an **admin** key
(``sk-admin-...``), not a regular project key. The key is supplied to
the factory by the runner; an empty key still builds the server fine —
the tools then return a clear "no admin key configured" message so the
persona files ONE finding rather than the run crashing.

Both endpoints are paginated via a ``next_page`` cursor in the response.
This server fetches a single page; if ``next_page`` is present it appends
a truncation note to the output so the persona knows the totals are
partial. A non-200 / network error never raises out of a tool — the
status + body are returned as a text content block.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

_COSTS_URL = "https://api.openai.com/v1/organization/costs"
_USAGE_URL = "https://api.openai.com/v1/organization/usage/completions"
_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Pure helpers — no I/O, unit-tested directly.
# ---------------------------------------------------------------------------
def _date_to_unix(date_str: str) -> int:
    """Convert a ``YYYY-MM-DD`` date to a unix start-of-day UTC timestamp.

    OpenAI's org APIs take ``start_time`` / ``end_time`` as unix-second
    integers. We anchor each date to 00:00:00 UTC so the range is
    deterministic regardless of the host's local timezone.
    """
    dt = datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    return int(dt.timestamp())


def _text_result(payload: Any) -> dict:
    """Wrap a payload in the MCP tool ``content`` envelope."""
    text = (
        payload
        if isinstance(payload, str)
        else json.dumps(payload, indent=2, default=str)
    )
    return {"content": [{"type": "text", "text": text}]}


def _truncation_note(body: dict) -> str:
    """A trailing note when the response carries a ``next_page`` cursor."""
    if isinstance(body, dict) and body.get("next_page"):
        return (
            "\n\nNOTE: results were TRUNCATED — OpenAI returned a "
            "`next_page` cursor that this tool did not follow, so the "
            "totals above cover only the first page of buckets."
        )
    return ""


def summarise_costs(body: dict) -> str:
    """Render the Costs API response as readable text.

    The Costs API returns ``data`` = a list of time buckets, each with a
    ``results`` list of ``{amount: {value, currency}, line_item, ...}``
    rows. We total the dollar value and group it by line item / bucket.
    """
    if not isinstance(body, dict):
        return "OpenAI costs response was not a JSON object."
    buckets = body.get("data") or []
    total = 0.0
    currency = "usd"
    by_line_item: dict[str, float] = {}
    rows = 0
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        for row in bucket.get("results") or []:
            if not isinstance(row, dict):
                continue
            rows += 1
            amount = row.get("amount") or {}
            value = float(amount.get("value") or 0.0)
            currency = amount.get("currency") or currency
            total += value
            label = row.get("line_item") or "(unspecified)"
            by_line_item[label] = by_line_item.get(label, 0.0) + value

    if rows == 0:
        out = (
            "OpenAI Costs API returned no cost rows for this date range "
            "(total cost: 0.00)."
        )
        return out + _truncation_note(body)

    lines = [
        f"OpenAI organization cost: {total:.4f} {currency.upper()} "
        f"across {rows} line-item row(s).",
        "By line item:",
    ]
    for label, value in sorted(
        by_line_item.items(), key=lambda kv: kv[1], reverse=True
    ):
        lines.append(f"  - {label}: {value:.4f} {currency.upper()}")
    return "\n".join(lines) + _truncation_note(body)


def summarise_usage(body: dict) -> str:
    """Render the completions Usage API response as readable text.

    The Usage API returns ``data`` = a list of time buckets, each with a
    ``results`` list of rows carrying ``input_tokens`` /
    ``output_tokens`` and an optional ``model``. We total tokens and
    break them down by model.
    """
    if not isinstance(body, dict):
        return "OpenAI usage response was not a JSON object."
    buckets = body.get("data") or []
    total_in = 0
    total_out = 0
    by_model: dict[str, dict[str, int]] = {}
    rows = 0
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        for row in bucket.get("results") or []:
            if not isinstance(row, dict):
                continue
            rows += 1
            in_tok = int(row.get("input_tokens") or 0)
            out_tok = int(row.get("output_tokens") or 0)
            total_in += in_tok
            total_out += out_tok
            model = row.get("model") or "(unspecified)"
            m = by_model.setdefault(model, {"input": 0, "output": 0})
            m["input"] += in_tok
            m["output"] += out_tok

    if rows == 0:
        out = (
            "OpenAI Usage API returned no usage rows for this date range "
            "(input tokens: 0, output tokens: 0)."
        )
        return out + _truncation_note(body)

    lines = [
        f"OpenAI organization token usage: {total_in} input tokens, "
        f"{total_out} output tokens "
        f"({total_in + total_out} total) across {rows} row(s).",
        "By model:",
    ]
    for model, toks in sorted(
        by_model.items(),
        key=lambda kv: kv[1]["input"] + kv[1]["output"],
        reverse=True,
    ):
        lines.append(
            f"  - {model}: {toks['input']} in / {toks['output']} out"
        )
    return "\n".join(lines) + _truncation_note(body)


# ---------------------------------------------------------------------------
# HTTP fetch — graceful, never raises out of a tool.
# ---------------------------------------------------------------------------
async def _fetch(
    url: str,
    *,
    admin_key: str,
    start_date: str,
    end_date: str,
    project_id: str = "",
) -> tuple[dict | None, str | None]:
    """GET ``url`` for the date range. Returns ``(body, error_text)``.

    Exactly one of the two is non-None: ``body`` is the parsed JSON on a
    200, ``error_text`` is a persona-facing message on any non-200 or
    transport/parse failure. Never raises.

    ``project_id`` (when set) scopes the read to a single OpenAI project via
    the API's ``project_ids`` filter — so a run against the sandbox project
    sees ONLY sandbox spend, not the whole organization's. Empty = org-wide.
    """
    if not admin_key:
        return None, (
            "No OpenAI admin key configured for this run, so the "
            "organization billing API can't be queried. An admin key "
            "(sk-admin-...) with the api.usage.read scope is required. "
            "File ONE finding noting the operator didn't supply an admin "
            "key, then cross-check using only SlyReply's internal estimate."
        )
    try:
        start_time = _date_to_unix(start_date)
        end_time = _date_to_unix(end_date)
    except Exception as exc:  # noqa: BLE001 - bad date never crashes the loop
        return None, (
            f"Could not parse the date range "
            f"({start_date!r} .. {end_date!r}); expected YYYY-MM-DD: "
            f"{type(exc).__name__}: {exc}"
        )

    params: dict = {"start_time": start_time, "end_time": end_time}
    if project_id:
        # The OpenAI Costs/Usage API takes project_ids as an array; httpx
        # serialises a list value as repeated query params, which the API
        # accepts. Scopes the read to one project (e.g. the sandbox).
        params["project_ids"] = [project_id]
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {admin_key}"},
            )
    except Exception as exc:  # noqa: BLE001 - transport error → text, not raise
        return None, (
            f"Request to {url} failed: {type(exc).__name__}: {exc}"
        )

    if resp.status_code != 200:
        return None, (
            f"OpenAI billing API returned HTTP {resp.status_code} for "
            f"{url}:\n{resp.text}"
        )
    try:
        return resp.json(), None
    except Exception as exc:  # noqa: BLE001 - non-JSON 200 → text, not raise
        return None, (
            f"OpenAI billing API returned a non-JSON 200 from {url}: "
            f"{type(exc).__name__}: {exc}\n{resp.text}"
        )


# ---------------------------------------------------------------------------
# SDK tool factory — mirrors build_openapi_server / build_identity_server.
# ---------------------------------------------------------------------------
def build_openai_billing_server(*, admin_key: str, project_id: str = ""):
    """Build the in-process MCP server exposing the two billing tools.

    ``admin_key`` is OpenAI's organization admin key (``sk-admin-...``),
    supplied by the runner. An empty string still builds the server fine
    — the tools then return a clear "no admin key configured" message.

    ``project_id`` (when set) scopes every read to that single OpenAI
    project. The sandbox has its own project, so passing its id means the
    cross-check sees ONLY sandbox spend — both safer (no prod figures in the
    report even though the key is org-wide) and more useful (isolates this
    run instead of org-wide totals). Empty = org-wide.

    Returns ``(server, tool_names)`` matching the other ``build_*_server``
    helpers; the runner qualifies the names as
    ``mcp__openai_billing__<name>``.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "openai_costs",
        "Fetch OpenAI's ORGANIZATION cost (dollar amounts) for a date "
        "range from the org Costs API, grouped by line item. Use this to "
        "cross-check SlyReply's internal cost estimate against the real "
        "external OpenAI bill. Dates are YYYY-MM-DD (UTC, end is "
        "exclusive). Requires an admin key.",
        {"start_date": str, "end_date": str},
    )
    async def openai_costs_tool(args: dict) -> dict:
        body, error = await _fetch(
            _COSTS_URL,
            admin_key=admin_key,
            start_date=args.get("start_date", ""),
            end_date=args.get("end_date", ""),
            project_id=project_id,
        )
        if error is not None:
            return _text_result(error)
        return _text_result(summarise_costs(body))

    @tool(
        "openai_usage",
        "Fetch OpenAI's ORGANIZATION token usage for a date range from "
        "the org completions Usage API: input/output tokens with a "
        "per-model breakdown. Use this to cross-check SlyReply's internal "
        "token accounting against OpenAI's. Dates are YYYY-MM-DD (UTC, "
        "end is exclusive). Requires an admin key.",
        {"start_date": str, "end_date": str},
    )
    async def openai_usage_tool(args: dict) -> dict:
        body, error = await _fetch(
            _USAGE_URL,
            admin_key=admin_key,
            start_date=args.get("start_date", ""),
            end_date=args.get("end_date", ""),
            project_id=project_id,
        )
        if error is not None:
            return _text_result(error)
        return _text_result(summarise_usage(body))

    server = create_sdk_mcp_server(
        name="openai_billing",
        tools=[openai_costs_tool, openai_usage_tool],
    )
    return server, ["openai_costs", "openai_usage"]
