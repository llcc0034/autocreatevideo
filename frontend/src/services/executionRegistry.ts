import type { JsonObject } from './platformTypes'
import type { ExecutionMediaSourcePolicy } from './executionMediaSourcePolicy'
import { readMediaAppearanceInputOptions, type MediaAppearanceInputOptions } from './mediaAppearanceInputs'

export interface ExecutionChoice { value: string; label: string }
export interface ExecutionInstance {
  id: string; key: string; name: string; description: string; is_active: boolean; revision: number
  created_at: string; updated_at: string
}
export interface ExecutionVersion {
  output_version_id?: string | null
  id: string; instance_id: string; instance_name: string; version: number; revision: number
  status: 'draft' | 'published' | 'retired'; deployment_id: string; operation: string; mode: string
  prompt_mode: 'profile' | 'manual' | 'none'; prompt_profile_version_id: string | null
  prompt_operation: string | null; prompt_profile_mode: string | null; workflow_version_id: string | null
  published_at: string | null; published_by_id: string | null; created_at: string; updated_at: string
}
export interface ExecutionInput {
  media_source_policy?: ExecutionMediaSourcePolicy | null
  data_structure_version_id?: string | null
  key: string; position: number; kind: string; required: boolean; min_items: number; max_items: number
  semantic_contract: {
    schema_version: number; allowed_purposes: string[]; allowed_subject_kinds: string[]; allowed_scope_kinds: string[]
    paired_with_input_key: string | null; allow_repeated_asset: boolean
  }
  targets: { target_kind: string; target_key: string; is_primary: boolean }[]
}
export type ExecutionScalar = string | number | boolean | null
export interface ExecutionParameter {
  key: string; value_type: string; minimum: ExecutionScalar; maximum: ExecutionScalar
  enum_values: ExecutionScalar[]; step: ExecutionScalar; remainder: ExecutionScalar; policy: string
  default_value: ExecutionScalar; locked_value: ExecutionScalar; seed_policy: string | null
}
export interface ExecutionConfiguration {
  output_version_id?: string
  deployment_id: string; operation: string; mode: string; prompt_mode: ExecutionVersion['prompt_mode']
  prompt_profile_version_id: string | null; workflow_version_id: string | null
  inputs: ExecutionInput[]; parameters: ExecutionParameter[]
  auxiliaries: { purpose: string; target_version_id: string }[]
}
export interface ExecutionVersionDetail extends ExecutionVersion { configuration: ExecutionConfiguration }
export interface ExecutionContract {
  appearance_inputs: MediaAppearanceInputOptions
  subject_categories: ExecutionChoice[]
  language_form: ExecutionLanguageForm
  input_forms: Record<string, { label: string; description: string; schemas: JsonObject[]; suggestions: ExecutionInputSuggestionChoice[] }>
  version_id: string; revision: number; frozen: boolean; schema_version: number; size_bytes: number
  sha256: string; target_fingerprint: string; definition: JsonObject
  context_fields: { selector_key: string; label: string; kind: string; scope_kind: string; revision_kind: string; description: string; required_stage_key?: string; value_schema?: JsonObject; revision_dependencies?: { key: string; revision_kind: string; stage_key: string }[] }[]
}
export interface ExecutionLanguageForm {
  choices: ExecutionChoice[]; allow_custom: boolean
  source: { required: boolean; fields: string[] }; target: { required: boolean; fields: string[] }
  fixed_fields: { label: string; language: string }[]
}
export function adaptExecutionLanguageForm(value: unknown): ExecutionLanguageForm {
  const row = object(value, 'language_form')
  const context = (key: string) => {
    const item = object(row[key], `language_form.${key}`)
    const fields = array(item.fields, 'language_fields').map(value => text(value, 'language_field'))
    const required = flag(item.required, 'language_required')
    if (required && !fields.length) fail('语言必填要求缺少作用字段')
    return { required, fields }
  }
  return { choices: choices(row.choices), allow_custom: flag(row.allow_custom, 'language_custom'),
    source: context('source'), target: context('target'), fixed_fields: array(row.fixed_fields, 'fixed_languages').map(value => {
      const field = object(value, 'fixed_language')
      return { label: text(field.label, 'label'), language: text(field.language, 'language') }
    }) }
}
export interface ExecutionInputSuggestionChoice { key: string; label: string; requires_project: boolean; example?: string; requires_production_scope?: boolean; prefill?: boolean }
export interface ExecutionInputSuggestion { input_key: string; definition_sha256: string; label: string; content: string; reference: string; origin: JsonObject }
export function adaptExecutionInputSuggestion(value: unknown): ExecutionInputSuggestion {
  const r = object(value, 'input_suggestion')
  return { input_key: text(r.input_key, 'input_key'), definition_sha256: text(r.definition_sha256, 'definition_sha256'),
    label: text(r.label, 'label'), content: text(r.content, 'content'), reference: text(r.reference, 'reference'), origin: json(r.origin, 'origin') }
}
export interface ResourceSmokeContract extends Omit<ExecutionContract, 'version_id' | 'revision' | 'frozen' | 'appearance_inputs'> {
  scope: 'resource_smoke'; version_id: null
}
export interface ExecutionSelectionOptions {
  prompt_modes: ExecutionChoice[]; version_statuses: ExecutionChoice[]; input_kinds: ExecutionChoice[]
  target_kinds: ExecutionChoice[]; parameter_types: ExecutionChoice[]; parameter_policies: ExecutionChoice[]
  seed_policies: ExecutionChoice[]; identity_options: { subject_kinds: ExecutionChoice[]; scope_kinds: ExecutionChoice[] }
  target: null | {
    deployment_id: string; model_profile_version_id: string; model_family: string | null; driver: string
    workflow_required: boolean; workflow: JsonObject | null; operations: Array<ExecutionChoice & { output_parser_keys: string[] }>; modes: ExecutionChoice[]
  }
}
export interface ExecutionAuthoringOptions {
  version_id: string; revision: number; resources: JsonObject; input_targets: JsonObject; profile_inputs: JsonObject
  derived_input_keys: string[]
  input_purposes: JsonObject; static_semantics: JsonObject; parameters: JsonObject; prompt_channels: JsonObject
  prompt_not_required: boolean; auxiliary_purposes: { purpose: string; operation: string }[]
  identity_options: { subject_kinds: ExecutionChoice[]; scope_kinds: ExecutionChoice[] }
}
export interface ExecutionPage<T> { count: number; next: string | null; previous: string | null; results: T[] }

