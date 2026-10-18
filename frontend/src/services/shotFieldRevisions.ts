import { isSavedShotId, readShotSourceFields, type ShotSourceField, type ShotSourceFields } from './shotSourceFields'
import type { JsonObject } from './platformTypes'

export interface ShotFieldRevisionDetail {
  source: ShotSourceFields['source']; source_sha256: string
  revision: null | { id: string; revision: number; previous_id: string | null; source_sha256: string; content_sha256: string; created_by: number; created_at: string }
  fields: { original: ShotSourceField; schema: JsonObject; overridden: boolean; present: boolean; value?: unknown }[]
}
export interface ShotFieldRevisionWrite {
  request_id: string; source_sha256: string; expected_revision_id: string | null
  node_key: string; value_path: (string | number)[]; operation: 'set' | 'reset'; value?: unknown
}
const object = (v: unknown): v is Record<string, any> => !!v && typeof v === 'object' && !Array.isArray(v)
const sha = (v: unknown) => typeof v === 'string' && /^[0-9a-f]{64}$/.test(v)
const uuid = (v: unknown) => typeof v === 'string' && isSavedShotId(v)
export function readShotFieldRevision(value: unknown, shotId: string): ShotFieldRevisionDetail {
  if (!object(value) || !sha(value.source_sha256) || !Array.isArray(value.fields)) throw Error('独立字段修订响应不完整。')
  readShotSourceFields({source:value.source,fields:value.fields.map(row=>row?.original)},shotId)
  const revision = value.revision
  if (revision !== null && (!object(revision) || !uuid(revision.id) || !(revision.previous_id === null || uuid(revision.previous_id))
    || !Number.isSafeInteger(revision.revision) || revision.revision < 1 || revision.source_sha256 !== value.source_sha256
    || !sha(revision.content_sha256) || !Number.isSafeInteger(revision.created_by) || revision.created_by < 1
    || typeof revision.created_at !== 'string' || !Number.isFinite(Date.parse(revision.created_at)))) throw Error('修订来源信息缺失或冲突。')
  for (const row of value.fields) {
    if (!object(row.schema) || typeof row.overridden !== 'boolean' || typeof row.present !== 'boolean'
      || row.present !== Object.prototype.hasOwnProperty.call(row,'value')
      || (row.overridden && (!revision || !row.present))) throw Error('修订字段不完整，不能用空值替代。')
  }
  return value as unknown as ShotFieldRevisionDetail
}
