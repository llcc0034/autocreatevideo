<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { shotFieldRevisionsApi } from '@/services/shotFieldRevisionsApi'
import { type ShotFieldRevisionDetail, type ShotFieldRevisionWrite } from '@/services/shotFieldRevisions'
import { isSavedShotId } from '@/services/shotSourceFields'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import StructuredJsonEditor from './StructuredJsonEditor.vue'

const props = defineProps<{projectId:string;shotId:string}>()
const emit = defineEmits<{changed:[]}>()
const detail=ref<ShotFieldRevisionDetail|null>(null), loading=ref(false), saving=ref(false)
const error=ref(''), notice=ref(''), historical=ref(false), stale=ref(false)
const selected=ref(''), draft=ref<unknown>(undefined), dirty=ref(false), errors=ref<string[]>([]), draftErrors=ref<string[]>([])
const pending=ref<ShotFieldRevisionWrite|null>(null)
const field=computed(()=>selected.value!==''?detail.value?.fields[Number(selected.value)]:undefined)
const locked=computed(()=>loading.value||saving.value||!!pending.value||historical.value||stale.value)
const saved=computed(()=>isSavedShotId(props.shotId))
let sequence=0
const clone=(v:unknown)=>v===undefined?undefined:JSON.parse(JSON.stringify(v))
function choose(){draft.value=clone(field.value?.value);dirty.value=false;errors.value=[];draftErrors.value=[];error.value=''}
async function load(revisionId?:string){
  if(loading.value||saving.value||pending.value||!saved.value)return
  if(dirty.value&&!window.confirm('重新读取会放弃当前未保存草稿，是否继续？'))return
  const token=++sequence, shotId=props.shotId
  loading.value=true;detail.value=null;selected.value='';dirty.value=false;draft.value=undefined;error.value='';notice.value=''
  try{const value=await shotFieldRevisionsApi.read(shotId,revisionId);if(token===sequence){detail.value=value;historical.value=!!revisionId;stale.value=false}}
  catch(cause){if(token===sequence)error.value=apiErrorSummary(cause)}
  finally{if(token===sequence)loading.value=false}
}
async function save(operation:'set'|'reset'='set'){
  if(saving.value||loading.value||!detail.value||!saved.value)return
  if(!pending.value){
    if(locked.value||!field.value||operation==='set'&&(draft.value===undefined||errors.value.length||draftErrors.value.length))return
    pending.value={request_id:crypto.randomUUID(),source_sha256:detail.value.source_sha256,
      expected_revision_id:detail.value.revision?.id||null,node_key:field.value.original.node_key,
      value_path:[...field.value.original.value_path],operation,...(operation==='set'?{value:clone(draft.value)}:{})}
  }
  const token=sequence, shotId=props.shotId, payload=pending.value
  saving.value=true;error.value='';notice.value=''
  try{
    const result=await shotFieldRevisionsApi.write(shotId,payload)
    if(token!==sequence)return
    detail.value=result.detail;pending.value=null;historical.value=true;dirty.value=false;choose()
    notice.value=`${result.replayed?'已核对原请求':'已另存'}修订 v${result.detail.revision?.revision}。这是该请求的确切回执；重新读取当前版本后可继续修改。`
    emit('changed')
  }catch(cause:any){
    if(token!==sequence)return
    error.value=apiErrorSummary(cause)
    const status=Number(cause?.response?.status)
    if([400,401,403,404,409,422].includes(status)){pending.value=null;stale.value=true}
  }finally{if(token===sequence)saving.value=false}
}
watch([()=>props.shotId,()=>props.projectId],()=>{
  sequence++;detail.value=null;loading.value=false;saving.value=false;pending.value=null;selected.value='';draft.value=undefined
  dirty.value=false;error.value='';notice.value='';stale.value=false;historical.value=false
})
onBeforeUnmount(()=>{sequence++})
</script>

