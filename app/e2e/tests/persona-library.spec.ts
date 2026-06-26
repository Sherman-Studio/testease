// Persona library — the centrepiece of the Test Ease redesign.
// Smoke flow covers: list renders, click into detail, edit, save, see
// the change reflected. Mocked API per fixtures.ts.

import { test, expect } from '@playwright/test'
import { installApiMocks } from './fixtures'

test.describe('Persona library', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
  })

  test('lists default personas as cards', async ({ page }) => {
    await page.goto('/personas')
    // #1009 redesign: H1 changed from "Personas" to "Pick your testers";
    // the "default" pill was removed (the activation toggle is now the
    // primary signal). Inverse assertion: non-default personas show a
    // "custom persona" label, the seeded set carries no marker. Mirror
    // of the same fix in PR #1027 — both branches needed it
    // independently since main hasn't picked it up yet.
    await expect(
      page.getByRole('heading', { name: 'Pick your testers' }),
    ).toBeVisible()
    await expect(page.getByText('Margaret Doyle')).toBeVisible()
    await expect(page.getByText('Daniel Lee')).toBeVisible()
    await expect(page.getByText('custom persona')).toHaveCount(0)
  })

  test('navigates to detail when a card is clicked', async ({ page }) => {
    await page.goto('/personas')
    await page.getByText('Margaret Doyle').click()
    await expect(page).toHaveURL(/\/personas\/margaret$/)
    await expect(
      page.getByRole('heading', { name: 'Margaret Doyle' }),
    ).toBeVisible()
    // The detail page shows the persona id below the name.
    await expect(page.getByText('margaret', { exact: true })).toBeVisible()
  })

  test('detail page exposes all four tabs', async ({ page }) => {
    await page.goto('/personas/margaret')
    // #1009 item 2 — "Overview" → "Settings" (id stayed 'overview').
    // #1822 §6 — the Flows tab is gone; flows render as read-only pills
    // on the Settings tab instead (covered below).
    for (const label of ['Settings', 'Prompts', 'Runs', 'Danger zone']) {
      await expect(page.getByRole('button', { name: new RegExp(label) })).toBeVisible()
    }
    await expect(page.getByRole('button', { name: /^Flows/ })).toHaveCount(0)
  })

  test('flows render as pills on the Settings tab (#1822)', async ({ page }) => {
    await page.goto('/personas/margaret')
    // Settings is the default landing tab; the Flows panel sits below
    // the edit form with one pill per flow tag from the fixture.
    await expect(page.getByRole('heading', { name: 'Flows' })).toBeVisible()
    for (const flow of ['signup', 'billing', 'password-reset']) {
      await expect(page.locator('.pill', { hasText: flow })).toBeVisible()
    }
  })

  test('clicking Prompts shows the explore + report prompts', async ({ page }) => {
    await page.goto('/personas/margaret')
    await page.getByRole('button', { name: /Prompts/ }).click()
    await expect(page.getByText('Explore prompt')).toBeVisible()
    await expect(page.getByText('Report prompt')).toBeVisible()
    // Body contains the prompt text we mocked.
    await expect(page.getByText(/Sheffield bookkeeper/)).toBeVisible()
  })

  test('editing display name on the Overview tab persists via PATCH', async ({ page }) => {
    await page.goto('/personas/margaret')
    // Overview tab is the default landing tab — the form is already there.
    // The Display name input sits in the same panel as a label of that text;
    // matching by value is the most robust locator across form layouts.
    const displayInput = page.locator('input').filter({
      hasText: '',
    }).and(page.locator('input[required]')).first()
    // Fallback: find the input whose current value is the persona display name.
    await page.waitForSelector('input', { state: 'attached' })
    const allInputs = page.locator('input')
    const count = await allInputs.count()
    let target = null
    for (let i = 0; i < count; i++) {
      const v = await allInputs.nth(i).inputValue()
      if (v === 'Margaret Doyle') {
        target = allInputs.nth(i)
        break
      }
    }
    expect(target).not.toBeNull()
    await target!.fill('Margaret Edited')

    // Submit triggers a PATCH; the mock writes back into state and returns
    // the updated doc, so the hero heading updates after the save resolves.
    await page.getByRole('button', { name: /Save changes/ }).click()
    await expect(
      page.getByRole('heading', { name: 'Margaret Edited' }),
    ).toBeVisible({ timeout: 5000 })
  })

  test('default personas show Hide (not Delete) on Danger zone', async ({ page }) => {
    await page.goto('/personas/margaret')
    await page.getByRole('button', { name: /Danger zone/ }).click()
    // Default → "Hide persona", not "Delete permanently"
    await expect(page.getByRole('button', { name: /Hide persona/ })).toBeVisible()
    await expect(page.getByRole('button', { name: /Delete permanently/ })).toHaveCount(0)
  })

  test('back link returns to the personas list', async ({ page }) => {
    await page.goto('/personas/margaret')
    await page.getByText('← All personas').click()
    await expect(page).toHaveURL(/\/personas$/)
  })
})
