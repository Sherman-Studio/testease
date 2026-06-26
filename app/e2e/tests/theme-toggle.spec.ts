// #1822 — light/dark theming. Dark ("control room") is the default;
// the sidebar toggle flips :root[data-theme] and persists the choice
// under localStorage["testease.theme"], which the index.html boot
// script applies pre-paint on the next load.

import { test, expect } from '@playwright/test'
import { installApiMocks } from './fixtures'

test.describe('Theme toggle', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
  })

  test('defaults to dark, toggles to light, persists across reloads', async ({ page }) => {
    await page.goto('/')
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')

    await page.getByTestId('theme-toggle').click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
    expect(
      await page.evaluate(() => localStorage.getItem('testease.theme')),
    ).toBe('light')

    // The boot script must re-apply the stored theme before first paint.
    await page.reload()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')

    await page.getByTestId('theme-toggle').click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  })
})
