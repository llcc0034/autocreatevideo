// Explicit UI fixture: no business API, model, selection or recovery write.
import {createApp,h,ref} from 'vue'
import ExecutionPreparationWorkbench from '../../src/components/ExecutionPreparationWorkbench.vue'
import {adaptExecutionPreparationDetail} from '../../src/services/executionPreparation'
import {executionPreparationApi} from '../../src/services/executionPreparationApi'
import {api} from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'

const mode=ref('success')
api.defaults.adapter=async()=>{throw Error('隔离夹具禁止业务网络请求。')}
executionPreparationApi.completeTextResponse=executionPreparationApi.continue=async()=>{
  throw Error('隔离夹具禁止提交、模型调用或人工修正写入。')
}
executionPreparationApi.get=async()=>{
  if(mode.value==='error')throw Error('隔离读取失败：原文文件不可读取；不会用候选内容替代。')
  const rejected=mode.value==='rejected', recovered=mode.value==='recovered'
  return adaptExecutionPreparationDetail({id:'fixture-prep',stage_run:'fixture-stage',asset_job:null,
    deployment_smoke:null,workflow_smoke:null,execution_instance_version:'fixture-version',purpose:'production',
    unit_key:'fixture-unit',request_key:'fixture-request',revision:3,status:'dispatched',intent_sha256:'a'.repeat(64),
    request_sha256:'b'.repeat(64),request_frozen_at:'2026-09-28T05:10:00Z',next_check_at:null,reason_code:'',reason:'',
    evaluation_revision:null,evaluated_at:null,evaluation:null,
    cancellation:{allowed:false,reason:'隔离夹具不允许操作实际任务。',cancelled_at:null,cancelled_by:null},
    outcome:{message:rejected?'主调用已返回，输出待处理。':'主调用成功，已记录候选，未自动采用。',artifact_count:rejected?0:1,selected_count:0},
    presentation:{kind:'primary',label:'隔离文本主调用',operation:'script.generate',parent:null,
      task:{id:'fixture-attempt',status:rejected?'awaiting_review':'succeeded',attempt_number:1,error_code:''}},
    created_by:1,created_at:'2026-09-28T05:10:00Z',updated_at:'2026-09-28T05:10:00Z',
    intent:{},resolved_inputs:{},review_contract:{schema_version:1,mode:'manual'},
    review_summary:{mode:'manual',label:'隔离配置',description:'这里不提交审核或采用。'},prepared_request:null,
    capacity_sample:null,auxiliary_batches:[],response_diagnostic:null,resource_review:null,field_slots:[],field_selections:[],
    text_response_review:{id:'fixture-review',attempt_id:'fixture-attempt',original_text:'{\n  "情绪.备注": "克制 <script>这是模型原文，不执行</script>",\n  "原文格式": "空白、换行和内容原样保留"\n}',
      original_sha256:'c'.repeat(64),editable:rejected,fields:[],notices:[],
      readonly_reason:rejected?'':'这是该次调用的原始返回，只读保留；候选的确认与采用请在对应业务步骤处理。',
      confirmed_at:recovered?'2026-09-28T05:12:00Z':null,
      asset_job_id:null, corrected_text:recovered?'明确标注的人工修订夹具':null,
      corrected_sha256:recovered?'d'.repeat(64):null, confirmed_by:recovered?'1':null,
      diagnostics:rejected||recovered?{contract_error_code:'response_contract_invalid',validation_code:'response_json_invalid',summary:'隔离样例：返回未通过格式校验，原文保留。'}:
        {contract_error_code:'response_candidate_recorded',summary:'已保存模型原始返回与解析后的候选；生成成功不代表已确认或采用。'}}})
}
createApp({setup(){return()=>h('main',{style:{maxWidth:'940px',margin:'16px auto',padding:'14px',background:'#101820',color:'#edf2f7'}},[
  h('h1','主调用原文 · 隔离验证'),h('p','实际工作台 + 明确模拟返回；不读取业务数据，不调用模型。'),
  h('nav',{style:{display:'flex',gap:'8px',flexWrap:'wrap'}},[
    ['success','模拟成功'],['rejected','模拟格式失败'],['recovered','模拟人工修正后'],['error','模拟读取失败'],
  ].map(([key,label])=>h('button',{class:'button ghost',onClick:()=>{mode.value=key!}},label))),
  h(ExecutionPreparationWorkbench,{key:mode.value,preparationId:'fixture-prep',versionId:'fixture-version',purpose:'production'}),
])}}).mount('#app')
