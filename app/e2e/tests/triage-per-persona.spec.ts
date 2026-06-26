import { test, expect } from '@playwright/test'
import { installApiMocks, SAMPLE_RUN_DETAIL } from './fixtures'

// #1172 (slice 4 of #1168) — Per-persona digest cards on the Triage
// view. Below the global blockers/majors/others sections, render one
// card per persona that filed findings. Each card shows:
//   • avatar + display name + archetype one-liner
//   • severity counts as pills
//   • collapsed by default; click expands
//   • top 3 findings with file-issue + view-trace

test.describe('Triage per-persona cards (#1172)', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
    await page.goto(`/runs/${SAMPLE_RUN_DETAIL.run_id}`)
    await expect(page.locator('[data-testid="triage-view"]')).toBeVisible()
  })

  test('renders one card per persona that filed actionable findings', async ({ page }) => {
    // Fixture: all fixable findings are filed by the margaret persona.
    // The card uses persona id as the testid suffix.
    const section = page.locator('[data-testid="triage-per-persona"]')
    await expect(section).toBeVisible()
    await expect(
      page.locator('[data-testid="per-persona-card-margaret"]'),
    ).toBeVisible()
  })

  test('card header surfaces severity counts as pills', async ({ page }) => {
    const card = page.locator('[data-testid="per-persona-card-margaret"]')
    // Fixture has 1 blocker + 2 major + 1 minor actionable findings
    // (the praise lives in the Wins panel, not on the persona card).
    await expect(card).toContainText('1 blocker')
    await expect(card).toContainText('2 major')
    await expect(card).toContainText('1 minor')
  })

  test('card body is collapsed by default', async ({ page }) => {
    await expect(
      page.locator('[data-testid="per-persona-body-margaret"]'),
    ).toHaveCount(0)
  })

  test('clicking the card expands the body', async ({ page }) => {
    await page.locator('[data-testid="per-persona-toggle-margaret"]').click()
    const body = page.locator('[data-testid="per-persona-body-margaret"]')
    await expect(body).toBeVisible()
  })

  test('top 3 findings render with file-issue affordance', async ({ page }) => {
    await page.locator('[data-testid="per-persona-toggle-margaret"]').click()
    const body = page.locator('[data-testid="per-persona-body-margaret"]')
    await expect(body).toBeVisible()
    // Fixable findings sorted by severity → blocker first, then majors.
    const blocker = SAMPLE_RUN_DETAIL.findings.find((f) => f.severity === 'blocker')!
    const finding = body.locator(
      `[data-testid="per-persona-finding-${blocker.finding_id}"]`,
    )
    await expect(finding).toBeVisible()
    await expect(finding).toContainText(blocker.title)
    // File button is there for findings that haven't been filed yet.
    await expect(finding.locator('button', { hasText: 'File' })).toBeVisible()
  })

  test('praise findings do NOT appear on persona cards', async ({ page }) => {
    // Praise findings live in the Wins panel above the Triage view,
    // not on any per-persona card.
    const praise = SAMPLE_RUN_DETAIL.findings.find((f) => f.kind === 'praise')!
    await page.locator('[data-testid="per-persona-toggle-margaret"]').click()
    await expect(
      page.locator(`[data-testid="per-persona-finding-${praise.finding_id}"]`),
    ).toHaveCount(0)
  })
})
