import type { JsonObject } from './platformTypes'
import {readStoryboardReviewDisplay, type StoryboardReviewDisplay} from './storyboardReviewDisplay'

export interface RecipeReferenceReviewUse {
  id: string; key: string; position: number; mediaKind: string; purpose: string
  sourceMode: string; assetId: string; assetSha256: string; referenceVersionId: string | null; version: number | null
  seriesId: string | null; acquisitionSource: string | null
}
export interface RecipeReferenceReview {
  display: StoryboardReviewDisplay
  sourceSnapshotSha256: string; selectionSha256: string
  shots: Array<{ id: string; uses: RecipeReferenceReviewUse[] }>
}
const record = (value: unknown): JsonObject => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('分镜参考审核返回缺失或格式错误。')
  return value as JsonObject
}
const id = (value: unknown): string => {
  if (typeof value !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) throw new Error('确切素材或用途身份缺失。')
  return value
}
const hash = (value: unknown): string => {
  if (typeof value !== 'string' || !/^[0-9a-f]{64}$/.test(value)) throw new Error('分镜来源或素材选择摘要缺失。')
  return value
}
/** Display only: preserves independent authored uses; no assembly or selection. */
export function recipeStoryboardReferenceReview(value: unknown, projectId: string): RecipeReferenceReview {
  const response = record(value), selection = record(response.selection)
  if (selection.schema_version !== 1 || selection.project_id !== projectId || !Array.isArray(selection.shots)
      || !selection.shots.length || response.shot_count !== selection.shots.length) throw new Error('审核范围与当前项目或镜头数量不一致。')
  const shotIds = new Set<string>(), useIds = new Set<string>()
  return {display: readStoryboardReviewDisplay(response.display, selection.shots.map(value => id(record(value).shot_id))),
    sourceSnapshotSha256: hash(response.source_snapshot_sha256), selectionSha256: hash(response.selection_sha256),
    shots: selection.shots.map(value => {
      const shot = record(value), shotId = id(shot.shot_id), keys = new Set<string>()
      if (shotIds.has(shotId) || !Array.isArray(shot.uses)) throw new Error('分镜镜头重复或用途明细缺失。')
      shotIds.add(shotId)
      let previous = -1
      return {id: shotId, uses: shot.uses.map(value => {
        const use = record(value), source = record(use.source), asset = record(use.asset), useId = id(use.use_id)
        if (useIds.has(useId) || typeof use.use_key !== 'string' || !use.use_key || keys.has(use.use_key)
            || !Number.isSafeInteger(use.position) || Number(use.position) <= previous || typeof use.purpose !== 'string'
            || !['image','video','audio'].includes(String(use.media_kind))
            || !['existing','upload','generated'].includes(String(use.source_mode))) throw new Error('逐镜用途身份、顺序或确切来源不完整。')
        useIds.add(useId); keys.add(use.use_key); previous = Number(use.position)
        const assetId = id(asset.asset_id), versionId = source.kind === 'reference_version' ? id(source.reference_version_id) : null
        if (versionId ? !Number.isSafeInteger(source.version) || Number(source.version) < 1
            || !['existing','upload','generated'].includes(String(source.acquisition_source))
            : source.kind !== 'asset' || source.asset_id !== assetId) throw new Error('素材版本或直接素材来源不一致。')
        return {id: useId, key: use.use_key, position: Number(use.position), mediaKind: String(use.media_kind), purpose: use.purpose,
          sourceMode: String(use.source_mode), assetId, assetSha256: hash(asset.sha256), referenceVersionId: versionId,
          version: versionId ? Number(source.version) : null, seriesId: versionId ? id(source.series_id) : null,
          acquisitionSource: versionId ? String(source.acquisition_source) : null}
      })}
    })}
}
