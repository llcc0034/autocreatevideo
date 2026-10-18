export interface RetryContext { expectedStageId: string; expectedAttemptIds: string[]; expectedAssemblyJobIds?: string[]; expectedInputSha256?: string }
export interface RetryImpact {
  id: string; kind: 'call' | 'preparation' | 'assembly'; unit_key: string; preparation_id: string | null; attempt_id: string | null
  status: string; error_code: string; disposition: 'retry' | 'skip' | 'attention'; message: string
  episode_number?: number; consumer_input_sha256?: string; error_message?: string
}
export interface RetryPreview {
  retryContext: RetryContext; mode: 'primary_calls' | 'stage_flow' | 'assembly_jobs'; can_submit: boolean; reason: string; message: string
  call_count: number; preparation_count: number; count: number; next: string | null; previous: string | null; results: RetryImpact[]
  assembly_count?: number
}
const invalid = (): never => { throw Error('阶段重试影响数据不完整，请重新读取。') }
const object = (v: unknown): Record<string, unknown> => v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : invalid()
const text = (v: unknown): string => typeof v === 'string' ? v : invalid()
const count = (v: unknown): number => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0 ? v : invalid()
const nullable = (v: unknown): string | null => v === null ? null : text(v)
export function adaptRetryContext(value: unknown): RetryContext {
  const c = object(value)
  const ids = (value: unknown): string[] => {
    if (!Array.isArray(value) || value.some(id => typeof id !== 'string' || !id) || new Set(value).size !== value.length) invalid()
    return [...value as string[]]
  }
  const result: RetryContext = {expectedStageId:text(c.expected_stage_id),expectedAttemptIds:ids(c.expected_attempt_ids)}
  if (!result.expectedStageId) invalid()
  if ('expected_assembly_job_ids' in c || 'expected_input_sha256' in c) {
    result.expectedAssemblyJobIds = ids(c.expected_assembly_job_ids)
    result.expectedInputSha256 = text(c.expected_input_sha256)
    if (result.expectedAttemptIds.length || !/^(?:[0-9a-f]{64})?$/.test(result.expectedInputSha256)) invalid()
  }
  return result
}
export function sameRetryContext(a: RetryContext, b: RetryContext): boolean {
  return a.expectedStageId === b.expectedStageId && a.expectedAttemptIds.length === b.expectedAttemptIds.length
    && a.expectedAttemptIds.every((id, i) => id === b.expectedAttemptIds[i])
    && a.expectedInputSha256 === b.expectedInputSha256
    && JSON.stringify(a.expectedAssemblyJobIds) === JSON.stringify(b.expectedAssemblyJobIds)
}
export function adaptRetryPreview(value: unknown): RetryPreview {
  const r = object(value), c = object(r.retry_context)
  if (!Array.isArray(c.expected_attempt_ids) || !Array.isArray(r.results) || typeof r.can_submit !== 'boolean'
    || !['primary_calls', 'stage_flow', 'assembly_jobs'].includes(text(r.mode))) invalid()
  const context = adaptRetryContext(c)
  if ((r.mode === 'assembly_jobs') !== (context.expectedAssemblyJobIds !== undefined)) invalid()
  const results = (r.results as unknown[]).map(value => {
    const item = object(value)
    if (!['call', 'preparation', 'assembly'].includes(text(item.kind)) || !['retry', 'skip', 'attention'].includes(text(item.disposition))) invalid()
    const row: RetryImpact = { id:text(item.id), kind:item.kind as RetryImpact['kind'], unit_key:text(item.unit_key),
      preparation_id:nullable(item.preparation_id), attempt_id:nullable(item.attempt_id), status:text(item.status),
      error_code:text(item.error_code), disposition:item.disposition as RetryImpact['disposition'], message:text(item.message) }
    if (!row.id || !row.message || (row.kind === 'call') !== (row.attempt_id !== null)) invalid()
    if (row.kind === 'assembly') {
      row.episode_number = count(item.episode_number)
      row.consumer_input_sha256 = text(item.consumer_input_sha256)
      row.error_message = text(item.error_message)
      if (r.mode !== 'assembly_jobs' || row.preparation_id !== null || !row.episode_number) invalid()
    } else if (!row.preparation_id || r.mode === 'assembly_jobs') invalid()
    return row
  })
  const result: RetryPreview = {retryContext:context,
    mode:r.mode as RetryPreview['mode'], can_submit:r.can_submit as boolean, reason:text(r.reason),message:text(r.message),
    call_count:count(r.call_count),preparation_count:count(r.preparation_count),count:count(r.count),next:nullable(r.next),previous:nullable(r.previous),results}
  if (r.mode === 'assembly_jobs') result.assembly_count = count(r.assembly_count)
  if (r.mode === 'assembly_jobs' && (result.call_count || result.preparation_count)) invalid()
  if (!result.retryContext.expectedStageId || result.count !== result.call_count + result.preparation_count + (result.assembly_count ?? 0)
    || results.length > result.count || (!result.can_submit && !result.reason)) invalid()
  return result
}
