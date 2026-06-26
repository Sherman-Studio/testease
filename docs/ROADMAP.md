# Test Ease — state & roadmap (handoff)

A fresh Claude Code session should read this + `CLAUDE.md` to know where things stand. Written 2026-06-26, just after extracting Test Ease from the SlyReply monorepo into this standalone repo.

## Where we are right now
- **Standalone repo scaffolded** (this repo): `store/` (qa_store — pymongo data layer + the Site Model), `harness/` (qa_agents — persona runner, Playwright, Claude Code), `app/` (review-ui — FastAPI + Vue "control room", incl. the Site Model browse/curate views). Clean slate, no carried git history.
- **The Site Model foundation is built** (extracted here): schema + DAOs (`store/qa_store/site_model.py`), the vector layer (`site_retriever.py`, `site_reconciler.py`, `embeddings.py` — local-default fastembed), the dogfood migration (`harness/.../migrate_site_model.py`), and the by-design injection into persona prompts (`harness/.../site_knowledge.py`).
- **NOT yet runnable** via `docker compose up` — see the punch-list. `docker-compose.yml` uses `mongodb-atlas-local` (the only Mongo with `$vectorSearch`).
- Dogfood proof (in SlyReply's live DB, not here): the migration turned SlyReply's hardcoded `personas.py` knowledge into **1 target + 158 test_flows + 91 by-design site_knowledge** rows. This repo starts fresh.

## Immediate next: run-readiness punch-list (make `docker compose up` work)
1. **Dockerfile COPY paths** — both Dockerfiles still assume the monorepo `qa-agents/` context; repoint to `store/`/`harness/`/`app/` from the repo root. (Build fails until fixed — the reason it's not runnable yet.)
2. **Atlas vector-index init** — atlas-local serves `$vectorSearch`, but the `site_surfaces`/`site_knowledge` vector indexes (384-d) need a one-shot creation step on boot.
3. **`QA_EMBEDDING_PROVIDER` env selector** — provider is injected in code today; make `.env` pick local (default) vs OpenAI.
4. **Cut remaining SlyReply hardcodes** — `harness/.../config.py` (`target_id="slyreply"`, `qa_store_db="slyreply_qa"`, `admin_email`), `app/api/.../settings.py` (`github_repo`, `qa_store_db`, `sandbox_namespace`), the `store/.../fixtures/slyreply.yaml` dogfood fixture, Dockerfile `ghcr.io/mccullya/slyreply-*` image names.
5. **Rename `qa_store`/`qa_agents`** to product-neutral names (deferred to avoid a churny mass-rename now).

## The product vision / roadmap (the real build)
The north star: **point it at any website; it onboards itself, then tests like real users.**
1. **Explorer role (distinct from testing personas)** — probes a site for affordances (signup? payments? logs? API? MCP? could the human grant read-only DB access?), writes up its understanding, and generates a **per-site human questionnaire** (unbounded; each site differs) surfaced in the UI. Re-runnable.
   - **Affordance discovery, NOT intrusion**: the agent *proposes* capabilities, the human *grants* them. It never attempts unauthorized access. (Ethics/legality for "any site" + cleaner model.)
   - The questionnaire is the product's hinge: it unifies consent/authorization + configuration (creds/scope) + knowledge-elicitation in one adaptive artifact.
2. **Secrets vault** — questionnaire answers flagged sensitive (DB passwords, test logins) go to an encrypted per-target vault; the Site Model stores only `credential_ref` *pointers*. Build this boundary before storing any secret. Store the dynamic questionnaire as a `site_questions` collection; give each target a lifecycle (`registered → exploring → awaiting-answers → configured → testing → re-explore`).
3. **Testing personas on loops** — ~30 generic archetypes (now target-agnostic templates) read `test_flows` + the by-design `site_knowledge` from the Site Model and run.
4. **"Watch it drive"** — a live browser-streaming view of the agent driving Playwright, for trust/debugging. (Andrew: "would be amazing.")
5. **Semantic retrieval wiring** — use the vector layer (`site_retriever`) at targeted points (per-flow "what do we know about this area", triage dedup); run the reconciler in the embedding image (bake the fastembed model). Largely dormant until there's discovered content.
6. **GitHub-issue generation** — later; log internally first.
7. **Productization** — multi-tenancy, authorization-to-test (domain ownership verification), billing. **Local-first now**; Helm/cloud later.

## Decisions that are settled — do NOT relitigate
- **BYOK + Claude Code flat-price is a MUST-HAVE.** Runs on the user's Claude Code Max OAuth token (flat price, not per-token); harness scrubs `ANTHROPIC_API_KEY` so the `claude` CLI uses the subscription; selectable `claude-code|api` backend; mint via `claude setup-token`. ToS gray-area caveat (subscription tokens for automated use; `api` is the sanctioned commercial path).
- **Embeddings default LOCAL/no-key** (`fastembed` `bge-small`, 384-d) because Anthropic has no embeddings API; OpenAI/Voyage optional via `qa-store[vector]`. atlas-local does local `$vectorSearch`.
- **Site knowledge is DATA, not code** — the Site Model (`site_targets`/`site_surfaces`/`test_flows`/`site_knowledge`), per-`(tenant,target)`. Flexible Mongo schema is deliberate.
- **Site-agnostic** — no SlyReply anywhere in the product.

## Provenance / what stayed in SlyReply
- Extracted from the `mccullya/slyreply` monorepo, June 2026.
- **Sherman** (SlyReply's separate internal ops agent) stayed behind — its own-repo + standalone-UI extraction is tracked as `mccullya/slyreply#2106`.
- The QA billing hooks (`/api/qa/billing/*`) stayed in the SlyReply backend; in the generic product they become a **per-target capability the human configures** (the first real example of the explorer/questionnaire model).
- SlyReply still has a copy of `qa-agents/`; delete it there once this repo runs standalone (don't maintain two).
