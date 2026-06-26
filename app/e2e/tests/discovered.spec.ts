// Discovered page — Slice 1 of #1002.
//
// Smoke-tests the corpus-wide browser plus the sidebar nav entry that
// gets users there. The mocked /api/discovered-* layer in fixtures.ts
// returns the canned sample data.

import { test, expect } from '@playwright/test'
import { installApiMocks } from './fixtures'

test.describe('Discovered (corpus-wide)', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
  })

  // #1822 — Discovered moved out of the primary nav into the ⚙
  // Utilities popover (it's a reference surface, not part of the
  // history → trigger → registry loop).
  test('utility menu shows a Discovered row', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('utility-menu-toggle').click()
    await expect(
      page.getByTestId('utility-menu').locator('.nav-row').filter({ hasText: 'Discovered' }),
    ).toBeVisible()
  })

  test('clicking Discovered in the utility menu navigates to /discovered', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('utility-menu-toggle').click()
    await page
      .getByTestId('utility-menu')
      .locator('.nav-row')
      .filter({ hasText: 'Discovered' })
      .click()
    await expect(page).toHaveURL(/\/discovered$/)
    await expect(page.getByRole('heading', { name: 'Discovered' })).toBeVisible()
  })

  test('actions tab renders the seeded rows', async ({ page }) => {
    await page.goto('/discovered')
    // Both sample actions appear with their descriptions.
    await expect(page.getByText('Sign up for a new account')).toBeVisible()
    await expect(page.getByText('Upgrade to a paid plan via Revolut')).toBeVisible()
    // Action ids are rendered as code pills.
    await expect(page.locator('code', { hasText: 'auth.signup' })).toBeVisible()
    await expect(page.locator('code', { hasText: 'billing.upgrade' })).toBeVisible()
  })

  test('tool-calls tab shows tool rows', async ({ page }) => {
    await page.goto('/discovered')
    // #1822 — tab label renamed "Tools" → "Tool calls" (the rows are
    // raw mcp_* call names, not the MCP catalog).
    await page.getByRole('button', { name: /^Tool calls/ }).click()
    await expect(page.getByText('mailpit')).toBeVisible()
    await expect(page.getByText('verify signup email arrived')).toBeVisible()
  })

  test('unexplored branches tab shows branch text', async ({ page }) => {
    await page.goto('/discovered')
    await page.getByRole('button', { name: /^Unexplored/ }).click()
    await expect(page.getByText('Cancel button but never clicked')).toBeVisible()
  })

  test('search box filters actions client-side', async ({ page }) => {
    await page.goto('/discovered')
    await page.locator('input[placeholder*="Search"]').fill('upgrade')
    // The matching action stays, the other doesn't.
    await expect(page.getByText('Upgrade to a paid plan via Revolut')).toBeVisible()
    await expect(page.getByText('Sign up for a new account')).toHaveCount(0)
  })

  test('category filter re-fetches and narrows the list', async ({ page }) => {
    await page.goto('/discovered')
    // Select "billing" — server-side filter returns only the upgrade row.
    await page.locator('select').selectOption('billing')
    await expect(page.getByText('Upgrade to a paid plan via Revolut')).toBeVisible()
    await expect(page.getByText('Sign up for a new account')).toHaveCount(0)
  })
})
