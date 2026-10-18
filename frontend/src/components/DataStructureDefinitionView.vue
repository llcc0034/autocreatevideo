<script setup lang="ts">
import { computed } from 'vue'
import type { DataStructureDetail } from '@/services/dataStructures'
import { structureVersionDetails } from '@/services/registryResourceDetails'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
const props=defineProps<{definition:DataStructureDetail}>()
const details=computed(()=>structureVersionDetails(props.definition))
</script>
<template>
  <section class="structure-definition" aria-label="已保存结构定义">
    <section v-for="section in details.sections" :key="section.title"><h3>{{section.title}}</h3><p>{{section.description}}</p><dl><div v-for="(fact,index) in section.facts" :key="index"><dt>{{fact.label}}</dt><dd><ReadonlyDataValue :value="fact.value" :label="fact.label" /></dd></div></dl><p v-if="!section.facts.length">此版本没有节点；空草稿不代表可启用。</p></section>
  </section>
</template>
<style scoped>
.structure-definition,.structure-definition section{display:grid;gap:var(--config-space-md);min-width:0}.structure-definition h3{font-size:var(--config-section-size);margin:0}.structure-definition p{margin:0;color:var(--text-secondary)}.structure-definition dl{margin:0;display:grid;gap:var(--config-space-md)}.structure-definition dt{font-weight:600}.structure-definition dd{margin:var(--config-space-xs) 0 0;min-width:0;overflow-wrap:anywhere}
</style>
