<template>
  <div class="mx-auto max-w-5xl p-6">
    <div class="mb-4 text-xs">
      <router-link to="/site">← Sites</router-link>
    </div>

    <div v-if="loadError" class="text-sm text-red-400">{{ loadError }}</div>

    <template v-else>
      <header class="mb-4 flex items-end justify-between gap-4">
        <div class="min-w-0">
          <h1 class="flex min-w-0 items-center gap-2">
            <span class="truncate">{{ target?.display_name || targetId }}</span>
            <HelpTip label="Site model">
              The <strong>site model</strong> is everything Test Ease knows about
              this site, as data: its <strong>Surfaces</strong> (pages/forms),
              the <strong>Flows</strong> personas should walk, curated
              <strong>Knowledge</strong> (what's intentional, so testers don't
              re-flag it), and the <strong>Questions</strong> you answer to
              configure the run.
            </HelpTip>
          </h1>
          <p v-if="target?.base_url" class="mt-1 truncate text-sm text-ink-500">
            {{ target.base_url }}
          </p>
        </div>
        <div class="flex shrink-0 items-center gap-2">
          <button
            v-if="capView.depth"
            class="pill text-[10px] uppercase tracking-wide text-brand-700"
            data-testid="depth-pill"
            title="Testing depth — grant more access to test deeper"
            @click="tab = 'capabilities'"
          >
            depth: {{ capView.depth.depth_label }} →
          </button>
          <span
            v-if="lifecycle"
            class="pill text-[10px] uppercase tracking-wide text-brand-700"
            data-testid="lifecycle-badge"
            :title="'Onboarding lifecycle'"
          >
            {{ lifecycle }}
          </span>
          <span class="pill text-[10px]">{{ targetId }}</span>
        </div>
      </header>

      <!-- Onboarding lifecycle stepper -->
      <div
        v-if="lifecycle && lifecycleStates.length"
        class="mb-6 panel panel-pad"
        data-testid="lifecycle-stepper"
      >
        <div class="flex flex-wrap items-center gap-1.5">
          <template v-for="(s, i) in lifecycleStates" :key="s">
            <span
              class="rounded-full px-2.5 py-1 text-[11px] font-medium transition"
              :class="
                i === lifecycleIndex
                  ? 'bg-brand-50 text-brand-800 ring-1 ring-brand-600/40'
                  : i < lifecycleIndex
                    ? 'text-brand-700'
                    : 'text-ink-400'
              "
            >
              {{ s }}
            </span>
            <span
              v-if="i < lifecycleStates.length - 1"
              class="text-ink-300"
              aria-hidden="true"
            >→</span>
          </template>
        </div>
        <div class="mt-2 flex flex-wrap items-center gap-3">
          <p v-if="nextStepHint" class="text-xs text-ink-500">
            <span class="font-medium text-ink-700">Next:</span> {{ nextStepHint }}
          </p>
          <button
            v-if="canExplore"
            class="btn-primary btn"
            :disabled="exploring"
            data-testid="explore-btn"
            @click="explore"
          >
            {{ exploring ? 'Exploring…' : (lifecycle === 're-explore' ? 'Re-explore the site' : 'Explore the site') }}
          </button>
        </div>
        <p v-if="exploreError" class="mt-2 text-xs text-red-400" data-testid="explore-error">
          {{ exploreError }}
        </p>
        <p v-if="exploreSummary" class="mt-2 text-xs text-emerald-400" data-testid="explore-summary">
          Explored{{ exploreSummary.title ? ` “${exploreSummary.title}”` : '' }} —
          {{ exploreSummary.counts.surfaces }} surface(s),
          {{ exploreSummary.counts.flows }} flow(s),
          {{ exploreSummary.counts.questions }} question(s).
          Answer the questionnaire below.
        </p>
      </div>

      <!-- Surfaces / Flows / Knowledge / Questions tabs -->
      <div class="mb-4 flex items-center gap-2 border-b border-ink-200">
        <button
          v-for="t in TABS"
          :key="t.id"
          class="border-b-2 px-3 py-2 text-sm font-medium transition"
          :class="
            tab === t.id
              ? 'border-brand-600 text-ink-900'
              : 'border-transparent text-ink-500 hover:text-ink-800'
          "
          @click="tab = t.id"
        >
          {{ t.label }}
          <span class="ml-1 text-xs text-ink-400">{{ t.count }}</span>
        </button>
      </div>

      <!-- Surfaces — read-only -->
      <section v-if="tab === 'surfaces'">
        <p class="mb-3 text-sm text-ink-600">
          Surfaces are the pages, forms, and API endpoints discovered on this
          site — the map the personas navigate.
        </p>
        <div v-if="surfaces.length" class="panel divide-y divide-ink-200/60">
          <div
            v-for="s in surfaces"
            :key="s.surface_id"
            class="px-5 py-3"
          >
            <div class="flex items-center gap-2">
              <span class="text-sm font-medium text-ink-900">
                {{ s.path || s.surface_id }}
              </span>
              <span v-if="s.kind" class="pill text-[10px]">{{ s.kind }}</span>
            </div>
            <p v-if="s.description" class="mt-1 text-xs text-ink-600">
              {{ s.description }}
            </p>
          </div>
        </div>
        <p v-else class="panel panel-pad text-sm text-ink-600">
          No surfaces mapped for this target yet.
        </p>
      </section>

      <!-- Flows — read-only -->
      <section v-else-if="tab === 'flows'">
        <p class="mb-3 text-sm text-ink-600">
          Flows are the journeys worth testing — signup, checkout, sharing —
          each tagged with the persona archetype best suited to walk it.
        </p>
        <div v-if="flows.length" class="panel divide-y divide-ink-200/60">
          <div
            v-for="f in flows"
            :key="f.flow_id"
            class="px-5 py-3"
          >
            <div class="flex items-center gap-2">
              <span class="text-sm font-medium text-ink-900">{{ f.flow_id }}</span>
              <span v-if="f.area" class="pill text-[10px]">{{ f.area }}</span>
              <span v-if="f.persona_archetype" class="text-xs text-ink-500">
                {{ f.persona_archetype }}
              </span>
            </div>
            <p v-if="f.description" class="mt-1 text-xs text-ink-600">
              {{ f.description }}
            </p>
          </div>
        </div>
        <p v-else class="panel panel-pad text-sm text-ink-600">
          No flows recorded for this target yet.
        </p>
      </section>

      <!-- Knowledge — curation (add / edit / delete) -->
      <section v-else-if="tab === 'knowledge'">
        <div class="mb-4 flex items-center justify-between gap-4">
          <p class="text-sm text-ink-600">
            Curated knowledge the testers read before every run — by-design
            behaviour, known issues, guidance, and glossary. The harness injects
            by-design entries so personas stop re-flagging them.
          </p>
          <button class="btn-ghost btn shrink-0" @click="startCreate">
            + Add entry
          </button>
        </div>

        <div v-if="curateError" class="mb-3 text-sm text-red-400">
          {{ curateError }}
        </div>

        <!-- Create / edit form -->
        <div v-if="editing" class="panel panel-pad mb-4">
          <h2 class="mb-3">{{ editing.entry_id ? 'Edit entry' : 'New entry' }}</h2>
          <div class="mb-3">
            <label class="label">Kind</label>
            <select v-model="editing.kind" class="select">
              <option v-for="k in KINDS" :key="k" :value="k">{{ k }}</option>
            </select>
          </div>
          <div class="mb-3">
            <label class="label">Applies to (surface/flow ids, comma-separated)</label>
            <input
              v-model="appliesToText"
              class="input"
              placeholder="e.g. s1, signup"
            />
          </div>
          <div class="mb-3">
            <label class="label">Body</label>
            <textarea
              v-model="editing.body"
              class="textarea"
              rows="5"
              placeholder="What should the testers know?"
            ></textarea>
          </div>
          <div class="flex items-center gap-2">
            <button
              class="btn-primary btn"
              :disabled="saving || !editing.body.trim()"
              @click="save"
            >
              {{ saving ? 'Saving…' : 'Save' }}
            </button>
            <button class="btn-ghost btn" :disabled="saving" @click="editing = null">
              Cancel
            </button>
          </div>
        </div>

        <!-- Grouped by kind -->
        <div v-if="knowledge.length">
          <div v-for="k in KINDS" :key="k" class="mb-5">
            <template v-if="byKind(k).length">
              <h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-500">
                {{ k }} ({{ byKind(k).length }})
              </h3>
              <div class="panel divide-y divide-ink-200/60">
                <div
                  v-for="entry in byKind(k)"
                  :key="entry.entry_id"
                  class="px-5 py-3"
                >
                  <div class="flex items-start justify-between gap-3">
                    <p class="prose-qa min-w-0 flex-1 whitespace-pre-wrap text-sm text-ink-800">
                      {{ entry.body }}
                    </p>
                    <div class="flex shrink-0 gap-2">
                      <button class="btn-ghost btn" @click="startEdit(entry)">
                        Edit
                      </button>
                      <button
                        class="btn-danger btn"
                        :disabled="deletingId === entry.entry_id"
                        @click="remove(entry)"
                      >
                        {{ deletingId === entry.entry_id ? '…' : 'Delete' }}
                      </button>
                    </div>
                  </div>
                  <div
                    v-if="entry.applies_to && entry.applies_to.length"
                    class="mt-1.5 flex flex-wrap gap-1"
                  >
                    <span
                      v-for="a in entry.applies_to"
                      :key="a"
                      class="pill text-[10px]"
                    >
                      {{ a }}
                    </span>
                  </div>
                </div>
              </div>
            </template>
          </div>
        </div>
        <p v-else class="panel panel-pad text-sm text-ink-600">
          No knowledge curated yet. <button class="text-brand-700 underline" @click="startCreate">Add the first entry</button>.
        </p>
      </section>

      <!-- Questions — the explorer's questionnaire -->
      <section v-else-if="tab === 'questions'" data-testid="questions-tab">
        <div class="mb-4">
          <p class="text-sm text-ink-600">
            The explorer's questionnaire — what the tool needs from you to test
            this site (credentials, scope, test data). Secret answers go to the
            encrypted vault; only a pointer is kept here.
          </p>
          <div class="mt-3 flex flex-wrap items-center gap-4 text-xs text-ink-500">
            <span data-testid="q-rollup">
              {{ qStatus.answered }}/{{ qStatus.total }} answered<template
                v-if="qStatus.required_open"
              >
                · <span class="text-red-400">{{ qStatus.required_open }} required open</span></template
              ><template v-if="qStatus.skipped"> · {{ qStatus.skipped }} skipped</template>
            </span>
            <span class="flex items-center gap-1.5">
              <label class="text-ink-500">Lifecycle</label>
              <select
                class="select !py-1 text-xs"
                :value="lifecycle"
                data-testid="lifecycle-select"
                @change="changeLifecycle($event.target.value)"
              >
                <option v-for="s in lifecycleStates" :key="s" :value="s">{{ s }}</option>
              </select>
            </span>
          </div>
        </div>

        <div v-if="questionsError" class="mb-3 text-sm text-red-400">
          {{ questionsError }}
        </div>

        <div v-if="questions.length">
          <div v-for="cat in questionCategories" :key="cat" class="mb-5">
            <h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-500">
              {{ cat }} ({{ byCategory(cat).length }})
            </h3>
            <div class="panel divide-y divide-ink-200/60">
              <div
                v-for="q in byCategory(cat)"
                :key="q.question_id"
                class="px-5 py-3"
                :data-testid="`q-${q.question_id}`"
              >
                <div class="flex items-start justify-between gap-3">
                  <div class="min-w-0 flex-1">
                    <p class="text-sm font-medium text-ink-900">
                      {{ q.text }}
                      <span v-if="q.required" class="text-red-400" title="required">*</span>
                    </p>
                    <p v-if="q.rationale" class="mt-0.5 text-xs text-ink-500">
                      {{ q.rationale }}
                    </p>
                  </div>
                  <div class="flex shrink-0 items-center gap-1.5">
                    <span class="pill text-[10px]">{{ q.kind }}</span>
                    <span class="pill text-[10px]" :class="statusClass(q.status)">
                      {{ q.status }}
                    </span>
                  </div>
                </div>

                <!-- answered: show value (secrets masked) -->
                <div
                  v-if="q.status === 'answered' && drafts[q.question_id] === undefined"
                  class="mt-2 flex items-center justify-between gap-3"
                >
                  <p
                    class="min-w-0 flex-1 truncate text-sm text-ink-700"
                    :data-testid="`q-${q.question_id}-answer`"
                  >
                    <span v-if="q.kind === 'secret'" class="text-ink-500">
                      •••••••• (vaulted)
                    </span>
                    <template v-else>{{ q.answer }}</template>
                  </p>
                  <button class="btn-ghost btn" @click="startAnswer(q)">Re-answer</button>
                </div>

                <!-- skipped -->
                <div
                  v-else-if="q.status === 'skipped' && drafts[q.question_id] === undefined"
                  class="mt-2 flex items-center justify-between gap-3"
                >
                  <p class="text-sm text-ink-500">Skipped</p>
                  <button class="btn-ghost btn" @click="startAnswer(q)">Answer</button>
                </div>

                <!-- open OR re-answering: the answer input -->
                <div v-else class="mt-2 flex items-end gap-2">
                  <div class="flex-1">
                    <select
                      v-if="q.kind === 'choice'"
                      v-model="drafts[q.question_id]"
                      class="select"
                      :data-testid="`q-${q.question_id}-input`"
                    >
                      <option value="" disabled>Choose…</option>
                      <option v-for="o in q.options" :key="o" :value="o">{{ o }}</option>
                    </select>
                    <select
                      v-else-if="q.kind === 'boolean'"
                      v-model="drafts[q.question_id]"
                      class="select"
                      :data-testid="`q-${q.question_id}-input`"
                    >
                      <option value="" disabled>Choose…</option>
                      <option value="yes">yes</option>
                      <option value="no">no</option>
                    </select>
                    <input
                      v-else
                      v-model="drafts[q.question_id]"
                      :type="q.kind === 'secret' ? 'password' : 'text'"
                      class="input"
                      :placeholder="placeholderFor(q)"
                      :data-testid="`q-${q.question_id}-input`"
                    />
                  </div>
                  <button
                    class="btn-primary btn"
                    :disabled="busyId === q.question_id || !String(drafts[q.question_id] || '').trim()"
                    :data-testid="`q-${q.question_id}-submit`"
                    @click="submitAnswer(q)"
                  >
                    {{ busyId === q.question_id ? '…' : 'Answer' }}
                  </button>
                  <button
                    v-if="q.status === 'open'"
                    class="btn-ghost btn"
                    :disabled="busyId === q.question_id"
                    @click="skip(q)"
                  >
                    Skip
                  </button>
                  <button v-else class="btn-ghost btn" @click="cancelAnswer(q)">Cancel</button>
                </div>
              </div>
            </div>
          </div>
        </div>
        <p v-else class="panel panel-pad text-sm text-ink-600">
          No questions yet. The explorer generates these when it probes the site.
        </p>
      </section>

      <!-- Capabilities — grant deeper access ("level up your testing") -->
      <section v-else-if="tab === 'capabilities'" data-testid="capabilities-tab">
        <!-- framing first: what this is + that you stay in control -->
        <p class="mb-4 text-sm text-ink-600">
          Test Ease tests with whatever access you give it — the more you grant,
          the deeper it can test (a vague "the page errored" becomes a real stack
          trace + request id).
          <strong class="text-ink-800">Everything here is optional and you stay
          in control:</strong> Test Ease only ever <em>uses what you grant</em>,
          and never connects to anything on its own. Credentials go to the
          encrypted vault — only a pointer is kept.
        </p>
        <p class="mb-4 text-sm text-ink-500">
          New here? You can <strong>skip this tab entirely</strong> — the personas
          still test your public site (signup, checkout, clicking around) as
          anonymous visitors. Grant access later when you want deeper testing.
        </p>

        <!-- depth hero -->
        <div v-if="capView.depth" class="panel panel-pad mb-5">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p class="flex items-center gap-1 text-[10px] uppercase tracking-wide text-ink-500">
                Testing depth
                <HelpTip label="Testing depth">
                  How deep Test Ease can test this site, based on what you've
                  granted — from <strong>Black-box</strong> (just a public URL,
                  anonymous clicking) up to <strong>Environment control</strong>
                  (logs, DB, infra). Each rung you grant turns guesses into
                  verified facts.
                </HelpTip>
              </p>
              <p class="text-lg font-semibold text-ink-900">
                {{ capView.depth.depth_label }}
                <span class="text-sm font-normal text-ink-500">
                  (level {{ capView.depth.depth_level }} of 5)
                </span>
              </p>
            </div>
            <p v-if="capView.depth.next_unlock" class="max-w-md text-xs text-ink-500">
              <span class="font-medium text-brand-700">Next unlock:</span>
              {{ capView.depth.next_unlock.title }} — {{ capView.depth.next_unlock.unlocks }}
            </p>
          </div>
          <div class="mt-3 flex gap-1" data-testid="depth-ladder">
            <div
              v-for="(lvl, i) in capView.depth.levels"
              :key="lvl"
              class="flex-1 rounded-full px-2 py-1 text-center text-[10px] font-medium"
              :class="i <= capView.depth.depth_level ? 'bg-brand-50 text-brand-800' : 'bg-ink-100 text-ink-400'"
            >
              {{ lvl }}
            </div>
          </div>
        </div>

        <div v-if="capError" class="mb-3 text-sm text-red-400">{{ capError }}</div>

        <!-- Suggested for this site — the explorer's short, tailored shortlist
             (proposed from what it detected) so a newcomer isn't staring at the
             whole catalog wondering where to start. -->
        <div
          v-if="proposedCaps.length"
          class="mb-6 rounded-lg border border-brand-600/40 bg-brand-50/40 p-4"
          data-testid="suggested-caps"
        >
          <h3 class="mb-1 flex items-center gap-1 text-sm font-semibold text-brand-800">
            ✨ Suggested for this site
          </h3>
          <p class="mb-3 text-xs text-ink-600">
            Based on what exploration found, these are the few worth granting
            first — each unlocks deeper testing of a flow we detected. The full
            catalog is below.
          </p>
          <div class="grid gap-2 sm:grid-cols-2">
            <div
              v-for="cap in proposedCaps"
              :key="cap.capability_id"
              class="panel panel-pad"
              :data-testid="`sugg-cap-${cap.capability_id}`"
            >
              <div class="flex items-start justify-between gap-2">
                <div class="min-w-0">
                  <p class="text-sm font-medium text-ink-900">{{ cap.title }}</p>
                  <p class="mt-0.5 text-xs text-ink-500">{{ cap.unlocks }}</p>
                </div>
                <span class="pill shrink-0 text-[10px]" :class="riskClass(cap.risk_class)">
                  {{ cap.risk_class }}
                </span>
              </div>
              <div class="mt-2 flex flex-wrap items-center gap-2">
                <template v-if="connecting[cap.capability_id] !== undefined">
                  <input
                    v-if="cap.grant_kind !== 'none'"
                    v-model="connecting[cap.capability_id]"
                    :type="cap.grant_kind === 'secret' ? 'password' : 'text'"
                    class="input flex-1"
                    :placeholder="connectPlaceholder(cap)"
                    :data-testid="`sugg-cap-${cap.capability_id}-input`"
                  />
                  <button
                    class="btn-primary btn"
                    :disabled="busyCapId === cap.capability_id"
                    :data-testid="`sugg-cap-${cap.capability_id}-save`"
                    @click="saveConnect(cap)"
                  >
                    Grant
                  </button>
                  <button class="btn-ghost btn" @click="cancelConnect(cap)">Cancel</button>
                </template>
                <template v-else>
                  <button
                    class="btn-primary btn"
                    :data-testid="`sugg-cap-${cap.capability_id}-connect`"
                    @click="startConnect(cap)"
                  >
                    Connect
                  </button>
                  <button class="btn-ghost btn" @click="setStatus(cap, 'not_applicable')">
                    Not applicable
                  </button>
                </template>
              </div>
            </div>
          </div>
        </div>

        <!-- capabilities, grouped by ladder rung (lighter rungs first;
             sensitive infra rungs are collapsed behind "Advanced access") -->
        <div v-for="lvl in capLevels" :key="lvl" class="mb-5">
          <!-- the gate to the high-trust rungs, shown once -->
          <button
            v-if="lvl === firstAdvancedLevel"
            class="mb-3 w-full rounded-md border border-ink-200 px-4 py-2 text-left text-sm text-ink-700 transition hover:border-amber-400/60"
            data-testid="advanced-toggle"
            @click="advancedOpen = !advancedOpen"
          >
            {{ advancedOpen ? '▾' : '▸' }}
            <span class="font-medium">Advanced access</span>
            <span class="text-ink-500">
              — sensitive infrastructure (read-only DB, admin API, Kubernetes…).
              Higher trust; grant only what you're comfortable with.
            </span>
          </button>
          <template v-if="lvl <= MAX_VISIBLE_LEVEL || advancedOpen">
          <h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-500">
            L{{ lvl }} · {{ capView.depth ? capView.depth.levels[lvl] : '' }}
          </h3>
          <div class="grid gap-2 sm:grid-cols-2">
            <div
              v-for="cap in capsByLevel(lvl)"
              :key="cap.capability_id"
              class="panel panel-pad"
              :data-testid="`cap-${cap.capability_id}`"
              :class="{ 'opacity-60': capView.depth && lvl > capView.depth.depth_level + 1 && cap.status === 'available' }"
            >
              <div class="flex items-start justify-between gap-2">
                <div class="min-w-0">
                  <p class="text-sm font-medium text-ink-900">{{ cap.title }}</p>
                  <p class="mt-0.5 text-xs text-ink-500">{{ cap.unlocks }}</p>
                </div>
                <span class="pill shrink-0 text-[10px]" :class="riskClass(cap.risk_class)">
                  {{ cap.risk_class }}
                </span>
              </div>
              <div class="mt-2 flex flex-wrap items-center gap-2">
                <template v-if="cap.status === 'granted'">
                  <span class="pill text-[10px] text-emerald-400">granted ✓</span>
                  <button class="btn-ghost btn" :disabled="busyCapId === cap.capability_id" @click="revokeCap(cap)">
                    Revoke
                  </button>
                </template>
                <template v-else-if="connecting[cap.capability_id] !== undefined">
                  <input
                    v-if="cap.grant_kind !== 'none'"
                    v-model="connecting[cap.capability_id]"
                    :type="cap.grant_kind === 'secret' ? 'password' : 'text'"
                    class="input flex-1"
                    :placeholder="connectPlaceholder(cap)"
                    :data-testid="`cap-${cap.capability_id}-input`"
                  />
                  <button
                    class="btn-primary btn"
                    :disabled="busyCapId === cap.capability_id"
                    :data-testid="`cap-${cap.capability_id}-save`"
                    @click="saveConnect(cap)"
                  >
                    Grant
                  </button>
                  <button class="btn-ghost btn" @click="cancelConnect(cap)">Cancel</button>
                </template>
                <template v-else>
                  <button
                    class="btn-primary btn"
                    :data-testid="`cap-${cap.capability_id}-connect`"
                    @click="startConnect(cap)"
                  >
                    Connect
                  </button>
                  <button
                    v-if="cap.status === 'available' || cap.status === 'proposed'"
                    class="btn-ghost btn"
                    @click="setStatus(cap, 'not_applicable')"
                  >
                    Not applicable
                  </button>
                  <span v-if="cap.status === 'proposed'" class="pill text-[10px] text-amber-400">proposed</span>
                  <span
                    v-else-if="cap.status === 'declined' || cap.status === 'not_applicable'"
                    class="text-xs text-ink-400"
                  >{{ cap.status.replace('_', ' ') }}</span>
                </template>
              </div>
            </div>
          </div>
          </template>
        </div>

        <!-- custom escape hatch -->
        <div class="panel panel-pad">
          <button
            v-if="!customOpen"
            class="text-sm text-brand-700 underline"
            data-testid="cap-custom-open"
            @click="customOpen = true"
          >
            + Connect a custom capability
          </button>
          <div v-else class="space-y-2">
            <p class="text-xs text-ink-500">
              Anything bespoke — a god-mode console, an internal GraphQL, an MCP
              server the site exposes.
            </p>
            <input v-model="customForm.title" class="input" placeholder="Name (e.g. Admin GraphQL)" data-testid="cap-custom-title" />
            <input v-model="customForm.unlocks" class="input" placeholder="What it unlocks for testing" />
            <input v-model="customForm.token" type="password" class="input" placeholder="Credential / URL (optional)" />
            <div class="flex gap-2">
              <button class="btn-primary btn" :disabled="!customForm.title.trim() || busyCapId === 'custom'" data-testid="cap-custom-add" @click="addCustom">
                Add capability
              </button>
              <button class="btn-ghost btn" @click="customOpen = false">Cancel</button>
            </div>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import {
  answerSiteQuestion,
  createSiteKnowledge,
  addCustomCapability,
  deleteSiteKnowledge,
  exploreSiteTarget,
  getSiteCapabilities,
  getSiteTarget,
  listSiteFlows,
  listSiteKnowledge,
  listSiteQuestions,
  listSiteSurfaces,
  revokeCapability,
  setCapability,
  setTargetLifecycle,
  skipSiteQuestion,
  updateSiteKnowledge,
} from '../api.js'
import HelpTip from '../components/HelpTip.vue'

