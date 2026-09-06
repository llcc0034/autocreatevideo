import type { GenerationAttempt, JsonObject, ShotReferenceBindingsInput, StageRun } from './platformTypes'
import { adaptConditioningModeCapability, type ConditioningModeCapability } from './conditioningCapabilities'
import type { ProductionEffectiveLimits } from './repository'

export interface ScriptReviewPayload {
  sourceStageKey: 'script_generate' | 'script_structure'
  candidateKind: 'generated_script_preview' | 'structured_script_preview'
  submissionSemantics: string
  scriptRevisionId: string
  artifactId: string
  attemptId: string | null
  contentSha256: string
  resultSha256: string
  snapshotSha256: string
  title: string
  logline: string
  warnings: string[]
}

export interface BibleCandidate {
  candidateId: string
  entityType: 'character' | 'scene' | 'prop'
  name: string
  description: string
  visualIdentityEn: string
  occurrence: number
  recommended: boolean
  reviewedContentSha256: string
}

export interface BibleReviewPayload {
  scriptRevisionId: string
  scriptContentSha256: string
  sourceFingerprint: string
  candidateSha256: string
  snapshotSha256: string
  collections: {
    characters: BibleCandidate[]
    scenes: BibleCandidate[]
    props: BibleCandidate[]
  }
  diagnostics: JsonObject[]
}

export interface BibleImageReviewCandidate {
  artifactId: string
  variantGroup: string
  variantIndex: number
  status: 'available' | 'selected'
  sha256: string
  attemptId: string
  promptArtifactId: string
  promptProfileVersionId: string
  promptProfileBindingKey: string
  sourceSnapshotSha256: string
  originProductionRunId: string
  historical: boolean
}

export interface BibleImageReviewEntity {
  entityType: 'character' | 'scene' | 'prop'
  entityId: string
  entityName: string
  sourceSnapshotSha256: string
  candidates: BibleImageReviewCandidate[]
}

export interface BibleImageReviewPayload {
  snapshotSha256: string
  generationStageId: string
  entities: BibleImageReviewEntity[]
}

export interface StoryboardReviewEpisode {
  id: string
  number: number
  title: string
  shotCount: number
  imageCount: number
  videoCount: number
  audioShotCount: number
  durationMs: number
  shots: StoryboardReviewShot[]
}

export interface StoryboardReviewShot {
  id: string
  number: number
  title: string
  mediaType: 'image' | 'video'
  conditioningMode: string
  conditioningModeCapability: ConditioningModeCapability | null
  includeAudio: boolean
  durationMs: number
  characterIds: string[]
  visualReferenceCharacterIds: string[]
  sceneId: string | null
  propIds: string[]
  referenceBindings: ShotReferenceBindingsInput
  requiresFirstFrameContinuity: boolean
  requiresLastFrameContinuity: boolean
}

export interface StoryboardApprovedBibleAsset {
  entityType: 'character' | 'scene' | 'prop'
  entityId: string
  entityName: string
  assetId: string
  checksum: string
  mimeType: string
  sizeBytes: number
  anchor: boolean
}

export interface StoryboardReviewPayload {
  snapshotSha256: string
  canonicalSha256: string
  episodeCount: number
  shotCount: number
  imageCount: number
  videoCount: number
  audioShotCount: number
  durationMs: number
  episodes: StoryboardReviewEpisode[]
  approvedBibleAssets: StoryboardApprovedBibleAsset[]
}

export interface PromptNormalizationCandidate {
  requestId: string
  scopeType: string
  scopeId: string
  fieldKey: string
  label: string
  sourceText: string
  sourceHash: string
  contextHash: string
  translatedText: string
}

export interface VisualRulesReviewPayload {
  snapshotSha256: string
  attemptId: string
  resultSha256: string
  provider: string
  model: string
  scriptRevisionId: string
  scriptContentSha256: string
  scriptContentSizeBytes: number
  baseRulesSha256: string
  confirmedBibleSha256: string
  candidateSha256: string
  candidate: {
    negativePrompt: string
    colorScript: string
    continuitySummary: string
  }
  warnings: string[]
  qualityAdvisories: {
    negativePromptMaxChars: number | null
    colorScriptMaxChars: number | null
    continuitySummaryMaxChars: number | null
  } | null
}

export interface PromptNormalizationReviewPayload {
  snapshotSha256: string
  attemptId: string | null
  resultSha256: string
  provider: string
  model: string
  submissionSemantics: string
  reviewOrigin: 'model_candidate' | 'server_deterministic_candidate' | 'hybrid_candidate' | 'no_submission' | 'human_after_validation_retry_exhausted' | 'human_only_recipe' | 'human_only_external_disabled'
  stopReason: 'retry_limit_reached' | 'safe_feedback_unavailable' | null
  retryPolicy: {
    configKey: string
    retryLimit: number
    maxAttempts: number
    attemptsUsed: number
  } | null
  validationFailures: Array<{
    attemptId: string
    attemptNumber: number
    routeIndex: number | null
    unitKey: string
    batchIndex: number
    errorCode: string
    errorMessage: string
    resolutionId: string
    resolutionEvidenceSha256: string
    contractErrorCode: string
    reasonCode: PromptNormalizationValidationReasonCode | null
    validationContext: PromptNormalizationValidationContext | null
  }>
  advisories: PromptNormalizationAdvisory[]
  candidates: PromptNormalizationCandidate[]
}

export type PromptNormalizationAdvisory =
  | {
      code: 'recommended_input_bytes_exceeded'
      batchIndex: number
      measured: number
      recommendedMaximum: number
      hardMaximum: number
    }
  | {
      code: 'recommended_output_tokens_exceeded'
      batchIndex: number
      usageField: 'completion_tokens' | 'output_tokens'
      measured: number
      recommendedMaximum: number
      hardMaximum: number
    }
  | {
      code: 'recommended_length_out_of_range'
      batchIndex: number
      requestId: string
      fieldKey: string
      measured: number
      minimum: number | null
      maximum: number | null
      lengthUnit: 'characters' | 'english_words' | 'items'
      requestIndex: number
    }

export type PromptNormalizationValidationReasonCode =
  | 'control_characters_invalid'
  | 'duplicate_items'
  | 'empty'
  | 'field_label_forbidden'
  | 'json_invalid'
  | 'json_type_invalid'
  | 'language_policy_invalid'
  | 'length_out_of_range'
  | 'protected_token_mismatch'
  | 'single_line_invalid'
  | 'unresolved_reference_or_placeholder'
  | 'value_invalid'

export interface PromptNormalizationValidationContext {
  schemaVersion: 1
  fieldKey: string
  measured: number | null
  minimum: number | null
  maximum: number | null
  lengthUnit: 'characters' | 'english_words' | 'items'
  requestIndex: number | null
}

export interface PromptNormalizationRetryRepairEvidence {
  failureAttemptId: string
  retryAttemptId: string
  retryAttemptNumber: number
  retryFeedbackSha256: string
}

export type ReviewParseResult<T> = { value: T; error: '' } | { value: null; error: string }
type ProductionReviewLimits = Pick<ProductionEffectiveLimits,
  'maxReferenceBindings' | 'promptReviewMaxCandidates' | 'promptReviewMaxAdvisories'
  | 'promptReviewMaxInputBytes' | 'promptReviewMaxFieldCharacters' | 'promptReviewMaxUsageCount'
