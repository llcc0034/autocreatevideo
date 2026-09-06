import type { JsonObject, ShotReferenceBindingsInput } from './platformTypes'
import type { StoryboardReviewShot } from './productionRunReview'
import { validateDynamicReferenceMediaBindings } from './conditioningCapabilities'

export type StoryboardVideoPurpose = NonNullable<ShotReferenceBindingsInput['videos']>[number]['purpose']
export type StoryboardAudioPurpose = NonNullable<ShotReferenceBindingsInput['audios']>[number]['purpose']
export type StoryboardVideoBindingDraft = NonNullable<ShotReferenceBindingsInput['videos']>[number]
export type StoryboardAudioBindingDraft = NonNullable<ShotReferenceBindingsInput['audios']>[number]

export interface StoryboardShotReferenceDraft {
  firstFrameAssetId: string
  lastFrameAssetId: string
  firstFrameReferenceAssetId: string
  characterAssetIds: Record<string, string>
  sceneAssetId: string
  propAssetIds: Record<string, string>
  videos: StoryboardVideoBindingDraft[]
  audios: StoryboardAudioBindingDraft[]
}

export const emptyStoryboardReferenceDraft = (): StoryboardShotReferenceDraft => ({
  firstFrameAssetId: '',
  lastFrameAssetId: '',
  firstFrameReferenceAssetId: '',
  characterAssetIds: {},
  sceneAssetId: '',
  propAssetIds: {},
  videos: [],
  audios: [],
})

export const hydrateStoryboardReferenceDraft = (
  bindings: ShotReferenceBindingsInput | undefined,
): StoryboardShotReferenceDraft => ({
  firstFrameAssetId: bindings?.firstFrameAssetId || '',
  lastFrameAssetId: bindings?.lastFrameAssetId || '',
  firstFrameReferenceAssetId: bindings?.firstFrameReferenceAssetId || '',
  characterAssetIds: Object.fromEntries(
    (bindings?.characters || []).map((item) => [item.entityId, item.assetId]),
  ),
  sceneAssetId: bindings?.scene?.assetId || '',
  propAssetIds: Object.fromEntries(
    (bindings?.props || []).map((item) => [item.entityId, item.assetId]),
  ),
  videos: (bindings?.videos || []).map((item) => ({
    assetId: item.assetId,
    purpose: item.purpose,
    useEmbeddedAudio: item.useEmbeddedAudio,
    ...(item.audioAssetId ? { audioAssetId: item.audioAssetId } : {}),
    ...((item.useEmbeddedAudio || item.audioAssetId) && item.audioPurpose
      ? { audioPurpose: item.audioPurpose }
      : {}),
  })),
  audios: (bindings?.audios || []).map((item) => ({ ...item })),
})

export const buildStoryboardShotReferenceBindings = (
  shot: StoryboardReviewShot,
  draft: StoryboardShotReferenceDraft,
): ShotReferenceBindingsInput => {
  const capability = shot.conditioningModeCapability
  if (shot.mediaType === 'image' || !capability || capability.inputKind === 'text_only') return {}
  if (capability.inputKind === 'frame_slots') return {
    ...(draft.firstFrameAssetId ? { firstFrameAssetId: draft.firstFrameAssetId } : {}),
    ...(draft.lastFrameAssetId ? { lastFrameAssetId: draft.lastFrameAssetId } : {}),
  }
  return {
    ...(draft.firstFrameReferenceAssetId
      ? { firstFrameReferenceAssetId: draft.firstFrameReferenceAssetId }
      : {}),
    characters: shot.visualReferenceCharacterIds.flatMap((entityId) => (
      draft.characterAssetIds[entityId]
        ? [{ entityId, assetId: draft.characterAssetIds[entityId] }]
        : []
    )),
    ...(shot.sceneId && draft.sceneAssetId ? {
      scene: { entityId: shot.sceneId, assetId: draft.sceneAssetId },
    } : {}),
    props: shot.propIds.flatMap((entityId) => (
      draft.propAssetIds[entityId]
        ? [{ entityId, assetId: draft.propAssetIds[entityId] }]
        : []
    )),
    ...(draft.videos.length ? {
      videos: draft.videos.map((item) => ({
        assetId: item.assetId,
        purpose: item.purpose,
        useEmbeddedAudio: item.useEmbeddedAudio,
        ...(item.audioAssetId ? { audioAssetId: item.audioAssetId } : {}),
        ...((item.useEmbeddedAudio || item.audioAssetId) && item.audioPurpose
          ? { audioPurpose: item.audioPurpose }
          : {}),
      })),
    } : {}),
    ...(draft.audios.length ? { audios: draft.audios.map((item) => ({ ...item })) } : {}),
  }
}

export const storyboardDraftAssetIds = (draft: StoryboardShotReferenceDraft): string[] => [
  ...new Set([
    draft.firstFrameAssetId,
    draft.lastFrameAssetId,
    draft.firstFrameReferenceAssetId,
    ...Object.values(draft.characterAssetIds),
    draft.sceneAssetId,
    ...Object.values(draft.propAssetIds),
    ...draft.videos.flatMap((item) => [item.assetId, item.audioAssetId || '']),
    ...draft.audios.map((item) => item.assetId),
  ].filter(Boolean)),
]

