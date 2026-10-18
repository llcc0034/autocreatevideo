import {api, unwrap} from './api'
import type {JsonObject} from './platformTypes'
export interface OutputPreviewVersion {id:string;revision:number;sha256:string}

export interface VisualRulesOutputPreview {
  previewOnly: true
  responseSchemaVersion: number
  valid: boolean
  candidate: Record<string,string>|null
  warnings: string[]
  errors: {code:string;message:string}[]
}
const object=(v:unknown):v is Record<string,unknown>=>!!v&&typeof v==='object'&&!Array.isArray(v)
export function adaptVisualRulesOutputPreview(raw:unknown):VisualRulesOutputPreview {
  const data=unwrap<unknown>(raw),invalid=()=>new Error('输出样例校验返回不完整，请重新校验')
  if(!object(data)||data.preview_only!==true||typeof data.valid!=='boolean'
    ||!Number.isSafeInteger(data.response_schema_version)||Number(data.response_schema_version)<1
    ||!Array.isArray(data.warnings)||!data.warnings.every(v=>typeof v==='string')
    ||!Array.isArray(data.errors)||!data.errors.every(v=>object(v)&&typeof v.code==='string'&&v.code&&typeof v.message==='string'&&v.message))throw invalid()
  if(data.valid?(!object(data.candidate)||!Object.keys(data.candidate).length||!Object.values(data.candidate).every(v=>typeof v==='string')||data.errors.length>0):
    (data.candidate!==null||data.errors.length===0||data.warnings.length>0))throw invalid()
  return {previewOnly:true,responseSchemaVersion:Number(data.response_schema_version),valid:data.valid,
    candidate:data.candidate as Record<string,string>|null,warnings:data.warnings,errors:data.errors as VisualRulesOutputPreview['errors']}
}
export async function previewVisualRulesOutput(responseText:string,qualityAdvisories?:JsonObject,version?:OutputPreviewVersion,allowedValues?:JsonObject,fieldSources?:JsonObject,responseKeys?:JsonObject):Promise<VisualRulesOutputPreview>{
  const result=await api.post('/business-output-versions/preview-visual-rules-output/',{
    response_text:responseText,...(version?{output_version_id:version.id,expected_revision:version.revision}:qualityAdvisories===undefined?{}:{quality_advisories:qualityAdvisories}),
    ...(!version&&allowedValues!==undefined?{allowed_values:allowedValues}:{}),
    ...(!version&&fieldSources!==undefined?{field_sources:fieldSources}:{}),
    ...(!version&&responseKeys!==undefined?{response_keys:responseKeys}:{}),
  })
  const raw=unwrap<Record<string,unknown>>(result.data)
  if(version&&(raw.output_version_id!==version.id||raw.output_revision!==version.revision||raw.output_contract_sha256!==version.sha256))throw new Error('预检结果与指定输出版本不一致，请重新读取后校验')
  return adaptVisualRulesOutputPreview(result.data)
}
