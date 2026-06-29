// <SiteTarget> — the Questions tab (explorer questionnaire + lifecycle).
//
// The component eagerly loads target/surfaces/flows/knowledge/questions on
// mount; we mock the whole ../api.js so nothing touches the network and assert
// the questionnaire contracts: render grouped questions, mask secret answers,
// answer/skip call the API, and the lifecycle select drives setTargetLifecycle.

import { describe, expect, it, vi, beforeEach } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'

const mocks = {
  getSiteTarget: vi.fn(),
  listSiteSurfaces: vi.fn(),
  listSiteFlows: vi.fn(),
  listSiteKnowledge: vi.fn(),
  listSiteQuestions: vi.fn(),
  answerSiteQuestion: vi.fn(),
  skipSiteQuestion: vi.fn(),
  setTargetLifecycle: vi.fn(),
  createSiteKnowledge: vi.fn(),
  updateSiteKnowledge: vi.fn(),
  deleteSiteKnowledge: vi.fn(),
  exploreSiteTarget: vi.fn(),
  getSiteCapabilities: vi.fn(),
  setCapability: vi.fn(),
  addCustomCapability: vi.fn(),
  revokeCapability: vi.fn(),
}

vi.mock('../api.js', () => ({
  getSiteTarget: (...a) => mocks.getSiteTarget(...a),
  listSiteSurfaces: (...a) => mocks.listSiteSurfaces(...a),
  listSiteFlows: (...a) => mocks.listSiteFlows(...a),
  listSiteKnowledge: (...a) => mocks.listSiteKnowledge(...a),
  listSiteQuestions: (...a) => mocks.listSiteQuestions(...a),
  answerSiteQuestion: (...a) => mocks.answerSiteQuestion(...a),
  skipSiteQuestion: (...a) => mocks.skipSiteQuestion(...a),
  setTargetLifecycle: (...a) => mocks.setTargetLifecycle(...a),
  createSiteKnowledge: (...a) => mocks.createSiteKnowledge(...a),
  updateSiteKnowledge: (...a) => mocks.updateSiteKnowledge(...a),
  deleteSiteKnowledge: (...a) => mocks.deleteSiteKnowledge(...a),
  exploreSiteTarget: (...a) => mocks.exploreSiteTarget(...a),
  getSiteCapabilities: (...a) => mocks.getSiteCapabilities(...a),
  setCapability: (...a) => mocks.setCapability(...a),
  addCustomCapability: (...a) => mocks.addCustomCapability(...a),
  revokeCapability: (...a) => mocks.revokeCapability(...a),
}))

import SiteTarget from './SiteTarget.vue'

const QUESTIONNAIRE = {
  questions: [
    {
      question_id: 'admin-email', text: 'Admin email?', kind: 'free_text',
      category: 'auth', rationale: 'to log in', required: true,
      status: 'open', answer: null, credential_ref: null, options: [],
    },
    {
      question_id: 'admin-pw', text: 'Admin password?', kind: 'secret',
      category: 'auth', status: 'answered', answer: null,
      credential_ref: 'vault://default/acme/q-admin-pw', options: [],
    },
    {
      question_id: 'has-api', text: 'Is there an API?', kind: 'boolean',
      category: 'api', status: 'open', answer: null, options: [],
    },
  ],
  status: { total: 3, answered: 1, open: 2, skipped: 0, required_open: 1 },
  lifecycle: 'awaiting-answers',
  lifecycle_states: [
    'registered', 'exploring', 'awaiting-answers', 'configured', 'testing', 're-explore',
  ],
}

