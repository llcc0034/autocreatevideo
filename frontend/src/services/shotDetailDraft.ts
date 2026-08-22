import type {
  ShotDetailDraft,
  ShotDetailDraftApplyResult,
  ShotDetailDraftData,
  ShotDetailDraftReviewIssue,
  ShotDetailDraftReviewSeverity,
  ShotDetailDraftReviewSummary,
  ShotDetailDraftValidation,
  ShotDetailDraftBatch,
  ShotDetailDraftBatchDetail,
  ShotDetailDraftBatchItem,
  ShotDetailDraftBatchApplyResult,
  ShotDetailDraftAppliedShot,
  ShotHighDetailPolicy,
  PromptProviderId,
} from '@/types'

type Raw = Record<string, any>

const objectValue = (value: unknown): Raw => (
  value && typeof value === 'object' ? value as Raw : {}
)
const text = (value: unknown): string => String(value ?? '')
const relationId = (value: unknown): string => String(
  value && typeof value === 'object' ? (value as Raw).id || '' : value || '',
)
const list = (value: unknown): string[] => Array.isArray(value)
  ? value.map(text).map((value) => value.trim()).filter(Boolean)
  : []
const objectList = (value: unknown): Raw[] => Array.isArray(value)
  ? value.filter((item): item is Raw => Boolean(item && typeof item === 'object'))
  : []

const reviewSeverity = (value: unknown): ShotDetailDraftReviewSeverity => {
  const normalized = text(value).trim().toLowerCase()
  if (normalized === 'requires_edit') return 'requires_edit'
  if (normalized === 'risk' || normalized === 'blocking' || normalized === 'error') return 'risk'
  return 'warning'
}

const safeSuggestion = (value: unknown): string | undefined => {
  const suggestion = text(value).replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim()
  return suggestion && /[\u3400-\u4dbf\u4e00-\u9fff]/.test(suggestion) ? suggestion.slice(0, 240) : undefined
}

const detailReviewField = (value: unknown): ShotDetailDraftReviewIssue['field'] | undefined => {
  const path = text(value).toLowerCase().replace(/[\[\]./]+/g, '_')
  if (/(?:detail_zh|detailzh)/.test(path)) return 'detailZh'
  if (/(?:detail_en|detailen)/.test(path)) return 'detailEn'
  return undefined
}

const adaptReviewIssues = (raw: unknown): ShotDetailDraftReviewIssue[] => objectList(raw).map((item) => {
  const path = text(item.path ?? item.field_path ?? item.fieldPath).trim().slice(0, 180)
  const field = detailReviewField(path)
  return {
    code: text(item.code || 'review_required').trim().slice(0, 80) || 'review_required',
    severity: reviewSeverity(item.severity),
    path,
    field,
    language: field === 'detailZh' ? 'zh' : field === 'detailEn' ? 'en' : undefined,
    suggestionZh: safeSuggestion(item.suggestion_zh ?? item.suggestionZh),
  }
})

const adaptReviewSummary = (
  raw: unknown,
  issues: ShotDetailDraftReviewIssue[],
): ShotDetailDraftReviewSummary => {
  const body = objectValue(raw)
  const issueCount = Number(body.issue_count ?? body.issueCount)
  const riskCount = Number(body.risk_count ?? body.riskCount)
  const requiresEditCount = Number(body.requires_edit_count ?? body.requiresEditCount)
  return {
    issueCount: Number.isFinite(issueCount) ? Math.max(issues.length, issueCount) : issues.length,
    riskCount: Number.isFinite(riskCount) ? Math.max(0, riskCount)
      : issues.filter((item) => item.severity === 'risk').length,
    requiresEditCount: Number.isFinite(requiresEditCount) ? Math.max(0, requiresEditCount)
      : issues.filter((item) => item.severity === 'requires_edit').length,
    status: issues.length || text(body.status) === 'needs_review' ? 'needs_review' : 'ready',
  }
}

