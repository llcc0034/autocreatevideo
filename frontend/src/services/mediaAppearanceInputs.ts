export type AppearanceDifferenceOption = { kind: 'manual_channel' | 'logical_input'; key: string }
  | { kind: 'reference_field'; key: string; field_key: string }
  | { kind: 'structured_field'; key: string; field_key: string }
export interface MediaAppearanceInputOptions {
  primary_inputs: { input_key: string; target_key: string }[]
  difference_sources: AppearanceDifferenceOption[]
}
const invalid = (): never => { throw Error('外观输入目录不完整，请重新读取精确实例合同') }
const object = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : invalid()
const exact = (row: Record<string, unknown>, keys: string[]) => { if (Object.keys(row).length !== keys.length || keys.some(key => !(key in row))) invalid() }
const text = (value: unknown): string => typeof value === 'string' && !!value.trim() && value.length <= 191 ? value : invalid()
const rows = (value: unknown): unknown[] => Array.isArray(value) ? value : invalid()
function unique<T>(values: T[], key: (value: T) => string): T[] {
  if (new Set(values.map(key)).size !== values.length) invalid()
  return values
}
export function readMediaAppearanceInputOptions(value: unknown): MediaAppearanceInputOptions {
  const row = object(value); exact(row, ['primary_inputs', 'difference_sources'])
  const primary_inputs = rows(row.primary_inputs).map(value => {
    const item = object(value); exact(item, ['input_key', 'target_key'])
    return { input_key: text(item.input_key), target_key: text(item.target_key) }
  })
  const difference_sources = rows(row.difference_sources).map((value): AppearanceDifferenceOption => {
    const item = object(value), kind = item.kind
    if (kind === 'reference_field' || kind === 'structured_field') {
      exact(item, ['kind', 'key', 'field_key'])
      return { kind, key: text(item.key), field_key: text(item.field_key) }
    }
    if (kind !== 'manual_channel' && kind !== 'logical_input') return invalid()
    exact(item, ['kind', 'key']); return { kind, key: text(item.key) }
  })
  unique(primary_inputs, item => item.input_key); unique(primary_inputs, item => item.target_key)
  return { primary_inputs, difference_sources: unique(difference_sources, item => JSON.stringify(item)) }
}
export const appearanceDifferenceLabels: Record<AppearanceDifferenceOption['kind'], string> = {
  manual_channel: '原人工通道', logical_input: '策略正文输入', reference_field: '参考单元贡献字段', structured_field: '结构化直通文字字段',
}
