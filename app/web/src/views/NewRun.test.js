// <NewRun> — the trigger console at /new-run (#1822, formerly the
// <RunControl> component on the Runs page). This suite carries forward
// the two contracts pinned by the old RunControl.test.js and adds the
// redesign's explicit-selection contracts:
//
//   1. "ui-de-modal" in-flight isolation (#1821): an active run lives in
//      its OWN isolated panel and must NOT lock the rest of the trigger
//      form — only the Launch button is guarded (server backstops 409).
//   2. multi-pod trigger controls (#1821): the "Pods" selector renders
//      next to Concurrency, the "up to N personas at once" hint computes
//      pods × concurrency, and the over-8 client-side guard disables
//      Launch and never calls triggerRun.
//   3. explicit selection (#1822): the picker pre-seeds from the
//      activated (★, is_active) persona set; the Launch label always
//      reflects the explicit selection count ("Launch N never lies");
//      an empty selection disables Launch and shows the
//      persona-selection-hint.
//   4. inline activation (#1822): the per-card star PATCHes is_active
//      via updatePersona, optimistically flips, and reverts on error.
//
// The component eagerly hits several endpoints + may open an EventSource
// on mount. We mock the whole ../api module so no network is touched;
// individual tests override the mocks (via setup()) to flip between
// "idle" and "running" or change the activated set.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'

// Every named export NewRun pulls from ../api. The mount-time loaders
// resolve to stub values so the form mounts deterministically.
const mocks = {
  getActiveRun: vi.fn(),
  getPersonas: vi.fn(),
  listPersonas: vi.fn(),
  listMCPServers: vi.fn(),
  getCoverageCatalog: vi.fn(),
  triggerRun: vi.fn(),
  createScenario: vi.fn(),
  deleteScenario: vi.fn(),
  listScenarios: vi.fn(),
  listSiteTargets: vi.fn(),
  getTargetMcp: vi.fn(),
  getRunAvailability: vi.fn(),
  updatePersona: vi.fn(),
}

vi.mock('../api', () => ({
  ACTIVE_LOGS_URL: '/api/runs/active/logs',
  getActiveRun: (...a) => mocks.getActiveRun(...a),
  getPersonas: (...a) => mocks.getPersonas(...a),
  listPersonas: (...a) => mocks.listPersonas(...a),
  listMCPServers: (...a) => mocks.listMCPServers(...a),
  getCoverageCatalog: (...a) => mocks.getCoverageCatalog(...a),
  triggerRun: (...a) => mocks.triggerRun(...a),
  createScenario: (...a) => mocks.createScenario(...a),
  deleteScenario: (...a) => mocks.deleteScenario(...a),
  listScenarios: (...a) => mocks.listScenarios(...a),
  listSiteTargets: (...a) => mocks.listSiteTargets(...a),
  getTargetMcp: (...a) => mocks.getTargetMcp(...a),
  getRunAvailability: (...a) => mocks.getRunAvailability(...a),
  updatePersona: (...a) => mocks.updatePersona(...a),
}))

vi.mock('../lib/apiError', () => ({
  formatApiError: (e) => (e && e.message) || 'error',
}))

// #1822 follow-up — NewRun reads ?personas= via useRoute() for the
// "Run this persona" deep link. Tests set `routeQuery` before mount.
let routeQuery = {}
vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/new-run', query: routeQuery }),
}))

import NewRun from './NewRun.vue'

const PERSONAS = [
  { id: 'alice', display_name: 'Alice', archetype: 'signup', region: 'GB', language: 'en' },
  { id: 'bob', display_name: 'Bob', archetype: 'mobile', region: 'US', language: 'en' },
]

const ACTIVE_RUN = {
  run_id: 'qa-ui-max-123',
  job_name: 'qa-ui-max-123',
  pod_name: 'qa-ui-max-123-abc',
  phase: 'explore',
}

const DB_PERSONAS_ALL_ACTIVE = [
  { persona_id: 'alice', is_active: true, hidden: false },
  { persona_id: 'bob', is_active: true, hidden: false },
]
const DB_PERSONAS_NONE_ACTIVE = [
  { persona_id: 'alice', is_active: false, hidden: false },
  { persona_id: 'bob', is_active: false, hidden: false },
]

