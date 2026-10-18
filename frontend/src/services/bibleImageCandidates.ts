import type {
  BibleImageAssetSummary,
  BibleImageCandidateProjection,
  BibleImageCandidatesResult,
  BibleImageEntityType,
  BibleImageLatestCandidate,
} from '@/types'

type Raw = Record<string, unknown>

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const SHA256_RE = /^[0-9a-f]{64}$/
const SAFE_CODE_RE = /^[a-z][a-z0-9_]{0,99}$/
const entityTypes: BibleImageEntityType[] = ['character', 'scene', 'prop']
const artifactStatuses: BibleImageLatestCandidate['status'][] = ['available', 'selected', 'rejected', 'invalid', 'legacy_missing']
const bindingRoles: BibleImageLatestCandidate['bindingRole'][] = ['anchor', 'reference', 'unbound']
const integrityStatuses: BibleImageLatestCandidate['integrityStatus'][] = ['valid', 'invalid']

const invalidContract = (): never => {
  throw new Error('服务端返回的正式 ProductionRun 实体参考图历史契约无效，请刷新后重试。')
}

const objectValue = (value: unknown): Raw => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as Raw : invalidContract()
)
const stringValue = (value: unknown): string => typeof value === 'string' ? value : invalidContract()
const booleanValue = (value: unknown): boolean => typeof value === 'boolean' ? value : invalidContract()
const integerValue = (value: unknown, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): number => (
  typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum && value <= maximum
    ? value
    : invalidContract()
)
const uuidValue = (value: unknown): string => {
  const result = stringValue(value)
  return UUID_RE.test(result) ? result : invalidContract()
}
const nullableUuidValue = (value: unknown): string | null => value === null ? null : uuidValue(value)
const sha256Value = (value: unknown): string => {
  const result = stringValue(value)
  return SHA256_RE.test(result) ? result : invalidContract()
}

const adaptAssetSummary = (value: unknown): BibleImageAssetSummary => {
  const raw = objectValue(value)
  return {
    id: uuidValue(raw.id),
    kind: stringValue(raw.kind),
    name: stringValue(raw.name),
    checksum: stringValue(raw.checksum),
    previewUrl: stringValue(raw.preview_url),
  }
}

const adaptAssetSummaries = (value: unknown): BibleImageAssetSummary[] => {
  if (!Array.isArray(value)) invalidContract()
  return (value as unknown[]).map(adaptAssetSummary)
}

const adaptCandidateHistory = (value: unknown): BibleImageLatestCandidate => {
  const raw = objectValue(value)
  const status = stringValue(raw.status) as BibleImageLatestCandidate['status']
  const bindingRole = stringValue(raw.binding_role) as BibleImageLatestCandidate['bindingRole']
  const integrityStatus = stringValue(raw.integrity_status) as BibleImageLatestCandidate['integrityStatus']
  if (
    !artifactStatuses.includes(status)
    || !bindingRoles.includes(bindingRole)
    || !integrityStatuses.includes(integrityStatus)
  ) invalidContract()
  const candidate: BibleImageLatestCandidate = {
    artifactId: uuidValue(raw.artifact_id),
    productionRunId: uuidValue(raw.production_run_id),
    stageRunId: uuidValue(raw.stage_run_id),
    generationAttemptId: uuidValue(raw.generation_attempt_id),
    generationTaskId: nullableUuidValue(raw.generation_task_id),
    preparationId: uuidValue(raw.preparation_id),
    executionInstanceVersionId: uuidValue(raw.execution_instance_version_id),
    executionInstanceName: stringValue(raw.execution_instance_name),
    requestSha256: sha256Value(raw.request_sha256),
    workflowVersionId: nullableUuidValue(raw.workflow_version_id),
    modelProfileVersionId: uuidValue(raw.model_profile_version_id),
    modelIdentifier: stringValue(raw.model_identifier),
    promptProfileVersionId: nullableUuidValue(raw.prompt_profile_version_id),
    targetSnapshotSha256: sha256Value(raw.target_snapshot_sha256),
    sourceCurrent: booleanValue(raw.source_current),
    integrityStatus,
    integrityErrorCode: stringValue(raw.integrity_error_code),
    variantGroup: stringValue(raw.variant_group),
    variantIndex: integerValue(raw.variant_index, 1),
    status,
    bindingRole,
    materializedAsset: raw.materialized_asset === null ? null : adaptAssetSummary(raw.materialized_asset),
    previewUrl: stringValue(raw.preview_url),
    downloadUrl: stringValue(raw.download_url),
    sha256: sha256Value(raw.sha256),
    sizeBytes: integerValue(raw.size_bytes, 1),
    mimeType: stringValue(raw.mime_type),
    createdAt: stringValue(raw.created_at),
  }
  if (!candidate.executionInstanceName || !candidate.variantGroup || !candidate.mimeType || !candidate.createdAt) invalidContract()
  if (candidate.integrityStatus === 'valid' && candidate.integrityErrorCode) invalidContract()
  if (candidate.integrityStatus === 'invalid' && !candidate.integrityErrorCode) invalidContract()
  if (candidate.bindingRole !== 'unbound' && candidate.materializedAsset === null) invalidContract()
  return candidate
}