>

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const SHA256 = /^[0-9a-f]{64}$/
const SAFE_REFERENCE_IDENTIFIER = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/
const SAFE_PROMPT_FIELD_KEY = /^[a-z][a-z0-9_.-]{0,190}$/
const SAFE_PROMPT_UNIT_KEY = /^[A-Za-z0-9_.:-]{1,191}$/
const SAFE_PROMPT_VALIDATION_REASON_CODES = new Set<PromptNormalizationValidationReasonCode>([
  'control_characters_invalid', 'duplicate_items', 'empty', 'field_label_forbidden',
  'json_invalid', 'json_type_invalid', 'language_policy_invalid', 'length_out_of_range',
  'protected_token_mismatch', 'single_line_invalid', 'unresolved_reference_or_placeholder',
  'value_invalid',
])
const SAFE_PROMPT_CONTRACT_ERROR_CODES = new Set([
  'prompt_field_generation_contract_invalid', 'prompt_normalize_contract_invalid',
  'prompt_normalization_batch_input_exceeded', 'prompt_normalization_batch_items_exceeded',
  'prompt_normalization_policy_invalid', 'prompt_normalization_policy_missing',
  'response_content_filtered', 'response_missing_json_text', 'response_missing_text',
  'response_not_strict_json', 'response_output_budget_exhausted',
  'response_reasoning_without_text', 'response_root_invalid', 'response_tool_call_without_text',
])
const SAFE_PROMPT_FINISH_REASONS = new Set(['content_filter', 'function_call', 'length', 'stop', 'tool_calls'])
const SAFE_PROMPT_USAGE_FIELDS = new Set(['prompt_tokens', 'completion_tokens', 'total_tokens'])
const record = (value: unknown): JsonObject | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : null
const failed = <T>(error: string): ReviewParseResult<T> => ({ value: null, error })
const text = (value: unknown): string | null => typeof value === 'string' ? value : null
const uuid = (value: unknown): string | null => typeof value === 'string' && UUID.test(value) ? value : null
const hash = (value: unknown): string | null => typeof value === 'string' && SHA256.test(value) ? value : null
const stringList = (value: unknown): string[] | null => Array.isArray(value) && value.every((item) => typeof item === 'string') ? value as string[] : null
const objectList = (value: unknown): JsonObject[] | null => Array.isArray(value) && value.every((item) => record(item) !== null) ? value as JsonObject[] : null
const sourceStage = (stages: StageRun[], key: string): StageRun | null => [...stages].sort((a, b) => b.ordinal - a.ordinal).find((stage) => stage.stageKey === key && stage.status === 'succeeded') || null

const validPositiveInteger = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0
const reviewLimits = (limits: ProductionReviewLimits | null | undefined): ProductionReviewLimits | null => {
  if (!limits || ![
    limits.maxReferenceBindings,
    limits.promptReviewMaxCandidates,
    limits.promptReviewMaxAdvisories,
    limits.promptReviewMaxInputBytes,
    limits.promptReviewMaxFieldCharacters,
    limits.promptReviewMaxUsageCount,
  ].every(validPositiveInteger)) return null
  return limits
}

const parsePromptNormalizationValidationContext = (
  value: unknown,
  reasonCode: PromptNormalizationValidationReasonCode,
  limits: ProductionReviewLimits,
): PromptNormalizationValidationContext | null => {
  const context = record(value)
  if (!context || reasonCode !== 'length_out_of_range') return null
  const required = ['schema_version', 'field_key', 'measured', 'minimum', 'maximum', 'length_unit']
  const allowed = new Set([...required, 'request_index'])
  if (required.some((key) => !Object.prototype.hasOwnProperty.call(context, key))
    || Object.keys(context).some((key) => !allowed.has(key))) return null
  const fieldKey = text(context.field_key)
  const lengthUnit = text(context.length_unit)
  const boundedNullableInteger = (candidate: unknown): number | null | undefined => {
    if (candidate === null) return null
    return Number.isInteger(candidate) && Number(candidate) >= 0 && Number(candidate) <= limits.promptReviewMaxFieldCharacters
      ? Number(candidate) : undefined
  }
  const measured = boundedNullableInteger(context.measured)
  const minimum = boundedNullableInteger(context.minimum)
  const maximum = boundedNullableInteger(context.maximum)
  const rawRequestIndex = context.request_index
  const requestIndex = rawRequestIndex === undefined ? null
    : Number.isInteger(rawRequestIndex) && Number(rawRequestIndex) >= 0 && Number(rawRequestIndex) < limits.promptReviewMaxCandidates
      ? Number(rawRequestIndex) : undefined
  if (
    context.schema_version !== 1
    || !fieldKey || !SAFE_PROMPT_FIELD_KEY.test(fieldKey)
    || !lengthUnit || !['characters', 'english_words', 'items'].includes(lengthUnit)
    || measured === undefined || minimum === undefined || maximum === undefined || requestIndex === undefined
    || (minimum === null && maximum === null)
    || (minimum !== null && maximum !== null && minimum > maximum)
    || (measured !== null && !(
      (minimum !== null && measured < minimum)
      || (maximum !== null && measured > maximum)
    ))
  ) return null
  return {
    schemaVersion: 1,
    fieldKey,
    measured,
    minimum,
    maximum,
    lengthUnit: lengthUnit as PromptNormalizationValidationContext['lengthUnit'],
    requestIndex,
  }
}

export const parsePromptNormalizationRetryRepairs = (
  attempts: GenerationAttempt[],
  failures: PromptNormalizationReviewPayload['validationFailures'],
): ReviewParseResult<PromptNormalizationRetryRepairEvidence[]> => {
  const failureIds = new Set(failures.map((failure) => failure.attemptId))
  const failureAttemptNumbers = new Map(failures.map((failure) => [failure.attemptId, failure.attemptNumber]))
  const repairs: PromptNormalizationRetryRepairEvidence[] = []
  const repaired = new Set<string>()
  for (const attempt of attempts.filter((value) => value.operation === 'prompt.normalize')) {
    const repairOfAttemptId = attempt.repairOfAttemptId || ''
    const retryFeedbackSha256 = attempt.retryFeedbackSha256 || ''
    if (!repairOfAttemptId && !retryFeedbackSha256) continue
    if (!uuid(repairOfAttemptId) || !hash(retryFeedbackSha256)) return failed('提示词安全修复链的 attempt 引用或反馈 hash 无效。')
    if (!failureIds.has(repairOfAttemptId)) continue
    if (repaired.has(repairOfAttemptId)) return failed('同一个失败 attempt 存在重复的安全修复链。')
    const failureAttemptNumber = failureAttemptNumbers.get(repairOfAttemptId) || 0
    if (!uuid(attempt.id) || !Number.isInteger(attempt.attemptNumber) || attempt.attemptNumber <= failureAttemptNumber) {
      return failed('提示词安全修复链的下一 attempt 顺序无效。')
    }
    repaired.add(repairOfAttemptId)
    repairs.push({
      failureAttemptId: repairOfAttemptId,
      retryAttemptId: attempt.id,
      retryAttemptNumber: attempt.attemptNumber,
      retryFeedbackSha256,
    })
  }
  return { value: repairs, error: '' }
}

