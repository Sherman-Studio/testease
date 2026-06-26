// Slice 3 of the run-detail v2 epic (#1078).
//
// Inline screenshots:
//   - Steps that captured an image (screenshot_id set) render the image
//     as a <figure> inside the step body, with a caption + click-to-
//     enlarge tooltip.
//   - The redundant "Captured screenshot" prose (args_summary from the
//     Slice 1 recorder rewrite) is suppressed when an image is
//     present — the picture IS the content.
//   - The Screenshots filter chip defaults ON so the new images aren't
//     invisible by default after the right-rail sidebar's removal in
//     Slice 2.

import { test, expect } from '@playwright/test'
import { installApiMocks, SAMPLE_RUN_DETAIL } from './fixtures'

test.describe('Inline screenshots (#1078 Slice 3)', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.goto(`/runs/${SAMPLE_RUN_DETAIL.run_id}`)
    await page.getByRole('button', { name: /Timeline/i }).first().click()
  })

  test('Screenshots chip is active by default', async ({ page }) => {
    const screenshotsChip = page.locator(
      '[data-testid=timeline-filters] button',
      { hasText: 'Screenshots' },
    )
    await expect(screenshotsChip).toHaveClass(/bg-brand-100/)
  })

  test('step with a screenshot renders inline <figure> + caption', async ({ page }) => {
    // SAMPLE_RUN_TIMELINE step #62 carries a screenshot_id; with the
    // Slice 3 default the step is visible.
    const fig = page
      .locator('li[data-event-idx]', { hasText: 'step #62' })
      .getByTestId('inline-screenshot')
    await expect(fig).toBeVisible()
    // Caption surfaces the step number + an enlarge hint.
    await expect(fig).toContainText('step #62')
    await expect(fig).toContainText(/click to enlarge/i)
  })

  test('redundant "Captured screenshot" prose is suppressed when image is present', async ({ page }) => {
    // step #70 is a PURE screenshot step. Its args_summary is the
    // Slice-1 prose "Captured screenshot" — the image IS the content
    // so the line above it should be hidden. The figure's caption
    // ("📷 captured at step #N · click to enlarge") is lowercase and
    // shaped differently; we assert the exact uppercased phrase
    // doesn't appear ANYWHERE in the row, which would only be true if
    // the suppression rule fires.
    const row = page.locator('li[data-event-idx]', { hasText: 'step #70' })
    await expect(row.getByTestId('inline-screenshot')).toBeVisible()
    await expect(row).not.toContainText('Captured screenshot')
  })

  test('image is wrapped in a button that opens the lightbox', async ({ page }) => {
    const fig = page
      .locator('li[data-event-idx]', { hasText: 'step #62' })
      .getByTestId('inline-screenshot')
    // The <figure> contains a <button> that triggers the lightbox.
    await expect(fig.locator('button')).toHaveCount(1)
  })
})
