<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, ref, watch } from 'vue'
import { taskCenterApi } from '@/services/taskCenterApi'
import { taskTime, adaptTaskDetail, type TaskDetail, type TaskKind } from '@/services/taskCenter'
import { platformApi } from '@/services/platformApi'
import { repository } from '@/services/repository'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { useAuthStore } from '@/stores/auth'
import TaskInputFacts from './TaskInputFacts.vue'
import TaskSubmissionEvidence from './TaskSubmissionEvidence.vue'
import PlatformStatusPill from './PlatformStatusPill.vue'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import BaseModal from './BaseModal.vue'
const ExecutionPreparationWorkbench=defineAsyncComponent(()=>import('./ExecutionPreparationWorkbench.vue'))
const TaskMediaInspector=defineAsyncComponent(()=>import('./TaskMediaInspector.vue'))
const props=defineProps<{kind:TaskKind;id:string;attemptId?:string;readonly?:boolean;expectedProjectId?:string}>()
const emit=defineEmits<{changed:[];busy:[value:boolean];dirty:[value:boolean];select:[kind:TaskKind,id:string,attemptId?:string];source:[id:string]}>()
const auth=useAuthStore(), data=ref<TaskDetail|null>(null), loading=ref(false), error=ref(''), tab=ref('input')
const busy=ref(false),dirty=ref(false),callPage=ref(1),outputPage=ref(1),reading=ref(''),notice=ref('')
const readSuccessNotice='完整正文已读取；内容来自本次保存记录。'
let serial=0, readSerial=0, disposed=false, pollTimer:ReturnType<typeof setTimeout>|undefined
const refreshPending=ref(false),selectedAttempt=ref('')
const cancelTarget=ref<{id:string;kind:string;sourceId:string;sourceKind:string;title:string;scope:string}|null>(null),cancelError=ref('')
let cancelSerial=0
const cancelScope=()=>JSON.stringify([props.kind,props.id,props.attemptId||'',auth.user?.id??null])
const cancellable=(task:TaskDetail|null)=>!!task?.source_id&&(task.source_kind==='asset_job'?task.actions.cancel_asset:['workflow_smoke','deployment_smoke'].includes(task.source_kind)&&task.actions.cancel_smoke)
const contentCache=new Map<string,{scope:string;sha256:string;value:unknown}>()
const contentScope=(d:TaskDetail,field:string,page:number)=>JSON.stringify([d.kind,d.id,d.project_id,d.request_sha256,d.source_kind,d.source_id,field.startsWith('output.')?page:0])
const locked=computed(()=>busy.value||dirty.value||!!reading.value||!!cancelTarget.value)
const deferred=(field:string)=>data.value?.deferred_fields.find(item=>item.field===field)
const stateLabels:Record<string,string>={frozen:'已冻结',not_frozen:'尚未冻结',available:'已记录',awaiting_review:'待人工修正',unavailable:'尚无记录',error:'校验异常',deferred:'大文本待加载',recorded:'已记录',valid:'冻结输入校验通过',invalid:'记录校验未通过'}
const purpose=computed(()=>data.value?.purpose || '')
watch([busy,reading,cancelTarget],()=>emit('busy',busy.value||!!reading.value||!!cancelTarget.value))
watch(dirty,v=>emit('dirty',v))
watch(locked,value=>{if(!value&&refreshPending.value){refreshPending.value=false;void load()}})
function schedulePoll(){
 clearTimeout(pollTimer)
 if(props.readonly||disposed||error.value||props.attemptId||!data.value||!['preparing','queued','running','submitting','submitted','cancel_requested','dispatched'].includes(data.value.status))return
 pollTimer=setTimeout(()=>{if(document.hidden||locked.value||loading.value)schedulePoll();else void load()},5000)
}
async function load(requestedCallPage=callPage.value,requestedOutputPage=outputPage.value,attemptId=selectedAttempt.value) {
 if(locked.value){refreshPending.value=true;return}
 clearTimeout(pollTimer)
 const n=++serial;loading.value=true;error.value=''
 if(notice.value===readSuccessNotice)notice.value=''
 try{const result=await taskCenterApi.detail(props.kind,props.id,{call_page:requestedCallPage,output_page:requestedOutputPage,...(attemptId?{attempt_id:attemptId}:{})})
 if(n!==serial)return
 if(result.id!==props.id||result.kind!==props.kind)throw Error('详情与当前任务不一致')
 if(props.expectedProjectId&&result.project_id!==props.expectedProjectId)throw Error('历史准备不属于当前项目，已停止显示')
 if(attemptId&&(result.source_kind!=='generation_attempt'||result.source_id!==attemptId))throw Error('详情与指定调用身份不一致，已拒绝显示返回的调用；请重试或返回最新调用')
 for(const field of [...result.deferred_fields]){
   const saved=contentCache.get(field.field)
   if(saved&&saved.sha256===field.sha256&&saved.scope===contentScope(result,field.field,requestedOutputPage)){
     const [section,key]=field.field.split('.');(result[section as 'input'|'output'] as any)[key]=saved.value
     result.deferred_fields=result.deferred_fields.filter(item=>item.field!==field.field)
   }
 }
 data.value=result;callPage.value=requestedCallPage;outputPage.value=requestedOutputPage;selectedAttempt.value=attemptId
 }catch(e){if(n===serial)error.value=apiErrorSummary(e)}finally{if(n===serial){loading.value=false;schedulePoll()}}
}
// Keep the refresh control focusable while reading so keyboard focus stays
// inside its dialog. aria-disabled must be backed by this action guard.
async function refresh(){if(!loading.value&&!locked.value)await load()}
async function readField(field:string) {
 if(locked.value||loading.value||!data.value)return
 const original=data.value,originalPage=outputPage.value
 const fixedAttempt=original.source_kind==='generation_attempt'?original.source_id:selectedAttempt.value
 const scope=contentScope(original,field,originalPage),expectedHash=deferred(field)?.sha256
 const n=++readSerial;reading.value=field;error.value='';notice.value=''
 let offset=0,parts='',hash='',total=-1
 try {
 while(true){
 const result=await taskCenterApi.detail(props.kind,props.id,{call_page:callPage.value,output_page:originalPage,...(fixedAttempt?{attempt_id:fixedAttempt}:{}),content_field:field,content_offset:offset,content_limit:65536})
 if(n!==readSerial)return
 const c=result.content
 if(contentScope(result,field,originalPage)!==scope||!c||c.field!==field||c.offset!==offset||typeof c.text!=='string'||!c.sha256||(expectedHash&&expectedHash!==c.sha256)||(hash&&hash!==c.sha256)||(total!==-1&&total!==c.total_characters))throw Error('正文读取期间记录发生变化，请刷新详情后重试')
 hash=c.sha256;total=c.total_characters;parts+=c.text
 if(c.next_offset===null)break
 if(!Number.isSafeInteger(c.next_offset)||c.next_offset<=offset)throw Error('正文分页无进展')
 offset=c.next_offset
 }
 const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(parts)))).map(x=>x.toString(16).padStart(2,'0')).join('')
 if(n!==readSerial)return
 if(Array.from(parts).length!==total||digest!==hash)throw Error('完整正文长度或摘要不一致，请重新读取')
 const value=JSON.parse(parts),[section,key]=field.split('.')
 if(!data.value||!['input','output'].includes(section)||!key)throw Error('正文归属无效')
 data.value=adaptTaskDetail({...data.value,[section]:{...data.value[section as 'input'|'output'],[key]:value},deferred_fields:data.value.deferred_fields.filter(f=>f.field!==field)})
 contentCache.set(field,{scope,sha256:hash,value})
 notice.value=readSuccessNotice
 }catch(e){if(n===readSerial)error.value=apiErrorSummary(e)}finally{if(n===readSerial)reading.value=''}
}
function cancelReading(){readSerial++;reading.value=''}
async function copy(value:string){try{await navigator.clipboard.writeText(value);notice.value='已复制'}catch{notice.value='复制失败，请选择正文复制'}}
function changeTab(value:string){if(((props.readonly||props.attemptId)&&value==='media')||(locked.value&&value!==tab.value))return;tab.value=value}
async function pageCalls(direction:number){await load(callPage.value+direction,outputPage.value)}
async function pageOutput(direction:number){await load(callPage.value,outputPage.value+direction)}
function openCancelTask(){
 const task=data.value
 if(props.readonly||props.attemptId||disposed||loading.value||locked.value||!task||!task.source_id||!cancellable(task))return
 cancelError.value=''
 cancelTarget.value={id:task.id,kind:task.kind,sourceId:task.source_id,sourceKind:task.source_kind,title:task.title,scope:cancelScope()}
}
function closeCancelTask(){if(!busy.value){cancelTarget.value=null;cancelError.value=''}}
async function cancelTask(){
 const target=cancelTarget.value,task=data.value
 if(props.readonly||props.attemptId||disposed||!target||busy.value||dirty.value||reading.value||loading.value)return
 if(target.scope!==cancelScope()||!task||!cancellable(task)||task.id!==target.id||task.kind!==target.kind||task.source_id!==target.sourceId||task.source_kind!==target.sourceKind){closeCancelTask();return}
 const n=++cancelSerial
 const current=()=>!disposed&&n===cancelSerial&&target.scope===cancelScope()
 busy.value=true;cancelError.value='';error.value=''
 try{if(target.sourceKind==='workflow_smoke')await platformApi.cancelWorkflowSmokeAttempt(target.sourceId)
 else if(target.sourceKind==='deployment_smoke')await platformApi.cancelDeploymentSmokeAttempt(target.sourceId)
 else await repository.cancelAssetGenerationJob(target.sourceId)
 if(!current())return
 cancelTarget.value=null;notice.value='取消请求已受理；正在重新读取原任务状态，不代表远端已停止或费用已结清。'
 refreshPending.value=true;emit('changed')
 }catch(e){if(current())cancelError.value=apiErrorSummary(e)}finally{if(current())busy.value=false}
}
function changed(){emit('changed');void load()}
watch(()=>[props.kind,props.id,props.attemptId,props.readonly,props.expectedProjectId,auth.user?.id],()=>{serial++;readSerial++;cancelSerial++;cancelTarget.value=null;cancelError.value='';busy.value=false;dirty.value=false;reading.value='';refreshPending.value=false;notice.value='';contentCache.clear();data.value=null;callPage.value=1;outputPage.value=1;selectedAttempt.value=props.attemptId||'';tab.value='input';void load()},{immediate:true})
onBeforeUnmount(()=>{disposed=true;serial++;readSerial++;cancelSerial++;clearTimeout(pollTimer)})
</script>
<template>
 <section class="task-detail" :aria-busy="loading">
 <div class="detail-actions"><button class="button ghost small" :disabled="locked" :aria-disabled="loading||locked" @click="refresh">刷新此任务</button><button v-if="!readonly&&!props.attemptId&&cancellable(data)" class="button danger small" :disabled="locked||loading" @click="openCancelTask">{{data?.source_kind==='asset_job'?'取消任务':'请求取消此烟测'}}</button><span v-if="loading">正在读取任务详情…</span><span role="status">{{notice}}</span></div>
 <p v-if="error" class="platform-alert danger" role="alert">{{error}}<span v-if="data"> · 当前保留上次读取结果，尚未更新。</span></p>
 <p v-if="props.attemptId">当前查看指定调用：{{props.attemptId}} <button class="button ghost small" :disabled="locked" @click="emit('select',props.kind,props.id)">返回最新调用</button></p>
 <template v-if="data">
 <p v-if="readonly" role="status">历史准备只读：不改选、不审核、不取消或重发调用。当前分镜编辑不替换此处原请求；尚未冻结时明确显示未完成准备。</p>
 <header class="task-summary"><div><h3>{{data.title}}</h3><p>{{data.execution_instance_name||'未关联业务执行实例'}}<span v-if="data.execution_version!==null"> · v{{data.execution_version}}</span> · {{data.operation||'未记录执行能力'}}</p></div><PlatformStatusPill :status="data.status"/></header>
 <p v-if="data.outcome" class="platform-alert" aria-label="调用与产物状态">{{data.outcome.message}}</p>
 <dl class="task-facts"><div><dt>创建 / 完成</dt><dd>{{taskTime(data.created_at)}}<br/>{{taskTime(data.finished_at)}}</dd></div><div><dt>估算 / 实际费用</dt><dd>{{data.estimated_cost??'未知'}} / {{data.actual_cost??'未知'}} {{data.cost_currency}}</dd></div><div><dt>项目</dt><dd><RouterLink v-if="data.project_id" :to="'/projects/'+data.project_id">{{data.project_title||'查看项目'}}</RouterLink><span v-else>不归属项目</span></dd></div><div><dt>准备状态</dt><dd>{{data.preparation_status||'无准备关联'}}</dd></div></dl>
 <details class="identity"><summary>任务身份与冻结证据</summary><dl><dt>任务记录</dt><dd>{{data.kind}} · {{data.id}}</dd><dt>原始调用</dt><dd>{{data.source_kind}} · {{data.source_id??'尚无主调用'}}</dd><dt>实例版本 ID</dt><dd>{{data.execution_instance_version_id??'未关联'}}</dd><dt>冻结时间</dt><dd>{{taskTime(data.request_frozen_at)}}</dd><dt>请求摘要</dt><dd>{{data.request_sha256??'尚未冻结'}}</dd></dl></details>
 <RouterLink v-if="data.project_id&&data.production_run_id" :to="'/projects/'+data.project_id+'/production/'+data.production_run_id">返回本轮生产工作台</RouterLink>
 <p v-if="data.anomaly" class="platform-alert warning">此任务存在关联异常。保留原始记录供核对，不伪装为其他任务。</p>
 <nav class="detail-tabs" aria-label="任务详情分区"><button v-for="item in [{id:'input',label:'输入与实际提示词'},{id:'output',label:'输出与校验'},{id:'calls',label:'准备与调用'}]" :key="item.id" class="button ghost small" :class="{active:tab===item.id}" :aria-pressed="tab===item.id" :disabled="locked&&tab!==item.id" @click="changeTab(item.id)">{{item.label}}</button><button v-if="!readonly&&!props.attemptId&&(data.links.generation_task_id||data.links.asset_job_id)" class="button ghost small" :class="{active:tab==='media'}" :disabled="locked&&tab!=='media'" @click="changeTab('media')">素材操作与媒体诊断</button></nav>
 <p v-if="locked">正在处理任务或有未保存编辑；请先完成当前操作再切换或关闭。</p>
 <div v-if="reading" class="detail-actions" role="status">正在读取完整正文… <button class="button ghost small" @click="cancelReading">停止读取</button></div>
 <TaskInputFacts v-show="tab==='input'" :data="data" :reading="reading||(loading?'正在读取':'')" @read="readField" @copy="copy"/>
 <section v-show="tab==='output'" class="detail-section">
 <h4>输出结果 · {{stateLabels[data.output.state]||data.output.state}}</h4>
 <div class="platform-alert validation-summary" :class="data.status==='failed'||data.validation.status==='invalid'?'danger':''"><strong>记录校验：{{stateLabels[data.validation.status]||data.validation.status}}</strong><p v-if="data.validation.error_code||data.validation.error_message">{{data.validation.error_code}} {{data.validation.error_message}}</p><p>调用状态、输出记录与选择结果分别展示；程序校验通过不等于内容符合人工预期，选择方式由对应流程决定。</p><p v-for="w in data.validation.warnings" :key="w">{{w}}</p></div>
 <p v-for="note in data.output.notes" :key="note" class="muted">{{note}}</p>
 <button v-if="!readonly&&data.status==='awaiting_review'&&data.preparation_id" class="button primary small" :disabled="locked" @click="props.attemptId?emit('select','preparation',data.preparation_id):changeTab('calls')">查看原因并修正响应（不调用模型）</button>
 <button v-if="deferred('output.text')" class="button ghost small" :disabled="!!reading" @click="readField('output.text')">加载完整输出正文</button><article v-else-if="data.output.text" class="prompt-card"><header><strong>输出正文</strong><button class="button ghost small" @click="copy(data.output.text)">复制正文</button></header><pre tabindex="0">{{data.output.text}}</pre></article>
 <button v-if="deferred('output.result')" class="button ghost small" :disabled="!!reading" @click="readField('output.result')">加载完整结构化结果</button><ReadonlyDataValue v-else :value="data.output.result" label="结构化输出与校验证据"/>
 <button v-if="deferred('output.media')" class="button ghost small" :disabled="!!reading" @click="readField('output.media')">加载完整媒体记录</button><ReadonlyDataValue v-else-if="data.output.media.length" :value="data.output.media" label="输出媒体记录（可在媒体诊断中预览）"/>
 <div class="detail-actions"><span>产物共 {{data.output_count}} 项 · 第 {{outputPage}} 页</span><button class="button ghost small" :disabled="loading||locked||!data.output_previous" @click="pageOutput(-1)">上一页产物</button><button class="button ghost small" :disabled="loading||locked||!data.output_next" @click="pageOutput(1)">下一页产物</button></div>
 </section>
 <section v-if="tab==='calls'" class="detail-section">
 <TaskSubmissionEvidence :evidence="data.submission" :source-kind="data.source_kind" :source-id="data.source_id"/>
 <h4>调用记录 · 共 {{data.call_count}} 项</h4><p>主调用与辅助调用归在当前准备下；重试保留独立调用事实。仅统计已创建的任务。</p>
 <div class="table-scroll"><table><thead><tr><th>调用 / 角色</th><th>状态</th><th>时间</th><th>关联</th></tr></thead><tbody><tr v-for="c in data.calls" :key="c.kind+':'+c.id"><td>{{c.operation||c.kind}}<small>{{c.role==='primary'?'主调用':'辅助调用'}} · {{c.id}}</small></td><td><PlatformStatusPill :status="c.status"/><small>{{c.error_code}}</small></td><td>{{taskTime(c.created_at)}}<small>完成：{{taskTime(c.finished_at)}}</small></td><td><button v-if="c.preparation_id&&c.preparation_id!==data.preparation_id" class="button ghost small" :disabled="locked" @click="emit('select','preparation',c.preparation_id,c.kind==='generation_attempt'?c.id:undefined)">查看此调用</button><button v-else-if="c.kind==='generation_attempt'" class="button ghost small" :disabled="locked||loading" @click="emit('select',props.kind,props.id,c.id)">查看本次输出</button><span v-else>当前任务</span><small v-if="c.retry_of_id">重试来源：{{c.retry_of_id}}</small></td></tr></tbody></table></div>
 <div class="detail-actions"><button class="button ghost small" :disabled="loading||locked||!data.calls_previous" @click="pageCalls(-1)">上一页调用</button><span>第 {{callPage}} 页</span><button class="button ghost small" :disabled="loading||locked||!data.calls_next" @click="pageCalls(1)">下一页调用</button></div>
 <p v-if="props.attemptId">指定调用为只读历史。需要处理当前准备时，请先返回最新调用。</p>
 <ExecutionPreparationWorkbench v-if="!readonly&&!props.attemptId&&data.preparation_id&&purpose" :key="data.preparation_id" :preparation-id="data.preparation_id" :version-id="data.execution_instance_version_id" :purpose="purpose" :allow-smoke="!!auth.user?.isStaff" @busy="busy=$event" @dirty="dirty=$event" @changed="changed" @smoke-created="emit('select','preparation',$event)"/>
 </section>
 <TaskMediaInspector v-if="!readonly&&!props.attemptId&&tab==='media'" :key="data.kind+data.id" :task-id="data.links.asset_job_id?null:data.links.generation_task_id" :asset-job-id="data.links.asset_job_id" @busy="busy=$event" @dirty="dirty=$event" @changed="changed" @select="emit('source',$event)"/>
 </template>
 <BaseModal :open="!!cancelTarget" title="确认取消任务" :dismissible="!busy" @close="closeCancelTask">
  <template v-if="cancelTarget"><p>请求取消 {{cancelTarget.title}}？</p><p class="cancel-identity">原始调用：{{cancelTarget.sourceKind}} · {{cancelTarget.sourceId}}</p><p>已提交的调用仍可能计费；不会删除原输入、输出、审核或费用记录，也不会自动重试。未提交的主调用将停止准备。</p></template>
  <p v-if="cancelError" class="platform-alert danger" role="alert">{{cancelError}} · 尚未确认取消结果，请核对原任务。</p>
  <template #footer><button class="button ghost" :disabled="busy" @click="closeCancelTask">暂不取消</button><button class="button danger" :disabled="busy||loading||dirty||!!reading||!cancelTarget" @click="cancelTask">{{busy?'正在请求取消…':'确认请求取消'}}</button></template>
 </BaseModal>
 </section>
