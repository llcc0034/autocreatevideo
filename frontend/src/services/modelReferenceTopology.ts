import type { ConditioningContractEditorOptions, JsonObject, ReferenceTopologyEditorOptions } from './platformTypes'
import { contractObject as object, contractRows as rows } from './modelConditioningContract'

const clone = <T>(value:T):T => JSON.parse(JSON.stringify(value))
const integer = (value:unknown, min:number, max:number):value is number => Number.isSafeInteger(value) && Number(value)>=min && Number(value)<=max
const strings = (value:unknown):value is string[] => Array.isArray(value) && value.every(item=>typeof item==='string')

export function adaptReferenceTopologyOptions(value:unknown):ReferenceTopologyEditorOptions {
  const item=object(value)
  const fail=()=>{throw new Error('素材用途／预处理目录不完整，请重新读取系统设置')}
  if(item.schema_version!==1 || ['maximum_rules','maximum_duration_ms','maximum_fps','label_max_characters','text_max_characters','maximum_targets_per_source'].some(key=>!integer(item[key],1,Number.MAX_SAFE_INTEGER))) fail()
  for(const key of ['media_groups','preprocessing_slots']) {
    if(!Array.isArray(item[key]) || !item[key].length) fail()
    for(const row of rows(item[key])) if(['value','label','input_slot'].some(name=>typeof row[name]!=='string'||!row[name])) fail()
  }
  for(const group of rows(item.media_groups)) if(!['video','audio'].includes(String(group.media_kind))) fail()
  for(const slot of rows(item.preprocessing_slots)) {
    if(typeof slot.include_target_fps!=='boolean'||!Array.isArray(slot.adapters)||!slot.adapters.length) fail()
    for(const adapter of rows(slot.adapters)) if(['value','label','description'].some(key=>typeof adapter[key]!=='string'||!adapter[key]) || !['required','forbidden','not_applicable'].includes(String(adapter.target_fps))) fail()
  }
  const direction=object(item.pairing_direction)
  if(direction.source_media_kind!=='video'||direction.target_media_kind!=='audio') fail()
  return clone(item) as unknown as ReferenceTopologyEditorOptions
}

