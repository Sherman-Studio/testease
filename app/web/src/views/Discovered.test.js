// <Discovered> — corpus-wide coverage browser. Component test focuses
// on the wiring contract: tabs work, search filters client-side,
// category filter re-fetches server-side.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

// Mock the API module BEFORE importing the component.
vi.mock('../api.js', () => ({
  listDiscoveredActions: vi.fn(),
  listDiscoveredTools: vi.fn(),
  listDiscoveredBranches: vi.fn(),
}))

import Discovered from './Discovered.vue'
import {
  listDiscoveredActions,
  listDiscoveredBranches,
  listDiscoveredTools,
} from '../api.js'

const ACTIONS = [
  {
    doc_id: 'r:p:auth.signup',
    run_id: 'r1', persona_id: 'margaret',
    action_id: 'auth.signup', category: 'auth',
    human_description: 'Sign up for a new account',
    url_seen: '/signup',
    evidence: 'persona filled the form',
    branches_noticed: ['Decline T&C not tried'],
  },
  {
    doc_id: 'r:p:billing.upgrade',
    run_id: 'r1', persona_id: 'daniel',
    action_id: 'billing.upgrade', category: 'billing',
    human_description: 'Upgrade to a paid plan',
    branches_noticed: [],
  },
]
const TOOLS = [
  { doc_id: 'r:p:mailpit', run_id: 'r1', persona_id: 'margaret', name: 'mailpit', purpose: 'verify' },
]
const BRANCHES = [
  { doc_id: 'r:p:branch-1', run_id: 'r1', persona_id: 'margaret', description: 'Cancel not tried' },
]

beforeEach(() => {
  listDiscoveredActions.mockResolvedValue({ actions: ACTIONS, count: ACTIONS.length })
  listDiscoveredTools.mockResolvedValue({ tools: TOOLS, count: TOOLS.length })
  listDiscoveredBranches.mockResolvedValue({ branches: BRANCHES, count: BRANCHES.length })
})

describe('<Discovered>', () => {
  it('renders the actions tab by default with the count badge', async () => {
    const wrapper = mount(Discovered)
    await flushPromises()
    expect(wrapper.text()).toContain('Sign up for a new account')
    expect(wrapper.text()).toContain('Upgrade to a paid plan')
    // Tabs render counts
    expect(wrapper.text()).toMatch(/Actions\s*2/)
    expect(wrapper.text()).toMatch(/Tool calls\s*1/)
    expect(wrapper.text()).toMatch(/Unexplored\s*1/)
  })

  it('switches to tools tab and shows tool rows', async () => {
    const wrapper = mount(Discovered)
    await flushPromises()
    const toolsTab = wrapper
      .findAll('button')
      .find((b) => b.text().startsWith('Tool calls'))
    await toolsTab.trigger('click')
    expect(wrapper.text()).toContain('mailpit')
  })

  it('switches to branches tab and shows branch descriptions', async () => {
    const wrapper = mount(Discovered)
    await flushPromises()
    const branchesTab = wrapper
      .findAll('button')
      .find((b) => b.text().startsWith('Unexplored'))
    await branchesTab.trigger('click')
    expect(wrapper.text()).toContain('Cancel not tried')
  })

  it('search filters actions client-side (no extra API call)', async () => {
    const wrapper = mount(Discovered)
    await flushPromises()
    listDiscoveredActions.mockClear()

    const searchInput = wrapper.find('input[placeholder*="Search"]')
    await searchInput.setValue('upgrade')

    // Search is client-side — no additional API call.
    expect(listDiscoveredActions).not.toHaveBeenCalled()
    // The matching action stays, the other doesn't.
    expect(wrapper.text()).toContain('Upgrade to a paid plan')
    expect(wrapper.text()).not.toContain('Sign up for a new account')
  })

  it('changing category re-fetches from the API', async () => {
    const wrapper = mount(Discovered)
    await flushPromises()
    listDiscoveredActions.mockClear()

    const select = wrapper.find('select')
    await select.setValue('billing')
    await flushPromises()

    expect(listDiscoveredActions).toHaveBeenCalledWith(
      expect.objectContaining({ category: 'billing' }),
    )
  })

  it('renders an empty-state when no actions are returned', async () => {
    listDiscoveredActions.mockResolvedValue({ actions: [], count: 0 })
    listDiscoveredTools.mockResolvedValue({ tools: [], count: 0 })
    listDiscoveredBranches.mockResolvedValue({ branches: [], count: 0 })
    const wrapper = mount(Discovered)
    await flushPromises()
    expect(wrapper.text()).toContain('No discovered actions yet')
  })

  it('renders an error banner when the API rejects', async () => {
    listDiscoveredActions.mockRejectedValue(
      Object.assign(new Error('boom'), {
        response: { data: { detail: 'simulated 500' } },
      }),
    )
    const wrapper = mount(Discovered)
    await flushPromises()
    expect(wrapper.text()).toContain('simulated 500')
  })
})
