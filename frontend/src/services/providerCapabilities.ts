import type { PromptProviderCapability, PromptProviderId } from '@/types'

type Raw = Record<string, unknown>

const objectValue = (value: unknown): Raw => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as Raw : {}
)

const booleanValue = (raw: Raw, snake: string, camel: string, fallback: boolean): boolean => {
  const value = raw[snake] ?? raw[camel]
  return typeof value === 'boolean' ? value : fallback
}

const optionalBooleanValue = (raw: Raw, snake: string, camel: string): boolean | undefined => {
  const value = raw[snake] ?? raw[camel]
  return typeof value === 'boolean' ? value : undefined
}

const positiveIntegerValue = (raw: Raw, snake: string, camel: string): number | undefined => {
  const value = Number(raw[snake] ?? raw[camel])
  return Number.isSafeInteger(value) && value > 0 ? value : undefined
}

const nonNegativeIntegerValue = (raw: Raw, snake: string, camel: string): number | undefined => {
  const value = Number(raw[snake] ?? raw[camel])
  return Number.isSafeInteger(value) && value >= 0 ? value : undefined
}

const safeCodeValue = (value: unknown): string | undefined => {
  const code = String(value ?? '').trim().toLowerCase()
  return /^[a-z][a-z0-9_]{0,79}$/.test(code) ? code : undefined
}

const positiveNumberValue = (raw: Raw, snake: string, camel: string): number | undefined => {
  const value = Number(raw[snake] ?? raw[camel])
  return Number.isFinite(value) && value > 0 ? value : undefined
}

export const promptProviderId = (value: unknown, fallback: PromptProviderId = 'local_qwen'): PromptProviderId => {
  const candidate = String(value || '').trim().toLowerCase()
  if (candidate === 'deepseek_api' || candidate === 'deepseek' || candidate === 'deepseek-api') return 'deepseek_api'
  if (candidate === 'local_qwen' || candidate === 'qwen-local' || candidate === 'local-qwen') return 'local_qwen'
  return fallback
}