const parseReferenceEntity = (value: unknown): NonNullable<ShotReferenceBindingsInput['characters']>[number] | null => {
  const item = record(value)
  if (!item || Object.keys(item).some((key) => !['entity_id', 'asset_id'].includes(key))) return null
  const entityId = uuid(item.entity_id)
  const assetId = uuid(item.asset_id)
  return entityId && assetId ? { entityId, assetId } : null
}

const parseShotReferenceBindings = (value: unknown, maxReferenceBindings: number): ShotReferenceBindingsInput | null => {
  const source = record(value)
  if (!source) return null
  const allowed = new Set([
    'first_frame_asset_id', 'last_frame_asset_id', 'first_frame_reference_asset_id',
    'characters', 'scene', 'props', 'videos', 'audios',
  ])
  if (Object.keys(source).some((key) => !allowed.has(key))) return null
  const result: ShotReferenceBindingsInput = {}
  for (const [sourceKey, targetKey] of [
    ['first_frame_asset_id', 'firstFrameAssetId'],
    ['last_frame_asset_id', 'lastFrameAssetId'],
    ['first_frame_reference_asset_id', 'firstFrameReferenceAssetId'],
  ] as const) {
    if (source[sourceKey] === undefined) continue
    const assetId = uuid(source[sourceKey])
    if (!assetId) return null
    result[targetKey] = assetId
  }
  for (const [sourceKey, targetKey] of [
    ['characters', 'characters'], ['props', 'props'],
  ] as const) {
    if (source[sourceKey] === undefined) continue
    if (!Array.isArray(source[sourceKey])) return null
    const items = (source[sourceKey] as unknown[]).map(parseReferenceEntity)
    if (items.some((item) => item === null)) return null
    result[targetKey] = items as NonNullable<ShotReferenceBindingsInput[typeof targetKey]>
  }
  if (source.scene !== undefined) {
    const scene = parseReferenceEntity(source.scene)
    if (!scene) return null
    result.scene = scene
  }
  if (source.videos !== undefined) {
    if (!Array.isArray(source.videos)) return null
    const videos: NonNullable<ShotReferenceBindingsInput['videos']> = []
    for (const value of source.videos) {
      const item = record(value)
      const assetId = uuid(item?.asset_id)
      const purpose = text(item?.purpose)
      const useEmbeddedAudio = item?.use_embedded_audio
      const audioAssetId = item?.audio_asset_id === undefined ? undefined : uuid(item.audio_asset_id)
      const audioPurpose = item?.audio_purpose === undefined ? undefined : text(item.audio_purpose)
      if (
        !item || Object.keys(item).some((key) => ![
          'asset_id', 'purpose', 'use_embedded_audio', 'audio_asset_id', 'audio_purpose',
        ].includes(key))
        || !assetId
        || !purpose || !SAFE_REFERENCE_IDENTIFIER.test(purpose)
        || typeof useEmbeddedAudio !== 'boolean'
        || (item.audio_asset_id !== undefined && !audioAssetId)
        || (audioPurpose !== undefined && (!audioPurpose || !SAFE_REFERENCE_IDENTIFIER.test(audioPurpose)))
        || (Boolean(useEmbeddedAudio || audioAssetId) !== Boolean(audioPurpose))
      ) return null
      videos.push({
        assetId,
        purpose: purpose as NonNullable<ShotReferenceBindingsInput['videos']>[number]['purpose'],
        useEmbeddedAudio,
        ...(audioAssetId ? { audioAssetId } : {}),
        ...(audioPurpose ? { audioPurpose } : {}),
      })
    }
    if (videos.length > maxReferenceBindings) return null
    result.videos = videos
  }
  if (source.audios !== undefined) {
    if (!Array.isArray(source.audios)) return null
    const audios: NonNullable<ShotReferenceBindingsInput['audios']> = []
    for (const value of source.audios) {
      const item = record(value)
      const assetId = uuid(item?.asset_id)
      const purpose = text(item?.purpose)
      if (
        !item || Object.keys(item).some((key) => !['asset_id', 'purpose'].includes(key))
        || !assetId || !purpose || !SAFE_REFERENCE_IDENTIFIER.test(purpose)
      ) return null
      audios.push({ assetId, purpose })
    }
    if (audios.length > maxReferenceBindings) return null
    result.audios = audios
  }
  const assetIds = [
    result.firstFrameAssetId,
    result.lastFrameAssetId,
    result.firstFrameReferenceAssetId,
    ...(result.characters || []).map((item) => item.assetId),
    result.scene?.assetId,
    ...(result.props || []).map((item) => item.assetId),
    ...(result.videos || []).flatMap((item) => [item.assetId, item.audioAssetId]),
    ...(result.audios || []).map((item) => item.assetId),
  ].filter((item): item is string => Boolean(item))
  if (new Set(assetIds).size !== assetIds.length) return null
  return result
}

export const parseScriptReviewPayload = (stages: StageRun[]): ReviewParseResult<ScriptReviewPayload> => {
  const stage = [...stages].sort((a, b) => b.ordinal - a.ordinal).find((item) => (
    ['script_generate', 'script_structure'].includes(item.stageKey) && item.status === 'succeeded'
  )) || null
  if (!stage) return failed('缺少已成功的 script_generate 或 script_structure 阶段，不能核验剧本来源。')
  if (!hash(stage.outputSnapshotSha256)) return failed(`${stage.stageKey} 输出快照 hash 缺失或格式错误。`)
  const snapshot = record(stage.outputSnapshot)
  if (!snapshot || snapshot.stage_key !== stage.stageKey) return failed(`${stage.stageKey} 输出快照缺失或 stage_key 不匹配。`)
  const payload = record(snapshot.review_payload)
  const candidateKind = text(payload?.kind)
  const expectedKind = stage.stageKey === 'script_generate' ? 'generated_script_preview' : 'structured_script_preview'
  if (!payload || candidateKind !== expectedKind) return failed(`${stage.stageKey} review_payload kind 缺失或不受支持。`)
  const revisionId = uuid(payload.script_revision_id)
  const artifactId = uuid(payload.artifact_id)
  const attemptId = payload.attempt_id === null ? null : uuid(payload.attempt_id)
  const contentSha256 = hash(payload.content_sha256)
  const submissionSemantics = text(payload.submission_semantics) || ''
  const noSubmission = submissionSemantics === 'no_submission'
  const resultSha256 = noSubmission && payload.result_sha256 === '' ? '' : hash(payload.result_sha256)
  const title = text(payload.title)
  const logline = text(payload.logline)
  const warnings = payload.warnings === undefined ? [] : stringList(payload.warnings)
  if (!revisionId || !artifactId || (!noSubmission && !attemptId) || (noSubmission && attemptId !== null)) return failed('剧本审核快照中的 revision、artifact 或 attempt 标识无效。')
  if (!contentSha256 || resultSha256 === null) return failed('剧本审核快照中的内容 hash 或模型结果 hash 无效。')
  if (title === null || logline === null || warnings === null) return failed('剧本审核快照中的标题、梗概或 warnings 结构无效。')
  const canonical = record(snapshot.canonical)
  if (!canonical || canonical.id !== revisionId || canonical.content_sha256 !== contentSha256) return failed('剧本审核快照与正式 ScriptRevision 指纹不一致。')
  return {
    value: {
      sourceStageKey: stage.stageKey as ScriptReviewPayload['sourceStageKey'],
      candidateKind: candidateKind as ScriptReviewPayload['candidateKind'],
      submissionSemantics,
      scriptRevisionId: revisionId,
      artifactId,
      attemptId,
      contentSha256,
      resultSha256,
      snapshotSha256: stage.outputSnapshotSha256,
      title,
      logline,
      warnings,
    },
    error: '',
  }
}