/** Stable operator copy keyed only by finite public issue codes. */
export const shotDetailDraftReviewIssueTitle = (issue: ShotDetailDraftReviewIssue): string => {
  const code = issue.code.toLowerCase()
  if (/model_output_protocol_validation_failed/.test(code)) {
    return '模型已返回可编辑正文，但结构协议未完全通过；请修改并保存校验后再应用'
  }
  if (/unknown|missing_(?:entity|speaker)|invalid_(?:entity|reference)|foreign_key/.test(code)) {
    return '关联的素材或说话人无效，正式应用前需要替换或删除'
  }
  if (/bilateral|both_sides|encirc/.test(code)) return '双侧动作的中英文表达可能不一致'
  if (/direction|handed|body_side|left_right|spatial/.test(code)) return '中英文方位、左右侧或动作归属可能不一致'
  if (/numeric|number|quantity/.test(code)) return '中英文数字或数量可能不一致'
  if (/canonical|glossary|term/.test(code)) return '英文可能没有使用已确认的专名或术语'
  if (/cjk|english|language|translation/.test(code)) return '英文说明可能仍含非英文内容，需人工核对'
  if (/length|too_long|budget/.test(code)) return '说明长度接近或超过建议范围，建议保留最关键画面信息'
  if (/missing|empty|required/.test(code)) return '候选内容不完整，建议补充后再使用'
  if (/action|camera|motion|composition/.test(code)) return '镜头运动或动作画面描述可能需要人工核对'
  if (issue.severity === 'requires_edit') return '该候选需要先人工修正并保存校验，才能正式应用'
  return '该候选可能与当前镜头事实或中英文对应关系不完全一致'
}

export const adaptShotDetailDraftData = (raw: unknown): ShotDetailDraftData => {
  const body = objectValue(raw)
  return {
    schemaVersion: 1,
    detailZh: text(body.detail_zh ?? body.detailZh),
    detailEn: text(body.detail_en ?? body.detailEn),
  }
}

const adaptValidation = (raw: unknown): ShotDetailDraftValidation => {
  const body = objectValue(raw)
  const status = ['passed', 'failed', 'pending', 'ready', 'needs_review'].includes(String(body.status))
    ? body.status as ShotDetailDraftValidation['status']
    : 'pending'
  return { status, errors: list(body.errors), warnings: list(body.warnings) }
}

export const adaptShotDetailDraft = (raw: Raw): ShotDetailDraft => {
  const validationBody = objectValue(raw.validation)
  const reviewIssues = adaptReviewIssues(
    raw.review_issues ?? raw.reviewIssues ?? validationBody.review_issues ?? validationBody.reviewIssues ?? validationBody.issues,
  )
  return {
  id: text(raw.id),
  projectId: relationId(raw.project_id ?? raw.project),
  shotId: relationId(raw.shot_id ?? raw.shot),
  status: ['draft', 'confirmed', 'applied', 'superseded', 'failed'].includes(String(raw.status))
    ? raw.status : 'failed',
  revision: Number(raw.revision || 1),
  generationNo: Number(raw.generation_no ?? raw.generationNo ?? 1),
  parentDraftId: relationId(raw.parent_draft ?? raw.parentDraft) || undefined,
  contextHash: text(raw.context_hash ?? raw.contextHash),
  contextCurrent: raw.context_current !== false && raw.contextCurrent !== false,
  variationNote: text(raw.variation_note ?? raw.variationNote),
  draftData: adaptShotDetailDraftData(raw.draft_data ?? raw.draftData),
  validation: adaptValidation(raw.validation),
  reviewIssues,
  reviewSummary: adaptReviewSummary(
    raw.review_summary ?? raw.reviewSummary ?? validationBody.summary,
    reviewIssues,
  ),
  warnings: list(raw.warnings),
  errorMessage: text(raw.error_message ?? raw.errorMessage) || undefined,
  provider: text(raw.provider) || undefined,
  model: text(raw.model) || undefined,
  runtimeMetadata: raw.runtime_metadata && typeof raw.runtime_metadata === 'object'
    ? raw.runtime_metadata : undefined,
  createdAt: text(raw.created_at ?? raw.createdAt) || undefined,
  updatedAt: text(raw.updated_at ?? raw.updatedAt) || undefined,
  confirmedAt: text(raw.confirmed_at ?? raw.confirmedAt) || undefined,
  appliedAt: text(raw.applied_at ?? raw.appliedAt) || undefined,
  canEdit: raw.can_edit === true || raw.canEdit === true,
  canRegenerate: raw.can_regenerate === true || raw.canRegenerate === true,
  canConfirm: raw.can_confirm === true || raw.canConfirm === true,
  canApply: raw.can_apply === true || raw.canApply === true,
}}

