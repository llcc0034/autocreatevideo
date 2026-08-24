import type { ConfigDefinition, JsonObject } from './platformTypes'

export interface ConfigValueValidation {
  value: unknown
  error: string
}

export interface ConfigResourceOption {
  value: string
  label: string
  disabled: boolean
  reason: string
}

const hasOwn = (value: JsonObject, key: string): boolean => Object.prototype.hasOwnProperty.call(value, key)
const finiteNumber = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null

export const validateConfigValue = (definition: ConfigDefinition, value: unknown): string => {
  const typeMatches = {
    boolean: typeof value === 'boolean',
    integer: typeof value === 'number' && Number.isInteger(value),
    number: typeof value === 'number' && Number.isFinite(value),
    string: typeof value === 'string',
    object: value !== null && typeof value === 'object' && !Array.isArray(value),
    array: Array.isArray(value),
  }[definition.valueType]
  if (!typeMatches) return `${definition.key} 的值类型必须为 ${definition.valueType}`

  const schema = definition.valueSchema || {}
  if (Array.isArray(schema.enum) && !schema.enum.some((candidate) => JSON.stringify(candidate) === JSON.stringify(value))) {
    return `${definition.key} 的值不在允许枚举中`
  }
  const numeric = finiteNumber(value)
  const minimum = finiteNumber(schema.minimum)
  const maximum = finiteNumber(schema.maximum)
  if (numeric !== null && minimum !== null && numeric < minimum) return `${definition.key} 不能小于 ${minimum}`
  if (numeric !== null && maximum !== null && numeric > maximum) return `${definition.key} 不能大于 ${maximum}`
  if (typeof value === 'string') {
    const minLength = Number(schema.minLength)
    const maxLength = Number(schema.maxLength)
    if (hasOwn(schema, 'minLength') && Number.isInteger(minLength) && value.length < minLength) return `${definition.key} 长度不能少于 ${minLength}`
    if (hasOwn(schema, 'maxLength') && Number.isInteger(maxLength) && value.length > maxLength) return `${definition.key} 长度不能超过 ${maxLength}`
    if (typeof schema.pattern === 'string' && schema.pattern) {
      try {
        if (!new RegExp(`^(?:${schema.pattern})$`).test(value)) return `${definition.key} 不符合格式约束`
      } catch {
        return `${definition.key} 的服务端 pattern 无法解析，已禁止保存`
      }
    }
  }
  if ((Array.isArray(value) || (value !== null && typeof value === 'object')) && hasOwn(schema, 'maxItems')) {
    const maxItems = Number(schema.maxItems)
    const size = Array.isArray(value) ? value.length : Object.keys(value as JsonObject).length
    if (Number.isInteger(maxItems) && size > maxItems) return `${definition.key} 最多允许 ${maxItems} 项`
  }
  return ''
}

export const parseConfigValue = (definition: ConfigDefinition, text: string): ConfigValueValidation => {
  let value: unknown
  try { value = JSON.parse(text) }
  catch { return { value: null, error: `${definition.key} 不是合法 JSON 值` } }
  return { value, error: validateConfigValue(definition, value) }
}

export const configValueEditorKind = (definition: ConfigDefinition): 'boolean' | 'enum' | 'number' | 'string' | 'resource' | 'json' => {
  if (definition.valueSchema?.resource_ref && typeof definition.valueSchema.resource_ref === 'object') return 'resource'
  if (Array.isArray(definition.valueSchema?.enum)) return 'enum'
  if (definition.valueType === 'boolean') return 'boolean'
  if (definition.valueType === 'integer' || definition.valueType === 'number') return 'number'
  if (definition.valueType === 'string') return 'string'
  return 'json'
}

export const configValueConstraintSummary = (definition: ConfigDefinition): string => {
  const schema = definition.valueSchema || {}
  const parts = [definition.valueType, definition.unit].filter(Boolean)
  if (hasOwn(schema, 'minimum') || hasOwn(schema, 'maximum')) parts.push(`范围 ${schema.minimum ?? '不限'}–${schema.maximum ?? '不限'}`)
  if (hasOwn(schema, 'maxItems')) parts.push(`最多 ${schema.maxItems} 项`)
  if (typeof schema.pattern === 'string' && schema.pattern) parts.push(`格式 ${schema.pattern}`)
  if (schema.resource_ref && typeof schema.resource_ref === 'object') {
    const reference = schema.resource_ref as JsonObject
    parts.push(`依赖 ${String(reference.resource || 'resource')} · ${String(reference.capability || '未声明能力')}`)
  }
  return parts.join(' · ')
}