const parseCandidate = (value: unknown, expectedType: BibleCandidate['entityType']): BibleCandidate | null => {
  const row = record(value)
  if (!row || row.entity_type !== expectedType) return null
  const candidateId = text(row.candidate_id)
  const name = text(row.name)
  const description = text(row.description)
  const visualIdentityEn = text(row.visual_identity_en)
  const reviewedContentSha256 = hash(row.reviewed_content_sha256)
  const occurrence = Number(row.occurrence)
  if (!candidateId?.trim() || !name?.trim() || description === null || visualIdentityEn === null || !reviewedContentSha256 || !Number.isInteger(occurrence) || occurrence < 0 || typeof row.recommended !== 'boolean') return null
  return { candidateId, entityType: expectedType, name, description, visualIdentityEn, occurrence, recommended: row.recommended, reviewedContentSha256 }
}

export const parseBibleReviewPayload = (stages: StageRun[]): ReviewParseResult<BibleReviewPayload> => {
  const stage = sourceStage(stages, 'bible_materialize')
  if (!stage) return failed('缺少已成功的 bible_materialize 阶段，不能核验 Bible 候选。')
  if (!hash(stage.outputSnapshotSha256)) return failed('bible_materialize 输出快照 hash 缺失或格式错误。')
  const snapshot = record(stage.outputSnapshot)
  if (!snapshot || snapshot.stage_key !== 'bible_materialize') return failed('bible_materialize 输出快照缺失或 stage_key 不匹配。')
  const payload = record(snapshot.review_payload)
  if (!payload || payload.kind !== 'script_bible_candidates') return failed('bible_materialize review_payload kind 缺失或不受支持。')
  const scriptRevisionId = uuid(payload.script_revision_id)
  const scriptContentSha256 = hash(payload.script_content_sha256)
  const sourceFingerprint = hash(payload.source_fingerprint)
  const candidateSha256 = hash(payload.candidate_sha256)
  const diagnostics = objectList(payload.diagnostics)
  const collections = record(payload.collections)
  if (!scriptRevisionId || !scriptContentSha256 || !sourceFingerprint || !candidateSha256) return failed('Bible 审核快照的 revision 或内容指纹无效。')
  if (!diagnostics || !collections) return failed('Bible 审核快照的 collections 或 diagnostics 结构无效。')
  const parsed = {} as BibleReviewPayload['collections']
  for (const [key, entityType] of [['characters', 'character'], ['scenes', 'scene'], ['props', 'prop']] as const) {
    const rows = collections[key]
    if (!Array.isArray(rows) || rows.length === 0) return failed(`Bible 审核快照缺少 ${key} 候选。`)
    const candidates = rows.map((row) => parseCandidate(row, entityType))
    if (candidates.some((row) => row === null)) return failed(`Bible 审核快照的 ${key} 候选结构无效。`)
    parsed[key] = candidates as BibleCandidate[]
  }
  return { value: { scriptRevisionId, scriptContentSha256, sourceFingerprint, candidateSha256, snapshotSha256: stage.outputSnapshotSha256, collections: parsed, diagnostics }, error: '' }
}

export const parseBibleImageReviewPayload = (
  stages: StageRun[],
): ReviewParseResult<BibleImageReviewPayload> => {
  const stage = sourceStage(stages, 'bible_image_generation')
  if (!stage) return failed('缺少已成功的 bible_image_generation 阶段，不能核验参考图候选。')
  if (!hash(stage.outputSnapshotSha256)) return failed('bible_image_generation 输出快照 hash 缺失或格式错误。')
  const snapshot = record(stage.outputSnapshot)
  if (!snapshot || snapshot.stage_key !== 'bible_image_generation' || snapshot.formal_object_type !== 'bible_image_candidate_manifest') {
    return failed('bible_image_generation 正式输出快照缺失或类型不匹配。')
  }
  const canonical = record(snapshot.canonical)
  const generationStageId = uuid(canonical?.generation_stage_id)
  if (!canonical || !generationStageId || !Array.isArray(canonical.entities) || canonical.entities.length === 0) {
    return failed('Bible 参考图候选快照没有完整实体清单。')
  }
  const entities: BibleImageReviewEntity[] = []
  const entityKeys = new Set<string>()
  const artifactIds = new Set<string>()
  for (const entityValue of canonical.entities) {
    const entity = record(entityValue)
    const entityType = text(entity?.entity_type)
    const entityId = uuid(entity?.entity_id)
    const entityName = text(entity?.entity_name)
    const sourceSnapshotSha256 = hash(entity?.source_snapshot_sha256)
    if (!entity || !['character', 'scene', 'prop'].includes(entityType || '') || !entityId || !entityName?.trim() || !sourceSnapshotSha256 || !Array.isArray(entity.candidates) || entity.candidates.length === 0) {
      return failed('Bible 参考图候选实体标识、来源或候选列表无效。')
    }
    const entityKey = `${entityType}:${entityId}`
    if (entityKeys.has(entityKey)) return failed('Bible 参考图候选快照重复声明同一实体。')
    entityKeys.add(entityKey)
    const candidates: BibleImageReviewCandidate[] = []
    for (const candidateValue of entity.candidates) {
      const candidate = record(candidateValue)
      const artifactId = uuid(candidate?.artifact_id)
      const variantGroup = text(candidate?.variant_group)
      const variantIndex = Number(candidate?.variant_index)
      const status = text(candidate?.status)
      const sha256 = hash(candidate?.sha256)
      const attemptId = uuid(candidate?.attempt_id)
      const promptArtifactId = uuid(candidate?.prompt_artifact_id)
      const promptProfileVersionId = uuid(candidate?.prompt_profile_version_id)
      const promptProfileBindingKey = text(candidate?.prompt_profile_binding_key)
      const candidateSourceSnapshotSha256 = hash(candidate?.source_snapshot_sha256)
      const originProductionRunId = uuid(candidate?.origin_production_run_id)
      if (!candidate || !artifactId || artifactIds.has(artifactId) || !variantGroup?.startsWith(`bible:${entityType}:${entityId}:candidate:`) || !Number.isInteger(variantIndex) || variantIndex < 1 || !['available', 'selected'].includes(status || '') || !sha256 || !attemptId || !promptArtifactId || !promptProfileVersionId || !promptProfileBindingKey?.trim() || !candidateSourceSnapshotSha256 || !originProductionRunId || typeof candidate.historical !== 'boolean') {
        return failed('Bible 参考图候选 artifact、版本、attempt 或 PromptProfile 证据无效。')
      }
      artifactIds.add(artifactId)
      candidates.push({ artifactId, variantGroup, variantIndex, status: status as BibleImageReviewCandidate['status'], sha256, attemptId, promptArtifactId, promptProfileVersionId, promptProfileBindingKey, sourceSnapshotSha256: candidateSourceSnapshotSha256, originProductionRunId, historical: candidate.historical })
    }
    entities.push({ entityType: entityType as BibleImageReviewEntity['entityType'], entityId, entityName, sourceSnapshotSha256, candidates })
  }
  return { value: { snapshotSha256: stage.outputSnapshotSha256, generationStageId, entities }, error: '' }
}

