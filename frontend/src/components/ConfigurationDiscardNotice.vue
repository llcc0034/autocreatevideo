<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
defineProps<{ message: string; busy: boolean; actionLabel?: string; title?: string }>()
defineEmits<{ keep: []; discard: [] }>()
const root = ref<HTMLElement | null>(null)
let returnTo: HTMLElement | null = null
onMounted(() => { returnTo = document.activeElement instanceof HTMLElement ? document.activeElement : null; root.value?.focus(); root.value?.scrollIntoView({ block: 'nearest' }) })
onBeforeUnmount(() => { void nextTick(() => { if (returnTo?.isConnected) returnTo.focus({ preventScroll: true }) }) })
</script>
<template>
  <section ref="root" class="configuration-discard-notice" role="alert" tabindex="-1" :aria-label="title || '关闭配置确认'">
    <strong>{{ title || '关闭前请确认' }}</strong><p>{{ message }}</p>
    <div><button type="button" class="button ghost small" :disabled="busy" @click="$emit('keep')">继续编辑</button><button type="button" class="button danger small" :disabled="busy" @click="$emit('discard')">{{ actionLabel || '放弃本地内容并关闭' }}</button></div>
  </section>
</template>
<style scoped>
.configuration-discard-notice{padding:var(--config-space-lg);border:1px solid var(--accent);border-radius:8px;margin-bottom:var(--config-space-lg);font-size:var(--config-body-size);line-height:1.6;overflow-wrap:anywhere}.configuration-discard-notice p{margin:var(--config-space-md) 0}.configuration-discard-notice>div{display:flex;gap:var(--config-space-sm);flex-wrap:wrap}.configuration-discard-notice button{font:500 var(--config-body-size)/1.5 var(--font);min-height:var(--config-control-height);max-width:100%;white-space:normal}.configuration-discard-notice:focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:640px){.configuration-discard-notice{--config-control-height:44px}}
</style>
