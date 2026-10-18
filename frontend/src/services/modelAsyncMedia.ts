import type { AsyncMediaEditorOptions, JsonObject } from './platformTypes'
import { chatObject } from './modelChatRequestOptions'

export const asyncObject = (value: unknown): JsonObject => chatObject(value) ? value : {}
const obj = asyncObject
const text = (value: unknown): value is string => typeof value === 'string' && !!value.trim()
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
export const asyncHas = (value: JsonObject, key: string) => Object.prototype.hasOwnProperty.call(value, key)
const has = asyncHas
export function adaptAsyncMediaOptions(value: unknown): AsyncMediaEditorOptions {
  const raw = obj(value), fail = (): never => { throw new Error('异步媒体合同目录不完整，请重新读取系统设置') }
  if (raw.schema_version !== 1 || !Number.isSafeInteger(raw.maximum_string_length) || Number(raw.maximum_string_length) < 1 || !text(raw.description) || !Array.isArray(raw.fields) || !raw.fields.length || !Array.isArray(raw.operations) || !raw.operations.length) return fail()
  const fields = raw.fields.map(item => {
    const row = obj(item)
    if (!text(row.key) || !text(row.label) || !text(row.source_description) || !Array.isArray(row.types) || !row.types.length || row.types.some(kind => !['string','integer','number','boolean'].includes(String(kind))) || new Set(row.types).size !== row.types.length) return fail()
    return { key:row.key, label:row.label, types:row.types as string[], sourceDescription:row.source_description }
  })
  const operations = raw.operations.map(item => {
    const row = obj(item)
    if (!text(row.key) || !['image','video','audio'].includes(String(row.output_kind))) return fail()
    return { key:row.key, outputKind:String(row.output_kind) }
  })
  if (new Set(fields.map(row=>row.key)).size !== fields.length || !fields.some(row=>row.key==='prompt') || new Set(operations.map(row=>row.key)).size !== operations.length) return fail()
  return { schemaVersion:1, maximumStringLength:Number(raw.maximum_string_length), description:raw.description, fields, operations }
}
export function asyncScalarValid(value: unknown, type: unknown): boolean {
  return type==='string' ? typeof value==='string' : type==='boolean' ? typeof value==='boolean' : type==='integer' ? Number.isSafeInteger(value) : type==='number' && finite(value)
}
function valueValid(value:unknown, rule:JsonObject, maximum:number):boolean {
  if (!asyncScalarValid(value,rule.type)) return false
  if (typeof value==='string' && ([...value].length < Number(rule.minLength??0) || [...value].length > Math.min(Number(rule.maxLength??maximum), maximum))) return false
  if (typeof value==='number' && (has(rule,'minimum')&&value<Number(rule.minimum) || has(rule,'maximum')&&value>Number(rule.maximum))) return false
  return !has(rule,'enum') || Array.isArray(rule.enum) && rule.enum.includes(value)
}
export function asyncMediaErrors(specification:unknown, options?:AsyncMediaEditorOptions|null, required=false):string[] {
  const spec=obj(specification)
  if (!required && spec.async_media===undefined) return []
  if (!options) return ['异步媒体合同目录未读取，不能保存']
  const errors:string[]=[], media=obj(spec.async_media), operations=obj(media.operations), caps=Array.isArray(spec.capabilities)?spec.capabilities:[]
  const selected=options.operations.filter(row=>caps.includes(row.key))
  if (media.schema_version!==options.schemaVersion || Object.keys(media).some(key=>!['schema_version','operations'].includes(key))) errors.push('async_media 协议结构无效；请修正或明确重建')
  if (!selected.length) errors.push('请先选择至少一个受支持的异步媒体能力')
  for(const key of Object.keys(operations)) if(!selected.some(row=>row.key===key)) errors.push(`${key} 未被选择或不受支持，请明确移除合同`)
  for(const operation of selected){
    const data=obj(operations[operation.key]),schema=obj(data.request_schema),properties=obj(schema.properties),requiredFields=schema.required,smoke=obj(data.smoke_inputs),prefix=operation.key
    if(data.output_kind!==operation.outputKind||Object.keys(data).some(key=>!['output_kind','request_schema','smoke_inputs'].includes(key))) errors.push(`${prefix} 产物类型或合同结构无效`)
    if(schema.type!=='object'||schema.additionalProperties!==false||Object.keys(schema).some(key=>!['type','additionalProperties','required','properties'].includes(key))||!Array.isArray(requiredFields)||requiredFields.some(key=>typeof key!=='string'||!has(properties,key))||new Set(Array.isArray(requiredFields)?requiredFields:[]).size!==(Array.isArray(requiredFields)?requiredFields.length:0)||!Array.isArray(requiredFields)||!requiredFields.includes('prompt'))errors.push(`${prefix} 须为封闭对象并声明必填 prompt`)
    if(!chatObject(data.smoke_inputs)||Object.keys(smoke).some(key=>!has(properties,key)))errors.push(`${prefix} Smoke 样例含未知字段或不是对象`)
    for(const [key,definition] of Object.entries(properties)){
      const rule=obj(definition),field=options.fields.find(row=>row.key===key),path=`${prefix}.${key}`
      if(!field||!field.types.includes(String(rule.type))||Object.keys(rule).some(name=>!['type','minimum','maximum','minLength','maxLength','enum'].includes(name))){errors.push(`${path} 包含未注册字段、类型或规则`);continue}
      for(const name of ['minimum','maximum'])if(has(rule,name)&&(!['number','integer'].includes(String(rule.type))||!asyncScalarValid(rule[name],rule.type)))errors.push(`${path} ${name} 须为对应类型的有限数值`)
      if(has(rule,'minimum')&&has(rule,'maximum')&&Number(rule.minimum)>Number(rule.maximum))errors.push(`${path} 最小值不能大于最大值`)
      for(const name of ['minLength','maxLength'])if(has(rule,name)&&(rule.type!=='string'||!Number.isSafeInteger(rule[name])||Number(rule[name])<(name==='maxLength'?1:0)||Number(rule[name])>options.maximumStringLength))errors.push(`${path} ${name} 须为安全范围内整数`)
      if(has(rule,'minLength')&&Number(rule.minLength)>Number(rule.maxLength??options.maximumStringLength))errors.push(`${path} 最小长度不能大于最大长度`)
      if(key==='prompt'&&!has(rule,'maxLength'))errors.push(`${path} 须明确字符上限`)
      if(has(rule,'enum')) {
        // Check enums without allowing the enum itself to mask a type/range error.
        const withoutEnum=Object.fromEntries(Object.entries(rule).filter(([name])=>name!=='enum'))
        if(!Array.isArray(rule.enum)||!rule.enum.length||new Set(rule.enum).size!==rule.enum.length||rule.enum.some(value=>!valueValid(value,withoutEnum,options.maximumStringLength)))errors.push(`${path} 枚举须非空、不重复且符合类型和范围`)
      }
      if(Array.isArray(requiredFields)&&requiredFields.includes(key)&&!has(smoke,key))errors.push(`${path} 缺少必填 Smoke 样例`)
      if(has(smoke,key)&&!valueValid(smoke[key],rule,options.maximumStringLength))errors.push(`${path} Smoke 样例类型、范围或枚举不符`)
    }
  }
  return errors
}
export function newAsyncOperation(outputKind:string):JsonObject {
  return {output_kind:outputKind,request_schema:{type:'object',additionalProperties:false,required:['prompt'],properties:{prompt:{type:'string'}}},smoke_inputs:{}}
}
export function assertAsyncMediaUnchanged(before:unknown,after:unknown){
  const stable=(value:unknown):string|undefined=>chatObject(value)?JSON.stringify(Object.keys(value).sort().map(key=>[key,stable(value[key])])):Array.isArray(value)?JSON.stringify(value.map(stable)):JSON.stringify(value)
  if(stable(obj(before).async_media)!==stable(obj(after).async_media))throw new Error('异步媒体合同请在固定表单调整，完整 JSON 不接受重复编辑')
}
