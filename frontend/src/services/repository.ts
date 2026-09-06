import { api, fetchAllPages, unwrap } from './api'
import type { AssemblyResult, Asset, AssetGenerationExecutionKind, AssetGenerationJob, AssetGenerationJobType, AssetGenerationMediaType, AssetGenerationResolutionOutcome, AssetGenerationRouteOption, BibleDraftEntityType, BibleImageCandidatesResult, CanonicalGlossaryEnsureResult, CanonicalGlossaryScope, DeferredGenerationRequest, DeferredGenerationStatus, Episode, GeneratedWorkflowExport, PromptTranslationEntry, PromptTranslationField, ReleaseBundle, ReleaseSelection, SeriesProject, Shot, ShotTranslationPromotionResult, StylePreset, TaskInputPrompt, TranslationCapability, Workflow } from '@/types'
import { adaptAssets, adaptCreatedEpisode, adaptProject, adaptProjectSummary, adaptStyles, adaptTasks, backendStyleId, isBackendId, persistBibleEntities, projectPayload, styleSlug, syncShot } from './backendAdapters'
import { adaptCanonicalGlossaryEnsureResult, adaptShotTranslationPromotionResult, adaptTranslationEntry } from './translationReview'
import { adaptPromptNormalizationCapability } from './promptNormalization'
import { adaptBibleImageCandidates } from './bibleImageCandidates'
import { adaptDeferredGenerationRequest } from './deferredGeneration'
import { adaptAuthenticatedUser, type AuthenticatedUser } from './authTypes'
import {
  buildAssetGenerationJobRequestPayload,
  type AssetGenerationJobCreateInput,
} from './assetGenerationPayload'
import {
  adaptConditioningModes,
  conditioningModeByKey,
  type ConditioningModeCapability,
} from './conditioningCapabilities'
import { adaptWorkflowSmokeFixtureSchema } from './platformAdapters'

const record = (value: unknown): Record<string, any> => value && typeof value === 'object' ? value as Record<string, any> : {}
const adaptAssetGenerationJob = (value: unknown): AssetGenerationJob => {
  const raw = record(value)
  const outputAsset = raw.output_asset ? adaptAssets([raw.output_asset])[0] || null : null
  return {
    id: String(raw.id || ''), jobType: String(raw.job_type || 'asset') as AssetGenerationJobType,
    mediaType: String(raw.media_type || 'image') as AssetGenerationMediaType,
    imageCategory: String(raw.image_category || '') as AssetGenerationJob['imageCategory'], name: String(raw.name || ''),
    description: String(raw.description || ''), prompt: String(raw.prompt || ''), negativePrompt: String(raw.negative_prompt || ''),
    parameters: record(raw.parameters), styleId: outputAsset?.styleId || styleSlug(String(raw.style || '')),
    executionKind: String(raw.execution_kind || 'deployment') as AssetGenerationExecutionKind,
    modelDeploymentId: String(raw.model_deployment || ''), modelDeploymentName: String(raw.model_deployment_name || ''),
    workflowVersionId: String(raw.workflow_template_version || ''), workflowName: String(raw.workflow_name || ''),
    workflowVersion: raw.workflow_version === null || raw.workflow_version === undefined ? null : Number(raw.workflow_version),
    outputAsset, status: String(raw.status || 'queued') as AssetGenerationJob['status'], operation: String(raw.operation || ''),
    isExternal: raw.is_external === true, allowExternalCalls: raw.allow_external_calls === true,
    budgetLimit: raw.budget_limit === null || raw.budget_limit === undefined ? null : String(raw.budget_limit),
    estimatedCost: raw.estimated_cost === null || raw.estimated_cost === undefined ? null : String(raw.estimated_cost),
    actualCost: raw.actual_cost === null || raw.actual_cost === undefined ? null : String(raw.actual_cost),
    costCurrency: String(raw.cost_currency || ''), resultText: String(raw.result_text || ''), remoteStatus: String(raw.remote_status || ''),
    providerRequestIdSha256: String(raw.provider_request_id_sha256 || ''),
    pollCount: Number(raw.poll_count || 0), errorCode: String(raw.error_code || ''), errorMessage: String(raw.error_message || ''),
    ignoreLateResult: raw.ignore_late_result === true,
    submissionStartedAt: String(raw.submission_started_at || ''), cancelRequestedAt: String(raw.cancel_requested_at || ''), cancelSubmittedAt: String(raw.cancel_submitted_at || ''),
    createdAt: String(raw.created_at || ''), updatedAt: String(raw.updated_at || ''), submittedAt: String(raw.submitted_at || ''), finishedAt: String(raw.finished_at || ''),
  }
}

const adaptAssetGenerationOption = (value: unknown): AssetGenerationRouteOption => {
  const raw = record(value)
  const targetKind = String(raw.target_kind || '')
  if (targetKind !== 'deployment' && targetKind !== 'comfy_workflow') {
    throw new Error(`服务端返回了不支持的素材生成方案类型：${targetKind || '空值'}`)
  }
  const common = {
    targetId: String(raw.target_id || ''), key: String(raw.key || ''), name: String(raw.name || ''),
    modelDeploymentId: String(raw.model_deployment_id || ''), modelDeploymentName: String(raw.model_deployment_name || ''),
    modelIdentifier: String(raw.model_identifier || ''), operation: String(raw.operation || ''),
    isExternal: raw.is_external === true, smokeEvidenceId: String(raw.smoke_evidence_id || ''),
    requestSchema: raw.request_schema && typeof raw.request_schema === 'object' ? raw.request_schema : null,
    parameterDefaults: record(raw.parameter_defaults),
    workflowInputSchema: raw.workflow_input_schema
      ? adaptWorkflowSmokeFixtureSchema(raw.workflow_input_schema) : null,
    acceptsNegativePrompt: raw.accepts_negative_prompt === true,
  }
  if (targetKind === 'comfy_workflow') {
    const workflowVersion = Number(raw.workflow_version)
    if (!raw.workflow_version_id || !raw.workflow_name || !Number.isInteger(workflowVersion) || workflowVersion < 1) {
      throw new Error('服务端返回的 ComfyUI 工作流方案缺少版本信息')
    }
    return {
      ...common, targetKind, smokeExpiresAt: null,
      workflowVersionId: String(raw.workflow_version_id), workflowName: String(raw.workflow_name), workflowVersion,
    }
  }
  return {
    ...common, targetKind, smokeExpiresAt: String(raw.smoke_expires_at || ''),
    workflowVersionId: '', workflowName: '', workflowVersion: null,
  }
}

const translationRows = (payload: unknown): PromptTranslationEntry[] => {
  const body = unwrap<unknown>(payload)
  const rows = Array.isArray(body)
    ? body
    : body && typeof body === 'object'
      ? ((body as Record<string, unknown>).entries || (body as Record<string, unknown>).translations || [])
      : []
  return Array.isArray(rows)
    ? rows.filter((row): row is Record<string, unknown> => Boolean(row && typeof row === 'object')).map(adaptTranslationEntry)
    : []
}

