// Smoke tests for the new Test Ease shell — branding, sidebar, nav.

import { test, expect } from '@playwright/test'
import { installApiMocks } from './fixtures'

test.describe('App shell', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
  })

  test('document title is Test Ease', async ({ page }) => {
    await page.goto('/')
    await expect(page).toHaveTitle('Test Ease')
  })

  test('sidebar shows the Test Ease brand mark', async ({ page }) => {
    await page.goto('/')
    // "Test Ease" appears three times across the App.vue render tree
    // (desktop sidebar header, ARIA-only label, mobile drawer slot),
    // tripping Playwright's strict mode. Scope to the <aside> ancestor.
    // Mirror of the same fix in PR #1027 — both branches needed it
    // independently since main hasn't picked it up yet.
    await expect(
      page.locator('aside').getByText('Test Ease', { exact: true }).first(),
    ).toBeVisible()
  })

  // #1822 — the old 7-item nav collapsed to three primary destinations
  // (history → trigger → registry). The reference/maintenance pages
  // moved into the ⚙ Utilities popover.
  test('sidebar contains the three primary nav rows', async ({ page }) => {
    await page.goto('/')
    const aside = page.getByTestId('desktop-sidebar')
    for (const label of ['Runs', 'New Run', 'Personas']) {
      await expect(aside.locator('.nav-row').filter({ hasText: label })).toBeVisible()
    }
    // The retired top-level rows are gone from the sidebar nav.
    for (const label of ['Scenarios', 'Memory', 'Transcripts', 'Insights']) {
      await expect(aside.locator('.nav-row').filter({ hasText: label })).toHaveCount(0)
    }
  })

  test('utility menu holds Discovered / MCP tools / Admin / API docs', async ({ page }) => {
    await page.goto('/')
    // Closed by default.
    await expect(page.getByTestId('utility-menu')).toHaveCount(0)
    await page.getByTestId('utility-menu-toggle').click()
    const menu = page.getByTestId('utility-menu')
    await expect(menu).toBeVisible()
    for (const label of ['Discovered', 'MCP tools', 'Admin']) {
      await expect(menu.locator('.nav-row').filter({ hasText: label })).toBeVisible()
    }
    await expect(menu.getByText('API docs')).toBeVisible()
  })

  test('utility menu navigates to /admin and closes on navigation', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('utility-menu-toggle').click()
    await page
      .getByTestId('utility-menu')
      .locator('.nav-row')
      .filter({ hasText: 'Admin' })
      .click()
    await expect(page).toHaveURL(/\/admin$/)
    // The popover closes once the route changes.
    await expect(page.getByTestId('utility-menu')).toHaveCount(0)
  })

  test('Personas nav row navigates to /personas', async ({ page }) => {
    await page.goto('/')
    await page.locator('.nav-row').filter({ hasText: 'Personas' }).click()
    await expect(page).toHaveURL(/\/personas$/)
  })

  test('New Run nav row navigates to /new-run', async ({ page }) => {
    await page.goto('/')
    await page.locator('.nav-row').filter({ hasText: 'New Run' }).click()
    await expect(page).toHaveURL(/\/new-run$/)
  })

  // #1822 retired the /scenarios and /transcripts pages; old bookmarks
  // must still resolve. Presets live on the New Run console; transcript
  // search lives inside each run's Timeline tab.
  test('/scenarios redirects to /new-run', async ({ page }) => {
    await page.goto('/scenarios')
    await expect(page).toHaveURL(/\/new-run$/)
  })

  test('/transcripts redirects to the runs home', async ({ page }) => {
    await page.goto('/transcripts')
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByRole('heading', { name: 'Runs' })).toBeVisible()
  })

  // #1078 Slice 0 — collapsible sidebar. Default is expanded so the
  // existing strict-text assertions above keep working unchanged.
  // The toggle hides labels and shrinks the column; state persists in
  // localStorage under ``testease.nav.collapsed``.
  test.describe('collapsible sidebar', () => {
    test('starts expanded by default', async ({ page }) => {
      await page.goto('/')
      const aside = page.getByTestId('desktop-sidebar')
      await expect(aside).toBeVisible()
      // Labels are visible in the expanded state.
      await expect(aside.getByText('Runs', { exact: true })).toBeVisible()
      // The toggle button is labelled "Collapse" when sidebar is open.
      await expect(
        page.getByTestId('nav-collapse-toggle'),
      ).toContainText(/Collapse/)
    })

    test('toggling collapse hides labels but keeps icons + nav-rows', async ({ page }) => {
      await page.goto('/')
      await page.getByTestId('nav-collapse-toggle').click()
      const aside = page.getByTestId('desktop-sidebar')
      // The "Runs" label text is gone from the sidebar...
      await expect(aside.getByText('Runs', { exact: true })).toHaveCount(0)
      // ...but the .nav-row elements are still there (icons only):
      // 3 primary destinations + the ⚙ Utilities toggle (#1822).
      await expect(aside.locator('.nav-row')).toHaveCount(4)
      // Toggle button no longer carries the "Collapse" word.
      await expect(
        page.getByTestId('nav-collapse-toggle'),
      ).not.toContainText('Collapse')
    })

    test('collapse state persists across reloads via localStorage', async ({ page }) => {
      await page.goto('/')
      await page.getByTestId('nav-collapse-toggle').click()
      await expect(
        page.getByTestId('desktop-sidebar').getByText('Runs', { exact: true }),
      ).toHaveCount(0)
      await page.reload()
      // After reload, the sidebar comes back collapsed — labels still hidden.
      await expect(
        page.getByTestId('desktop-sidebar').getByText('Runs', { exact: true }),
      ).toHaveCount(0)
      // The localStorage value reflects the collapsed state.
      expect(
        await page.evaluate(() => localStorage.getItem('testease.nav.collapsed')),
      ).toBe('1')
    })

    test('toggling back restores labels', async ({ page }) => {
      await page.goto('/')
      await page.getByTestId('nav-collapse-toggle').click()
      await page.getByTestId('nav-collapse-toggle').click()
      await expect(
        page.getByTestId('desktop-sidebar').getByText('Runs', { exact: true }),
      ).toBeVisible()
    })

    test('collapsed nav rows still route correctly when clicked', async ({ page }) => {
      await page.goto('/')
      await page.getByTestId('nav-collapse-toggle').click()
      // With labels hidden, find the row by its title attribute (which
      // doubles as the hover tooltip).
      await page
        .getByTestId('desktop-sidebar')
        .locator('.nav-row[title="Personas"]')
        .click()
      await expect(page).toHaveURL(/\/personas$/)
    })
  })
})
