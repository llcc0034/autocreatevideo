import type {
  BibleItem,
  ContinuityBible,
  ScriptBibleApplyCollections,
  ScriptBibleApplyResult,
  ScriptBibleCandidate,
  ScriptBibleCandidatesResult,
  ScriptBibleConflictCode,
  ScriptBibleDiagnostic,
  ScriptBibleEntityType,
  ScriptBibleEvidence,
  ScriptBibleEvidenceKind,
  ScriptBibleReviewedSelection,
} from '@/types'
import { adaptBibleItem } from './backendAdapters'

type Raw = Record<string, unknown>

export interface ScriptBibleCandidateDraft {
  name: string
  description: string
  visualIdentityEn: string
}

export interface ScriptBibleSelectionRefreshState {
  selectedIds: Set<string>
  recommendedSeeded: boolean
}

const SHA256_RE = /^[0-9a-f]{64}$/
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const SAFE_CODE_RE = /^[a-z][a-z0-9_]{0,79}$/
const entityTypes: ScriptBibleEntityType[] = ['character', 'scene', 'prop']
const evidenceKinds: ScriptBibleEvidenceKind[] = ['body_mention', 'dialogue_speaker', 'shot_setting']
const conflictCodes: ScriptBibleConflictCode[] = [
  'script_bible_cross_type_name_conflict',
  'script_bible_content_conflict',
  'script_bible_reviewed_content_conflict',
  'script_bible_provenance_conflict',
]

const invalidContract = (): never => {
  throw new Error('服务端返回的剧本 Bible 候选契约无效，请刷新后重试。')
}

const objectValue = (value: unknown): Raw => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as Raw : invalidContract()
)
const stringValue = (value: unknown): string => typeof value === 'string' ? value : invalidContract()
const booleanValue = (value: unknown): boolean => typeof value === 'boolean' ? value : invalidContract()
const integerValue = (value: unknown, minimum = 0): number => {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum
    ? value
    : invalidContract()
}
const charLength = (value: string): number => Array.from(value).length

const adaptEvidence = (value: unknown): ScriptBibleEvidence => {
  const raw = objectValue(value)
  const kind = stringValue(raw.kind)
  if (!evidenceKinds.includes(kind as ScriptBibleEvidenceKind)) invalidContract()
  const excerpt = stringValue(raw.excerpt)
  if (charLength(excerpt) > 240) invalidContract()
  return { line: integerValue(raw.line, 1), kind: kind as ScriptBibleEvidenceKind, excerpt }
}

const adaptCandidate = (value: unknown): ScriptBibleCandidate => {
  const raw = objectValue(value)
  const candidateId = stringValue(raw.candidate_id)
  const entityType = stringValue(raw.entity_type)
  const name = stringValue(raw.name)
  const description = stringValue(raw.description)
  const visualIdentityEn = stringValue(raw.visual_identity_en)
  const matchedBibleId = raw.matched_bible_id === null ? null : stringValue(raw.matched_bible_id)
  const conflictCode = raw.conflict_code === null ? null : stringValue(raw.conflict_code)
  const evidence = raw.evidence
  if (!Array.isArray(evidence)) invalidContract()
  const evidenceRows = evidence as unknown[]
  if (
    !SHA256_RE.test(candidateId)
    || !entityTypes.includes(entityType as ScriptBibleEntityType)
    || charLength(name) > 160
    || charLength(description) > 2_000
    || charLength(visualIdentityEn) > 3_000
    || (matchedBibleId !== null && !UUID_RE.test(matchedBibleId))
    || (conflictCode !== null && !conflictCodes.includes(conflictCode as ScriptBibleConflictCode))
  ) invalidContract()
  return {
    candidateId,
    entityType: entityType as ScriptBibleEntityType,
    name,
    description,
    visualIdentityEn,
    declarationLine: integerValue(raw.declaration_line, 1),
    occurrence: integerValue(raw.occurrence),
    evidence: evidenceRows.map(adaptEvidence),
    recommended: booleanValue(raw.recommended),
    matchedBibleId,
    existing: booleanValue(raw.existing),
    confirmed: booleanValue(raw.confirmed),
    conflictCode: conflictCode as ScriptBibleConflictCode | null,
  }
}

const adaptDiagnostic = (value: unknown): ScriptBibleDiagnostic => {
  const raw = objectValue(value)
  const severity = stringValue(raw.severity)
  const code = stringValue(raw.code)
  if (!['info', 'warning', 'error'].includes(severity) || !SAFE_CODE_RE.test(code)) invalidContract()
  const diagnostic: ScriptBibleDiagnostic = {
    severity: severity as ScriptBibleDiagnostic['severity'],
    code,
  }
  if (raw.line !== undefined) diagnostic.line = raw.line === null ? null : integerValue(raw.line, 1)
  if (raw.detail !== undefined) diagnostic.detail = stringValue(raw.detail)
  if (raw.entity_type !== undefined) {
    const entityType = stringValue(raw.entity_type)
    if (!entityTypes.includes(entityType as ScriptBibleEntityType)) invalidContract()
    diagnostic.entityType = entityType as ScriptBibleEntityType
  }
  if (raw.candidate_id !== undefined) {
    const candidateId = stringValue(raw.candidate_id)
    if (!SHA256_RE.test(candidateId)) invalidContract()
    diagnostic.candidateId = candidateId
  }
  return diagnostic
}

