import type { JsonObject } from './platformTypes'
import type { AssetAppearanceTarget } from './assetAppearanceTarget'
import { adaptPreparationOutcome, type PreparationOutcome } from './preparationOutcome.ts'

export interface ExecutionSmokeFixture {
  inputs: JsonObject; parameters: JsonObject; manual_channels: Record<string, string>; subject_facts: JsonObject[]
  source_language: string; target_language: string
  appearance_target?: AssetAppearanceTarget | null
}

export interface ExecutionSmokeRequest {
  reviewedSourceContext?: { run_id: string; stage_key: string; run_plan_sha256: string }
  expectedDefinitionSha256: string
  fixture: ExecutionSmokeFixture
  fieldValues: { unit_key: string; field_key: string; text: string }[]
  allowExternalCalls: boolean
  reuse?: { preparationId: string; expectedRevision: number; fields: { source_preparation_id: string; field_value_id: string; unit_key: string; field_key: string }[] }
}

export const executionSmokePayload = (input: ExecutionSmokeRequest) => ({
  ...(input.reviewedSourceContext ? { reviewed_source_context: input.reviewedSourceContext } : {}),
  expected_definition_sha256: input.expectedDefinitionSha256, fixture: input.fixture, field_values: input.fieldValues,
  allow_external_calls: input.allowExternalCalls,
  ...(input.reuse ? { reuse_preparation_id: input.reuse.preparationId, expected_reuse_revision: input.reuse.expectedRevision, field_selections: input.reuse.fields } : {}),
})

export interface ExecutionSmokeReuseFixture {
  versionId: string
  expectedDefinitionSha256: string
  preparationId: string
  expectedRevision: number
  fixture: ExecutionSmokeRequest['fixture']
  fields: NonNullable<ExecutionSmokeRequest['reuse']>['fields']
}

export const adaptExecutionSmokeReuseFixture = (value: unknown): ExecutionSmokeReuseFixture => {
  const r = record(value), fixture = record(r.fixture)
  const keys = ['inputs', 'parameters', 'manual_channels', 'subject_facts', 'source_language', 'target_language']
  if (keys.some(key => !(key in fixture)) || Object.keys(fixture).some(key => !keys.includes(key) && key !== 'appearance_target')) invalid('fixture')
  const channels = record(fixture.manual_channels), subjects = fixture.subject_facts
  if (Object.values(channels).some(value => typeof value !== 'string') || !Array.isArray(subjects)
    || !Array.isArray(r.field_selections)) invalid('fixture')
  const fields = (r.field_selections as unknown[]).map(value => {
    const row = record(value)
    const result = { source_preparation_id: string(row, 'source_preparation_id'), field_value_id: string(row, 'field_value_id'),
      unit_key: string(row, 'unit_key'), field_key: string(row, 'field_key') }
    if (Object.values(result).some(value => !value)) invalid('field_selections')
    return result
  })
  if (new Set(fields.map(row => JSON.stringify([row.unit_key, row.field_key]))).size !== fields.length) invalid('field_selections')
  const sha = string(r, 'expected_definition_sha256')
  if (!/^[a-f0-9]{64}$/.test(sha)) invalid('expected_definition_sha256')
  return { versionId: string(r, 'execution_instance_version_id'), expectedDefinitionSha256: sha,
    preparationId: string(r, 'reuse_preparation_id'), expectedRevision: revision({ revision: r.expected_reuse_revision }),
    fixture: { inputs: record(fixture.inputs), parameters: record(fixture.parameters), manual_channels: channels as Record<string, string>,
      subject_facts: (subjects as unknown[]).map(record), source_language: string(fixture, 'source_language'), target_language: string(fixture, 'target_language'),
      ...('appearance_target' in fixture ? { appearance_target: fixture.appearance_target === null ? null
        : record(fixture.appearance_target) as unknown as AssetAppearanceTarget } : {}) }, fields }
}

export const executionReuseSmokeRequest = (source: ExecutionSmokeReuseFixture, allowExternalCalls: boolean): ExecutionSmokeRequest => {
  if (typeof allowExternalCalls !== 'boolean') throw new Error('请明确本次外部调用授权')
  return { expectedDefinitionSha256: source.expectedDefinitionSha256, fixture: source.fixture, fieldValues: [],
    allowExternalCalls,
    reuse: { preparationId: source.preparationId, expectedRevision: source.expectedRevision, fields: source.fields } }
}

