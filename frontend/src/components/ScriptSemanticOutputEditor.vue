<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import RecipeStructureMappingEditor from './RecipeStructureMappingEditor.vue'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import ScriptSemanticGuide from './ScriptSemanticGuide.vue'
import {dataStructures} from '@/services/dataStructures'
import {scriptSemantics,type ScriptSemanticOptions,type ScriptSemanticPreview} from '@/services/scriptSemantics'
import type {BusinessOutputVersion} from '@/services/businessOutputs'
import type {JsonObject} from '@/services/platformTypes'
import {apiErrorSummary} from '@/services/apiErrorPresentation'

const props=defineProps<{modelValue:JsonObject;structureId:string;version:BusinessOutputVersion|null;disabled?:boolean;previewDisabled?:boolean;hideGuide?:boolean}>()
const emit=defineEmits<{'update:modelValue':[JsonObject];validity:[string[]]}>()
const options=ref<ScriptSemanticOptions|null>(null),schema=ref<JsonObject|null>(null)
const loading=ref(false),error=ref(''),text=ref(''),busy=ref(false),previewError=ref('')
const result=ref<ScriptSemanticPreview|null>(null),draftErrors=ref<string[]>([])
let loadSerial=0,previewSerial=0
const node=computed(()=>props.modelValue.node&&typeof props.modelValue.node==='object'&&!Array.isArray(props.modelValue.node)?props.modelValue.node as JsonObject:{})
const readiness=computed(()=>[...draftErrors.value,...(error.value?[error.value]:[]),...(!schema.value||!options.value?['请先读取确切结构与语义目录']:[])])
watch(readiness,value=>emit('validity',value),{immediate:true})
function invalidate(){previewSerial++;busy.value=false;previewError.value='';result.value=null}
async function load(){
  const request=++loadSerial;invalidate();schema.value=null;options.value=null;error.value='';draftErrors.value=[]
  loading.value=!!props.structureId
  if(!props.structureId)return
  try{const [catalog,response]=await Promise.all([scriptSemantics.options(),dataStructures.schema(props.structureId)])
    if(request!==loadSerial)return
    if(!response.valid||!response.json_schema)throw new Error('响应结构不可用，请先修正结构：'+JSON.stringify(response.errors))
    options.value=catalog;schema.value=response.json_schema
  }catch(cause){if(request===loadSerial)error.value=apiErrorSummary(cause,'读取配置目录失败；已有映射未改写')}
  finally{if(request===loadSerial)loading.value=false}
}
function update(value:JsonObject){if(!props.disabled&&options.value)emit('update:modelValue',{schema_version:options.value.mapping.schema_version,node:value})}
async function preview(){
  if(!props.version||props.previewDisabled||readiness.value.length||busy.value)return
  const request=++previewSerial;busy.value=true;previewError.value='';result.value=null
  try{const response=await scriptSemantics.preview({...props.version},text.value);if(request===previewSerial)result.value=response}
  catch(cause){if(request===previewSerial)previewError.value=apiErrorSummary(cause,'预览失败；样例原文仍保留')}
  finally{if(request===previewSerial)busy.value=false}
}
watch(()=>props.structureId,load,{immediate:true})
watch(()=>[props.modelValue,props.version?.id,props.version?.revision,props.version?.contract_sha256,props.previewDisabled,text.value,draftErrors.value],invalidate,{deep:true,flush:'sync'})
onBeforeUnmount(()=>{loadSerial++;previewSerial++})
</script>
<template>
  <section class="script-semantics" aria-label="剧本语义映射配置" :aria-busy="loading||busy">
    <h4>响应字段 → 剧本含义</h4>
    <p>字段名、层级和必填规则来自所选结构；下方只声明实体、主图、变体、分集和镜头的业务含义。额外创作字段保留在来源值中，不自动变成业务操作。映射保存在本输出版本，不另存字段字典。</p>
    <p v-if="loading" role="status">正在读取确切结构与映射目录…</p>
    <p v-if="!structureId">请先选择模型响应结构版本。</p>
    <p v-if="error" role="alert">{{error}} <button type="button" :disabled="loading" @click="load">重读结构与目录</button></p>
    <ScriptSemanticGuide v-if="options&&!hideGuide" :options="options" :mapping="modelValue" :draft="!disabled"/>
    <details v-if="options&&schema"><summary>编辑／查看实体、变体与分镜映射</summary>
      <RecipeStructureMappingEditor :key="structureId" :model-value="node" :schema="options.json_schema" label="剧本语义" :nodes="options.mapping.nodes" :preview-context="{schema,scopes:options.mapping.scopes}" :fields="[]" :outputs="[]" :scopes="[]" :prior-stages="[]" :disabled="disabled" @update:model-value="update" @draft-validity="draftErrors=$event"/>
    </details>
    <ul v-if="draftErrors.length" role="alert"><li v-for="message in draftErrors" :key="message">{{message}}</li></ul>
    <details><summary>完整映射合同（技术详情）</summary><ReadonlyDataValue :value="modelValue" label="当前语义映射配置"/></details>
    <h4>按已保存版本预览</h4>
    <p>只检查结构与映射；尚不验证实体引用关系、不创建剧本／Bible／镜头、不审核采用或调用模型。</p>
    <label>样例JSON原文<textarea v-model="text" rows="7" spellcheck="false"/></label>
    <button type="button" :disabled="!version||previewDisabled||readiness.length>0||busy" @click="preview">{{busy?'正在预览…':'解析样例并查看剧本语义'}}</button>
    <p v-if="!version||previewDisabled">请先保存当前配置，再按确切输出版本预览。</p>
    <p v-if="previewError" role="alert">{{previewError}}</p>
    <section v-if="result" aria-label="剧本语义预览结果">
      <p role="status">{{result.valid?'结构与映射通过（不代表关系校验或业务生成完成）':'样例未通过；原文和实际解析值保留'}}</p>
      <ul v-if="Object.keys(result.errors).length" role="alert"><li v-for="(messages,path) in result.errors" :key="path">{{path}}：{{messages.join('；')}}</li></ul>
      <ReadonlyDataValue :value="result.raw_value" label="原始解析值"/>
      <ReadonlyDataValue :value="result.source_value" label="完整来源值（含自定义字段）"/>
      <ReadonlyDataValue :value="result.value" label="剧本语义投影"/>
      <ReadonlyDataValue :value="result.defaults" label="显式默认值来源"/>
      <ReadonlyDataValue :value="result.provenance" label="字段来源路径与版本"/>
      <ReadonlyDataValue :value="result.warnings" label="未声明字段提醒"/>
    </section>
  </section>
</template>
<style scoped>
.script-semantics{display:grid;gap:var(--config-space-md);min-width:0;font:var(--config-body-size)/1.6 var(--font)}.script-semantics h4{font-size:var(--config-section-size);margin:0}.script-semantics p,.script-semantics li{overflow-wrap:anywhere;margin:0}.script-semantics label{display:grid;gap:var(--config-space-xs)}.script-semantics textarea{box-sizing:border-box;width:100%;min-width:0;padding:var(--config-space-sm);background:var(--bg-soft);color:inherit;border:1px solid var(--border-strong);border-radius:var(--config-radius);font:var(--config-body-size)/1.6 monospace}.script-semantics button{min-height:var(--config-control-height);justify-self:start;white-space:normal;font-size:var(--config-body-size)}.script-semantics summary{min-height:var(--config-control-height);cursor:pointer;align-content:center}.script-semantics [role=alert]{color:var(--warning)}.script-semantics :focus-visible{outline:2px solid var(--accent);outline-offset:2px}@media(max-width:640px){.script-semantics{--config-control-height:44px}.script-semantics :deep(button),.script-semantics :deep(select){min-height:44px}.script-semantics :deep(.mapping-node){padding:var(--config-space-sm)}}
</style>
