<template>
  <span ref="root" class="relative inline-flex align-middle">
    <button
      type="button"
      class="inline-flex h-4 w-4 items-center justify-center rounded-full border border-ink-300 text-[10px] font-semibold leading-none text-ink-500 transition hover:border-brand-600 hover:text-brand-700"
      :aria-expanded="open"
      :aria-label="label ? `What is ${label}?` : 'What is this?'"
      data-testid="helptip"
      @click.stop="toggle"
    >
      ?
    </button>
    <span
      v-if="open"
      class="panel absolute left-0 top-6 z-40 w-72 p-3 text-xs leading-relaxed text-ink-700 shadow-xl"
      data-testid="helptip-pop"
      @click.stop
    >
      <span v-if="label" class="mb-1 block text-[10px] font-semibold uppercase tracking-wide text-ink-500">
        {{ label }}
      </span>
      <slot>{{ text }}</slot>
    </span>
  </span>
</template>

<script setup>
// A small "(?)" affordance that toggles a plain-language explanation of a
// concept, in place. Closes on outside-click + Escape. Pass copy via the
// default slot or the `text` prop; `label` titles the popover.
import { onBeforeUnmount, onMounted, ref } from 'vue'

defineProps({
  label: { type: String, default: '' },
  text: { type: String, default: '' },
})

const open = ref(false)
const root = ref(null)

function toggle() {
  open.value = !open.value
}
function onDocClick(e) {
  if (open.value && root.value && !root.value.contains(e.target)) open.value = false
}
function onKey(e) {
  if (e.key === 'Escape') open.value = false
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKey)
})
</script>
