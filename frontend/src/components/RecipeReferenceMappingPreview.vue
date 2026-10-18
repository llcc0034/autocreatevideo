<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { recipeObject as obj, recipeRows as rows } from '@/services/productionRecipeAuthoring'
import { referenceMappingPreview, type ReferenceMappingPreview } from '@/services/productionReferenceMapping'
import type { ManualTarget } from '@/services/productionManual'
import type { ProductionRun } from '@/services/platformTypes'
import StructuredJsonEditor from './StructuredJsonEditor.vue'
import RecipeStageInputAmendment from './RecipeStageInputAmendment.vue'
const props=defineProps<{run:ProductionRun}>()
const open=ref(false),busy=ref(false),error=ref(''),stageId=ref(''),shotId=ref(''),page=ref(1),hasNext=ref(false)
const targets=ref<ManualTarget[]>([]),result=ref<ReferenceMappingPreview|null>(null)
const contextNotice=ref('')
const candidate=ref<number|null>(null)
const candidateNumbers=computed(()=>{
  const stage=stages.value.find(row=>row.id===stageId.value)
  const declaration=rows(obj(obj(props.run.executionPlan?.value.recipe).definition).stages).find(row=>row.key===stage?.stageKey)
  const count=obj(declaration?.routing).variant_count
  return typeof count==='number'&&Number.isSafeInteger(count)&&count>0?Array.from({length:count},(_,index)=>index+1):[]
})
let sequence=0
onUnmounted(()=>{++sequence})
const awaitingStoryboardReview=computed(()=>props.run.currentStageKey==='storyboard_review'&&props.run.stages?.some(stage=>stage.stageKey==='storyboard_review'&&stage.status!=='succeeded'))
const sourceDeclarations=computed(()=>rows(obj(obj(props.run.executionPlan?.value.recipe).definition).stages)
  .filter(stage=>stage.key==='media_generation').flatMap(stage=>rows(obj(stage.routing).routes).map((route,index)=>({index,sources:obj(route.input_sources)}))))
