import type { JsonObject, TextExecutionEditorOptions } from './platformTypes'

export const textContractObject = (value:unknown): JsonObject => value !== null && typeof value==='object' && !Array.isArray(value) ? value as JsonObject : {}
const own = (value:JsonObject,key:string) => Object.prototype.hasOwnProperty.call(value,key)
export function textExecutionErrors(value:unknown, capabilities:unknown, options?:TextExecutionEditorOptions|null):string[] {
  const caps=Array.isArray(capabilities)?capabilities:[]
  if (!options?.integerConstraints) return value === undefined ? [] : ['文本执行协议目录未读取，不能核对或编辑合同']
  const declared=options.operations.filter(row=>caps.includes(row.value)).map(row=>row.value)
  if (!declared.length && value === undefined) return []
  const contract=textContractObject(value), operations=textContractObject(contract.operations), errors:string[]=[]
  if (contract.schema_version!==options.schemaVersion || Object.keys(contract).some(key=>!['schema_version','operations'].includes(key))) errors.push('文本合同结构无效；不会自动覆盖已有数据')
  for (const operation of declared) if (!own(operations,operation)) errors.push(`请补齐 ${options.operations.find(row=>row.value===operation)?.label} 的参数配置`)
  for (const [operation,raw] of Object.entries(operations)) {
    if (!declared.includes(operation)) errors.push(`${operation} 未在模型能力中勾选，请核对后删除配置或恢复能力`)
    const row=textContractObject(raw), parameters=textContractObject(row.parameters)
    if (!own(row,'parameters') || !own(row,'chat_request_options') || Object.keys(row).some(key=>!['parameters','chat_request_options'].includes(key))) errors.push(`${operation} 必须明确声明参数及额外请求选项`)
    if (row.parameters === null || typeof row.parameters!=='object' || Array.isArray(row.parameters)) errors.push(`${operation} 参数必须是分组`)
    if (row.chat_request_options === null || typeof row.chat_request_options!=='object' || Array.isArray(row.chat_request_options)) errors.push(`${operation} 请求选项必须是分组`)
    for (const parameter of options.parameters) {
      if (!own(parameters,parameter.value)) { if(parameter.required) errors.push(`${operation} 缺少${parameter.label}`); continue }
      const rule=textContractObject(parameters[parameter.value]), label=`${operation} · ${parameter.label}`
      if (rule.type!==parameter.template.type || typeof rule.required!=='boolean' || (parameter.required && rule.required!==true)) errors.push(`${label}类型或必填声明无效`)
      if (parameter.requireMaximum && !own(rule,'maximum')) errors.push(`${label}需要明确填写上限`)
      for(const key of ['minimum','maximum','default','const']) if(own(rule,key) && !Number.isSafeInteger(rule[key])) errors.push(`${label}的${key}必须为整数`)
      if (typeof rule.minimum==='number' && typeof rule.maximum==='number' && rule.minimum>rule.maximum) errors.push(`${label}最小值不能大于最大值`)
      if (typeof parameter.template.minimum==='number' && (typeof rule.minimum!=='number' || rule.minimum<parameter.template.minimum)) errors.push(`${label}最小值不能低于 ${parameter.template.minimum}`)
      for(const key of ['default','const']) if (typeof rule[key]==='number' && ((typeof rule.minimum==='number' && rule[key]<rule.minimum) || (typeof rule.maximum==='number' && rule[key]>rule.maximum))) errors.push(`${label}的${key}不在范围内`)
      if (rule.seed_policy==='generate_once' && (own(rule,'default') || own(rule,'const'))) errors.push(`${label}生成一次时不能同时配置默认或固定值`)
      if (own(rule,'seed_policy') && !options.integerConstraints.seedPolicies.some(row=>row.value===rule.seed_policy)) errors.push(`${label}种子策略不在服务端目录中`)
      const supported=['type','required','enum','alignments','seed_policy',...options.integerConstraints.valueFields.map(row=>row.value)]
      if(Object.keys(rule).some(key=>!supported.includes(key))) errors.push(`${label}含不适用于整数合同的字段`)
      const alignments=Array.isArray(rule.alignments)?rule.alignments:[]
      const validAlignments=alignments.every(raw=>{
        const item=textContractObject(raw)
        return Object.keys(item).length===2 && Number.isSafeInteger(item.modulus) && Number.isSafeInteger(item.remainder)
          && Number(item.modulus)>0 && Number(item.remainder)>=0 && Number(item.remainder)<Number(item.modulus)
      })
      if ((own(rule,'alignments')&&!Array.isArray(rule.alignments)) || !validAlignments) errors.push(`${label}对齐规则须填写整数步长与余数，满足 0 ≤ 余数 < 步长`)
      const check=(value:unknown,includeEnum:boolean,includeConst:boolean)=>{
        if(!Number.isSafeInteger(value)) return false
        const number=Number(value)
        return !(typeof rule.minimum==='number'&&number<rule.minimum) && !(typeof rule.maximum==='number'&&number>rule.maximum)
          && (!includeEnum||!own(rule,'enum')||(Array.isArray(rule.enum)&&rule.enum.includes(number)))
          && (!includeConst||!own(rule,'const')||rule.const===number)
          && (!validAlignments||alignments.every(raw=>{const item=textContractObject(raw);return (BigInt(number)-BigInt(Number(item.remainder)))%BigInt(Number(item.modulus))===0n}))
      }
      if (own(rule,'enum') && (!Array.isArray(rule.enum)||!rule.enum.length||new Set(rule.enum).size!==rule.enum.length||rule.enum.some(value=>!check(value,false,false)))) errors.push(`${label}允许值列表不能为空、重复或违反范围／对齐规则`)
      for(const key of ['default','const']) if(own(rule,key)&&!check(rule[key],true,true)) errors.push(`${label}的${key}不符合全部约束`)
      if(rule.seed_policy==='generate_once'&&!Array.isArray(rule.enum)&&!(Number.isSafeInteger(rule.minimum)&&Number.isSafeInteger(rule.maximum))) errors.push(`${label}生成一次需要明确有限范围或允许值列表`)
    }
    for(const key of Object.keys(parameters)) if(!options.parameters.some(row=>row.value===key)) errors.push(`${operation}含未登记参数 ${key}`)
  }
  return errors
}

