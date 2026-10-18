// Explicit in-memory fixture. No business records, calls or approval facts.
export const assetTextCorrectionData = () => ({
  id:'fixture-prep', stage_run:null, asset_job:'fixture-job', deployment_smoke:null, workflow_smoke:null,
  execution_instance_version:'fixture-version', purpose:'production', unit_key:'fixture', request_key:'fixture',
  revision:3, status:'dispatched', intent_sha256:'a'.repeat(64), request_sha256:'b'.repeat(64),
  request_frozen_at:'2026-10-02T01:00:00Z', next_check_at:null, reason_code:'', reason:'',
  evaluation_revision:null, evaluated_at:null, evaluation:null, cancellation:null, presentation:null,
  outcome:{message:'隔离样例：模型已返回但正文校验失败，未产生可用结果。',artifact_count:0,selected_count:0},
  created_by:1,created_at:'2026-10-02T01:00:00Z',updated_at:'2026-10-02T01:00:00Z',
  intent:{},resolved_inputs:{},review_contract:{schema_version:1,mode:'manual'},
  review_summary:{mode:'manual',label:'隔离样例',description:'此页仅模拟修订，不操作业务数据。'},
  prepared_request:null,capacity_sample:null,auxiliary_batches:[],response_diagnostic:null,resource_review:null,
  field_slots:[],field_selections:[],text_response_review:{
    id:'fixture-review',attempt_id:null,asset_job_id:'fixture-job', original_text:'  原始正文\r\n<script>不执行</script>\u0000',
    original_sha256:'c'.repeat(64),editable:true,fields:[],notices:[],readonly_reason:'',
    confirmed_at:null as string|null,confirmed_by:null as string|null,corrected_text:null as string|null,corrected_sha256:null as string|null,
    diagnostics:{contract_error_code:'response_contract_invalid',validation_code:'text_result_invalid',summary:'隔离返回未通过正文校验；原文保留。'},
  },
})
