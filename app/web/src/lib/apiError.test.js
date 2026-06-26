import { describe, it, expect } from 'vitest'
import { formatApiError } from './apiError'

describe('formatApiError', () => {
  it('returns string detail as-is', () => {
    const err = {
      response: { data: { detail: 'unknown persona(s): foo' } },
    }
    expect(formatApiError(err)).toBe('unknown persona(s): foo')
  })

  it('formats Pydantic 422 detail array — field: msg', () => {
    // Regression test for the 2026-05-28 bug where the trigger UI
    // showed "Could not start the run: [object Object]" because
    // FastAPI's 422 body is a list of dicts, not a string.
    const err = {
      response: {
        data: {
          detail: [
            {
              type: 'string_pattern_mismatch',
              loc: ['body', 'target_url'],
              msg: "String should match pattern '^https?://[^\\s]+$'",
              input: 'not-a-url',
            },
          ],
        },
      },
    }
    expect(formatApiError(err)).toBe(
      "target_url: String should match pattern '^https?://[^\\s]+$'",
    )
  })

  it('joins multiple Pydantic errors with semicolons', () => {
    const err = {
      response: {
        data: {
          detail: [
            { loc: ['body', 'concurrency'], msg: 'Input should be greater than 0' },
            { loc: ['body', 'max_turns'], msg: 'Input should be less than 5000' },
          ],
        },
      },
    }
    expect(formatApiError(err)).toBe(
      'concurrency: Input should be greater than 0; max_turns: Input should be less than 5000',
    )
  })

  it('handles object-without-loc by surfacing the msg only', () => {
    const err = {
      response: { data: { detail: [{ msg: 'something went sideways' }] } },
    }
    expect(formatApiError(err)).toBe('something went sideways')
  })

  it('falls back to JSON.stringify for non-array object detail', () => {
    const err = {
      response: { data: { detail: { code: 'NOPE', extra: 42 } } },
    }
    expect(formatApiError(err)).toBe('{"code":"NOPE","extra":42}')
  })

  it('falls back to e.message on network failure with no response', () => {
    const err = new Error('Network Error')
    expect(formatApiError(err)).toBe('Network Error')
  })

  it('falls back to "unknown error" when everything is empty', () => {
    expect(formatApiError({})).toBe('unknown error')
    expect(formatApiError(undefined)).toBe('unknown error')
  })

  it('handles entries where the array contains a non-object', () => {
    const err = {
      response: { data: { detail: ['raw string entry'] } },
    }
    expect(formatApiError(err)).toBe('raw string entry')
  })
})