const props = defineProps({
  targetId: { type: String, required: true },
})

const KINDS = ['by_design', 'known_issue', 'guidance', 'glossary']

const target = ref(null)
const surfaces = ref([])
const flows = ref([])
const knowledge = ref([])
const loadError = ref('')

// Questionnaire + lifecycle.
const questions = ref([])
const qStatus = ref({ total: 0, answered: 0, open: 0, skipped: 0, required_open: 0 })
const lifecycle = ref('')
const lifecycleStates = ref([])

const lifecycleIndex = computed(() => lifecycleStates.value.indexOf(lifecycle.value))
// The explorer is offered before a site is configured (or to re-run discovery).
const canExplore = computed(() =>
  ['registered', 'exploring', 're-explore'].includes(lifecycle.value),
)
const _NEXT_STEP = {
  registered: 'explore the site, then answer its questionnaire in the Questions tab.',
  exploring: 'review what was discovered, then answer the questionnaire below.',
  'awaiting-answers': 'answer the required questions in the Questions tab below.',
  configured: "you're ready — launch a run from New Run.",
  testing: 'personas are running — review what they find under Runs.',
  're-explore': 'run discovery again to refresh this site model.',
}
const nextStepHint = computed(() => _NEXT_STEP[lifecycle.value] || '')

