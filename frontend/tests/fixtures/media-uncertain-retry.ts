// Synthetic UI only. All business network calls are forbidden.
import {createApp,h,ref} from 'vue'
import {createMemoryHistory,createRouter} from 'vue-router'
import Panel from '../../src/components/ExecutionPreparationWorkbench.vue'
import {executionPreparationApi} from '../../src/services/executionPreparationApi'
import {adaptExecutionPreparationDetail} from '../../src/services/executionPreparation'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'
import '../../src/responsive.css'

api.defaults.adapter=async()=>{throw Error('隔离验证禁止业务请求')}
const writes=ref(0), failure=ref(false)
const data=adaptExecutionPreparationDetail({id:'fixture',stage_run:'fixture-stage',asset_job:null,deployment_smoke:null,workflow_smoke:null,
  execution_instance_version:'fixture-version',purpose:'production',unit_key:'fixture-unit',request_key:'fixture-request',revision:3,
  status:'dispatched',intent_sha256:'a'.repeat(64),request_sha256:'b'.repeat(64),request_frozen_at:'2026-10-14T06:00:00Z',next_check_at:null,
  reason_code:'',reason:'',evaluation_revision:null,evaluated_at:null,created_by:1,created_at:'2026-10-14T06:00:00Z',updated_at:'2026-10-14T06:00:00Z',
  presentation:null,outcome:{message:'隔离示例：远端提交结果未知。',artifact_count:0,selected_count:0},
  intent:{},resolved_inputs:{},review_contract:{schema_version:1,mode:'policy'},review_summary:{mode:'policy',label:'隔离自动规则',description:'没有真实审核或生成'},
  evaluation:null,prepared_request:null,field_slots:[],field_selections:[],auxiliary_batches:[],capacity_sample:null,text_response_review:null,resource_review:null,response_diagnostic:null,
  cancellation:{allowed:false,reason:'隔离验证',cancelled_at:null,cancelled_by:null},media_archive_recovery:null,
  media_uncertain_retry:{task_id:'original-task',attempt_id:'original-attempt',task_updated_at:'2026-10-14T06:00:00Z',request_sha256:'b'.repeat(64),can_retry:true,reason:''}})
executionPreparationApi.get=async()=>structuredClone(data)
executionPreparationApi.retryUncertainMedia=async()=>{
  if(failure.value)throw Error('隔离冲突：原任务观察已变化，请重新读取')
  writes.value++; data.revision++;data.mediaUncertainRetry=null
  data.outcome={message:'隔离样例已排队，不是真实模型结果。',artifactCount:0,selectedCount:0}
  return structuredClone(data)
}
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]})
const app=createApp({render:()=>h('main',{style:{maxWidth:'1080px',margin:'20px auto',padding:'12px'}},[
  h('h1','未决媒体重试 · 隔离验证'),h('p','模拟数据，业务写入0，不连接模型。'),
  h('label',[h('input',{type:'checkbox',checked:failure.value,onChange:(e:Event)=>{failure.value=(e.target as HTMLInputElement).checked}}),'模拟观察冲突']),
  h('p',`模拟提交次数 ${writes.value}`),h(Panel,{preparationId:'fixture',versionId:'fixture-version',purpose:'production'})
])}).use(router)
void router.push('/').then(()=>router.isReady()).then(()=>app.mount('#app'))
