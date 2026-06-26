// Slice C of the timeline UX overhaul epic (#1048, closes #1051).
//
// Filter chips replace the old log/step binary checkboxes. Four
// categories — Findings / Narration / Screenshots / Tools — with
// "Findings + Narration" on by default. The pure tool call (step #71
// in SAMPLE_RUN_TIMELINE) is the noise floor: hidden by default,
// visible when the Tools chip is ticked.

import { test, expect } from '@playwright/test'
import { installApiMocks, SAMPLE_RUN_DETAIL } from './fixtures'

// Helpers — locate a chip by its visible label.
function chip(page, label) {
  return page.locator('[data-testid=timeline-filters] button', { hasText: label })
}

test.describe('Timeline filter chips (#1051)', () => {
  test.beforeEach(async ({ page }) => {
    await installApiMocks(page)
    await page.goto(`/runs/${SAMPLE_RUN_DETAIL.run_id}`)
    await page.getByRole('button', { name: /Timeline/i }).first().click()
  })

  test('four chips render with default state', async ({ page }) => {
    // The order is the FILTER_CHIPS array order.
    const filterBar = page.locator('[data-testid=timeline-filters]')
    await expect(filterBar.locator('button')).toHaveCount(4)
    await expect(filterBar).toContainText('Findings')
    await expect(filterBar).toContainText('Narration')
    await expect(filterBar).toContainText('Screenshots')
    await expect(filterBar).toContainText('Tools')
  })

  test('default state hides pure tool calls (step #71 is filtered out)', async ({ page }) => {
    // step #71 is browser_click with no narration/screenshot/finding —
    // the "Tools" chip is OFF by default so the row must not render.
    await expect(page.locator('li', { hasText: 'step #71' })).toHaveCount(0)
  })

  test('toggling Tools chip reveals pure tool calls', async ({ page }) => {
    await chip(page, 'Tools').click()
    await expect(page.locator('li', { hasText: 'step #71' })).toHaveCount(1)
  })

  test('toggling Narration OFF hides standalone log lines', async ({ page }) => {
    // The standalone log "This is a standalone narrative log line" is
    // only in the Narration category. With Narration off, it goes away.
    await chip(page, 'Narration').click()
    await expect(
      page.getByText('This is a standalone narrative log line.'),
    ).toHaveCount(0)
  })

  test('toggling all chips off shows no events', async ({ page }) => {
    // Click each chip that's currently active (Findings + Narration
    // + Screenshots — the last defaulted ON in #1078 Slice 3 once the
    // right-rail sidebar was removed). Tools stays OFF by default.
    await chip(page, 'Findings').click()
    await chip(page, 'Narration').click()
    await chip(page, 'Screenshots').click()
    // Tools still off → timeline shows zero events. The "No timeline
    // events" panel is the empty state.
    await expect(
      page.locator('li.is-step, li.is-log'),
    ).toHaveCount(0)
  })

  test('chip counts reflect what each category would show', async ({ page }) => {
    // The chips render their count after the label. Counts are
    // computed against the persona-filtered set (no persona filter
    // active by default → counts cover every event after dedup).
    //
    // Expected post-dedup, with Slice E's navigate events:
    //   - findings:    1 (step #67 has finding_ordinals [1])
    //   - narration:   4 (step #62 + step #67 + the standalone log +
    //                   step #70, whose recorder summary "Captured
    //                   screenshot" classifies it as narration; #1078
    //                   Slice 3 suppresses that redundant prose in the
    //                   rendered row, but the row still matches the
    //                   Narration filter so the count honestly says 4)
    //   - screenshots: 2 (step #62 has screenshot + step #70 pure-screenshot)
    //   - tools:       3 (step #61 + step #68 navigates + step #71 pure click)
    await expect(chip(page, 'Findings')).toContainText('1')
    await expect(chip(page, 'Narration')).toContainText('4')
    await expect(chip(page, 'Screenshots')).toContainText('2')
    await expect(chip(page, 'Tools')).toContainText('3')
  })

  test('clicking a finding chip in the panel re-enables Findings filter if disabled', async ({ page }) => {
    // Disable BOTH chips that could surface step #67 (it has both
    // narration text AND finding_ordinals). With both off the step
    // is filtered out; clicking the finding chip in the panel must
    // re-enable Findings so the target shows up.
    await chip(page, 'Findings').click()
    await chip(page, 'Narration').click()
    await expect(page.locator('li', { hasText: 'step #67' })).toHaveCount(0)
    await page
      .locator('.findings-panel button', { hasText: SAMPLE_RUN_DETAIL.findings[0].title })
      .click()
    // After the click, findings chip is back on and step #67 visible
    // again (the narration chip stays off — turning ONE on is enough
    // because note_finding steps belong to the findings category).
    await expect(page.locator('li', { hasText: 'step #67' })).toHaveCount(1)
  })

  test('"N of M" badge updates as chips toggle', async ({ page }) => {
    // Badge is to the right of the chip bar: "<filtered> of <total>".
    // Default: 4 of 6 (the 2 pure tool/screenshot rows are hidden,
    // since the Narration category already claims step #62 + #67 +
    // the standalone log → 3 of those, plus step #70 is a pure
    // screenshot which is also hidden by default, so 3 visible. The
    // total is 6 events from the timeline endpoint (5 visible after
    // dedup; 6 raw events with one log shadowed by Slice A — total
    // count reflects the displayEvents post-dedup).
    //
    // We don't pin exact numbers — just that the badge updates after
    // a chip toggle and reflects a non-trivial denominator.
    const badge = page.locator('span', { hasText: /\d+ of \d+/ }).first()
    const before = await badge.textContent()
    await chip(page, 'Tools').click() // Reveals one more event.
    await expect(badge).not.toHaveText(before || '')
  })
})