const fail = (field: string): never => { throw new Error(`执行实例响应不完整：${field}`) }
const object = (value: unknown, field: string): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : fail(field)
const text = (value: unknown, field: string): string => typeof value === 'string' ? value : fail(field)
const nullableText = (value: unknown, field: string): string | null => value === null ? null : text(value, field)
const integer = (value: unknown, field: string, min = 0): number => typeof value === 'number' && Number.isSafeInteger(value) && value >= min ? value : fail(field)
const flag = (value: unknown, field: string): boolean => typeof value === 'boolean' ? value : fail(field)
const array = (value: unknown, field: string): unknown[] => Array.isArray(value) ? value : fail(field)
const texts = (value: unknown, field: string): string[] => array(value, field).map(item => text(item, field))
const scalar = (value: unknown, field: string): ExecutionScalar => value === null || typeof value === 'string' || typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value)) ? value : fail(field)
const enumText = <T extends string>(value: unknown, choices: readonly T[], field: string): T => choices.includes(value as T) ? value as T : fail(field)
const json = (value: unknown, field: string): JsonObject => object(value, field) as JsonObject
const choices = (value: unknown): ExecutionChoice[] => array(value, '选项').map(item => {
  const row = object(item, '选项'); return { value: text(row.value, 'value'), label: text(row.label, 'label') }
})
const identity = (value: unknown) => {
  const row = object(value, 'identity_options')
  return { subject_kinds: choices(row.subject_kinds), scope_kinds: choices(row.scope_kinds) }
}

