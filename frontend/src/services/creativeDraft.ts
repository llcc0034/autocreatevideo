import type {
  CreativeDraft,
  CreativeDraftApplyResult,
  CreativeDraftData,
  CreativeDraftDialogueLine,
  CreativeDraftReviewField,
  CreativeDraftReviewIssue,
  CreativeDraftReviewSeverity,
  CreativeDraftReviewSummary,
  CreativeDraftShot,
  CreativeDraftValidation,
  H3Mode,
} from '@/types'

type Raw = Record<string, any>

export const CREATIVE_DRAFT_ENGLISH_LIMITS = {
  narrative: { generation: 1000, editor: 1000 },
  action: { generation: 1000, editor: 1000 },
} as const

const objectValue = (value: unknown): Raw => (
  value && typeof value === 'object' ? value as Raw : {}
)

const relationId = (value: unknown): string => String(
  value && typeof value === 'object' ? (value as Raw).id || '' : value || '',
)
const text = (value: unknown): string => String(value ?? '')
const list = (value: unknown): Raw[] => Array.isArray(value)
  ? value.filter((item): item is Raw => Boolean(item && typeof item === 'object'))
  : []
const ids = (value: unknown): string[] => Array.isArray(value) ? value.map(relationId).filter(Boolean) : []
const h3Mode = (value: unknown): H3Mode => (
  ['T2VA', 'I2VA', 'FL2VA', 'L2VA', 'Ref2VA'].includes(String(value)) ? String(value) as H3Mode : 'T2VA'
)

const reviewSeverity = (value: unknown): CreativeDraftReviewSeverity => {
  const normalized = String(value || '').trim().toLowerCase()
  if (normalized === 'requires_edit') return 'requires_edit'
  if (normalized === 'risk' || normalized === 'blocking' || normalized === 'error') return 'risk'
  return 'warning'
}

const reviewFieldAliases: Record<string, CreativeDraftReviewField> = {
  title_zh: 'titleZh', titlezh: 'titleZh', title_en: 'titleEn', titleen: 'titleEn',
  narrative_zh: 'narrativeZh', narrativezh: 'narrativeZh', narrative_en: 'narrativeEn', narrativeen: 'narrativeEn',
  narrative: 'narrativeEn',
  action_zh: 'actionZh', actionzh: 'actionZh', action_en: 'actionEn', actionen: 'actionEn',
  action: 'actionEn',
  camera_zh: 'cameraZh', camerazh: 'cameraZh', camera_en: 'cameraEn', cameraen: 'cameraEn',
  camera_movement: 'cameraEn', camera_motivation: 'cameraEn', camera: 'cameraEn',
  continuity_zh: 'continuityZh', continuityzh: 'continuityZh', continuity_en: 'continuityEn', continuityen: 'continuityEn',
  continuity: 'continuityEn',
  soundscape_zh: 'soundscapeZh', soundscapezh: 'soundscapeZh', soundscape_en: 'soundscapeEn', soundscapeen: 'soundscapeEn',
  ambient_zh: 'soundscapeZh', ambient_en: 'soundscapeEn',
  music_zh: 'musicZh', musiczh: 'musicZh', music_en: 'musicEn', musicen: 'musicEn',
  duration_ms: 'durationMs', duration: 'durationMs', h3_mode: 'h3Mode', include_audio: 'includeAudio',
  scene_id: 'sceneId', character_ids: 'characterIds', prop_ids: 'propIds',
  speaker_id: 'dialogueSpeakerId', text_zh: 'dialogueTextZh', dialogue: 'dialogueSpeakerId',
  delivery_zh: 'dialogueDeliveryZh', delivery_en: 'dialogueDeliveryEn',
}

