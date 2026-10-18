import { unusedLanguageForm } from './executionLanguage.ts'
import { mediaContractRaw } from './execution-media-authoring-data.ts'

// Representative response fixtures only: no model availability or executed result.
export const generationOptions: any[] = [['image','manual'],['video','manual'],['audio','none'],['script','profile'],['prompt','manual'],['video','profile']].map(([kind, mode], i) => ({
  versionId: `20000000-0000-0000-0000-00000000000${i+1}`, name: `隔离${kind} · ${mode}`, version: 1,
  operation: kind === 'prompt' ? 'prompt.draft' : kind === 'script' ? 'script.generate' : `${kind}.generate`,
  mode: '', promptMode: mode, definitionSha256: String(i+1).repeat(64), kind,
}))
export const profileId = '30000000-0000-0000-0000-000000000001'
export const auxiliaryIds = ['40000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000002']
export const generationDefinition = (row: any) => ({
  deployment_id: 'fixture-deployment', operation: row.operation, mode: '', workflow_version_id: null,
  prompt_mode: row.promptMode, prompt_profile_version_id: row.promptMode === 'profile' ? profileId : null, output_version_id: null,
  inputs: row.promptMode === 'profile' ? [{
    key: 'creative_brief', position: 0, kind: 'text', required: true, min_items: 1, max_items: 1,
    semantic_contract: { schema_version: 1, allowed_purposes: [], allowed_subject_kinds: [], allowed_scope_kinds: ['request'], paired_with_input_key: null, allow_repeated_asset: false },
    targets: [{ target_kind: 'profile_input', target_key: 'creative_source', is_primary: true }],
  }] : [], parameters: [],
  auxiliaries: row.promptMode === 'profile' && row.kind === 'video' ? auxiliaryIds.map((id, i) => ({ purpose: i ? 'prompt.fields.translate' : 'prompt.fields.generate', target_version_id: id })) : [],
})
export const generationContract = (row: any): any => ({
  version_id: row.versionId, revision: 1, frozen: true, sha256: row.definitionSha256, context_fields: [],
  input_forms: row.promptMode === 'profile' ? { creative_brief: { label: '本轮创作说明', description: '本次提供的创作事实，传入策略字段，不是最终提示词。', schemas: [{type:'string',minLength:1}], suggestions: [] } } : {},
  subject_categories: [{value:'character',label:'人物'},{value:'scene',label:'场景'},{value:'prop',label:'道具'}],
  language_form: row.promptMode === 'profile' && row.kind === 'video' ? {
    ...unusedLanguageForm(), allow_custom: false,
    source: { required: true, fields: ['本轮创作说明'] }, target: { required: true, fields: ['生成与翻译字段'] },
  } : unusedLanguageForm(),
  appearance_inputs: {primary_inputs:[],difference_sources:[{kind:'manual_channel',key:'positive_prompt'}]},
  definition: { version_id: row.versionId, configuration: generationDefinition(row), interface: {
    output: {kind:['script','prompt'].includes(row.kind)?'text':row.kind}, input_purposes: {},
    parameters: { seed: { rule: {type:'integer',required:true}, value_source:null } },
    profile_inputs: row.promptMode === 'profile' ? {creative_source:{source_kinds:['literal'],value_domain:'string'}} : {},
    prompt: { mode: row.promptMode, channels: row.promptMode === 'manual' ? {positive_prompt:{rule:{type:'string',required:true}}} : {} },
  } },
})

// Cross-window scenario uses the same contract in the calling author and the
// actual registry detail. It is not a backend compile or model readiness claim.
export function generationMaintenanceDefinition(row: any) {
  return { ...generationDefinition(row), workflow_version_id: mediaContractRaw.definition.configuration.workflow_version_id,
    inputs: [{ ...structuredClone(mediaContractRaw.definition.configuration.inputs[0]), required: true, min_items: 1 }] }
}
export function generationMaintenanceContract(row: any) {
  const result = generationContract(row)
  result.definition.configuration = generationMaintenanceDefinition(row)
  result.input_forms.pictures = structuredClone(mediaContractRaw.input_forms.pictures)
  result.definition.resources = { deployment_id: 'fixture-deployment', workflow: { id: result.definition.configuration.workflow_version_id }, prompt_profile: null }
  result.definition.interface.output = structuredClone(mediaContractRaw.definition.interface.output)
  return result
}
