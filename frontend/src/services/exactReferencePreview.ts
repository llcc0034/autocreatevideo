import type { Asset } from '@/types'
import type { AssetReferenceSeries, AssetReferenceVersion } from './assetReferenceApi'
import { inputAssetCompatible } from './referenceInputSelection'
import type { TaskMediaTrace } from './taskCenter'

/** Use only the frozen source identity; never infer a reference version by name. */
export function taskMediaSelection(item: TaskMediaTrace['items'][number]): ExactReferenceSelection | null {
  if (!item.asset_id || !item.source_bytes_sha256 || !['image', 'video', 'audio'].includes(item.kind)) return null
  const source = item.reference_binding.shot_reference?.source
  return {assetId: item.asset_id, assetSha256: item.source_bytes_sha256, mediaKind: item.kind,
    referenceVersionId: source?.kind === 'reference_version' ? source.reference_version_id : null,
    ...(source?.kind === 'reference_version' ? {seriesId: source.series_id, version: source.version, acquisitionSource: source.acquisition_source} : {})}
}

export interface ExactReferenceSelection {
  assetId: string | null; assetSha256: string; mediaKind: string
  referenceVersionId: string | null
  version?: number | null; seriesId?: string | null; acquisitionSource?: string | null
}
export interface ReferencePreviewApi {
  getVersion(id: string): Promise<AssetReferenceVersion>
  getSeries(id: string): Promise<AssetReferenceSeries>
  getAsset(id: string): Promise<Asset>
}
/** Metadata for the observed identity only. Never queries latest or changes a selection. */
export async function readExactReferencePreview(api: ReferencePreviewApi, selection: ExactReferenceSelection) {
  const kind = selection.mediaKind
  if (kind !== 'image' && kind !== 'video' && kind !== 'audio') throw Error('参考素材类型无效')
  const version = selection.referenceVersionId ? await api.getVersion(selection.referenceVersionId) : null
  if (selection.referenceVersionId && (!version || version.id !== selection.referenceVersionId
    || !Number.isSafeInteger(version.version) || version.version < 1
    || (selection.version != null && version.version !== selection.version)
    || (selection.seriesId != null && version.series_id !== selection.seriesId)
    || (selection.acquisitionSource != null && version.source !== selection.acquisitionSource)
    || (selection.assetId != null && version.asset.id !== selection.assetId)))
    throw Error('原素材版本、来源或素材身份已不一致，未寻找替代版本')
  const id = version?.asset.id ?? selection.assetId
  if (!id) throw Error('缺少确切素材身份，未选择默认素材')
  const asset = await api.getAsset(id)
  if (asset.id !== id || asset.checksum !== selection.assetSha256 || !inputAssetCompatible(asset, kind))
    throw Error('保存的素材已失效或文件指纹变化，未替换为当前文件')
  const series = version ? await api.getSeries(version.series_id) : null
  if (version && (!series || series.id !== version.series_id)) throw Error('系列身份不一致')
  return {asset, version, series, label: version ? `${series!.name} · 版本 ${version.version} · ${asset.name}` : asset.name}
}
