export type ConditioningInputKind = 'text_only' | 'frame_slots' | 'classified_references'

export interface ConditioningInputCardinality {
  minimumItems: number
  maximumItems: number
}

export interface ConditioningModeInputs {
  totalImages: ConditioningInputCardinality
  firstFrame: ConditioningInputCardinality
  lastFrame: ConditioningInputCardinality
  entityImages: ConditioningInputCardinality
  referenceVideos: ConditioningInputCardinality
  pairedVideoAudios: ConditioningInputCardinality
  referenceAudios: ConditioningInputCardinality
}

export interface ReferencePurposeCapability extends ConditioningInputCardinality {
  key: string
  label: string
  description: string
}

export interface ReferenceMediaGroupCapability extends ConditioningInputCardinality {
  purposes: ReferencePurposeCapability[]
  defaultPurposeKey: string | null
}

export interface ReferencePairingRuleCapability {
  key: string
  label: string
  description: string
  sourceMediaKind: 'video'
  targetMediaKind: 'audio'
  sourcePurposeKeys: string[]
  targetPurposeKeys: string[]
  defaultTargetPurposeKey: string
  minimumTargetsPerSource: number
  maximumTargetsPerSource: number
  allowEmbeddedSource: boolean
  allowExternalTarget: boolean
  sourceAndExternalMutuallyExclusive: boolean
}

export interface ReferenceMutualExclusionRuleCapability {
  key: string
  label: string
  description: string
  mediaKind: 'video' | 'audio'
  purposeKeys: string[]
  maximumDistinctPurposes: number
}

export interface ReferenceVideoPreprocessingCapability {
  adapterKey: string
  minimumDurationMs: number
  maximumDurationMs: number
  targetFps: number | null
  requiresSourceFps: boolean
}

export interface ReferenceAudioPreprocessingCapability {
  adapterKey: string
  minimumDurationMs: number
  maximumDurationMs: number
}

export interface DynamicReferenceTopologyCapability {
  schemaVersion: 1
  videos: ReferenceMediaGroupCapability
  audios: ReferenceMediaGroupCapability
  preprocessing: {
    videos: ReferenceVideoPreprocessingCapability | null
    pairedVideoAudios: ReferenceAudioPreprocessingCapability | null
    referenceAudios: ReferenceAudioPreprocessingCapability | null
  }
  pairingRules: ReferencePairingRuleCapability[]
  mutualExclusionRules: ReferenceMutualExclusionRuleCapability[]
}

export interface ConditioningModeCapability {
  key: string
  label: string
  description: string
  inputKind: ConditioningInputKind
  requiresEntityReferenceBible: boolean
  supportsNativeAudio: boolean
  inputs: ConditioningModeInputs
  dynamicReferenceTopology: DynamicReferenceTopologyCapability | null
}

type Raw = Record<string, unknown>

const record = (value: unknown): Raw | null => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as Raw : null
)

const requiredText = (value: unknown, field: string): string => {
  const result = typeof value === 'string' ? value.trim() : ''
  if (!result) throw new Error(`条件模式能力缺少 ${field}`)
  return result
}
const SAFE_IDENTIFIER = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/
const identifier = (value: unknown, field: string): string => {
  const result = requiredText(value, field)
  if (!SAFE_IDENTIFIER.test(result)) throw new Error(`条件模式能力的 ${field} 不是安全 identifier`)
  return result
}

const cardinality = (value: unknown, field: string): ConditioningInputCardinality => {
  const raw = record(value)
  const minimumItems = Number(raw?.minimum_items)
  const maximumItems = Number(raw?.maximum_items)
  if (
    !raw
    || !Number.isSafeInteger(minimumItems)
    || minimumItems < 0
    || !Number.isSafeInteger(maximumItems)
    || maximumItems < minimumItems
  ) throw new Error(`条件模式能力的 ${field} 输入基数无效`)
  return { minimumItems, maximumItems }
}

