import type { JsonObject } from './platformTypes'

// JSON presentation only. Business labels, types and limits come from the API.
export const jsonValueTypes = [
  { value: 'string', label: '文字', help: '一段文字，例如故事创意。' },
  { value: 'number', label: '数字', help: '一个数值，例如数量或时长；是否允许小数由输入要求决定。' },
  { value: 'boolean', label: '是 / 否', help: '明确选择启用或不启用。' },
  { value: 'object', label: '字段集合（对象）', help: '多个带名称的字段，例如“姓名”和“年龄”。不是制作步骤分组。' },
  { value: 'array', label: '多项列表（数组）', help: '按顺序填写多项内容，例如多个镜头。不是单选选项。' },
  { value: 'null', label: '空值', help: '明确传入 null，不等于不提供这项输入。仅在规则允许时使用。' },
]
export const jsonKind = (value: unknown): string => value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value
export const jsonObject = (v: unknown): JsonObject => v !== null && typeof v === 'object' && !Array.isArray(v) ? v as JsonObject : {}
const object = jsonObject
const has = (v: object, key: string) => Object.prototype.hasOwnProperty.call(v, key)
const same = (a: unknown, b: unknown): boolean => {
  if (Object.is(a, b)) return true
  if (jsonKind(a) !== jsonKind(b) || a === null || typeof a !== 'object') return false
  if (Array.isArray(a)) return a.length === (b as unknown[]).length && a.every((v, i) => same(v, (b as unknown[])[i]))
  const left = object(a), right = object(b)
  return Object.keys(left).length === Object.keys(right).length && Object.keys(left).every(key => has(right,key) && same(left[key],right[key]))
}
export function jsonAllowedTypes(schemas: JsonObject[]) {
  const allows = (schema: JsonObject, kind: string): boolean => (!schema.type || schema.type === kind || schema.type === 'integer' && kind === 'number')
    && (!Array.isArray(schema.anyOf) || schema.anyOf.some(branch => allows(object(branch), kind)))
  return jsonValueTypes.filter(row => schemas.every(schema => allows(schema, row.value)))
}
// The shared compiler uses anyOf for nullable nodes. Keep the outer annotations
// and resolve only the branch for the value being edited; never copy defaults.
export function jsonRules(schemas: JsonObject[], kind: string): JsonObject[] {
  return schemas.flatMap(schema => [schema, ...(Array.isArray(schema.anyOf)
    ? schema.anyOf.flatMap(branch => jsonAllowedTypes([object(branch)]).some(type => type.value === kind) ? jsonRules([object(branch)], kind) : []) : [])])
}
export const jsonValuePath = (path: string, key: string, array = false) => array ? `${path}[${key}]`
  : /^[A-Za-z_][A-Za-z0-9_]*$/.test(key) ? `${path}.${key}` : `${path}[${JSON.stringify(key)}]`
