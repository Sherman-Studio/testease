<template>
  <div
    class="group flex items-start gap-2 rounded-md border border-ink-200 bg-panel p-2 text-xs shadow-sm"
    :class="[
      draggable ? 'cursor-grab active:cursor-grabbing' : '',
      'hover:border-brand-400',
    ]"
    :title="action.human_description"
  >
    <code class="rounded bg-amber-500/10 px-1.5 py-0.5 font-mono text-[10px] text-amber-300">
      {{ action.id }}
    </code>
    <span class="flex-1 text-ink-700">{{ action.human_description }}</span>
    <button
      v-if="removable"
      type="button"
      class="rounded text-ink-400 opacity-0 transition group-hover:opacity-100 hover:text-red-400"
      @click="$emit('remove')"
    >
      ✕
    </button>
  </div>
</template>

<script setup>
defineProps({
  action: { type: Object, required: true },
  draggable: { type: Boolean, default: false },
  removable: { type: Boolean, default: false },
})
defineEmits(['remove'])
</script>