function setup({ active = null, dbPersonas = DB_PERSONAS_ALL_ACTIVE } = {}) {
  mocks.getActiveRun.mockResolvedValue(active)
  mocks.getPersonas.mockResolvedValue(PERSONAS)
  // Deep-clone: the component flips is_active on the rows in place (the
  // optimistic ★ toggle), so sharing the module-level arrays across
  // tests would leak state between them.
  mocks.listPersonas.mockResolvedValue(structuredClone(dbPersonas))
  mocks.listMCPServers.mockResolvedValue([])
  mocks.getCoverageCatalog.mockResolvedValue({ categories: [], actions: [] })
  mocks.triggerRun.mockResolvedValue({ job_name: 'qa-ui-stub' })
  mocks.createScenario.mockResolvedValue({})
  mocks.deleteScenario.mockResolvedValue(true)
  mocks.listScenarios.mockResolvedValue([])
  // A registered site so the Target field defaults to a real URL (and Launch,
  // which now requires a target, enables) — mirrors the real onMounted load.
  mocks.listSiteTargets.mockResolvedValue([
    { target_id: 'acme', base_url: 'https://acme.test' },
  ])
  mocks.getTargetMcp.mockResolvedValue({ server_ids: [], servers: [] })
  mocks.getRunAvailability.mockResolvedValue({ available: true, reason: null })
  mocks.updatePersona.mockResolvedValue({})
}

