<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { platformApi } from '@/services/platformApi'
import { apiErrorPresentation, apiErrorSummary } from '@/services/apiErrorPresentation'
import { isDefinitiveCreationRejection } from '@/services/productionOnboarding'
import type { JsonObject, ProductionRun } from '@/services/platformTypes'
import type { ManualActionOptions } from '@/services/productionManual'
import { readManualStoryboardReferences, buildManualStoryboardReferenceApproval, manualStoryboardReferencePreview, type ManualStoryboardReferenceReview } from '@/services/productionManualStoryboardReview'
import ExactReferencePreview from './ExactReferencePreview.vue'
import StoryboardShotRelations from './StoryboardShotRelations.vue'
import ShotExecutionEvidence from './ShotExecutionEvidence.vue'
import {readStoryboardReviewDisplay} from '@/services/storyboardReviewDisplay'

const props=defineProps<{run:ProductionRun;options:ManualActionOptions}>()
const emit=defineEmits<{changed:[];busy:[boolean]}>()
const detail=ref<ManualStoryboardReferenceReview|null>(null),loading=ref(false),submitting=ref(false),error=ref(''),notice=ref('')
const checked=ref<string[]>([]),comment=ref(''),pending=ref<JsonObject|null>(null),key=ref(crypto.randomUUID())
const terminal=computed(()=>['succeeded','failed','cancelled'].includes(props.run.status))
const ready=computed(()=>detail.value?.ready&&detail.value.references.every(row=>checked.value.includes(row.shot_id)))
const sources=computed(()=>Object.fromEntries(detail.value?.source_options.map(row=>[row.value,row.label])??[]))
const statuses={not_reviewed:'尚未审核',current:'与已审核内容一致',changed:'内容或上游依据已变化，需要重新核对'}
const references=computed(()=>new Map(detail.value?.references.map(row=>[row.shot_id,row.items])))
const assets=computed(()=>new Map(detail.value?.contract?.assets.map(row=>[row.id,row])))
const display=computed(()=>detail.value?.ready ? readStoryboardReviewDisplay(detail.value.display,detail.value.references.map(row=>row.shot_id)) : null)
const relations=computed(()=>new Map(display.value?.shots.map(row=>[row.shot_id,row])))
const orderedEpisodes=computed(()=>[...(detail.value?.contract?.canonical.episodes||[])].sort((a,b)=>a.number-b.number||a.id.localeCompare(b.id))
  .map(episode=>({...episode,shots:[...episode.shots].sort((a,b)=>a.number-b.number||a.id.localeCompare(b.id))})))
const previews=computed(()=>new Map(detail.value?.references.flatMap(row=>row.items.map(item=>[
  `${row.shot_id}:${item.reference_key}`, manualStoryboardReferencePreview(detail.value!,row.shot_id,item)]))))
let sequence=0
async function load(){
  if(submitting.value||pending.value)return
  const token=++sequence;loading.value=true;error.value='';detail.value=null;checked.value=[]
  try{const raw=await platformApi.getManualStoryboardReview(props.run.id);if(token!==sequence)return
    detail.value=readManualStoryboardReferences(raw,props.run);pending.value=null;key.value=crypto.randomUUID()
  }catch(e){if(token===sequence)error.value=apiErrorSummary(e)}finally{if(token===sequence)loading.value=false}
}
async function approve(){
  if(loading.value||submitting.value||terminal.value||!detail.value||(!pending.value&&!ready.value))return
  submitting.value=true;error.value='';notice.value='';emit('busy',true);const token=sequence,retrying=Boolean(pending.value)
  try{
    pending.value??=buildManualStoryboardReferenceApproval(detail.value,props.run,props.options,key.value,checked.value,comment.value)
    const result=await platformApi.performManualProductionAction(props.run.id,pending.value)
    if(result.action!=='approve'||result.result_kind!=='approval_decision'||typeof result.target_id!=='string'
      ||!/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(result.target_id))throw Error('未返回正式审核决定，请重试同一请求核对')
    if(token!==sequence)return
    pending.value=null;notice.value='已保存逐镜参考审核。没有运行模型、分配素材槽位或自动开始下一步。';emit('changed')
  }catch(e){if(token===sequence){
    error.value=apiErrorSummary(e)
    const conflict=(e as {response?:{status?:number}})?.response?.status===409&&apiErrorPresentation(e).code==='manual_selection_conflict'
    // Only an initial explicit rejection proves no write. A failed lookup of
    // an earlier unknown command must retain its original body and key.
    if(!retrying&&(isDefinitiveCreationRejection(e)||conflict)){
      pending.value=null;detail.value=null;checked.value=[];key.value=crypto.randomUUID()
    }
  }}finally{submitting.value=false;emit('busy',Boolean(pending.value))}
  if(notice.value)await load()
}
watch(()=>props.run.id,()=>{pending.value=null;notice.value='';void load()},{immediate:true})
onBeforeUnmount(()=>{sequence++})
</script>

