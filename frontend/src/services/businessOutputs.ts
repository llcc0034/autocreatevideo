import {api,unwrap} from './api'
import type {JsonObject,VisualRulesOutputOptions} from './platformTypes'
import {adaptVisualRulesOutputOptions} from './visualRulesOutput'
import {adaptOutputAllowedValueFields} from './outputAllowedValues'
import {adaptOutputFieldSourceOptions,adaptOutputResponseKeyPolicy} from './outputFieldSources'

export interface BusinessOutputVersion {
  id:string; output_key:string; version:number; name:string; description:string; parser_key:string
  status:'draft'|'published'|'retired'; revision:number; contract_sha256:string
  data_structure_version:string|null
  quality_advisories?:JsonObject
  parser_contract?:JsonObject
}
export interface OutputParserOption {key:string;label:string;description:string;configuration:'structure'|'visual_rules'|'script_semantics'}
export function adaptOutputParsers(raw:unknown):OutputParserOption[]{
  const implemented:Record<string,string>={'structured_json.v1':'structure','visual_rules.v2':'visual_rules','script_semantics.v1':'script_semantics'}
  if(!Array.isArray(raw)||!raw.length||raw.some(row=>!row||!['key','label','description'].every(key=>typeof row[key]==='string'&&row[key].length)||!Object.prototype.hasOwnProperty.call(implemented,row.key)||implemented[row.key]!==row.configuration)||new Set(raw.map(row=>row.key)).size!==raw.length)throw new Error('输出协议目录不完整或含未实现协议')
  return raw
}
export interface StructuredOutputPreview {
  preview_only:true;output_version_id:string;output_revision:number;output_contract_sha256:string;structure_version_id:string
  parsed:boolean;valid:boolean;raw_value:unknown;value:unknown;json_schema:JsonObject
  fields:{path:unknown[];field_definition:{label:string;value_type:string}}[]
  defaults:JsonObject[];warnings:JsonObject[];errors:Record<string,string[]>
}
function validParser(value:BusinessOutputVersion):boolean {
  const parser=value.parser_contract
  if(!parser||parser.key!==value.parser_key)return false
  if(value.parser_key==='structured_json.v1')return parser.format==='json'&&Object.keys(parser).length===2
  if(value.parser_key==='script_semantics.v1')return parser.format==='json'&&Object.keys(parser).length===3&&!!parser.mapping&&typeof parser.mapping==='object'&&!Array.isArray(parser.mapping)
  return value.parser_key==='visual_rules.v2'&&Array.isArray(parser.fields)&&!!parser.response_example
}
export function adaptBusinessOutput(raw:unknown,detail=false):BusinessOutputVersion {
  if(!raw||typeof raw!=='object'||Array.isArray(raw))throw new Error('输出版本记录无效')
  const value=raw as BusinessOutputVersion
  if(!['id','output_key','name','parser_key','contract_sha256'].every(key=>typeof (value as unknown as JsonObject)[key]==='string'&&String((value as unknown as JsonObject)[key]).length)
    ||!Number.isSafeInteger(value.version)||value.version<1||!Number.isSafeInteger(value.revision)||value.revision<1
    ||!['draft','published','retired'].includes(value.status)||typeof value.description!=='string'
    ||!(value.data_structure_version===null||typeof value.data_structure_version==='string'&&value.data_structure_version.length>0)
    ||detail&&(!value.data_structure_version||!value.quality_advisories||typeof value.quality_advisories!=='object'||Array.isArray(value.quality_advisories)
      ||!validParser(value)))throw new Error('输出版本记录不完整，请重新读取')
  return value
}
export function outputVersionOptions(metadata:VisualRulesOutputOptions,row:BusinessOutputVersion):VisualRulesOutputOptions {
  const parser=row.parser_contract
  if(!parser||parser.key!==row.parser_key)throw new Error('输出版本缺少冻结解析合同，不能使用当前目录代替')
  if(!Array.isArray(parser.fields)||!parser.fields.every(field=>field&&typeof field==='object'&&typeof field.field==='string'&&typeof field.response_field==='string'))throw new Error('输出版本字段映射不完整')
  const fields=parser.fields as JsonObject[]
  const keys=parser.response_keys===undefined?{}:parser.response_keys
  if(!keys||typeof keys!=='object'||Array.isArray(keys)||Object.keys(keys).length>0&&
    (Object.keys(keys).length!==fields.length||fields.some(field=>typeof (keys as JsonObject)[String(field.field)]!=='string')))throw new Error('冻结响应字段名映射不完整')
  const result=adaptVisualRulesOutputOptions({schema_version:metadata.schemaVersion,operation:parser.operation,
    response_schema_version:parser.response_schema_version,response_example:parser.response_example,outputs:fields.map(field=>({...field,response_field:(keys as JsonObject)[String(field.field)]??field.response_field})),
    number_fields:metadata.numberFields,number_pairs:metadata.numberPairs,clauses_max_bytes:metadata.clausesMaxBytes,color_choices:metadata.colorChoices})
  for(const row of result.outputs){const format=fields.find(field=>field.field===row.field)?.response_field;if(format!==row.responseField)row.sourceFormat=String(format)}
  return result
}
const base='/business-output-versions/'
export const businessOutputs={
  async list(page=1,status='',search='',outputKey?:string) {
    const raw=(await api.get(base,{params:{page,page_size:20,status:status||undefined,search,output_key:outputKey}})).data
    const value=raw.data||raw
    if(!Number.isSafeInteger(value.count)||value.count<0||!Array.isArray(value.results)||!['next','previous'].every(key=>value[key]===null||typeof value[key]==='string'&&value[key].length>0))throw new Error('输出版本列表读取失败')
    return {count:value.count as number,results:value.results.map((row:unknown)=>adaptBusinessOutput(row)) as BusinessOutputVersion[],hasNext:value.next!==null,hasPrevious:value.previous!==null}
  },
  async detail(id:string){return adaptBusinessOutput(unwrap((await api.get(`${base}${encodeURIComponent(id)}/`)).data),true)},
  async options(){const raw=unwrap<{parsers:unknown;parser:JsonObject;editor:unknown;allowed_value_fields:unknown;field_source_options:unknown;response_key_policy:unknown}>((await api.get(base+'parsers/')).data);return {parsers:adaptOutputParsers(raw.parsers),parser:raw.parser,editor:adaptVisualRulesOutputOptions(raw.editor),allowedValueFields:adaptOutputAllowedValueFields(raw.allowed_value_fields),fieldSourceOptions:adaptOutputFieldSourceOptions(raw.field_source_options),responseKeyPolicy:adaptOutputResponseKeyPolicy(raw.response_key_policy)}},
  async previewJson(row:BusinessOutputVersion,response_text:string):Promise<StructuredOutputPreview>{
    const value=unwrap<StructuredOutputPreview>((await api.post(`${base}${encodeURIComponent(row.id)}/preview-json/`,{response_text,expected_revision:row.revision,expected_contract_sha256:row.contract_sha256})).data)
    if(value.output_version_id!==row.id||value.output_revision!==row.revision||value.output_contract_sha256!==row.contract_sha256||value.structure_version_id!==row.data_structure_version)throw new Error('预览返回的输出版本不一致，请重新读取')
    if(value.preview_only!==true||typeof value.valid!=='boolean'||typeof value.parsed!=='boolean'||!Object.prototype.hasOwnProperty.call(value,'raw_value')||!Object.prototype.hasOwnProperty.call(value,'value')||!value.json_schema||typeof value.json_schema!=='object'||Array.isArray(value.json_schema)||!Array.isArray(value.fields)||!Array.isArray(value.defaults)||!Array.isArray(value.warnings)||!value.errors||typeof value.errors!=='object'||Array.isArray(value.errors)||Object.values(value.errors).some(messages=>!Array.isArray(messages)||messages.some(message=>typeof message!=='string')))throw new Error('输出预览报告不完整')
    if(value.fields.some(field=>!field||!Array.isArray(field.path)||!field.field_definition||typeof field.field_definition.label!=='string'||typeof field.field_definition.value_type!=='string')||value.valid&&(!value.parsed||Object.keys(value.errors).length>0)
      ||value.defaults.some(item=>!item||typeof item.path!=='string'||typeof item.structure_version_id!=='string'||item.source!=='configured_default'||!Object.prototype.hasOwnProperty.call(item,'value'))
      ||value.warnings.some(item=>!item||typeof item.path!=='string'||typeof item.message!=='string'))throw new Error('输出预览字段目录或状态无效')
    return value
  },
  async save(data:{name:string;description:string;parser_key:string;data_structure_version:string;quality_advisories:JsonObject;allowed_values?:JsonObject;field_sources?:JsonObject;response_keys?:JsonObject;semantic_mapping?:JsonObject},row:BusinessOutputVersion|null){
    return adaptBusinessOutput(unwrap((row?await api.patch(`${base}${row.id}/`,{...data,expected_revision:row.revision}):await api.post(base,data)).data),true)
  },
  async action(row:BusinessOutputVersion,action:'publish'|'successor'|'retire'){
    return adaptBusinessOutput(unwrap((await api.post(`${base}${row.id}/${action}/`,{expected_revision:row.revision})).data),true)
  },
}