export const adaptScriptBibleCandidates = (value: unknown): ScriptBibleCandidatesResult => {
  const raw = objectValue(value)
  const projectId = stringValue(raw.project_id)
  const sourceFingerprint = stringValue(raw.source_fingerprint)
  const candidates = raw.candidates
  const diagnostics = raw.diagnostics
  if (!Array.isArray(candidates) || !Array.isArray(diagnostics)) invalidContract()
  const candidateRows = candidates as unknown[]
  const diagnosticRows = diagnostics as unknown[]
  if (
    raw.schema_version !== 1
    || !UUID_RE.test(projectId)
    || !SHA256_RE.test(sourceFingerprint)
  ) invalidContract()
  return {
    schemaVersion: 1,
    projectId,
    sourceFingerprint,
    hasBlock: booleanValue(raw.has_block),
    candidates: candidateRows.map(adaptCandidate),
    diagnostics: diagnosticRows.map(adaptDiagnostic),
  }
}

const adaptBibleRows = (value: unknown): BibleItem[] => {
  if (!Array.isArray(value)) invalidContract()
  const rows = value as unknown[]
  return rows.map((item) => {
    const raw = objectValue(item)
    if (!UUID_RE.test(stringValue(raw.id)) || !stringValue(raw.name).trim()) invalidContract()
    return adaptBibleItem(raw)
  })
}

const adaptCollections = (value: unknown): ScriptBibleApplyCollections => {
  const raw = objectValue(value)
  return {
    characters: adaptBibleRows(raw.characters),
    scenes: adaptBibleRows(raw.scenes),
    props: adaptBibleRows(raw.props),
  }
}

export const adaptScriptBibleApplyResult = (value: unknown): ScriptBibleApplyResult => {
  const raw = objectValue(value)
  const projectId = stringValue(raw.project_id)
  const sourceFingerprint = stringValue(raw.source_fingerprint)
  const counts = objectValue(raw.counts)
  const requested = integerValue(counts.requested, 1)
  const created = integerValue(counts.created)
  const alreadyExists = integerValue(counts.already_exists)
  const createdCollections = adaptCollections(raw.created)
  const alreadyExistsCollections = adaptCollections(raw.already_exists)
  const createdRows = createdCollections.characters.length + createdCollections.scenes.length + createdCollections.props.length
  const alreadyExistsRows = alreadyExistsCollections.characters.length + alreadyExistsCollections.scenes.length + alreadyExistsCollections.props.length
  if (
    raw.schema_version !== 1
    || !UUID_RE.test(projectId)
    || !SHA256_RE.test(sourceFingerprint)
    || created + alreadyExists !== requested
    || createdRows !== created
    || alreadyExistsRows !== alreadyExists
  ) invalidContract()
  return {
    schemaVersion: 1,
    projectId,
    sourceFingerprint,
    updatedAt: String(raw.updated_at || '') || undefined,
    created: createdCollections,
    alreadyExists: alreadyExistsCollections,
    counts: { requested, created, alreadyExists },
  }
}

export const createScriptBibleCandidateDrafts = (
  candidates: ScriptBibleCandidate[],
): Record<string, ScriptBibleCandidateDraft> => Object.fromEntries(candidates.map((candidate) => [
  candidate.candidateId,
  {
    name: candidate.name,
    description: candidate.description,
    visualIdentityEn: candidate.visualIdentityEn,
  },
]))

export const scriptBibleCandidateSelectable = (candidate: ScriptBibleCandidate): boolean => (
  !candidate.confirmed && candidate.conflictCode === null
)

export const reconcileScriptBibleSelectionAfterRefresh = (
  candidates: ScriptBibleCandidate[],
  currentSelectedIds: Iterable<string>,
  recommendedSeeded: boolean,
): ScriptBibleSelectionRefreshState => {
  const selectable = new Set(candidates.filter(scriptBibleCandidateSelectable).map((item) => item.candidateId))
  const selectedIds = new Set([...currentSelectedIds].filter((candidateId) => selectable.has(candidateId)))
  if (!recommendedSeeded) {
    for (const candidate of candidates) {
      if (candidate.recommended && selectable.has(candidate.candidateId)) selectedIds.add(candidate.candidateId)
    }
  }
  return { selectedIds, recommendedSeeded: true }
}

export const masterScriptNeedsScriptBibleRefresh = (
  masterScript: string,
  savedMasterScript: string,
): boolean => masterScript !== savedMasterScript