<template>
  <section class="reference-review">
    <header><h4>逐镜参考素材审核</h4><button class="button ghost small" :disabled="loading||submitting||Boolean(pending)" @click="load">重新读取全部镜头</button></header>
    <p>这里只确认已采用镜头的素材身份、用途与配对关系。素材不足、数量范围和模型槽位会在创建执行准备时按所选实例合同检查，不代表烟测通过。</p>
    <p v-if="loading" role="status">正在读取本轮镜头与参考清单…</p><p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="pending&&!submitting" class="platform-alert warning" role="status">审核结果尚未确认，可能已经保存。已保留原请求；请重试同一审核请求核对回执，暂不刷新、关闭或改选。</p>
    <template v-if="detail">
      <p>{{ statuses[detail.approval.status] }} · 审核修订 {{ detail.approval.revision }}</p>
      <p v-if="!detail.ready" class="platform-alert warning">{{ detail.reason }}</p>
      <template v-if="detail.contract">
        <section v-for="episode in orderedEpisodes" :key="episode.id" class="episode">
          <header><h5>第{{ episode.number }}集 · {{ episode.title }}</h5><RouterLink v-if="!submitting&&!pending" :to="{name:'storyboard',params:{projectId:run.project,episodeId:episode.id}}">查看 / 编辑原分镜</RouterLink></header>
          <article v-for="shot in episode.shots" :key="shot.id">
            <h6>镜头{{ shot.number }} · {{ shot.title }}</h6>
            <StoryboardShotRelations :shot="relations.get(shot.id)!"/>
            <p v-if="!references.get(shot.id)!.length">这个镜头当前没有绑定参考素材；这不保证所选模型可以无参考执行。</p>
            <ul v-else class="use-list">
              <li v-for="item in references.get(shot.id)!" :key="item.reference_key">
                <strong>{{ item.reference_key.startsWith('shot-use:') ? item.reference_key.slice(9) : sources[item.source_key] }} · {{ sources[item.source_key] }}</strong>
                <p>创作描述：{{ item.purpose || '未填写（可选，不自动推断）' }}</p>
                <p>{{ previews.get(`${shot.id}:${item.reference_key}`)!.sourceLabel }}</p>
                <ExactReferencePreview v-if="previews.get(`${shot.id}:${item.reference_key}`)!.selection" :selection="previews.get(`${shot.id}:${item.reference_key}`)!.selection!" :name="assets.get(item.source.asset_id)!.name"/>
                <p v-else class="platform-alert danger" role="alert">{{ previews.get(`${shot.id}:${item.reference_key}`)!.error }}</p>
                <details v-if="item.subject_ref || item.paired_reference_key"><summary>原主体 / 配对身份</summary><p v-if="item.subject_ref">{{ item.subject_ref.kind }} · {{ item.subject_ref.id }}</p><p v-if="item.paired_reference_key">配对引用：{{ item.paired_reference_key }}</p></details>
              </li>
            </ul>
            <label><input v-model="checked" type="checkbox" :value="shot.id" :disabled="submitting||terminal||Boolean(pending)">我已核对这个镜头的参考清单（无参考素材时也已明确确认）。</label>
            <ShotExecutionEvidence :project-id="run.project" :shot-id="shot.id"/>
          </article>
        </section>
        <details><summary>上游审核依据与完整合同</summary><pre>{{ JSON.stringify(detail.contract,null,2) }}</pre></details>
        <label>审核说明（可选）<textarea v-model="comment" rows="2" :disabled="submitting||terminal||Boolean(pending)"/></label>
        <button class="button primary small" :disabled="loading||submitting||terminal||(!pending&&!ready)" @click="approve">{{ submitting?'正在提交…':pending?'重试同一审核请求':'确认已逐镜核对参考素材' }}</button>
      </template>
    </template>
  </section>
</template>

<style scoped>
.reference-review,.episode{display:grid;gap:12px;min-width:0}.reference-review header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.reference-review h4,.reference-review h5,.reference-review h6,.reference-review p{margin:0}.reference-review h6{font-size:inherit}.reference-review article{display:grid;gap:10px;border:1px solid var(--border);border-radius:8px;padding:14px;min-width:0}.reference-review .content,.reference-review pre{white-space:pre-wrap;overflow-wrap:anywhere}.reference-review label{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.reference-review textarea{width:100%}
.use-list{display:grid;gap:12px;list-style:none;padding:0;margin:0}.use-list li{display:grid;gap:8px;min-width:0;overflow-wrap:anywhere}
</style>