const tab = ref('surfaces')
const TABS = computed(() => [
  { id: 'surfaces', label: 'Surfaces', count: surfaces.value.length },
  { id: 'flows', label: 'Flows', count: flows.value.length },
  { id: 'knowledge', label: 'Knowledge', count: knowledge.value.length },
  { id: 'questions', label: 'Questions', count: questions.value.length },
  { id: 'capabilities', label: 'Capabilities', count: capView.value.depth ? capView.value.depth.granted_count : 0 },
])

function byKind(kind) {
  return knowledge.value.filter((k) => k.kind === kind)
}

// Categories in first-seen order (the explorer's ordering, preserved by the
// API's (order, question_id) sort).
const questionCategories = computed(() => {
  const seen = []
  for (const q of questions.value) {
    if (!seen.includes(q.category)) seen.push(q.category)
  }
  return seen
})
function byCategory(cat) {
  return questions.value.filter((q) => q.category === cat)
}

async function refreshKnowledge() {
  knowledge.value = await listSiteKnowledge(props.targetId)
}

async function refreshQuestions() {
  const data = await listSiteQuestions(props.targetId)
  questions.value = data.questions || []
  qStatus.value = data.status || qStatus.value
  lifecycle.value = data.lifecycle || lifecycle.value
  lifecycleStates.value = data.lifecycle_states || lifecycleStates.value
}

