<template>
  <div class="mx-auto max-w-7xl p-6">
    <header class="mb-5">
      <h1>New Run</h1>
      <p class="mt-1 text-sm text-ink-600">
        Point the harness at a site, pick who runs, launch. Everything
        else has a sensible default under Advanced.
      </p>
    </header>

    <p v-if="loadError" class="error">{{ loadError }}</p>

    <template v-else>
      <!-- #1821 — an in-flight run is its OWN isolated surface, never a
           lock on the form. Only the Launch button is guarded (the
           server backstops with a 409). -->
      <div
        v-if="active"
        class="mb-4 rounded-lg border border-emerald-500/30 bg-emerald-500/[0.07] p-4"
        data-testid="active-run-panel"
      >
        <div class="flex items-start gap-3">
          <span class="lamp lamp-live mt-1.5"></span>
          <div class="min-w-0 flex-1">
            <strong class="text-emerald-300">
              A QA run is in progress —
              <code class="font-mono text-emerald-200">{{ active.job_name || active.pod_name || active.run_id }}</code>
              <span class="font-normal text-emerald-400/80"> ({{ active.phase }})</span>
            </strong>
            <p class="mt-0.5 text-xs text-ink-600">
              One run at a time — Launch re-enables when this finishes.
              The form stays live, so line up your next run meanwhile.
            </p>
            <div class="mt-1.5 flex items-center gap-3 text-xs">
              <router-link
                v-if="active.run_id"
                :to="`/runs/${active.run_id}`"
                class="text-emerald-400"
              >
                Open live run →
              </router-link>
              <button class="text-brand-700 hover:underline" @click="logsOpen = !logsOpen">
                {{ logsOpen ? 'Hide' : 'Show' }} live logs
              </button>
            </div>
          </div>
        </div>
        <pre
          v-if="logsOpen"
          ref="logBox"
          class="mt-3 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md border border-hairline/5 bg-void p-3 font-mono text-xs leading-relaxed text-ink-700"
        >{{ logText }}</pre>
      </div>

      <!-- ── 01 · TARGET ─────────────────────────────────────────── -->
      <section class="section">
        <header class="section-head">
          <span class="console-index">01</span>
          <h4>Target</h4>
          <p class="section-sub">
            The site each persona signs up for and walks through. Must be
            reachable from the harness pod.
          </p>
        </header>
        <input
          type="url"
          class="input max-w-2xl font-mono"
          maxlength="500"
          :placeholder="DEFAULT_TARGET_URL"
          v-model.trim="targetUrl"
          aria-label="Target URL"
        />
        <p class="help mt-1">
          Cluster-internal URLs work; public URLs work too. Default:
          <code class="rounded bg-ink-200 px-1 py-0.5 font-mono text-brand-800">{{ DEFAULT_TARGET_URL }}</code>
          (the in-cluster Test Ease sandbox).
        </p>

        <!-- Presets — saved persona + coverage combos. #1822 retired the
             /scenarios page; saving and loading presets lives here now. -->
        <div v-if="presets.length" class="mt-3 flex flex-wrap items-center gap-1.5">
          <span class="text-xs uppercase tracking-wide text-ink-500">Presets</span>
          <span
            v-for="s in presets"
            :key="s.id"
            class="group inline-flex items-center gap-1 rounded-full bg-ink-100 pl-2.5 pr-1 py-0.5 text-xs text-ink-700 ring-1 ring-inset ring-hairline/5"
          >
            <button
              class="hover:text-brand-700"
              :title="s.description || `Apply preset: ${s.name}`"
              :data-testid="`preset-apply-${s.id}`"
              @click="applyPreset(s)"
            >
              {{ s.name }}
            </button>
            <button
              class="rounded-full px-1 text-ink-500 opacity-0 transition hover:text-red-400 group-hover:opacity-100"
              :title="`Delete preset ${s.name}`"
              :data-testid="`preset-delete-${s.id}`"
              @click="onDeletePreset(s)"
            >
              ×
            </button>
          </span>
        </div>
      </section>

      <!-- ── 02 · WHO RUNS ───────────────────────────────────────── -->
      <section class="section">
        <header class="section-head">
          <span class="console-index">02</span>
          <h4>Who runs</h4>
          <p class="section-sub">
            {{ selected.length }} of {{ personaIds.length }} selected.
            Activated personas (★) are pre-selected for every new run.
          </p>
          <div class="ml-auto flex items-center gap-2 text-xs">
            <input
              type="search"
              class="input !w-44 !py-1"
              placeholder="Filter personas…"
              v-model.trim="personaFilter"
              data-testid="persona-filter"
            />
            <button class="text-brand-700 hover:underline" type="button" @click="selectAllPersonas">
              Select all
            </button>
            <span class="text-ink-500" aria-hidden="true">·</span>
            <button
              class="text-brand-700 hover:underline disabled:cursor-not-allowed disabled:text-ink-500"
              type="button"
              :disabled="!selected.length"
              @click="clearPersonaSelection"
            >
              Clear
            </button>
          </div>
        </header>
        <div class="persona-grid">
          <button
            v-for="p in filteredPersonas"
            :key="personaMeta(p).id"
            type="button"
            :class="['persona-card', { selected: isPersonaSelected(personaMeta(p).id) }]"
            @click="togglePersona(personaMeta(p).id)"
            :aria-pressed="isPersonaSelected(personaMeta(p).id)"
          >
            <span class="persona-icon" aria-hidden="true">{{ archetypeIcon(personaMeta(p).archetype) }}</span>
            <span class="persona-body">
              <span class="persona-name">{{ personaMeta(p).display_name }}</span>
              <span class="persona-arch">{{ personaMeta(p).archetype || personaMeta(p).id }}</span>
              <span class="persona-chips" v-if="personaMeta(p).region || personaMeta(p).language">
                <span v-if="personaMeta(p).region" class="pill !py-0 text-[10px]">{{ personaMeta(p).region }}</span>
                <span v-if="personaMeta(p).language" class="pill !py-0 text-[10px]">{{ personaMeta(p).language }}</span>
              </span>
            </span>
            <span class="flex flex-col items-end gap-1">
              <!-- #1822 — inline activation. The star saves is_active on
                   the persona (the "pre-select me by default" flag) so
                   the operator never has to detour via /personas. -->
              <span
                role="button"
                tabindex="0"
                class="rounded px-0.5 text-sm leading-none transition"
                :class="isActivated(personaMeta(p).id)
                  ? 'text-amber-300 hover:text-amber-200'
                  : 'text-ink-400 hover:text-ink-600'"
                :title="isActivated(personaMeta(p).id)
                  ? 'Activated — pre-selected for every new run. Click to deactivate.'
                  : 'Not activated. Click to activate (pre-selected for every new run).'"
                :aria-label="`${isActivated(personaMeta(p).id) ? 'Deactivate' : 'Activate'} ${personaMeta(p).display_name}`"
                :data-testid="`persona-default-toggle-${personaMeta(p).id}`"
                @click.stop="toggleActivation(personaMeta(p).id)"
                @keydown.enter.stop.prevent="toggleActivation(personaMeta(p).id)"
              >
                {{ isActivated(personaMeta(p).id) ? '★' : '☆' }}
              </span>
              <span class="persona-check" aria-hidden="true">
                <svg v-if="isPersonaSelected(personaMeta(p).id)" width="16" height="16" viewBox="0 0 16 16">
                  <path fill="currentColor" d="M6.5 11.5L3 8l1.4-1.4 2.1 2.1L11.6 3.5 13 4.9z"/>
                </svg>
              </span>
            </span>
          </button>
        </div>
        <p v-if="activationError" class="error mt-2 text-xs">{{ activationError }}</p>
      </section>

      <!-- ── 03 · THROTTLE ───────────────────────────────────────── -->
      <section class="section">
        <header class="section-head">
          <span class="console-index">03</span>
          <h4>Parallelism &amp; notes</h4>
          <p class="section-sub">
            Pods × concurrency is the simultaneous-persona budget (ceiling {{ MAX_SIMULTANEOUS_PERSONAS }}).
          </p>
        </header>
        <div class="flex flex-wrap items-start gap-6">
          <label class="flex flex-col gap-1 text-xs font-medium text-ink-700">
            Concurrency
            <input
              type="number"
              class="input !w-32"
              min="1"
              max="6"
              step="1"
              placeholder="4 (default)"
              v-model.number="concurrency"
            />
            <span class="font-normal text-ink-500">personas in parallel per pod</span>
          </label>
          <label class="flex flex-col gap-1 text-xs font-medium text-ink-700">
            Pods
            <input
              type="number"
              class="input !w-32"
              min="1"
              max="4"
              step="1"
              placeholder="1 (single pod)"
              v-model.number="podCount"
              data-testid="pod-count-input"
            />
            <span class="font-normal text-ink-500" data-testid="pods-hint">
              <strong class="text-ink-700">{{ simultaneousHint }}</strong>
            </span>
          </label>
          <label class="flex min-w-[16rem] flex-1 flex-col gap-1 text-xs font-medium text-ink-700">
            Run notes
            <input
              type="text"
              class="input"
              maxlength="500"
              placeholder="What is this run about? (optional, shows on the runs list)"
              v-model.trim="runNotes"
            />
          </label>
        </div>
        <p v-if="ceilingExceeded" class="error mt-2 text-xs" data-testid="pods-ceiling-error">
          pods × concurrency ({{ effectivePodCount }} × {{ effectiveConcurrency }}
          = {{ effectivePodCount * effectiveConcurrency }}) exceeds the
          {{ MAX_SIMULTANEOUS_PERSONAS }}-persona simultaneous ceiling.
          Lower one of them to launch.
        </p>
      </section>

      <!-- ── ADVANCED ────────────────────────────────────────────── -->
      <section class="section">
        <button
          class="flex w-full items-center gap-2 text-left"
          data-testid="advanced-toggle"
          @click="advancedOpen = !advancedOpen"
        >
          <span class="inline-block w-3 text-xs text-ink-500">{{ advancedOpen ? '▾' : '▸' }}</span>
          <h4 class="m-0 text-sm font-semibold text-ink-900">Advanced</h4>
          <span class="text-xs text-ink-500">
            models · max turns · duration · MCP tools · coverage
          </span>
          <span
            v-if="overrideCount > 0"
            class="ml-auto pill bg-brand-50 text-brand-800 ring-1 ring-inset ring-brand-500/30"
          >
            {{ overrideCount }} override{{ overrideCount === 1 ? '' : 's' }}
          </span>
        </button>

        <div v-if="advancedOpen" class="mt-4 space-y-6 border-t border-hairline/[0.06] pt-4">
          <!-- Models -->
          <div class="grid gap-4 sm:grid-cols-2">
            <label class="flex flex-col gap-1 text-xs font-medium text-ink-700">
              Explore model
              <select class="select" v-model="exploreModel">
                <option :value="null">(default — Sonnet 4.6)</option>
                <option value="claude-haiku-4-5">claude-haiku-4-5</option>
                <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
                <option value="claude-opus-4-7">claude-opus-4-7</option>
              </select>
              <span class="font-normal text-ink-500">Haiku is ~70% cheaper but lower quality.</span>
            </label>
            <label class="flex flex-col gap-1 text-xs font-medium text-ink-700">
              Report model
              <select class="select" v-model="reportModel">
                <option :value="null">(default — Opus 4.7)</option>
                <option value="claude-haiku-4-5">claude-haiku-4-5</option>
                <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
                <option value="claude-opus-4-7">claude-opus-4-7</option>
              </select>
              <span class="font-normal text-ink-500">
                Per-persona review synthesis — small token volume, big quality impact.
              </span>
            </label>
          </div>

          <!-- Max turns -->
          <div class="slider-field max-w-2xl">
            <div class="slider-row">
              <span class="slider-label">Max turns</span>
              <span class="slider-value">
                {{ maxTurns ?? MAX_TURNS_DEFAULT }}
                <span v-if="maxTurns == null" class="text-xs font-normal text-ink-500">(default)</span>
              </span>
            </div>
            <input
              type="range"
              class="slider"
              :min="MAX_TURNS_MIN"
              :max="MAX_TURNS_MAX"
              :step="MAX_TURNS_STEP"
              :value="maxTurns ?? MAX_TURNS_DEFAULT"
              @input="(e) => { maxTurns = Number(e.target.value) }"
              aria-label="Max turns per persona"
            />
            <div class="slider-ticks" aria-hidden="true">
              <span
                v-for="t in MAX_TURNS_TICKS"
                :key="t.value"
                class="tick"
                :style="{ left: ((t.value - MAX_TURNS_MIN) / (MAX_TURNS_MAX - MAX_TURNS_MIN) * 100) + '%' }"
              >
                <span class="tick-mark"></span>
                <span class="tick-label">{{ t.value }} · {{ t.label }}</span>
              </span>
            </div>
            <div class="slider-actions">
              <button
                v-if="maxTurns != null"
                type="button"
                class="text-xs text-brand-700 hover:underline"
                @click="maxTurns = null"
              >
                Reset to default ({{ MAX_TURNS_DEFAULT }})
              </button>
            </div>
            <p class="help">
              <strong>A ceiling, not a target.</strong> Most personas finish
              naturally well before the cap. Lower (20–30) for a fast sniff
              test; higher (1000+) only when chasing a regression. Above
              ~400 turns the wall-clock duration below is usually the
              binding constraint.
            </p>
          </div>

          <!-- Run duration -->
          <div class="slider-field max-w-2xl">
            <div class="slider-row">
              <span class="slider-label">Run duration</span>
              <span class="slider-value">
                {{ formatDuration(runDurationS ?? RUN_DURATION_DEFAULT) }}
                <span v-if="runDurationS == null" class="text-xs font-normal text-ink-500">(default)</span>
              </span>
            </div>
            <input
              type="range"
              class="slider"
              :min="RUN_DURATION_MIN"
              :max="RUN_DURATION_MAX"
              :step="RUN_DURATION_STEP"
              :value="runDurationS ?? RUN_DURATION_DEFAULT"
              @input="(e) => { runDurationS = Number(e.target.value) }"
              aria-label="Wall-clock duration per persona"
            />
            <div class="slider-ticks" aria-hidden="true">
              <span
                v-for="t in RUN_DURATION_TICKS"
                :key="t.value"
                class="tick"
                :style="{ left: ((t.value - RUN_DURATION_MIN) / (RUN_DURATION_MAX - RUN_DURATION_MIN) * 100) + '%' }"
              >
                <span class="tick-mark"></span>
                <span class="tick-label">{{ t.label }}</span>
              </span>
            </div>
            <div class="slider-actions">
              <button
                v-if="runDurationS != null"
                type="button"
                class="text-xs text-brand-700 hover:underline"
                @click="runDurationS = null"
              >
                Reset to default (2 h)
              </button>
            </div>
            <p class="help">
              How long each persona may run before the harness stops them.
              5 min verifies the rig still boots; 2 h is the default and
              matches the cluster CronJob runs.
            </p>
          </div>

          <!-- MCP tools (#1031 Slice C) -->
          <div class="mcp-tools">
            <button class="flex items-center gap-2 text-sm font-semibold text-ink-900" @click="mcpOpen = !mcpOpen">
              <span class="inline-block w-3 text-xs text-ink-500">{{ mcpOpen ? '▾' : '▸' }}</span>
              MCP tools
              <span class="text-xs font-normal text-ink-500">
                ({{ enabledMCPServers.length }} of {{ mcpCatalog.length }} enabled)
              </span>
            </button>
            <p class="help mt-1 pl-5">
              Which MCP servers each persona can call this run. Unticking
              email skips the verification round-trip; unticking findings
              means the persona can't record anything. Defaults match the
              catalog's <router-link to="/mcp-tools">default-on</router-link> set.
            </p>
            <div v-if="mcpOpen" class="mcp-body mt-2 max-h-96 overflow-y-auto rounded-md border border-hairline/5 bg-ink-50 p-3 pl-5">
              <p v-if="mcpError" class="error">{{ mcpError }}</p>
              <p v-else-if="!mcpCatalog.length" class="muted">Loading MCP catalog…</p>
              <template v-else>
                <label
                  v-for="s in mcpCatalog"
                  :key="s.id"
                  class="mcp-row flex items-start gap-2 border-b border-hairline/5 py-2 last:border-b-0"
                >
                  <input type="checkbox" :value="s.id" v-model="enabledMCPServers" class="mt-0.5 accent-cyan-400" />
                  <span class="flex flex-col gap-0.5 text-sm">
                    <strong class="text-ink-900">{{ s.display_name }}</strong>
                    <span class="text-xs text-ink-600">{{ s.description }}</span>
                  </span>
                </label>
              </template>
            </div>
          </div>

          <!-- Coverage requirements (#861) -->
          <div class="coverage">
            <button class="flex items-center gap-2 text-sm font-semibold text-ink-900" @click="coverageOpen = !coverageOpen">
              <span class="inline-block w-3 text-xs text-ink-500">{{ coverageOpen ? '▾' : '▸' }}</span>
              Coverage requirements
              <span v-if="mandatoryActionIds.length" class="text-xs font-normal text-ink-500">
                ({{ mandatoryActionIds.length }} selected)
              </span>
            </button>
            <p class="help mt-1 pl-5">
              Tick items the persona MUST attempt this session. Unticked
              items stay in the persona's free-rein space.
            </p>

            <div v-if="coverageOpen" class="mt-2 max-h-96 overflow-y-auto rounded-md border border-hairline/5 bg-ink-50 p-3 pl-5">
              <p v-if="coverageError" class="error">{{ coverageError }}</p>
              <p v-else-if="!coverageCatalog" class="muted">Loading coverage catalog…</p>
              <template v-else>
                <div v-for="cat in coverageCatalog.categories" :key="cat" class="mb-2">
                  <div class="flex items-baseline gap-3">
                    <button class="text-sm font-semibold text-ink-800 hover:text-ink-900" @click="toggleCategory(cat)">
                      {{ openCategories[cat] ? '▾' : '▸' }} {{ cat }}
                      <span class="text-xs font-normal text-ink-500">
                        ({{ countSelectedInCategory(cat) }} / {{ actionsByCategory[cat]?.length || 0 }})
                      </span>
                    </button>
                    <button
                      class="text-xs text-brand-700 hover:underline"
                      @click="selectAllInCategory(cat)"
                      :title="`Select every action in ${cat}`"
                    >
                      select all
                    </button>
                    <button
                      v-if="countSelectedInCategory(cat) > 0"
                      class="text-xs text-brand-700 hover:underline"
                      @click="clearCategory(cat)"
                      :title="`Clear ${cat} selections`"
                    >
                      clear
                    </button>
                  </div>
                  <div v-if="openCategories[cat]" class="ml-5 mt-1.5 grid gap-1">
                    <label
                      v-for="action in actionsByCategory[cat] || []"
                      :key="action.id"
                      class="grid grid-cols-[auto_auto_1fr] items-baseline gap-x-2 text-sm"
                    >
                      <input type="checkbox" :value="action.id" v-model="mandatoryActionIds" class="accent-cyan-400" />
                      <code class="whitespace-nowrap font-mono text-xs text-brand-800">{{ action.id }}</code>
                      <span class="text-ink-700">{{ action.human_description }}</span>
                    </label>
                  </div>
                </div>
              </template>
            </div>

            <div v-if="mandatoryActionIds.length" class="mt-2 flex flex-wrap gap-1.5 pl-5">
              <span
                v-for="id in mandatoryActionIds"
                :key="id"
                class="inline-flex items-center gap-1 rounded-full bg-brand-50 px-2 py-0.5 font-mono text-xs text-brand-800 ring-1 ring-inset ring-brand-500/30"
                :title="actionsById[id]?.human_description || ''"
              >
                {{ id }}
                <button
                  class="px-0.5 text-brand-700 hover:text-red-400"
                  @click="removeMandatory(id)"
                  title="Remove from mandatory list"
                >
                  ×
                </button>
              </span>
            </div>
          </div>

          <p class="text-xs text-ink-500">
            Billing is fixed: every run bills Claude Code Max (the
            operator's subscription) — there is no per-run dollar cost.
          </p>
        </div>
      </section>

      <!-- ── Launch bar ──────────────────────────────────────────── -->
      <div class="cta-bar">
        <div class="cta-summary">
          <strong class="font-display">{{ runLabel }}</strong>
          <span class="muted">
            against <code>{{ targetUrl || DEFAULT_TARGET_URL }}</code>
          </span>
          <div
            v-if="!selected.length"
            class="w-full text-xs text-amber-300"
            data-testid="persona-selection-hint"
          >
            Nothing selected — pick at least one persona above to launch.
          </div>
        </div>
        <div class="cta-actions">
          <button
            class="text-sm text-brand-700 hover:underline disabled:cursor-not-allowed disabled:text-ink-500 disabled:no-underline"
            :disabled="!canSavePreset || saving"
            :title="
              !canSavePreset
                ? 'Pick at least one persona or one mandatory coverage item first — an empty preset would just save the defaults.'
                : 'Save the current persona / coverage selection as a reusable preset.'
            "
            data-testid="save-preset-toggle"
            @click="showSavePreset = !showSavePreset"
          >
            {{ showSavePreset ? 'Cancel save' : '💾 Save as preset' }}
          </button>
          <button
            class="cta-start"
            :disabled="busy || !!active || ceilingExceeded || !selected.length"
            :title="
              active
                ? 'A run is already in progress — wait for it to finish before starting another.'
                : ceilingExceeded
                  ? `pods × concurrency exceeds the ${MAX_SIMULTANEOUS_PERSONAS}-persona simultaneous ceiling — lower one to launch`
                  : !selected.length
                    ? 'Pick at least one persona to launch.'
                    : undefined
            "
            data-testid="cta-start"
            @click="trigger"
          >
            {{ busy ? 'Starting…' : active ? 'Run in progress…' : `▶ Launch ${selected.length || ''}`.trim() }}
          </button>
        </div>
        <p v-if="error" class="error cta-error">{{ error }}</p>
      </div>

      <!-- Save-as-preset inline form (inline, not a modal — the
           selections being saved live on this same page). -->
      <div v-if="showSavePreset" class="panel mt-3 grid grid-cols-1 gap-3 p-4 sm:grid-cols-2">
        <label class="flex flex-col gap-1 text-xs font-medium text-ink-700">
          Id (slug)
          <input type="text" class="input" v-model.trim="newPreset.id" placeholder="smoke-billing" maxlength="64" />
        </label>
        <label class="flex flex-col gap-1 text-xs font-medium text-ink-700">
          Name
          <input type="text" class="input" v-model.trim="newPreset.name" placeholder="Smoke billing" maxlength="120" />
        </label>
        <label class="flex flex-col gap-1 text-xs font-medium text-ink-700 sm:col-span-2">
          Description
          <input type="text" class="input" v-model.trim="newPreset.description" placeholder="(optional)" maxlength="500" />
        </label>
        <div class="flex items-center gap-3 sm:col-span-2">
          <button
            class="btn"
            :disabled="!newPreset.id || !newPreset.name || saving"
            data-testid="save-preset-submit"
            @click="onSavePreset"
          >
            {{ saving ? 'Saving…' : 'Save preset' }}
          </button>
          <span v-if="saveError" class="error text-xs">{{ saveError }}</span>
          <span v-if="saveOk" class="text-xs text-emerald-300">Saved!</span>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  ACTIVE_LOGS_URL,
  createScenario,
  deleteScenario,
  getActiveRun,
  getCoverageCatalog,
  getPersonas,
  listMCPServers,
  listPersonas,
  listScenarios,
  triggerRun,
  updatePersona,
} from '../api'
import { formatApiError } from '../lib/apiError'

const route = useRoute()

// Client-side cap so a multi-hour run's log can't balloon the DOM.
const MAX_LOG_LINES = 2000

// #1821 — the simultaneous-persona ceiling, mirrored from the API layer
// (MAX_SIMULTANEOUS_PERSONAS in api/qa_review_api/runs.py). The client
// guards pods × concurrency up-front; the server enforces it (422).
const MAX_SIMULTANEOUS_PERSONAS = 8

// #1047 — the in-cluster Test Ease sandbox, pre-populated so an operator
// hitting Launch without changing anything gets the historical
// "test the sandbox" behaviour explicitly.
const DEFAULT_TARGET_URL = 'https://sandbox.slyreply.ai'

// #1047 / #1115 — max-turns slider calibration. See the help text: this
// is a runaway ceiling, not a target.
const MAX_TURNS_MIN = 10
const MAX_TURNS_MAX = 5000
const MAX_TURNS_STEP = 10
const MAX_TURNS_DEFAULT = 200
const MAX_TURNS_TICKS = [
  { value: 30, label: 'sniff' },
  { value: 200, label: 'default' },
  { value: 1000, label: 'deep dive' },
  { value: 5000, label: 'no truncation' },
]

// #1115 — run-duration slider. Wall-clock wrapper around the whole
// explore + report phases (QA_RUN_TIMEOUT_S; harness default 7200s).
const RUN_DURATION_MIN = 300
const RUN_DURATION_MAX = 7200
const RUN_DURATION_STEP = 60
const RUN_DURATION_DEFAULT = 7200
const RUN_DURATION_TICKS = [
  { value: 300, label: '5 min · sniff' },
  { value: 1800, label: '30 min' },
  { value: 3600, label: '1 h' },
  { value: 7200, label: '2 h · default' },
]

function formatDuration(seconds) {
  if (seconds == null) return '—'
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`
  const h = Math.floor(seconds / 3600)
  const m = Math.round((seconds % 3600) / 60)
  return m === 0 ? `${h} h` : `${h} h ${m} min`
}

// Emoji per archetype keyword — a quick visual anchor per card without
// shipping per-persona images. Falls back to 🧪.
const ARCHETYPE_ICONS = [
  [/mobile|phone-first/i, '📱'],
  [/desktop|comparison/i, '🖥️'],
  [/privacy|policy/i, '🔒'],
  [/shopper/i, '🛒'],
  [/signup/i, '✅'],
  [/verifier|email/i, '📧'],
  [/oauth/i, '🔑'],
  [/returning|forgetter/i, '🔁'],
  [/upgrade|buyer/i, '💳'],
  [/declined/i, '⛔'],
  [/cancel/i, '🧯'],
  [/admin|team/i, '👥'],
  [/export/i, '📤'],
  [/settings|sprawler/i, '⚙️'],
  [/search/i, '🔍'],
  [/docs/i, '📚'],
  [/keyboard/i, '⌨️'],
  [/screen-?reader|a11y/i, '🦯'],
  [/slow|connection/i, '🐢'],
  [/long|input|unicode/i, '✍️'],
  [/adversarial|security/i, '🕵️'],
  [/brand|trademark/i, '™️'],
  [/first-impression|critic/i, '👀'],
]

function archetypeIcon(archetype) {
  if (!archetype) return '🧪'
  for (const [pattern, icon] of ARCHETYPE_ICONS) {
    if (pattern.test(archetype)) return icon
  }
  return '🧪'
}

// Personas is the catalogue from /api/runs/personas — {id, display_name,
// archetype, region, language} objects (bare-string ids from pre-#1047
// servers are still normalised defensively).
const personas = ref([])
const selected = ref([])
const personaFilter = ref('')
// null = pod-spec defaults; see TriggerRunRequest for the bounds.
const concurrency = ref(null)
const podCount = ref(null)
const exploreModel = ref(null)
const reportModel = ref(null)
const maxTurns = ref(null)
const runDurationS = ref(null)
const runNotes = ref('')
const targetUrl = ref(DEFAULT_TARGET_URL)
// #1031 — per-run MCP server selection, pre-populated with catalog
// defaults so an untouched panel reproduces historical behaviour.
const mcpCatalog = ref([])
const enabledMCPServers = ref([])
const mcpOpen = ref(false)
const mcpError = ref('')
// #861 — coverage requirements panel state.
const mandatoryActionIds = ref([])
const coverageOpen = ref(false)
const coverageCatalog = ref(null)
const coverageError = ref('')
const openCategories = ref({})
const advancedOpen = ref(false)
// Presets (saved scenarios) — load/apply/save/delete, all on this page
// since #1822 retired the /scenarios builder.
const presets = ref([])
const showSavePreset = ref(false)
const newPreset = ref({ id: '', name: '', description: '' })
const saving = ref(false)
const saveError = ref('')
const saveOk = ref(false)
const active = ref(null)
const busy = ref(false)
const error = ref('')
const loadError = ref('')
const activationError = ref('')
const logsOpen = ref(false)
const logLines = ref([])
const logBox = ref(null)

let pollTimer = null
let logSource = null

const personaIds = computed(() =>
  personas.value.map((p) => (typeof p === 'string' ? p : p.id)),
)

function personaMeta(idOrObj) {
  if (typeof idOrObj === 'string') {
    return { id: idOrObj, display_name: idOrObj, archetype: '', region: null, language: null }
  }
  return idOrObj
}

const filteredPersonas = computed(() => {
  const q = personaFilter.value.toLowerCase()
  if (!q) return personas.value
  return personas.value.filter((p) => {
    const m = personaMeta(p)
    return [m.id, m.display_name, m.archetype, m.region, m.language]
      .filter(Boolean)
      .some((v) => String(v).toLowerCase().includes(q))
  })
})

// DB-side persona state — carries is_active for the inline ★ toggles
// and the pre-seeded selection.
const dbPersonas = ref([])
const activatedIds = computed(() =>
  dbPersonas.value.filter((p) => p.is_active && !p.hidden).map((p) => p.persona_id),
)
function isActivated(id) {
  return activatedIds.value.includes(id)
}

// #1822 — inline activation. Optimistic flip; revert on error. The flag
// only affects which personas are PRE-selected on this page — the run
// itself always sends the explicit selection.
async function toggleActivation(id) {
  activationError.value = ''
  const row = dbPersonas.value.find((p) => p.persona_id === id)
  if (!row) {
    activationError.value = `Persona ${id} has no registry row to activate.`
    return
  }
  const next = !row.is_active
  row.is_active = next
  try {
    await updatePersona(id, { is_active: next })
  } catch (e) {
    row.is_active = !next
    activationError.value = `Could not ${next ? 'activate' : 'deactivate'} ${id}: ${formatApiError(e)}`
  }
}

// The launch label never lies (#1089's "Run 0 personas" is structurally
// impossible now): selection is always explicit, pre-seeded from the
// activated set, and an empty selection disables Launch outright.
const runLabel = computed(() => {
  const n = selected.value.length
  return `Launch ${n} persona${n === 1 ? '' : 's'}`
})

// #1821 — effective pods × concurrency, mirroring server resolution.
const effectivePodCount = computed(() => podCount.value || 1)
const effectiveConcurrency = computed(() => concurrency.value || 1)
const simultaneousHint = computed(() => {
  const n = effectivePodCount.value * effectiveConcurrency.value
  return `up to ${n} persona${n === 1 ? '' : 's'} at once (pods × concurrency).`
})
const ceilingExceeded = computed(
  () =>
    effectivePodCount.value * effectiveConcurrency.value >
    MAX_SIMULTANEOUS_PERSONAS,
)

const overrideCount = computed(() => {
  let n = [exploreModel.value, reportModel.value, maxTurns.value, runDurationS.value]
    .filter((v) => v != null).length
  if (mandatoryActionIds.value.length) n += 1
  if (!isDefaultMcpSelection.value && mcpCatalog.value.length) n += 1
  return n
})

function selectAllPersonas() {
  selected.value = [...personaIds.value]
}
function clearPersonaSelection() {
  selected.value = []
}
function togglePersona(id) {
  const i = selected.value.indexOf(id)
  if (i >= 0) selected.value.splice(i, 1)
  else selected.value.push(id)
}
function isPersonaSelected(id) {
  return selected.value.includes(id)
}

const logText = computed(() =>
  logLines.value.length
    ? logLines.value.join('\n')
    : '(connecting to the log stream…)',
)

async function refreshActive() {
  try {
    active.value = await getActiveRun()
  } catch {
    /* transient cluster blip — keep the last known state */
  }
}

function openLogStream() {
  if (logSource) return
  logLines.value = []
  // PR-G E35 — bound EventSource auto-reconnect: tear down after
  // MAX_SSE_RETRIES consecutive failures instead of looping forever.
  let sseFailures = 0
  const MAX_SSE_RETRIES = 5
  logSource = new EventSource(ACTIVE_LOGS_URL)
  logSource.onopen = () => {
    sseFailures = 0
  }
  logSource.onmessage = (ev) => {
    logLines.value.push(ev.data)
    if (logLines.value.length > MAX_LOG_LINES) {
      logLines.value = logLines.value.slice(-MAX_LOG_LINES)
    }
  }
  logSource.onerror = () => {
    sseFailures += 1
    if (
      logSource &&
      (logSource.readyState === EventSource.CLOSED ||
        sseFailures >= MAX_SSE_RETRIES)
    ) {
      const note = '(log stream disconnected — close + re-open to try again)'
      if (!logLines.value.length || logLines.value.at(-1) !== note) {
        logLines.value.push(note)
      }
      closeLogStream()
    }
  }
  logSource.addEventListener('end', closeLogStream)
}

function closeLogStream() {
  if (logSource) {
    logSource.close()
    logSource = null
  }
}

// #1031 — "selection equals catalog defaults" check, shared by the
// confirm phrase, the override count, and the request body.
const isDefaultMcpSelection = computed(() => {
  const defaultEnabledIds = mcpCatalog.value
    .filter((s) => s.default_enabled)
    .map((s) => s.id)
    .sort()
  const currentMcpIds = [...enabledMCPServers.value].sort()
  return (
    defaultEnabledIds.length === currentMcpIds.length &&
    defaultEnabledIds.every((id, i) => id === currentMcpIds[i])
  )
})

async function trigger() {
  error.value = ''
  const want = [...selected.value]
  if (!want.length) return
  const who = want.join(', ')
  const conc = concurrency.value || undefined
  const concPhrase = conc
    ? ` at concurrency ${conc}`
    : ' at the default concurrency'
  const pods = podCount.value && podCount.value > 1 ? podCount.value : undefined
  const podsPhrase = pods
    ? `\n  • pods: ${pods} (up to ${pods * (conc || 1)} personas at once)`
    : ''
  const explore = exploreModel.value || undefined
  const report = reportModel.value || undefined
  const explorePhrase = explore
    ? `\n  • explore model: ${explore}`
    : '\n  • explore model: default (Sonnet 4.6)'
  const reportPhrase = report
    ? `\n  • report model: ${report}`
    : '\n  • report model: default (Opus 4.7)'
  const turns = maxTurns.value || undefined
  const turnsPhrase = turns
    ? `\n  • max turns: ${turns}`
    : '\n  • max turns: default (200)'
  const durationS = runDurationS.value || undefined
  const durationPhrase = durationS
    ? `\n  • run duration: ${formatDuration(durationS)}`
    : '\n  • run duration: default (2 h)'
  const notes = runNotes.value || ''
  const notesPhrase = notes ? `\n  • notes: ${notes}` : ''
  const mandatory = [...mandatoryActionIds.value]
  const mandatoryPhrase = mandatory.length
    ? `\n  • mandatory coverage actions: ${mandatory.length} selected`
    : ''
  // #1018 — surface the target URL so a wrong paste is spotted before
  // burning Claude credits.
  const target = targetUrl.value.trim()
  const targetPhrase = target
    ? `\n  • target URL: ${target}`
    : '\n  • target URL: default (in-cluster sandbox)'
  const currentMcpIds = [...enabledMCPServers.value].sort()
  const mcpPhrase = isDefaultMcpSelection.value
    ? ''
    : `\n  • MCP servers: ${
        currentMcpIds.length ? currentMcpIds.join(', ') : '(none — persona has no tools)'
      }`
  const backendPhrase = '\n  • backend: Claude Code MAX (personal subscription)'
  const closingLine =
    '\n\nThis run bills against the operator PERSONAL Claude Code Max ' +
    'subscription. Concurrent personas share one session window. Only ' +
    'one run can be active at a time.'
  if (
    !window.confirm(
      `Start a QA harness run for ${who}${concPhrase}?` +
        `${targetPhrase}${podsPhrase}${backendPhrase}${explorePhrase}${reportPhrase}${turnsPhrase}${durationPhrase}${notesPhrase}${mandatoryPhrase}${mcpPhrase}` +
        closingLine,
    )
  ) {
    return
  }
  busy.value = true
  try {
    await triggerRun(want, {
      concurrency: conc,
      podCount: pods,
      exploreModel: explore,
      reportModel: report,
      maxTurns: turns,
      runDurationS: durationS,
      runNotes: notes,
      mandatoryActionIds: mandatory,
      targetUrl: target,
      // #1031 — omit when the selection matches catalog defaults so the
      // server keeps the pre-Slice-C "catalog decides" semantics.
      enabledMCPServers: isDefaultMcpSelection.value ? [] : currentMcpIds,
    })
    await refreshActive()
    logsOpen.value = true
  } catch (e) {
    const status = e.response && e.response.status
    error.value =
      status === 409
        ? 'A run is already in progress — watch its logs above.'
        : `Could not start the run (HTTP ${status || '?'}): ${formatApiError(e)}`
    await refreshActive()
  } finally {
    busy.value = false
  }
}

// #861 — lazy-fetch the coverage catalog on first expansion.
watch(coverageOpen, async (open) => {
  if (!open || coverageCatalog.value) return
  try {
    coverageCatalog.value = await getCoverageCatalog()
  } catch (e) {
    coverageError.value = `Could not load coverage catalog: ${e.message}`
  }
})

// #1031 — eager-fetch the MCP catalog (the default_enabled flags
// pre-populate the v-model array even if the panel never opens).
async function _loadMcpCatalogOnce() {
  try {
    const servers = await listMCPServers()
    mcpCatalog.value = servers
    enabledMCPServers.value = servers
      .filter((s) => s.default_enabled)
      .map((s) => s.id)
  } catch (e) {
    mcpError.value = `Could not load MCP catalog: ${e.message}`
  }
}

const actionsByCategory = computed(() => {
  if (!coverageCatalog.value) return {}
  const map = {}
  for (const a of coverageCatalog.value.actions) {
    if (!map[a.category]) map[a.category] = []
    map[a.category].push(a)
  }
  return map
})
const actionsById = computed(() => {
  if (!coverageCatalog.value) return {}
  return Object.fromEntries(coverageCatalog.value.actions.map((a) => [a.id, a]))
})

function toggleCategory(cat) {
  openCategories.value[cat] = !openCategories.value[cat]
}
function countSelectedInCategory(cat) {
  const ids = new Set((actionsByCategory.value[cat] || []).map((a) => a.id))
  return mandatoryActionIds.value.filter((id) => ids.has(id)).length
}
function selectAllInCategory(cat) {
  const ids = (actionsByCategory.value[cat] || []).map((a) => a.id)
  const merged = new Set([...mandatoryActionIds.value, ...ids])
  mandatoryActionIds.value = Array.from(merged)
  openCategories.value[cat] = true
}
function clearCategory(cat) {
  const ids = new Set((actionsByCategory.value[cat] || []).map((a) => a.id))
  mandatoryActionIds.value = mandatoryActionIds.value.filter(
    (id) => !ids.has(id),
  )
}
function removeMandatory(id) {
  mandatoryActionIds.value = mandatoryActionIds.value.filter((x) => x !== id)
}

// ── Presets (saved scenarios) ──────────────────────────────────────
async function _loadPresets() {
  try {
    presets.value = await listScenarios()
  } catch {
    /* best-effort — the preset row just doesn't render */
  }
}

function applyPreset(s) {
  if (s.persona_id && personaIds.value.includes(s.persona_id)) {
    selected.value = [s.persona_id]
  }
  if (Array.isArray(s.mandatory_action_ids) && s.mandatory_action_ids.length) {
    mandatoryActionIds.value = s.mandatory_action_ids.slice()
    advancedOpen.value = true
    coverageOpen.value = true // show the operator what they got
  }
}

async function onDeletePreset(s) {
  if (!window.confirm(`Delete preset "${s.name}"? This cannot be undone.`)) return
  try {
    await deleteScenario(s.id)
    presets.value = presets.value.filter((p) => p.id !== s.id)
  } catch (e) {
    error.value = `Could not delete preset: ${formatApiError(e)}`
  }
}

const canSavePreset = computed(
  () => mandatoryActionIds.value.length > 0 || selected.value.length > 0,
)

async function onSavePreset() {
  saveError.value = ''
  saveOk.value = false
  saving.value = true
  try {
    const personaId = selected.value[0] || personaIds.value[0] || ''
    await createScenario({
      id: newPreset.value.id,
      name: newPreset.value.name,
      description: newPreset.value.description || '',
      persona_id: personaId,
      mandatory_action_ids: [...mandatoryActionIds.value],
    })
    saveOk.value = true
    newPreset.value = { id: '', name: '', description: '' }
    _loadPresets()
    setTimeout(() => {
      showSavePreset.value = false
      saveOk.value = false
    }, 1500)
  } catch (e) {
    saveError.value = e.response?.data?.detail || e.message
  } finally {
    saving.value = false
  }
}

watch(logsOpen, (open) => {
  if (open) openLogStream()
  else closeLogStream()
})
watch(active, (now, prev) => {
  if (!now && prev) closeLogStream()
})
watch(logText, async () => {
  await nextTick()
  if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight
})

async function _refreshDbPersonas() {
  try {
    dbPersonas.value = await listPersonas({ includeHidden: false })
  } catch {
    /* swallow — the ★ toggles degrade to "no registry rows" */
  }
}

onMounted(async () => {
  try {
    personas.value = await getPersonas()
  } catch (e) {
    loadError.value = `Run control is unavailable: ${e.message}`
    return
  }
  await _refreshDbPersonas()
  // #1822 follow-up — deep-link pre-selection: /new-run?personas=a,b
  // (e.g. the "Run this persona" button on a persona's page). An explicit
  // query selection wins over the activated-set pre-seed; unknown ids are
  // dropped silently so a stale link still lands on a working console.
  const fromQuery = String(route.query.personas ?? route.query.persona ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter((id) => id && personaIds.value.includes(id))
  if (fromQuery.length) {
    selected.value = fromQuery
  } else if (!selected.value.length && activatedIds.value.length) {
    // Pre-seed the selection from the activated set (#1822 — selection is
    // always explicit; activation just decides the starting point).
    selected.value = activatedIds.value.filter((id) =>
      personaIds.value.includes(id),
    )
  }
  _loadMcpCatalogOnce()
  _loadPresets()
  await refreshActive()
  if (active.value) logsOpen.value = true
  pollTimer = setInterval(refreshActive, 10000)
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  closeLogStream()
})
</script>

<style scoped>
/* The numbered section index — the launch console's mission-control
   wayfinding glyph. */
.console-index {
  font-family: theme('fontFamily.display');
  @apply -mt-0.5 inline-block rounded border border-brand-500/30 bg-brand-50 px-1.5 py-0.5 text-[10px] font-bold tracking-widest text-brand-700;
}
</style>
