<script setup lang="ts">
import {computed} from 'vue'
import type {BusinessOutputVersion} from '@/services/businessOutputs'
import {businessOutputDetails} from '@/services/registryResourceDetails'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import ScriptSemanticGuide from './ScriptSemanticGuide.vue'
import type {JsonObject} from '@/services/platformTypes'
const props=defineProps<{definition:BusinessOutputVersion}>()
const details=computed(()=>businessOutputDetails(props.definition))
</script>
<template>
  <section class="output-definition" aria-label="已保存业务输出定义">
    <ScriptSemanticGuide v-if="definition.parser_key==='script_semantics.v1'" :key="definition.id" :mapping="(definition.parser_contract?.mapping??{}) as JsonObject"/>
    <section v-for="section in details.sections" :key="section.title"><h3>{{section.title}}</h3><p>{{section.description}}</p><dl><div v-for="(fact,index) in section.facts" :key="index"><dt>{{fact.label}}</dt><dd><ReadonlyDataValue :value="fact.value" :label="fact.label"/></dd></div></dl></section>
  </section>
</template>
<style scoped>
.output-definition,.output-definition section{display:grid;gap:var(--config-space-md);min-width:0}.output-definition h3{font-size:var(--config-section-size);margin:0}.output-definition p{margin:0;color:var(--text-secondary)}.output-definition dl{margin:0;display:grid;gap:var(--config-space-md)}.output-definition dt{font-weight:600}.output-definition dd{margin:var(--config-space-xs) 0 0;min-width:0;overflow-wrap:anywhere}
</style>
