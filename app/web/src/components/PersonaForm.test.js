// <PersonaForm> — the create + edit form for personas. Tests focus on
// the data-shape contract with parent views: flow-string parsing, empty-
// string → null coercion on submit, initial-data hydration on edit.

import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import PersonaForm from './PersonaForm.vue'

const FULL_PERSONA = {
  persona_id: 'alice',
  display_name: 'Alice Tester',
  registered_email: 'alice@example.com',
  explore_system_prompt: 'You are Alice.',
  report_system_prompt: 'Write a review.',
  flows: ['signup', 'billing'],
  uses_admin_login: false,
  setup_actions: null,
  // #1009 — region + language replace browser_locale.
  region: 'GB',
  language: 'en',
  browser_locale: 'en-GB',
  color_token: 'rose',
  avatar_seed: 'alice',
}

describe('<PersonaForm>', () => {
  it('hides persona_id field when editing', () => {
    const wrapper = mount(PersonaForm, {
      props: { editing: true, initial: FULL_PERSONA },
    })
    // Look for the label text "Persona ID" — absent in edit mode.
    expect(wrapper.text()).not.toContain('Persona ID')
  })

  it('shows persona_id field when creating', () => {
    const wrapper = mount(PersonaForm)
    expect(wrapper.text()).toContain('Persona ID')
  })

  it('hydrates inputs from `initial` on edit', () => {
    const wrapper = mount(PersonaForm, {
      props: { editing: true, initial: FULL_PERSONA },
    })
    const inputs = wrapper.findAll('input')
    const values = inputs.map((i) => i.element.value)
    expect(values).toContain('Alice Tester')
    expect(values).toContain('alice@example.com')
    // #1009 — single browser_locale input got split into Region + Language.
    expect(values).toContain('GB')
    expect(values).toContain('en')
  })

  it('flows render as one-per-line in the textarea', () => {
    // Switched from a comma-separated single-line input to a newline-
    // separated textarea in PR-B — the field was mis-labelled when real
    // data shipped (200–700-char narrative steps, not "tags").
    const wrapper = mount(PersonaForm, {
      props: { editing: true, initial: FULL_PERSONA },
    })
    const textareas = wrapper.findAll('textarea')
    const flowsArea = textareas.find((t) => t.element.value === 'signup\nbilling')
    expect(flowsArea).toBeTruthy()
  })

  it('parses a newline-separated flows textarea into an array on submit', async () => {
    const wrapper = mount(PersonaForm, {
      props: { editing: true, initial: FULL_PERSONA },
    })
    const flowsArea = wrapper.findAll('textarea').find(
      (t) => t.element.value === 'signup\nbilling',
    )
    await flowsArea.setValue('one\ntwo\n  three  ')
    await wrapper.find('form').trigger('submit.prevent')
    const submitted = wrapper.emitted('submit')[0][0]
    expect(submitted.flows).toEqual(['one', 'two', 'three'])
  })

  it('coerces empty optional strings to null on submit', async () => {
    // Without this, the server sees "" which is a valid string and stored
    // verbatim — we want null to mean "no value" consistently.
    const init = {
      ...FULL_PERSONA,
      setup_actions: '',
      browser_locale: '',
      avatar_seed: '',
    }
    const wrapper = mount(PersonaForm, {
      props: { editing: true, initial: init },
    })
    await wrapper.find('form').trigger('submit.prevent')
    const submitted = wrapper.emitted('submit')[0][0]
    expect(submitted.setup_actions).toBeNull()
    expect(submitted.browser_locale).toBeNull()
    expect(submitted.avatar_seed).toBeNull()
  })

  it('emits cancel when the Cancel button is clicked', async () => {
    const wrapper = mount(PersonaForm)
    const cancel = wrapper.findAll('button').find((b) => b.text() === 'Cancel')
    await cancel.trigger('click')
    expect(wrapper.emitted('cancel')).toHaveLength(1)
  })

  it('renders the error banner when an error prop is set', () => {
    const wrapper = mount(PersonaForm, {
      props: { error: 'persona_id already exists' },
    })
    expect(wrapper.text()).toContain('persona_id already exists')
  })
})