export const adaptPromptProviderCapability = (
  value: unknown,
  fallbackId: PromptProviderId,
): PromptProviderCapability => {
  const raw = objectValue(value)
  const id = promptProviderId(raw.id ?? raw.provider, fallbackId)
  const enabled = booleanValue(raw, 'enabled', 'enabled', true)
  const configured = booleanValue(raw, 'configured', 'configured', false)
  const modelDownloaded = booleanValue(raw, 'model_downloaded', 'modelDownloaded', false)
  const modelVerified = booleanValue(raw, 'model_verified', 'modelVerified', false)
  const explicitReady = raw.ready
  const ready = typeof explicitReady === 'boolean'
    ? explicitReady
    : id === 'deepseek_api'
      ? enabled && configured
      : enabled && configured && modelDownloaded && modelVerified
  const transportValue = String(raw.transport || '').trim().toLowerCase()
  const transport = ['remote', 'remote_api'].includes(transportValue) || id === 'deepseek_api'
    ? 'remote'
    : 'local'
  return {
    id,
    label: String(raw.label || (id === 'deepseek_api' ? 'DeepSeek API' : '本地 Qwen')),
    enabled,
    configured,
    ready,
    temporarilyBusy: booleanValue(raw, 'temporarily_busy', 'temporarilyBusy', false),
    model: String(raw.model || ''),
    transport,
    dataEgressNotice: String(raw.data_egress_notice ?? raw.dataEgressNotice ?? '').trim() || undefined,
    notice: String(raw.notice ?? raw.message ?? '').trim() || undefined,
    modelDownloaded,
    modelVerified,
    device: String(raw.device || '').trim() || undefined,
    cudaRequested: optionalBooleanValue(raw, 'cuda_requested', 'cudaRequested'),
    gpuReady: optionalBooleanValue(raw, 'gpu_ready', 'gpuReady'),
    gpuLayers: nonNegativeIntegerValue(raw, 'gpu_layers', 'gpuLayers'),
    runtimeProbeErrorCode: safeCodeValue(raw.runtime_probe_error_code ?? raw.runtimeProbeErrorCode),
    loadedOnDemand: optionalBooleanValue(raw, 'loaded_on_demand', 'loadedOnDemand'),
    unloadedAfterRequest: optionalBooleanValue(raw, 'unloaded_after_request', 'unloadedAfterRequest'),
    reviewRequired: optionalBooleanValue(raw, 'review_required', 'reviewRequired'),
    translationReviewReused: optionalBooleanValue(raw, 'translation_review_reused', 'translationReviewReused'),
    minShots: positiveIntegerValue(raw, 'min_shots', 'minShots'),
    defaultShots: positiveIntegerValue(raw, 'default_shots', 'defaultShots'),
    recommendedMaxShots: positiveIntegerValue(raw, 'recommended_max_shots', 'recommendedMaxShots'),
    batchShots: positiveIntegerValue(raw, 'batch_shots', 'batchShots'),
    maxDialoguePerShot: positiveIntegerValue(raw, 'max_dialogue_per_shot', 'maxDialoguePerShot'),
    maxSourceChars: positiveIntegerValue(raw, 'max_source_chars', 'maxSourceChars'),
    recommendedTitleZhChars: positiveIntegerValue(raw, 'recommended_title_zh_chars', 'recommendedTitleZhChars'),
    recommendedTitleEnChars: positiveIntegerValue(raw, 'recommended_title_en_chars', 'recommendedTitleEnChars'),
    recommendedNarrativeZhChars: positiveIntegerValue(raw, 'recommended_narrative_zh_chars', 'recommendedNarrativeZhChars'),
    recommendedNarrativeEnChars: positiveIntegerValue(raw, 'recommended_narrative_en_chars', 'recommendedNarrativeEnChars'),
    recommendedCameraZhChars: positiveIntegerValue(raw, 'recommended_camera_zh_chars', 'recommendedCameraZhChars'),
    recommendedCameraEnChars: positiveIntegerValue(raw, 'recommended_camera_en_chars', 'recommendedCameraEnChars'),
    recommendedActionZhChars: positiveIntegerValue(raw, 'recommended_action_zh_chars', 'recommendedActionZhChars'),
    recommendedActionEnChars: positiveIntegerValue(raw, 'recommended_action_en_chars', 'recommendedActionEnChars'),
    recommendedContinuityZhChars: positiveIntegerValue(raw, 'recommended_continuity_zh_chars', 'recommendedContinuityZhChars'),
    recommendedContinuityEnChars: positiveIntegerValue(raw, 'recommended_continuity_en_chars', 'recommendedContinuityEnChars'),
    recommendedSoundscapeZhChars: positiveIntegerValue(raw, 'recommended_soundscape_zh_chars', 'recommendedSoundscapeZhChars'),
    recommendedSoundscapeEnChars: positiveIntegerValue(raw, 'recommended_soundscape_en_chars', 'recommendedSoundscapeEnChars'),
    recommendedMusicZhChars: positiveIntegerValue(raw, 'recommended_music_zh_chars', 'recommendedMusicZhChars'),
    recommendedMusicEnChars: positiveIntegerValue(raw, 'recommended_music_en_chars', 'recommendedMusicEnChars'),
    maxCameraMovements: positiveIntegerValue(raw, 'max_camera_movements', 'maxCameraMovements'),
    maxActionSequenceMarkers: positiveIntegerValue(raw, 'max_action_sequence_markers', 'maxActionSequenceMarkers'),
    minScriptActionCoveragePercent: positiveIntegerValue(raw, 'min_script_action_coverage_percent', 'minScriptActionCoveragePercent'),
    minShotDurationMs: positiveIntegerValue(raw, 'min_shot_duration_ms', 'minShotDurationMs'),
    maxShotDurationMs: positiveIntegerValue(raw, 'max_shot_duration_ms', 'maxShotDurationMs'),
    baselineMaxNewTokens: positiveIntegerValue(raw, 'baseline_max_new_tokens', 'baselineMaxNewTokens'),
    maxNewTokens: positiveIntegerValue(raw, 'max_new_tokens', 'maxNewTokens'),
    configuredMaxNewTokens: positiveIntegerValue(raw, 'configured_max_new_tokens', 'configuredMaxNewTokens'),
    contextSize: positiveIntegerValue(raw, 'context_size', 'contextSize'),
    runtimeTimeoutSeconds: positiveNumberValue(raw, 'runtime_timeout_seconds', 'runtimeTimeoutSeconds'),
    processTimeoutSeconds: positiveNumberValue(raw, 'process_timeout_seconds', 'processTimeoutSeconds'),
  }
}

