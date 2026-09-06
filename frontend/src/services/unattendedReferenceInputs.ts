import type {
  DynamicReferenceTopologyCapability,
  ReferencePairingRuleCapability,
} from '@/services/conditioningCapabilities'
import type { UnattendedReferenceInput } from '@/services/platformTypes'

export interface UnattendedReferenceInputDraft extends UnattendedReferenceInput {
  rowKey: string
}

export interface UnattendedReferenceInputCardinality {
  minimumItems: number
  maximumItems: number
}

let nextRow = 0

export const createUnattendedReferenceInputDraft = (
  defaultPurposeKey = '',
): UnattendedReferenceInputDraft => ({
  rowKey: `unattended-reference-${++nextRow}`,
  assetId: '',
  purpose: defaultPurposeKey,
  useEmbeddedAudio: false,
})

export const createUnattendedReferenceInputDrafts = (
  cardinality: UnattendedReferenceInputCardinality,
  defaultPurposeKey = '',
): UnattendedReferenceInputDraft[] => Array.from(
  { length: Math.max(0, Math.min(cardinality.minimumItems, cardinality.maximumItems)) },
  () => createUnattendedReferenceInputDraft(defaultPurposeKey),
)

export const selectedUnattendedReferenceInputs = (
  drafts: UnattendedReferenceInputDraft[],
): UnattendedReferenceInput[] => drafts
  .filter((draft) => Boolean(draft.assetId))
  .map(({ assetId, purpose, useEmbeddedAudio }) => ({
    assetId,
    purpose,
    useEmbeddedAudio,
  }))

export const referencePairingRuleForPurpose = (
  topology: DynamicReferenceTopologyCapability | null,
  purpose: string,
): ReferencePairingRuleCapability | null => {
  if (!topology || !purpose) return null
  const matches = topology.pairingRules.filter((rule) => (
    rule.sourcePurposeKeys.includes(purpose)
  ))
  return matches.length === 1 ? matches[0] : null
}

interface ValidationInput {
  drafts: UnattendedReferenceInputDraft[]
  cardinality: UnattendedReferenceInputCardinality
  availableAssetIds: Set<string>
  topology: DynamicReferenceTopologyCapability | null
}

export const unattendedReferenceInputDraftError = ({
  drafts,
  cardinality,
  availableAssetIds,
  topology,
}: ValidationInput): string => {
  const selected = selectedUnattendedReferenceInputs(drafts)
  if (
    selected.length < cardinality.minimumItems
    || selected.length > cardinality.maximumItems
  ) {
    return `动态参考视频必须选择 ${cardinality.minimumItems}–${cardinality.maximumItems} 条；当前为 ${selected.length} 条。`
  }
  const assetIds = selected.map((item) => item.assetId)
  if (new Set(assetIds).size !== assetIds.length) return '同一动态参考视频不能重复选择。'
  if (assetIds.some((assetId) => !availableAssetIds.has(assetId))) {
    return '已选择的动态参考视频当前不可执行，请移除或改选。'
  }
  if (!selected.length) return ''
  if (!topology || !topology.videos.purposes.length) {
    return '所选工作流未冻结动态参考视频用途合同，不能创建运行。'
  }
  const purposeDefinitions = new Map(
    topology.videos.purposes.map((purpose) => [purpose.key, purpose]),
  )
  const purposeCounts = new Map<string, number>()
  for (const item of selected) {
    const definition = purposeDefinitions.get(item.purpose)
    if (!definition) return '请选择工作流合同声明的动态参考视频用途。'
    purposeCounts.set(item.purpose, (purposeCounts.get(item.purpose) || 0) + 1)
    const matches = topology.pairingRules.filter((rule) => (
      rule.sourcePurposeKeys.includes(item.purpose)
    ))
    if (matches.length > 1) return `用途“${definition.label || item.purpose}”命中多条音频配对规则。`
    const pairing = matches[0]
    if (!pairing) {
      if (item.useEmbeddedAudio) return `用途“${definition.label || item.purpose}”未声明可使用内嵌音轨。`
      continue
    }
    const pairedCount = item.useEmbeddedAudio ? 1 : 0
    if (item.useEmbeddedAudio && !pairing.allowEmbeddedSource) {
      return `用途“${definition.label || item.purpose}”不允许使用视频内嵌音轨。`
    }
    if (
      pairedCount < pairing.minimumTargetsPerSource
      || pairedCount > pairing.maximumTargetsPerSource
    ) {
      if (pairing.minimumTargetsPerSource > 0 && !pairing.allowEmbeddedSource) {
        return `用途“${definition.label || item.purpose}”要求外部配套音频；当前创建页尚未提交外部音频。`
      }
      return `用途“${definition.label || item.purpose}”要求启用视频内嵌音轨。`
    }
  }
  for (const definition of topology.videos.purposes) {
    const count = purposeCounts.get(definition.key) || 0
    if (count < definition.minimumItems || count > definition.maximumItems) {
      return `用途“${definition.label || definition.key}”允许 ${definition.minimumItems}–${definition.maximumItems} 条；当前为 ${count} 条。`
    }
  }
  for (const rule of topology.mutualExclusionRules.filter((item) => item.mediaKind === 'video')) {
    const used = rule.purposeKeys.filter((purpose) => (purposeCounts.get(purpose) || 0) > 0)
    if (used.length > rule.maximumDistinctPurposes) {
      return `动态参考视频不满足互斥规则“${rule.label || rule.key}”。`
    }
  }
  return ''
}
