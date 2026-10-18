<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import type { PromptProfileVersion } from '@/services/platformTypes'
import { executionFieldSmokeApi, type FieldSmokeSource } from '@/services/executionFieldSmoke'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { resourceSmokeApi, type ResourceSmokeContext } from '@/services/executionResourceSmoke'
const props=defineProps<{versionId:string|null;resource?:ResourceSmokeContext;definitionSha256:string;disabled:boolean}>()
const emit=defineEmits<{built:[value:FieldSmokeSource<string|null>];invalidated:[];busy:[value:boolean]}>()
const profiles=ref<PromptProfileVersion[]>([]),selected=ref(''),source=ref<FieldSmokeSource<string|null>|null>(null)
const loading=ref(false),building=ref(false),error=ref('')
const fields=ref<Record<string,{enabled:boolean;text:string;sourceLanguage:string;targetLanguage:string;contexts:Record<string,string>;tokens:string}>>({})
const busy=computed(()=>loading.value||building.value)
let serial=0
onBeforeUnmount(()=>{serial++})
watch(busy,value=>emit('busy',value))
watch(fields,()=>emit('invalidated'),{deep:true})
async function loadProfiles(){
  if(props.disabled||busy.value)return
  const request=++serial;loading.value=true;error.value=''
  try{const rows=await platformApi.listPromptProfileVersions();if(request===serial)profiles.value=rows.filter(row=>row.status==='published')}
  catch(cause){if(request===serial)error.value=apiErrorSummary(cause)}
  finally{if(request===serial)loading.value=false}
}
async function choose(id:string){
  if(props.disabled||busy.value)return
  const request=++serial;emit('invalidated');selected.value=id;source.value=null;fields.value={};error.value=''
  if(!id)return
  loading.value=true
  try{
    if ((props.versionId === null) !== Boolean(props.resource)) throw new Error('字段烟测来源缺少明确资源范围')
    const result=props.resource ? await resourceSmokeApi.readFields(props.resource,id) : await executionFieldSmokeApi.read(props.versionId!,id)
    if(request!==serial)return
    if(result.execution_instance_version_id!==props.versionId||result.definition_sha256!==props.definitionSha256||result.profile_version_id!==id)throw new Error('字段来源与选定版本不一致')
    source.value=result
    fields.value=Object.fromEntries(result.rules.map(rule=>[rule.field_key,{enabled:false,text:'',sourceLanguage:'',targetLanguage:'',contexts:Object.fromEntries(rule.generation_context_selectors.map(key=>[key,''])),tokens:''}]))
  }catch(cause){if(request===serial)error.value=apiErrorSummary(cause)}
  finally{if(request===serial)loading.value=false}
}
async function build(){
  if(props.disabled||busy.value||!source.value)return
  building.value=true;error.value=''
  try{
    const items=source.value.rules.filter(rule=>fields.value[rule.field_key]?.enabled).map(rule=>{
      const row=fields.value[rule.field_key]!
      return {field_key:rule.field_key,source_text:row.text,source_language:row.sourceLanguage,target_language:row.targetLanguage,context_facts:{...row.contexts},protected_tokens:row.tokens.split('\n').filter(token=>token.length>0)}
    })
    if(!items.length)throw new Error('请选择至少一个需要测试的字段')
    const result=props.resource
      ? await resourceSmokeApi.buildFields(props.resource,source.value as FieldSmokeSource<null>,items)
      : await executionFieldSmokeApi.build(source.value as FieldSmokeSource,items)
    if (result.execution_instance_version_id!==props.versionId||result.definition_sha256!==props.definitionSha256||result.profile_version_id!==selected.value) throw new Error('组装结果与选定字段来源不一致')
    emit('built',result)
  }catch(cause){error.value=apiErrorSummary(cause)}
  finally{building.value=false}
}
onMounted(()=>{void loadProfiles()})
</script>
<template>
  <section class="field-smoke-source">
    <h4>字段测试来源</h4><p>选择已发布策略中的真实字段规则，填写用于本次测试的文字。这里只生成测试输入，不绑定父实例、不创建假项目，也不会调用模型。随后仍需确认创建烟测任务。</p>
    <p v-if="busy" role="status">正在读取或核对字段规则…</p><p v-if="error" role="alert" class="platform-alert danger">{{ error }}</p>
    <label>已发布策略<select :value="selected" :disabled="disabled||busy" @change="choose(($event.target as HTMLSelectElement).value)"><option value="">请选择测试规则来源</option><option v-for="profile in profiles" :key="profile.id" :value="profile.id">{{ profile.name }} · v{{ profile.version }}</option></select></label>
    <button type="button" class="button ghost small" :disabled="disabled||busy" @click="loadProfiles">刷新策略目录</button>
    <template v-if="source">
      <article v-for="rule in source.rules" :key="rule.field_key">
        <label class="field-check"><input v-model="fields[rule.field_key]!.enabled" type="checkbox" :disabled="disabled||busy"/>测试字段 {{ rule.field_key }}</label>
        <p>{{ rule.generation_instruction }} · 语言要求 {{ rule.language_policy }} · 最多 {{ rule.max_chars }} 字符</p>
        <fieldset v-if="fields[rule.field_key]!.enabled" :disabled="disabled||busy">
          <label>测试来源正文<textarea v-model="fields[rule.field_key]!.text" rows="3" placeholder="提供实际希望生成或翻译的测试内容"/></label>
          <label>来源语言<input v-model="fields[rule.field_key]!.sourceLanguage" placeholder="规则要求保留来源语言时必填"/></label>
          <label>目标语言<input v-model="fields[rule.field_key]!.targetLanguage" placeholder="规则要求继承目标语言时必填"/></label>
          <label v-for="selector in rule.generation_context_selectors" :key="selector">测试上下文：{{ selector }}<textarea v-model="fields[rule.field_key]!.contexts[selector]" rows="2" placeholder="仅本次测试说明，不读取或伪造真实项目"/></label>
          <label>必须保留的原文（可选，每行一段）<textarea v-model="fields[rule.field_key]!.tokens" rows="2"/></label>
        </fieldset>
      </article>
      <button type="button" class="button ghost" :disabled="disabled||busy" @click="build">核对并组装字段测试输入</button>
    </template>
  </section>
</template>
<style scoped>
.field-smoke-source,.field-smoke-source fieldset{display:grid;gap:10px}.field-smoke-source label{display:grid;gap:6px}.field-smoke-source p{font-size:12px;line-height:1.7}.field-smoke-source article{padding:12px;border:1px solid var(--border);border-radius:8px}.field-check{display:flex!important;gap:8px;align-items:center}.field-check input{width:auto}
</style>
