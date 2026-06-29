<template>
  <div class="panel panel-pad" :data-testid="`cap-${cap.capability_id}`">
    <div class="flex items-start justify-between gap-2">
      <div class="min-w-0">
        <p class="text-sm font-medium text-ink-900">{{ cap.title }}</p>
        <p class="mt-0.5 text-xs text-ink-500">{{ cap.unlocks }}</p>
      </div>
      <span class="pill shrink-0 text-[10px]" :class="riskClass">{{ cap.risk_class }}</span>
    </div>
    <div class="mt-2 flex flex-wrap items-center gap-2">
      <template v-if="cap.status === 'granted'">
        <span class="pill text-[10px] text-emerald-400">granted ✓</span>
        <button class="btn-ghost btn" :disabled="busy" @click="$emit('revoke')">Revoke</button>
      </template>
      <template v-else-if="connecting">
        <input
          v-if="cap.grant_kind !== 'none'"
          v-model="tokenInput"
          :type="cap.grant_kind === 'secret' ? 'password' : 'text'"
          class="input flex-1"
          :placeholder="placeholder"
          :data-testid="`cap-${cap.capability_id}-input`"
        />
        <button
          class="btn-primary btn"
          :disabled="busy"
          :data-testid="`cap-${cap.capability_id}-save`"
          @click="save"
        >
          Grant
        </button>
        <button class="btn-ghost btn" @click="connecting = false">Cancel</button>
      </template>
      <template v-else>
        <button
          class="btn-primary btn"
          :data-testid="`cap-${cap.capability_id}-connect`"
          @click="start"
        >
          Connect
        </button>
        <button
          v-if="cap.status === 'available' || cap.status === 'proposed'"
          class="btn-ghost btn"
          @click="$emit('na')"
        >
          Not applicable
        </button>
        <span
          v-if="cap.status === 'declined' || cap.status === 'not_applicable'"
          class="text-xs text-ink-400"
        >{{ cap.status.replace('_', ' ') }}</span>
      </template>
    </div>
  </div>
</template>

<script setup>
// A single capability card: shows what it unlocks + its risk, and lets the
// operator grant (with a vaulted credential), decline, or revoke. Owns its own
// connect-input state; bubbles the action up to the parent.
import { computed, ref } from 'vue'

const props = defineProps({
  cap: { type: Object, required: true },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['grant', 'na', 'revoke'])

const connecting = ref(false)
const tokenInput = ref('')

const riskClass = computed(() => ({
  'write-control': 'text-rose-400',
  'prod-read': 'text-amber-400',
  'read-only': 'text-ink-400',
  'sandbox-only': 'text-emerald-400',
}[props.cap.risk_class] || 'text-ink-400'))

const placeholder = computed(() => {
  if (props.cap.grant_kind === 'secret') return 'Paste the credential (vaulted)'
  if (props.cap.grant_kind === 'url') return 'https://…'
  return 'Connection detail / URL'
})

function start() {
  if (props.cap.grant_kind === 'none') return emit('grant', null)
  connecting.value = true
}
function save() {
  emit('grant', String(tokenInput.value || '').trim() || null)
  connecting.value = false
  tokenInput.value = ''
}
</script>