<template>
  <section class="panel shot-field-revisions" aria-label="独立字段人工修订">
    <header><div><h3>独立字段 · 人工修订</h3><p>另存输入原值，不改原剧本、对白/人物关系或时间轴；对象字段与其子字段分别修订，不互相覆盖。保存不代表审核采用或模型已收到；只有策略明确映射的字段才会进入新的执行准备，已有请求保持原冻结值。</p></div>
      <button type="button" class="button ghost small" :disabled="loading||saving||!!pending||!saved" @click="load()">读取当前字段修订</button></header>
    <p v-if="!saved">先保存镜头。有确切结构化来源才能修订独立字段；请先在剧本页填写并解析，不能自动推造字段身份。</p>
    <p v-if="loading" role="status">正在读取独立字段修订…</p>
    <p v-if="error" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="stale">本次提交未成功，草稿保留。请先重新读取依据再修改，不会自动重发。</p>
    <div v-if="pending"><p>保存结果尚未确认，当前草稿已锁定。只允许用同一请求核对，不创建重复修订。</p><button type="button" class="button" :disabled="saving" @click="save()">核对并重试原请求</button></div>
    <template v-if="detail">
      <p>{{ historical?'已保存/历史回执（只读）':'当前读取版本' }}：{{ detail.revision?`人工修订 v${detail.revision.revision}`:'原结构化值（尚无人工修订）' }}</p>
      <p v-if="detail.revision">操作人 ID {{ detail.revision.created_by }} · {{ detail.revision.created_at }} · {{ detail.revision.id }}</p>
      <button v-if="detail.revision?.previous_id" type="button" class="button ghost small" :disabled="loading||saving||!!pending" @click="load(detail.revision.previous_id)">查看上一修订</button>
      <label class="field">选择独立字段<select v-model="selected" :disabled="loading||saving||!!pending||dirty" @change="choose"><option value="">请选择字段</option><option v-for="(row,index) in detail.fields" :key="index" :value="String(index)">{{ row.original.field_definition.label }} · {{ row.original.source_path }}</option></select></label>
      <p v-if="!detail.fields.length">原结构中没有属于本镜的独立字段。</p>
      <template v-if="field">
        <p>{{ field.original.field_definition.key }} · 定义 v{{ field.original.field_definition.version }} · {{ field.original.field_definition.field_definition_version_id }}</p>
        <details><summary>确切原值与来源</summary><p>结构 {{ field.original.structure_version_id }} · 节点 {{ field.original.node_key }} · {{ field.original.source_path }}</p><p>{{ field.original.source_present?'原输入提供':field.original.present?'配置默认值':'原字段缺失' }}</p><ReadonlyDataValue :value="field.original.value" label="原值" /></details>
        <p>{{ field.overridden?'本修订人工填写值':'本修订沿用原值' }}</p>
        <StructuredJsonEditor :key="`${selected}:${detail.revision?.id||'original'}`" :model-value="draft" label="独立字段值" :schemas="[field.schema]" :readonly="historical" :disabled="locked" @update:model-value="draft=$event;dirty=true" @validity="errors=$event" @draft-validity="draftErrors=$event" />
        <div class="actions" v-if="!historical"><button type="button" class="button" :disabled="locked||draft===undefined||!!errors.length||!!draftErrors.length||!dirty" @click="save('set')">另存字段修订</button><button type="button" class="button ghost" :disabled="locked||!field.overridden" @click="save('reset')">另存修订，恢复此字段原值</button><button type="button" class="button ghost" :disabled="locked||!dirty" @click="choose">放弃此字段草稿</button></div>
      </template>
    </template>
  </section>
</template>
<style scoped>
.shot-field-revisions{display:grid;gap:12px;padding:16px;min-width:0}.shot-field-revisions header{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.shot-field-revisions header>div{flex:1 1 250px;min-width:0}.shot-field-revisions :is(p,h3){margin:0;overflow-wrap:anywhere}.shot-field-revisions p{line-height:1.7;color:var(--text-secondary)}.shot-field-revisions select{width:100%;min-width:0;max-width:100%}.shot-field-revisions .actions{display:flex;flex-wrap:wrap;gap:8px}.shot-field-revisions summary{cursor:pointer;padding:8px 0}.shot-field-revisions :is(button,select,summary):focus-visible{outline:2px solid var(--accent)}
</style>
