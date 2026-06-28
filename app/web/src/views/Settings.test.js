// <Settings> — BYOK LLM credentials.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const mocks = {
  getLLMConfig: vi.fn(),
  setLLMConfig: vi.fn(),
  clearLLMToken: vi.fn(),
}
vi.mock('../api.js', () => ({
  getLLMConfig: (...a) => mocks.getLLMConfig(...a),
  setLLMConfig: (...a) => mocks.setLLMConfig(...a),
  clearLLMToken: (...a) => mocks.clearLLMToken(...a),
}))

import Settings from './Settings.vue'

const BACKENDS = [
  { id: 'claude-code', env: 'CLAUDE_CODE_OAUTH_TOKEN', label: 'Claude Code subscription (flat price)', hint: 'h', recommended: true },
  { id: 'api', env: 'ANTHROPIC_API_KEY', label: 'Anthropic API key (per-token billing)', hint: 'h', recommended: false },
]
function cfg(over = {}) {
  return { backend: 'claude-code', env_var: 'CLAUDE_CODE_OAUTH_TOKEN', token_configured: false, token_source: null, backends: BACKENDS, ...over }
}

beforeEach(() => {
  for (const m of Object.values(mocks)) m.mockReset()
  mocks.getLLMConfig.mockResolvedValue(cfg())
  mocks.setLLMConfig.mockImplementation(async (p) => cfg({ backend: p.backend, token_configured: !!p.token, token_source: p.token ? 'vault' : null }))
  mocks.clearLLMToken.mockResolvedValue(cfg())
})

describe('<Settings> — BYOK', () => {
  it('shows "Not configured" and both backends', async () => {
    const w = mount(Settings)
    await flushPromises()
    expect(w.find('[data-testid="token-status"]').text()).toContain('Not configured')
    expect(w.find('[data-testid="backend-claude-code"]').exists()).toBe(true)
    expect(w.find('[data-testid="backend-api"]').exists()).toBe(true)
  })

  it('reports an env-provided token as configured', async () => {
    mocks.getLLMConfig.mockResolvedValue(cfg({ token_configured: true, token_source: 'env' }))
    const w = mount(Settings)
    await flushPromises()
    expect(w.find('[data-testid="token-status"]').text()).toContain('from environment')
  })

  it('saves the backend + token (vaulted server-side)', async () => {
    const w = mount(Settings)
    await flushPromises()
    await w.find('[data-testid="token-input"]').setValue('sk-oauth')
    await w.find('[data-testid="save-config"]').trigger('click')
    await flushPromises()
    expect(mocks.setLLMConfig).toHaveBeenCalledWith({ backend: 'claude-code', token: 'sk-oauth' })
    expect(w.find('[data-testid="settings-saved"]').exists()).toBe(true)
  })

  it('omits the token when the field is left blank (keep existing)', async () => {
    mocks.getLLMConfig.mockResolvedValue(cfg({ token_configured: true, token_source: 'vault' }))
    const w = mount(Settings)
    await flushPromises()
    await w.find('[data-testid="backend-api"]').setValue()
    await w.find('[data-testid="save-config"]').trigger('click')
    await flushPromises()
    expect(mocks.setLLMConfig).toHaveBeenCalledWith({ backend: 'api' })
  })

  it('removes a saved token', async () => {
    mocks.getLLMConfig.mockResolvedValue(cfg({ token_configured: true, token_source: 'vault' }))
    const w = mount(Settings)
    await flushPromises()
    await w.find('[data-testid="clear-token"]').trigger('click')
    await flushPromises()
    expect(mocks.clearLLMToken).toHaveBeenCalled()
  })
})
