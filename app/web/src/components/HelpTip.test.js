// <HelpTip> — the in-context "(?)" concept explainer.
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import HelpTip from './HelpTip.vue'

describe('<HelpTip>', () => {
  it('is hidden by default and toggles on click', async () => {
    const w = mount(HelpTip, { props: { label: 'Persona', text: 'A fictional user.' } })
    expect(w.find('[data-testid="helptip-pop"]').exists()).toBe(false)
    await w.find('[data-testid="helptip"]').trigger('click')
    const pop = w.find('[data-testid="helptip-pop"]')
    expect(pop.exists()).toBe(true)
    expect(pop.text()).toContain('A fictional user.')
    expect(pop.text()).toContain('Persona')
  })

  it('renders default-slot content over the text prop', async () => {
    const w = mount(HelpTip, { slots: { default: 'Slot copy wins' } })
    await w.find('[data-testid="helptip"]').trigger('click')
    expect(w.find('[data-testid="helptip-pop"]').text()).toContain('Slot copy wins')
  })

  it('closes on Escape', async () => {
    const w = mount(HelpTip, { attachTo: document.body, props: { text: 'x' } })
    await w.find('[data-testid="helptip"]').trigger('click')
    expect(w.find('[data-testid="helptip-pop"]').exists()).toBe(true)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await w.vm.$nextTick()
    expect(w.find('[data-testid="helptip-pop"]').exists()).toBe(false)
    w.unmount()
  })

  it('sets aria-expanded for accessibility', async () => {
    const w = mount(HelpTip, { props: { text: 'x' } })
    const btn = w.find('[data-testid="helptip"]')
    expect(btn.attributes('aria-expanded')).toBe('false')
    await btn.trigger('click')
    expect(btn.attributes('aria-expanded')).toBe('true')
  })
})
