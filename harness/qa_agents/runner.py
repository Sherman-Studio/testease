"""The persona run loop — two Claude Agent SDK ``query()`` phases.

Phase 1 (explore): a cheaper model drives a real browser (Playwright MCP) plus
the in-process email + findings tools, fully in persona, walking the persona's
flow list. Findings land in a shared ``Findings`` collector as it goes.

Phase 2 (report): a stronger model (Opus), NO tools, BEING the persona, writes
an honest first-person review from the collected findings + a transcript
digest.

Both phases' ``ResultMessage`` usage is summed into one ``RunAccounting``.

Live, per-turn logging is emitted as the SDK streams messages back — see
``_emit_*`` helpers — so an operator running ``kubectl logs -f`` on the harness
Job sees what the agent is doing in real time (#664). Without this the harness
runs silently for ~30 min per persona between banners.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    UserMessage,
    query,
)

from .accounting import RunAccounting
from .config import Config
from .personas import Persona, render_explore_prompt
from .report import RunResult, new_run_id
from .run_recorder import RunRecorder
from .setup_actions import run_setup
from .tools.cost import build_cost_server
from .tools.email import MailpitClient, build_email_server
from .tools.findings import Findings, build_findings_server
from .tools.identity import build_identity_server
from .tools.loadgen import build_loadgen_server
from .tools.openai_billing import build_openai_billing_server
from .tools.openapi import build_openapi_server

# Configure a stdout handler so live log lines flush immediately to the Job
# logs. We use a bare ``%(message)s`` format because the lines carry their own
# ``[persona t=N]`` bracket prefix — adding a timestamp/level on top would be
# visual noise in ``kubectl logs -f``. ``force=True`` so an importing test
# harness that pre-installed its own root config gets overridden cleanly.
logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
        force=True,
    )

# httpx logs one INFO line per request. wait_for_email polls Mailpit every
# 2s, so at INFO that single tool floods the Job log — ~46% of all lines in
# one real run — and buries the persona narration. Lift the HTTP-client
# loggers to WARNING so `kubectl logs -f` shows what the persona is doing.
for _noisy_logger in ("httpx", "httpcore"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)


@dataclass
class ExploreOutcome:
    """Result of the explore phase.

    ``digest`` is the bounded transcript digest (a multi-line ``str``) that
    feeds the report phase. ``truncated_reason`` is ``None`` on a clean run;
    when set it carries a short human string explaining why the explore phase
    ended early (the Claude Agent SDK raised — most commonly because the run
    hit ``max_turns`` — or any other unexpected exception). In either case the
    report phase still runs against whatever was collected so a review is
    written and persisted instead of crashing the orchestrator (#652).
    """

    digest: str
    truncated_reason: str | None = None

    @property
    def truncated(self) -> bool:
        return self.truncated_reason is not None


def _is_max_turns_error(exc: BaseException) -> bool:
    """Identify the Claude Agent SDK's max-turns truncation.

    The SDK raises a plain ``Exception`` whose message reads
    ``"Claude Code returned an error result: Reached maximum number of turns
    (N)"`` — there is no dedicated exception class, so we substring-match on
    ``"maximum number of turns"`` case-insensitively. Loose by design: any
    future wording the SDK uses around the same phrase will still match.
    """
    return "maximum number of turns" in str(exc).lower()


def _is_success_completion(exc: BaseException) -> bool:
    """Identify the SDK's contradictory ``error result: success`` quirk (#668).

    Priya and Tomas both failed on the first real harness run with
    ``Exception('Claude Code returned an error result: success')`` — a
    contradictory message where the "error" field is literally the string
    ``"success"``. This appears to be an SDK quirk where the CLI subprocess
    exits cleanly (``subtype: success``) but the wrapping layer still raises.
    The accumulated transcript + findings are usable, so we treat it the same
    way as the max-turns truncation: don't propagate, feed what we have into
    the report phase. Match the specific phrase so this never absorbs a real
    error that happens to contain the word "success" somewhere.
    """
    return "error result: success" in str(exc).lower()

# How many transcript lines from the explore phase to carry into the report
# phase, and how long each line may be — keeps the report prompt bounded.
_DIGEST_MAX_LINES = 80
_DIGEST_MAX_LINE_CHARS = 400

# Live-log truncation widths — tuned for one-line readability in `kubectl logs`.
# #916 — `_TEXT_LINE_MAX_CHARS` was removed; text emits now log
# in full (previously 200 chars cut narration mid-sentence and made
# the log strictly less useful than no truncation). Tool-arg
# summaries stay truncated because they DO benefit from scannability.
_TOOL_ARG_TEXT_MAX_CHARS = 60
_TOOL_ARG_TITLE_MAX_CHARS = 80
# Per-typed text truncation for the "Typed 'X' into Y" prose form
# (#1078). Slightly more generous than `_TOOL_ARG_TEXT_MAX_CHARS`
# because the prose row carries the WHOLE value, not a key=value pair.
_TOOL_ARG_TYPED_MAX_CHARS = 80

# Sentinel returned by ``_format_tool_args`` for tool calls whose
# timeline row carries zero operator value (snapshots, console-message
# polls, payload-less unknowns). The run recorder treats the sentinel
# as "advance the step ordinal but suppress the qa_run_logs mirror" —
# see ``run_recorder._should_skip_log_row``. The literal string keeps
# the function's signature ``str`` (no Optional, no Union) so callers
# downstream of the live ``_emit_tool_use`` log path don't need to
# special-case None.
_SKIP_LOG_ROW = "__SKIP__"

# How long to wait between SDK messages before emitting a "still working"
# heartbeat. The SDK can sit silent for a minute or two while the model
# composes a long reply; the heartbeat reassures the operator the loop is
# alive (and not, e.g., stuck on a hung MCP call).
_HEARTBEAT_INTERVAL_S = 60.0


def _resolve_openapi_spec_url(web_base_url: str | None = None) -> str | None:
    """Resolve the OpenAPI spec URL for this run, or ``None`` if there is
    none.

    The in-process OpenAPI MCP server (``qa_agents.tools.openapi``) fetches
    the tenant's spec from this URL to expose list_endpoints / get_endpoint
    / search to the api-poker persona.

    Precedence (#1354):
      1. ``QA_OPENAPI_URL`` env (operator-provided per-run override — use
         this for non-FastAPI tenants that publish the spec at a non-default
         path, or for a YAML spec).
      2. ``{web_base_url}/openapi.json`` derived fallback. FastAPI publishes
         the OpenAPI spec there by default, so an api-poker run against any
         FastAPI tenant works without the operator setting anything extra.
         The trailing slash on web_base_url is stripped before appending.
      3. Neither available → ``None``. The MCP server still exposes the
         tools, but each one returns a "no spec configured" message so the
         persona files ONE finding rather than fishing blindly (per its
         prompt).
    """
    spec_url = os.environ.get("QA_OPENAPI_URL", "").strip()
    if not spec_url and web_base_url:
        spec_url = f"{web_base_url.rstrip('/')}/openapi.json"
    return spec_url or None


def _chrome_devtools_mcp_config() -> dict:
    """The Chrome DevTools MCP server config (stdio) — #1024.

    Wraps Google's ``chrome-devtools-mcp`` which speaks the Chrome
    DevTools Protocol. Same image-baked pattern as Playwright:
    ``chrome-devtools-mcp`` is installed globally in the Dockerfile;
    no runtime npx fetch.

    The server launches its own Chrome instance — separate from the
    Playwright MCP's Chromium — so it can independently set CPU /
    network throttling without disturbing the persona's browser session.
    Reuses the same ``/usr/local/bin/qa-chromium`` symlink Playwright
    is pointed at so we only ship one browser binary in the image.

    ``QA_CHROME_DEVTOOLS_MCP_CMD`` and ``QA_CHROME_DEVTOOLS_EXECUTABLE``
    override the command and the browser path for a local (non-image)
    run.
    """
    args: list[str] = []
    executable = os.environ.get(
        "QA_CHROME_DEVTOOLS_EXECUTABLE", "/usr/local/bin/qa-chromium"
    )
    if executable:
        args += ["--executable-path", executable]
    # Headless is the right default for the harness pod. The persona
    # never needs to *see* the perf-tool Chrome; the trace artifacts
    # come back through the MCP tool results.
    args += ["--headless"]
    return {
        "type": "stdio",
        "command": os.environ.get(
            "QA_CHROME_DEVTOOLS_MCP_CMD", "chrome-devtools-mcp"
        ),
        "args": args,
    }


def _a11y_mcp_config() -> dict:
    """The a11y / axe-core MCP server config (stdio) — #1021.

    Wraps the community ``a11y-mcp`` Node package, which exposes
    axe-core audits against a target URL. The MCP package spawns its
    own short-lived headless browser per audit; it does NOT share the
    persona's Playwright session, so calling it does not disturb the
    persona's in-flight click / scroll state.

    ``QA_A11Y_MCP_CMD`` overrides the command for a local (non-image)
    run — set it to ``npx -y a11y-mcp`` if you've not pre-installed it.
    """
    return {
        "type": "stdio",
        "command": os.environ.get("QA_A11Y_MCP_CMD", "a11y-mcp"),
        "args": [],
    }


def _playwright_mcp_config(browser_locale: str | None = None) -> dict:
    """The Playwright MCP server config (stdio).

    Uses the ``@playwright/mcp`` server installed globally in the image (see
    ``harness/Dockerfile``) — no npx download at runtime. The server is pointed
    at the image's bundled Chromium with ``--executable-path``: its
    ``--browser`` flag does not accept ``"chromium"`` (only the Chrome channel,
    which has no arm64 build). ``--headless`` / ``--no-sandbox`` / ``--isolated``
    are required for a containerised, non-root, read-only-rootfs run.

    ``QA_PLAYWRIGHT_MCP_CMD`` and ``QA_BROWSER_EXECUTABLE`` override the command
    and the browser path for a local (non-image) run — set
    ``QA_BROWSER_EXECUTABLE`` empty to let the server fall back to its own
    browser discovery.

    #891 — when ``QA_TOKEN`` is set, write a tiny JSON config file with
    ``contextOptions.extraHTTPHeaders`` that injects ``X-QA-Token`` on
    every browser request. The backend's slowapi key-derivation
    (``app.limiter.client_ip``) routes matching requests to a dedicated
    ``qa:client`` bucket so concurrent personas don't collide on the
    cluster's shared egress IP. The config file lives under /tmp
    (writable) and is recreated on every run — no leakage between
    persona runs.

    #934 — when ``browser_locale`` is set (BCP-47 e.g. ``en-GB``), the
    same config file also pins Playwright's ``contextOptions.locale`` and
    sends the locale as the ``Accept-Language`` header. Without this,
    every persona run from the German VPS shows up as German to the
    checkout widget's country picker and the frontend's currency-detect
    code, regardless of the persona's intended nationality.
    """
    args = ["--headless", "--no-sandbox", "--isolated"]
    executable = os.environ.get(
        "QA_BROWSER_EXECUTABLE", "/usr/local/bin/qa-chromium"
    )
    if executable:
        args += ["--executable-path", executable]

    qa_token = os.environ.get("QA_TOKEN", "").strip()

    # The JSON config file is written whenever we have anything to put
    # in it — a QA token, a persona locale, or both. Without either,
    # the @playwright/mcp server uses its own defaults.
    context_options: dict = {}
    extra_headers: dict[str, str] = {}
    if qa_token:
        extra_headers["X-QA-Token"] = qa_token
    if browser_locale:
        context_options["locale"] = browser_locale
        # Match the locale on the wire too — some country-detect paths
        # (the checkout widget) read Accept-Language directly.
        extra_headers["Accept-Language"] = browser_locale
    if extra_headers:
        context_options["extraHTTPHeaders"] = extra_headers

    if context_options:
        # @playwright/mcp doesn't expose a CLI flag for any of these —
        # they go via a JSON config file. /tmp is writable in the
        # harness pod (it's an emptyDir).
        config_path = os.environ.get(
            "QA_PLAYWRIGHT_MCP_CONFIG_PATH",
            "/tmp/playwright-mcp-qa.json",
        )
        with open(config_path, "w") as fh:
            json.dump(
                {"browser": {"contextOptions": context_options}},
                fh,
            )
        args += ["--config", config_path]

    return {
        "type": "stdio",
        "command": os.environ.get("QA_PLAYWRIGHT_MCP_CMD", "playwright-mcp"),
        "args": args,
    }


def _qualified(server: str, names: list[str]) -> list[str]:
    """Qualify in-process SDK tool names as ``mcp__<server>__<name>``."""
    return [f"mcp__{server}__{name}" for name in names]


# ---------------------------------------------------------------------------
# #1031 — Slice C of the MCP visibility epic. Per-run server gating.
# ---------------------------------------------------------------------------
def _resolve_enabled_mcp_servers(
    operator_choice: tuple[str, ...] | list[str],
    *,
    persona_id: str | None = None,
) -> frozenset[str]:
    """Resolve which MCP server ids should be wired for this run.

    ``operator_choice`` is the parsed ``QA_ENABLED_MCPS`` value (an empty
    tuple ⇒ no selection passed). When empty, every catalog entry with
    ``default_enabled=True`` is included. When non-empty, the result is
    the intersection with the catalog — unknown ids are dropped with a
    log warning rather than crashing the run.

    ``persona_id`` (when given) auto-unions every catalog entry that
    lists this persona in ``persona_compat`` into the result, regardless
    of operator_choice. The rationale (#1354): MCPs in ``persona_compat``
    are *required* tools for that persona — the api-poker persona literally
    cannot probe an API without the OpenAPI MCP — so a forgetful operator
    triggering an api-poker run shouldn't have to remember to also tick
    "OpenAPI Schema Explorer" in the MCP picker. An operator who *does*
    want to test the missing-MCP failure mode can run a different persona.

    Returns a frozenset so the downstream lookups (in ``_allowed_tools_for``
    and ``_mcp_servers_for``) are O(1) per server without re-parsing on
    every check.
    """
    from .mcp_catalog import CATALOG  # noqa: PLC0415 — defer to keep import lean

    catalog_ids = {s.id for s in CATALOG}
    if not operator_choice:
        # No explicit selection — fall back to catalog defaults.
        chosen = {s.id for s in CATALOG if s.default_enabled}
    else:
        chosen = set()
        unknown = []
        for sid in operator_choice:
            if sid in catalog_ids:
                chosen.add(sid)
            else:
                unknown.append(sid)
        if unknown:
            logger.warning(
                "QA_ENABLED_MCPS includes unknown server id(s) %r — dropping. "
                "Valid ids: %r",
                unknown, sorted(catalog_ids),
            )

    # #1354 — union persona-required MCPs in unconditionally so
    # operator-chosen lists and catalog defaults both gain the tools the
    # persona literally cannot work without. Servers list ``persona_compat``
    # in mcp_catalog.py.
    if persona_id is not None:
        compat = {s.id for s in CATALOG if persona_id in s.persona_compat}
        if compat - chosen:
            logger.info(
                "auto-enabling persona-required MCP server(s) %r for "
                "persona %r (not in operator choice / not default-enabled)",
                sorted(compat - chosen), persona_id,
            )
        chosen |= compat

    return frozenset(chosen)


def _allowed_tools_for(
    enabled: frozenset[str],
    email_tools: list[str],
    findings_tools: list[str],
    identity_tools: list[str],
    openapi_tools: list[str] | None = None,
    loadgen_tools: list[str] | None = None,
    cost_tools: list[str] | None = None,
    openai_billing_tools: list[str] | None = None,
) -> list[str]:
    """Filter the allowed-tools list by the enabled-MCP set.

    The Playwright + Chrome DevTools tool lists are module-level
    constants because the SDK loads those tools from the external
    subprocess regardless of any per-call factory; we gate them the
    same way as the in-process servers — if the operator disabled it,
    its tool names drop out of allowed_tools so the model never sees
    them.
    """
    out: list[str] = []
    if "playwright" in enabled:
        out.extend(_PLAYWRIGHT_TOOLS)
    if "email" in enabled:
        out.extend(_qualified("email", email_tools))
    if "findings" in enabled:
        out.extend(_qualified("findings", findings_tools))
    if "identity" in enabled:
        out.extend(_qualified("identity", identity_tools))
    if "chrome_devtools" in enabled:
        out.extend(_CHROME_DEVTOOLS_TOOLS)
    if "a11y" in enabled:
        out.extend(_A11Y_TOOLS)
    if "openapi" in enabled:
        out.extend(_qualified("openapi", openapi_tools or []))
    if "loadgen" in enabled:
        out.extend(_qualified("loadgen", loadgen_tools or []))
    if "cost" in enabled:
        out.extend(_qualified("cost", cost_tools or []))
    if "openai_billing" in enabled:
        out.extend(_qualified("openai_billing", openai_billing_tools or []))
    return out


def _mcp_servers_for(
    enabled: frozenset[str],
    *,
    persona_browser_locale: str | None,
    email_server: object,
    findings_server: object,
    identity_server: object,
    openapi_server: object | None = None,
    loadgen_server: object | None = None,
    cost_server: object | None = None,
    openai_billing_server: object | None = None,
) -> dict:
    """Filter the ``ClaudeAgentOptions.mcp_servers`` dict by the
    enabled-MCP set.

    A disabled server's entry is simply omitted; the SDK won't try to
    spawn its subprocess or register its tools. Combined with the
    matching ``_allowed_tools_for`` filter, the model has no way to
    discover or call a disabled server.
    """
    out: dict = {}
    if "playwright" in enabled:
        out["playwright"] = _playwright_mcp_config(persona_browser_locale)
    if "email" in enabled:
        out["email"] = email_server
    if "findings" in enabled:
        out["findings"] = findings_server
    if "identity" in enabled:
        out["identity"] = identity_server
    if "chrome_devtools" in enabled:
        out["chrome_devtools"] = _chrome_devtools_mcp_config()
    if "a11y" in enabled:
        out["a11y"] = _a11y_mcp_config()
    if "openapi" in enabled and openapi_server is not None:
        out["openapi"] = openapi_server
    if "loadgen" in enabled and loadgen_server is not None:
        out["loadgen"] = loadgen_server
    if "cost" in enabled and cost_server is not None:
        out["cost"] = cost_server
    if "openai_billing" in enabled and openai_billing_server is not None:
        out["openai_billing"] = openai_billing_server
    return out


def _options_env(config: Config) -> dict[str, str]:
    """Build the ``ClaudeAgentOptions.env`` overlay for one query() call.

    The harness ALWAYS scrubs ``ANTHROPIC_API_KEY`` to the empty string
    so the spawned ``claude`` CLI always uses OAuth/Max auth — every
    persona run is billed against the operator's Claude Code Max
    subscription, never the org's Anthropic API budget.

    The Agent SDK's subprocess transport starts from the parent
    process's ``os.environ`` and overlays ``options.env`` on top (see
    claude_agent_sdk subprocess_cli.py:430). This means we MUST return an
    explicit ``""`` for the key, NOT an empty dict (#894): an empty dict
    would let any inherited ``ANTHROPIC_API_KEY`` leak through to the
    spawned ``claude`` CLI, which would then prefer API-key auth over
    OAuth and silently bill the run to the org API. Overriding with ""
    forces the CLI down its OAuth / Keychain / CLAUDE_CODE_OAUTH_TOKEN
    fallback path, which is what bills the run to Max. The scrub is
    belt-and-braces against:
      - Pod env (cluster Max Job, #894): the pod spec already declines
        to envFrom the API-key Secret, but an accidental envFrom in a
        future kustomize/Terraform change can't re-enable API mode
        through the harness.
      - Shell env (laptop, #882): a Python process spawned with
        ANTHROPIC_API_KEY exported still gets the scrub.
    """
    # Empty-string override (NOT a no-op): see docstring.
    return {"ANTHROPIC_API_KEY": ""}


def _assistant_text(message: AssistantMessage) -> str:
    """Concatenate the text blocks of an AssistantMessage."""
    chunks: list[str] = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def _digest(lines: list[str]) -> str:
    """Trim the explore-phase narration into a bounded transcript digest."""
    trimmed = [
        (line[:_DIGEST_MAX_LINE_CHARS] + "…")
        if len(line) > _DIGEST_MAX_LINE_CHARS
        else line
        for line in lines
        if line.strip()
    ]
    if len(trimmed) > _DIGEST_MAX_LINES:
        head = trimmed[: _DIGEST_MAX_LINES // 2]
        tail = trimmed[-_DIGEST_MAX_LINES // 2 :]
        trimmed = head + ["… (middle of the session omitted) …"] + tail
    return "\n".join(trimmed)


# ---------------------------------------------------------------------------
# Live per-turn logging (#664).
#
# These helpers format a single-line log entry per SDK content block / result.
# They are pure (no I/O, no side-effects) apart from the final ``logger.info``,
# so tests can use ``caplog`` to assert format and ordering.
# ---------------------------------------------------------------------------
_WS_RE = re.compile(r"\s+")


def _shorten_text(text: str, max_chars: int) -> str:
    """Collapse whitespace and truncate to ``max_chars`` with an ellipsis."""
    flat = _WS_RE.sub(" ", text).strip()
    if len(flat) > max_chars:
        return flat[: max_chars - 1].rstrip() + "…"
    return flat


def _fmt_tokens(n: int | float) -> str:
    """Format a token count with a k-suffix for readability above 1,000."""
    n = int(n or 0)
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _short_tool_name(name: str) -> str:
    """Strip the ``mcp__<server>__`` prefix from a tool name for display."""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return f"{parts[1]}.{parts[2]}"
    return name


def _format_tool_args(name: str, args: dict) -> str:
    """Render a tool call's args as a short, prose-style timeline row (#1078).

    Where today's harness produced terse ``key=val`` pairs (or empty
    strings, which the recorder then fell back to printing the bare
    tool name for), Slice 1 of run-detail v2 rewrites these as
    English-y phrases — "Clicked submit-btn", "Typed 'hi' into name" —
    so the run-detail timeline reads like a story rather than an MCP
    tool log.

    Two cases stay raw:

    * ``mcp__playwright__browser_navigate`` keeps ``url=…`` because
      Slice 2 parses that token to build the URL spine. Don't change
      the surface contract there until #1078 Slice 2 ships.
    * ``mcp__findings__note_finding`` keeps its ``cat/sev title``
      shape because the findings panel already surfaces it well.

    For known operator-noise tools (``browser_snapshot``,
    ``browser_console_messages``, payload-less unknowns, ``static=``
    -style internal kwargs) this returns the module-level sentinel
    :data:`_SKIP_LOG_ROW`. The recorder treats the sentinel as
    "advance the step but suppress the timeline mirror"; the live
    ``_emit_tool_use`` log path drops it on the floor too (renders as
    if rendered=="").
    """
    args = args or {}

    def _short(value: object, n: int = _TOOL_ARG_TEXT_MAX_CHARS) -> str:
        return _shorten_text(str(value), n)

    # Playwright MCP browser tools.
    if name == "mcp__playwright__browser_navigate":
        # KEEP raw url=… — Slice 2 parses this for the URL spine.
        return f"url={_short(args.get('url'), 120)}"
    if name == "mcp__playwright__browser_navigate_back":
        return "Went back"
    if name == "mcp__playwright__browser_snapshot":
        # No payload — no operator value. Skip the row entirely.
        return _SKIP_LOG_ROW
    if name == "mcp__playwright__browser_click":
        target = (
            args.get("ref")
            or args.get("text")
            or args.get("name")
            or args.get("element")
            or ""
        )
        target = _short(target)
        return f"Clicked {target}".rstrip() if target else "Clicked"
    if name == "mcp__playwright__browser_type":
        ref = args.get("ref") or args.get("element") or ""
        text = _short(args.get("text", ""), _TOOL_ARG_TYPED_MAX_CHARS)
        if ref:
            return f"Typed '{text}' into {ref}"
        return f"Typed '{text}'"
    if name == "mcp__playwright__browser_fill_form":
        fields = args.get("fields") or []
        n = len(fields) if isinstance(fields, list) else 0
        if n == 1:
            return "Filled 1 field"
        return f"Filled {n} fields"
    if name == "mcp__playwright__browser_select_option":
        ref = args.get("ref") or ""
        values = args.get("values") or args.get("value") or ""
        # ``values`` is often a list — render it readably.
        if isinstance(values, list):
            values = ", ".join(str(v) for v in values)
        values = _short(values)
        if ref:
            return f"Selected '{values}' in {ref}"
        return f"Selected '{values}'"
    if name == "mcp__playwright__browser_press_key":
        key = args.get("key", "")
        return f"Pressed {key}".rstrip() if key else "Pressed key"
    if name == "mcp__playwright__browser_hover":
        ref = args.get("ref", "")
        return f"Hovered {ref}".rstrip() if ref else "Hovered"
    if name == "mcp__playwright__browser_wait_for":
        text = args.get("text") or args.get("textGone") or ""
        if text:
            return f"Waited for '{_short(text)}' to appear"
        seconds = args.get("time", "")
        if seconds not in ("", None):
            return f"Waited {seconds}s"
        return "Waited"
    if name == "mcp__playwright__browser_take_screenshot":
        # Slice 3 wires the inline image. Keep the row but with prose.
        return "Captured screenshot"
    if name == "mcp__playwright__browser_console_messages":
        # If the console returned actual messages they're surfaced as
        # separate text-block content elsewhere; the bare tool call is
        # noise on the timeline.
        return _SKIP_LOG_ROW
    if name == "mcp__playwright__browser_resize":
        width = args.get("width", "")
        height = args.get("height", "")
        return f"Resized viewport to {width}×{height}"
    if name in (
        "mcp__playwright__browser_evaluate",
        "mcp__playwright__browser_run_code_unsafe",
    ):
        # Drop the raw JS dump from the row; Slice 3 adds a "Show code"
        # expander that reads the real input off the qa_run_steps doc.
        return "Ran custom JavaScript"
    if name == "mcp__playwright__browser_tabs":
        return f"Tabs: {args.get('action', '')}".rstrip()

    # Findings tool — category/severity + the title is the most useful summary.
    if name == "mcp__findings__note_finding":
        cat = args.get("category", "")
        sev = args.get("severity", "")
        title = _shorten_text(str(args.get("title", "")), _TOOL_ARG_TITLE_MAX_CHARS)
        return f"{cat}/{sev} {title}".strip()

    # Email tools — prose form.
    if name == "mcp__email__send_email":
        to = args.get("to", "")
        subject = _short(args.get("subject", ""))
        return f"Emailed {to}: '{subject}'"
    if name == "mcp__email__wait_for_email":
        to = args.get("to_address", "") or args.get("to", "")
        timeout_s = args.get("timeout_s", "")
        return f"Waited for email to {to} ({timeout_s}s timeout)"
    if name == "mcp__email__get_email":
        return f"Read email {args.get('id', '')}"

    # Unknown tool — fall through with care.
    #
    # Skip "internal" or low-signal kwargs the agent SDK sometimes
    # passes (``static=False`` flags, bare booleans, etc.). Otherwise
    # keep the existing first-arg fallback so a new tool isn't
    # silently invisible.
    if not args:
        return _SKIP_LOG_ROW
    k, v = next(iter(args.items()))
    if k == "static" or isinstance(v, bool):
        return _SKIP_LOG_ROW
    return f"{k}={_short(v)}"


def _join_pairs(*pairs: tuple[str, object]) -> str:
    """Render ``(key, value)`` pairs as ``"k=v k=v"``, skipping empty values."""
    return " ".join(f"{k}={v}" for k, v in pairs if v not in ("", None))


def _emit_text(tag: str, turn: int | None, text: str) -> None:
    """Log a TextBlock as ``[<tag> t=<N>] » <text>``.

    #916 — emits the FULL text, no truncation. Previously capped at
    ``_TEXT_LINE_MAX_CHARS`` (200 chars), which cut agent narration
    mid-sentence in ``kubectl logs`` and left operators guessing what
    the agent was actually saying. Tool-arg summaries on
    ``_emit_tool_use`` stay truncated (call args benefit from being
    scannable; reasoning text does not).
    """
    if not text or not text.strip():
        return
    prefix = _line_prefix(tag, turn)
    logger.info(f"{prefix} » {text}")


def _emit_tool_use(tag: str, turn: int | None, name: str, args: dict) -> None:
    """Log a ToolUseBlock as ``[<tag> t=<N>] → tool(short args)``.

    A rendered value equal to :data:`_SKIP_LOG_ROW` means the tool's
    timeline row is being suppressed (see ``_format_tool_args``); we
    still log the tool name to the kubectl stream so operators
    following live can see the bare call, but we drop the sentinel
    from the displayed args body.
    """
    pretty = _short_tool_name(name)
    rendered = _format_tool_args(name, args)
    if rendered == _SKIP_LOG_ROW:
        rendered = ""
    body = f"{pretty}({rendered})" if rendered else f"{pretty}()"
    prefix = _line_prefix(tag, turn)
    logger.info(f"{prefix} → {body}")


def _emit_result(
    tag: str,
    message: ResultMessage,
    accounting: RunAccounting,
) -> None:
    """Log a ResultMessage with per-phase + running totals."""
    usage = getattr(message, "usage", None) or {}

    def _get(k: str) -> int:
        if isinstance(usage, dict):
            return int(usage.get(k, 0) or 0)
        return int(getattr(usage, k, 0) or 0)

    in_t = _get("input_tokens")
    out_t = _get("output_tokens")
    cache = _get("cache_creation_input_tokens") + _get("cache_read_input_tokens")
    # #1822 — no dollar figure any more (runs bill the operator's flat-rate
    # Claude Code Max plan); the running total is the run's token count.
    logger.info(
        f"[{tag}] turn done  "
        f"in={_fmt_tokens(in_t)} out={_fmt_tokens(out_t)} "
        f"cache={_fmt_tokens(cache)}  "
        f"run={_fmt_tokens(accounting.total_tokens)} tokens"
    )


def _line_prefix(tag: str, turn: int | None) -> str:
    if turn is None:
        return f"[{tag}]"
    return f"[{tag} t={turn}]"


async def _stream_with_heartbeat(
    agen,
    tag: str,
    interval_s: float | None = None,
):
    """Wrap an async generator and emit a heartbeat between slow yields.

    When the model is composing a long reply, the SDK can sit silent for tens
    of seconds. ``kubectl logs -f`` then looks frozen. This wrapper races the
    next yield against ``asyncio.sleep(interval_s)``: every timeout it logs a
    heartbeat and keeps waiting; on yield it forwards the message. Cleanly
    propagates StopAsyncIteration and any other exception the wrapped
    generator raises (e.g. the SDK's max-turns ``Exception``).

    ``interval_s`` defaults to the module-level ``_HEARTBEAT_INTERVAL_S`` —
    read at call time, not at function-definition time, so tests can
    ``monkeypatch.setattr`` to a smaller value.
    """
    if interval_s is None:
        interval_s = _HEARTBEAT_INTERVAL_S
    last_turn_holder = {"turn": 0}
    aiter = agen.__aiter__()
    while True:
        next_task = asyncio.ensure_future(aiter.__anext__())
        while True:
            try:
                message = await asyncio.wait_for(
                    asyncio.shield(next_task), timeout=interval_s
                )
            except TimeoutError:
                logger.info(
                    f"[{tag}] still working "
                    f"(turn {last_turn_holder['turn']}, awaiting model)"
                )
                continue
            except StopAsyncIteration:
                return
            break
        yield message, last_turn_holder


# OpenAPI MCP tools are exposed by the in-process server in
# qa_agents.tools.openapi (build_openapi_server returns the bare names);
# the runner qualifies them via _qualified("openapi", ...) in
# _allowed_tools_for, the same way email / findings / identity tools are
# handled. No module-level constant needed.

# Allowed Chrome DevTools MCP tools — the subset perf-focused personas
# need. #1024 — Vita (slow-connection) needs emulate_network; Pia
# (perf-budget-evaluator) needs the trace + analysis tools.
_CHROME_DEVTOOLS_TOOLS = [
    "mcp__chrome_devtools__emulate_cpu",
    "mcp__chrome_devtools__emulate_network",
    "mcp__chrome_devtools__performance_start_trace",
    "mcp__chrome_devtools__performance_analyze_insight",
    "mcp__chrome_devtools__get_network_request",
]

# Allowed a11y MCP tools — the axe-core audit interface. Conservative
# allow-list: just the two tools we know personas need today (audit a
# URL, audit raw HTML). If the third-party server adds more useful
# tools, extend this list in a follow-up. #1021.
_A11Y_TOOLS = [
    "mcp__a11y__audit",
    "mcp__a11y__audit_html",
]

# Allowed Playwright MCP browser tools — the subset the persona needs.
_PLAYWRIGHT_TOOLS = [
    "mcp__playwright__browser_navigate",
    "mcp__playwright__browser_navigate_back",
    "mcp__playwright__browser_snapshot",
    "mcp__playwright__browser_click",
    "mcp__playwright__browser_type",
    "mcp__playwright__browser_fill_form",
    "mcp__playwright__browser_select_option",
    "mcp__playwright__browser_press_key",
    "mcp__playwright__browser_hover",
    "mcp__playwright__browser_wait_for",
    "mcp__playwright__browser_take_screenshot",
    "mcp__playwright__browser_console_messages",
    "mcp__playwright__browser_tabs",
    # Security-recon tools — riley's explore prompt directs her to inspect
    # response headers, cookies, storage and network traffic. Without these
    # in the allowlist she burns ToolSearch calls hunting for tools the
    # harness never granted.
    "mcp__playwright__browser_network_requests",
    "mcp__playwright__browser_evaluate",
]

# Built-in SDK tools the persona must NOT have. A persona is a website *user*,
# not an operator of the test rig: it has no reason to run a shell, read the
# harness source, edit files, or browse the wider web. ``disallowed_tools``
# removes them from the model's context entirely — a hard block, independent
# of ``permission_mode`` (whereas ``allowed_tools`` only governs prompting and
# is a no-op under ``bypassPermissions``). The first real run showed a persona
# burn ~60 turns using Bash to hand-patch the Playwright MCP server's
# node_modules and kill its process when the browser would not launch;
# removing these tools makes that whole failure mode impossible. ``ToolSearch``
# is deliberately KEPT — the MCP tool schemas are deferred and the agent needs
# it to load them.
_DISALLOWED_BUILTIN_TOOLS = [
    "Bash",
    "BashOutput",
    "KillShell",
    "Edit",
    "Write",
    "NotebookEdit",
    "Read",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
]


async def _run_setup_for_persona(
    action: str,
    *,
    persona: Persona,
    config: Config,
) -> None:
    """Drive the scripted-setup prelude (issue #837) end-to-end.

    Opens a Playwright async Page (using the ``playwright`` Python package),
    builds a fresh :class:`MailpitClient`, delegates to
    :func:`qa_agents.setup_actions.run_setup`, and tears the browser down.
    The wrapper exists at module scope so tests can
    ``monkeypatch.setattr(runner_mod, "_run_setup_for_persona", …)`` to
    bypass Playwright entirely.

    NOTE: the harness's primary browser is the Playwright MCP subprocess
    (Node), which the AI drives. The scripted prelude needs its own browser
    because the MCP doesn't expose a typed Python Page surface to the
    harness process. This is the minimal-refactor path flagged in #837 —
    the alternative is to teach the MCP wrapper to accept scripted
    sequences, which is a bigger change than the per-persona cost saving
    warrants for the v1 of this feature.

    If the ``playwright`` Python package isn't installed (e.g. a local
    checkout without the optional dep), this function logs a warning and
    returns cleanly — the AI explore loop then runs as it does today,
    without the cost saving but also without crashing.
    """
    try:
        from playwright.async_api import (  # type: ignore[import-not-found]
            async_playwright,
        )
    except ImportError:
        logger.warning(
            "[%s] setup_actions=%r but the playwright Python package is not "
            "installed; skipping scripted setup. Install with "
            "`uv pip install playwright && playwright install chromium`.",
            persona.id,
            action,
        )
        return

    # #1105 Slice 1.1 — credential-aware setup variants need a
    # qa-store handle to read/write the persona's credentials sub-
    # doc. Connect lazily so legacy ``signup_then_*`` paths don't
    # pay the connect cost when they don't need it. Failure here
    # is non-fatal — setup_actions falls back to plain signup when
    # ``store`` is None.
    store = None
    if action in (
        "signup",
        "signup_or_login",
        "signup_then_pro",
        "signup_then_power",
        "clear_credentials_then_signup",
    ):
        try:
            from qa_store import connect  # noqa: PLC0415
            store = connect(
                url=config.qa_store_url,
                db=config.qa_store_db,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "[%s] could not connect to qa-store for credential lookup; "
                "setup will run without lifecycle awareness",
                persona.id, exc_info=True,
            )
            store = None

    mailpit = MailpitClient(config.mailpit_url)
    playwright_ctx = await async_playwright().start()
    try:
        browser = await playwright_ctx.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await run_setup(
                action,
                page=page,
                mailpit=mailpit,
                persona=persona,
                web_base_url=config.web_base_url,
                store=store,
            )
        finally:
            await browser.close()
    finally:
        await playwright_ctx.stop()


def _load_persona_resume_url(persona: Persona, config: Config) -> str:
    """Build the magic-link resume URL for a persona, or "" if none.

    Gated on atlas-sink + run_id conditions — qa-store has to be
    reachable to read the saved token at all. Returns an empty string for every
    failure mode:

      - sink isn't atlas (local dev) → no qa-store wiring,
      - run_id missing (synthetic test runs) → ditto,
      - persona has no saved credentials yet (first run, never logged in),
      - persona has credentials but no token (slice 2 didn't successfully
        request one — backend was down, env var missing, etc),
      - token expired (the load helper does the clock arithmetic and
        collapses to None).

    The empty string flows through ``render_explore_prompt`` and
    becomes the "(none — sign up or log in as usual)" sentinel.
    """
    if config.sink != "atlas" or not config.run_id:
        return ""
    try:
        from qa_store import connect as _qa_store_connect  # noqa: PLC0415

        from . import credentials as _creds  # noqa: PLC0415

        store = _qa_store_connect(config.qa_store_url, config.qa_store_db)
        record = _creds.load_resume_token(store, persona.id)
        if record is None:
            return ""
        token = record.get("resume_token") or ""
        if not token:
            return ""
        return f"{config.web_base_url.rstrip('/')}/auth/restore?token={token}"
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[%s] could not load resume token: %s: %s — persona will use "
            "the email + password login path",
            persona.id, type(exc).__name__, exc,
        )
        return ""


async def run_explore_phase(
    persona: Persona,
    config: Config,
    findings: Findings,
    accounting: RunAccounting,
    *,
    by_design_block: str = "",
) -> ExploreOutcome:
    """Run the explore phase. Returns the transcript digest for the report.

    Any exception raised by the Claude Agent SDK (``query()``) is caught here
    and turned into a truncation marker on the returned ``ExploreOutcome`` —
    most commonly the SDK's max-turns guard, but also network blips and any
    other unexpected SDK error. The exception is NEVER re-raised: that would
    crash the orchestrator and lose the entire run's worth of data, which is
    exactly what bit us when Slice 5 first ran for real (#652). Findings that
    ``note_finding`` already recorded are preserved in the shared ``findings``
    collector and the report phase still runs against whatever digest exists,
    so the persona's review is honest about the truncation instead of vanishing.
    """
    # ``not_before`` fences wait_for_email to mail that arrives AFTER this
    # point, so a stale message left in the sink by an earlier run can never
    # satisfy a wait (belt-and-braces alongside the per-run Mailpit wipe in
    # the QA Job's wipe-and-seed init container).
    #
    # Per-persona isolation under concurrent runs (#824): each persona is
    # bound to its own distinct ``@example.com`` registered address (see
    # personas.py), and wait_for_email / get_email filter Mailpit messages
    # by recipient. Two personas running in parallel cannot see each other's
    # mail because their tools query disjoint ``to`` addresses, and SMTP
    # send is stateless (smtplib opens a fresh connection per send). No
    # shared client state inside the email tools either — MailpitClient is
    # constructed per-persona in build_email_server.
    # #1031 — Slice C of #1028. Resolve which MCP servers this run is
    # allowed to spin up. Empty tuple = "catalog defaults" (every
    # default_enabled=True entry). Non-empty = exact opt-in list,
    # validated against the catalog. The trigger UI passes the
    # operator's selection here via QA_ENABLED_MCPS; CronJob runs have
    # an empty value → catalog defaults stand.
    enabled_servers = _resolve_enabled_mcp_servers(
        config.enabled_mcp_servers,
        persona_id=persona.id,
    )
    email_server, email_tools = build_email_server(
        smtp_host=config.smtp_host,
        smtp_port=config.smtp_port,
        mailpit_url=config.mailpit_url,
        persona_from_address=persona.registered_email,
        not_before=datetime.now(UTC),
    )
    # #1115 follow-up — live-streaming finding writer. When sink=atlas
    # and the orchestrator has set a shared run_id, every note_finding
    # call also upserts to qa_findings in qa-store so the review-UI's
    # 4s auto-refresh sees findings appear within seconds instead of
    # waiting for the persona-end add_persona_result flush. Outside
    # those conditions (local file-sink dev runs, no shared run_id)
    # the live writer stays None and findings buffer in memory until
    # persona-end, matching the pre-this-slice behaviour.
    live_finding_writer = None
    if config.sink == "atlas" and config.run_id:
        try:
            from qa_store import connect as _qa_store_connect  # noqa: PLC0415
            from qa_store import upsert_live_finding  # noqa: PLC0415

            _live_store = _qa_store_connect(
                config.qa_store_url, config.qa_store_db,
            )

            def _write_live_finding(ordinal: int, finding_dict: dict) -> None:
                upsert_live_finding(
                    _live_store,
                    config.run_id,
                    persona.id,
                    ordinal,
                    finding_dict,
                )

            live_finding_writer = _write_live_finding
        except Exception as exc:  # noqa: BLE001 — never block the run
            logger.warning(
                "[%s] could not initialise live-finding writer: %s: %s — "
                "findings will only appear at persona-end (the pre-#1115-"
                "follow-up behaviour)",
                persona.id, type(exc).__name__, exc,
            )

    findings_server, findings_tools = build_findings_server(
        findings, live_writer=live_finding_writer,
    )
    # #1023 — identity server. Reuses the persona's registered-email
    # domain for generated addresses so wait_for_email still sees
    # verification mail.
    identity_email_domain = persona.registered_email.split("@", 1)[-1]
    identity_server, identity_tools = build_identity_server(
        persona_region=persona.region,
        persona_language=persona.language,
        email_domain=identity_email_domain,
    )
    # OpenAPI surface-discovery server (in-process rebuild of #1026). The
    # spec URL is resolved from QA_OPENAPI_URL or derived from web_base_url;
    # None means "no spec" and the tools return a file-a-finding message.
    # Built unconditionally like the others; only wired into the SDK options
    # below when "openapi" is in the enabled set (auto-unioned for api-poker
    # via persona_compat in _resolve_enabled_mcp_servers).
    openapi_server, openapi_tools = build_openapi_server(
        spec_url=_resolve_openapi_spec_url(config.web_base_url),
    )
    # Internal-group servers (Nadia / internal-load-economist). Built
    # unconditionally like openapi; only wired into the SDK options when
    # in the enabled set (auto-unioned for the internal persona via
    # persona_compat in _resolve_enabled_mcp_servers). loadgen sends from
    # the persona's registered (authenticated) address; cost + openai_billing
    # degrade gracefully when their credentials are empty.
    loadgen_server, loadgen_tools = build_loadgen_server(
        smtp_host=config.smtp_host,
        smtp_port=config.smtp_port,
        persona_from_address=persona.registered_email,
    )
    cost_server, cost_tools = build_cost_server(
        web_base_url=config.web_base_url,
        admin_token=config.admin_api_token,
        # When no static token is provisioned, the cost client self-logs-in
        # with the admin creds the harness already holds (sandbox admin by
        # default) — more robust than a static token or the admin UI, which
        # can flake under the load this persona drives.
        admin_email=config.admin_email,
        admin_password=config.admin_password,
    )
    openai_billing_server, openai_billing_tools = build_openai_billing_server(
        admin_key=config.openai_admin_key,
        # Scope the external read to the sandbox OpenAI project so the
        # cross-check isolates this run and the report never surfaces prod
        # spend, even though the admin key is org-wide.
        project_id=config.openai_project_id,
    )

    # #1257 slice 3 — resume URL. If the harness has a saved token
    # from a prior login/signup (slice 2), build the magic-link URL
    # the persona will navigate to as their first action. Gated on
    # the same atlas-sink + run_id + qa-store conditions as the
    # memory loader; any failure leaves resume_url="" and the
    # render_explore_prompt fallback shows the "(none — log in
    # normally)" sentinel.
    resume_url = _load_persona_resume_url(persona, config)

    options = ClaudeAgentOptions(
        model=config.explore_model,
        system_prompt=render_explore_prompt(
            persona,
            config.web_base_url,
            admin_email=config.admin_email,
            admin_password=config.admin_password,
            mandatory_action_ids=config.mandatory_action_ids,
            resume_url=resume_url,
            by_design_block=by_design_block,
        ),
        # #1031 — allowed_tools and mcp_servers are scoped to the
        # enabled-MCP set resolved above. A disabled server's tool
        # prefix is excluded from allowed_tools AND its config is
        # omitted from mcp_servers, so the model never even sees it.
        allowed_tools=_allowed_tools_for(
            enabled_servers,
            email_tools,
            findings_tools,
            identity_tools,
            openapi_tools,
            loadgen_tools=loadgen_tools,
            cost_tools=cost_tools,
            openai_billing_tools=openai_billing_tools,
        ),
        disallowed_tools=_DISALLOWED_BUILTIN_TOOLS,
        mcp_servers=_mcp_servers_for(
            enabled_servers,
            persona_browser_locale=persona.browser_locale,
            email_server=email_server,
            findings_server=findings_server,
            identity_server=identity_server,
            openapi_server=openapi_server,
            loadgen_server=loadgen_server,
            cost_server=cost_server,
            openai_billing_server=openai_billing_server,
        ),
        max_turns=config.max_turns,
        permission_mode="bypassPermissions",
        # The harness always scrubs ANTHROPIC_API_KEY to "" so the spawned
        # ``claude`` CLI falls through to its OAuth credentials (Keychain
        # on macOS / CLAUDE_CODE_OAUTH_TOKEN in-cluster), billing the run
        # against the operator's Claude Code Max plan rather than the
        # org's API budget. See _options_env for the #894 hardening.
        env=_options_env(config),
    )

    tag = persona.id

    # #860 — per-persona step recorder. Captures one qa_run_steps doc per
    # tool call AND uploads Playwright screenshots to GridFS so the
    # review UI's Transcript tab can show what the persona did + saw.
    # Best-effort: only runs when sink=atlas (file sink has no qa-store
    # connection) AND config.run_id is set (the orchestrator always sets
    # QA_RUN_ID in production; local file-sink dev runs have no UI to
    # view a transcript anyway). All recorder writes are wrapped in _safe
    # inside RunRecorder, so a flaky Mongo / GridFS connection can lose
    # the transcript without crashing the persona.
    recorder: RunRecorder | None = None
    if config.sink == "atlas" and config.run_id:
        try:
            from qa_store import connect as _qa_store_connect

            _qa_store = _qa_store_connect(config.qa_store_url, config.qa_store_db)
            recorder = RunRecorder(
                store=_qa_store,
                run_id=config.run_id,
                persona_id=persona.id,
                findings=findings,
                summarize_args=_format_tool_args,
            )
        except Exception as exc:  # noqa: BLE001 - never block the run on transcripts
            logger.warning(
                "[%s] could not initialise transcript recorder: %s: %s — "
                "the Transcript tab will be empty for this persona",
                persona.id,
                type(exc).__name__,
                exc,
            )

    # #837 — scripted setup prelude. For personas where signup (and sometimes
    # tier-upgrade) is NOT the test itself but a gate to the test, run a
    # deterministic Playwright + Mailpit flow up front. The AI then takes
    # over post-signup, saving ~22 turns / ~$0.66 per persona. ``None`` keeps
    # today's behaviour: the AI gets control at ``/`` and drives the form
    # itself. ``_run_setup_for_persona`` is module-level so tests can
    # monkeypatch it without touching Playwright.
    if persona.setup_actions is not None:
        logger.info(
            "[%s] running scripted setup (%s) before AI explore loop",
            persona.id,
            persona.setup_actions,
        )
        try:
            await _run_setup_for_persona(
                persona.setup_actions,
                persona=persona,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001
            # A failed scripted setup must NOT crash the orchestrator — the
            # persona's review still has value even if it had to land at
            # ``/`` without a session. Log it and let the AI take over.
            logger.warning(
                "[%s] scripted setup %r failed: %s: %s",
                persona.id,
                persona.setup_actions,
                type(exc).__name__,
                exc,
            )

    prompt = (
        f"Begin. Open a browser to {config.web_base_url} and start working "
        f"through your list as {persona.display_name} would. Go at her pace, "
        "narrate what you see and feel as you go, and call note_finding "
        "whenever you react."
    )

    narration: list[str] = []
    truncated_reason: str | None = None
    turn_counter = 0
    try:
        agen = query(prompt=prompt, options=options)
        async for message, turn_holder in _stream_with_heartbeat(agen, tag):
            if isinstance(message, AssistantMessage):
                turn_counter += 1
                turn_holder["turn"] = turn_counter
                # Log each content block on its own line so an operator can
                # see the model's narration and tool use interleaved in real
                # time. We still build the digest from text blocks only.
                text_parts: list[str] = []
                for block in getattr(message, "content", []) or []:
                    block_name = getattr(block, "name", None)
                    block_input = getattr(block, "input", None)
                    block_text = getattr(block, "text", None)
                    if block_name is not None and block_input is not None:
                        _emit_tool_use(tag, turn_counter, block_name, block_input)
                    elif block_text:
                        _emit_text(tag, turn_counter, block_text)
                        text_parts.append(block_text)
                        # #902/#903 — also persist the narration into
                        # the qa_run_logs archive so slice 2 + 3 can
                        # query why the persona did what they did, not
                        # just what tool they called.
                        if recorder is not None:
                            recorder.log_emit(
                                "text",
                                str(block_text),
                                turn=turn_counter,
                                phase="explore",
                            )
                if text_parts:
                    narration.append("\n".join(text_parts))
                # #860 — persist the same blocks as qa_run_steps docs.
                # Recorder ordering: AFTER log emission so a recorder
                # exception (already swallowed by _safe) can NEVER suppress
                # the operator's live-log view.
                if recorder is not None:
                    recorder.on_assistant_message(
                        getattr(message, "content", []) or []
                    )
            elif isinstance(message, UserMessage):
                # Tool results land here. The runner has historically
                # skipped them (the model's next AssistantMessage shows
                # what it did with them — see comment in #860). The
                # recorder needs them for one specific purpose: extracting
                # the PNG bytes from browser_take_screenshot results into
                # GridFS. Everything else in the user message is ignored
                # by the recorder (see RunRecorder.on_user_message).
                if recorder is not None:
                    recorder.on_user_message(
                        getattr(message, "content", []) or []
                    )
            elif isinstance(message, ResultMessage):
                accounting.record(
                    phase="explore",
                    model=config.explore_model,
                    usage=getattr(message, "usage", None),
                    num_turns=getattr(message, "num_turns", 0) or 0,
                )
                _emit_result(tag, message, accounting)
                # #902/#903 — record the turn-done accounting in the
                # archive so cross-run analyzers can see per-turn token
                # usage alongside the narrative. metadata carries the same
                # numbers _emit_result formats for the operator log
                # (#1822: token counts only, no dollar conversion).
                if recorder is not None:
                    usage = getattr(message, "usage", None) or {}
                    _turn_tokens = sum(
                        int(
                            (usage.get(k, 0) if isinstance(usage, dict)
                             else getattr(usage, k, 0)) or 0
                        )
                        for k in (
                            "input_tokens",
                            "output_tokens",
                            "cache_creation_input_tokens",
                            "cache_read_input_tokens",
                        )
                    )
                    recorder.log_emit(
                        "result",
                        f"turn done tokens={_turn_tokens:,}",
                        phase="explore",
                        metadata={
                            "num_turns": int(
                                getattr(message, "num_turns", 0) or 0
                            ),
                            "usage": (
                                dict(usage) if isinstance(usage, dict)
                                else {
                                    "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                                    "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                                }
                            ),
                        },
                    )
    except Exception as exc:  # noqa: BLE001 - mid-run SDK errors must NOT crash the orchestrator
        if _is_max_turns_error(exc):
            truncated_reason = (
                f"max_turns reached ({config.max_turns}) — the explore phase "
                "ran out of turns before the persona finished her flow list"
            )
            print(
                f"WARNING: explore phase for {persona.id!r} hit max_turns "
                f"({config.max_turns}); proceeding to the report phase with "
                f"the partial transcript and {len(findings)} finding(s)",
                file=sys.stderr,
            )
        elif _is_success_completion(exc):
            # SDK quirk: claude CLI exited cleanly but the wrapping layer
            # raised with "error result: success" (#668). Treat as a graceful
            # completion — the accumulated transcript + findings are real.
            truncated_reason = (
                "explore phase ended via the Claude Code SDK's "
                "'error result: success' quirk (treated as graceful)"
            )
            print(
                f"NOTICE: explore phase for {persona.id!r} ended on the SDK's "
                f"'result: success' quirk; treating as graceful completion and "
                f"proceeding to the report phase with the transcript and "
                f"{len(findings)} finding(s)",
                file=sys.stderr,
            )
        else:
            truncated_reason = (
                f"exploration failed: {type(exc).__name__}: {exc}"
            )
            print(
                f"WARNING: explore phase for {persona.id!r} aborted on "
                f"{type(exc).__name__}: {exc!r}; proceeding to the report "
                f"phase with the partial transcript and {len(findings)} "
                "finding(s)",
                file=sys.stderr,
            )

    digest_text = _digest(narration)
    if truncated_reason is not None:
        # Make the truncation visible inside the digest itself so the report
        # phase's prompt sees it and the persona can write an honest review.
        header = f"_(explore phase ended early: {truncated_reason})_"
        digest_text = (
            f"{header}\n\n{digest_text}" if digest_text else header
        )
    return ExploreOutcome(digest=digest_text, truncated_reason=truncated_reason)


async def run_report_phase(
    persona: Persona,
    config: Config,
    findings: Findings,
    explore_digest: str,
    accounting: RunAccounting,
) -> str:
    """Run the report phase (no tools). Returns the review markdown."""
    if len(findings):
        findings_block = "\n".join(
            f"- [{f.category}/{f.severity}] {f.title}\n  {f.body}"
            for f in findings
        )
    else:
        findings_block = "(no findings were recorded)"

    options = ClaudeAgentOptions(
        model=config.report_model,
        system_prompt=persona.report_system_prompt,
        allowed_tools=[],
        mcp_servers={},
        max_turns=1,
        permission_mode="bypassPermissions",
        # Same ANTHROPIC_API_KEY scrub as the explore phase — always Max.
        # See the explore-phase ClaudeAgentOptions / _options_env.
        env=_options_env(config),
    )

    prompt = (
        "Here is what happened while you used SlyReply.\n\n"
        "STRUCTURED FINDINGS YOU RECORDED:\n"
        f"{findings_block}\n\n"
        "TRANSCRIPT DIGEST (what you did and said, in order):\n"
        f"{explore_digest or '(no narration captured)'}\n\n"
        "Now write your honest first-person review, in markdown, using the "
        "section headings you were told to use. Base it only on the findings "
        "and digest above."
    )

    tag = f"{persona.id} report"
    logger.info(f"[{tag}] writing review")
    review_chunks: list[str] = []
    agen = query(prompt=prompt, options=options)
    async for message, _turn_holder in _stream_with_heartbeat(agen, tag):
        if isinstance(message, AssistantMessage):
            text_parts: list[str] = []
            for block in getattr(message, "content", []) or []:
                block_name = getattr(block, "name", None)
                block_input = getattr(block, "input", None)
                block_text = getattr(block, "text", None)
                if block_name is not None and block_input is not None:
                    _emit_tool_use(tag, None, block_name, block_input)
                elif block_text:
                    _emit_text(tag, None, block_text)
                    text_parts.append(block_text)
            if text_parts:
                review_chunks.append("\n".join(text_parts))
        elif isinstance(message, ResultMessage):
            accounting.record(
                phase="report",
                model=config.report_model,
                usage=getattr(message, "usage", None),
                num_turns=getattr(message, "num_turns", 0) or 0,
            )
            _emit_result(tag, message, accounting)

    logger.info(f"[{tag}] done")
    return "\n".join(review_chunks).strip()


async def run_persona(
    persona: Persona, config: Config, *, by_design_block: str = "",
) -> RunResult:
    """Run both phases for a persona and assemble the ``RunResult``.

    ``by_design_block`` is the run's pre-loaded by-design site_knowledge (#2097),
    built once at run setup and injected into the explore prompt so the persona
    doesn't re-flag intentional behaviour. Empty string → no injection.

    The whole run is wrapped in ``config.run_timeout_s`` so a stuck agent
    fails cleanly rather than hanging the Job.
    """
    started_at = datetime.now(UTC)
    run_id = new_run_id(persona.id, now=started_at)
    findings = Findings()
    # Runs always bill Claude Code Max — stamp the accounting record so
    # the Atlas sink + review UI render the run as Max-billed (#1822: only
    # token totals are tracked, no dollar conversion).
    accounting = RunAccounting(backend="claude-code")

    async def _run() -> tuple[str, str]:
        outcome = await run_explore_phase(
            persona, config, findings, accounting,
            by_design_block=by_design_block,
        )
        review = await run_report_phase(
            persona, config, findings, outcome.digest, accounting
        )
        return review, outcome.digest

    try:
        review, digest = await asyncio.wait_for(
            _run(), timeout=config.run_timeout_s
        )
    except TimeoutError:
        review = (
            "_The run exceeded its time budget before a review could be "
            "written. The findings recorded up to that point are below._"
        )
        digest = "(run timed out)"

    finished_at = datetime.now(UTC)
    return RunResult(
        run_id=run_id,
        persona_id=persona.id,
        persona_display_name=persona.display_name,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        accounting=accounting,
        findings=findings,
        review_markdown=review,
        explore_digest=digest,
    )