</template>
<style scoped>
.validation-summary{display:grid;grid-template-columns:minmax(0,1fr);min-width:0;overflow-wrap:anywhere}
.cancel-identity{overflow-wrap:anywhere;font-size:12px}
.detail-actions button[aria-disabled="true"]{opacity:.55;cursor:wait}
.task-detail{display:grid;gap:16px;min-width:0}.detail-actions,.task-summary,.detail-tabs,.prompt-card header{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.task-summary,.prompt-card header{justify-content:space-between}.task-summary h3,.detail-section h4{margin:0}.task-summary p,.muted,.task-facts dt,small{color:var(--text-secondary);font-size:12px}.task-facts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:0}.task-facts>div{border:1px solid var(--border);border-radius:8px;padding:12px}.task-facts dd{margin:8px 0 0;font-size:12px;line-height:1.8}.identity{font-size:12px}.identity summary{cursor:pointer}.identity dl{display:grid;grid-template-columns:100px 1fr;gap:8px}.identity dd{margin:0;overflow-wrap:anywhere}.detail-tabs{border-bottom:1px solid var(--border);padding-bottom:12px}.detail-tabs .active{border-color:var(--accent);color:var(--accent)}.detail-section{display:grid;gap:14px;min-width:0}.detail-section p{font-size:13px;line-height:1.8;margin:0}.prompt-card{border:1px solid var(--border);border-radius:8px;padding:14px;min-width:0}.prompt-card pre{max-height:420px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.9;margin:12px 0 0;background:var(--bg-input);padding:12px}.table-scroll{overflow:auto;max-height:440px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--border);overflow-wrap:anywhere;max-width:560px}small{display:block;line-height:1.8}td small{max-width:280px}.detail-actions{font-size:12px}.platform-alert{padding:12px;border:1px solid var(--border);border-radius:8px}.platform-alert.danger{border-color:var(--danger);color:var(--danger)}@media(max-width:700px){.task-facts{grid-template-columns:1fr 1fr}th,td{min-width:110px}.identity dl{grid-template-columns:1fr}}
</style>
