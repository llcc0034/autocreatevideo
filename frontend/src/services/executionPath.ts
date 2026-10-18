import type { ExecutionContract, ExecutionVersionDetail } from './executionRegistry'
import { executionLifecyclePayload } from './executionRegistry'

export const contractRecord = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
const id = (value: unknown): string | null => typeof value === 'string' && value.length > 0 ? value : null

/** A read projection, never a second contract compiler or a claim of actual execution. */
export function executionPath(version: ExecutionVersionDetail, contract: ExecutionContract) {
  executionLifecyclePayload(version, contract)
  const configuration = contractRecord(contract.definition.configuration)
  const resources = contractRecord(contract.definition.resources)
  const intf = contractRecord(contract.definition.interface)
  if (!configuration || !resources || !intf) throw new Error('链路合同不完整，请重新读取；不以当前资源补齐历史版本')
  const prompt = contractRecord(intf.prompt)
  const workflow = contractRecord(resources.workflow)
  const projector = contractRecord(workflow?.projector)
  const output = contractRecord(intf.output)
  const primaryOutput = contractRecord(contractRecord(output?.contract)?.primary)
  return {
    configuration, resources, intf, prompt,
    inputs: Array.isArray(configuration.inputs) ? configuration.inputs.map(value => {
      const input = contractRecord(value)
      if (!input || !id(input.key) || !Array.isArray(input.targets)) throw new Error('实例输入声明不完整')
      return input
    }) : null,
    modelId: id(resources.model_profile_version_id), deploymentId: id(resources.deployment_id),
    workflowId: id(workflow?.id), projectorId: id(projector?.version_id),
    profileId: id(contractRecord(resources.prompt_profile)?.id),
    outputId: id(configuration.output_version_id),
    auxiliaries: Array.isArray(configuration.auxiliaries) ? configuration.auxiliaries.map(value => {
      const item = contractRecord(value)
      if (!item || !id(item.purpose) || !id(item.target_version_id)) throw new Error('辅助实例引用不完整')
      return { purpose: String(item.purpose), id: String(item.target_version_id) }
    }) : null,
    output, primaryOutput,
    outputKind: id(primaryOutput?.kind) || id(output?.kind),
    outputAdapter: typeof output?.adapter==='string' ? output.adapter : id(contractRecord(output?.adapter)?.id),
    businessOutput: contractRecord(intf.business_output),
  }
}

/** UI-only relocation. Keep every exact ID and context, never select another version. */
export function canonicalSettingsResourceUrl(value: string): string {
  if (!/^\/settings(?:[/?]|$)/.test(value) || /[\\\r\n]/.test(value)) throw new Error('资源导航地址无效')
  const url = new URL(value, 'https://navigation.invalid')
  if (url.pathname === '/settings' && url.searchParams.has('execution_version')) url.searchParams.set('section', 'executions')
  return url.pathname + url.search + url.hash
}