export const adaptExecutionSmokeCreated = (value: unknown) => adaptPreparedSmokeCreated(value, 'instance_smoke')
export const adaptResourceSmokeCreated = (value: unknown) => adaptPreparedSmokeCreated(value, 'resource_smoke')
const adaptPreparedSmokeCreated = (value: unknown, purpose: 'instance_smoke' | 'resource_smoke') => {
  const r = record(value)
  const kind = string(r, 'attempt_kind')
  if (kind !== 'deployment_smoke' && kind !== 'workflow_smoke') throw new Error('验收响应缺少正式任务类型')
  const preparation = adaptExecutionPreparation(r.preparation)
  const attemptId = string(r, 'attempt_id')
  if (preparation.purpose !== purpose || (preparation.executionInstanceVersion === null) !== (purpose === 'resource_smoke')
    || (kind === 'deployment_smoke' ? preparation.deploymentSmoke : preparation.workflowSmoke) !== attemptId) {
    throw new Error('验收任务与执行准备关联不一致')
  }
  return { attemptKind: kind, attemptId, status: string(r, 'status'), preparation }
}

export interface ExecutionPreparationSummary {
  id: string
  stageRun: string | null
  assetJob: string | null
  deploymentSmoke: string | null
  workflowSmoke: string | null
  executionInstanceVersion: string | null
  purpose: string
  unitKey: string
  requestKey: string
  revision: number
  status: string
  intentSha256: string
  requestSha256: string | null
  requestFrozenAt: string | null
  nextCheckAt: string | null
  reasonCode: string
  reason: string
  evaluationRevision: number | null
  evaluatedAt: string | null
  createdBy: number
  createdAt: string
  updatedAt: string
}
export interface ExecutionFieldSelection {
  id: string
  preparation: string
  unitKey: string
  fieldKey: string
  fieldValue: string
  revision: number
  selectedBy: number
  selectedAt: string
}
export interface ExecutionPreparationDetail extends ExecutionPreparationSummary {
  mediaArchiveRecovery: {taskId: string; attemptId: string; taskUpdatedAt: string; receiptSha256: string; canRecover: boolean; reason: string} | null
  mediaUncertainRetry: {taskId: string; attemptId: string; taskUpdatedAt: string; requestSha256: string; canRetry: boolean; reason: string} | null
  presentation: ExecutionPreparationPresentation | null
  outcome: PreparationOutcome | null
  textResponseReview: { id: string; attemptId: string | null; assetJobId: string | null; originalText: string; originalSha256: string; correctedText: string | null; correctedSha256: string | null; confirmedBy: string | null; editable: boolean; confirmedAt: string | null; readonlyReason: string; diagnostics: Record<string, unknown>; fields: { unitKey: string; fieldKey: string; label: string; text: string }[]; notices: ExecutionFieldWarning[] } | null
  resourceReview: ExecutionResourceReview | null
  cancellation: { allowed: boolean; reason: string; cancelledAt: string | null; cancelledBy: string | null } | null
  responseDiagnostic: ExecutionResponseDiagnostic | null
  auxiliaryBatches: ExecutionAuxiliaryBatch[]
  intent: JsonObject
  resolvedInputs: JsonObject
  reviewContract: JsonObject
  reviewSummary: { mode: 'manual' | 'policy'; label: string; description: string }
  preparedRequest: JsonObject | null
  evaluation: JsonObject | null
  fieldSelections: ExecutionFieldSelection[]
  capacitySample: ExecutionCapacitySample | null
  fieldSlots: ExecutionPreparationFieldSlot[]
}
/** A dated field evaluation explains the next action; it does not grant approval or adoption. */
export const preparationFieldWait = (preparation: ExecutionPreparationDetail | null): { label: string; message: string } | null => {
  if (!preparation || preparation.presentation?.kind === 'auxiliary' || preparation.status !== 'awaiting_review'
      || preparation.reasonCode !== 'execution_fields_unselected' || preparation.requestFrozenAt !== null
      || preparation.evaluationRevision !== preparation.revision) return null
  const evaluation = preparation.evaluation
  if (!evaluation || evaluation.schema_version !== 1 || evaluation.phase !== 'fields'
      || evaluation.status !== preparation.status || evaluation.preparation_revision !== preparation.revision
      || evaluation.reason_code !== preparation.reasonCode || !Array.isArray(evaluation.outstanding_fields)
      || !evaluation.outstanding_fields.length) return null
  const identities = new Set<string>()
  const actions: string[] = []
  for (const value of evaluation.outstanding_fields) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null
    const row = value as Record<string, unknown>
    if (row.required !== true || !['review', 'select'].includes(String(row.action))
        || typeof row.unit_key !== 'string' || typeof row.field_key !== 'string'
        || !preparation.fieldSlots.some(slot => slot.required && slot.unitKey === row.unit_key && slot.fieldKey === row.field_key)
        || preparation.fieldSelections.some(selection => selection.unitKey === row.unit_key && selection.fieldKey === row.field_key)) return null
    const identity = JSON.stringify([row.unit_key, row.field_key])
    if (identities.has(identity)) return null
    identities.add(identity); actions.push(row.action as string)
  }
  if (actions.every(action => action === 'select')) return { label: '待采用字段', message: '未采用的必需字段已有已确认修订；仍需明确选择本次使用的修订。尚未采用，不代表已提交执行。' }
  if (actions.every(action => action === 'review')) return { label: '待审核字段', message: '必需字段仍有待审核候选；审核与采用是两个独立动作，不会自动提交执行。' }
  return { label: '待审核 / 采用字段', message: '部分必需字段仍需审核，部分已有已确认修订但尚未采用。请分别处理，页面不会自动审核、采用或继续执行。' }
}
export interface ExecutionPreparationPresentation {
  kind: 'primary' | 'auxiliary'
  label: string
  operation: string
  parent: { id: string; label: string; status: string; unitKey: string } | null
  task: { id: string; status: string; errorCode: string; attemptNumber: number } | null
}
export type StageExecutionPreparation = ExecutionPreparationSummary & { presentation: ExecutionPreparationPresentation; outcome: PreparationOutcome }
export const adaptPreparationPresentation = (value: unknown): ExecutionPreparationPresentation => {
  const r = record(value)
  if (!['primary', 'auxiliary'].includes(String(r.kind))) invalid('presentation.kind')
  const parent = r.parent === null ? null : record(r.parent)
  const task = r.task === null ? null : record(r.task)
  if (parent && r.kind !== 'auxiliary') invalid('presentation.parent')
  if (task && !['queued', 'running', 'submitting', 'submitted', 'cancel_requested', 'succeeded', 'failed', 'cancelled', 'awaiting_review'].includes(String(task.status))) invalid('presentation.task.status')
  return { kind: r.kind as ExecutionPreparationPresentation['kind'], label: string(r, 'label'), operation: string(r, 'operation'),
    parent: parent ? { id: string(parent, 'id'), label: string(parent, 'label'), status: string(parent, 'status'), unitKey: string(parent, 'unit_key') } : null,
    task: task ? { id: string(task, 'id'), status: string(task, 'status'), errorCode: string(task, 'error_code'), attemptNumber: revision({ revision: task.attempt_number }) } : null }
}
export interface ExecutionResourceReview {
  fingerprint: string; policyCurrent: boolean; confirmed: boolean; confirmedAt: string | null
  projectedCalls: number; projectedTotal: string | null; currency: string
  issues: { code: string; message: string }[]
}
export const adaptExecutionResourceReview = (value: unknown): ExecutionResourceReview => {
  const r = record(value)
  if (!/^[a-f0-9]{64}$/.test(String(r.fingerprint)) || typeof r.policy_current !== 'boolean'
    || typeof r.confirmed !== 'boolean' || !Array.isArray(r.issues)
    || !Number.isSafeInteger(r.projected_calls) || Number(r.projected_calls) < 1) invalid('resource_review')
  return { fingerprint: string(r, 'fingerprint'), policyCurrent: r.policy_current as boolean,
    confirmed: r.confirmed as boolean, confirmedAt: nullable(r, 'confirmed_at'), projectedCalls: r.projected_calls as number,
    projectedTotal: nullable(r, 'projected_total'), currency: string(r, 'currency'),
    issues: (r.issues as unknown[]).map(value => { const row = record(value); return { code: string(row, 'code'), message: string(row, 'message') } }) }
}
export interface ExecutionResponseDiagnostic {
  state: 'recorded' | 'unverifiable'; taskId: string | null; observedAt: string | null
  summary: string; details: { label: string; value: string }[]
}
export interface ExecutionAuxiliaryBatch {
  fieldBatchKey: string; purpose: 'generate_fields' | 'translate_fields'; fieldKeys: string[]
  attemptNumber: number; taskId: string | null; preparationId: string | null; status: string; reason: string; canRetry: boolean
  requiresUncertainAcknowledgement: boolean
}
export interface ExecutionPreparationFieldSlot {
  fieldLabel: string
  latestValueId: string | null
  effectiveLanguage: string
  unitKey: string
  fieldKey: string
  nextRevision: number
  fieldContract: JsonObject
  sourceLanguage: string
  targetLanguage: string
  maxChars: number
  required: boolean
  instruction: string
  generationMode: string
}
export interface ExecutionCapacitySample {
  id: string
  observedAt: string
  elapsedMs: number
  signatureSha256: string | null
  dimensionsComplete: boolean
  knownDimensions: JsonObject
  missingDimensions: string[]
}
export interface ExecutionFieldSummary {
  id: string
  preparation: string
  unitKey: string
  fieldKey: string
  revision: number
  sourceSha256: string
  fieldContractSha256: string
  textSha256: string
  originKind: string
  originAttempt: string | null
  originAssetJob: string | null
  status: string
  approvalKind: string | null
  approvalLabel: string
  reviewedBy: number | null
  reviewedAt: string | null
  approvalPolicySha256: string | null
  createdBy: number
  createdAt: string
  updatedAt: string
}
export interface ExecutionFieldWarning { code: string; message: string }
export interface ExecutionFieldDetail extends ExecutionFieldSummary { text: string; warnings: ExecutionFieldWarning[] }
export interface ExecutionFieldActionResult {
  field: ExecutionFieldDetail
  preparation: ExecutionPreparationSummary
  selection: ExecutionFieldSelection | null
}
export interface ExecutionFieldCreate {
  preparationId: string
  unitKey: string
  fieldKey: string
  text: string
  revision: number
  expectedPreparationRevision: number
}
export interface ExecutionFieldReview {
  expectedTextSha256: string
  expectedSourceSha256: string
  expectedPreparationRevision: number
}
export interface ExecutionFieldConfirm extends ExecutionFieldReview {
  selectForExecution: boolean
  replace: boolean
  expectedSelectionRevision: number | null
}

