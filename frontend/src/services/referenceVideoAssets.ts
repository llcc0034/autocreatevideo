import type { Asset } from '@/types'
import type { ReferenceVideoPreprocessingCapability } from './conditioningCapabilities'


export const REFERENCE_VIDEO_CONTENT_SAFETY_NOTICE = (
  '原生 Ref2VA 会读取完整视频；用途（purpose）只表达提示词意图，不是模型级内容隔离。'
  + '正式生产请选择与本剧同故事、且人物、场景、道具不冲突的素材。'
)

export const isWorkflowSmokeFixtureAsset = (asset: Asset): boolean => (
  String(asset.metadata?.fixture_scope || '').trim().toLowerCase() === 'workflow_smoke'
)

export const isProductionReferenceAsset = (asset: Asset): boolean => (
  !isWorkflowSmokeFixtureAsset(asset)
)


export const referenceVideoDurationMs = (asset: Asset): number => {
  const value = Number(asset.metadata?.duration_ms)
  return Number.isInteger(value) ? value : 0
}

export const referenceVideoSourceFps = (asset: Asset): number => {
  const value = Number(asset.metadata?.source_fps)
  return Number.isFinite(value) && value > 0 ? value : 0
}

export const referenceVideoUsableForOwner = (
  asset: Asset,
  ownerId: string,
  preprocessing?: ReferenceVideoPreprocessingCapability | null,
): boolean => {
  const durationMs = referenceVideoDurationMs(asset)
  const sourceFps = referenceVideoSourceFps(asset)
  return Boolean(
    ownerId
    && (asset.ownerId === null || asset.ownerId === ownerId)
    && asset.isActive !== false
    && isProductionReferenceAsset(asset)
    && asset.type === 'video'
    && asset.backendKind === 'reference_video'
    && asset.mimeType?.startsWith('video/')
    && /^[0-9a-f]{64}$/i.test(asset.checksum || '')
    && durationMs > 0
    && (!preprocessing || (
      durationMs >= preprocessing.minimumDurationMs
      && durationMs <= preprocessing.maximumDurationMs
      && (!preprocessing.requiresSourceFps || sourceFps > 0)
    )),
  )
}

export const referenceVideoOptionLabel = (asset: Asset): string => {
  const duration = (referenceVideoDurationMs(asset) / 1000).toFixed(1)
  const fps = referenceVideoSourceFps(asset).toFixed(2).replace(/\.00$/, '')
  return `${asset.name} · ${duration}s / ${fps}fps · ${asset.checksum?.slice(0, 10)}`
}
