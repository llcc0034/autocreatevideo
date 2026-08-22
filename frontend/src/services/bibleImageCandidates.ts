import type {
  Asset,
  BibleImageAssetSummary,
  BibleImageCandidateCreateOptions,
  BibleImageCandidateCreateResult,
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
  BibleItem,
} from '@/types'
import { adaptTaskOutputMedia } from './backendAdapters'

type Raw = Record<string, unknown>

export interface BibleImageCandidateRetryState {
  projectId: string
  entityType: BibleImageEntityType
  entityId: string
  idempotencyKey: string
  extraDirection: string
  promptOverrides: Record<string, string>
  seed: number | null
  priority: number
  maxAttempts: number
  queueConfirmationRequired: boolean
}

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

export const adaptBibleImageCandidateCreateResult = (value: unknown): BibleImageCandidateCreateResult => {
  const raw = objectValue(value)
  const candidate = adaptBibleImageCandidateProjection(raw.candidate)
  return {
    projectId: uuidValue(raw.project_id),
    promptArtifactId: uuidValue(raw.prompt_artifact_id),
    generatedWorkflowId: uuidValue(raw.generated_workflow_id),
    generationTaskId: uuidValue(raw.generation_task_id),
    idempotentReplay: booleanValue(raw.idempotent_replay),
    reviewRequired: booleanValue(raw.review_required),
    persistedAsAsset: booleanValue(raw.persisted_as_asset),
    candidate,
  }
}

const characterLength = (value: string): number => Array.from(value).length

export const bibleImageCandidateCreatePayload = (options: BibleImageCandidateCreateOptions) => {
  if (!entityTypes.includes(options.entityType) || !UUID_RE.test(options.entityId)) throw new Error('实体图片候选目标无效。')
  if (!options.idempotencyKey || characterLength(options.idempotencyKey) > 120) throw new Error('实体图片候选幂等键无效。')
  if (characterLength(options.extraDirection) > 2_000) throw new Error('补充创作要求不能超过 2,000 个字符。')
  if (options.seed !== undefined && options.seed !== null && (!Number.isSafeInteger(options.seed) || options.seed < 0)) throw new Error('实体图片候选 Seed 无效。')
  if (!Number.isInteger(options.priority) || options.priority < -100 || options.priority > 100) throw new Error('实体图片候选优先级无效。')
  if (!Number.isInteger(options.maxAttempts) || options.maxAttempts < 1 || options.maxAttempts > 10) throw new Error('实体图片候选最大尝试次数无效。')
  const promptOverrides = Object.fromEntries(
    Object.entries(options.promptOverrides || {})
      .filter(([key, value]) => key.trim() && typeof value === 'string')
      .map(([key, value]) => {
        if (characterLength(key) > 120 || !/^[A-Za-z0-9_.-]+$/.test(key)) throw new Error(`提示词字段名无效：${key}`)
        if (characterLength(value) > 4_000) throw new Error(`提示词字段“${key}”不能超过 4,000 个字符。`)
        return [key, value]
      }),
  )
  return {
    entity_type: options.entityType,
    entity_id: options.entityId,
    idempotency_key: options.idempotencyKey,
    extra_direction: options.extraDirection,
    prompt_overrides: promptOverrides,
    ...(options.seed === undefined || options.seed === null ? {} : { seed: options.seed }),
    priority: options.priority,
    max_attempts: options.maxAttempts,
    extended_generation_confirmed: options.extendedGenerationConfirmed === true,
  }
}

export const bibleImageCandidateRetryStorageKey = (
  projectId: string,
  entityType: BibleImageEntityType,
  entityId: string,
): string => `cineforge_bible_image_candidate_${projectId}_${entityType}_${entityId}`

export const createBibleImageCandidateRetryState = (
  projectId: string,
  entityType: BibleImageEntityType,
  entityId: string,
  extraDirection: string,
  promptOverrides: Record<string, string>,
  randomId: string,
): BibleImageCandidateRetryState => ({
  projectId,
  entityType,
  entityId,
  idempotencyKey: `bible-image-${randomId}`.slice(0, 120),
  extraDirection,
  promptOverrides: { ...promptOverrides },
  seed: null,
  priority: 0,
  maxAttempts: 3,
  queueConfirmationRequired: false,
})

