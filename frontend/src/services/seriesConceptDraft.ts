import type {
  PromptProviderId,
  PromptProviderCapability,
  SeriesConceptBible,
  SeriesConceptBibleEntity,
  SeriesConceptCandidate,
  SeriesConceptDraftInput,
  SeriesConceptDraftResult,
  SeriesConceptGenerationCapability,
} from '@/types'

type Raw = Record<string, unknown>

export interface SeriesConceptDiagnosticUsage {
  promptTokens?: number
  completionTokens?: number
  totalTokens?: number
}

export interface SeriesConceptDiagnosticOutput {
  characterCount?: number
  utf8ByteCount?: number
  sha256?: string
  redactedJsonShape?: string
  jsonDecodeLine?: number
  jsonDecodeColumn?: number
  jsonDecodePosition?: number
}

export interface SeriesConceptDiagnosticDetails {
  attempts?: number
  elapsedMs?: number
  httpStatus?: number
  incomplete?: boolean
  requestedMaxTokens?: number
  configuredMaxTokens?: number
  configuredTimeoutSeconds?: number
  validationErrorCode?: string
  validationPath?: string
  repairAttempted?: boolean
  repairSucceeded?: boolean
  repairReasonCode?: string
  output?: SeriesConceptDiagnosticOutput
}

/** A deliberately allow-listed, content-free view of a failed generation. */
export interface SeriesConceptDiagnostic {
  diagnosticId: string
  provider: PromptProviderId
  errorCode: string
  failurePhase: string
  finishReason?: string
  usage?: SeriesConceptDiagnosticUsage
  detailsAvailable: boolean
  details: SeriesConceptDiagnosticDetails
}

export interface SeriesConceptDraftFailure {
  message: string
  diagnostic: SeriesConceptDiagnostic | null
}

export const SERIES_CONCEPT_MAX_IDEA_CHARS = 6_000
export const SERIES_CONCEPT_MAX_STYLE_HINT_CHARS = 2_000

export interface SeriesConceptPlanningEstimate {
  totalShots: number
  plannedTotalDurationSeconds: number
  estimatedOutputTokensLower: number
  estimatedOutputTokensUpper: number
  estimatedWaitLevel: '较短' | '中等' | '较长' | '很长'
  localResourcePressureLevel: '低' | '中' | '高' | '很高'
  deepseekCostLevel: '不适用' | '低' | '中' | '较高' | '高'
  estimatedOutputHintUtilizationPercent?: number
}

const objectValue = (value: unknown): Raw => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as Raw : {}
)
const textValue = (value: unknown): string => typeof value === 'string' ? value : ''
const positiveInteger = (value: unknown): number => {
  const number = Number(value)
  return Number.isSafeInteger(number) && number > 0 ? number : 0
}
const providerValue = (value: unknown): PromptProviderId | '' => {
  const normalized = String(value || '').trim().toLowerCase()
  if (normalized === 'local_qwen' || normalized === 'deepseek_api') return normalized
  return ''
}
const invalidResponse = (): never => {
  throw new Error('生成服务未返回可审核且兼容自动拆解的完整候选，请重试。')
}

const SAFE_DIAGNOSTIC_CODE_RE = /^[a-z][a-z0-9_]{0,63}$/
const SAFE_DIAGNOSTIC_ID_RE = /^[0-9a-f]{32}$/
const SAFE_SHA256_RE = /^[0-9a-f]{64}$/
const SAFE_VALIDATION_PATH_RE = /^(?:result\.)?(?:title|logline|bible\.(?:characters|scenes|props)\[\d+\](?:\.(?:name|description|visual_identity_en))?|episodes\[\d+\](?:\.(?:title|shots(?:\[\d+\])?(?:\.(?:setting|action|duration_ms|dialogue(?:\.(?:speaker|delivery|text))?))?))?)$/
const SAFE_REDACTED_SHAPE_TOKEN_RE = /\s+|[{}\[\]:,?]|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null|<redacted_middle>|"(?:title|logline|bible|characters|scenes|props|name|description|visual_identity_en|episodes|shots|setting|action|dialogue|speaker|delivery|text|duration_ms|schema_version)"|"<(?:redacted_text|unterminated_text|unknown_key)>"/y

