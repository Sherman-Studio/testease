# QA persona harness

> Slice 3 (#620) + Slice 4 (#621) + Slice 5 (#622) of the Persona QA Agents epic ([#616](https://github.com/mccullya/slyreply/issues/616)).
> Background: [`../EPIC.md`](../EPIC.md) · personas defined in [`qa_agents/personas.py`](./qa_agents/personas.py) · the Slice 1 spike in [`../spike/`](../spike/).

An autonomous Claude agent that **plays a fictional SlyReply user**. It drives a
real browser (Playwright) *and* sends/reads real email, walks the SlyReply flows
in persona, records observations as structured *findings*, then writes a
human-sounding first-person review.

Slice 3 built the harness and proved it with `margaret`. Slice 4 adds the
remaining three personas — all four now live in the `PERSONAS` registry in
`personas.py`.

## The four personas

| Id | Who | What they cover | Signs up? |
|---|---|---|---|
| `margaret` | Reluctant, low-tech-comfort newcomer | Full newcomer journey at a crawl; signup → agent → email round-trip → conversations → *glances* at billing. No admin. | fresh signup |
| `daniel` | Chat-native ChatGPT/Claude user | Everything margaret does, at a confident pace, plus avatar tuning and a **paid-tier upgrade** through the Revolut checkout. Probes the email-vs-chat paradigm and positioning copy. | fresh signup |
| `priya` | Highly technical power user | Everything daniel does, **plus the fair-use abuse run** — a deliberate burst of ~8 emails to trip the cooldown — and edge cases (threading, weird subjects, unknown sender). Wants exact numbers. | fresh signup |
| `tomas` | Day-one administrator | **Logs in** as the seeded sandbox admin (no signup) and walks *every* admin page, attempting realistic ops tasks and reporting console friction. | logs in — see below |

Each persona is two system prompts (explore + report), a flow list, and a
registered email. Personas use distinct `@example.com` local parts (never
`.test`, which is non-routable). `tomas` is the exception — see below.

### Persona admin credentials (`tomas`)

`tomas` does **not** sign up. He logs in as the dedicated sandbox administrator.
His credentials come from `QA_ADMIN_EMAIL` / `QA_ADMIN_PASSWORD`, defaulting to
`admin@sandbox.slyreply.ai` / `testpass123` — the fixed sandbox admin that
[`k8s/sandbox/seed-extra-configmap.yaml`](../../k8s/sandbox/seed-extra-configmap.yaml)
upserts on every seed run.

> **Slice 2 seed coordination — resolved.** The sandbox seed now *always*
> upserts `admin@sandbox.slyreply.ai` with the fixed password `testpass123`
> (idempotent, keyed on that email — not conditional on "no admin exists").
> Those are exactly the `QA_ADMIN_EMAIL` / `QA_ADMIN_PASSWORD` defaults, so the
> sandbox guarantees a loginable admin matching the harness defaults and
> `tomas` works out of the box — no env-var overrides needed.

## How the run works — two phases

`runner.run_persona()` runs two Claude Agent SDK `query()` calls:

1. **Explore phase** — `QA_EXPLORE_MODEL` (Sonnet by default). The agent is put
   fully in persona by the persona's explore system prompt, and given three MCP
   tool servers:
   - **Playwright MCP** — the pinned `@playwright/mcp` server (external, stdio),
     installed in the image and driving the image's bundled Chromium. Browser
     tools `mcp__playwright__browser_*`.
   - **`email`** — an in-process SDK MCP server: `send_email`, `wait_for_email`,
     `get_email` (real SMTP send + Mailpit HTTP API read, MIME-decoded).
   - **`findings`** — an in-process SDK MCP server: `note_finding`, which appends
     to a run-scoped `Findings` collector the runner owns.

   The agent starts at `QA_WEB_BASE_URL`, walks the persona's flow list, and
   calls `note_finding` every time it is confused, worried, surprised, or
   suspects a bug. `max_turns` and `QA_RUN_TIMEOUT_S` bound a stuck run.

2. **Report phase** — `QA_REPORT_MODEL` (Opus by default), **no tools**. The
   agent *is* the persona and writes an honest first-person markdown review.
   Its prompt carries the collected findings plus a bounded transcript digest
   from phase 1.

Both phases' `ResultMessage` usage (tokens) and cost are summed into one
`RunAccounting`. The run then writes, via the `ReportSink` interface:

- `<out>/<persona>-review.md` — the persona-voiced review + accounting table +
  structured findings appendix.
- `<out>/run-summary.json` — run id, persona, timestamps, per-phase and total
  tokens/cost, the findings list, turn counts.

The output layer is behind `report.ReportSink`; `FileReportSink` writes to disk
today, and Slice 5/6 swaps in a MongoDB Atlas sink with no runner change.

## Run it

```bash
# One persona:
python -m qa_agents --persona margaret --out ./qa-runs

# Every persona, under one shared run id (what the sandbox CronJob runs):
python -m qa_agents --all

# A named subset, also under one shared run:
python -m qa_agents --personas margaret,priya
```

`--persona` / `--out` override `QA_PERSONA` / `QA_OUT_DIR`. Everything else is
env-driven.

### Single vs. multi-persona — one shared run

`--persona` runs one persona; if `QA_SINK=atlas` it stamps that run's totals
itself. `--all` / `--personas` is the **orchestrator** (`orchestrator.py`,
Slice 5): it generates **one** `run_id`, builds **one** `AtlasReportSink`, runs
every selected persona's two-phase loop in sequence against that single sink,
and calls the sink's `finish()` **exactly once** at the end — so all the
personas land in one `qa_runs` document. It then posts **one** Discord
run-summary alert (`QA_DISCORD_WEBHOOK_URL`; a no-op if unset). The harness
never opens a GitHub issue — that is a human action in the review UI (#626).

### On-demand runs in the sandbox — the suspended CronJob

There is **no GitHub Actions trigger workflow.** The on-demand mechanism is a
**suspended CronJob** (`k8s/sandbox/qa-agents-cronjob.yaml`): `suspend: true`
with a schedule that never fires, so it exists only as a Job template. An
operator triggers a run with:

```bash
kubectl create job --from=cronjob/qa-agents qa-run-$(date +%s) -n slyreply-sandbox
```

The Job, in order: an init container (backend image) **drops the sandbox
`slyreply` database and re-runs the standard seeders + the sandbox-extra step**
(the same logic and ConfigMap as `seed-job.yaml`), then the main container
(harness image) runs `python -m qa_agents --all`. The run writes to the Atlas
`slyreply_qa` store and posts a Discord alert; a maintainer then reviews it in
the review UI (`kubectl -n slyreply-qa port-forward svc/qa-review 8000:8000`).

### Max billing (#894)

Every run bills the LLM usage against the operator's personal Claude Code Max
subscription, never the org's Anthropic API budget. The org-API backend was
removed entirely: `runner._options_env` **unconditionally** scrubs
`ANTHROPIC_API_KEY` to the empty string in the spawned `claude` CLI's env, so
the CLI always falls through to OAuth (via `CLAUDE_CODE_OAUTH_TOKEN` in-cluster,
or Keychain / `~/.claude` on a laptop). There is no per-run billing selector —
in the harness, the review-UI trigger, or the CronJob.

The cluster CronJob runs at `QA_HARNESS_CONCURRENCY=1` (mandatory in Max mode —
keeps you inside the rolling session window and obviously-supervised) and
`backoffLimit: 0` (a Max failure must not auto-retry and burn through your
session quota).

> **Note:** there is no QA Anthropic API key any more. The org-API-keyed
> background analysis paths (persona-memory distillation, the cross-run
> insights analyzer, canonicalisation, and the timeline organiser) were
> retired, so `ANTHROPIC_API_KEY` is no longer mounted into the pod or read
> by any QA code. The only Anthropic credential the QA system holds is the
> Claude Code Max OAuth token. The `_options_env` scrub stays as a defensive
> belt-and-braces override so an *inherited* key can never leak into the
> spawned `claude` CLI.

**One-time setup**, on a laptop logged into your personal Max account:

```bash
make qa-claude-token     # wraps `claude setup-token`, caches the token
make infra-apply         # Terraform lands the token as a k8s Secret
```

`qa-claude-token` writes the long-lived OAuth token to
`~/.config/slyreply/claude-code-token` (mode 600, never enters
`terraform.tfvars`). The Terraform `data.local_sensitive_file` data source
reads it at plan time; `kubernetes_secret.qa_claude_code_credentials` lands
it as `qa-claude-code-credentials` in the `slyreply-sandbox` namespace.

**Triggering a run**:

```bash
make qa-cluster-run-max
```

The target creates a Job from the suspended CronJob template, picking up the
cached token from the Secret. Follow logs the usual way:

```bash
kubectl -n slyreply-sandbox logs -f job/qa-max-<timestamp> -c harness
```

**Rotation**: `claude setup-token` invalidates the previous token whenever it
mints a new one. When the cluster Job starts 401-ing (the harness's existing
run-failure path posts a Discord alert), rotate with the same two commands:

```bash
make qa-claude-token && make infra-apply
```

**ToS reality check**: this is for operator-initiated, supervised runs. Don't
turn the schedule on. A scheduled-cron Max workload from a fixed server IP is
the prototypical pattern Anthropic polices on personal-use plans; if your
personal Max account gets flagged, you lose Claude Code for your own work too,
not just the QA Job.

## Fair-use cooldown — two ways to exercise it

Per the epic, the fair-use cooldown can be tripped two ways:

1. **Organically** — the `priya` persona's flow includes a deliberate
   *fair-use abuse run*: she sends a burst of ~8 emails to her own agent. The
   Slice 2 sandbox seeds **low** thresholds (`fair_use_burst_threshold: 5`), so
   the burst trips the cooldown for real and she documents what the user sees.
   This needs no extra tooling — just the `send_email` tool, repeatedly.

2. **The fast path — `qa_agents.fair_use_override`.** A standalone harness
   **utility** (not an agent tool — a persona must never mutate the system it
   reviews). It connects to the sandbox MongoDB and force-sets a user's
   `fair_use.cooldown_until` to a near-future time, so the cooldown *messaging*
   (the "agents are resting" wall) can be exercised in seconds, with no burst:

   ```bash
   # Put a user into a 60-minute cooldown (default):
   python -m qa_agents.fair_use_override priya.raghunathan@example.com

   # Custom lead time:
   python -m qa_agents.fair_use_override --minutes 30 someone@example.com
   ```

   The user is matched on `registered_emails`; Mongo connection comes from
   `QA_MONGODB_URL`. Exit code is non-zero if no such user exists.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | — | **Required in-cluster.** Claude Code Max OAuth token the spawned `claude` CLI authenticates with. On a laptop the CLI falls through to Keychain / `~/.claude` instead. |
| `QA_PERSONA` | `margaret` | Persona id to run. |
| `QA_WEB_BASE_URL` | `http://localhost:5173` | SlyReply frontend the agent opens. |
| `QA_SMTP_HOST` | `localhost` | Inbound SMTP host (the aiosmtpd service). |
| `QA_SMTP_PORT` | `1025` | Inbound SMTP port. |
| `QA_MAILPIT_URL` | `http://localhost:8025` | Mailpit base URL (HTTP API). |
| `QA_EXPLORE_MODEL` | `claude-sonnet-4-6` | Model for the explore loop. |
| `QA_REPORT_MODEL` | `claude-opus-4-7` | Model for the review. |
| `QA_MAX_TURNS` | `60` | Per-phase turn budget. |
| `QA_RUN_TIMEOUT_S` | `1800` | Whole-run wall-clock budget. |
| `QA_OUT_DIR` | `./qa-runs` | Where the review + summary are written. |
| `QA_ADMIN_EMAIL` | `admin@sandbox.slyreply.ai` | Admin login email for the `tomas` persona. |
| `QA_ADMIN_PASSWORD` | `testpass123` | Admin login password for `tomas`. |
| `QA_MONGODB_URL` | `mongodb://mongodb:27017/slyreply` | Sandbox MongoDB — used **only** by the fair-use override utility, not the agent loop. |
| `QA_SINK` | `file` | Report sink: `file` (markdown + JSON to a dir) or `atlas` (the shared `slyreply_qa` store). |
| `QA_RUN_ID` | — | Shared run id grouping every persona of one orchestrated job. Generated by the Atlas sink if unset. |
| `QA_STORE_URL` | `mongodb://localhost:27017` | MongoDB connection for the Atlas sink. |
| `QA_STORE_DB` | `slyreply_qa` | Atlas store database name. |
| `QA_DISCORD_WEBHOOK_URL` | — | Discord webhook for the orchestrator's run-summary alert. Empty → the alert is a no-op. |

Slice 5 promotes the model vars to Terraform variables that render into
these same env vars — the names are stable for that reason.

The `QA_PRICE_*` price-table vars were retired in #1822 along with the
per-run dollar conversion: every run bills the operator's flat-rate Claude
Code Max subscription, so the harness tracks raw token totals only. Any
`QA_PRICE_*` env still present in a deploy is ignored.

## Development

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/pytest
```

Tests cover the deterministic parts only — MIME decoding, accounting math,
report rendering, persona config. They make **no real Anthropic API calls** and
mock the Mailpit HTTP layer.

## Manual end-to-end procedure

The harness is built and statically verified, but a **live `margaret` run** is
a post-merge step (it needs the `claude` CLI authenticated against a Claude
Code Max account; the cloud sandbox of Slice 2/5 is not deployed yet). To run
it by hand against a local full dev stack:

1. **Bring up the app stack with an inbound SMTP server and a mail sink.** The
   dev compose (`docker-compose.dev.yml`) runs only the FastAPI backend — it has
   no inbound SMTP. The Slice 1 spike adds exactly that. From the repo root:

   ```bash
   docker compose -p slyreply-qa \
     -f docker-compose.dev.yml \
     -f qa-agents/spike/docker-compose.spike.yml up --wait
   ```

   This gives you: MongoDB, the backend, the frontend (Vite on `:5173`), the
   inbound `smtp-inbound` service on `:1025`, and a mail sink on `:8025`.

   > The spike's compose uses **MailHog** (`/api/v1`, `/api/v2`). The harness's
   > email tool targets **Mailpit**'s `/api/v1` API. For a faithful end-to-end
   > run, point `smtp-inbound`'s outbound relay at a Mailpit container and set
   > `QA_MAILPIT_URL` to it. The sandbox in Slice 2 standardises on Mailpit;
   > until then, either run a local Mailpit (`docker run -p 8025:8025 -p
   > 1025:1025 axllent/mailpit`) as the relay target, or accept that the
   > round-trip read needs the Mailpit API specifically.

2. **Seed a registered user for the persona.** Inbound mail is sender-is-auth
   and `email_verified`-gated. `margaret`'s registered address is
   `margaret.doyle@example.com` (see `personas.py`; all signup personas use
   `@example.com` local parts). For a *fresh* run she signs up and verifies
   through the UI herself; if you want to skip straight to the email
   round-trip, seed her like the spike's `seed_spike.py` does (a user with
   that address in `registered_emails`, `email_verified: true`, plus a UID she
   owns). `tomas` does not sign up — see "Persona admin credentials" above.

3. **Run the harness** pointed at the local stack. The spawned `claude` CLI
   authenticates against your Claude Code Max account (Keychain / `~/.claude`
   on a laptop, or `CLAUDE_CODE_OAUTH_TOKEN` in-cluster):

   ```bash
   python -m qa_agents \
     --persona margaret \
     --out ./qa-runs \
     # QA_WEB_BASE_URL / QA_SMTP_* / QA_MAILPIT_URL default to the local stack
   ```

4. **Read the output** — `./qa-runs/margaret-review.md` (the review) and
   `./qa-runs/run-summary.json` (tokens, cost, findings).

5. Tear down: `docker compose -p slyreply-qa ... down -v`.

## Docker

```bash
docker build -t qa-agents:dev .
docker run --rm \
  -e CLAUDE_CODE_OAUTH_TOKEN \
  -e QA_WEB_BASE_URL -e QA_SMTP_HOST -e QA_SMTP_PORT -e QA_MAILPIT_URL \
  -v "$PWD/qa-runs:/app/qa-runs" \
  qa-agents:dev --persona margaret
```

The image carries Python 3.12, Node.js, the pinned `@playwright/mcp` server
installed globally, and a bundled Chromium installed at a fixed
`PLAYWRIGHT_BROWSERS_PATH` (`/ms-playwright`). The browser is provisioned at
build time — see the `Dockerfile` header for why HOME/`npx`-cached browsers
break at runtime.
