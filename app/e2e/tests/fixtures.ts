// Shared Playwright fixtures + mock-API installer.
//
// Every spec calls `installApiMocks(page)` in beforeEach. The function
// registers a single page.route('/api/**') handler that maps requests
// to canned responses. Tests can override individual endpoints by
// adding their own page.route() AFTER the install call — Playwright
// matches routes in reverse registration order, so the test-specific
// override wins.

import { Page, expect } from '@playwright/test'

export interface MockPersona {
  persona_id: string
  display_name: string
  registered_email: string
  explore_system_prompt: string
  report_system_prompt: string
  flows: string[]
  uses_admin_login: boolean
  setup_actions: string | null
  browser_locale: string | null
  color_token: string
  avatar_seed: string
  is_default: boolean
  hidden: boolean
  // #1009 — activation gate. Default False; operator flips per-tenant
  // via the Personas page. Trigger flow defaults to the active set.
  is_active: boolean
  created_at?: string
  updated_at?: string
}

export const SAMPLE_PERSONAS: MockPersona[] = [
  {
    persona_id: 'margaret',
    display_name: 'Margaret Doyle',
    registered_email: 'margaret@example.com',
    explore_system_prompt: 'You are Margaret, a Sheffield bookkeeper…',
    report_system_prompt: 'Write a review as Margaret would.',
    flows: ['signup', 'billing', 'password-reset'],
    uses_admin_login: false,
    setup_actions: null,
    browser_locale: 'en-GB',
    color_token: 'teal',
    avatar_seed: 'margaret',
    is_default: true,
    hidden: false,
    is_active: false,
  },
  {
    persona_id: 'daniel',
    display_name: 'Daniel Lee',
    registered_email: 'daniel@example.com',
    explore_system_prompt: 'You are Daniel, a chat-native creative…',
    report_system_prompt: 'Write a review as Daniel.',
    flows: ['signup', 'agent-create'],
    uses_admin_login: false,
    setup_actions: null,
    browser_locale: null,
    color_token: 'amber',
    avatar_seed: 'daniel',
    is_default: true,
    hidden: false,
    is_active: false,
  },
]

export const SAMPLE_RUNS = [
  {
    run_id: 'qa-run-001',
    status: 'reviewed',
    started_at: '2026-05-25T12:00:00Z',
    finished_at: '2026-05-25T12:30:00Z',
    personas: ['margaret', 'daniel'],
    run_notes: 'Smoke run',
    finding_counts: { blocker: 1, major: 2 },
    // #1029 — MCP servers used summary. Empty in this sample; the
    // run-detail mock below overrides this with a populated list for
    // the trigger-flow e2e tests.
    mcp_servers_used: [],
    totals: {
      input_tokens: 1000, output_tokens: 500, cache_tokens: 0,
      cost_usd: 0.5, real_cost_usd: 0.5, backend: 'api',
    },
  },
]

// Single-run detail mock used by the GET /api/runs/{id} handler — the
// list endpoint returns the slimmer SAMPLE_RUNS shape, the detail
// endpoint returns this expanded one with reviews + findings + the
// #1029 MCP chip data.
//
// #1049/#1050 — findings include a non-empty list so the Slice B sticky
// panel renders, and one finding maps to a real step in
// SAMPLE_RUN_TIMELINE (via the finding_id ordinal → step.finding_ordinals).
export const SAMPLE_RUN_DETAIL = {
  ...SAMPLE_RUNS[0],
  reviews: [
    { persona: 'margaret', review_markdown: 'Sample review from Margaret.', verdict: 'Cautiously yes.' },
    { persona: 'daniel', review_markdown: 'Sample review from Daniel.', verdict: 'Probably.' },
  ],
  findings: [
    {
      finding_id: 'qa-run-001:margaret:1',
      persona: 'margaret',
      severity: 'blocker',
      kind: 'bug',
      category: 'bug',
      title: 'Verification email never arrives at Mailpit',
      body: 'Persona waited 60s; the API returned 404.',
      status: 'open',
    },
    {
      finding_id: 'qa-run-001:margaret:2',
      persona: 'margaret',
      severity: 'minor',
      kind: 'bug',
      category: 'copy',
      title: 'Footer placeholder text still says «TBC»',
      body: 'Unfilled company-number / address.',
      status: 'open',
    },
    // #1169 — extend the fixture with a major + a praise so the
    // Triage view's three-section split (blocker / major / other)
    // and the slice-2 kind/severity separation both have data to
    // exercise without needing per-test fixtures.
    {
      finding_id: 'qa-run-001:margaret:3',
      persona: 'margaret',
      severity: 'major',
      kind: 'bug',
      category: 'bug',
      title: 'Profile avatar upload returns 500 on >2MB JPEGs',
      body: 'Persona uploaded a 3.4MB selfie; got "Server Error" with no recovery path.',
      status: 'open',
    },
    {
      finding_id: 'qa-run-001:margaret:4',
      persona: 'margaret',
      severity: 'nit',
      kind: 'praise',
      category: 'surprise',
      title: 'Empty-state illustration on /agents is delightful',
      body: 'Persona noted the hand-drawn cat. Not a fix; just a 🌟.',
      status: 'open',
    },
    // #1171 — regression finding for slice-3 STILL-BROKEN badge
    // assertions. is_regression=true means a previously-fixed bug
    // came back; last_verified_run_id points the operator at the
    // run that the persona thought it was fixed in. The badge
    // becomes a router-link to that run on the Triage view.
    {
      finding_id: 'qa-run-001:margaret:5',
      persona: 'margaret',
      severity: 'major',
      kind: 'bug',
      category: 'bug',
      title: 'Re-verify [qa-run-000:1] STILL BROKEN — login button does nothing on Safari',
      body: 'Persona retested the Safari fix from qa-run-000 and it still fails silently.',
      status: 'open',
      is_regression: true,
      last_verified_run_id: 'qa-run-000',
    },
  ],
  mcp_servers_used: [
    { server: 'playwright', calls: 47 },
    { server: 'email', calls: 3 },
    { server: 'findings', calls: 12 },
  ],
}