const stages=computed(()=>{
  const declared=rows(obj(obj(props.run.executionPlan?.value.recipe).definition).stages)
  return props.run.stages?.filter(stage=>stage.stageKey==='media_generation'&&declared.some(row=>row.key===stage.stageKey&&rows(obj(row.routing).routes).some(route=>
    Object.keys(obj(route.input_sources)).length>0)))||[]
})
function invalidate(){++sequence;result.value=null;error.value='';busy.value=false}
// Scalar identities: a fresh Run object from polling is not a changed contract.
watch(()=>`${props.run.id}:${props.run.executionPlan?.sha256}`,()=>{invalidate();targets.value=[];shotId.value='';stageId.value='';open.value=false;contextNotice.value='';page.value=1;hasNext.value=false},{flush:'sync'})
watch(()=>props.run.stages?.map(row=>`${row.id}:${row.status}:${row.outputSnapshotSha256}:${row.selectedRouteIndex}:${row.routingDecisionSha256}`).join('|'),()=>{
  invalidate()
  if(open.value)contextNotice.value='上游审核或选路发生变化，原预览已失效；保留当前展开与选择，请重新预览。'
},{flush:'sync'})
watch([stageId,shotId,candidate],invalidate,{flush:'sync'})
watch(stageId,()=>{candidate.value=null})
async function load(next=1){
  shotId.value='';const current=++sequence;busy.value=true;error.value='';result.value=null;targets.value=[]
  try{const data=await platformApi.listProductionTargets(props.run.id,'shot',next);if(current!==sequence)return
    if(data.runPlanSha256!==props.run.executionPlan?.sha256)throw new Error('运行计划已变化，请重新读取运行详情')
    targets.value=data.results;page.value=next;hasNext.value=data.next!==null
  }catch(e){if(current===sequence)error.value=apiErrorSummary(e)}finally{if(current===sequence)busy.value=false}
}
async function preview(){
  if(!stageId.value||!shotId.value||!props.run.executionPlan||candidate.value===null)return
  if(awaitingStoryboardReview.value){error.value='当前分镜仍待审核。下方参考素材是草稿，批准后才能预览正式执行输入。';return}
  const current=++sequence,expected={plan:props.run.executionPlan.sha256,stage:stageId.value,shot:shotId.value,candidate:candidate.value};busy.value=true;error.value='';result.value=null
  contextNotice.value=''
  try{const data=await platformApi.previewProductionReferenceMapping(props.run.id,{stage_run_id:expected.stage,shot_id:expected.shot,candidate_number:expected.candidate,expected_run_plan_sha256:expected.plan});if(current===sequence)result.value=referenceMappingPreview(data,expected)
  }catch(e){if(current===sequence)error.value=apiErrorSummary(e)}finally{if(current===sequence)busy.value=false}
}
const pairingLabel=(key:string)=>result.value?.inputs.flatMap(input=>input.items).find(item=>item.item_key===key)?.name||key
</script>
<template>
  <section v-if="run.planSource==='recipe'&&stages.length" class="platform-card mapping-preview">
    <button class="button ghost small" type="button" @click="open=!open; open?load():invalidate()">{{open?'收起':'查看'}}逐镜输入映射</button>
    <template v-if="open">
      <p>按本轮冻结配方、已记录的输入修订和当前已审核内容预览素材、参考单元与上下文。已有准备的输入不会随上游更新；其实际输入请查看原准备详情。不创建任务、不调用模型。</p>
      <p v-if="awaitingStoryboardReview" role="status">当前分镜仍待审核：可以先查看冻结的来源设置。下方逐镜参考素材是可修改草稿，不代表正式执行输入；完成审核后再预览最终映射。</p>
      <p v-if="contextNotice" role="status">{{contextNotice}}</p>
      <details v-for="declaration in sourceDeclarations" :key="declaration.index"><summary>路线 {{declaration.index+1}} · 查看冻结来源设置</summary><p>输入字段与来源组（配方声明，非执行结果）</p><pre class="source-settings" tabindex="0" aria-label="冻结来源设置只读内容">{{JSON.stringify(declaration.sources,null,2)}}</pre></details>
      <label>生成阶段<select v-model="stageId" :disabled="busy"><option value="">请选择阶段</option><option v-for="stage in stages" :key="stage.id" :value="stage.id">{{stage.stageKey}}</option></select></label>
      <label>本轮镜头<select v-model="shotId" :disabled="busy"><option value="">请选择真实镜头</option><option v-for="target in targets" :key="target.scope.id" :value="target.scope.id">{{target.label}}</option></select></label>
      <label>候选编号<select v-model="candidate" :disabled="busy||!stageId"><option :value="null">请选择候选</option><option v-for="number in candidateNumbers" :key="number" :value="number">候选 {{number}}</option></select></label>
      <div><button class="button ghost tiny" :disabled="busy||page===1" @click="load(page-1)">上一页镜头</button> 第 {{page}} 页 <button class="button ghost tiny" :disabled="busy||!hasNext" @click="load(page+1)">下一页镜头</button> <button class="button ghost tiny" :disabled="busy" @click="load(page)">重新读取镜头</button></div>
      <button class="button primary small" :disabled="busy||!stageId||!shotId||candidate===null||awaitingStoryboardReview" @click="preview">{{busy?'正在核对…':'预览当前映射'}}</button>
      <p v-if="error" role="alert">{{error}}</p>
      <RecipeStageInputAmendment v-if="run.currentStageKey==='media_generation'&&stageId&&run.executionPlan" :run-id="run.id" :stage-id="stageId" :plan-sha="run.executionPlan.sha256" :sources="sourceDeclarations" @applied="invalidate(); contextNotice='输入修订已记录，请重新预览当前映射。未启动生成。'"/>
      <template v-if="result">
        <p v-if="result.input_amendment">使用输入修订 {{result.input_amendment.id}} · {{result.input_amendment.sha256}}</p>
        <details><summary>查看当前生效的输入来源设置（含修订）</summary><pre class="source-settings" tabindex="0">{{JSON.stringify(result.effective_input_sources,null,2)}}</pre></details>
        <p>候选 {{result.candidate_number}} · 路线 {{result.route_index+1}} · 执行实例版本 {{result.execution_instance_version_id}}</p>
        <p>{{result.notice}}</p>
        <details v-for="input in result.value_inputs" :key="input.key"><summary>{{input.key}} · 已解析内容</summary>
          <StructuredJsonEditor :model-value="input.value" :label="input.key" disabled/>
          <details><summary>查看此输入的来源证据</summary><StructuredJsonEditor :model-value="input.provenance" label="来源与精确审核身份" disabled/></details>
        </details>
        <article v-for="input in result.inputs" :key="input.key"><h4>{{input.key}} · {{input.items.length}} 项 / 允许 {{input.minimum}}–{{input.maximum}} 项</h4>
          <p v-if="!input.items.length">此可选输入没有匹配素材。</p>
          <ol><li v-for="item in input.items" :key="item.item_key"><strong>{{item.name}}</strong> · {{item.purpose===null?'未声明用途':item.purpose}} <span v-if="item.subject_ref">· 主体 {{item.subject_ref.kind}} / {{item.subject_ref.id}}</span><span v-if="item.paired_to"> · 配套视频：{{pairingLabel(item.paired_to)}}</span></li></ol>
        </article>
        <p>未映射参考 {{result.excluded_reference_keys.length}} 项，未自动补入其他槽位。</p>
      </template>
    </template>
  </section>
</template>
<style scoped>
.mapping-preview{display:grid;grid-template-columns:minmax(0,1fr);gap:10px;padding:18px;min-width:0}.mapping-preview>*{min-width:0}.mapping-preview label{display:grid;gap:5px;min-width:0}.mapping-preview select{width:100%;max-width:100%;padding:8px;background:var(--bg-input,#090c10);color:inherit;border:1px solid var(--border,#303743);min-width:0}.mapping-preview p{margin:0;color:var(--text-secondary);overflow-wrap:anywhere}.mapping-preview article{border-top:1px solid var(--border);overflow-wrap:anywhere}.mapping-preview [role=alert]{color:#e7b669}
.source-settings{max-height:320px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;padding:12px;background:#0a0f13;border:1px solid var(--border);border-radius:6px}.mapping-preview select,.mapping-preview option{background:#111820;color:#d4dbe2;color-scheme:dark}.mapping-preview summary{cursor:pointer;min-height:36px;padding:8px 0;overflow-wrap:anywhere}
</style>
