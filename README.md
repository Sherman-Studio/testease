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

This commit is a **scaffold + code copy**, deliberately not yet runnable.
The follow-ups:

- [ ] **Re-point the Dockerfile COPY paths.** Both `app/Dockerfile` and
      `harness/Dockerfile` still copy monorepo-relative paths (`qa-store/`,
      `harness/`, `review-ui/...`). They must copy `store/`, `harness/`,
      `app/...` from the repo-root build context.
- [ ] **Atlas vector-index init.** mongodb-atlas-local serves `$vectorSearch`,
      but the Site Model's vector indexes must be created on first boot (an
      init step / one-shot job that creates the search indexes on
      `site_surfaces` / `site_knowledge`). The healthcheck only proves `mongod`
      answers, not that `mongot` is index-ready.
- [ ] **Harness image is heavy** — it builds the fastembed model, the
      `claude` CLI, and Playwright + Chromium. Verify the multi-stage build
      and the baked fastembed cache path (`QA_FASTEMBED_CACHE`) resolve at
      runtime.
- [ ] **Cord-cut the remaining slyreply hardcodes** — default `target_id`,
      `qa_store_db`, admin email, and the `store/fixtures/slyreply.yaml`
      dogfood fixture (see `CLAUDE.md` "Provenance / cord-cuts").
- [ ] **Embedding-provider env selector** — provider is chosen in code
      today; add a `QA_EMBEDDING_PROVIDER` knob so `.env` can pick
      local vs OpenAI without a code change.
- [ ] **Rename `qa_store` / `qa_agents`** to product-neutral package names.
