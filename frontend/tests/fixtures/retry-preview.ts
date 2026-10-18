// Explicit simulated API response for component tests; not production evidence.
export const retryPreviewFixture = () => ({
  retry_context: {expected_stage_id:'stage',expected_attempt_ids:['failed']},mode:'primary_calls',can_submit:true,reason:'',
  message:'仅重试确认的失败主调用；未提交的准备请处理后继续原阶段。',call_count:2,preparation_count:1,count:3,next:null,previous:null,
  results:[
    {id:'failed',kind:'call',unit_key:'隔离镜头 2',preparation_id:'prep-failed',attempt_id:'failed',status:'failed',error_code:'fixture_failure',disposition:'retry',message:'明确失败，可沿原准备创建新调用；复用原提示词、素材、参数和种子。'},
    {id:'success',kind:'call',unit_key:'隔离镜头 1',preparation_id:'prep-success',attempt_id:'success',status:'succeeded',error_code:'',disposition:'skip',message:'已成功，跳过；已有产物及选择保持不变。'},
    {id:'pending',kind:'preparation',unit_key:'隔离镜头 3',preparation_id:'pending',attempt_id:null,status:'blocked',error_code:'fixture_missing_input',disposition:'attention',message:'尚未创建主调用；查看准备原因并处理后继续，不重做成功辅助字段。'},
  ],
})

export const assemblyRetryPreviewFixture = () => ({
  retry_context: {expected_stage_id:'assembly-stage',expected_attempt_ids:[],expected_assembly_job_ids:['failed-job'],expected_input_sha256:'a'.repeat(64)},
  mode:'assembly_jobs',can_submit:true,reason:'',message:'仅重试确认的失败装配，保留原选片；不重新生成媒体。',
  call_count:0,preparation_count:0,assembly_count:3,count:3,next:null,previous:null,
  results:[['failed-job','failed','retry'],['success-job','succeeded','skip'],['running-job','running','attention']].map(([id,status,disposition],i)=>({
    id,kind:'assembly',unit_key:`episode:${i+1}`,preparation_id:null,attempt_id:null,status,error_code:'',disposition,
    episode_number:i+1,consumer_input_sha256:'a'.repeat(64),error_message:i===0?'隔离示例：编码失败':'',
    message:i===0?'明确失败；仅使用原冻结来源重试。':i===1?'成功装配保留，不重跑。':'结果尚未确认，读取原任务，不重新提交。',
  })),
})