export const parseStoryboardReviewPayload = (
  stages: StageRun[],
  limits: ProductionReviewLimits | null | undefined,
): ReviewParseResult<StoryboardReviewPayload> => {
  const resolvedLimits = reviewLimits(limits)
  if (!resolvedLimits) return failed('服务端生产限制缺失或无效，不能核验分镜审核快照。')
  const stage = sourceStage(stages, 'storyboard')
  if (!stage) return failed('缺少已成功的 storyboard 阶段，不能核验分镜来源。')
  if (!hash(stage.outputSnapshotSha256)) return failed('storyboard 输出快照 hash 缺失或格式错误。')
  const snapshot = record(stage.outputSnapshot)
  if (!snapshot || snapshot.stage_key !== 'storyboard' || snapshot.formal_object_type !== 'storyboard') return failed('storyboard 正式输出快照缺失或类型不匹配。')
  const canonicalSha256 = hash(snapshot.canonical_sha256)
  const canonical = record(snapshot.canonical)
  if (!canonicalSha256 || !canonical) return failed('storyboard canonical 或服务端 canonical hash 缺失。')
  if (!Array.isArray(canonical.episodes) || canonical.episodes.length === 0) return failed('storyboard canonical 中没有 Episode。')

  const episodes: StoryboardReviewEpisode[] = []
  for (const episodeValue of canonical.episodes) {
    const episode = record(episodeValue)
    const episodeId = uuid(episode?.id)
    const episodeNumber = Number(episode?.number)
    const episodeTitle = text(episode?.title)
    if (!episode || !episodeId || !Number.isInteger(episodeNumber) || episodeNumber < 1 || episodeTitle === null || !Array.isArray(episode.shots) || episode.shots.length === 0) {
      return failed('storyboard canonical 的 Episode 结构无效或没有 Shot。')
    }
    let durationMs = 0
    let imageCount = 0
    let videoCount = 0
    let audioShotCount = 0
    const shots: StoryboardReviewShot[] = []
    for (const shotValue of episode.shots) {
      const shot = record(shotValue)
      const shotId = uuid(shot?.id)
      const shotNumber = Number(shot?.number)
      const shotTitle = text(shot?.title)
      const shotDurationMs = Number(shot?.duration_ms)
      const mediaType = text(shot?.media_type)
      const conditioningMode = text(shot?.conditioning_mode)
      const conditioningModeCapabilityValue = record(shot?.conditioning_mode_capability)
      let conditioningModeCapability: ConditioningModeCapability | null = null
      try {
        conditioningModeCapability = conditioningModeCapabilityValue
          ? adaptConditioningModeCapability(conditioningModeCapabilityValue)
          : null
      } catch {
        return failed('storyboard canonical 的条件输入能力合同无效。')
      }
      const characterIds = Array.isArray(shot?.character_ids) ? shot.character_ids.map(uuid) : null
      const visualReferenceCharacterIds = Array.isArray(shot?.visual_reference_character_ids)
        ? shot.visual_reference_character_ids.map(uuid)
        : null
      const sceneId = shot?.scene_id === null ? null : uuid(shot?.scene_id)
      const propIds = Array.isArray(shot?.prop_ids) ? shot.prop_ids.map(uuid) : null
      const referenceBindings = parseShotReferenceBindings(shot?.reference_bindings, resolvedLimits.maxReferenceBindings)
      if (!shot || !shotId || !Number.isInteger(shotNumber) || shotNumber < 1 || shotTitle === null || !Number.isInteger(shotDurationMs) || shotDurationMs <= 0 || !['image', 'video'].includes(mediaType || '') || (mediaType === 'video' && (!conditioningMode || !/^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(conditioningMode) || !conditioningModeCapability || conditioningModeCapability.key !== conditioningMode)) || typeof shot.include_audio !== 'boolean' || !characterIds || characterIds.some((value) => !value) || !visualReferenceCharacterIds || visualReferenceCharacterIds.some((value) => !value || !characterIds.includes(value)) || new Set(visualReferenceCharacterIds).size !== visualReferenceCharacterIds.length || !propIds || propIds.some((value) => !value) || (shot.scene_id !== null && !sceneId) || !referenceBindings || typeof shot.requires_first_frame_continuity !== 'boolean' || typeof shot.requires_last_frame_continuity !== 'boolean') {
        return failed('storyboard canonical 的 Shot 标识、媒体类型、音频或时长结构无效。')
      }
      durationMs += shotDurationMs
      if (mediaType === 'image') imageCount += 1
      else videoCount += 1
      if (shot.include_audio) audioShotCount += 1
      shots.push({
        id: shotId,
        number: shotNumber,
        title: shotTitle,
        mediaType: mediaType as StoryboardReviewShot['mediaType'],
        conditioningMode: conditioningMode || '',
        conditioningModeCapability,
        includeAudio: shot.include_audio,
        durationMs: shotDurationMs,
        characterIds: characterIds as string[],
        visualReferenceCharacterIds: visualReferenceCharacterIds as string[],
        sceneId,
        propIds: propIds as string[],
        referenceBindings,
        requiresFirstFrameContinuity: shot.requires_first_frame_continuity,
        requiresLastFrameContinuity: shot.requires_last_frame_continuity,
      })
    }
    episodes.push({ id: episodeId, number: episodeNumber, title: episodeTitle, shotCount: episode.shots.length, imageCount, videoCount, audioShotCount, durationMs, shots })
  }

  const approvedBibleAssets: StoryboardApprovedBibleAsset[] = []
  if (stages.some((item) => item.stageKey === 'bible_image_generation')) {
    const reviewStage = sourceStage(stages, 'bible_image_review')
    if (!reviewStage || !hash(reviewStage.outputSnapshotSha256)) return failed('缺少已成功且可校验的 bible_image_review，不能选择逐镜参考图。')
    const reviewSnapshot = record(reviewStage.outputSnapshot)
    const reviewCanonical = record(reviewSnapshot?.canonical)
    if (!reviewSnapshot || reviewSnapshot.formal_object_type !== 'bible_image_bindings' || !reviewCanonical || reviewCanonical.kind !== 'bible_image_bindings' || !Array.isArray(reviewCanonical.bindings)) {
      return failed('bible_image_review 正式绑定快照缺失或类型不匹配。')
    }
    const seenAssets = new Set<string>()
    for (const bindingValue of reviewCanonical.bindings) {
      const binding = record(bindingValue)
      const entityType = text(binding?.entity_type)
      const entityId = uuid(binding?.entity_id)
      const entityName = text(binding?.entity_name)
      const selectedAssetIds = Array.isArray(binding?.selected_asset_ids) ? binding.selected_asset_ids.map(uuid) : null
      const anchorAssetId = uuid(binding?.anchor_asset_id)
      const anchorFact = record(binding?.anchor_asset)
      const referenceFacts = objectList(binding?.reference_assets)
      if (!binding || !['character', 'scene', 'prop'].includes(entityType || '') || !entityId || !entityName?.trim() || !selectedAssetIds || selectedAssetIds.some((value) => !value) || !anchorAssetId || !selectedAssetIds.includes(anchorAssetId) || !anchorFact || !referenceFacts) {
        return failed('bible_image_review 绑定实体、选择集或 anchor 结构无效。')
      }
      const facts = [anchorFact, ...referenceFacts]
      for (const rawAssetId of selectedAssetIds) {
        const assetId = rawAssetId as string
        if (seenAssets.has(assetId)) return failed('bible_image_review 在多个实体中重复声明同一 Asset。')
        const fact = facts.find((item) => item.asset_id === assetId) || null
        const checksum = hash(fact?.checksum)
        const mimeType = text(fact?.mime_type)
        const sizeBytes = Number(fact?.size_bytes)
        if (!fact || !checksum || !mimeType?.startsWith('image/') || !Number.isInteger(sizeBytes) || sizeBytes <= 0) {
          return failed('bible_image_review 所选 Asset 缺少可校验的图片 hash、MIME 或大小。')
        }
        seenAssets.add(assetId)
        approvedBibleAssets.push({
          entityType: entityType as StoryboardApprovedBibleAsset['entityType'],
          entityId,
          entityName,
          assetId,
          checksum,
          mimeType,
          sizeBytes,
          anchor: assetId === anchorAssetId,
        })
      }
    }
    if (!approvedBibleAssets.length) return failed('bible_image_review 没有冻结任何可供逐镜选择的 Asset。')
  }
  return {
    value: {
      snapshotSha256: stage.outputSnapshotSha256,
      canonicalSha256,
      episodeCount: episodes.length,
      shotCount: episodes.reduce((total, item) => total + item.shotCount, 0),
      imageCount: episodes.reduce((total, item) => total + item.imageCount, 0),
      videoCount: episodes.reduce((total, item) => total + item.videoCount, 0),
      audioShotCount: episodes.reduce((total, item) => total + item.audioShotCount, 0),
      durationMs: episodes.reduce((total, item) => total + item.durationMs, 0),
      episodes,
      approvedBibleAssets,
    },
    error: '',
  }
}