const safeDiagnosticCode = (value: unknown): string => {
  if (typeof value !== 'string') return ''
  const normalized = value.trim().toLowerCase()
  return SAFE_DIAGNOSTIC_CODE_RE.test(normalized) ? normalized : ''
}

const boundedInteger = (
  value: unknown,
  minimum: number,
  maximum = Number.MAX_SAFE_INTEGER,
): number | undefined => (
  typeof value === 'number'
  && Number.isSafeInteger(value)
  && value >= minimum
  && value <= maximum
    ? value
    : undefined
)

const boundedNumber = (
  value: unknown,
  minimum: number,
  maximum: number,
): number | undefined => (
  typeof value === 'number'
  && Number.isFinite(value)
  && value >= minimum
  && value <= maximum
    ? value
    : undefined
)

/** Mirrors the server's redacted-shape grammar; arbitrary quoted text is rejected. */
const safeRedactedJsonShape = (value: unknown): string => {
  if (typeof value !== 'string' || !value || value.length > 900) return ''
  let position = 0
  while (position < value.length) {
    SAFE_REDACTED_SHAPE_TOKEN_RE.lastIndex = position
    const matched = SAFE_REDACTED_SHAPE_TOKEN_RE.exec(value)
    if (!matched) return ''
    position = SAFE_REDACTED_SHAPE_TOKEN_RE.lastIndex
  }
  return value
}

export const cloneSeriesConceptCandidate = (
  candidate: SeriesConceptCandidate,
): SeriesConceptCandidate => ({
  title: candidate.title,
  logline: candidate.logline,
  masterScript: candidate.masterScript,
  bible: {
    characters: candidate.bible.characters.map((item) => ({ ...item })),
    scenes: candidate.bible.scenes.map((item) => ({ ...item })),
    props: candidate.bible.props.map((item) => ({ ...item })),
  },
})

const adaptSeriesConceptBibleEntity = (value: unknown): SeriesConceptBibleEntity => {
  const raw = objectValue(value)
  const name = textValue(raw.name)
  const description = textValue(raw.description)
  const visualIdentityEn = textValue(raw.visual_identity_en)
  if (
    !name.trim()
    || name.length > 80
    || description.length > 500
    || visualIdentityEn.length > 500
  ) invalidResponse()
  return { name, description, visualIdentityEn }
}

const adaptSeriesConceptBible = (value: unknown): SeriesConceptBible => {
  const raw = objectValue(value)
  const adaptCollection = (collection: unknown): SeriesConceptBibleEntity[] => {
    const rows = Array.isArray(collection) ? collection as unknown[] : null
    if (rows === null) return invalidResponse()
    return rows.map(adaptSeriesConceptBibleEntity)
  }
  return {
    characters: adaptCollection(raw.characters),
    scenes: adaptCollection(raw.scenes),
    props: adaptCollection(raw.props),
  }
}