function mountTarget() {
  return mount(SiteTarget, {
    props: { targetId: 'acme' },
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
}

async function openQuestionsTab(wrapper) {
  const tab = wrapper.findAll('button').find((b) => b.text().startsWith('Questions'))
  await tab.trigger('click')
}

beforeEach(() => {
  for (const m of Object.values(mocks)) m.mockReset()
  mocks.getSiteTarget.mockResolvedValue({ display_name: 'Acme', base_url: 'https://a.test' })
  mocks.listSiteSurfaces.mockResolvedValue([])
  mocks.listSiteFlows.mockResolvedValue([])
  mocks.listSiteKnowledge.mockResolvedValue([])
  mocks.listSiteQuestions.mockResolvedValue(structuredClone(QUESTIONNAIRE))
  mocks.answerSiteQuestion.mockResolvedValue({})
  mocks.skipSiteQuestion.mockResolvedValue({})
  mocks.setTargetLifecycle.mockResolvedValue({})
  mocks.exploreSiteTarget.mockResolvedValue({
    lifecycle: 'awaiting-answers', fetched: true, title: 'Acme',
    detected: ['login'], counts: { surfaces: 2, flows: 1, knowledge: 1, questions: 3 },
  })
  mocks.getSiteCapabilities.mockResolvedValue(CAPS())
  mocks.setCapability.mockImplementation(async () => CAPS({ depth_level: 3 }))
  mocks.addCustomCapability.mockResolvedValue(CAPS())
  mocks.revokeCapability.mockResolvedValue(CAPS())
})

function CAPS(over = {}) {
  return {
    depth: {
      depth_level: over.depth_level ?? 0, depth_label: 'Black-box',
      levels: ['Black-box', 'Authenticated', 'Instrumented inputs', 'Observability', 'State verification', 'Environment control'],
      granted_count: 0, next_unlock: { capability_id: 'test-account', title: 'Test account login(s)', unlocks: 'Get past auth.', level: 1 },
    },
    capabilities: [
      { capability_id: 'test-account', title: 'Test account login(s)', category: 'identity', level: 1, risk_class: 'sandbox-only', grant_kind: 'secret', unlocks: 'Get past auth.', status: 'available' },
      { capability_id: 'app-logs', title: 'Application logs (read)', category: 'observability', level: 3, risk_class: 'read-only', grant_kind: 'secret', unlocks: 'Root-cause errors.', status: 'available' },
      { capability_id: 'kube-exec', title: 'Kubernetes access', category: 'environment', level: 5, risk_class: 'write-control', grant_kind: 'connection', unlocks: 'Runtime inspection.', status: 'available' },
    ],
  }
}

describe('<SiteTarget> Questions tab', () => {
  it('shows the lifecycle badge + rollup and renders grouped questions', async () => {
    const wrapper = mountTarget()
    await flushPromises()
    expect(wrapper.find('[data-testid="lifecycle-badge"]').text()).toBe('awaiting-answers')
    await openQuestionsTab(wrapper)
    expect(wrapper.find('[data-testid="questions-tab"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="q-rollup"]').text()).toContain('1/3 answered')
    expect(wrapper.find('[data-testid="q-rollup"]').text()).toContain('1 required open')
    expect(wrapper.find('[data-testid="q-admin-email"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="q-has-api"]').exists()).toBe(true)
  })

  it('masks a vaulted secret answer (never shows the value)', async () => {
    const wrapper = mountTarget()
    await flushPromises()
    await openQuestionsTab(wrapper)
    const answered = wrapper.find('[data-testid="q-admin-pw-answer"]')
    expect(answered.text()).toContain('vaulted')
    // An answered secret shows the mask, not an input to re-enter the value.
    expect(wrapper.find('[data-testid="q-admin-pw-input"]').exists()).toBe(false)
  })

  it('answers an open question through the API and refreshes', async () => {
    const wrapper = mountTarget()
    await flushPromises()
    await openQuestionsTab(wrapper)
    await wrapper.find('[data-testid="q-admin-email-input"]').setValue('ops@acme.test')
    await wrapper.find('[data-testid="q-admin-email-submit"]').trigger('click')
    await flushPromises()
    expect(mocks.answerSiteQuestion).toHaveBeenCalledWith('acme', 'admin-email', 'ops@acme.test')
    expect(mocks.listSiteQuestions).toHaveBeenCalledTimes(2) // mount + refresh
  })

  it('skips an open question through the API', async () => {
    const wrapper = mountTarget()
    await flushPromises()
    await openQuestionsTab(wrapper)
    // has-api is the open question with a Skip button.
    const skipBtn = wrapper
      .find('[data-testid="q-has-api"]')
      .findAll('button')
      .find((b) => b.text() === 'Skip')
    await skipBtn.trigger('click')
    await flushPromises()
    expect(mocks.skipSiteQuestion).toHaveBeenCalledWith('acme', 'has-api')
  })

  it('drives the target lifecycle from the select', async () => {
    const wrapper = mountTarget()
    await flushPromises()
    await openQuestionsTab(wrapper)
    await wrapper.find('[data-testid="lifecycle-select"]').setValue('configured')
    await flushPromises()
    expect(mocks.setTargetLifecycle).toHaveBeenCalledWith('acme', 'configured')
  })
})

describe('<SiteTarget> explorer', () => {
  function registered() {
    return { questions: [], status: { total: 0, answered: 0, open: 0, skipped: 0, required_open: 0 },
      lifecycle: 'registered', lifecycle_states: QUESTIONNAIRE.lifecycle_states }
  }

  it('shows the Explore button while registered, hides it once configured', async () => {
    mocks.listSiteQuestions.mockResolvedValue(registered())
    const w = mountTarget()
    await flushPromises()
    expect(w.find('[data-testid="explore-btn"]').exists()).toBe(true)
    // The default fixture (awaiting-answers) is past the explore stage.
    mocks.listSiteQuestions.mockResolvedValue(structuredClone(QUESTIONNAIRE))
    const w2 = mountTarget()
    await flushPromises()
    expect(w2.find('[data-testid="explore-btn"]').exists()).toBe(false)
  })

  it('runs the explorer and reloads the model', async () => {
    mocks.listSiteQuestions.mockResolvedValue(registered())
    const w = mountTarget()
    await flushPromises()
    mocks.listSiteQuestions.mockClear()
    await w.find('[data-testid="explore-btn"]').trigger('click')
    await flushPromises()
    expect(mocks.exploreSiteTarget).toHaveBeenCalledWith('acme')
    expect(mocks.listSiteQuestions).toHaveBeenCalled() // reloaded after exploring
    expect(w.find('[data-testid="explore-summary"]').exists()).toBe(true)
  })
})

describe('<SiteTarget> capabilities', () => {
  async function openCapsTab(w) {
    await w.findAll('button').find((b) => b.text().startsWith('Capabilities')).trigger('click')
  }

  it('shows the depth pill + ladder and the catalog grouped by level', async () => {
    const w = mountTarget()
    await flushPromises()
    expect(w.find('[data-testid="depth-pill"]').text()).toContain('Black-box')
    await openCapsTab(w)
    expect(w.find('[data-testid="capabilities-tab"]').exists()).toBe(true)
    expect(w.find('[data-testid="depth-ladder"]').exists()).toBe(true)
    expect(w.find('[data-testid="cap-test-account"]').exists()).toBe(true)
    expect(w.find('[data-testid="cap-app-logs"]').exists()).toBe(true)
  })

  it('connects (grants) a capability with a vaulted secret', async () => {
    const w = mountTarget()
    await flushPromises()
    await openCapsTab(w)
    await w.find('[data-testid="cap-test-account-connect"]').trigger('click')
    await w.find('[data-testid="cap-test-account-input"]').setValue('admin:pw')
    await w.find('[data-testid="cap-test-account-save"]').trigger('click')
    await flushPromises()
    expect(mocks.setCapability).toHaveBeenCalledWith('acme', 'test-account', { status: 'granted', token: 'admin:pw' })
  })

  it('adds a custom capability', async () => {
    const w = mountTarget()
    await flushPromises()
    await openCapsTab(w)
    await w.find('[data-testid="cap-custom-open"]').trigger('click')
    await w.find('[data-testid="cap-custom-title"]').setValue('Admin GraphQL')
    await w.find('[data-testid="cap-custom-add"]').trigger('click')
    await flushPromises()
    expect(mocks.addCustomCapability).toHaveBeenCalledWith('acme', expect.objectContaining({ title: 'Admin GraphQL' }))
  })

  it('hides sensitive infra (L4-5) behind an Advanced-access toggle', async () => {
    const w = mountTarget()
    await flushPromises()
    await openCapsTab(w)
    // L1/L3 visible; the alarming L5 (kube) is hidden until you expand.
    expect(w.find('[data-testid="cap-test-account"]').exists()).toBe(true)
    expect(w.find('[data-testid="cap-kube-exec"]').exists()).toBe(false)
    await w.find('[data-testid="advanced-toggle"]').trigger('click')
    expect(w.find('[data-testid="cap-kube-exec"]').exists()).toBe(true)
  })
})
