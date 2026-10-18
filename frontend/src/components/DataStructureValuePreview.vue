<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { dataStructures, type StructureValuePreview } from '@/services/dataStructures'
import type { JsonObject } from '@/services/platformTypes'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import StructuredJsonEditor from './StructuredJsonEditor.vue'

const props = defineProps<{ structureVersionId: string }>()
const schema = ref<JsonObject|null>(null), value = ref<unknown>(undefined)
const result = ref<StructureValuePreview|null>(null), error = ref(''), busy = ref(false)
const schemaErrors = ref<Record<string,string[]>>({})
const draftErrors = ref<string[]>([])
let serial=0
onBeforeUnmount(()=>{serial++})
async function load() {
  const request=++serial; busy.value=true; error.value=''; schema.value=null; schemaErrors.value={}; result.value=null
  if(!props.structureVersionId){busy.value=false;return}
  try {
    const response=await dataStructures.schema(props.structureVersionId)
    if(request!==serial)return
    schemaErrors.value=response.errors
    if(!response.valid || !response.json_schema){error.value='此结构尚不能生成表单；请先修正结构配置';return}
    schema.value=response.json_schema
  } catch(cause){if(request===serial)error.value=apiErrorSummary(cause,'结构读取失败')}
  finally{if(request===serial)busy.value=false}
}
function edit(next: unknown){serial++;busy.value=false;value.value=next;result.value=null;error.value=''}
watch(()=>props.structureVersionId,()=>{value.value=undefined;void load()},{immediate:true})
async function preview() {
  if(busy.value || !schema.value || value.value===undefined || draftErrors.value.length)return
  const request=++serial;busy.value=true;error.value='';result.value=null
  try {
    const response=await dataStructures.preview(props.structureVersionId,value.value)
    if(request!==serial)return
    if(response.preview_only!==true || response.structure_version_id!==props.structureVersionId)throw new Error('预览返回的结构版本不一致')
    result.value=response
  }catch(cause){if(request===serial)error.value=apiErrorSummary(cause,'样例预览失败')}
  finally{if(request===serial)busy.value=false}
}
</script>
<template>
  <section class="structure-preview" aria-label="结构样例预览">
    <p>仅用于试填和校验。样例不会保存为默认值，不创建任务、上传文件或调用模型。</p>
    <p v-if="busy" role="status">正在读取／校验结构样例…</p>
    <p v-if="error" role="alert">{{ error }} <button type="button" class="button ghost small" :disabled="busy" @click="load">重新读取结构</button></p>
    <p v-for="(messages,path) in schemaErrors" :key="path" role="alert">{{ path }}：{{ messages.join('；') }}</p>
    <template v-if="schema">
      <StructuredJsonEditor :model-value="value" :schemas="[schema]" :default-schemas="[schema]" label="试填样例" :disabled="busy" :field-errors="result?.errors" select-media @update:model-value="edit" @draft-validity="draftErrors=$event"/>
      <p v-if="draftErrors.length" role="alert">请先修正控件中尚不能读取的输入，不会用之前的数值代替。</p>
      <button type="button" class="button ghost small" :disabled="busy || value===undefined || Boolean(draftErrors.length)" @click="preview">校验样例并查看解析结果</button>
      <div v-if="result" class="preview-result">
        <p role="status">{{ result.valid?'样例结构校验通过（不等于素材可用或任务成功）':'样例存在字段错误，实际值仍可查看' }}</p>
        <StructuredJsonEditor :model-value="result.value" :schemas="[schema]" label="解析后的样例（只读）" readonly :field-errors="result.errors"/>
        <ul v-if="result.defaults.length"><li v-for="(source,index) in result.defaults" :key="index">{{ source.path }}：采用显式缺键默认值 {{ JSON.stringify(source.value) }}<details><summary>默认来源版本</summary><p>结构 {{ source.structure_version_id }} · 字段 {{ source.field_definition_version_id }}</p></details></li></ul>
        <details v-if="Object.keys(result.errors).length"><summary>全部字段错误</summary><p v-for="(messages,path) in result.errors" :key="path">{{ path }}：{{ messages.join('；') }}</p></details>
      </div>
      <details><summary>原始样例 JSON（只读）</summary><pre>{{ value===undefined?'尚未填写':JSON.stringify(value,null,2) }}</pre></details>
    </template>
  </section>
</template>
<style scoped>
.structure-preview{display:grid;gap:12px;min-width:0;margin-top:12px}.structure-preview p{margin:0;overflow-wrap:anywhere;font-size:12px}.structure-preview pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;overflow:auto}.preview-result{border-top:1px solid var(--border-color,#354350);padding-top:12px;display:grid;gap:12px}.structure-preview [role=alert]{color:var(--warning,#e9aa52)}
</style>
