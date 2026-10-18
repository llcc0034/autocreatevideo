import { configuration, instance, version, choices, authoring } from './execution-authoring-data.ts'

// Synthetic service responses for UI acceptance only, not a backend compiler or real admission evidence.
export const derivedInput = {
  key: 'creative_brief', position: 1, kind: 'text', required: true, min_items: 1, max_items: 1,
  semantic_contract: { schema_version: 1, allowed_purposes: [], allowed_subject_kinds: [], allowed_scope_kinds: ['request'], paired_with_input_key: null, allow_repeated_asset: false },
  targets: [{ target_kind: 'profile_input', target_key: 'request.theme', is_primary: false }],
}
export const profileInputTarget = { target_kind: 'profile_input', target_key: 'request.theme', kind: 'text', required: true, min_items: 1, max_items: 1,
  field_keys: ['direction'], fields: [{ field_key: 'direction', usage: 'generation_source' }] }
export const auxiliaryVersion = { ...structuredClone(version), id: 'fixture-auxiliary', instance_id: 'fixture-helper', instance_name: '隔离文本辅助',
  status: 'published', version: 1, operation: 'prompt.fields.generate', prompt_mode: 'none', workflow_version_id: null, prompt_profile_version_id: null,
  configuration: { ...structuredClone(configuration), deployment_id: 'fixture-direct', operation: 'prompt.fields.generate', mode: '', prompt_mode: 'none', workflow_version_id: null, prompt_profile_version_id: null, inputs: [], auxiliaries: [] } }
export const modeScenarios = (['manual', 'profile', 'none'] as const).map(mode => {
  const family = { ...instance, id: `fixture-${mode}-family`, key: `fixture-${mode}`, name: `隔离${mode}已发布来源`, revision: 2 }
  const config = { ...structuredClone(configuration), prompt_mode: mode,
    deployment_id: mode === 'none' ? 'fixture-direct' : configuration.deployment_id,
    workflow_version_id: mode === 'none' ? null : configuration.workflow_version_id,
    operation: mode === 'none' ? 'fixture.direct' : configuration.operation, mode: mode === 'none' ? '' : configuration.mode,
    prompt_profile_version_id: mode === 'profile' ? 'fixture-profile' : null,
    inputs: mode === 'profile' ? [...structuredClone(configuration.inputs), structuredClone(derivedInput)] : mode === 'none' ? [] : structuredClone(configuration.inputs),
    auxiliaries: mode === 'profile' ? [{ purpose: 'prompt.fields.generate', target_version_id: auxiliaryVersion.id }] : [],
  }
  return { mode, family, source: { ...structuredClone(version), ...config, id: `fixture-${mode}-published`, instance_id: family.id, instance_name: family.name,
    version: 1, revision: 4, status: 'published', configuration: config } }
})
export function scenarioChoices(deployment?: string) {
  const row = structuredClone(choices)
  if (deployment === 'fixture-direct') Object.assign(row.target, {
    deployment_id: deployment, model_profile_version_id: 'fixture-direct-model', workflow_required: false, workflow: null,
    operations: [{ value: 'fixture.direct', label: '直接接口能力（隔离）', output_parser_keys: [] }], modes: [],
  })
  return deployment ? row : { ...row, target: null }
}
export function scenarioAuthoring(row: any) {
  const profile = row.configuration.prompt_mode === 'profile', direct = row.configuration.workflow_version_id === null
  return { ...structuredClone(authoring), version_id: row.id, revision: row.revision,
    input_targets: direct ? {} : structuredClone(authoring.input_targets),
    profile_inputs: profile ? { 'request.theme': structuredClone(profileInputTarget) } : {},
    derived_input_keys: profile ? [derivedInput.key] : [],
    auxiliary_purposes: profile ? [{ purpose: 'prompt.fields.generate', operation: 'prompt.fields.generate' }] : [],
    prompt_not_required: row.configuration.prompt_mode === 'none',
  }
}
export function scenarioContract(row: any) {
  const config = structuredClone(row.configuration), profile = config.prompt_mode === 'profile'
  return { version_id: row.id, revision: row.revision, frozen: row.status !== 'draft', sha256: `fixture-definition-${row.id}-${row.revision}`,
    target_fingerprint: 'synthetic-not-a-real-smoke', schema_version: 1, size_bytes: 0,
    input_forms: {}, subject_categories: [], context_fields: [],
    language_form: { choices: [], allow_custom: false, source: { required: false, fields: [] }, target: { required: false, fields: [] }, fixed_fields: [] },
    definition: { configuration: config, resources: { deployment_id: config.deployment_id, model_profile_version_id: config.workflow_version_id ? 'fixture-model' : 'fixture-direct-model',
      workflow: config.workflow_version_id ? { id: config.workflow_version_id, projector: { version_id: 'fixture-projector' } } : null,
      prompt_profile: profile ? { id: config.prompt_profile_version_id } : null },
    interface: { prompt: { mode: config.prompt_mode }, parameters: {}, output: { kind: 'video' }, ...(profile ? { profile_inputs: { 'request.theme': structuredClone(profileInputTarget) } } : {}) } },
  }
}
