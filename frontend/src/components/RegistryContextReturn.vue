<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { relationKinds, relationOverviewUrl, type RelationKind } from '@/services/registryRelations'
import { readRegistryTrail } from '@/services/registryHierarchy'
const route=useRoute(), router=useRouter()
const target=computed(()=>{
  const [kind,id]=String(route.query.relation_context || '').split(':')
  if (!kind || !Object.prototype.hasOwnProperty.call(relationKinds,kind) || !id || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(id)) return null
  return {...router.resolve(relationOverviewUrl(kind as RelationKind,id)),state:{registryTrail:readRegistryTrail(router.options.history.state.registryTrail).map(entry=>({...entry}))}}
})
</script>
<template><div v-if="target" class="registry-context-return"><RouterLink class="button ghost small" :to="target">返回关联总览</RouterLink><small>保留原下钻路径；配置仍在此对象的唯一维护页处理。</small></div></template>
<style scoped>.registry-context-return{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.registry-context-return small{color:var(--text-secondary);font-size:12px}</style>
