import type { JsonObject } from './platformTypes'
export interface ReferenceMappingPreview {
  schema_version: 1; status: 'inputs_validated'; run_plan_sha256: string; definition_sha256: string
  stage_run_id: string; shot_id: string; review: JsonObject | null; excluded_reference_keys: string[]; notice: string
  candidate_number: number; route_index: number; execution_instance_version_id: string
  input_amendment: {id:string;sha256:string}|null; effective_input_sources: JsonObject
  value_inputs: {key:string;kind:'text'|'json';value:unknown;provenance:JsonObject}[]
  inputs: { key: string; kind: string; minimum: number; maximum: number; items: {
    item_key: string; name: string; purpose: string | null; order: number; paired_to: string | null
    subject_ref: {kind:string;id:string} | null; asset_id?: string; artifact_id?: string
  }[] }[]
}
export function referenceMappingPreview(value: unknown, expected: {plan:string;stage:string;shot:string;candidate:number}): ReferenceMappingPreview {
  const v=value as ReferenceMappingPreview, object=(r:unknown)=>r!==null&&typeof r==='object'&&!Array.isArray(r)
  const digest=(s:unknown)=>typeof s==='string'&&/^[a-f0-9]{64}$/.test(s)
  if(!object(v)||v.schema_version!==1||v.status!=='inputs_validated'||v.run_plan_sha256!==expected.plan
    ||v.stage_run_id!==expected.stage||v.shot_id!==expected.shot||!digest(v.definition_sha256)
    ||v.candidate_number!==expected.candidate||!Number.isSafeInteger(v.route_index)||v.route_index<0
    ||typeof v.execution_instance_version_id!=='string'||!v.execution_instance_version_id
    ||!object(v.effective_input_sources)||!(v.input_amendment===null||object(v.input_amendment)&&typeof v.input_amendment.id==='string'&&digest(v.input_amendment.sha256))
    ||!Array.isArray(v.inputs)||!Array.isArray(v.value_inputs)||!Array.isArray(v.excluded_reference_keys)||v.excluded_reference_keys.some(s=>typeof s!=='string')
    ||typeof v.notice!=='string'||!(v.review===null||object(v.review)))throw new Error('映射预览缺少完整事实或不属于当前选择，请重新读取')
  const keys=new Set<string>(),uses=new Set<string>()
  for(const input of v.inputs){
    if(!object(input)||typeof input.key!=='string'||!input.key||keys.has(input.key)||!['image','video','audio'].includes(input.kind)
      ||!Number.isSafeInteger(input.minimum)||!Number.isSafeInteger(input.maximum)||input.minimum<0||input.maximum<input.minimum
      ||!Array.isArray(input.items)||input.items.length<input.minimum||input.items.length>input.maximum)throw new Error('映射输入槽位或数量合同无效')
    keys.add(input.key)
    input.items.forEach((item,index)=>{
      if(!object(item)||typeof item.item_key!=='string'||!item.item_key||uses.has(item.item_key)||item.order!==index
        ||typeof item.name!=='string'||!(item.purpose===null||typeof item.purpose==='string')
        ||!(item.paired_to===null||typeof item.paired_to==='string')
        ||!(item.subject_ref===null||object(item.subject_ref)&&typeof item.subject_ref.kind==='string'&&typeof item.subject_ref.id==='string'))throw new Error('映射素材的身份、顺序或语义缺失')
      uses.add(item.item_key)
    })
  }
  for(const input of v.inputs)for(const item of input.items)if(item.paired_to!==null&&!uses.has(item.paired_to))throw new Error('映射预览缺少配套视频')
  for(const input of v.value_inputs){
    if(!object(input)||typeof input.key!=='string'||!input.key||keys.has(input.key)||!['text','json'].includes(input.kind)
      ||!Object.prototype.hasOwnProperty.call(input,'value')||input.kind==='text'&&typeof input.value!=='string'
      ||!object(input.provenance)||input.provenance.input_key!==input.key||typeof input.provenance.kind!=='string'
      ||!digest(input.provenance.value_sha256))throw new Error('映射内容或来源证据不完整')
    keys.add(input.key)
  }
  return v
}
