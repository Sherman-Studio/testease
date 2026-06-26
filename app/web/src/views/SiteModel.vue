<template>
  <div class="mx-auto max-w-5xl p-6">
    <header class="mb-6">
      <h1>Site Model</h1>
      <p class="mt-1 max-w-2xl text-sm text-ink-600">
        What the testers know about each site they test: its surfaces, the
        flows they walk, and the curated by-design knowledge that keeps them
        from re-flagging intentional behaviour. Pick a target to browse and
        curate its model.
      </p>
    </header>

    <div v-if="loading" class="text-sm text-ink-600">Reading the map…</div>
    <div v-else-if="error" class="text-sm text-red-400">{{ error }}</div>

    <div
      v-else-if="!targets.length"
      class="panel panel-pad text-center text-sm text-ink-600"
    >
      <p>
        No targets modelled yet. The Site Model fills in as the harness
        discovers surfaces and the by-design migration lands its first entries.
      </p>
    </div>

    <div v-else class="panel divide-y divide-ink-200/60">
      <router-link
        v-for="t in targets"
        :key="t.target_id"
        :to="`/site/${encodeURIComponent(t.target_id)}`"
        class="flex items-center gap-4 px-5 py-4 transition hover:bg-brand-50/40 hover:no-underline"
      >
        <div class="min-w-0 flex-1">
          <div class="truncate text-sm font-semibold text-ink-900">
            {{ t.display_name || t.target_id }}
          </div>
          <div class="truncate text-xs text-ink-500">
            {{ t.base_url || t.target_id }}
          </div>
        </div>
        <span class="pill text-[10px]">{{ t.target_id }}</span>
        <span class="text-ink-400" aria-hidden="true">→</span>
      </router-link>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { listSiteTargets } from '../api.js'

const targets = ref([])
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  try {
    targets.value = await listSiteTargets()
  } catch (e) {
    error.value = e?.response?.data?.detail || e.message || 'Failed to load targets'
  } finally {
    loading.value = false
  }
})
</script>
