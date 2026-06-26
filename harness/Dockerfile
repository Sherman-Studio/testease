# QA persona harness image.
#
# The harness drives a browser via the Playwright MCP server. The image
# carries: Python 3.12 (the package), Node.js (to run the MCP server), the
# pinned `@playwright/mcp` server installed globally, and the bundled Chromium
# the server drives — installed at a fixed, HOME-independent path.
#
# Browser provisioning is deliberate. An earlier image let the MCP server
# fetch its own browser on first run and launched it via `npx
# @playwright/mcp@latest`; both broke at runtime and a persona burned ~60
# turns trying to install a browser by hand. The reasons, and the fixes:
#   * The pod sets HOME=/tmp AND mounts an emptyDir over /tmp, so anything
#     Playwright caches under ~/.cache or ~/.npm at BUILD time is invisible at
#     RUNTIME. PLAYWRIGHT_BROWSERS_PATH pins the browser to /ms-playwright —
#     image-baked and outside any runtime volume.
#   * `@playwright/mcp` is PINNED (no @latest drift) and installed globally,
#     so the runtime needs no npx download.
#   * Chromium is installed by the very Playwright that `@playwright/mcp`
#     depends on, so the browser revision always matches the server. We use
#     the bundled *Chromium*, not the Chrome channel: Chrome has no arm64
#     Linux build and this image is multi-arch (amd64 + arm64).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Node.js (for the Playwright MCP server) + curl/ca-certs for the install script.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The shared slyreply_qa store package — the AtlasReportSink depends on it.
# Installed first so the harness's `qa-store` dependency resolves locally
# (it is not published to PyPI). The build context must be `qa-agents/` so
# both `harness/` and `qa-store/` are reachable; see qa-agents CI wiring.
COPY qa-store /tmp/qa-store
RUN pip install /tmp/qa-store

COPY harness/pyproject.toml harness/README.md ./
COPY harness/qa_agents ./qa_agents

# Install the package (no dev extras in the runtime image). qa-store is
# already satisfied above, so pip will not try to fetch it from an index.
#
# ``--no-deps`` means pyproject's dependency list is NOT walked; every
# runtime dep MUST be listed explicitly below. The Anthropic SDK is
# needed by qa_agents.memory_distillation for the post-run Haiku call
# (#872); it was historically a transitive dep of claude-agent-sdk, but
# a recent SDK release dropped that transitive — distillation crashed
# with ``ModuleNotFoundError: No module named 'anthropic'`` (#906).
# Pin floor matches pyproject (>=0.42 ships claude-haiku-4-5).
RUN pip install --no-deps . \
    && pip install claude-agent-sdk httpx pymongo "anthropic>=0.42"

# ── Playwright MCP server + its Chromium ──────────────────────────────────
# PLAYWRIGHT_BROWSERS_PATH MUST be a fixed path outside /tmp (the runtime
# emptyDir) and independent of HOME — see the header comment. `@playwright/mcp`
# is pinned and installed globally; bump PLAYWRIGHT_MCP_VERSION to move it.
# Chromium is then installed by the Playwright that `@playwright/mcp` itself
# depends on (resolved whether npm nested or hoisted it), so the revision
# always matches the server. runner.py points the server at the resolved
# binary through the /usr/local/bin/qa-chromium symlink.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ARG PLAYWRIGHT_MCP_VERSION=0.0.75

RUN npm install -g @playwright/mcp@${PLAYWRIGHT_MCP_VERSION} \
    && PW_DIR="$(npm root -g)/@playwright/mcp/node_modules/playwright" \
    && [ -d "$PW_DIR" ] || PW_DIR="$(npm root -g)/playwright" \
    && node "$PW_DIR/cli.js" install --with-deps chromium \
    && CHROMIUM_BIN="$(find /ms-playwright -type f -name chrome -path '*chrome-linux*' | head -1)" \
    && test -n "$CHROMIUM_BIN" \
    && ln -s "$CHROMIUM_BIN" /usr/local/bin/qa-chromium \
    && /usr/local/bin/qa-chromium --version

# ── OpenAPI surface explorer ──────────────────────────────────────────────
# The OpenAPI surface-discovery MCP server is now IN-PROCESS — see
# qa_agents/tools/openapi.py (build_openapi_server). It fetches the tenant's
# spec with httpx and exposes list_endpoints / get_endpoint / search as
# tools, in the same shape as the email / findings / identity servers. No
# npm package is installed: the original kadykov/mcp-openapi-schema-explorer
# wiring (#1026) exposed MCP *resources*, not the tools the runner
# allow-listed, and took the spec as a positional arg (not the --spec flag
# the runner passed), so api-poker runs saw zero openapi tools. The
# in-process server removes that whole failure mode.

# ── Chrome DevTools MCP server (#1024) ────────────────────────────────────
# Google's chrome-devtools-mcp wraps the Chrome DevTools Protocol so
# perf-focused personas (slow-connection, perf-budget-evaluator) can
# emulate CPU + network throttling, record performance traces, and
# inspect network requests. Same image-baked pattern as @playwright/mcp:
# pinned version, installed globally, no runtime npx fetch. The server
# launches its own Chrome via the Chrome DevTools Protocol; it reuses
# the same /usr/local/bin/qa-chromium symlink the Playwright MCP server
# was already pointed at (the chromium binary supports CDP natively).
ARG CHROME_DEVTOOLS_MCP_VERSION=0.4.0
RUN npm install -g chrome-devtools-mcp@${CHROME_DEVTOOLS_MCP_VERSION}

# ── Axe / a11y MCP server (#1021) ─────────────────────────────────────────
# Community a11y-mcp wraps axe-core so the keyboard-only and screen-reader
# personas can run WCAG-citation-grade audits ("violates WCAG 2.1 AA,
# criterion 2.4.7 Focus Visible, on selector .cta-primary") instead of
# the qualitative "focus rings are missing" they produce on snapshot
# alone. Same image-baked pattern as Playwright + Chrome DevTools:
# pinned, installed globally, no runtime npx fetch. The MCP package
# spawns its own short-lived browser per audit so it doesn't interfere
# with the persona's Playwright session.
ARG A11Y_MCP_VERSION=1.0.6
RUN npm install -g a11y-mcp@${A11Y_MCP_VERSION}

# (Stripe official MCP removed in the Stripe→Revolut migration — the
# harness no longer drives a payment-processor MCP. Subscription
# lifecycle is exercised via the QA-only billing hooks #2007:
# /api/qa/billing/{provision,advance,state}. Revolut has no test-clock
# and the checkout widget #1977 isn't built yet, so the hooks are the
# deterministic substitute.)

# ── Claude Code CLI (#894) ────────────────────────────────────────────────
# claude-agent-sdk spawns the `claude` CLI as a subprocess. The API-key
# code path doesn't strictly require it (the SDK has a built-in transport
# for that), but in QA_LLM_BACKEND=claude-code mode the CLI is the
# component that resolves OAuth credentials — via the
# CLAUDE_CODE_OAUTH_TOKEN env (cluster Max Job pattern) or via Keychain
# / ~/.claude (laptop pattern). Installing it globally here makes the
# same image work for both backends.
#
# Pinned to a known-good version; bump CLAUDE_CODE_VERSION to upgrade.
# The CLI is published as @anthropic-ai/claude-code on npm.
ARG CLAUDE_CODE_VERSION=2.1.150
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && claude --version

# Defaults; the k8s Job overrides these via env.
ENV QA_PERSONA=margaret \
    QA_OUT_DIR=/app/qa-runs

ENTRYPOINT ["python", "-m", "qa_agents"]
