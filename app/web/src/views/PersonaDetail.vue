<template>
  <div class="mx-auto max-w-6xl p-6">
    <router-link to="/personas" class="mb-3 inline-block text-xs text-ink-600 hover:text-ink-700">
      ← All personas
    </router-link>

    <div v-if="loading" class="text-sm text-ink-600">Loading…</div>
    <div v-else-if="error" class="text-sm text-red-400">{{ error }}</div>

    <div v-else-if="persona" class="space-y-5">
      <!-- Hero — dossier header: avatar, identity, provenance pills, and
           the mono persona_id as the file reference. -->
      <div class="panel flex items-start gap-5 p-6">
        <Avatar
          :seed="persona.avatar_seed || persona.persona_id"
          :color-token="persona.color_token || 'slate'"
          size="xl"
        />
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-3">
            <h1 class="!text-2xl">{{ persona.display_name }}</h1>
            <span v-if="persona.is_default" class="pill" :class="`tint-${persona.color_token || 'slate'}`">
              default
            </span>
            <span v-if="persona.hidden" class="pill">hidden</span>
          </div>
          <div v-if="persona.archetype" class="mt-0.5 text-sm text-ink-600">
            {{ persona.archetype }}
          </div>
          <div class="mt-2 flex flex-wrap items-center gap-1.5">
            <span v-if="persona.region" class="pill" title="Region">{{ persona.region }}</span>
            <span v-if="persona.language" class="pill" title="Language">{{ persona.language }}</span>
            <span v-if="persona.browser_locale" class="pill font-mono" title="Browser locale">
              {{ persona.browser_locale }}
            </span>
            <span class="pill font-mono" title="Registered email">{{ persona.registered_email }}</span>
          </div>
          <div class="mt-2 font-mono text-xs text-ink-500">{{ persona.persona_id }}</div>
        </div>
        <div class="flex flex-col items-end gap-2">
          <!-- #1822 follow-up — launch straight from the dossier. Deep-links
               into the New Run console with this persona pre-selected, so
               the confirm dialog / active-run guard / advanced knobs all
               still apply. -->
          <router-link
            :to="`/new-run?personas=${encodeURIComponent(persona.persona_id)}`"
            class="btn btn-primary"
            :title="`Open New Run with ${persona.display_name} pre-selected`"
            data-testid="persona-run-button"
          >
            <svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="currentColor">
              <path d="M7 4l13 8-13 8z" />
            </svg>
            Run this persona
          </router-link>
          <span class="readout">{{ runCount }} runs</span>
        </div>
      </div>

      <!--
        Tab strip. Pre-PR-E "Danger zone" sat in the normal tab row with no
        visual differentiation. Now it's pushed to the right (ml-auto) and
        tinted red so a destructive surface doesn't look like its peers.
        Implementation note: PERSONA_TABS_NORMAL + PERSONA_TAB_DANGER let
        the danger tab keep `id`/`label`/`count` semantics while opting out
        of the default underline/hover treatment.
      -->
      <div class="flex items-center gap-1 border-b border-ink-200">
        <button
          v-for="t in normalTabs"
          :key="t.id"
          class="border-b-2 px-3 py-2 text-sm font-medium transition"
          :class="
            activeTab === t.id
              ? 'border-brand-600 text-ink-900'
              : 'border-transparent text-ink-500 hover:text-ink-800'
          "
          @click="activeTab = t.id"
        >
          {{ t.label }}
          <span v-if="t.count != null" class="ml-1 text-xs text-ink-500">{{ t.count }}</span>
        </button>
        <button
          v-if="dangerTab"
          class="ml-auto border-b-2 px-3 py-2 text-sm font-medium transition"
          :class="
            activeTab === dangerTab.id
              ? 'border-red-500 text-red-300'
              : 'border-transparent text-red-400 hover:bg-red-500/10 hover:text-red-300'
          "
          @click="activeTab = dangerTab.id"
        >
          ⚠ {{ dangerTab.label }}
        </button>
      </div>

      <!-- Settings (was "Overview" pre-#1009). The id stays
           ``overview`` so existing deep links + e2e selectors keep
           working; only the user-facing label changes. The tab
           hosts a full edit form — "Settings" describes that
           honestly; "Overview" implied read-only summary. -->
      <div v-if="activeTab === 'overview'">
        <div class="panel panel-pad">
          <PersonaForm
            editing
            :initial="persona"
            :submit-label="savingBusy ? 'Saving…' : 'Save changes'"
            :error="saveError"
            :disabled="savingBusy"
            @submit="onSave"
            @cancel="cancelEdit"
          />
        </div>
        <!-- Flows — read-only pills. The dedicated Flows tab was dropped
             in #1822: it duplicated this list one click away. The
             editable source is the "Mandatory steps" textarea in the
             form above; these are free-text tags analyzers use to group
             findings. -->
        <div class="panel panel-pad mt-4">
          <h3 class="!text-base">Flows</h3>
          <p
            v-if="!persona.flows || !persona.flows.length"
            class="mt-2 text-sm text-ink-500"
          >
            No flows tagged. Add steps in the “Mandatory steps” field above —
            they're free-text tags used by analyzers to group findings.
          </p>
          <div v-else class="mt-2 flex flex-wrap gap-1.5">
            <span v-for="f in persona.flows" :key="f" class="pill">{{ f }}</span>
          </div>
        </div>
        <!-- #1105 Slice 1 — saved-login row. Visible only when the
             persona has actually been signed up (Slice 1.1 will wire
             the recorder hook that populates this; pre-Slice-1.1
             it's typically empty + the row reads "no saved login").
             The Reset button calls DELETE which is idempotent so
             operators can hammer it without harm. -->
        <div
          class="panel panel-pad mt-4"
          data-testid="persona-credentials-row"
        >
          <h3 class="!text-base">Saved login</h3>
          <p v-if="credsLoading" class="mt-2 text-sm text-ink-500">
            Checking…
          </p>
          <template v-else-if="credsStatus?.has_credentials">
            <p class="mt-2 text-sm text-ink-700">
              Returning user · signs in as
              <code class="rounded bg-ink-100 px-1.5 py-0.5 font-mono text-xs">{{ credsStatus.email }}</code>
              <span v-if="credsStatus.verified" class="ml-2 text-emerald-300">✓ verified</span>
              <span v-else class="ml-2 text-amber-300">⚠ unverified</span>
            </p>
            <p v-if="credsStatus.last_rotation_n" class="mt-1 text-xs text-ink-500">
              Rotated {{ credsStatus.last_rotation_n }} time{{ credsStatus.last_rotation_n === 1 ? '' : 's' }}.
            </p>
            <div class="mt-3 flex gap-2">
              <button
                class="btn-ghost btn"
                :disabled="credsResetBusy"
                title="Clear the saved login so the next run signs this persona up fresh. Useful if the account has been corrupted or you want to test the signup flow again."
                data-testid="persona-credentials-reset"
                @click="onResetCredentials"
              >
                {{ credsResetBusy ? 'Resetting…' : '↺ Reset login' }}
              </button>
            </div>
            <p v-if="credsError" class="mt-2 text-sm text-red-400">
              {{ credsError }}
            </p>
          </template>
          <template v-else>
            <p class="mt-2 text-sm text-ink-500">
              No saved login yet. The harness will sign this persona up
              on the next run; once signed up, the credentials are
              stored encrypted so subsequent runs log in instead.
            </p>
          </template>
        </div>
      </div>

      <!-- Prompts (read-only, large panel) -->
      <div v-else-if="activeTab === 'prompts'" class="space-y-4">
        <div class="panel panel-pad">
          <h3 class="mb-2">Explore prompt</h3>
          <pre class="whitespace-pre-wrap break-words rounded-md border border-hairline/5 bg-ink-50 p-3 font-mono text-xs text-ink-800">{{ persona.explore_system_prompt }}</pre>
        </div>
        <div class="panel panel-pad">
          <h3 class="mb-2">Report prompt</h3>
          <pre class="whitespace-pre-wrap break-words rounded-md border border-hairline/5 bg-ink-50 p-3 font-mono text-xs text-ink-800">{{ persona.report_system_prompt }}</pre>
        </div>
        <div v-if="persona.setup_actions" class="panel panel-pad">
          <h3 class="mb-2">Setup actions</h3>
          <pre class="whitespace-pre-wrap break-words rounded-md border border-hairline/5 bg-ink-50 p-3 font-mono text-xs text-ink-800">{{ persona.setup_actions }}</pre>
        </div>
      </div>

      <!-- #1822 §6 — the Flows tab is gone. It was a one-click-away
           duplicate of data already editable on Settings; the read-only
           pills now live inline on the Settings tab. -->

      <!-- #1009 item 3 — Memory tab removed. It was a tab whose only
           content was a single redirect link to /memory; the tab-for-
           a-link mismatch confused the IA. The link moved to the
           hero (above) as a discoverable quick-link. -->

      <!-- Runs -->
      <div v-else-if="activeTab === 'runs'" class="space-y-2">
        <div v-if="!recentRuns.length" class="panel panel-pad text-sm text-ink-600">
          No runs yet for this persona.
          <router-link :to="`/new-run?personas=${encodeURIComponent(persona.persona_id)}`">
            Start one →
          </router-link>
        </div>
        <router-link
          v-for="r in recentRuns"
          :key="r.run_id"
          :to="`/runs/${r.run_id}`"
          class="panel flex items-center justify-between p-4 transition hover:bg-brand-50/40 hover:no-underline"
        >
          <div>
            <div class="font-mono text-sm text-ink-900">{{ r.run_id }}</div>
            <div class="text-xs text-ink-600">
              {{ formatTimestamp(r.started_at) }}
              <span v-if="r.status" class="ml-2">· {{ r.status }}</span>
            </div>
          </div>
          <FindingCounts :counts="r.finding_counts" />
        </router-link>
      </div>

      <!-- Danger zone -->
      <div v-else-if="activeTab === 'danger'" class="panel !border-red-500/30 panel-pad">
        <h3 class="!text-red-300">Danger zone</h3>
        <p class="mt-2 text-sm text-ink-600">
          <template v-if="persona.is_default">
            This is a default persona — it can be <strong>hidden</strong> from the dashboard
            but not deleted, so the harness can always fall back to it.
          </template>
          <template v-else>
            User-created personas can be permanently deleted. Runs, findings, and
            memory rows that reference this persona are unaffected.
          </template>
        </p>
        <div class="mt-3 flex gap-2">
          <button
            v-if="persona.is_default"
            class="btn-danger btn"
            :disabled="dangerBusy"
            @click="toggleHidden"
          >
            {{ persona.hidden ? 'Restore' : 'Hide persona' }}
          </button>
          <button
            v-else
            class="btn-danger btn"
            :disabled="dangerBusy"
            @click="onDelete"
          >
            Delete permanently
          </button>
        </div>
        <p v-if="dangerError" class="mt-2 text-sm text-red-400">{{ dangerError }}</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  clearPersonaCredentials,
  deletePersona,
  getPersonaCredentialsStatus,
  getPersonaDetail,
  listRuns,
  updatePersona,
} from '../api.js'
import { formatTimestamp } from '../format.js'
import Avatar from '../components/Avatar.vue'
import FindingCounts from '../components/FindingCounts.vue'
import PersonaForm from '../components/PersonaForm.vue'

const props = defineProps({ personaId: { type: String, required: true } })
const router = useRouter()

const persona = ref(null)
const loading = ref(true)
const error = ref('')

const recentRuns = ref([])

const activeTab = ref('overview')
// #1822 §6 — no Flows tab. The flow tags render as read-only pills on
// the Settings tab, next to the form field that edits them.
const TABS = computed(() => [
  { id: 'overview', label: 'Settings' },
  { id: 'prompts', label: 'Prompts' },
  { id: 'runs', label: 'Runs', count: recentRuns.value.length },
  { id: 'danger', label: 'Danger zone' },
])
// Split for the template — danger zone gets a different visual treatment
// (right-aligned, red-tinted) so it doesn't look like its peers.
const normalTabs = computed(() => TABS.value.filter((t) => t.id !== 'danger'))
const dangerTab = computed(() => TABS.value.find((t) => t.id === 'danger') || null)

const runCount = computed(() => recentRuns.value.length)

const savingBusy = ref(false)
const saveError = ref('')

const dangerBusy = ref(false)
const dangerError = ref('')

// #1105 — credentials status (operator-readable; never carries
// password). credsResetBusy gates the reset button so a double-click
// can't fire two DELETEs in flight.
const credsLoading = ref(true)
const credsStatus = ref(null)
const credsError = ref('')
const credsResetBusy = ref(false)

async function loadCredentials() {
  credsLoading.value = true
  credsError.value = ''
  try {
    credsStatus.value = await getPersonaCredentialsStatus(props.personaId)
  } catch (e) {
    // A 404 here means the persona itself was deleted between the
    // main refresh and this call — leave credsStatus null and let
    // the parent error path handle the persona-missing UX.
    credsStatus.value = null
    if (e?.response?.status !== 404) {
      credsError.value = e?.response?.data?.detail || e.message || 'Failed to load login status'
    }
  } finally {
    credsLoading.value = false
  }
}

async function onResetCredentials() {
  if (credsResetBusy.value) return
  if (
    !window.confirm(
      `Reset the saved login for ${props.personaId}?\n\n` +
        'The next run will sign this persona up fresh under a new email ' +
        'and start without any prior session state. Use this when the ' +
        'account got into a bad state, or you want to A/B-test the ' +
        'first-time signup flow.',
    )
  ) {
    return
  }
  credsResetBusy.value = true
  credsError.value = ''
  try {
    await clearPersonaCredentials(props.personaId)
    await loadCredentials()
  } catch (e) {
    credsError.value = e?.response?.data?.detail || e.message || 'Reset failed'
  } finally {
    credsResetBusy.value = false
  }
}

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    persona.value = await getPersonaDetail(props.personaId)
    // Fire credential load in parallel — independent endpoint, the UI
    // gracefully renders a "Checking…" placeholder while it lands.
    loadCredentials()
  } catch (e) {
    error.value =
      e?.response?.status === 404
        ? `Persona "${props.personaId}" not found.`
        : e?.response?.data?.detail || e.message || 'Failed to load persona'
  } finally {
    loading.value = false
  }
}