export const seriesConceptDraftInputErrors = (
  input: SeriesConceptDraftInput,
  limits?: Pick<SeriesConceptGenerationCapability, 'minimumShotDurationMs'>,
): string[] => {
  const errors: string[] = []
  const idea = input.idea.trim()
  const styleHint = input.styleHint.trim()
  if (!idea) errors.push('请先输入故事想法。')
  if (idea.length > SERIES_CONCEPT_MAX_IDEA_CHARS) {
    errors.push(`故事想法不能超过 ${SERIES_CONCEPT_MAX_IDEA_CHARS.toLocaleString()} 字符。`)
  }
  if (styleHint.length > SERIES_CONCEPT_MAX_STYLE_HINT_CHARS) {
    errors.push(`风格提示不能超过 ${SERIES_CONCEPT_MAX_STYLE_HINT_CHARS.toLocaleString()} 字符。`)
  }
  if (!Number.isSafeInteger(input.episodeCount) || input.episodeCount < 1) {
    errors.push('计划集数必须为正整数。')
  }
  if (!Number.isSafeInteger(input.shotsPerEpisode) || input.shotsPerEpisode < 1) {
    errors.push('每集镜头数必须为正整数。')
  }
  if (!Number.isSafeInteger(input.episodeDurationSeconds) || input.episodeDurationSeconds < 1) {
    errors.push('单集时长必须为正整数秒。')
  }
  const totalShots = input.episodeCount * input.shotsPerEpisode
  if (input.episodeCount > 0 && input.shotsPerEpisode > 0 && !Number.isSafeInteger(totalShots)) {
    errors.push('预计镜头总数超出浏览器可安全计算范围。')
  }
  const minimumShotDurationMs = limits?.minimumShotDurationMs
  if (
    Number.isSafeInteger(input.shotsPerEpisode)
    && input.shotsPerEpisode > 0
    && Number.isSafeInteger(input.episodeDurationSeconds)
    && input.episodeDurationSeconds > 0
    && minimumShotDurationMs !== undefined
    && minimumShotDurationMs > 0
    && input.shotsPerEpisode * minimumShotDurationMs > input.episodeDurationSeconds * 1_000
  ) {
    const minimumSeconds = input.shotsPerEpisode * minimumShotDurationMs / 1_000
    errors.push(`按当前最短单镜 ${(minimumShotDurationMs / 1_000).toLocaleString()} 秒计算，每集 ${input.shotsPerEpisode.toLocaleString()} 镜至少需要 ${minimumSeconds.toLocaleString()} 秒；请提高单集时长或减少每集镜头数。`)
  }
  if (!Number.isSafeInteger(input.attempt) || input.attempt < 1 || input.attempt > 100) {
    errors.push('生成尝试序号必须为 1–100 的整数。')
  }
  return errors
}

export const seriesConceptCandidateErrors = (candidate?: SeriesConceptCandidate | null): string[] => {
  if (!candidate) return ['尚无可采用的候选。']
  const errors: string[] = []
  if (!candidate.title.trim()) errors.push('候选项目名不能为空。')
  if (candidate.title.length > 60) errors.push('候选项目名不能超过 60 字符。')
  if (!candidate.logline.trim()) errors.push('候选一句话梗概不能为空。')
  if (candidate.logline.length > 500) errors.push('候选一句话梗概不能超过 500 字符。')
  if (!candidate.masterScript.trim()) errors.push('候选完整剧本不能为空。')
  return errors
}

export const seriesConceptDraftPayload = (input: SeriesConceptDraftInput) => {
  const errors = seriesConceptDraftInputErrors(input)
  if (errors.length) throw new Error(errors[0])
  return {
    idea: input.idea.trim(),
    provider: input.provider,
    episode_count: input.episodeCount,
    shots_per_episode: input.shotsPerEpisode,
    episode_duration_seconds: input.episodeDurationSeconds,
    style_hint: input.styleHint.trim(),
    attempt: input.attempt,
  }
}

/** Includes the selected provider so switching channels marks an older candidate stale. */
export const seriesConceptSourceSnapshot = (input: SeriesConceptDraftInput): string => JSON.stringify({
  idea: input.idea.trim(),
  provider: input.provider,
  episodeCount: input.episodeCount,
  shotsPerEpisode: input.shotsPerEpisode,
  episodeDurationSeconds: input.episodeDurationSeconds,
  styleHint: input.styleHint.trim(),
})

/**
 * Non-blocking browser-side planning only. Token ranges and qualitative levels
 * are deliberately coarse; the server response is the authoritative estimate.
 */
