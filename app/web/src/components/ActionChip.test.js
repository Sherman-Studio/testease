// <ActionChip> — the draggable coverage-action chip used by the
// scenario builder. Two behavioural surfaces: optionally draggable
// (in catalog) and optionally removable (in mandatory lane).

import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ActionChip from './ActionChip.vue'

const action = {
  id: 'sign-up-as-pro',
  category: 'auth',
  human_description: 'Sign up as a Pro user',
}

describe('<ActionChip>', () => {
  it('renders the action id and description', () => {
    const wrapper = mount(ActionChip, { props: { action } })
    expect(wrapper.text()).toContain('sign-up-as-pro')
    expect(wrapper.text()).toContain('Sign up as a Pro user')
  })

  it('shows the description as the title (hover tooltip)', () => {
    const wrapper = mount(ActionChip, { props: { action } })
    expect(wrapper.attributes('title')).toBe('Sign up as a Pro user')
  })

  it('adds cursor-grab class when draggable', () => {
    const wrapper = mount(ActionChip, {
      props: { action, draggable: true },
    })
    expect(wrapper.classes()).toEqual(expect.arrayContaining(['cursor-grab']))
  })

  it('does NOT add cursor-grab class when not draggable', () => {
    const wrapper = mount(ActionChip, { props: { action } })
    expect(wrapper.classes()).not.toEqual(expect.arrayContaining(['cursor-grab']))
  })

  it('renders the remove button only when removable=true', () => {
    const noBtn = mount(ActionChip, { props: { action } })
    expect(noBtn.find('button').exists()).toBe(false)

    const withBtn = mount(ActionChip, {
      props: { action, removable: true },
    })
    expect(withBtn.find('button').exists()).toBe(true)
  })

  it('emits remove when the remove button is clicked', async () => {
    const wrapper = mount(ActionChip, {
      props: { action, removable: true },
    })
    await wrapper.find('button').trigger('click')
    expect(wrapper.emitted('remove')).toHaveLength(1)
  })
})