// #1049 — timeline events exercising the Slice A dedup logic. The
// pair (log, step) carries the same text; the renderer must show only
// the step entry. The second step's `finding_ordinals: [1]` makes it
// the target of jumpToFinding(SAMPLE_RUN_DETAIL.findings[0]).
// #1054 — Slice F. AI-organised view doc used by the timeline-
// organiser spec to assert the overlay renders. Step numbers line up
// with SAMPLE_RUN_TIMELINE below (step #62 + #67 are real events the
// fixture creates).
export const SAMPLE_TIMELINE_VIEW = {
  run_id: 'qa-run-001',
  generated_at: '2026-05-25T12:05:00Z',
  model: 'claude-haiku-4-5',
  schema_version: 1,
  summary: 'Margaret signed up cleanly and reached /profile, then filed a verification-email blocker on /inbox.',
  phases: [
    {
      id: 0,
      name: 'Sign-up attempt',
      summary: 'Filled the signup form and submitted.',
      start_step: 61,
      end_step: 67,
      important_step: 67,
      tone: 'frustrated',
      finding_ids: ['qa-run-001:margaret:1'],
    },
    {
      id: 1,
      name: 'Profile exploration',
      summary: 'Walked the post-signup dashboard.',
      start_step: 68,
      end_step: 71,
      important_step: null,
      tone: 'neutral',
      finding_ids: [],
    },
  ],
  highlights: [
    {
      step_n: 67,
      why: 'Filed a blocker: verification email never arrives at Mailpit',
      category: 'finding',
    },
    {
      step_n: 62,
      why: 'Snapshot of /login showed a clear error state',
      category: 'decision',
    },
  ],
}

