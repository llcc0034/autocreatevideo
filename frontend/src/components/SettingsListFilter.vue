<script setup lang="ts">
import { Search } from 'lucide-vue-next'

defineProps<{
  search: string
  showRetired: boolean
  visibleCount: number
  totalCount: number
  hiddenRetiredCount: number
  placeholder?: string
}>()

const emit = defineEmits<{
  'update:search': [value: string]
  'update:showRetired': [value: boolean]
}>()
</script>

<template>
  <section class="settings-list-filter" aria-label="设置列表筛选">
    <label class="settings-list-search">
      <Search :size="14"/>
      <input
        :value="search"
        type="search"
        :placeholder="placeholder || '搜索名称、机器键、类型、说明或状态'"
        aria-label="搜索当前设置分区"
        @input="emit('update:search', ($event.target as HTMLInputElement).value)"
      >
    </label>
    <label class="settings-list-retired-toggle">
      <input
        :checked="showRetired"
        type="checkbox"
        @change="emit('update:showRetired', ($event.target as HTMLInputElement).checked)"
      >
      <span>显示已退役/停用记录（Include retired/inactive）</span>
    </label>
    <small>
      当前显示 {{ visibleCount }} / {{ totalCount }} 项
      <template v-if="!showRetired && hiddenRetiredCount">；默认隐藏 {{ hiddenRetiredCount }} 项已退役/停用记录</template>
    </small>
  </section>
</template>

<style scoped>
.settings-list-filter{padding:9px 11px;display:grid;grid-template-columns:minmax(260px,1fr) auto;align-items:center;gap:8px;border:1px solid var(--border);border-radius:8px;background:#0c0f13}.settings-list-search{min-width:0;display:flex;align-items:center;gap:7px;padding:0 9px;border:1px solid var(--border);border-radius:6px;background:#080b0e;color:#6f7881}.settings-list-search input{min-width:0;width:100%;height:34px;border:0;background:transparent;color:#aeb5bc;font-size:9px;outline:0}.settings-list-retired-toggle{display:flex;align-items:center;gap:6px;color:#8b949d;font-size:9px;white-space:nowrap}.settings-list-filter>small{grid-column:1/-1;color:#68717a;font-size:8px}
@media(max-width:720px){.settings-list-filter{grid-template-columns:1fr}.settings-list-filter>small{grid-column:1}.settings-list-retired-toggle{white-space:normal}}
</style>
