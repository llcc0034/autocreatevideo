import type {
  ShotDetailDraft,
  ShotDetailDraftApplyResult,
  ShotDetailDraftData,
  ShotDetailDraftReviewIssue,
  ShotDetailDraftReviewSeverity,
  ShotDetailDraftReviewSummary,
  ShotDetailDraftValidation,
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
  if (issue.severity === 'requires_edit' || /unknown|missing_(?:entity|speaker)|invalid_(?:entity|reference)|foreign_key/.test(code)) {
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

export const shotDetailDraftEditorWarnings = (data: ShotDetailDraftData): string[] => {
  const warnings: string[] = []
  if (!data.detailZh.trim()) warnings.push('缺少中文高细节说明；可补充后再确认。')
  if (!data.detailEn.trim()) warnings.push('缺少英文高细节说明；可人工填写或核对后再确认。')
  if (data.detailZh.length > 500) warnings.push('中文高细节说明超过 500 字。')
  if (data.detailEn.length > 750) warnings.push('英文高细节说明超过 750 字符。')
  return warnings
}

/** Normalize the small, resource-aware error vocabulary used by the detail API. */
export const shotDetailDraftRequestError = (cause: unknown, fallback: string): string => {
  const candidate = objectValue(cause)
  const response = objectValue(candidate.response)
  const body = objectValue(response.data)
  const envelope = objectValue(body.error)
  const code = text(envelope.code || body.code || candidate.code)
  if (['creative_resource_busy', 'shot_detail_resource_busy'].includes(code)) {
    return '视频生产或其他本地 Qwen 请求正在占用资源；高细节候选尚未生成，当前正式分镜未修改。'
  }
  if (['creative_runtime_unavailable', 'shot_detail_runtime_unavailable'].includes(code)) {
    return '本地 Qwen CUDA 运行环境不可用或模型未就绪；当前正式分镜未修改。'
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
    return '本地 Qwen 未返回可编辑的高细节候选；请重新扩写。当前正式镜头未修改。'
  }
  if (code === 'ECONNABORTED' || /timeout|timed out/i.test(text(candidate.message))) {
    return '浏览器等待高细节候选超时；服务端可能仍在保存审计记录。请刷新当前镜头查看版本，正式分镜未修改。'
  }
  // Backend detail may contain candidate text, parser locations or process
  // diagnostics. The panel intentionally renders only this caller-provided
  // recovery copy for unknown failures.
  return fallback
}