export function adaptExecutionInstance(value: unknown): ExecutionInstance {
  const r = object(value, 'instance')
  return { id: text(r.id, 'id'), key: text(r.key, 'key'), name: text(r.name, 'name'), description: text(r.description, 'description'),
    is_active: flag(r.is_active, 'is_active'), revision: integer(r.revision, 'revision', 1), created_at: text(r.created_at, 'created_at'), updated_at: text(r.updated_at, 'updated_at') }
}
export function adaptExecutionVersion(value: unknown): ExecutionVersion {
  const r = object(value, 'version')
  if (r.prompt_mode === 'profile' && (typeof r.prompt_profile_version_id !== 'string' || typeof r.prompt_operation !== 'string' || !r.prompt_operation)) fail('策略版本或只读提示词用途')
  if ((r.prompt_mode === 'manual' || r.prompt_mode === 'none') && (r.prompt_profile_version_id !== null || r.prompt_operation !== null || r.prompt_profile_mode !== null)) fail('无策略方式不能带策略引用')
  return { id: text(r.id, 'id'), instance_id: text(r.instance_id, 'instance_id'), instance_name: text(r.instance_name, 'instance_name'),
    version: integer(r.version, 'version', 1), revision: integer(r.revision, 'revision', 1), status: enumText(r.status, ['draft', 'published', 'retired'], 'status'),
    deployment_id: text(r.deployment_id, 'deployment_id'), operation: text(r.operation, 'operation'), mode: text(r.mode, 'mode'),
    prompt_mode: enumText(r.prompt_mode, ['profile', 'manual', 'none'], 'prompt_mode'), prompt_profile_version_id: nullableText(r.prompt_profile_version_id, 'prompt_profile_version_id'),
    prompt_operation: nullableText(r.prompt_operation, 'prompt_operation'), prompt_profile_mode: nullableText(r.prompt_profile_mode, 'prompt_profile_mode'), workflow_version_id: nullableText(r.workflow_version_id, 'workflow_version_id'),
    published_at: nullableText(r.published_at, 'published_at'), published_by_id: nullableText(r.published_by_id, 'published_by_id'), created_at: text(r.created_at, 'created_at'), updated_at: text(r.updated_at, 'updated_at') }
}
export function adaptExecutionConfiguration(value: unknown): ExecutionConfiguration {
  const r = object(value, 'configuration')
  return {
    ...(r.output_version_id == null ? {} : { output_version_id: text(r.output_version_id, 'output_version_id') }),
    deployment_id: text(r.deployment_id, 'deployment_id'), operation: text(r.operation, 'operation'), mode: text(r.mode, 'mode'),
    prompt_mode: enumText(r.prompt_mode, ['profile', 'manual', 'none'], 'prompt_mode'), prompt_profile_version_id: nullableText(r.prompt_profile_version_id, 'prompt_profile_version_id'), workflow_version_id: nullableText(r.workflow_version_id, 'workflow_version_id'),
    inputs: array(r.inputs, 'inputs').map(item => {
      const i = object(item, 'input'); const s = object(i.semantic_contract, 'semantic_contract')
      return { key: text(i.key, 'key'), position: integer(i.position, 'position'), kind: text(i.kind, 'kind'), required: flag(i.required, 'required'), min_items: integer(i.min_items, 'min_items'), max_items: integer(i.max_items, 'max_items'),
        ...(Object.prototype.hasOwnProperty.call(i, 'data_structure_version_id') ? { data_structure_version_id: nullableText(i.data_structure_version_id, 'data_structure_version_id') } : {}),
        ...(i.media_source_policy == null ? {} : {media_source_policy: adaptMediaPolicy(i.media_source_policy)}),
        semantic_contract: { schema_version: integer(s.schema_version, 'schema_version', 1), allowed_purposes: texts(s.allowed_purposes, 'allowed_purposes'), allowed_subject_kinds: texts(s.allowed_subject_kinds, 'allowed_subject_kinds'), allowed_scope_kinds: texts(s.allowed_scope_kinds, 'allowed_scope_kinds'), paired_with_input_key: nullableText(s.paired_with_input_key, 'paired_with_input_key'), allow_repeated_asset: flag(s.allow_repeated_asset, 'allow_repeated_asset') },
        targets: array(i.targets, 'targets').map(t => { const target = object(t, 'target'); return { target_kind: text(target.target_kind, 'target_kind'), target_key: text(target.target_key, 'target_key'), is_primary: flag(target.is_primary, 'is_primary') } }) }
    }),
    parameters: array(r.parameters, 'parameters').map(item => {
      const p = object(item, 'parameter')
      return { key: text(p.key, 'key'), value_type: text(p.value_type, 'value_type'), minimum: scalar(p.minimum, 'minimum'), maximum: scalar(p.maximum, 'maximum'), enum_values: array(p.enum_values, 'enum_values').map(v => scalar(v, 'enum_values')),
        step: scalar(p.step, 'step'), remainder: scalar(p.remainder, 'remainder'), policy: text(p.policy, 'policy'), default_value: scalar(p.default_value, 'default_value'), locked_value: scalar(p.locked_value, 'locked_value'), seed_policy: nullableText(p.seed_policy, 'seed_policy') }
    }),
    auxiliaries: array(r.auxiliaries, 'auxiliaries').map(item => { const a = object(item, 'auxiliary'); return { purpose: text(a.purpose, 'purpose'), target_version_id: text(a.target_version_id, 'target_version_id') } }),
  }
}
function adaptMediaPolicy(value: unknown): ExecutionMediaSourcePolicy {
  const row=object(value,'media_source_policy')
  if(Object.keys(row).length!==5 || row.schema_version!==1)fail('media_source_policy')
  return {schema_version:1,allowed_sources:texts(row.allowed_sources,'allowed_sources'),default_source:text(row.default_source,'default_source'),
    allow_override:flag(row.allow_override,'allow_override'),order:enumText(row.order,['provided'],'order') as 'provided'}
}
export function adaptExecutionVersionDetail(value: unknown): ExecutionVersionDetail {
  const r = object(value, 'version'); const version = adaptExecutionVersion(r); const configuration = adaptExecutionConfiguration(r.configuration)
  if (r.output_version_id != null) version.output_version_id = text(r.output_version_id, 'output_version_id')
  if (selectionFields.some(key => (version[key] ?? null) !== (configuration[key] ?? null))) fail('列表身份与配置身份不一致')
  return { ...version, configuration }
}
export function adaptExecutionContract(value: unknown): ExecutionContract {
  const r = object(value, 'contract')
  const context_fields = adaptExecutionContextFields(r.context_fields)
  return { appearance_inputs: readMediaAppearanceInputOptions(r.appearance_inputs), subject_categories: adaptExecutionSubjectCategories(r.subject_categories), version_id: text(r.version_id, 'version_id'), revision: integer(r.revision, 'revision', 1), frozen: flag(r.frozen, 'frozen'), schema_version: integer(r.schema_version, 'schema_version', 1), size_bytes: integer(r.size_bytes, 'size_bytes'), sha256: text(r.sha256, 'sha256'), target_fingerprint: text(r.target_fingerprint, 'target_fingerprint'), definition: json(r.definition, 'definition'), context_fields, input_forms: adaptInputForms(r.input_forms), language_form: adaptExecutionLanguageForm(r.language_form) }
}
export function executionFrozenProfileInputs(version: ExecutionVersion, contract: ExecutionContract): JsonObject {
  if (!contract.frozen || contract.version_id !== version.id || contract.revision !== version.revision) {
    throw new Error('来源关系合同与当前执行版本不一致，请重新读取')
  }
  const interfaceValue = object(contract.definition.interface, '冻结 interface')
  // Only Profile mode owns this part of the contract. Manual/no-prompt modes
  // legitimately omit it; do not request today's strategy catalog as a fix.
  if (version.prompt_mode === 'manual' || version.prompt_mode === 'none') {
    if (interfaceValue.profile_inputs !== undefined) fail('非策略方式不应声明冻结 profile_inputs')
    return {}
  }
  if (version.prompt_mode !== 'profile') fail('冻结提示词方式')
  return json(interfaceValue.profile_inputs, '冻结 profile_inputs')
}
function adaptInputForms(value: unknown): NonNullable<ExecutionContract['input_forms']> {
  return Object.fromEntries(Object.entries(object(value, 'input_forms')).map(([key,value])=>{
    const row=object(value,'input_form')
    return [key,{label:text(row.label,'label'),description:text(row.description,'description'),schemas:array(row.schemas,'schemas').map(value=>json(value,'schema')),
      suggestions: array(row.suggestions,'suggestions').map(value=>{const s=object(value,'suggestion');return {key:text(s.key,'key'),label:text(s.label,'label'),requires_project:flag(s.requires_project,'requires_project'),...(s.example===undefined?{}:{example:text(s.example,'example')}),
        ...(s.requires_production_scope===undefined?{}:{requires_production_scope:flag(s.requires_production_scope,'requires_production_scope')}),
        ...(s.prefill===undefined?{}:{prefill:flag(s.prefill,'prefill')})}})}]
  }))
}
function adaptExecutionContextFields(value: unknown): ExecutionContract['context_fields'] {
  return array(value, 'context_fields').map(value => {
    const row = object(value, 'context_field')
    return { selector_key: text(row.selector_key, 'selector_key'), label: text(row.label, 'label'), kind: text(row.kind, 'kind'),
      scope_kind: text(row.scope_kind, 'scope_kind'), revision_kind: text(row.revision_kind, 'revision_kind'), description: text(row.description, 'description'),
      ...(row.value_schema === undefined ? {} : { value_schema: json(row.value_schema, 'value_schema') }),
      ...(row.required_stage_key === undefined ? {} : { required_stage_key: text(row.required_stage_key, 'required_stage_key') }),
      ...(row.revision_dependencies === undefined ? {} : { revision_dependencies: array(row.revision_dependencies, 'revision_dependencies').map(value => {
        const dependency = object(value, 'revision_dependency')
        return { key: text(dependency.key, 'key'), revision_kind: text(dependency.revision_kind, 'revision_kind'), stage_key: text(dependency.stage_key, 'stage_key') }
      }) }) }
  })
}
export function adaptResourceSmokeContract(value: unknown): ResourceSmokeContract {
  const r = object(value, 'resource-smoke-contract')
  if (r.scope !== 'resource_smoke' || r.version_id !== null || 'frozen' in r || 'revision' in r) fail('基础资源合同不能冒充已发布实例')
  return { scope: 'resource_smoke', version_id: null, schema_version: integer(r.schema_version, 'schema_version', 1),
    size_bytes: integer(r.size_bytes, 'size_bytes'), sha256: text(r.sha256, 'sha256'), target_fingerprint: text(r.target_fingerprint, 'target_fingerprint'),
    subject_categories: adaptExecutionSubjectCategories(r.subject_categories), definition: json(r.definition, 'definition'), context_fields: adaptExecutionContextFields(r.context_fields), input_forms: adaptInputForms(r.input_forms), language_form: adaptExecutionLanguageForm(r.language_form) }
}
export function adaptExecutionSubjectCategories(value: unknown): ExecutionChoice[] {
  const result = choices(value)
  if (!result.length || new Set(result.map(row=>row.value)).size !== result.length || result.some(row=>!row.value || !row.label)) fail('主体语义类别目录')
  return result
}
export function adaptExecutionSelectionOptions(value: unknown): ExecutionSelectionOptions {
  const r = object(value, 'selection-options'); const t = r.target === null ? null : object(r.target, 'target')
  return { prompt_modes: choices(r.prompt_modes), version_statuses: choices(r.version_statuses), input_kinds: choices(r.input_kinds), target_kinds: choices(r.target_kinds), parameter_types: choices(r.parameter_types), parameter_policies: choices(r.parameter_policies), seed_policies: choices(r.seed_policies), identity_options: identity(r.identity_options),
    target: t === null ? null : { deployment_id: text(t.deployment_id, 'deployment_id'), model_profile_version_id: text(t.model_profile_version_id, 'model_profile_version_id'), model_family: t.model_family === null ? null : text(t.model_family, 'model_family'), driver: text(t.driver, 'driver'), workflow_required: flag(t.workflow_required, 'workflow_required'), workflow: t.workflow === null ? null : json(t.workflow, 'workflow'), operations: array(t.operations, 'operations').map(value => {
      const row = object(value, 'operation')
      return { value: text(row.value, 'value'), label: text(row.label, 'label'), output_parser_keys: texts(row.output_parser_keys, 'output_parser_keys') }
    }), modes: choices(t.modes) } }
}
export function adaptExecutionAuthoringOptions(value: unknown): ExecutionAuthoringOptions {
  const r = object(value, 'authoring-options')
  return { version_id: text(r.version_id, 'version_id'), revision: integer(r.revision, 'revision', 1), resources: json(r.resources, 'resources'), input_targets: json(r.input_targets, 'input_targets'), profile_inputs: json(r.profile_inputs, 'profile_inputs'), derived_input_keys: array(r.derived_input_keys, 'derived_input_keys').map(v=>text(v,'derived_input_key')), input_purposes: json(r.input_purposes, 'input_purposes'), static_semantics: json(r.static_semantics, 'static_semantics'), parameters: json(r.parameters, 'parameters'), prompt_channels: json(r.prompt_channels, 'prompt_channels'), prompt_not_required: flag(r.prompt_not_required, 'prompt_not_required'), identity_options: identity(r.identity_options),
    auxiliary_purposes: array(r.auxiliary_purposes, 'auxiliary_purposes').map(item => { const a = object(item, 'auxiliary'); return { purpose: text(a.purpose, 'purpose'), operation: text(a.operation, 'operation') } }) }
}
export function adaptExecutionPage<T>(value: unknown, adapt: (row: unknown) => T): ExecutionPage<T> {
  const r = object(value, 'page')
  return { count: integer(r.count, 'count'), next: nullableText(r.next, 'next'), previous: nullableText(r.previous, 'previous'), results: array(r.results, 'results').map(adapt) }
}
export function executionDraftPatch(original: ExecutionVersionDetail, draft: ExecutionConfiguration) {
  if (original.status !== 'draft') throw new Error('已发布或退役版本只能查看或创建后继草稿')
  const checked = adaptExecutionConfiguration(draft)
  return { expected_revision: original.revision, ...Object.fromEntries(Object.entries(checked).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(original.configuration[key as keyof ExecutionConfiguration]))), ...(original.configuration.output_version_id && !checked.output_version_id ? { output_version_id: null } : {}) }
}
export function executionLifecyclePayload(version: ExecutionVersion, contract: ExecutionContract | null) {
  if (!contract || contract.version_id !== version.id || contract.revision !== version.revision || contract.frozen !== (version.status !== 'draft')) throw new Error('请重新读取并复核此精确版本合同')
  return { expected_revision: version.revision, expected_definition_sha256: contract.sha256 }
}
const selectionFields = ['deployment_id', 'operation', 'mode', 'prompt_mode', 'prompt_profile_version_id', 'workflow_version_id', 'output_version_id'] as const
export function executionSelectionMatches(left: ExecutionConfiguration, right: ExecutionConfiguration) {
  return selectionFields.every(key => left[key] === right[key])
}
