import type { TaskInputPrompt } from '../types/index.ts'

export function adaptTaskInputPrompt(value: unknown): TaskInputPrompt {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('提示词响应不完整')
  const raw = value as Record<string, unknown>
  const text = (row: Record<string, unknown>, key: string, allowEmpty = false) => {
    const item = row[key]
    if (typeof item !== 'string' || (!allowEmpty && !item)) throw new Error(`提示词响应缺少 ${key}`)
    return item
  }
  if (!['manual', 'profile', 'none'].includes(String(raw.prompt_mode))) throw new Error('提示词来源方式缺失')
  if (raw.prompt_profile_version_id !== null && typeof raw.prompt_profile_version_id !== 'string') throw new Error('提示词策略版本事实缺失')
  if (!Array.isArray(raw.channels) || !Array.isArray(raw.declared_reference_labels) || raw.declared_reference_labels.some(item => typeof item !== 'string')) throw new Error('提示词通道或参考标签缺失')
  const channels = raw.channels.map(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item) || !Number.isInteger(item.character_count) || item.character_count < 0) throw new Error('提示词通道事实无效')
    return { key: text(item, 'key'), semanticKey: text(item, 'semantic_key'), text: text(item, 'text', true),
      characterCount: item.character_count as number, valueSource: text(item, 'value_source') }
  })
  if (new Set(channels.map(item => item.key)).size !== channels.length) throw new Error('提示词通道重复')
  return { taskId: text(raw, 'task_id'), attemptId: text(raw, 'attempt_id'), preparationId: text(raw, 'preparation_id'),
    executionInstanceVersionId: text(raw, 'execution_instance_version_id'), requestSha256: text(raw, 'request_sha256'),
    promptMode: raw.prompt_mode as TaskInputPrompt['promptMode'], promptProfileVersionId: raw.prompt_profile_version_id,
    channels, declaredReferenceLabels: raw.declared_reference_labels as string[] }
}