const fileDataUrl = (file: File): Promise<string> => new Promise((resolve, reject) => {
  const reader = new FileReader()
  reader.onload = () => resolve(String(reader.result || ''))
  reader.onerror = () => reject(new Error('无法读取封面文件，请重新选择'))
  reader.readAsDataURL(file)
})

// Enqueue performs tenant/path validation and ffprobe snapshots before the
// durable job is returned. Give long episodes enough time without tying the
// browser to the hours-long FFmpeg execution itself.
// Server-side managed publication may spend up to 100s in FFmpeg. Keep this
// below Gunicorn's 120s ceiling, then reconcile by GET because a proxy/client
// timeout does not prove that the content-addressed publication failed.

export interface ProductionCapability {
  conditioningModes: ConditioningModeCapability[]
  conditioningContractSha256: string
  defaultConditioningMode: string
  recommendedMaxPending: number
  recommendedMaxActivePerUser: number
  maxConcurrent: number
  configuredConcurrency: number
  deploymentMaxConcurrent: number
  taskTimeoutSeconds: number
  configuredTaskTimeoutSeconds: number
  videoShotMinDurationMs: number
  videoShotMaxDurationMs: number
  scriptBreakdownDefaultShotDurationMs: number
  scriptBreakdownDefaultFirstFrameContinuity: boolean
  scriptBreakdownDefaultLastFrameContinuity: boolean
  scriptBreakdownAutoSplitTriggerChars: number
  scriptBreakdownAutoSplitTargetChars: number
  activeTaskCount: number
  userActiveTaskCount: number
  queueGuidanceEnforcement: 'advisory'
  queueAdvisories: QueueCapacityAdvisory[]
  businessVolumeLimited: boolean
  configurationPublicSha256: string
  configurationSources: Record<string, string>
  effectiveLimits: ProductionEffectiveLimits
  creationDefaults: ProductionCreationDefaults
}

export interface ProductionEffectiveLimits {
  maxVariants: number
  maxRoutingRoutes: number
  maxEpisodes: number
  maxShotsPerEpisode: number
  maxTotalShots: number
  maxGenerationTasks: number
  maxReferenceBindings: number
  maxBibleCandidatesPerEntity: number
  maxBibleReferenceAssetsPerEntity: number
  maxBibleRegenerationEntitiesPerRequest: number
  dialogueMaxEntries: number
  dialogueSpeakerIdMaxCharacters: number
  dialogueTextMaxCharacters: number
  dialogueDeliveryMaxCharacters: number
  executionMonitoringMaxNodeTypeOverrides: number
  executionMonitoringMaxNodeOverrides: number
  executionMonitoringMaxIdleTimeoutSeconds: number
  promptReviewMaxCandidates: number
  promptReviewMaxAdvisories: number
  promptReviewMaxInputBytes: number
  promptReviewMaxFieldCharacters: number
  promptReviewMaxUsageCount: number
  auditNoteMinCharacters: number
  auditNoteMaxCharacters: number
}

export interface ProductionCreationDefaults {
  defaultLanguage: string
  defaultAspectRatio: string
  aspectRatioOptions: ProductionAspectRatioOption[]
  defaultShotMediaType: 'video' | 'image'
  defaultIncludeAudio: boolean
  episodeCount: number
  shotsPerEpisode: number
  episodeDurationSeconds: number
  maxGenerationTasks: number
  fps: number
  width: number
  height: number
}

export interface ProductionAspectRatioOption {
  value: string
  label: string
  width: number
  height: number
}

export interface QueueCapacityAdvisory {
  schemaVersion: 1
  code: 'recommended_max_pending_exceeded' | 'recommended_max_active_per_user_exceeded'
  severity: 'warning'
  scope: 'platform' | 'user'
  measured: number
  recommendedMaximum: number
}

export interface DeferredGenerationRequestFilters {
  project?: string
  sourceTask?: string
  targetShot?: string
  status?: DeferredGenerationStatus
  frameSlot?: 'first_frame' | 'last_frame'
}

export const productionErrorCode = (error: unknown): string => {
  const candidate = error as { response?: { data?: { code?: unknown; error?: { code?: unknown } } } }
  const code = String(candidate?.response?.data?.error?.code ?? candidate?.response?.data?.code ?? '').trim().toLowerCase()
  return /^[a-z][a-z0-9_]{0,79}$/.test(code) ? code : ''
}

