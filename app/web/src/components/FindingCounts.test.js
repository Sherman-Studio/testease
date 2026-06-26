// <FindingCounts> — renders the severity-bucketed finding counts as
// coloured pills across the Runs list and the run-detail header.

import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import FindingCounts from './FindingCounts.vue'

describe('<FindingCounts>', () => {
  it('renders nothing but a "none" pill when total is zero', () => {
    const wrapper = mount(FindingCounts, { props: { counts: {} } })
    const pills = wrapper.findAll('.pill')
    expect(pills).toHaveLength(1)
    expect(pills[0].text()).toBe('none')
  })

  it('renders a pill per non-zero severity', () => {
    const wrapper = mount(FindingCounts, {
      props: { counts: { blocker: 1, major: 2, minor: 0, nit: 3 } },
    })
    // Each non-zero pill carries an aria-label of "<count> <severity>" — the
    // visible text also has a non-colour glyph prefix for colorblind users.
    const labels = wrapper.findAll('.pill').map((p) => p.attributes('aria-label'))
    expect(labels).toContain('1 blocker')
    expect(labels).toContain('2 major')
    expect(labels).toContain('3 nit')
    // minor=0 must not render
    expect(labels.some((l) => l && l.includes('minor'))).toBe(false)
  })

  it('orders pills blocker > major > minor > nit', () => {
    const wrapper = mount(FindingCounts, {
      props: { counts: { nit: 1, blocker: 1, minor: 1, major: 1 } },
    })
    const labels = wrapper.findAll('.pill').map((p) => p.attributes('aria-label'))
    expect(labels).toEqual([
      '1 blocker',
      '1 major',
      '1 minor',
      '1 nit',
    ])
  })

  it('survives a null counts prop without crashing', () => {
    // Defensive — pre-#860 runs may not carry finding_counts at all.
    const wrapper = mount(FindingCounts, { props: { counts: null } })
    expect(wrapper.text()).toContain('none')
  })
})
