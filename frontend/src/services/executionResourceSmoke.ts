import { api } from './api'
import { adaptResourceSmokeContract, adaptExecutionSelectionOptions } from './executionRegistry'
import { adaptResourceSmokeCreated, executionSmokePayload, type ExecutionSmokeRequest } from './executionPreparation'
import { adaptResourceFieldSmokeSource, type FieldSmokeSource, type FieldSmokeItem } from './executionFieldSmoke'

export interface ResourceSmokeTarget { kind: 'deployment' | 'workflow'; id: string }
export interface ResourceSmokeSelection {
  output_version_id?: string
  operation: string; mode: string; prompt_mode: 'manual' | 'profile' | 'none'; prompt_profile_version_id: string | null
}
export interface ResourceSmokeContext { target: ResourceSmokeTarget; selection: ResourceSmokeSelection }
const path = (target: ResourceSmokeTarget) => {
  if (!target.id || !['deployment', 'workflow'].includes(target.kind)) throw new Error('请选择明确的基础验收对象')
  return `/${target.kind === 'deployment' ? 'model-deployments' : 'workflow-template-versions'}/${encodeURIComponent(target.id)}/`
}
export const resourceSmokeApi = {
  async choices(target: ResourceSmokeTarget) {
    return adaptExecutionSelectionOptions((await api.get(path(target) + 'smoke-options/')).data)
  },
  async contract(context: ResourceSmokeContext) {
    const contract = adaptResourceSmokeContract((await api.post(path(context.target) + 'smoke-contract/', context.selection)).data)
    const config = contract.definition.configuration as Record<string, unknown>
    if (!config || Object.entries(context.selection).some(([key, value]) => config[key] !== value)
      || (context.target.kind === 'workflow' ? config.workflow_version_id !== context.target.id
        : config.deployment_id !== context.target.id || config.workflow_version_id !== null)) throw new Error('基础烟测合同与当前资源选择不一致')
    return contract
  },
  async create(context: ResourceSmokeContext, input: ExecutionSmokeRequest, key: string) {
    if (!key.trim() || input.reuse) throw new Error('基础烟测需要独立幂等键和明确输入，不能借用实例准备')
    const result = adaptResourceSmokeCreated((await api.post(path(context.target) + 'smoke-test/',
      { selection: context.selection, ...executionSmokePayload(input) }, { headers: { 'Idempotency-Key': key } })).data)
    if (result.attemptKind !== (context.target.kind === 'workflow' ? 'workflow_smoke' : 'deployment_smoke')) throw new Error('返回任务不属于所选资源类型')
    return result
  },
  async readFields(context: ResourceSmokeContext, profileId: string) {
    return adaptResourceFieldSmokeSource((await api.post(path(context.target) + 'smoke-field-source/',
      { selection: context.selection, profile_version_id: profileId })).data)
  },
  async buildFields(context: ResourceSmokeContext, source: FieldSmokeSource<null>, items: FieldSmokeItem[]) {
    return adaptResourceFieldSmokeSource((await api.post(path(context.target) + 'smoke-field-source/', {
      selection: context.selection, profile_version_id: source.profile_version_id,
      expected_definition_sha256: source.definition_sha256, expected_profile_contract_sha256: source.profile_contract_sha256, items,
    })).data, true)
  },
}