const purposeDefinitions = (
  value: unknown,
  field: string,
  maximumItems: number,
): ReferencePurposeCapability[] => {
  if (!Array.isArray(value)) throw new Error(`条件模式能力的 ${field} 用途清单无效`)
  const purposes = value.map((item, index) => {
    const raw = record(item)
    if (!raw) throw new Error(`条件模式能力的 ${field}[${index}] 必须是对象`)
    const bounds = cardinality(raw, `${field}[${index}]`)
    if (bounds.maximumItems > maximumItems) throw new Error(`条件模式能力的 ${field}[${index}] 上限越界`)
    return {
      key: identifier(raw.key, `${field}[${index}].key`),
      label: requiredText(raw.label, `${field}[${index}].label`),
      description: typeof raw.description === 'string' ? raw.description.trim() : '',
      ...bounds,
    }
  })
  if (new Set(purposes.map((item) => item.key)).size !== purposes.length) throw new Error(`条件模式能力的 ${field} 用途 key 重复`)
  return purposes
}

const referencePreprocessing = (
  value: unknown,
  field: string,
  required: boolean,
  includeTargetFps: boolean,
): ReferenceVideoPreprocessingCapability | ReferenceAudioPreprocessingCapability | null => {
  if (value === null || value === undefined) {
    if (required) throw new Error(`条件模式能力的 ${field} 必须声明预处理 adapter`)
    return null
  }
  const raw = record(value)
  const expected = new Set([
    'adapter_key', 'minimum_duration_ms', 'maximum_duration_ms',
    ...(includeTargetFps ? ['target_fps', 'requires_source_fps'] : []),
  ])
  if (!raw || Object.keys(raw).some((key) => !expected.has(key)) || Object.keys(raw).length !== expected.size) {
    throw new Error(`条件模式能力的 ${field} 预处理合同不完整`)
  }
  const minimumDurationMs = Number(raw.minimum_duration_ms)
  const maximumDurationMs = Number(raw.maximum_duration_ms)
  if (
    !Number.isSafeInteger(minimumDurationMs)
    || !Number.isSafeInteger(maximumDurationMs)
    || minimumDurationMs < 1
    || maximumDurationMs < minimumDurationMs
  ) throw new Error(`条件模式能力的 ${field} 时长范围无效`)
  const common = {
    adapterKey: identifier(raw.adapter_key, `${field}.adapter_key`),
    minimumDurationMs,
    maximumDurationMs,
  }
  if (!includeTargetFps) return common
  const targetFps = raw.target_fps === null ? null : Number(raw.target_fps)
  if (targetFps !== null && (!Number.isSafeInteger(targetFps) || targetFps < 1)) {
    throw new Error(`条件模式能力的 ${field}.target_fps 无效`)
  }
  if (typeof raw.requires_source_fps !== 'boolean') {
    throw new Error(`条件模式能力的 ${field}.requires_source_fps 无效`)
  }
  return { ...common, targetFps, requiresSourceFps: raw.requires_source_fps }
}

