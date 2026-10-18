import type {JsonObject, VisualRulesOutputOptions} from './platformTypes'
import {validateResponseExample} from './outputResponseLayout'

const object=(value:unknown):value is JsonObject=>!!value&&typeof value==='object'&&!Array.isArray(value)
const integer=(value:unknown):value is number=>Number.isSafeInteger(value)&&Number(value)>=0
const clone=<T>(value:T):T=>JSON.parse(JSON.stringify(value)) as T

export function adaptVisualRulesOutputOptions(raw:unknown):VisualRulesOutputOptions {
  const invalid=()=>new Error('影视规则输出目录缺失或无效，请重新读取系统设置')
  if(!object(raw)||raw.schema_version!==1||raw.operation!=='visual_rules.generate'||!integer(raw.response_schema_version)||raw.response_schema_version<1
    ||!Array.isArray(raw.number_fields)||!raw.number_fields.length||!Array.isArray(raw.number_pairs)
    ||!integer(raw.clauses_max_bytes)||raw.clauses_max_bytes<1||!object(raw.color_choices)||!Object.keys(raw.color_choices).length
    ||!Array.isArray(raw.outputs)||!raw.outputs.length||!object(raw.response_example)
    ||raw.response_example.schema_version!==raw.response_schema_version)throw invalid()
  const fields=raw.number_fields.map(row=>{
    if(!object(row)||typeof row.key!=='string'||!/^[a-z][a-z0-9_]*$/.test(row.key)||typeof row.label!=='string'||!row.label.trim()
      ||typeof row.unit!=='string'||!row.unit.trim()||!integer(row.minimum)||!integer(row.maximum)||row.minimum>row.maximum)throw invalid()
    return {key:row.key,label:row.label,unit:row.unit,minimum:row.minimum,maximum:row.maximum}
  })
  const keys=new Set(fields.map(row=>row.key))
  if(keys.size!==fields.length)throw invalid()
  const pairs=raw.number_pairs.map(pair=>{
    if(!Array.isArray(pair)||pair.length!==2||pair[0]===pair[1]||!pair.every(key=>typeof key==='string'&&keys.has(key)))throw invalid()
    return pair as [string,string]
  })
  const outputs=raw.outputs.map(row=>{
    if(!object(row)||!['source_selector','field','label','response_field','transformation'].every(key=>typeof row[key]==='string'&&row[key].trim()))throw invalid()
    return {sourceSelector:String(row.source_selector),field:String(row.field),label:String(row.label),responseField:String(row.response_field),transformation:String(row.transformation)}
  })
  if(new Set(outputs.map(row=>row.sourceSelector)).size!==outputs.length
    ||new Set(outputs.map(row=>row.responseField)).size!==outputs.length
    ||new Set(outputs.map(row=>row.field)).size!==outputs.length)throw invalid()
  validateResponseExample(raw.response_example,outputs.map(row=>row.responseField))
  const choices:Record<string,(string|boolean)[]>={}
  for(const [key,values] of Object.entries(raw.color_choices)) {
    if(!Array.isArray(values)||!values.length||!values.every(v=>typeof v==='boolean'||typeof v==='string'&&v.length>0)||new Set(values).size!==values.length)throw invalid()
    choices[key]=[...values]
  }
  return {schemaVersion:1,operation:raw.operation,responseSchemaVersion:raw.response_schema_version,responseExample:clone(raw.response_example),numberFields:fields,numberPairs:pairs,
    clausesMaxBytes:raw.clauses_max_bytes,colorChoices:choices,outputs}
}

export function visualRulesAdvisoryErrors(value:unknown, options?:VisualRulesOutputOptions|null):string[] {
  if(!options)return ['影视规则输出目录未读取，不能保存此能力的配置']
  if(value===undefined)return []
  if(!object(value))return ['影视规则质量建议必须是对象；请明确清除此草稿的建议后重填']
  const errors:string[]=[]
  const allowed=new Set(['schema_version','recommended_negative_clauses',...options.numberFields.map(row=>row.key)])
  if(value.schema_version!==options.schemaVersion)errors.push('影视规则质量建议结构版本无效')
  for(const key of Object.keys(value))if(!allowed.has(key))errors.push(`质量建议包含未登记字段：${key}`)
  for(const row of options.numberFields)if(Object.prototype.hasOwnProperty.call(value,row.key)) {
    const number=value[row.key]
    if(!integer(number)||number<row.minimum||number>row.maximum)errors.push(`${row.label}须为${row.minimum}–${row.maximum}的整数${row.unit}`)
  }
  for(const [min,max] of options.numberPairs)if(typeof value[min]==='number'&&typeof value[max]==='number'&&value[min]>value[max])errors.push(`${options.numberFields.find(row=>row.key===min)?.label}不能大于${options.numberFields.find(row=>row.key===max)?.label}`)
  if(Object.prototype.hasOwnProperty.call(value,'recommended_negative_clauses')) {
    const clauses=value.recommended_negative_clauses
    if(!Array.isArray(clauses))errors.push('建议负面短语必须为列表')
    else {
      const normalized=clauses.map(raw=>typeof raw==='string'?raw.replace(/^[ .]+|[ .]+$/g,''):'')
      normalized.forEach((clause,index)=>{
        if(!clause||!/^[\x20-\x7e]+$/.test(clause)||/[,;\r\n]/.test(clause)||/<\/?[A-Za-z][^>]*>/.test(clause))errors.push(`建议短语${index+1}须为一条无HTML、控制字符或逗号分号的可打印英文`)
      })
      if(new Set(normalized.map(v=>v.toLowerCase())).size!==normalized.length)errors.push('建议负面短语不能重复（不区分英文大小写）')
      if(new TextEncoder().encode(JSON.stringify(normalized)).length>options.clausesMaxBytes)errors.push('建议短语列表超过服务端安全字节上限')
    }
  }
  return [...new Set(errors)]
}

export function updateVisualRulesAdvisory(value:unknown,options:VisualRulesOutputOptions,key:string,next:unknown):JsonObject|undefined {
  if(!options.numberFields.some(row=>row.key===key)&&key!=='recommended_negative_clauses')throw new Error('不能写入未登记的质量建议字段')
  if(value!==undefined&&!object(value))throw new Error('质量建议格式异常，请明确清除此草稿的建议后重填')
  if(key==='recommended_negative_clauses'&&object(value)&&Object.prototype.hasOwnProperty.call(value,key)&&!Array.isArray(value[key]))throw new Error('建议短语列表格式异常，请明确清除此草稿的建议后重填')
  const result=clone(value??{schema_version:options.schemaVersion}) as JsonObject
  if(next===undefined)delete result[key]
  else result[key]=clone(next)
  return Object.keys(result).length===1&&result.schema_version===options.schemaVersion?undefined:result
}

export function assertVisualRulesAdvisoriesUnchanged(before:unknown,after:unknown) {
  const value=(spec:unknown)=>object(spec)&&object(spec.visual_rules)?spec.visual_rules.quality_advisories:undefined
  const stable=(v:unknown):string=>object(v)?JSON.stringify(Object.keys(v).sort().map(key=>[key,stable(v[key])])):JSON.stringify(v)
  if(stable(value(before))!==stable(value(after)))throw new Error('影视规则质量建议请在固定表单调整，完整 JSON 不接受重复编辑此字段')
}