export const validateStoryboardFrameDraft = (
  shot: StoryboardReviewShot,
  draft: StoryboardShotReferenceDraft,
  isUsable: (assetId: string) => boolean,
): string => {
  const capability = shot.conditioningModeCapability
  if (shot.mediaType === 'image' || !capability || capability.inputKind !== 'frame_slots') return ''
  if (capability.inputs.firstFrame.minimumItems > 0 && !isUsable(draft.firstFrameAssetId)) {
    return `镜头 ${shot.number}：请选择本 Run 已批准的精确首帧 Asset。`
  }
  if (capability.inputs.lastFrame.minimumItems > 0 && !isUsable(draft.lastFrameAssetId)) {
    return `镜头 ${shot.number}：请选择本 Run 已批准的精确尾帧 Asset。`
  }
  if (draft.firstFrameAssetId && !isUsable(draft.firstFrameAssetId)) {
    return `镜头 ${shot.number}：所选首帧不是本 Run 已批准的可用 Asset。`
  }
  if (draft.lastFrameAssetId && !isUsable(draft.lastFrameAssetId)) {
    return `镜头 ${shot.number}：所选尾帧不是本 Run 已批准的可用 Asset。`
  }
  return draft.firstFrameAssetId && draft.firstFrameAssetId === draft.lastFrameAssetId
    ? `镜头 ${shot.number}：首帧和尾帧不能复用同一 Asset。`
    : ''
}

export const validateStoryboardMediaDraft = (
  shot: StoryboardReviewShot,
  draft: StoryboardShotReferenceDraft,
  isMediaUsable: (
    assetId: string,
    kind: 'video' | 'audio',
    usage?: 'paired' | 'standalone',
  ) => boolean,
  hasEmbeddedAudio: (assetId: string) => boolean,
): string => {
  const capability = shot.conditioningModeCapability
  if (shot.mediaType !== 'video' || !capability || capability.inputKind !== 'classified_references') return ''
  const dynamicError = validateDynamicReferenceMediaBindings(
    capability,
    draft.videos,
    draft.audios,
    isMediaUsable,
    hasEmbeddedAudio,
  )
  if (dynamicError) return `镜头 ${shot.number}：${dynamicError}`
  const allIds = storyboardDraftAssetIds(draft)
  const rawIds = [
    draft.firstFrameAssetId,
    draft.lastFrameAssetId,
    draft.firstFrameReferenceAssetId,
    ...Object.values(draft.characterAssetIds),
    draft.sceneAssetId,
    ...Object.values(draft.propAssetIds),
    ...draft.videos.flatMap((item) => [item.assetId, item.audioAssetId || '']),
    ...draft.audios.map((item) => item.assetId),
  ].filter(Boolean)
  if (allIds.length !== rawIds.length) {
    return `镜头 ${shot.number}：同一素材不能重复承担图片、视频或音频槽位。`
  }
  const pictureCount = Number(Boolean(draft.firstFrameReferenceAssetId))
    + Object.values(draft.characterAssetIds).filter(Boolean).length
    + Object.values(draft.propAssetIds).filter(Boolean).length
    + Number(Boolean(draft.sceneAssetId))
  if (pictureCount > capability.inputs.totalImages.maximumItems) return `镜头 ${shot.number}：分类参考图最多允许 ${capability.inputs.totalImages.maximumItems} 张。`
  return ''
}

/** The sole camelCase -> API snake_case serializer for Shot reference bindings. */
export const shotReferenceBindingsPayload = (
  bindings: ShotReferenceBindingsInput | undefined,
): JsonObject => ({
  ...(bindings?.firstFrameAssetId ? { first_frame_asset_id: bindings.firstFrameAssetId } : {}),
  ...(bindings?.lastFrameAssetId ? { last_frame_asset_id: bindings.lastFrameAssetId } : {}),
  ...(bindings?.firstFrameReferenceAssetId
    ? { first_frame_reference_asset_id: bindings.firstFrameReferenceAssetId }
    : {}),
  ...(bindings?.characters?.length ? {
    characters: bindings.characters.map((item) => ({ entity_id: item.entityId, asset_id: item.assetId })),
  } : {}),
  ...(bindings?.scene ? {
    scene: { entity_id: bindings.scene.entityId, asset_id: bindings.scene.assetId },
  } : {}),
  ...(bindings?.props?.length ? {
    props: bindings.props.map((item) => ({ entity_id: item.entityId, asset_id: item.assetId })),
  } : {}),
  ...(bindings?.videos?.length ? {
    videos: bindings.videos.map((item) => ({
      asset_id: item.assetId,
      purpose: item.purpose,
      use_embedded_audio: item.useEmbeddedAudio,
      ...(item.audioAssetId ? { audio_asset_id: item.audioAssetId } : {}),
      ...((item.useEmbeddedAudio || item.audioAssetId) && item.audioPurpose
        ? { audio_purpose: item.audioPurpose }
        : {}),
    })),
  } : {}),
  ...(bindings?.audios?.length ? {
    audios: bindings.audios.map((item) => ({ asset_id: item.assetId, purpose: item.purpose })),
  } : {}),
})