const adaptProductionCapability = (raw: Record<string, unknown>): ProductionCapability => {
  const positiveInteger = (key: string): number => {
    const value = Number(raw[key])
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`生产策略缺少有效字段 ${key}`)
    return value
  }
  const positiveNumber = (key: string): number => {
    const value = Number(raw[key])
    if (!Number.isFinite(value) || value <= 0) throw new Error(`生产策略缺少有效字段 ${key}`)
    return value
  }
  const nonNegativeInteger = (key: string): number => {
    const value = Number(raw[key])
    if (!Number.isSafeInteger(value) || value < 0) throw new Error(`生产策略缺少有效字段 ${key}`)
    return value
  }
  const boolean = (key: string): boolean => {
    if (typeof raw[key] !== 'boolean') throw new Error(`生产策略缺少有效字段 ${key}`)
    return raw[key] as boolean
  }
  if (raw.queue_guidance_enforcement !== 'advisory') {
    throw new Error('生产策略缺少队列建议值告警口径')
  }
  if (!Array.isArray(raw.queue_advisories)) {
    throw new Error('生产策略缺少队列建议值告警')
  }
  const queueAdvisories = raw.queue_advisories.map((value): QueueCapacityAdvisory => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new Error('生产策略的队列建议值告警无效')
    }
    const advisory = value as Record<string, unknown>
    const code = String(advisory.code || '')
    const scope = String(advisory.scope || '')
    const measured = Number(advisory.measured)
    const recommendedMaximum = Number(advisory.recommended_maximum)
    if (
      advisory.schema_version !== 1
      || !['recommended_max_pending_exceeded', 'recommended_max_active_per_user_exceeded'].includes(code)
      || advisory.severity !== 'warning'
      || !['platform', 'user'].includes(scope)
      || !Number.isSafeInteger(measured) || measured < 0
      || !Number.isSafeInteger(recommendedMaximum) || recommendedMaximum < 1
      || measured <= recommendedMaximum
    ) throw new Error('生产策略的队列建议值告警无效')
    return {
      schemaVersion: 1,
      code: code as QueueCapacityAdvisory['code'],
      severity: 'warning',
      scope: scope as QueueCapacityAdvisory['scope'],
      measured,
      recommendedMaximum,
    }
  })
  const videoShotMinDurationMs = positiveInteger('video_shot_min_duration_ms')
  const videoShotMaxDurationMs = positiveInteger('video_shot_max_duration_ms')
  if (videoShotMinDurationMs > videoShotMaxDurationMs) throw new Error('生产策略返回的视频镜头时长范围无效')
  const scriptBreakdownDefaultShotDurationMs = positiveInteger('script_breakdown_default_shot_duration_ms')
  if (
    scriptBreakdownDefaultShotDurationMs < videoShotMinDurationMs
    || scriptBreakdownDefaultShotDurationMs > videoShotMaxDurationMs
  ) throw new Error('生产策略返回的母稿默认镜头时长无效')
  const scriptBreakdownAutoSplitTriggerChars = positiveInteger('script_breakdown_auto_split_trigger_chars')
  const scriptBreakdownAutoSplitTargetChars = positiveInteger('script_breakdown_auto_split_target_chars')
  if (scriptBreakdownAutoSplitTargetChars > scriptBreakdownAutoSplitTriggerChars) {
    throw new Error('生产策略返回的母稿自动分段阈值无效')
  }
  const configuredConcurrency = positiveInteger('configured_concurrency')
  const deploymentMaxConcurrent = positiveInteger('deployment_max_concurrent')
  const maxConcurrent = positiveInteger('max_concurrent')
  if (maxConcurrent !== Math.min(configuredConcurrency, deploymentMaxConcurrent)) {
    throw new Error('生产策略返回的有效并发与配置/部署上限不一致')
  }
  const configuredTaskTimeoutSeconds = positiveNumber('configured_task_timeout_seconds')
  const taskTimeoutSeconds = positiveNumber('task_timeout_seconds')
  if (taskTimeoutSeconds !== configuredTaskTimeoutSeconds) {
    throw new Error('生产策略返回的有效任务超时与配置值不一致')
  }
  const configurationPublicSha256 = String(raw.configuration_public_sha256 || '')
  if (!/^[0-9a-f]{64}$/.test(configurationPublicSha256)) throw new Error('生产策略缺少公开配置指纹')
  const rawSources = raw.configuration_sources
  if (!rawSources || typeof rawSources !== 'object' || Array.isArray(rawSources)) {
    throw new Error('生产策略缺少配置来源')
  }
  const configurationSources = Object.fromEntries(
    Object.entries(rawSources as Record<string, unknown>).map(([key, value]) => [key, String(value || '')]),
  )
  const rawEffectiveLimits = raw.effective_limits
  if (!rawEffectiveLimits || typeof rawEffectiveLimits !== 'object' || Array.isArray(rawEffectiveLimits)) {
    throw new Error('生产策略缺少有效安全上限投影')
  }
  const effectiveLimit = (key: string): number => {
    const value = Number((rawEffectiveLimits as Record<string, unknown>)[key])
    if (!Number.isSafeInteger(value) || value < 1) throw new Error(`生产策略缺少有效安全上限 ${key}`)
    return value
  }
  const effectiveLimits: ProductionEffectiveLimits = {
    maxVariants: effectiveLimit('max_variants'),
    maxRoutingRoutes: effectiveLimit('max_routing_routes'),
    maxEpisodes: effectiveLimit('max_episodes'),
    maxShotsPerEpisode: effectiveLimit('max_shots_per_episode'),
    maxTotalShots: effectiveLimit('max_total_shots'),
    maxGenerationTasks: effectiveLimit('max_generation_tasks'),
    maxReferenceBindings: effectiveLimit('max_reference_bindings'),
    maxBibleCandidatesPerEntity: effectiveLimit('max_bible_candidates_per_entity'),
    maxBibleReferenceAssetsPerEntity: effectiveLimit('max_bible_reference_assets_per_entity'),
    maxBibleRegenerationEntitiesPerRequest: effectiveLimit('max_bible_regeneration_entities_per_request'),
    dialogueMaxEntries: effectiveLimit('dialogue_max_entries'),
    dialogueSpeakerIdMaxCharacters: effectiveLimit('dialogue_speaker_id_max_characters'),
    dialogueTextMaxCharacters: effectiveLimit('dialogue_text_max_characters'),
    dialogueDeliveryMaxCharacters: effectiveLimit('dialogue_delivery_max_characters'),
    executionMonitoringMaxNodeTypeOverrides: effectiveLimit('execution_monitoring_max_node_type_overrides'),
    executionMonitoringMaxNodeOverrides: effectiveLimit('execution_monitoring_max_node_overrides'),
    executionMonitoringMaxIdleTimeoutSeconds: effectiveLimit('execution_monitoring_max_idle_timeout_seconds'),
    promptReviewMaxCandidates: effectiveLimit('prompt_review_max_candidates'),
    promptReviewMaxAdvisories: effectiveLimit('prompt_review_max_advisories'),
    promptReviewMaxInputBytes: effectiveLimit('prompt_review_max_input_bytes'),
    promptReviewMaxFieldCharacters: effectiveLimit('prompt_review_max_field_characters'),
    promptReviewMaxUsageCount: effectiveLimit('prompt_review_max_usage_count'),
    auditNoteMinCharacters: effectiveLimit('audit_note_min_characters'),
    auditNoteMaxCharacters: effectiveLimit('audit_note_max_characters'),
  }
  if (effectiveLimits.auditNoteMinCharacters > effectiveLimits.auditNoteMaxCharacters) {
    throw new Error('生产策略返回的审计说明范围无效')
  }
  const rawCreationDefaults = raw.creation_defaults
  if (!rawCreationDefaults || typeof rawCreationDefaults !== 'object' || Array.isArray(rawCreationDefaults)) {
    throw new Error('生产策略缺少项目创建默认值投影')
  }
  const creationDefault = (key: string): number => {
    const value = Number((rawCreationDefaults as Record<string, unknown>)[key])
    if (!Number.isSafeInteger(value) || value < 1) throw new Error(`生产策略缺少有效创建默认值 ${key}`)
    return value
  }
  const creationText = (key: string): string => {
    const value = rawCreationDefaults as Record<string, unknown>
    if (typeof value[key] !== 'string' || !value[key].trim()) {
      throw new Error(`生产策略缺少有效创建默认值 ${key}`)
    }
    return value[key].trim()
  }
  const rawAspectRatioOptions = (rawCreationDefaults as Record<string, unknown>).aspect_ratio_options
  if (!Array.isArray(rawAspectRatioOptions) || !rawAspectRatioOptions.length) {
    throw new Error('生产策略缺少有效画幅选项')
  }
  const aspectRatioOptions = rawAspectRatioOptions.map((item): ProductionAspectRatioOption => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) throw new Error('生产策略画幅选项无效')
    const option = item as Record<string, unknown>
    const value = typeof option.value === 'string' ? option.value.trim() : ''
    const label = typeof option.label === 'string' ? option.label.trim() : ''
    const width = Number(option.width)
    const height = Number(option.height)
    if (!value || !label || !Number.isSafeInteger(width) || width < 1 || !Number.isSafeInteger(height) || height < 1) {
      throw new Error('生产策略画幅选项无效')
    }
    return { value, label, width, height }
  })
  if (new Set(aspectRatioOptions.map((option) => option.value)).size !== aspectRatioOptions.length) {
    throw new Error('生产策略画幅选项存在重复值')
  }
  const defaultAspectRatio = creationText('default_aspect_ratio')
  const defaultAspectRatioOption = aspectRatioOptions.find((option) => option.value === defaultAspectRatio)
  if (!defaultAspectRatioOption) throw new Error('生产策略默认画幅不在已声明选项中')
  const rawDefaultShotMediaType = creationText('default_shot_media_type')
  if (rawDefaultShotMediaType !== 'video' && rawDefaultShotMediaType !== 'image') {
    throw new Error('生产策略默认分镜媒体类型无效')
  }
  const rawDefaultIncludeAudio = (rawCreationDefaults as Record<string, unknown>).default_include_audio
  if (typeof rawDefaultIncludeAudio !== 'boolean') throw new Error('生产策略默认分镜音频开关无效')
  if (rawDefaultShotMediaType === 'image' && rawDefaultIncludeAudio) {
    throw new Error('图片分镜不能声明默认音频')
  }
  const creationDefaults: ProductionCreationDefaults = {
    defaultLanguage: creationText('default_language'),
    defaultAspectRatio,
    aspectRatioOptions,
    defaultShotMediaType: rawDefaultShotMediaType,
    defaultIncludeAudio: rawDefaultIncludeAudio,
    episodeCount: creationDefault('episode_count'),
    shotsPerEpisode: creationDefault('shots_per_episode'),
    episodeDurationSeconds: creationDefault('episode_duration_seconds'),
    maxGenerationTasks: creationDefault('max_generation_tasks'),
    fps: creationDefault('fps'),
    width: creationDefault('width'),
    height: creationDefault('height'),
  }
  if (creationDefaults.width !== defaultAspectRatioOption.width || creationDefaults.height !== defaultAspectRatioOption.height) {
    throw new Error('生产策略默认画幅尺寸与创建默认尺寸不一致')
  }
  const conditioningModes = adaptConditioningModes(raw.conditioning_modes)
  if (!conditioningModes.length) throw new Error('生产策略未声明任何条件输入模式')
  const conditioningContractSha256 = String(raw.conditioning_contract_sha256 || '')
  if (!/^[0-9a-f]{64}$/.test(conditioningContractSha256)) throw new Error('生产策略缺少条件输入合同指纹')
  const defaultConditioningMode = String(raw.default_conditioning_mode || '').trim()
  if (defaultConditioningMode && !conditioningModeByKey(conditioningModes, defaultConditioningMode)) {
    throw new Error('生产策略的默认条件输入模式不在已声明合同中')
  }
  return {
    conditioningModes,
    conditioningContractSha256,
    defaultConditioningMode,
    recommendedMaxPending: positiveInteger('recommended_max_pending'),
    recommendedMaxActivePerUser: positiveInteger('recommended_max_active_per_user'),
    maxConcurrent,
    configuredConcurrency,
    deploymentMaxConcurrent,
    taskTimeoutSeconds,
    configuredTaskTimeoutSeconds,
    videoShotMinDurationMs,
    videoShotMaxDurationMs,
    scriptBreakdownDefaultShotDurationMs,
    scriptBreakdownDefaultFirstFrameContinuity: boolean('script_breakdown_default_first_frame_continuity'),
    scriptBreakdownDefaultLastFrameContinuity: boolean('script_breakdown_default_last_frame_continuity'),
    scriptBreakdownAutoSplitTriggerChars,
    scriptBreakdownAutoSplitTargetChars,
    activeTaskCount: nonNegativeInteger('active_task_count'),
    userActiveTaskCount: nonNegativeInteger('user_active_task_count'),
    queueGuidanceEnforcement: 'advisory',
    queueAdvisories,
    businessVolumeLimited: boolean('business_volume_limited'),
    configurationPublicSha256,
    configurationSources,
    effectiveLimits,
    creationDefaults,
  }
}

