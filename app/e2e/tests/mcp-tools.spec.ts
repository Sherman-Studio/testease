// Slice B of #1028 — read-only MCP catalog page at /mcp-tools.
// Source-of-truth lives in qa_agents/mcp_catalog.py (the harness
// package). This spec exercises the SPA route, navigation, and the
// catalog-derived display-name upgrade on Slice A's chips.

import { test, expect } from '@playwright/test'
import { installApiMocks, SAMPLE_MCP_SERVERS, SAMPLE_RUN_DETAIL } from './fixtures'

test.describe('MCP tools catalog page', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
  })

  test('renders a card per catalog entry', async ({ page }) => {
    await page.goto('/mcp-tools')
    await expect(page.getByRole('heading', { name: /MCP tools at your disposal/i })).toBeVisible()
    for (const s of SAMPLE_MCP_SERVERS) {
      const card = page.getByTestId(`mcp-server-${s.id}`)
      await expect(card).toBeVisible()
      await expect(card).toContainText(s.display_name)
      await expect(card).toContainText(s.description)
    }
  })

  test('shows the default-on pill on every default-enabled server', async ({ page }) => {
    await page.goto('/mcp-tools')
    const defaultOn = SAMPLE_MCP_SERVERS.filter((s) => s.default_enabled)
    for (const s of defaultOn) {
      const card = page.getByTestId(`mcp-server-${s.id}`)
      await expect(card.getByText('default-on')).toBeVisible()
    }
  })

  test('shows the tool count footer on each card', async ({ page }) => {
    await page.goto('/mcp-tools')
    for (const s of SAMPLE_MCP_SERVERS) {
      const card = page.getByTestId(`mcp-server-${s.id}`)
      // "22 tools", "3 tools", "1 tool"
      const noun = s.tool_count === 1 ? 'tool' : 'tools'
      await expect(card).toContainText(`${s.tool_count} ${noun}`)
    }
  })

  test('utility menu links to /mcp-tools', async ({ page }) => {
    // #1822 — MCP tools moved from the primary nav into the ⚙
    // Utilities popover.
    await page.goto('/')
    await page.getByTestId('utility-menu-toggle').click()
    await page
      .getByTestId('utility-menu')
      .locator('.nav-row')
      .filter({ hasText: 'MCP tools' })
      .click()
    await expect(page).toHaveURL(/\/mcp-tools$/)
  })

  test('empty-state copy renders when the catalog is empty', async ({ page }) => {
    // Override the default mock to return an empty catalog.
    await page.route('**/api/mcp-servers', (route) =>
      route.fulfill({ json: { servers: [] } }),
    )
    await page.goto('/mcp-tools')
    await expect(page.getByText(/The catalog is empty/i)).toBeVisible()
  })
})

test.describe('Slice A chip layer — display name upgrade from catalog (#1030)', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
  })

  test('chips show catalog display names not raw ids when catalog loaded', async ({ page }) => {
    await page.goto(`/runs/${SAMPLE_RUN_DETAIL.run_id}`)
    const panel = page.getByTestId('mcp-servers-used')
    // The Slice A panel renders display_name from the catalog. We assert
    // the display_name strings show up (each is unique enough to anchor).
    for (const m of SAMPLE_RUN_DETAIL.mcp_servers_used) {
      const entry = SAMPLE_MCP_SERVERS.find((s) => s.id === m.server)
      if (!entry) continue
      await expect(panel.getByText(entry.display_name)).toBeVisible()
    }
  })

  test('chip falls back to raw id when not in catalog', async ({ page }) => {
    // Override the catalog to a single entry — the OTHER chips on the
    // run-detail page fall back to raw ids.
    await page.route('**/api/mcp-servers', (route) =>
      route.fulfill({
        json: { servers: [SAMPLE_MCP_SERVERS[0]] }, // only playwright
      }),
    )
    await page.goto(`/runs/${SAMPLE_RUN_DETAIL.run_id}`)
    const panel = page.getByTestId('mcp-servers-used')
    // email + findings are not in the truncated catalog; they appear
    // as raw ids in the chip.
    await expect(panel.getByText('email', { exact: true })).toBeVisible()
    await expect(panel.getByText('findings', { exact: true })).toBeVisible()
  })

  test('chip links to /mcp-tools when clicked', async ({ page }) => {
    await page.goto(`/runs/${SAMPLE_RUN_DETAIL.run_id}`)
    const panel = page.getByTestId('mcp-servers-used')
    // Click the first chip — any of them links to the catalog page.
    await panel.locator('a').first().click()
    await expect(page).toHaveURL(/\/mcp-tools$/)
  })
})