export const seriesConceptPlanningEstimate = (
  input: Pick<SeriesConceptDraftInput, 'provider' | 'episodeCount' | 'shotsPerEpisode' | 'episodeDurationSeconds'>,
  provider?: Pick<PromptProviderCapability, 'configuredMaxNewTokens' | 'maxNewTokens'>,
): SeriesConceptPlanningEstimate => {
  const totalShots = Number.isSafeInteger(input.episodeCount * input.shotsPerEpisode)
    && input.episodeCount > 0 && input.shotsPerEpisode > 0
    ? input.episodeCount * input.shotsPerEpisode
    : 0
  const plannedTotalDurationSeconds = Number.isSafeInteger(input.episodeCount * input.episodeDurationSeconds)
    && input.episodeCount > 0 && input.episodeDurationSeconds > 0
    ? input.episodeCount * input.episodeDurationSeconds
    : 0
  const midpointTokens = Math.max(1_024, 512 + totalShots * 200)
  const estimatedOutputTokensLower = Math.max(800, Math.floor(midpointTokens * 0.75 / 100) * 100)
  const estimatedOutputTokensUpper = Math.max(1_300, Math.ceil(midpointTokens * 1.25 / 100) * 100)
  const configuredBudget = provider?.configuredMaxNewTokens || provider?.maxNewTokens
  const estimatedOutputHintUtilizationPercent = configuredBudget
    ? Math.round(midpointTokens / configuredBudget * 100)
    : undefined
  const workloadLevel = totalShots <= 6 ? 0 : totalShots <= 18 ? 1 : totalShots <= 36 ? 2 : 3
  const estimatedWaitLevel = (['较短', '中等', '较长', '很长'] as const)[workloadLevel]
  const pressureLevel = estimatedOutputHintUtilizationPercent === undefined
    ? workloadLevel
    : estimatedOutputHintUtilizationPercent <= 30 ? 0
      : estimatedOutputHintUtilizationPercent <= 65 ? 1
        : estimatedOutputHintUtilizationPercent <= 100 ? 2 : 3
  const localResourcePressureLevel = input.provider === 'deepseek_api'
    ? '低'
    : (['低', '中', '高', '很高'] as const)[pressureLevel]
  const deepseekCostLevel = input.provider !== 'deepseek_api'
    ? '不适用'
    : estimatedOutputTokensUpper <= 3_000 ? '低'
      : estimatedOutputTokensUpper <= 8_000 ? '中'
        : estimatedOutputTokensUpper <= 16_000 ? '较高' : '高'
  return {
    totalShots,
    plannedTotalDurationSeconds,
    estimatedOutputTokensLower,
    estimatedOutputTokensUpper,
    estimatedWaitLevel,
    localResourcePressureLevel,
    deepseekCostLevel,
    estimatedOutputHintUtilizationPercent,
  }
}

/** The parent applies this complete patch in one assignment after explicit human confirmation. */
export const seriesConceptAcceptedFields = (
  candidate: SeriesConceptCandidate,
): Pick<SeriesConceptCandidate, 'title' | 'logline' | 'masterScript'> => ({
  title: candidate.title,
  logline: candidate.logline,
  masterScript: candidate.masterScript,
})