const dynamicReferenceTopology = (
  value: unknown,
  inputs: ConditioningModeInputs,
): DynamicReferenceTopologyCapability | null => {
  const dynamicMaximum = Math.max(
    inputs.referenceVideos.maximumItems,
    inputs.pairedVideoAudios.maximumItems,
    inputs.referenceAudios.maximumItems,
  )
  if (value === null || value === undefined) {
    if (dynamicMaximum > 0) throw new Error('条件模式声明动态输入时必须冻结 dynamic_reference_topology')
    return null
  }
  const raw = record(value)
  if (!raw || raw.schema_version !== 1) throw new Error('dynamic_reference_topology schema_version 无效')
  const videosRaw = record(raw.videos)
  const audiosRaw = record(raw.audios)
  if (!videosRaw || !audiosRaw) throw new Error('dynamic_reference_topology 缺少视频或音频组')
  const videos = cardinality(videosRaw, 'dynamic_reference_topology.videos')
  const audios = cardinality(audiosRaw, 'dynamic_reference_topology.audios')
  if (
    videos.minimumItems !== inputs.referenceVideos.minimumItems
    || videos.maximumItems !== inputs.referenceVideos.maximumItems
    || audios.minimumItems !== inputs.referenceAudios.minimumItems
    || audios.maximumItems !== inputs.referenceAudios.maximumItems
  ) throw new Error('dynamic_reference_topology 媒体基数与 inputs 不一致')
  const videoPurposes = purposeDefinitions(
    videosRaw.purposes,
    'dynamic_reference_topology.videos.purposes',
    videos.maximumItems,
  )
  const audioPurposeMaximum = Math.max(audios.maximumItems, inputs.pairedVideoAudios.maximumItems)
  const audioPurposes = purposeDefinitions(
    audiosRaw.purposes,
    'dynamic_reference_topology.audios.purposes',
    audioPurposeMaximum,
  )
  if (videos.maximumItems > 0 && !videoPurposes.length) throw new Error('动态参考视频必须声明用途')
  if (audioPurposeMaximum > 0 && !audioPurposes.length) throw new Error('动态参考音频必须声明用途')
  const defaultPurpose = (rawValue: unknown, purposes: ReferencePurposeCapability[], field: string) => {
    if (!purposes.length) {
      if (rawValue !== null) throw new Error(`${field} 没有用途时必须为 null`)
      return null
    }
    const key = identifier(rawValue, field)
    if (!purposes.some((item) => item.key === key)) throw new Error(`${field} 未指向已声明用途`)
    return key
  }
  const videoDefaultPurposeKey = defaultPurpose(
    videosRaw.default_purpose_key,
    videoPurposes,
    'dynamic_reference_topology.videos.default_purpose_key',
  )
  const audioDefaultPurposeKey = defaultPurpose(
    audiosRaw.default_purpose_key,
    audioPurposes,
    'dynamic_reference_topology.audios.default_purpose_key',
  )
  const preprocessingRaw = record(raw.preprocessing)
  if (
    !preprocessingRaw
    || new Set(Object.keys(preprocessingRaw)).size !== 3
    || Object.keys(preprocessingRaw).some((key) => ![
      'videos', 'paired_video_audios', 'reference_audios',
    ].includes(key))
  ) {
    throw new Error('dynamic_reference_topology 缺少精确的 preprocessing 合同')
  }
  const videoPreprocessing = referencePreprocessing(
    preprocessingRaw.videos,
    'dynamic_reference_topology.preprocessing.videos',
    videos.maximumItems > 0,
    true,
  ) as ReferenceVideoPreprocessingCapability | null
  const pairedVideoAudioPreprocessing = referencePreprocessing(
    preprocessingRaw.paired_video_audios,
    'dynamic_reference_topology.preprocessing.paired_video_audios',
    inputs.pairedVideoAudios.maximumItems > 0,
    false,
  ) as ReferenceAudioPreprocessingCapability | null
  const referenceAudioPreprocessing = referencePreprocessing(
    preprocessingRaw.reference_audios,
    'dynamic_reference_topology.preprocessing.reference_audios',
    audios.maximumItems > 0,
    false,
  ) as ReferenceAudioPreprocessingCapability | null
  const videoPurposeKeys = new Set(videoPurposes.map((item) => item.key))
  const audioPurposeKeys = new Set(audioPurposes.map((item) => item.key))
  if (!Array.isArray(raw.pairing_rules)) throw new Error('动态参考配对规则无效')
  const pairingRules = raw.pairing_rules.map((item, index) => {
    const rule = record(item)
    if (!rule) throw new Error(`动态参考配对规则 ${index + 1} 必须是对象`)
    const sourcePurposeKeys = Array.isArray(rule.source_purpose_keys) ? rule.source_purpose_keys.map(String) : []
    const targetPurposeKeys = Array.isArray(rule.target_purpose_keys) ? rule.target_purpose_keys.map(String) : []
    const defaultTargetPurposeKey = typeof rule.default_target_purpose_key === 'string'
      ? rule.default_target_purpose_key.trim()
      : ''
    const minimumTargetsPerSource = Number(rule.minimum_targets_per_source)
    const maximumTargetsPerSource = Number(rule.maximum_targets_per_source)
    if (
      rule.source_media_kind !== 'video'
      || rule.target_media_kind !== 'audio'
      || !sourcePurposeKeys.length
      || sourcePurposeKeys.some((key) => !videoPurposeKeys.has(key))
      || !targetPurposeKeys.length
      || targetPurposeKeys.some((key) => !audioPurposeKeys.has(key))
      || !targetPurposeKeys.includes(defaultTargetPurposeKey)
      || !Number.isSafeInteger(minimumTargetsPerSource)
      || !Number.isSafeInteger(maximumTargetsPerSource)
      || minimumTargetsPerSource < 0
      || maximumTargetsPerSource < minimumTargetsPerSource
      || typeof rule.allow_embedded_source !== 'boolean'
      || typeof rule.allow_external_target !== 'boolean'
      || typeof rule.source_and_external_mutually_exclusive !== 'boolean'
    ) throw new Error(`动态参考配对规则 ${index + 1} 无效`)
    return {
      key: identifier(rule.key, `pairing_rules[${index}].key`),
      label: requiredText(rule.label, `pairing_rules[${index}].label`),
      description: typeof rule.description === 'string' ? rule.description.trim() : '',
      sourceMediaKind: 'video' as const,
      targetMediaKind: 'audio' as const,
      sourcePurposeKeys,
      targetPurposeKeys,
      defaultTargetPurposeKey,
      minimumTargetsPerSource,
      maximumTargetsPerSource,
      allowEmbeddedSource: rule.allow_embedded_source,
      allowExternalTarget: rule.allow_external_target,
      sourceAndExternalMutuallyExclusive: rule.source_and_external_mutually_exclusive,
    }
  })
  if (new Set(pairingRules.map((item) => item.key)).size !== pairingRules.length) throw new Error('动态参考配对规则 key 重复')
  if (!Array.isArray(raw.mutual_exclusion_rules)) throw new Error('动态参考互斥规则无效')
  const mutualExclusionRules = raw.mutual_exclusion_rules.map((item, index) => {
    const rule = record(item)
    if (!rule) throw new Error(`动态参考互斥规则 ${index + 1} 必须是对象`)
    const mediaKind = rule.media_kind
    const purposeKeys = Array.isArray(rule.purpose_keys) ? rule.purpose_keys.map(String) : []
    const allowed = mediaKind === 'video' ? videoPurposeKeys : mediaKind === 'audio' ? audioPurposeKeys : null
    const maximumDistinctPurposes = Number(rule.maximum_distinct_purposes)
    if (
      !allowed
      || purposeKeys.length < 2
      || purposeKeys.some((key) => !allowed.has(key))
      || !Number.isSafeInteger(maximumDistinctPurposes)
      || maximumDistinctPurposes < 1
      || maximumDistinctPurposes >= purposeKeys.length
    ) throw new Error(`动态参考互斥规则 ${index + 1} 无效`)
    return {
      key: identifier(rule.key, `mutual_exclusion_rules[${index}].key`),
      label: requiredText(rule.label, `mutual_exclusion_rules[${index}].label`),
      description: typeof rule.description === 'string' ? rule.description.trim() : '',
      mediaKind: mediaKind as 'video' | 'audio',
      purposeKeys,
      maximumDistinctPurposes,
    }
  })
  if (new Set(mutualExclusionRules.map((item) => item.key)).size !== mutualExclusionRules.length) throw new Error('动态参考互斥规则 key 重复')
  return {
    schemaVersion: 1,
    videos: { ...videos, purposes: videoPurposes, defaultPurposeKey: videoDefaultPurposeKey },
    audios: { ...audios, purposes: audioPurposes, defaultPurposeKey: audioDefaultPurposeKey },
    preprocessing: {
      videos: videoPreprocessing,
      pairedVideoAudios: pairedVideoAudioPreprocessing,
      referenceAudios: referenceAudioPreprocessing,
    },
    pairingRules,
    mutualExclusionRules,
  }
}