// Missing facts must surface as a bad response, not an empty field or a fake
// pending/confirmed state. Unknown future status strings remain visible.
const invalid = (key: string): never => { throw new Error(`执行准备响应字段不完整或格式错误：${key}`) }
const record = (value: unknown): JsonObject => value !== null && typeof value === 'object' && !Array.isArray(value)
  ? value as JsonObject : invalid('object')
const string = (raw: JsonObject, key: string): string => typeof raw[key] === 'string' ? raw[key] as string : invalid(key)
const nullable = (raw: JsonObject, key: string): string | null => raw[key] === null ? null : string(raw, key)
const actorId = (raw: JsonObject, key: string): number => typeof raw[key] === 'number' && Number.isSafeInteger(raw[key]) && (raw[key] as number) > 0 ? raw[key] as number : invalid(key)
const revision = (raw: JsonObject): number => typeof raw.revision === 'number' && Number.isSafeInteger(raw.revision) && raw.revision > 0
  ? raw.revision : invalid('revision')

export const executionPreparationContinuePayload = (expectedRevision: number) => ({
  expected_preparation_revision: revision({ revision: expectedRevision }),
})

export const adaptStagePreparationPage = (value: unknown, stageId: string) => {
  const r = record(value)
  if (!stageId || !Number.isSafeInteger(r.count) || Number(r.count) < 0 || !Array.isArray(r.results)
    || !(r.next === null || typeof r.next === 'string') || !(r.previous === null || typeof r.previous === 'string')) {
    throw new Error('阶段执行准备分页响应不完整')
  }
  const results = r.results.map(value => ({ ...adaptExecutionPreparation(value), presentation: adaptPreparationPresentation(record(value).presentation), outcome: adaptPreparationOutcome(record(value).outcome) }))
  if (new Set(results.map(row => row.id)).size !== results.length || results.some(row => row.stageRun !== stageId
    || row.assetJob !== null || row.deploymentSmoke !== null || row.workflowSmoke !== null)) {
    throw new Error('执行准备不属于当前生产阶段')
  }
  return { count: Number(r.count), next: r.next as string | null, previous: r.previous as string | null, results }
}

