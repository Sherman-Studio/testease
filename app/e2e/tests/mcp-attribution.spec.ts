// Slice A of #1028 — MCP attribution chip list on the run-detail
// overview. Slice B (#1030) upgraded the chip's label to use catalog
// display names instead of raw ids — these tests were updated then to
// match. The fall-back-to-raw-id case is exercised in mcp-tools.spec.ts.

import { test, expect } from '@playwright/test'
import { installApiMocks, SAMPLE_MCP_SERVERS, SAMPLE_RUN_DETAIL } from './fixtures'

test.describe('MCP servers used — run-detail chips', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
  })

  test('renders one chip per MCP server with the call count', async ({ page }) => {
    await page.goto(`/runs/${SAMPLE_RUN_DETAIL.run_id}`)
    const panel = page.getByTestId('mcp-servers-used')
    await expect(panel).toBeVisible()
    await expect(panel.getByText('MCP servers used')).toBeVisible()
    // Every chip is an <a> linking to /mcp-tools (Slice B). Locate by
    // the catalog display name (the chip's label) and assert the call
    // count text is also in the same anchor element.
    for (const m of SAMPLE_RUN_DETAIL.mcp_servers_used) {
      const entry = SAMPLE_MCP_SERVERS.find((s) => s.id === m.server)
      const labelText = entry ? entry.display_name : m.server
      const chip = panel.locator('a', { hasText: labelText })
      await expect(chip).toBeVisible()
      await expect(chip).toContainText(String(m.calls))
    }
  })

  test('chip ordering matches the API response (most active first)', async ({ page }) => {
    await page.goto(`/runs/${SAMPLE_RUN_DETAIL.run_id}`)
    const chips = page.getByTestId('mcp-servers-used').locator('a')
    // SAMPLE_RUN_DETAIL ships [playwright(47), email(3), findings(12)].
    // The Slice B label upgrade means we assert on catalog display
    // names — playwright → "Playwright (browser automation)", etc.
    const labels = SAMPLE_RUN_DETAIL.mcp_servers_used.map((m) => {
      const entry = SAMPLE_MCP_SERVERS.find((s) => s.id === m.server)
      return entry ? entry.display_name : m.server
    })
    const texts = await chips.allTextContents()
    for (let i = 0; i < labels.length; i++) {
      expect(texts[i]).toContain(labels[i])
    }
  })

  test('hides the panel entirely when no MCP calls landed', async ({ page }) => {
    // Override SAMPLE_RUN_DETAIL just for this test — empty list.
    await page.route('**/api/runs/qa-run-001', (route) =>
      route.fulfill({
        json: { ...SAMPLE_RUN_DETAIL, mcp_servers_used: [] },
      }),
    )
    await page.goto(`/runs/${SAMPLE_RUN_DETAIL.run_id}`)
    // The header still renders (run id chip), but the MCP panel doesn't.
    await expect(page.getByText('MCP servers used')).toHaveCount(0)
    await expect(page.getByTestId('mcp-servers-used')).toHaveCount(0)
  })
})
