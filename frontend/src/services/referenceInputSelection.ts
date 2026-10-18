import type { Asset } from '@/types'
import type { AssetReferenceVersion } from './assetReferenceApi'
import type { MediaKind } from './inputSourcePolicies'

export function inputAssetCompatible(asset: Asset, kind: MediaKind): boolean {
  return asset.isActive !== false && /^[a-f0-9]{64}$/i.test(asset.checksum ?? '')
    && asset.mimeType?.startsWith(`${kind}/`) === true && asset.metadata?.usage !== 'project_cover'
}

// A series is a browsing route, not a mutable "latest" execution reference.
// Re-read the exact version and return its original Asset through the existing input contract.
export async function resolveReferenceInputAsset(api: {
  getVersion(id: string): Promise<AssetReferenceVersion>
  getAsset(id: string): Promise<Asset>
}, expected: AssetReferenceVersion, kind: MediaKind): Promise<Asset> {
  const version = await api.getVersion(expected.id)
  if (!version || version.id !== expected.id || version.series_id !== expected.series_id
    || version.version !== expected.version || version.asset.id !== expected.asset.id) {
    throw Error('版本关联已变化或返回不一致，请重新读取后选择；未改用其他版本')
  }
  const asset = await api.getAsset(version.asset.id)
  if (asset.id !== version.asset.id || !inputAssetCompatible(asset, kind)) {
    throw Error('此版本的原素材已失效、缺少文件摘要或类型不符合本项；未改用其他素材')
  }
  return asset
}
