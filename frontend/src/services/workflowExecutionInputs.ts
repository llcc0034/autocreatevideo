import type { Asset } from '@/types'
import type {
  ComfyWorkflowParameterScalar,
  WorkflowExecutionInput,
  WorkflowExecutionInputSchema,
  WorkflowSmokeFixtureParameter,
} from './platformTypes'
import {
  validateDynamicReferenceMediaBindings,
  type ConditioningModeCapability,
} from './conditioningCapabilities'

// HTML number steps start at `min`, but contract multiples start at zero (or
// an explicit alignment remainder). Never let native validation reject a
// contract-valid value just because a narrowed lower bound is off the lattice.
export const workflowExecutionParameterStep = (parameter: WorkflowSmokeFixtureParameter): number | 'any' => {
  const step = parameter.alignmentModulus ?? parameter.multipleOf ?? (parameter.type === 'integer' ? 1 : null)
  if (!step || parameter.min === null) return 'any'
  const integral = (value: number) => Math.abs(value - Math.round(value)) < 1e-9
  if (parameter.alignmentModulus && !integral((parameter.min - (parameter.alignmentRemainder ?? 0)) / parameter.alignmentModulus)) return 'any'
  if (parameter.multipleOf && (!integral(parameter.min / parameter.multipleOf) || !integral(step / parameter.multipleOf))) return 'any'
  if (parameter.type === 'integer' && !Number.isInteger(parameter.min)) return 'any'
  return step
}

export type WorkflowInputAssetCandidate = Pick<Asset, 'id' | 'name'> & Partial<Pick<
  Asset,
  'type' | 'mimeType' | 'checksum' | 'isActive' | 'backendKind' | 'metadata' | 'hasEmbeddedAudio'
>> & {
  smokeEligible?: boolean
  smokeIneligibleReason?: string
  durationMs?: number | null
  sourceFps?: number | null
}

export type WorkflowInputAssetBindingTarget =
  | { kind: 'asset_slot'; semanticKey: string }
  | { kind: 'picture'; rowKey: string }
  | { kind: 'video'; rowKey: string }
  | { kind: 'paired_audio'; rowKey: string }
  | { kind: 'audio'; rowKey: string }

export interface WorkflowInputAssetUploadRequest {
  key: string
  mediaKind: 'image' | 'video' | 'audio'
  target: WorkflowInputAssetBindingTarget
}

export interface WorkflowPictureInputDraft {
  key: string
  assetId: string
  category: 'character' | 'scene' | 'prop' | 'continuity_frame'
}

export interface WorkflowVideoInputDraft {
  key: string
  assetId: string
  purpose: string
  useEmbeddedAudio: boolean
  audioAssetId: string
  audioPurpose: string
}

export interface WorkflowAudioInputDraft {
  key: string
  assetId: string
  purpose: string
}

export interface WorkflowExecutionInputDraft {
  values: Record<string, ComfyWorkflowParameterScalar | null>
  assetIds: Record<string, string>
  pictures: WorkflowPictureInputDraft[]
  videos: WorkflowVideoInputDraft[]
  audios: WorkflowAudioInputDraft[]
}

let draftSequence = 0
export const workflowInputDraftKey = () => `workflow-input-${Date.now()}-${++draftSequence}`

export const createWorkflowExecutionInputDraft = (
  schema: WorkflowExecutionInputSchema | null | undefined,
): WorkflowExecutionInputDraft => ({
  values: Object.fromEntries((schema?.parameters || []).map((parameter) => [
    parameter.semanticKey,
    parameter.recommendedValue === undefined ? null : parameter.recommendedValue,
  ])),
  assetIds: Object.fromEntries((schema?.assetSlots || []).map((slot) => [slot.semanticKey, ''])),
  pictures: [],
  videos: [],
  audios: [],
})

export const workflowAssetMediaKind = (asset: WorkflowInputAssetCandidate): 'image' | 'video' | 'audio' | null => {
  if (
    asset.isActive === false
    || asset.smokeEligible === false
    || asset.type === 'script'
    || !/^[0-9a-f]{64}$/i.test(asset.checksum || '')
  ) return null
  if (asset.mimeType?.startsWith('video/')) return 'video'
  if (asset.mimeType?.startsWith('audio/')) return 'audio'
  if (asset.mimeType?.startsWith('image/')) return 'image'
  if (asset.type === 'video') return 'video'
  if (asset.type === 'audio') return 'audio'
  return 'image'
}

export const compatibleWorkflowAssets = (
  assets: WorkflowInputAssetCandidate[],
  mediaKind: 'image' | 'video' | 'audio',
) => assets.filter((asset) => workflowAssetMediaKind(asset) === mediaKind)

