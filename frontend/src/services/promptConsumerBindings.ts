import { executionRegistryApi } from './executionRegistryApi'
import { apiErrorSummary } from './apiErrorPresentation'
import type { ExecutionVersionDetail } from './executionRegistry'

export async function readPromptConsumerBindings(profileId: string, consumerId: string) {
  const owner = await executionRegistryApi.version(consumerId)
  if (owner.id !== consumerId || owner.prompt_mode !== 'profile' || owner.prompt_profile_version_id !== profileId
    || owner.configuration.prompt_mode !== 'profile' || owner.configuration.prompt_profile_version_id !== profileId) {
    throw new Error('使用方身份或策略引用已变化，请重新读取；不会替换为同名或最新版本。')
  }
  const bindings = owner.configuration.auxiliaries
  if (bindings.some(item => !item.purpose.trim() || !item.target_version_id.trim())
    || new Set(bindings.map(item => item.purpose)).size !== bindings.length) throw new Error('辅助绑定用途为空或重复，不能明确解释绑定关系。')
  const targets = new Map<string, { target?: ExecutionVersionDetail; error?: string }>()
  await Promise.all([...new Set(bindings.map(item => item.target_version_id))].map(async id => {
    try {
      const target = await executionRegistryApi.version(id)
      if (target.id !== id) throw new Error('辅助目标响应身份不匹配')
      targets.set(id, { target })
    } catch (cause) { targets.set(id, { error: apiErrorSummary(cause, '辅助目标读取失败') }) }
  }))
  return { owner, bindings: bindings.map(item => ({ ...item, ...targets.get(item.target_version_id) })) }
}
