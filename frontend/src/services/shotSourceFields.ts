/** Transport validation only; field semantics belong to the frozen server contract. */
export interface ShotSourceField {
  structure_version_id: string
  node_key: string
  field_definition: Record<string, unknown> & {
    field_definition_id: string; field_definition_version_id: string
    key: string; label: string; version: number
  }
  source_path: string
  value_path: (string | number)[]
  present: boolean
  source_present: boolean
  default_paths: string[]
  value?: unknown
}
export interface ShotSourceFields {
  source: {
    shot_id: string; structure_id: string; stage_id: string
    episode_number: number; shot_number: number
    document_sha256: string; materialization_sha256: string
  }
  fields: ShotSourceField[]
}
export const isSavedShotId = (value: string) => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
const object = (v: unknown): v is Record<string, any> => !!v && typeof v === 'object' && !Array.isArray(v)
const text = (v: unknown): v is string => typeof v === 'string' && v.length > 0
const positive = (v: unknown) => Number.isSafeInteger(v) && Number(v) > 0
const uuid = (v: unknown) => typeof v === 'string' && isSavedShotId(v)
const sha = (v: unknown) => typeof v === 'string' && /^[0-9a-f]{64}$/.test(v)
export function readShotSourceFields(value: unknown, shotId: string): ShotSourceFields {
  if (!object(value) || !object(value.source) || value.source.shot_id !== shotId
    || !['shot_id', 'structure_id', 'stage_id'].every(k => uuid(value.source[k]))
    || !['episode_number', 'shot_number'].every(k => positive(value.source[k]))
    || !['document_sha256', 'materialization_sha256'].every(k => sha(value.source[k]))
    || !Array.isArray(value.fields)) throw Error('原结构化来源与当前镜头不一致或响应不完整。')
  for (const field of value.fields) {
    const definition = field?.field_definition
    if (!object(field) || !uuid(field.structure_version_id) || !text(field.node_key)
      || !object(definition) || !uuid(definition.field_definition_id) || !uuid(definition.field_definition_version_id)
      || !text(definition.key) || !text(definition.label) || !positive(definition.version)
      || !text(field.source_path) || !Array.isArray(field.value_path)
      || !field.value_path.every((v: unknown) => typeof v === 'string' || (Number.isSafeInteger(v) && Number(v) >= 0))
      || typeof field.present !== 'boolean' || typeof field.source_present !== 'boolean'
      || (!field.present && field.source_present)
      || field.present !== Object.prototype.hasOwnProperty.call(field, 'value')
      || !Array.isArray(field.default_paths) || !field.default_paths.every(text))
      throw Error('原结构化字段响应不完整，不能用空值替代。')
  }
  return value as unknown as ShotSourceFields
}
