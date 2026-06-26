<template>
  <!--
    #1146 — Admin / nuclear-button page.

    Two sections:
      1. Wipe & re-seed — the destructive button + typed confirmation.
      2. Recent wipes — audit history surviving wipes themselves.
  -->
  <div class="mx-auto max-w-6xl p-6">
    <header class="mb-6">
      <h1>Admin</h1>
      <p class="mt-1 text-sm text-ink-500">
        Destructive resets. The wipe drops every per-run + per-persona
        collection and re-seeds the persona catalog; the audit list below
        records who reset the store and why.
      </p>
    </header>

    <!-- ─── Section 1 — Wipe ─────────────────────────────────────── -->
    <section class="panel mb-6 border-2 border-rose-500/30 p-4">
      <h2 class="text-lg font-semibold text-rose-300">
        🔥 Wipe &amp; re-seed
      </h2>
      <p class="mt-1 text-sm text-rose-300">
        Drops every run, finding, run step + log, screenshot, discovered
        action / tool / branch, action variant, scenario preset, and the
        persona catalog. Re-seeds the catalog from fixtures. NOT REVERSIBLE
        except via an Atlas snapshot.
      </p>
      <button
        class="btn btn-danger mt-3 font-semibold"
        data-testid="open-wipe-modal"
        @click="modalOpen = true"
      >
        Open wipe dialog
      </button>
    </section>

    <!-- ─── Section 2 — Recent wipes ────────────────────────────── -->
    <section class="panel mb-6 p-4">
      <h2 class="text-base font-semibold">Recent wipes</h2>
      <p v-if="loadingWipes" class="mt-2 text-sm text-ink-500">Loading…</p>
      <p v-else-if="!wipes.length" class="mt-2 text-sm text-ink-500">
        No wipes recorded yet. The audit collection survives wipes deliberately,
        so this list outlives each reset.
      </p>
      <ul v-else class="mt-3 space-y-2 text-sm" data-testid="wipe-list">
        <li
          v-for="w in wipes"
          :key="w.wipe_id"
          class="rounded border border-ink-100 p-2"
        >
          <div class="flex items-baseline justify-between">
            <span class="font-mono text-xs text-ink-700">{{ w.wipe_id }}</span>
            <span class="text-xs text-ink-500">{{ formatDate(w.wiped_at) }}</span>
          </div>
          <p v-if="w.requester_note" class="mt-1 italic text-ink-800">
            "{{ w.requester_note }}"
          </p>
          <p class="mt-1 text-xs text-ink-500">
            Dropped {{ w.dropped_total }} rows across
            {{ Object.keys(w.dropped_counts || {}).length }} collections
          </p>
        </li>
      </ul>
    </section>

    <!-- ─── Wipe modal ──────────────────────────────────────────── -->
    <div
      v-if="modalOpen"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      data-testid="wipe-modal"
    >
      <div class="panel w-full max-w-lg p-5 shadow-xl">
        <h3 class="text-lg font-semibold text-rose-300">
          🔥 Confirm wipe
        </h3>
        <p class="mt-2 text-sm text-ink-700">
          This will drop every run, finding, run step + log, screenshot,
          discovered action / tool / branch, action variant, and scenario
          preset, then re-seed the persona catalog. The audit row this
          creates survives the wipe.
        </p>
        <label class="mt-3 block text-sm font-medium">
          Reason (optional — shows up in the audit list)
          <input
            v-model="requesterNote"
            class="input mt-1 w-full text-sm"
            placeholder="Validating Slice 3 of #1104"
            data-testid="requester-note"
          />
        </label>
        <label class="mt-3 block text-sm font-medium">
          Type <span class="rounded bg-rose-500/15 px-1 font-mono text-rose-300">WIPE</span>
          to confirm
          <input
            v-model="confirmText"
            class="input mt-1 w-full font-mono"
            data-testid="confirm-text"
            @keyup.enter="canConfirm && onWipe()"
          />
        </label>
        <!--
          #1108 — opt-in Mailpit content wipe. Off by default to keep
          cross-run inbox continuity (personas that re-read last week's
          verification email). The PVC itself is NEVER deleted; this
          toggle only controls whether the API also fires DELETE
          /api/v1/messages against Mailpit's admin API.
        -->
        <label class="mt-3 flex items-start gap-2 text-sm">
          <input
            v-model="wipeMailpit"
            type="checkbox"
            class="mt-0.5"
            data-testid="wipe-mailpit"
          />
          <span>
            <span class="font-medium">Also wipe Mailpit messages</span>
            <span class="block text-xs text-ink-500">
              Off by default — inbox history is kept across runs. Tick
              this only for a true full-reset. The Mailpit PVC itself is
              never deleted.
            </span>
          </span>
        </label>
        <p
          v-if="wipeError"
          class="mt-2 text-sm text-rose-400"
          data-testid="wipe-error"
        >
          {{ wipeError }}
        </p>
        <p
          v-if="wipeResult"
          class="mt-2 text-sm text-emerald-300"
          data-testid="wipe-result"
        >
          ✓ Wiped {{ wipeResult.audit.dropped_total }} rows across
          {{ Object.keys(wipeResult.dropped).length }} collections.
          <span v-if="wipeResult.audit.mailpit_wiped" data-testid="mailpit-wiped">
            Mailpit messages cleared.
          </span>
        </p>
        <!--
          #1108 — surface a Mailpit failure separately from the wipe
          success. The Mongo wipe already landed (irreversible); the
          operator may want to retry the Mailpit clear via the
          per-run init container.
        -->
        <p
          v-if="wipeResult && wipeResult.audit.mailpit_error"
          class="mt-2 text-sm text-amber-300"
          data-testid="mailpit-error"
        >
          ⚠ Mongo wipe succeeded but Mailpit clear failed:
          {{ wipeResult.audit.mailpit_error }}
        </p>
        <div class="mt-4 flex justify-end gap-2">
          <button
            class="btn"
            @click="closeModal"
          >
            {{ wipeResult ? 'Close' : 'Cancel' }}
          </button>
          <button
            v-if="!wipeResult"
            :disabled="!canConfirm || wiping"
            class="btn btn-danger font-semibold"
            data-testid="confirm-wipe"
            @click="onWipe"
          >
            {{ wiping ? 'Wiping…' : 'Drop everything' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'

import {
  adminWipe,
  listAdminWipes,
} from '../api.js'
import { formatDate } from '../format.js'

const wipes = ref([])
const loadingWipes = ref(true)

const modalOpen = ref(false)
const confirmText = ref('')
const requesterNote = ref('')
// #1108 — opt-in Mailpit content wipe. Defaults to false on every
// modal open (reset alongside the other modal fields in closeModal).
const wipeMailpit = ref(false)
const wiping = ref(false)
const wipeError = ref('')
const wipeResult = ref(null)

// The confirm token is the literal string "WIPE". The button is
// disabled until the operator types it exactly — a clicked button
// without a typed confirmation can't trigger the destructive call.
const canConfirm = computed(() => confirmText.value === 'WIPE')

async function loadWipes() {
  loadingWipes.value = true
  try {
    wipes.value = await listAdminWipes({ limit: 20 })
  } catch (e) {
    wipes.value = []
    console.warn('admin: failed to load wipes', e)  // eslint-disable-line no-console
  } finally {
    loadingWipes.value = false
  }
}

async function onWipe() {
  wipeError.value = ''
  wiping.value = true
  try {
    wipeResult.value = await adminWipe({
      confirm: confirmText.value,
      requesterNote: requesterNote.value,
      wipeMailpit: wipeMailpit.value,
    })
    // Refresh the audit list so the new wipe shows up immediately.
    await loadWipes()
  } catch (e) {
    wipeError.value =
      e?.response?.data?.detail || e?.message || 'Wipe failed.'
  } finally {
    wiping.value = false
  }
}

function closeModal() {
  modalOpen.value = false
  confirmText.value = ''
  requesterNote.value = ''
  wipeMailpit.value = false
  wipeError.value = ''
  wipeResult.value = null
}

onMounted(() => {
  loadWipes()
})
</script>
