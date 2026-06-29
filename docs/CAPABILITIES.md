# Capabilities — granting Test Ease deeper access

> Status: **design** (agreed direction; build is phased — see the end).

## The idea

A site can let Test Ease *see and do* more — and the more it can, the more like
an **insider** it tests, and the fewer false alarms it raises. A **capability**
is something the operator can *grant* (a test login, a sandbox inbox, read-only
logs, a read-only DB, kubectl access…). Each grant turns a **symptom into a
verified fact**:

- Without logs: *"the page errored."* With logs: *"500 on `POST /checkout`,
  `NullPointer at OrderService:42`, request id `abc`."*
- Without a DB read: *"I think signup worked."* With it: *"the user row was
  created but the welcome-email row is missing — silent failure."*
- Without a test inbox: *"couldn't finish signup (needs email verification)."*
  With it: the whole flow completes.

This is **not a new subsystem** — it's the **questionnaire generalised**, sitting
on top of the pieces already built:
- a grant *is* a questionnaire answer — usually a **secret → the vault** (pointer only);
- each granted capability **lights up an MCP tool** for the harness (the same MCP
  catalog the personas already use: email, openapi, a11y, cost, chrome-devtools);
- the **explorer proposes, the operator grants** — the product's
  "**affordance discovery, not intrusion**" principle, made concrete.

## The access ladder (how "more access = better" is explained)

Testing **depth** is the spine. Each rung adds capabilities and converts guesses
into proof. The operator always sees their current depth + the next unlock.

| Lvl | Depth | Grant | Unlocks |
|----|-------|-------|---------|
| 0 | **Black-box** | nothing (a URL) | Anonymous clicking — surface UX / visual / a11y. |
| 1 | **Authenticated** | test login(s) per role | Past auth → real flows. |
| 2 | **Instrumented inputs** | sandbox inbox, test cards, sandbox mode | Complete signup / verification / checkout end-to-end. |
| 3 | **Observability (read)** | logs, error tracking, traces, metrics | Root-caused findings, not symptoms. |
| 4 | **State verification (read)** | read-only DB, admin/read API, object store | *Verify* backend effects; catch silent data bugs. |
| 5 | **Environment control** | kubectl/exec, feature flags, seed/reset, test clock | Set preconditions, drive time, toggle variants, inspect runtime. |

**Earn-trust / progressive:** the explorer asks only for the **next** rung. Higher
rungs are shown as *locked previews* ("reach L3 to unlock root-cause findings")
so the operator sees the value of going deeper **without being asked for the keys
up front**.

## The catalog

Each capability has: `level` (0–5), `category`, `risk_class`
(`sandbox-only` / `read-only` / `prod-read` / `write-control`), `grant_kind`
(what the operator provides → drives the connect form: `secret` / `url` /
`connection` / `file` / `none`), and an optional `proposed_when` hint the
explorer matches against what it found.

### Identity & access — *L1*
- `test-account` — test login(s), one per role (user / admin / …). `secret`.
- `sso-sandbox` — a sandbox SSO/OAuth app for federated login. `connection`.
- `sandbox-tenant` — a throwaway org/workspace to test in. `connection`.
- `api-token` — a personal access / API token. `secret`.

### Email & messaging — *L2* (`sandbox-only`)
- `sandbox-inbox` — catch-all inbox to read verification codes / reset links
  (Mailpit / Mailosaur — **Test Ease already ships Mailpit**). `connection`.
- `sandbox-sending` — outbound mail the persona must trigger / inspect. `connection`.
- `sms-sandbox` — OTP / 2FA over SMS (Twilio test creds). `secret`.
- `webhook-capture` — inspect outbound webhooks the site fires. `url`.

### Payments — *L2* (`sandbox-only`)
- `payments-sandbox` — processor sandbox + test cards (Stripe / Revolut). `connection`.
- `test-clock` — advance subscription lifecycle / drive billing time. `connection`.
- `entitlement-seed` — seed coupons / plans / entitlements. `url`.

### API & contracts — *L2–4*
- `openapi-spec` — OpenAPI / GraphQL schema to exercise + contract-test
  (**Test Ease has an openapi tool**). `url` / `file`.
- `api-access` — API base URL + auth to drive endpoints directly. `secret`.

### Observability (read) — *L3* (`read-only`)
- `app-logs` — read application logs / a log-stream endpoint. `secret`.
- `error-tracking` — Sentry / Rollbar read API → attach stack traces to findings. `secret`.
- `apm-metrics` — APM / metrics read (Datadog / Grafana) → perf regressions. `secret`.
- `request-tracing` — correlate a persona action to a server trace id. `connection`.