async function reloadModel() {
  const [t, s, f, k] = await Promise.all([
    getSiteTarget(props.targetId),
    listSiteSurfaces(props.targetId),
    listSiteFlows(props.targetId),
    listSiteKnowledge(props.targetId),
    refreshQuestions(),
    refreshCapabilities(),
  ])
  target.value = t
  surfaces.value = s
  flows.value = f
  knowledge.value = k
}

// ── explorer ──
const exploring = ref(false)
const exploreError = ref('')
const exploreSummary = ref(null)
async function explore() {
  exploring.value = true
  exploreError.value = ''
  exploreSummary.value = null
  try {
    exploreSummary.value = await exploreSiteTarget(props.targetId)
    await reloadModel()
    tab.value = 'questions' // land on the freshly-generated questionnaire
  } catch (e) {
    exploreError.value =
      e?.response?.data?.detail || e.message || 'Exploration failed'
  } finally {
    exploring.value = false
  }
}

// ── capabilities ──
const capView = ref({ depth: null, capabilities: [] })
const capError = ref('')
const busyCapId = ref(null)
const connecting = reactive({})
const customOpen = ref(false)
const customForm = reactive({ title: '', unlocks: '', token: '' })

async function refreshCapabilities() {
  capView.value = await getSiteCapabilities(props.targetId)
}
const capLevels = computed(() => {
  const seen = [...new Set(capView.value.capabilities.map((c) => c.level))]
  return seen.sort((a, b) => a - b)
})
// Rungs above this are sensitive infra — collapsed behind "Advanced access".
const MAX_VISIBLE_LEVEL = 3
const advancedOpen = ref(false)
const firstAdvancedLevel = computed(
  () => capLevels.value.find((l) => l > MAX_VISIBLE_LEVEL) ?? null,
)
// The explorer's tailored shortlist — surfaced in its own "Suggested" section,
// so it's excluded from the full ladder below to avoid showing twice.
const proposedCaps = computed(() =>
  capView.value.capabilities.filter((c) => c.status === 'proposed'),
)
function capsByLevel(lvl) {
  return capView.value.capabilities.filter(
    (c) => c.level === lvl && c.status !== 'proposed',
  )
}
function riskClass(risk) {
  return {
    'write-control': 'text-rose-400',
    'prod-read': 'text-amber-400',
    'read-only': 'text-ink-400',
    'sandbox-only': 'text-emerald-400',
  }[risk] || 'text-ink-400'
}
function connectPlaceholder(cap) {
  if (cap.grant_kind === 'secret') return 'Paste the credential (vaulted)'
  if (cap.grant_kind === 'url') return 'https://…'
  return 'Connection detail / URL'
}
function startConnect(cap) {
  capError.value = ''
  if (cap.grant_kind === 'none') return setStatus(cap, 'granted')
  connecting[cap.capability_id] = ''
}
function cancelConnect(cap) {
  delete connecting[cap.capability_id]
}
async function _apply(capabilityId, payload, busy) {
  busyCapId.value = busy
  capError.value = ''
  try {
    capView.value = await setCapability(props.targetId, capabilityId, payload)
    delete connecting[capabilityId]
  } catch (e) {
    capError.value = e?.response?.data?.detail || e.message || 'Could not update capability'
  } finally {
    busyCapId.value = null
  }
}
function saveConnect(cap) {
  const token = String(connecting[cap.capability_id] || '').trim()
  return _apply(cap.capability_id, { status: 'granted', token: token || undefined }, cap.capability_id)
}
function setStatus(cap, status) {
  return _apply(cap.capability_id, { status }, cap.capability_id)
}
async function revokeCap(cap) {
  busyCapId.value = cap.capability_id
  try {
    capView.value = await revokeCapability(props.targetId, cap.capability_id)
  } catch (e) {
    capError.value = e?.response?.data?.detail || e.message || 'Could not revoke'
  } finally {
    busyCapId.value = null
  }
}
async function addCustom() {
  if (!customForm.title.trim()) return
  busyCapId.value = 'custom'
  capError.value = ''
  try {
    capView.value = await addCustomCapability(props.targetId, {
      title: customForm.title.trim(),
      unlocks: customForm.unlocks.trim(),
      token: customForm.token.trim() || undefined,
    })
    customForm.title = ''
    customForm.unlocks = ''
    customForm.token = ''
    customOpen.value = false
  } catch (e) {
    capError.value = e?.response?.data?.detail || e.message || 'Could not add capability'
  } finally {
    busyCapId.value = null
  }
}