export const shotDetailDraftDataPayload = (data: ShotDetailDraftData) => ({
  schema_version: 1,
  detail_zh: data.detailZh,
  detail_en: data.detailEn,
})

export const adaptShotDetailDraftApplyResult = (raw: Raw): ShotDetailDraftApplyResult => {
  const applied = objectValue(raw.applied_shot ?? raw.appliedShot)
  return {
    draft: adaptShotDetailDraft(objectValue(raw.draft)),
    appliedShot: {
      id: text(applied.id),
      detailedDescription: text(applied.detailed_description ?? applied.detailedDescription),
      ...(applied.metadata && typeof applied.metadata === 'object'
        ? { metadata: applied.metadata as Record<string, unknown> }
        : {}),
    },
    translationWritten: raw.translation_written === true || raw.translationWritten === true,
  }
}

export const shotDetailDraftIsEditable = (draft?: ShotDetailDraft | null): boolean => Boolean(
  draft?.canEdit && ['draft', 'failed'].includes(draft.status) && draft.contextCurrent,
)

export const shotDetailDraftEditorWarnings = (
  data: ShotDetailDraftData,
  policy?: Pick<ShotHighDetailPolicy,
    'recommendedZhChars' | 'recommendedEnChars' | 'transportZhMaxChars' | 'transportEnMaxChars'>,
): string[] => {
  const warnings: string[] = []
  if (!data.detailZh.trim()) warnings.push('缺少中文高细节说明；可补充后再确认。')
  if (!data.detailEn.trim()) warnings.push('缺少英文高细节说明；可人工填写或核对后再确认。')
  if (policy?.transportZhMaxChars && data.detailZh.length > policy.transportZhMaxChars) {
    warnings.push(`中文高细节说明超过应用前配置阈值 ${policy.transportZhMaxChars} 字；候选会保留，请精简后保存校验。`)
  } else if (policy?.recommendedZhChars && data.detailZh.length > policy.recommendedZhChars) {
    warnings.push(`中文高细节说明超过当前通道建议长度 ${policy.recommendedZhChars} 字，可人工精简。`)
  }
  if (policy?.transportEnMaxChars && data.detailEn.length > policy.transportEnMaxChars) {
    warnings.push(`英文高细节说明超过应用前配置阈值 ${policy.transportEnMaxChars} 字符；候选会保留，请精简后保存校验。`)
  } else if (policy?.recommendedEnChars && data.detailEn.length > policy.recommendedEnChars) {
    warnings.push(`英文高细节说明超过当前通道建议长度 ${policy.recommendedEnChars} 字符，可人工精简。`)
  }
  return warnings
}