export function initializeTextExecution(value:unknown, capabilities:unknown, options:TextExecutionEditorOptions):JsonObject {
  const contract=textContractObject(value), operations={...textContractObject(contract.operations)}
  if (value!==undefined && contract.schema_version!==options.schemaVersion) throw new Error('已有合同结构无法识别，未进行替换')
  for(const operation of options.operations.filter(row=>Array.isArray(capabilities)&&capabilities.includes(row.value))) {
    if (own(operations,operation.value)) continue
    operations[operation.value]={parameters:Object.fromEntries(options.parameters.filter(row=>row.required).map(row=>[row.value,{...row.template}])),chat_request_options:{}}
  }
  return {...contract,schema_version:options.schemaVersion,operations}
}

export function updateTextExecutionParameter(value:unknown, operation:string, parameter:string, key:string, raw:string):JsonObject {
  return setTextExecutionConstraint(value,operation,parameter,key,raw===''?undefined:Number(raw))
}

export function setTextExecutionConstraint(value:unknown, operation:string, parameter:string, key:string, next:unknown):JsonObject {
  const contract=textContractObject(value), operations=textContractObject(contract.operations), row=textContractObject(operations[operation]), parameters=textContractObject(row.parameters), rule={...textContractObject(parameters[parameter])}
  if(next===undefined) delete rule[key]
  else rule[key]=next
  return {...contract,operations:{...operations,[operation]:{...row,parameters:{...parameters,[parameter]:rule}}}}
}

export function assertTextParametersUnchanged(before:unknown,after:unknown) {
  const project=(value:unknown)=>{
    const contract=textContractObject(textContractObject(value).text_execution)
    return {schema_version:contract.schema_version,operations:Object.fromEntries(Object.entries(textContractObject(contract.operations)).map(([key,row])=>[key,textContractObject(row).parameters]))}
  }
  const stable=(value:unknown):string|undefined=>value!==null&&typeof value==='object'&&!Array.isArray(value)?JSON.stringify(Object.keys(value).sort().map(key=>[key,stable((value as JsonObject)[key])])):Array.isArray(value)?JSON.stringify(value.map(stable)):JSON.stringify(value)
  if(stable(project(before))!==stable(project(after))) throw new Error('文本参数合同请在“文本执行协议与参数范围”表单调整，完整 JSON 不接受重复编辑')
}
