import type {
  CanonicalGlossaryEnsureResult,
  CanonicalGlossaryScope,
  PromptTranslationEntry,
  PromptTranslationField,
  PromptTranslationPromotedField,
  Shot,
  ShotTranslationPromotionResult,
  TranslationBackendStatus,
  TranslationReviewStatus,
} from '../types'
import { formatDialogueLines, parseDialogueText, type DialogueCharacterOption } from './dialogue.ts'

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
      messages.push(`源文中的“${glossaryTerms.join('”“')}”尚未配置英文 canonical 名称。请先到“项目总览 → 全剧视觉规则”补全术语，然后重新翻译并确认。`)
    }
    if (configuredMappings.length) {
      messages.push(`修改后的英文缺少必须逐字保留的 canonical 名称：${configuredMappings.map((row) => `${row.source} = ${row.target}`).join('；')}。请补回后再确认。`)
    }
    return messages.join(' ') || '修改后的英文缺少必须保留的 canonical 名称，请核对全剧视觉规则后重试。'
  }
  if (code === 'context_changed') return '中文源文或 canonical 术语上下文已变化，请先重新翻译生成候选，再核对并确认。'
  if (code === 'source_changed') return '中文源文已变化，请刷新页面并重新翻译生成候选后再确认。'
  if (code === 'revision_conflict') return '该译文已被其他操作更新，请刷新后基于最新修订继续编辑。'
  if (code === 'canonical_target_collision') return 'canonical 英文名称存在冲突，请先到“项目总览 → 全剧视觉规则”修正后再确认。'

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

export const adaptCanonicalGlossaryEnsureResult = (payload: unknown): CanonicalGlossaryEnsureResult => {
  const envelope = payload && typeof payload === 'object' ? payload as Raw : {}
  const raw = envelope.data && typeof envelope.data === 'object' ? envelope.data as Raw : envelope
  const summary = raw.summary && typeof raw.summary === 'object' ? raw.summary as Raw : {}
  const visualSummary = raw.visual_summary && typeof raw.visual_summary === 'object' ? raw.visual_summary as Raw : {}
  const translationGlossary = raw.translation_glossary && typeof raw.translation_glossary === 'object'
    ? Object.fromEntries(Object.entries(raw.translation_glossary as Raw).filter(([source, target]) => source.trim() && String(target).trim()).map(([source, target]) => [source, String(target)]))
    : {}
  return {
    projectId: String(raw.project_id || ''),
    updatedAt: String(raw.updated_at || '') || undefined,
    translationGlossary,
    summary: {
      requested: Number(summary.requested || 0), generated: Number(summary.generated || 0),
      reused: Number(summary.reused || 0), conflict: Number(summary.conflict || 0), failed: Number(summary.failed || 0),
    },
    entries: Array.isArray(raw.entries) ? raw.entries.map((value) => {
      const entry = value && typeof value === 'object' ? value as Raw : {}
      return {
        scopeType: String(entry.scope_type || '') as CanonicalGlossaryScope,
        scopeId: String(entry.scope_id || ''), name: String(entry.name || ''),
        canonicalEnglish: String(entry.canonical_english || ''),
        status: String(entry.status || 'failed') as CanonicalGlossaryEnsureResult['entries'][number]['status'],
        origin: String(entry.origin || 'none') as CanonicalGlossaryEnsureResult['entries'][number]['origin'],
        retryable: entry.retryable === true,
        errorCode: String(entry.error_code || '') || undefined,
        message: String(entry.message || '') || undefined,
      }
    }) : [],
    visualSummary: {
      requested: Number(visualSummary.requested || 0), generated: Number(visualSummary.generated || 0),
      reused: Number(visualSummary.reused || 0), notRequired: Number(visualSummary.not_required || 0),
      conflict: Number(visualSummary.conflict || 0), failed: Number(visualSummary.failed || 0),
    },
    visualTranslations: Array.isArray(raw.visual_translations) ? raw.visual_translations.map((value) => {
      const row = value && typeof value === 'object' ? value as Raw : {}
      const translationId = String(row.translation_id || '')
      const translationStatus = String(row.translation_status || '') as TranslationBackendStatus
      const translation = translationId ? adaptTranslationEntry({
        ...row,
        id: translationId,
        status: translationStatus,
        error_message: String(row.message || ''),
      }) : undefined
      return {
        scopeType: String(row.scope_type || '') as CanonicalGlossaryScope,
        scopeId: String(row.scope_id || ''), fieldKey: String(row.field_key || ''),
        sourceText: String(row.source_text || ''), translatedText: String(row.translated_text || ''),
        translationId: translationId || undefined,
        translationStatus: translationId ? translationStatus : undefined,
        revision: Number(row.revision || 0) || undefined,
        status: String(row.status || 'failed') as CanonicalGlossaryEnsureResult['visualTranslations'][number]['status'],
        origin: String(row.origin || 'none') as CanonicalGlossaryEnsureResult['visualTranslations'][number]['origin'],
        contextCurrent: row.context_current === true,
        retryable: row.retryable === true,
        errorCode: String(row.error_code || '') || undefined,
        message: String(row.message || '') || undefined,
        ...(translation ? { translation } : {}),
      }
    }) : [],
    ...(raw.runtime && typeof raw.runtime === 'object' ? {
      runtime: {
        invocationAttempted: (raw.runtime as Raw).invocation_attempted === true,
        submissionUncertain: (raw.runtime as Raw).submission_uncertain === true,
        modelDeploymentId: String((raw.runtime as Raw).model_deployment_id || '') || undefined,
        provider: String((raw.runtime as Raw).provider || '') || undefined,
        dataEgress: (raw.runtime as Raw).data_egress === true,
      },
    } : {}),
  }
}

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
    promotedFields: adaptPromotedFields(raw.promoted_fields),
  }
}

