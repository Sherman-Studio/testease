"""OpenAPI surface-discovery MCP server — in-process rebuild of #1026.

In-process MCP server exposing three TOOLS the api-poker persona (Asha)
uses to discover a tenant's API surface from its OpenAPI / Swagger spec
and then probe the interesting endpoints via the Playwright MCP:

  - ``list_endpoints`` — every (method, path) + summary the spec declares.
  - ``get_endpoint``   — one operation's full schema (parameters, request
                         body, responses), with local ``$ref``s resolved a
                         couple of levels deep so the output is
                         self-contained.
  - ``search``         — keyword search across paths / methods / summaries /
                         descriptions / tags / operationIds.

Why in-process instead of a packaged server
--------------------------------------------
The first cut (#1026 / PR #1142) wired the npm package
``mcp-openapi-schema-explorer``. That package exposes the spec as MCP
*resources* (resource templates), **not tools**, and takes the spec as a
*positional* CLI argument — but the runner allow-listed three tool names
that don't exist on it and launched it with ``--spec <url>`` (an
unrecognised flag, so the server crashed on boot trying to load a spec
literally named ``"--spec"``). Net effect on every api-poker run: the
persona saw zero ``mcp__openapi__*`` tools and reported "the OpenAPI MCP
tools aren't available in this session", then fell back to guessing from
network traffic.

Rather than re-bet on a third-party package's interface — the same gamble
that produced the fictitious-npm-package revert in #1149 — this server is
in-process, in the same shape as the ``email`` / ``findings`` /
``identity`` servers: it owns its own tool contract, fetches the spec with
``httpx``, and is unit-testable with no subprocess. The api-poker persona
prompt already names these exact three tools, so no prompt change is
needed.

Spec loading is lazy + cached: the spec is fetched on the first tool call
and reused for the rest of the run. A missing or unreachable spec never
crashes the run — every tool returns a clear message so the persona files
ONE finding and falls back to UI-only exploration (exactly what its prompt
tells it to do).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

# Lower-cased OpenAPI/Swagger operation keys. Anything else under a path
# item (``parameters``, ``summary``, ``$ref``, vendor extensions) is not an
# operation and is skipped by _iter_operations.
_HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "patch", "options", "head", "trace"}
)

# How many levels of local ``$ref`` get_endpoint inlines. Two is enough to
# turn "requestBody -> $ref Foo -> property $ref Bar" into a self-contained
# blob without risking a context blow-up on deeply-nested schemas.
_REF_INLINE_DEPTH = 2


# ---------------------------------------------------------------------------
# Pure spec helpers — no I/O, unit-tested directly.
# ---------------------------------------------------------------------------
def _iter_operations(spec: dict) -> list[tuple[str, str, dict]]:
    """Return ``(METHOD, path, operation)`` for every operation in the spec.

    Tolerant of malformed specs: a non-dict ``paths`` value, a non-dict
    path item, or a non-dict operation are all skipped rather than raising.
    """
    out: list[tuple[str, str, dict]] = []
    paths = spec.get("paths") if isinstance(spec, dict) else None
    if not isinstance(paths, dict):
        return out
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() in _HTTP_METHODS and isinstance(op, dict):
                out.append((method.upper(), path, op))
    return out


def _summary_of(op: dict) -> str:
    """Best one-line label for an operation: summary, else operationId."""
    return op.get("summary") or op.get("operationId") or ""


def list_endpoints(spec: dict) -> list[dict]:
    """Every declared endpoint as ``{method, path, summary}`` rows."""
    return [
        {"method": m, "path": p, "summary": _summary_of(op)}
        for m, p, op in _iter_operations(spec)
    ]


def _resolve_ref(spec: dict, ref: str) -> Any:
    """Resolve a local JSON-pointer ``$ref`` (``#/a/b/c``) against the spec.

    Returns ``None`` for external refs or any pointer that doesn't resolve.
    """
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    node: Any = spec
    for raw in ref[2:].split("/"):
        # JSON-pointer escaping: ~1 -> "/", ~0 -> "~".
        part = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _inline_refs(
    spec: dict,
    node: Any,
    depth: int = _REF_INLINE_DEPTH,
    _seen: frozenset = frozenset(),
) -> Any:
    """Inline local ``$ref``s up to ``depth`` levels.

    Keeps ``get_endpoint`` output self-contained without unbounded
    recursion: a ref already on the current resolution path (cycle) or a
    ref hit past the depth budget is left as ``{"$ref": ...}`` so the
    persona can still see the pointer.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if depth <= 0 or ref in _seen:
                return {"$ref": ref}
            target = _resolve_ref(spec, ref)
            if target is None:
                return node
            return _inline_refs(spec, target, depth - 1, _seen | {ref})
        return {k: _inline_refs(spec, v, depth, _seen) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(spec, v, depth, _seen) for v in node]
    return node


def get_endpoint(spec: dict, method: str, path: str) -> dict | None:
    """The full operation object for one ``(method, path)``, refs inlined.

    Returns ``None`` if no such operation exists in the spec.
    """
    wanted = (method or "").upper()
    for m, p, op in _iter_operations(spec):
        if m == wanted and p == path:
            return _inline_refs(spec, op)
    return None


def search_endpoints(spec: dict, query: str) -> list[dict]:
    """Endpoints whose path/method/summary/description/tags/operationId
    contain ``query`` (case-insensitive). Empty query → no matches."""
    q = (query or "").strip().lower()
    if not q:
        return []
    out: list[dict] = []
    for m, p, op in _iter_operations(spec):
        haystack = " ".join(
            str(x)
            for x in (
                p,
                m,
                op.get("summary"),
                op.get("description"),
                op.get("operationId"),
                " ".join(t for t in (op.get("tags") or []) if isinstance(t, str)),
            )
            if x
        ).lower()
        if q in haystack:
            out.append({"method": m, "path": p, "summary": _summary_of(op)})
    return out


def parse_spec(text: str) -> dict:
    """Parse a spec document — JSON first, YAML as an optional fallback.

    FastAPI's ``/openapi.json`` is JSON, so the common path never touches
    YAML. An operator-supplied ``QA_OPENAPI_URL`` pointing at a ``.yaml``
    spec hits the fallback, which needs PyYAML installed.
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        import yaml  # noqa: PLC0415 — optional dep, only for YAML specs
    except ImportError as exc:
        raise ValueError(
            "spec is not valid JSON and PyYAML is not installed to parse YAML"
        ) from exc
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise ValueError("spec did not parse to a mapping")
    return parsed


async def _fetch_spec(spec_url: str, *, timeout: float = 20.0) -> dict:
    """Fetch + parse the spec from ``spec_url`` (HTTP/HTTPS)."""
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True
    ) as client:
        resp = await client.get(spec_url)
        resp.raise_for_status()
        text = resp.text
    return parse_spec(text)


# ---------------------------------------------------------------------------
# Lazy, cached spec provider — testable without the SDK.
# ---------------------------------------------------------------------------
class SpecProvider:
    """Loads the spec once, on first use, and caches the result.

    ``load()`` returns the parsed spec dict, or ``None`` when there is no
    usable spec — in which case ``error`` holds a persona-facing message
    explaining what to do (file ONE finding, fall back to UI-only). The
    load is attempted at most once; subsequent calls return the cached
    spec or the same error.
    """

    def __init__(self, spec_url: str | None) -> None:
        self._spec_url = spec_url
        self._loaded = False
        self._spec: dict | None = None
        self.error: str | None = None

    async def load(self) -> dict | None:
        if self._loaded:
            return self._spec
        self._loaded = True
        if not self._spec_url:
            self.error = (
                "No OpenAPI spec URL was configured for this run (neither "
                "QA_OPENAPI_URL nor a derivable {web_base_url}/openapi.json). "
                "File ONE finding noting the operator didn't supply a spec "
                "URL, then fall back to UI-only exploration."
            )
            return None
        try:
            self._spec = await _fetch_spec(self._spec_url)
        except Exception as exc:  # noqa: BLE001 - never crash the agent loop
            self.error = (
                f"Could not load the OpenAPI spec from {self._spec_url}: "
                f"{type(exc).__name__}: {exc}. The tenant may not publish a "
                "spec at this path. File ONE finding and fall back to "
                "UI-only exploration."
            )
            self._spec = None
        return self._spec


def _text_result(payload: Any) -> dict:
    """Wrap a payload in the MCP tool ``content`` envelope."""
    text = (
        payload
        if isinstance(payload, str)
        else json.dumps(payload, indent=2, default=str)
    )
    return {"content": [{"type": "text", "text": text}]}


def make_openapi_handlers(provider: SpecProvider) -> dict:
    """Build the three async tool handlers bound to ``provider``.

    Exposed separately from :func:`build_openapi_server` so tests can drive
    the handlers directly — the SDK doesn't surface handlers on the server
    object (same idiom as ``make_note_finding_handler``).
    """

    async def list_endpoints_handler(_args: dict) -> dict:
        spec = await provider.load()
        if spec is None:
            return _text_result(provider.error)
        endpoints = list_endpoints(spec)
        if not endpoints:
            return _text_result(
                "The loaded OpenAPI spec declares no paths. File ONE finding "
                "and fall back to UI-only exploration."
            )
        return _text_result({"count": len(endpoints), "endpoints": endpoints})

    async def get_endpoint_handler(args: dict) -> dict:
        spec = await provider.load()
        if spec is None:
            return _text_result(provider.error)
        method = args.get("method", "")
        path = args.get("path", "")
        op = get_endpoint(spec, method, path)
        if op is None:
            return _text_result(
                f"No operation {(method or '').upper()} {path} in the spec. "
                "Use list_endpoints to see the valid (method, path) pairs."
            )
        return _text_result(
            {"method": (method or "").upper(), "path": path, "operation": op}
        )

    async def search_handler(args: dict) -> dict:
        spec = await provider.load()
        if spec is None:
            return _text_result(provider.error)
        query = args.get("query", "")
        matches = search_endpoints(spec, query)
        return _text_result(
            {"query": query, "count": len(matches), "matches": matches}
        )

    return {
        "list_endpoints": list_endpoints_handler,
        "get_endpoint": get_endpoint_handler,
        "search": search_handler,
    }


def build_openapi_server(*, spec_url: str | None):
    """Build the in-process MCP server exposing the three OpenAPI tools.

    ``spec_url`` is the fully-resolved spec URL (see
    ``qa_agents.runner._resolve_openapi_spec_url``) or ``None`` when the
    run has no spec — in the latter case the tools still exist but return
    the "no spec configured" message so the persona files a finding.

    Returns ``(server, tool_names)`` matching the other ``build_*_server``
    helpers; the runner qualifies the names as ``mcp__openapi__<name>``.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    provider = SpecProvider(spec_url)
    handlers = make_openapi_handlers(provider)

    list_endpoints_tool = tool(
        "list_endpoints",
        "List every (method, path) endpoint the tenant's OpenAPI spec "
        "declares, with a one-line summary each. Call this first to see the "
        "shape of the API — including endpoints the UI never surfaces.",
        {},
    )(handlers["list_endpoints"])

    get_endpoint_tool = tool(
        "get_endpoint",
        "Fetch one endpoint's full schema (parameters, request body, "
        "responses) by method + path. Local $refs are inlined so the schema "
        "is self-contained. Use list_endpoints first to get exact "
        "(method, path) pairs.",
        {"method": str, "path": str},
    )(handlers["get_endpoint"])

    search_tool = tool(
        "search",
        "Keyword-search the spec across paths, methods, summaries, "
        "descriptions, tags and operationIds (e.g. 'admin', 'export', "
        "'billing'). Returns matching (method, path, summary) rows.",
        {"query": str},
    )(handlers["search"])

    server = create_sdk_mcp_server(
        name="openapi",
        tools=[list_endpoints_tool, get_endpoint_tool, search_tool],
    )
    return server, ["list_endpoints", "get_endpoint", "search"]
