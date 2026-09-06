import type { JsonObject } from './platformTypes'

export const parameterRangeFields = [
  { key: 'min', label: '最小值' }, { key: 'max', label: '最大值' },
  { key: 'multiple_of', label: '步长（从 0 起算）' }, { key: 'default', label: '默认值' },
  { key: 'alignment_modulus', label: '整数对齐模数' }, { key: 'alignment_remainder', label: '整数对齐余数' },
] as const

export function parameterRangeErrors(value: unknown): string[] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ['参数范围必须是对象']
  const range = value as JsonObject
  const errors: string[] = []
  const allowed = new Set<string>(parameterRangeFields.map(field => field.key))
  if (Object.entries(range).some(([key, value]) => !allowed.has(key) || typeof value !== 'number' || !Number.isFinite(value) || Math.abs(value) > Number.MAX_SAFE_INTEGER)) errors.push('参数范围包含未知字段或非安全数值')
  if (range.min !== undefined && range.max !== undefined && Number(range.min) > Number(range.max)) errors.push('最小值不能大于最大值')
  if (range.multiple_of !== undefined && Number(range.multiple_of) <= 0) errors.push('步长必须大于 0')
  if (range.alignment_modulus !== undefined || range.alignment_remainder !== undefined) {
    if (!Number.isSafeInteger(range.alignment_modulus) || !Number.isSafeInteger(range.alignment_remainder) || Number(range.alignment_remainder) < 0 || Number(range.alignment_remainder) >= Number(range.alignment_modulus)) errors.push('整数对齐需要同时填写正整数模数及小于模数的非负余数')
  }
  if (range.default !== undefined && ((range.min !== undefined && Number(range.default) < Number(range.min)) || (range.max !== undefined && Number(range.default) > Number(range.max)))) errors.push('默认值必须在配置范围内')
  return errors
}

export function updateParameterRange(value: unknown, key: string, raw: string): JsonObject {
  const next = { ...(value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}) }
  if (!raw.trim()) delete next[key]
  else next[key] = Number(raw)
  return next
}