const relationId = (value: unknown): string => {
  if (value && typeof value === 'object') return String((value as Record<string, unknown>).id || '')
  return value === null || value === undefined ? '' : String(value)
}

const adaptAssembly = (raw: Record<string, any>): AssemblyResult => {
  const status: AssemblyResult['status'] = ['planned', 'queued', 'running', 'succeeded', 'failed'].includes(raw.status)
    ? raw.status
    : 'planned'
  const scope: AssemblyResult['scope'] = raw.scope === 'series' ? 'series' : 'episode'
  const episodeId = relationId(raw.episode) || undefined
  return {
    id: String(raw.id),
    status,
    scope,
    projectId: relationId(raw.project),
    episodeId,
    outputUrl: String(raw.output_url || '') || undefined,
    downloadUrl: String(raw.download_url || '') || undefined,
    aiGenerated: raw.ai_generated !== false,
    provenanceUrl: String(raw.provenance_url || '') || undefined,
    sourceTaskIds: Array.isArray(raw.source_tasks) ? raw.source_tasks.map(relationId).filter(Boolean) : [],
    createdAt: String(raw.created_at || '') || undefined,
    queuedAt: String(raw.queued_at || '') || undefined,
    startedAt: String(raw.started_at || '') || undefined,
    finishedAt: String(raw.finished_at || '') || undefined,
    attempts: Number.isFinite(Number(raw.attempts)) ? Number(raw.attempts) : undefined,
    maxAttempts: Number.isFinite(Number(raw.max_attempts)) ? Number(raw.max_attempts) : undefined,
    errorMessage: String(raw.error_message || '') || undefined,
    message: status === 'succeeded'
      ? `${scope === 'episode' ? '本集' : '整剧'}成片已组装完成`
      : status === 'failed'
        ? (String(raw.error_message || '') || '成片组装失败')
        : status === 'running'
          ? '成片正在后台组装'
          : status === 'queued'
            ? '成片组装任务已进入队列'
            : '成片组装任务已创建'
  }
}

const adaptReleaseSelection = (raw: any): ReleaseSelection => ({
  shots: Array.isArray(raw?.shots) ? raw.shots.map((row: any) => ({
    shotId: String(row.shot_id || ''),
    taskId: String(row.task_id || ''),
    outputIndex: Number(row.output_index || 0),
  })).filter((row: { shotId: string; taskId: string }) => row.shotId && row.taskId) : [],
  episodes: Array.isArray(raw?.episodes) ? raw.episodes.map((row: any) => ({
    episodeId: String(row.episode_id || ''),
    assemblyId: String(row.assembly_id || ''),
  })).filter((row: { episodeId: string; assemblyId: string }) => row.episodeId && row.assemblyId) : [],
  seriesAssemblyId: String(raw?.series_assembly_id || '') || undefined,
})

