<template>
  <form @submit.prevent="onSubmit" class="space-y-4">
    <div v-if="error" class="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
      {{ error }}
    </div>

    <div class="grid grid-cols-2 gap-3">
      <div v-if="!editing">
        <label class="label">Persona ID</label>
        <input
          v-model="form.persona_id"
          class="input font-mono"
          placeholder="newuser"
          required
          pattern="[a-z][a-z0-9_-]*"
        />
      </div>
      <div :class="editing ? 'col-span-2' : ''">
        <label class="label">Display name</label>
        <input
          v-model="form.display_name"
          class="input"
          placeholder="Jane Doe"
          required
        />
      </div>
    </div>

    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="label">Registered email</label>
        <input
          v-model="form.registered_email"
          type="email"
          class="input"
          placeholder="jane@example.com"
          required
        />
      </div>
      <div class="grid grid-cols-2 gap-2">
        <div>
          <label class="label">Region</label>
          <input
            v-model="form.region"
            class="input"
            placeholder="GB"
            maxlength="8"
            title="BCP-47 region: GB, US, ES, JP, …"
          />
        </div>
        <div>
          <label class="label">Language</label>
          <input
            v-model="form.language"
            class="input"
            placeholder="en"
            maxlength="8"
            title="BCP-47 language: en, es, fr, …"
          />
        </div>
      </div>
    </div>

    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="label">Colour</label>
        <!-- Swatches stay the real identity colours (decorative, not
             severity — the one sanctioned exception to "cyan only").
             Selection is signalled by the cyan ring per #1822. -->
        <div class="flex flex-wrap gap-1.5">
          <button
            type="button"
            v-for="token in COLOR_TOKENS"
            :key="token"
            class="h-7 w-7 rounded-full ring-2 ring-offset-2 ring-offset-panel transition"
            :class="[
              SWATCH_BG[token],
              form.color_token === token
                ? 'ring-brand-500'
                : 'ring-transparent opacity-70 hover:opacity-100',
            ]"
            :title="token"
            @click="form.color_token = token"
          />
        </div>
      </div>
      <div>
        <label class="label">Avatar seed</label>
        <div class="flex items-center gap-2">
          <Avatar
            :seed="form.avatar_seed || form.persona_id || 'preview'"
            :color-token="form.color_token"
            size="md"
          />
          <input
            v-model="form.avatar_seed"
            class="input"
            :placeholder="form.persona_id || 'auto'"
          />
        </div>
      </div>
    </div>

    <fieldset class="space-y-3 rounded-md border border-ink-200 bg-ink-50/60 p-3">
      <legend class="px-1 text-xs font-medium uppercase tracking-wide text-ink-600">
        Run behaviour
      </legend>
      <label class="flex items-center gap-2 text-sm text-ink-700">
        <input type="checkbox" v-model="form.uses_admin_login" class="rounded border-ink-300" />
        Use admin login (skip the marketing site, log in directly as admin@)
      </label>
      <div>
        <label class="label">Mandatory steps (one per line)</label>
        <textarea
          :value="flowsStr"
          @input="onFlowsInput"
          class="textarea h-32 font-sans text-sm leading-snug"
          :placeholder="FLOWS_PLACEHOLDER"
        ></textarea>
        <p class="help mt-1">
          Each line is one step the persona must attempt during the explore
          phase. Free-form prose is fine — these are appended to the explore
          prompt verbatim. Empty lines are ignored.
        </p>
      </div>
    </fieldset>

    <div>
      <label class="label">Explore system prompt</label>
      <textarea
        v-model="form.explore_system_prompt"
        class="textarea h-40"
        placeholder="You are a fictional user named…"
        required
      ></textarea>
    </div>

    <div>
      <label class="label">Report system prompt</label>
      <textarea
        v-model="form.report_system_prompt"
        class="textarea h-32"
        placeholder="Now write your review of the experience…"
        required
      ></textarea>
    </div>

    <div>
      <label class="label">Setup actions (optional)</label>
      <textarea
        v-model="form.setup_actions"
        class="textarea h-20"
        placeholder="Optional: extra steps the harness should perform before handing control to the persona."
      ></textarea>
    </div>

    <div class="flex items-center justify-end gap-2 border-t border-ink-200 pt-3">
      <button type="button" class="btn-ghost btn" @click="$emit('cancel')">
        Cancel
      </button>
      <button type="submit" class="btn-primary btn" :disabled="disabled">
        {{ submitLabel }}
      </button>
    </div>
  </form>
</template>

<script setup>
import { computed, reactive, watch } from 'vue'
import Avatar from './Avatar.vue'

const COLOR_TOKENS = [
  'teal', 'amber', 'rose', 'indigo', 'emerald', 'violet',
  'sky', 'fuchsia', 'lime', 'orange', 'cyan', 'slate',
]

// Solid fills for the swatch picker. The `.tint-*` classes went to
// 10%-alpha washes in the dark redesign — fine for pills, invisible as
// 7px swatches — so the picker uses the full-strength colours. Literal
// class names (not template strings) so Tailwind's content scan keeps
// them in the build.
const SWATCH_BG = {
  teal: 'bg-teal-500',
  amber: 'bg-amber-500',
  rose: 'bg-rose-500',
  indigo: 'bg-indigo-500',
  emerald: 'bg-emerald-500',
  violet: 'bg-violet-500',
  sky: 'bg-sky-500',
  fuchsia: 'bg-fuchsia-500',
  lime: 'bg-lime-500',
  orange: 'bg-orange-500',
  cyan: 'bg-cyan-500',
  slate: 'bg-slate-400',
}

const props = defineProps({
  initial: { type: Object, default: null },
  editing: { type: Boolean, default: false },
  submitLabel: { type: String, default: 'Save' },
  error: { type: String, default: '' },
  disabled: { type: Boolean, default: false },
})

const emit = defineEmits(['submit', 'cancel'])

function blank() {
  return {
    persona_id: '',
    display_name: '',
    registered_email: '',
    explore_system_prompt: '',
    report_system_prompt: '',
    flows: [],
    uses_admin_login: false,
    setup_actions: '',
    // #1009 — region + language replace browser_locale. Persona form
    // sends both; backend recomputes the composite at run time.
    region: '',
    language: '',
    browser_locale: '',
    color_token: 'teal',
    avatar_seed: '',
  }
}

const form = reactive(blank())

watch(
  () => props.initial,
  (v) => {
    if (!v) return
    Object.assign(form, blank(), v, {
      flows: Array.isArray(v.flows) ? [...v.flows] : [],
    })
  },
  { immediate: true },
)

// Pre-#999 the field was rendered as a single-line input with comma-separated
// tags ("signup, billing, password-reset"). That made the form unusable in
// practice — real persona flows are 200–700-char narrative steps, several per
// persona. The data has always been list[str]; we now edit it as one
// newline-separated textarea entry per step.
const flowsStr = computed(() => (form.flows || []).join('\n'))

function onFlowsInput(ev) {
  form.flows = ev.target.value
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
}

const FLOWS_PLACEHOLDER = `Read the landing page as a curious newcomer; note anything jargony.
Sign up using your registered email and complete email verification.
Compose a test email to the agent address and confirm threading works.`

function onSubmit() {
  // Strip empty optional strings so the API doesn't store ""s where null
  // is the conventional "unset" marker.
  const payload = { ...form }
  for (const k of ['region', 'language', 'browser_locale', 'setup_actions', 'avatar_seed']) {
    if (payload[k] === '') payload[k] = null
  }
  emit('submit', payload)
}
</script>
