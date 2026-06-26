# Test Ease — Playwright e2e

Self-contained Playwright smoke tests for the Test Ease SPA. The
`/api/**` layer is mocked per-test (`tests/fixtures.ts`), so these tests
run **without** a real FastAPI backend or MongoDB instance — they're
exercising the SPA's wiring, not the persistence layer (that's covered
by `qa-agents/review-ui/api/tests/test_api.py`).

## Run locally

```bash
cd qa-agents/review-ui/e2e
npm ci
npm run install-browsers  # one-off: downloads the chromium binary
npm test                  # spawns Vite dev server in cwd=../web and runs
```

To watch:
```bash
npm run test:ui
```

To debug a single test in a real browser window:
```bash
npm run test:headed -- tests/persona-library.spec.ts
```

## What's covered

- `app-shell.spec.ts` — branding, sidebar, nav routing
- `persona-library.spec.ts` — list, click into detail, tab switching,
  edit + save, danger-zone variant for default personas

## What's NOT covered

- Scenario drag-drop builder — drag interactions are slow to test
  reliably across browsers; a follow-up adds focused interaction tests
  there.
- Run-detail timeline — needs a richer mock harness with run-step
  data; deferred.
- Transcript search — scheduled for the polish pass. (The memory cockpit
  and insights views were retired along with the org-API-key analysis
  features.)

## Adding tests

1. Drop a `*.spec.ts` under `tests/`.
2. Call `installApiMocks(page)` in `beforeEach` to get the canned
   `/api/**` layer plus a `state` object you can mutate.
3. Override individual routes after the install call when a test needs
   a different response shape (Playwright matches routes in reverse
   registration order, so later registrations win).
