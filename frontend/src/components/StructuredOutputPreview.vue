<script setup lang="ts">
import {onBeforeUnmount,ref,watch} from 'vue'
import {businessOutputs,type BusinessOutputVersion,type StructuredOutputPreview} from '@/services/businessOutputs'
import {apiErrorSummary} from '@/services/apiErrorPresentation'
import StructuredJsonEditor from './StructuredJsonEditor.vue'

const props=defineProps<{version:BusinessOutputVersion;disabled?:boolean}>()
const text=ref(''),busy=ref(false),error=ref(''),result=ref<StructuredOutputPreview|null>(null)
let serial=0
function invalidate(){serial++;busy.value=false;result.value=null;error.value=''}
watch(()=>[props.version.id,props.version.revision,props.version.contract_sha256,props.disabled],invalidate)
onBeforeUnmount(()=>{serial++})
function edit(value:string){text.value=value;invalidate()}
async function preview(){
  if(busy.value||props.disabled)return
  const request=++serial;busy.value=true;result.value=null;error.value=''
  const version={...props.version}
  try{const value=await businessOutputs.previewJson(version,text.value);if(request===serial)result.value=value}
  catch(cause){if(request===serial)error.value=apiErrorSummary(cause,'样例解析失败；原文已保留')}
  finally{if(request===serial)busy.value=false}
}
</script>
<template>
  <section class="json-output-preview" aria-label="通用JSON输出预览" :aria-busy="busy">
    <h4>按已保存结构解析样例</h4>
    <p>仅检查JSON与已保存输出合同；不会生成内容、创建任务或采用结果。结构以外的字段保留并提醒，缺键默认来源单独显示。</p>
    <label>样例JSON原文<textarea :value="text" rows="7" :disabled="disabled" spellcheck="false" @input="edit(($event.target as HTMLTextAreaElement).value)"/></label>
    <p v-if="disabled">请先保存修改后再预览，已有任务不随此配置更新。</p>
    <button type="button" class="button ghost small" :disabled="busy||disabled" @click="preview">{{busy?'正在解析…':'解析样例并查看字段目录'}}</button>
    <p v-if="error" role="alert">{{error}}</p>
    <template v-if="result">
      <p role="status">{{result.valid?'样例结构通过，不等于业务执行或生成成功':result.parsed?'存在字段错误，实际内容保留如下':'JSON未能解析，请核对原文'}}</p>
      <ul v-if="Object.keys(result.errors).length" role="alert"><li v-for="(messages,path) in result.errors" :key="path">{{path}}：{{messages.join('；')}}</li></ul>
      <StructuredJsonEditor v-if="result.parsed" :model-value="result.value" :schemas="[result.json_schema]" path="response" label="解析后的实际内容" readonly :field-errors="result.errors"/>
      <ul v-if="result.defaults.length"><li v-for="(item,index) in result.defaults" :key="index">{{item.path}}：显式默认 {{JSON.stringify(item.value)}} · 结构版本 {{item.structure_version_id}}</li></ul>
      <ul v-if="result.warnings.length"><li v-for="(item,index) in result.warnings" :key="index">{{item.path}}：{{item.message}}</li></ul>
      <details><summary>所选结构的可引用字段目录（非实际产物）</summary><ul><li v-for="(item,index) in result.fields" :key="index"><code>{{JSON.stringify(item.path)}}</code> · {{item.field_definition.label}} · {{item.field_definition.value_type}}</li></ul></details>
      <details v-if="result.parsed"><summary>原始解析值（不含配置默认）</summary><pre>{{JSON.stringify(result.raw_value,null,2)}}</pre></details>
    </template>
  </section>
</template>
<style scoped>
.json-output-preview{display:grid;gap:12px;min-width:0}.json-output-preview h4,.json-output-preview p{margin:0}.json-output-preview p,.json-output-preview li{overflow-wrap:anywhere}.json-output-preview label{display:grid;gap:6px}.json-output-preview textarea{box-sizing:border-box;width:100%;min-width:0;background:var(--bg-primary);color:inherit;border:1px solid var(--border);border-radius:6px;padding:10px}.json-output-preview button{min-height:36px;justify-self:start}.json-output-preview pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:300px;overflow:auto}.json-output-preview [role=alert]{color:var(--warning,#e9aa52)}
</style>
