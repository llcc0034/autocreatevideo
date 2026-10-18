import type {JsonObject,VisualRulesOutputOptions} from './platformTypes'
import {packResponseExample,responseValueAt} from './outputResponseLayout'

export interface OutputFieldSourceOption {field:string;label:string;optional?:boolean;choices:{response_field:string;value_type:'text'|'object'|'array';transformation:string;example:unknown}[]}
export interface OutputResponseKeyPolicy {pattern:string;max_length:number;reserved:string[];description:string}
export function adaptOutputResponseKeyPolicy(raw:unknown):OutputResponseKeyPolicy{
  const value=raw as OutputResponseKeyPolicy
  if(!value||typeof value.pattern!=='string'||!value.pattern||!Number.isSafeInteger(value.max_length)||value.max_length<1
    ||!Array.isArray(value.reserved)||!value.reserved.every(key=>typeof key==='string')||typeof value.description!=='string'||!value.description)throw new Error('响应字段名规则缺失，请重新读取')
  try{new RegExp(value.pattern)}catch{throw new Error('响应字段名规则无效')}
  return value
}
export function outputResponseKeyErrors(value:JsonObject,sources:JsonObject,policy:OutputResponseKeyPolicy|null):string[]{
  if(!policy)return ['尚未读取响应字段名规则']
  if(!Object.keys(value).length)return []
  const fields=Object.keys(sources),keys=Object.values(value)
  if(Object.keys(value).length!==fields.length||fields.some(field=>!Object.prototype.hasOwnProperty.call(value,field))
    ||keys.some(key=>typeof key!=='string'||key.length>policy.max_length||new RegExp(policy.pattern).exec(key)?.[0]!==key||key.split('.').some(part=>policy.reserved.includes(part)))
    ||new Set(keys).size!==fields.length||keys.some(left=>keys.some(right=>String(left).startsWith(String(right)+'.'))))return ['请完整填写合法响应位置，不得重复、互为父子、使用保留字或超出目录支持的层级；数组使用从0开始的整数下标，不接受脚本。']
  try{packResponseExample(null,keys.map(path=>({path:String(path),value:null})))}catch(error){return [error instanceof Error?error.message:'响应位置结构无效']}
  return []
}
export function adaptOutputFieldSourceOptions(raw:unknown):OutputFieldSourceOption[]{
  if(!Array.isArray(raw)||!raw.length)throw new Error('输出来源选项目录缺失，请重新读取')
  const keys=new Set<string>()
  for(const row of raw){
    if(!row||typeof row.field!=='string'||!row.field||keys.has(row.field)||typeof row.label!=='string'||!row.label
      ||(row.optional!==undefined&&typeof row.optional!=='boolean')
      ||!Array.isArray(row.choices)||!row.choices.length||new Set(row.choices.map((v:any)=>v?.response_field)).size!==row.choices.length
      ||row.choices.some((v:any)=>!v||typeof v.response_field!=='string'||!v.response_field||typeof v.transformation!=='string'||!v.transformation
        ||!['text','object','array'].includes(v.value_type)||!Object.prototype.hasOwnProperty.call(v,'example')
        ||(v.value_type==='text'?typeof v.example!=='string':v.value_type==='array'?!Array.isArray(v.example):!v.example||typeof v.example!=='object'||Array.isArray(v.example))))throw new Error('输出来源选项目录不完整')
    keys.add(row.field)
  }
  return raw
}
export function outputFieldSources(parser:JsonObject|undefined):JsonObject{
  if(!parser||!Array.isArray(parser.fields)||!parser.fields.length)throw new Error('输出版本缺少冻结字段映射')
  const value:JsonObject={}
  for(const row of parser.fields){if(!row||typeof row.field!=='string'||typeof row.response_field!=='string'||Object.prototype.hasOwnProperty.call(value,row.field))throw new Error('输出字段映射不完整');value[row.field]=row.response_field}
  return value
}
export function outputFieldSourceErrors(value:JsonObject,options:OutputFieldSourceOption[],allowed:JsonObject,quality?:JsonObject):string[]{
  if(!options.length)return ['尚未读取输出来源选项目录']
  const errors:string[]=[]
  if(Object.keys(value).some(key=>!options.some(row=>row.field===key))||options.some(row=>
    Object.prototype.hasOwnProperty.call(value,row.field)?!row.choices.some(choice=>choice.response_field===value[row.field]):!row.optional))errors.push('请选择完整的受控输出来源，不能填写任意字段或路径。')
  if(value.color_script==='color_script'&&(Object.keys(allowed).length||['base_colors_min_items','base_colors_max_items'].some(key=>Object.prototype.hasOwnProperty.call(quality||{},key))))errors.push('英文色彩说明没有结构化色彩字段：请明确清除色彩取值限定与基础色数量建议，或选回 color_design；不会自动删除配置。')
  return errors
}
export function draftOutputSourceOptions(base:VisualRulesOutputOptions,value:JsonObject,options:OutputFieldSourceOption[],responseKeys:JsonObject={}):VisualRulesOutputOptions{
  // Preserve frozen labels/example when the source was not changed by the author.
  const result:VisualRulesOutputOptions=JSON.parse(JSON.stringify(base))
  result.outputs=result.outputs.filter(row=>Object.prototype.hasOwnProperty.call(value,row.field))
  for(const option of options){
    if(!Object.prototype.hasOwnProperty.call(value,option.field)||result.outputs.some(row=>row.field===option.field))continue
    const choice=option.choices.find(item=>item.response_field===value[option.field])
    if(!choice)throw new Error('输出来源不在已读取目录中')
    result.outputs.push({field:option.field,label:option.label,sourceSelector:`reviewed_visual_rules.${option.field}`,responseField:choice.response_field,transformation:choice.transformation})
  }
  const example:{path:string;value:unknown}[]=[]
  for(const row of result.outputs){
    const key=String(responseKeys[row.field]??value[row.field])
    if(base.outputs.some(item=>item.field===row.field)&&value[row.field]===(row.sourceFormat??row.responseField)){
      example.push({path:key,value:responseValueAt(base.responseExample,row.responseField)})
    }else{
    const choice=options.find(option=>option.field===row.field)?.choices.find(choice=>choice.response_field===value[row.field])
    if(!choice)throw new Error('输出来源不在已读取目录中')
      example.push({path:key,value:JSON.parse(JSON.stringify(choice.example))});row.transformation=choice.transformation
    }
    if(key!==value[row.field])row.sourceFormat=String(value[row.field]);else delete row.sourceFormat
    row.responseField=key
  }
  result.responseExample=packResponseExample(base.responseExample.schema_version,example)
  return result
}
