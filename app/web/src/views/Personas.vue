<template>
  <div class="mx-auto max-w-7xl p-6">
    <!--
      The header copy is deliberately warm — Test Ease is a developer tool
      but it shouldn't feel like one. "Pick your testers" is friendlier
      than "Manage Personas" and tells the operator what to actually do.
    -->
    <header class="mb-6 flex items-end justify-between gap-4">
      <div>
        <h1>Pick your testers</h1>
        <p class="mt-1 max-w-2xl text-sm text-ink-600">
          {{ activeCount }} of {{ totalCount }} personas activated. Tick
          the ones that match how your real users behave — activated
          testers are pre-selected every time you open
          <router-link to="/new-run">New Run</router-link>.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <!-- #1089 — bulk activate / deactivate. The previous default
             ("23 personas inactive after wipe") meant operators
             clicked 23 individual toggles to get all 25 running. The
             button label reflects what's actionable: "Activate all"
             when anyone's off, "Deactivate all" when everyone's on. -->
        <button
          v-if="inactiveVisibleCount > 0"
          class="btn-ghost btn"
          :disabled="bulkBusy"
          :title="`Turn on all ${inactiveVisibleCount} inactive personas in one click. Each PATCH still fires individually so the optimistic UI matches single-row toggling.`"
          data-testid="personas-activate-all"
          @click="bulkActivateAll"
        >
          {{ bulkBusy ? 'Activating…' : `✓ Activate all (${inactiveVisibleCount})` }}
        </button>
        <button
          v-else-if="activeCount > 0"
          class="btn-ghost btn"
          :disabled="bulkBusy"
          :title="`Turn off all ${activeCount} active personas. Subsequent runs will fall through to whatever you re-activate.`"
          data-testid="personas-deactivate-all"
          @click="bulkDeactivateAll"
        >
          {{ bulkBusy ? 'Deactivating…' : `✗ Deactivate all (${activeCount})` }}
        </button>
        <button class="btn-ghost btn" @click="creating = true">
          + New persona
        </button>
      </div>
    </header>

    <!-- Active / All / Hidden filter — primary axis for the operator -->
    <div class="mb-4 flex items-center gap-2 border-b border-ink-200">
      <button
        v-for="tab in TABS"
        :key="tab.id"
        class="border-b-2 px-3 py-2 text-sm font-medium transition"
        :class="
          activeTab === tab.id
            ? 'border-brand-600 text-ink-900'
            : 'border-transparent text-ink-500 hover:text-ink-800'
        "
        @click="activeTab = tab.id"
      >
        {{ tab.label }}
        <span class="ml-1 text-xs text-ink-400">{{ tab.count }}</span>
      </button>
      <div class="ml-auto text-xs text-ink-500">
        {{ filteredPersonas.length }}
        {{ filteredPersonas.length === 1 ? 'persona' : 'personas' }}
      </div>
    </div>

    <div v-if="loading" class="text-sm text-ink-600">Waking up the catalog…</div>
    <div v-else-if="error" class="text-sm text-red-400">{{ error }}</div>

    <!-- Empty state when filter hides everything — charm via copy -->
    <div
      v-else-if="!filteredPersonas.length"
      class="panel panel-pad mt-4 text-center text-sm text-ink-600"
    >
      <div class="text-4xl">🫶</div>
      <p class="mt-2">
        <template v-if="activeTab === 'active'">
          Nobody's activated yet. Switch to the
          <button class="text-brand-700 underline" @click="activeTab = 'all'">
            All
          </button>
          tab and tick a few personas to bring them on board.
        </template>
        <template v-else>
          Nothing here. Try a different filter, or add a new persona.
        </template>
      </p>
    </div>

    <div
      v-else
      class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4"
    >
      <div
        v-for="p in filteredPersonas"
        :key="p.persona_id"
        class="panel persona-card group relative flex flex-col gap-3 overflow-hidden p-5 pl-6 transition"
        :class="[
          { 'opacity-60': p.hidden },
          { 'ring-2 ring-brand-500 ring-offset-2 ring-offset-void': p.is_active },
          `persona-card-color-${p.color_token || 'slate'}`,
        ]"
      >
        <!-- Activation toggle — primary action, top-right -->
        <button
          class="absolute right-3 top-3 z-10 flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium transition"
          :class="
            p.is_active
              ? 'bg-brand-600 text-ink-50 hover:bg-brand-700'
              : 'bg-ink-100 text-ink-600 ring-1 ring-inset ring-hairline/5 hover:bg-ink-200'
          "
          :disabled="togglingPersonaId === p.persona_id"
          @click.stop="onToggleActive(p)"
          :title="p.is_active ? 'Click to deactivate' : 'Click to activate this persona'"
        >
          <span
            class="h-1.5 w-1.5 rounded-full transition"
            :class="p.is_active ? 'bg-ink-50' : 'bg-ink-400'"
          ></span>
          {{ p.is_active ? 'On' : 'Off' }}
        </button>
        <!-- #1105 Slice 1.1 — returning-user marker. Tiny ↻ badge on
             cards where the persona has saved credentials (set by the
             harness recorder after a successful signup). Helps the
             operator scan "which personas have lifecycle state I
             should respect" before triggering a run. Reads
             p.credentials?.email (truthy when creds exist); the
             password never leaves the server. -->
        <span
          v-if="p.credentials && p.credentials.email"
          class="absolute right-3 top-10 z-10 inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[10px] font-medium text-emerald-300 ring-1 ring-emerald-500/30"
          :title="`Returning user — signs in as ${p.credentials.email}. Reset on the persona detail page if you want a fresh signup next run.`"
          data-testid="persona-returning-user-badge"
        >
          ↻ Returning
        </span>

        <!-- Card body — clicks into the detail editor -->
        <router-link
          :to="`/personas/${p.persona_id}`"
          class="-m-2 flex flex-col gap-3 rounded-lg p-2 transition hover:bg-brand-50/40 hover:no-underline"
        >
          <div class="flex items-center gap-3 pr-14">
            <Avatar
              :seed="p.avatar_seed || p.persona_id"
              :color-token="p.color_token || 'slate'"
              size="lg"
            />
            <div class="min-w-0 flex-1">
              <div class="truncate text-sm font-semibold text-ink-900">
                {{ p.display_name }}
              </div>
              <div class="truncate text-xs text-ink-500">
                {{ p.archetype || p.persona_id }}
              </div>
            </div>
          </div>

          <div class="text-xs text-ink-600">
            <div v-if="p.region || p.language" class="truncate">
              <span class="text-ink-500">from&nbsp;</span>
              {{ regionLabel(p) }}
            </div>
            <div v-if="!p.is_default" class="text-ink-500">
              custom persona
            </div>
          </div>

          <div v-if="p.flows && p.flows.length" class="flex flex-wrap gap-1">
            <span
              v-for="flow in p.flows.slice(0, 3)"
              :key="flow"
              class="pill text-[10px]"
            >
              {{ flow }}
            </span>
            <span v-if="p.flows.length > 3" class="pill text-[10px]">
              +{{ p.flows.length - 3 }}
            </span>
          </div>
        </router-link>
      </div>
    </div>

    <!-- Bulk-activate hint — friendly nudge if nobody's on board yet -->
    <div
      v-if="!loading && activeCount === 0 && totalCount > 0"
      class="panel panel-pad mt-6 border-brand-200 bg-brand-50 text-sm"
    >
      <p class="text-ink-700">
        <strong>Just getting started?</strong> Try activating
        <button class="text-brand-700 underline" @click="bulkActivate(STARTER_SET)">
          a starter set of 5 personas
        </button>
        — covers signup, billing, mobile, accessibility, and a
        first-impression critic. You can always change your mind.
      </p>
    </div>

    <!-- Create modal -->
    <Teleport to="body">
      <div
        v-if="creating"
        class="fixed inset-0 z-30 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
        @click.self="creating = false"
      >
        <div class="panel w-full max-w-xl panel-pad">
          <h2 class="mb-4">New persona</h2>
          <PersonaForm
            :submit-label="creatingBusy ? 'Creating…' : 'Create persona'"
            :error="createError"
            :disabled="creatingBusy"
            @submit="onCreate"
            @cancel="creating = false"
          />
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { createPersona, listPersonas, updatePersona } from '../api.js'
import Avatar from '../components/Avatar.vue'
import PersonaForm from '../components/PersonaForm.vue'

