// Trigger flow e2e — the primary user action in Test Ease.
//
// Pre-#1018 this surface had ZERO e2e coverage; #1018 (the per-trigger
// target_url field) shipped with API tests + Vitest API-shape tests but
// no end-to-end. This spec exercises the trigger console from the
// user's POV: fill the form, accept the confirm dialog, assert the
// POST body Test Ease built.
//
// #1822 moved the form from the Runs home to its own /new-run route
// and made persona selection EXPLICIT: with the fixture personas
// (margaret + daniel, both is_active=false) nothing pre-seeds, so each
// test picks its persona card before launching. The target URL field
// is now pre-filled with the in-cluster sandbox default — hitting
// Launch untouched sends that default explicitly; clearing the field
// preserves the old "omit → server decides" contract.
//
// All API calls are mocked via the fixtures handler — these are fast
// SPA-wiring smoke tests, not backend integration. The trigger handler
// captures POST bodies into `state.triggerCalls` so assertions read the
// real request the SPA sent.

import { test, expect } from '@playwright/test'
import { installApiMocks } from './fixtures'

const SANDBOX_URL = 'https://sandbox.slyreply.ai'

function targetUrlInput(page) {
  return page.getByLabel('Target URL')
}

// Explicit selection (#1822): click the margaret persona card, then the
// Launch CTA. The button label counts the explicit selection.
async function selectMargaretAndLaunch(page) {
  await page.locator('.persona-card').filter({ hasText: 'margaret' }).click()
  const cta = page.getByTestId('cta-start')
  await expect(cta).toHaveText(/▶ Launch 1/)
  await cta.click()
}

test.describe('Trigger flow — New Run console', () => {
  test('renders the Target URL field before the persona picker', async ({ page }) => {
    await installApiMocks(page)
    await page.goto('/new-run')
    // The Target URL field comes BEFORE persona selection — "which site
    // are we testing?" is more fundamental than "which personas?".
    // Section 01 is Target, section 02 is Who runs.
    const sections = page.locator('section.section')
    await expect(sections.nth(0)).toContainText('Target')
    await expect(sections.nth(0).getByLabel('Target URL')).toBeVisible()
    await expect(sections.nth(1)).toContainText('Who runs')
    // #1822 — pre-filled with the in-cluster sandbox so an untouched
    // Launch reproduces the historical "test the sandbox" behaviour
    // explicitly rather than implicitly.
    await expect(targetUrlInput(page)).toHaveValue(SANDBOX_URL)
  })

  test('sends the sandbox default as target_url when the field is untouched', async ({ page }) => {
    const state = await installApiMocks(page)
    // Auto-accept the confirm dialog so the POST fires. window.confirm
    // surfaces in Playwright as a `dialog` event.
    page.on('dialog', (dialog) => dialog.accept())
    await page.goto('/new-run')

    await selectMargaretAndLaunch(page)

    await expect.poll(() => state.triggerCalls.length).toBe(1)
    const body = state.triggerCalls[0]
    // The pre-filled sandbox URL goes out explicitly (#1822) — the POST
    // body ALWAYS carries the explicit personas array too.
    expect(body.target_url).toBe(SANDBOX_URL)
    expect(body.personas).toEqual(['margaret'])
    // Max-only billing: there is no backend selector, so the POST body
    // must never carry a `backend` field.
    expect('backend' in body).toBe(false)
  })

  test('omits target_url from POST body when the field is cleared', async ({ page }) => {
    const state = await installApiMocks(page)
    page.on('dialog', (dialog) => dialog.accept())
    await page.goto('/new-run')

    // Cleared field ⇒ no target_url key in the body. The server falls
    // through to the CronJob template default. This is the pre-#1018
    // behaviour preservation contract.
    await targetUrlInput(page).fill('')
    await selectMargaretAndLaunch(page)

    await expect.poll(() => state.triggerCalls.length).toBe(1)
    const body = state.triggerCalls[0]
    expect('target_url' in body).toBe(false)
    expect(body.personas).toEqual(['margaret'])
  })

  test('forwards target_url in POST body when the field is filled', async ({ page }) => {
    const state = await installApiMocks(page)
    page.on('dialog', (dialog) => dialog.accept())
    await page.goto('/new-run')

    await targetUrlInput(page).fill('https://staging.example.com')
    await selectMargaretAndLaunch(page)

    await expect.poll(() => state.triggerCalls.length).toBe(1)
    const body = state.triggerCalls[0]
    expect(body.target_url).toBe('https://staging.example.com')
    expect(body.personas).toEqual(['margaret'])
  })

  test('trims whitespace before sending — pastes from chat are forgiving', async ({ page }) => {
    const state = await installApiMocks(page)
    page.on('dialog', (dialog) => dialog.accept())
    await page.goto('/new-run')

    // Leading + trailing whitespace is the classic Slack-paste failure.
    // v-model.trim on the input handles it client-side; api.js then
    // strips again as belt-and-braces.
    await targetUrlInput(page).fill('  https://staging.example.com  ')
    await selectMargaretAndLaunch(page)

    await expect.poll(() => state.triggerCalls.length).toBe(1)
    expect(state.triggerCalls[0].target_url).toBe('https://staging.example.com')
  })

  test('confirm dialog surfaces the chosen target URL', async ({ page }) => {
    await installApiMocks(page)
    let confirmMessage = ''
    page.on('dialog', (dialog) => {
      confirmMessage = dialog.message()
      dialog.dismiss() // don't actually submit
    })
    await page.goto('/new-run')

    await targetUrlInput(page).fill('https://staging.example.com')
    await selectMargaretAndLaunch(page)

    // Operator pasted the wrong URL? They spot it in the confirm before
    // any Claude credits get burned — this is the safety net the dialog
    // exists for. The exact line format is "target URL: <value>".
    await expect.poll(() => confirmMessage).toContain(
      'target URL: https://staging.example.com',
    )
  })

  test('confirm dialog says "in-cluster sandbox" when the URL is cleared', async ({ page }) => {
    await installApiMocks(page)
    let confirmMessage = ''
    page.on('dialog', (dialog) => {
      confirmMessage = dialog.message()
      dialog.dismiss()
    })
    await page.goto('/new-run')
    await targetUrlInput(page).fill('')
    await selectMargaretAndLaunch(page)

    // The "default (in-cluster sandbox)" phrasing matches the comment
    // in NewRun.vue's trigger() function. If a future change renames
    // the default phrasing this test should be updated in lockstep.
    await expect.poll(() => confirmMessage).toContain(
      'target URL: default (in-cluster sandbox)',
    )
  })
})
