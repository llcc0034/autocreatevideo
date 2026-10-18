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

  if (definition.key === 'execution.resources.review_policy') {
    const policy = value as Record<string,unknown>
    if(Object.keys(policy).sort().join(',') !== 'budget_by_currency,call_count,output_length'
      || !Number.isSafeInteger(policy.call_count) || Number(policy.call_count)<1 || !Number.isSafeInteger(policy.output_length) || Number(policy.output_length)<1)
      return '调用次数和输出额度必须是正整数，且仅允许预算、次数和输出额度三项配置。'
    if(!policy.budget_by_currency || typeof policy.budget_by_currency !== 'object' || Array.isArray(policy.budget_by_currency)
      || Object.entries(policy.budget_by_currency).some(([code,amount])=> !/^[A-Z][A-Z0-9]{2,11}$/.test(code) || typeof amount !== 'string' || !/^(?:0|[1-9][0-9]{0,7})(?:\.[0-9]{1,4})?$/.test(amount)))
      return '每个币种均需填写非负金额，最多 8 位整数和 4 位小数；不自动换算。'
  }

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
    if (schema.format === 'comfyui-saved-workflow-directory') {
      const parts = value.split('/')
      if (
        value !== value.trim()
        || value !== value.normalize('NFC')
        || value.length > 500
        || parts[0] !== 'workflows'
        || parts.some((part) => !part || part === '.' || part === '..' || part !== part.trim() || part.length > 255)
        || /[\\:%\u0000-\u001f\u007f]/u.test(value)
      ) return `${definition.key} 只能是 workflows 或其安全子目录`
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

export const configEnumLabel = (definition: ConfigDefinition, value: unknown): string => {
  const labels = definition.valueSchema?.enum_labels
  if (typeof value === 'string' && labels && typeof labels === 'object' && !Array.isArray(labels)) {
    const label = (labels as JsonObject)[value]
    if (typeof label === 'string' && label) return label
  }
  return typeof value === 'string' ? value : JSON.stringify(value)
}

export const configValueConstraintSummary = (definition: ConfigDefinition): string => {
  const schema = definition.valueSchema || {}
  const parts = [definition.valueType, definition.unit].filter(Boolean)
  if (hasOwn(schema, 'minimum') || hasOwn(schema, 'maximum')) parts.push(`范围 ${schema.minimum ?? '不限'}–${schema.maximum ?? '不限'}`)
  if (hasOwn(schema, 'maxItems')) parts.push(`最多 ${schema.maxItems} 项`)
  if (typeof schema.pattern === 'string' && schema.pattern) parts.push(`格式 ${schema.pattern}`)
  if (schema.format === 'comfyui-saved-workflow-directory') parts.push('ComfyUI userdata 相对目录')
  if (schema.resource_ref && typeof schema.resource_ref === 'object') {
    const reference = schema.resource_ref as JsonObject
    parts.push(`${reference.resource === 'execution_instance_version' ? '推荐精确实例版本' : String(reference.resource || '未声明资源')} · ${String(reference.capability || '未声明能力')}`)
  }
  return parts.join(' · ')
}
