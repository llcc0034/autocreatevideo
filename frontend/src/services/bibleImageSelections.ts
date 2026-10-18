import type { Asset, BibleImageLatestCandidate } from '@/types'
import type { BibleImageFulfilledMaterial, BibleImageReviewEntity } from './productionRunReview'

export interface BibleImageReviewSelection {
  selectedArtifactIds: string[]; selectedAssetIds: string[]; anchorArtifactId: string; anchorAssetId: string
}
export const emptyBibleImageSelection = (): BibleImageReviewSelection => ({ selectedArtifactIds: [], selectedAssetIds: [], anchorArtifactId: '', anchorAssetId: '' })
export const fulfilledBiblePrimary = (entity: BibleImageReviewEntity): string => entity.fulfilledMaterials?.find(item => item.appearanceRole === 'primary')?.assetId || ''
export const initialBibleImageSelection = (entity: BibleImageReviewEntity): BibleImageReviewSelection => ({
  ...emptyBibleImageSelection(), selectedAssetIds: [...new Set(entity.fulfilledMaterials?.map(item => item.assetId) || [])],
  anchorAssetId: fulfilledBiblePrimary(entity),
})
export const verifiedBibleMaterialAsset = (assets: Asset[], material: BibleImageFulfilledMaterial): Asset | undefined => assets.find(asset =>
  asset.id === material.assetId && asset.isActive !== false && asset.checksum === material.checksum
  && asset.mimeType === material.mimeType && Boolean(asset.thumbnail || asset.mediaUrl))
export const eligibleBibleLibraryAssets = (assets: Asset[], kind: string): Asset[] => assets.filter(asset =>
  asset.type === kind && asset.isActive !== false && asset.metadata?.usage !== 'project_cover'
  && asset.mimeType?.startsWith('image/') && /^[0-9a-f]{64}$/i.test(asset.checksum || '') && Boolean(asset.thumbnail || asset.mediaUrl))
export const verifiedBibleCandidatePreview = (history: Record<string, BibleImageLatestCandidate>, id: string, hash: string): string => {
  const row = history[id]
  return row && row.sha256 === hash && row.sourceCurrent && row.integrityStatus === 'valid' ? row.previewUrl : ''
}
export function reviewedBibleImageBindings(entities: BibleImageReviewEntity[], selections: Record<string, BibleImageReviewSelection>,
  assets: Asset[], history: Record<string, BibleImageLatestCandidate>, maximum: number) {
  if (!entities.length || !Number.isSafeInteger(maximum) || maximum < 1) throw new Error('实体清单或参考图数量配置缺失')
  const used = new Set<string>()
  return entities.map(entity => {
    const row = selections[`${entity.entityType}:${entity.entityId}`] ?? emptyBibleImageSelection()
    const all = [...row.selectedArtifactIds, ...row.selectedAssetIds]
    if (!all.length || all.length > maximum || new Set(all).size !== all.length || all.some(id => used.has(id))) throw new Error(`${entity.entityName}：请选择数量有效、不重复的图片`)
    if (Number(!!row.anchorArtifactId) + Number(!!row.anchorAssetId) !== 1
      || (row.anchorArtifactId && !row.selectedArtifactIds.includes(row.anchorArtifactId))
      || (row.anchorAssetId && !row.selectedAssetIds.includes(row.anchorAssetId))) throw new Error(`${entity.entityName}：请从已选图片中指定唯一主图`)
    const materials = entity.fulfilledMaterials || []
    if (materials.some(item => !row.selectedAssetIds.includes(item.assetId))) throw new Error(`${entity.entityName}：不能移除已履约素材，改选须沿依赖变更流程处理`)
    const primary = fulfilledBiblePrimary(entity)
    if (primary && row.anchorAssetId !== primary) throw new Error(`${entity.entityName}：不能替换已履约主图或将变体设为主图`)
    for (const id of row.selectedArtifactIds) {
      const candidate = entity.candidates.find(item => item.artifactId === id)
      if (!candidate || !verifiedBibleCandidatePreview(history, id, candidate.sha256)) throw new Error(`${entity.entityName}：候选归属或预览不可用，请重新读取`)
      if (materials.some(item => item.reference.artifact_id === id)) throw new Error(`${entity.entityName}：已登记候选不能与履约素材重复提交`)
    }
    const eligible = eligibleBibleLibraryAssets(assets, entity.entityType)
    const library = row.selectedAssetIds.map(id => {
      const material = materials.find(item => item.assetId === id)
      const asset = material ? verifiedBibleMaterialAsset(assets, material) : eligible.find(item => item.id === id)
      if (!asset) throw new Error(`${entity.entityName}：素材已不可用，请重新读取`)
      return { asset_id: id, expected_sha256: asset.checksum as string }
    })
    all.forEach(id => used.add(id))
    return { entity_type: entity.entityType, entity_id: entity.entityId, selected_artifact_ids: [...row.selectedArtifactIds],
      selected_library_assets: library, anchor_artifact_id: row.anchorArtifactId || null, anchor_library_asset_id: row.anchorAssetId || null }
  })
}