onMounted(async () => {
  try {
    const [t, s, f, k] = await Promise.all([
      getSiteTarget(props.targetId),
      listSiteSurfaces(props.targetId),
      listSiteFlows(props.targetId),
      listSiteKnowledge(props.targetId),
      refreshQuestions(),
      refreshCapabilities(),
    ])
    target.value = t
    surfaces.value = s
    flows.value = f
    knowledge.value = k
  } catch (e) {
    loadError.value =
      e?.response?.data?.detail || e.message || 'Failed to load the site model'
  }
})

// ── questionnaire answering ──
const drafts = reactive({})
const busyId = ref(null)
const questionsError = ref('')

function statusClass(status) {
  return {
    answered: 'text-emerald-400',
    skipped: 'text-ink-400',
    open: 'text-amber-400',
  }[status] || ''
}

function placeholderFor(q) {
  if (q.kind === 'url') return 'https://…'
  if (q.kind === 'number') return '123'
  if (q.kind === 'secret') return '••••••••'
  return 'Your answer'
}

function startAnswer(q) {
  questionsError.value = ''
  drafts[q.question_id] = ''
}

function cancelAnswer(q) {
  delete drafts[q.question_id]
}

async function submitAnswer(q) {
  const answer = String(drafts[q.question_id] || '').trim()
  if (!answer) return
  busyId.value = q.question_id
  questionsError.value = ''
  try {
    await answerSiteQuestion(props.targetId, q.question_id, answer)
    delete drafts[q.question_id]
    await refreshQuestions()
  } catch (e) {
    questionsError.value =
      e?.response?.data?.detail || e.message || 'Failed to save answer'
  } finally {
    busyId.value = null
  }
}

