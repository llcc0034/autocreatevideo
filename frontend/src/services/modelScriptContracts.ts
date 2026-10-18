import type { JsonObject, ScriptContractEditorOptions, ScriptContractPolicyOption } from './platformTypes'
import { chatObject } from './modelChatRequestOptions'

export const scriptObject = (value: unknown): JsonObject => chatObject(value) ? value : {}
const obj = scriptObject
const nonempty = (value: unknown): value is string => typeof value === 'string' && !!value.trim()
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const unique = (rows: JsonObject[]) => new Set(rows.map(row=>row.key)).size === rows.length
const exact = (row: JsonObject, keys: string[]) => Object.keys(row).length===keys.length&&keys.every(key=>Object.prototype.hasOwnProperty.call(row,key))

export function adaptScriptContracts(value: unknown): ScriptContractEditorOptions {
  const raw=obj(value)
  const fail=():never=>{throw new Error('剧本模型传输设置目录不完整，请重新读取系统设置')}
  if(!exact(raw,['schema_version','description','policies'])||raw.schema_version!==3||!nonempty(raw.description)||!Array.isArray(raw.policies)||raw.policies.length!==2) return fail()
  const policies=raw.policies.map(item=>{
    const row=obj(item)
    if(!exact(row,['key','operation','label','number_fields'])||!nonempty(row.key)||!nonempty(row.operation)||!nonempty(row.label)||!Array.isArray(row.number_fields)||!row.number_fields.length)return fail()
    if(!((row.key==='series_concept'&&row.operation==='script.generate')||(row.key==='script_structure'&&row.operation==='script.structure')))return fail()
    const numbers=row.number_fields.map(obj)
    if(!unique(numbers))return fail()
    return {key:row.key,operation:row.operation,label:row.label,
      numberFields:numbers.map(field=>{
        if(!exact(field,['key','label','unit','integer','minimum','maximum','exclusive_minimum'])||!nonempty(field.key)||!nonempty(field.label)||!nonempty(field.unit)||typeof field.integer!=='boolean'||!finite(field.minimum)||!(field.maximum===null||finite(field.maximum)&&field.maximum>=field.minimum)||typeof field.exclusive_minimum!=='boolean')return fail()
        return {key:field.key,label:field.label,unit:field.unit,integer:field.integer,minimum:field.minimum,maximum:field.maximum,exclusiveMinimum:field.exclusive_minimum}
      })}
  })
  if(!unique(policies)||new Set(policies.map(row=>row.operation)).size!==policies.length)return fail()
  return {schemaVersion:3,description:raw.description,policies}
}

export function scriptContractErrors(specification: unknown, options?: ScriptContractEditorOptions|null): string[] {
  const spec=obj(specification),caps=Array.isArray(spec.capabilities)?spec.capabilities:[]
  if(!options) return ['剧本模型传输设置目录未读取，不能保存']
  const errors:string[]=[]
  for(const descriptor of options.policies){
    if(spec[descriptor.key]===undefined&&!caps.includes(descriptor.operation))continue
    const policy=obj(spec[descriptor.key]),path=`specification.${descriptor.key}`
    const extras=scriptPolicyExtras(policy,descriptor)
    if(extras.length)errors.push(`${path} 包含已移除或未注册字段：${extras.join(', ')}；业务规则请在输出版本维护`)
    for(const field of descriptor.numberFields){
      const value=policy[field.key]
      if(!finite(value)||(field.integer&&!Number.isSafeInteger(value))||(field.exclusiveMinimum?value<=field.minimum:value<field.minimum)||(field.maximum!==null&&value>field.maximum))errors.push(`${descriptor.label} ${field.label}须填写目录范围内${field.integer?'整数':'数值'}（${field.unit}）`)
    }
    if(!chatObject(policy.chat_request_options))errors.push(`${path}.chat_request_options 须显式声明为对象`)
  }
  return errors
}

export function scriptPolicyExtras(policy: JsonObject, descriptor: ScriptContractPolicyOption): string[] {
  return Object.keys(policy).filter(key=>!['chat_request_options',...descriptor.numberFields.map(row=>row.key)].includes(key))
}

// Explicit initialization adds no business defaults and never silently drops old fields.
export function initializeScriptPolicy(value: unknown): JsonObject {
  const policy=obj(value)
  return {...policy,chat_request_options:policy.chat_request_options===undefined?{}:policy.chat_request_options}
}

export function assertScriptContractsUnchanged(before: unknown, after: unknown, options?: ScriptContractEditorOptions|null) {
  if(!options)throw new Error('剧本模型传输设置目录未读取，不能编辑完整 JSON')
  const stable=(value:unknown):string|undefined=>chatObject(value)?JSON.stringify(Object.keys(value).sort().map(key=>[key,stable(value[key])])):Array.isArray(value)?JSON.stringify(value.map(stable)):JSON.stringify(value)
  if(options.policies.some(row=>stable(obj(before)[row.key])!==stable(obj(after)[row.key])))throw new Error('剧本模型传输设置请在固定表单调整，完整 JSON 不接受重复编辑')
}
