<template>
  <div class="mx-auto max-w-7xl p-6">
    <!--
      Read-only catalog. The intent is "developer reference page" — not
      something an operator changes. The header copy leans into that:
      explains what each chip on the runs page means and lets a future
      tenant see what their persona toolset actually covers.
    -->
    <header class="mb-6">
      <h1>MCP tools at your disposal</h1>
      <p class="mt-1 max-w-3xl text-sm text-ink-600">
        Every persona run can call these MCP servers. Some are universal
        (Playwright is in every browser-based persona's hands), others
        are situational (email round-trip, billing checks). New servers
        land via the
        <code class="rounded bg-ink-100 px-1.5 py-0.5 font-mono text-xs">#1019</code>
        epic; this page is the canonical roster. Toggle servers per run
        from
        <router-link to="/new-run" class="text-brand-700 hover:text-brand-800">New Run</router-link>
        → Advanced → MCP tools.
      </p>
    </header>

    <div v-if="loading" class="text-sm text-ink-600">Loading the catalog…</div>
    <div v-else-if="error" class="text-sm text-red-400">{{ error }}</div>

    <div
      v-else-if="!servers.length"
      class="panel panel-pad mt-4 text-center text-sm text-ink-600"
    >
      <div class="text-4xl">🛠️</div>
      <p class="mt-2">
        The catalog is empty — that almost certainly means the harness
        package isn't installed in the review-ui image (a deploy issue).
        See
        <code class="rounded bg-ink-100 px-1.5 py-0.5 font-mono text-xs">qa_agents/mcp_catalog.py</code>
        in the source.
      </p>
    </div>

    <div v-else class="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
      <div
        v-for="s in servers"
        :key="s.id"
        class="panel panel-pad flex flex-col gap-3"
        :data-testid="`mcp-server-${s.id}`"
      >
        <header class="flex items-start justify-between gap-3">
          <div class="min-w-0 flex-1">
            <div class="font-mono text-xs uppercase tracking-wide text-ink-500">
              {{ s.id }}
            </div>
            <h2 class="mt-1 text-lg font-semibold leading-tight">
              {{ s.display_name }}
            </h2>
          </div>
          <span
            v-if="s.default_enabled"
            class="rounded-full bg-emerald-500/10 px-2 py-0.5 text-[10px] font-medium text-emerald-300 ring-1 ring-emerald-500/30"
            title="Enabled by default for new runs. Toggle servers per run from New Run → Advanced."
          >
            default-on
          </span>
        </header>

        <p class="text-sm leading-relaxed text-ink-700">
          {{ s.description }}
        </p>

        <footer class="mt-auto flex items-center justify-between text-xs text-ink-500">
          <span>
            <strong class="font-semibold text-ink-700">{{ s.tool_count }}</strong>
            tool{{ s.tool_count === 1 ? '' : 's' }}
          </span>
          <span v-if="s.persona_compat && s.persona_compat.length">
            <strong class="font-semibold text-ink-700">{{ s.persona_compat.length }}</strong>
            persona{{ s.persona_compat.length === 1 ? '' : 's' }}
          </span>
          <span v-else class="text-ink-400">
            all personas
          </span>
        </footer>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { listMCPServers } from '../api'

const servers = ref([])
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  try {
    servers.value = await listMCPServers()
  } catch (e) {
    error.value =
      (e.response && e.response.data && e.response.data.detail) || e.message
  } finally {
    loading.value = false
  }
})
</script>
