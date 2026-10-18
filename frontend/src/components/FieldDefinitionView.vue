<script setup lang="ts">
import {computed} from 'vue'
import type {FieldDefinitionVersion} from '@/services/fieldDefinitionVersions'
import {fieldVersionDetails} from '@/services/registryResourceDetails'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
const props=defineProps<{definition:FieldDefinitionVersion}>()
const detail=computed(()=>fieldVersionDetails(props.definition))
</script>
<template>
  <div class="field-definition">
    <section v-for="section in detail.sections" :key="section.title"><h3>{{section.title}}</h3><p>{{section.description}}</p><dl><template v-for="fact in section.facts" :key="fact.label"><dt>{{fact.label}}</dt><dd><ReadonlyDataValue :value="fact.value" /></dd></template></dl></section>
    <details><summary>类型样例与精确标识（只读）</summary><p>服务器提供的类型样例，不是默认值或实际业务内容。</p><ReadonlyDataValue :value="definition.preview_example" /><ReadonlyDataValue :value="{'定义版本':definition.id,'字段身份':definition.field_definition}" /></details>
  </div>
</template>
<style scoped>
.field-definition{display:grid;gap:var(--config-space-lg);min-width:0;font:400 var(--config-body-size)/1.6 var(--font)}.field-definition section{border-top:1px solid var(--border);padding-top:var(--config-space-md)}.field-definition h3{font-size:var(--config-section-size);margin:0}.field-definition p{color:var(--text-secondary);margin:var(--config-space-sm) 0}.field-definition dl{display:grid;grid-template-columns:minmax(100px,1fr) minmax(0,2fr);gap:var(--config-space-sm);margin:0}.field-definition dd{margin:0;min-width:0;overflow-wrap:anywhere}.field-definition dt{color:var(--text-secondary)}.field-definition summary{min-height:44px;align-content:center;cursor:pointer;font-size:var(--config-help-size)}@media(max-width:640px){.field-definition dl{grid-template-columns:1fr}}
</style>
