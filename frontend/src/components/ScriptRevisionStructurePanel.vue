<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {apiErrorSummary,apiErrorPresentation} from '@/services/apiErrorPresentation'
import {scriptRevisionStructures as api,type ScriptRevisionStructure,type ScriptStructureOption} from '@/services/scriptRevisionStructures'
import {platformApi} from '@/services/platformApi'
import {jsonInputErrors} from '@/services/structuredInputForm'
import StructuredJsonEditor from './StructuredJsonEditor.vue'
import type {JsonObject,ScriptRevision} from '@/services/platformTypes'

const props=defineProps<{revisionId:string;contentSha256:string;status:string;disabled?:boolean}>()
const emit=defineEmits<{'revision-created':[revision:ScriptRevision,basisId:string];'edit-busy':[busy:boolean]}>()
const editing=ref(false),draft=ref<unknown>(null),editSaving=ref(false),editUncertain=ref(false),editError=ref('')
const fieldErrors=ref<Record<string,string[]>>({}),draftErrors=ref<string[]>([])
const result=ref<ScriptRevisionStructure|null>(null),loaded=ref(false),reading=ref(false),saving=ref(false),error=ref('')
const rows=ref<ScriptStructureOption[]>([]),selected=ref<ScriptStructureOption|null>(null),search=ref(''),page=ref(1),count=ref(0)
const optionsBusy=ref(false),optionsLoaded=ref(false),optionsError=ref(''),optionsOpen=ref(false)
let epoch=0,queryEpoch=0,disposed=false
const projection=computed(()=>result.value?.document.projection as JsonObject|undefined)
const output=computed(()=>result.value?.document.output as JsonObject|undefined)
const editErrors=computed(()=>result.value&&editing.value?jsonInputErrors(draft.value,[result.value.json_schema],'剧本字段'):[])
const canRevise=computed(()=>!!result.value?.can_revise&&editing.value&&!props.disabled&&!editSaving.value&&!editUncertain.value&&!editErrors.value.length&&!draftErrors.value.length)
const canSave=computed(()=>loaded.value&&!result.value&&props.status==='preview'&&!!selected.value&&!props.disabled&&!reading.value&&!saving.value)
function validDetail(value:ScriptRevisionStructure|null){
  if(value){const revision=value.document.revision as JsonObject
    if(revision?.id!==props.revisionId||revision.content_sha256!==props.contentSha256)throw new Error('结构化详情与当前剧本修订不一致，请重新读取')}
  return value
}
async function read(){
  if(editing.value||editSaving.value)return
  const token=++epoch,id=props.revisionId;reading.value=true;loaded.value=false;result.value=null;error.value=''
  try{const value=await api.read(id);if(disposed||token!==epoch)return;result.value=validDetail(value);loaded.value=true}
  catch(cause){if(!disposed&&token===epoch)error.value=apiErrorSummary(cause)}
  finally{if(!disposed&&token===epoch)reading.value=false}
}
function startEdit(){
  if(!loaded.value||!result.value?.can_revise||props.disabled||reading.value||saving.value||editSaving.value)return
  draft.value=JSON.parse(JSON.stringify(projection.value?.source_value));editing.value=true;editError.value='';editUncertain.value=false
  fieldErrors.value={};draftErrors.value=[]
}
function changeDraft(value:unknown){if(props.disabled||editSaving.value||editUncertain.value)return;draft.value=value;fieldErrors.value={};editError.value=''}
function cancelEdit(){if(editSaving.value)return;editing.value=false;draft.value=null;fieldErrors.value={};editError.value='';editUncertain.value=false}
async function revise(){
  if(!canRevise.value||!result.value)return
  const token=epoch,id=props.revisionId,document=result.value.document,sourceId=(document.revision as JsonObject).source_id
  if(typeof sourceId!=='string'||!sourceId){editError.value='缺少原修订来源，不能另存字段草稿';return}
  const content=JSON.stringify(draft.value,null,2),basis={revision_id:id,document_sha256:result.value.document_sha256}
  editSaving.value=true;editError.value='';fieldErrors.value={};emit('edit-busy',true)
  try{
    const revision=await platformApi.createScriptRevision(sourceId,content,basis)
    if(disposed||token!==epoch)return
    if(!revision.id||revision.id===id||revision.source!==sourceId||revision.status!=='preview'||revision.content!==content){
      editUncertain.value=true;throw new Error('另存回执与本次草稿不一致，请从已有版本核对，不能自动再次提交')}
    editing.value=false;draft.value=null;emit('revision-created',revision,id)
  }catch(cause){if(!disposed&&token===epoch){
    editError.value=apiErrorSummary(cause);fieldErrors.value=apiErrorPresentation(cause).fields
    const status=(cause as {response?:{status?:number}})?.response?.status
    if(!status||status>=500)editUncertain.value=true
  }}finally{if(!disposed&&token===epoch){editSaving.value=false;emit('edit-busy',false)}}
}
async function loadOptions(nextPage=1){
  const token=++queryEpoch;optionsOpen.value=true;optionsBusy.value=true;optionsLoaded.value=false;optionsError.value='';rows.value=[]
  try{const value=await api.options(nextPage,search.value);if(disposed||token!==queryEpoch)return;rows.value=value.results;count.value=value.count;page.value=nextPage;optionsLoaded.value=true}
  catch(cause){if(!disposed&&token===queryEpoch)optionsError.value=apiErrorSummary(cause)}
  finally{if(!disposed&&token===queryEpoch)optionsBusy.value=false}
}
function choose(id:string){selected.value=rows.value.find(row=>row.id===id)||null}
async function save(){
  if(!canSave.value||!selected.value)return
  const token=++epoch,id=props.revisionId,outputId=selected.value.id;saving.value=true;error.value=''
  try{const value=await api.save(id,outputId);if(disposed||token!==epoch)return
    validDetail(value);if((value.document.output as JsonObject).id!==outputId)throw new Error('保存结果的输出版本与本次选择不一致')
    result.value=value;loaded.value=true;optionsOpen.value=false
  }catch(cause){if(!disposed&&token===epoch)error.value=apiErrorSummary(cause)}
  finally{if(!disposed&&token===epoch)saving.value=false}
}
watch(()=>[props.revisionId,props.contentSha256,props.status],()=>{
  if(editSaving.value)emit('edit-busy',false)
  editing.value=false;editSaving.value=false;draft.value=null;editUncertain.value=false;editError.value='';fieldErrors.value={};draftErrors.value=[]
  ++queryEpoch;selected.value=null;rows.value=[];optionsOpen.value=false;optionsBusy.value=false;optionsLoaded.value=false;optionsError.value='';saving.value=false
  void read()
},{immediate:true,flush:'sync'})
onBeforeUnmount(()=>{if(editSaving.value)emit('edit-busy',false);disposed=true;++epoch;++queryEpoch})
const pretty=(value:unknown)=>JSON.stringify(value,null,2)
</script>

