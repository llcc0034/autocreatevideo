<script setup lang="ts" generic="T extends string">
defineProps<{ items: readonly { key: T; label: string; description?: string }[]; modelValue: T; label: string }>()
defineEmits<{ 'update:modelValue': [value: T] }>()
</script>
<template>
  <nav class="studio-tabs production-workspace-tabs" :aria-label="label"><button v-for="item in items" :key="item.key" type="button" :class="{active:modelValue===item.key}" :aria-current="modelValue===item.key?'page':undefined" :title="item.description" @click="$emit('update:modelValue',item.key)">{{item.label}}</button></nav>
</template>
<style scoped>
.production-workspace-tabs.studio-tabs{margin:0;padding:0;height:auto;min-height:52px;display:flex;flex-wrap:wrap;gap:4px;border:0;border-bottom:1px solid var(--border);border-radius:0;background:transparent;z-index:auto}
.production-workspace-tabs.studio-tabs button{background:none;color:var(--text-muted);font:inherit;font-size:14px;font-weight:400;min-height:52px;padding:12px 14px;white-space:normal;cursor:pointer;border:0;border-bottom:2px solid transparent}
.production-workspace-tabs.studio-tabs button.active{color:var(--accent);font-weight:600}
.production-workspace-tabs button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
@media(max-width:640px){.production-workspace-tabs.studio-tabs button{padding:12px}}
</style>
