<script setup lang="ts">
import { computed } from 'vue'
import type { ContentSourceVersion } from '@/services/contentSources'
import { contentSourceDetails } from '@/services/registryResourceDetails'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
const props = defineProps<{ source: ContentSourceVersion }>()
defineEmits<{ inspectStructure: [id: string, event: Event] }>()
const presentation = computed(() => {
  try { return { detail: contentSourceDetails(props.source), error: '' } }
  catch (cause) { return { detail: null, error: cause instanceof Error ? cause.message : '读取规则无法展示' } }
})
</script>
<template>
  <div class="source-definition">
    <p v-if="presentation.error" role="alert">{{ presentation.error }}</p>
    <section v-for="section in presentation.detail?.sections" :key="section.title">
      <h3>{{ section.title }}</h3><p>{{ section.description }}</p>
      <dl><template v-for="fact in section.facts" :key="fact.label"><dt>{{ fact.label }}</dt><dd><ReadonlyDataValue :value="fact.value" /></dd></template></dl>
    </section>
    <button v-if="source.data_structure_version" type="button" @click="$emit('inspectStructure', source.data_structure_version, $event)">查看精确输出结构</button>
    <details><summary>实现详情与版本标识</summary><ReadonlyDataValue :value="{ '版本 ID': source.id, '来源族': source.source_key, '修订': source.revision, '合同摘要': source.contract_sha256, '读取合同': source.reader }" /></details>
  </div>
</template>
<style scoped>
.source-definition{display:grid;gap:var(--config-space-lg);font:400 var(--config-body-size)/1.6 var(--font);min-width:0}.source-definition section{padding:var(--config-space-lg);border:1px solid var(--border);border-radius:var(--config-radius);min-width:0}.source-definition h3{font-size:var(--config-section-size);margin:0 0 var(--config-space-sm)}.source-definition p{color:var(--text-secondary);margin:0 0 var(--config-space-md)}.source-definition dl{display:grid;grid-template-columns:minmax(100px,1fr) minmax(0,2fr);gap:var(--config-space-sm);margin:0}.source-definition dd{margin:0;min-width:0;overflow-wrap:anywhere}.source-definition dt{color:var(--text-secondary)}.source-definition button{min-height:44px;background:var(--surface);color:var(--text);border:1px solid var(--border-strong);border-radius:var(--config-radius);padding:var(--config-space-sm);cursor:pointer}.source-definition summary{cursor:pointer;min-height:44px;align-content:center;font-size:var(--config-help-size)}@media(max-width:600px){.source-definition dl{grid-template-columns:1fr}}
</style>
