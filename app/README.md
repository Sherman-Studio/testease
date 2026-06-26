# Test Ease

Persona QA workbench — manage personas, launch runs, watch live
workflow timelines, triage findings, file GitHub issues.

Originally built as the human side of the SlyReply Persona QA Agents epic
([#616]), redesigned and rebranded in [#988] / [#1000], rebuilt ground-up
around the trigger → watch → triage loop in [#1822] (see `DESIGN.md` for
the "control room" design system). Tracked for
spinout as a standalone product; current home stays under
`qa-agents/review-ui/` until the spinout lands.

[#616]: https://github.com/mccullya/slyreply/issues/616

Two parts, both in this directory:

- `api/` — a FastAPI backend over the `qa-store` package.
- `web/` — a Vue 3 + Vite SPA.

In production they ship as **one image** (`ghcr.io/mccullya/slyreply-qa-review`):
the SPA is built and the FastAPI app serves the `dist/` static files alongside
`/api`. See `Dockerfile`.

## What it does

Three primary destinations plus a ⚙ utility menu:

- **Runs** (`/`) — every QA run newest-first: live lamp, status, relative
  start time, the personas that ran, finding counts by severity, and token
  weight. Empty probe runs fold behind a disclosure row.
- **New Run** (`/new-run`) — the launch console: target URL, a persona
  picker with inline activation (★) pre-seeded from the activated set,
  parallelism, and everything else (models · turns · duration · MCP tools ·
  coverage) behind an Advanced disclosure. Presets (saved scenarios) are
  applied and saved here — the old `/scenarios` page redirects.
- **Run detail** (`/runs/:run_id`) — four tabs: **Triage** (blockers →
  major → other, plus coverage + discovered strips), **Timeline** (live
  step stream with URL spine + narration search — absorbs the old
  `/transcripts` page), **Findings**, **Review**. A **File GitHub issue**
  button composes one issue (every review + the `included` findings grouped
  by severity), creates it via the GitHub REST API, and marks the run
  `filed`.
- **Personas** (`/personas`) — the registry: activation toggles, per-persona
  settings/prompts/runs.
- **⚙ Utilities** — Discovered (site coverage map), MCP tools (server
  catalog), Admin (wipe & re-seed + audit log), API docs.

[#1822]: https://github.com/mccullya/slyreply/issues/1822

## Run it locally

You need a MongoDB the `qa-store` can reach (a local `mongod`, or an Atlas SRV
URI). Seed it by running the harness with `QA_SINK=atlas`, or hand-insert a run.

```bash
# 1. API (terminal one) — from review-ui/api
python -m venv .venv && . .venv/bin/activate
pip install -e ../../qa-store -e ".[dev]"
export QA_STORE_URL="mongodb://localhost:27017"
export QA_STORE_DB="slyreply_qa"
export GITHUB_TOKEN="ghp_..."          # only needed to file issues
uvicorn qa_review_api.app:app --factory --reload --port 8000

# 2. SPA (terminal two) — from review-ui/web
npm install
npm run dev                            # http://localhost:5173, proxies /api → :8000
```

`npm run dev` proxies `/api` to the uvicorn on `:8000` (see `vite.config.js`),
so the two halves iterate independently. For a production-shaped local check,
`npm run build` and let the API serve `dist/` (point `QA_REVIEW_SPA_DIR` at it,
or build the Docker image).

## Build the image

The build context is the **`qa-agents/` directory** — the API depends on the
sibling `qa-store/` package:

```bash
docker build -f qa-agents/review-ui/Dockerfile -t slyreply-qa-review qa-agents
docker run --rm -p 8000:8000 \
  -e QA_STORE_URL="mongodb+srv://..." \
  -e QA_STORE_DB="slyreply_qa" \
  -e GITHUB_TOKEN="ghp_..." \
  slyreply-qa-review
```

The `docker build` above is for a local check. In CI, `build-qa-review-image`
(`.github/workflows/ci.yml`) builds and pushes `ghcr.io/mccullya/slyreply-qa-review`
on every push to `main` that touches `qa-agents/review-ui/` or `qa-agents/qa-store/`,
and rewrites the image tag in `k8s/qa/kustomization.yaml` so ArgoCD picks it up.

## Environment

| Variable       | Default              | Purpose                                   |
|----------------|----------------------|-------------------------------------------|
| `QA_STORE_URL` | `mongodb://localhost:27017` | Atlas SRV URI for the `slyreply_qa` store |
| `QA_STORE_DB`  | `slyreply_qa`        | Database name                             |
| `GITHUB_TOKEN` | _(unset)_            | PAT used to create the run's GitHub issue. Without it, `POST /file-issue` returns 503. |
| `GITHUB_REPO`  | `mccullya/slyreply`  | Repo the issue is filed against           |
| `QA_REVIEW_SPA_DIR` | `<pkg>/static`  | Where the built SPA lives (set by the image) |

## Production access

There is **no ingress** for this UI — it is an internal tool. Reach it with a
port-forward against the deployment in the QA namespace:

```bash
kubectl -n slyreply-qa port-forward deploy/qa-review 8000:8000
# then open http://localhost:8000
```

The k8s manifests for the deployment live under `k8s/qa/`, and the `slyreply-qa`
namespace is provisioned by `infra/qa-review.tf`.

## API contract

| Method & path | Purpose |
|---|---|
| `GET /api/runs?limit=N` | Runs newest-first; each carries `finding_counts` by severity. |
| `GET /api/runs/{run_id}` | One run in full — `reviews` (per-persona markdown) + `findings`. |
| `PATCH /api/findings/{finding_id}` | Body `{"status": "open\|included\|dismissed"}`; returns the updated finding. |
| `POST /api/runs/{run_id}/file-issue` | Composes + creates one GitHub issue; marks the run `filed`; returns `{"gh_issue_url": "..."}`. |
