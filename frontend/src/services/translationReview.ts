import type {
  PromptTranslationEntry,
  PromptTranslationField,
  TranslationBackendStatus,
  TranslationReviewStatus,
} from '../types'

type Raw = Record<string, unknown>

const rawRecord = (value: unknown): Raw => value && typeof value === 'object' ? value as Raw : {}

/** Convert translation-review API conflicts into operator actions without exposing internal ids. */
export const translationReviewRequestError = (cause: unknown, fallback: string): string => {
  const response = rawRecord(rawRecord(cause).response)
  const data = rawRecord(response.data)
  const apiError = rawRecord(data.error)
  const details = rawRecord(apiError.details)
  const code = String(details.code || apiError.code || '')

  if (code === 'canonical_target_missing') {
    const rows = Array.isArray(details.missing_mappings)
      ? details.missing_mappings.map(rawRecord)
      : []
    const glossaryTerms = rows
      .filter((row) => String(row.requires_glossary || '').toLowerCase() === 'true')
      .map((row) => String(row.source_term || '').trim())
      .filter(Boolean)
    const configuredMappings = rows
      .map((row) => ({
        source: String(row.source_term || '').trim(),
        target: String(row.canonical_target || '').trim(),
      }))
      .filter((row) => row.source && row.target)
    const messages: string[] = []
    if (glossaryTerms.length) {
      messages.push(`本次翻译要求源文中的“${glossaryTerms.join('”“')}”提供英文译名。这是本次翻译的旧术语约束，当前工作台已停止名称翻译；候选保留，不能视为已确认。原名称生产不以此为前提。`)
    }
    if (configuredMappings.length) {
      messages.push(`修改后的英文缺少必须逐字保留的 canonical 名称：${configuredMappings.map((row) => `${row.source} = ${row.target}`).join('；')}。请补回后再确认。`)
    }
    return messages.join(' ') || '修改后的英文缺少必须保留的 canonical 名称，请核对全剧视觉规则后重试。'
  }
  if (code === 'translation_source_conflict') return '项目、名称说明、正式术语或候选修订已变化，请重新读取并核对；未覆盖已有内容。'
  if (code === 'context_changed') return '中文源文或 canonical 术语上下文已变化，请先重新翻译生成候选，再核对并确认。'
  if (code === 'source_changed') return '中文源文已变化，请刷新页面并重新翻译生成候选后再确认。'
  if (code === 'revision_conflict') return '该译文已被其他操作更新，请刷新后基于最新修订继续编辑。'
  if (code === 'canonical_target_collision') return '本次翻译的既有英文名称映射冲突，未确认候选。当前工作台已停止名称翻译；原名称生产不依赖该译名。'

  const message = cause instanceof Error ? cause.message.trim() : ''
  if (message && !/\b(?:entry_id|field_key|scope_id)\s*:/i.test(message)) return message
  return fallback
}

/** Prevents an older translations GET from overwriting a later mutation response. */
export const createTranslationLoadGate = () => {
  let sequence = 0
  return {
    begin: () => ++sequence,
    invalidate: () => { sequence += 1 },
    isCurrent: (requestSequence: number) => requestSequence === sequence,
  }
}

const relationId = (value: unknown) => String(
  value && typeof value === 'object' ? (value as Raw).id || '' : value || '',
)

export const adaptTranslationEntry = (raw: Raw): PromptTranslationEntry => {
  const rawStatus = String(raw.status || '')
  const status: TranslationBackendStatus = ['draft_translation', 'confirmed', 'needs_retranslation', 'failed'].includes(rawStatus)
    ? rawStatus as TranslationBackendStatus
    : 'failed'
  return {
    id: String(raw.id || ''),
    projectId: relationId(raw.project_id ?? raw.project) || undefined,
    fieldKey: String(raw.field_key || ''),
    scopeType: ['project', 'episode', 'shot', 'character', 'scene', 'prop'].includes(String(raw.scope_type || ''))
      ? String(raw.scope_type) as PromptTranslationEntry['scopeType']
      : undefined,
    scopeId: relationId(raw.scope_id) || undefined,
    sourceText: String(raw.source_text || ''),
    translatedText: String(raw.translated_text || ''),
    status,
    revision: Number(raw.revision || 0),
    confirmedAt: String(raw.confirmed_at || '') || undefined,
    updatedAt: String(raw.updated_at || '') || undefined,
    provider: String(raw.provider || '') || undefined,
    model: String(raw.model || '') || undefined,
    error: String(raw.error || raw.error_message || '') || undefined,
    runtimeMetadata: raw.runtime_metadata && typeof raw.runtime_metadata === 'object'
      ? raw.runtime_metadata as Record<string, unknown>
      : undefined,
    contextCurrent: raw.context_current !== false,
    confirmationTarget: raw.confirmation_target === 'canonical_glossary' || raw.confirmation_target === 'field_review' ? raw.confirmation_target : undefined,
    canonicalValue: raw.canonical_value === null ? null : typeof raw.canonical_value === 'string' ? raw.canonical_value : undefined,
  }
}

export const translationFieldIdentity = (field: Pick<PromptTranslationField, 'fieldKey' | 'scopeType' | 'scopeId'>) => (
  `${field.scopeType}:${field.scopeId}:${field.fieldKey}`
)

export const translationEntryIdentity = (entry: PromptTranslationEntry) => (
  `${entry.scopeType || ''}:${entry.scopeId || ''}:${entry.fieldKey}`
)

/**
 * Reconcile server-side translation candidates with the operator's textarea
 * drafts. A passive refresh must not erase an unsaved edit while the server
 * revision is unchanged, but a new revision (or an explicit action response)
 * must immediately expose the candidate returned by the backend.
 */
export const reconcileTranslationDrafts = (
  currentDrafts: Record<string, string>,
  previousEntries: PromptTranslationEntry[],
  nextEntries: PromptTranslationEntry[],
  forceEntryIds: Iterable<string> = [],
): Record<string, string> => {
  const reconciled = { ...currentDrafts }
  const previousById = new Map(previousEntries.map((entry) => [entry.id, entry]))
  const forced = new Set(forceEntryIds)

  for (const entry of nextEntries) {
    const previous = previousById.get(entry.id)
    const hasDraft = Object.prototype.hasOwnProperty.call(currentDrafts, entry.id)
    if (forced.has(entry.id) || !hasDraft || !previous || previous.revision !== entry.revision) {
      reconciled[entry.id] = entry.translatedText
    }
  }
  return reconciled
}

export const translationReviewStatus = (
  field: PromptTranslationField,
  entry: PromptTranslationEntry | undefined,
  translating = false,
): TranslationReviewStatus => {
  if (translating) return 'translating'
  if (!entry) return 'untranslated'
  if (entry.sourceText !== field.sourceText || entry.contextCurrent === false || entry.status === 'needs_retranslation') return 'source_changed'
  if (entry.status === 'confirmed') return 'confirmed'
  if (entry.status === 'draft_translation') return 'pending_review'
  return 'failed'
}
