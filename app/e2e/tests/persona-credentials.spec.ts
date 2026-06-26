// #1105 Slice 1.0 — persona credentials UI on PersonaDetail.
//
// The qa-store layer pins the security contract (status endpoint
// never carries the password). These tests cover the operator-facing
// surface: empty state, populated state, and the reset flow.

import { test, expect } from '@playwright/test'
import { installApiMocks } from './fixtures'

test.describe('Persona credentials row (#1105)', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
  })

  test('empty state explains the lifecycle for a brand-new persona', async ({ page }) => {
    await page.goto('/personas/margaret')
    const row = page.getByTestId('persona-credentials-row')
    await expect(row).toBeVisible()
    await expect(row).toContainText(/No saved login yet/i)
    // Reset button not rendered when there's nothing to reset.
    await expect(
      page.getByTestId('persona-credentials-reset'),
    ).toHaveCount(0)
  })

  test('populated state surfaces email + verified pill', async ({ page }) => {
    // Inject credentials into the mock state via a per-test override.
    // The fixture handler reads state.personas[i].credentials so any
    // route that returns the persona doc will carry it too.
    await page.route('**/api/personas/margaret/credentials/status', (route) =>
      route.fulfill({
        json: {
          has_credentials: true,
          email: 'margaret+r1@testease.example.com',
          registered_at: '2026-05-20T09:00:00Z',
          verified: true,
          last_rotation_n: 0,
          has_session_jwt: true,
          jwt_expires_at: '2026-06-20T09:00:00Z',
        },
      }),
    )
    await page.goto('/personas/margaret')
    const row = page.getByTestId('persona-credentials-row')
    await expect(row).toContainText('margaret+r1@testease.example.com')
    await expect(row).toContainText(/Returning user/i)
    await expect(row).toContainText(/✓ verified/)
    await expect(
      page.getByTestId('persona-credentials-reset'),
    ).toBeVisible()
  })

  test('reset flow confirms and clears the saved login', async ({ page }) => {
    // First page-load: credentials present. After DELETE: cleared.
    let credentialsActive = true
    await page.route('**/api/personas/margaret/credentials/status', (route) => {
      if (credentialsActive) {
        return route.fulfill({
          json: {
            has_credentials: true,
            email: 'margaret+r1@x.com',
            registered_at: '2026-05-20T09:00:00Z',
            verified: true,
            last_rotation_n: 0,
            has_session_jwt: false,
            jwt_expires_at: null,
          },
        })
      }
      return route.fulfill({ json: { has_credentials: false } })
    })
    await page.route('**/api/personas/margaret/credentials', (route) => {
      if (route.request().method() === 'DELETE') {
        credentialsActive = false
        return route.fulfill({ status: 204, body: '' })
      }
      return route.continue()
    })
    page.on('dialog', (dialog) => dialog.accept())

    await page.goto('/personas/margaret')
    await expect(page.getByTestId('persona-credentials-row')).toContainText(
      'margaret+r1@x.com',
    )
    await page.getByTestId('persona-credentials-reset').click()
    // After DELETE the row collapses to the empty-state copy.
    await expect(page.getByTestId('persona-credentials-row')).toContainText(
      /No saved login yet/i,
    )
  })

  test('cancelling the confirm dialog leaves credentials intact', async ({ page }) => {
    await page.route('**/api/personas/margaret/credentials/status', (route) =>
      route.fulfill({
        json: {
          has_credentials: true,
          email: 'margaret+r1@x.com',
          registered_at: '2026-05-20T09:00:00Z',
          verified: true,
          last_rotation_n: 0,
          has_session_jwt: false,
          jwt_expires_at: null,
        },
      }),
    )
    let deleteFired = false
    await page.route('**/api/personas/margaret/credentials', (route) => {
      if (route.request().method() === 'DELETE') {
        deleteFired = true
      }
      return route.continue()
    })
    page.on('dialog', (dialog) => dialog.dismiss())

    await page.goto('/personas/margaret')
    await page.getByTestId('persona-credentials-reset').click()
    // Dismissing the confirm means no DELETE goes out + row stays put.
    await expect(page.getByTestId('persona-credentials-row')).toContainText(
      'margaret+r1@x.com',
    )
    expect(deleteFired).toBe(false)
  })
})