export const adaptBibleImageCandidateProjection = (value: unknown): BibleImageCandidateProjection => {
  const raw = objectValue(value)
  const entityType = stringValue(raw.entity_type)
  if (!entityTypes.includes(entityType as BibleImageEntityType) || !Array.isArray(raw.candidate_history)) invalidContract()
  const candidateHistory = (raw.candidate_history as unknown[]).map(adaptCandidateHistory)
  const latestCandidate = raw.latest_candidate === null ? null : adaptCandidateHistory(raw.latest_candidate)
  if (
    (latestCandidate === null && candidateHistory.length > 0)
    || (latestCandidate !== null && candidateHistory[0]?.artifactId !== latestCandidate.artifactId)
  ) invalidContract()
  return {
    entityType: entityType as BibleImageEntityType,
    entityId: uuidValue(raw.entity_id),
    entityName: stringValue(raw.entity_name),
    anchorAsset: raw.anchor_asset === null ? null : adaptAssetSummary(raw.anchor_asset),
    referenceAssets: adaptAssetSummaries(raw.reference_assets),
    latestCandidate,
    candidateHistory,
  }
}

export const adaptBibleImageCandidates = (value: unknown): BibleImageCandidatesResult => {
  const raw = objectValue(value)
  const counts = objectValue(raw.counts)
  if (!Array.isArray(raw.candidates)) invalidContract()
  const result: BibleImageCandidatesResult = {
    projectId: uuidValue(raw.project_id),
    candidates: (raw.candidates as unknown[]).map(adaptBibleImageCandidateProjection),
    counts: {
      characters: integerValue(counts.characters),
      scenes: integerValue(counts.scenes),
      props: integerValue(counts.props),
      total: integerValue(counts.total),
      withCandidate: integerValue(counts.with_candidate),
    },
  }
  if (
    result.counts.total !== result.counts.characters + result.counts.scenes + result.counts.props
    || result.candidates.length !== result.counts.total
    || result.counts.withCandidate !== result.candidates.filter((item) => item.candidateHistory.length > 0).length
  ) invalidContract()
  return result
}

const bibleImageCandidateErrorCode = (error: unknown): string => {
  const response = error as { response?: { data?: { error?: { code?: unknown } } } }
  const code = String(response.response?.data?.error?.code ?? '').trim().toLowerCase()
  return SAFE_CODE_RE.test(code) ? code : ''
}

const errorMessages: Record<string, string> = {
  bible_image_candidate_target_invalid: '实体参考图历史的冻结目标校验失败。',
  bible_image_candidate_target_conflict: '实体参考图历史与冻结目标不一致。',
  bible_image_candidate_provenance_mismatch: '实体参考图历史的来源校验失败。',
  bible_image_source_changed: 'Bible 实体来源已变化；请新建 ProductionRun 重新生成并审核。',
}

export const bibleImageCandidateErrorMessage = (error: unknown, fallback: string): string => {
  const code = bibleImageCandidateErrorCode(error)
  if (errorMessages[code]) return errorMessages[code]
  if (code.startsWith('bible_image_')) return '实体参考图历史未通过服务端校验，本次没有伪造成功或自动降级。'
  return error instanceof Error && error.message ? error.message : fallback
}