const adaptReleaseBundle = (raw: Record<string, any>): ReleaseBundle => ({
  id: String(raw.id),
  projectId: relationId(raw.project),
  version: Number(raw.version || 0),
  title: String(raw.title || ''),
  notes: String(raw.notes || ''),
  status: ['draft', 'sealed', 'released', 'superseded'].includes(raw.status)
    ? raw.status : 'draft',
  selection: adaptReleaseSelection(raw.selection),
  manifestSha256: String(raw.manifest_sha256 || '') || undefined,
  verificationStatus: ['draft', 'verified', 'invalid'].includes(raw.verification_status)
    ? raw.verification_status : 'invalid',
  sealedAt: String(raw.sealed_at || '') || undefined,
  releasedAt: String(raw.released_at || '') || undefined,
  createdAt: String(raw.created_at || '') || undefined,
  updatedAt: String(raw.updated_at || '') || undefined,
})

const currentUserForNewSession = async (
  tokens: { access: string; refresh: string },
  fallbackName: string,
): Promise<AuthenticatedUser> => {
  localStorage.setItem('cineforge_token', tokens.access)
  localStorage.setItem('cineforge_refresh_token', tokens.refresh)
  try {
    const raw = unwrap<Record<string, any>>((await api.get('/auth/me/')).data)
    return adaptAuthenticatedUser(raw, fallbackName)
  } catch (error) {
    if (localStorage.getItem('cineforge_token') === tokens.access) {
      localStorage.removeItem('cineforge_token')
      localStorage.removeItem('cineforge_refresh_token')
      localStorage.removeItem('cineforge_user')
    }
    throw error
  }
}