export const parseBibleImageCandidateRetryState = (value: string | null): BibleImageCandidateRetryState | null => {
  if (!value) return null
  try {
    const raw = objectValue(JSON.parse(value))
    const entityType = stringValue(raw.entityType)
    const state: BibleImageCandidateRetryState = {
      projectId: uuidValue(raw.projectId),
      entityType: entityType as BibleImageEntityType,
      entityId: uuidValue(raw.entityId),
      idempotencyKey: stringValue(raw.idempotencyKey),
      extraDirection: stringValue(raw.extraDirection),
      promptOverrides: raw.promptOverrides && typeof raw.promptOverrides === 'object' && !Array.isArray(raw.promptOverrides)
        ? Object.fromEntries(Object.entries(raw.promptOverrides as Raw).filter(([key, value]) => typeof key === 'string' && typeof value === 'string')) as Record<string, string>
        : {},
      seed: raw.seed === null ? null : integerValue(raw.seed),
      priority: integerValue(raw.priority, -100, 100),
      maxAttempts: integerValue(raw.maxAttempts, 1, 10),
      queueConfirmationRequired: booleanValue(raw.queueConfirmationRequired),
    }
    bibleImageCandidateCreatePayload({ ...state, extendedGenerationConfirmed: false })
    return entityTypes.includes(state.entityType) ? state : null
  } catch {
    return null
  }
}

export const mergeBibleImagePromotion = (
  projection: BibleImageCandidateProjection,
  bibleItem: BibleItem,
  asset: Asset,
  outputIndex: number,
  targetCandidate: BibleImageLatestCandidate | null = projection.latestCandidate,
): BibleImageCandidateProjection => {
  if (!bibleItem.referenceAssetIds.includes(asset.id)) bibleItem.referenceAssetIds.push(asset.id)
  const summary: BibleImageAssetSummary = {
    id: asset.id,
    kind: asset.backendKind || asset.type,
    name: asset.name,
    checksum: asset.checksum || '',
    previewUrl: asset.thumbnail || asset.mediaUrl || '',
  }
  const referenceAssets = projection.referenceAssets.some((item) => item.id === summary.id)
    ? projection.referenceAssets
    : [...projection.referenceAssets, summary]
  const latest = targetCandidate
  if (!latest) return { ...projection, referenceAssets }
  const promotedAssets = latest.promotedAssets.some((item) => item.id === summary.id)
    ? latest.promotedAssets
    : [...latest.promotedAssets, summary]
  const outputMedia = latest.outputMedia.map((media, index) => (
    (media.index ?? index) === outputIndex ? { ...media, promotedAssetId: asset.id } : media
  ))
  const updatedCandidate = {
    ...latest,
    outputMedia,
    promotedAssets,
    reviewRequired: false,
    persistedAsAsset: true,
  }
  const sameCandidate = (candidate: BibleImageLatestCandidate | null) => (
    candidate?.promptArtifactId === latest.promptArtifactId
    && candidate?.generationTaskId === latest.generationTaskId
  )
  return {
    ...projection,
    referenceAssets,
    latestCandidate: sameCandidate(projection.latestCandidate)
      ? updatedCandidate
      : projection.latestCandidate,
    candidateHistory: projection.candidateHistory.map((candidate) => (
      sameCandidate(candidate) ? updatedCandidate : candidate
    )),
  }
}

export const bibleImageCandidateErrorCode = (error: unknown): string => {
  const response = error as { response?: { data?: { error?: { code?: unknown } } } }
  const code = String(response.response?.data?.error?.code ?? '').trim().toLowerCase()
  return SAFE_CODE_RE.test(code) ? code : ''
}

const errorMessages: Record<string, string> = {
  production_queue_confirmation_required: '当前生产队列超过建议值。确认后仍会使用同一请求和幂等键完整排队，不会提高 worker 并发。',
  bible_image_candidate_idempotency_conflict: '该安全重试键已用于不同请求。请先到任务中心核对已有任务，不要直接换键重复提交。',
  bible_image_candidate_incomplete_chain: '该请求的持久化候选链不完整，系统没有静默补写。请到任务中心核对后再处理。',
  bible_image_candidate_template_unavailable: '实体参考图系统模板当前不可用，请联系管理员检查固定模板。',
  bible_image_candidate_prompt_invalid: '当前 Bible 字段无法构建实体参考图提示，请检查已保存的视觉字段。',
  bible_image_candidate_target_invalid: '实体图片候选的冻结目标校验失败。',
  bible_image_candidate_target_conflict: '图片产物与冻结的 Bible 目标不一致，未加入参考图。',
  bible_image_candidate_provenance_mismatch: '图片产物来源与冻结任务不一致，未加入参考图。',
  bible_image_candidate_source_stale: 'Bible、已确认译文或全局视觉规则已变化；旧候选需要人工确认后才能作为历史参考图入库。',
  bible_image_candidate_source_invalid: '当前 Bible 或已确认译文无法重建原候选输入；请修正后重新生成。',
}

export const bibleImageCandidateErrorMessage = (error: unknown, fallback: string): string => {
  const code = bibleImageCandidateErrorCode(error)
  if (errorMessages[code]) return errorMessages[code]
  if (code.startsWith('bible_image_candidate_')) return '实体图片候选未通过服务端校验，本次没有伪造成功或自动降级。'
  return error instanceof Error && error.message ? error.message : fallback
}