export const adaptConditioningModeCapability = (value: unknown): ConditioningModeCapability => {
  const raw = record(value)
  if (!raw) throw new Error('条件模式能力必须是对象')
  const inputKind = requiredText(raw.input_kind, 'input_kind')
  if (!['text_only', 'frame_slots', 'classified_references'].includes(inputKind)) {
    throw new Error(`条件模式能力使用了不受支持的 input_kind：${inputKind}`)
  }
  if (typeof raw.requires_entity_reference_bible !== 'boolean' || typeof raw.supports_native_audio !== 'boolean') {
    throw new Error('条件模式能力缺少明确的 Bible 或原生音频布尔合同')
  }
  const inputs = record(raw.inputs)
  if (!inputs) throw new Error('条件模式能力缺少 inputs 合同')
  const result: ConditioningModeCapability = {
    key: requiredText(raw.key, 'key'),
    label: requiredText(raw.label, 'label'),
    description: typeof raw.description === 'string' ? raw.description.trim() : '',
    inputKind: inputKind as ConditioningInputKind,
    requiresEntityReferenceBible: raw.requires_entity_reference_bible,
    supportsNativeAudio: raw.supports_native_audio,
    inputs: {
      totalImages: cardinality(inputs.total_images, 'total_images'),
      firstFrame: cardinality(inputs.first_frame, 'first_frame'),
      lastFrame: cardinality(inputs.last_frame, 'last_frame'),
      entityImages: cardinality(inputs.entity_images, 'entity_images'),
      referenceVideos: cardinality(inputs.reference_videos, 'reference_videos'),
      pairedVideoAudios: cardinality(inputs.paired_video_audios, 'paired_video_audios'),
      referenceAudios: cardinality(inputs.reference_audios, 'reference_audios'),
    },
    dynamicReferenceTopology: null,
  }
  result.dynamicReferenceTopology = dynamicReferenceTopology(raw.dynamic_reference_topology, result.inputs)
  const maximumDeclaredImages = (
    result.inputs.firstFrame.maximumItems
    + result.inputs.lastFrame.maximumItems
    + result.inputs.entityImages.maximumItems
  )
  if (result.inputs.totalImages.maximumItems > maximumDeclaredImages) {
    throw new Error('条件模式能力的 total_images 上限超过已声明图片槽位总量')
  }
  if (result.inputs.pairedVideoAudios.maximumItems > result.inputs.referenceVideos.maximumItems) {
    throw new Error('条件模式能力的 paired_video_audios 上限不能超过参考视频上限')
  }
  if (result.inputKind === 'text_only' && Object.values(result.inputs).some((item) => item.maximumItems > 0)) {
    throw new Error('text_only 条件模式不能声明参考输入槽位')
  }
  return result
}

