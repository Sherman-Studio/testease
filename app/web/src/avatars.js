// Open Peeps avatar generation — produces a data URI SVG keyed by seed.
//
// We use the @dicebear runtime (NOT the dicebear/avatars service URL) so
// nothing ever leaves the browser. The `openPeeps` style ships in
// @dicebear/collection. Seeds are stable strings (typically the persona id),
// so the same persona always gets the same illustration. The hue passed in
// `backgroundColor` tints the open-peeps background swatch to match the
// persona's colour token.

import { createAvatar } from '@dicebear/core'
import { openPeeps } from '@dicebear/collection'

// Tailwind palette → hex (200-weight). Keep in sync with style.css tints.
// Used as the avatar background so the card and the avatar share a tone.
const COLOR_TOKEN_HEX = {
  teal:    'b2f5ea',
  amber:   'fde68a',
  rose:    'fecdd3',
  indigo:  'c7d2fe',
  emerald: 'a7f3d0',
  violet:  'ddd6fe',
  sky:     'bae6fd',
  fuchsia: 'f5d0fe',
  lime:    'd9f99d',
  orange:  'fed7aa',
  cyan:    'a5f3fc',
  slate:   'e2e8f0',
}

const _cache = new Map()

function _key(seed, colorToken) {
  return `${seed}::${colorToken}`
}

export function avatarUri(seed, colorToken = 'slate') {
  if (!seed) seed = 'qa-anon'
  const k = _key(seed, colorToken)
  const hit = _cache.get(k)
  if (hit) return hit

  const bg = COLOR_TOKEN_HEX[colorToken] || COLOR_TOKEN_HEX.slate
  const svg = createAvatar(openPeeps, {
    seed,
    backgroundColor: [bg],
    backgroundType: ['solid'],
    radius: 50,
    size: 128,
  }).toString()

  // Encode as data URI so it slots directly into <img :src=...> with no
  // network call. The Open Peeps SVGs are small (~3–5 KB) — caching the
  // raw string is cheap and avoids the per-mount createAvatar call.
  const uri = `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`
  _cache.set(k, uri)
  return uri
}
