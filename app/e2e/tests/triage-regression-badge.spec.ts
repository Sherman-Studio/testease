import { test, expect } from '@playwright/test'
import { installApiMocks, SAMPLE_RUN_DETAIL } from './fixtures'

// #1171 (slice 3 of #1168) — STILL-BROKEN regression badge on the
// Triage view. A finding with is_regression=true is a previously-fixed
// bug that has come back. The badge:
//   • is magenta to match the memory cockpit's regression pill
//   • carries the STILL-BROKEN label (stronger than the older "Regression")
//   • is a clickable router-link to the prior run when last_verified_run_id
//     is present; tooltip cites the prior run id
//   • appears on the Triage card/row alongside the severity pill so
//     the operator's eye lands on it before the title

test.describe('Triage regression badge (#1171)', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
    await page.goto(`/runs/${SAMPLE_RUN_DETAIL.run_id}`)
    await expect(page.locator('[data-testid="triage-view"]')).toBeVisible()
  })

  test('renders STILL-BROKEN badge on the regression major row', async ({ page }) => {
    const regression = SAMPLE_RUN_DETAIL.findings.find(
      (f) => (f as { is_regression?: boolean }).is_regression === true,
    )!
    const badge = page.locator(`[data-testid="regression-badge-${regression.finding_id}"]`)
    await expect(badge).toBeVisible()
    await expect(badge).toContainText('STILL BROKEN')
  })

  test('badge links to prior run + tooltip cites the prior run id', async ({ page }) => {
    const regression = SAMPLE_RUN_DETAIL.findings.find(
      (f) => (f as { is_regression?: boolean }).is_regression === true,
    )!
    const priorRunId = (regression as { last_verified_run_id: string }).last_verified_run_id
    const badge = page.locator(`[data-testid="regression-badge-${regression.finding_id}"]`)
    await expect(badge).toHaveAttribute('href', `/runs/${priorRunId}`)
    const title = await badge.getAttribute('title')
    expect(title).toContain(priorRunId)
    expect(title).toContain('previously fixed')
  })

  test('non-regression findings do NOT carry the badge', async ({ page }) => {
    const nonReg = SAMPLE_RUN_DETAIL.findings.find(
      (f) => (f as { is_regression?: boolean }).is_regression !== true,
    )!
    await expect(
      page.locator(`[data-testid="regression-badge-${nonReg.finding_id}"]`),
    ).toHaveCount(0)
  })

  test('chip panel still shows the STILL-BROKEN cue (non-clickable span)', async ({ page }) => {
    // The chip-panel chip is a <button> wrapper so the badge there
    // can't be a nested link — it stays as a span. The LABEL still
    // matches the triage variant so the operator's mental model is
    // one cue, two surfaces.
    const regression = SAMPLE_RUN_DETAIL.findings.find(
      (f) => (f as { is_regression?: boolean }).is_regression === true,
    )!
    // The findings chip panel lives on the Timeline tab (the default
    // tab is Triage) — switch there first.
    await page.getByRole('button', { name: /Timeline/i }).first().click()
    const chipBadge = page.locator(
      `[data-testid="finding-regression-badge-${regression.finding_id}"]`,
    )
    await expect(chipBadge).toBeVisible()
    await expect(chipBadge).toContainText('STILL BROKEN')
    // The chip-panel variant is a <span>, not an <a>.
    expect(await chipBadge.evaluate((el) => el.tagName)).toBe('SPAN')
  })
})
