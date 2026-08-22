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
  PromptProviderCapability,
  PromptProviderId,
} from '@/types'

type Raw = Record<string, any>

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

const collectPositiveShotNumbers = (source: string, pattern: RegExp): number[] => {
  const matches: number[] = []
  pattern.lastIndex = 0
  let match = pattern.exec(source)
  while (match) {
    const value = Number(match[1])
    if (Number.isInteger(value) && value > 0) matches.push(value)
    match = pattern.exec(source)
  }
  return matches
}

export const inferCreativeDraftShotCountFromScript = (value: unknown): number | null => {
  const source = text(value).replace(/\r\n/g, '\n').trim()
  if (!source) return null
  const numbers = new Set<number>([
    ...collectPositiveShotNumbers(source, /^\s*(?:镜头|分镜)\s*(?:#|第)?\s*0*([1-9]\d*)\b/gmu),
    ...collectPositiveShotNumbers(source, /^\s*shot\s*#?\s*0*([1-9]\d*)\b/gimu),
    ...collectPositiveShotNumbers(source, /(?:^|\n)\s*[【\[]\s*0*([1-9]\d*)\s*(?=(?:[\[【][^\]\n】]*[\]】]\s*)*[:：])/gu),
  ])
  if (!numbers.size) return null
  const ordered = [...numbers].sort((left, right) => left - right)
  let contiguous = 0
  for (const number of ordered) {
    if (number === contiguous + 1) contiguous = number
    else if (number > contiguous + 1) break
  }
  return contiguous || ordered.length
}

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
  requires_first_frame_continuity: 'requiresFirstFrameContinuity',
  first_frame_continuity: 'requiresFirstFrameContinuity',
  requires_last_frame_continuity: 'requiresLastFrameContinuity',
  last_frame_continuity: 'requiresLastFrameContinuity',
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
  ...(
    Number.isSafeInteger(Number(raw.start_ms ?? raw.startMs))
    && Number.isSafeInteger(Number(raw.end_ms ?? raw.endMs))
    && Number(raw.start_ms ?? raw.startMs) >= 0
    && Number(raw.end_ms ?? raw.endMs) > Number(raw.start_ms ?? raw.startMs)
      ? {
        startMs: Number(raw.start_ms ?? raw.startMs),
        endMs: Number(raw.end_ms ?? raw.endMs),
      }
      : {}
  ),
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
  durationMs: Number(raw.duration_ms ?? raw.durationMs ?? 0),
  mediaType: 'video',
  h3Mode: h3Mode(raw.h3_mode ?? raw.h3Mode),
  includeAudio: raw.include_audio !== false && raw.includeAudio !== false,
  requiresFirstFrameContinuity: raw.requires_first_frame_continuity === true || raw.requiresFirstFrameContinuity === true,
  requiresLastFrameContinuity: raw.requires_last_frame_continuity === true || raw.requiresLastFrameContinuity === true,
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
    requires_first_frame_continuity: shot.requiresFirstFrameContinuity,
    requires_last_frame_continuity: shot.requiresLastFrameContinuity,
    scene_id: shot.sceneId || null,
    character_ids: shot.characterIds,
    prop_ids: shot.propIds,
    dialogue: shot.dialogue.map((line) => ({
      speaker_id: line.speakerId,
      text_zh: line.textZh,
      delivery_zh: line.deliveryZh,
      delivery_en: line.deliveryEn,
      voiceover: line.voiceover,
      ...(
        Number.isSafeInteger(line.startMs)
        && Number.isSafeInteger(line.endMs)
        && line.startMs! >= 0
        && line.endMs! > line.startMs!
          ? { start_ms: line.startMs, end_ms: line.endMs }
          : {}
      ),
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

/** Extracts the stable runtime/request error independently from field diagnostics. */
export const creativeSafeFailureCode = (metadata: unknown): string => {
  const body = objectValue(metadata)
  const rawCode = String(body.error_code ?? body.errorCode ?? '').trim().toLowerCase()
  return /^[a-z][a-z0-9_]{0,79}$/.test(rawCode) ? rawCode : ''
}

export const creativeProviderLabel = (provider: PromptProviderId | string = 'local_qwen'): string => (
  provider === 'deepseek_api' ? 'DeepSeek API' : '本地 Qwen'
)

export const creativeProviderFailureFallback = (
  provider: PromptProviderId | string = 'local_qwen',
): string => (
  `${creativeProviderLabel(provider)} 未返回可审核的结构化镜头。请一键重试整稿；若持续失败，`
  + '请减少本次镜数、缩短本次创作要求或按剧情边界拆分后重试。正式分镜未修改。'
)

const retryInstruction = (contextCurrent: boolean): string => (
  contextCurrent
    ? '请一键重试整稿；若持续失败，请减少本次镜数、缩短本次创作要求或按剧情边界拆分后重试。'
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
  provider: PromptProviderId | string = 'local_qwen',
): string | undefined => {
  const code = String(rawCode || '').trim()
  const retry = retryInstruction(contextCurrent)
  const unchanged = '正式分镜未修改。'
  const providerLabel = creativeProviderLabel(provider)
  if (code === 'creative_generation_confirmation_required') {
    return `${providerLabel} 本次镜数或权威剧本超过当前单批建议参考值；请确认是否继续完整生成。${unchanged}`
  }
  if (code === 'creative_dialogue_capacity_exceeded') {
    return `本段剧本中的权威对白超过所选通道当前配置的镜数承载量；请拆分本集片段后重新生成。${unchanged}`
  }
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
      ? '请不要按原设置重复提交：减少本次镜数，或按剧情与权威对白边界拆分后重新生成。'
      : retry
    return `${providerLabel} 已用完本次动态输出容量，返回内容不完整且没有可采用草稿。${action}${unchanged}`
  }
  if (['creative_repair_not_started', 'repair_not_started'].includes(code)) {
    const action = contextCurrent
      ? '请减少本次镜数、缩短本次创作要求，并按权威对白边界拆分后重新生成。'
      : retry
    return `首轮输出未通过结构校验，剩余执行时间或上下文不足，修复阶段未能启动。${action}${unchanged}`
  }
  if (code === 'creative_output_invalid_json') {
    return `${providerLabel} 返回的分镜草稿格式不完整，未生成可审核草稿。${retry}${unchanged}`
  }
  if (['creative_timeout', 'creative_parent_timeout'].includes(code)) {
    return `${providerLabel} 本次创作超过运行时限，未生成可审核草稿。${retry}${unchanged}`
  }
  if (code === 'creative_invalid_runtime_json') {
    return `本地 Qwen 运行时返回的响应格式异常，未生成可审核草稿。请刷新页面并检查“模型与能力”状态后再试；若持续出现，请重启本地 Qwen 服务。${unchanged}`
  }
  if (code === 'creative_runtime_exit') {
    return `本地 Qwen 进程异常退出，未生成可审核草稿。请检查“模型与能力”状态，确认没有视频生产或其他 Qwen 任务占用后再重试。${unchanged}`
  }
  if (code === 'creative_validation_failed') {
    return `${providerLabel} 输出未通过结构校验，未生成可审核草稿。${retry}${unchanged}`
  }
  if (code === 'creative_runtime_failed') {
    return `${providerLabel} 运行失败，未生成可审核草稿。${retry}${unchanged}`
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
  if (code === 'creative_storyboard_invalid_request') {
    return `分镜创作请求未通过安全协议校验；请刷新页面后重试。${unchanged}`
  }
  if (code === 'creative_storyboard_repair_invalid_json') {
    return `DeepSeek 修复后的分镜结构仍无法安全解析；请减少镜数或按剧情边界拆分后重试。${unchanged}`
  }
  if (code === 'creative_storyboard_repair_validation_failed') {
    return `DeepSeek 修复后的候选仍未通过结构校验；请根据问题位置缩短对应字段后重试。${unchanged}`
  }
  if ([
    'creative_storyboard_repair_output_budget_exhausted',
    'creative_storyboard_repair_incomplete_response',
    'creative_storyboard_repair_empty_output',
  ].includes(code)) {
    return `DeepSeek 未完整返回修复后的分镜结构。请减少镜数、缩短创作要求或按剧情边界拆分后重试。${unchanged}`
  }
  if (['deepseek_api_disabled', 'deepseek_api_misconfigured', 'deepseek_protocol_unavailable', 'deepseek_provider_unavailable'].includes(code)) {
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
  if (code === 'deepseek_api_failed') {
    return `DeepSeek API 本次请求失败；请稍后重试并检查服务端通道状态。${unchanged}`
  }
  return undefined
}

export interface CreativeGenerationConfirmationContext {
  targetShotCount: number
  recommendedMaxShots?: number
  batchShots?: number
  sourceCharCount?: number
  maxSourceChars?: number
  requiredDialogueCount?: number
  dialogueCapacity?: number
  maxDialoguePerShot?: number
  effectiveMaxDialoguePerShot?: number
}

/** Reads only the public integer policy values needed to recover from config drift. */
export const creativeGenerationConfirmationContext = (
  cause: unknown,
): CreativeGenerationConfirmationContext | undefined => {
  const candidate = objectValue(cause)
  const response = objectValue(candidate.response)
  const data = objectValue(response.data)
  const envelope = objectValue(data.error)
  const details = objectValue(envelope.details)
  const code = String(details.code || envelope.code || data.code || candidate.code || '')
  if (code !== 'creative_generation_confirmation_required') return undefined
  const safePositiveInteger = (value: unknown): number | undefined => {
    const number = Number(value)
    return Number.isSafeInteger(number) && number > 0 ? number : undefined
  }
  const targetShotCount = safePositiveInteger(details.target_shot_count ?? details.targetShotCount)
  if (!targetShotCount) return undefined
  const result: CreativeGenerationConfirmationContext = { targetShotCount }
  const recommendedMaxShots = safePositiveInteger(details.recommended_max_shots ?? details.recommendedMaxShots)
  const batchShots = safePositiveInteger(details.batch_shots ?? details.batchShots)
  const sourceCharCount = safePositiveInteger(details.source_char_count ?? details.sourceCharCount)
  const maxSourceChars = safePositiveInteger(details.max_source_chars ?? details.maxSourceChars)
  const safeNonNegativeInteger = (value: unknown): number | undefined => {
    const number = Number(value)
    return Number.isSafeInteger(number) && number >= 0 ? number : undefined
  }
  const requiredDialogueCount = safeNonNegativeInteger(
    details.required_dialogue_count ?? details.requiredDialogueCount,
  )
  const dialogueCapacity = safeNonNegativeInteger(
    details.dialogue_capacity ?? details.dialogueCapacity,
  )
  const maxDialoguePerShot = safePositiveInteger(
    details.max_dialogue_per_shot ?? details.maxDialoguePerShot,
  )
  const effectiveMaxDialoguePerShot = safePositiveInteger(
    details.effective_max_dialogue_per_shot ?? details.effectiveMaxDialoguePerShot,
  )
  if (recommendedMaxShots) result.recommendedMaxShots = recommendedMaxShots
  if (batchShots) result.batchShots = batchShots
  if (sourceCharCount) result.sourceCharCount = sourceCharCount
  if (maxSourceChars) result.maxSourceChars = maxSourceChars
  if (requiredDialogueCount !== undefined) result.requiredDialogueCount = requiredDialogueCount
  if (dialogueCapacity !== undefined) result.dialogueCapacity = dialogueCapacity
  if (maxDialoguePerShot) result.maxDialoguePerShot = maxDialoguePerShot
  if (effectiveMaxDialoguePerShot) result.effectiveMaxDialoguePerShot = effectiveMaxDialoguePerShot
  return result
}

/** Normalizes flat DRF errors, the unified error envelope and browser timeouts. */
export const creativeDraftRequestError = (
  cause: unknown,
  fallback: string,
  provider: PromptProviderId | string = 'local_qwen',
  timing?: Pick<PromptProviderCapability, 'runtimeTimeoutSeconds' | 'processTimeoutSeconds'>,
): string => {
  const candidate = objectValue(cause)
  const response = objectValue(candidate.response)
  const data = objectValue(response.data)
  const envelope = objectValue(data.error)
  const envelopeDetails = objectValue(envelope.details)
  const code = String(envelopeDetails.code || envelope.code || data.code || candidate.code || '')
  const rawMessage = String(candidate.message || '')
  if (code === 'creative_generation_confirmation_required') {
    const confirmation = creativeGenerationConfirmationContext(cause)
    if (confirmation) {
      const reasons = []
      if (
        confirmation.recommendedMaxShots
        && confirmation.targetShotCount > confirmation.recommendedMaxShots
      ) {
        reasons.push(`${confirmation.targetShotCount} 镜超过当前建议单次 ${confirmation.recommendedMaxShots} 镜`)
      }
      if (
        confirmation.sourceCharCount
        && confirmation.maxSourceChars
        && confirmation.sourceCharCount > confirmation.maxSourceChars
      ) {
        reasons.push(`权威剧本 ${confirmation.sourceCharCount} 字符超过单批 ${confirmation.maxSourceChars} 字符参考值`)
      }
      if (
        confirmation.requiredDialogueCount !== undefined
        && confirmation.dialogueCapacity !== undefined
        && confirmation.requiredDialogueCount > confirmation.dialogueCapacity
      ) {
        reasons.push(
          `${confirmation.requiredDialogueCount} 句权威对白超过当前 ${confirmation.dialogueCapacity} 句建议容量`,
        )
      }
      if (reasons.length) {
        const batchNote = confirmation.batchShots
          ? `，当前单次批次能力参考为 ${confirmation.batchShots} 镜`
          : ''
        return `${creativeProviderLabel(provider)} 本次请求${reasons.join('，')}${batchNote}。系统不会截断镜数或剧本；请在页面确认后按原镜数和完整剧本继续，任务预计耗时更长。正式分镜未修改。`
      }
    }
  }
  if (code === 'creative_dialogue_capacity_exceeded') {
    const required = Number(envelopeDetails.required_dialogue_count)
    const shots = Number(envelopeDetails.target_shot_count)
    const capacity = Number(envelopeDetails.dialogue_capacity)
    if ([required, shots, capacity].every((value) => Number.isSafeInteger(value) && value >= 0)) {
      return `本段剧本包含 ${required} 句必须保留的原文对白；所选通道对 ${shots} 个镜头的当前建议容量为 ${capacity} 句。这是可配置的创作提醒，不会裁剪模型已返回的草稿；请确认后继续，或按需调整配置。正式分镜未修改。`
    }
  }
  const recovery = creativeFailureRecovery(code, true, provider)
  if (recovery) return recovery
  const timedOut = code === 'ECONNABORTED' || code === 'ETIMEDOUT' || /timeout|timed out/i.test(rawMessage)
  if (timedOut) {
    const safeSeconds = (value: unknown): number | undefined => {
      const seconds = Number(value)
      return Number.isFinite(seconds) && seconds > 0 ? seconds : undefined
    }
    const runtimeSeconds = safeSeconds(timing?.runtimeTimeoutSeconds)
    const processSeconds = safeSeconds(timing?.processTimeoutSeconds)
    const configuredTiming = [
      runtimeSeconds ? `模型运行时限 ${runtimeSeconds} 秒` : '',
      processSeconds ? `服务进程时限 ${processSeconds} 秒` : '',
    ].filter(Boolean).join('、')
    const timingNote = configuredTiming ? `所选通道当前配置为${configuredTiming}。` : ''
    return `浏览器等待 ${creativeProviderLabel(provider)} 时请求超时。${timingNote}这不等于服务端已取消。系统已刷新版本记录：若出现“生成失败”版本，可点击“一键重试整稿”；若没有，请先检查服务连接或刷新后再重试。正式分镜未修改。`
  }
  // Unknown backend details are intentionally not rendered: they may contain
  // transport, parser, or model internals. Callers provide a safe fallback.
  return fallback
}

/** Local editor checks are advisory; the server remains the apply gate. */
export const creativeDraftEditorWarnings = (
  data: CreativeDraftData,
  policy?: Pick<
    PromptProviderCapability,
    'minShotDurationMs' | 'maxShotDurationMs'
    | 'recommendedNarrativeEnChars' | 'recommendedActionEnChars'
  >,
): string[] => {
  const warnings: string[] = []
  if (!data.shots.length) warnings.push('草稿没有分镜。')
  for (const shot of data.shots) {
    const label = `镜头 ${shot.order}`
    const minimumDurationMs = policy?.minShotDurationMs
    const maximumDurationMs = policy?.maxShotDurationMs
    if (minimumDurationMs !== undefined && maximumDurationMs !== undefined
      && (shot.durationMs < minimumDurationMs || shot.durationMs > maximumDurationMs)) {
      warnings.push(`${label} 时长必须为 ${minimumDurationMs / 1000}–${maximumDurationMs / 1000} 秒。`)
    }
    if (!shot.narrativeZh.trim() || !shot.narrativeEn.trim()) warnings.push(`${label} 缺少中英叙事结构。`)
    if (policy?.recommendedNarrativeEnChars !== undefined
      && shot.narrativeEn.length > policy.recommendedNarrativeEnChars) {
      warnings.push(`${label} 英文叙事结构超过 ${policy.recommendedNarrativeEnChars} 字符，建议人工核对并按需精简；仍可保存为草稿。`)
    }
    if (policy?.recommendedActionEnChars !== undefined
      && shot.actionEn.length > policy.recommendedActionEnChars) {
      warnings.push(`${label} 英文主动作超过 ${policy.recommendedActionEnChars} 字符，建议人工核对并按需精简；仍可保存为草稿。`)
    }
    if (!shot.actionZh.trim() || !shot.actionEn.trim()) warnings.push(`${label} 缺少中英主动作。`)
    if (!shot.cameraZh.trim() || !shot.cameraEn.trim()) warnings.push(`${label} 缺少中英单一运镜。`)
    if (!shot.continuityZh.trim() || !shot.continuityEn.trim()) warnings.push(`${label} 缺少中英连续性进出状态。`)
    if (shot.dialogue.some((line) => !line.textZh.trim())) warnings.push(`${label} 存在空对白正文。`)
    if (!shot.includeAudio && shot.dialogue.length) warnings.push(`${label} 选择了静音但仍含锁定对白；可保存，请在应用前决定保留音频或重生此镜。`)
  }
  return warnings
}
