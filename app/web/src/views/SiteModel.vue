<template>
  <div class="mx-auto max-w-5xl p-6">
    <header class="mb-5 flex items-start justify-between gap-4">
      <div class="min-w-0">
        <h1 class="flex items-center gap-2">
          Sites
          <HelpTip label="Site">
            A <strong>site</strong> is a website you want Test Ease to test.
            Each one gets its own <strong>site model</strong> — everything the
            tool learns about it (its pages, the journeys to test, and notes on
            what's intentional) — plus a questionnaire you answer to configure
            the testers.
          </HelpTip>
        </h1>
        <p class="mt-1 max-w-2xl text-sm text-ink-600">
          Test Ease points a library of fictional users at your website and
          reports what a real user would hit. Start by adding a site.
        </p>
        <p class="mt-2 text-xs text-ink-500">
          How it works:
          <span class="text-ink-400">1</span> Add a site →
          <span class="text-ink-400">2</span> Answer its questionnaire →
          <span class="text-ink-400">3</span> Run the personas →
          <span class="text-ink-400">4</span> Review what they found.
        </p>
      </div>
      <button
        class="btn-primary btn shrink-0"
        data-testid="add-site"
        @click="openModal"
      >
        + Add a site
      </button>
    </header>

    <div v-if="loading" class="text-sm text-ink-600">Reading the map…</div>
    <div v-else-if="error" class="text-sm text-red-400">{{ error }}</div>

    <div
      v-else-if="!targets.length"
      class="panel panel-pad text-center text-sm text-ink-600"
    >
      <p class="mb-3">No sites yet — add the website you want to test.</p>
      <button class="text-brand-700 underline" data-testid="add-first-site" @click="openModal">
        Add your first site →
      </button>
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
        <span
          v-if="t.lifecycle"
          class="pill text-[10px] uppercase tracking-wide text-brand-700"
          :title="lifecycleHint(t.lifecycle)"
        >
          {{ t.lifecycle }}
        </span>
        <span class="text-ink-400" aria-hidden="true">→</span>
      </router-link>
    </div>

    <!-- Register-a-site modal -->
    <div
      v-if="modalOpen"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      data-testid="add-site-modal"
    >
      <div class="panel w-full max-w-lg p-5 shadow-xl">
        <h3 class="text-lg font-semibold text-ink-900">Add a site</h3>
        <p class="help mt-1">
          Point Test Ease at a website. We'll register it and you can configure
          how the personas test it next.
        </p>
        <div class="mt-4">
          <label class="label">Website URL</label>
          <input
            v-model="form.base_url"
            class="input"
            placeholder="https://app.example.com"
            data-testid="add-site-url"
            @keyup.enter="submit"
          />
        </div>
        <div class="mt-3">
          <label class="label">Display name <span class="text-ink-400">(optional)</span></label>
          <input
            v-model="form.display_name"
            class="input"
            placeholder="e.g. Example App"
            data-testid="add-site-name"
            @keyup.enter="submit"
          />
        </div>
        <div v-if="formError" class="mt-3 text-sm text-red-400" data-testid="add-site-error">
          {{ formError }}
        </div>
        <div class="mt-4 flex justify-end gap-2">
          <button class="btn" :disabled="creating" @click="closeModal">Cancel</button>
          <button
            class="btn-primary btn"
            :disabled="creating || !form.base_url.trim()"
            data-testid="add-site-submit"
            @click="submit"
          >
            {{ creating ? 'Adding…' : 'Add site' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { createSiteTarget, listSiteTargets } from '../api.js'
import HelpTip from '../components/HelpTip.vue'

const router = useRouter()
const targets = ref([])

// One-line meaning for each onboarding state (the list's status badge tooltip).
const _LIFECYCLE_HINTS = {
  registered: 'Added, not yet explored — open it and run Explore.',
  exploring: 'The explorer is probing the site.',
  'awaiting-answers': 'Explored — waiting for you to answer its questionnaire.',
  configured: 'Configured and ready to run the personas.',
  testing: 'Persona runs are happening.',
  're-explore': 'Marked for another discovery pass.',
}
function lifecycleHint(state) {
  return _LIFECYCLE_HINTS[state] || 'Onboarding status'
}
const loading = ref(true)
const error = ref('')

const modalOpen = ref(false)
const creating = ref(false)
const formError = ref('')
const form = reactive({ base_url: '', display_name: '' })

function openModal() {
  formError.value = ''
  form.base_url = ''
  form.display_name = ''
  modalOpen.value = true
}
function closeModal() {
  modalOpen.value = false
}

async function submit() {
  const base_url = form.base_url.trim()
  if (!base_url) return
  creating.value = true
  formError.value = ''
  try {
    const t = await createSiteTarget({
      base_url,
      display_name: form.display_name.trim(),
    })
    modalOpen.value = false
    router.push(`/site/${encodeURIComponent(t.target_id)}`)
  } catch (e) {
    formError.value =
      e?.response?.data?.detail || e.message || 'Could not add the site'
  } finally {
    creating.value = false
  }
}

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
