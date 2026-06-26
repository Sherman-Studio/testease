// <RegressionBadge> — magenta STILL-BROKEN pill rendered on findings
// where is_regression=true. Three states:
//   1. is_regression=false → renders nothing visible
//   2. is_regression=true + last_verified_run_id present → router-link
//      to the prior run, "STILL BROKEN" label, tooltip cites the run id
//   3. is_regression=true + no prior run id → plain span fallback,
//      "Regression" label, generic tooltip

import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import RegressionBadge from './RegressionBadge.vue'

function mountWithRouter(props) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/runs/:id', component: { template: '<div />' } }],
  })
  return mount(RegressionBadge, { props, global: { plugins: [router] } })
}

describe('<RegressionBadge>', () => {
  it('renders nothing visible when is_regression=false', () => {
    const w = mountWithRouter({
      finding: { finding_id: 'f-1', is_regression: false },
    })
    // The component emits an empty <span /> placeholder; text content is empty.
    expect(w.text()).toBe('')
  })

  it('renders a clickable STILL-BROKEN link when prior run id is present', () => {
    const w = mountWithRouter({
      finding: {
        finding_id: 'f-2',
        is_regression: true,
        last_verified_run_id: 'qa-prev-001',
      },
    })
    const link = w.find('[data-testid="regression-badge-f-2"]')
    expect(link.exists()).toBe(true)
    expect(link.element.tagName).toBe('A')
    expect(link.attributes('href')).toBe('/runs/qa-prev-001')
    expect(link.text()).toBe('⚠ STILL BROKEN')
    // Tooltip cites the prior run id so the operator knows what they're
    // about to navigate to before clicking.
    expect(link.attributes('title')).toContain('qa-prev-001')
    expect(link.attributes('title')).toContain('previously fixed')
  })

  it('falls back to a plain non-clickable span when prior run id is missing', () => {
    const w = mountWithRouter({
      finding: {
        finding_id: 'f-3',
        is_regression: true,
        // last_verified_run_id deliberately omitted — legacy finding.
      },
    })
    const badge = w.find('[data-testid="regression-badge-f-3"]')
    expect(badge.exists()).toBe(true)
    expect(badge.element.tagName).toBe('SPAN')
    expect(badge.text()).toBe('⚠ Regression')
    expect(badge.attributes('href')).toBeUndefined()
  })

  it('stops click propagation so wrapping clickable rows do not also fire', async () => {
    // The badge often sits inside a clickable card or row. Operator
    // clicks the badge → navigates to prior run; they do NOT also
    // toggle the row expanded or jump to the filing step.
    let outerClicked = false
    const w = mount(
      {
        components: { RegressionBadge },
        template: `
          <button data-testid="outer" @click="onClick">
            <RegressionBadge :finding="finding" />
          </button>
        `,
        props: ['finding'],
        methods: { onClick() { outerClicked = true } },
      },
      {
        props: {
          finding: {
            finding_id: 'f-4',
            is_regression: true,
            last_verified_run_id: 'qa-prev-002',
          },
        },
        global: {
          plugins: [
            createRouter({
              history: createMemoryHistory(),
              routes: [{ path: '/runs/:id', component: { template: '<div />' } }],
            }),
          ],
        },
      },
    )
    await w.find('[data-testid="regression-badge-f-4"]').trigger('click')
    expect(outerClicked).toBe(false)
  })
})
