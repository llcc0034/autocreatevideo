<script setup lang="ts">
import { computed, ref } from 'vue'

const props = withDefaults(defineProps<{ value: unknown; label?: string }>(), { label: '结构化内容' })
const expanded = ref(false)
const structured = computed(() => props.value !== null && typeof props.value === 'object')
const kind = computed(() => Array.isArray(props.value) ? '列表' : '字段集合')
const count = computed(() => structured.value ? Object.keys(props.value as object).length : 0)
const scalar = computed(() => props.value === undefined ? '字段缺失'
  : props.value === null ? '空值（null）'
  : props.value === '' ? '空文本'
  : typeof props.value === 'boolean' ? (props.value ? '是（true）' : '否（false）') : String(props.value))
// Do not serialize or mount a potentially large diagnostic until it is opened.
const json = computed(() => expanded.value ? JSON.stringify(props.value, null, 2) : '')
</script>

<template>
  <details v-if="structured" class="readonly-data" @toggle="expanded = ($event.target as HTMLDetailsElement).open">
    <summary>{{ label }}<small>{{ kind }} · {{ count }} 项 · 只读</small></summary>
    <pre v-if="expanded" tabindex="0" :aria-label="label">{{ json }}</pre>
  </details>
  <pre v-else class="readonly-scalar" :class="{ 'empty-value': value === null || value === undefined || value === '' }" tabindex="0" :aria-label="label">{{ scalar }}</pre>
</template>

<style scoped>
.readonly-data{min-width:0}.readonly-data summary{cursor:pointer;min-height:var(--config-control-height);align-content:center;line-height:1.7;padding:var(--config-space-xs) 0;font-size:var(--config-body-size);overflow-wrap:anywhere}.readonly-data small{margin-left:var(--config-space-sm);color:var(--text-secondary);font-size:var(--config-help-size);font-weight:400}.readonly-data pre,.readonly-scalar{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;max-height:280px;overflow:auto;min-width:0;max-width:100%;font:inherit;font-size:var(--config-body-size);line-height:1.75;color:var(--text-primary);tab-size:2}.readonly-data pre{padding:var(--config-space-md);background:var(--bg-input,#080d10);border:1px solid var(--border);border-radius:var(--config-radius);font-family:Consolas,monospace;font-size:var(--config-help-size)}.empty-value{color:var(--text-secondary)}.readonly-data summary:focus-visible,.readonly-data pre:focus-visible,.readonly-scalar:focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:640px){.readonly-data{--config-control-height:44px}.readonly-data small{display:block;margin-left:var(--config-space-lg)}}
</style>
