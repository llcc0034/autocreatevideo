import type { PromptNormalizationDeployment, TranslationCapability } from '@/types'

const record = (value: unknown, label: string): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label} 响应必须是对象`)
  return value as Record<string, unknown>
}

const requiredString = (value: unknown, label: string): string => {
  const text = String(value ?? '').trim()
  if (!text) throw new Error(`${label} 缺失`)
  return text
}

const optionalString = (value: unknown): string | undefined => {
  const text = String(value ?? '').trim()
  return text || undefined
}

const deployment = (value: unknown): PromptNormalizationDeployment => {
  const raw = record(value, 'prompt.normalize deployment')
  const state = requiredString(raw.state, 'deployment.state')
  if (!['ready', 'smoke_test_required', 'unavailable'].includes(state)) throw new Error('deployment.state 无效')
  const transport = requiredString(raw.transport, 'deployment.transport')
  if (!['local_endpoint', 'remote_api'].includes(transport)) throw new Error('deployment.transport 无效')
  const dataEgress = raw.data_egress === true
  const ready = raw.ready === true
  if (ready !== (state === 'ready')) throw new Error('deployment.ready 与 state 不一致')
  if (transport === 'remote_api' && ready) throw new Error('外部 prompt.normalize deployment 不可标记 ready')
  if (dataEgress !== (transport === 'remote_api')) throw new Error('deployment.data_egress 与 transport 不一致')
  const capability = requiredString(raw.capability, 'deployment.capability')
  if (capability !== 'prompt.normalize') throw new Error('deployment.capability 无效')
  return {
    modelDeploymentId: requiredString(raw.model_deployment_id, 'model_deployment_id'),
    deploymentKey: requiredString(raw.deployment_key, 'deployment_key'),
    label: requiredString(raw.label, 'label'),
    providerKey: requiredString(raw.provider_key, 'provider_key'),
    providerLabel: requiredString(raw.provider_label, 'provider_label'),
    providerDriver: requiredString(raw.provider_driver, 'provider_driver'),
    modelProfileVersionId: requiredString(raw.model_profile_version_id, 'model_profile_version_id'),
    modelIdentifier: requiredString(raw.model_identifier, 'model_identifier'),
    capability: 'prompt.normalize',
    state: state as PromptNormalizationDeployment['state'],
    ready,
    reasonCode: requiredString(raw.reason_code, 'reason_code'),
    transport: transport as PromptNormalizationDeployment['transport'],
    dataEgress,
    dataEgressNotice: String(raw.data_egress_notice ?? ''),
    estimatedCost: optionalString(raw.estimated_cost),
    costCurrency: String(raw.cost_currency ?? ''),
    costBasis: requiredString(raw.cost_basis, 'cost_basis'),
    smokeSnapshotId: optionalString(raw.smoke_snapshot_id),
    smokeExpiresAt: optionalString(raw.smoke_expires_at),
  }
}

export const adaptPromptNormalizationCapability = (value: unknown): TranslationCapability => {
  const raw = record(value, 'prompt.normalize capability')
  if (Number(raw.schema_version) !== 2 || raw.capability_key !== 'prompt.normalize') {
    throw new Error('prompt.normalize capability 版本无效')
  }
  const hash = requiredString(raw.configuration_public_sha256, 'configuration_public_sha256')
  if (!/^[0-9a-f]{64}$/.test(hash)) throw new Error('configuration_public_sha256 无效')
  if (!Array.isArray(raw.deployments)) throw new Error('prompt.normalize deployments 缺失')
  return {
    schemaVersion: 2,
    capabilityKey: 'prompt.normalize',
    reviewRequired: raw.review_required === true,
    defaultModelDeploymentId: optionalString(raw.default_model_deployment_id),
    configurationPublicSha256: hash,
    deployments: raw.deployments.map(deployment),
  }
}