export const adaptConditioningModes = (value: unknown): ConditioningModeCapability[] => {
  if (!Array.isArray(value)) throw new Error('条件模式能力清单必须是数组')
  const modes = value.map(adaptConditioningModeCapability)
  const keys = modes.map((item) => item.key)
  if (new Set(keys).size !== keys.length) throw new Error('条件模式能力清单包含重复 key')
  return modes
}

export const conditioningModeByKey = (
  modes: ConditioningModeCapability[],
  key: unknown,
): ConditioningModeCapability | undefined => {
  const normalized = typeof key === 'string' ? key.trim() : ''
  return modes.find((item) => item.key === normalized)
}

export const conditioningInputAvailable = (
  mode: ConditioningModeCapability | undefined,
  input: keyof ConditioningModeInputs,
): boolean => Boolean(mode && mode.inputs[input].maximumItems > 0)

export interface DynamicReferenceVideoBinding {
  assetId: string
  purpose: string
  useEmbeddedAudio: boolean
  audioAssetId?: string
  audioPurpose?: string
}

export interface DynamicReferenceAudioBinding {
  assetId: string
  purpose: string
}

/**
 * Validate the dynamic video/audio part of a classified-reference contract.
 * Storyboard production and standalone asset generation intentionally share
 * this one rule engine; callers only provide their own Asset visibility/probe
 * predicate.
 */
