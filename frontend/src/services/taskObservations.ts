export interface TaskObservation {
  id: string; taskId: string; kind: 'preflight' | 'resource_release'; status: 'passed' | 'failed'
  claimNumber: number; claimStartedAt: string; startedAt: string; finishedAt: string; requestSha256: string
  nodeCount: number | null; linkCount: number | null; schemaSha256: string; policySha256: string
  unloadModels: boolean | null; freeMemory: boolean | null; httpStatus: number | null; errorCode: string
}
export interface TaskObservationPage { count: number; hasNext: boolean; hasPrevious: boolean; results: TaskObservation[] }

export function adaptTaskObservations(value: unknown, taskId: string): TaskObservationPage {
  const object = (value: unknown): Record<string, unknown> => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('检查记录响应不完整')
    return value as Record<string, unknown>
  }
  const text = (row: Record<string, unknown>, key: string, empty = false): string => {
    if (typeof row[key] !== 'string' || (!empty && !row[key])) throw new Error(`检查记录缺少 ${key}`)
    return row[key] as string
  }
  const date = (row: Record<string, unknown>, key: string) => {
    const result = text(row, key)
    if (!Number.isFinite(Date.parse(result))) throw new Error('检查记录时间无效')
    return result
  }
  const integer = (value: unknown): number => {
    if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) throw new Error('检查记录计数无效')
    return value
  }
  const nullableInteger = (value: unknown) => value === null ? null : integer(value)
  const nullableBoolean = (value: unknown): boolean | null => {
    if (value !== null && typeof value !== 'boolean') throw new Error('检查记录操作声明缺失')
    return value
  }
  const raw = object(value)
  if (!Array.isArray(raw.results) || ![raw.next, raw.previous].every(item => item === null || typeof item === 'string' && !!item)) throw new Error('检查记录分页信息缺失')
  const results = raw.results.map(item => {
    const row = object(item)
    if (row.task !== taskId || !['preflight', 'resource_release'].includes(String(row.kind)) || !['passed', 'failed'].includes(String(row.status))) throw new Error('检查记录不属于当前任务或状态无效')
    const result: TaskObservation = { id: text(row, 'id'), taskId, kind: row.kind as TaskObservation['kind'], status: row.status as TaskObservation['status'],
      claimNumber: integer(row.claim_number), claimStartedAt: date(row, 'claim_started_at'), startedAt: date(row, 'started_at'), finishedAt: date(row, 'finished_at'),
      requestSha256: text(row, 'request_sha256'), nodeCount: nullableInteger(row.node_count), linkCount: nullableInteger(row.link_count),
      schemaSha256: text(row, 'schema_sha256', true), policySha256: text(row, 'policy_sha256', true),
      unloadModels: nullableBoolean(row.unload_models), freeMemory: nullableBoolean(row.free_memory), httpStatus: nullableInteger(row.http_status), errorCode: text(row, 'error_code', true) }
    if (result.claimNumber < 1 || Date.parse(result.finishedAt) < Date.parse(result.startedAt)
        || !/^[a-f0-9]{64}$/.test(result.requestSha256)
        || (result.status === 'passed') !== (result.errorCode === '')) throw new Error('检查记录结果或身份不完整')
    if (result.kind === 'preflight') {
      if (result.status === 'passed' && (result.nodeCount === null || result.linkCount === null || !/^[a-f0-9]{64}$/.test(result.schemaSha256))) throw new Error('通过的节点检查缺少证据')
    } else if (!/^[a-f0-9]{64}$/.test(result.policySha256) || result.unloadModels === null || result.freeMemory === null
        || (result.status === 'passed' && (result.httpStatus === null || result.httpStatus < 200 || result.httpStatus >= 300))) throw new Error('资源释放记录缺少证据')
    return result
  })
  if (new Set(results.map(item => item.id)).size !== results.length) throw new Error('检查记录重复')
  return { count: integer(raw.count), hasNext: raw.next !== null, hasPrevious: raw.previous !== null, results }
}
