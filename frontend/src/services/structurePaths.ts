import type { JsonObject } from './platformTypes'
import { jsonChildSchemas, jsonPropertyOptions, jsonRules } from './structuredInputForm'

export type StructurePath = (string | number)[]
export const pathObject = (value: unknown): JsonObject => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}
export function pathType(schema: JsonObject): unknown {
  return schema.type ?? (Array.isArray(schema.anyOf) ? schema.anyOf.map(pathObject).find(row=>row.type!=='null')?.type : undefined)
}
export function pathSchema(schema: JsonObject, tokens: unknown): {schema: JsonObject; error: string} {
  if (!Array.isArray(tokens)) return {schema:{},error:'请选择明确的字段路径'}
  let current=schema
  for(const [index,token] of tokens.entries()) {
    const type=pathType(current)
    if(type==='object'&&typeof token==='string'&&jsonPropertyOptions([current]).includes(token)) current=jsonChildSchemas([current],token,false)[0]||{}
    else if(type==='array'&&typeof token==='number'&&Number.isSafeInteger(token)&&token>=0&&jsonRules([current],'array').some(row=>row.items&&typeof row.items==='object')) current=jsonChildSchemas([current],String(token),true)[0]||{}
    else return {schema:{},error:`第 ${index+1} 级路径未在来源结构中声明；请从目录选择属性或明确列表下标`}
  }
  return {schema:current,error:''}
}

export function pathLabel(tokens: StructurePath): string {
  return '$'+tokens.map(token=>typeof token==='number'?`[${token}]`:`[${JSON.stringify(token)}]`).join('')
}