export const SAMPLE_RUN_TIMELINE = {
  events: [
    {
      // #1053 — navigate event opens phase 1 ("/login"). Two phases
      // total in this fixture (with the second navigate below) so the
      // phase ribbon has something to render.
      kind: 'step',
      persona_id: 'margaret',
      step_n: 61,
      ts: '2026-05-25T11:59:59Z',
      tool: 'browser_navigate',
      tool_name: 'mcp__playwright__browser_navigate',
      args_summary: 'url=https://sandbox.slyreply.ai/login',
      finding_ordinals: [],
    },
    {
      kind: 'log',
      persona_id: 'margaret',
      ts: '2026-05-25T12:00:00Z',
      content: 'Still on /login — let me see what the page is showing.',
      seq: 1,
    },
    {
      kind: 'step',
      persona_id: 'margaret',
      step_n: 62,
      ts: '2026-05-25T12:00:01Z',
      tool: 'browser_take_screenshot',
      text_from_persona: 'Still on /login — let me see what the page is showing.',
      screenshot_id: 'stub-screenshot-1',
      finding_ordinals: [],
    },
    {
      kind: 'log',
      persona_id: 'margaret',
      ts: '2026-05-25T12:00:02Z',
      content: 'Filed a finding about the verification email.',
      seq: 2,
    },
    {
      kind: 'step',
      persona_id: 'margaret',
      step_n: 67,
      ts: '2026-05-25T12:00:03Z',
      tool: 'note_finding',
      text_from_persona: 'Filed a finding about the verification email.',
      finding_ordinals: [1],
    },
    {
      // #1053 — second navigate opens phase 2 ("/profile"). All later
      // events (the standalone log + pure-screenshot + pure-tool below)
      // belong to this phase.
      kind: 'step',
      persona_id: 'margaret',
      step_n: 68,
      ts: '2026-05-25T12:00:03.5Z',
      tool: 'browser_navigate',
      tool_name: 'mcp__playwright__browser_navigate',
      args_summary: 'url=https://sandbox.slyreply.ai/profile',
      finding_ordinals: [],
    },
    {
      // Genuine standalone log (no paired step). Should still render.
      kind: 'log',
      persona_id: 'margaret',
      ts: '2026-05-25T12:00:04Z',
      content: 'This is a standalone narrative log line.',
      seq: 3,
    },
    {
      // #1051 — Pure screenshot step (no narration). Lets the Slice C
      // tests exercise the Screenshots chip in isolation.
      // #1078 Slice 3 — also carries the prose summary the recorder
      // emits post-Slice-1 ("Captured screenshot"), so Slice 3 tests
      // can verify the redundant-prose suppression rule.
      kind: 'step',
      persona_id: 'margaret',
      step_n: 70,
      ts: '2026-05-25T12:00:05Z',
      tool: 'browser_take_screenshot',
      summary: 'Captured screenshot',
      screenshot_id: 'stub-screenshot-2',
      finding_ordinals: [],
    },
    {
      // #1051 — Pure tool call (no narration, no finding, no
      // screenshot). The "noise floor" category — hidden by default
      // filters; visible only when the Tools chip is ticked.
      kind: 'step',
      persona_id: 'margaret',
      step_n: 71,
      ts: '2026-05-25T12:00:06Z',
      tool: 'browser_click',
      finding_ordinals: [],
    },
  ],
}

export const SAMPLE_CATALOG = {
  categories: ['auth', 'billing'],
  actions: [
    { id: 'sign-up-pro', category: 'auth', human_description: 'Sign up as Pro' },
    { id: 'cancel-sub', category: 'billing', human_description: 'Cancel subscription' },
  ],
}

// Slice 1 of #1002 — sample discovered_* rows. Two actions across two
// categories, one tool, one branch — enough to exercise both tabs.
export const SAMPLE_DISCOVERED_ACTIONS = [
  {
    doc_id: 'qa-run-001:margaret:auth.signup',
    run_id: 'qa-run-001', persona_id: 'margaret',
    action_id: 'auth.signup', category: 'auth',
    human_description: 'Sign up for a new account',
    url_seen: '/signup',
    evidence: 'Persona filled the signup form',
    branches_noticed: ['Decline T&C not tried'],
  },
  {
    doc_id: 'qa-run-001:daniel:billing.upgrade',
    run_id: 'qa-run-001', persona_id: 'daniel',
    action_id: 'billing.upgrade', category: 'billing',
    human_description: 'Upgrade to a paid plan via Revolut',
    branches_noticed: [],
  },
]
export const SAMPLE_DISCOVERED_TOOLS = [
  {
    doc_id: 'qa-run-001:margaret:mailpit',
    run_id: 'qa-run-001', persona_id: 'margaret',
    name: 'mailpit', purpose: 'verify signup email arrived',
  },
]
export const SAMPLE_DISCOVERED_BRANCHES = [
  {
    doc_id: 'qa-run-001:margaret:branch-1',
    run_id: 'qa-run-001', persona_id: 'margaret',
    description: 'Persona saw a Cancel button but never clicked it',
  },
]

// Slice B of #1028 — MCP catalog the /mcp-tools view fetches on mount,
// and the RunDetail chip layer uses to swap raw server ids for display
// names. Three baseline servers matching qa_agents/mcp_catalog.py's
// shipping catalog.
export const SAMPLE_MCP_SERVERS = [
  {
    id: 'playwright',
    display_name: 'Playwright (browser automation)',
    description: 'Persona browser eyes and hands.',
    default_enabled: true,
    persona_compat: [],
    tool_count: 22,
  },
  {
    id: 'email',
    display_name: 'Email (Mailpit read + SMTP send)',
    description: 'Verification mail read + persona-to-app email send.',
    default_enabled: true,
    persona_compat: [],
    tool_count: 3,
  },
  {
    id: 'findings',
    display_name: 'Findings recorder',
    description: 'How the persona files what it saw.',
    default_enabled: true,
    persona_compat: [],
    tool_count: 1,
  },
]