const parseReviewTarget = (pathValue: unknown): Pick<CreativeDraftReviewIssue, 'path' | 'shotIndex' | 'field' | 'language'> => {
  const path = String(pathValue || '').trim().slice(0, 180)
  const shotMatch = path.match(/shots?(?:\[|\.|\/)(\d+)/i)
  const shotIndex = shotMatch ? Number(shotMatch[1]) : undefined
  const normalizedPath = path.toLowerCase().replace(/[\[\]./]+/g, '_')
  const fieldAlias = Object.keys(reviewFieldAliases)
    .sort((left, right) => right.length - left.length)
    .find((key) => normalizedPath.includes(key))
  const field = fieldAlias ? reviewFieldAliases[fieldAlias] : undefined
  const language = field?.endsWith('Zh') ? 'zh' : field?.endsWith('En') ? 'en' : undefined
  return { path, shotIndex, field, language }
}

const safeSuggestion = (value: unknown): string | undefined => {
  const suggestion = String(value || '').replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim()
  return suggestion && /[\u3400-\u4dbf\u4e00-\u9fff]/.test(suggestion) ? suggestion.slice(0, 240) : undefined
}

const actionCoverageIssue = (code: string): boolean => (
  /script_action_coverage_low|action_coverage_low|script.*action.*coverage/i.test(code)
)

const adaptReviewIssues = (raw: unknown): CreativeDraftReviewIssue[] => list(raw).map((item) => {
  const code = String(item.code || 'review_required').trim().slice(0, 80) || 'review_required'
  const target = parseReviewTarget(item.path ?? item.field_path ?? item.fieldPath)
  return {
    code,
    // Missing action coverage is always advisory. A stale/over-strict backend
    // severity must not turn this content-quality hint into an application gate.
    severity: actionCoverageIssue(code) ? 'warning' : reviewSeverity(item.severity),
    ...target,
    // Script-action coverage is always reviewed against the authoritative Chinese
    // action chain, even when an older backend omitted the terminal field in path.
    ...(actionCoverageIssue(code) ? { field: 'actionZh' as const, language: 'zh' as const } : {}),
    suggestionZh: safeSuggestion(item.suggestion_zh ?? item.suggestionZh),
  }
})

const adaptReviewSummary = (
  raw: unknown,
  issues: CreativeDraftReviewIssue[],
): CreativeDraftReviewSummary => {
  const body = objectValue(raw)
  const issueCount = Number(body.issue_count ?? body.issueCount)
  const riskCount = Number(body.risk_count ?? body.riskCount)
  const requiresEditCount = Number(body.requires_edit_count ?? body.requiresEditCount)
  const displayedRequiresEditCount = issues.filter((item) => item.severity === 'requires_edit').length
  const advisoryCoverageCount = issues.filter((item) => actionCoverageIssue(item.code)).length
  return {
    issueCount: Number.isFinite(issueCount) ? Math.max(issues.length, issueCount) : issues.length,
    riskCount: Number.isFinite(riskCount)
      ? Math.max(0, riskCount)
      : issues.filter((item) => item.severity === 'risk').length,
    // Preserve reported data-integrity gates while removing any stale count
    // contributed by action-coverage hints, which are always advisory.
    requiresEditCount: Number.isFinite(requiresEditCount)
      ? Math.max(displayedRequiresEditCount, Math.max(0, requiresEditCount - advisoryCoverageCount))
      : displayedRequiresEditCount,
    status: issues.length || String(body.status) === 'needs_review' ? 'needs_review' : 'ready',
  }
}

/** Stable operator copy keyed only by finite issue codes; raw backend details are never rendered. */
export const creativeDraftReviewIssueTitle = (issue: CreativeDraftReviewIssue): string => {
  const code = issue.code.toLowerCase()
  if (actionCoverageIssue(code)) return '本镜可能未完整覆盖剧本动作链'
  if (/dialogue.*manifest|manifest.*dialogue/.test(code)) {
    return '草稿中的对白说话人或原文分配与原剧本清单不一致'
  }
  if (/unknown.*scene|scene.*(?:missing|invalid)|invalid_scene/.test(code)) return '关联场景不存在，应用前需要替换或删除'
  if (/unknown.*character|character.*(?:missing|invalid)|invalid_character/.test(code)) return '关联人物不存在，应用前需要替换或删除'
  if (/unknown.*prop|prop.*(?:missing|invalid)|invalid_prop/.test(code)) return '关联道具不存在，应用前需要替换或删除'
  if (/unknown.*speaker|speaker.*(?:missing|invalid)|invalid_speaker/.test(code)) return '对白说话人不存在，应用前需要替换或删除'
  if (/unknown|missing_(?:entity|speaker)|invalid_(?:entity|reference)|foreign_key/.test(code)) {
    return '关联的素材或说话人不存在，应用前需要替换或删除'
  }
  if (/bilateral|both_sides|encirc/.test(code)) return '双侧动作的中英文表达可能不一致'
  if (/direction|handed|body_side|left_right|spatial/.test(code)) return '中英文方位、左右侧或动作归属可能不一致'
  if (/numeric|number|quantity/.test(code)) return '中英文数字或数量可能不一致'
  if (/negation|negative_fact/.test(code)) return '中英文肯定、否定含义可能不一致'
  if (/canonical|glossary|term/.test(code)) return '英文可能没有使用已确认的专名或术语'
  if (/shot_(?:count|order)|order_irregular/.test(code)) return '镜头数量、顺序或内部标识需要人工核对'
  if (/required.*missing|content_missing/.test(code)) return '此字段缺少建议填写的内容'
  if (/camera.*motivation|motivation.*camera/.test(code)) return '运镜动机可能不够明确'
  if (/camera|shot_move/.test(code)) return '运镜表达可能需要人工核对'
  if (/cjk|english|language|translation/.test(code)) return '英文字段可能仍含中文或双语含义不一致'
  if (/continuity|end_state|chronology|order/.test(code)) return '前后连续性或动作顺序可能需要核对'
  if (/character|identity|speaker|actor/.test(code)) return '人物身份或动作主体可能需要核对'
  if (/prop|scene|location/.test(code)) return '场景、道具或位置关系可能需要核对'
  if (/length|too_long|budget/.test(code)) return '字段较长，建议确认重点是否清晰'
  if (/duration/.test(code)) return '镜头时长超出建议范围'
  if (/audio_disabled|audio.*content/.test(code)) return '镜头标记为静音，但仍保留了音频内容'
  if (/duplicate_binding/.test(code)) return '同一素材引用出现重复，请核对是否需要精简'
  if (/density|arc_dense/.test(code)) return '短镜头中的动作或对白较密，建议核对可执行性'
  if (/dialogue|delivery|voice/.test(code)) return '对白表达方式可能需要人工核对'
  return '此处可能与剧本或中英文对应关系不完全一致'
}

/** Finite, operator-facing recovery copy. It is derived from code/field only and never exposes backend detail. */
export const creativeDraftReviewIssueAction = (issue: CreativeDraftReviewIssue): string => {
  const code = issue.code.toLowerCase()
  if (actionCoverageIssue(code)) {
    return '仅作人工核验提醒，不会阻止保存、确认或应用；请对照剧本补全连续动作链，或提高本镜时长后再判断。'
  }
  if (/dialogue.*manifest|manifest.*dialogue/.test(code)) {
    return '逐镜核对说话人与对白原文；无法判断时，重新生成对应镜头。'
  }
  if (issue.field === 'sceneId') return '核对本镜场景绑定；无法匹配 Bible 时，重新生成本镜。'
  if (issue.field === 'characterIds') return '核对本镜人物绑定；移除未知人物或重新生成本镜。'
  if (issue.field === 'propIds') return '核对本镜道具绑定；移除未知道具或重新生成本镜。'
  if (issue.field === 'dialogueSpeakerId') return '核对说话人与对白归属；对白为只读，无法匹配时请重新生成本镜。'
  if (issue.field === 'dialogueTextZh') return '对照原剧本核对对白原文；对白为只读，异常时请重新生成本镜。'
  return issue.severity === 'requires_edit'
    ? '按定位字段修正；若该字段只读，请重新生成对应镜头。'
    : '对照中文权威源人工核对，必要时修改或重新生成本镜。'
}

const adaptDialogue = (raw: Raw): CreativeDraftDialogueLine => ({
  speakerId: relationId(raw.speaker_id ?? raw.speakerId),
  textZh: text(raw.text_zh ?? raw.textZh),
  deliveryZh: text(raw.delivery_zh ?? raw.deliveryZh),
  deliveryEn: text(raw.delivery_en ?? raw.deliveryEn),
  voiceover: raw.voiceover === true,
})

const adaptShot = (raw: Raw, position: number): CreativeDraftShot => ({
  clientKey: text(raw.client_key ?? raw.clientKey) || `shot-${position + 1}`,
  order: Number(raw.order || position + 1),
  titleZh: text(raw.title_zh ?? raw.titleZh),
  titleEn: text(raw.title_en ?? raw.titleEn),
  narrativeZh: text(raw.narrative_zh ?? raw.narrativeZh),
  narrativeEn: text(raw.narrative_en ?? raw.narrativeEn),
  cameraZh: text(raw.camera_zh ?? raw.cameraZh),
  cameraEn: text(raw.camera_en ?? raw.cameraEn),
  actionZh: text(raw.action_zh ?? raw.actionZh),
  actionEn: text(raw.action_en ?? raw.actionEn),
  continuityZh: text(raw.continuity_zh ?? raw.continuityZh),
  continuityEn: text(raw.continuity_en ?? raw.continuityEn),
  soundscapeZh: text(raw.soundscape_zh ?? raw.soundscapeZh),
  soundscapeEn: text(raw.soundscape_en ?? raw.soundscapeEn),
  musicZh: text(raw.music_zh ?? raw.musicZh),
  musicEn: text(raw.music_en ?? raw.musicEn),
  durationMs: Number(raw.duration_ms ?? raw.durationMs ?? 5000),
  mediaType: 'video',
  h3Mode: h3Mode(raw.h3_mode ?? raw.h3Mode),
  includeAudio: raw.include_audio !== false && raw.includeAudio !== false,
  sceneId: relationId(raw.scene_id ?? raw.sceneId) || undefined,
  characterIds: ids(raw.character_ids ?? raw.characterIds),
  propIds: ids(raw.prop_ids ?? raw.propIds),
  dialogue: list(raw.dialogue).map(adaptDialogue),
})

export const adaptCreativeDraftData = (raw: unknown): CreativeDraftData => {
  const body = raw && typeof raw === 'object' ? raw as Raw : {}
  const episode = body.episode && typeof body.episode === 'object' ? body.episode as Raw : {}
  return {
    schemaVersion: 1,
    episode: {
      titleZh: text(episode.title_zh ?? episode.titleZh),
      titleEn: text(episode.title_en ?? episode.titleEn),
      synopsisZh: text(episode.synopsis_zh ?? episode.synopsisZh),
      synopsisEn: text(episode.synopsis_en ?? episode.synopsisEn),
    },
    shots: list(body.shots).map(adaptShot),
  }
}

const adaptValidation = (raw: unknown): CreativeDraftValidation => {
  const body = raw && typeof raw === 'object' ? raw as Raw : {}
  const status = ['passed', 'failed', 'pending', 'ready', 'needs_review'].includes(String(body.status))
    ? body.status as CreativeDraftValidation['status']
    : 'pending'
  return {
    status,
    errors: Array.isArray(body.errors) ? body.errors.map(text).filter(Boolean) : [],
    warnings: Array.isArray(body.warnings) ? body.warnings.map(text).filter(Boolean) : [],
  }
}

export const adaptCreativeDraft = (raw: Raw): CreativeDraft => {
  const validationBody = objectValue(raw.validation)
  const reviewIssues = adaptReviewIssues(
    raw.review_issues ?? raw.reviewIssues ?? validationBody.review_issues ?? validationBody.reviewIssues ?? validationBody.issues,
  )
  return {
    id: text(raw.id),
    projectId: relationId(raw.project),
    episodeId: relationId(raw.episode),
    status: ['draft', 'confirmed', 'applied', 'superseded', 'failed'].includes(String(raw.status))
      ? raw.status : 'failed',
    revision: Number(raw.revision || 1),
    generationNo: Number(raw.generation_no ?? raw.generationNo ?? 1),
    parentDraftId: relationId(raw.parent_draft ?? raw.parentDraft) || undefined,
    contextHash: text(raw.context_hash ?? raw.contextHash),
    contextCurrent: raw.context_current !== false && raw.contextCurrent !== false,
    creativity: ['conservative', 'balanced', 'bold'].includes(String(raw.creativity))
      ? raw.creativity : 'balanced',
    variationNote: text(raw.variation_note ?? raw.variationNote),
    draftData: adaptCreativeDraftData(raw.draft_data ?? raw.draftData),
    validation: adaptValidation(raw.validation),
    reviewIssues,
    reviewSummary: adaptReviewSummary(
      raw.review_summary ?? raw.reviewSummary ?? validationBody.summary,
      reviewIssues,
    ),
    warnings: Array.isArray(raw.warnings) ? raw.warnings.map(text).filter(Boolean) : [],
    errorMessage: text(raw.error_message ?? raw.errorMessage) || undefined,
    provider: text(raw.provider) || undefined,
    model: text(raw.model) || undefined,
    runtimeMetadata: raw.runtime_metadata && typeof raw.runtime_metadata === 'object'
      ? raw.runtime_metadata : undefined,
    confirmedAt: text(raw.confirmed_at ?? raw.confirmedAt) || undefined,
    appliedAt: text(raw.applied_at ?? raw.appliedAt) || undefined,
    createdAt: text(raw.created_at ?? raw.createdAt) || undefined,
    updatedAt: text(raw.updated_at ?? raw.updatedAt) || undefined,
    canEdit: raw.can_edit === true || raw.canEdit === true,
    canRegenerate: raw.can_regenerate === true || raw.canRegenerate === true,
    canConfirm: raw.can_confirm === true || raw.canConfirm === true,
    canApply: raw.can_apply === true || raw.canApply === true,
  }
}

export const creativeDraftDataPayload = (data: CreativeDraftData) => ({
  schema_version: 1,
  episode: {
    title_zh: data.episode.titleZh,
    title_en: data.episode.titleEn,
    synopsis_zh: data.episode.synopsisZh,
    synopsis_en: data.episode.synopsisEn,
  },
  shots: data.shots.map((shot) => ({
    client_key: shot.clientKey,
    order: shot.order,
    title_zh: shot.titleZh,
    title_en: shot.titleEn,
    narrative_zh: shot.narrativeZh,
    narrative_en: shot.narrativeEn,
    camera_zh: shot.cameraZh,
    camera_en: shot.cameraEn,
    action_zh: shot.actionZh,
    action_en: shot.actionEn,
    continuity_zh: shot.continuityZh,
    continuity_en: shot.continuityEn,
    soundscape_zh: shot.soundscapeZh,
    soundscape_en: shot.soundscapeEn,
    music_zh: shot.musicZh,
    music_en: shot.musicEn,
    duration_ms: shot.durationMs,
    media_type: 'video',
    h3_mode: shot.h3Mode,
    include_audio: shot.includeAudio,
    scene_id: shot.sceneId || null,
    character_ids: shot.characterIds,
    prop_ids: shot.propIds,
    dialogue: shot.dialogue.map((line) => ({
      speaker_id: line.speakerId,
      text_zh: line.textZh,
      delivery_zh: line.deliveryZh,
      delivery_en: line.deliveryEn,
      voiceover: line.voiceover,
    })),
  })),
})

export const adaptCreativeDraftApplyResult = (raw: Raw): CreativeDraftApplyResult => ({
  draft: adaptCreativeDraft(raw.draft || {}),
  episodeId: relationId(raw.episode),
  appliedShotIds: ids(raw.applied_shot_ids ?? raw.appliedShotIds),
  translationsWritten: Number(raw.translations_written ?? raw.translationsWritten ?? 0),
})

export const creativeDraftIsEditable = (draft?: CreativeDraft | null): boolean => Boolean(
  draft?.canEdit && ['draft', 'failed'].includes(draft.status) && draft.contextCurrent,
)

/** A persisted runtime-failure record is auditable and retryable, but is not an editable empty draft. */
export const creativeDraftIsEmptyFailure = (draft?: CreativeDraft | null): boolean => Boolean(
  draft?.status === 'failed' && draft.draftData.shots.length === 0,
)

export interface CreativeSafeFailureDiagnostic {
  code: string
  path: string
}

/**
 * Extracts only stable identifiers that are safe to show to an operator.
 * Model text, parser offsets and free-form validation messages remain hidden.
 */
export const creativeSafeFailureDiagnostic = (metadata: unknown): CreativeSafeFailureDiagnostic => {
  const body = objectValue(metadata)
  const validation = objectValue(body.validation_error ?? body.validationError)
  const rawCode = String(
    validation.code ?? body.validation_error_code ?? body.validationErrorCode
    ?? body.error_code ?? body.errorCode ?? '',
  ).trim().toLowerCase()
  const rawPath = String(
    validation.path ?? body.validation_path ?? body.validationPath
    ?? body.error_path ?? body.errorPath ?? '',
  ).trim()
  const code = /^[a-z][a-z0-9_]{0,79}$/.test(rawCode) ? rawCode : ''
  const safePathCharacters = /^[a-zA-Z0-9_.$/\[\]-]{1,160}$/.test(rawPath)
  const safePathRoot = /^(?:\$\.?|\/)?(?:episode|shots)(?:[.\[/]|$)/.test(rawPath)
  return { code, path: safePathCharacters && safePathRoot ? rawPath : '' }
}

const retryInstruction = (contextCurrent: boolean): string => (
  contextCurrent
    ? '请一键重试整稿；若持续失败，改为 1–2 镜或缩短本次创作要求。'
    : '当前故事、Bible 或剧本已变化，请按当前内容生成新草稿。'
)

/**
 * Converts only known, public failure codes to operator recovery guidance.
 * Raw backend detail is deliberately never accepted here because it can carry
 * model output, parser offsets, or process internals.
 */
export const creativeFailureRecovery = (
  rawCode: unknown,
  contextCurrent = true,
): string | undefined => {
  const code = String(rawCode || '').trim()
  const retry = retryInstruction(contextCurrent)
  const unchanged = '正式分镜未修改。'
  if (code === 'creative_resource_busy') {
    return `视频生产或其他本地 Qwen 请求正在占用资源；请等待当前任务结束后再重试。${unchanged}`
  }
  if (code === 'creative_runtime_unavailable') {
    return `本地 Qwen 运行环境不可用或模型未就绪；请检查“模型与能力”状态后重试。${unchanged}`
  }
  if (code === 'creative_draft_conflict') {
    return `项目内容或草稿版本已变化；请刷新并基于当前故事重新生成。${unchanged}`
  }
  if (code === 'creative_review_acknowledgement_required') {
    return '草稿仍有人工检查项；请打开风险摘要，勾选“已阅读并承担风险”后再继续。正式分镜未修改。'
  }
  if (code === 'creative_review_requires_edit') {
    return '草稿仍引用未知素材、说话人或无效数据；请跳到对应镜头，替换、删除或重新生成该镜后再应用。正式分镜未修改。'
  }
  if (['creative_output_budget_exhausted', 'output_budget_exhausted'].includes(code)) {
    const action = contextCurrent
      ? '请不要按原设置重复提交：把本段改为 1–2 镜，或按剧情与权威对白边界拆分后重新生成。'
      : retry
    return `本地 Qwen 已用完本次动态输出容量，返回内容不完整且没有可采用草稿。${action}${unchanged}`
  }
  if (['creative_repair_not_started', 'repair_not_started'].includes(code)) {
    const action = contextCurrent
      ? '请缩减为 1–2 镜、缩短本次创作要求，并按权威对白边界拆分后重新生成。'
      : retry
    return `首轮输出未通过结构校验，剩余执行时间或上下文不足，修复阶段未能启动。${action}${unchanged}`
  }
  if (code === 'creative_output_invalid_json') {
    return `本地 Qwen 返回的分镜草稿格式不完整，未生成可审核草稿。${retry}${unchanged}`
  }
  if (['creative_timeout', 'creative_parent_timeout'].includes(code)) {
    return `本地 Qwen 本次创作超过运行时限，模型进程已结束，未生成可审核草稿。${retry}${unchanged}`
  }
  if (code === 'creative_invalid_runtime_json') {
    return `本地 Qwen 运行时返回的响应格式异常，未生成可审核草稿。请刷新页面并检查“模型与能力”状态后再试；若持续出现，请重启本地 Qwen 服务。${unchanged}`
  }
  if (code === 'creative_runtime_exit') {
    return `本地 Qwen 进程异常退出，未生成可审核草稿。请检查“模型与能力”状态，确认没有视频生产或其他 Qwen 任务占用后再重试。${unchanged}`
  }
  if (code === 'creative_validation_failed') {
    return `本地 Qwen 输出未通过结构校验，未生成可审核草稿。${retry}${unchanged}`
  }
  if (code === 'creative_runtime_failed') {
    return `本地 Qwen 运行失败，未生成可审核草稿。${retry}${unchanged}`
  }
  if (code === 'creative_storyboard_validation_failed') {
    return `DeepSeek 候选未通过结构或字段长度校验，已安全拒绝采用。请根据下方问题位置缩短对应字段后重试。${unchanged}`
  }
  if (code === 'creative_storyboard_output_invalid_json') {
    return `DeepSeek 返回的分镜结构无法安全解析，未生成可审核草稿。${retry}${unchanged}`
  }
  if (['creative_storyboard_output_budget_exhausted', 'creative_storyboard_incomplete_response'].includes(code)) {
    return `DeepSeek 未完整返回本次分镜结构。请减少镜数、缩短创作要求或按剧情边界拆分后重试。${unchanged}`
  }
  if (code === 'creative_storyboard_empty_output') {
    return `DeepSeek 本次未返回可审核内容。请稍后重试；若持续出现，请检查服务端模型配置。${unchanged}`
  }
  if (['deepseek_api_disabled', 'deepseek_api_misconfigured', 'deepseek_protocol_unavailable'].includes(code)) {
    return `DeepSeek 通道尚未正确配置；请检查服务端启用状态、模型和凭据后重试。${unchanged}`
  }
  if (code === 'deepseek_api_authentication_failed') {
    return `DeepSeek API 鉴权失败；请更新服务端凭据后重试。${unchanged}`
  }
  if (code === 'deepseek_api_rate_limited') {
    return `DeepSeek API 当前触发限流；请稍后重试，避免连续重复提交。${unchanged}`
  }
  if (['deepseek_api_network_failed', 'deepseek_api_unavailable'].includes(code)) {
    return `DeepSeek API 当前无法连接或服务暂不可用；请稍后重试。${unchanged}`
  }
  if (['deepseek_api_request_rejected', 'deepseek_api_invalid_response'].includes(code)) {
    return `DeepSeek API 拒绝了请求或返回格式异常；请检查服务端配置后重试。${unchanged}`
  }
  return undefined
}

/** Normalizes flat DRF errors, the unified error envelope and browser timeouts. */
export const creativeDraftRequestError = (cause: unknown, fallback: string): string => {
  const candidate = objectValue(cause)
  const response = objectValue(candidate.response)
  const data = objectValue(response.data)
  const envelope = objectValue(data.error)
  const code = String(envelope.code || data.code || candidate.code || '')
  const rawMessage = String(candidate.message || '')
  const recovery = creativeFailureRecovery(code)
  if (recovery) return recovery
  const timedOut = code === 'ECONNABORTED' || code === 'ETIMEDOUT' || /timeout|timed out/i.test(rawMessage)
  if (timedOut) {
    return '浏览器等待本地 Qwen 超过 17 分钟仍未收到响应；服务端正常应在 16 分钟硬上限内结束。这不等于服务端已取消。系统已刷新版本记录：若出现“生成失败”版本，可点击“一键重试整稿”；若没有，请先检查服务连接、等待模型释放或刷新后再重试。正式分镜未修改。'
  }
  // Unknown backend details are intentionally not rendered: they may contain
  // transport, parser, or model internals. Callers provide a safe fallback.
  return fallback
}

/** Local editor checks are advisory; the server remains the apply gate. */
export const creativeDraftEditorWarnings = (data: CreativeDraftData): string[] => {
  const warnings: string[] = []
  if (!data.shots.length) warnings.push('草稿没有分镜。')
  for (const shot of data.shots) {
    const label = `镜头 ${shot.order}`
    if (shot.durationMs < 5000 || shot.durationMs > 15000) warnings.push(`${label} 时长必须为 5–15 秒。`)
    if (!shot.narrativeZh.trim() || !shot.narrativeEn.trim()) warnings.push(`${label} 缺少中英叙事结构。`)
    if (shot.narrativeEn.length > CREATIVE_DRAFT_ENGLISH_LIMITS.narrative.editor) {
      warnings.push(`${label} 英文叙事结构超过 ${CREATIVE_DRAFT_ENGLISH_LIMITS.narrative.editor} 字符，建议人工核对并按需精简；仍可保存为草稿。`)
    }
    if (shot.actionEn.length > CREATIVE_DRAFT_ENGLISH_LIMITS.action.editor) {
      warnings.push(`${label} 英文主动作超过 ${CREATIVE_DRAFT_ENGLISH_LIMITS.action.editor} 字符，建议人工核对并按需精简；仍可保存为草稿。`)
    }
    if (!shot.actionZh.trim() || !shot.actionEn.trim()) warnings.push(`${label} 缺少中英主动作。`)
    if (!shot.cameraZh.trim() || !shot.cameraEn.trim()) warnings.push(`${label} 缺少中英单一运镜。`)
    if (!shot.continuityZh.trim() || !shot.continuityEn.trim()) warnings.push(`${label} 缺少中英连续性进出状态。`)
    if (shot.dialogue.some((line) => !line.textZh.trim())) warnings.push(`${label} 存在空对白正文。`)
    if (!shot.includeAudio && shot.dialogue.length) warnings.push(`${label} 选择了静音但仍含锁定对白；可保存，请在应用前决定保留音频或重生此镜。`)
  }
  return warnings
}
