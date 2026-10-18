// Isolated UI fixture: no business reads/writes or model calls are allowed.
import {createApp,h,ref} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory} from 'vue-router'
import TaskCenterDetail from '../../src/components/TaskCenterDetail.vue'
import {taskCenterApi} from '../../src/services/taskCenterApi'
import {adaptTaskDetail} from '../../src/services/taskCenter'
import {executionPreparationApi} from '../../src/services/executionPreparationApi'
import {adaptExecutionPreparationDetail} from '../../src/services/executionPreparation'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'

api.defaults.adapter=async()=>{throw Error('隔离夹具禁止业务网络请求。')}
const mode=ref('failed'),attempt=ref<string>(),reads=ref(0)
const messages:Record<string,string>={
  failed:'主调用失败，请查看原调用错误与已保留的返回；辅助字段成功不能抵消主调用失败。',
  success:'主调用成功，已记录 1 项产物，其中 0 项标记为已选择。',
  stopped:'此准备已停止，不会继续准备或自动重新提交。关联调用尚未确认终态，请查看原任务；停止准备不等于远端已经结束。',
  missing:'主调用成功，但未读取到关联产物；请核对输出解析与落库记录。',
}
taskCenterApi.detail=async(_kind,id,query={})=>{
  reads.value++
  const historical=!!query.attempt_id,key=historical?'success':mode.value
  return adaptTaskDetail({kind:'preparation',id,category:key==='stopped'?'asset':'production',title:'隔离示例 · '+(key==='stopped'?'独立素材任务':'镜头主调用'),
    status:key==='failed'?'failed':key==='stopped'?'submitted':'succeeded',operation:'video.generate',
    project_id:null,project_title:null,production_run_id:null,execution_instance_version_id:'fixture-version',execution_instance_name:'隔离实例',execution_version:1,
    preparation_id:id,source_id:historical?'old-success':'latest-call',source_kind:key==='stopped'?'asset_job':'generation_attempt',
    created_at:'2026-09-29T00:00:00Z',updated_at:'2026-09-29T00:00:00Z',finished_at:null,estimated_cost:null,actual_cost:null,cost_currency:'',anomaly:false,
    outcome:{message:messages[key],artifact_count:historical?1:0,selected_count:0},actions:{cancel_asset:false,cancel_smoke:false},purpose:key==='stopped'?'asset_generation':'',
    preparation_status:key==='stopped'?'cancelled':'dispatched',request_frozen_at:'2026-09-29T00:00:00Z',request_sha256:'a'.repeat(64),
    submission:{state:'recorded',started_at:null,registered_at:null,identifier_state:'not_recorded',message:'隔离示例仅演示原记录，不是发送证明。',notes:[]},
    input:{state:'frozen',submitted:{},resolved:{},parameters:[],prompts:[],prompt_trace:null,field_batch_trace:null,media_trace:null,api_mapping:null,native_input_trace:null,system_message_trace:null,notes:[]},
    output:{state:historical?'available':'unavailable',result:null,text:'',media:[],notes:[historical?'隔离历史产物记录':'本次没有可展示的产物记录。']},
    validation:{status:'valid',warnings:[],error_code:key==='failed'?'fixture_failure':'',error_message:key==='failed'?'隔离示例：主调用失败':' '},
    calls:key==='stopped'?[]:[{kind:'generation_attempt',id:'old-success',role:'primary',status:'succeeded',operation:'video.generate',preparation_id:id,created_at:'2026-09-29T00:00:00Z',finished_at:null,error_code:'',retry_of_id:null}],
    call_count:key==='stopped'?0:1,calls_next:null,calls_previous:null,output_count:historical?1:0,output_next:null,output_previous:null,deferred_fields:[],links:{generation_task_id:null,asset_job_id:null}})
}
executionPreparationApi.get=async id=>adaptExecutionPreparationDetail({id,stage_run:null,asset_job:'fixture-job',deployment_smoke:null,workflow_smoke:null,
  execution_instance_version:'fixture-version',purpose:'asset_generation',unit_key:'asset',request_key:'fixture',revision:1,status:'cancelled',intent_sha256:'a'.repeat(64),request_sha256:'b'.repeat(64),request_frozen_at:'2026-09-29T00:00:00Z',next_check_at:null,
  reason_code:'fixture_stop',reason:'隔离示例：已停止此准备。',evaluation_revision:null,evaluated_at:null,created_by:1,created_at:'2026-09-29T00:00:00Z',updated_at:'2026-09-29T00:00:00Z',
  presentation:null,outcome:{message:messages.stopped,artifact_count:0,selected_count:0},intent:{},resolved_inputs:{},review_contract:{schema_version:1,mode:'policy'},review_summary:{mode:'policy',label:'隔离规则',description:'此处不执行实际审核。'},prepared_request:null,evaluation:null,field_selections:[],field_slots:[],auxiliary_batches:[],capacity_sample:null,response_diagnostic:null,cancellation:null,resource_review:null,text_response_review:null})
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]})
createApp({render:()=>h('main',{style:{maxWidth:'1100px',margin:'20px auto',padding:'12px'}},[
  h('h1','任务结果 · 隔离样例'),h('p','只验证组件，不连接业务 API / 模型。只读请求次数：'+reads.value),
  h('nav',{style:{display:'flex',gap:'8px',flexWrap:'wrap',marginBottom:'18px'}},Object.entries({failed:'主调用失败',stopped:'独立素材已停止',missing:'成功但无产物'}).map(([key,label])=>h('button',{class:'button ghost',onClick:()=>{mode.value=key;attempt.value=undefined}},label))),
  h(TaskCenterDetail,{key:mode.value,kind:'preparation',id:mode.value,attemptId:attempt.value,onSelect:(_kind,_id,chosen)=>{attempt.value=chosen}}),
])}).use(createPinia()).use(router).mount('#app')
