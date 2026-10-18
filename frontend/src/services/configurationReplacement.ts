export interface ConfigurationSuccessor { id: string; version: number; name: string; profile_name: string; profile_version: number; definition_sha256: string }
export interface ConfigurationReplacementOptions { preparation_id: string; revision: number; allowed: boolean; reason: string; replacement_id: string | null; versions: ConfigurationSuccessor[] }
export interface ConfigurationReplacementPreview { preparation_id: string; version_id: string; revision: number; preview_sha256: string; changes: { path: string; before: unknown; after: unknown }[] }
export interface ConfigurationReplacementResult { preparation_id: string; replacement_id: string; version_id: string }
const object = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('策略切换响应不完整')
  return value as Record<string, unknown>
}
const text = (value: unknown) => typeof value === 'string' && value.length > 0
const positive = (value: unknown) => Number.isSafeInteger(value) && Number(value) > 0
const sha = (value: unknown) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
export function replacementOptions(raw: unknown, id: string): ConfigurationReplacementOptions {
  const value = object(raw)
  if (value.preparation_id !== id || !positive(value.revision) || typeof value.allowed !== 'boolean' || typeof value.reason !== 'string'
    || !(value.replacement_id === null || text(value.replacement_id)) || !Array.isArray(value.versions)
    || value.versions.some(item => { const row = object(item); return !text(row.id) || !text(row.name) || !text(row.profile_name)
      || !positive(row.version) || !positive(row.profile_version) || !sha(row.definition_sha256) })
    || new Set(value.versions.map(item => object(item).id)).size !== value.versions.length) throw Error('后继目录身份或版本不完整，请重新读取')
  return value as unknown as ConfigurationReplacementOptions
}
export function replacementPreview(raw: unknown, id: string, version: string, revision: number): ConfigurationReplacementPreview {
  const value = object(raw)
  if (value.preparation_id !== id || value.version_id !== version || value.revision !== revision || !sha(value.preview_sha256)
    || !Array.isArray(value.changes) || value.changes.some(item => { const row = object(item); return !text(row.path) || !('before' in row) || !('after' in row) })) {
    throw Error('预览与当前准备或后继版本不一致，请重新读取')
  }
  return value as unknown as ConfigurationReplacementPreview
}
export function replacementResult(raw: unknown, id: string, version: string): ConfigurationReplacementResult {
  const value = object(raw)
  if (value.preparation_id !== id || value.version_id !== version || !text(value.replacement_id) || value.replacement_id === id) throw Error('替代回执身份不一致，请重新读取原准备')
  return value as unknown as ConfigurationReplacementResult
}
