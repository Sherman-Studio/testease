// <CapabilityCard> — a single grantable capability.
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import CapabilityCard from './CapabilityCard.vue'

const CAP = {
  capability_id: 'app-logs', title: 'Application logs (read)', unlocks: 'Root-cause errors.',
  risk_class: 'read-only', grant_kind: 'secret', status: 'available',
}

describe('<CapabilityCard>', () => {
  it('renders title, unlocks, risk', () => {
    const w = mount(CapabilityCard, { props: { cap: CAP } })
    expect(w.text()).toContain('Application logs (read)')
    expect(w.text()).toContain('Root-cause errors.')
    expect(w.text()).toContain('read-only')
  })

  it('connect → input → grant emits the token', async () => {
    const w = mount(CapabilityCard, { props: { cap: CAP } })
    await w.find('[data-testid="cap-app-logs-connect"]').trigger('click')
    await w.find('[data-testid="cap-app-logs-input"]').setValue('logkey')
    await w.find('[data-testid="cap-app-logs-save"]').trigger('click')
    expect(w.emitted('grant')[0]).toEqual(['logkey'])
  })

  it('a grant_kind=none capability grants immediately (no input)', async () => {
    const w = mount(CapabilityCard, { props: { cap: { ...CAP, grant_kind: 'none' } } })
    await w.find('[data-testid="cap-app-logs-connect"]').trigger('click')
    expect(w.emitted('grant')[0]).toEqual([null])
  })

  it('granted shows Revoke and emits it', async () => {
    const w = mount(CapabilityCard, { props: { cap: { ...CAP, status: 'granted' } } })
    expect(w.text()).toContain('granted')
    await w.findAll('button').find((b) => b.text() === 'Revoke').trigger('click')
    expect(w.emitted('revoke')).toBeTruthy()
  })
})