export const adaptSeriesConceptDraft = (value: unknown): SeriesConceptDraftResult => {
  const raw = objectValue(value)
  const candidateRaw = objectValue(raw.candidate)
  const formatRaw = objectValue(raw.format)
  const resourceRaw = objectValue(raw.resource_estimate)
  const runtimeRaw = objectValue(raw.runtime)

  const sourceFingerprint = textValue(raw.source_fingerprint)
  const title = textValue(candidateRaw.title)
  const logline = textValue(candidateRaw.logline)
  // Deliberately preserve every character; the browser must never silently truncate a script.
  const masterScript = textValue(candidateRaw.master_script)
  const bible = adaptSeriesConceptBible(candidateRaw.bible)
  const schemaVersion = positiveInteger(formatRaw.schema_version)
  const templateId = textValue(formatRaw.template_id)
  const episodeCount = positiveInteger(formatRaw.episode_count)
  const shotCount = positiveInteger(formatRaw.shot_count)
  const episodeDurationSeconds = positiveInteger(formatRaw.episode_duration_seconds)
  const totalDurationMs = positiveInteger(formatRaw.total_duration_ms)
  const shotDurationsMs = Array.isArray(formatRaw.shot_durations_ms)
    ? formatRaw.shot_durations_ms.map((row) => Array.isArray(row) ? row.map(positiveInteger) : [])
    : []
  const flattenedDurations = shotDurationsMs.flat()
  const resourceTotalShots = positiveInteger(resourceRaw.total_shots)
  const estimatedInputChars = positiveInteger(resourceRaw.estimated_input_chars)
  const estimatedOutputTokens = positiveInteger(resourceRaw.estimated_output_tokens)
  const configuredOutputTokenHint = positiveInteger(resourceRaw.configured_output_token_hint)
  const outputHintUtilizationPercent = positiveInteger(resourceRaw.output_hint_utilization_percent)
  const provider = providerValue(runtimeRaw.provider)
  const model = textValue(runtimeRaw.model).trim()
  const transport = String(runtimeRaw.transport || '')
  const elapsed = runtimeRaw.elapsed_ms
  const elapsedMs = elapsed === null || elapsed === undefined
    ? null
    : Number.isFinite(Number(elapsed)) && Number(elapsed) >= 0 ? Number(elapsed) : null

  if (
    !/^[0-9a-f]{64}$/.test(sourceFingerprint)
    || !title.trim()
    || title.length > 60
    || !logline.trim()
    || logline.length > 500
    || !masterScript.trim()
    || schemaVersion !== 2
    || templateId !== 'deterministic-script-breakdown-v3'
    || formatRaw.compatible !== true
    || !episodeCount
    || !shotCount
    || !episodeDurationSeconds
    || !totalDurationMs
    || shotDurationsMs.length !== episodeCount
    || shotDurationsMs.some((row) => row.length < 1 || row.some((duration) => duration < 1))
    || shotCount % episodeCount !== 0
    || shotDurationsMs.some((row) => row.length !== shotCount / episodeCount)
    || shotDurationsMs.some((row) => (
      row.reduce((sum, duration) => sum + duration, 0) > episodeDurationSeconds * 1_000
    ))
    || flattenedDurations.length !== shotCount
    || flattenedDurations.reduce((sum, duration) => sum + duration, 0) !== totalDurationMs
    || totalDurationMs > episodeCount * episodeDurationSeconds * 1_000
    || resourceTotalShots !== shotCount
    || !estimatedInputChars
    || !estimatedOutputTokens
    || !configuredOutputTokenHint
    || outputHintUtilizationPercent !== Math.ceil(
      estimatedOutputTokens / configuredOutputTokenHint * 100,
    )
    || !provider
    || !model
    || !['local_process', 'remote_api'].includes(transport)
    || runtimeRaw.review_required !== true
    || runtimeRaw.persisted !== false
  ) invalidResponse()

  return {
    sourceFingerprint,
    candidate: { title, logline, masterScript, bible },
    format: {
      schemaVersion: 2,
      templateId: 'deterministic-script-breakdown-v3',
      compatible: true,
      episodeCount,
      shotCount,
      episodeDurationSeconds,
      totalDurationMs,
      shotDurationsMs,
    },
    resourceEstimate: {
      totalShots: resourceTotalShots,
      estimatedInputChars,
      estimatedOutputTokens,
      configuredOutputTokenHint,
      outputHintUtilizationPercent,
    },
    warnings: Array.isArray(raw.warnings)
      ? raw.warnings.map((item) => textValue(item).trim()).filter(Boolean)
      : [],
    runtime: {
      provider: provider as PromptProviderId,
      model,
      transport: transport as 'local_process' | 'remote_api',
      elapsedMs,
      reviewRequired: true,
      persisted: false,
    },
  }
}

const errorEnvelope = (error: unknown): Raw => {
  const body = objectValue(error)
  const response = objectValue(body.response)
  const data = objectValue(response.data)
  return objectValue(data.error)
}

const safeErrorCode = (error: unknown): string => {
  const body = objectValue(error)
  const response = objectValue(body.response)
  const data = objectValue(response.data)
  const envelope = errorEnvelope(error)
  const candidates = [
    envelope.code, data.code, body.code,
  ]
  const code = candidates.map(safeDiagnosticCode).find(Boolean)
  return code || ''
}