<template>
  <section class="script-structure-panel" aria-label="剧本JSON结构化">
    <header><strong>JSON 结构化结果</strong><button type="button" class="button ghost small" :disabled="reading||saving||editing||editSaving" @click="read">重新读取结果</button></header>
    <p v-if="reading" role="status">正在读取已保存结果…</p>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}。原稿与选择未被替换；可重新读取核对或修改原稿后另存预览。</p>
    <template v-if="loaded&&!result">
      <p>本修订尚无结构化结果。JSON 原稿可按所选输出版本直接解析，不调用模型；自由文本需要后续的显式 AI 结构化。</p>
      <template v-if="status==='preview'">
        <button type="button" class="button ghost small" :disabled="disabled||saving||optionsBusy" @click="loadOptions()">选择剧本输出版本</button>
        <p v-if="selected">本次选择：{{ selected.name }} · v{{ selected.version }}</p>
        <div v-if="optionsOpen" class="structure-options">
          <label class="platform-field"><span>搜索输出版本</span><input v-model="search" :disabled="saving" @keydown.enter.prevent="loadOptions()"/></label>
          <button type="button" class="button ghost small" :disabled="optionsBusy||saving" @click="loadOptions()">查询版本</button>
          <p v-if="optionsBusy" role="status">正在读取可用版本…</p>
          <p v-if="optionsError" class="platform-alert danger" role="alert">{{ optionsError }}</p>
          <template v-if="optionsLoaded">
            <label class="platform-field"><span>确切输出版本（不会自动选用）</span><select :value="selected?.id||''" :disabled="disabled||saving" @change="choose(($event.target as HTMLSelectElement).value)">
              <option value="">请选择</option><option v-if="selected&&!rows.some(row=>row.id===selected?.id)" :value="selected.id">{{ selected.name }} · v{{ selected.version }}（已选，本页外）</option>
              <option v-for="row in rows" :key="row.id" :value="row.id">{{ row.name }} · v{{ row.version }}</option>
            </select></label>
            <p v-if="!count">没有匹配的已发布剧本输出版本，请在字段／输出配置中维护后重新查询。</p>
            <div class="structure-pagination"><button type="button" class="button ghost small" :disabled="page<=1||optionsBusy" @click="loadOptions(page-1)">上一页</button><span>第 {{ page }} 页 · 共 {{ count }} 个</span><button type="button" class="button ghost small" :disabled="page*20>=count||optionsBusy" @click="loadOptions(page+1)">下一页</button></div>
          </template>
        </div>
        <button type="button" class="button primary small" :disabled="!canSave" @click="save">{{ saving?'解析保存中…':'解析 JSON 并保存结构化结果' }}</button>
      </template>
      <p v-else>已确认的原稿不追加结构化内容；请将原文另存为预览修订后解析。</p>
    </template>
    <template v-if="result&&projection">
      <p>已保存 · {{ output?.name }} · v{{ output?.version }}。仅保存结构化结果，未自动确认、采用或创建素材。</p>
      <details><summary>结构化字段（含自定义字段）</summary><pre>{{ pretty(projection.source_value) }}</pre></details>
      <details><summary>实体、变体与逐镜语义</summary><pre>{{ pretty(projection.value) }}</pre></details>
      <details><summary>取值路径、默认值与提示</summary><pre>{{ pretty({provenance:projection.provenance,defaults:projection.defaults,warnings:projection.warnings}) }}</pre></details>
      <button v-if="!editing&&result.can_revise" type="button" class="button ghost small" :disabled="disabled||reading||saving" @click="startEdit">按字段编辑并另存</button>
      <p v-if="!result.can_revise">此来源仅查看；模型候选须在原生产审核流程中处理，不能从公共修订入口派生。</p>
      <section v-if="editing" class="field-draft" aria-label="结构化剧本字段草稿">
        <p>使用本修订保存的字段合同；另存会生成新的 JSON 预览修订，原文与原修订不变，不自动确认或采用。</p>
        <StructuredJsonEditor :model-value="draft" label="剧本字段" path="source" :schemas="[result.json_schema]" :field-errors="fieldErrors" :disabled="disabled||editSaving||editUncertain" @update:model-value="changeDraft" @draft-validity="draftErrors=$event"/>
        <p v-if="editError" class="platform-alert danger" role="alert">{{ editError }}</p>
        <p v-if="editUncertain" role="alert">提交结果尚需核对，草稿已保留；请取消编辑后从已有版本查看，不会自动再次提交。</p>
        <ul v-if="editErrors.length"><li v-for="item in editErrors" :key="item">{{ item }}</li></ul>
        <div class="structure-pagination"><button type="button" class="button ghost small" :disabled="editSaving" @click="cancelEdit">取消编辑</button><button type="button" class="button primary small" :disabled="!canRevise" @click="revise">{{ editSaving?'另存中…':'另存预览修订并解析' }}</button></div>
      </section>
    </template>
  </section>
</template>

<style scoped>
.field-draft{display:grid;gap:10px;min-width:0}
.script-structure-panel{min-width:0;margin-top:16px;border-top:1px solid var(--border-color);padding-top:12px;display:grid;gap:10px;overflow-wrap:anywhere}.script-structure-panel header,.structure-pagination{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}.script-structure-panel p{margin:0}.structure-options{display:grid;gap:8px;min-width:0}.script-structure-panel pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:360px;overflow:auto;font-size:12px}.script-structure-panel summary{cursor:pointer;min-height:36px;display:flex;align-items:center}.script-structure-panel select,.script-structure-panel input{max-width:100%;min-width:0}.script-structure-panel button{white-space:normal}
</style>
