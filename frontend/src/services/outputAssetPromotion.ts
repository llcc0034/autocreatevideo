import type { GeneratedImageAssetTarget } from '../types/index.ts'

export interface GeneratedImageAssetPromotionPayload {
  output_index: number
  name?: string
  reference_category?: GeneratedImageAssetTarget['referenceCategory']
  entity_id?: string
}

export const buildGeneratedImageAssetPromotionPayload = (
  outputIndex: number,
  name = '',
  target?: GeneratedImageAssetTarget,
): GeneratedImageAssetPromotionPayload => ({
  output_index: outputIndex,
  ...(name.trim() ? { name: name.trim() } : {}),
  ...(target ? {
    reference_category: target.referenceCategory,
    entity_id: target.entityId,
  } : {}),
})