export interface PreparationUnitGroup {
  unitKey: string; label: string; versionCount: number; latest: StageExecutionPreparation
}
export const preparationUnitKey = (row: StageExecutionPreparation) => row.presentation.parent?.unitKey || row.unitKey
export const adaptPreparationGroupPage = (value: unknown, stageId: string, kind: 'primary' | 'auxiliary') => {
  const r = record(value)
  if (!Array.isArray(r.results)) invalid('results')
  const rawGroups = (r.results as unknown[]).map(record)
  const page = adaptStagePreparationPage({ ...r, results: rawGroups.map(row => row.latest) }, stageId)
  const results: PreparationUnitGroup[] = rawGroups.map((row, index) => {
    const latest = page.results[index]!, unitKey = string(row, 'unit_key')
    if (latest.presentation.kind !== kind || preparationUnitKey(latest) !== unitKey) throw new Error('准备分组与当前对象或分类不一致')
    return { unitKey, label: string(row, 'label'), versionCount: revision({ revision: row.version_count }), latest }
  })
  if (new Set(results.map(row => row.unitKey)).size !== results.length) throw new Error('准备分组重复')
  return { ...page, results }
}

export const adaptExecutionPreparation = (value: unknown): ExecutionPreparationSummary => {
  const r = record(value)
  return { id: string(r, 'id'), stageRun: nullable(r, 'stage_run'), assetJob: nullable(r, 'asset_job'),
    deploymentSmoke: nullable(r, 'deployment_smoke'), workflowSmoke: nullable(r, 'workflow_smoke'),
    executionInstanceVersion: nullable(r, 'execution_instance_version'), purpose: string(r, 'purpose'),
    unitKey: string(r, 'unit_key'), requestKey: string(r, 'request_key'), revision: revision(r), status: string(r, 'status'),
    intentSha256: string(r, 'intent_sha256'), requestSha256: nullable(r, 'request_sha256'),
    requestFrozenAt: nullable(r, 'request_frozen_at'), nextCheckAt: nullable(r, 'next_check_at'),
    reasonCode: string(r, 'reason_code'), reason: string(r, 'reason'),
    evaluationRevision: r.evaluation_revision === null ? null : revision({ revision: r.evaluation_revision }),
    evaluatedAt: nullable(r, 'evaluated_at'),
    createdBy: actorId(r, 'created_by'), createdAt: string(r, 'created_at'), updatedAt: string(r, 'updated_at') }
}
export const adaptExecutionFieldSelection = (value: unknown): ExecutionFieldSelection => {
  const r = record(value)
  return { id: string(r, 'id'), preparation: string(r, 'preparation'), unitKey: string(r, 'unit_key'),
    fieldKey: string(r, 'field_key'), fieldValue: string(r, 'field_value'), revision: revision(r),
    selectedBy: actorId(r, 'selected_by'), selectedAt: string(r, 'selected_at') }
}
export const adaptExecutionCapacitySample = (value: unknown): ExecutionCapacitySample => {
  const r = record(value)
  const elapsed = r.elapsed_ms, complete = r.dimensions_complete, missing = r.missing_dimensions
  if (typeof elapsed !== 'number' || !Number.isSafeInteger(elapsed) || elapsed < 0) return invalid('elapsed_ms')
  if (typeof complete !== 'boolean' || !Array.isArray(missing)
    || missing.some(v => typeof v !== 'string' || !v)) return invalid('missing_dimensions')
  const signature = nullable(r, 'signature_sha256')
  if (complete !== (signature !== null) || complete !== (missing.length === 0)
    || (signature !== null && !/^[a-f0-9]{64}$/.test(signature))) return invalid('dimensions_complete')
  return { id: string(r, 'id'), observedAt: string(r, 'observed_at'), elapsedMs: elapsed,
    signatureSha256: signature, dimensionsComplete: complete,
    knownDimensions: record(r.known_dimensions), missingDimensions: missing as string[] }
}
export const adaptExecutionPreparationDetail = (value: unknown): ExecutionPreparationDetail => {
  const r = record(value)
  const review = record(r.review_summary), contract = record(r.review_contract)
  if (!['manual', 'policy'].includes(String(review.mode)) || contract.mode !== review.mode) invalid('review_summary')
  if (!Array.isArray(r.field_selections)) invalid('field_selections')
  if (!Array.isArray(r.field_slots)) invalid('field_slots')
  if (!Array.isArray(r.auxiliary_batches)) invalid('auxiliary_batches')
  const cancellation = r.cancellation === null ? null : record(r.cancellation)
  if ((r.stage_run === null) !== (cancellation === null)) invalid('cancellation')
  if (cancellation !== null && (typeof cancellation.allowed !== 'boolean' || typeof cancellation.reason !== 'string'
    || (cancellation.allowed && r.status === 'cancelled'))) invalid('cancellation')
  const textResponseReview = r.text_response_review === null ? null : adaptTextResponseReview(r.text_response_review)
  const recovery = r.media_archive_recovery == null ? null : record(r.media_archive_recovery)
  const uncertainRetry = r.media_uncertain_retry == null ? null : record(r.media_uncertain_retry)
  if (uncertainRetry && (typeof uncertainRetry.can_retry !== 'boolean' || !/^[a-f0-9]{64}$/.test(String(uncertainRetry.request_sha256)))) invalid('media_uncertain_retry')
  if (recovery && (typeof recovery.can_recover !== 'boolean' || (recovery.can_recover && !/^[a-f0-9]{64}$/.test(String(recovery.receipt_sha256))))) invalid('media_archive_recovery')
  if (textResponseReview?.assetJobId && textResponseReview.assetJobId !== r.asset_job) invalid('text_response_review.asset_job_id')
  return { ...adaptExecutionPreparation(r), intent: record(r.intent), resolvedInputs: record(r.resolved_inputs),
    presentation: r.presentation === null ? null : adaptPreparationPresentation(r.presentation),
    outcome: r.outcome === null ? null : adaptPreparationOutcome(r.outcome),
    textResponseReview,
    mediaUncertainRetry: uncertainRetry ? {taskId: string(uncertainRetry, 'task_id'), attemptId: string(uncertainRetry, 'attempt_id'),
      taskUpdatedAt: string(uncertainRetry, 'task_updated_at'), requestSha256: string(uncertainRetry, 'request_sha256'),
      canRetry: uncertainRetry.can_retry as boolean, reason: string(uncertainRetry, 'reason')} : null,
    mediaArchiveRecovery: recovery ? {taskId: string(recovery, 'task_id'), attemptId: string(recovery, 'attempt_id'),
      taskUpdatedAt: string(recovery, 'task_updated_at'), receiptSha256: string(recovery, 'receipt_sha256'),
      canRecover: recovery.can_recover as boolean, reason: string(recovery, 'reason')} : null,
    resourceReview: r.resource_review === null ? null : adaptExecutionResourceReview(r.resource_review),
    cancellation: cancellation === null ? null : { allowed: cancellation.allowed as boolean, reason: string(cancellation, 'reason'),
      cancelledAt: nullable(cancellation, 'cancelled_at'), cancelledBy: nullable(cancellation, 'cancelled_by') },
    responseDiagnostic: r.response_diagnostic === null ? null : adaptExecutionResponseDiagnostic(r.response_diagnostic),
    auxiliaryBatches: (r.auxiliary_batches as unknown[]).map(adaptExecutionAuxiliaryBatch),
    reviewContract: contract, reviewSummary: { mode: review.mode as 'manual' | 'policy', label: string(review, 'label'), description: string(review, 'description') },
    preparedRequest: r.prepared_request === null ? null : record(r.prepared_request),
    evaluation: r.evaluation === null ? null : record(r.evaluation),
    capacitySample: r.capacity_sample === null ? null : adaptExecutionCapacitySample(r.capacity_sample),
    fieldSelections: (r.field_selections as unknown[]).map(adaptExecutionFieldSelection),
    fieldSlots: (r.field_slots as unknown[]).map(adaptExecutionPreparationFieldSlot) }
}
const adaptTextResponseReview = (value: unknown): NonNullable<ExecutionPreparationDetail['textResponseReview']> => {
  const r = record(value)
  if (typeof r.editable !== 'boolean' || !/^[a-f0-9]{64}$/.test(String(r.original_sha256)) || typeof r.original_text !== 'string' || !Array.isArray(r.fields)) invalid('text_response_review')
  const attemptId = nullable(r, 'attempt_id'), assetJobId = nullable(r, 'asset_job_id')
  const confirmedAt = nullable(r, 'confirmed_at'), confirmedBy = nullable(r, 'confirmed_by')
  const correctedText = nullable(r, 'corrected_text'), correctedSha256 = nullable(r, 'corrected_sha256')
  if ((attemptId === null) === (assetJobId === null)
    || (confirmedAt === null ? [confirmedBy, correctedText, correctedSha256].some(v => v !== null)
      : !confirmedBy || !correctedText || !/^[a-f0-9]{64}$/.test(correctedSha256 ?? '') || r.editable)) invalid('text_response_review')
  return { id: string(r, 'id'), attemptId, assetJobId, correctedText, correctedSha256, confirmedBy, originalText: r.original_text as string,
    originalSha256: string(r, 'original_sha256'), editable: r.editable as boolean, confirmedAt: nullable(r, 'confirmed_at'), readonlyReason: string(r, 'readonly_reason'), diagnostics: record(r.diagnostics),
    fields: (r.fields as unknown[]).map(value => { const row = record(value); if (typeof row.text !== 'string') invalid('response_field.text'); return { unitKey: string(row, 'unit_key'), fieldKey: string(row, 'field_key'), label: string(row, 'label'), text: row.text as string } }), notices: adaptFieldWarnings(r.notices) }
}
const adaptFieldWarnings = (value: unknown): ExecutionFieldWarning[] => {
  if (!Array.isArray(value)) invalid('field.warnings')
  return (value as unknown[]).map(value => { const row = record(value); return { code: string(row, 'code'), message: string(row, 'message') } })
}
export const adaptExecutionResponseDiagnostic = (value: unknown): ExecutionResponseDiagnostic => {
  const r = record(value)
  if (!['recorded', 'unverifiable'].includes(String(r.state)) || !Array.isArray(r.details)) invalid('response_diagnostic')
  const taskId = nullable(r, 'task_id'), observedAt = nullable(r, 'observed_at')
  if (r.state === 'recorded' && (!taskId || !observedAt)) invalid('response_diagnostic')
  if (r.state === 'unverifiable' && (observedAt !== null || (r.details as unknown[]).length)) invalid('response_diagnostic')
  return { state: r.state as ExecutionResponseDiagnostic['state'], taskId, observedAt, summary: string(r, 'summary'),
    details: (r.details as unknown[]).map(value => { const item = record(value); return { label: string(item, 'label'), value: string(item, 'value') } }) }
}
export const adaptExecutionAuxiliaryBatch = (value: unknown): ExecutionAuxiliaryBatch => {
  const r = record(value)
  if (!['generate_fields', 'translate_fields'].includes(String(r.purpose)) || !Array.isArray(r.field_keys)
      || r.field_keys.some(key => typeof key !== 'string') || !Number.isSafeInteger(r.attempt_number)
      || (r.attempt_number as number) < 0 || typeof r.can_retry !== 'boolean'
      || typeof r.requires_uncertain_acknowledgement !== 'boolean'
      || !/^[a-f0-9]{64}$/.test(String(r.field_batch_key))) invalid('auxiliary_batches')
  return { fieldBatchKey: string(r, 'field_batch_key'), purpose: r.purpose as ExecutionAuxiliaryBatch['purpose'],
    fieldKeys: r.field_keys as string[], attemptNumber: r.attempt_number as number,
    taskId: r.task_id === null ? null : string(r, 'task_id'), preparationId: r.preparation_id === null ? null : string(r, 'preparation_id'),
    status: string(r, 'status'), reason: string(r, 'reason'), canRetry: r.can_retry as boolean,
    requiresUncertainAcknowledgement: r.requires_uncertain_acknowledgement as boolean }
}
export const adaptExecutionPreparationFieldSlot = (value: unknown): ExecutionPreparationFieldSlot => {
  const r = record(value), rule = record(r.field_contract)
  const maximum = rule.max_chars, required = rule.generation_required, next = r.next_revision
  if (!Number.isSafeInteger(maximum) || (maximum as number) < 1 || typeof required !== 'boolean'
    || !Number.isSafeInteger(next) || (next as number) < 1 || rule.field_key !== r.field_key) invalid('field_slots')
  return { unitKey: string(r, 'unit_key'), fieldKey: string(r, 'field_key'), nextRevision: next as number,
    fieldLabel: string(r, 'field_label'), latestValueId: r.latest_value_id === null ? null : string(r, 'latest_value_id'),
    effectiveLanguage: string(r, 'effective_language'),
    fieldContract: rule, sourceLanguage: string(r, 'source_language'), targetLanguage: string(r, 'target_language'),
    maxChars: maximum as number, required: required as boolean, instruction: string(rule, 'generation_instruction'),
    generationMode: string(rule, 'generation_mode') }
}

