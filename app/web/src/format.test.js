// Format helpers — these are pure functions used across the studio, so
// a regression in any of them rolls back tens of UI strings at once.

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { formatDate, formatTimestamp, relativeTime } from './format.js'

describe('formatDate', () => {
  it('returns em-dash for null/empty', () => {
    expect(formatDate(null)).toBe('—')
    expect(formatDate('')).toBe('—')
    expect(formatDate(undefined)).toBe('—')
  })

  it('passes through unparseable strings unchanged', () => {
    expect(formatDate('not-a-date')).toBe('not-a-date')
  })

  it('formats an ISO timestamp via toLocaleString', () => {
    const out = formatDate('2026-05-25T12:00:00Z')
    // Locale-specific so we can't assert exact format; assert it's a
    // non-empty string that's NOT the input (i.e. parsing succeeded).
    expect(out).not.toBe('2026-05-25T12:00:00Z')
    expect(out.length).toBeGreaterThan(5)
  })
})

describe('formatTimestamp', () => {
  it('returns em-dash for null/empty', () => {
    expect(formatTimestamp(null)).toBe('—')
  })

  it('uses HH:MM:SS for same-day timestamps', () => {
    const today = new Date()
    today.setHours(14, 30, 45, 0)
    const out = formatTimestamp(today.toISOString())
    // Loose match: two-digit hours, colons, two-digit seconds. AM/PM
    // suffix possible depending on locale.
    expect(out).toMatch(/\d{1,2}:\d{2}/)
  })

  it('uses month-day for prior-day timestamps', () => {
    // A year ago — definitely not today.
    const oneYearAgo = new Date()
    oneYearAgo.setFullYear(oneYearAgo.getFullYear() - 1)
    const out = formatTimestamp(oneYearAgo.toISOString())
    // Should contain a month abbreviation. en-US locale uses "Jan"/"Feb"/etc.
    expect(out).toMatch(/[A-Z][a-z]{2}/)
  })
})

describe('relativeTime', () => {
  // Freeze "now" so the relative outputs are deterministic.
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-05-25T12:00:00Z'))
  })

  it('returns "just now" for <5s ago', () => {
    expect(relativeTime('2026-05-25T11:59:59Z')).toBe('just now')
  })

  it('returns seconds for <60s ago', () => {
    expect(relativeTime('2026-05-25T11:59:30Z')).toBe('30s ago')
  })

  it('returns minutes+seconds for <60m ago', () => {
    expect(relativeTime('2026-05-25T11:55:00Z')).toBe('5m 0s ago')
  })

  it('returns hours+minutes for <24h ago', () => {
    expect(relativeTime('2026-05-25T08:30:00Z')).toBe('3h 30m ago')
  })

  it('falls back to formatDate for >24h ago', () => {
    const out = relativeTime('2026-05-20T12:00:00Z')
    // Not in the "Xs ago" / "Xm ago" / "Xh ago" shape.
    expect(out).not.toMatch(/\d+[hms]/)
  })

  it('returns empty string for null/empty', () => {
    expect(relativeTime('')).toBe('')
    expect(relativeTime(null)).toBe('')
  })
})
