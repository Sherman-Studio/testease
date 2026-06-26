import { test, expect } from '@playwright/test'
import { installApiMocks, SAMPLE_RUN_DETAIL } from './fixtures'

// #1169 (slice 1 of #1168) — Triage view as the new default run-detail
// tab. The bug was: an operator opening a run with 109 findings
// couldn't see what to fix, because severity/kind/regression were all
// collapsed into ~109 same-size chips. The fix is a new top-level tab
// that ranks fixable findings into three buckets (blockers as cards,
// majors as a compact list, the rest collapsed) and is the default.

test.describe('Triage view (#1169)', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
    await page.goto(`/runs/${SAMPLE_RUN_DETAIL.run_id}`)
    await expect(page.locator('[data-testid="triage-view"]')).toBeVisible()
  })

  test('lands on Triage by default (not Timeline)', async ({ page }) => {
    // The contract this slice ships: Triage is the first thing the
    // operator sees. Timeline must still be reachable but it's now
    // a click away. Without this default, the operator opens the
    // page to a wall of 1,000+ timeline events instead of "here's
    // what to fix".
    await expect(page.locator('[data-testid="triage-view"]')).toBeVisible()
    // Timeline-only fixtures (the URL spine, the step rows) should
    // NOT render on initial load.
    await expect(page.locator('text=URLs Visited')).toHaveCount(0)
  })

  test('renders blockers as full-width cards with body shown', async ({ page }) => {
    const blocker = SAMPLE_RUN_DETAIL.findings.find((f) => f.severity === 'blocker')!
    const card = page.locator(`[data-testid="triage-blocker-${blocker.finding_id}"]`)
    await expect(card).toBeVisible()
    await expect(card).toContainText(blocker.title)
    // Body renders inline — no expander click required for blockers.
    await expect(card).toContainText(blocker.body)
    // File-issue affordance is right there on the card.
    await expect(
      card.locator(`[data-testid="triage-file-button-${blocker.finding_id}"]`),
    ).toBeVisible()
  })

  test('renders majors as a compact list that expands on click', async ({ page }) => {
    const major = SAMPLE_RUN_DETAIL.findings.find((f) => f.severity === 'major')!
    const row = page.locator(`[data-testid="triage-major-${major.finding_id}"]`)
    await expect(row).toBeVisible()
    await expect(row).toContainText(major.title)
    // Body is collapsed initially.
    await expect(row).not.toContainText(major.body)
    // Click the row title to expand.
    await row.locator('button').first().click()
    await expect(row).toContainText(major.body)
  })

  test('"Other actionable" section is collapsed by default + opens on click', async ({ page }) => {
    const others = page.locator('[data-testid="triage-others"]')
    await expect(others).toBeVisible()
    const minor = SAMPLE_RUN_DETAIL.findings.find(
      (f) => f.severity === 'minor' && f.kind === 'bug',
    )!
    // Collapsed initially — minor finding is not yet visible.
    await expect(
      page.locator(`[data-testid="triage-other-${minor.finding_id}"]`),
    ).toHaveCount(0)
    // Click the toggle.
    await others.locator('button').first().click()
    await expect(
      page.locator(`[data-testid="triage-other-${minor.finding_id}"]`),
    ).toBeVisible()
  })

  test('praise + observation findings do NOT appear in any Triage section', async ({ page }) => {
    // The whole point of the new view is to keep praise out of the
    // fix-list. The "delightful empty state" praise in the fixture
    // wears severity=nit but kind=praise; it must not render in
    // blockers, majors, or others.
    const praise = SAMPLE_RUN_DETAIL.findings.find((f) => f.kind === 'praise')!
    await expect(
      page.locator(`[data-testid="triage-blocker-${praise.finding_id}"]`),
    ).toHaveCount(0)
    await expect(
      page.locator(`[data-testid="triage-major-${praise.finding_id}"]`),
    ).toHaveCount(0)
    // Expand the others section in case it's collapsed by default.
    await page.locator('[data-testid="triage-others"] button').first().click()
    await expect(
      page.locator(`[data-testid="triage-other-${praise.finding_id}"]`),
    ).toHaveCount(0)
  })

  test('Timeline tab is still reachable from the strip', async ({ page }) => {
    await page.getByRole('button', { name: /Timeline/i }).first().click()
    await expect(page.locator('[data-testid="triage-view"]')).toHaveCount(0)
    await expect(page.locator('text=URLs Visited').first()).toBeVisible()
  })
})
