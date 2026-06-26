<!--
  #860 — Per-persona Transcript section.

  Renders one persona's step-by-step actions taken during the explore phase:
  each step is one tool call, with the prose the persona narrated before it,
  a compact args summary, and an inline screenshot thumbnail when the tool
  was browser_take_screenshot. Findings made via note_finding link back to
  their step (and the RunDetail page renders "→ jump to step N" on the
  matching finding).

  Self-contained: fetches its own data on mount, owns its own loading/error
  states. The parent RunDetail just drops one of these per persona below
  each review block.

  Collapsed by default — the transcript is verbose (~100–300 steps per
  persona for a real run) and most operators only want the review +
  findings on first read. One click expands; the data only loads on
  expansion (no point paying the round-trip on the closed state).
-->
<template>
  <section class="persona-transcript">
    <div class="header">
      <button class="link toggle" @click="open = !open">
        {{ open ? '▼' : '▶' }} Transcript ({{ stepCount }})
      </button>
      <span v-if="open && error" class="error">{{ error }}</span>
      <span v-else-if="open && loading" class="muted">Loading…</span>
    </div>

    <div v-if="open && !loading && !error" class="steps">
      <p v-if="steps.length === 0" class="muted">
        No steps recorded for this persona. The harness may have run before
        the transcript recorder was wired (#860), or this persona made no
        tool calls.
      </p>

      <div
        v-for="step in steps"
        :key="step.step_n"
        :id="anchorId(step.step_n)"
        :class="['step', { 'is-finding': step.tool_name === NOTE_FINDING_TOOL }]"
      >
        <div class="step-head">
          <span class="step-n">#{{ step.step_n }}</span>
          <span class="tool-name">{{ shortToolName(step.tool_name) }}</span>
          <span v-if="step.args_summary" class="args">
            {{ step.args_summary }}
          </span>
          <span v-if="step.finding_ordinals?.length" class="finding-link">
            ↳ finding
            <template v-for="ord in step.finding_ordinals" :key="ord">
              #{{ ord }}
            </template>
          </span>
        </div>
        <div v-if="step.text_from_persona" class="prose">
          {{ step.text_from_persona }}
        </div>
        <div v-if="step.screenshot_id" class="screenshot">
          <a
            :href="screenshotUrl(runId, step.screenshot_id)"
            target="_blank"
            rel="noopener"
            :title="`Open full-size screenshot from step ${step.step_n}`"
          >
            <img
              :src="screenshotUrl(runId, step.screenshot_id)"
              :alt="`Step ${step.step_n} screenshot`"
              loading="lazy"
            />
          </a>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
import { ref, watch } from 'vue'
import { getTranscript, screenshotUrl } from '../api'

const props = defineProps({
  runId: { type: String, required: true },
  personaId: { type: String, required: true },
})

// Hard-coded so it matches qa_agents/run_recorder.py's _NOTE_FINDING_TOOL.
// A future rename of the tool would need both sides updated; this string
// is the join key.
const NOTE_FINDING_TOOL = 'mcp__findings__note_finding'

const open = ref(false)
const loading = ref(false)
const error = ref('')
const steps = ref([])
// Step count shown in the collapsed header. Computed lazily — the parent
// passes nothing here, so we discover the count on first open. Showing
// `?` before open avoids forcing every persona's transcript to fetch
// just to compute a number nobody asked for.
const stepCount = ref('')

watch(open, async (isOpen) => {
  if (!isOpen || steps.value.length > 0) return  // loaded already
  loading.value = true
  error.value = ''
  try {
    steps.value = await getTranscript(props.runId, props.personaId)
    stepCount.value = String(steps.value.length)
  } catch (e) {
    error.value = `Could not load transcript: ${e.message}`
  } finally {
    loading.value = false
  }
})

function shortToolName(name) {
  // The Playwright + email + findings MCP tools are namespaced with
  // ``mcp__<server>__<tool>``. The server prefix is mostly noise to
  // an operator reading the timeline; the bare tool name is what they
  // recognise. Built-in tools (TaskUpdate, etc.) pass through as-is.
  if (typeof name !== 'string') return ''
  const m = name.match(/^mcp__[^_]+__(.+)$/)
  return m ? m[1] : name
}

function anchorId(stepN) {
  // Used by RunDetail's "→ jump to step N" finding-linkback. Stable
  // string keyed by persona so multiple personas on the same page
  // don't collide.
  return `step-${props.personaId}-${stepN}`
}
</script>

<style scoped>
/* #1822 — translated from the paper theme to the control-room palette
   (ink/brand hexes from tailwind.config.js, hand-inlined because this
   block is plain scoped CSS rather than utility classes). */
.persona-transcript {
  border-top: 1px solid rgb(var(--te-hairline) / 0.08);
  margin-top: 0.5rem;
  padding-top: 0.5rem;
}
.header {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}
.toggle {
  background: none;
  border: none;
  color: rgb(var(--te-brand-700));
  cursor: pointer;
  padding: 0;
  font-size: 0.9rem;
  text-decoration: underline;
}
.muted {
  color: rgb(var(--te-ink-500));
}
.error {
  color: rgb(var(--te-red-400));
}
.steps {
  margin-top: 0.5rem;
}
.step {
  padding: 0.5rem 0.75rem;
  border-left: 3px solid rgb(var(--te-ink-300));
  margin: 0.25rem 0;
  background: rgb(var(--te-ink-100));
  border-radius: 0 4px 4px 0;
  scroll-margin-top: 1rem;  /* anchor scroll lands below the page header */
}
.step.is-finding {
  border-left-color: #f59e0b;
  background: rgba(245, 158, 11, 0.08);
}
.step-head {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  font-size: 0.875rem;
  flex-wrap: wrap;
}
.step-n {
  color: rgb(var(--te-ink-500));
  font-family: monospace;
  font-weight: 600;
  min-width: 3rem;
}
.tool-name {
  font-family: monospace;
  font-weight: 600;
  color: rgb(var(--te-ink-900));
}
.args {
  font-family: monospace;
  color: rgb(var(--te-ink-600));
  word-break: break-all;
}
.finding-link {
  margin-left: auto;
  color: rgb(var(--te-amber-400));
  font-size: 0.8rem;
}
.prose {
  margin-top: 0.4rem;
  color: rgb(var(--te-ink-700));
  font-style: italic;
  font-size: 0.9rem;
  line-height: 1.4;
  white-space: pre-wrap;
}
.screenshot {
  margin-top: 0.5rem;
}
.screenshot img {
  max-width: 100%;
  max-height: 200px;
  border: 1px solid rgb(var(--te-hairline) / 0.12);
  border-radius: 4px;
  cursor: zoom-in;
  background: rgb(var(--te-panel));
}
</style>
