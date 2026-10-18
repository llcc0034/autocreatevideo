import {api,unwrap} from './api'
import type {JsonObject} from './platformTypes'

export interface ScriptStructureOption {id:string;output_key:string;version:number;name:string;description:string}
export interface ScriptRevisionStructure {document:JsonObject;document_sha256:string;json_schema:JsonObject;can_revise:boolean}
const record=(value:unknown):value is JsonObject=>!!value&&typeof value==='object'&&!Array.isArray(value)
const hash=(value:unknown)=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value)

export function adaptScriptRevisionStructure(raw:unknown):ScriptRevisionStructure|null {
  if(!raw||typeof raw!=='object'||!Object.prototype.hasOwnProperty.call(raw,'structure'))throw new Error('结构化详情响应不完整')
  const value=(raw as {structure:unknown}).structure
  if(value===null)return null
  if(!record(value)||!record(value.document)||!hash(value.document_sha256)||!record(value.json_schema)||typeof value.can_revise!=='boolean')throw new Error('结构化详情缺少确切修订或保存结果')
  const doc=value.document,revision=doc.revision,output=doc.output,projection=doc.projection
  if(doc.schema_version!==1||!record(revision)||typeof revision.id!=='string'||!revision.id||!hash(revision.content_sha256)
    ||!record(output)||typeof output.id!=='string'||!output.id||typeof output.name!=='string'||!output.name||!Number.isSafeInteger(output.version)||Number(output.version)<1
    ||!record(projection)||projection.validation_scope!=='structure_mapping_and_relations'
    ||!['source_value','value'].every(key=>Object.prototype.hasOwnProperty.call(projection,key))
    ||!['provenance','defaults','warnings'].every(key=>Array.isArray(projection[key])))throw new Error('结构化详情缺少确切修订或保存结果')
  return value as unknown as ScriptRevisionStructure
}

const base='/script-revisions/'
export const scriptRevisionStructures={
  async options(page=1,search='') {
    // The shared detail unwrap extracts results and would discard the page count.
    const payload=(await api.get(`${base}structure-options/`,{params:{page,page_size:20,search}})).data
    const raw=(payload?.data??payload) as {count:number;results:ScriptStructureOption[]}
    if(!raw||!Number.isSafeInteger(raw.count)||raw.count<0||!Array.isArray(raw.results)
      ||raw.results.some(row=>!row||typeof row.id!=='string'||!row.id||typeof row.output_key!=='string'||typeof row.name!=='string'
        ||typeof row.description!=='string'||!Number.isSafeInteger(row.version)||row.version<1))throw new Error('剧本输出版本目录不完整')
    return raw
  },
  async read(revisionId:string) {
    return adaptScriptRevisionStructure(unwrap((await api.get(`${base}${revisionId}/structure/`)).data))
  },
  async save(revisionId:string,outputVersionId:string) {
    const result=adaptScriptRevisionStructure(unwrap((await api.post(`${base}${revisionId}/structure/`,{output_version_id:outputVersionId})).data))
    if(result===null)throw new Error('未返回已保存的结构化结果，请重新读取本修订核对')
    return result
  },
}
