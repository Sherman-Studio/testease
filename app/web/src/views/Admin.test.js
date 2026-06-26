// <Admin> — nuclear-button page (#1146).
// Component tests focus on the destructive-confirmation contract:
// the wipe button stays disabled until "WIPE" is typed exactly; the
// PATCH fires the right payload; errors surface; the audit list
// renders the API response shapes.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

vi.mock('../api.js', () => ({
  adminWipe: vi.fn(),
  listAdminWipes: vi.fn(),
}))
vi.mock('../format.js', () => ({
  formatDate: (s) => `[date:${s || 'null'}]`,
}))
vi.mock('vue-router', () => ({
  // Stub <router-link> so the audit-row run links don't error.
  RouterLink: { template: '<a><slot/></a>' },
}))

import Admin from './Admin.vue'
import { adminWipe, listAdminWipes } from '../api.js'

const WIPES = [
  {
    wipe_id: 'abc123',
    wiped_at: '2026-05-29T12:00:00Z',
    dropped_counts: { qa_runs: 5, qa_findings: 10 },
    dropped_total: 15,
    requester_note: 'Validating Slice 3',
  },
]

beforeEach(() => {
  listAdminWipes.mockResolvedValue(WIPES)
  adminWipe.mockReset()
})


describe('<Admin> — wipe button + modal', () => {
  it('opens the modal when the wipe button is clicked', async () => {
    const wrapper = mount(Admin, { global: { components: { RouterLink: { template: '<a><slot/></a>' } } } })
    await flushPromises()
    expect(wrapper.find('[data-testid="wipe-modal"]').exists()).toBe(false)
    await wrapper.find('[data-testid="open-wipe-modal"]').trigger('click')
    expect(wrapper.find('[data-testid="wipe-modal"]').exists()).toBe(true)
  })

  it('confirm button is disabled until the operator types WIPE', async () => {
    const wrapper = mount(Admin, { global: { components: { RouterLink: { template: '<a><slot/></a>' } } } })
    await flushPromises()
    await wrapper.find('[data-testid="open-wipe-modal"]').trigger('click')
    const btn = wrapper.find('[data-testid="confirm-wipe"]')
    expect(btn.attributes('disabled')).toBeDefined()
    await wrapper.find('[data-testid="confirm-text"]').setValue('wipe')
    expect(btn.attributes('disabled')).toBeDefined()  // lowercase doesn't pass
    await wrapper.find('[data-testid="confirm-text"]').setValue('WIPE')
    expect(btn.attributes('disabled')).toBeUndefined()
  })

  it('PATCHes adminWipe with the typed confirm + note', async () => {
    adminWipe.mockResolvedValue({
      audit: { wipe_id: 'new', wiped_at: 'now', dropped_total: 7, requester_note: 'test' },
      dropped: { qa_runs: 7 },
    })
    const wrapper = mount(Admin, { global: { components: { RouterLink: { template: '<a><slot/></a>' } } } })
    await flushPromises()
    await wrapper.find('[data-testid="open-wipe-modal"]').trigger('click')
    await wrapper.find('[data-testid="confirm-text"]').setValue('WIPE')
    await wrapper.find('[data-testid="requester-note"]').setValue('test')
    await wrapper.find('[data-testid="confirm-wipe"]').trigger('click')
    await flushPromises()
    // #1108 — wipeMailpit defaults to false; the modal must not
    // silently send `true` unless the operator explicitly ticks the
    // checkbox.
    expect(adminWipe).toHaveBeenCalledWith({
      confirm: 'WIPE',
      requesterNote: 'test',
      wipeMailpit: false,
    })
  })

  it('passes wipeMailpit=true when the operator ticks the checkbox', async () => {
    adminWipe.mockResolvedValue({
      audit: {
        wipe_id: 'new', wiped_at: 'now', dropped_total: 3,
        requester_note: '', mailpit_wiped: true,
      },
      dropped: { qa_runs: 3 },
    })
    const wrapper = mount(Admin, { global: { components: { RouterLink: { template: '<a><slot/></a>' } } } })
    await flushPromises()
    await wrapper.find('[data-testid="open-wipe-modal"]').trigger('click')
    await wrapper.find('[data-testid="confirm-text"]').setValue('WIPE')
    await wrapper.find('[data-testid="wipe-mailpit"]').setValue(true)
    await wrapper.find('[data-testid="confirm-wipe"]').trigger('click')
    await flushPromises()
    expect(adminWipe).toHaveBeenCalledWith({
      confirm: 'WIPE',
      requesterNote: '',
      wipeMailpit: true,
    })
    // Result message surfaces the Mailpit confirmation.
    expect(wrapper.find('[data-testid="mailpit-wiped"]').exists()).toBe(true)
  })

  it('surfaces a Mailpit error alongside a successful Mongo wipe', async () => {
    adminWipe.mockResolvedValue({
      audit: {
        wipe_id: 'new', wiped_at: 'now', dropped_total: 2,
        requester_note: '', mailpit_wiped: false,
        mailpit_error: 'ConnectError: nope',
      },
      dropped: { qa_runs: 2 },
    })
    const wrapper = mount(Admin, { global: { components: { RouterLink: { template: '<a><slot/></a>' } } } })
    await flushPromises()
    await wrapper.find('[data-testid="open-wipe-modal"]').trigger('click')
    await wrapper.find('[data-testid="confirm-text"]').setValue('WIPE')
    await wrapper.find('[data-testid="wipe-mailpit"]').setValue(true)
    await wrapper.find('[data-testid="confirm-wipe"]').trigger('click')
    await flushPromises()
    // The Mongo wipe still landed — success line is present.
    expect(wrapper.find('[data-testid="wipe-result"]').exists()).toBe(true)
    // Mailpit error surfaced separately so the operator can retry.
    const err = wrapper.find('[data-testid="mailpit-error"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('ConnectError: nope')
  })

  it('shows a result message after a successful wipe', async () => {
    adminWipe.mockResolvedValue({
      audit: { wipe_id: 'new', wiped_at: 'now', dropped_total: 42, requester_note: '' },
      dropped: { qa_runs: 5, qa_findings: 37 },
    })
    const wrapper = mount(Admin, { global: { components: { RouterLink: { template: '<a><slot/></a>' } } } })
    await flushPromises()
    await wrapper.find('[data-testid="open-wipe-modal"]').trigger('click')
    await wrapper.find('[data-testid="confirm-text"]').setValue('WIPE')
    await wrapper.find('[data-testid="confirm-wipe"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="wipe-result"]').text()).toContain('42')
  })

  it('surfaces an error from a failed wipe', async () => {
    adminWipe.mockRejectedValue(
      Object.assign(new Error('boom'), {
        response: { data: { detail: 'confirm must be the literal string WIPE' } },
      }),
    )
    const wrapper = mount(Admin, { global: { components: { RouterLink: { template: '<a><slot/></a>' } } } })
    await flushPromises()
    await wrapper.find('[data-testid="open-wipe-modal"]').trigger('click')
    await wrapper.find('[data-testid="confirm-text"]').setValue('WIPE')
    await wrapper.find('[data-testid="confirm-wipe"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="wipe-error"]').text())
      .toContain('confirm must be the literal string WIPE')
  })
})


describe('<Admin> — recent wipes list', () => {
  it('renders the audit rows from listAdminWipes', async () => {
    const wrapper = mount(Admin, { global: { components: { RouterLink: { template: '<a><slot/></a>' } } } })
    await flushPromises()
    const list = wrapper.find('[data-testid="wipe-list"]')
    expect(list.exists()).toBe(true)
    expect(list.text()).toContain('abc123')
    expect(list.text()).toContain('Validating Slice 3')
    expect(list.text()).toContain('15 rows')
  })

  it('renders an empty state when there are no wipes', async () => {
    listAdminWipes.mockResolvedValueOnce([])
    const wrapper = mount(Admin, { global: { components: { RouterLink: { template: '<a><slot/></a>' } } } })
    await flushPromises()
    expect(wrapper.find('[data-testid="wipe-list"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('No wipes recorded yet')
  })
})
