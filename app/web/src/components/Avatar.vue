<template>
  <img
    :src="src"
    :alt="alt"
    :class="['rounded-full ring-2 ring-white shadow-sm', sizeClass]"
    :width="pixelSize"
    :height="pixelSize"
    loading="lazy"
  />
</template>

<script setup>
import { computed } from 'vue'
import { avatarUri } from '../avatars.js'

const props = defineProps({
  seed: { type: String, default: 'qa-anon' },
  colorToken: { type: String, default: 'slate' },
  size: { type: String, default: 'md' }, // xs | sm | md | lg | xl
  alt: { type: String, default: '' },
})

const SIZE_TO_CLASS = {
  xs: 'h-6 w-6',
  sm: 'h-8 w-8',
  md: 'h-12 w-12',
  lg: 'h-16 w-16',
  xl: 'h-24 w-24',
}
const SIZE_TO_PX = { xs: 24, sm: 32, md: 48, lg: 64, xl: 96 }

const sizeClass = computed(() => SIZE_TO_CLASS[props.size] || SIZE_TO_CLASS.md)
const pixelSize = computed(() => SIZE_TO_PX[props.size] || SIZE_TO_PX.md)
const src = computed(() => avatarUri(props.seed, props.colorToken))
</script>
