// #1822 follow-up — "Run this persona" on the persona dossier deep-links
// into the New Run console with that persona pre-selected via ?personas=.

import { test, expect } from '@playwright/test'
import { installApiMocks } from './fixtures'

test.describe('Run from persona page', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
  })

  test('dossier button lands on New Run with the persona selected', async ({ page }) => {
    await page.goto('/personas/margaret')
    const btn = page.getByTestId('persona-run-button')
    await expect(btn).toBeVisible()
    await btn.click()

    await expect(page).toHaveURL(/\/new-run\?personas=margaret/)
    // Selection is exactly the deep-linked persona — the launch label
    // never lies (margaret is not activated in the fixtures, so without
    // the query param the selection would be empty).
    await expect(page.getByTestId('cta-start')).toContainText('Launch 1')
    const card = page.locator('.persona-card', { hasText: 'Margaret' })
    await expect(card).toHaveAttribute('aria-pressed', 'true')
  })

  test('unknown ids in the query are dropped silently', async ({ page }) => {
    await page.goto('/new-run?personas=ghost,margaret')
    await expect(page.getByTestId('cta-start')).toContainText('Launch 1')
  })
})
