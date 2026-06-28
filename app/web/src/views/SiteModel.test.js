// <SiteModel> — the Sites home + "Add a site" registration front door.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'

const mocks = {
  listSiteTargets: vi.fn(),
  createSiteTarget: vi.fn(),
  push: vi.fn(),
}

vi.mock('../api.js', () => ({
  listSiteTargets: (...a) => mocks.listSiteTargets(...a),
  createSiteTarget: (...a) => mocks.createSiteTarget(...a),
}))
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mocks.push }),
  RouterLink: RouterLinkStub,
}))

import SiteModel from './SiteModel.vue'

function mountPage() {
  return mount(SiteModel, { global: { stubs: { RouterLink: RouterLinkStub } } })
}

beforeEach(() => {
  for (const m of Object.values(mocks)) m.mockReset()
  mocks.listSiteTargets.mockResolvedValue([])
  mocks.createSiteTarget.mockResolvedValue({ target_id: 'example', lifecycle: 'registered' })
})

describe('<SiteModel> — Sites front door', () => {
  it('shows the Sites heading and an Add-a-site action', async () => {
    const w = mountPage()
    await flushPromises()
    expect(w.find('h1').text()).toContain('Sites')
    expect(w.find('[data-testid="add-site"]').exists()).toBe(true)
  })

  it('empty state offers to add the first site', async () => {
    const w = mountPage()
    await flushPromises()
    expect(w.find('[data-testid="add-first-site"]').exists()).toBe(true)
  })

  it('opens the modal and registers a site, then navigates to it', async () => {
    const w = mountPage()
    await flushPromises()
    await w.find('[data-testid="add-site"]').trigger('click')
    expect(w.find('[data-testid="add-site-modal"]').exists()).toBe(true)
    await w.find('[data-testid="add-site-url"]').setValue('https://app.example.com')
    await w.find('[data-testid="add-site-submit"]').trigger('click')
    await flushPromises()
    expect(mocks.createSiteTarget).toHaveBeenCalledWith({
      base_url: 'https://app.example.com',
      display_name: '',
    })
    expect(mocks.push).toHaveBeenCalledWith('/site/example')
  })

  it('surfaces a registration error and stays on the modal', async () => {
    mocks.createSiteTarget.mockRejectedValue({ response: { data: { detail: 'bad url' } } })
    const w = mountPage()
    await flushPromises()
    await w.find('[data-testid="add-site"]').trigger('click')
    await w.find('[data-testid="add-site-url"]').setValue('nope')
    await w.find('[data-testid="add-site-submit"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="add-site-error"]').text()).toContain('bad url')
    expect(w.find('[data-testid="add-site-modal"]').exists()).toBe(true)
    expect(mocks.push).not.toHaveBeenCalled()
  })

  it('lists targets with their lifecycle', async () => {
    mocks.listSiteTargets.mockResolvedValue([
      { target_id: 'acme', display_name: 'Acme', base_url: 'https://acme.test', lifecycle: 'configured' },
    ])
    const w = mountPage()
    await flushPromises()
    expect(w.text()).toContain('Acme')
    expect(w.text()).toContain('configured')
  })
})
