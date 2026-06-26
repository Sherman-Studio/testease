# Persona QA review UI — Slice 6 (#626).
#
# Multi-stage:
#   stage 1 (web)  — builds the Vue 3 SPA with Vite → /web/dist
#   stage 2 (api)  — Python; installs the FastAPI app + the qa-store package,
#                    copies the built SPA in, runs uvicorn.
#
# The image is ghcr.io/mccullya/slyreply-qa-review. CI wiring (the build +
# push) is deferred to Slice 5; this Dockerfile is the contract.
#
# IMPORTANT: the build context must be the `qa-agents/` directory, because the
# API depends on the sibling `qa-store/` package:
#
#   docker build -f qa-agents/review-ui/Dockerfile -t slyreply-qa-review qa-agents

# ---------------------------------------------------------------------------
# Stage 1 — build the SPA.
# ---------------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY review-ui/web/package.json review-ui/web/package-lock.json* ./
RUN npm install
COPY review-ui/web/ ./
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

# The shared slyreply_qa store package (pymongo only — keeps the image light).
COPY qa-store /tmp/qa-store
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
COPY review-ui/api/pyproject.toml review-ui/api/README.md ./
COPY review-ui/api/qa_review_api ./qa_review_api
# Deps are installed explicitly (the package is installed --no-deps) — keep
# this list in sync with pyproject.toml's [project.dependencies].
RUN pip install --no-deps . \
    && pip install "fastapi>=0.110" "uvicorn[standard]>=0.27" "httpx>=0.27" \
        "kubernetes>=29" "anthropic>=0.39"

# The built SPA from stage 1 — FastAPI serves these static files at /.
COPY --from=web /web/dist ./static

EXPOSE 8000

# --factory because qa_review_api.app:app is the create_app callable, so
# uvicorn builds the app (and opens the Mongo connection) at startup, not import.
CMD ["uvicorn", "qa_review_api.app:app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