/**
 * #1089 Slice A — Insights fixtures. Two rows: one unfiled (the e2e
 * exercises the "File as GitHub issue" button against this one),
 * one already-filed (regression guard that the "Filed as #N" link
 * renders directly without going through the button).
 */
export const SAMPLE_INSIGHTS = [
  {
    insight_id: 'ins-unfiled-1',
    category: 'ux',
    severity: 'major',
    title: 'Signup form rejects spaces in passwords',
    body: 'Three personas hit the same wall on the signup form.',
    evidence: [
      { run_id: 'qa-A', persona: 'margaret', snippet: 'rejected on submit' },
    ],
    status: 'new',
    generated_at: '2026-05-26T09:00:00Z',
    gh_issue_url: null,
    gh_issue_number: null,
  },
  {
    insight_id: 'ins-filed-1',
    category: 'bug',
    severity: 'minor',
    title: 'Verification email subject is empty',
    body: 'Mailpit shows a blank subject on the verification template.',
    evidence: [],
    status: 'actioned',
    generated_at: '2026-05-25T09:00:00Z',
    gh_issue_url: 'https://github.com/mccullya/slyreply/issues/4200',
    gh_issue_number: 4200,
    // #1089 Slice B — already-synced state, fresh enough that the
    // lazy refresh on GET wouldn't normally re-fire.
    gh_issue_state: 'open',
    gh_issue_state_synced_at: new Date().toISOString(),
  },
]

/**
 * Register a default /api/* mock layer.
 *
 * Returns a `state` object the spec can mutate (e.g. push a new persona
 * into state.personas) so the canned data evolves across actions
 * within one test.
 */