const adaptDiagnosticUsage = (value: unknown): SeriesConceptDiagnosticUsage | undefined => {
  const raw = objectValue(value)
  const usage: SeriesConceptDiagnosticUsage = {}
  const promptTokens = boundedInteger(raw.prompt_tokens, 0)
  const completionTokens = boundedInteger(raw.completion_tokens, 0)
  const totalTokens = boundedInteger(raw.total_tokens, 0)
  if (promptTokens !== undefined) usage.promptTokens = promptTokens
  if (completionTokens !== undefined) usage.completionTokens = completionTokens
  if (totalTokens !== undefined) usage.totalTokens = totalTokens
  return Object.keys(usage).length ? usage : undefined
}

const adaptDiagnosticOutput = (value: unknown): SeriesConceptDiagnosticOutput | undefined => {
  const raw = objectValue(value)
  const output: SeriesConceptDiagnosticOutput = {}
  const characterCount = boundedInteger(raw.character_count, 0)
  const utf8ByteCount = boundedInteger(raw.utf8_byte_count, 0)
  const jsonDecodeLine = boundedInteger(raw.json_decode_line, 1)
  const jsonDecodeColumn = boundedInteger(raw.json_decode_column, 1)
  const jsonDecodePosition = boundedInteger(raw.json_decode_position, 0)
  const sha256 = typeof raw.sha256 === 'string' && SAFE_SHA256_RE.test(raw.sha256)
    ? raw.sha256
    : ''
  const redactedJsonShape = safeRedactedJsonShape(raw.redacted_json_shape)
  if (characterCount !== undefined) output.characterCount = characterCount
  if (utf8ByteCount !== undefined) output.utf8ByteCount = utf8ByteCount
  if (sha256) output.sha256 = sha256
  if (redactedJsonShape) output.redactedJsonShape = redactedJsonShape
  if (jsonDecodeLine !== undefined) output.jsonDecodeLine = jsonDecodeLine
  if (jsonDecodeColumn !== undefined) output.jsonDecodeColumn = jsonDecodeColumn
  if (jsonDecodePosition !== undefined) output.jsonDecodePosition = jsonDecodePosition
  return Object.keys(output).length ? output : undefined
}

const adaptDiagnosticDetails = (value: unknown): SeriesConceptDiagnosticDetails => {
  const raw = objectValue(value)
  const details: SeriesConceptDiagnosticDetails = {}
  const attempts = boundedInteger(raw.attempts, 0)
  const elapsedMs = boundedInteger(raw.elapsed_ms, 0)
  const httpStatus = boundedInteger(raw.http_status, 100, 599)
  const requestedMaxTokens = boundedInteger(raw.requested_max_tokens, 1)
  const configuredMaxTokens = boundedInteger(raw.configured_max_tokens, 1)
  const configuredTimeoutSeconds = boundedNumber(raw.configured_timeout_seconds, 0, 3_600)
  const validationErrorCode = safeDiagnosticCode(raw.validation_error_code)
  const repairReasonCode = safeDiagnosticCode(raw.repair_reason_code)
  const validationPath = typeof raw.validation_path === 'string'
    && raw.validation_path.length <= 160
    && SAFE_VALIDATION_PATH_RE.test(raw.validation_path)
    ? raw.validation_path
    : ''
  const output = adaptDiagnosticOutput(raw.output)
  if (attempts !== undefined) details.attempts = attempts
  if (elapsedMs !== undefined) details.elapsedMs = elapsedMs
  if (httpStatus !== undefined) details.httpStatus = httpStatus
  if (typeof raw.incomplete === 'boolean') details.incomplete = raw.incomplete
  if (requestedMaxTokens !== undefined) details.requestedMaxTokens = requestedMaxTokens
  if (configuredMaxTokens !== undefined) details.configuredMaxTokens = configuredMaxTokens
  if (configuredTimeoutSeconds !== undefined) details.configuredTimeoutSeconds = configuredTimeoutSeconds
  if (validationErrorCode) details.validationErrorCode = validationErrorCode
  if (validationPath) details.validationPath = validationPath
  if (typeof raw.repair_attempted === 'boolean') details.repairAttempted = raw.repair_attempted
  if (typeof raw.repair_succeeded === 'boolean') details.repairSucceeded = raw.repair_succeeded
  if (repairReasonCode) details.repairReasonCode = repairReasonCode
  if (output) details.output = output
  return details
}

