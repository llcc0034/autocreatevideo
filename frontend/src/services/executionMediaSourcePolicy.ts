import type { ExecutionInput } from './executionRegistry'
import { selectedMediaSource, type MediaKind, type MediaSourcePolicy } from './inputSourcePolicies'

export type ExecutionMediaSourcePolicy = Pick<MediaSourcePolicy, 'schema_version'|'allowed_sources'|'default_source'|'allow_override'|'order'>

export function executionMediaPolicy(input: ExecutionInput): MediaSourcePolicy | null {
  if (!input.media_source_policy) return null
  return { ...input.media_source_policy, media_kind: input.kind as MediaKind,
    min_items: input.min_items, max_items: input.max_items, allowed_purposes: input.semantic_contract.allowed_purposes }
}

export function executionMediaSelection(input: ExecutionInput, mode: string | undefined, items: Record<string, unknown>[]) {
  const policy = executionMediaPolicy(input)
  if (!policy) return { items }
  if (!mode) throw new Error(`${input.key} 请明确选择素材来源`)
  const kind = mode === 'selected_output' ? 'selected_output' : 'asset'
  if (items.some(item => item.kind !== kind)) throw new Error(`${input.key} 素材与所选来源不一致，请重新选择`)
  const compact = { source: mode, items: items.map(item => ({ item_key: String(item.item_key), purpose: item.purpose as string|null,
    ...(kind === 'asset' ? {asset_id: item.asset_id as string} : {decision_id: item.decision_id as string, artifact_id: item.artifact_id as string}) })) }
  const selection = { source: policy.default_source, items: mode === policy.default_source ? items : [] }
  const overridden = mode !== policy.default_source
  selectedMediaSource(policy, overridden ? {source:policy.default_source,items:[]} : compact, overridden ? compact : undefined)
  return { selection, ...(overridden ? {override:{source:mode,items}} : {}) }
}
