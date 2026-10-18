export const changeReferenceLabels = {
  recorded_reference: '已记录引用', manual_review_required: '需人工核对', unverifiable: '来源无法核对',
} as const
export const changeReferenceKindLabels = {
  script_input: '剧本输入', reviewed_content: '已审核内容', related_review: '关联审核内容',
  selected_output: '已采用产物', adoption_baseline: '结果写回基线（不是提示词输入）',
} as const
type ReferenceState = keyof typeof changeReferenceLabels
type ReferenceKind = keyof typeof changeReferenceKindLabels
const replacementCodes = {
  no_dependents: '', stopped_unused: '', review_only: '', review_and_stopped: '', media_review_required: '', text_review_required: '', run_read_only: '', assembly_locked: '',
  dependent_results: '', active_preparations: '', inconsistent_records: '', unresolved_calls: '',
} as const
export interface ManualChangeImpact {
  runId: string; runPlanSha256: string; checkedAt: string; assemblyLocked: boolean; runReadOnly: boolean
  replacement: { allowed: boolean; code: keyof typeof replacementCodes; reason: string }
  source: { stageRunId: string; stageKey: string; decisionId: string | null; revision: number; scriptRevisionId: string | null }
  stages: { id: string; stageKey: string; status: string; preparationCount: number }[]
  count: number; offset: number; limit: number; nextOffset: number | null
  results: { id: string; stageRunId: string; stageKey: string; unitKey: string; status: string; revision: number;
    referenceState: ReferenceState; references: { inputPath: string; kind: ReferenceKind }[];
    attempts: { role: 'primary' | 'auxiliary'; status: string; count: number }[] }[]
}
const invalid = (): never => { throw new Error('改选影响记录缺失或不一致，请重新读取；不能据此认定没有影响') }
const object = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid()
  return value as Record<string, unknown>
}
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : invalid()
const text = (value: unknown): string => typeof value === 'string' && value.length > 0 ? value : invalid()
const id = (value: unknown): string => /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(text(value)) ? String(value) : invalid()
const number = (value: unknown, minimum = 0): number => Number.isSafeInteger(value) && Number(value) >= minimum ? Number(value) : invalid()
const boolean = (value: unknown): boolean => typeof value === 'boolean' ? value : invalid()
const choice = <T extends string>(value: unknown, choices: Record<T, string>): T => Object.prototype.hasOwnProperty.call(choices, text(value)) ? value as T : invalid()
export function adaptManualChangeImpact(value: unknown): ManualChangeImpact {
  const raw = object(value), source = object(raw.source)
  const replacementRaw = object(raw.replacement)
  const replacement = { allowed: boolean(replacementRaw.allowed), code: choice(replacementRaw.code, replacementCodes), reason: text(replacementRaw.reason) }
  const assemblyLocked = boolean(raw.assembly_locked), runReadOnly = boolean(raw.run_read_only)
  if (replacement.allowed !== (replacement.code === 'no_dependents' || replacement.code === 'stopped_unused' || replacement.code === 'review_only' || replacement.code === 'review_and_stopped' || replacement.code === 'media_review_required' || replacement.code === 'text_review_required')
    || replacement.allowed && (assemblyLocked || runReadOnly)) return invalid()
  const digest = text(raw.run_plan_sha256), checkedAt = text(raw.checked_at)
  if (!/^[a-f0-9]{64}$/.test(digest) || !Number.isFinite(Date.parse(checkedAt))) return invalid()
  const stages = list(raw.stages).map(value => {
    const row = object(value)
    return { id: id(row.id), stageKey: text(row.stage_key), status: text(row.status), preparationCount: number(row.preparation_count) }
  })
  if (new Set(stages.map(row => row.id)).size !== stages.length) return invalid()
  const results = list(raw.results).map(value => {
    const row = object(value), stageRunId = id(row.stage_run_id), stageKey = text(row.stage_key)
    if (!stages.some(stage => stage.id === stageRunId && stage.stageKey === stageKey)) return invalid()
    const referenceState = choice(row.reference_state, changeReferenceLabels)
    const references = list(row.references).map(value => {
      const ref = object(value); return { inputPath: text(ref.input_path), kind: choice(ref.kind, changeReferenceKindLabels) }
    })
    if ((referenceState === 'recorded_reference') !== Boolean(references.length)) return invalid()
    const attempts = list(row.attempts).map(value => {
      const item = object(value)
      if (item.role !== 'primary' && item.role !== 'auxiliary') return invalid()
      return { role: item.role as 'primary' | 'auxiliary', status: text(item.status), count: number(item.count, 1) }
    })
    return { id: id(row.id), stageRunId, stageKey, unitKey: text(row.unit_key), status: text(row.status), revision: number(row.revision, 1),
      referenceState, references, attempts }
  })
  const count = number(raw.count), offset = number(raw.offset), limit = number(raw.limit, 1)
  const nextOffset = raw.next_offset === null ? null : number(raw.next_offset)
  if (limit > 50 || results.length !== Math.min(limit, Math.max(0, count - offset))
    || stages.reduce((sum, stage) => sum + stage.preparationCount, 0) !== count
    || nextOffset !== (offset + limit < count ? offset + limit : null)
    || new Set(results.map(row => row.id)).size !== results.length) return invalid()
  return { runId: id(raw.run_id), runPlanSha256: digest, checkedAt, assemblyLocked, runReadOnly, replacement,
    source: { stageRunId: id(source.stage_run_id), stageKey: text(source.stage_key), revision: number(source.revision),
      decisionId: source.decision_id === null ? null : id(source.decision_id), scriptRevisionId: source.script_revision_id === null ? null : id(source.script_revision_id) },
    stages, count, offset, limit, nextOffset, results }
}