const adaptSeriesConceptDiagnostic = (
  error: unknown,
  provider: PromptProviderId,
  publicErrorCode: string,
): SeriesConceptDiagnostic | null => {
  const raw = objectValue(objectValue(errorEnvelope(error).details).diagnostic)
  const diagnosticId = textValue(raw.diagnostic_id)
  const diagnosticProvider = providerValue(raw.provider)
  const errorCode = safeDiagnosticCode(raw.error_code)
  const failurePhase = safeDiagnosticCode(raw.failure_phase)
  const finishReason = safeDiagnosticCode(raw.finish_reason)
  const rawDetails = raw.details
  if (
    !SAFE_DIAGNOSTIC_ID_RE.test(diagnosticId)
    || diagnosticProvider !== provider
    || !errorCode
    || errorCode !== publicErrorCode
    || !failurePhase
    || typeof raw.details_available !== 'boolean'
    || !rawDetails
    || typeof rawDetails !== 'object'
    || Array.isArray(rawDetails)
  ) return null
  const usage = adaptDiagnosticUsage(raw.usage)
  const details = adaptDiagnosticDetails(rawDetails)
  const diagnostic: SeriesConceptDiagnostic = {
    diagnosticId,
    provider: diagnosticProvider,
    errorCode,
    failurePhase,
    detailsAvailable: raw.details_available === true && Object.keys(details).length > 0,
    details,
  }
  if (finishReason) diagnostic.finishReason = finishReason
  if (usage) diagnostic.usage = usage
  return diagnostic
}