export const adaptPromotedFields = (value: unknown): PromptTranslationPromotedField[] => (
  Array.isArray(value) ? value.flatMap((raw) => {
    if (!raw || typeof raw !== 'object') return []
    const row = raw as Raw
    const fieldKey = String(row.field_key || '')
    if (!fieldKey) return []
    const rawIndex = row.dialogue_index
    const dialogueIndex = rawIndex === null || rawIndex === undefined || rawIndex === ''
      ? null
      : Number.isInteger(Number(rawIndex)) && Number(rawIndex) >= 0 ? Number(rawIndex) : null
    return [{ fieldKey, value: String(row.value || ''), dialogueIndex }]
  }) : []
)

export const adaptShotTranslationPromotionResult = (raw: Raw): ShotTranslationPromotionResult => ({
  shotId: String(raw.shot_id || ''),
  promotedFields: adaptPromotedFields(raw.promoted_fields),
  entries: Array.isArray(raw.entries)
    ? raw.entries.filter((entry): entry is Raw => Boolean(entry && typeof entry === 'object')).map(adaptTranslationEntry)
    : [],
})

/** Patch only the formal fields returned by the promotion transaction. */
export const applyPromotedFieldsToShot = (
  shot: Shot,
  promotedFields: PromptTranslationPromotedField[],
  characters: DialogueCharacterOption[],
): string[] => {
  const applied: string[] = []
  const dialogue = parseDialogueText(shot.audio.dialogue, characters)
  let dialogueChanged = false
  for (const field of promotedFields) {
    if (field.fieldKey === 'shot.narrative') shot.action = field.value
    else if (field.fieldKey === 'shot.camera') shot.camera = field.value
    else if (field.fieldKey === 'shot.detailed_description') shot.detailedDescription = field.value
    else if (field.fieldKey === 'shot.continuity_notes') shot.continuityDirection = field.value
    else if (field.fieldKey === 'shot.sound_effects') shot.audio.soundscape = field.value
    else if (field.fieldKey === 'shot.music') shot.audio.music = field.value
    else if (field.fieldKey.startsWith('shot.dialogue.') && field.fieldKey.endsWith('.delivery') && dialogue) {
      const index = field.dialogueIndex ?? Number(field.fieldKey.match(/^shot\.dialogue\.(\d+)\.delivery$/)?.[1] ?? -1)
      if (!Number.isInteger(index) || index < 0 || !dialogue[index]) continue
      dialogue[index] = { ...dialogue[index], delivery: field.value }
      dialogueChanged = true
    } else continue
    applied.push(field.fieldKey)
  }
  if (dialogueChanged && dialogue) shot.audio.dialogue = formatDialogueLines(dialogue)
  return applied
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

export const confirmedTranslationForPrompt = (
  field: PromptTranslationField,
  entry: PromptTranslationEntry | undefined,
) => translationReviewStatus(field, entry) === 'confirmed' ? entry?.translatedText.trim() || '' : ''

export const formatTranslationGlossary = (value: Record<string, string> | undefined) => Object.entries(value || {})
  .map(([source, target]) => `${source} = ${target}`)
  .join('\n')

const containsCjk = (value: string) => /[\u3400-\u4dbf\u4e00-\u9fff]/.test(value)

/**
 * Only CJK Bible names need a Chinese-to-English canonical mapping. Preserve
 * insertion order so the UI and backend batch request remain predictable.
 */
export const missingBibleCanonicalNames = (
  names: Iterable<string>,
  glossary: Record<string, string> | undefined,
): string[] => {
  const existing = glossary || {}
  const missing = new Set<string>()
  for (const rawName of names) {
    const name = rawName.trim()
    if (name && containsCjk(name) && !String(existing[name] || '').trim()) missing.add(name)
  }
  return [...missing]
}

/**
 * The ensure endpoint returns the row-locked, authoritative project glossary.
 * Do not merge a stale browser snapshot back over it: an operator in another
 * tab may have saved a newer manual mapping while local Qwen was running.
 */
export const acceptAuthoritativeCanonicalGlossary = (
  ensured: Record<string, string> | undefined,
): Record<string, string> => ({ ...(ensured || {}) })

export interface BibleCanonicalTargetCollision {
  canonicalTarget: string
  sourceNames: string[]
}

/**
 * Canonical names are identity keys, so visually equivalent Unicode, repeated
 * whitespace and letter case must not create separate identities.  The backend
 * remains authoritative; this helper gives the glossary editor the same
 * fail-closed UX before a save or generation request is sent.
 */
const canonicalIdentityKey = (value: string): string => value
  .normalize('NFKC')
  .replace(/\s+/gu, ' ')
  .trim()
  .toLocaleLowerCase('en-US')
  .replace(/ß/gu, 'ss')

export const bibleCanonicalTargetCollisions = (
  names: Iterable<string>,
  glossary: Record<string, string> | undefined,
): BibleCanonicalTargetCollision[] => {
  const bibleNames = new Map<string, string>()
  for (const rawName of names) {
    const displayName = rawName.normalize('NFKC').replace(/\s+/gu, ' ').trim()
    const key = canonicalIdentityKey(displayName)
    if (key && !bibleNames.has(key)) bibleNames.set(key, displayName)
  }

  const targetGroups = new Map<string, { canonicalTarget: string; sources: Map<string, string> }>()
  for (const [rawSource, rawTarget] of Object.entries(glossary || {})) {
    const sourceKey = canonicalIdentityKey(rawSource)
    const sourceName = bibleNames.get(sourceKey)
    const targetKey = canonicalIdentityKey(rawTarget)
    if (!sourceName || !targetKey) continue
    const group = targetGroups.get(targetKey) || {
      canonicalTarget: rawTarget.normalize('NFKC').replace(/\s+/gu, ' ').trim(),
      sources: new Map<string, string>(),
    }
    group.sources.set(sourceKey, sourceName)
    targetGroups.set(targetKey, group)
  }

  return [...targetGroups.values()]
    .filter((group) => group.sources.size > 1)
    .map((group) => ({ canonicalTarget: group.canonicalTarget, sourceNames: [...group.sources.values()] }))
}

export const bibleCanonicalTargetCollisionMessage = (
  collisions: BibleCanonicalTargetCollision[],
): string => collisions.length
  ? `canonical_target_collision：${collisions.map((item) => `Bible 实体“${item.sourceNames.join('”“')}”共用英文 canonical “${item.canonicalTarget}”`).join('；')}。请人工填写互不重复的英文名称；系统不会猜测或自动改名。`
  : ''

export const parseTranslationGlossary = (raw: string): { value: Record<string, string>; error: string } => {
  const value: Record<string, string> = {}
  const lines = raw.split(/\r?\n/)
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index].trim()
    if (!line || line.startsWith('#')) continue
    const match = line.match(/^(.+?)\s*(?:=>|->|=)\s*(.+)$/)
    if (!match) return { value: {}, error: `术语表第 ${index + 1} 行格式错误；请使用“中文术语 = Canonical English”。` }
    const source = match[1].trim(); const target = match[2].trim()
    if (!source || !target) return { value: {}, error: `术语表第 ${index + 1} 行的中文术语或英文 canonical 名称为空。` }
    if (/[\u3400-\u4dbf\u4e00-\u9fff]/.test(target) || !/[A-Za-z]/.test(target)) return { value: {}, error: `术语表第 ${index + 1} 行右侧必须是英文 canonical 名称。` }
    if (Object.prototype.hasOwnProperty.call(value, source)) return { value: {}, error: `术语表中的“${source}”重复，请只保留一个 canonical 英文名称。` }
    value[source] = target
  }
  return { value, error: '' }
}

export const dialogueDeliveryTranslationFields = (
  dialogue: string,
  characters: DialogueCharacterOption[],
  shotId: string,
): PromptTranslationField[] => {
  const lines = parseDialogueText(dialogue, characters)
  if (!lines) return []
  return lines.flatMap((line, index) => line.delivery ? [{
    fieldKey: `shot.dialogue.${index}.delivery`,
    label: `对白 ${index + 1} · ${line.speaker_id} · delivery`,
    sourceText: line.delivery,
    scopeType: 'shot' as const,
    scopeId: shotId,
    impact: 'prompt' as const,
    help: '只翻译括号中的演绎方式；对白正文和 voiceover 控制词保持原文',
  }] : [])
}
