# CLAUDE.md

Guidance for Claude Code working in the **Test Ease** repository.

> **Picking up where the last session left off?** Read [`docs/ROADMAP.md`](docs/ROADMAP.md) — current state, the run-readiness punch-list, the product roadmap, and the settled decisions. This repo was just extracted from a monorepo (2026-06-26); the immediate task is making `docker compose up` work.

## What it is

Test Ease is an autonomous AI QA tool you point at **any** website. A library
of fictional-user *personas* explores the site through a real browser — signing
up, paying, poking the API, hitting edge cases — and reports what a real user
would experience, filing the bugs they find. It is **site-agnostic**: there is
no coupling to any particular product. (It was extracted from the SlyReply
monorepo, where it dogfooded on SlyReply, but the goal is zero hard
dependence on that or any single site.)

It runs **local-first** (`docker compose up` against a local Atlas
deployment) and on the user's **Claude Code Max subscription** (flat price).

## Vision

The product's north star is an **onboarding "explorer" role** distinct from the
testing personas:

1. **Explore.** Point the explorer at a site. It probes for affordances — is
   there signup? payments? an API? logs? an MCP server? could the human grant
   read-only DB access? — and writes up its understanding of the site.
2. **Ask.** From that understanding it generates a per-site **human
   questionnaire** (unbounded — every site's questions differ), surfaced in the
   UI for the operator to answer. Answers may include secrets (test-card
   numbers, sandbox credentials, API keys).
3. **Configure.** The answers configure the testing personas — which flows
   exist, what credentials to use, what's in-scope.
4. **Run & iterate.** ~30 generic persona archetypes run loops against the
   site; findings accumulate; the model of the site sharpens each pass.

Issues are logged **internally** now; GitHub issue filing is a later option. A
**"watch it drive"** live-browser view (see the persona's session in real time)
is a goal.

### Affordance discovery, not intrusion

The explorer **proposes** capabilities and the human **grants** them. It never
attempts unauthorized access — it asks "may I have read-only DB access?" and
waits for the operator to provide it. Discovery is a conversation, not a scan.

### Secrets are pointers, never raw

Questionnaire answers that are secrets go through a **vault**.
`site_targets.auth.credential_ref` is a **pointer** to a vaulted secret, never
the raw value. No secret is stored inline in the Site Model.

## Architecture

Three top-level packages (Python package names `qa_store` / `qa_agents` are
retained from the monorepo to avoid a mass-rename in the extraction; renaming
them to product-neutral names is a follow-up):

- **`harness/`** (`qa_agents`) — the persona runner and orchestrator. Drives a
  browser via the Playwright MCP server, runs the `claude` CLI as the agent
  loop, applies setup actions, distills findings. Owns the Site Model
  migration and the per-persona MCP tool wiring (email, openapi, a11y, cost…).
- **`store/`** (`qa_store`) — a light **pymongo** data layer (sync). The
  `Store` + module-level functions over the collections; the **Site Model**
  (below); the embeddings + vector retrieval/reconciliation layer.
- **`app/`** — the **control-room UI**: a FastAPI `api/` that reads/curates the
  store, plus a Vue 3 `web/` SPA (the "Control Room" design system; see
  `app/DESIGN.md`). One Docker image serves the API + built SPA.

## Site Model — knowledge as DATA, not code

Each site is modelled per `(tenant_id, target_id)` across four Mongo
collections:

- `site_targets` — the site itself (display name, base URL, `auth` with the
  vaulted `credential_ref`).
- `site_surfaces` — discovered pages/areas.
- `test_flows` — the journeys personas walk (signup, checkout, …).
- `site_knowledge` — curated notes: `by_design` / `known_issue` / `guidance` /
  `glossary`. The harness injects `by_design` entries into persona prompts so
  testers stop re-flagging intentional behaviour (closing the code→data loop).

The Mongo schema is **deliberately flexible** — every site's questionnaire and
discoveries differ, so the model can't be a rigid fixed schema. The control
room (`app/`) browses and curates this model.

## BYOK + Claude Code flat-price (MUST-HAVE)

Test Ease runs on the user's **Claude Code Max OAuth token** — flat
subscription price, not per-token API billing. Mechanics:

- The harness **scrubs `ANTHROPIC_API_KEY`** from the environment so the
  `claude` CLI resolves the **subscription** token (`CLAUDE_CODE_OAUTH_TOKEN`),
  not an API key.
- Backend is selectable: **`claude-code`** (subscription token, the default and
  the point of the product) | **`api`** (`ANTHROPIC_API_KEY`, per-token).
- Mint a token with **`claude setup-token`**.
- **ToS caveat:** using subscription tokens for sustained automated/headless
  runs is a **gray area**. The sanctioned commercial path is the `api` backend.
  Do not design features that depend on running an unattended subscription loop
  indefinitely.

## Embeddings — local default

Anthropic has **no embeddings API**, so embeddings do **not** run on the Claude
token:

- **Default: LOCAL.** `fastembed` running `BAAI/bge-small-en-v1.5` (384-d),
  in-process, **no API key**. The model is baked into the image at build time
  (read-only-root friendly; `QA_FASTEMBED_CACHE` points at the baked path).
- **Optional:** OpenAI (`text-embedding-3-small`, 1536-d) or Voyage, via the
  `qa-store[vector]` extra. Provider is currently chosen **in code** (injected
  into the reconciler); adding a `QA_EMBEDDING_PROVIDER` env selector is a
  follow-up.
- **Local $vectorSearch:** `mongodb/mongodb-atlas-local` bundles `mongot`, so
  vector search runs locally with no cloud Atlas. Vector indexes still need a
  one-time init step (see README "What's left").

## Local-first

`docker compose up` → `atlas` (vector-capable local Mongo) + `app` (UI). The
`harness` is an on-demand batch job (`--profile run`), not a long-lived
service. `--profile email` adds Mailpit for email-flow personas. Secrets live
in a gitignored `.env` (`cp .env.example .env`).

## Provenance / cord-cuts

Extracted from the **SlyReply monorepo, June 2026**, as a clean-slate repo (no
carried git history). **Sherman** — SlyReply's separate *internal ops* agent —
stayed behind; it is not part of Test Ease and should not be reintroduced here.

Known slyreply couplings still in the copied code (cord-cut follow-ups; all are
env-overridable today except the fixture):

- `harness/qa_agents/config.py` defaults: `target_id="slyreply"`,
  `qa_store_db="slyreply_qa"`, `mongodb_url=…/slyreply`,
  `admin_email=admin@sandbox.slyreply.ai` — override via `QA_TARGET_ID`,
  `QA_STORE_DB`, etc.
- `app/api/qa_review_api/settings.py` defaults: `github_repo="mccullya/slyreply"`,
  `qa_store_db="slyreply_qa"`, `sandbox_namespace="slyreply-sandbox"`.
- `store/fixtures/slyreply.yaml` — the dogfood fixture; make it a generic
  example or drop it.
- The Dockerfiles reference `ghcr.io/mccullya/slyreply-*` image names and
  monorepo-relative COPY paths.

When you touch these, prefer making them site-agnostic (env/config-driven) over
swapping one hardcoded site for another.

## Conventions

- `store/` is **pymongo (sync)**, deliberately light. Idempotent
  `$setOnInsert` upserts; module-level functions, not DAO classes.
- The UI follows `app/DESIGN.md` (Control Room): cyan = interactive only;
  `.panel` / `.pill` / `.btn-*` / `.input` / `.select` / `.textarea`. Use
  relative imports in the SPA (no `@/` alias).
- Tests: pytest for `store/` and `harness/` and `app/api/`; vitest for
  `app/web/`. Run them before committing.