export const validateDynamicReferenceMediaBindings = (
  capability: ConditioningModeCapability,
  videos: DynamicReferenceVideoBinding[],
  audios: DynamicReferenceAudioBinding[],
  isMediaUsable: (
    assetId: string,
    kind: 'video' | 'audio',
    usage?: 'paired' | 'standalone',
  ) => boolean,
  hasEmbeddedAudio: (assetId: string) => boolean,
): string => {
  const topology = capability.dynamicReferenceTopology
  const dynamicMaximum = Math.max(
    capability.inputs.referenceVideos.maximumItems,
    capability.inputs.pairedVideoAudios.maximumItems,
    capability.inputs.referenceAudios.maximumItems,
  )
  if (!topology) {
    return dynamicMaximum > 0 || videos.length > 0 || audios.length > 0
      ? '当前工作流未冻结动态参考素材用途与配对合同，不能提交。'
      : ''
  }
  if (videos.length < topology.videos.minimumItems || videos.length > topology.videos.maximumItems) {
    return `参考视频数量必须为 ${topology.videos.minimumItems}–${topology.videos.maximumItems} 个。`
  }
  if (audios.length < topology.audios.minimumItems || audios.length > topology.audios.maximumItems) {
    return `独立参考音频数量必须为 ${topology.audios.minimumItems}–${topology.audios.maximumItems} 个。`
  }
  for (const [kind, items, purposes] of [
    ['视频', videos, topology.videos.purposes],
    ['音频', audios, topology.audios.purposes],
  ] as const) {
    const definitions = new Map(purposes.map((item) => [item.key, item]))
    for (const [index, item] of items.entries()) {
      if (!definitions.has(item.purpose)) return `参考${kind} ${index + 1} 的用途未在工作流合同中声明。`
    }
    for (const purpose of purposes) {
      const count = items.filter((item) => item.purpose === purpose.key).length
      if (count < purpose.minimumItems || count > purpose.maximumItems) {
        return `${purpose.label}（${purpose.key}）数量必须为 ${purpose.minimumItems}–${purpose.maximumItems} 个。`
      }
    }
  }
  for (const rule of topology.mutualExclusionRules) {
    const items = rule.mediaKind === 'video' ? videos : audios
    const used = new Set(items.map((item) => item.purpose).filter((purpose) => rule.purposeKeys.includes(purpose)))
    if (used.size > rule.maximumDistinctPurposes) {
      return `${rule.label}，最多允许 ${rule.maximumDistinctPurposes} 种用途。`
    }
  }
  const pairedVideoCount = videos.filter((item) => item.useEmbeddedAudio || item.audioAssetId).length
  if (
    pairedVideoCount < capability.inputs.pairedVideoAudios.minimumItems
    || pairedVideoCount > capability.inputs.pairedVideoAudios.maximumItems
  ) {
    return `带配套音频的视频数量必须为 ${capability.inputs.pairedVideoAudios.minimumItems}–${capability.inputs.pairedVideoAudios.maximumItems} 个。`
  }
  const mediaProbeRange = (kind: 'video' | 'audio') => {
    const preprocessing = topology.preprocessing[kind === 'video' ? 'videos' : 'referenceAudios']
    if (!preprocessing) return '工作流冻结的媒体事实'
    return `${(preprocessing.minimumDurationMs / 1000).toLocaleString('zh-CN')}–${(preprocessing.maximumDurationMs / 1000).toLocaleString('zh-CN')} 秒媒体探测`
  }
  for (const [index, item] of videos.entries()) {
    if (!isMediaUsable(item.assetId, 'video')) {
      return `参考视频 ${index + 1} 不存在、已停用或未通过${mediaProbeRange('video')}。`
    }
    if (item.useEmbeddedAudio && !hasEmbeddedAudio(item.assetId)) {
      return `参考视频 ${index + 1} 未检测到可用内嵌音轨。`
    }
    if (item.audioAssetId && !isMediaUsable(item.audioAssetId, 'audio', 'paired')) {
      return `参考视频 ${index + 1} 的外部配套音频无效。`
    }
    if ((item.useEmbeddedAudio || item.audioAssetId) && !item.audioPurpose) {
      return `参考视频 ${index + 1} 的配套音频必须选择用途。`
    }
    const rules = topology.pairingRules.filter((rule) => rule.sourcePurposeKeys.includes(item.purpose))
    if (rules.length > 1) return `参考视频 ${index + 1} 命中了多条配对规则。`
    const pairedTargets = Number(item.useEmbeddedAudio) + Number(Boolean(item.audioAssetId))
    const rule = rules[0]
    if (!rule) {
      if (pairedTargets) return `参考视频 ${index + 1} 的用途不允许绑定音频。`
      continue
    }
    if (pairedTargets < rule.minimumTargetsPerSource || pairedTargets > rule.maximumTargetsPerSource) {
      return `参考视频 ${index + 1} 的配套音频数量不满足“${rule.label}”。`
    }
    if (item.useEmbeddedAudio && !rule.allowEmbeddedSource) return `参考视频 ${index + 1} 的用途不允许内嵌音轨。`
    if (item.audioAssetId && !rule.allowExternalTarget) return `参考视频 ${index + 1} 的用途不允许外部配套音频。`
    if (item.useEmbeddedAudio && item.audioAssetId && rule.sourceAndExternalMutuallyExclusive) {
      return `参考视频 ${index + 1} 不能同时使用内嵌音轨和外配音频。`
    }
    if (pairedTargets && (!item.audioPurpose || !rule.targetPurposeKeys.includes(item.audioPurpose))) {
      return `参考视频 ${index + 1} 的音轨用途不在“${rule.label}”允许范围内。`
    }
  }
  for (const [index, item] of audios.entries()) {
    if (!isMediaUsable(item.assetId, 'audio', 'standalone')) {
      return `独立参考音频 ${index + 1} 不存在、已停用或未通过${mediaProbeRange('audio')}。`
    }
  }
  return ''
}
