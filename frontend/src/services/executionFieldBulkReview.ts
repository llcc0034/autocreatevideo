export interface BulkField {
  unit_key: string; field_key: string; label: string; state: 'candidate' | 'selected' | 'blocked'
  reason: string; value_id: string | null; revision: number | null; text: string; warnings: string[]
}
export interface BulkFieldReceipt { preparation_id: string; expected_preparation_revision: number; preview_sha256: string }
export interface BulkFieldPreview extends BulkFieldReceipt { label: string; fields: BulkField[]; ready: boolean; reason: string }
const object = (v: unknown): Record<string, any> => {
  if (!v || typeof v !== 'object' || Array.isArray(v)) throw Error('批量审核响应不完整')
  return v as Record<string, any>
}
const positive = (v: unknown) => Number.isSafeInteger(v) && Number(v) > 0
export function adaptBulkFieldPreview(raw: unknown, ids: string[]): BulkFieldPreview[] {
  const items = object(raw).items
  if (!Array.isArray(items) || items.length !== ids.length || new Set(ids).size !== ids.length) throw Error('批量审核范围不一致')
  const seen = new Set<string>()
  return items.map(value => {
    const row = object(value)
    if (!ids.includes(row.preparation_id) || seen.has(row.preparation_id) || !positive(row.expected_preparation_revision)
      || typeof row.preview_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(row.preview_sha256)
      || typeof row.label !== 'string' || typeof row.ready !== 'boolean' || typeof row.reason !== 'string' || !Array.isArray(row.fields)) throw Error('批量审核身份或修订无效')
    seen.add(row.preparation_id)
    const keys = new Set<string>()
    for (const value of row.fields) {
      const field = object(value), key = JSON.stringify([field.unit_key, field.field_key])
      if (keys.has(key) || !['unit_key','field_key','label','reason','text'].every(k => typeof field[k] === 'string')
        || !['candidate','selected','blocked'].includes(field.state) || !(field.value_id === null || typeof field.value_id === 'string')
        || !(field.revision === null || positive(field.revision)) || !Array.isArray(field.warnings)
        || field.warnings.some((w: unknown) => typeof w !== 'string')
        || (field.state === 'candidate' && (!field.value_id || !positive(field.revision)))) throw Error('批量审核字段不完整')
      keys.add(key)
    }
    if (row.ready && (row.reason || !row.fields.some((f: BulkField) => f.state === 'candidate')
      || row.fields.some((f: BulkField) => f.state === 'blocked'))) throw Error('批量审核就绪状态不一致')
    return row as BulkFieldPreview
  })
}
export function bulkFieldReceipts(items: BulkFieldPreview[], selected: string[]): BulkFieldReceipt[] {
  if (!selected.length || selected.length > 50 || new Set(selected).size !== selected.length) throw Error('请选择1–50个镜头')
  return selected.map(id => {
    const item = items.find(row => row.preparation_id === id)
    if (!item?.ready) throw Error('所选镜头需要单独处理，请重新预览')
    return { preparation_id: id, expected_preparation_revision: item.expected_preparation_revision, preview_sha256: item.preview_sha256 }
  })
}
export function adaptBulkFieldResult(raw: unknown, receipts: BulkFieldReceipt[]): number {
  const value = object(raw)
  if (!positive(value.confirmed_field_count) || !Array.isArray(value.items) || value.items.length !== receipts.length
    || new Set(value.items.map((row: any) => row.preparation_id)).size !== receipts.length
    || value.items.some((row: any) => !receipts.some(r => r.preparation_id === row.preparation_id
      && positive(row.revision) && row.revision > r.expected_preparation_revision))) throw Error('批量确认回执不完整，请重新读取；不要重复提交')
  return value.confirmed_field_count
}
