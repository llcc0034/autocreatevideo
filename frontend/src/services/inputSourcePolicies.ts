import { api, unwrap } from './api'

export type MediaKind = 'image'|'video'|'audio'
export interface InputSourceOption {
  value:string; label:string; kinds:string[]; reader:string|null; available:boolean; requires:string[]; description:string
}
export interface InputSourceCatalog {
  schema_version:1; sources:InputSourceOption[]; orders:{value:string;label:string}[]; configuration_owner:string; description:string
}
export interface MediaSourcePolicy {
  schema_version:1; media_kind:MediaKind; allowed_sources:string[]; default_source:string; allow_override:boolean;
  min_items:number; max_items:number; allowed_purposes:string[]; order:'provided'
}
export interface MediaSourceItem { item_key:string; purpose:string|null; asset_id?:string; decision_id?:string; artifact_id?:string }
export interface MediaSourceSelection { source:string; items:MediaSourceItem[] }
export interface MediaSourcePreview {preview_only:true;valid:boolean;resolved:Record<string,unknown>|null;errors:Record<string,string[]>}

const validQuantity=(policy:MediaSourcePolicy)=>Number.isInteger(policy.min_items)&&Number.isInteger(policy.max_items)&&policy.min_items>=0&&policy.max_items>=policy.min_items

export function sourcePolicyErrors(policy:MediaSourcePolicy, catalog:InputSourceCatalog):string[] {
  const errors:string[]=[]
  if(policy.schema_version!==1)errors.push('来源策略版本无效')
  if(!['image','video','audio'].includes(policy.media_kind))errors.push('请选择媒体类型')
  if(!policy.allowed_sources.length||new Set(policy.allowed_sources).size!==policy.allowed_sources.length)errors.push('请选择不重复的允许来源')
  for(const key of policy.allowed_sources){
    const option=catalog.sources.find(row=>row.value===key)
    if(!option?.available||!option.kinds.includes(policy.media_kind))errors.push(`来源不可用：${option?.label||key}`)
  }
  if(!policy.allowed_sources.includes(policy.default_source))errors.push('默认来源必须属于允许来源')
  if(typeof policy.allow_override!=='boolean')errors.push('请明确本次是否允许改选')
  if(!validQuantity(policy))errors.push('数量上下限必须是有效非负整数')
  if(new Set(policy.allowed_purposes).size!==policy.allowed_purposes.length||policy.allowed_purposes.some(value=>!value.trim()))errors.push('用途不能为空或重复')
  if(!catalog.orders.some(row=>row.value===policy.order))errors.push('素材顺序方式不可用')
  return errors
}

export function selectedMediaSource(policy:MediaSourcePolicy, configured:MediaSourceSelection|null, override?:MediaSourceSelection|null):MediaSourceSelection {
  if(!validQuantity(policy))throw new Error('请先修正来源策略的数量上下限')
  if(override!==undefined&&!policy.allow_override)throw new Error('此输入不允许本次改选')
  const choice=override===undefined?configured:override
  if(!choice)throw new Error('尚未明确选择素材；不会自动取默认图')
  if(!policy.allowed_sources.includes(choice.source))throw new Error('所选来源不在允许范围')
  if(override===undefined&&choice.source!==policy.default_source)throw new Error('配置选择须使用默认来源；其他来源请明确改选')
  if(choice.items.length<policy.min_items||choice.items.length>policy.max_items)throw new Error('素材数量不符合配置，不会截断或补齐')
  const keys=new Set<string>()
  for(const item of choice.items){
    if(!item.item_key.trim()||keys.has(item.item_key))throw new Error('每次素材使用须有不同的标识')
    keys.add(item.item_key)
    if(policy.allowed_purposes.length?!policy.allowed_purposes.includes(item.purpose??''):item.purpose!==null)throw new Error('请明确选择允许的素材用途')
    const expected=['item_key','purpose',...(choice.source==='selected_output'?['decision_id','artifact_id']:['asset_id'])]
    if(Object.keys(item).length!==expected.length||expected.some(key=>!Object.prototype.hasOwnProperty.call(item,key)))throw new Error('素材来源引用不完整或混有其他来源')
    const ids=choice.source==='selected_output'?[item.decision_id,item.artifact_id]:[item.asset_id]
    if(ids.some(id=>typeof id!=='string'||!/^[a-f\d]{8}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{12}$/i.test(id)))throw new Error('须选择确切正式素材或产物，不接受临时路径')
  }
  return {source:choice.source,items:choice.items.map(row=>({...row}))}
}

export function moveMediaItem(selection:MediaSourceSelection,index:number,offset:number):MediaSourceSelection {
  const items=selection.items.map(row=>({...row})),target=index+offset
  if(Number.isInteger(index)&&Number.isInteger(offset)&&index>=0&&index<items.length&&target>=0&&target<items.length)
    [items[index],items[target]]=[items[target]!,items[index]!]
  return {...selection,items}
}

export const inputSourcePolicies={
  async options(){return unwrap<InputSourceCatalog>((await api.get('/data-structure-versions/input-source-options/')).data)},
  async preview(policy:MediaSourcePolicy,selection:MediaSourceSelection|null,override?:MediaSourceSelection|null){
    return unwrap<MediaSourcePreview>((await api.post('/data-structure-versions/media-source-preview/',{policy,selection,...(override!==undefined?{override}:{})})).data)
  },
}