/** Normalize the small, resource-aware error vocabulary used by the detail API. */
export const shotDetailDraftRequestError = (
  cause: unknown,
  fallback: string,
  provider?: PromptProviderId,
): string => {
  const candidate = objectValue(cause)
  const response = objectValue(candidate.response)
  const body = objectValue(response.data)
  const envelope = objectValue(body.error)
  const code = text(envelope.code || body.code || candidate.code)
  const providerLabel = provider === 'deepseek_api' ? 'DeepSeek API' : provider === 'local_qwen' ? '本地 Qwen' : '所选扩写通道'
  if (['creative_resource_busy', 'shot_detail_resource_busy'].includes(code)) {
    return '扩写资源暂时占用；单镜可稍后重试，整集扩写会入队自动等待。当前正式分镜未修改。'
  }
  if (['creative_runtime_unavailable', 'shot_detail_runtime_unavailable'].includes(code)) {
    return `${providerLabel} 运行环境不可用或尚未就绪；当前正式分镜未修改。`
  }
  if (['creative_draft_conflict', 'shot_detail_draft_conflict'].includes(code)) {
    return '当前镜头、Bible 或上下文已变化；请刷新后基于最新镜头重新生成。'
  }
  if (['output_budget_exhausted', 'shot_detail_output_budget_exhausted'].includes(code)) {
    return '本次逐镜扩写已达到输出容量上限，未应用到正式镜头。请缩短补充要求后重新生成。'
  }
  if (code === 'shot_detail_review_acknowledgement_required') {
    return '候选仍有人工核验项；请阅读风险清单并勾选“已人工核验”后再继续。当前正式镜头未修改。'
  }
  if (code === 'shot_detail_review_requires_edit') {
    return '候选仍引用无效素材或说话人；可继续编辑，但正式应用前必须修正。当前正式镜头未修改。'
  }
  if (['shot_detail_output_invalid_json', 'shot_detail_validation_failed'].includes(code)) {
    return `${providerLabel} 未返回可编辑的高细节候选；请重新扩写。当前正式镜头未修改。`
  }
  if (code === 'ECONNABORTED' || /timeout|timed out/i.test(text(candidate.message))) {
    return '浏览器等待高细节候选超时；服务端可能仍在保存审计记录。请刷新当前镜头查看版本，正式分镜未修改。'
  }
  // Backend detail may contain candidate text, parser locations or process
  // diagnostics. The panel intentionally renders only this caller-provided
  // recovery copy for unknown failures.
  return fallback
}

const batchStatus = (value: unknown): ShotDetailDraftBatch['generationStatus'] => {
  const normalized = text(value)
  return ['queued', 'running', 'completed', 'completed_with_errors', 'failed', 'cancelled'].includes(normalized)
    ? normalized as ShotDetailDraftBatch['generationStatus'] : 'failed'
}

export const adaptShotDetailDraftBatch = (raw: Raw): ShotDetailDraftBatch => ({
  id: text(raw.id),
  projectId: relationId(raw.project),
  episodeId: relationId(raw.episode),
  requestId: text(raw.request_id ?? raw.requestId),
  provider: text(raw.provider) === 'deepseek_api' ? 'deepseek_api' : 'local_qwen',
  variationNote: text(raw.variation_note ?? raw.variationNote),
  shotVersionKey: text(raw.shot_version_key ?? raw.shotVersionKey),
  shotVersionLabel: text(raw.shot_version_label ?? raw.shotVersionLabel),
  versionScopeStatus: ['resolved', 'legacy_mixed', 'legacy_partial', 'legacy_unknown'].includes(text(raw.version_scope_status ?? raw.versionScopeStatus))
    ? text(raw.version_scope_status ?? raw.versionScopeStatus) as ShotDetailDraftBatch['versionScopeStatus']
    : 'legacy_unknown',
  generationStatus: batchStatus(raw.generation_status ?? raw.generationStatus),
  applyStatus: text(raw.apply_status ?? raw.applyStatus) === 'complete' ? 'complete' : 'not_started',
  revision: Number(raw.revision || 1),
  totalCount: Number(raw.total_count ?? raw.totalCount ?? 0),
  pendingCount: Number(raw.pending_count ?? raw.pendingCount ?? 0),
  succeededCount: Number(raw.succeeded_count ?? raw.succeededCount ?? 0),
  failedCount: Number(raw.failed_count ?? raw.failedCount ?? 0),
  staleCount: Number(raw.stale_count ?? raw.staleCount ?? 0),
  skippedCount: Number(raw.skipped_count ?? raw.skippedCount ?? 0),
  appliedCount: Number(raw.applied_count ?? raw.appliedCount ?? 0),
  policySnapshot: objectValue(raw.policy_snapshot ?? raw.policySnapshot),
  createdAt: text(raw.created_at ?? raw.createdAt) || undefined,
  updatedAt: text(raw.updated_at ?? raw.updatedAt) || undefined,
  startedAt: text(raw.started_at ?? raw.startedAt) || undefined,
  finishedAt: text(raw.finished_at ?? raw.finishedAt) || undefined,
})

