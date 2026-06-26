// #1089 → #1822 — the Launch button NEVER lies. Pre-#1089 the label
// counted the catalog (25) but the server substituted the activated set
// (2) when the body was empty, so "Run 25 personas" silently ran 2.
// #1822 makes the selection structurally explicit: the picker pre-seeds
// from the activated (★, is_active) set, the POST always carries the
// explicit personas array, and an empty selection disables Launch
// outright — "Launch 0" is impossible to fire.

import { test, expect } from '@playwright/test'
import { installApiMocks, SAMPLE_PERSONAS } from './fixtures'

test.describe('Launch-N label honesty (#1089 / #1822)', () => {
  test('0 activated + 0 selected → Launch disabled and hint visible', async ({ page }) => {
    await installApiMocks(page)
    await page.goto('/new-run')
    // Default SAMPLE_PERSONAS has 2 personas, neither activated — so
    // nothing pre-seeds and the empty-selection label reads "Launch 0".
    await expect(
      page.locator('.cta-summary strong', { hasText: /Launch 0 personas?/i }),
    ).toBeVisible()
    // The button is disabled: a run with nobody in it cannot start.
    await expect(page.getByTestId('cta-start')).toBeDisabled()
    // And the hint tells the operator why.
    const hint = page.getByTestId('persona-selection-hint')
    await expect(hint).toBeVisible()
    await expect(hint).toContainText(/Nothing selected/i)
  })

  test('hint disappears and Launch enables once an explicit selection is made', async ({ page }) => {
    await installApiMocks(page)
    await page.goto('/new-run')
    await expect(page.getByTestId('persona-selection-hint')).toBeVisible()
    // Click the first persona card to add it to the selection.
    await page
      .locator('.persona-card')
      .filter({ hasText: SAMPLE_PERSONAS[0].persona_id })
      .click()
    // The label now reflects the EXPLICIT selection (1) — and the
    // hint hides because the count is unambiguous.
    await expect(
      page.locator('.cta-summary strong', { hasText: /Launch 1 persona/i }),
    ).toBeVisible()
    await expect(page.getByTestId('cta-start')).toBeEnabled()
    await expect(page.getByTestId('cta-start')).toHaveText(/▶ Launch 1/)
    await expect(page.getByTestId('persona-selection-hint')).toHaveCount(0)
  })

  test('activated personas pre-seed the selection and the button says Launch 2', async ({ page }) => {
    // Flip both fixture personas to is_active BEFORE load — the
    // activated set is the starting point for the explicit selection.
    const state = await installApiMocks(page)
    for (const p of state.personas) p.is_active = true
    await page.goto('/new-run')

    await expect(
      page.locator('.cta-summary strong', { hasText: /Launch 2 personas/i }),
    ).toBeVisible()
    await expect(page.getByTestId('cta-start')).toHaveText(/▶ Launch 2/)
    await expect(page.getByTestId('cta-start')).toBeEnabled()
    // No discrepancy → no hint.
    await expect(page.getByTestId('persona-selection-hint')).toHaveCount(0)
  })

  test('the POST body always carries the exact selection the label counted', async ({ page }) => {
    // The end-to-end honesty contract: label says 2 → the API gets
    // exactly those 2. No server-side substitution path remains.
    const state = await installApiMocks(page)
    for (const p of state.personas) p.is_active = true
    page.on('dialog', (dialog) => dialog.accept())
    await page.goto('/new-run')

    const cta = page.getByTestId('cta-start')
    await expect(cta).toHaveText(/▶ Launch 2/)
    await cta.click()

    await expect.poll(() => state.triggerCalls.length).toBe(1)
    expect(new Set(state.triggerCalls[0].personas as string[])).toEqual(
      new Set(['margaret', 'daniel']),
    )
  })
})
