<template>
  <div class="mx-auto max-w-3xl p-6">
    <header class="mb-5">
      <h1 class="flex items-center gap-2">
        Settings
        <HelpTip label="BYOK">
          Test Ease runs the personas on <strong>your own</strong> Claude
          credentials — bring your own key. Use a <strong>Claude Code
          subscription</strong> token (flat price) or an Anthropic
          <strong>API key</strong> (per-token). The token is stored encrypted in
          the vault; we only ever keep a pointer.
        </HelpTip>
      </h1>
      <p class="mt-1 text-sm text-ink-600">
        Choose how Test Ease talks to Claude and provide the credential it runs
        on. Exploring a site works without this — it's the <strong>AI persona
        runs</strong> (the fictional users actually testing your site) that need
        a credential.
      </p>
    </header>

    <div v-if="loading" class="text-sm text-ink-600">Loading…</div>
    <div v-else class="section">
      <div class="section-head">
        <h2>Claude credentials</h2>
        <span
          class="pill text-[10px]"
          :class="cfg.token_configured ? 'text-emerald-400' : 'text-amber-400'"
          data-testid="token-status"
        >
          {{ statusLabel }}
        </span>
      </div>

      <div class="panel panel-pad space-y-4">
        <!-- Backend choice -->
        <div>
          <label class="label">Backend</label>
          <div class="mt-1 space-y-2">
            <label
              v-for="b in cfg.backends"
              :key="b.id"
              class="flex cursor-pointer items-start gap-2 rounded-md border border-ink-200 p-3 transition hover:border-brand-600"
              :class="{ 'ring-1 ring-brand-600/40': form.backend === b.id }"
            >
              <input
                v-model="form.backend"
                type="radio"
                :value="b.id"
                class="mt-0.5"
                :data-testid="`backend-${b.id}`"
              />
              <span class="min-w-0">
                <span class="text-sm font-medium text-ink-900">
                  {{ b.label }}
                  <span v-if="b.recommended" class="ml-1 text-[10px] text-brand-700">recommended</span>
                </span>
                <span class="mt-0.5 block text-xs text-ink-500">{{ b.hint }}</span>
              </span>
            </label>
          </div>
        </div>

        <!-- Token -->
        <div>
          <label class="label">
            {{ form.backend === 'api' ? 'Anthropic API key' : 'Claude Code OAuth token' }}
          </label>
          <input
            v-model="form.token"
            type="password"
            class="input"
            :placeholder="tokenConfiguredHere ? '•••••••• (saved — leave blank to keep)' : 'Paste your token'"
            data-testid="token-input"
            autocomplete="off"
          />
          <p class="help mt-1">
            Sets the <code>{{ currentEnvVar }}</code> credential.
            <template v-if="cfg.token_source === 'env'">
              A token is currently provided by the <strong>environment</strong> —
              you can override it here.
            </template>
            <template v-else-if="cfg.token_source === 'vault'">
              A token is saved. Leave blank to keep it, or paste a new one to replace it.
            </template>
          </p>
          <p class="help mt-1" data-testid="token-help">
            <template v-if="form.backend === 'api'">
              Where to get this: create an API key in the
              <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer">Anthropic Console</a>
              (billed per token).
            </template>
            <template v-else>
              Where to get this: with the
              <a href="https://docs.anthropic.com/en/docs/claude-code/overview" target="_blank" rel="noreferrer">Claude Code CLI</a>
              installed, run <code>claude setup-token</code> in a terminal and paste
              the result here. Needs a Claude Pro/Max subscription; runs at flat price.
            </template>
          </p>
        </div>

        <div v-if="error" class="text-sm text-red-400" data-testid="settings-error">{{ error }}</div>
        <div v-if="saved" class="text-sm text-emerald-400" data-testid="settings-saved">Saved.</div>

        <div class="flex items-center gap-2">
          <button
            class="btn-primary btn"
            :disabled="saving"
            data-testid="save-config"
            @click="save"
          >
            {{ saving ? 'Saving…' : 'Save' }}
          </button>
          <button
            v-if="cfg.token_source === 'vault'"
            class="btn-ghost btn"
            :disabled="saving"
            data-testid="clear-token"
            @click="clearToken"
          >
            Remove saved token
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { clearLLMToken, getLLMConfig, setLLMConfig } from '../api.js'
import HelpTip from '../components/HelpTip.vue'

const loading = ref(true)
const saving = ref(false)
const saved = ref(false)
const error = ref('')
const cfg = ref({ backend: 'claude-code', backends: [], token_configured: false, token_source: null, env_var: '' })
const form = reactive({ backend: 'claude-code', token: '' })

const statusLabel = computed(() => {
  if (cfg.value.token_source === 'vault') return 'Configured ✓ (saved)'
  if (cfg.value.token_source === 'env') return 'Configured ✓ (from environment)'
  return 'Not configured'
})
const currentEnvVar = computed(() => {
  const b = cfg.value.backends.find((x) => x.id === form.backend)
  return (b && b.env) || cfg.value.env_var || ''
})
// Whether a token is already configured for the currently-selected backend.
const tokenConfiguredHere = computed(
  () => cfg.value.token_configured && form.backend === cfg.value.backend,
)

async function load() {
  cfg.value = await getLLMConfig()
  form.backend = cfg.value.backend
  form.token = ''
}

async function save() {
  saving.value = true
  saved.value = false
  error.value = ''
  try {
    const payload = { backend: form.backend }
    if (form.token.trim()) payload.token = form.token.trim()
    cfg.value = await setLLMConfig(payload)
    form.token = ''
    saved.value = true
  } catch (e) {
    error.value = e?.response?.data?.detail || e.message || 'Could not save settings'
  } finally {
    saving.value = false
  }
}

async function clearToken() {
  saving.value = true
  saved.value = false
  error.value = ''
  try {
    cfg.value = await clearLLMToken()
    form.token = ''
  } catch (e) {
    error.value = e?.response?.data?.detail || e.message || 'Could not remove token'
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    error.value = e?.response?.data?.detail || e.message || 'Failed to load settings'
  } finally {
    loading.value = false
  }
})
</script>
