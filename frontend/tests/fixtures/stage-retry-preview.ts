// Browser proof uses the real component with explicit simulated reads only.
import {createApp,h,ref} from 'vue'
import {createRouter,createMemoryHistory} from 'vue-router'
import BaseModal from '../../src/components/BaseModal.vue'
import ProductionRetryPreview from '../../src/components/ProductionRetryPreview.vue'
import {platformApi} from '../../src/services/platformApi'
import {api} from '../../src/services/api'
import {adaptRetryPreview} from '../../src/services/productionRetryPreview'
import {retryPreviewFixture,assemblyRetryPreviewFixture} from './retry-preview'
import {productionIssuePresentation,productionRunCanRetry} from '../../src/services/productionRunPresentation'
import '../../src/styles.css'
import '../../src/platform.css'

api.defaults.adapter=async()=>{throw Error('隔离夹具禁止业务网络请求')}
const mode=ref('mixed'),open=ref(false),ready=ref(false),reads=ref(0)
const recovery=ref({status:'awaiting_input',blockedCode:'waiting_material_recovery',reason:''})
const retryStates=[
  {label:'配置重试等待',status:'running',blockedCode:'execution_retry_wait',reason:'1 个失败调用：已尝试 1/3 次（含首次）；下一次自动重试不早于 2026-10-11T14:00:10+08:00。已成功任务与产物保留，不会重新生成。'},
  {label:'正在重试',status:'running',blockedCode:'execution_retry_running',reason:'1 个失败调用已进入重试队列或正在重试；每个原请求最多 3 次（含首次）。成功任务和已采用字段不会重跑。'},
  {label:'耗尽转人工',status:'awaiting_input',blockedCode:'execution_retry_exhausted',reason:'1 个失败调用：已尝试 3/3 次（含首次），自动重试已耗尽，请人工处理。已成功任务与产物保留，不会重新生成。'},
]
platformApi.getProductionRetryPreview=async(_id,_stage,page=1)=>{
  reads.value++
  if(mode.value==='error')throw Error('隔离示例：预览读取失败')
  if(mode.value==='assembly' || mode.value==='assembly-drift'){
    const raw=assemblyRetryPreviewFixture()
    if(mode.value==='assembly-drift')raw.retry_context.expected_input_sha256='b'.repeat(64)
    return adaptRetryPreview(raw)
  }
  const raw=retryPreviewFixture()
  if(mode.value==='material'){
    raw.message='仅重试候选全部失败的素材单元；已有成功候选或仍在执行的单元跳过，不改原选择和主图依据。'
    raw.call_count=3;raw.preparation_count=0
    raw.results[0]!.unit_key='隔离外观：全部候选失败'
    raw.results[1]!.unit_key='隔离外观：已有成功候选'
    raw.results[2]={id:'peer',kind:'call',unit_key:'隔离外观：已有成功候选',preparation_id:'prep-peer',attempt_id:'peer',status:'failed',error_code:'fixture_failure',disposition:'skip',message:'同一素材单元已有成功候选或仍在执行，不重发其失败同伴；原调用记录保留。'}
  }
  if(mode.value==='unknown'){
    raw.can_submit=false;raw.reason='失败集合仍有远端结果未知的调用，本次整批不会提交。'
    raw.results[0]!.disposition='attention';raw.results[0]!.message='远端结果未知，请恢复原任务查询；不会直接重新提交。'
    raw.results[0]!.error_code='uncertain_remote_response'
  }
  const all=raw.results
  raw.results=page===1?all.slice(0,2):all.slice(2)
  return adaptRetryPreview({...raw,next:page===1?'?page=2':null,previous:page===2?'?page=1':null})
}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',name:'workflows',component:{render:()=>h('p','隔离任务链接目标')}}]})
const context={expectedStageId:'stage',expectedAttemptIds:['failed']}
const assemblyContext=adaptRetryPreview(assemblyRetryPreviewFixture()).retryContext
createApp({render:()=>h('main',{style:{maxWidth:'1000px',margin:'20px auto',padding:'16px'}},[
  h('h1','阶段重试影响 · 隔离样例'),h('p','组件验证，不连接业务 API / 模型，不执行重试。只读次数：'+reads.value),
  h('nav',{style:{display:'flex',gap:'8px',flexWrap:'wrap'}},retryStates.map(state=>h('button',{class:'button ghost',onClick:()=>{recovery.value=state}},state.label))),
  h('p',{class:'platform-alert '+productionIssuePresentation(recovery.value.blockedCode,recovery.value.reason,recovery.value.status).tone,role:'status'},productionIssuePresentation(recovery.value.blockedCode,recovery.value.reason,recovery.value.status).summary),
  productionRunCanRetry(recovery.value)?h('button',{class:'button ghost',onClick:()=>{mode.value='material';open.value=true;ready.value=false}},'重试当前阶段'):null,
  h('nav',{style:{display:'flex',gap:'8px',flexWrap:'wrap'}},Object.entries({mixed:'成功与失败混合',unknown:'远端状态未知',error:'读取失败',assembly:'装配确切失败与在途', 'assembly-drift':'装配输入已变化'}).map(([key,label])=>h('button',{class:'button ghost',onClick:()=>{mode.value=key;open.value=true;ready.value=false}},label))),
  h(BaseModal,{open:open.value,title:'确认重试当前阶段',width:'660px',onClose:()=>{open.value=false}}, {
    default:()=>open.value?h(ProductionRetryPreview,{key:mode.value,runId:'fixture-run',context:mode.value.startsWith('assembly')?assemblyContext:context,onReady:(value:boolean)=>{ready.value=value}}):null,
    footer:()=>[h('button',{class:'button ghost',onClick:()=>{open.value=false}},'返回'),h('button',{class:'button primary',disabled:!ready.value,onClick:()=>{throw Error('隔离示例不执行提交')}},'确认重试（仅检查按钮状态）')],
  }),
])}).use(router).mount('#app')