export const parseVisualRulesReviewPayload = (
  stages: StageRun[],
): ReviewParseResult<VisualRulesReviewPayload> => {
  const stage = sourceStage(stages, 'visual_rules_generate')
  if (!stage) return failed('缺少已成功的 visual_rules_generate 阶段，不能核验全剧影视规则候选。')
  if (!hash(stage.outputSnapshotSha256)) return failed('全剧影视规则生成输出快照 hash 缺失或格式错误。')
  const snapshot = record(stage.outputSnapshot)
  if (!snapshot || snapshot.stage_key !== 'visual_rules_generate') return failed('全剧影视规则输出快照缺失或 stage_key 不匹配。')
  const payload = record(snapshot.review_payload)
  if (!payload || payload.kind !== 'visual_rules_candidates') return failed('全剧影视规则 review_payload kind 缺失或不受支持。')
  const attemptId = uuid(payload.attempt_id)
  const resultSha256 = hash(payload.result_sha256)
  const provider = text(payload.provider)
  const model = text(payload.model)
  const source = record(payload.source_revision)
  const scriptRevisionId = uuid(source?.id)
  const scriptContentSha256 = hash(source?.content_sha256)
  const scriptContentSizeBytes = source?.content_size_bytes
  const baseRulesSha256 = hash(payload.base_rules_sha256)
  const confirmedBibleSha256 = hash(payload.confirmed_bible_sha256)
  const candidateSha256 = hash(payload.candidate_sha256)
  const candidate = record(payload.candidate)
  const warnings = stringList(payload.warnings)
  const rawQualityAdvisories = payload.quality_advisories === undefined
    ? null
    : record(payload.quality_advisories)
  const advisoryMaximum = (key: string): number | null | undefined => {
    if (!rawQualityAdvisories || rawQualityAdvisories[key] === undefined) return null
    const value = rawQualityAdvisories[key]
    return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : undefined
  }
  const negativePromptMaxChars = advisoryMaximum('negative_prompt_max_chars')
  const colorScriptMaxChars = advisoryMaximum('color_script_max_chars')
  const continuitySummaryMaxChars = advisoryMaximum('continuity_summary_max_chars')
  if (
    !attemptId || !resultSha256 || !provider?.trim() || !model?.trim()
    || !scriptRevisionId || !scriptContentSha256
    || typeof scriptContentSizeBytes !== 'number' || !Number.isSafeInteger(scriptContentSizeBytes) || scriptContentSizeBytes < 1
    || !baseRulesSha256 || !confirmedBibleSha256 || !candidateSha256 || !candidate || !warnings
    || (rawQualityAdvisories !== null && rawQualityAdvisories.schema_version !== 1)
    || negativePromptMaxChars === undefined
    || colorScriptMaxChars === undefined
    || continuitySummaryMaxChars === undefined
  ) return failed('全剧影视规则候选的 attempt、剧本、Bible、模型或 hash 证据无效。')
  const allowedCandidateKeys = ['negative_prompt', 'color_script', 'continuity_summary']
  if (Object.keys(candidate).length !== allowedCandidateKeys.length || Object.keys(candidate).some((key) => !allowedCandidateKeys.includes(key))) {
    return failed('全剧影视规则候选字段不完整或包含未知字段。')
  }
  const negativePrompt = text(candidate.negative_prompt)
  const colorScript = text(candidate.color_script)
  const continuitySummary = text(candidate.continuity_summary)
  if (!negativePrompt?.trim() || !colorScript?.trim() || !continuitySummary?.trim()) {
    return failed('全剧影视规则候选三项字段必须全部非空。')
  }
  return {
    value: {
      snapshotSha256: stage.outputSnapshotSha256,
      attemptId,
      resultSha256,
      provider,
      model,
      scriptRevisionId,
      scriptContentSha256,
      scriptContentSizeBytes,
      baseRulesSha256,
      confirmedBibleSha256,
      candidateSha256,
      candidate: { negativePrompt, colorScript, continuitySummary },
      warnings,
      qualityAdvisories: rawQualityAdvisories === null ? null : {
        negativePromptMaxChars,
        colorScriptMaxChars,
        continuitySummaryMaxChars,
      },
    },
    error: '',
  }
}

