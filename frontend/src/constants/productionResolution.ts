import type { ProductionResolutionProfile } from '@/types'

export interface ProductionResolutionOption {
  value: ProductionResolutionProfile
  label: string
  shortHint: string
  group: 'common' | 'more'
  risk: 'low' | 'recommended' | 'standard' | 'high'
}

export const DEFAULT_PRODUCTION_RESOLUTION_PROFILE: ProductionResolutionProfile = '0.3'
export const DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE = 32
export const MIN_PRODUCTION_RESOLUTION_MULTIPLE = 8
export const MAX_PRODUCTION_RESOLUTION_MULTIPLE = 128
export const PRODUCTION_RESOLUTION_MULTIPLE_STEP = 4
export const IMAGE_PRODUCTION_RESOLUTION_MULTIPLE_STEP = 8

export const PRODUCTION_RESOLUTION_OPTIONS: readonly ProductionResolutionOption[] = [
  { value: '0.1', label: '0.1 MP', shortHint: '最省显存', group: 'common', risk: 'low' },
  { value: '0.2', label: '0.2 MP', shortHint: '低显存', group: 'common', risk: 'low' },
  { value: '0.3', label: '0.3 MP', shortHint: '低显存起点', group: 'common', risk: 'recommended' },
  { value: '0.4', label: '0.4 MP', shortHint: '标准档位', group: 'more', risk: 'standard' },
  { value: '0.5', label: '0.5 MP', shortHint: '标准档位', group: 'more', risk: 'standard' },
  { value: '0.6', label: '0.6 MP', shortHint: '显存增加', group: 'more', risk: 'standard' },
  { value: '0.7', label: '0.7 MP', shortHint: '显存增加', group: 'more', risk: 'standard' },
  { value: '0.8', label: '0.8 MP', shortHint: '高显存', group: 'more', risk: 'high' },
  { value: '0.9', label: '0.9 MP', shortHint: '高显存', group: 'more', risk: 'high' },
  { value: '0.98', label: '0.98 MP', shortHint: '高显存', group: 'more', risk: 'high' },
  { value: '1.0', label: '1.0 MP', shortHint: '极高显存 · 易 OOM', group: 'more', risk: 'high' },
  { value: '1.2', label: '1.2 MP', shortHint: '极高显存 · 易 OOM', group: 'more', risk: 'high' },
  { value: '1.5', label: '1.5 MP', shortHint: '极高显存 · 易 OOM', group: 'more', risk: 'high' },
  { value: '1.8', label: '1.8 MP', shortHint: '极高显存 · 易 OOM', group: 'more', risk: 'high' },
  { value: '2.0', label: '2.0 MP', shortHint: '极高显存 · 易 OOM', group: 'more', risk: 'high' },
  { value: 'project', label: '项目目标尺寸', shortHint: '按项目设定', group: 'more', risk: 'high' }
]

export const COMMON_PRODUCTION_RESOLUTION_OPTIONS = PRODUCTION_RESOLUTION_OPTIONS.filter((option) => option.group === 'common')
export const MORE_PRODUCTION_RESOLUTION_OPTIONS = PRODUCTION_RESOLUTION_OPTIONS.filter((option) => option.group === 'more')

export const isProductionResolutionProfile = (value: string | null | undefined): value is ProductionResolutionProfile =>
  Boolean(value && PRODUCTION_RESOLUTION_OPTIONS.some((option) => option.value === value))

const normalizeResolutionMultiple = (value: number | string | null | undefined, step: number): number => {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE
  const clamped = Math.min(MAX_PRODUCTION_RESOLUTION_MULTIPLE, Math.max(MIN_PRODUCTION_RESOLUTION_MULTIPLE, numeric))
  return Math.round(clamped / step) * step
}

export const normalizeProductionResolutionMultiple = (value: number | string | null | undefined): number =>
  normalizeResolutionMultiple(value, PRODUCTION_RESOLUTION_MULTIPLE_STEP)

export const normalizeImageProductionResolutionMultiple = (value: number | string | null | undefined): number =>
  normalizeResolutionMultiple(value, IMAGE_PRODUCTION_RESOLUTION_MULTIPLE_STEP)
