// Slice 2 of the run-detail v2 epic (#1078).
//
// URL spine — a vertical, sticky left-rail list of URL phases that
// replaces both the horizontal phase ribbon (was Slice E / #1053)
// AND the right-rail screenshot sidebar (was Slice D / #1052). Each
// spine row is click-to-scroll, shows finding + screenshot counts for
// its URL, and gets the brand highlight when its events are in
// viewport. Section headers in the stream below anchor every event
// to the page it happened on.
//
// SAMPLE_RUN_TIMELINE has two browser_navigate events (step #61 → /login
// and step #68 → /profile) producing two spine entries.

import { test, expect } from '@playwright/test'
import { installApiMocks, SAMPLE_RUN_DETAIL } from './fixtures'

test.describe('URL spine (#1078)', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.goto(`/runs/${SAMPLE_RUN_DETAIL.run_id}`)
    await page.getByRole('button', { name: /Timeline/i }).first().click()
  })

  test('spine renders one entry per browser_navigate transition', async ({ page }) => {
    const spine = page.locator('[data-testid=url-spine]')
    await expect(spine).toBeVisible()
    await expect(spine.locator('button')).toHaveCount(2)
  })

  test('entries are labeled with the URL pathname', async ({ page }) => {
    const spine = page.locator('[data-testid=url-spine]')
    // SAMPLE_RUN_TIMELINE navigates to /login then /profile.
    await expect(spine).toContainText('/login')
    await expect(spine).toContainText('/profile')
  })

  test('entry with a finding shows a finding-count pip', async ({ page }) => {
    // The /login phase contains step #67 which has finding_ordinals=[1].
    // The /profile phase has zero findings.
    const spine = page.locator('[data-testid=url-spine]')
    const loginEntry = spine.locator('button', { hasText: '/login' })
    const profileEntry = spine.locator('button', { hasText: '/profile' })
    // The /login row must contain a fixable-count pip (#1115 bucketed
    // the single rose dot into 🐞/✓/🔎 badges — finding #1 is a bug, so
    // it lands in the 🐞 bucket); the /profile row renders no finding
    // pip at all (zero-count badges are suppressed).
    // (.last() — hasText also matches the badge's wrapper span.)
    await expect(
      loginEntry.locator('span').filter({ hasText: '🐞1' }).last(),
    ).toBeVisible()
    await expect(profileEntry.locator('span').filter({ hasText: /🐞|✓|🔎/ })).toHaveCount(0)
  })

  test('clicking a spine entry scrolls the first event of that section into view', async ({ page }) => {
    // Toggle Tools on so step #68 (the /profile navigate) is visible —
    // it's a pure tool call (no narration / no finding / no screenshot)
    // so the default chip set hides it.
    const chip = (label) =>
      page.locator('[data-testid=timeline-filters] button', { hasText: label })
    await chip('Tools').click()
    await page.waitForTimeout(150)
    // Click the /profile spine entry.
    await page
      .locator('[data-testid=url-spine] button', { hasText: '/profile' })
      .click()
    // Step #68 (the navigate that opened /profile) is the first event
    // in section 2 — it should be in viewport after the click.
    await expect(
      page.locator('li[data-event-idx]', { hasText: 'step #68' }),
    ).toBeInViewport()
  })

  test('current spine entry receives the active highlight', async ({ page }) => {
    // The IntersectionObserver fires on mount and currentEventIdx
    // settles to the topmost visible event. With the small fixture,
    // that's idx 0 (step #61 navigate /login) — so the /login row
    // should be the active one initially.
    await page.waitForTimeout(300) // observer settle
    const spine = page.locator('[data-testid=url-spine]')
    const loginEntry = spine.locator('button', { hasText: '/login' })
    await expect(loginEntry).toHaveClass(/bg-brand-600/)
  })

  test('section headers render above the first event of each URL section', async ({ page }) => {
    // The first section header (id 0) sits above step #61's navigate to
    // /login; the second (id 1) sits above step #68's navigate to
    // /profile (which is filtered out by default, so enable Tools first).
    const chip = (label) =>
      page.locator('[data-testid=timeline-filters] button', { hasText: label })
    await chip('Tools').click()
    await page.waitForTimeout(150)
    await expect(page.getByTestId('url-section-0')).toBeVisible()
    await expect(page.getByTestId('url-section-1')).toBeVisible()
    // Each header shows the URL path inline.
    await expect(page.getByTestId('url-section-0')).toContainText('/login')
    await expect(page.getByTestId('url-section-1')).toContainText('/profile')
  })

  test('spine does NOT render when no browser_navigate events exist', async ({ page }) => {
    // Override the timeline endpoint to return events with no navigate
    // calls — the spine must not appear at all.
    await page.route('**/api/runs/qa-run-001/timeline', (route) =>
      route.fulfill({
        json: {
          events: [
            {
              kind: 'step',
              persona_id: 'margaret',
              step_n: 1,
              ts: '2026-05-25T12:00:00Z',
              tool: 'note_finding',
              text_from_persona: 'Just a finding, no navigation.',
              finding_ordinals: [1],
            },
          ],
        },
      }),
    )
    await page.reload()
    await page.getByRole('button', { name: /Timeline/i }).first().click()
    await page.waitForTimeout(300)
    await expect(page.locator('[data-testid=url-spine]')).toHaveCount(0)
  })

  test('right-rail screenshot sidebar is gone (Slice D superseded)', async ({ page }) => {
    // The old `.timeline-sidebar-sticky` rail with the "Page state"
    // header + thumbnail no longer exists — screenshots inline at
    // their step instead (Slice 3 will polish that further).
    await expect(page.locator('.timeline-sidebar-sticky')).toHaveCount(0)
    await expect(page.getByText('Page state', { exact: true })).toHaveCount(0)
  })

  test('horizontal phase ribbon is gone', async ({ page }) => {
    await expect(page.locator('[data-testid=phase-ribbon]')).toHaveCount(0)
  })
})