export const adaptShotDetailDraftBatchItem = (raw: Raw): ShotDetailDraftBatchItem => {
  const before = objectValue(raw.before_snapshot ?? raw.beforeSnapshot)
  const draftRaw = objectValue(raw.draft)
  const draft = Object.keys(draftRaw).length ? adaptShotDetailDraft({
    ...draftRaw,
    project: '',
    context_current: raw.status !== 'stale',
  }) : undefined
  return {
    id: text(raw.id), batchId: relationId(raw.batch), shotId: relationId(raw.shot),
    ordinal: Number(raw.ordinal || 0),
    beforeSnapshot: { targetShot: objectValue(before.target_shot ?? before.targetShot ?? before.shot) },
    draft,
    status: ['pending', 'running', 'succeeded', 'failed', 'stale', 'skipped'].includes(text(raw.status))
      ? text(raw.status) as ShotDetailDraftBatchItem['status'] : 'failed',
    attempts: Number(raw.attempts || 0), maxAttempts: Number(raw.max_attempts ?? raw.maxAttempts ?? 0),
    errorCode: text(raw.error_code ?? raw.errorCode) || undefined,
    errorMessage: text(raw.error_message ?? raw.errorMessage) || undefined,
    createdAt: text(raw.created_at ?? raw.createdAt) || undefined,
    updatedAt: text(raw.updated_at ?? raw.updatedAt) || undefined,
  }
}

export const adaptShotDetailDraftBatchDetail = (raw: Raw): ShotDetailDraftBatchDetail => {
  const pagination = objectValue(raw.pagination)
  return {
    batch: adaptShotDetailDraftBatch(objectValue(raw.batch)),
    items: objectList(raw.items).map(adaptShotDetailDraftBatchItem),
    appliedShots: adaptShotDetailAppliedShots(raw.applied_shots ?? raw.appliedShots),
    translationWritten: raw.translation_written === true || raw.translationWritten === true,
    pagination: {
      count: Number(pagination.count || 0),
      next: text(pagination.next) || undefined,
      previous: text(pagination.previous) || undefined,
    },
  }
}

const adaptShotDetailAppliedShots = (raw: unknown): ShotDetailDraftAppliedShot[] => (
  objectList(raw).map((value) => ({
    id: text(value.id),
    detailedDescription: text(value.detailed_description ?? value.detailedDescription),
    ...(value.metadata && typeof value.metadata === 'object'
      ? { metadata: value.metadata as Record<string, unknown> }
      : {}),
  }))
)

export const adaptShotDetailDraftBatchApplyResult = (raw: Raw): ShotDetailDraftBatchApplyResult => ({
  batch: adaptShotDetailDraftBatch(objectValue(raw.batch)),
  items: objectList(raw.items).map(adaptShotDetailDraftBatchItem),
  appliedShots: adaptShotDetailAppliedShots(raw.applied_shots ?? raw.appliedShots),
  translationWritten: raw.translation_written === true || raw.translationWritten === true,
})
