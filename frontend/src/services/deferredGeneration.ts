import type { DeferredFrameSlot, DeferredGenerationRequest, DeferredGenerationStatus } from '@/types'

const statuses: DeferredGenerationStatus[] = [
  'preparing_prompt',
  'waiting_upstream',
  'extracting',
  'materializing',
  'materialized',
  'failed',
  'stale',
  'cancelled',
]
const frameSlots: DeferredFrameSlot[] = ['first_frame', 'last_frame']

const invalidContract = (): never => {
  throw new Error('服务端返回的延迟连续生成请求契约无效')
}
const object = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalidContract()
  return value as Record<string, unknown>
}
const requiredString = (raw: Record<string, unknown>, key: string): string => {
  const value = raw[key]
  if (typeof value === 'string') {
    if (!value.trim()) invalidContract()
    return value
  }
  return invalidContract()
}
const optionalString = (raw: Record<string, unknown>, key: string): string | null => {
  const value = raw[key]
  if (value === null || value === undefined || value === '') return null
  if (typeof value === 'string') return value
  return invalidContract()
}
const requiredNumber = (raw: Record<string, unknown>, key: string): number => {
  const value = Number(raw[key])
  if (!Number.isFinite(value)) invalidContract()
  return value
}
const nullableNumber = (raw: Record<string, unknown>, key: string): number | null => {
  if (raw[key] === null || raw[key] === undefined || raw[key] === '') return null
  const value = Number(raw[key])
  if (!Number.isFinite(value)) invalidContract()
  return value
}
const booleanField = (raw: Record<string, unknown>, key: string): boolean => {
  if (typeof raw[key] !== 'boolean') invalidContract()
  return raw[key] as boolean
}

export const adaptDeferredGenerationRequest = (value: unknown): DeferredGenerationRequest => {
  const raw = object(value)
  const status = requiredString(raw, 'status')
  const frameSlot = requiredString(raw, 'frame_slot')
  const sourceOutputRole = requiredString(raw, 'source_output_role')
  if (!statuses.includes(status as DeferredGenerationStatus)) invalidContract()
  if (!frameSlots.includes(frameSlot as DeferredFrameSlot)) invalidContract()
  if (sourceOutputRole !== 'primary_video') invalidContract()
  return {
    id: requiredString(raw, 'id'),
    projectId: requiredString(raw, 'project_id'),
    sourceTaskId: requiredString(raw, 'source_task_id'),
    sourceOutputRole: 'primary_video',
    targetShotId: requiredString(raw, 'target_shot_id'),
    frameSlot: frameSlot as DeferredFrameSlot,
    targetTemplateId: requiredString(raw, 'target_template_id'),
    targetTemplateHash: requiredString(raw, 'target_template_hash'),
    idempotencyKey: requiredString(raw, 'idempotency_key'),
    requestFingerprint: requiredString(raw, 'request_fingerprint'),
    targetInputFingerprint: requiredString(raw, 'target_input_fingerprint'),
    status: status as DeferredGenerationStatus,
    resolvedSourceOutputIndex: nullableNumber(raw, 'resolved_source_output_index'),
    sourceArtifactSha256: optionalString(raw, 'source_artifact_sha256'),
    sourceManifestSha256: optionalString(raw, 'source_manifest_sha256'),
    derivedAssetId: optionalString(raw, 'derived_asset_id'),
    materializedTaskId: optionalString(raw, 'materialized_task_id'),
    materializedPromptArtifactId: optionalString(raw, 'materialized_prompt_artifact_id'),
    materializedWorkflowId: optionalString(raw, 'materialized_workflow_id'),
    priority: requiredNumber(raw, 'priority'),
    attempts: requiredNumber(raw, 'attempts'),
    maxAttempts: requiredNumber(raw, 'max_attempts'),
    nextAttemptAt: optionalString(raw, 'next_attempt_at'),
    extendedGenerationConfirmed: booleanField(raw, 'extended_generation_confirmed'),
    errorCode: String(raw.error_code || ''),
    errorMessage: String(raw.error_message || ''),
    startedAt: optionalString(raw, 'started_at'),
    finishedAt: optionalString(raw, 'finished_at'),
    createdAt: requiredString(raw, 'created_at'),
    updatedAt: requiredString(raw, 'updated_at'),
    provisionalPromptText: typeof raw.provisional_prompt_text === 'string' ? raw.provisional_prompt_text : undefined,
    provisionalNegativePrompt: typeof raw.provisional_negative_prompt === 'string' ? raw.provisional_negative_prompt : undefined,
    provisionalPromptSha256: typeof raw.provisional_prompt_sha256 === 'string' ? raw.provisional_prompt_sha256 : undefined,
  }
}

export const deferredGenerationStatusLabel = (status: DeferredGenerationStatus): string => ({
  preparing_prompt: '准备临时提示词',
  waiting_upstream: '等待上游视频终态',
  extracting: '抽取首尾帧',
  materializing: '物化下游任务',
  materialized: '已创建下游任务',
  failed: '失败',
  stale: '源输入已过期',
  cancelled: '已取消',
})[status]

export const deferredGenerationFrameSlotLabel = (slot: DeferredFrameSlot): string => (
  slot === 'first_frame' ? '首帧' : '尾帧'
)
