<template>
  <div class="mx-auto max-w-7xl p-6">
    <!--
      #1822 — the home page does ONE job now: run history + watch. The
      trigger console moved to its own /new-run route; the only trace of
      it here is the launch button.
    -->
    <header class="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1>Runs</h1>
        <p class="mt-1 text-sm text-ink-600">
          One row per harness invocation. Click into a run to triage
          findings, read persona reviews, or watch the live timeline.
        </p>
      </div>
      <router-link to="/new-run" class="cta-start" data-testid="new-run-button">
        <svg viewBox="0 0 24 24" class="h-4 w-4" fill="currentColor">
          <path d="M7 4l13 8-13 8z" />
        </svg>
        New Run
      </router-link>
    </header>

    <div v-if="loading" class="text-sm text-ink-600">Loading runs…</div>
    <div v-else-if="error" class="text-sm text-red-400">{{ error }}</div>
    <div v-else-if="runs.length === 0" class="panel panel-pad text-sm text-ink-600">
      No QA runs recorded yet.
      <router-link to="/new-run">Start your first run →</router-link>
    </div>

    <div v-else class="panel overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="bg-ink-50 text-xs uppercase tracking-wide text-ink-500">
          <tr>
            <th class="px-4 py-2 text-left font-medium">Run</th>
            <th
              class="px-4 py-2 text-left font-medium"
              title="Lifecycle state: starting → running → finished (logs distilled) → reviewed (per-persona review written). Failed and cancelled are terminal."
            >
              Status
            </th>
            <th class="px-4 py-2 text-left font-medium">Started</th>
            <th class="px-4 py-2 text-left font-medium">Personas</th>
            <th class="px-4 py-2 text-left font-medium">Findings</th>
            <th
              class="px-4 py-2 text-right font-medium"
              title="Output tokens across all turns — the 'how heavy was this run' signal. Runs bill the operator's Claude Code Max subscription, so there is no per-run dollar figure."
            >
              Tokens
            </th>
          </tr>
        </thead>
        <tbody class="divide-y divide-ink-100">
          <tr
            v-for="run in visibleRuns"
            :key="run.run_id"
            class="group cursor-pointer transition hover:bg-brand-50/40"
            :class="isLive(run) ? 'bg-emerald-500/[0.04]' : ''"
            @click="onRowClick($event, run.run_id)"
          >
            <td class="px-4 py-3 font-mono text-xs text-ink-900">
              <div class="flex items-center gap-2">
                <span class="lamp" :class="lampClass(run)"></span>
                <!-- Anchored so middle-click / cmd-click open-in-new-tab
                     works; the row-wide handler bails on modifiers. -->
                <router-link
                  :to="`/runs/${run.run_id}`"
                  class="text-ink-900 hover:no-underline"
                  :title="run.run_notes || ''"
                >
                  {{ run.run_id }}
                </router-link>
              </div>
              <div
                v-if="run.run_notes"
                class="mt-0.5 max-w-[28ch] truncate pl-4 font-sans text-[11px] text-ink-500"
                :title="run.run_notes"
              >
                {{ run.run_notes }}
              </div>
            </td>
            <td class="px-4 py-3">
              <span v-if="isLive(run)" class="pill pill-status-running">
                <span class="lamp lamp-live"></span>
                live
              </span>
              <span v-else class="pill" :class="`pill-status-${run.status}`">{{ run.status }}</span>
            </td>
            <td
              class="px-4 py-3 text-xs text-ink-600"
              :title="formatDate(run.started_at)"
            >
              {{ relativeTime(run.started_at) }}
            </td>
            <td class="px-4 py-3" @click.stop>
              <div class="flex flex-wrap items-center gap-1">
                <router-link
                  v-for="p in run.personas || []"
                  :key="p"
                  :to="`/personas/${p}`"
                  class="flex items-center gap-1 rounded-full bg-ink-100 px-2 py-0.5 text-xs text-ink-700 ring-1 ring-inset ring-hairline/5 hover:bg-brand-50 hover:no-underline"
                >
                  <Avatar
                    :seed="personaMeta[p]?.avatar_seed || p"
                    :color-token="personaMeta[p]?.color_token || 'slate'"
                    size="xs"
                  />
                  {{ personaMeta[p]?.display_name || p }}
                </router-link>
              </div>
            </td>
            <td class="px-4 py-3">
              <FindingCounts :counts="run.finding_counts || {}" />
            </td>
            <td class="px-4 py-3 text-right">
              <span class="readout" :title="tokenTitle(run)">
                {{ formatTokens(run.totals?.output_tokens) }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>

      <!--
        Harness-recovery probes and other empty invocations (no findings,
        no output tokens) used to bury real results. They fold into one
        quiet disclosure row instead (#1822 §6).
      -->
      <button
        v-if="emptyRuns.length > 0"
        class="flex w-full items-center gap-2 border-t border-hairline/[0.06] bg-ink-50 px-4 py-2 text-left text-xs text-ink-500 transition hover:text-ink-700"
        data-testid="empty-runs-toggle"
        @click="showEmpty = !showEmpty"
      >
        <span class="inline-block w-3 text-center">{{ showEmpty ? '▾' : '▸' }}</span>
        {{ showEmpty ? 'Hide' : 'Show' }} {{ emptyRuns.length }} empty
        run{{ emptyRuns.length === 1 ? '' : 's' }} (no findings, no output —
        probes &amp; aborted starts)
      </button>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import Avatar from '../components/Avatar.vue'
import FindingCounts from '../components/FindingCounts.vue'
import { listPersonas, listRuns } from '../api.js'
import { formatDate, formatTokens, relativeTime } from '../format.js'

const router = useRouter()
const runs = ref([])
const personaMeta = ref({})
const loading = ref(true)
const error = ref('')
const showEmpty = ref(false)

const LIVE_STATUSES = ['starting', 'running', 'new']
function isLive(run) {
  return LIVE_STATUSES.includes(run.status) && !run.finished_at
}
function lampClass(run) {
  if (isLive(run)) return 'lamp-live'
  if (run.status === 'failed' || run.status === 'cancelled') return 'lamp-fail'
  if ((run.finding_counts?.blocker || 0) > 0) return 'lamp-fail'
  return 'lamp-ok'
}

// An "empty" run produced nothing: no findings of any kind and no output
// tokens. Live runs are never folded — a fresh run starts empty.
function isEmptyRun(run) {
  if (isLive(run)) return false
  const counts = run.finding_counts || {}
  const anyFinding = Object.values(counts).some((n) => (n || 0) > 0)
  return !anyFinding && !(run.totals?.output_tokens > 0)
}

const emptyRuns = computed(() => runs.value.filter(isEmptyRun))
const visibleRuns = computed(() =>
  showEmpty.value ? runs.value : runs.value.filter((r) => !isEmptyRun(r)),
)

function tokenTitle(run) {
  const t = run.totals || {}
  return [
    `${(t.input_tokens || 0).toLocaleString()} in`,
    `${(t.output_tokens || 0).toLocaleString()} out`,
    `${(t.cache_tokens || 0).toLocaleString()} cache`,
  ].join(' · ')
}

function onRowClick(ev, runId) {
  // Let links/buttons inside the row handle themselves; the row-wide
  // handler is a convenience, not a hijack. Modifier clicks fall through
  // to the anchor for open-in-new-tab.
  if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return
  const target = ev.target
  if (target && target.closest('a, button, input, select, [role="button"]')) {
    return
  }
  router.push({ name: 'run', params: { runId } })
}

let pollTimer = null
async function load() {
  const [r, personas] = await Promise.all([
    listRuns(),
    listPersonas({ includeHidden: true }).catch(() => []),
  ])
  runs.value = r
  personaMeta.value = Object.fromEntries(
    (personas || []).map((p) => [p.persona_id, p]),
  )
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    error.value = `Could not load runs: ${e.message}`
  } finally {
    loading.value = false
  }
  // Keep the table fresh while a run is in flight — cheap full refetch
  // every 10s only when a live row is visible.
  pollTimer = setInterval(() => {
    if (runs.value.some(isLive)) load().catch(() => {})
  }, 10000)
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>
