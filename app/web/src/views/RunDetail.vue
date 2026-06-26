<template>
  <div class="mx-auto max-w-7xl p-6">
    <router-link to="/" class="mb-3 inline-block text-xs text-ink-600 hover:text-ink-700">
      ← All runs
    </router-link>

    <div v-if="loading" class="text-sm text-ink-600">Loading run…</div>
    <div v-else-if="error" class="text-sm text-red-400">{{ error }}</div>

    <template v-else-if="run">
      <!-- Header card -->
      <div class="panel mb-4 flex flex-wrap items-start gap-5 p-6">
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-2">
            <h1>
              <code class="rounded bg-ink-100 px-2 py-0.5 font-mono text-lg">{{ run.run_id }}</code>
            </h1>
            <span class="pill" :class="`pill-status-${run.status}`">{{ run.status }}</span>
            <span
              v-if="isMaxBilled"
              class="pill tint-violet ring-1 ring-inset"
              title="Billed to operator's Claude Code Max subscription, not the org API"
              data-testid="max-billed-badge"
            >
              Max
            </span>
            <!-- #1115 follow-up — live indicator. When the run is still
                 in progress the page auto-refreshes the timeline + run
                 document every 4s; this pill tells the operator that
                 their view IS updating, not stale. -->
            <span
              v-if="isLive"
              class="pill bg-emerald-500/10 text-emerald-300 ring-1 ring-inset ring-emerald-500/30 flex items-center gap-1.5"
              title="Run is in progress — timeline + findings refresh every 4 seconds"
              data-testid="run-live-indicator"
            >
              <span class="lamp lamp-live"></span>
              Live · auto-refreshing
            </span>
          </div>
          <p class="mt-2 text-xs text-ink-600">
            Started {{ formatDate(run.started_at) }}
            <template v-if="run.finished_at">
              · Finished {{ formatDate(run.finished_at) }}
            </template>
            <template v-else>
              · still running
            </template>
          </p>
          <div class="mt-3 flex flex-wrap items-center gap-2">
            <span
              v-for="pid in run.personas || []"
              :key="pid"
              class="flex items-center gap-1.5 rounded-full bg-ink-100 px-2 py-1 text-xs"
            >
              <Avatar
                :seed="personaMeta[pid]?.avatar_seed || pid"
                :color-token="personaMeta[pid]?.color_token || 'slate'"
                size="xs"
              />
              {{ personaMeta[pid]?.display_name || pid }}
            </span>
          </div>
        </div>

        <div class="flex flex-col items-end gap-2 text-right text-xs text-ink-600">
          <!--
            #1822 — the $ figure is gone entirely (a "$0.0000" / estimate
            on Max-billed runs was noise, and operators read tokens, not
            dollars). The token readout is the primary telemetry figure;
            the violet "Max" pill in the title row carries the billing
            qualifier (operator's Claude Code Max subscription).
          -->
          <div title="Token usage across all turns in this run: input + output + cache (cache writes count toward cost at the discounted rate).">
            <span class="mr-1.5 text-ink-500">Tokens</span>
            <span class="readout text-sm text-ink-900">
              {{ totals.input_tokens.toLocaleString() }} in /
              {{ totals.output_tokens.toLocaleString() }} out /
              {{ totals.cache_tokens.toLocaleString() }} cache
            </span>
          </div>
          <div class="flex gap-2">
            <button
              class="btn-primary btn"
              :disabled="filing || run.status === 'filed'"
              :title="
                run.status === 'filed'
                  ? 'A GitHub issue has already been filed from this run.'
                  : `Compose a GitHub issue from the ${(run?.findings || []).length} finding(s) on this run (only those marked 'open' or 'included') and post it to the configured GitHub repo. You'll get a chance to confirm before it posts.`
              "
              @click="onFileIssue"
            >
              {{
                run.status === 'filed'
                  ? 'Issue filed'
                  : filing
                    ? 'Filing…'
                    : 'File GitHub issue'
              }}
            </button>
          </div>
          <p v-if="run.gh_issue_url" class="text-xs">
            <a :href="run.gh_issue_url" target="_blank" rel="noopener">View issue →</a>
          </p>
          <p v-if="fileError" class="text-xs text-red-400">{{ fileError }}</p>
        </div>
      </div>

      <!--
        #1029 — Slice A of the MCP visibility epic. Show which MCP
        servers the persona(s) actually exercised, with call counts.
        Hidden when the run produced no MCP-shaped tool calls (a raw
        text-only run, or one that pre-dates the recorder wiring).
        #1030 — Slice B upgrade: chip text uses catalog display names
        (looked up against the MCP catalog fetched at mount), with a
        graceful fall-through to the raw id for servers that aren't in
        the catalog yet (a new MCP from #1019 not catalog-added yet).
      -->
      <div
        v-if="(run.mcp_servers_used || []).length"
        class="panel mb-4 flex flex-wrap items-center gap-2 p-4"
        data-testid="mcp-servers-used"
      >
        <span class="text-xs font-medium text-ink-600">MCP servers used</span>
        <router-link
          v-for="m in run.mcp_servers_used"
          :key="m.server"
          :to="{ name: 'mcp-tools' }"
          class="flex items-center gap-1.5 rounded-full bg-ink-100 px-2.5 py-1 text-xs no-underline transition hover:bg-ink-200"
          :title="mcpChipTitle(m)"
        >
          <span class="font-semibold">{{ mcpChipLabel(m.server) }}</span>
          <span class="text-ink-500">{{ m.calls }}</span>
        </router-link>
      </div>

      <!-- Tab strip -->
      <div class="mb-4 flex gap-1 border-b border-ink-200">
        <button
          v-for="t in TABS"
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
      </div>

      <!-- Triage tab (#1169 / slice 1 of #1168) — default landing view.
           Renders fixable findings ranked by severity so blockers and
           majors jump out without scrolling. Praise/observation lives
           in the Findings tab; this view is "what do I fix today". -->
      <div v-if="activeTab === 'triage'" data-testid="triage-view">
        <!-- Empty state: clean run, no fixable findings yet. -->
        <div
          v-if="
            !triageSections.blockers.length &&
              !triageSections.majors.length &&
              !triageSections.others.length
          "
          class="panel panel-pad text-center"
        >
          <p class="text-base font-semibold">{{ isLive ? '⏳ Nothing filed yet.' : '🎉 Nothing to fix.' }}</p>
          <p class="mt-1 text-sm text-ink-600">
            {{ isLive
              ? 'Run in progress — findings appear here the moment a persona files one. Watch the Timeline tab for the live step stream.'
              : 'No actionable findings filed in this run. Take a victory lap.' }}
          </p>
        </div>

        <!-- 🚨 BLOCKERS — full-width cards, body shown, file-issue + trace inline -->
        <section
          v-if="triageSections.blockers.length"
          class="mb-6"
          data-testid="triage-blockers"
        >
          <header class="mb-2 flex items-baseline gap-2">
            <h3 class="text-base font-semibold text-red-300">🚨 Blockers</h3>
            <span class="text-xs text-ink-600">
              {{ triageSections.blockers.length }} · fix today
            </span>
          </header>
          <article
            v-for="f in triageSections.blockers"
            :key="f.finding_id"
            class="panel mb-2 border-l-4 border-l-red-500 bg-red-500/10 p-4"
            :data-testid="`triage-blocker-${f.finding_id}`"
          >
            <!-- #1822 — standardized finding card: severity pill + kind
                 glyph + title on the first row, body below, meta row with
                 persona avatar and the trace / file affordances. -->
            <div class="flex flex-wrap items-start gap-2">
              <span class="pill pill-blocker">blocker</span>
              <span :title="KIND_META[_kindOf(f)].label" aria-hidden="true">{{ KIND_META[_kindOf(f)].icon }}</span>
              <RegressionBadge :finding="f" />
              <span class="text-base font-semibold leading-tight text-ink-900">{{ f.title }}</span>
            </div>
            <p v-if="f.body" class="mt-2 whitespace-pre-wrap text-sm text-ink-800">
              {{ f.body }}
            </p>
            <div class="mt-3 flex flex-wrap items-center gap-3 text-xs text-ink-600">
              <span class="flex items-center gap-1">
                <Avatar
                  :seed="personaMeta[f.persona]?.avatar_seed || f.persona"
                  :color-token="personaMeta[f.persona]?.color_token || 'slate'"
                  size="xs"
                />
                {{ personaMeta[f.persona]?.display_name || f.persona }}
              </span>
              <span>·</span>
              <span>{{ KIND_META[_kindOf(f)].icon }} {{ KIND_META[_kindOf(f)].label }}</span>
              <span class="ml-auto flex items-center gap-2">
                <button
                  type="button"
                  class="text-xs font-medium text-brand-700 transition hover:text-brand-800"
                  :data-testid="`triage-view-trace-${f.finding_id}`"
                  @click="viewTraceForFinding(f)"
                >
                  View trace →
                </button>
                <a
                  v-if="f.gh_issue_url"
                  :href="f.gh_issue_url"
                  target="_blank"
                  rel="noopener"
                  class="pill tint-emerald text-xs"
                  :title="`Filed as #${f.gh_issue_number || '?'}`"
                  :data-testid="`triage-issue-link-${f.finding_id}`"
                >✓ #{{ f.gh_issue_number || '?' }} →</a>
                <button
                  v-else
                  type="button"
                  class="btn-primary btn text-xs"
                  :disabled="filingFindingId === f.finding_id"
                  :data-testid="`triage-file-button-${f.finding_id}`"
                  @click="onFileFindingIssue(f)"
                >
                  {{ filingFindingId === f.finding_id ? 'Filing…' : 'File issue ↗' }}
                </button>
              </span>
            </div>
            <p
              v-if="filingFindingErrors[f.finding_id]"
              class="mt-2 text-xs text-red-400"
            >{{ filingFindingErrors[f.finding_id] }}</p>
          </article>
        </section>

        <!-- ⚠ MAJORS — compact rows, click to expand body -->
        <section
          v-if="triageSections.majors.length"
          class="mb-6"
          data-testid="triage-majors"
        >
          <header class="mb-2 flex items-baseline gap-2">
            <h3 class="text-base font-semibold text-amber-300">⚠ Major</h3>
            <span class="text-xs text-ink-600">
              {{ triageSections.majors.length }} · fix this sprint
            </span>
          </header>
          <div class="panel divide-y divide-ink-200">
            <div
              v-for="f in triageSections.majors"
              :key="f.finding_id"
              class="p-3"
              :data-testid="`triage-major-${f.finding_id}`"
            >
              <div class="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  class="flex min-w-0 flex-1 items-center gap-2 text-left"
                  @click="toggleTriageRow(f.finding_id)"
                >
                  <span class="text-xs text-ink-500">
                    {{ expandedTriageRows[f.finding_id] ? '▾' : '▸' }}
                  </span>
                  <span class="pill pill-major">major</span>
                  <RegressionBadge :finding="f" />
                  <span class="truncate font-medium">{{ f.title }}</span>
                </button>
                <span class="flex items-center gap-1 text-xs text-ink-600">
                  <Avatar
                    :seed="personaMeta[f.persona]?.avatar_seed || f.persona"
                    :color-token="personaMeta[f.persona]?.color_token || 'slate'"
                    size="xs"
                  />
                  {{ personaMeta[f.persona]?.display_name || f.persona }}
                </span>
                <span class="ml-2 flex items-center gap-2">
                  <button
                    type="button"
                    class="text-xs font-medium text-brand-700 transition hover:text-brand-800"
                    :data-testid="`triage-view-trace-${f.finding_id}`"
                    @click.stop="viewTraceForFinding(f)"
                  >
                    Trace →
                  </button>
                  <a
                    v-if="f.gh_issue_url"
                    :href="f.gh_issue_url"
                    target="_blank"
                    rel="noopener"
                    class="pill tint-emerald text-xs"
                    :title="`Filed as #${f.gh_issue_number || '?'}`"
                    :data-testid="`triage-issue-link-${f.finding_id}`"
                    @click.stop
                  >✓ #{{ f.gh_issue_number || '?' }}</a>
                  <button
                    v-else
                    type="button"
                    class="text-xs font-medium text-brand-700 transition hover:text-brand-800"
                    :disabled="filingFindingId === f.finding_id"
                    :data-testid="`triage-file-button-${f.finding_id}`"
                    @click.stop="onFileFindingIssue(f)"
                  >
                    {{ filingFindingId === f.finding_id ? 'Filing…' : 'File ↗' }}
                  </button>
                </span>
              </div>
              <div
                v-if="expandedTriageRows[f.finding_id]"
                class="mt-2 pl-6"
              >
                <p
                  v-if="f.body"
                  class="whitespace-pre-wrap text-sm text-ink-700"
                >{{ f.body }}</p>
                <p
                  v-if="filingFindingErrors[f.finding_id]"
                  class="mt-2 text-xs text-red-400"
                >{{ filingFindingErrors[f.finding_id] }}</p>
              </div>
            </div>
          </div>
        </section>

        <!-- Minor + nit actionable — collapsed by default. Operator can
             open if they want to clear backlog noise. -->
        <section
          v-if="triageSections.others.length"
          class="mb-6"
          data-testid="triage-others"
        >
          <button
            type="button"
            class="flex items-center gap-2 text-sm text-ink-700 hover:text-ink-900"
            @click="showOthers = !showOthers"
          >
            <span class="text-xs">{{ showOthers ? '▾' : '▸' }}</span>
            <h3 class="text-base font-semibold">Other actionable</h3>
            <span class="text-xs text-ink-500">
              {{ triageSections.others.length }} · minor + nit · backlog
            </span>
          </button>
          <div v-if="showOthers" class="panel mt-2 divide-y divide-ink-200">
            <div
              v-for="f in triageSections.others"
              :key="f.finding_id"
              class="flex flex-wrap items-center gap-2 p-2 text-sm"
              :data-testid="`triage-other-${f.finding_id}`"
            >
              <span class="pill" :class="`pill-${f.severity}`">{{ f.severity }}</span>
              <RegressionBadge :finding="f" />
              <span class="truncate">{{ f.title }}</span>
              <span class="ml-auto flex items-center gap-1 text-xs text-ink-500">
                <Avatar
                  :seed="personaMeta[f.persona]?.avatar_seed || f.persona"
                  :color-token="personaMeta[f.persona]?.color_token || 'slate'"
                  size="xs"
                />
                {{ personaMeta[f.persona]?.display_name || f.persona }}
              </span>
              <a
                v-if="f.gh_issue_url"
                :href="f.gh_issue_url"
                target="_blank"
                rel="noopener"
                class="pill tint-emerald text-xs"
                :title="`Filed as #${f.gh_issue_number || '?'}`"
              >✓ #{{ f.gh_issue_number || '?' }}</a>
              <button
                v-else
                type="button"
                class="text-xs font-medium text-brand-700 transition hover:text-brand-800"
                :disabled="filingFindingId === f.finding_id"
                @click="onFileFindingIssue(f)"
              >{{ filingFindingId === f.finding_id ? '…' : 'File ↗' }}</button>
            </div>
          </div>
        </section>

        <!-- Per persona — collapsible cards (#1172 / slice 4 of #1168).
             One card per persona that filed at least 1 actionable
             finding. Lazy-loads a 1-paragraph first-person synthesis
             from Haiku when the card is expanded. -->
        <section
          v-if="triagePerPersona.length"
          class="mb-6"
          data-testid="triage-per-persona"
        >
          <header class="mb-2 flex items-baseline gap-2">
            <h3 class="text-base font-semibold">Per persona</h3>
            <span class="text-xs text-ink-500">
              {{ triagePerPersona.length }} persona{{ triagePerPersona.length === 1 ? '' : 's' }} filed findings
            </span>
          </header>
          <article
            v-for="entry in triagePerPersona"
            :key="entry.personaId"
            class="panel mb-2 p-3"
            :data-testid="`per-persona-card-${entry.personaId}`"
          >
            <button
              type="button"
              class="flex w-full items-center gap-3 text-left"
              :data-testid="`per-persona-toggle-${entry.personaId}`"
              @click="togglePersonaCard(entry.personaId)"
            >
              <span class="text-xs text-ink-500">
                {{ expandedPersonaCards[entry.personaId] ? '▾' : '▸' }}
              </span>
              <Avatar
                :seed="personaMeta[entry.personaId]?.avatar_seed || entry.personaId"
                :color-token="personaMeta[entry.personaId]?.color_token || 'slate'"
                size="sm"
              />
              <span class="min-w-0 flex-1">
                <span class="font-semibold">{{
                  personaMeta[entry.personaId]?.display_name || entry.personaId
                }}</span>
                <span
                  v-if="personaMeta[entry.personaId]?.archetype"
                  class="ml-2 text-xs text-ink-600"
                >— {{ personaMeta[entry.personaId].archetype }}</span>
              </span>
              <span class="flex items-center gap-1 text-xs">
                <span
                  v-if="entry.counts.blocker"
                  class="pill pill-blocker"
                >{{ entry.counts.blocker }} blocker</span>
                <span
                  v-if="entry.counts.major"
                  class="pill pill-major"
                >{{ entry.counts.major }} major</span>
                <span
                  v-if="entry.counts.minor"
                  class="pill pill-minor"
                >{{ entry.counts.minor }} minor</span>
                <span
                  v-if="entry.counts.nit"
                  class="pill pill-nit"
                >{{ entry.counts.nit }} nit</span>
              </span>
            </button>

            <div
              v-if="expandedPersonaCards[entry.personaId]"
              class="mt-3 space-y-3 pl-9"
              :data-testid="`per-persona-body-${entry.personaId}`"
            >
              <!-- Top 3 findings — blocker + major take priority -->
              <div class="space-y-1.5">
                <p class="text-xs font-semibold text-ink-700">Top findings:</p>
                <div
                  v-for="f in entry.findings.slice(0, 3)"
                  :key="f.finding_id"
                  class="flex flex-wrap items-center gap-2 text-sm"
                  :data-testid="`per-persona-finding-${f.finding_id}`"
                >
                  <span class="pill" :class="`pill-${f.severity}`">{{ f.severity }}</span>
                  <RegressionBadge :finding="f" />
                  <span class="truncate">{{ f.title }}</span>
                  <span class="ml-auto flex items-center gap-2">
                    <button
                      type="button"
                      class="text-xs font-medium text-brand-700 transition hover:text-brand-800"
                      @click="viewTraceForFinding(f)"
                    >Trace →</button>
                    <a
                      v-if="f.gh_issue_url"
                      :href="f.gh_issue_url"
                      target="_blank"
                      rel="noopener"
                      class="pill tint-emerald text-xs"
                      :title="`Filed as #${f.gh_issue_number || '?'}`"
                    >✓ #{{ f.gh_issue_number || '?' }}</a>
                    <button
                      v-else
                      type="button"
                      class="text-xs font-medium text-brand-700 transition hover:text-brand-800"
                      :disabled="filingFindingId === f.finding_id"
                      @click="onFileFindingIssue(f)"
                    >{{ filingFindingId === f.finding_id ? '…' : 'File ↗' }}</button>
                  </span>
                </div>
                <p
                  v-if="entry.findings.length > 3"
                  class="text-xs text-ink-500"
                >
                  + {{ entry.findings.length - 3 }} more — see sections above.
                </p>
              </div>
            </div>
          </article>
        </section>

        <!-- #1822 §4 — Mandatory-coverage strip. The old Coverage tab
             collapsed into a compact strip on Triage (collapsed by
             default): same id + catalog-description list, none of the
             tab chrome. Hidden entirely when the run was triggered
             without mandatory coverage. -->
        <section
          v-if="mandatoryActions.length"
          class="mb-6"
          data-testid="triage-coverage-strip"
        >
          <button
            type="button"
            class="flex items-center gap-2 text-sm text-ink-700 hover:text-ink-900"
            @click="showCoverageStrip = !showCoverageStrip"
          >
            <span class="text-xs">{{ showCoverageStrip ? '▾' : '▸' }}</span>
            <h3 class="text-base font-semibold">
              Mandatory coverage ({{ mandatoryActions.length }})
            </h3>
          </button>
          <div v-if="showCoverageStrip" class="panel mt-2 p-4">
            <ul class="divide-y divide-ink-100">
              <li
                v-for="aid in mandatoryActions"
                :key="aid"
                class="grid grid-cols-[max-content_1fr] items-baseline gap-3 py-2"
              >
                <code class="rounded bg-amber-500/10 px-2 py-0.5 font-mono text-xs text-amber-300">
                  {{ aid }}
                </code>
                <span v-if="catalogById[aid]" class="text-sm text-ink-700">
                  {{ catalogById[aid].human_description }}
                </span>
                <span v-else class="text-sm text-ink-500">
                  (no longer in catalog)
                </span>
              </li>
            </ul>
          </div>
        </section>

        <!-- #1822 §4 — Discovered strip. The old Discovered tab condensed
             into a collapsed strip: counts in the header, the distilled
             actions / tools / unexplored branches inside, and a link out
             to the global coverage map for the cross-run view. -->
        <section
          v-if="discoveredActions.length || discoveredTools.length || discoveredBranches.length"
          class="mb-6"
          data-testid="triage-discovered-strip"
        >
          <button
            type="button"
            class="flex items-center gap-2 text-sm text-ink-700 hover:text-ink-900"
            @click="showDiscoveredStrip = !showDiscoveredStrip"
          >
            <span class="text-xs">{{ showDiscoveredStrip ? '▾' : '▸' }}</span>
            <h3 class="text-base font-semibold">
              Discovered this run —
              {{ discoveredActions.length }} actions ·
              {{ discoveredTools.length }} tool calls ·
              {{ discoveredBranches.length }} unexplored
            </h3>
          </button>
          <div v-if="showDiscoveredStrip" class="mt-2">
            <div class="grid gap-4 lg:grid-cols-[2fr_1fr]">
              <div v-if="discoveredActions.length">
                <div
                  v-for="a in discoveredActions"
                  :key="a.doc_id"
                  class="panel mb-2 p-3"
                >
                  <div class="flex flex-wrap items-center gap-2">
                    <span class="pill" :class="`tint-${categoryTint(a.category)}`">
                      {{ a.category }}
                    </span>
                    <code class="rounded bg-amber-500/10 px-1.5 py-0.5 font-mono text-xs text-amber-300">
                      {{ a.action_id }}
                    </code>
                    <span class="text-xs text-ink-500">· {{ a.persona_id }}</span>
                  </div>
                  <p class="mt-1.5 text-sm text-ink-800">{{ a.human_description }}</p>
                </div>
              </div>
              <div class="space-y-4">
                <div v-if="discoveredTools.length">
                  <h4 class="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-500">
                    Tools used
                  </h4>
                  <div
                    v-for="t in discoveredTools"
                    :key="t.doc_id"
                    class="panel mb-1.5 p-2 text-xs"
                  >
                    <code class="font-mono text-[10px]">{{ t.name }}</code>
                    <p class="mt-0.5 text-ink-600">{{ t.purpose }}</p>
                  </div>
                </div>
                <div v-if="discoveredBranches.length">
                  <h4 class="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-500">
                    Unexplored branches
                  </h4>
                  <div
                    v-for="b in discoveredBranches"
                    :key="b.doc_id"
                    class="panel mb-1.5 p-2 text-xs text-ink-700"
                  >
                    {{ b.description }}
                  </div>
                </div>
              </div>
            </div>
            <p class="mt-2 text-xs">
              <router-link
                to="/discovered"
                class="font-medium text-brand-700 hover:text-brand-800"
              >Full coverage map →</router-link>
            </p>
          </div>
        </section>

        <p class="mt-6 text-center text-xs text-ink-500">
          Looking for praise, observations, or the full chip view?
          <button
            type="button"
            class="underline hover:text-ink-700"
            @click="activeTab = 'findings'"
          >Findings tab →</button>
        </p>
      </div>

      <!-- Timeline tab -->
      <div v-else-if="activeTab === 'timeline'">
        <!--
          #1050 — Slice B of #1048. Sticky findings summary at the top
          of the timeline. Bubbles the persona's note_finding calls
          (today scattered in the middle of 80 rows) into a pinned
          card; each chip jumps to its step in the timeline. Hidden
          when the run has no findings yet.
        -->
        <div
          v-if="panelFindings.length"
          class="findings-panel panel mb-3 p-3"
        >
          <div class="mb-2 flex items-center gap-2">
            <span class="text-base">🎯</span>
            <span class="font-semibold">Findings ({{ panelFindings.length }})</span>
            <span class="text-xs text-ink-600">{{ findingCountSummary }}</span>
            <span class="ml-auto text-xs text-ink-500">
              Click any to jump to the step that filed it
            </span>
          </div>
          <div class="flex max-h-32 flex-wrap gap-1.5 overflow-y-auto">
            <!-- The chip is now a flex container with two clickable
                 zones: the body (jumps to the filing step) and a
                 tail-end file/link affordance. Operators reported
                 in 2026-05-28 that the file-issue button was only
                 reachable from the detailed findings section at the
                 bottom of the page — too far from the chips at the
                 top. Surfacing it inline cuts the path from "see
                 the bug" to "filed in GitHub" to one click. -->
            <div
              v-for="f in panelFindings"
              :key="f.finding_id"
              class="flex max-w-md items-center gap-1 rounded-full bg-ink-100 pl-2 pr-1 py-0.5 text-xs"
              :class="{ 'opacity-50': f.status === 'dismissed' }"
              :data-testid="`finding-chip-${f.finding_id}`"
            >
              <button
                type="button"
                class="flex min-w-0 items-center gap-1.5 rounded-full py-0.5 pr-1 transition hover:bg-ink-200"
                :title="findingTitle(f)"
                @click="jumpToFinding(f)"
              >
                <span class="pill" :class="`pill-${f.severity}`">{{ f.severity }}</span>
                <!-- Slice 2.2 of #1106 — regression badge. A finding flagged
                     is_regression=true is a previously-fixed bug that came
                     back; the magenta pill matches the memory pill colour
                     so the operator's eye links the cockpit + finding views.
                     #1171 (slice 3 of #1168) — label is now STILL BROKEN
                     so the regression cue matches the triage-view badge.
                     Kept as a non-clickable span here because the chip
                     itself is a <button> (jumpToFinding); nesting a
                     router-link inside would be invalid HTML. The
                     clickable RegressionBadge variant lives on the
                     Triage tab where rows aren't wrapped in a button. -->
                <span
                  v-if="f.is_regression"
                  class="rounded-full bg-fuchsia-500/10 px-1.5 py-0.5 text-[10px] font-semibold text-fuchsia-300 ring-1 ring-fuchsia-500/30"
                  :data-testid="`finding-regression-badge-${f.finding_id}`"
                  :title="
                    f.last_verified_run_id
                      ? `Regression — previously fixed in run ${f.last_verified_run_id}, now broken again.`
                      : 'Regression — previously fixed, now broken again.'
                  "
                >⚠ STILL BROKEN</span>
                <!-- Recurring-count badge — non-loud, just a number on
                     findings that have been seen multiple times across
                     runs. Helps operators prioritise. -->
                <span
                  v-if="(f.recurring_count || 1) > 1"
                  class="rounded-full bg-rose-500/10 px-1.5 py-0.5 text-[10px] font-semibold text-rose-300 ring-1 ring-rose-500/30"
                  :data-testid="`finding-recurring-badge-${f.finding_id}`"
                  :title="`Seen ${f.recurring_count} times across runs`"
                >↻ ×{{ f.recurring_count }}</span>
                <span class="truncate">{{ f.title }}</span>
              </button>
              <!-- File-issue affordance — three states:
                     1. Already filed → green pill linking to issue.
                     2. Currently filing → disabled spinner.
                     3. Idle → small octocat button. -->
              <a
                v-if="f.gh_issue_url"
                :href="f.gh_issue_url"
                target="_blank"
                rel="noopener"
                class="rounded-full bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-300 hover:bg-emerald-500/20"
                :title="`Filed as #${f.gh_issue_number || '?'}`"
                :data-testid="`finding-chip-issue-link-${f.finding_id}`"
                @click.stop
              >✓ #{{ f.gh_issue_number || '?' }}</a>
              <button
                v-else
                type="button"
                class="rounded-full bg-panel px-1.5 py-0.5 text-[10px] font-medium text-ink-700 ring-1 ring-ink-300 hover:bg-ink-100 disabled:opacity-40"
                :disabled="filingFindingId === f.finding_id"
                :title="filingFindingId === f.finding_id ? 'Filing…' : 'File as GitHub issue'"
                :data-testid="`finding-chip-file-button-${f.finding_id}`"
                @click.stop="onFileFindingIssue(f)"
              >{{ filingFindingId === f.finding_id ? '…' : 'File ↗' }}</button>
            </div>
          </div>
        </div>
        <!--
          #1170 (slice 2 of #1168) — Wins panel. Praise + observation
          findings used to live in the main Findings chip panel above,
          where they wore severity pills (a praise-nit looks pixel-
          identical to a bug-nit) and inflated the panel header count.
          They're still worth surfacing — the personas wrote them and
          the operator wants to see what's working — but they belong
          out of the fix-list. Collapsed by default so the eye lands on
          actionable findings first; one click expands the catalog.
        -->
        <div
          v-if="winsFindings.length"
          class="wins-panel panel mb-3 p-3"
          data-testid="wins-panel"
        >
          <button
            type="button"
            class="flex w-full items-center gap-2 text-left"
            data-testid="wins-panel-toggle"
            @click="showWins = !showWins"
          >
            <span class="text-xs text-ink-500">{{ showWins ? '▾' : '▸' }}</span>
            <span class="text-base">🌟</span>
            <span class="font-semibold">Wins ({{ winsFindings.length }})</span>
            <span class="text-xs text-ink-600">
              praise + observations · no fixes to file
            </span>
          </button>
          <div
            v-if="showWins"
            class="mt-2 flex max-h-32 flex-wrap gap-1.5 overflow-y-auto"
          >
            <button
              v-for="f in winsFindings"
              :key="f.finding_id"
              type="button"
              class="flex max-w-md items-center gap-1.5 rounded-full bg-emerald-500/10 px-2 py-1 text-xs transition hover:bg-emerald-500/15"
              :title="findingTitle(f)"
              :data-testid="`wins-chip-${f.finding_id}`"
              @click="jumpToFinding(f)"
            >
              <span class="rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-300">
                {{ _kindOf(f) === 'praise' ? '✓ praise' : '◦ note' }}
              </span>
              <span class="truncate">{{ f.title }}</span>
            </button>
          </div>
        </div>
        <!--
          #1078 Slice 2 — the horizontal phase ribbon (was #1053 / Slice
          E of #1048) is removed. URL phases now live in the vertical
          spine on the LEFT of the timeline grid below — same data, same
          IntersectionObserver-driven highlight, but the spine reads
          top-to-bottom and lets the operator anchor every event to the
          page it happened on.
        -->
        <div class="mb-3 flex flex-wrap items-center gap-3 text-xs text-ink-600">
          <label class="flex items-center gap-1.5">
            Filter:
            <select v-model="timelinePersona" class="select w-auto">
              <option value="">all personas</option>
              <option v-for="pid in run.personas || []" :key="pid" :value="pid">
                {{ personaMeta[pid]?.display_name || pid }}
              </option>
            </select>
          </label>
          <!--
            #1051 — Slice C of #1048. Filter chips replace the old
            log/step binary checkboxes. Four categories let the
            operator peel back layers from the 80-step transcript:
              🎯 Findings    — steps that filed a note_finding
              🧠 Narration   — steps with persona reasoning text
                              + standalone log entries (the dedup pass
                              from Slice A already strips paired logs)
              📸 Screenshots — steps with a screenshot_id
              🔧 Tools       — pure tool calls (no narration, no
                              finding, no screenshot — the noise floor)

            Default state: Findings + Narration ON, Screenshots +
            Tools OFF. Tuned to surface ~30% of events that carry
            actual review value. The operator toggles screenshots on
            when they want to walk the visual story, tools on when
            debugging the harness itself.
          -->
          <div class="flex items-center gap-1.5" data-testid="timeline-filters">
            <button
              v-for="chip in FILTER_CHIPS"
              :key="chip.id"
              type="button"
              class="flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium transition"
              :class="
                activeFilters[chip.id]
                  ? 'bg-brand-100 text-brand-900 ring-1 ring-brand-300'
                  : 'bg-ink-50 text-ink-500 hover:bg-ink-100'
              "
              :title="chip.title"
              @click="toggleFilter(chip.id)"
            >
              <span aria-hidden="true">{{ chip.icon }}</span>
              <span>{{ chip.label }}</span>
              <span class="text-ink-500">{{ chipCounts[chip.id] }}</span>
            </button>
          </div>
          <!-- #1822 §6 — the global /transcripts page is gone; narration
               search lives here now. Case-insensitive substring match
               against each event's content / summary / text_from_persona,
               applied on top of the persona + chip filters. The "N of M"
               summary doubles as the match count. -->
          <input
            v-model="timelineSearch"
            type="search"
            class="input w-48"
            placeholder="Search narration…"
            data-testid="timeline-search"
          />
          <span class="text-xs text-ink-500">
            {{ filteredEvents.length }} of {{ baselineEvents.length }}
          </span>
          <button class="btn-ghost btn ml-auto" @click="refreshTimeline" :disabled="timelineLoading">
            {{ timelineLoading ? 'Refreshing…' : 'Refresh' }}
          </button>
        </div>

        <div v-if="!filteredEvents.length" class="panel panel-pad text-sm text-ink-600">
          No timeline events yet — either the run hasn't started recording, or the
          filters are too narrow.
        </div>

        <!--
          #1078 Slice 2 — URL spine layout. The vertical spine on the
          LEFT replaces both the horizontal phase ribbon (was at the top)
          AND the right-rail screenshot sidebar (was on the right, from
          Slice D / #1052). One axis of navigation, anchored to the URL
          the persona was on. Each spine row carries finding +
          screenshot counts for that page; the active row matches the
          IntersectionObserver-tracked scroll position. Mobile / narrow
          viewports collapse the spine above the stream.

          Screenshots that used to live in the right rail now render
          inline at their owning step (the existing screenshot_id
          rendering on step rows below — no separate sidebar).
        -->
        <div class="timeline-grid">
          <aside
            v-if="urlSections.length"
            class="url-spine"
            data-testid="url-spine"
            role="navigation"
            aria-label="URLs visited during this run"
          >
            <div class="url-spine-sticky">
              <h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-500">
                URLs visited
              </h3>
              <ol class="space-y-0.5">
                <li v-for="(p, idx) in urlSections" :key="p.id">
                  <button
                    type="button"
                    class="group flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs transition"
                    :class="
                      idx === currentPhaseIdx
                        ? 'bg-brand-600 text-ink-50'
                        : 'text-ink-700 hover:bg-ink-100'
                    "
                    :title="`${p.href}  ·  ${p.endIdx - p.startIdx + 1} events  ·  ${p.fixableCount} to fix · ${p.praiseCount} wins · ${p.observationCount} observations · ${p.screenshotCount} screenshot${p.screenshotCount === 1 ? '' : 's'}`"
                    @click="scrollToStep(p.startIdx)"
                  >
                    <span class="truncate font-medium">{{ phaseLabel(p) }}</span>
                    <span class="ml-auto flex shrink-0 items-center gap-1">
                      <!-- #1115 — bucket badges replace the single rose
                           dot. Fixables → red, praise → green, observations
                           → slate. Each only renders when non-zero, so a
                           clean page shows no pip. -->
                      <span
                        v-if="p.fixableCount"
                        class="inline-flex h-3.5 min-w-[14px] items-center justify-center rounded-full bg-rose-500 px-1 text-[9px] font-bold leading-none text-ink-50"
                        :title="`${p.fixableCount} to fix on this page`"
                      >
                        🐞{{ p.fixableCount }}
                      </span>
                      <span
                        v-if="p.praiseCount"
                        class="inline-flex h-3.5 min-w-[14px] items-center justify-center rounded-full bg-emerald-500 px-1 text-[9px] font-bold leading-none text-ink-50"
                        :title="`${p.praiseCount} win${p.praiseCount === 1 ? '' : 's'} on this page`"
                      >
                        ✓{{ p.praiseCount }}
                      </span>
                      <span
                        v-if="p.observationCount"
                        class="inline-flex h-3.5 min-w-[14px] items-center justify-center rounded-full bg-slate-400 px-1 text-[9px] font-bold leading-none text-ink-50"
                        :title="`${p.observationCount} observation${p.observationCount === 1 ? '' : 's'} on this page`"
                      >
                        🔎{{ p.observationCount }}
                      </span>
                      <span
                        v-if="p.screenshotCount"
                        :class="
                          idx === currentPhaseIdx
                            ? 'text-ink-50/80'
                            : 'text-ink-400'
                        "
                        class="text-[9px]"
                        :title="`${p.screenshotCount} screenshot${p.screenshotCount === 1 ? '' : 's'} on this page`"
                      >
                        📷{{ p.screenshotCount }}
                      </span>
                    </span>
                  </button>
                </li>
              </ol>
            </div>
          </aside>

          <ol class="timeline-stream relative space-y-0">
            <template v-for="(ev, i) in displayEvents" :key="ev._key || i">
              <!-- URL section header — emitted just before the first
                   event of each spine section. Acts as a sticky divider
                   anchoring the events below to a specific page. -->
              <li
                v-if="ev._urlSectionStart"
                :id="`url-section-${ev._urlSectionStart.id}`"
                class="url-section-header"
                role="separator"
                :data-testid="`url-section-${ev._urlSectionStart.id}`"
              >
                <div class="flex items-center gap-2 py-2 text-xs font-medium text-ink-600">
                  <span class="text-ink-400">↳ on</span>
                  <span class="rounded bg-ink-100 px-1.5 py-0.5 font-mono text-ink-700">
                    {{ phaseLabel(ev._urlSectionStart) }}
                  </span>
                  <!-- #1115 — three badges replace the misleading single
                       "X findings" count. The rose chip is fixables only;
                       praise + observations get their own chips so an
                       operator can see "🐞 3 to fix on this page, ✓ 2
                       wins" without opening every row. -->
                  <span
                    v-if="ev._urlSectionStart.fixableCount"
                    class="rounded-full bg-rose-500/10 px-1.5 py-0.5 text-[10px] font-medium text-rose-300"
                    :title="`${ev._urlSectionStart.fixableCount} to fix on this page`"
                  >
                    🐞 {{ ev._urlSectionStart.fixableCount }} to fix
                  </span>
                  <span
                    v-if="ev._urlSectionStart.praiseCount"
                    class="rounded-full bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-medium text-emerald-300"
                    :title="`${ev._urlSectionStart.praiseCount} win${ev._urlSectionStart.praiseCount === 1 ? '' : 's'} on this page`"
                  >
                    ✓ {{ ev._urlSectionStart.praiseCount }} {{ ev._urlSectionStart.praiseCount === 1 ? 'win' : 'wins' }}
                  </span>
                  <span
                    v-if="ev._urlSectionStart.observationCount"
                    class="rounded-full bg-slate-500/10 px-1.5 py-0.5 text-[10px] font-medium text-slate-300"
                    :title="`${ev._urlSectionStart.observationCount} observation${ev._urlSectionStart.observationCount === 1 ? '' : 's'} on this page`"
                  >
                    🔎 {{ ev._urlSectionStart.observationCount }}
                  </span>
                  <span
                    v-if="ev._urlSectionStart.screenshotCount"
                    class="text-[10px] text-ink-500"
                  >
                    · {{ ev._urlSectionStart.screenshotCount }} 📷
                  </span>
                  <span class="ml-auto truncate text-[10px] text-ink-400" :title="ev._urlSectionStart.href">
                    {{ ev._urlSectionStart.href }}
                  </span>
                </div>
              </li>
            <li
              :ref="(el) => registerStepRefWithIdx(ev, el, i)"
              :data-event-idx="i"
              class="group relative flex gap-4 py-3 animate-fade-in"
              :class="ev.kind === 'step' ? 'is-step' : 'is-log'"
            >
            <!--
              Persona avatar gutter + connecting rail. Pre-PR-C the avatar
              repeated for every consecutive row by the same persona; now we
              hide the avatar (but keep the rail) when the previous row was
              the same persona, Slack-style. The visible-avatar row anchors a
              new persona stretch.
            -->
            <div class="flex flex-col items-center">
              <Avatar
                v-if="ev._showAvatar"
                :seed="personaMeta[ev.persona_id]?.avatar_seed || ev.persona_id"
                :color-token="personaMeta[ev.persona_id]?.color_token || 'slate'"
                size="sm"
              />
              <div v-else class="h-8 w-8 rounded-full"></div>
              <div
                v-if="i !== displayEvents.length - 1"
                class="mt-1 w-px flex-1 bg-ink-200"
              ></div>
            </div>

            <!-- Event card -->
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2 text-xs">
                <span v-if="ev._showAvatar" class="font-medium text-ink-900">
                  {{ personaMeta[ev.persona_id]?.display_name || ev.persona_id }}
                </span>
                <!--
                  Step + log get visually distinct treatment now:
                  - step pills carry the brand teal — the explicit action moment
                  - log pills stay neutral grey — quieter narrative chatter
                -->
                <span v-if="ev.kind === 'step'" class="pill bg-brand-100 text-brand-800">
                  step #{{ ev.step_n }} · {{ ev.tool || ev.action || 'action' }}
                </span>
                <span v-else class="pill">
                  {{ ev.kind_label || ev.kind || 'log' }}
                </span>
                <span class="ml-auto text-ink-500">{{ formatTimestamp(ev.ts) }}</span>
              </div>

              <!-- Step body: action description + optional snapshot. Empty
                   step rows (no summary/text/url/screenshot — sometimes the
                   harness logs a step marker without a payload) get nothing
                   below the header, so they sit as a slim row instead of an
                   empty card.

                   #1078 Slice 3: when the step carries a screenshot, the
                   image IS the content — suppress the redundant
                   "Captured screenshot" prose so the panel doesn't read
                   like a caption above a picture. text_from_persona +
                   url still render (those add real context that the
                   image alone doesn't carry). -->
              <div
                v-if="ev.kind === 'step' && (ev.summary || ev.text_from_persona || ev.url || ev.screenshot_id)"
                class="mt-1.5 panel p-3"
              >
                <div
                  v-if="ev.summary && !_isImageRedundantSummary(ev)"
                  class="text-sm text-ink-800"
                >{{ ev.summary }}</div>
                <div
                  v-else-if="ev.text_from_persona"
                  class="text-sm italic text-ink-700"
                >
                  "{{ ev.text_from_persona }}"
                </div>
                <div v-if="ev.url" class="mt-1 truncate font-mono text-xs text-ink-600">
                  {{ ev.url }}
                </div>
                <figure
                  v-if="ev.screenshot_id"
                  class="mt-2"
                  data-testid="inline-screenshot"
                >
                  <button
                    type="button"
                    class="group block w-full max-w-md overflow-hidden rounded border border-ink-200 transition hover:border-brand-400"
                    :title="`Open screenshot at step #${ev.step_n} full size`"
                    @click="lightboxSrc = screenshotUrl(run.run_id, ev.screenshot_id)"
                  >
                    <img
                      :src="screenshotUrl(run.run_id, ev.screenshot_id)"
                      :alt="`Screenshot captured at step ${ev.step_n}`"
                      loading="lazy"
                      class="block w-full transition group-hover:scale-[1.01]"
                    />
                  </button>
                  <figcaption class="mt-1 text-[10px] text-ink-500">
                    📷 captured at step #{{ ev.step_n }} · click to enlarge
                  </figcaption>
                </figure>
              </div>

              <!-- Log body: narrative content. -->
              <div v-else-if="ev.kind !== 'step'" class="mt-1.5 panel bg-ink-50 p-3">
                <p class="whitespace-pre-wrap text-sm text-ink-700">{{ ev.content }}</p>
              </div>
            </div>
          </li>
          </template>
        </ol>
        </div>
      </div>

      <!-- Review tab (#1822 — singular label; tab id stays 'reviews') -->
      <div v-else-if="activeTab === 'reviews'" class="space-y-4">
        <div v-for="review in run.reviews" :key="review.persona" class="panel panel-pad">
          <div class="mb-2 flex items-center gap-3">
            <Avatar
              :seed="personaMeta[review.persona]?.avatar_seed || review.persona"
              :color-token="personaMeta[review.persona]?.color_token || 'slate'"
              size="sm"
            />
            <div class="flex-1">
              <h2 class="!text-base">
                {{ personaMeta[review.persona]?.display_name || review.persona }}
              </h2>
              <p v-if="review.verdict" class="text-xs text-ink-600">— {{ review.verdict }}</p>
            </div>
          </div>
          <!-- eslint-disable-next-line vue/no-v-html -->
          <div class="prose-qa" v-html="renderMarkdown(review.review_markdown)"></div>
        </div>
      </div>

      <!-- Findings tab — #1115 restructure.
           Split into three sections (🐞 Fix / ✓ Working well / 🔎 Observations)
           so positives don't get buried alongside fixables, and per-finding
           [File issue] surfaces inline on each fixable. The severity +
           category filters apply across all three sections; legacy
           findings without a ``kind`` fall into 🐞 Fix (the qa-store
           default), preserving pre-#1115 visibility. -->
      <div v-else-if="activeTab === 'findings'">
        <div class="mb-3 flex flex-wrap items-center gap-3 text-xs text-ink-600">
          <label class="flex items-center gap-1.5">
            Severity:
            <select v-model="severityFilter" class="select w-auto">
              <option value="">all</option>
              <option v-for="s in SEVERITIES" :key="s" :value="s">{{ s }}</option>
            </select>
          </label>
          <label class="flex items-center gap-1.5">
            Category:
            <select v-model="categoryFilter" class="select w-auto">
              <option value="">all</option>
              <option v-for="c in CATEGORIES" :key="c" :value="c">{{ c }}</option>
            </select>
          </label>
          <span class="text-ink-500">
            {{ fixableFindings.length }} to fix ·
            {{ praiseFindings.length }} wins ·
            {{ observationFindings.length }} observations
          </span>
        </div>

        <!-- 🐞 Fix list -->
        <section class="mb-4">
          <header class="mb-2 flex items-baseline gap-2">
            <h3 class="text-base font-semibold">🐞 Fix these</h3>
            <span class="text-xs text-ink-500">{{ fixableFindings.length }}</span>
          </header>
          <div v-if="!fixableFindings.length" class="panel panel-pad text-sm text-ink-500">
            Nothing flagged for fix in this run. (Or your filters hide it.)
          </div>
          <div
            v-for="f in fixableFindings"
            :key="f.finding_id"
            class="panel mb-2 p-4"
            :class="{ 'opacity-60': f.status === 'dismissed' }"
          >
            <div class="flex flex-wrap items-center gap-2">
              <span class="pill" :class="`tint-${KIND_META[_kindOf(f)].tint}`">
                {{ KIND_META[_kindOf(f)].icon }} {{ KIND_META[_kindOf(f)].label }}
              </span>
              <span class="pill" :class="`pill-${f.severity}`">{{ f.severity }}</span>
              <span class="text-xs text-ink-600">{{ f.category }}</span>
              <span class="font-medium">{{ f.title }}</span>
              <span class="flex items-center gap-1 text-xs text-ink-600">
                · <Avatar
                  :seed="personaMeta[f.persona]?.avatar_seed || f.persona"
                  :color-token="personaMeta[f.persona]?.color_token || 'slate'"
                  size="xs"
                />
                {{ personaMeta[f.persona]?.display_name || f.persona }}
              </span>
              <span class="ml-auto flex items-center gap-2">
                <select
                  :value="f.status"
                  class="select w-auto text-xs"
                  @change="onStatusChange(f, $event.target.value)"
                >
                  <option v-for="s in STATUSES" :key="s" :value="s">{{ s }}</option>
                </select>
                <!-- #1115 — per-finding file-as-issue. Mirrors the
                     Insights detail drawer button. Once filed, the
                     button becomes a link to the issue (with the
                     issue number); we don't expose a re-file path
                     since the API 409s anyway. -->
                <a
                  v-if="f.gh_issue_url"
                  :href="f.gh_issue_url"
                  target="_blank"
                  rel="noopener"
                  class="pill tint-emerald text-xs"
                  :title="`Filed as #${f.gh_issue_number || '?'}`"
                >
                  ✓ #{{ f.gh_issue_number || '?' }} →
                </a>
                <button
                  v-else
                  type="button"
                  class="text-xs font-medium text-brand-700 transition hover:text-brand-800"
                  :disabled="filingFindingId === f.finding_id"
                  data-testid="finding-file-issue-button"
                  @click="onFileFindingIssue(f)"
                >
                  {{ filingFindingId === f.finding_id ? 'Filing…' : 'File issue' }}
                </button>
              </span>
            </div>
            <p v-if="f.body" class="mt-2 whitespace-pre-wrap text-sm text-ink-700">{{ f.body }}</p>
            <p
              v-if="filingFindingErrors[f.finding_id]"
              class="mt-2 text-xs text-red-400"
            >
              {{ filingFindingErrors[f.finding_id] }}
            </p>
          </div>
        </section>

        <!-- ✓ Working well -->
        <section class="mb-4">
          <header class="mb-2 flex items-baseline gap-2">
            <h3 class="text-base font-semibold">✓ Working well</h3>
            <span class="text-xs text-ink-500">{{ praiseFindings.length }}</span>
            <span class="text-xs text-ink-400">
              — things the personas called out as good. No issue filing here.
            </span>
          </header>
          <div v-if="!praiseFindings.length" class="panel panel-pad text-sm text-ink-500">
            No praise from this run.
          </div>
          <div
            v-for="f in praiseFindings"
            :key="f.finding_id"
            class="panel mb-2 p-4 border-l-2 border-l-emerald-500/60"
          >
            <div class="flex flex-wrap items-center gap-2">
              <span class="pill tint-emerald">✓ Praise</span>
              <span class="font-medium">{{ f.title }}</span>
              <span class="flex items-center gap-1 text-xs text-ink-600">
                · <Avatar
                  :seed="personaMeta[f.persona]?.avatar_seed || f.persona"
                  :color-token="personaMeta[f.persona]?.color_token || 'slate'"
                  size="xs"
                />
                {{ personaMeta[f.persona]?.display_name || f.persona }}
              </span>
            </div>
            <p v-if="f.body" class="mt-2 whitespace-pre-wrap text-sm text-ink-700">{{ f.body }}</p>
          </div>
        </section>

        <!-- 🔎 Observations -->
        <section>
          <header class="mb-2 flex items-baseline gap-2">
            <h3 class="text-base font-semibold">🔎 Observations</h3>
            <span class="text-xs text-ink-500">{{ observationFindings.length }}</span>
            <span class="text-xs text-ink-400">
              — neutral context the persona noted; not bugs.
            </span>
          </header>
          <div v-if="!observationFindings.length" class="panel panel-pad text-sm text-ink-500">
            No observations from this run.
          </div>
          <div
            v-for="f in observationFindings"
            :key="f.finding_id"
            class="panel mb-2 p-4 border-l-2 border-l-slate-500/40"
          >
            <div class="flex flex-wrap items-center gap-2">
              <span class="pill tint-slate">🔎 Observation</span>
              <span class="font-medium">{{ f.title }}</span>
              <span class="flex items-center gap-1 text-xs text-ink-600">
                · <Avatar
                  :seed="personaMeta[f.persona]?.avatar_seed || f.persona"
                  :color-token="personaMeta[f.persona]?.color_token || 'slate'"
                  size="xs"
                />
                {{ personaMeta[f.persona]?.display_name || f.persona }}
              </span>
            </div>
            <p v-if="f.body" class="mt-2 whitespace-pre-wrap text-sm text-ink-700">{{ f.body }}</p>
          </div>
        </section>
      </div>

      <!-- #1822 §4 — the Coverage and Discovered tabs are gone; their
           content lives in the two collapsible strips on the Triage tab
           above, and the cross-run view at /discovered. -->
    </template>

    <!-- Lightbox for full-size snapshots. -->
    <Teleport to="body">
      <div
        v-if="lightboxSrc"
        class="fixed inset-0 z-30 flex items-center justify-center bg-black/80 p-6"
        @click.self="lightboxSrc = ''"
      >
        <img :src="lightboxSrc" class="max-h-full max-w-full rounded shadow-2xl" />
        <button
          class="btn absolute right-4 top-4 rounded-full"
          @click="lightboxSrc = ''"
        >
          Close ✕
        </button>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { marked } from 'marked'
import {
  fileFindingIssue,
  fileIssue,
  getActiveRun,
  getCoverageCatalog,
  getRun,
  getRunTimeline,
  listDiscoveredActions,
  listDiscoveredBranches,
  listDiscoveredTools,
  listMCPServers,
  listPersonas,
  screenshotUrl,
  setFindingStatus,
} from '../api.js'
import { formatDate, formatTimestamp } from '../format.js'
import Avatar from '../components/Avatar.vue'
import RegressionBadge from '../components/RegressionBadge.vue'

const props = defineProps({ runId: { type: String, required: true } })
const router = useRouter()

const SEVERITIES = ['blocker', 'major', 'minor', 'nit']
const CATEGORIES = ['bug', 'confusion', 'copy', 'missing-feature', 'worry', 'surprise']
const STATUSES = ['open', 'included', 'dismissed']

// #1115 — orthogonal sentiment axis. Three "fix me" kinds, two "no fix
// needed". Matches harness/qa_agents/tools/findings.py KINDS and the
// qa-store finding doc's ``kind`` field. The Findings tab splits on this:
// fixable → 🐞 Fix list (file-as-issue), praise → ✓ Working well,
// observation → 🔎 Observations. Legacy findings without ``kind`` default
// to 'bug' (qa-store schema), so old runs keep rendering in the Fix list.
const FIXABLE_KINDS = new Set(['bug', 'gap', 'risk', 'nit'])
const KIND_META = {
  // #1822 — tints renamed onto the dark `.tint-*` palette in style.css
  // (`tint-red` / `tint-green` were never defined there; rose/emerald
  // are the console equivalents).
  bug: { label: 'Bug', icon: '🐞', tint: 'rose' },
  gap: { label: 'Gap', icon: '🧩', tint: 'amber' },
  risk: { label: 'Risk', icon: '⚠', tint: 'rose' },
  nit: { label: 'Nit', icon: '✏', tint: 'slate' },
  praise: { label: 'Praise', icon: '✓', tint: 'emerald' },
  observation: { label: 'Observation', icon: '🔎', tint: 'slate' },
}
const SEVERITY_RANK = { blocker: 0, major: 1, minor: 2, nit: 3 }

const run = ref(null)
const loading = ref(true)
const error = ref('')
const filing = ref(false)
const fileError = ref('')
const severityFilter = ref('')
const categoryFilter = ref('')

const personaMeta = ref({}) // persona_id → row from /api/personas
// #1030 — MCP catalog fetched once on mount; used to swap raw server
// ids for human-friendly display names on the Slice A chip list. Empty
// when the catalog hasn't loaded yet (or 404'd, the same defensive
// fall-through the harness package handles).
const mcpCatalog = ref([])
const lightboxSrc = ref('')

function mcpChipLabel(serverId) {
  const entry = mcpCatalog.value.find((s) => s.id === serverId)
  return entry ? entry.display_name : serverId
}

function mcpChipTitle(m) {
  const entry = mcpCatalog.value.find((s) => s.id === m.server)
  const calls = `${m.calls} call${m.calls === 1 ? '' : 's'} to mcp__${m.server}__*`
  return entry ? `${entry.description}\n\n${calls}` : calls
}

// #1169 (slice 1 of #1168) — Triage is the new default landing tab. The
// run-detail page used to default to Timeline, which is a debugger view;
// the operator's first question is "what do I fix today", not "what did
// each persona do at step #743". The Triage tab presents the same
// fixable-findings set as the Findings tab, but ranked: blockers as
// full-width cards, majors as a compact list, the rest collapsed.
const activeTab = ref('triage')
// #1822 §4 — 6 tabs → 4. Coverage + Discovered collapsed into compact
// strips on the Triage tab; "Reviews" renamed to "Review" (the tab id
// stays 'reviews' so deep links / state survive).
const TABS = computed(() => [
  {
    id: 'triage',
    label: 'Triage',
    count:
      triageSections.value.blockers.length + triageSections.value.majors.length,
  },
  { id: 'timeline', label: 'Timeline', count: filteredEvents.value.length },
  { id: 'findings', label: 'Findings', count: run.value?.findings?.length || 0 },
  { id: 'reviews', label: 'Review', count: (run.value?.reviews || []).length },
])

// #1822 §4 — collapsed-by-default strips on the Triage tab that absorbed
// the deleted Coverage / Discovered tabs.
const showCoverageStrip = ref(false)
const showDiscoveredStrip = ref(false)

// -- Discovered (#1002) -----------------------------------------------------
const discoveredActions = ref([])
const discoveredTools = ref([])
const discoveredBranches = ref([])

// Same category→tint map as Discovered.vue. Duplicated for now — when a
// third surface needs it we'll promote it into a shared util.
const _CAT_TINT = {
  auth: 'indigo', billing: 'emerald', agents: 'fuchsia',
  playground: 'sky', account: 'violet', contact: 'amber',
  docs: 'cyan', admin: 'rose', other: 'slate',
}
function categoryTint(cat) {
  return _CAT_TINT[cat] || 'slate'
}

// -- Timeline ---------------------------------------------------------------
const timelineEvents = ref([])
const timelineLoading = ref(false)
const timelinePersona = ref('')
// #1822 §6 — narration search (absorbs the deleted global /transcripts
// page). Case-insensitive substring filter over each event's content /
// summary / text_from_persona, applied after the persona + chip filters.
const timelineSearch = ref('')
// #1051 — Slice C. Filter chips. Defaults tuned for "scan a run in
// 30 seconds": surface findings + persona narration, hide pure tool
// calls and bulk screenshots. Operators can toggle them back on for
// deeper debugging.
const FILTER_CHIPS = [
  {
    id: 'findings',
    icon: '🎯',
    label: 'Findings',
    title: 'Show steps that filed a finding (note_finding calls).',
  },
  {
    id: 'narration',
    icon: '🧠',
    label: 'Narration',
    title: "Show steps with persona reasoning + standalone log lines.",
  },
  {
    id: 'screenshots',
    icon: '📸',
    label: 'Screenshots',
    title: 'Show steps that captured a screenshot.',
  },
  {
    id: 'tools',
    icon: '🔧',
    label: 'Tools',
    title: 'Show pure tool calls (no narration, no finding, no screenshot).',
  },
]
// Plain reactive object — not `ref({})`. The chip click handler
// assigns to a nested property, and Vue's auto-unwrap for refs only
// covers READS in templates; LHS assignments through ref() didn't
// trigger reactivity in this setup. `reactive(...)` is the right
// primitive for "an object whose properties I'll be toggling."
const activeFilters = reactive({
  findings: true,
  narration: true,
  // #1078 Slice 3 — Screenshots default ON. Pre-Slice-2 the operator
  // had the right-rail "page state" sidebar showing the most-recent
  // image as they scrolled, so hiding screenshot-step rows in the
  // stream was tolerable. With the sidebar gone (Slice 2), the only
  // place screenshots live is inline at their step — hiding them by
  // default makes them invisible until the operator discovers the chip.
  screenshots: true,
  tools: false,
})

function toggleFilter(id) {
  activeFilters[id] = !activeFilters[id]
}

// #1078 Slice 3 — suppress the "Captured screenshot" / similar bare
// prose when the step carries an actual image; the image is the
// content, the line above it is just noise. Match the exact strings
// the Slice 1 recorder emits for image-bearing tools.
const _IMAGE_REDUNDANT_SUMMARIES = new Set([
  'Captured screenshot',
])
function _isImageRedundantSummary(ev) {
  return Boolean(ev.screenshot_id) &&
    _IMAGE_REDUNDANT_SUMMARIES.has((ev.summary || '').trim())
}

// Classify each event into one of the four chip categories. An event
// can match more than one (a step that filed a finding AND has a
// screenshot is BOTH findings + screenshots) — we keep it if any
// active chip claims it.
function _categoriesFor(ev) {
  const out = []
  if (ev.kind === 'step') {
    if ((ev.finding_ordinals || []).length) out.push('findings')
    if (ev.screenshot_id) out.push('screenshots')
    if (ev.summary || ev.text_from_persona) out.push('narration')
    // Pure tool call — no narration, no finding, no screenshot.
    if (!out.length) out.push('tools')
  } else {
    // Standalone log entries (dedup already removed paired ones).
    out.push('narration')
  }
  return out
}

let pollTimer = null

// Pipeline: timelineEvents → persona filter → dedup → chip filter
//                                           ↑           ↑
//                                         baseline    final
//
// `baselineEvents` is the post-dedup set — what the operator would see
// if every chip were on. Both `chipCounts` (header counts) and
// `filteredEvents` (current display) derive from it. Counts reflect
// the displayed reality, not the raw event count.
const baselineEvents = computed(() => {
  const events = timelineEvents.value.filter(
    (ev) => !timelinePersona.value || ev.persona_id === timelinePersona.value,
  )
  // Slice A dedup: shadow log entries whose text matches the next
  // step's text from the same persona.
  const shadowed = new Set()
  for (let i = 0; i < events.length; i++) {
    const ev = events[i]
    if (ev.kind !== 'log' || !ev.content) continue
    for (let j = i + 1; j < Math.min(i + 4, events.length); j++) {
      const next = events[j]
      if (next.kind !== 'step') continue
      if (next.persona_id !== ev.persona_id) break
      const ntext = next.text_from_persona || next.summary || ''
      const norm = (s) => s.trim().replace(/^["']|["']$/g, '').toLowerCase()
      const a = norm(ev.content)
      const b = norm(ntext)
      if (b && a.startsWith(b.slice(0, 80))) shadowed.add(i)
      if (b.startsWith(a.slice(0, 80))) shadowed.add(i)
      break
    }
  }
  return events.filter((_, i) => !shadowed.has(i))
})

const filteredEvents = computed(() => {
  // Chip filtering on the baseline. Keep the event if ANY of its
  // categories is currently active.
  const chipped = baselineEvents.value.filter((ev) =>
    _categoriesFor(ev).some((c) => activeFilters[c]),
  )
  // #1822 §6 — narration search on top of the chip filter. The "N of M"
  // summary in the filter row doubles as the match count.
  const q = timelineSearch.value.trim().toLowerCase()
  if (!q) return chipped
  return chipped.filter((ev) =>
    [ev.content, ev.summary, ev.text_from_persona].some(
      (s) => typeof s === 'string' && s.toLowerCase().includes(q),
    ),
  )
})

// Per-chip event count for the filter-bar header. Computed against the
// baseline (post-dedup) so the count matches what's selectable — not
// the raw event total. Updates reactively when persona filter changes.
const chipCounts = computed(() => {
  const counts = { findings: 0, narration: 0, screenshots: 0, tools: 0 }
  for (const ev of baselineEvents.value) {
    for (const c of _categoriesFor(ev)) counts[c]++
  }
  return counts
})

// #1053 — Slice E. Infer high-level phases from URL transitions in
// browser_navigate calls. Each phase = a contiguous stretch of events
// between two navigations. Used to render the horizontal phase ribbon
// at the top of the timeline.
//
// args_summary for browser_navigate is "url=<URL>" (see runner.py's
// _summarize_args). Other tools have different args summaries with no
// URL — those don't start a new phase.
function _parseNavUrl(ev) {
  if (ev.kind !== 'step') return null
  const tool = ev.tool_name || ev.tool || ''
  if (!tool.includes('browser_navigate') || tool.includes('back')) return null
  const m = (ev.args_summary || '').match(/^url=(.+)$/)
  if (!m) return null
  try {
    return new URL(m[1].trim())
  } catch {
    return null
  }
}

const phases = computed(() => {
  const events = baselineEvents.value
  // #1115 — bucket per-URL-section finding counts by kind so the spine
  // and section-header badges can render "🐞 3 · ✓ 2" instead of a
  // single "5 findings" that conflates praise with bugs. Each
  // ev.finding_ordinals entry is a 1-based index into run.findings;
  // we look up each one to get its kind, then bucket. Legacy findings
  // without ``kind`` default to 'bug' (qa-store schema), which lands
  // in fixableCount.
  const findings = run.value?.findings || []
  const list = []
  let cur = null
  events.forEach((ev, idx) => {
    const url = _parseNavUrl(ev)
    if (url) {
      if (cur) {
        cur.endIdx = idx - 1
        list.push(cur)
      }
      cur = {
        id: list.length,
        pathname: url.pathname || '/',
        host: url.host,
        href: url.href,
        startIdx: idx,
        endIdx: idx,
        // #1115 — findingCount is the total across all kinds, kept for
        // back-compat with the title strings + tests; the bucketed
        // counts below are what the new badges read.
        findingCount: 0,
        fixableCount: 0,
        praiseCount: 0,
        observationCount: 0,
        // #1078 Slice 2 — count screenshots per URL section so the
        // spine can pip "📷N" pills. Step rows that captured an image
        // carry a screenshot_id (set by the recorder when an inline
        // image lands in GridFS, see run_recorder.attach_screenshot_to_step).
        screenshotCount: 0,
      }
    }
    if (cur && ev.kind === 'step') {
      const ords = ev.finding_ordinals || []
      if (ords.length) {
        cur.findingCount += ords.length
        for (const ord of ords) {
          const f = findings[ord - 1]
          if (!f) continue
          const k = f.kind || 'bug'
          if (FIXABLE_KINDS.has(k)) cur.fixableCount += 1
          else if (k === 'praise') cur.praiseCount += 1
          else if (k === 'observation') cur.observationCount += 1
        }
      }
      if (ev.screenshot_id) {
        cur.screenshotCount += 1
      }
    }
  })
  if (cur) {
    cur.endIdx = events.length - 1
    list.push(cur)
  }
  return list
})

// #1078 Slice 2 — alias for the new spine template. `phases` is still
// used elsewhere (currentPhaseIdx, phaseLabel, _semanticPhaseFor) so
// we don't rename the computed; the spine consumes the alias and the
// rest of the file keeps reading `phases`.
const urlSections = phases

// Index of the phase containing the currentEventIdx (driven by the
// Slice D IntersectionObserver). -1 when no phase contains it (e.g.
// the run hasn't navigated yet and currentEventIdx is at a log entry
// before any navigation).
const currentPhaseIdx = computed(() => {
  const idx = currentEventIdx.value
  return phases.value.findIndex((p) => idx >= p.startIdx && idx <= p.endIdx)
})

// Display a phase's label compactly. "/login/admin" with too many
// segments gets the trailing one only (".../admin"). Empty pathname
// is the root "/".
function phaseLabel(p) {
  const path = p.pathname || '/'
  if (path === '/') return '/'
  // Drop trailing slash; keep first 2 segments.
  const trimmed = path.replace(/\/$/, '')
  const segs = trimmed.split('/').filter(Boolean)
  if (segs.length <= 2) return trimmed
  return '/…/' + segs[segs.length - 1]
}

// Display layer on top of filteredEvents — annotates each event with a
// `_showAvatar` flag that's true only when the previous visible event has a
// different persona. The avatar gutter collapses consecutive entries from the
// same persona Slack-style; the rail (the thin vertical line between rows)
// stays, so the reader can still tell entries belong to the same speaker.
//
// #1049 — Slice A: collapse "log + step" duplicate pairs. The persona's
// pre-action narration is recorded TWICE in qa_run_logs / qa_run_steps:
// once as a log line ("Let me see the page") and once as the step's
// text_from_persona ("Let me see the page"). Same text, both shown,
// double the visual noise. Skip the log entry when the next event is a
// step from the same persona whose text_from_persona starts with the
// log's content (within ~5s — defensive against unrelated logs that
// happen to share a prefix).
// Display layer — dedup already happened upstream in baselineEvents.
// All this does is annotate consecutive-same-persona entries with
// _showAvatar=false so the avatar gutter collapses Slack-style.
const displayEvents = computed(() => {
  // #1078 Slice 2 — for each event in the filtered stream, compute
  // which URL section it belongs to (the most recent navigation at or
  // before the event in the baseline order). Emit `_urlSectionStart`
  // on the FIRST event of each section as it appears in the filtered
  // stream, so the template renders a section header above it.
  //
  // When a section has no events surviving the current filter chips,
  // it gets no header — which is the right behaviour. The spine
  // (which reads from `urlSections` directly off baseline) still shows
  // the URL pip; clicking it scrolls to wherever the first non-filtered
  // event after that URL lives.
  const sections = phases.value
  const baseline = baselineEvents.value
  // baseline-index → section (cheap lookup while walking filteredEvents).
  function sectionFor(baselineIdx) {
    // Sections are in increasing startIdx order. Linear scan is fine for
    // typical run sizes; bail at the first section whose start exceeds
    // baselineIdx.
    let match = null
    for (const s of sections) {
      if (s.startIdx > baselineIdx) break
      match = s
    }
    return match
  }
  const out = []
  let lastPersona = null
  let lastSectionId = null
  for (const ev of filteredEvents.value) {
    const baselineIdx = baseline.indexOf(ev)
    const section = sectionFor(baselineIdx)
    const isFirstInSection =
      section && (lastSectionId == null || section.id !== lastSectionId)
    out.push({
      ...ev,
      _showAvatar: ev.persona_id !== lastPersona,
      _urlSectionStart: isFirstInSection ? section : null,
    })
    lastPersona = ev.persona_id
    if (section) lastSectionId = section.id
  }
  return out
})

// #1050 — Slice B: jump from a finding card to its step in the timeline.
// Findings are keyed by ``finding_id`` = ``<run_id>:<persona>:<ordinal>``.
// Steps carry ``finding_ordinals: [int, ...]``. The id-to-step map is
// computed once per timeline reload; clicking a finding chip scrolls
// the matching step into view and pulses it for a beat.
const stepRefs = ref({})
// #1052 — keep parallel ref-by-index for the IntersectionObserver
// (the sidebar tracks the topmost intersecting event idx; we don't
// need step-keys for that — just an array of <li> elements).
const eventEls = ref([])
function _stepKey(personaId, stepN) {
  return `${personaId}#${stepN}`
}
function registerStepRef(ev, el) {
  if (!el || ev.kind !== 'step' || ev.step_n == null) return
  stepRefs.value[_stepKey(ev.persona_id, ev.step_n)] = el
}
function registerStepRefWithIdx(ev, el, idx) {
  registerStepRef(ev, el)
  // Track every event, not just steps — the IntersectionObserver
  // needs the FULL ordered list so "topmost in viewport" works for
  // logs too. Vue calls this callback with `null` on unmount; that
  // happens to compact the array when the filter set shrinks. Re-
  // sparse arrays are tolerated by the observer wiring below.
  eventEls.value[idx] = el || null
}

function _findingOrdinal(findingId) {
  // finding_id = "<run_id>:<persona>:<ordinal>". Pull the ordinal off
  // the end; tolerant to colons in run_id (which by convention has no
  // colons but we don't enforce it client-side).
  if (!findingId) return null
  const last = findingId.split(':').pop()
  const n = parseInt(last, 10)
  return Number.isNaN(n) ? null : n
}

// Slice 2.2 of #1106 — hover tooltip that surfaces regression /
// recurring context alongside the finding body. Operators with the
// sticky panel collapsed get the cross-run state without expanding.
function findingTitle(f) {
  const parts = [f.body || f.title]
  if (f.is_regression) parts.push('⚠ Regression — previously fixed.')
  if ((f.recurring_count || 1) > 1) {
    parts.push(`↻ Seen ${f.recurring_count} times across runs.`)
  }
  return parts.filter(Boolean).join('\n\n')
}

function jumpToFinding(f) {
  // Find the step whose finding_ordinals includes this finding's
  // ordinal. Search the raw timelineEvents (not filteredEvents) so the
  // jump works even when the operator's filter hides the step today.
  const ordinal = _findingOrdinal(f.finding_id)
  if (ordinal == null) return
  const step = timelineEvents.value.find(
    (e) =>
      e.kind === 'step' &&
      e.persona_id === f.persona &&
      Array.isArray(e.finding_ordinals) &&
      e.finding_ordinals.includes(ordinal),
  )
  if (!step) return
  // Make sure the step is unfiltered so it renders.
  if (timelinePersona.value && timelinePersona.value !== step.persona_id) {
    timelinePersona.value = ''
  }
  // #1051 — ensure the Findings chip is on so the target step isn't
  // filtered out by chip selection. A note_finding step always
  // belongs to the 'findings' category; turning that chip on is
  // sufficient (no need to flip the others).
  if (!activeFilters.findings) activeFilters.findings = true
  nextTick(() => {
    const el = stepRefs.value[_stepKey(step.persona_id, step.step_n)]
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    // A short pulse hint so the operator's eye locks onto the row that
    // just moved into view.
    el.classList.add('finding-jump-pulse')
    setTimeout(() => el.classList.remove('finding-jump-pulse'), 1500)
  })
}

// #1170 (slice 2 of #1168) — the sticky chip panel used to render
// every finding regardless of kind, so a praise-nit and a bug-nit
// looked pixel-identical. Operator complaint 2026-05-28: "I can't
// see any bugs/issues jumping out". The fix is to render only
// actionable kinds (bug/gap/risk/nit) in the Findings chip panel,
// and pull praise + observation into a sibling collapsible Wins
// panel underneath. The header count now reflects actionable-only
// so "Findings (77)" doesn't lie about including 26 praises.
const panelFindings = computed(() => {
  return (run.value?.findings || []).filter((f) => FIXABLE_KINDS.has(_kindOf(f)))
})
const winsFindings = computed(() => {
  return (run.value?.findings || []).filter((f) => {
    const k = _kindOf(f)
    return k === 'praise' || k === 'observation'
  })
})
const showWins = ref(false)

// Per-severity finding counts for the findings panel header.
// Scoped to actionable findings only — praise/observation get
// their own count in the Wins panel.
const findingCountSummary = computed(() => {
  const counts = { blocker: 0, major: 0, minor: 0, nit: 0 }
  for (const f of panelFindings.value) {
    if (counts[f.severity] != null) counts[f.severity]++
  }
  const parts = []
  if (counts.blocker) parts.push(`${counts.blocker} blocker`)
  if (counts.major) parts.push(`${counts.major} major`)
  if (counts.minor) parts.push(`${counts.minor} minor`)
  if (counts.nit) parts.push(`${counts.nit} nit`)
  return parts.length ? parts.join(' · ') : '—'
})

async function refreshTimeline() {
  timelineLoading.value = true
  try {
    const { events } = await getRunTimeline(props.runId)
    timelineEvents.value = (events || []).map((e, i) => ({
      ...e,
      _key: `${e.kind}:${e.persona_id}:${e.step_n ?? e.seq ?? i}`,
    }))
  } catch (e) {
    // Timeline endpoint is non-fatal — the Reviews and Findings tabs still
    // function without it. Surface the error inline in the timeline area.
    timelineEvents.value = []
  } finally {
    timelineLoading.value = false
  }
}

// #1115 follow-up — refresh the RUN DOCUMENT itself while the run is
// active. The pre-existing 4s poll only re-ran refreshTimeline (steps +
// screenshots), so the operator saw a growing timeline but a frozen
// findings tab, frozen URL-spine bucket badges, frozen totals, frozen
// status. This keeps run.value in sync so the Findings tab, the spine
// badges (which derive from run.findings), and the run-summary header
// all update live.
//
// Two reasons to keep this SEPARATE from refreshTimeline rather than
// folding both into one call:
//   1. The full-run fetch is the more expensive endpoint
//      (joins reviews + findings + mcp summary) — we don't want to
//      double the per-tick cost.
//   2. The two payloads update at different rates: steps land every
//      few seconds, but run.findings only repopulates when a persona
//      finishes its review phase. Keeping them split lets us tune
//      cadences independently if cost becomes a concern.
async function refreshRunDoc() {
  try {
    const fresh = await getRun(props.runId)
    if (fresh) {
      // Replace the whole value so reactive computeds (filteredFindings,
      // fixableFindings, phases bucket counts, finding-status select
      // bindings) recompute correctly. The per-finding gh_issue_*
      // fields we set locally on file-issue are server-canonical, so
      // the server's view wins on each refresh.
      run.value = fresh
    }
  } catch (e) {
    // Non-fatal. The previous run.value stays on screen and the next
    // tick retries.
  }
}

// Totals shape — defensive defaults so the page renders even on a
// pre-#882 run document missing real_cost_usd/backend.
const totals = computed(() => ({
  input_tokens: 0,
  output_tokens: 0,
  cache_tokens: 0,
  cost_usd: 0,
  real_cost_usd:
    run.value?.totals?.real_cost_usd ?? run.value?.totals?.cost_usd ?? 0,
  backend: 'api',
  ...(run.value?.totals || {}),
}))

const isMaxBilled = computed(() => totals.value.backend === 'claude-code')

// #1115 follow-up — single source of truth for "this run is still
// happening", used by the live pill in the header and (via the poll
// loop predicate) gating the auto-refresh. Mirror the predicate on
// onMounted so adding new "alive" statuses here cascades.
const isLive = computed(
  () => run.value?.status === 'running' || run.value?.status === 'new',
)

const mandatoryActions = computed(
  () => run.value?.config_snapshot?.mandatory_action_ids || [],
)

const coverageCatalog = ref(null)
const catalogById = computed(() => {
  if (!coverageCatalog.value) return {}
  return Object.fromEntries(
    coverageCatalog.value.actions.map((a) => [a.id, a]),
  )
})

const filteredFindings = computed(() => {
  const all = run.value?.findings || []
  return all.filter(
    (f) =>
      (!severityFilter.value || f.severity === severityFilter.value) &&
      (!categoryFilter.value || f.category === categoryFilter.value),
  )
})

// #1115 — three-section split of the run's findings, applied on top of
// the severity/category filters so an operator narrowing on "blocker"
// still sees the three sections (with 0 in two of them).
function _kindOf(f) {
  // Legacy findings (pre-#1115) lack ``kind``. The qa-store schema
  // defaults to 'bug' on insert; this fallback handles in-flight docs
  // that haven't been re-upserted yet.
  const k = f?.kind || 'bug'
  return KIND_META[k] ? k : 'bug'
}
const fixableFindings = computed(() => {
  return filteredFindings.value
    .filter((f) => FIXABLE_KINDS.has(_kindOf(f)))
    .slice()
    .sort((a, b) => {
      const sa = SEVERITY_RANK[a.severity] ?? 99
      const sb = SEVERITY_RANK[b.severity] ?? 99
      return sa - sb
    })
})
const praiseFindings = computed(() =>
  filteredFindings.value.filter((f) => _kindOf(f) === 'praise'),
)
const observationFindings = computed(() =>
  filteredFindings.value.filter((f) => _kindOf(f) === 'observation'),
)

// #1169 — three-tier split for the Triage tab. Blockers get banner
// cards, majors get a compact list, minors+nits collapse behind a
// toggle. Praise/observation aren't here at all (they belong in the
// Findings tab's existing "Working well" section, and in slice 2 of
// #1168 they get pulled into a sibling Wins panel).
const triageSections = computed(() => {
  const fixable = fixableFindings.value
  return {
    blockers: fixable.filter((f) => f.severity === 'blocker'),
    majors: fixable.filter((f) => f.severity === 'major'),
    others: fixable.filter((f) => f.severity === 'minor' || f.severity === 'nit'),
  }
})

// Triage UI state — track which majors/others rows are open and whether
// the collapsed "others" group is expanded. Kept in component state
// rather than the URL because the operator usually wants a fresh
// triage on each visit, not a remembered scroll position.
const expandedTriageRows = reactive({})
const showOthers = ref(false)
function toggleTriageRow(findingId) {
  expandedTriageRows[findingId] = !expandedTriageRows[findingId]
}

// View-trace from a triage card — flip to the Timeline tab first so
// the target step renders, then reuse jumpToFinding for the scroll +
// pulse. Without the tab flip jumpToFinding scrolls the hidden
// timeline section and the operator sees nothing happen.
function viewTraceForFinding(f) {
  activeTab.value = 'timeline'
  nextTick(() => jumpToFinding(f))
}

// #1172 (slice 4 of #1168) — Per-persona digest cards.
//
// Below the blockers/majors/others sections on the Triage view, render
// one card per persona that filed any findings. Each card shows:
//   • persona avatar + display_name + archetype one-liner
//   • severity counts (blocker · major · minor · nit)
//   • a 1-paragraph first-person synthesis lazy-loaded from the
//     /api/runs/{id}/personas/{pid}/synthesis endpoint when expanded
//   • top 3 fixable findings (blocker + major) with the same
//     File-issue + View-trace affordances as the global sections
const _SEV_COUNT_INIT = { blocker: 0, major: 0, minor: 0, nit: 0 }
const triagePerPersona = computed(() => {
  const fixable = fixableFindings.value
  if (!fixable.length) return []
  // Group fixable findings by persona, with severity counts.
  const byPersona = new Map()
  for (const f of fixable) {
    const pid = f.persona || 'unknown'
    if (!byPersona.has(pid)) {
      byPersona.set(pid, {
        personaId: pid,
        findings: [],
        counts: { ..._SEV_COUNT_INIT },
      })
    }
    const entry = byPersona.get(pid)
    entry.findings.push(f)
    if (entry.counts[f.severity] != null) entry.counts[f.severity] += 1
  }
  // Each persona's findings are already severity-sorted because
  // fixableFindings is, so the first 3 are the top 3. Sort personas
  // by their worst severity so the persona who hit a blocker lands
  // above the persona who only filed nits.
  const _personaWorst = (entry) => {
    if (entry.counts.blocker) return 0
    if (entry.counts.major) return 1
    if (entry.counts.minor) return 2
    return 3
  }
  return [...byPersona.values()].sort(
    (a, b) => _personaWorst(a) - _personaWorst(b),
  )
})

const expandedPersonaCards = reactive({})

function togglePersonaCard(personaId) {
  expandedPersonaCards[personaId] = !expandedPersonaCards[personaId]
}

// #1115 — per-finding file-issue state. Tracks which finding_id is
// currently mid-POST so the inline button can show a spinner without a
// page-wide busy flag. Errors are surfaced into the per-finding row.
const filingFindingId = ref('')
const filingFindingErrors = reactive({})

async function onFileFindingIssue(finding) {
  if (finding.gh_issue_url) return // already filed — no-op
  filingFindingId.value = finding.finding_id
  filingFindingErrors[finding.finding_id] = ''
  try {
    const result = await fileFindingIssue(finding.finding_id)
    finding.gh_issue_url = result.gh_issue_url
    finding.gh_issue_number = result.gh_issue_number
  } catch (e) {
    if (e.response?.status === 409 && e.response.data?.detail) {
      // Already filed under our feet (concurrent operator click) —
      // try to recover the URL from the detail message and surface
      // it instead of erroring.
      const m = String(e.response.data.detail).match(/https?:\/\/\S+/)
      if (m) {
        finding.gh_issue_url = m[0]
        const num = m[0].match(/\/issues\/(\d+)/)
        if (num) finding.gh_issue_number = parseInt(num[1], 10)
        return
      }
    }
    filingFindingErrors[finding.finding_id] =
      e.response?.data?.detail || e.message || 'Failed to file issue'
  } finally {
    filingFindingId.value = ''
  }
}

function renderMarkdown(md) {
  return marked.parse(md || '', { breaks: true })
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    run.value = await getRun(props.runId)
  } catch (e) {
    if (e.response?.status === 404) {
      // #1822 follow-up — pre-fix "open live run" links (and old
      // bookmarks) carry the JOB name (qa-ui-max-<epoch>) instead of
      // the run id. If the active run's job matches, hop to the real
      // run id instead of dead-ending on "not found".
      try {
        const active = await getActiveRun()
        if (
          active?.run_id &&
          active.run_id !== props.runId &&
          (active.job_name === props.runId || active.pod_name === props.runId)
        ) {
          router.replace(`/runs/${active.run_id}`)
          return
        }
      } catch {
        /* active-run lookup is best-effort — fall through to the 404 */
      }
      error.value = `Run "${props.runId}" was not found.`
    } else {
      error.value = `Could not load the run: ${e.message}`
    }
  } finally {
    loading.value = false
  }
  // Parallel best-effort secondary fetches. None should blank the page.
  await Promise.allSettled([
    getCoverageCatalog().then((c) => (coverageCatalog.value = c)),
    listPersonas({ includeHidden: true }).then((rows) => {
      personaMeta.value = Object.fromEntries(
        (rows || []).map((p) => [p.persona_id, p]),
      )
    }),
    refreshTimeline(),
    // Slice 1 of #1002 — discovered_* for the Discovered tab. Empty
    // results on a pre-#1002 run; the tab renders an empty-state.
    listDiscoveredActions({ runId: props.runId, limit: 1000 })
      .then((r) => (discoveredActions.value = r.actions || [])),
    listDiscoveredTools({ runId: props.runId, limit: 500 })
      .then((r) => (discoveredTools.value = r.tools || [])),
    listDiscoveredBranches({ runId: props.runId, limit: 500 })
      .then((r) => (discoveredBranches.value = r.branches || [])),
  ])
}

async function onStatusChange(finding, status) {
  try {
    const updated = await setFindingStatus(finding.finding_id, status)
    finding.status = updated.status
  } catch (e) {
    error.value = `Could not update the finding: ${e.message}`
  }
}

async function onFileIssue() {
  // Pre-PR-C this button posted straight to GitHub with no friction. The
  // issue is expensive to file by mistake (issue gets created, comments
  // can't be unreviewed), so surface a confirm spelling out which findings
  // will and won't make it into the body. Only `included` findings post —
  // see qa_review_api/issue.py compose_issue() — so an issue filed with
  // every finding still at `open` will be near-empty.
  const findings = run.value?.findings || []
  const includedCount = findings.filter((f) => f.status === 'included').length
  const openCount = findings.filter((f) => f.status === 'open').length
  const dismissedCount = findings.filter((f) => f.status === 'dismissed').length
  const personaList = (run.value?.personas || []).join(', ') || '(none)'
  const warnEmpty =
    includedCount === 0
      ? '\n  ⚠ Only findings marked "included" are posted. With 0 included, ' +
        'the issue body will be a near-empty placeholder. Triage the ' +
        'findings tab first if you want the body populated.\n'
      : ''
  if (
    !window.confirm(
      `File a GitHub issue from this run?\n\n` +
        `  Run: ${props.runId}\n` +
        `  Personas: ${personaList}\n` +
        `  Findings included (will post): ${includedCount}\n` +
        `  Findings still open (will not post): ${openCount}\n` +
        `  Findings dismissed (will not post): ${dismissedCount}\n` +
        warnEmpty +
        '\nThe issue title and body are composed server-side and posted to ' +
        'the configured GitHub repo. Not undoable from this UI — only by ' +
        'closing the issue on GitHub.',
    )
  ) {
    return
  }
  filing.value = true
  fileError.value = ''
  try {
    const { gh_issue_url } = await fileIssue(props.runId)
    run.value.gh_issue_url = gh_issue_url
    run.value.status = 'filed'
  } catch (e) {
    fileError.value =
      e.response?.data?.detail || `Could not file the issue: ${e.message}`
  } finally {
    filing.value = false
  }
}

// #1052 — Slice D. IntersectionObserver-driven "current event in view"
// for the sticky screenshot sidebar. The observer watches every <li>
// element and we recompute the topmost-intersecting one on each batch.
const currentEventIdx = ref(0)
let intersectionObserver = null

function _setupTimelineObserver() {
  if (intersectionObserver) intersectionObserver.disconnect()
  if (typeof window === 'undefined' || !window.IntersectionObserver) return
  // rootMargin "-80px 0px -50% 0px" trains the observer to consider
  // an event "in view" when it crosses the top 80px (under the run
  // header) and ignores anything below the viewport midline — so the
  // sidebar latches onto whatever's anchored near the top of the
  // visible scroll position.
  intersectionObserver = new window.IntersectionObserver(
    (entries) => {
      // Find the topmost intersecting entry (smallest idx).
      let topIdx = null
      for (const entry of entries) {
        if (!entry.isIntersecting) continue
        const idx = Number(entry.target.dataset.eventIdx)
        if (Number.isNaN(idx)) continue
        if (topIdx == null || idx < topIdx) topIdx = idx
      }
      if (topIdx != null) currentEventIdx.value = topIdx
    },
    { rootMargin: '-80px 0px -50% 0px', threshold: 0 },
  )
  // Observe whatever <li> refs are currently registered.
  for (const el of eventEls.value) {
    if (el) intersectionObserver.observe(el)
  }
}

// Re-attach the observer when the displayed event set changes (the
// filter chips remount <li> nodes which detaches the prior wiring).
watch(displayEvents, () => {
  nextTick(() => _setupTimelineObserver())
}, { flush: 'post' })

// #1078 Slice 2 — the right-rail "page state" screenshot sidebar
// (introduced in #1052 / Slice D) is removed. Screenshots now render
// inline at their step, anchored to the URL section the spine carries.
// The IntersectionObserver-driven `currentEventIdx` still feeds
// `currentPhaseIdx` (which highlights the active spine row) and the
// hover/jump affordances on the findings panel, so the observer
// scaffolding above stays.

function scrollToStep(eventIdx) {
  const el = eventEls.value[eventIdx]
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

onMounted(() => {
  load()
  // #1030 — fetch the MCP catalog so the Slice A chip layer can render
  // human-friendly display names. Catalog load is non-blocking — the
  // run still renders if /api/mcp-servers 404s (chips fall back to
  // raw ids gracefully via mcpChipLabel).
  listMCPServers()
    .then((servers) => {
      mcpCatalog.value = servers
    })
    .catch(() => {
      // Silent — chip layer's fall-through handles an empty catalog.
    })
  // Auto-poll the timeline while the run is active so the page feels live
  // without a websocket. 4s is gentle enough that the API isn't hammered
  // and tight enough that steps appear "shortly after" they're recorded.
  //
  // #1115 follow-up — also refresh the run document itself (findings,
  // reviews, totals, status) on the same tick, so the operator sees
  // findings + URL-spine bucket badges + the run-summary header come
  // alive as each persona finishes, not just the timeline.
  pollTimer = setInterval(() => {
    if (run.value?.status === 'running' || run.value?.status === 'new') {
      refreshTimeline()
      refreshRunDoc()
    }
  }, 4000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (intersectionObserver) intersectionObserver.disconnect()
})

watch(() => props.runId, load)
</script>

<style scoped>
/* #1078 Slice 2 — two-column layout: URL spine on the LEFT (sticky,
   220px), event stream on the right. Below the lg breakpoint the
   spine drops above the stream as a horizontal scroll row so narrow
   viewports don't squeeze the cards. Replaces the right-rail
   screenshot sidebar from Slice D (#1052) — screenshots now render
   inline at their owning step.

   #1115 follow-up — fixed sticky regression. Pre-fix the spine had
   ``position: sticky`` but ``align-items: start`` on the grid made the
   spine cell collapse to content height, leaving sticky no scroll-room.
   Now the cell stretches to the row height (= the timeline column
   height), AND we offset top: by the findings panel's height + gap so
   the spine doesn't end up hidden behind the findings panel sticking
   at top:0. */
.timeline-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 1rem;
}
@media (min-width: 1024px) {
  .timeline-grid {
    grid-template-columns: 220px minmax(0, 1fr);
    /* Was `align-items: start` — collapsed the spine cell to content
       height and broke sticky. ``stretch`` (the default for grid items)
       lets the spine's grid cell take the row height, which gives the
       inner sticky element scroll-room. */
    align-items: stretch;
  }
}
.url-spine {
  /* On narrow viewports the spine lives above the stream — no sticky
     behaviour, just sits inline. */
}
.url-spine-sticky {
  position: sticky;
  /* Offset = findings panel's effective top-sticky height + a small
     gap. The findings panel is sticky at top:0 with z-index:10 so it
     hovers above the spine; pushing the spine to ~9rem keeps both
     visible while scrolling the timeline. The exact value is
     approximate by design — findings panel content varies. */
  top: 9rem;
  max-height: calc(100vh - 10rem);
  overflow-y: auto;
  /* Below the findings-panel's z-index (10) so a long URL list doesn't
     overlap the panel while scrolling. */
  z-index: 5;
}
@media (max-width: 1023px) {
  .url-spine-sticky {
    position: static;
    max-height: none;
    overflow-y: visible;
  }
  .url-spine ol {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem;
  }
  .url-spine li {
    flex: 0 1 auto;
  }
}

/* URL section dividers within the stream. The header sits above the
   first event of each section; its sticky-ish look anchors the events
   below to a specific page. */
.url-section-header {
  margin-top: 1rem;
  margin-bottom: 0.25rem;
  border-top: 1px dashed rgb(var(--te-hairline) / 0.08);
}
.url-section-header:first-child {
  margin-top: 0;
  border-top: none;
}

/* #1050 — sticky findings panel + brief pulse on the jumped-to step.
   #1822 — the panel's fill/border come from the dark `.panel` component
   class; the scoped rule only pins it (an explicit opaque background so
   timeline rows never bleed through while it's stuck). */
.findings-panel {
  position: sticky;
  top: 0;
  z-index: 10;
  background: rgb(var(--te-panel));
  border: 1px solid rgb(var(--te-hairline) / 0.08);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
}
.finding-jump-pulse {
  animation: jump-pulse 1.5s ease-out;
}
@keyframes jump-pulse {
  /* Signal cyan (brand-600 #27c2e4) — was the old paper-theme teal. */
  0%   { background: rgb(var(--te-brand-500) / 0.2); }
  50%  { background: rgb(var(--te-brand-500) / 0.12); }
  100% { background: transparent; }
}
</style>