const mediaDurationMs = (asset: WorkflowInputAssetCandidate) => {
  const value = asset.durationMs ?? Number(asset.metadata?.duration_ms)
  return Number.isSafeInteger(value) && Number(value) > 0 ? Number(value) : 0
}

const mediaSourceFps = (asset: WorkflowInputAssetCandidate) => {
  const value = asset.sourceFps ?? Number(asset.metadata?.source_fps ?? asset.metadata?.fps)
  return Number.isFinite(value) && Number(value) > 0 ? Number(value) : 0
}

export const dynamicWorkflowAssetUsable = (
  asset: WorkflowInputAssetCandidate,
  kind: 'video' | 'audio',
  capability: ConditioningModeCapability,
  usage: 'paired' | 'standalone' = 'standalone',
) => {
  if (workflowAssetMediaKind(asset) !== kind) return false
  if (asset.backendKind !== `reference_${kind}`) return false
  const topology = capability.dynamicReferenceTopology
  if (!topology) return false
  if (kind === 'video') {
    const preprocessing = topology.preprocessing.videos
    if (!preprocessing) return false
    const duration = mediaDurationMs(asset)
    return duration >= preprocessing.minimumDurationMs
      && duration <= preprocessing.maximumDurationMs
      && (!preprocessing.requiresSourceFps || mediaSourceFps(asset) > 0)
  }
  const preprocessing = usage === 'paired'
    ? topology.preprocessing.pairedVideoAudios
    : topology.preprocessing.referenceAudios
  if (!preprocessing) return false
  const duration = mediaDurationMs(asset)
  return duration >= preprocessing.minimumDurationMs && duration <= preprocessing.maximumDurationMs
}

export const compatibleDynamicWorkflowAssets = (
  assets: WorkflowInputAssetCandidate[],
  kind: 'video' | 'audio',
  capability: ConditioningModeCapability,
  usage: 'paired' | 'standalone' = 'standalone',
) => assets.filter((asset) => dynamicWorkflowAssetUsable(asset, kind, capability, usage))

export const workflowInputUploadCandidateCompatible = (
  schema: WorkflowExecutionInputSchema,
  draft: WorkflowExecutionInputDraft,
  request: WorkflowInputAssetUploadRequest,
  asset: WorkflowInputAssetCandidate,
) => {
  const target = request.target
  if (target.kind === 'asset_slot') {
    const semanticKey = target.semanticKey
    const slot = schema.assetSlots.find((item) => item.semanticKey === semanticKey)
    return Boolean(
      slot
      && slot.mediaKind === request.mediaKind
      && compatibleWorkflowAssets([asset], slot.mediaKind).length,
    )
  }
  if (target.kind === 'picture') {
    const rowKey = target.rowKey
    return request.mediaKind === 'image'
      && draft.pictures.some((item) => item.key === rowKey)
      && compatibleWorkflowAssets([asset], 'image').length > 0
  }
  const dynamic = schema.dynamicReferenceModes[
    String(draft.values.CONDITIONING_MODE || '')
  ] || schema.dynamicReferences
  if (!dynamic) return false
  if (target.kind === 'video') {
    const rowKey = target.rowKey
    return request.mediaKind === 'video'
      && draft.videos.some((item) => item.key === rowKey)
      && dynamicWorkflowAssetUsable(asset, 'video', dynamic.conditioningMode)
  }
  if (target.kind === 'paired_audio') {
    const rowKey = target.rowKey
    return request.mediaKind === 'audio'
      && draft.videos.some((item) => item.key === rowKey)
      && dynamicWorkflowAssetUsable(asset, 'audio', dynamic.conditioningMode, 'paired')
  }
  const rowKey = target.rowKey
  return request.mediaKind === 'audio'
    && draft.audios.some((item) => item.key === rowKey)
    && dynamicWorkflowAssetUsable(asset, 'audio', dynamic.conditioningMode)
}