### Data & state (read) — *L4* (`read-only`, often `prod-read`)
- `readonly-db` — a read-only DB connection → verify state, catch corruption. `secret`.
- `admin-read-api` — admin/read API to query domain objects. `secret`.
- `object-store-read` — verify uploads / generated files. `secret`.
- `search-index-read` — verify indexing. `secret`.

### Environment & control — *L5* (`write-control` — strongest)
- `kube-exec` — a kube context to read pods/logs + exec (you did this). `connection`.
- `feature-flags` — toggle flags to test variants (LaunchDarkly…). `secret`.
- `seed-reset` — seed / reset / factory endpoints for preconditions. `url`.
- `time-control` — a test clock / time-travel hook. `url`.
- `preview-deploys` — CI / per-PR preview environments to test. `connection`.

### Context (no creds, big payoff) — *any level*
- `repo-read` — the explorer reads code to learn *intent* → fewer by-design
  false flags. `connection`.
- `internal-docs` — runbooks / design docs / specs. `connection` / `file`.
- `issue-tracker-read` — known issues, so it doesn't re-file them. `secret`.
- `product-analytics` — which flows real users take → prioritise testing. `secret`.

### Custom — *the open tail*
- `custom` — anything bespoke: a god-mode debug console, a GraphQL admin, an
  MCP server the site exposes. The operator (or the explorer) **describes it +
  connects it**. This is how the catalog handles the infinite tail without
  enumerating it.

## Data model

**Catalog as data** (extensible without code) — a `capability_catalog`
collection, **seeded** on boot from a baseline list (like personas are seeded),
and operator-extendable (custom entries). One row per capability with the fields
above.

**Grants** — a `site_capabilities` collection keyed by
`(tenant_id, target_id, capability_id)`:
- `status`: `proposed` | `granted` | `declined` | `not_applicable`
- `credential_ref`: vault pointer (when the grant carried a secret) — never inline
- `config`: non-secret connection config (a base URL, a db host, scope notes)
- `proposed_by`: `explorer` | `operator`; timestamps.

**Depth score** — derived per target:
- `depth_level` = max `level` among `granted` capabilities (0 = black-box).
- `depth_label` = the rung name.
- `next_unlock` = the highest-impact `proposed`/available capability at
  `depth_level + 1` (the CTA).
- plus counts ("3 of 7 proposed granted") for progress within a rung.

## How the explorer feeds it

During exploration it already detects affordances. It additionally **proposes
capabilities**: for each catalog entry whose `proposed_when` matches what it saw
(a login form → `test-account`; `/pricing` → `payments-sandbox`; a `/health` or
`/metrics` endpoint → `app-logs`/`apm-metrics`; an OpenAPI link → `openapi-spec`),
it writes a `site_capabilities` row at `status=proposed`. **Earn-trust:** it
proposes the current + next rung; deeper rungs surface as locked previews.

## UX — a distinct "level up your testing" hub

Capabilities gets a **dedicated, prominent surface** (more than a 5th data tab):

1. **A "Testing depth" pill in the site header** (next to the lifecycle badge),
   always visible: *"Depth: Authenticated · level up →"*. It's the persistent
   hook and jumps to the Capabilities view.
2. **A Capabilities tab whose hero is the ladder**, not a list: the 6 rungs with
   the current one lit, each rung's capabilities as cards. A card sells the
   **unlock** ("Read-only logs → root-cause errors instead of reporting them"),
   carries a **risk badge**, and a **[Connect]** action (a small form that vaults
   the secret / saves the config) or **[Not applicable]** (so it stops asking).
3. **Locked previews** for higher rungs — visible but greyed, with their unlock
   value shown, so the operator *wants* to climb without being pressured.
4. **A "Connect a custom capability" escape hatch** for the open tail (describe
   + URL + auth → vault), and "point at an MCP server".
5. **Consent is the spine**: nothing is used until granted; every grant is scoped
   (the risk badge) + revocable; the card states exactly what it lets the
   personas do.

## Phased build

- **P1 — data + score (store):** `capability_catalog` (seed + the baseline
  catalog above) + `site_capabilities` (grants, vault-backed) + the depth-score
  derivation. Tests.
- **P2 — API + the Capabilities tab:** list catalog/grants, the depth rollup,
  grant/decline/connect (secret → vault), the custom escape hatch. The header
  depth pill + the ladder-hero UI. Playwright proof.
- **P3 — explorer proposes:** the explorer writes `proposed` grants (earn-trust
  ordering) during a pass; the tab shows the tailored, ranked shortlist + locked
  previews.
- **P4 — harness consumes:** a granted capability lights up its MCP tool for the
  personas/explorer (wires into the MCP catalog).

Open question carried into P2: the exact visual of the **depth score** —
qualitative rungs vs. an explicit numeric/level score (agreed: yes to a score;
final treatment decided when we see it in the UI).