async function loadRuns() {
  try {
    const all = await listRuns()
    recentRuns.value = (all || []).filter((r) =>
      (r.personas || []).includes(props.personaId),
    )
  } catch {
    recentRuns.value = []
  }
}

watch(() => props.personaId, async () => {
  await refresh()
  await loadRuns()
}, { immediate: false })

onMounted(async () => {
  await refresh()
  await loadRuns()
})

async function onSave(payload) {
  savingBusy.value = true
  saveError.value = ''
  try {
    // The form sends every field; build a minimal patch so the server's
    // exclude_unset semantics aren't relevant — we only forward changes.
    const patch = {}
    const fields = [
      'display_name', 'registered_email', 'explore_system_prompt',
      'report_system_prompt', 'flows', 'uses_admin_login',
      'setup_actions', 'browser_locale', 'color_token', 'avatar_seed',
    ]
    for (const k of fields) {
      const cur = persona.value[k]
      const next = payload[k]
      if (JSON.stringify(cur) !== JSON.stringify(next)) {
        patch[k] = next
      }
    }
    if (Object.keys(patch).length === 0) {
      saveError.value = 'No changes to save.'
      return
    }
    persona.value = await updatePersona(props.personaId, patch)
  } catch (e) {
    saveError.value = e?.response?.data?.detail || e.message || 'Save failed'
  } finally {
    savingBusy.value = false
  }
}

function cancelEdit() {
  // Form is uncontrolled by parent state — easiest reset is a re-fetch.
  refresh()
}

async function toggleHidden() {
  dangerBusy.value = true
  dangerError.value = ''
  try {
    persona.value = await updatePersona(props.personaId, { hidden: !persona.value.hidden })
  } catch (e) {
    dangerError.value = e?.response?.data?.detail || e.message || 'Failed'
  } finally {
    dangerBusy.value = false
  }
}

async function onDelete() {
  if (!window.confirm(`Permanently delete persona "${props.personaId}"?`)) return
  dangerBusy.value = true
  dangerError.value = ''
  try {
    await deletePersona(props.personaId)
    router.push('/personas')
  } catch (e) {
    dangerError.value = e?.response?.data?.detail || e.message || 'Delete failed'
  } finally {
    dangerBusy.value = false
  }
}
</script>