// Suggested starter set — covers the main flow archetypes. Used by the
// "activate a starter set" CTA on a freshly-seeded tenant.
const STARTER_SET = [
  'happy-path-signup',
  'first-impression-critic',
  'mobile-signup-visitor',
  'declined-payer',
  'keyboard-only',
]

const allPersonas = ref([])
const loading = ref(true)
const error = ref('')

const activeTab = ref('all')
const togglingPersonaId = ref(null)

const TABS = computed(() => [
  { id: 'active', label: 'Activated', count: activeCount.value },
  { id: 'all', label: 'All', count: totalCount.value },
  { id: 'hidden', label: 'Hidden', count: hiddenCount.value },
])

const totalCount = computed(() => allPersonas.value.filter((p) => !p.hidden).length)
const activeCount = computed(() => allPersonas.value.filter((p) => p.is_active && !p.hidden).length)
const hiddenCount = computed(() => allPersonas.value.filter((p) => p.hidden).length)
// #1089 — drives the bulk-activate button label/visibility.
const inactiveVisibleCount = computed(
  () => allPersonas.value.filter((p) => !p.is_active && !p.hidden).length,
)
const bulkBusy = ref(false)

const filteredPersonas = computed(() => {
  if (activeTab.value === 'active') {
    return allPersonas.value.filter((p) => p.is_active && !p.hidden)
  }
  if (activeTab.value === 'hidden') {
    return allPersonas.value.filter((p) => p.hidden)
  }
  return allPersonas.value.filter((p) => !p.hidden)
})

