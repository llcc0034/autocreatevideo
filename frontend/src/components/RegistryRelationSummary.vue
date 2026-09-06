<script setup lang="ts">
import type { RelationRow } from '@/services/registryRelations'
import { Boxes, Server, Workflow, BookOpen, SlidersHorizontal, GitBranch, Route, Info, ArrowRight } from 'lucide-vue-next'
import ConfigurationRelationButton from './ConfigurationRelationButton.vue'

const icons = { model: Boxes, deployment: Server, workflow: Workflow, prompt: BookOpen, binding: SlidersHorizontal, projector: GitBranch, recipe: Route }
defineProps<{ row?: RelationRow; loading?: boolean; error?: string; expanded?: boolean; condensed?: boolean }>()
defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="registry-relations" :class="{condensed}" aria-label="关联配置摘要" :aria-busy="loading">
    <span v-if="loading" class="muted">关联信息读取中…</span>
    <span v-else-if="error" role="alert">{{ error }} <button class="button ghost tiny" type="button" @click="$emit('retry')">重试</button></span>
    <template v-else-if="row">
      <RouterLink v-if="condensed" class="button ghost tiny" :to="row.overview_url" :title="`${row.label} · 查看所有关联配置`">更多关联</RouterLink>
      <div v-else class="relation-chips">
        <ConfigurationRelationButton v-for="group in row.groups" :key="group.key" :to="group.url"
          :label="group.label" :status="`${group.count} 项`" :tone="group.count ? 'info' : 'muted'" :icon="icons[group.kind]"
          :description="`查看${row.label}的${group.label}；数量包含停用或历史配置，不代表验收结果。`"/>
        <RouterLink v-if="!expanded" class="button ghost tiny relation-all" :to="row.overview_url">全部关联<ArrowRight :size="13" aria-hidden="true"/></RouterLink>
      </div>
      <div v-if="expanded&&row.notices.length&&row.status==='retired'" class="relation-notices">
        <p class="notice-preview"><Info :size="14" aria-hidden="true"/><span>{{ row.notices[0] }}</span></p>
        <details v-if="row.notices.length>1"><summary>更多配置说明（{{ row.notices.length-1 }}）</summary><ul><li v-for="notice in row.notices.slice(1)" :key="notice">{{ notice }}</li></ul></details>
      </div>
      <ul v-else-if="expanded&&row.notices.length" class="current-notices"><li v-for="notice in row.notices" :key="notice">{{ notice }}</li></ul>
      <details v-else-if="!condensed && row.notices.length"><summary>关联说明</summary><ul><li v-for="notice in row.notices" :key="notice">{{ notice }}</li></ul></details>
    </template>
    <span v-else class="muted">关联信息尚未读取，不代表没有关联。</span>
  </div>
</template>

<style scoped>
.registry-relations.condensed{padding:0}.condensed>.button{white-space:nowrap}
.registry-relations{padding:8px 0;min-width:0;font-size:11px;line-height:1.6;color:var(--text-secondary)}
.relation-chips{display:flex;flex-wrap:wrap;gap:6px}
.relation-chips>.configuration-relation{min-width:150px;max-width:280px}
.relation-all{min-height:36px;gap:6px}
.relation-all:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.relation-notices{display:grid;gap:4px;margin-top:9px}
.notice-preview{display:flex;align-items:flex-start;gap:6px;margin:0;overflow-wrap:anywhere}
.notice-preview svg{flex:none;margin-top:2px;color:var(--text-secondary)}
.current-notices{margin-top:9px}
details{margin-top:5px}summary{cursor:pointer;color:var(--text-secondary);min-height:28px;line-height:28px}
ul{margin:6px 0;padding-left:20px;overflow-wrap:anywhere}.muted{color:var(--text-secondary)}
@media(max-width:620px){.relation-chips>.configuration-relation{flex:1 1 150px;max-width:100%}}
</style>
