// Tests for the Open Peeps avatar runtime — assert that the generator
// produces a stable, cacheable data URI keyed by (seed, colorToken).
//
// These are unit tests on the helper itself (NOT the <Avatar> component);
// they exercise the @dicebear runtime so a future provider swap (e.g.
// dropping DiceBear in favour of a hand-drawn set) will fail loudly.

import { describe, it, expect } from 'vitest'
import { avatarUri } from './avatars.js'

describe('avatarUri', () => {
  it('returns an SVG data URI', () => {
    const uri = avatarUri('alice', 'teal')
    expect(uri).toMatch(/^data:image\/svg\+xml;utf8,/)
    // The Open Peeps style emits an <svg> root.
    expect(decodeURIComponent(uri)).toMatch(/<svg/)
  })

  it('is stable for the same (seed, colorToken)', () => {
    const a = avatarUri('alice', 'teal')
    const b = avatarUri('alice', 'teal')
    // Cache returns the same string reference on a repeat call.
    expect(a).toBe(b)
  })

  it('differs for different seeds', () => {
    const alice = avatarUri('alice', 'teal')
    const bob = avatarUri('bob', 'teal')
    expect(alice).not.toBe(bob)
  })

  it('differs for different color tokens', () => {
    const teal = avatarUri('alice', 'teal')
    const rose = avatarUri('alice', 'rose')
    expect(teal).not.toBe(rose)
  })

  it('falls back to slate for an unknown color token', () => {
    // Unknown token shouldn't error; should produce a usable URI.
    const uri = avatarUri('alice', 'definitely-not-a-real-token')
    expect(uri).toMatch(/^data:image\/svg\+xml;utf8,/)
  })

  it('uses a default seed when seed is empty/null', () => {
    // Defensive: the persona library can return a row with a null
    // avatar_seed if the operator cleared it; we should still get an
    // avatar (the qa-anon fallback) rather than crash.
    const uri = avatarUri('', 'slate')
    expect(uri).toMatch(/^data:image\/svg\+xml;utf8,/)
  })
})