function regionLabel(p) {
  if (p.region && p.language) return `${p.region} · ${p.language}`
  return p.region || p.language || ''
}

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    allPersonas.value = await listPersonas({ includeHidden: true })
  } catch (e) {
    error.value = e?.response?.data?.detail || e.message || 'Failed to load personas'
  } finally {
    loading.value = false
  }
}

async function onToggleActive(persona) {
  // Optimistic UI — flip the local copy first, then patch the server.
  // If the patch fails, revert.
  togglingPersonaId.value = persona.persona_id
  const previous = persona.is_active
  persona.is_active = !previous
  try {
    await updatePersona(persona.persona_id, { is_active: !previous })
  } catch (e) {
    persona.is_active = previous
    error.value = e?.response?.data?.detail || e.message || 'Failed to update persona'
  } finally {
    togglingPersonaId.value = null
  }
}

async function bulkActivate(personaIds) {
  for (const id of personaIds) {
    const persona = allPersonas.value.find((p) => p.persona_id === id)
    if (persona && !persona.is_active) {
      await onToggleActive(persona)
    }
  }
}

// #1089 — bulk-activate/deactivate everything visible. Skips hidden
// personas so the "hidden" tab's state is preserved; the operator
// expects hidden = "not part of my normal triage" and shouldn't be
// surprise-activated. Sequential PATCHes (not concurrent) keep the
// optimistic-revert path simple and avoid hammering the API.
async function bulkActivateAll() {
  if (bulkBusy.value) return
  bulkBusy.value = true
  try {
    for (const p of allPersonas.value) {
      if (!p.hidden && !p.is_active) {
        await onToggleActive(p)
      }
    }
  } finally {
    bulkBusy.value = false
  }
}

async function bulkDeactivateAll() {
  if (bulkBusy.value) return
  if (
    !window.confirm(
      `Deactivate all ${activeCount.value} active personas?\n\n` +
        'Subsequent default runs (the "Run N personas" button without an ' +
        'explicit selection) will refuse to start until you re-activate at ' +
        'least one.',
    )
  ) {
    return
  }
  bulkBusy.value = true
  try {
    for (const p of allPersonas.value) {
      if (!p.hidden && p.is_active) {
        await onToggleActive(p)
      }
    }
  } finally {
    bulkBusy.value = false
  }
}

const creating = ref(false)
const creatingBusy = ref(false)
const createError = ref('')

async function onCreate(payload) {
  creatingBusy.value = true
  createError.value = ''
  try {
    await createPersona(payload)
    creating.value = false
    await refresh()
  } catch (e) {
    createError.value = e?.response?.data?.detail || e.message || 'Failed to create persona'
  } finally {
    creatingBusy.value = false
  }
}

onMounted(refresh)
</script>

<style scoped>
/* Persona card colour stripe on the left edge — same pattern as PR-E. */
.persona-card::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 4px;
  background: currentColor;
}
.persona-card-color-teal::before     { color: #14b8a6; }
.persona-card-color-amber::before    { color: #f59e0b; }
.persona-card-color-rose::before     { color: #f43f5e; }
.persona-card-color-indigo::before   { color: #6366f1; }
.persona-card-color-emerald::before  { color: #10b981; }
.persona-card-color-violet::before   { color: #8b5cf6; }
.persona-card-color-sky::before      { color: #0ea5e9; }
.persona-card-color-fuchsia::before  { color: #d946ef; }
.persona-card-color-lime::before     { color: #84cc16; }
.persona-card-color-orange::before   { color: #f97316; }
.persona-card-color-cyan::before     { color: #06b6d4; }
.persona-card-color-slate::before    { color: #94a3b8; }
</style>
