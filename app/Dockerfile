# Persona QA review UI — Slice 6 (#626).
#
# Multi-stage:
#   stage 1 (web)  — builds the Vue 3 SPA with Vite → /web/dist
#   stage 2 (api)  — Python; installs the FastAPI app + the qa-store package,
#                    copies the built SPA in, runs uvicorn.
#
# The image serves the control-room API + built SPA. CI wiring (the build +
# push) is deferred; this Dockerfile is the contract.
#
# IMPORTANT: the build context must be the repo root, because the API depends
# on the sibling `store/` (qa-store) package and the harness's personas:
#
#   docker build -f app/Dockerfile -t testease-app .

# ---------------------------------------------------------------------------
# Stage 1 — build the SPA.
# ---------------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY app/web/package.json app/web/package-lock.json* ./
RUN npm install
COPY app/web/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 — the FastAPI app.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS api

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    QA_REVIEW_SPA_DIR=/app/static

WORKDIR /app

# The shared qa_store package (pymongo only — keeps the image light).
COPY store /tmp/qa-store
RUN pip install /tmp/qa-store

# The harness's persona definitions — needed by the qa_personas seed call in
# create_app(). ``qa_agents.personas`` is a pure-stdlib module (only imports
# ``dataclasses``), so installing the harness with --no-deps skips the heavy
# claude-agent-sdk / playwright / anthropic deps and keeps the image small.
# Without this, the seed silently skips with ImportError on every boot and
# the persona library is empty — what shipped in #988 and broke /personas/{id}.
COPY harness /tmp/qa-agents
RUN pip install --no-deps /tmp/qa-agents

# The API package itself.
COPY app/api/pyproject.toml app/api/README.md ./
COPY app/api/qa_review_api ./qa_review_api
# Deps are installed explicitly (the package is installed --no-deps) — keep
# this list in sync with pyproject.toml's [project.dependencies].
RUN pip install --no-deps . \
    && pip install "fastapi>=0.110" "uvicorn[standard]>=0.27" "httpx>=0.27" \
        "kubernetes>=29" "anthropic>=0.39" "docker>=7"

# The built SPA from stage 1 — FastAPI serves these static files at /.
COPY --from=web /web/dist ./static

EXPOSE 8000

# --factory because qa_review_api.app:app is the create_app callable, so
# uvicorn builds the app (and opens the Mongo connection) at startup, not import.
CMD ["uvicorn", "qa_review_api.app:app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
