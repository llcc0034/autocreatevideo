import type { PromptProviderCapability, PromptProviderId } from '@/types'

type Raw = Record<string, unknown>

const objectValue = (value: unknown): Raw => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as Raw : {}
)

const booleanValue = (raw: Raw, snake: string, camel: string, fallback: boolean): boolean => {
  const value = raw[snake] ?? raw[camel]
  return typeof value === 'boolean' ? value : fallback
}

export const promptProviderId = (value: unknown, fallback: PromptProviderId = 'local_qwen'): PromptProviderId => {
  const candidate = String(value || '').trim().toLowerCase()
  if (candidate === 'deepseek_api' || candidate === 'deepseek' || candidate === 'deepseek-api') return 'deepseek_api'
  if (candidate === 'local_qwen' || candidate === 'qwen-local' || candidate === 'local-qwen') return 'local_qwen'
  return fallback
}

export const adaptPromptProviderCapability = (
  value: unknown,
  fallbackId: PromptProviderId,
): PromptProviderCapability => {
  const raw = objectValue(value)
  const id = promptProviderId(raw.id ?? raw.provider, fallbackId)
  const enabled = booleanValue(raw, 'enabled', 'enabled', true)
  const configured = booleanValue(raw, 'configured', 'configured', false)
  const modelDownloaded = booleanValue(raw, 'model_downloaded', 'modelDownloaded', false)
  const modelVerified = booleanValue(raw, 'model_verified', 'modelVerified', false)
  const explicitReady = raw.ready
  const ready = typeof explicitReady === 'boolean'
    ? explicitReady
    : id === 'deepseek_api'
      ? enabled && configured
      : enabled && configured && modelDownloaded && modelVerified
  const transportValue = String(raw.transport || '').trim().toLowerCase()
  const transport = ['remote', 'remote_api'].includes(transportValue) || id === 'deepseek_api'
    ? 'remote'
    : 'local'
  return {
    id,
    label: String(raw.label || (id === 'deepseek_api' ? 'DeepSeek API' : '本地 Qwen')),
    enabled,
    configured,
    ready,
    model: String(raw.model || ''),
    transport,
    dataEgressNotice: String(raw.data_egress_notice ?? raw.dataEgressNotice ?? '').trim() || undefined,
    notice: String(raw.notice ?? raw.message ?? '').trim() || undefined,
    modelDownloaded,
    modelVerified,
    device: String(raw.device || '').trim() || undefined,
  }
}

export const adaptPromptProviders = (
  rawCapability: Raw,
  fallbackId: PromptProviderId = 'local_qwen',
): { defaultProvider: PromptProviderId; providers: PromptProviderCapability[] } => {
  const defaultProvider = promptProviderId(
    rawCapability.default_provider ?? rawCapability.defaultProvider ?? rawCapability.provider,
    fallbackId,
  )
  const rows = Array.isArray(rawCapability.providers) ? rawCapability.providers : [rawCapability]
  const providers = rows.map((row, index) => adaptPromptProviderCapability(
    row,
    index === 0 ? defaultProvider : fallbackId,
  ))
  const deduplicated = [...new Map(providers.map((provider) => [provider.id, provider])).values()]
  return {
    defaultProvider: deduplicated.some((provider) => provider.id === defaultProvider)
      ? defaultProvider
      : deduplicated[0]?.id || fallbackId,
    providers: deduplicated,
  }
}

export const promptProvider = (
  providers: PromptProviderCapability[] | undefined,
  id: PromptProviderId,
): PromptProviderCapability | undefined => providers?.find((provider) => provider.id === id)

export const REMOTE_PROMPT_EGRESS_NOTICE = '远程通道会将本次提示词及生成所需的项目上下文发送给外部 DeepSeek API。'
