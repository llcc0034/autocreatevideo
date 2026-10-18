import type { JsonObject } from './platformTypes'

export interface VisualRuleReviewField {
  key: string; label: string; value_type: 'text'; allow_blank: boolean; source_selector: string
  response_path: string; response_format: string; transformation: string
}
export interface VisualRuleReviewContract {
  schema_version: 1
  output: { id: string; output_key: string; version: number; name: string; sha256: string }
  fields: VisualRuleReviewField[]
}
export function readVisualRuleReviewContract(value: unknown): VisualRuleReviewContract {
  const object = (value: unknown): JsonObject => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('冻结审核合同缺失，不会使用当前字段目录')
    return value as JsonObject
  }
  const text = (value: unknown) => typeof value === 'string' && value.trim().length > 0
  const raw = object(value), output = object(raw.output)
  if (raw.schema_version !== 1 || !Array.isArray(raw.fields) || !raw.fields.length
    || !['id', 'output_key', 'name'].every(key => text(output[key]))
    || !Number.isSafeInteger(output.version) || Number(output.version) < 1
    || typeof output.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(output.sha256)) throw new Error('冻结审核合同身份无效')
  const keys = new Set<string>(), selectors = new Set<string>()
  for (const item of raw.fields) {
    const row = object(item)
    if (!['key', 'label', 'source_selector', 'response_path', 'response_format', 'transformation'].every(key => text(row[key]))
      || typeof row.key !== 'string' || !/^[a-z][a-z0-9_]*$/.test(row.key)
      || ['constructor', 'prototype'].includes(row.key) || row.value_type !== 'text' || typeof row.allow_blank !== 'boolean'
      || row.source_selector !== `reviewed_visual_rules.${row.key}` || keys.has(row.key) || selectors.has(String(row.source_selector))) {
      throw new Error('冻结审核字段重复、缺失或类型不受支持')
    }
    keys.add(row.key); selectors.add(String(row.source_selector))
  }
  return raw as unknown as VisualRuleReviewContract
}
