// Activation-toggle e2e — retroactive coverage for #1009.
//
// #1009 introduced the per-persona activation gate (is_active=False by
// default; operator flips it from the Personas page). The relaunch
// shipped with API + Vitest coverage, but no e2e — this spec backfills
// that. Companion to trigger-flow.spec.ts (the Slice 1.5 pair).
//
// Restored after PR #1027's second commit was dropped during squash
// merge.
//
// The toggle button is an "On/Off" pill in the top-right of each
// persona card. Clicking it fires `updatePersona(id, { is_active })`
// with optimistic UI; we assert the PATCH body landed correctly.

import { test, expect } from '@playwright/test'
import { installApiMocks } from './fixtures'

test.describe('Activation toggle — Personas page', () => {
  test('renders the Off pill for inactive personas', async ({ page }) => {
    await installApiMocks(page)
    await page.goto('/personas')
    // SAMPLE_PERSONAS all default to is_active=false → all pills show "Off".
    const offPills = page.getByRole('button', { name: /^Off$/ })
    await expect(offPills.first()).toBeVisible()
    await expect(offPills).toHaveCount(2)
  })

  test('clicking Off flips to On and fires PATCH is_active=true', async ({ page }) => {
    const state = await installApiMocks(page)
    await page.goto('/personas')

    // Find the toggle on Margaret's card. The card title is the display
    // name; the toggle is the "Off" button inside the same card.
    const margaretCard = page.locator('.persona-card', { hasText: 'Margaret Doyle' })
    const toggle = margaretCard.getByRole('button', { name: /^Off$/ })
    await toggle.click()

    // Optimistic UI flips immediately to "On", then the PATCH resolves.
    await expect(
      margaretCard.getByRole('button', { name: /^On$/ }),
    ).toBeVisible({ timeout: 3000 })

    // The mock fixtures PATCH handler merges the body into state, so we
    // can read the post-PATCH state to confirm the wire shape.
    expect(state.personas.find((p) => p.persona_id === 'margaret')?.is_active).toBe(
      true,
    )
  })

  test('clicking On flips back to Off and fires PATCH is_active=false', async ({ page }) => {
    const state = await installApiMocks(page)
    // Seed margaret as already active so the test starts from On.
    state.personas[0].is_active = true
    await page.goto('/personas')

    const margaretCard = page.locator('.persona-card', { hasText: 'Margaret Doyle' })
    const toggle = margaretCard.getByRole('button', { name: /^On$/ })
    await toggle.click()

    await expect(
      margaretCard.getByRole('button', { name: /^Off$/ }),
    ).toBeVisible({ timeout: 3000 })
    expect(state.personas.find((p) => p.persona_id === 'margaret')?.is_active).toBe(
      false,
    )
  })

  test('active personas get a brand-coloured ring', async ({ page }) => {
    const state = await installApiMocks(page)
    state.personas[0].is_active = true
    await page.goto('/personas')
    // The ring class is applied directly on the card when is_active=true.
    // Smoke test: the class shows up on Margaret's card and not Daniel's.
    const margaretCard = page.locator('.persona-card', { hasText: 'Margaret Doyle' })
    const danielCard = page.locator('.persona-card', { hasText: 'Daniel Lee' })
    await expect(margaretCard).toHaveClass(/ring-2/)
    await expect(danielCard).not.toHaveClass(/ring-2/)
  })
})
