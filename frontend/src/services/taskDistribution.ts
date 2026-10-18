import type { JsonObject } from './platformTypes'

export const distributionStates = { not_requested: '尚未安排副本', queued: '等待处理', running: '正在处理', ready: '副本处理完成', failed: '副本处理失败' } as const
export interface DistributionPhase {
  taskId: string; kind: 'image' | 'video'; state: keyof typeof distributionStates; revision: number
  requestedAt: string | null; startedAt: string | null; finishedAt: string | null; error: string
}
const record = (v: unknown): JsonObject => {
  if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('副本处理记录不完整')
  return v as JsonObject
}
const date = (v: unknown): string | null => {
  if (v === null) return null
  if (typeof v !== 'string' || !Number.isFinite(Date.parse(v))) throw new Error('副本处理时间不可核验')
  return v
}
export function readDistributionPhase(value: unknown): DistributionPhase {
  const row = record(value)
  if (typeof row.task_id !== 'string' || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(row.task_id)
      || typeof row.kind !== 'string' || !['image','video'].includes(row.kind) || typeof row.state !== 'string' || !Object.prototype.hasOwnProperty.call(distributionStates, row.state)
      || typeof row.revision !== 'number' || !Number.isSafeInteger(row.revision) || row.revision < 0 || typeof row.error !== 'string')
    throw new Error('副本处理状态或修订不可核验')
  const result = { taskId: row.task_id, kind: row.kind as DistributionPhase['kind'], state: row.state as DistributionPhase['state'],
    revision: row.revision, requestedAt: date(row.requested_at), startedAt: date(row.started_at), finishedAt: date(row.finished_at), error: row.error }
  if (result.state === 'not_requested' ? result.revision !== 0 || result.requestedAt !== null || result.startedAt !== null || result.finishedAt !== null
      : result.revision < 1 || !result.requestedAt || (result.state === 'queued' ? result.startedAt !== null || result.finishedAt !== null
        : result.state === 'running' ? !result.startedAt || result.finishedAt !== null : !result.finishedAt))
    throw new Error('副本状态与处理时间不一致')
  return result
}
export function readDistributionPage(value: unknown) {
  const row = record(value)
  if (typeof row.count !== 'number' || !Number.isSafeInteger(row.count) || row.count < 0 || !Array.isArray(row.results)
      || (row.next !== null && typeof row.next !== 'string') || (row.previous !== null && typeof row.previous !== 'string'))
    throw new Error('副本处理分页不可核验')
  const rows = row.results.map(readDistributionPhase)
  if (new Set(rows.map(row => row.taskId)).size !== rows.length || rows.length > row.count) throw new Error('副本任务列表重复或数量异常')
  return { rows, count: row.count, hasNext: row.next !== null, hasPrevious: row.previous !== null }
}
export function distributionRecoveryRequest(row: DistributionPhase): JsonObject {
  if (!['not_requested','failed'].includes(row.state)) throw new Error('此副本不能重复安排')
  return { task_id: row.taskId, expected_revision: row.revision }
}
