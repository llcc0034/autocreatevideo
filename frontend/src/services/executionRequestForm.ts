import { adaptExecutionConfiguration, adaptExecutionLanguageForm, adaptExecutionSubjectCategories, type ExecutionLanguageForm, type ExecutionContract, type ResourceSmokeContract, type ExecutionInput, type ExecutionScalar } from './executionRegistry'
import type { ExecutionSmokeRequest } from './executionPreparation'
import type { JsonObject } from './platformTypes'
import { emptyJsonInput, jsonInputErrors } from './structuredInputForm'
import { executionMediaSelection } from './executionMediaSourcePolicy'
import type { ExactReferenceSelection } from './exactReferencePreview'

export interface ExecutionValueRule {
  type: 'integer' | 'number' | 'string' | 'boolean'; required: boolean
  minimum?: number; maximum?: number; min_length?: number; max_length?: number
  enum?: Exclude<ExecutionScalar, null>[]; const?: Exclude<ExecutionScalar, null>; default?: Exclude<ExecutionScalar, null>
  alignments?: { modulus: number; remainder: number }[]; seed_policy?: 'fixed' | 'generate_once'
}
export interface ExecutionRequestFormContract<VersionId extends string | null = string> {
  subjectCategories: ExecutionContract['subject_categories']
  languageForm: ExecutionLanguageForm
  versionId: VersionId; sha256: string; operation: string; mode: string
  outputKind: string; workflowVersionId: string | null
  framePolicy?: JsonObject | null
  inputs: ExecutionInput[]; parameters: { key: string; rule: ExecutionValueRule; source: string | null; evidence?: unknown }[]
  promptMode: 'profile' | 'manual' | 'none'; channels: { key: string; rule: ExecutionValueRule }[]
  serverOwnedInputs: string[]
  contextFields: ExecutionContract['context_fields']
  sourceConstraints: Record<string, { kinds: string[]; selectors: string[] | null }>
  structureSchemas?: Record<string, JsonObject[]>
  inputForms?: ExecutionContract['input_forms']
  purposes: Record<string, { value: string; label: string; description: string }[]>
}
export interface ExecutionMediaInputDraft {
  itemKey: string; assetId: string; purpose: string | null; pairedTo: string | null; subjectId: string | null
  selectedOutput?: ExecutionSelectedMediaSource
  reviewedShot?: ExecutionReviewedShotSource
  videoAudio?: boolean
}
export interface ExecutionReviewedShotSource {
  runId: string; shotId: string; referenceKey: string; assetId: string; kind: string; name: string
  subjectRef: { kind: string; id: string } | null; creativePurpose: string | null
  review: { decision_id: string; snapshot_sha256: string }
  preview: ExactReferenceSelection
}
export interface ExecutionSelectedMediaSource {
  runId: string; artifactId: string; decisionId: string; kind: string; unitKey: string; revision: number
}
export interface ExecutionRequestDraft {
  mediaSourceModes?: Record<string, string>
  literals: Record<string, { enabled: boolean; value: unknown; reference?: { token: string; label: string; content: string } }>
  contexts: Record<string, { selectorKey: string; projectId: string; revisionId: string } | null>
  media: Record<string, ExecutionMediaInputDraft[]>
  parameters: Record<string, ExecutionScalar>; channels: Record<string, string>
  subjects: { id: string; category: string; name: string; description: string }[]
  sourceLanguage: string; targetLanguage: string
}
const fail = (field: string): never => { throw new Error(`实例输入合同不完整：${field}`) }
const object = (v: unknown, key: string): Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : fail(key)
const has = (v: object, key: string) => Object.prototype.hasOwnProperty.call(v, key)
const scalar = (v: unknown): v is Exclude<ExecutionScalar, null> => typeof v === 'string' || typeof v === 'boolean' || (typeof v === 'number' && Number.isFinite(v))
export const executionValueRule = (value: unknown, key: string): ExecutionValueRule => {
  const r = object(value, key)
  if (!['integer', 'number', 'string', 'boolean'].includes(r.type as string) || typeof r.required !== 'boolean') fail(key)
  for (const field of ['minimum', 'maximum', 'min_length', 'max_length']) {
    if (has(r, field) && (typeof r[field] !== 'number' || !Number.isFinite(r[field])
    || (field.endsWith('length') && !Number.isSafeInteger(r[field])))) fail(`${key}.${field}`)
  }
  for (const field of ['const', 'default']) if (has(r, field) && !scalar(r[field])) fail(`${key}.${field}`)
  if (has(r, 'enum') && (!Array.isArray(r.enum) || !r.enum.length || r.enum.some(v => !scalar(v) || (r.type === 'integer' && !Number.isSafeInteger(v))))) fail(`${key}.enum`)
  if (has(r, 'alignments') && (!Array.isArray(r.alignments) || r.alignments.some(v => {
    const row = object(v, key)
    return typeof row.modulus !== 'number' || !Number.isFinite(row.modulus) || row.modulus <= 0
      || typeof row.remainder !== 'number' || !Number.isFinite(row.remainder) || row.remainder < 0 || row.remainder >= row.modulus
  }))) fail(`${key}.alignments`)
  if (has(r, 'seed_policy') && !['fixed', 'generate_once'].includes(r.seed_policy as string)) fail(`${key}.seed_policy`)
  return r as unknown as ExecutionValueRule
}
export function executionRequestFormContract(contract: ExecutionContract): ExecutionRequestFormContract {
  if (!contract.frozen || typeof contract.version_id !== 'string' || !contract.version_id) fail('请使用已发布精确版本')
  return requestFormContract(contract)
}
export function resourceSmokeRequestFormContract(contract: ResourceSmokeContract): ExecutionRequestFormContract<null> {
  if (contract.scope !== 'resource_smoke' || contract.version_id !== null || 'frozen' in contract || 'revision' in contract) fail('请使用明确的基础资源合同')
  return requestFormContract(contract)
}
function requestFormContract<VersionId extends string | null>(contract: {
  version_id: VersionId; sha256: string; definition: ExecutionContract['definition']; context_fields: ExecutionContract['context_fields']
  input_forms: ExecutionContract['input_forms']
  language_form: ExecutionLanguageForm
  subject_categories: ExecutionContract['subject_categories']
}): ExecutionRequestFormContract<VersionId> {
  const definition = contract.definition, config = adaptExecutionConfiguration(definition.configuration)
  const intf = object(definition.interface, 'interface'), prompt = object(intf.prompt, 'prompt')
  if (definition.version_id !== contract.version_id || prompt.mode !== config.prompt_mode
    || !/^[a-f0-9]{64}$/.test(contract.sha256)) fail('请使用已发布精确版本')
  const output = object(intf.output, 'output')
  const outputKind = config.workflow_version_id === null ? output.kind
    : object(object(output.contract, 'output.contract').primary, 'output.contract.primary').kind
  if (typeof outputKind !== 'string' || !outputKind) fail('output.kind')
  const purposes: ExecutionRequestFormContract['purposes'] = {}
  const sourceConstraints: ExecutionRequestFormContract['sourceConstraints'] = {}
  const structureSchemas: Record<string, JsonObject[]> = {}
  for (const input of config.inputs.filter(row=>['text','json'].includes(row.kind))) {
    let kinds = ['literal','context_field','selected_output','structured','project_snapshot'], selectors: string[] | null = null
    for (const target of input.targets.filter(row=>['operation_input','profile_input'].includes(row.target_kind))) {
      const group = target.target_kind === 'profile_input' ? 'profile_inputs' : 'input_targets'
      const spec = object(object(intf[group] || {},group)[target.target_key] || {},target.target_key)
      for (const key of ['source_kinds','context_selectors']) if (has(spec,key) && (!Array.isArray(spec[key]) || !(spec[key] as unknown[]).length || (spec[key] as unknown[]).some(v=>typeof v!=='string'))) fail(key)
      if (has(spec,'source_kinds')) kinds = kinds.filter(value=>(spec.source_kinds as string[]).includes(value))
      if (has(spec,'context_selectors')) selectors = selectors === null ? spec.context_selectors as string[] : selectors.filter(value=>(spec.context_selectors as string[]).includes(value))
    }
    if (!input.targets.some(target => target.target_kind === 'profile_input'
      && object(object(intf.profile_inputs || {},'profile_inputs')[target.target_key] || {},target.target_key).source_reader)) kinds = kinds.filter(kind=>kind!=='project_snapshot')
    if (!kinds.length) fail(`${input.key} 的来源限制没有交集`)
    sourceConstraints[input.key] = {kinds, selectors}
    structureSchemas[input.key] = [...(contract.input_forms?.[input.key]?.schemas || []), ...input.targets.flatMap(target => {
      const group = target.target_kind === 'profile_input' ? 'profile_inputs' : 'input_targets'
      const spec = object(object(intf[group] || {},group)[target.target_key] || {},target.target_key)
      if (spec.value_schema !== undefined) return [object(spec.value_schema,'value_schema') as JsonObject]
      const domain=String(spec.value_domain||'')
      if(!['unit_context','string_list','string','number','boolean'].includes(domain))return []
      const schema:JsonObject=domain==='unit_context'?{type:'object',additionalProperties:{}}:domain==='string_list'?{type:'array',items:{type:'string'}}:{type:domain}
      if(Array.isArray(spec.enum))schema.enum=spec.enum
      return [schema]
    })].filter((schema,index,rows)=>rows.findIndex(row=>JSON.stringify(row)===JSON.stringify(schema))===index)
  }
  for (const [kind, values] of Object.entries(object(intf.input_purposes, 'input_purposes'))) {
    if (!Array.isArray(values)) fail('input_purposes')
    purposes[kind] = (values as unknown[]).map(value => {
      const row = object(value, kind)
      if (typeof row.value !== 'string' || typeof row.label !== 'string' || typeof row.description !== 'string') fail('input_purposes')
      return { value: row.value as string, label: row.label as string, description: row.description as string }
    })
  }
  return { versionId: contract.version_id, sha256: contract.sha256, operation: config.operation, mode: config.mode,
    subjectCategories: adaptExecutionSubjectCategories(contract.subject_categories),
    languageForm: adaptExecutionLanguageForm(contract.language_form),
    outputKind: outputKind as string, workflowVersionId: config.workflow_version_id,
    framePolicy: intf.frame_policy == null ? null : object(intf.frame_policy, 'frame_policy') as JsonObject,
    serverOwnedInputs: config.inputs.filter(row => row.targets.some(target => target.target_kind === 'operation_input'
      && object(object(intf.input_targets, 'input_targets')[target.target_key],target.target_key).server_owned === true)).map(row=>row.key),
    inputs: [...config.inputs].sort((a, b) => a.position - b.position), promptMode: config.prompt_mode, purposes, contextFields: contract.context_fields, sourceConstraints, structureSchemas, inputForms: contract.input_forms,
    parameters: Object.entries(object(intf.parameters, 'parameters')).filter(([key, value]) => !(key === 'max_tokens' && Array.isArray((value as { layers?: unknown[] }).layers) && ((value as { layers: { source?: string }[] }).layers).some(layer => layer.source === 'system.execution.resources.review_policy'))).map(([key, value]) => {
      const row = object(value, key)
      if (row.value_source !== null && typeof row.value_source !== 'string') fail(`${key}.value_source`)
      return { key, rule: executionValueRule(row.rule, key), source: row.value_source as string | null, evidence: row }
    }), channels: Object.entries(object(prompt.channels, 'channels')).map(([key, value]) => ({ key, rule: executionValueRule(object(value, key).rule, key) })) }
}
export function emptyExecutionRequestDraft(contract: ExecutionRequestFormContract<string | null>): ExecutionRequestDraft {
  const contexts = Object.fromEntries(contract.inputs.map(row => {
    const constraint = contract.sourceConstraints[row.key]
    const source = constraint?.kinds.length === 1 && constraint.kinds[0] === 'context_field' && constraint.selectors?.length === 1
      ? contract.contextFields.find(option => option.selector_key === constraint.selectors![0] && option.kind === row.kind
        && option.scope_kind === 'project' && option.revision_kind === 'visual_rules_approval') : undefined
    // The strategy already chose this source. Only the actual approval still
    // needs explicit preview/confirmation; never infer a revision or project.
    return [row.key, source && row.semantic_contract.allowed_scope_kinds.includes('project')
      ? { selectorKey: source.selector_key, projectId: '', revisionId: '' } : null]
  }))
  return { contexts, literals: Object.fromEntries(contract.inputs.filter(row => ['text', 'json'].includes(row.kind))
    .map(row => [row.key, { enabled: row.required || row.min_items > 0, value: row.kind === 'text' ? '' : emptyJsonInput(contract.structureSchemas?.[row.key] || []) }])),
    media: Object.fromEntries(contract.inputs.filter(row => ['image', 'video', 'audio'].includes(row.kind)).map(row => [row.key, []])),
    mediaSourceModes: Object.fromEntries(contract.inputs.filter(row=>row.media_source_policy).map(row=>[row.key,row.media_source_policy!.default_source])),
    parameters: {}, channels: {}, subjects: [], sourceLanguage: '', targetLanguage: '' }
}
export function executionRuleDescription(r: ExecutionValueRule): string {
  const display = (value: unknown) => r.type === 'integer' && typeof value === 'number' && !Number.isSafeInteger(value)
    ? '超出页面精确整数范围，由服务端保留并校验' : JSON.stringify(value)
  const parts = [r.required ? '必填' : '可选', r.type]
  if (has(r, 'minimum') || has(r, 'maximum')) parts.push(`${has(r, 'minimum') ? display(r.minimum) : '未声明下限'} ～ ${has(r, 'maximum') ? display(r.maximum) : '未声明上限'}`)
  if (has(r, 'min_length') || has(r, 'max_length')) parts.push(`长度 ${r.min_length ?? '未声明下限'} ～ ${r.max_length ?? '未声明上限'}`)
  if (r.enum) parts.push(`可选值 ${r.enum.map(display).join('、')}`)
  for (const alignment of r.alignments ?? []) parts.push(`值减 ${display(alignment.remainder)} 后是 ${display(alignment.modulus)} 的整数倍`)
  if (has(r, 'const')) parts.push(`固定值 ${display(r.const)}`)
  else if (r.seed_policy === 'generate_once') parts.push('首次准备时生成一次；继续与重试不变')
  else if (has(r, 'default')) parts.push(`合同默认 ${display(r.default)}`)
  return parts.join(' · ')
}
function validateValue(value: unknown, r: ExecutionValueRule, label: string) {
  if (r.type === 'integer' ? !Number.isSafeInteger(value)
    : r.type === 'number' ? typeof value !== 'number' || !Number.isFinite(value)
    : typeof value !== r.type) throw new Error(`${label} 不符合 ${r.type} 类型`)
  if (has(r, 'const') && value !== r.const) throw new Error(`${label} 不能修改固定值`)
  if (r.enum && !r.enum.some(item => Object.is(item, value))) throw new Error(`${label} 不在可选值中`)
  if (typeof value === 'number') {
    if ((r.minimum !== undefined && value < r.minimum) || (r.maximum !== undefined && value > r.maximum)) throw new Error(`${label} 超出范围`)
    // The server performs exact rational alignment checks. Do not implement
    // a second floating-point lattice solver or silently round input here.
  } else if (typeof value === 'string') {
    const length = Array.from(value).length
    if ((r.min_length !== undefined && length < r.min_length) || (r.max_length !== undefined && length > r.max_length)) throw new Error(`${label} 超出长度范围`)
  }
}
export async function prepareExecutionSelectedSources(draft: ExecutionRequestDraft): Promise<ExecutionRequestDraft> {
  const result = { ...draft, media: Object.fromEntries(Object.entries(draft.media).map(([key, rows]) =>
    [key, rows.map(row => ({ ...row, ...(row.selectedOutput ? { selectedOutput: { ...row.selectedOutput } } : {}) }))])) }, remap = new Map<string, string>()
  const seen = new Set<string>()
  for (const rows of Object.values(result.media)) for (const row of rows) {
    if (!row.itemKey || seen.has(row.itemKey)) throw new Error('素材使用标识重复或缺失，无法确定配对关系')
    seen.add(row.itemKey)
  }
  for (const [inputKey, rows] of Object.entries(result.media)) for (const row of rows) {
    if (!row.selectedOutput && !row.reviewedShot) continue
    // Same fixed scalar object and sorted keys as selected_output_item_key.
    // This is a stable use identity, never an approval or source lookup.
    if (row.selectedOutput && row.reviewedShot) throw new Error('不能混用本镜用途和上游产物来源')
    const value = row.reviewedShot ? { input_key: inputKey, reference_key: row.reviewedShot.referenceKey,
      shot_id: row.reviewedShot.shotId } : { artifact_id: row.selectedOutput!.artifactId, decision_id: row.selectedOutput!.decisionId,
      input_key: inputKey, purpose: row.purpose }
    const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(value)))
    const key = (row.reviewedShot ? 'reviewed:' : 'selected:') + Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join('')
    remap.set(row.itemKey, key); row.itemKey = key
  }
  for (const rows of Object.values(result.media)) for (const row of rows) if (row.pairedTo && remap.has(row.pairedTo)) row.pairedTo = remap.get(row.pairedTo)!
  return result
}
export function buildExecutionRequestFixture(contract: ExecutionRequestFormContract<string | null>, draft: ExecutionRequestDraft,
  context?: { productionRunId?: string; productionScope?: { kind: string; id: string }; managedInputKeys?: readonly string[] }): ExecutionSmokeRequest['fixture'] {
  for (const [key, label, rule] of [
    ['sourceLanguage', '输入语言', contract.languageForm.source],
    ['targetLanguage', '提示词目标语言', contract.languageForm.target],
  ] as const) {
    const value = draft[key].trim()
    if (rule.required && !value) throw new Error(`请选择${label}（用于：${rule.fields.join('、')}）`)
    if (value.length > 40) throw new Error(`${label}不能超过40个字符`)
    if (value && !contract.languageForm.allow_custom && !contract.languageForm.choices.some(row => row.value === value)) throw new Error(`${label}不在服务端可选目录中`)
  }
  const inputs: JsonObject = {}, parameters: JsonObject = {}, channels: Record<string, string> = {}
  const managed = context?.managedInputKeys ?? []
  if (new Set(managed).size !== managed.length || managed.some(key => !contract.inputs.some(row => row.key === key && row.kind === 'json'))) {
    throw new Error('页面托管输入必须是合同中明确的结构化槽位')
  }
  const selected = new Map<string, { inputKey: string; scope: { kind: string; id?: string } }>(), paired = new Set<string>()
  if ([...Object.keys(draft.literals), ...Object.keys(draft.media), ...Object.keys(draft.contexts)].some(key => !contract.inputs.some(row => row.key === key))) throw new Error('存在合同之外的输入')
  for (const spec of contract.inputs) {
    // The caller's formal API assembles and validates these exact inputs from
    // real sources; do not send empty placeholders or invent source values.
    if (managed.includes(spec.key)) continue
    if (!['text', 'json', 'image', 'video', 'audio'].includes(spec.kind)) throw new Error(`${spec.key} 输入类型尚未接入，不能忽略`)
    if (['text', 'json'].includes(spec.kind)) {
      const row = draft.literals[spec.key]
      if (!row?.enabled) { if (spec.required || spec.min_items) throw new Error(`请填写 ${spec.key}`); continue }
      if (contract.sourceConstraints[spec.key]!.kinds.includes('project_snapshot')) {
        if (spec.kind !== 'text' || !row.reference?.token || row.value !== row.reference.content || !row.reference.content.trim()) {
          throw new Error(`${contract.inputForms?.[spec.key]?.label || spec.key} 需要预览并确认项目内容，不能手工替换已保存事实`)
        }
        inputs[spec.key] = { kind: 'project_snapshot', reference: row.reference.token }
        continue
      }
      const context = draft.contexts[spec.key]
      if (context) {
        const option = contract.contextFields.find(row=>row.selector_key===context.selectorKey && row.kind===spec.kind)
        const constraint = contract.sourceConstraints[spec.key]!
        if (!constraint.kinds.includes('context_field') || (constraint.selectors !== null && !constraint.selectors.includes(context.selectorKey))
          || contract.serverOwnedInputs.includes(spec.key) || !option || option.scope_kind!=='project'
          || !spec.semantic_contract.allowed_scope_kinds.includes('project') || !context.projectId || !context.revisionId) throw new Error(`${spec.key} 需要完整且受支持的项目内容修订`)
        inputs[spec.key] = { kind: 'context_field', selector_key: context.selectorKey,
          scope: { kind: 'project', id: context.projectId }, content_revision_id: context.revisionId }
        continue
      }
      if (!contract.sourceConstraints[spec.key]!.kinds.includes('literal')) throw new Error(`${spec.key} 必须选择真实上下文来源，不能手工编写内容快照`)
      if (spec.kind === 'text' && typeof row.value !== 'string') throw new Error(`${spec.key} 应为文本`)
      const schemas=contract.structureSchemas?.[spec.key] || []
      const errors=jsonInputErrors(row.value,schemas,contract.inputForms?.[spec.key]?.label || spec.key,spec.data_structure_version_id?schemas.slice(0,1):[])
      if(errors.length) throw new Error(errors.join('；'))
      if (row.reference && (spec.kind !== 'text' || !row.reference.token)) throw new Error(`${spec.key} 的引用凭据无效，请重新引用`)
      inputs[spec.key] = { kind: 'literal', value: row.value, ...(row.reference ? { reference: row.reference.token } : {}) } as JsonObject
    } else {
      const rows = draft.media[spec.key]
      if (!rows || rows.length < spec.min_items || rows.length > spec.max_items || (spec.required && !rows.length)) throw new Error(`${spec.key} 素材数量不符合合同`)
      if (!rows.length) continue
      const items = rows.map(row => {
        const source = row.selectedOutput, shot = row.reviewedShot
        // Original audio has no independent asset/review. Its scope belongs to
        // the explicitly paired video, regardless of the contract's input order.
        let scopeShot = shot
        if (row.videoAudio) {
          const parentKey = spec.semantic_contract.paired_with_input_key
          const parents = parentKey ? draft.media[parentKey]?.filter(item=>item.itemKey===row.pairedTo) : undefined
          if (!parentKey || contract.inputs.find(input=>input.key===parentKey)?.kind !== 'video' || parents?.length !== 1)
            throw new Error(`${spec.key} 视频原音必须配对本次父视频输入中的唯一素材使用`)
          scopeShot = parents[0]!.reviewedShot
        }
        const scope = scopeShot ? { kind: 'shot', id: scopeShot.shotId } : { kind: 'request' }
        if (!spec.semantic_contract.allowed_scope_kinds.includes(scope.kind)) throw new Error(`${spec.key} 不允许此输入范围，请在支持该真实上下文的业务步骤中使用`)
        if (shot && (context?.productionRunId !== shot.runId || context?.productionScope?.kind !== 'shot'
          || context.productionScope.id !== shot.shotId || shot.kind !== spec.kind || row.assetId !== shot.assetId
          || source || row.videoAudio || row.subjectId || !/^reviewed:[a-f0-9]{64}$/.test(row.itemKey)
          || (shot.subjectRef && !spec.semantic_contract.allowed_subject_kinds.includes(shot.subjectRef.kind))))
          throw new Error(`${spec.key} 需要当前制作镜头的确切已审核用途，不能替换主体或素材`)
        if (!row.itemKey || selected.has(row.itemKey) || (!source && !row.assetId && !row.videoAudio)) throw new Error(`${spec.key} 需要明确且唯一的素材使用`)
        if (row.videoAudio && (spec.kind !== 'audio' || !spec.semantic_contract.paired_with_input_key
          || row.assetId || source || row.subjectId)) throw new Error(`${spec.key} 视频原音必须使用明确配对，不能混入其他素材或主体`)
        if (source && (!context?.productionRunId || source.runId !== context.productionRunId || source.kind !== spec.kind
          || row.assetId || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(source.artifactId)
          || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(source.decisionId)
          || !/^selected:[a-f0-9]{64}$/.test(row.itemKey))) throw new Error(`${spec.key} 需要本轮明确已审核产物，不能混用素材来源`)
        selected.set(row.itemKey, { inputKey: spec.key, scope })
        if (spec.semantic_contract.allowed_purposes.length ? !spec.semantic_contract.allowed_purposes.includes(row.purpose ?? '') : row.purpose !== null) throw new Error(`请选择 ${spec.key} 的素材用途`)
        if (row.subjectId && (!spec.semantic_contract.allowed_subject_kinds.includes('external') || !draft.subjects.some(subject => subject.id === row.subjectId))) throw new Error(`${spec.key} 主体引用无效`)
        return { ...(row.videoAudio ? { kind: 'video_audio' } : source ? { kind: 'selected_output', decision_id: source.decisionId, artifact_id: source.artifactId }
          : { kind: 'asset', asset_id: row.assetId }), item_key: row.itemKey, purpose: row.purpose,
          scope_ref: scope,
          ...(row.pairedTo ? { paired_to: row.pairedTo } : {}), ...(shot?.subjectRef ? { subject_ref: { ...shot.subjectRef } }
            : row.subjectId ? { subject_ref: { kind: 'external', id: row.subjectId } } : {}) }
      })
      inputs[spec.key] = executionMediaSelection(spec,draft.mediaSourceModes?.[spec.key],items) as JsonObject
    }
  }
  for (const spec of contract.inputs) for (const row of draft.media[spec.key] ?? []) {
    const parent = spec.semantic_contract.paired_with_input_key
    if (parent ? !row.pairedTo || selected.get(row.pairedTo)?.inputKey !== parent || paired.has(row.pairedTo) : row.pairedTo !== null) throw new Error(`${spec.key} 必须配对本次父输入中唯一的素材使用`)
    if (row.pairedTo) {
      const scope = selected.get(row.itemKey)!.scope, parentScope = selected.get(row.pairedTo)!.scope
      if (scope.kind !== parentScope.kind || scope.id !== parentScope.id) throw new Error(`${spec.key} 配对素材必须属于同一输入范围`)
      paired.add(row.pairedTo)
    }
  }
  if (Object.keys(draft.parameters).some(key => !contract.parameters.some(row => row.key === key))) throw new Error('存在合同之外的参数')
  for (const { key, rule: r } of contract.parameters) {
    if (has(draft.parameters, key)) {
      if (r.seed_policy === 'generate_once') throw new Error(`${key} 由首次准备生成，不能另填`)
      validateValue(draft.parameters[key], r, key); parameters[key] = draft.parameters[key]!
    } else if (r.required && !has(r, 'const') && !has(r, 'default') && r.seed_policy !== 'generate_once') throw new Error(`请填写参数 ${key}`)
  }
  if (contract.promptMode !== 'manual') {
    if (Object.keys(draft.channels).length) throw new Error('非人工提示词方式不能另传完整提示词')
  } else {
    if (Object.keys(draft.channels).some(key => !contract.channels.some(row => row.key === key))) throw new Error('存在合同之外的提示词通道')
    for (const { key, rule: r } of contract.channels) {
      if (has(draft.channels, key)) { validateValue(draft.channels[key], r, key); channels[key] = draft.channels[key]! }
      else if (r.required && !has(r, 'const')) throw new Error(`请填写 ${key}`)
    }
  }
  if (draft.subjects.some(row => !row.id || !row.name.trim()) || new Set(draft.subjects.map(row => row.id)).size !== draft.subjects.length) throw new Error('本次主体需要唯一身份和明确名称')
  if (draft.subjects.some(row => !contract.subjectCategories.some(choice => choice.value === row.category))) throw new Error('请从服务端目录选择每个主体的语义类别')
  return { inputs, parameters, manual_channels: channels, subject_facts: draft.subjects,
    source_language: draft.sourceLanguage, target_language: draft.targetLanguage }
}
