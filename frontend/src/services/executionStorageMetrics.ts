import { api, unwrap } from './api'

const keys = ['intent', 'resolved_inputs', 'review_contract', 'evaluation', 'prepared_request'] as const
type Part = typeof keys[number]
export interface ExecutionStorageMetrics {
  preparationId: string; preparationRevision: number; observedAt: string
  items: { key: Part; label: string; state: 'recorded' | 'not_generated'; basis: 'canonical_json_utf8' | 'canonical_request_file_utf8'
    schemaVersion: number | null; sizeBytes: number | null; sha256: string | null; currentLimitBytes: number; overCurrentLimitBytes: number | null }[]
}
const invalid = (): never => { throw new Error('准备字节明细不完整或与当前准备不一致，请重新读取') }
const object = (v: unknown): Record<string, unknown> => v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : invalid()
const integer = (v: unknown, min: number): number => typeof v === 'number' && Number.isSafeInteger(v) && v >= min ? v : invalid()
export function adaptExecutionStorageMetrics(raw: unknown, id: string, revision: number): ExecutionStorageMetrics {
  const row = object(raw)
  if (row.preparation_id !== id || row.preparation_revision !== revision || !Number.isSafeInteger(revision) || revision < 1
    || typeof row.observed_at !== 'string' || !Number.isFinite(Date.parse(row.observed_at))
    || !Array.isArray(row.items) || row.items.length !== keys.length) return invalid()
  const items = row.items.map((rawItem, index): ExecutionStorageMetrics['items'][number] => {
    const item = object(rawItem), key = keys[index]!
    const basis = key === 'prepared_request' ? 'canonical_request_file_utf8' : 'canonical_json_utf8'
    if (item.key !== key || item.basis !== basis || typeof item.label !== 'string' || !item.label.trim()) return invalid()
    const currentLimitBytes = integer(item.current_limit_bytes, 1)
    if (item.state === 'not_generated') {
      if (!['evaluation', 'prepared_request'].includes(key) || item.schema_version !== null || item.size_bytes !== null
        || item.sha256 !== null || item.over_current_limit_bytes !== null) return invalid()
      return { key, label: item.label, basis, state: item.state, schemaVersion: null, sizeBytes: null, sha256: null, currentLimitBytes, overCurrentLimitBytes: null }
    }
    if (item.state !== 'recorded' || typeof item.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(item.sha256)) return invalid()
    const schemaVersion = integer(item.schema_version, 1), sizeBytes = integer(item.size_bytes, 2)
    const overCurrentLimitBytes = integer(item.over_current_limit_bytes, 0)
    if (overCurrentLimitBytes !== Math.max(0, sizeBytes - currentLimitBytes) || (key === 'prepared_request' && schemaVersion !== 1)) return invalid()
    return { key, label: item.label, state: item.state, basis, schemaVersion, sizeBytes, sha256: item.sha256, currentLimitBytes, overCurrentLimitBytes }
  })
  return { preparationId: id, preparationRevision: revision, observedAt: row.observed_at, items }
}
export const executionStorageApi = {
  async get(id: string, revision: number): Promise<ExecutionStorageMetrics> {
    const response = await api.get(`/execution-preparations/${id}/storage-metrics/`, { params: { expected_preparation_revision: revision } })
    return adaptExecutionStorageMetrics(unwrap<unknown>(response), id, revision)
  },
}
