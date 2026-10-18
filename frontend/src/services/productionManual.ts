import type { JsonObject, ProductionManualOptions, ProductionManualRunCreateInput, ProductionRunPlanEnvelope } from './platformTypes'
import type { ExecutionRequestDraft, ExecutionRequestFormContract } from './executionRequestForm'
import { buildExecutionRequestFixture } from './executionRequestForm'

const object = (value: unknown, label: string): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label}缺失或不是对象`)
  return value as Record<string, unknown>
}
const text = (value: unknown, label: string): string => {
  if (typeof value !== 'string' || !value.trim()) throw new Error(`${label}缺失`)
  return value
}
const digest = (value: unknown): string => {
  if (typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value)) throw new Error('冻结合同摘要缺失或无效，请重新读取')
  return value
}
const strings = (value: unknown, label: string): string[] => {
  if (!Array.isArray(value) || value.some(item => typeof item !== 'string')) throw new Error(`${label}目录不完整`)
  return value as string[]
}
const exactKeys = (value: object, keys: string[], label: string): void => {
  if (Object.keys(value).some(key => !keys.includes(key))) throw new Error(`${label}含未支持的配置，不能静默忽略`)
}

export interface ManualScriptStructureCheck {
  runId: string
  currentScript: JsonObject | null
  currentScriptDecisionId: string | null
  status: 'no_script' | 'structured' | 'unstructured'
  reasonCode: string
  reason: string
  episodeCount: number | null
  shotCount: number | null
}
export function adaptManualScriptStructureCheck(value: unknown): ManualScriptStructureCheck {
  const row = object(value, '剧本结构检查')
  if (!['no_script', 'structured', 'unstructured'].includes(String(row.status))) throw new Error('剧本结构检查状态无效')
  const current = row.current_script === null ? null : object(row.current_script, '检查修订')
  const decision = row.current_script_decision_id === null ? null : text(row.current_script_decision_id, '检查采用决定')
  if ((row.status === 'no_script') !== (current === null) || (current === null) !== (decision === null)) throw new Error('检查结果与采用依据不一致')
  if (current) { text(current.id, '检查修订'); digest(current.sha256) }
  if (typeof row.reason_code !== 'string' || (row.status === 'unstructured' ? row.reason_code !== 'script_structure_required' : row.reason_code !== '')) throw new Error('检查原因码缺失或冲突')
  const structured = row.status === 'structured'
  for (const count of [row.episode_count, row.shot_count]) {
    if (structured ? !Number.isSafeInteger(count) || Number(count) < 0 : count !== null) throw new Error('结构化数量缺失或与状态冲突')
  }
  if (structured && row.episode_count === 0 && row.shot_count !== 0) throw new Error('无分集时不能存在镜头数量')
  return { runId: text(row.run_id, '检查轮次'), currentScript: current as JsonObject | null, currentScriptDecisionId: decision,
    status: row.status as ManualScriptStructureCheck['status'], reasonCode: row.reason_code, reason: text(row.reason, '检查说明'),
    episodeCount: row.episode_count as number | null, shotCount: row.shot_count as number | null }
}

export const adaptProductionPlanEnvelope = (value: unknown): ProductionRunPlanEnvelope => {
  const raw = object(value, '执行计划')
  if (raw.schema_version !== 1 || !Number.isSafeInteger(raw.size_bytes) || Number(raw.size_bytes) < 1 || Number(raw.size_bytes) > 65536) {
    throw new Error('执行计划版本或大小无效，不能用当前配方补齐')
  }
  return { value: object(raw.value, '冻结内容'), schemaVersion: 1, sizeBytes: Number(raw.size_bytes), sha256: digest(raw.sha256) }
}

export const adaptProductionManualOptions = (value: unknown): ProductionManualOptions => {
  const raw = object(value, '人工制作选项'), facts = object(raw.facts, '项目事实')
  for (const key of ['confirmed_script_revisions', 'episodes', 'shots']) {
    if (!Number.isSafeInteger(facts[key]) || Number(facts[key]) < 0) throw new Error('项目已有内容统计缺失或无效')
  }
  const stageCatalog = adaptProductionPlanEnvelope(raw.stage_catalog)
  text(stageCatalog.value.key, '阶段目录标识')
  if (!Array.isArray(stageCatalog.value.stages) || !stageCatalog.value.stages.length) throw new Error('受控阶段目录为空')
  return { projectId: text(raw.project_id, '项目'), stageCatalog,
    scopeKinds: strings(raw.scope_kinds, '范围'), deliveryKinds: strings(raw.delivery_kinds, '交付'),
    initialContentKinds: strings(raw.initial_content_kinds, '初始内容'), facts: facts as Record<string, number> }
}

export const buildManualProductionRunPayload = (input: ProductionManualRunCreateInput): JsonObject => {
  const allowed = new Set(['projectId', 'planSource', 'manualPlan', 'idempotencyKey', 'allowExternalCalls', 'configOverrides'])
  if (Object.keys(input).some(key => !allowed.has(key)) || input.planSource !== 'manual') throw new Error('人工制作不能混入配方或独立模型/工作流选择')
  const plan = input.manualPlan
  if (!plan || typeof plan !== 'object' || !plan.scope || !plan.delivery) throw new Error('请先读取人工制作目录并选择本次范围')
  exactKeys(plan, ['stageCatalogKey', 'expectedStageCatalogSha256', 'scope', 'delivery', 'initialContent'], '人工计划')
  let scope: JsonObject
  if (plan.scope.kind === 'project') {
    if (Object.keys(plan.scope).length !== 1) throw new Error('项目范围不能同时指定剧集数组')
    scope = { kind: 'project' }
  } else if (plan.scope.kind === 'episodes') {
    exactKeys(plan.scope, ['kind', 'episodeIds'], '剧集范围')
    const ids = plan.scope.episodeIds
    if (!Array.isArray(ids) || !ids.length || new Set(ids).size !== ids.length || ids.some(id => typeof id !== 'string' || !id.trim())) throw new Error('请明确选择不重复的剧集')
    scope = { kind: 'episodes', episode_ids: [...ids].sort() }
  } else throw new Error('请选择本次生产范围')
  if (plan.delivery.kind !== 'episode_masters') throw new Error('交付方式不在当前目录内')
  exactKeys(plan.delivery, ['kind'], '交付方式')
  const content = plan.initialContent
  if (content !== null && (!content || content.kind !== 'script_revision')) throw new Error('请明确选择已确认剧本或从空白开始')
  if (content !== null) exactKeys(content, ['kind', 'id', 'expectedSha256'], '初始内容')
  const payload: JsonObject = { project_id: text(input.projectId, '项目'), plan_source: 'manual',
    idempotency_key: text(input.idempotencyKey, '幂等键'), config_overrides: input.configOverrides ?? {},
    manual_plan: { stage_catalog_key: text(plan.stageCatalogKey, '阶段目录'), expected_stage_catalog_sha256: digest(plan.expectedStageCatalogSha256),
      scope, delivery: { kind: plan.delivery.kind }, initial_content: content === null ? null : {
        kind: content.kind, id: text(content.id, '已确认剧本'), expected_sha256: digest(content.expectedSha256),
      } } }
  if (input.allowExternalCalls != null) {
    if (typeof input.allowExternalCalls !== 'boolean') throw new Error('外发授权必须明确选择')
    payload.allow_external_calls = input.allowExternalCalls
  }
  return payload
}

export interface ManualScriptIdentity {
  sourceName: string; sourceTypeLabel: string; sourceFormatLabel: string
  decisionStageKey: 'script_input' | 'script_review'
  candidate: { artifactId: string; variantIndex: number; stageId: string; stageKey: string; attemptId: string; preparationId: string; editedFromRevisionId?: string | null } | null
}

export interface ManualActionOptions {
  executionActivity: ManualExecutionActivity[]
  runId: string
  runPlanSha256: string
  contentChangeStageKeys: string[]
  scriptAdoption: { revision: number; decisionId: string | null; content: JsonObject | null }
  scriptReview: { revision: number; decisionId: string | null; content: JsonObject | null }
  visualRulesReview: { revision: number; decisionId: string | null; status: 'not_reviewed' | 'current' | 'changed'; reasons: string[] }
  currentScript: JsonObject | null
  currentScriptDecisionId: string | null
  currentScriptIdentity: ManualScriptIdentity | null
  actions: { stageKey: string; action: string; operation: string | null; outputKinds: string[]; scopeKinds: ManualScopeKind[]; sourceStageKeys?: string[] }[]
}

export interface ManualExecutionActivity {
  stageId: string; stageKey: string; stageStatus: string; lifecycleLabel: string
  preparations: { status: string; label: string; count: number }[]
  calls: { status: string; label: string; count: number; role: string; roleLabel: string }[]
}

const activityCounts = (value: unknown) => {
  if (!Array.isArray(value)) throw new Error('执行活动统计缺失，不能视为零次调用')
  return value.map(item => {
    const row = object(item, '执行活动')
    if (!Number.isSafeInteger(row.count) || Number(row.count) < 1) throw new Error('执行活动数量无效')
    return { status: text(row.status, '活动状态'), label: text(row.label, '状态名称'), count: Number(row.count) }
  })
}
export const adaptManualExecutionActivity = (value: unknown): ManualExecutionActivity[] => {
  if (!Array.isArray(value)) throw new Error('执行活动记录缺失，请重新读取')
  const rows = value.map(item => {
    const row = object(item, '步骤活动'), counts = activityCounts(row.calls)
    const calls = counts.map((count, index) => {
      const call = (row.calls as JsonObject[])[index]!
      return { ...count, role: text(call.role, '调用类型'), roleLabel: text(call.role_label, '调用类型名称') }
    })
    return { stageId: text(row.stage_id, '阶段'), stageKey: text(row.stage_key, '步骤'),
      stageStatus: text(row.stage_status, '阶段状态'), lifecycleLabel: text(row.lifecycle_label, '阶段名称'),
      preparations: activityCounts(row.preparations), calls }
  })
  if (new Set(rows.map(row => row.stageId)).size !== rows.length) throw new Error('执行活动阶段重复')
  return rows
}

export const manualScopeLabels = { project: '项目', episode: '剧集', shot: '镜头', character: '角色', scene: '场景', prop: '道具' }
export type ManualScopeKind = keyof typeof manualScopeLabels
export interface ManualTarget { scope: { kind: ManualScopeKind; id: string }; unitKey: string; label: string }
export interface ManualTargetPage {
  runId: string; runPlanSha256: string; count: number; next: string | null; previous: string | null; results: ManualTarget[]
}
const scopeKinds = (value: unknown): ManualScopeKind[] => {
  const kinds = strings(value, '制作对象类型')
  if (!kinds.length || new Set(kinds).size !== kinds.length || kinds.some(kind => !Object.prototype.hasOwnProperty.call(manualScopeLabels, kind))) throw new Error('制作对象类型目录不完整')
  return kinds as ManualScopeKind[]
}
export const adaptManualTargetPage = (value: unknown): ManualTargetPage => {
  const raw = object(value, '制作对象目录')
  if (!Number.isSafeInteger(raw.count) || Number(raw.count) < 0 || !Array.isArray(raw.results)
    || raw.results.length > Number(raw.count)) throw new Error('制作对象分页不完整')
  const results = raw.results.map(value => {
    const row = object(value, '制作对象'), scope = object(row.scope, '对象定位')
    const kind = scopeKinds([scope.kind])[0]!, id = text(scope.id, '对象标识'), unitKey = text(row.unit_key, '生产单元')
    if (!/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(id) || unitKey !== `${kind}:${id}`) throw new Error('制作对象定位不一致')
    return { scope: { kind, id }, unitKey, label: text(row.label, '制作对象名称') }
  })
  if (new Set(results.map(row => row.unitKey)).size !== results.length) throw new Error('制作对象目录重复')
  return { runId: text(raw.run_id, '运行'), runPlanSha256: digest(raw.run_plan_sha256), count: Number(raw.count), results,
    next: raw.next === null ? null : text(raw.next, '下一页'), previous: raw.previous === null ? null : text(raw.previous, '上一页') }
}

const scriptStep = (value: unknown) => {
  const adopted = object(value, '剧本采用步骤')
  if (!Number.isSafeInteger(adopted.revision) || Number(adopted.revision) < 0) throw new Error('人工动作修订缺失')
  const content = adopted.content === null ? null : object(adopted.content, '已采用内容')
  if (adopted.revision === 0 ? (adopted.decision_id !== null || content !== null) : (adopted.decision_id === null || content === null)) throw new Error('人工采用决策和修订不一致')
  if (content) { text(content.id, '已确认剧本'); digest(content.sha256) }
  return { revision: Number(adopted.revision), decisionId: adopted.decision_id === null ? null : text(adopted.decision_id, '采用决策'), content }
}
const readScriptIdentity = (value: unknown, current: JsonObject | null, reviewed: boolean): ManualScriptIdentity | null => {
  if (current === null) {
    if (value !== null) throw new Error('当前剧本身份与采用状态不一致')
    return null
  }
  const row = object(value, '当前剧本身份'), stage = reviewed ? 'script_review' : 'script_input'
  if (typeof row.source_name !== 'string' || row.decision_stage_key !== stage) throw new Error('当前剧本身份与采用决定不一致')
  let candidate: ManualScriptIdentity['candidate'] = null
  if (reviewed) {
    const item = object(row.candidate, '当前采用候选')
    if (!Number.isSafeInteger(item.variant_index) || Number(item.variant_index) < 1) throw new Error('当前采用候选编号缺失')
    candidate = { artifactId: text(item.artifact_id, '候选产物'), variantIndex: Number(item.variant_index),
      stageId: text(item.stage_id, '生成步骤'), stageKey: text(item.stage_key, '生成步骤'),
      attemptId: text(item.attempt_id, '原调用'), preparationId: text(item.preparation_id, '原准备'),
      ...(item.edited_from_revision_id != null ? { editedFromRevisionId: text(item.edited_from_revision_id, '人工编辑依据修订') } : {}) }
  } else if (row.candidate !== null) throw new Error('改选已确认剧本不能伪装成候选审核')
  return { sourceName: row.source_name, sourceTypeLabel: text(row.source_type_label, '剧本来源类型'),
    sourceFormatLabel: text(row.source_format_label, '剧本原稿格式'), decisionStageKey: stage, candidate }
}

export const adaptManualActionOptions = (value: unknown): ManualActionOptions => {
  const raw = object(value, '人工动作')
  if (!Array.isArray(raw.actions)) throw new Error('人工动作目录缺失')
  const initial = scriptStep(raw.script_adoption), reviewed = scriptStep(raw.script_review)
  const visual = object(raw.visual_rules_review, '影视规则审核')
  if (!Number.isSafeInteger(visual.revision) || Number(visual.revision) < 0 || (visual.revision === 0) !== (visual.decision_id === null)) throw new Error('影视规则审核修订和决策不一致')
  if (!['not_reviewed','current','changed'].includes(String(visual.status))) throw new Error('影视规则审核依据状态缺失')
  const visualReasons = strings(visual.reasons, '影视规则依据变化说明')
  if ((visual.status === 'not_reviewed') !== (visual.revision === 0)
    || (visual.status === 'changed') !== (visualReasons.length > 0) || visualReasons.some(reason => !reason.trim())) throw new Error('影视规则审核依据和变化说明不一致')
  const current = raw.current_script === null ? null : object(raw.current_script, '本轮当前剧本')
  const currentDecisionId = raw.current_script_decision_id === null ? null : text(raw.current_script_decision_id, '当前剧本采用决定')
  const selected = [initial, reviewed].filter(step => step.decisionId !== null && step.decisionId === currentDecisionId)
  if (currentDecisionId === null ? (current !== null || initial.decisionId !== null || reviewed.decisionId !== null)
    : selected.length !== 1) throw new Error('本轮当前剧本缺少唯一采用决定')
  const expected = selected[0]?.content ?? null
  if ((current === null) !== (expected === null) || (current && (current.id !== expected?.id || current.sha256 !== expected?.sha256))) throw new Error('本轮当前剧本与采用记录不一致')
  return { runId: text(raw.run_id, '运行'), runPlanSha256: digest(raw.run_plan_sha256),
    executionActivity: adaptManualExecutionActivity(raw.execution_activity),
    contentChangeStageKeys: strings(raw.content_change_stage_keys, '改选影响步骤'),
    scriptAdoption: initial, scriptReview: reviewed, currentScript: current, currentScriptDecisionId: currentDecisionId,
    currentScriptIdentity: readScriptIdentity(raw.current_script_identity, current, currentDecisionId !== null && currentDecisionId === reviewed.decisionId),
    visualRulesReview: { revision: Number(visual.revision), decisionId: visual.decision_id === null ? null : text(visual.decision_id, '影视规则决策'),
      status: visual.status as ManualActionOptions['visualRulesReview']['status'], reasons: visualReasons },
    actions: raw.actions.map(value => { const row = object(value, '动作'), action = text(row.action, '动作')
      if (row.operation !== null && (typeof row.operation !== 'string' || !row.operation.trim())) throw new Error('人工动作能力目录不完整')
      const outputKinds = strings(row.output_kinds, '人工动作能力目录'), scopes = scopeKinds(row.scope_kinds)
      if (outputKinds.some(kind => !kind) || new Set(outputKinds).size !== outputKinds.length
        || (action === 'generate' ? (row.operation === null) !== Boolean(outputKinds.length)
          : row.operation !== null || outputKinds.length > 0)) throw new Error('人工动作能力目录不完整')
      const sourceStageKeys = row.source_stage_keys === undefined ? undefined : strings(row.source_stage_keys, '审核来源步骤')
      if (action === 'approve' && ['script_review', 'visual_rules_review'].includes(String(row.stage_key)) && (!sourceStageKeys?.length || new Set(sourceStageKeys).size !== sourceStageKeys.length)) throw new Error('审核来源步骤目录不完整')
      return { stageKey: text(row.stage_key, '步骤'), action, operation: row.operation as string | null, outputKinds, scopeKinds: scopes, sourceStageKeys } }) }
}

export const buildManualTextApproval = (input: { projectId: string; options: ManualActionOptions; idempotencyKey: string;
  artifactId: string; artifactSha256: string; manifestSha256: string; comment: string } & (
    { stageKey: 'script_review'; scriptContent?: string } |
    { stageKey: 'visual_rules_review'; candidateOverrides: Record<string, string> })): JsonObject => {
  if (!input.options.actions.some(row => row.stageKey === input.stageKey && row.action === 'approve')) throw new Error('此运行未开放所选文本审核')
  const visual = input.stageKey === 'visual_rules_review'
  return { action: 'approve', stage_key: input.stageKey, unit_key: `project:${text(input.projectId, '项目')}`,
    scope: { kind: 'project', id: input.projectId }, expected_run_plan_sha256: digest(input.options.runPlanSha256),
    expected_selection_revision: visual ? input.options.visualRulesReview.revision : input.options.scriptReview.revision, idempotency_key: text(input.idempotencyKey, '请求标识'),
    payload: { text_candidate: { artifact_id: text(input.artifactId, '候选'), expected_sha256: digest(input.artifactSha256),
      expected_manifest_sha256: digest(input.manifestSha256) },
      ...(visual ? { candidate_overrides: { ...input.candidateOverrides } } : {
        expected_current_script: input.options.currentScript,
        ...(input.scriptContent !== undefined ? { script_content: input.scriptContent } : {}) }), comment: input.comment } }
}

export const validateManualGenerationContract = (action: ManualActionOptions['actions'][number] | undefined, contract: ExecutionRequestFormContract): void => {
  if (!action || action.action !== 'generate' || (action.operation !== null
    ? action.operation !== contract.operation || contract.workflowVersionId !== null
    : !action.outputKinds.includes(contract.outputKind))) throw new Error('实例能力或输出类型与当前人工步骤不一致')
}

export const buildManualGeneration = (input: { projectId: string; options: ManualActionOptions; stageKey: string;
  target: ManualTarget; idempotencyKey: string; contract: ExecutionRequestFormContract; draft: ExecutionRequestDraft }): JsonObject => {
  const action = input.options.actions.find(row => row.stageKey === input.stageKey && row.action === 'generate')
  validateManualGenerationContract(action, input.contract)
  const projectId = text(input.projectId, '项目')
  const target = input.target
  if (!target || !action!.scopeKinds.includes(target.scope.kind) || !target.scope.id
    || target.unitKey !== `${target.scope.kind}:${target.scope.id}`
    || (target.scope.kind === 'project' && target.scope.id !== projectId)) throw new Error('请从本轮目录中明确选择制作对象')
  const fixture = buildExecutionRequestFixture(input.contract, input.draft, { productionRunId: input.options.runId, productionScope: target.scope })
  const reviewed = Object.values(input.draft.media).flat().flatMap(row=>row.reviewedShot ? [row.reviewedShot] : [])
  const pin = reviewed[0]?.review
  if (reviewed.length && (input.stageKey !== 'media_generation' || !pin
    || reviewed.some(row=>row.review.decision_id!==pin.decision_id || row.review.snapshot_sha256!==pin.snapshot_sha256)))
    throw new Error('本次镜头用途必须来自同一份本轮已审核清单，请重新读取选择')
  // Validate the inputs actually submitted. Unticked optional fields may keep
  // an unconfirmed picker draft, but are not part of this request.
  for (const value of Object.values(fixture.inputs)) {
    const row = object(value, '执行输入')
    if (row.kind === 'context_field' && object(row.scope, '输入范围').id !== projectId) throw new Error('输入内容必须来自本轮项目')
  }
  return { action: 'generate', stage_key: input.stageKey, unit_key: target.unitKey, scope: { ...target.scope },
    expected_run_plan_sha256: digest(input.options.runPlanSha256), expected_selection_revision: null,
    idempotency_key: text(input.idempotencyKey, '请求标识'), payload: { execution_instance_version_id: input.contract.versionId,
      expected_definition_sha256: digest(input.contract.sha256), ...fixture,
      ...(pin ? {expected_storyboard_review: {...pin}} : {}) } }
}

export const manualGenerationTarget = (raw: JsonObject): string => {
  if (raw.action !== 'generate' || raw.result_kind !== 'preparation' || typeof raw.target_id !== 'string'
    || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i.test(raw.target_id)) throw new Error('未返回有效执行准备，请重试同一请求核对结果')
  return raw.target_id
}

export const buildManualScriptAdoption = (input: { projectId: string; options: ManualActionOptions;
  idempotencyKey: string; revisionId: string; contentSha256: string; comment: string }): JsonObject => {
  if (!input.options.actions.some(row => row.stageKey === 'script_input' && row.action === 'adopt')) throw new Error('此运行未开放剧本采用动作')
  return { action: 'adopt', stage_key: 'script_input', unit_key: `project:${text(input.projectId, '项目')}`,
    scope: { kind: 'project', id: input.projectId }, expected_run_plan_sha256: digest(input.options.runPlanSha256),
    expected_selection_revision: input.options.scriptAdoption.revision, idempotency_key: text(input.idempotencyKey, '请求标识'),
    payload: { comment: input.comment, content: { kind: 'script_revision', id: text(input.revisionId, '已确认剧本'), expected_sha256: digest(input.contentSha256) } } }
}
