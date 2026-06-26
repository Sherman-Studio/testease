<template>
  <div class="mx-auto max-w-5xl p-6">
    <div class="mb-4 text-xs">
      <router-link to="/site">← Site Model</router-link>
    </div>

    <div v-if="loadError" class="text-sm text-red-400">{{ loadError }}</div>

    <template v-else>
      <header class="mb-6 flex items-end justify-between gap-4">
        <div class="min-w-0">
          <h1 class="truncate">{{ target?.display_name || targetId }}</h1>
          <p v-if="target?.base_url" class="mt-1 truncate text-sm text-ink-500">
            {{ target.base_url }}
          </p>
        </div>
        <span class="pill text-[10px]">{{ targetId }}</span>
      </header>

      <!-- Surfaces / Flows / Knowledge tabs -->
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
      <section v-else>
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
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  createSiteKnowledge,
  deleteSiteKnowledge,
  getSiteTarget,
  listSiteFlows,
  listSiteKnowledge,
  listSiteSurfaces,
  updateSiteKnowledge,
} from '../api.js'

const props = defineProps({
  targetId: { type: String, required: true },
})

const KINDS = ['by_design', 'known_issue', 'guidance', 'glossary']

const target = ref(null)
const surfaces = ref([])
const flows = ref([])
const knowledge = ref([])
const loadError = ref('')

const tab = ref('surfaces')
const TABS = computed(() => [
  { id: 'surfaces', label: 'Surfaces', count: surfaces.value.length },
  { id: 'flows', label: 'Flows', count: flows.value.length },
  { id: 'knowledge', label: 'Knowledge', count: knowledge.value.length },
])

function byKind(kind) {
  return knowledge.value.filter((k) => k.kind === kind)
}

async function refreshKnowledge() {
  knowledge.value = await listSiteKnowledge(props.targetId)
}

onMounted(async () => {
  try {
    const [t, s, f, k] = await Promise.all([
      getSiteTarget(props.targetId),
      listSiteSurfaces(props.targetId),
      listSiteFlows(props.targetId),
      listSiteKnowledge(props.targetId),
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
