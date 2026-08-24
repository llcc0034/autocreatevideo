import type {
  BibleImageAssetSummary,
  BibleImageCandidateProjection,
  BibleImageCandidateQuality,
  BibleImageCandidatesResult,
  BibleImageEntityType,
  BibleImageLatestCandidate,
  BibleImagePromptConfig,
  BibleImagePromptInput,
  BibleImagePromptInputs,
  BibleImageQualityIssue,
  BibleImageTaskStatus,
} from '@/types'
import { adaptTaskOutputMedia } from './backendAdapters'

type Raw = Record<string, unknown>

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const SHA256_RE = /^[0-9a-f]{64}$/
const SAFE_CODE_RE = /^[a-z][a-z0-9_]{0,99}$/
const entityTypes: BibleImageEntityType[] = ['character', 'scene', 'prop']
const taskStatuses: BibleImageTaskStatus[] = ['queued', 'running', 'succeeded', 'failed', 'cancel_requested', 'cancelled', 'incomplete']

const invalidContract = (): never => {
  throw new Error('服务端返回的实体图片候选契约无效，请刷新后重试。')
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
const entityMapValue = (value: unknown): Record<BibleImageEntityType, string> => {
  const raw = objectValue(value)
  return {
    character: stringValue(raw.character),
    scene: stringValue(raw.scene),
    prop: stringValue(raw.prop),
  }
}

export const adaptBibleImagePromptConfig = (value: unknown): BibleImagePromptConfig => {
  const raw = objectValue(value)
  return {
    schemaVersion: 1,
    stylePrompt: stringValue(raw.style_prompt),
    styleNegativePrompt: stringValue(raw.style_negative_prompt),
    compositionRules: entityMapValue(raw.composition_rules),
    negativeRules: entityMapValue(raw.negative_rules),
    qualitySuffix: stringValue(raw.quality_suffix),
  }
}

const adaptPromptInput = (value: unknown): BibleImagePromptInput => {
  const raw = objectValue(value)
  const role = stringValue(raw.role)
  if (role !== 'positive' && role !== 'negative') invalidContract()
  return {
    key: stringValue(raw.key),
    label: stringValue(raw.label),
    value: stringValue(raw.value),
    role: role as BibleImagePromptInput['role'],
    source: stringValue(raw.source),
    editable: booleanValue(raw.editable),
  }
}

const adaptPromptInputs = (value: unknown): BibleImagePromptInputs => {
  const raw = objectValue(value)
  if (!Array.isArray(raw.positive) || !Array.isArray(raw.negative)) invalidContract()
  return {
    schemaVersion: 1,
    positive: (raw.positive as unknown[]).map(adaptPromptInput),
    negative: (raw.negative as unknown[]).map(adaptPromptInput),
    finalPromptText: stringValue(raw.final_prompt_text),
    negativePrompt: stringValue(raw.negative_prompt),
  }
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

const adaptQualityIssue = (value: unknown): BibleImageQualityIssue => {
  const raw = objectValue(value)
  return {
    code: stringValue(raw.code),
    severity: stringValue(raw.severity),
    path: stringValue(raw.path),
    message: stringValue(raw.message),
  }
}

export const adaptBibleImageCandidateQuality = (value: unknown): BibleImageCandidateQuality => {
  const raw = objectValue(value)
  const rawIssues = raw.issues === undefined ? [] : raw.issues
  const rawWarnings = raw.warnings === undefined ? [] : raw.warnings
  if (!Array.isArray(rawIssues) || !Array.isArray(rawWarnings) || rawWarnings.some((item) => typeof item !== 'string')) invalidContract()
  return {
    policyId: typeof raw.policy_id === 'string' ? raw.policy_id : undefined,
    status: typeof raw.status === 'string' ? raw.status : undefined,
    advisoryOnly: typeof raw.advisory_only === 'boolean' ? raw.advisory_only : undefined,
    wouldBlockEnqueue: typeof raw.would_block_enqueue === 'boolean' ? raw.would_block_enqueue : undefined,
    issues: (rawIssues as unknown[]).map(adaptQualityIssue),
    warnings: rawWarnings as string[],
    raw: { ...raw },
  }
}

const adaptLatestCandidate = (value: unknown): BibleImageLatestCandidate => {
  const raw = objectValue(value)
  const status = stringValue(raw.task_status)
  if (!taskStatuses.includes(status as BibleImageTaskStatus) || !Array.isArray(raw.output_media)) invalidContract()
  return {
    promptArtifactId: uuidValue(raw.prompt_artifact_id),
    generatedWorkflowId: nullableUuidValue(raw.generated_workflow_id),
    generationTaskId: nullableUuidValue(raw.generation_task_id),
    taskStatus: status as BibleImageTaskStatus,
    createdAt: stringValue(raw.created_at),
    idempotencyKey: stringValue(raw.idempotency_key),
    requestFingerprint: sha256Value(raw.request_fingerprint),
    sourceFingerprint: sha256Value(raw.source_fingerprint),
    sourceCurrent: booleanValue(raw.source_current),
    quality: adaptBibleImageCandidateQuality(raw.quality),
    promptInputs: adaptPromptInputs(raw.prompt_inputs),
    outputMedia: (raw.output_media as unknown[]).map(adaptTaskOutputMedia),
    promotedAssets: adaptAssetSummaries(raw.promoted_assets),
    reviewRequired: booleanValue(raw.review_required),
    persistedAsAsset: booleanValue(raw.persisted_as_asset),
  }
}

export const adaptBibleImageCandidateProjection = (value: unknown): BibleImageCandidateProjection => {
  const raw = objectValue(value)
  const entityType = stringValue(raw.entity_type)
  if (!entityTypes.includes(entityType as BibleImageEntityType) || !Array.isArray(raw.candidate_history)) invalidContract()
  const candidateHistory = (raw.candidate_history as unknown[]).map(adaptLatestCandidate)
  const latestCandidate = raw.latest_candidate === null ? null : adaptLatestCandidate(raw.latest_candidate)
  if (
    (latestCandidate === null && candidateHistory.length > 0)
    || (latestCandidate !== null && candidateHistory[0]?.promptArtifactId !== latestCandidate.promptArtifactId)
  ) invalidContract()
  return {
    entityType: entityType as BibleImageEntityType,
    entityId: uuidValue(raw.entity_id),
    entityName: stringValue(raw.entity_name),
    sourceFingerprint: sha256Value(raw.source_fingerprint),
    currentPromptInputs: adaptPromptInputs(raw.current_prompt_inputs),
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
    promptConfig: adaptBibleImagePromptConfig(raw.prompt_config),
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
  bible_image_candidate_incomplete_chain: '实体参考图候选历史链不完整，页面不会推断或补写。',
  bible_image_candidate_target_invalid: '实体参考图候选历史的冻结目标校验失败。',
  bible_image_candidate_target_conflict: '实体参考图候选历史与冻结目标不一致。',
  bible_image_candidate_provenance_mismatch: '实体参考图候选历史的来源校验失败。',
  bible_image_candidate_source_invalid: '实体参考图候选历史无法重建原始来源。',
}

export const bibleImageCandidateErrorMessage = (error: unknown, fallback: string): string => {
  const code = bibleImageCandidateErrorCode(error)
  if (errorMessages[code]) return errorMessages[code]
  if (code.startsWith('bible_image_candidate_')) return '实体图片候选未通过服务端校验，本次没有伪造成功或自动降级。'
  return error instanceof Error && error.message ? error.message : fallback
}