export const bindUploadedWorkflowInputAsset = (
  draft: WorkflowExecutionInputDraft,
  target: WorkflowInputAssetBindingTarget,
  assetId: string,
): WorkflowExecutionInputDraft | null => {
  if (!assetId) return null
  if (target.kind === 'asset_slot') {
    if (!(target.semanticKey in draft.assetIds)) return null
    return {
      ...draft,
      assetIds: { ...draft.assetIds, [target.semanticKey]: assetId },
    }
  }
  if (target.kind === 'picture') {
    if (!draft.pictures.some((item) => item.key === target.rowKey)) return null
    return {
      ...draft,
      pictures: draft.pictures.map((item) => (
        item.key === target.rowKey ? { ...item, assetId } : item
      )),
    }
  }
  if (target.kind === 'video') {
    if (!draft.videos.some((item) => item.key === target.rowKey)) return null
    return {
      ...draft,
      videos: draft.videos.map((item) => (
        item.key === target.rowKey ? { ...item, assetId } : item
      )),
    }
  }
  if (target.kind === 'paired_audio') {
    if (!draft.videos.some((item) => item.key === target.rowKey)) return null
    return {
      ...draft,
      videos: draft.videos.map((item) => (
        item.key === target.rowKey
          ? { ...item, audioAssetId: assetId, useEmbeddedAudio: false }
          : item
      )),
    }
  }
  if (!draft.audios.some((item) => item.key === target.rowKey)) return null
  return {
    ...draft,
    audios: draft.audios.map((item) => (
      item.key === target.rowKey ? { ...item, assetId } : item
    )),
  }
}

const scalarError = (
  parameter: WorkflowSmokeFixtureParameter,
  value: ComfyWorkflowParameterScalar | null | undefined,
) => {
  if (value === null || value === undefined || value === '') {
    return parameter.required ? `${parameter.semanticKey} 为必填项` : ''
  }
  if (parameter.type === 'integer' && (typeof value !== 'number' || !Number.isInteger(value))) {
    return `${parameter.semanticKey} 必须是整数`
  }
  if (parameter.type === 'number' && (typeof value !== 'number' || !Number.isFinite(value))) {
    return `${parameter.semanticKey} 必须是有限数字`
  }
  if (parameter.type === 'boolean' && typeof value !== 'boolean') return `${parameter.semanticKey} 必须是布尔值`
  if (parameter.type === 'string' && typeof value !== 'string') return `${parameter.semanticKey} 必须是文本`
  if (parameter.options.length && !parameter.options.some((option) => Object.is(option, value))) {
    return `${parameter.semanticKey} 不在允许选项中`
  }
  if (typeof value === 'number') {
    if (parameter.min !== null && value < parameter.min) return `${parameter.semanticKey} 不能小于 ${parameter.min}`
    if (parameter.max !== null && value > parameter.max) return `${parameter.semanticKey} 不能大于 ${parameter.max}`
    if (parameter.multipleOf !== null && parameter.multipleOf > 0
      && Math.abs(value / parameter.multipleOf - Math.round(value / parameter.multipleOf)) > 1e-9) {
      return `${parameter.semanticKey} 必须是 ${parameter.multipleOf} 的倍数`
    }
    const modulus = parameter.alignmentModulus
    const remainder = parameter.alignmentRemainder
    if ((modulus === null) !== (remainder === null)) {
      return `${parameter.semanticKey} 的整数对齐合同无效`
    }
    if (modulus !== null && remainder !== null) {
      if (
        parameter.type !== 'integer' || !Number.isSafeInteger(modulus) || modulus < 1
        || !Number.isSafeInteger(remainder) || remainder < 0 || remainder >= modulus
      ) {
        return `${parameter.semanticKey} 的整数对齐合同无效`
      }
      const normalizedRemainder = ((value % modulus) + modulus) % modulus
      if (Math.abs(normalizedRemainder - remainder) > 1e-9) {
        return `${parameter.semanticKey} 必须满足 ${remainder} + ${modulus}k`
      }
    }
  }
  if (typeof value === 'string') {
    if (parameter.minLength != null && value.length < parameter.minLength) return `${parameter.semanticKey} 长度不能小于 ${parameter.minLength}`
    if (parameter.maxLength != null && value.length > parameter.maxLength) return `${parameter.semanticKey} 长度不能大于 ${parameter.maxLength}`
  }
  return ''
}

