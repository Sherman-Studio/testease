// Unit tests for the request-shape transforms inside ``triggerRun``.
// Keeps the slice-1 (#1018) target_url behaviour pinned: empty/whitespace
// stay out of the body so the server falls through to the CronJob default;
// non-empty values get trimmed.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Mock axios *before* the SUT import so the `axios.create()` call inside
// api.js sees our spy. We expose the captured POST/GET so each test can
// read the request shape (triggerRun's body, listMCPServers' URL).
const lastPost = { url: null, body: null }
const lastGet = { url: null, params: null }
// Scriptable GET response — tests set this then trigger the call.
let nextGetResponse = { data: {} }

vi.mock('axios', () => ({
  default: {
    create: () => ({
      post: (url, body) => {
        lastPost.url = url
        lastPost.body = body
        return Promise.resolve({ data: { job_name: 'qa-ui-stub' } })
      },
      get: (url, config) => {
        lastGet.url = url
        // Slice 2.2 of #1106 — capture the config.params object so
        // tests can assert filter query parameters were forwarded.
        lastGet.params = (config && config.params) || null
        return Promise.resolve(nextGetResponse)
      },
    }),
  },
}))

const { triggerRun, listMCPServers } = await import('./api.js')

beforeEach(() => {
  lastPost.url = null
  lastPost.body = null
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('triggerRun — body shape', () => {
  it('posts personas only when no options provided', async () => {
    await triggerRun(['happy-path-signup'])
    expect(lastPost.url).toBe('/runs/trigger')
    expect(lastPost.body).toEqual({ personas: ['happy-path-signup'] })
  })

  it('includes concurrency when set', async () => {
    await triggerRun(['x'], { concurrency: 3 })
    expect(lastPost.body.concurrency).toBe(3)
  })

  // #1821 — podCount forwards as pod_count only when explicitly set; an
  // omitted value keeps the field out of the body so the server builds
  // today's single-pod Job.
  it('includes pod_count when set', async () => {
    await triggerRun(['x'], { podCount: 3 })
    expect(lastPost.body.pod_count).toBe(3)
  })

  it('omits pod_count when not provided', async () => {
    await triggerRun(['x'], { concurrency: 2 })
    expect('pod_count' in lastPost.body).toBe(false)
  })

  it('omits unset/null/empty optional fields', async () => {
    await triggerRun(['x'], {
      concurrency: null,
      runNotes: '',
      mandatoryActionIds: [],
      targetUrl: '',
    })
    // Only `personas` survives — every other field defers to the server default.
    expect(lastPost.body).toEqual({ personas: ['x'] })
  })

  it('never sends a backend field (runs are always Max-billed)', async () => {
    await triggerRun(['x'], { concurrency: 2 })
    expect('backend' in lastPost.body).toBe(false)
  })
})

// -- #1018 — target_url plumbing -------------------------------------------
describe('triggerRun — target_url (Slice 1 of #1006)', () => {
  it('omits target_url when undefined (preserve in-cluster default)', async () => {
    await triggerRun(['x'])
    expect('target_url' in lastPost.body).toBe(false)
  })

  it('omits target_url when empty string', async () => {
    // The UI's v-model binding for an untouched field yields '' — and the
    // contract is that empty means "fall through to the CronJob default
    // (QA_WEB_BASE_URL in the pod spec, today the in-cluster sandbox)".
    await triggerRun(['x'], { targetUrl: '' })
    expect('target_url' in lastPost.body).toBe(false)
  })

  it('omits target_url when whitespace-only', async () => {
    // Belt-and-braces — a stray paste of "   " from a chat message shouldn't
    // 422 on the server-side pattern. Trim happens in api.js.
    await triggerRun(['x'], { targetUrl: '   ' })
    expect('target_url' in lastPost.body).toBe(false)
  })

  it('forwards a trimmed target_url when set', async () => {
    await triggerRun(['x'], { targetUrl: '  https://staging.example.com  ' })
    expect(lastPost.body.target_url).toBe('https://staging.example.com')
  })

  it('forwards http URLs unchanged (cluster-internal services often http)', async () => {
    await triggerRun(['x'], { targetUrl: 'http://frontend' })
    expect(lastPost.body.target_url).toBe('http://frontend')
  })

  it('does NOT client-side-validate the http(s) shape — server is canonical', async () => {
    // The server enforces the http(s) pattern; the client just trims and
    // forwards. If a bad URL gets through it's a 422 with a clear detail,
    // surfaced in the existing trigger error path.
    await triggerRun(['x'], { targetUrl: 'not-a-url' })
    expect(lastPost.body.target_url).toBe('not-a-url')
  })
})

// -- Slice B of #1028 — listMCPServers ----------------------------------
describe('listMCPServers', () => {
  beforeEach(() => {
    lastGet.url = null
    nextGetResponse = {
      data: {
        servers: [
          { id: 'playwright', display_name: 'Playwright', description: '', default_enabled: true, persona_compat: [], tool_count: 22 },
          { id: 'email', display_name: 'Email', description: '', default_enabled: true, persona_compat: [], tool_count: 3 },
        ],
      },
    }
  })

  it('GETs /mcp-servers (no params, catalog is static)', async () => {
    await listMCPServers()
    expect(lastGet.url).toBe('/mcp-servers')
  })

  it('unwraps the {servers: [...]} envelope', async () => {
    const result = await listMCPServers()
    expect(Array.isArray(result)).toBe(true)
    expect(result).toHaveLength(2)
    expect(result[0].id).toBe('playwright')
  })

  it('preserves the catalog order from the server', async () => {
    // The catalog is curated; api.js does NOT re-sort. Any UI sort
    // happens at the component level.
    const result = await listMCPServers()
    expect(result.map((s) => s.id)).toEqual(['playwright', 'email'])
  })
})

// -- Slice C of #1028 — triggerRun forwards enabledMCPServers --------------
describe('triggerRun — enabledMCPServers (Slice C of #1028)', () => {
  it('omits enabled_mcp_servers when undefined', async () => {
    await triggerRun(['x'])
    expect('enabled_mcp_servers' in lastPost.body).toBe(false)
  })

  it('omits enabled_mcp_servers when empty list (defer to server defaults)', async () => {
    // Empty list = "use catalog defaults" — same contract as the
    // mandatory_action_ids handling above. The server's
    // _resolve_enabled_mcp_servers falls back when QA_ENABLED_MCPS
    // is unset, so no body key is needed.
    await triggerRun(['x'], { enabledMCPServers: [] })
    expect('enabled_mcp_servers' in lastPost.body).toBe(false)
  })

  it('forwards a non-empty list as the exact opt-in', async () => {
    await triggerRun(['x'], { enabledMCPServers: ['playwright', 'findings'] })
    expect(lastPost.body.enabled_mcp_servers).toEqual(['playwright', 'findings'])
  })

  it('does NOT client-side-validate ids — server is canonical', async () => {
    // The server validates each id against the catalog. If a bad id
    // gets through it's a 422 with a clear detail; the UI surfaces
    // it via the existing trigger error path. api.js's job is plumbing.
    await triggerRun(['x'], { enabledMCPServers: ['totally-fake'] })
    expect(lastPost.body.enabled_mcp_servers).toEqual(['totally-fake'])
  })
})
