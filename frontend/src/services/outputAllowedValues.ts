import type {JsonObject} from './platformTypes'

export interface OutputAllowedValueField {key:string;label:string;choices:(string|boolean)[]}
export function adaptOutputAllowedValueFields(raw:unknown):OutputAllowedValueField[]{
  if(!Array.isArray(raw)||!raw.length)throw new Error('输出取值选项目录缺失，请重新读取')
  const keys=new Set<string>()
  for(const row of raw){
    if(!row||typeof row.key!=='string'||!row.key||keys.has(row.key)||typeof row.label!=='string'||!row.label
      ||!Array.isArray(row.choices)||!row.choices.length||row.choices.some((v:unknown)=>typeof v!=='string'&&typeof v!=='boolean')
      ||new Set(row.choices).size!==row.choices.length)throw new Error('输出取值选项目录不完整')
    keys.add(row.key)
  }
  return raw
}
export function outputAllowedValueErrors(value:JsonObject,fields:OutputAllowedValueField[]):string[]{
  if(!fields.length)return ['尚未读取输出取值选项目录']
  const errors:string[]=[]
  for(const [key,choices] of Object.entries(value)){
    const field=fields.find(row=>row.key===key)
    if(!field||!Array.isArray(choices)||!choices.length||new Set(choices).size!==choices.length||choices.some(v=>!field.choices.includes(v as string|boolean)))errors.push(`${field?.label||key}：请选择至少一项合法且不重复的取值；也可取消限定。`)
  }
  return errors
}