export const validateWorkflowExecutionInputDraft = (
  schema: WorkflowExecutionInputSchema | null | undefined,
  draft: WorkflowExecutionInputDraft,
  assets: WorkflowInputAssetCandidate[] = [],
) => {
  if (!schema?.available) return `工作流输入合同不可用：${schema?.reasonCode || 'workflow_input_contract_missing'}`
  for (const parameter of schema.parameters) {
    const error = scalarError(parameter, draft.values[parameter.semanticKey])
    if (error) return error
  }
  for (const slot of schema.assetSlots) {
    if (slot.required && !draft.assetIds[slot.semanticKey]) return `请选择必填素材槽 ${slot.semanticKey}`
    const assetId = draft.assetIds[slot.semanticKey]
    if (assetId && !compatibleWorkflowAssets(assets, slot.mediaKind).some((asset) => asset.id === assetId)) {
      return `${slot.semanticKey} 选择的素材已停用、摘要无效或媒体类型不匹配`
    }
  }
  for (const group of schema.assetGroups || []) {
    const selected = group.semanticKeys.filter((key) => draft.assetIds[key])
    if (selected.length < group.minItems || selected.length > group.maxItems
      || selected.some((key, index) => key !== group.semanticKeys[index])) {
      return `${group.key} 必须按顺序选择 ${group.minItems}–${group.maxItems} 个素材，不能跳过中间项`
    }
  }
  const explicitDimensions = ['image.width', 'image.height'].filter((key) => {
    const value = draft.values[key]
    return value !== null && value !== undefined && value !== ''
  })
  if (explicitDimensions.length === 1) return 'image.width/image.height 必须成对填写'
  const dynamic = schema.dynamicReferenceModes[
    String(draft.values.CONDITIONING_MODE || '')
  ] || schema.dynamicReferences
  if (!dynamic) return ''
  if (draft.pictures.length < dynamic.pictures.minItems || draft.pictures.length > dynamic.pictures.maxItems) {
    return `参考图片数量必须在 ${dynamic.pictures.minItems}–${dynamic.pictures.maxItems}`
  }
  if (draft.videos.length < dynamic.videos.minItems || draft.videos.length > dynamic.videos.maxItems) {
    return `参考视频数量必须在 ${dynamic.videos.minItems}–${dynamic.videos.maxItems}`
  }
  if (draft.audios.length < dynamic.audios.minItems || draft.audios.length > dynamic.audios.maxItems) {
    return `独立参考音频数量必须在 ${dynamic.audios.minItems}–${dynamic.audios.maxItems}`
  }
  if (draft.pictures.some((item) => (
    !item.assetId
    || !dynamic.pictures.categories.includes(item.category)
    || !compatibleWorkflowAssets(assets, 'image').some((asset) => asset.id === item.assetId)
  ))) return '参考图片必须选择合同允许的分类和可用 image 素材'
  if (draft.pictures.filter((item) => item.category === 'continuity_frame').length > 1) {
    return 'continuity_frame 参考图最多一张'
  }
  const mediaError = validateDynamicReferenceMediaBindings(
    dynamic.conditioningMode,
    draft.videos,
    draft.audios,
    (assetId, kind, usage) => {
      const asset = assets.find((item) => item.id === assetId)
      return Boolean(asset && dynamicWorkflowAssetUsable(
        asset,
        kind,
        dynamic.conditioningMode,
        usage || 'standalone',
      ))
    },
    (assetId) => assets.find((item) => item.id === assetId)?.hasEmbeddedAudio === true,
  )
  if (mediaError) return mediaError
  const ids = [
    ...draft.pictures.map((item) => item.assetId),
    ...draft.videos.flatMap((item) => [item.assetId, item.audioAssetId].filter(Boolean)),
    ...draft.audios.map((item) => item.assetId),
  ]
  if (ids.length !== new Set(ids).size) return '同一素材不能在动态参考输入中重复使用'
  if (ids.length > dynamic.maximumPhysicalAssets) return `动态参考物理素材最多 ${dynamic.maximumPhysicalAssets} 个`
  return ''
}

export const buildWorkflowExecutionInput = (
  schema: WorkflowExecutionInputSchema,
  draft: WorkflowExecutionInputDraft,
): WorkflowExecutionInput => ({
  schemaVersion: 2,
  values: Object.fromEntries(schema.parameters.flatMap((parameter) => {
    const value = draft.values[parameter.semanticKey]
    return value === null || value === undefined || (value === '' && !parameter.required)
      ? [] : [[parameter.semanticKey, value as ComfyWorkflowParameterScalar]]
  })),
  assets: schema.assetSlots.flatMap((slot) => {
    const assetId = draft.assetIds[slot.semanticKey]
    return assetId ? [{ semanticKey: slot.semanticKey, assetId }] : []
  }),
  references: {
    pictures: draft.pictures.map(({ assetId, category }) => ({ assetId, category })),
    videos: draft.videos.map((item) => ({
      assetId: item.assetId,
      purpose: item.purpose,
      useEmbeddedAudio: item.useEmbeddedAudio,
      ...(item.audioAssetId ? { audioAssetId: item.audioAssetId } : {}),
      ...((item.useEmbeddedAudio || item.audioAssetId) ? { audioPurpose: item.audioPurpose } : {}),
    })),
    audios: draft.audios.map(({ assetId, purpose }) => ({ assetId, purpose })),
  },
})
