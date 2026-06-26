<template>
  <span class="flex flex-wrap items-center gap-1">
    <template v-for="sev in order" :key="sev">
      <span
        v-if="counts && counts[sev]"
        class="pill"
        :class="`pill-${sev}`"
        :aria-label="`${counts[sev]} ${sev}`"
      >
        <span class="sev-glyph" aria-hidden="true">{{ GLYPH[sev] }}</span>
        {{ counts[sev] }} {{ sev }}
      </span>
    </template>
    <span v-if="total === 0" class="pill">none</span>
  </span>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  counts: { type: Object, default: () => ({}) },
})

const order = ['blocker', 'major', 'minor', 'nit']

// Glyph prefix so severity is distinguishable without colour — fixes the
// colorblind-accessibility gap where the pills were red/orange/amber/grey
// only. Filled circle (worst) → outline diamond (best).
const GLYPH = {
  blocker: '●',
  major: '◆',
  minor: '▲',
  nit: '·',
}

const total = computed(() =>
  order.reduce((n, s) => n + (props.counts?.[s] || 0), 0),
)
</script>

<style scoped>
.sev-glyph {
  font-size: 0.7em;
  line-height: 1;
}
</style>
