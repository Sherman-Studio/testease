<template>
  <div class="mx-auto max-w-7xl p-6">
    <header class="mb-5 flex items-end justify-between">
      <div>
        <h1>Discovered</h1>
        <p class="mt-1 text-sm text-ink-500">
          The coverage map of the target site — everything personas have
          learned it can do, accumulated run by run. Each row is one
          coverage observation from one run.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <label class="flex items-center gap-1.5 text-xs text-ink-600">
          Category
          <select v-model="category" class="select w-auto">
            <option value="">all</option>
            <option v-for="c in CATEGORIES" :key="c" :value="c">{{ c }}</option>
          </select>
        </label>
        <input
          v-model="searchText"
          class="input w-56 text-xs"
          placeholder="Search description, evidence…"
        />
      </div>
    </header>

    <!-- Tabs: actions / tools / branches -->
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
        :data-testid="`tab-${t.id}`"
        @click="activeTab = t.id"
      >
        {{ t.label }}
        <span class="ml-1 text-xs text-ink-400">{{ t.count }}</span>
      </button>
    </div>

    <p v-if="loading" class="text-sm text-ink-500">Loading…</p>
    <p v-else-if="error" class="text-sm text-red-400">{{ error }}</p>

    <!-- Actions tab -->
    <div v-else-if="activeTab === 'actions'">
      <p v-if="!filteredActions.length" class="panel panel-pad text-sm text-ink-500">
        No discovered actions yet — distillation runs at the end of each
        persona run. New runs will start populating this view.
      </p>
      <div v-for="a in filteredActions" :key="a.doc_id" class="panel mb-2 p-4">
        <div class="flex flex-wrap items-center gap-2">
          <span class="pill" :class="`tint-${categoryTint(a.category)}`">
            {{ a.category }}
          </span>
          <code class="rounded bg-amber-500/10 px-2 py-0.5 font-mono text-xs text-amber-300">
            {{ a.action_id }}
          </code>
          <span class="text-xs text-ink-500">
            · <router-link :to="`/personas/${a.persona_id}`">{{ a.persona_id }}</router-link>
            · <router-link :to="`/runs/${a.run_id}`" class="font-mono">{{ a.run_id }}</router-link>
          </span>
        </div>
        <p class="mt-2 text-sm text-ink-800">{{ a.human_description }}</p>
        <p v-if="a.url_seen" class="mt-1 font-mono text-xs text-ink-500">
          url: {{ a.url_seen }}
        </p>
        <p v-if="a.evidence" class="mt-1 text-xs italic text-ink-600">
          "{{ a.evidence }}"
        </p>
        <div v-if="a.branches_noticed?.length" class="mt-2">
          <p class="text-xs font-medium uppercase tracking-wide text-ink-400">
            Branches noticed
          </p>
          <ul class="mt-1 list-disc space-y-0.5 pl-5 text-xs text-ink-700">
            <li v-for="(b, i) in a.branches_noticed" :key="i">{{ b }}</li>
          </ul>
        </div>
      </div>
    </div>

    <!-- Tools tab -->
    <div v-else-if="activeTab === 'tools'">
      <p class="mb-3 text-xs text-ink-500">
        Tool calls are the raw <code class="font-mono">mcp_*</code> names
        personas actually invoked in runs; the catalog of available servers
        lives under
        <router-link to="/mcp-tools" class="text-brand-700 hover:text-brand-800">MCP tools</router-link>.
      </p>
      <p v-if="!filteredTools.length" class="panel panel-pad text-sm text-ink-500">
        No tool discoveries yet.
      </p>
      <div v-for="t in filteredTools" :key="t.doc_id" class="panel mb-2 p-3 text-sm">
        <code class="font-mono text-xs">{{ t.name }}</code>
        <span class="ml-3 text-ink-700">{{ t.purpose }}</span>
        <span class="ml-3 text-xs text-ink-400">
          <router-link :to="`/personas/${t.persona_id}`">{{ t.persona_id }}</router-link>
        </span>
      </div>
    </div>

    <!-- Branches tab -->
    <div v-else-if="activeTab === 'branches'">
      <p v-if="!filteredBranches.length" class="panel panel-pad text-sm text-ink-500">
        No unexplored branches recorded yet.
      </p>
      <div v-for="b in filteredBranches" :key="b.doc_id" class="panel mb-2 p-3 text-sm">
        <p class="text-ink-800">{{ b.description }}</p>
        <p class="mt-1 text-xs text-ink-400">
          <router-link :to="`/personas/${b.persona_id}`">{{ b.persona_id }}</router-link>
          · <router-link :to="`/runs/${b.run_id}`" class="font-mono">{{ b.run_id }}</router-link>
        </p>
      </div>
    </div>

  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import {
  listDiscoveredActions,
  listDiscoveredBranches,
  listDiscoveredTools,
} from '../api.js'

const CATEGORIES = [
  'auth', 'billing', 'agents', 'playground',
  'account', 'contact', 'docs', 'admin', 'other',
]

// Map our category names to the existing 12-tint palette so category
// pills look at home next to persona avatars. Reused below.
const _CAT_TINT = {
  auth: 'indigo', billing: 'emerald', agents: 'fuchsia',
  playground: 'sky', account: 'violet', contact: 'amber',
  docs: 'cyan', admin: 'rose', other: 'slate',
}
function categoryTint(cat) {
  return _CAT_TINT[cat] || 'slate'
}

const actions = ref([])
const tools = ref([])
const branches = ref([])
const loading = ref(true)
const error = ref('')

const activeTab = ref('actions')
const category = ref('')
const searchText = ref('')

const TABS = computed(() => [
  { id: 'actions', label: 'Actions', count: actions.value.length },
  { id: 'tools', label: 'Tool calls', count: tools.value.length },
  { id: 'branches', label: 'Unexplored', count: branches.value.length },
])

const filteredActions = computed(() => {
  const q = searchText.value.trim().toLowerCase()
  if (!q) return actions.value
  return actions.value.filter((a) =>
    `${a.action_id} ${a.human_description} ${a.evidence}`
      .toLowerCase()
      .includes(q),
  )
})

const filteredTools = computed(() => {
  const q = searchText.value.trim().toLowerCase()
  if (!q) return tools.value
  return tools.value.filter((t) =>
    `${t.name} ${t.purpose}`.toLowerCase().includes(q),
  )
})

const filteredBranches = computed(() => {
  const q = searchText.value.trim().toLowerCase()
  if (!q) return branches.value
  return branches.value.filter((b) =>
    (b.description || '').toLowerCase().includes(q),
  )
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [a, t, b] = await Promise.all([
      listDiscoveredActions({ category: category.value || undefined, limit: 2000 }),
      listDiscoveredTools({ limit: 2000 }),
      listDiscoveredBranches({ limit: 2000 }),
    ])
    actions.value = a.actions || []
    tools.value = t.tools || []
    branches.value = b.branches || []
  } catch (e) {
    error.value = e?.response?.data?.detail || e.message || 'Failed to load'
  } finally {
    loading.value = false
  }
}

// Re-fetch when the category server-side filter changes. The text box
// is purely client-side filtering of already-loaded rows.
watch(category, load)
onMounted(load)
</script>