export const repository = {
  async getCurrentUser(): Promise<AuthenticatedUser> {
    return (async () => {
      const raw = unwrap<Record<string, any>>((await api.get('/auth/me/')).data)
      return adaptAuthenticatedUser(raw)
    })()
  },
  async getProductionCapability(projectId?: string): Promise<ProductionCapability> {
    return (async () => {
      const raw = unwrap<Record<string, unknown>>(
        (await api.get('/capabilities/production/', {
          params: projectId ? { project_id: projectId } : undefined,
        })).data,
      )
      return adaptProductionCapability(raw)
    })()
  },
  async getTranslationCapability(projectId: string): Promise<TranslationCapability> {
    return (async () => {
      return adaptPromptNormalizationCapability(unwrap<unknown>((await api.get(
        `/projects/${projectId}/capabilities/prompt-normalization/`,
      )).data))
    })()
  },
  async ensureCanonicalGlossary(
    projectId: string,
    options: {
      entries?: Array<{ scopeType: CanonicalGlossaryScope; scopeId: string }>
      includeAllMissing?: boolean
      ensureVisualIdentityTranslation?: boolean
    } = {},
  ): Promise<CanonicalGlossaryEnsureResult> {
    return (async () => {
      const response = await api.post(`/projects/${projectId}/ensure-canonical-glossary/`, {
        entries: options.entries?.map((entry) => ({ scope_type: entry.scopeType, scope_id: entry.scopeId })),
        include_all_missing: options.includeAllMissing === true,
        ensure_visual_identity_translation: options.ensureVisualIdentityTranslation === true,
      })
      return adaptCanonicalGlossaryEnsureResult(response.data)
    })()
  },
  async getProjectTranslations(
    projectId: string,
    filters: { episodeId?: string; shotId?: string; status?: string } = {},
  ): Promise<PromptTranslationEntry[]> {
    return (async () => {
      const response = await api.get(`/projects/${projectId}/translations/`, { params: {
        episode: filters.episodeId || undefined,
        shot: filters.shotId || undefined,
        status: filters.status || undefined,
      } })
      return translationRows(response.data)
    })()
  },
  async translateProjectFields(
    projectId: string,
    fields: PromptTranslationField[],
    options: {
      modelDeploymentId: string
    },
  ): Promise<PromptTranslationEntry[]> {
    if (!fields.length) return []
    return (async () => {
      const response = await api.post(`/projects/${projectId}/translations/translate/`, {
        source_lang: 'Chinese', target_lang: 'English',
        model_deployment_id: options.modelDeploymentId,
        entries: fields.map((field) => ({
          field_key: field.fieldKey,
          source_text: field.sourceText,
          scope_type: field.scopeType,
          scope_id: field.scopeId,
        })),
      })
      return translationRows(response.data)
    })()
  },
  async createManualProjectTranslation(
    projectId: string,
    field: PromptTranslationField,
  ): Promise<PromptTranslationEntry> {
    const response = await api.post(`/projects/${projectId}/translations/manual/`, {
      entries: [{
        field_key: field.fieldKey,
        source_text: field.sourceText,
        scope_type: field.scopeType,
        scope_id: field.scopeId,
      }],
    })
    const rows = translationRows(response.data)
    if (rows.length !== 1) throw new Error('手工英文候选创建结果无效')
    return rows[0]
  },
  async retranslateProjectField(
    entryId: string,
    sourceText: string | undefined,
    options: {
      modelDeploymentId: string
    },
  ): Promise<PromptTranslationEntry> {
    return (async () => adaptTranslationEntry(unwrap<Record<string, unknown>>((await api.post(
      `/translations/${entryId}/retranslate/`, {
        ...(sourceText === undefined ? {} : { source_text: sourceText }),
        model_deployment_id: options.modelDeploymentId,
      },
    )).data)))()
  },
  async confirmProjectTranslation(entryId: string, translatedText: string, expectedRevision: number): Promise<PromptTranslationEntry> {
    return (async () => adaptTranslationEntry(unwrap<Record<string, unknown>>((await api.post(
      `/translations/${entryId}/confirm/`, { expected_revision: expectedRevision, translation: translatedText },
    )).data)))()
  },
  async promoteShotTranslations(
    shotId: string,
    entries: Array<{ entryId: string; expectedRevision: number }>,
  ): Promise<ShotTranslationPromotionResult> {
    return (async () => adaptShotTranslationPromotionResult(unwrap<Record<string, unknown>>((await api.post(
      '/translations/promote-shot/', {
        shot_id: shotId,
        entries: entries.map((entry) => ({ entry_id: entry.entryId, expected_revision: entry.expectedRevision })),
      },
    )).data)))()
  },
  async revokeProjectTranslation(entryId: string): Promise<PromptTranslationEntry> {
    return (async () => adaptTranslationEntry(unwrap<Record<string, unknown>>((await api.post(
      `/translations/${entryId}/revoke/`, {},
    )).data)))()
  },
  async login(account: string, password: string) {
    return (async () => {
        const tokens = unwrap<{ access: string; refresh: string }>((await api.post('/auth/token/', { username: account, password })).data)
        const user = await currentUserForNewSession(tokens, account)
        return { access: tokens.access, refresh: tokens.refresh, user }
      })()
  },
  async register(account: string, email: string, password: string) {
    return (async () => {
        await api.post('/auth/register/', { username: account, email, password })
        const tokens = unwrap<{ access: string; refresh: string }>((await api.post('/auth/token/', { username: account, password })).data)
        const user = await currentUserForNewSession(tokens, account)
        return { access: tokens.access, refresh: tokens.refresh, user }
      })()
  },
  async getRegistrationCapability(): Promise<{ publicRegistrationEnabled: boolean }> {
    return (async () => {
      const raw = unwrap<Record<string, unknown>>((await api.get('/auth/register/')).data)
      return { publicRegistrationEnabled: raw.public_registration_enabled === true }
    })()
  },
  async getStyles(): Promise<StylePreset[]> {
    return (async () => adaptStyles(await fetchAllPages('/styles/', { is_active: true, page_size: 500 })))()
  },
  async getAssets(params?: { style?: string; type?: string; search?: string }): Promise<Asset[]> {
    return (async () => {
      // UI categories are derived from both native kinds and generic
      // reference.metadata.reference_category. Filtering by backend `kind`
      // would silently hide classified generic references (and `image` is not
      // a backend kind), so adapt first and filter the complete result here.
      const rows=adaptAssets(await fetchAllPages('/assets/', { style: backendStyleId(params?.style), search: params?.search, page_size: 500 }))
        .filter((asset)=>asset.metadata?.usage !== 'project_cover')
      return params?.type?rows.filter((asset)=>asset.type===params.type):rows
    })()
  },
  async getAsset(id: string): Promise<Asset> {
    const asset = adaptAssets([unwrap<unknown>((await api.get(`/assets/${id}/`)).data)])[0]
    if (!asset) throw new Error('服务端未返回可用素材')
    return asset
  },
  async createAsset(payload: {
    file: File
    mediaType: AssetGenerationMediaType
    imageCategory?: BibleDraftEntityType | 'reference'
    name: string
    description: string
    promptHint: string
    styleId?: string
  }): Promise<Asset> {
    return (async () => {
      const form = new FormData()
      form.append('file', payload.file, payload.file.name)
      const kind = payload.mediaType === 'video' ? 'reference_video'
        : payload.mediaType === 'audio' ? 'reference_audio'
          : payload.mediaType === 'script' ? 'script'
            : payload.imageCategory || 'reference'
      form.append('kind', kind)
      form.append('name', payload.name.trim())
      form.append('description', payload.description.trim())
      form.append('prompt_hint', payload.promptHint.trim())
      const style = backendStyleId(payload.styleId)
      if (style) form.append('style', style)
      const raw = unwrap<Record<string, unknown>>((await api.post('/assets/', form, { headers: { 'Content-Type': 'multipart/form-data' } })).data)
      const asset = adaptAssets([raw])[0]
      if (!asset) throw new Error('上传成功，但服务端未返回可用素材')
      return asset
    })()
  },
  async getAssetGenerationOptions(jobType: AssetGenerationJobType, mediaType: AssetGenerationMediaType): Promise<AssetGenerationRouteOption[]> {
    const raw = record(unwrap<unknown>((await api.get('/asset-generation-jobs/options/', { params: { job_type: jobType, media_type: mediaType } })).data))
    return Array.isArray(raw.routes) ? raw.routes.map(adaptAssetGenerationOption) : []
  },
  async getAssetGenerationJobs(): Promise<AssetGenerationJob[]> {
    return (await fetchAllPages('/asset-generation-jobs/', { page_size: 100 })).map(adaptAssetGenerationJob)
  },
  async createAssetGenerationJob(payload: AssetGenerationJobCreateInput): Promise<AssetGenerationJob> {
    const raw = unwrap<unknown>((await api.post(
      '/asset-generation-jobs/',
      buildAssetGenerationJobRequestPayload(payload, backendStyleId(payload.styleId) || null),
      { headers: { 'Idempotency-Key': payload.idempotencyKey } },
    )).data)
    return adaptAssetGenerationJob(raw)
  },
  async cancelAssetGenerationJob(id: string): Promise<AssetGenerationJob> {
    return adaptAssetGenerationJob(unwrap<unknown>((await api.post(`/asset-generation-jobs/${id}/cancel/`, {})).data))
  },
  async retryAssetGenerationJob(id: string): Promise<AssetGenerationJob> {
    return adaptAssetGenerationJob(unwrap<unknown>((await api.post(
      `/asset-generation-jobs/${id}/retry/`,
      {},
      { headers: { 'Idempotency-Key': `asset-generation-retry:${id}` } },
    )).data))
  },
  async resolveUncertainAssetGenerationJob(id: string, payload: {
    outcome: AssetGenerationResolutionOutcome
    note: string
    providerRequestId?: string
    actualCost?: string | null
    costCurrency?: string
    idempotencyKey: string
  }): Promise<void> {
    await api.post(`/asset-generation-jobs/${id}/resolve-uncertain/`, {
      outcome: payload.outcome,
      note: payload.note,
      provider_request_id: payload.providerRequestId || '',
      actual_cost: payload.actualCost === undefined || payload.actualCost === '' ? null : payload.actualCost,
      cost_currency: payload.costCurrency || '',
    }, { headers: { 'Idempotency-Key': payload.idempotencyKey } })
  },
  async setProjectCover(project: SeriesProject, file: File | null): Promise<SeriesProject> {
    return (async () => {
      if (!file) {
        const raw = unwrap<Record<string, unknown>>((await api.patch(`/projects/${project.id}/`, { cover_asset: null })).data)
        return {
          ...project,
          coverAssetId: relationId(raw.cover_asset) || undefined,
          coverUrl: String(raw.cover_url || '') || undefined,
          updatedAt: String(raw.updated_at || project.updatedAt),
        }
      }

      const form = new FormData()
      const assetName = Array.from(`${project.title.trim() || '连续剧'} · 封面`).slice(0, 160).join('')
      form.append('file', file, file.name)
      form.append('kind', 'reference')
      form.append('name', assetName)
      form.append('metadata', JSON.stringify({ usage: 'project_cover', project_id: project.id }))
      const rawAsset = unwrap<Record<string, unknown>>((await api.post('/assets/', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })).data)
      const uploadedAssetId = relationId(rawAsset.id)
      if (!uploadedAssetId) throw new Error('封面上传成功，但服务端未返回素材 ID')

      try {
        const raw = unwrap<Record<string, unknown>>((await api.patch(`/projects/${project.id}/`, {
          cover_asset: uploadedAssetId,
        })).data)
        return {
          ...project,
          coverAssetId: relationId(raw.cover_asset) || uploadedAssetId,
          coverUrl: String(raw.cover_url || '') || undefined,
          updatedAt: String(raw.updated_at || project.updatedAt),
        }
      } catch (bindingError) {
        try {
          await api.delete(`/assets/${uploadedAssetId}/`)
        } catch {
          throw new Error('封面绑定失败，且已上传的临时封面未能自动清理，请稍后在素材管理中检查。')
        }
        throw bindingError
      }
    })()
  },
  async updateAssetMetadata(assetId: string, payload: { name: string; description: string; promptHint: string; kind?: BibleDraftEntityType | 'reference'; styleId?: string }): Promise<Asset> {
    return (async () => {
      const raw = unwrap<Record<string, unknown>>((await api.patch(`/assets/${assetId}/`, {
        name: payload.name.trim(), description: payload.description.trim(), prompt_hint: payload.promptHint.trim(),
        ...(payload.kind ? { kind: payload.kind } : {}), style: backendStyleId(payload.styleId) || null,
      })).data)
      const asset = adaptAssets([raw])[0]
      if (!asset) throw new Error('保存成功，但服务端未返回可用的素材信息')
      return asset
    })()
  },
  async deleteAsset(assetId: string): Promise<void> {
    return (async () => {
      await api.delete(`/assets/${assetId}/`)
    })()
  },
  async uploadReferenceAsset(file: File, kind: 'reference_video'|'reference_audio', styleId?: string):Promise<Asset> {
    return this.createAsset({
      file, mediaType: kind === 'reference_video' ? 'video' : 'audio',
      name: file.name.replace(/\.[^.]+$/, ''), description: '', promptHint: '', styleId,
    })
  },
  async getProjects(): Promise<SeriesProject[]> {
    return (async () => {
      const raw = await fetchAllPages<Record<string, unknown>>('/projects/', { page_size: 200 })
      return raw.map((project) => adaptProjectSummary(project))
    })()
  },
  async getProject(id: string): Promise<SeriesProject> {
    return (async () => adaptProject(unwrap<Record<string, unknown>>((await api.get(`/projects/${id}/`)).data)))()
  },
  async getBibleImageCandidates(projectId: string): Promise<BibleImageCandidatesResult> {
    return (async () => {
      const response = await api.get(`/projects/${projectId}/bible-image-candidates/`)
      const result = adaptBibleImageCandidates(unwrap<unknown>(response.data))
      if (result.projectId !== projectId) throw new Error('服务端返回的实体图片候选项目不匹配。')
      return result
    })()
  },
  async createProject(payload: Partial<SeriesProject>): Promise<SeriesProject> {
    return (async () => adaptProjectSummary(unwrap<Record<string, unknown>>((await api.post('/projects/', projectPayload(payload))).data), true))()
  },
  async updateProject(project: SeriesProject): Promise<SeriesProject> {
    return (async () => {
      const patched = unwrap<Record<string, unknown>>((await api.patch(`/projects/${project.id}/`, projectPayload(project))).data)
      const bibleProject = { ...project, updatedAt: String(patched.updated_at || project.updatedAt) }
      await persistBibleEntities(bibleProject)
      return adaptProject(unwrap<Record<string, unknown>>(
        (await api.get(`/projects/${project.id}/`)).data,
      ))
    })()
  },
  async setProjectFavorite(project: SeriesProject, favorite: boolean): Promise<SeriesProject> {
    return (async () => {
      const raw = unwrap<Record<string, unknown>>((await api.patch(`/projects/${project.id}/`, { is_favorite: favorite })).data)
      return { ...project, isFavorite: raw.is_favorite === true, updatedAt: String(raw.updated_at || project.updatedAt) }
    })()
  },
  async deleteProject(projectId: string): Promise<void> {
    return (async () => { await api.delete(`/projects/${projectId}/`) })()
  },
  async addEpisode(projectId: string, payload: Pick<Episode, 'title' | 'synopsis'> & { script?: string }): Promise<Episode> {
    return (async () => adaptCreatedEpisode(unwrap<Record<string, unknown>>((await api.post(`/projects/${projectId}/next-episode/`, payload)).data)))()
  },
  async deleteEpisode(projectId: string, episodeId: string): Promise<void> {
    return (async () => { await api.delete(`/episodes/${episodeId}/`) })()
  },
  async saveEpisode(project: SeriesProject, episode: Episode): Promise<Episode> {
    return (async () => {
      const patched = unwrap<Record<string, any>>((await api.patch(`/episodes/${episode.id}/`, {
        project: project.id,
        number: episode.number,
        title: episode.title,
        synopsis: episode.synopsis,
        script: episode.script || '',
        status: episode.status === 'rendering' ? 'generating' : episode.status,
        prompt_settings: { include_audio: episode.audioEnabled },
      })).data)
      const shots = []
      for (const shot of episode.shots) shots.push(await syncShot(project, episode, shot))
      return {
        ...episode,
        ...adaptCreatedEpisode(patched),
        shots,
        shotVersions: episode.shotVersions,
        activeShotVersionKey: episode.activeShotVersionKey,
      }
    })()
  },
  async saveShot(project: SeriesProject, episode: Episode, shot: Shot): Promise<Shot> {
    return (async () => syncShot(project, episode, shot))()
  },
  async getWorkflows(): Promise<Workflow[]> {
    return (async () => adaptTasks(await fetchAllPages('/tasks/', { page_size: 500 })))()
  },
  async getShotWorkflows(shotId: string, kind: 'video' | 'image' = 'video'): Promise<Workflow[]> {
    return (async () => {
      if (!isBackendId(shotId)) return []
      return adaptTasks(await fetchAllPages('/tasks/', {
        shot_id: shotId,
        kind,
        ordering: '-queued_at',
        page_size: 100
      }))
    })()
  },
  async getWorkflow(taskId: string): Promise<Workflow> {
    return (async () => {
      const raw = unwrap<Record<string, any>>((await api.get(`/tasks/${taskId}/`)).data)
      const workflow = adaptTasks([raw])[0]
      if (!workflow) throw new Error('服务端未返回任务详情')
      return workflow
    })()
  },
  async getDeferredGenerationRequests(filters: DeferredGenerationRequestFilters = {}): Promise<DeferredGenerationRequest[]> {
    return (async () => {
      const params: Record<string, unknown> = { page_size: 100 }
      if (filters.project) params.project = filters.project
      if (filters.sourceTask) params.source_task = filters.sourceTask
      if (filters.targetShot) params.target_shot = filters.targetShot
      if (filters.status) params.status = filters.status
      if (filters.frameSlot) params.frame_slot = filters.frameSlot
      return (await fetchAllPages<unknown>('/deferred-generation-requests/', params))
        .map(adaptDeferredGenerationRequest)
    })()
  },
  async getTaskInputPrompt(taskId: string): Promise<TaskInputPrompt> {
    return (async () => {
      const raw = unwrap<Record<string, any>>((await api.get(`/tasks/${taskId}/input-prompt/`)).data)
      const prompt = String(raw.prompt || '')
      const negativePrompt = String(raw.negative_prompt || '')
      return {
        taskId: String(raw.task_id || taskId),
        promptArtifactId: String(raw.prompt_artifact_id || '') || undefined,
        promptType: String(raw.prompt_type || '') || undefined,
        model: String(raw.model || '') || undefined,
        conditioningMode: String(raw.conditioning_mode || '') || undefined,
        effectiveConditioningMode: String(raw.effective_conditioning_mode || '') || undefined,
        promptTemplateKey: String(raw.prompt_template_key || '') || undefined,
        contentHash: String(raw.content_hash || '') || undefined,
        prompt,
        negativePrompt,
        promptCharacterCount: Number(raw.prompt_character_count ?? prompt.length),
        negativePromptCharacterCount: Number(raw.negative_prompt_character_count ?? negativePrompt.length),
        negativePromptSubmitted: raw.negative_prompt_submitted === true,
        promptSource: String(raw.prompt_source || '') || undefined,
        negativePromptSource: String(raw.negative_prompt_source || '') || undefined
      }
    })()
  },
  async getGeneratedWorkflowExport(task: Workflow, workflowFormat: 'api' | 'ui' = 'ui'): Promise<GeneratedWorkflowExport> {
    return (async () => {
      if (!task.generatedWorkflowId) throw new Error('该任务未关联可查看的 generated_workflow')
      let raw: Record<string, any>
      try {
        raw = unwrap<Record<string, any>>((await api.get(
          `/tasks/${task.taskId || task.id}/comfyui-workflow/`,
          { params: { workflow_format: workflowFormat } }
        )).data)
      } catch (error: any) {
        if (error?.response?.status !== 404) throw error
        const workflow = unwrap<Record<string, any>>((await api.get(`/workflows/${task.generatedWorkflowId}/`)).data)
        raw = { task_id: task.taskId || task.id, generated_workflow_id: workflow.id, workflow: workflow.workflow_json, injected_values: workflow.injected_values, content_hash: workflow.content_hash, filename: `comfyui-workflow-${workflow.id}.json`, format: 'comfyui_api', safe_to_open: false, direct_open_supported: false, open_mode: 'download_import' }
      }
      const workflowJson = raw.workflow || raw.workflow_json
      if (!workflowJson || typeof workflowJson !== 'object' || Array.isArray(workflowJson)) throw new Error('服务端未返回有效的 ComfyUI 工作流 JSON')
      const safeToOpen = raw.safe_to_open === true
      return {
        taskId: String(raw.task_id || task.taskId || task.id),
        generatedWorkflowId: String(raw.generated_workflow_id || raw.id || task.generatedWorkflowId),
        workflowJson,
        injectedValues: raw.injected_values && typeof raw.injected_values === 'object' ? raw.injected_values : {},
        contentHash: String(raw.content_hash || ''),
        filename: String(raw.filename || `comfyui-workflow-${task.generatedWorkflowId}.json`),
        format: String(raw.format || 'comfyui_api'),
        templateSlug: String(raw.template_slug || '') || undefined,
        templateVersion: Number(raw.template_version || 0) || undefined,
        importReady: raw.import_ready !== false,
        comfyPromptId: task.comfyPromptId,
        comfyuiUrl: String(raw.comfyui_url || '') || undefined,
        directOpenUrl: String(raw.direct_open_url || '') || undefined,
        safeToOpen,
        directOpenSupported: raw.direct_open_supported === true,
        openMode: String(raw.open_mode || 'download_import'),
        downloadUrl: String(raw.download_url || '') || undefined,
        availableFormats: Array.isArray(raw.available_formats)
          ? raw.available_formats.filter((value: unknown) => value === 'comfyui_api' || value === 'comfyui_ui')
          : undefined,
        apiDownloadUrl: String(raw.api_download_url || '') || undefined,
        uiDownloadUrl: String(raw.ui_download_url || '') || undefined,
        containsStagedPlaceholders: raw.contains_staged_placeholders === true,
        stagedPlaceholderCount: Number(raw.staged_placeholder_count || 0),
        openReason: String(raw.open_reason || '') || (safeToOpen
          ? (raw.direct_open_supported === true ? '后端已确认可通过安全地址打开该工作流。' : '请先下载 UI 画布 JSON，再打开 ComfyUI 并拖入或打开该文件；网页不会自动载入画布。')
          : '当前没有可安全打开的 ComfyUI 地址，请先下载工作流 JSON。')
      }
    })()
  },
  async getEpisodeAssemblies(projectId: string): Promise<AssemblyResult[]> {
    return (async () => {
      if (!isBackendId(projectId)) return []
      const rows = await fetchAllPages<Record<string, any>>('/assemblies/', {
        project: projectId,
        scope: 'episode',
        dry_run: false,
        ordering: '-created_at',
        page_size: 500
      })
      return rows.map(adaptAssembly)
    })()
  },
  async getAssembly(assemblyId: string): Promise<AssemblyResult> {
    return (async () => {
      const raw = unwrap<Record<string, any>>((await api.get(`/assemblies/${assemblyId}/`)).data)
      return adaptAssembly(raw)
    })()
  },
  async getReleaseBundles(projectId: string): Promise<ReleaseBundle[]> {
    return (async () => {
      if (!isBackendId(projectId)) return []
      const rows = await fetchAllPages<Record<string, any>>('/release-bundles/', {
        project: projectId,
        ordering: '-version',
        page_size: 100,
      })
      return rows.map(adaptReleaseBundle)
    })()
  },
  async verifyReleaseBundle(bundleId: string): Promise<{ status: string; signatureVerified: boolean; liveVerified: boolean }> {
    return (async () => {
      const raw = unwrap<Record<string, any>>((await api.get(`/release-bundles/${bundleId}/verify/`)).data)
      return {
        status: String(raw.verification?.status || 'invalid'),
        signatureVerified: raw.verification?.signature_verified === true,
        liveVerified: raw.verification?.live_verified === true,
      }
    })()
  },
  async testComfy(host: string): Promise<{ ok: boolean; message: string; latency?: number }> {
    const started = performance.now()
    try {
      const result = unwrap<Record<string, any>>((await api.get('/comfyui/health/')).data)
      const reachable = Boolean(result.reachable)
      const degraded = Boolean(result.degraded)
      const message = degraded
        ? `ComfyUI API 可达，但健康检查异常：${String(result.error || 'system_stats 返回异常')}`
        : reachable
          ? 'Django 服务端已连通其受控 ComfyUI 目标'
          : String(result.error || 'ComfyUI 当前不可达')
      return { ok: reachable && !degraded, message, latency: Math.round(performance.now() - started) }
    } catch (error: any) {
      throw new Error(error?.message || 'ComfyUI 当前不可达')
    }
  }
}
