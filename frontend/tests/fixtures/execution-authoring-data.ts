// Synthetic authoring data only. No model, workflow, credential or business ID.
export const configuration = {
  deployment_id: 'fixture-deployment', operation: 'fixture.reference', mode: 'reference', prompt_mode: 'manual',
  prompt_profile_version_id: null, workflow_version_id: 'fixture-workflow',
  inputs: [{ key: 'reference_pixels', position: 0, kind: 'image', required: true, min_items: 1, max_items: 2,
    semantic_contract: { schema_version: 1, allowed_purposes: [], allowed_subject_kinds: [], allowed_scope_kinds: ['request'], paired_with_input_key: null, allow_repeated_asset: false },
    targets: [{ target_kind: 'workflow_slot', target_key: 'reference_pixels', is_primary: true }] }],
  parameters: [{ key: 'sample.scale', value_type: 'number', policy: 'overridable', minimum: null, maximum: null, enum_values: [], step: null, remainder: null, default_value: 0, locked_value: null, seed_policy: null }], auxiliaries: [],
}
export const instance = { id: 'fixture-instance', key: 'fixture', name: '隔离参考生视频', revision: 7, description: '', is_active: true }
export const version = { id: 'fixture-version', instance_id: instance.id, instance_name: instance.name, version: 2, revision: 3, status: 'draft', ...configuration,
  configuration: structuredClone(configuration), prompt_operation: null, prompt_profile_mode: '', published_at: null, published_by_id: null, created_at: '', updated_at: '' }
export const deployments = [{ id: 'fixture-deployment', name: '隔离模型部署', providerDriver: 'fixture', status: 'active' }]
export const workflows = [{ id: 'fixture-workflow', templateName: '隔离参考工作流', version: 1, status: 'published', modelProfileVersion: 'fixture-model', validatedDeployment: 'fixture-deployment' }]
export const profiles = [{ id: 'fixture-profile', name: '隔离字段组装策略', version: 1, status: 'published', modelFamily: 'fixture', mode: 'reference', operation: 'fixture.prompt' }]
export const choices = {
  prompt_modes: [{ value: 'manual', label: '人工完整提示词' }, { value: 'profile', label: '策略组装' }, { value: 'none', label: '无需提示词' }],
  version_statuses: [{ value: 'draft', label: '草稿' }], input_kinds: [{ value: 'image', label: '图片' }],
  target_kinds: [{ value: 'workflow_slot', label: '工作流槽位' }], parameter_types: [{ value: 'number', label: '数值' }],
  parameter_policies: [{ value: 'overridable', label: '允许运行时配置' }], seed_policies: [],
  identity_options: { subject_kinds: [], scope_kinds: [{ value: 'request', label: '本次请求' }] },
  target: { deployment_id: 'fixture-deployment', model_profile_version_id: 'fixture-model', model_family: 'fixture', driver: 'fixture',
    workflow_required: true, workflow: { projector: 'fixture-projector' }, operations: [{ value: 'fixture.reference', label: '参考生视频（隔离）', output_parser_keys: [] }], modes: [{ value: 'reference', label: '参考模式' }] },
}
export const authoring = {
  version_id: version.id, revision: version.revision, resources: {},
  input_targets: { reference_pixels: { target_kind: 'workflow_slot', target_key: 'reference_pixels', kind: 'image', required: true, min_items: 1, max_items: 2 } },
  profile_inputs: {}, derived_input_keys: [], input_purposes: { image: [] }, static_semantics: {},
  parameters: { 'sample.scale': { rule: { type: 'number', minimum: 0, maximum: 2 }, sources: [] } },
  prompt_channels: {}, prompt_not_required: false, auxiliary_purposes: [], identity_options: choices.identity_options,
}
