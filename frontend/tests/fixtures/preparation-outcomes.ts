// Isolated presentation fixture; every business network request is forbidden.
import {createApp,h} from 'vue'
import StageExecutionPreparations from '../../src/components/StageExecutionPreparations.vue'
import {adaptExecutionPreparationDetail} from '../../src/services/executionPreparation'
import {executionPreparationApi} from '../../src/services/executionPreparationApi'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'

api.defaults.adapter=async()=>{throw Error('隔离夹具禁止业务网络请求。')}
const cases=[
  ['failed','dispatched','failed','主调用失败，请查看原调用错误与已保留的返回；辅助字段成功不能抵消主调用失败。'],
  ['blocked','blocked',null,'主调用尚未创建；输入准备受阻，请查看下方原因。'],
  ['cancelled','cancelled','submitted','此准备已停止，不会继续准备或自动重新提交。关联调用尚未确认终态，请查看原任务；停止准备不等于远端已经结束。'],
  ['success','dispatched','succeeded','主调用成功，已记录 1 项产物，其中 0 项标记为已选择。'],
] as const
function detail(index:number,auxiliary=false){
  const [id,status,taskStatus,message]=cases[index]!
  return adaptExecutionPreparationDetail({id:auxiliary?`aux-${id}`:id,stage_run:'fixture-stage',asset_job:null,deployment_smoke:null,workflow_smoke:null,
    execution_instance_version:'fixture-version',purpose:'production',unit_key:`shot:${id}`,request_key:id,revision:1,
    status,intent_sha256:'a'.repeat(64),request_sha256:status==='blocked'?null:'b'.repeat(64),
    request_frozen_at:status==='blocked'?null:'2026-09-29T00:00:00Z',next_check_at:null,
    reason_code:status==='blocked'?'fixture_missing_reference':'',reason:status==='blocked'?'隔离示例：本次输入的参考素材不存在。':'',
    evaluation_revision:null,evaluated_at:null,created_by:1,created_at:'2026-09-29T00:00:00Z',updated_at:'2026-09-29T00:00:00Z',
    presentation:{kind:auxiliary?'auxiliary':'primary',label:`第1集 · 镜头${index+1}`,operation:auxiliary?'prompt.fields.generate':'video.generate',parent:null,
      task:taskStatus?{id:`task-${id}`,status:auxiliary?'succeeded':taskStatus,error_code:taskStatus==='failed'&&!auxiliary?'fixture_error':'',attempt_number:1}:null},
    outcome:{message:auxiliary?'辅助调用成功，仅代表输入字段生成或翻译完成，不代表主调用成功或已生成媒体。':message,artifact_count:id==='success'&&!auxiliary?1:0,selected_count:0},
    intent:{},resolved_inputs:{},review_contract:{schema_version:1,mode:'policy'},review_summary:{mode:'policy',label:'隔离规则',description:'不执行真实审核。'},
    evaluation:null,prepared_request:null,field_slots:[],field_selections:[],auxiliary_batches:auxiliary?[]:[{field_batch_key:'c'.repeat(64),purpose:'generate_fields',field_keys:['direction'],attempt_number:1,task_id:'fixture-aux',preparation_id:'fixture-child',status:'succeeded',reason:'隔离辅助成功结果，不代表主调用成功。',can_retry:false}],
    capacity_sample:null,text_response_review:null,resource_review:null,response_diagnostic:null,
    cancellation:{allowed:false,reason:'隔离只读验证',cancelled_at:status==='cancelled'?'2026-09-29T00:01:00Z':null,cancelled_by:null}})
}
executionPreparationApi.groups=async(_stage,_page,kind)=>({count:kind==='auxiliary'?1:cases.length,next:null,previous:null,
  results:(kind==='auxiliary'?[0]:cases.map((_,i)=>i)).map(index=>{const row=detail(index,kind==='auxiliary');return{unitKey:row.unitKey,label:row.presentation!.label,versionCount:1,latest:{...row,presentation:row.presentation!,outcome:row.outcome!}}})})
executionPreparationApi.get=async id=>detail(cases.findIndex(row=>row[0]===(id.startsWith('aux-')?id.slice(4):id)),id.startsWith('aux-'))
createApp({render:()=>h('main',{style:{maxWidth:'1150px',margin:'20px auto',padding:'12px'}},[
  h('h1','执行状态 · 隔离夹具'),h('p','仅验证真实组件的展示；所有数据均为模拟样例，不连接业务 API 或模型。'),
  h(StageExecutionPreparations,{stageId:'fixture-stage'}),
])}).mount('#app')