/** Mock-only capability values stay configurable and use the same adapter as the real endpoint. */
export const mockCreativePromptProviderCapability = (): PromptProviderCapability => adaptPromptProviderCapability({
  id: 'local_qwen', label: '本地 Qwen', enabled: true, configured: true, ready: true,
  model: 'Qwen/Qwen3-4B-GGUF', transport: 'local', model_downloaded: true,
  model_verified: true, device: 'cpu', loaded_on_demand: true, unloaded_after_request: true,
  review_required: true, translation_review_reused: true,
  min_shots: import.meta.env.VITE_MOCK_CREATIVE_MIN_SHOTS,
  default_shots: import.meta.env.VITE_MOCK_CREATIVE_DEFAULT_SHOTS,
  recommended_max_shots: import.meta.env.VITE_MOCK_CREATIVE_RECOMMENDED_MAX_SHOTS,
  batch_shots: import.meta.env.VITE_MOCK_CREATIVE_BATCH_SHOTS,
  max_dialogue_per_shot: import.meta.env.VITE_MOCK_CREATIVE_MAX_DIALOGUE_PER_SHOT,
  max_source_chars: import.meta.env.VITE_MOCK_CREATIVE_MAX_SOURCE_CHARS,
  min_shot_duration_ms: import.meta.env.VITE_MOCK_CREATIVE_MIN_SHOT_DURATION_MS,
  max_shot_duration_ms: import.meta.env.VITE_MOCK_CREATIVE_MAX_SHOT_DURATION_MS,
  baseline_max_new_tokens: import.meta.env.VITE_MOCK_CREATIVE_BASELINE_MAX_NEW_TOKENS,
  max_new_tokens: import.meta.env.VITE_MOCK_CREATIVE_MAX_NEW_TOKENS,
  configured_max_new_tokens: import.meta.env.VITE_MOCK_CREATIVE_MAX_NEW_TOKENS,
  context_size: import.meta.env.VITE_MOCK_CREATIVE_CONTEXT_SIZE,
  runtime_timeout_seconds: import.meta.env.VITE_MOCK_CREATIVE_RUNTIME_TIMEOUT_SECONDS,
  process_timeout_seconds: import.meta.env.VITE_MOCK_CREATIVE_PROCESS_TIMEOUT_SECONDS,
}, 'local_qwen')

export const adaptPromptProviders = (
  rawCapability: Raw,
  fallbackId: PromptProviderId = 'local_qwen',
): { defaultProvider: PromptProviderId; providers: PromptProviderCapability[] } => {
  const defaultProvider = promptProviderId(
    rawCapability.default_provider ?? rawCapability.defaultProvider ?? rawCapability.provider,
    fallbackId,
  )
  const rows = Array.isArray(rawCapability.providers) ? rawCapability.providers : [rawCapability]
  const providers = rows.map((row, index) => adaptPromptProviderCapability(
    row,
    index === 0 ? defaultProvider : fallbackId,
  ))
  const deduplicated = [...new Map(providers.map((provider) => [provider.id, provider])).values()]
  return {
    defaultProvider: deduplicated.some((provider) => provider.id === defaultProvider)
      ? defaultProvider
      : deduplicated[0]?.id || fallbackId,
    providers: deduplicated,
  }
}

export const promptProvider = (
  providers: PromptProviderCapability[] | undefined,
  id: PromptProviderId,
): PromptProviderCapability | undefined => providers?.find((provider) => provider.id === id)

export interface CreativeShotCountDecision {
  valid: boolean
  confirmationRequired: boolean
  minimum?: number
  recommendedMaximum?: number
  batchReference?: number
  reason?: string
}

/**
 * Treats provider shot counts as workload guidance, not a business-total gate.
 * Only the provider's recommendation triggers acknowledgement. `batchShots`
 * describes internal orchestration and is never a user-facing confirmation gate.
 */
export const creativeShotCountDecision = (
  count: number,
  capability?: PromptProviderCapability,
): CreativeShotCountDecision => {
  const minimum = capability?.minShots
  const recommendedMaximum = capability?.recommendedMaxShots
  const batchReference = capability?.batchShots
  if (!Number.isSafeInteger(count)) {
    return { valid: false, confirmationRequired: false, minimum, recommendedMaximum, batchReference, reason: '请输入整数镜数。' }
  }
  if (count <= 0) {
    return {
      valid: false,
      confirmationRequired: false,
      minimum,
      recommendedMaximum,
      batchReference,
      reason: '请输入大于 0 的整数镜数。',
    }
  }
  return {
    valid: true,
    confirmationRequired: (
      (minimum !== undefined && count < minimum)
      || (recommendedMaximum !== undefined && count > recommendedMaximum)
    ),
    minimum,
    recommendedMaximum,
    batchReference,
  }
}

export const REMOTE_PROMPT_EGRESS_NOTICE = '远程通道会将本次提示词及生成所需的项目上下文发送给外部 DeepSeek API。'