function mountNewRun() {
  return mount(NewRun, {
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
}

async function mountReady(opts) {
  if (opts) setup(opts)
  const wrapper = mountNewRun()
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  routeQuery = {}
  // Default to an idle, ready-to-launch form (both personas activated →
  // pre-seeded selection of 2). Individual tests re-call setup() before
  // mount to flip to "running" or "nothing activated".
  setup()
  // EventSource isn't implemented in happy-dom; stub a no-op so the live
  // log stream code can run without throwing.
  globalThis.EventSource = class {
    constructor() {
      this.readyState = 0
    }
    addEventListener() {}
    close() {}
  }
  globalThis.EventSource.CLOSED = 2
  // happy-dom lacks window.confirm; default to "yes".
  globalThis.confirm = vi.fn(() => true)
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('<NewRun> in-flight isolation (#1821)', () => {
  it('renders an isolated active-run panel while a run is in flight', async () => {
    const wrapper = await mountReady({ active: ACTIVE_RUN })
    const panel = wrapper.find('[data-testid="active-run-panel"]')
    expect(panel.exists()).toBe(true)
    expect(panel.text()).toContain('qa-ui-max-123')
    expect(panel.text()).toContain('explore')
  })

  it('shows no active-run panel when idle', async () => {
    const wrapper = await mountReady({ active: null })
    expect(wrapper.find('[data-testid="active-run-panel"]').exists()).toBe(false)
  })

  it('keeps the form interactive during an active run — only Launch is disabled', async () => {
    const wrapper = await mountReady({ active: ACTIVE_RUN })

    // The Launch button is the ONLY guarded control.
    const start = wrapper.find('[data-testid="cta-start"]')
    expect(start.attributes('disabled')).toBeDefined()
    expect(start.text()).toContain('Run in progress')

    // Everything else stays live: the target URL field, the persona
    // filter, the pods input — none carry a disabled attribute…
    for (const sel of [
      'input[aria-label="Target URL"]',
      '[data-testid="persona-filter"]',
      '[data-testid="pod-count-input"]',
    ]) {
      const el = wrapper.find(sel)
      expect(el.exists()).toBe(true)
      expect(el.attributes('disabled')).toBeUndefined()
    }

    // …and the persona picker still toggles selection (line up the next
    // run while this one finishes).
    const aliceCard = wrapper
      .findAll('.persona-card')
      .find((c) => c.text().includes('Alice'))
    expect(aliceCard.attributes('aria-pressed')).toBe('true')
    await aliceCard.trigger('click')
    expect(aliceCard.attributes('aria-pressed')).toBe('false')
  })

  it('leaves the Launch button enabled when idle with a selection', async () => {
    const wrapper = await mountReady({ active: null })
    const start = wrapper.find('[data-testid="cta-start"]')
    expect(start.attributes('disabled')).toBeUndefined()
  })

  it('defaults the target to a registered site (not a hardcoded URL)', async () => {
    const wrapper = await mountReady({ active: null })
    const field = wrapper.find('input[aria-label="Target URL"]')
    expect(field.element.value).toBe('https://acme.test')
    // The retired slyreply sandbox default must be gone everywhere.
    expect(wrapper.html()).not.toContain('slyreply')
  })

  it('disables Launch when there is no target and blocks the run', async () => {
    setup({ active: null })
    mocks.listSiteTargets.mockResolvedValue([]) // no sites added yet (override default)
    const wrapper = mountNewRun()
    await flushPromises()
    expect(wrapper.find('input[aria-label="Target URL"]').element.value).toBe('')
    const start = wrapper.find('[data-testid="cta-start"]')
    expect(start.attributes('disabled')).toBeDefined()
    await start.trigger('click')
    await flushPromises()
    expect(mocks.triggerRun).not.toHaveBeenCalled()
  })
})

describe('<NewRun> — #1821 pods selector', () => {
  it('renders a Pods number input alongside Concurrency', async () => {
    const wrapper = await mountReady()
    const pods = wrapper.find('[data-testid="pod-count-input"]')
    expect(pods.exists()).toBe(true)
    expect(pods.attributes('max')).toBe('4')
    expect(pods.attributes('min')).toBe('1')
    // Concurrency is still present — pods sits next to it, not instead of it.
    expect(wrapper.text()).toContain('Concurrency')
    expect(wrapper.text()).toContain('Pods')
  })

  it('hint computes pods × concurrency ("up to N personas at once")', async () => {
    const wrapper = await mountReady()
    // Defaults: pods omitted (→1), concurrency omitted (→1) ⇒ up to 1.
    expect(wrapper.find('[data-testid="pods-hint"]').text()).toContain(
      'up to 1 persona at once',
    )
    // 3 pods × 2 concurrency = 6.
    wrapper.vm.podCount = 3
    wrapper.vm.concurrency = 2
    await flushPromises()
    expect(wrapper.find('[data-testid="pods-hint"]').text()).toContain(
      'up to 6 personas at once',
    )
  })

  it('disables Launch when pods × concurrency exceeds the 8 ceiling', async () => {
    const wrapper = await mountReady()
    const start = wrapper.find('[data-testid="cta-start"]')
    // Under the ceiling — enabled.
    wrapper.vm.podCount = 4
    wrapper.vm.concurrency = 2 // 4 × 2 = 8, exactly at the ceiling → allowed
    await flushPromises()
    expect(start.attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-testid="pods-ceiling-error"]').exists()).toBe(false)

    // Over the ceiling — disabled + error surfaced.
    wrapper.vm.concurrency = 3 // 4 × 3 = 12 > 8
    await flushPromises()
    expect(start.attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="pods-ceiling-error"]').exists()).toBe(true)
  })

  it('does not call triggerRun while the ceiling guard is active', async () => {
    const wrapper = await mountReady()
    wrapper.vm.podCount = 4
    wrapper.vm.concurrency = 3 // 12 > 8
    await flushPromises()
    // The button is disabled, but assert the guard directly: a click on a
    // disabled button is a no-op in the DOM; ceilingExceeded gates it.
    expect(wrapper.vm.ceilingExceeded).toBe(true)
    await wrapper.find('[data-testid="cta-start"]').trigger('click')
    expect(mocks.triggerRun).not.toHaveBeenCalled()
  })
})

describe('<NewRun> — #1822 explicit selection pre-seeded from activation', () => {
  it('pre-seeds the selection from activated (is_active) personas', async () => {
    setup({
      dbPersonas: [
        { persona_id: 'alice', is_active: true, hidden: false },
        { persona_id: 'bob', is_active: false, hidden: false },
      ],
    })
    const wrapper = mountNewRun()
    await flushPromises()

    // Only alice is activated → only alice is pre-selected.
    const cards = wrapper.findAll('.persona-card')
    const alice = cards.find((c) => c.text().includes('Alice'))
    const bob = cards.find((c) => c.text().includes('Bob'))
    expect(alice.attributes('aria-pressed')).toBe('true')
    expect(bob.attributes('aria-pressed')).toBe('false')

    // The Launch label reflects the explicit selection count — 1.
    const start = wrapper.find('[data-testid="cta-start"]')
    expect(start.text()).toBe('▶ Launch 1')
    expect(start.attributes('disabled')).toBeUndefined()
  })

  it('Launch label reflects the full activated set (count 2)', async () => {
    const wrapper = await mountReady() // both activated by default setup
    expect(wrapper.find('[data-testid="cta-start"]').text()).toBe('▶ Launch 2')
    expect(wrapper.text()).toContain('2 of 2 selected')
  })

  it('pre-seeds from the catalog default-on set when nobody is activated', async () => {
    // A fresh operator has activated nobody (is_active=false) but the catalog
    // marks personas is_default — so New Run still arrives launch-ready.
    setup({
      dbPersonas: [
        { persona_id: 'alice', is_active: false, is_default: true, hidden: false },
        { persona_id: 'bob', is_active: false, is_default: false, hidden: false },
      ],
    })
    const wrapper = mountNewRun()
    await flushPromises()
    // alice (default) pre-selected → Launch is live; bob (not default) is not.
    const start = wrapper.find('[data-testid="cta-start"]')
    expect(start.text()).toBe('▶ Launch 1')
    expect(start.attributes('disabled')).toBeUndefined()
  })

  it('hidden personas never pre-seed even when is_active', async () => {
    setup({
      dbPersonas: [
        { persona_id: 'alice', is_active: true, hidden: true },
        { persona_id: 'bob', is_active: true, hidden: false },
      ],
    })
    const wrapper = mountNewRun()
    await flushPromises()
    expect(wrapper.find('[data-testid="cta-start"]').text()).toBe('▶ Launch 1')
  })

  it('empty selection disables Launch and shows the persona-selection-hint', async () => {
    const wrapper = await mountReady({ dbPersonas: DB_PERSONAS_NONE_ACTIVE })

    const start = wrapper.find('[data-testid="cta-start"]')
    expect(start.attributes('disabled')).toBeDefined()
    // The label never lies: nothing selected → bare "Launch", no count.
    expect(start.text()).toBe('▶ Launch')
    expect(wrapper.find('[data-testid="persona-selection-hint"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="persona-selection-hint"]').text()).toContain(
      'Nothing selected',
    )
  })

  it('hint disappears and Launch enables once a card is picked', async () => {
    const wrapper = await mountReady({ dbPersonas: DB_PERSONAS_NONE_ACTIVE })
    const aliceCard = wrapper
      .findAll('.persona-card')
      .find((c) => c.text().includes('Alice'))
    await aliceCard.trigger('click')

    expect(wrapper.find('[data-testid="persona-selection-hint"]').exists()).toBe(false)
    const start = wrapper.find('[data-testid="cta-start"]')
    expect(start.attributes('disabled')).toBeUndefined()
    expect(start.text()).toBe('▶ Launch 1')
  })

  it('clicking Launch with an empty selection never calls triggerRun', async () => {
    const wrapper = await mountReady({ dbPersonas: DB_PERSONAS_NONE_ACTIVE })
    await wrapper.find('[data-testid="cta-start"]').trigger('click')
    expect(mocks.triggerRun).not.toHaveBeenCalled()
  })
})

describe('<NewRun> — #1822 inline activation star', () => {
  it('star PATCHes is_active toggled via updatePersona (deactivate)', async () => {
    const wrapper = await mountReady() // alice is_active=true
    const star = wrapper.find('[data-testid="persona-default-toggle-alice"]')
    expect(star.text()).toBe('★')

    await star.trigger('click')
    await flushPromises()

    expect(mocks.updatePersona).toHaveBeenCalledWith('alice', { is_active: false })
    expect(star.text()).toBe('☆')
  })

  it('star PATCHes is_active toggled via updatePersona (activate)', async () => {
    const wrapper = await mountReady({ dbPersonas: DB_PERSONAS_NONE_ACTIVE })
    const star = wrapper.find('[data-testid="persona-default-toggle-bob"]')
    expect(star.text()).toBe('☆')

    await star.trigger('click')
    await flushPromises()

    expect(mocks.updatePersona).toHaveBeenCalledWith('bob', { is_active: true })
    expect(star.text()).toBe('★')
  })

  it('activating via the star does NOT mutate the current explicit selection', async () => {
    // Activation decides the pre-seed for FUTURE page loads; the run
    // itself always sends the explicit selection.
    const wrapper = await mountReady({ dbPersonas: DB_PERSONAS_NONE_ACTIVE })
    await wrapper.find('[data-testid="persona-default-toggle-alice"]').trigger('click')
    await flushPromises()
    // alice is now activated but the selection stays empty → Launch
    // stays disabled.
    expect(wrapper.find('[data-testid="cta-start"]').attributes('disabled')).toBeDefined()
  })

  it('reverts the optimistic flip and surfaces an error when the PATCH fails', async () => {
    setup()
    mocks.updatePersona.mockRejectedValue(new Error('boom'))
    const wrapper = mountNewRun()
    await flushPromises()

    const star = wrapper.find('[data-testid="persona-default-toggle-alice"]')
    expect(star.text()).toBe('★')

    await star.trigger('click')
    await flushPromises()

    // PATCH attempted with the toggled value…
    expect(mocks.updatePersona).toHaveBeenCalledWith('alice', { is_active: false })
    // …but the row reverted to its original state on failure.
    expect(star.text()).toBe('★')
    expect(wrapper.text()).toContain('Could not deactivate alice')
  })
})

describe('<NewRun> — trigger POST always carries the explicit personas array', () => {
  it('sends the pre-seeded selection on Launch', async () => {
    const wrapper = await mountReady() // both activated → both selected
    await wrapper.find('[data-testid="cta-start"]').trigger('click')
    await flushPromises()
    expect(mocks.triggerRun).toHaveBeenCalledTimes(1)
    expect(mocks.triggerRun.mock.calls[0][0]).toEqual(['alice', 'bob'])
  })
})

describe('<NewRun> — ?personas= deep link (#1822 follow-up)', () => {
  it('query selection wins over the activated-set pre-seed', async () => {
    routeQuery = { personas: 'bob' }
    const wrapper = await mountReady() // both activated, but query says bob
    const start = wrapper.find('[data-testid="cta-start"]')
    expect(start.text()).toContain('Launch 1')
    await start.trigger('click')
    await flushPromises()
    expect(mocks.triggerRun.mock.calls[0][0]).toEqual(['bob'])
  })

  it('drops unknown ids and accepts comma lists', async () => {
    routeQuery = { personas: 'ghost,bob,alice' }
    const wrapper = await mountReady({ dbPersonas: DB_PERSONAS_NONE_ACTIVE })
    expect(wrapper.find('[data-testid="cta-start"]').text()).toContain('Launch 2')
  })

  it('an all-unknown query falls back to the activated pre-seed', async () => {
    routeQuery = { personas: 'ghost' }
    const wrapper = await mountReady() // both activated
    expect(wrapper.find('[data-testid="cta-start"]').text()).toContain('Launch 2')
  })
})

describe('<NewRun> — P4 capability-driven MCP auto-enable', () => {
  it('forwards the resolved target_id so the server can auto-enable tools', async () => {
    const wrapper = await mountReady() // defaults target to the acme site
    await wrapper.find('[data-testid="cta-start"]').trigger('click')
    await flushPromises()
    expect(mocks.triggerRun.mock.calls[0][1].targetId).toBe('acme')
  })

  it('shows an "auto-enabled tools" note when the site has granted access', async () => {
    setup({ active: null })
    mocks.getTargetMcp.mockResolvedValue({
      server_ids: ['openapi'],
      servers: [{ server_id: 'openapi', display_name: 'OpenAPI surface explorer', friendly_name: 'API probing tool', capabilities: ['openapi-spec'] }],
    })
    const wrapper = mountNewRun()
    await flushPromises()
    const note = wrapper.find('[data-testid="auto-mcp-note"]')
    expect(note.exists()).toBe(true)
    // Plain, benefit-oriented name — not the "OpenAPI surface explorer" jargon.
    expect(note.text()).toContain('API probing tool')
    expect(note.text()).not.toContain('OpenAPI surface explorer')
  })

  it('hides the note when the site has no granted access', async () => {
    const wrapper = await mountReady() // getTargetMcp default → no servers
    expect(wrapper.find('[data-testid="auto-mcp-note"]').exists()).toBe(false)
  })
})

describe('<NewRun> — run availability (local-first has no cluster)', () => {
  it('warns and disables Launch when runs cannot be dispatched', async () => {
    setup({ active: null })
    mocks.getRunAvailability.mockResolvedValue({
      available: false, reason: 'Persona runs execute on a Kubernetes cluster…',
    })
    const wrapper = mountNewRun()
    await flushPromises()
    const banner = wrapper.find('[data-testid="runs-unavailable-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Kubernetes cluster')
    // Launch is blocked even with a valid target + selection.
    expect(wrapper.find('[data-testid="cta-start"]').attributes('disabled')).toBeDefined()
  })

  it('no banner and Launch live when runs are available', async () => {
    const wrapper = await mountReady({ active: null })
    expect(wrapper.find('[data-testid="runs-unavailable-banner"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="cta-start"]').attributes('disabled')).toBeUndefined()
  })
})
