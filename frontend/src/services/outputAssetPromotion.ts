import type { GeneratedImageAssetTarget } from '../types/index.ts'

export interface GeneratedImageAssetPromotionPayload {
  output_index: number
  name?: string
  accept_stale_bible_image_candidate?: boolean
  reference_category?: GeneratedImageAssetTarget['referenceCategory']
  entity_id?: string
}

export const buildGeneratedImageAssetPromotionPayload = (
  outputIndex: number,
  name = '',
  target?: GeneratedImageAssetTarget,
  options: { acceptStaleBibleImageCandidate?: boolean } = {},
): GeneratedImageAssetPromotionPayload => ({
  output_index: outputIndex,
  ...(name.trim() ? { name: name.trim() } : {}),
  ...(options.acceptStaleBibleImageCandidate ? { accept_stale_bible_image_candidate: true } : {}),
  ...(target ? {
    reference_category: target.referenceCategory,
    entity_id: target.entityId,
  } : {}),
})