const validateDraft = (draft: ScriptBibleCandidateDraft): void => {
  if (!draft.name.trim()) throw new Error('已选候选的名称不能为空。')
  if (charLength(draft.name) > 160) throw new Error('候选名称不能超过 160 个字符。')
  if (charLength(draft.description) > 2_000) throw new Error('候选基础描述不能超过 2,000 个字符。')
  if (charLength(draft.visualIdentityEn) > 3_000) throw new Error('候选视觉锚点不能超过 3,000 个字符。')
}

export const buildScriptBibleSelections = (
  candidates: ScriptBibleCandidate[],
  selectedIds: Iterable<string>,
  drafts: Record<string, ScriptBibleCandidateDraft>,
): ScriptBibleReviewedSelection[] => {
  const byId = new Map(candidates.map((candidate) => [candidate.candidateId, candidate]))
  return [...selectedIds].map((candidateId) => {
    const candidate = byId.get(candidateId)
    const draft = drafts[candidateId]
    if (!candidate || !draft || !scriptBibleCandidateSelectable(candidate)) {
      throw new Error('所选候选已失效，请刷新候选后重新确认。')
    }
    validateDraft(draft)
    return {
      candidateId,
      reviewed: true,
      name: draft.name,
      description: draft.description,
      visualIdentityEn: draft.visualIdentityEn,
    }
  })
}

export const scriptBibleApplyPayload = (
  sourceFingerprint: string,
  selections: ScriptBibleReviewedSelection[],
) => {
  if (!SHA256_RE.test(sourceFingerprint) || !selections.length) {
    throw new Error('请至少选择一项有效候选后再确认。')
  }
  return {
    source_fingerprint: sourceFingerprint,
    selections: selections.map((selection) => ({
      candidate_id: selection.candidateId,
      reviewed: true as const,
      name: selection.name,
      description: selection.description,
      visual_identity_en: selection.visualIdentityEn,
    })),
  }
}

export const mergeScriptBibleApplyIntoBible = (
  bible: ContinuityBible,
  result: ScriptBibleApplyResult,
): number => {
  let appended = 0
  const merge = (target: BibleItem[], incoming: BibleItem[]) => {
    const ids = new Set(target.map((item) => item.id))
    for (const item of incoming) {
      if (ids.has(item.id)) continue
      target.push(item)
      ids.add(item.id)
      appended += 1
    }
  }
  for (const collections of [result.created, result.alreadyExists]) {
    merge(bible.characters, collections.characters)
    merge(bible.scenes, collections.scenes)
    merge(bible.props, collections.props)
  }
  return appended
}

export const scriptBibleErrorCode = (error: unknown): string => {
  const response = error as { response?: { data?: { error?: { code?: unknown } } } }
  const code = String(response.response?.data?.error?.code ?? '').trim().toLowerCase()
  return SAFE_CODE_RE.test(code) ? code : ''
}

export const scriptBiblePendingCountFromError = (error: unknown): number => {
  const response = error as { response?: { data?: { error?: { details?: { pending_count?: unknown } } } } }
  const value = Number(response.response?.data?.error?.details?.pending_count)
  return Number.isSafeInteger(value) && value >= 0 ? value : 0
}

const errorMessages: Record<string, string> = {
  script_bible_candidates_stale: '完整剧本已变化，当前候选已过期。请刷新候选后重新审核；你的编辑仍保留在本页。',
  script_bible_apply_conflict: '候选与现有 Bible 或既有审核记录冲突，服务端未写入任何内容。请刷新并逐项核对。',
  script_bible_confirmation_required: '剧本中仍有尚未确认的 Bible 建议。请先审核，或明确跳过后再继续拆解。',
  script_bible_selection_invalid: '所选候选已失效或不允许写入，请刷新候选后重新选择。',
  script_bible_field_invalid: '候选字段格式无效，请检查人工编辑内容。',
  script_bible_field_reserved_character: '候选字段包含不允许的保留字符，请修改后重试。',
  script_bible_field_control_character: '候选字段包含不可见控制字符，请修改后重试。',
  script_bible_field_required: '候选必填字段为空，请补充后重试。',
  script_bible_field_too_long: '候选字段超过服务端长度限制，请缩短后重试。',
  script_bible_cross_type_name_conflict: '同名实体已存在于另一种 Bible 类型中。',
  script_bible_content_conflict: '同名 Bible 已存在，但内容不同。',
  script_bible_reviewed_content_conflict: '该候选曾以不同内容确认，不能覆盖既有审核记录。',
  script_bible_provenance_conflict: '该 Bible 的候选来源记录冲突，请人工核对。',
}

export const scriptBibleErrorMessage = (error: unknown, fallback: string): string => {
  const code = scriptBibleErrorCode(error)
  if (errorMessages[code]) return errorMessages[code]
  if (code.startsWith('script_bible_')) return '剧本 Bible 数据未通过服务端校验，未写入任何内容。请按页面提示检查后重试。'
  return error instanceof Error && error.message ? error.message : fallback
}

export const scriptBibleConflictMessage = (code: ScriptBibleConflictCode): string => (
  errorMessages[code] || '候选与现有 Bible 冲突。'
)