export const jsonDefinition = (schemas: JsonObject[]) => object(schemas.find(schema => schema['x-field-definition'])?.['x-field-definition'])
export function jsonChoices(schemas: JsonObject[]): unknown[] | null {
  const lists = schemas.flatMap(schema => has(schema, 'const') ? [[schema.const]] : Array.isArray(schema.enum) ? [schema.enum] : [])
  return lists.length ? lists[0]!.filter(value => lists.every(list => list.some(item => same(value,item))) && !jsonInputErrors(value,schemas).length) : null
}
export function emptyJsonInput(schemas: JsonObject[]): unknown {
  const types = jsonAllowedTypes(schemas)
  // No implicit zero, false, enum selection or business default.
  return types.length === 1 ? types[0]!.value === 'object' ? {} : types[0]!.value === 'array' ? [] : types[0]!.value === 'string' ? '' : null : null
}
export function jsonChildSchemas(schemas: JsonObject[], key: string, array: boolean): JsonObject[] {
  return jsonRules(schemas, array ? 'array' : 'object').flatMap(schema => {
    const properties = object(schema.properties)
    const value = array ? schema.items : has(properties,key) ? properties[key] : schema.additionalProperties
    return value && typeof value === 'object' && !Array.isArray(value) ? [value as JsonObject] : []
  })
}
export function jsonPropertyOptions(schemas: JsonObject[]) {
  return [...new Set(jsonRules(schemas,'object').flatMap(schema => Object.keys(object(schema.properties))))]
    .filter(key => jsonPropertyAllowed(schemas,key))
}
export function jsonPropertyAllowed(schemas: JsonObject[], key: string) {
  return jsonRules(schemas,'object').every(schema => schema.additionalProperties !== false || has(object(schema.properties),key))
}
export function jsonFieldLabel(schemas: JsonObject[], key: string) {
  return schemas.flatMap(schema => [schema, ...(Array.isArray(schema.anyOf) ? schema.anyOf.map(object) : [])]).find(schema => typeof schema.title === 'string')?.title as string || key
}
export function jsonInputErrors(value: unknown, schemas: JsonObject[], label = '此项', defaultSchemas: JsonObject[] = []): string[] {
  const errors: string[] = []
  for (const schema of schemas) {
    if (Array.isArray(schema.anyOf)) {
      const branches = schema.anyOf.map(branch => jsonInputErrors(value,[object(branch)],label,defaultSchemas))
      if (branches.every(branch => branch.length)) {
        const index = schema.anyOf.findIndex(branch => jsonAllowedTypes([object(branch)]).some(type => type.value === jsonKind(value)))
        errors.push(...(branches[index < 0 ? 0 : index] || [`${label}类型不符合结构要求`]))
      }
    }
    const type = schema.type, kind = jsonKind(value)
    if (type && (type === 'integer' ? !Number.isSafeInteger(value) : type !== kind)) {
      errors.push(`${label}需要${type === 'integer' ? '整数' : jsonValueTypes.find(row=>row.value===type)?.label || String(type)}`)
      continue
    }
    if (has(schema,'const') && !same(value,schema.const)) errors.push(`${label}需要使用规定的固定值`)
    if (Array.isArray(schema.enum) && !schema.enum.some(item=>same(item,value))) errors.push(`${label}不在允许的选项中`)
    if (typeof value === 'number') {
      if (!Number.isFinite(value)) errors.push(`${label}需要有效数字`)
      if (typeof schema.minimum === 'number' && value < schema.minimum) errors.push(`${label}不能小于 ${schema.minimum}`)
      if (typeof schema.maximum === 'number' && value > schema.maximum) errors.push(`${label}不能大于 ${schema.maximum}`)
    }
    if (typeof value === 'string') {
      if (typeof schema.pattern === 'string') {
        try { if (!new RegExp(schema.pattern).test(value)) errors.push(`${label}不符合字段格式要求`) }
        catch { errors.push(`${label}的格式约束无效，请检查配置`) }
      }
      if (schema.format === 'uuid' && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) errors.push(`${label}需要有效素材 ID`)
      if (typeof schema.minLength === 'number' && Array.from(value).length < schema.minLength) errors.push(`${label}至少需要 ${schema.minLength} 个字`)
      if (typeof schema.maxLength === 'number' && Array.from(value).length > schema.maxLength) errors.push(`${label}最多允许 ${schema.maxLength} 个字`)
    }
    if (Array.isArray(value)) {
      if (typeof schema.minItems === 'number' && value.length < schema.minItems) errors.push(`${label}至少需要 ${schema.minItems} 项`)
      if (typeof schema.maxItems === 'number' && value.length > schema.maxItems) errors.push(`${label}最多允许 ${schema.maxItems} 项`)
      value.forEach((item,index)=>errors.push(...jsonInputErrors(item,jsonChildSchemas([schema],String(index),true),`${label}第 ${index+1} 项`,jsonChildSchemas(defaultSchemas,String(index),true))))
    } else if (value !== null && typeof value === 'object') {
      const fields = object(value)
      // Only an explicitly selected input structure can defer missing keys to
      // the server resolver. Do not apply defaults or infer them from targets.
      const defaultKeys = jsonPropertyOptions(defaultSchemas).filter(key=>jsonChildSchemas(defaultSchemas,key,false).some(rule=>has(rule,'default')))
      if (typeof schema.minProperties === 'number' && new Set([...Object.keys(fields),...defaultKeys]).size < schema.minProperties) errors.push(`${label}至少需要 ${schema.minProperties} 个字段`)
      for (const key of Array.isArray(schema.required) ? schema.required : []) if (!has(fields,String(key))&&!defaultKeys.includes(String(key))) errors.push(`${label}需要添加 ${jsonFieldLabel(jsonChildSchemas([schema],String(key),false),String(key))}`)
      for (const [key,item] of Object.entries(fields)) {
        if (!jsonPropertyAllowed([schema],key)) errors.push(`${label}不允许添加 ${key}`)
        errors.push(...jsonInputErrors(item,jsonChildSchemas([schema],key,false),`${label} · ${key}`,jsonChildSchemas(defaultSchemas,key,false)))
      }
    }
  }
  return [...new Set(errors)]
}