// Only protocol structure is initialized. Model capacity, purposes and adapters are not inferred.
export function newReferenceTopology(mode:JsonObject, options:ReferenceTopologyEditorOptions):JsonObject {
  return {schema_version:options.schema_version,
    ...Object.fromEntries(options.media_groups.map(group=>[group.value,{...clone(object(object(mode.inputs)[group.input_slot])),purposes:[],default_purpose_key:null}])),
    preprocessing:Object.fromEntries(options.preprocessing_slots.map(slot=>[slot.value,null])),pairing_rules:[],mutual_exclusion_rules:[]}
}
export function nextTopologyKey(prefix:string, items:JsonObject[]) {
  let n=1;const used=new Set(items.map(row=>row.key));while(used.has(`${prefix}_${n}`))n++
  return `${prefix}_${n}`
}
export function referenceTopologyErrors(mode:JsonObject, options:ConditioningContractEditorOptions):string[] {
  const value=mode.dynamic_reference_topology
  if(value===undefined||value===null) return [] // Required topology is checked by the parent.
  const catalog=options.dynamicReferenceTopology
  if(!catalog) return ['素材用途／预处理目录未读取，不能校验或保存这些规则']
  const topology=object(value), inputs=object(mode.inputs), errors:string[]=[], id=new RegExp(options.identifierPattern)
  const bounds=(value:unknown,label:string,max=options.maximumItems,min=0)=>{
    const row=object(value)
    if(!integer(row.minimum_items,min,max)||!integer(row.maximum_items,min,max)||Number(row.minimum_items)>Number(row.maximum_items)) errors.push(`${label}数量范围无效`)
  }
  const exact=(value:unknown,allowed:string[],label:string)=>{const row=object(value);if(Object.keys(row).some(key=>!allowed.includes(key))||allowed.some(key=>!(key in row))) errors.push(`${label}字段缺失或未受支持，请使用固定配置项`)}
  const identity=(items:JsonObject[],label:string)=>{
    const used=new Set<unknown>()
    for(const item of items){
      if(typeof item.key!=='string'||!id.test(item.key)||used.has(item.key)) errors.push(`${label}标识无效或重复`)
      used.add(item.key)
      if(typeof item.label!=='string'||!item.label.trim()||item.label.length>catalog.label_max_characters) errors.push(`${label}名称不能为空或超长`)
      if(typeof item.description!=='string'||item.description.length>catalog.text_max_characters) errors.push(`${label}说明缺失或超长`)
    }
  }
  exact(topology,['schema_version','videos','audios','preprocessing','pairing_rules','mutual_exclusion_rules'],'用途合同')
  if(topology.schema_version!==catalog.schema_version) errors.push('用途合同版本无效')
  const keys:Record<string,Set<unknown>>={}
  for(const group of catalog.media_groups){
    const data=object(topology[group.value]), source=object(inputs[group.input_slot]), purposes=rows(data.purposes)
    exact(data,['minimum_items','maximum_items','purposes','default_purpose_key'],group.label)
    bounds(data,group.label)
    if(data.minimum_items!==source.minimum_items||data.maximum_items!==source.maximum_items) errors.push(`${group.label}组数量必须来自上方素材槽`)
    const paired=Number(object(inputs.paired_video_audios).maximum_items)
    const max=group.media_kind==='audio'?Math.max(Number(source.maximum_items),paired):Number(source.maximum_items)
    const min=group.media_kind==='audio'?0:Number(source.minimum_items)
    if(!Array.isArray(data.purposes)||purposes.length>options.maximumItems||(max>0&&!purposes.length)||(max===0&&purposes.length)) errors.push(`${group.label}须按已启用数量明确声明用途`)
    identity(purposes,group.label)
    for(const purpose of purposes){
      exact(purpose,['key','label','description','minimum_items','maximum_items'],group.label)
      bounds(purpose,`${group.label} ${String(purpose.key)}`)
      if(!(group.media_kind==='audio'&&paired>0)&&Number(purpose.maximum_items)>max) errors.push(`${group.label}用途上限超过媒体组上限`)
    }
    if(purposes.reduce((n,p)=>n+Number(p.minimum_items),0)>max||purposes.reduce((n,p)=>n+Number(p.maximum_items),0)<min) errors.push(`${group.label}用途数量合计与组数量不一致`)
    keys[group.media_kind]=new Set(purposes.map(row=>row.key))
    if(purposes.length?!keys[group.media_kind]!.has(data.default_purpose_key):data.default_purpose_key!==null) errors.push(`${group.label}默认用途须从本组选择；无用途时应留空`)
  }
  const preprocessing=object(topology.preprocessing)
  exact(preprocessing,catalog.preprocessing_slots.map(slot=>slot.value),'预处理')
  for(const slot of catalog.preprocessing_slots){
    const data=preprocessing[slot.value]
    if(data===null){if(Number(object(inputs[slot.input_slot]).maximum_items)>0)errors.push(`${slot.label}已启用，必须配置预处理`);continue}
    const item=object(data)
    exact(item,['adapter_key','minimum_duration_ms','maximum_duration_ms',...(slot.include_target_fps?['target_fps','requires_source_fps']:[])],slot.label)
    const adapter=slot.adapters.find(row=>row.value===item.adapter_key)
    if(!adapter) errors.push(`${slot.label}预处理器未注册，保留原值但不可执行，请选择受支持项`)
    if(!integer(item.minimum_duration_ms,1,catalog.maximum_duration_ms)||!integer(item.maximum_duration_ms,1,catalog.maximum_duration_ms)||Number(item.minimum_duration_ms)>Number(item.maximum_duration_ms)) errors.push(`${slot.label}预处理时长范围无效（毫秒）`)
    if(slot.include_target_fps){
      if(typeof item.requires_source_fps!=='boolean') errors.push('视频须明确源帧率要求')
      if(item.target_fps!==null&&!integer(item.target_fps,1,catalog.maximum_fps))errors.push('目标帧率无效')
      if(adapter?.target_fps==='required'&&item.target_fps===null)errors.push('恒定帧率预处理必须填写目标帧率')
      if(adapter?.target_fps==='forbidden'&&item.target_fps!==null)errors.push('原样视频不允许指定目标帧率，请清空')
    }
  }
  const selection=(value:unknown,kind:string,label:string,minimum=1)=>{
    if(!strings(value)||value.length<minimum||new Set(value).size!==value.length||value.some(key=>!keys[kind]?.has(key)))errors.push(`${label}存在缺失、重复或未声明的用途，请重新选择`)
  }
  for(const field of ['pairing_rules','mutual_exclusion_rules'])if(!Array.isArray(topology[field])||rows(topology[field]).length>catalog.maximum_rules)errors.push(`${field}规则数量无效`)
  const pairing=rows(topology.pairing_rules), used=new Set<unknown>()
  identity(pairing,'配对规则')
  if(Number(object(inputs.paired_video_audios).maximum_items)>0&&!pairing.length)errors.push('视频配对音频已启用，必须声明配对规则')
  for(const rule of pairing){
    exact(rule,['key','label','description','source_media_kind','target_media_kind','source_purpose_keys','target_purpose_keys','default_target_purpose_key','minimum_targets_per_source','maximum_targets_per_source','allow_embedded_source','allow_external_target','source_and_external_mutually_exclusive'],'配对规则')
    if(rule.source_media_kind!==catalog.pairing_direction.source_media_kind||rule.target_media_kind!==catalog.pairing_direction.target_media_kind)errors.push('当前只支持视频→音频配对')
    selection(rule.source_purpose_keys,'video','配对来源');selection(rule.target_purpose_keys,'audio','配对目标')
    if(strings(rule.source_purpose_keys))for(const key of rule.source_purpose_keys){if(used.has(key))errors.push(`视频用途 ${key} 命中多条配对规则`);used.add(key)}
    if(!strings(rule.target_purpose_keys)||!rule.target_purpose_keys.includes(String(rule.default_target_purpose_key)))errors.push('默认配对用途必须在目标用途中')
    bounds({minimum_items:rule.minimum_targets_per_source,maximum_items:rule.maximum_targets_per_source},'每视频配对',catalog.maximum_targets_per_source)
    for(const key of ['allow_embedded_source','allow_external_target','source_and_external_mutually_exclusive'])if(typeof rule[key]!=='boolean')errors.push('配对开关须显式声明')
    if(rule.allow_embedded_source!==true&&rule.allow_external_target!==true)errors.push('配对规则至少允许一种音频来源')
    if(rule.allow_external_target===true&&Number(object(inputs.paired_video_audios).maximum_items)===0)errors.push('允许外部配对须启用视频配对音频槽')
  }
  const exclusions=rows(topology.mutual_exclusion_rules);identity(exclusions,'互斥规则')
  for(const rule of exclusions){
    exact(rule,['key','label','description','media_kind','purpose_keys','maximum_distinct_purposes'],'互斥规则')
    selection(rule.purpose_keys,String(rule.media_kind),'互斥用途',2)
    if(!integer(rule.maximum_distinct_purposes,1,Array.isArray(rule.purpose_keys)?rule.purpose_keys.length-1:0))errors.push('同时允许的用途种类数须为1到所选用途数减1')
  }
  return [...new Set(errors)]
}

export function assertConditioningUnchanged(before:unknown,after:unknown){
  const stable=(value:unknown):string=>Array.isArray(value)?JSON.stringify(value.map(stable)):value&&typeof value==='object'?JSON.stringify(Object.keys(value).sort().map(key=>[key,stable(object(value)[key])])):JSON.stringify(value)
  if(stable(object(before).conditioning_contract)!==stable(object(after).conditioning_contract))throw new Error('素材合同请在“模型素材数量合同”表单调整，完整 JSON 不接受重复编辑此字段')
}