export async function installApiMocks(page: Page) {
  const state = {
    personas: structuredClone(SAMPLE_PERSONAS),
    runs: structuredClone(SAMPLE_RUNS),
    catalog: structuredClone(SAMPLE_CATALOG),
    scenarios: [] as Array<Record<string, unknown>>,
    insights: structuredClone(SAMPLE_INSIGHTS) as Array<Record<string, unknown>>,
    discoveredActions: structuredClone(SAMPLE_DISCOVERED_ACTIONS),
    discoveredTools: structuredClone(SAMPLE_DISCOVERED_TOOLS),
    discoveredBranches: structuredClone(SAMPLE_DISCOVERED_BRANCHES),
    // #1018 / #1031 — captured trigger POSTs so the trigger-flow
    // and mcp-selection specs can assert the body Test Ease built
    // (personas, target_url, enabled_mcp_servers). Each entry is
    // the parsed request body in order of submission.
    triggerCalls: [] as Array<Record<string, unknown>>,
  }

  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    const method = route.request().method()

    // --- runs --------------------------------------------------------
    if (path === '/api/runs' && method === 'GET') {
      return route.fulfill({ json: state.runs })
    }
    if (path === '/api/runs/active' && method === 'GET') {
      return route.fulfill({ json: { active: null } })
    }
    if (path === '/api/runs/personas' && method === 'GET') {
      return route.fulfill({
        json: { personas: state.personas.map((p) => p.persona_id) },
      })
    }
    if (path === '/api/runs/coverage-catalog' && method === 'GET') {
      return route.fulfill({ json: state.catalog })
    }
    // #1018 / #1031 — capture POSTs so trigger-flow + mcp-selection
    // specs can assert payload shape end-to-end (target_url,
    // enabled_mcp_servers, etc). Returning a stub job_name keeps the
    // SPA's busy state happy without spawning a real Job.
    if (path === '/api/runs/trigger' && method === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}')
      state.triggerCalls.push(body)
      return route.fulfill({
        json: { job_name: 'qa-ui-stub-e2e', personas: body.personas || [] },
      })
    }
    // #1029 — single run detail. Match /api/runs/<id> as long as it's
    // not one of the more-specific paths above. SAMPLE_RUN_DETAIL ships
    // a populated mcp_servers_used so the chip-render e2e can pin on it.
    const runDetailMatch = path.match(/^\/api\/runs\/([^/]+)$/)
    if (runDetailMatch && method === 'GET') {
      const runId = runDetailMatch[1]
      if (runId === SAMPLE_RUN_DETAIL.run_id) {
        return route.fulfill({ json: SAMPLE_RUN_DETAIL })
      }
      return route.fulfill({ status: 404, json: { detail: 'not found' } })
    }
    // #1049 — timeline endpoint. Returns the events list the Timeline
    // tab consumes; specs override per-test via page.route() if they
    // need custom shapes.
    const timelineMatch = path.match(/^\/api\/runs\/([^/]+)\/timeline$/)
    if (timelineMatch && method === 'GET') {
      return route.fulfill({ json: SAMPLE_RUN_TIMELINE })
    }
    // #1030 — Slice B MCP catalog. Static data; tests that need a
    // custom catalog override per-test via page.route() AFTER calling
    // installApiMocks.
    if (path === '/api/mcp-servers' && method === 'GET') {
      return route.fulfill({ json: { servers: SAMPLE_MCP_SERVERS } })
    }

    // --- personas ----------------------------------------------------
    if (path === '/api/personas' && method === 'GET') {
      const includeHidden = url.searchParams.get('include_hidden') === 'true'
      const list = includeHidden
        ? state.personas
        : state.personas.filter((p) => !p.hidden)
      return route.fulfill({ json: { personas: list } })
    }
    // #1105 — credentials status + clear. Match BEFORE the bare
    // /api/personas/{id} regex below so the more-specific path wins.
    const credsStatusMatch = path.match(/^\/api\/personas\/([^/]+)\/credentials\/status$/)
    if (credsStatusMatch && method === 'GET') {
      const id = credsStatusMatch[1]
      const persona = state.personas.find((p) => p.persona_id === id)
      if (!persona) return route.fulfill({ status: 404, json: { detail: 'not found' } })
      const creds = persona.credentials
      if (!creds) return route.fulfill({ json: { has_credentials: false } })
      return route.fulfill({
        json: {
          has_credentials: true,
          email: creds.email,
          registered_at: creds.registered_at,
          verified: !!creds.verified,
          last_rotation_n: creds.last_rotation_n || 0,
          has_session_jwt: !!creds.session_jwt,
          jwt_expires_at: creds.jwt_expires_at || null,
        },
      })
    }
    const credsDeleteMatch = path.match(/^\/api\/personas\/([^/]+)\/credentials$/)
    if (credsDeleteMatch && method === 'DELETE') {
      const id = credsDeleteMatch[1]
      const persona = state.personas.find((p) => p.persona_id === id)
      if (!persona) return route.fulfill({ status: 404, json: { detail: 'not found' } })
      delete persona.credentials
      return route.fulfill({ status: 204, body: '' })
    }
    const personaMatch = path.match(/^\/api\/personas\/([^/]+)$/)
    if (personaMatch) {
      const id = personaMatch[1]
      const idx = state.personas.findIndex((p) => p.persona_id === id)
      if (method === 'GET') {
        if (idx < 0) return route.fulfill({ status: 404, json: { detail: 'not found' } })
        return route.fulfill({ json: state.personas[idx] })
      }
      if (method === 'PATCH') {
        if (idx < 0) return route.fulfill({ status: 404, json: { detail: 'not found' } })
        const patch = JSON.parse(route.request().postData() || '{}')
        state.personas[idx] = { ...state.personas[idx], ...patch }
        return route.fulfill({ json: state.personas[idx] })
      }
      if (method === 'DELETE') {
        if (idx < 0) return route.fulfill({ status: 404, json: { detail: 'not found' } })
        if (state.personas[idx].is_default) {
          return route.fulfill({
            status: 422,
            json: { detail: 'default persona — use hidden=true' },
          })
        }
        state.personas.splice(idx, 1)
        return route.fulfill({ status: 204, body: '' })
      }
    }

    // --- scenarios ---------------------------------------------------
    if (path === '/api/scenarios' && method === 'GET') {
      return route.fulfill({ json: { scenarios: state.scenarios } })
    }

    // --- discovered_* (Slice 1 of #1002) ----------------------------
    // The category filter is the only one we filter server-side; the
    // run_id/persona_id filters could be added too but the current
    // tests don't exercise them.
    if (path === '/api/discovered-actions' && method === 'GET') {
      const cat = url.searchParams.get('category')
      const list = cat
        ? state.discoveredActions.filter((a) => a.category === cat)
        : state.discoveredActions
      return route.fulfill({ json: { actions: list, count: list.length } })
    }
    if (path === '/api/discovered-tools' && method === 'GET') {
      return route.fulfill({
        json: { tools: state.discoveredTools, count: state.discoveredTools.length },
      })
    }
    if (path === '/api/discovered-branches' && method === 'GET') {
      return route.fulfill({
        json: { branches: state.discoveredBranches, count: state.discoveredBranches.length },
      })
    }

    // --- catchall ----------------------------------------------------
    console.warn(`unmocked API call: ${method} ${path}`)
    return route.fulfill({ status: 404, json: { detail: 'unmocked' } })
  })

  return state
}

export { expect }