export const parsePromptNormalizationReviewPayload = (
  stages: StageRun[],
  limits: ProductionReviewLimits | null | undefined,
): ReviewParseResult<PromptNormalizationReviewPayload> => {
  const resolvedLimits = reviewLimits(limits)
  if (!resolvedLimits) return failed('服务端生产限制缺失或无效，不能核验提示词标准化审核快照。')
  const stage = sourceStage(stages, 'script_normalize')
  if (!stage) return failed('缺少已成功的 script_normalize 阶段，不能核验英文候选。')
  if (!hash(stage.outputSnapshotSha256)) return failed('script_normalize 输出快照 hash 缺失或格式错误。')
  const snapshot = record(stage.outputSnapshot)
  if (!snapshot || snapshot.stage_key !== 'script_normalize') return failed('script_normalize 输出快照缺失或 stage_key 不匹配。')
  const payload = record(snapshot.review_payload)
  if (!payload || payload.kind !== 'prompt_normalization_candidates') return failed('script_normalize review_payload kind 缺失或不受支持。')
  const attemptId = uuid(payload.attempt_id)
  const resultSha256 = hash(payload.result_sha256)
  const provider = text(payload.provider)
  const model = text(payload.model)
  const submissionSemantics = text(payload.submission_semantics)
  const rawReviewOrigin = text(payload.review_origin)
  if (!resultSha256 || provider === null || model === null || submissionSemantics === null) return failed('提示词标准化审核快照的 attempt、hash 或模型信息无效。')
  if (!Array.isArray(payload.candidates) || payload.candidates.length > resolvedLimits.promptReviewMaxCandidates) return failed('提示词标准化审核快照候选数量无效。')
  const noSubmission = submissionSemantics === 'no_submission'
  const reviewOrigin = rawReviewOrigin || (noSubmission ? 'no_submission' : 'model_candidate')
  const humanOnlyNoSubmission = submissionSemantics === 'human_only_no_submission'
  const humanOnlyOrigin = ['human_only_recipe', 'human_only_external_disabled'].includes(reviewOrigin)
  if (!['model_candidate', 'server_deterministic_candidate', 'hybrid_candidate', 'no_submission', 'human_after_validation_retry_exhausted', 'human_only_recipe', 'human_only_external_disabled'].includes(reviewOrigin)) return failed('提示词标准化审核来源无效。')
  if (humanOnlyOrigin !== humanOnlyNoSubmission) return failed('纯人工提示词审核的来源与提交语义不匹配。')
  if (noSubmission && (attemptId !== null || payload.candidates.length !== 0)) return failed('no_submission 审核快照不能包含 attempt 或候选。')
  if (humanOnlyNoSubmission && (payload.attempt_id !== null || attemptId !== null || payload.candidates.length === 0 || provider.trim() || model.trim())) return failed('纯人工提示词审核快照不能包含 attempt 或模型信息，且必须包含待定稿字段。')
  if (!noSubmission && !humanOnlyNoSubmission && (!attemptId || payload.candidates.length === 0 || !provider.trim() || !model.trim() || !submissionSemantics.trim())) return failed('已提交的提示词规范化审核快照缺少 attempt、候选或模型信息。')
  let retryPolicy: PromptNormalizationReviewPayload['retryPolicy'] = null
  let stopReason: PromptNormalizationReviewPayload['stopReason'] = null
  const validationFailures: PromptNormalizationReviewPayload['validationFailures'] = []
  if (reviewOrigin === 'human_after_validation_retry_exhausted') {
    const policy = record(payload.retry_policy)
    const parsedStopReason = text(payload.stop_reason)
    const configKey = text(policy?.config_key)
    const retryLimit = policy?.retry_limit
    const maxAttempts = policy?.max_attempts
    const attemptsUsed = policy?.attempts_used
    if (
      !policy
      || configKey !== 'production.prompt_field_retry_limit'
      || !Number.isInteger(retryLimit)
      || !Number.isInteger(maxAttempts)
      || !Number.isInteger(attemptsUsed)
      || Number(retryLimit) < 0
      || Number(maxAttempts) !== Number(retryLimit) + 1
      || Number(attemptsUsed) < 1
      || Number(attemptsUsed) > Number(maxAttempts)
      || !['retry_limit_reached', 'safe_feedback_unavailable'].includes(parsedStopReason || '')
      || (
        parsedStopReason === 'retry_limit_reached'
        && Number(attemptsUsed) !== Number(maxAttempts)
      )
      || (
        parsedStopReason === 'safe_feedback_unavailable'
        && Number(attemptsUsed) >= Number(maxAttempts)
      )
    ) return failed('提示词标准化人工接管的冻结重试策略无效。')
    stopReason = parsedStopReason as PromptNormalizationReviewPayload['stopReason']
    retryPolicy = {
      configKey,
      retryLimit: Number(retryLimit),
      maxAttempts: Number(maxAttempts),
      attemptsUsed: Number(attemptsUsed),
    }
    if (!Array.isArray(payload.validation_failures) || !payload.validation_failures.length) return failed('提示词标准化人工接管缺少校验失败审计。')
    for (const value of payload.validation_failures) {
      const failure = record(value)
      const allowedFailureKeys = new Set([
        'attempt_id', 'attempt_number', 'route_index', 'unit_key', 'batch_index',
        'error_code', 'error_message', 'resolution_id', 'resolution_evidence_sha256',
        'contract_error_code', 'reason_code', 'validation_context', 'finish_reason',
        'usage', 'response_id_sha256',
      ])
      if (!failure || Object.keys(failure).some((key) => !allowedFailureKeys.has(key))) {
        return failed('提示词标准化人工接管的失败审计包含未知字段。')
      }
      const failureAttemptId = uuid(failure?.attempt_id)
      const attemptNumber = failure?.attempt_number
      const routeIndex = failure?.route_index
      const unitKey = text(failure?.unit_key)
      const batchIndex = failure?.batch_index
      const errorCode = text(failure?.error_code)
      const errorMessage = text(failure?.error_message)
      const resolutionId = uuid(failure?.resolution_id)
      const resolutionEvidenceSha256 = hash(failure?.resolution_evidence_sha256)
      const contractErrorCode = text(failure?.contract_error_code)
      const expectedErrorMessage = errorCode === 'provider_response_invalid'
        ? 'Provider 返回的响应未通过严格合同校验。'
        : errorCode === 'prompt_normalize_contract_failed'
          ? '提示词规范化结果未通过严格结构或语义校验。'
          : ''
      const rawReasonCode = failure.reason_code
      const reasonCode = rawReasonCode === undefined ? null
        : typeof rawReasonCode === 'string' && SAFE_PROMPT_VALIDATION_REASON_CODES.has(rawReasonCode as PromptNormalizationValidationReasonCode)
          ? rawReasonCode as PromptNormalizationValidationReasonCode
          : undefined
      const rawValidationContext = failure.validation_context
      const validationContext = rawValidationContext === undefined ? null
        : reasonCode ? parsePromptNormalizationValidationContext(rawValidationContext, reasonCode, resolvedLimits) : null
      const finishReason = failure.finish_reason
      const usage = failure.usage
      const responseIdSha256 = failure.response_id_sha256
      const usageValid = usage === undefined || Boolean(
        record(usage)
        && Object.keys(usage as JsonObject).length > 0
        && Object.keys(usage as JsonObject).every((key) => SAFE_PROMPT_USAGE_FIELDS.has(key))
        && Object.values(usage as JsonObject).every((candidate) => (
          Number.isInteger(candidate) && Number(candidate) >= 0 && Number(candidate) <= resolvedLimits.promptReviewMaxUsageCount
        )),
      )
      if (
        !failureAttemptId
        || !Number.isInteger(attemptNumber)
        || Number(attemptNumber) < 1
        || !(routeIndex === null || (Number.isInteger(routeIndex) && Number(routeIndex) >= 0))
        || !unitKey || !SAFE_PROMPT_UNIT_KEY.test(unitKey)
        || !Number.isInteger(batchIndex) || Number(batchIndex) < 0 || Number(batchIndex) >= resolvedLimits.promptReviewMaxCandidates
        || !['prompt_normalize_contract_failed', 'provider_response_invalid'].includes(errorCode || '')
        || errorMessage !== expectedErrorMessage
        || !resolutionId
        || !resolutionEvidenceSha256
        || !contractErrorCode || !SAFE_PROMPT_CONTRACT_ERROR_CODES.has(contractErrorCode)
        || reasonCode === undefined
        || (rawValidationContext !== undefined && !validationContext)
        || (rawValidationContext !== undefined && reasonCode !== 'length_out_of_range')
        || (finishReason !== undefined && (typeof finishReason !== 'string' || !SAFE_PROMPT_FINISH_REASONS.has(finishReason)))
        || !usageValid
        || (responseIdSha256 !== undefined && !hash(responseIdSha256))
      ) return failed('提示词标准化人工接管的失败审计无效。')
      validationFailures.push({
        attemptId: failureAttemptId,
        attemptNumber: Number(attemptNumber),
        routeIndex: routeIndex === null ? null : Number(routeIndex),
        unitKey,
        batchIndex: Number(batchIndex),
        errorCode: errorCode!,
        errorMessage,
        resolutionId,
        resolutionEvidenceSha256,
        contractErrorCode: contractErrorCode!,
        reasonCode,
        validationContext,
      })
    }
    if (validationFailures.length !== Number(attemptsUsed)) return failed('提示词标准化人工接管的失败次数与冻结重试策略不一致。')
    if (validationFailures[validationFailures.length - 1]?.attemptId !== attemptId) return failed('提示词标准化人工接管的最新失败 attempt 不匹配。')
  }
  const advisories: PromptNormalizationAdvisory[] = []
  const rawAdvisories = payload.advisories === undefined ? [] : payload.advisories
  if (!Array.isArray(rawAdvisories) || rawAdvisories.length > resolvedLimits.promptReviewMaxAdvisories) return failed('提示词标准化建议值审计结构无效。')
  for (const value of rawAdvisories) {
    const advisory = record(value)
    const code = text(advisory?.code)
    const batchIndex = advisory?.batch_index
    const measured = advisory?.measured
    if (!advisory || advisory.schema_version !== 1 || advisory.severity !== 'warning'
      || !Number.isInteger(batchIndex) || Number(batchIndex) < 0 || Number(batchIndex) >= resolvedLimits.promptReviewMaxCandidates
      || !Number.isInteger(measured) || Number(measured) < 0) return failed('提示词标准化建议值审计无效。')
    if (code === 'recommended_input_bytes_exceeded') {
      const allowed = new Set(['schema_version', 'code', 'severity', 'measured', 'recommended_maximum', 'hard_maximum', 'batch_index'])
      const recommendedMaximum = advisory.recommended_maximum
      const hardMaximum = advisory.hard_maximum
      if (Object.keys(advisory).some((key) => !allowed.has(key))
        || !Number.isInteger(recommendedMaximum) || Number(recommendedMaximum) < 1
        || !Number.isInteger(hardMaximum) || Number(hardMaximum) < Number(recommendedMaximum)
        || Number(hardMaximum) > resolvedLimits.promptReviewMaxInputBytes
        || Number(measured) <= Number(recommendedMaximum)
        || Number(measured) > Number(hardMaximum)) return failed('输入字节建议值审计无效。')
      advisories.push({
        code,
        batchIndex: Number(batchIndex),
        measured: Number(measured),
        recommendedMaximum: Number(recommendedMaximum),
        hardMaximum: Number(hardMaximum),
      })
    } else if (code === 'recommended_output_tokens_exceeded') {
      const allowed = new Set(['schema_version', 'code', 'severity', 'usage_field', 'measured', 'recommended_maximum', 'hard_maximum', 'batch_index'])
      const usageField = advisory.usage_field
      const recommendedMaximum = advisory.recommended_maximum
      const hardMaximum = advisory.hard_maximum
      if (Object.keys(advisory).some((key) => !allowed.has(key))
        || !['completion_tokens', 'output_tokens'].includes(String(usageField || ''))
        || !Number.isInteger(recommendedMaximum) || Number(recommendedMaximum) < 1
        || !Number.isInteger(hardMaximum) || Number(hardMaximum) < Number(recommendedMaximum)
        || Number(measured) <= Number(recommendedMaximum)) return failed('输出令牌建议值审计无效。')
      advisories.push({
        code,
        batchIndex: Number(batchIndex),
        usageField: usageField as 'completion_tokens' | 'output_tokens',
        measured: Number(measured),
        recommendedMaximum: Number(recommendedMaximum),
        hardMaximum: Number(hardMaximum),
      })
    } else if (code === 'recommended_length_out_of_range') {
      const allowed = new Set(['schema_version', 'code', 'severity', 'request_id', 'field_key', 'measured', 'minimum', 'maximum', 'length_unit', 'request_index', 'batch_index'])
      const requestId = text(advisory.request_id)
      const fieldKey = text(advisory.field_key)
      const minimum = advisory.minimum
      const maximum = advisory.maximum
      const lengthUnit = advisory.length_unit
      const requestIndex = advisory.request_index
      const bound = (candidate: unknown) => candidate === null || (Number.isInteger(candidate) && Number(candidate) >= 1 && Number(candidate) <= resolvedLimits.promptReviewMaxFieldCharacters)
      if (Object.keys(advisory).some((key) => !allowed.has(key))
        || !requestId || !fieldKey || !bound(minimum) || !bound(maximum)
        || !['characters', 'english_words', 'items'].includes(String(lengthUnit || ''))
        || !Number.isInteger(requestIndex) || Number(requestIndex) < 0 || Number(requestIndex) >= resolvedLimits.promptReviewMaxCandidates
        || !((minimum !== null && Number(measured) < Number(minimum)) || (maximum !== null && Number(measured) > Number(maximum)))) return failed('字段长度建议值审计无效。')
      advisories.push({
        code,
        batchIndex: Number(batchIndex),
        requestId,
        fieldKey,
        measured: Number(measured),
        minimum: minimum === null ? null : Number(minimum),
        maximum: maximum === null ? null : Number(maximum),
        lengthUnit: lengthUnit as 'characters' | 'english_words' | 'items',
        requestIndex: Number(requestIndex),
      })
    } else return failed('提示词标准化建议值审计包含未知类型。')
  }
  const candidates: PromptNormalizationCandidate[] = []
  const seen = new Set<string>()
  for (const value of payload.candidates) {
    const row = record(value)
    const requestId = text(row?.request_id)
    const scopeType = text(row?.scope_type)
    const scopeId = uuid(row?.scope_id)
    const fieldKey = text(row?.field_key)
    const label = text(row?.label)
    const sourceText = text(row?.source_text)
    const sourceHash = hash(row?.source_hash)
    const contextHash = hash(row?.context_hash)
    const translatedText = text(row?.translated_text)
    if (!row || !requestId?.trim() || requestId.length > 191 || seen.has(requestId) || !scopeType?.trim() || !scopeId || !fieldKey?.trim() || label === null || sourceText === null || !sourceHash || !contextHash || translatedText === null) return failed('提示词标准化候选的标识、字段或 hash 无效。')
    seen.add(requestId)
    candidates.push({ requestId, scopeType, scopeId, fieldKey, label, sourceText, sourceHash, contextHash, translatedText })
  }
  return {
    value: {
      snapshotSha256: stage.outputSnapshotSha256,
      attemptId,
      resultSha256,
      provider,
      model,
      submissionSemantics,
      reviewOrigin: reviewOrigin as PromptNormalizationReviewPayload['reviewOrigin'],
      stopReason,
      retryPolicy,
      validationFailures,
      advisories,
      candidates,
    },
    error: '',
  }
}