export const seriesConceptDraftRequestFailure = (
  error: unknown,
  provider: PromptProviderId,
): SeriesConceptDraftFailure => {
  const code = safeErrorCode(error)
  const providerLabel = provider === 'deepseek_api' ? 'DeepSeek API' : '本地 Qwen'
  const messages: Record<string, string> = {
    series_concept_idea_required: '请先输入故事想法。',
    series_concept_idea_too_long: `故事想法超过当前 ${SERIES_CONCEPT_MAX_IDEA_CHARS.toLocaleString()} 字符上限，请精简后重试。`,
    series_concept_style_hint_too_long: '当前风格提示过长，请精简后重试。',
    series_concept_episode_count_invalid: '计划集数必须为正整数。',
    series_concept_shots_per_episode_invalid: '每集镜头数必须为正整数。',
    series_concept_episode_duration_invalid: '单集时长必须为正整数秒。',
    series_concept_episode_duration_infeasible: '当前单集时长无法容纳所选镜头数，请提高单集时长或减少每集镜头数。',
    series_concept_input_budget_exceeded: '当前请求超过所选通道的运行时输入预算；这不是集数或镜头数的产品上限，请提高通道上下文预算后重试。',
    series_concept_invalid_candidate: `${providerLabel} 未返回可审核的完整结构，请重试。`,
    series_concept_invalid_request: '本次生成参数未通过服务端校验，请核对输入后重试。',
    series_concept_resource_busy: `${providerLabel} 当前正被其他生成任务占用，请刷新状态或稍后直接重试。`,
    series_concept_model_load_failed: '本地模型加载失败，请检查模型文件、显存或内存和本地运行服务后重试。',
    series_concept_prompt_failed: '本地模型请求构建失败，请检查运行服务后重试。',
    series_concept_inference_failed: '本地模型推理失败或中断，请检查本地资源状态后重试。',
    series_concept_output_audit_failed: '本地模型输出不完整或未通过完整性审计，请重试。',
    series_concept_output_invalid_json: '本地模型输出无法解析为结构化候选，请重试。',
    series_concept_validation_failed: '本地模型输出未通过兼容模板校验，请重试。',
    series_concept_runtime_failed: '本地模型运行阶段失败，请检查运行日志和资源状态后重试。',
    series_concept_parent_timeout: '本地模型生成超过进程等待时限；请检查模型速度和运行时超时配置后重试。',
    series_concept_runtime_protocol_failed: '本地模型进程返回了无效协议响应，请检查本地运行时后重试。',
    series_concept_runtime_exit: '本地模型进程异常退出，请检查显存、内存和运行日志后重试。',
    series_concept_runtime_unavailable: `${providerLabel} 运行服务当前不可用，请重新检查通道状态后重试。`,
    series_concept_provider_network_failed: '连接 DeepSeek API 失败，请检查服务端网络后重试。',
    series_concept_provider_rate_limited: 'DeepSeek API 当前限流，请稍后重试。',
    series_concept_provider_request_rejected: 'DeepSeek API 拒绝了本次完整请求，请检查所选模型及上游服务限制后重试。',
    series_concept_provider_invalid_response: 'DeepSeek API 返回了无效响应，请稍后重试。',
    series_concept_provider_output_truncated: 'DeepSeek 达到当前连续剧模型的输出能力后停止，返回内容不完整；这不是集数或镜头数的产品上限，本次仍按单次请求执行。请重试；若持续出现，请管理员检查模型输出能力配置。',
    series_concept_generation_failed: `${providerLabel} 本次生成未完成，请检查通道状态后重试。`,
    series_concept_invalid_response: `${providerLabel} 返回结果未通过安全响应校验，请重试。`,
    series_concept_draft_invalid_request: 'DeepSeek 结构化生成请求未通过校验，请核对输入后重试。',
    series_concept_draft_output_invalid_json: 'DeepSeek 返回内容无法解析为结构化候选，请重试。',
    series_concept_draft_empty_output: 'DeepSeek 本次没有返回候选正文，请稍后重试。',
    series_concept_draft_validation_failed: 'DeepSeek 返回内容未通过兼容模板校验，请重试。',
    deepseek_api_authentication_failed: 'DeepSeek API 鉴权失败，请检查服务端密钥配置后重试。',
    deepseek_api_disabled: 'DeepSeek API 通道当前已停用，请启用后重新检查状态。',
    deepseek_api_misconfigured: 'DeepSeek API 通道配置不完整，请修正服务端配置后重试。',
    deepseek_protocol_unavailable: 'DeepSeek 结构化生成协议当前不可用，请检查服务端运行环境后重试。',
    deepseek_provider_unavailable: 'DeepSeek API 服务模块当前不可用，请检查服务端运行环境后重试。',
    deepseek_api_rate_limited: 'DeepSeek API 当前限流，请稍后重试。',
    deepseek_api_unavailable: 'DeepSeek API 当前不可用，请稍后重试。',
    deepseek_api_network_failed: '连接 DeepSeek API 失败，请检查服务端网络后重试。',
    deepseek_api_request_rejected: 'DeepSeek API 拒绝了本次请求，请检查服务端配置和请求预算后重试。',
    deepseek_api_invalid_response: 'DeepSeek API 返回了无效响应，请重试。',
    creative_provider_busy: `${providerLabel} 当前繁忙，请稍后重试。`,
    creative_generation_busy: `${providerLabel} 当前繁忙，请稍后重试。`,
    shared_resource_busy: `${providerLabel} 当前繁忙，请稍后重试。`,
  }
  return {
    message: `${messages[code] || `${providerLabel} 生成失败，请检查通道状态后重试。`}手工表单和上一版候选均未修改。`,
    diagnostic: code ? adaptSeriesConceptDiagnostic(error, provider, code) : null,
  }
}

export const seriesConceptDraftRequestError = (
  error: unknown,
  provider: PromptProviderId,
): string => seriesConceptDraftRequestFailure(error, provider).message
