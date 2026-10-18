import {api, unwrap} from './api'
import type {BusinessOutputVersion} from './businessOutputs'
import type {StructureMappingOptions} from './dataStructures'
import type {JsonObject} from './platformTypes'

export interface ScriptSemanticOptions {key:'script_semantics.v1';json_schema:JsonObject;mapping:StructureMappingOptions}
export interface ScriptSemanticPreview {
  preview_only:true;validation_scope:'structure_and_mapping';output_version_id:string;output_revision:number
  output_contract_sha256:string;configuration_sha256:string;source_structure_version_id:string
  parsed:boolean;valid:boolean;raw_value:unknown;source_value:unknown;value:unknown
  source_fields:JsonObject[];defaults:JsonObject[];provenance:JsonObject[];warnings:JsonObject[];errors:Record<string,string[]>
}
const object=(value:unknown):value is JsonObject=>!!value&&typeof value==='object'&&!Array.isArray(value)
export function adaptScriptSemanticOptions(value:unknown):ScriptSemanticOptions {
  if(!object(value)||value.key!=='script_semantics.v1'||!object(value.json_schema)||!object(value.mapping))throw new Error('剧本语义目录不完整，请重新读取')
  const mapping=value.mapping
  if(mapping.schema_version!==1||!['nodes','scopes'].every(key=>Array.isArray(mapping[key])&&mapping[key].length&&mapping[key].every((row:unknown)=>object(row)&&typeof row.value==='string'&&typeof row.label==='string')))throw new Error('剧本映射目录不完整，请重新读取')
  return value as unknown as ScriptSemanticOptions
}
export function adaptScriptSemanticPreview(value:unknown,row:BusinessOutputVersion):ScriptSemanticPreview {
  if(!object(value)||value.preview_only!==true||value.validation_scope!=='structure_and_mapping'
    ||value.output_version_id!==row.id||value.output_revision!==row.revision||value.output_contract_sha256!==row.contract_sha256||value.source_structure_version_id!==row.data_structure_version
    ||typeof value.configuration_sha256!=='string'||!/^[a-f0-9]{64}$/.test(value.configuration_sha256)
    ||typeof value.parsed!=='boolean'||typeof value.valid!=='boolean'
    ||!['raw_value','source_value','value'].every(key=>Object.prototype.hasOwnProperty.call(value,key))
    ||!['source_fields','defaults','provenance','warnings'].every(key=>Array.isArray(value[key])&&value[key].every(object))
    ||!object(value.errors)||Object.values(value.errors).some(messages=>!Array.isArray(messages)||messages.some(message=>typeof message!=='string'))
    ||value.valid&&(!value.parsed||Object.keys(value.errors).length))throw new Error('剧本预览报告或版本不一致，请重新读取')
  return value as unknown as ScriptSemanticPreview
}
export const scriptSemantics={
  async options(){const result=unwrap<{script_semantics:unknown}>((await api.get('/business-output-versions/parsers/')).data);return adaptScriptSemanticOptions(result.script_semantics)},
  async preview(row:BusinessOutputVersion,response_text:string){return adaptScriptSemanticPreview(unwrap((await api.post(`/business-output-versions/${encodeURIComponent(row.id)}/preview-script-semantics/`,{expected_revision:row.revision,expected_contract_sha256:row.contract_sha256,response_text})).data),row)},
}
