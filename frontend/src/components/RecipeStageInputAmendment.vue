<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import type { JsonObject } from '@/services/platformTypes'
const props=defineProps<{runId:string;stageId:string;planSha:string;sources?:{index:number;sources:JsonObject}[];projectText?:boolean;disabled?:boolean}>()
const emit=defineEmits<{applied:[]}>()
const result=ref<JsonObject|null>(null),busy=ref(false),error=ref(''),confirmed=ref(false)
const defaultPolicy=()=>props.projectText?'explicit_project_text_input_sources_v1':'dialogue_missing_voiceover_is_false_v1'
const policy=ref(defaultPolicy()),selected=ref('')
const explicitSource=computed(()=>['explicit_media_input_sources_v1','explicit_project_text_input_sources_v1'].includes(policy.value))
const drafts=ref<{route_index:number;input_key:string;text:string}[]>([])
const options=computed(()=>(props.sources||[]).flatMap(route=>Object.entries(route.sources)
  .filter(([,source])=>source&&typeof source==='object'&&!Array.isArray(source)&&!['reviewed_references','asset','artifacts'].includes(String((source as JsonObject).kind)))
  .map(([key,source])=>({route_index:route.index,input_key:key,source}))))
let sequence=0
onUnmounted(()=>{++sequence})
function invalidate(){++sequence;result.value=null;busy.value=false;error.value='';confirmed.value=false}
watch(()=>`${props.runId}:${props.stageId}:${props.planSha}:${props.projectText}`,()=>{invalidate();drafts.value=[];selected.value='';policy.value=defaultPolicy()},{flush:'sync'})
watch(()=>props.disabled,()=>{if(props.disabled)invalidate()},{flush:'sync'})
watch([policy,()=>JSON.stringify(drafts.value)],invalidate,{flush:'sync'})
function add(){
  if(props.disabled||busy.value||selected.value==='')return
  const option=options.value[Number(selected.value)]
  if(!option||drafts.value.some(row=>row.route_index===option.route_index&&row.input_key===option.input_key))return
  drafts.value.push({route_index:option.route_index,input_key:option.input_key,text:JSON.stringify(option.source,null,2)})
}
async function request(action:'preview'|'apply'){
  if(props.disabled||busy.value||!props.stageId||action==='apply'&&(!confirmed.value||result.value?.status!=='proposed'))return
  const current=++sequence, sha=result.value?.sha256
  busy.value=true;error.value=''
  try{
    const changes=explicitSource.value?drafts.value.map(row=>({route_index:row.route_index,input_key:row.input_key,source:JSON.parse(row.text)})):undefined
    if(changes&&!changes.length)throw new Error('请先选择并修改至少一个文本或JSON输入来源')
    const data=await platformApi.amendProductionStageInputs(props.runId,{stage_run_id:props.stageId,
      expected_run_plan_sha256:props.planSha,action,policy:policy.value,...(changes?{source_changes:changes}:{}),...(action==='apply'?{expected_amendment_sha256:sha}:{})})
    if(current!==sequence)return
    if(!['proposed','applied'].includes(String(data.status))||typeof data.sha256!=='string'||!/^[a-f0-9]{64}$/.test(data.sha256)
      ||!data.evidence||typeof data.evidence!=='object'||Array.isArray(data.evidence)
      ||(data.evidence as JsonObject).run_plan_sha256!==props.planSha||(data.evidence as JsonObject).policy!==policy.value)throw new Error('修订回执不完整或不属于当前计划，请重新核对')
    result.value=data;confirmed.value=false
    if(data.status==='applied')emit('applied')
  }catch(e){if(current===sequence)error.value=apiErrorSummary(e)}finally{if(current===sequence)busy.value=false}
}
</script>
<template>
  <details class="stage-input-amendment">
    <summary>输入配置阻断？核对执行前受控修订</summary>
    <p>仅适用于当前{{projectText?'项目文本':'媒体'}}阶段尚未创建任何执行准备或调用的情况。不会改写剧本、审核、原计划、参数或已完成产物；每阶段仅允许一份不可变修订，请一起核对所有缺口。</p>
    <fieldset :disabled="disabled" class="amendment-fields">
    <label>修订方式<select v-model="policy" :disabled="busy||result?.status==='applied'">
      <option v-if="!projectText" value="dialogue_missing_voiceover_is_false_v1">对白缺少画外音标记：明确补 false</option>
      <option :value="projectText?'explicit_project_text_input_sources_v1':'explicit_media_input_sources_v1'">明确修改文本 / JSON 输入来源（高级）</option>
    </select></label>
    <template v-if="explicitSource">
      <p>选择原路线输入，修改其来源配置。只能使用现有来源规则；不更换模型、媒体参考或参数。预览会校验原实例合同并展示前后差异；修改草稿会使旧确认失效。</p>
      <label>原路线输入<select v-model="selected" :disabled="busy||result?.status==='applied'"><option value="">请选择</option><option v-for="(option,index) in options" :key="index" :value="String(index)">路线 {{option.route_index+1}} · {{option.input_key}}</option></select></label>
      <button type="button" class="button ghost small" :disabled="busy||selected===''||result?.status==='applied'" @click="add">加入来源修订</button>
      <div v-for="(draft,index) in drafts" :key="`${draft.route_index}:${draft.input_key}`">
        <label>路线 {{draft.route_index+1}} · {{draft.input_key}} · 来源配置 JSON<textarea v-model="draft.text" rows="12" :disabled="busy||result?.status==='applied'" spellcheck="false"/></label>
        <button type="button" class="button ghost small" :disabled="busy||result?.status==='applied'" @click="drafts.splice(index,1)">移除此修订项</button>
      </div>
    </template>
    <button type="button" class="button ghost small" :disabled="busy||!stageId||result?.status==='applied'" @click="request('preview')">{{busy?'正在核对…':explicitSource?'核对来源映射修订':'核对对白转换修订'}}</button>
    <p v-if="error" role="alert">{{error}}</p>
    <template v-if="result">
      <p role="status">{{result.status==='applied'?'此修订已生效；需通过原运行的重试入口继续，已成功阶段不会重跑。':'修订待确认，尚未写入。'}} {{result.notice}}</p>
      <p v-if="result.id">审计 {{result.id}} · 操作人 {{result.created_by}} · {{result.created_at}}</p>
      <details><summary>查看修订前后映射及摘要</summary><pre tabindex="0">{{JSON.stringify(result.evidence,null,2)}}</pre></details>
      <template v-if="result.status==='proposed'">
        <label class="confirmation"><input v-model="confirmed" type="checkbox" :disabled="busy"/>我确认此转换规则；只修订输入，不启动生成</label>
        <button type="button" class="button primary small" :disabled="busy||!confirmed" @click="request('apply')">确认并记录输入修订</button>
      </template>
    </template>
    </fieldset>
  </details>
</template>
<style scoped>
.amendment-fields{border:0;margin:0;padding:0;min-width:0}
.stage-input-amendment{border:1px solid var(--border,#303743);border-radius:6px;padding:12px;min-width:0}.stage-input-amendment p{overflow-wrap:anywhere}.stage-input-amendment pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:300px;overflow:auto}.stage-input-amendment label{display:block}.stage-input-amendment textarea,.stage-input-amendment select{width:100%;min-width:0;box-sizing:border-box}.stage-input-amendment textarea{font-family:var(--font-mono,monospace)}.confirmation{display:flex!important;gap:8px;align-items:center;min-height:36px}.confirmation input{width:auto;min-height:24px}
</style>
