// #1089 — bulk activate / deactivate on the Personas page. The
// previous default ("23 personas inactive after a fresh seed")
// meant operators had to click 23 individual toggles to get a
// full-fleet run. The new button does it in one click.

import { test, expect } from '@playwright/test'
import { installApiMocks } from './fixtures'

test.describe('Personas — bulk activate/deactivate (#1089)', () => {
  test('"Activate all" appears when at least one persona is inactive', async ({ page }) => {
    await installApiMocks(page)
    await page.goto('/personas')
    // Default SAMPLE_PERSONAS: 2 personas, both is_active=false.
    const btn = page.getByTestId('personas-activate-all')
    await expect(btn).toBeVisible()
    await expect(btn).toContainText(/Activate all \(2\)/i)
  })

  test('clicking Activate all flips every visible persona ON', async ({ page }) => {
    await installApiMocks(page)
    await page.goto('/personas')
    await page.getByTestId('personas-activate-all').click()
    // After bulk activate the button mode flips to Deactivate.
    await expect(page.getByTestId('personas-deactivate-all')).toBeVisible()
    await expect(page.getByTestId('personas-deactivate-all')).toContainText(
      /Deactivate all \(2\)/i,
    )
    // The "Activate all" button is gone (everyone's on).
    await expect(page.getByTestId('personas-activate-all')).toHaveCount(0)
  })

  test('Deactivate all confirms before clearing the set', async ({ page }) => {
    // Get to the all-activated state by clicking Activate all first,
    // then exercise the deactivate flow on top of that.
    await installApiMocks(page)
    let confirmMessage = ''
    page.on('dialog', (dialog) => {
      confirmMessage = dialog.message()
      dialog.dismiss() // Cancel — verify no patches fire.
    })
    await page.goto('/personas')
    await page.getByTestId('personas-activate-all').click()
    // Wait for the bulk PATCHes to settle.
    await expect(page.getByTestId('personas-deactivate-all')).toBeVisible()
    await page.getByTestId('personas-deactivate-all').click()
    expect(confirmMessage).toContain('Deactivate all 2')
    expect(confirmMessage).toContain('refuse to start')
    // Dismissal preserves the state — Deactivate button still there.
    await expect(page.getByTestId('personas-deactivate-all')).toBeVisible()
  })

  test('Deactivate all clears active state when confirmed', async ({ page }) => {
    await installApiMocks(page)
    let acceptedDialogs = 0
    page.on('dialog', (dialog) => {
      acceptedDialogs += 1
      dialog.accept()
    })
    await page.goto('/personas')
    await page.getByTestId('personas-activate-all').click()
    await expect(page.getByTestId('personas-deactivate-all')).toBeVisible()
    await page.getByTestId('personas-deactivate-all').click()
    // The activate button reappears after the confirmed deactivate.
    await expect(page.getByTestId('personas-activate-all')).toBeVisible()
    expect(acceptedDialogs).toBe(1)
  })
})
