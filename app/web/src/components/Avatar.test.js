// Component test for <Avatar> — the persona portrait used across
// the studio (cards, run timeline, finding chips, etc).
//
// Asserts: src/seed mapping, alt text passthrough, size variant
// resolution. The underlying SVG generation is covered separately in
// avatars.test.js.

import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import Avatar from './Avatar.vue'

describe('<Avatar>', () => {
  it('renders an <img> with a data-URI src', () => {
    const wrapper = mount(Avatar, {
      props: { seed: 'alice', colorToken: 'teal' },
    })
    const img = wrapper.find('img')
    expect(img.exists()).toBe(true)
    expect(img.attributes('src')).toMatch(/^data:image\/svg\+xml;utf8,/)
  })

  it('passes alt text through unchanged', () => {
    const wrapper = mount(Avatar, {
      props: { seed: 'alice', colorToken: 'teal', alt: 'Alice Tester' },
    })
    expect(wrapper.find('img').attributes('alt')).toBe('Alice Tester')
  })

  it.each([
    ['xs', 'h-6 w-6', 24],
    ['sm', 'h-8 w-8', 32],
    ['md', 'h-12 w-12', 48],
    ['lg', 'h-16 w-16', 64],
    ['xl', 'h-24 w-24', 96],
  ])(
    'size=%s resolves to %s and %dpx',
    (size, cls, px) => {
      const wrapper = mount(Avatar, {
        props: { seed: 'alice', colorToken: 'teal', size },
      })
      const img = wrapper.find('img')
      expect(img.classes()).toEqual(expect.arrayContaining(cls.split(' ')))
      expect(Number(img.attributes('width'))).toBe(px)
      expect(Number(img.attributes('height'))).toBe(px)
    },
  )

  it('defaults to md when size is omitted', () => {
    const wrapper = mount(Avatar, { props: { seed: 'alice' } })
    expect(wrapper.find('img').classes()).toEqual(
      expect.arrayContaining(['h-12', 'w-12']),
    )
  })

  it('unknown size falls back to md', () => {
    const wrapper = mount(Avatar, {
      props: { seed: 'alice', size: 'huge' },
    })
    expect(wrapper.find('img').classes()).toEqual(
      expect.arrayContaining(['h-12', 'w-12']),
    )
  })
})