export const preparationFieldConfirmInput = (preparation: ExecutionPreparationDetail, field: ExecutionFieldDetail,
  selectForExecution: boolean, replaceAcknowledged: boolean): ExecutionFieldConfirm => {
  if (field.preparation !== preparation.id || preparation.requestFrozenAt !== null || preparation.status === 'cancelled') {
    throw new Error('此字段不属于可修改的当前准备')
  }
  const selected = preparation.fieldSelections.find(row => row.unitKey === field.unitKey && row.fieldKey === field.fieldKey)
  const replacing = selectForExecution && selected !== undefined && selected.fieldValue !== field.id
  if (replacing && !replaceAcknowledged) throw new Error('请明确确认替换当前采用的修订')
  return { expectedTextSha256: field.textSha256, expectedSourceSha256: field.sourceSha256,
    expectedPreparationRevision: preparation.revision, selectForExecution, replace: replacing,
    expectedSelectionRevision: selectForExecution && selected ? selected.revision : null }
}
export const adaptExecutionField = (value: unknown): ExecutionFieldSummary => {
  const r = record(value)
  return { id: string(r, 'id'), preparation: string(r, 'preparation'), unitKey: string(r, 'unit_key'), fieldKey: string(r, 'field_key'),
    revision: revision(r), sourceSha256: string(r, 'source_sha256'), fieldContractSha256: string(r, 'field_contract_sha256'),
    textSha256: string(r, 'text_sha256'), originKind: string(r, 'origin_kind'), originAttempt: nullable(r, 'origin_attempt'),
    originAssetJob: nullable(r, 'origin_asset_job'), status: string(r, 'status'), approvalKind: nullable(r, 'approval_kind'), approvalLabel: string(r, 'approval_label'),
    reviewedBy: r.reviewed_by === null ? null : actorId(r, 'reviewed_by'), reviewedAt: nullable(r, 'reviewed_at'), approvalPolicySha256: nullable(r, 'approval_policy_sha256'),
    createdBy: actorId(r, 'created_by'), createdAt: string(r, 'created_at'), updatedAt: string(r, 'updated_at') }
}
export const adaptExecutionFieldDetail = (value: unknown): ExecutionFieldDetail => ({ ...adaptExecutionField(value), text: string(record(value), 'text'), warnings: adaptFieldWarnings(record(value).warnings) })
export const adaptExecutionFieldActionResult = (value: unknown): ExecutionFieldActionResult => {
  const r = record(value)
  return { field: adaptExecutionFieldDetail(r.field), preparation: adaptExecutionPreparation(r.preparation),
    selection: r.selection === null ? null : adaptExecutionFieldSelection(r.selection) }
}
export const executionFieldCreatePayload = (input: ExecutionFieldCreate) => ({
  preparation_id: input.preparationId, unit_key: input.unitKey, field_key: input.fieldKey, text: input.text,
  revision: input.revision, expected_preparation_revision: input.expectedPreparationRevision,
})
export const executionFieldReviewPayload = (input: ExecutionFieldReview) => ({
  expected_text_sha256: input.expectedTextSha256, expected_source_sha256: input.expectedSourceSha256,
  expected_preparation_revision: input.expectedPreparationRevision,
})
export const executionFieldConfirmPayload = (input: ExecutionFieldConfirm) => ({
  ...executionFieldReviewPayload(input), select_for_execution: input.selectForExecution,
  replace: input.replace, expected_selection_revision: input.expectedSelectionRevision,
})
