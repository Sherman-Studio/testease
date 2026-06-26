<script setup>
// #1171 (slice 3 of #1168) — STILL-BROKEN regression badge. A finding
// with `is_regression=true` is a previously-fixed bug that has come
// back. That's high-signal information — a bug we *thought* was fixed
// isn't — and the persona currently leans on textual cues like
// "Re-verify [qa-…Z:5] STILL BROKEN — …" in the finding title.
//
// This component upgrades the existing magenta "⚠ Regression" pill
// (originally from #1106 slice 2.2) so it's clickable: clicking
// navigates to the run that last verified the fix. When
// `last_verified_run_id` is missing (legacy findings) it degrades to a
// non-clickable span with a generic tooltip.
//
// The pill colour matches the memory cockpit's regression pill so the
// operator's eye links the two surfaces.
import { computed } from 'vue'

const props = defineProps({
  finding: { type: Object, required: true },
})

const priorRunId = computed(() => props.finding?.last_verified_run_id || '')

const tooltip = computed(() => {
  if (priorRunId.value) {
    return `Regression — previously fixed in run ${priorRunId.value}, now broken again. Click to open the prior run.`
  }
  return 'Regression — previously fixed, now broken again.'
})

// The two surfaces share class names so the visual is identical
// whether the badge ends up as a router-link or a plain span.
const PILL_CLASS =
  'rounded-full bg-fuchsia-500/10 px-1.5 py-0.5 text-[10px] font-semibold ' +
  'text-fuchsia-300 ring-1 ring-fuchsia-500/30 hover:bg-fuchsia-500/20'
</script>

<template>
  <span v-if="!finding?.is_regression" />
  <router-link
    v-else-if="priorRunId"
    :to="`/runs/${priorRunId}`"
    :class="PILL_CLASS"
    :title="tooltip"
    :data-testid="`regression-badge-${finding.finding_id}`"
    @click.stop
  >⚠ STILL BROKEN</router-link>
  <span
    v-else
    :class="PILL_CLASS"
    :title="tooltip"
    :data-testid="`regression-badge-${finding.finding_id}`"
  >⚠ Regression</span>
</template>
