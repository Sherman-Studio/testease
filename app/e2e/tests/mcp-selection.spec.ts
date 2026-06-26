// Slice C of #1028 — per-run MCP server selection on the trigger form.
//
// #1822 moved the trigger console to /new-run and tucked the MCP panel
// behind the Advanced disclosure: open Advanced first, then the "MCP
// tools" toggle inside it. The selection semantics are unchanged:
// when the catalog loads (eager fetch on mount), every
// default_enabled=True server is pre-ticked. An operator who never
// opens the panel hits Launch with the catalog defaults → api.js omits
// enabled_mcp_servers from the POST → harness falls back to its own
// catalog default lookup. That's the "preserve pre-Slice-C behaviour"
// contract.
//
// Operator-flipped checkboxes produce a non-empty enabled_mcp_servers
// list in the POST body, which the harness reads as the exact opt-in.

import { test, expect } from '@playwright/test'
import { installApiMocks, SAMPLE_MCP_SERVERS } from './fixtures'

// The MCP panel lives inside the Advanced disclosure (#1822).
async function openAdvanced(page) {
  await page.getByTestId('advanced-toggle').click()
  await expect(page.locator('.mcp-tools')).toBeVisible()
}

// The MCP panel's own disclosure button — scoped to .mcp-tools because
// the Advanced toggle's caption also says "MCP tools".
function mcpToggle(page) {
  return page.locator('.mcp-tools').getByRole('button', { name: /MCP tools/i })
}

// Explicit selection (#1822): pick margaret's card, then Launch.
async function selectMargaretAndLaunch(page) {
  await page.locator('.persona-card').filter({ hasText: 'margaret' }).click()
  const cta = page.getByTestId('cta-start')
  await expect(cta).toHaveText(/▶ Launch 1/)
  await cta.click()
}

test.describe('MCP selection on trigger form (#1031)', () => {
  test('omits enabled_mcp_servers when the operator does not change selection', async ({ page }) => {
    // Catalog default selection (all default_enabled servers ticked) =
    // omit-from-body contract. The harness applies catalog defaults.
    const state = await installApiMocks(page)
    page.on('dialog', (dialog) => dialog.accept())
    await page.goto('/new-run')

    await selectMargaretAndLaunch(page)

    await expect.poll(() => state.triggerCalls.length).toBe(1)
    expect('enabled_mcp_servers' in state.triggerCalls[0]).toBe(false)
  })

  test('header collapsed by default and shows the count', async ({ page }) => {
    await installApiMocks(page)
    await page.goto('/new-run')
    await openAdvanced(page)
    // The header reads "MCP tools (N of M enabled)". Wait for the
    // count to reflect the loaded catalog (default-on count == catalog
    // length in SAMPLE_MCP_SERVERS).
    const defaultOnCount = SAMPLE_MCP_SERVERS.filter((s) => s.default_enabled).length
    await expect(page.locator('.mcp-tools')).toContainText(
      `${defaultOnCount} of ${SAMPLE_MCP_SERVERS.length} enabled`,
      { timeout: 3000 },
    )
    // The panel body is hidden until the operator clicks.
    await expect(page.locator('.mcp-body')).toHaveCount(0)
  })

  test('expands to show one row per catalog entry', async ({ page }) => {
    await installApiMocks(page)
    await page.goto('/new-run')
    await openAdvanced(page)
    // Wait for the catalog to load and the panel header to settle.
    await expect(page.locator('.mcp-tools')).toContainText(/of \d+ enabled/, {
      timeout: 3000,
    })
    await mcpToggle(page).click()
    await expect(page.locator('.mcp-body')).toBeVisible()
    for (const s of SAMPLE_MCP_SERVERS) {
      await expect(
        page.locator('.mcp-row').filter({ hasText: s.display_name }),
      ).toBeVisible()
    }
  })

  test('unticking a server sends the remaining set as the exact opt-in', async ({ page }) => {
    const state = await installApiMocks(page)
    page.on('dialog', (dialog) => dialog.accept())
    await page.goto('/new-run')
    await openAdvanced(page)
    await expect(page.locator('.mcp-tools')).toContainText(/of \d+ enabled/, {
      timeout: 3000,
    })

    // Open panel, untick "email" — the persona run goes without the
    // verification round-trip.
    await mcpToggle(page).click()
    const emailRow = page.locator('.mcp-row').filter({ hasText: /Email/ })
    await emailRow.locator('input[type=checkbox]').uncheck()

    await selectMargaretAndLaunch(page)

    await expect.poll(() => state.triggerCalls.length).toBe(1)
    const sent = state.triggerCalls[0].enabled_mcp_servers
    // The selection no longer matches catalog defaults, so the list is
    // serialised. Order doesn't matter — assert as a set.
    expect(sent).toBeDefined()
    expect(new Set(sent)).toEqual(new Set(['playwright', 'findings']))
  })

  test('confirm dialog surfaces the diff when selection differs from defaults', async ({ page }) => {
    await installApiMocks(page)
    let confirmMessage = ''
    page.on('dialog', (dialog) => {
      confirmMessage = dialog.message()
      dialog.dismiss()
    })
    await page.goto('/new-run')
    await openAdvanced(page)
    await expect(page.locator('.mcp-tools')).toContainText(/of \d+ enabled/, {
      timeout: 3000,
    })

    await mcpToggle(page).click()
    await page
      .locator('.mcp-row')
      .filter({ hasText: /Email/ })
      .locator('input[type=checkbox]')
      .uncheck()

    await selectMargaretAndLaunch(page)

    // The confirm dialog includes a "MCP servers:" line listing the
    // current selection so an accidental disable gets spotted before
    // the run starts.
    await expect.poll(() => confirmMessage).toContain('MCP servers:')
    expect(confirmMessage).toContain('findings')
    expect(confirmMessage).toContain('playwright')
    // The unticked email should NOT appear in the listed selection.
    expect(confirmMessage).not.toMatch(/MCP servers:.*email/)
  })

  test('confirm dialog says "(none — persona has no tools)" when all unticked', async ({ page }) => {
    await installApiMocks(page)
    let confirmMessage = ''
    page.on('dialog', (dialog) => {
      confirmMessage = dialog.message()
      dialog.dismiss()
    })
    await page.goto('/new-run')
    await openAdvanced(page)
    await expect(page.locator('.mcp-tools')).toContainText(/of \d+ enabled/, {
      timeout: 3000,
    })

    await mcpToggle(page).click()
    // Untick everything.
    for (const s of SAMPLE_MCP_SERVERS) {
      await page
        .locator('.mcp-row')
        .filter({ hasText: s.display_name })
        .locator('input[type=checkbox]')
        .uncheck()
    }

    await selectMargaretAndLaunch(page)
    await expect.poll(() => confirmMessage).toContain('(none — persona has no tools)')
  })
})