async function skip(q) {
  busyId.value = q.question_id
  questionsError.value = ''
  try {
    await skipSiteQuestion(props.targetId, q.question_id)
    await refreshQuestions()
  } catch (e) {
    questionsError.value =
      e?.response?.data?.detail || e.message || 'Failed to skip question'
  } finally {
    busyId.value = null
  }
}

async function changeLifecycle(next) {
  if (!next || next === lifecycle.value) return
  questionsError.value = ''
  const prev = lifecycle.value
  lifecycle.value = next
  try {
    await setTargetLifecycle(props.targetId, next)
  } catch (e) {
    lifecycle.value = prev
    questionsError.value =
      e?.response?.data?.detail || e.message || 'Failed to set lifecycle'
  }
}

// ── curation ──
const editing = ref(null)
const appliesToText = ref('')
const saving = ref(false)
const deletingId = ref(null)
const curateError = ref('')

function startCreate() {
  curateError.value = ''
  appliesToText.value = ''
  editing.value = { entry_id: null, kind: 'by_design', body: '' }
  tab.value = 'knowledge'
}

function startEdit(entry) {
  curateError.value = ''
  appliesToText.value = (entry.applies_to || []).join(', ')
  editing.value = {
    entry_id: entry.entry_id,
    kind: entry.kind || 'by_design',
    body: entry.body || '',
  }
}

function parseAppliesTo() {
  return appliesToText.value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}

async function save() {
  saving.value = true
  curateError.value = ''
  try {
    const applies_to = parseAppliesTo()
    if (editing.value.entry_id) {
      await updateSiteKnowledge(editing.value.entry_id, props.targetId, {
        body: editing.value.body,
        kind: editing.value.kind,
        applies_to,
      })
    } else {
      await createSiteKnowledge(props.targetId, {
        body: editing.value.body,
        kind: editing.value.kind,
        applies_to,
      })
    }
    editing.value = null
    await refreshKnowledge()
  } catch (e) {
    curateError.value =
      e?.response?.data?.detail || e.message || 'Failed to save entry'
  } finally {
    saving.value = false
  }
}

async function remove(entry) {
  if (!window.confirm(`Delete this ${entry.kind} entry?`)) return
  deletingId.value = entry.entry_id
  curateError.value = ''
  try {
    await deleteSiteKnowledge(entry.entry_id, props.targetId)
    await refreshKnowledge()
  } catch (e) {
    curateError.value =
      e?.response?.data?.detail || e.message || 'Failed to delete entry'
  } finally {
    deletingId.value = null
  }
}
</script>
