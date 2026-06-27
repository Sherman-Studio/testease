# Test Ease

Test Ease is an autonomous AI QA tool you point at **any** website. It runs a
library of fictional-user *personas* that explore your site through a real
browser — signing up, paying, poking the API, filing the bugs they hit — and
writes up what a real user would experience. It learns each site as a **Site
Model** (surfaces, flows, and curated by-design knowledge stored as data, not
code) so the testers stop re-flagging intentional behaviour over time.

It runs on your **Claude Code Max** subscription (flat price, not per-token
API billing) and is **local-first**: `docker compose up` brings up the whole
stack against a local Atlas deployment — no cloud account required.

> Extracted from a monorepo (June 2026). This is a clean-slate repository
> with no carried git history. See `CLAUDE.md` for the full context.

## Quickstart

```bash
cp .env.example .env
# add your Claude Code token:  claude setup-token  → CLAUDE_CODE_OAUTH_TOKEN
docker compose up            # control-room UI on http://localhost:8000
```

To run the persona harness against a target (on-demand batch job):

```bash
# set QA_TARGET_ID / QA_WEB_BASE_URL in .env first
docker compose run --rm harness
```

Optional profiles: `--profile email` adds Mailpit (catch-all SMTP + inbox at
http://localhost:8025) for personas that test email flows.

## Layout

```
store/     qa_store — pymongo data layer + the Site Model (Store, schema,
           site_model / site_retriever / site_reconciler, embeddings)
harness/   qa_agents — persona runner, orchestrator, Playwright + Claude
           Code CLI, Site Model migration, MCP tools  (Dockerfile)
app/       control-room UI — FastAPI api/ + Vue web/  (Dockerfile, DESIGN.md)
docker-compose.yml   atlas (vector-capable) + app + harness + mailpit
```

(Internal Python packages keep their `qa_store` / `qa_agents` names for now;
renaming them is a follow-up — see `CLAUDE.md`.)

## What's left to make `docker compose up` actually work

`docker compose up` now **builds and serves** (atlas + vector-init + app); the
control room is at http://localhost:8000. Remaining follow-ups:

- [x] **Re-point the Dockerfile COPY paths.** Both Dockerfiles now copy
      `store/` / `harness/` / `app/...` from the repo-root build context; both
      images build clean.
- [x] **Atlas vector-index init.** `qa_store.ensure_vector_indexes` creates the
      384-d `site_knowledge` / `site_surfaces` `$vectorSearch` indexes. It runs
      at the app's startup (best-effort) and as the compose `vector-init`
      one-shot — `python -m qa_store.init_vector_indexes`, which polls until
      `mongot` is index-ready (the healthcheck only proves `mongod` answers).
      Idempotent; safe to re-run.
- [x] **Harness image builds** — the multi-stage image (fastembed model,
      `claude` CLI, Playwright + Chromium) builds and tags clean.
- [~] **Cord-cut the remaining slyreply hardcodes** — run-blocking config
      defaults (`target_id`, `qa_store_db`, admin email, etc. in `config.py` /
      `settings.py`) are now site-agnostic. Still open: the
      `store/fixtures/slyreply.yaml` dogfood fixture and deeper couplings
      (email domains, the `qa-run-id` pod label) — see `CLAUDE.md` / `docs/ROADMAP.md`.
- [x] **Embedding-provider env selector.** `QA_EMBEDDING_PROVIDER`
      (`local` default / `openai` / `mock`) picks the provider via
      `qa_store.embeddings.make_embedding_provider()`; the vector-index
      bootstrap sizes itself to the provider's dimension
      (`embedding_dim_for()` → 384 local, 1536 OpenAI). Set in `.env`.
- [ ] **Rename `qa_store` / `qa_agents`** to product-neutral package names.
