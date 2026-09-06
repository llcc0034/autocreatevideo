import type { WorkflowResolutionOptions, WorkflowResolutionSize } from './platformTypes'

/** Present server-owned resolution evidence, without a second resizing algorithm. */
export const workflowResolutionSummary = (contract: Record<string, unknown> | undefined): string => {
  const strategy = contract?.resolution_strategy
  if (!strategy || typeof strategy !== 'object' || Array.isArray(strategy)) return ''
  const value = strategy as Record<string, unknown>
  if (value.mode !== 'source_image_derived') return ''
  if (typeof value.adapter !== 'string' || !Array.isArray(value.asset_semantic_keys)
    || value.asset_semantic_keys.length !== 1 || typeof value.asset_semantic_keys[0] !== 'string') {
    return '已识别输入图尺寸模式，但尚未形成完整执行合同；请查看具体阻断项。'
  }
  const configured = Array.isArray(value.sizes) ? `在配置的 ${value.sizes.length} 组目标尺寸中按原图比例选择` : '由原节点尺寸规则推导'
  return `输出尺寸${configured}，来源为参考图片 ${value.asset_semantic_keys[0]}，无需运行时手填宽高。规则 ${value.adapter} 随工作流版本冻结；执行时读取图片真实尺寸，生成后核对产物。`
}

export const resolutionPolicyErrors = (policy: unknown, options: WorkflowResolutionOptions): string[] => {
  if (!policy || typeof policy !== 'object' || Array.isArray(policy)) return ['尺寸配置必须是对象']
  const row = policy as Record<string, unknown>
  if (Object.keys(row).sort().join(',') !== 'adapter,sizes' || row.adapter !== options.configuredAdapter) return ['尺寸配置必须引用受支持的缩放规则']
  if (!Array.isArray(row.sizes) || !row.sizes.length || row.sizes.length > options.limits.maxCandidates) return [`请选择 1–${options.limits.maxCandidates} 组目标尺寸`]
  const keys = new Set<string>()
  for (const size of row.sizes) {
    if (!size || typeof size !== 'object' || Array.isArray(size) || Object.keys(size).sort().join(',') !== 'height,width') return ['目标尺寸字段无效']
    const { width, height } = size as WorkflowResolutionSize
    const key = `${width}x${height}`
    if ([width, height].some(value => !Number.isSafeInteger(value) || value < options.limits.minDimension
      || value > options.limits.maxDimension || value % options.limits.multiple !== 0)
      || width * height > options.limits.maxPixels) return [`宽高须为 ${options.limits.minDimension}–${options.limits.maxDimension} 内 ${options.limits.multiple} 的倍数，且总像素不超过 ${options.limits.maxPixels}`]
    if (keys.has(key)) return ['目标尺寸不能重复']
    keys.add(key)
  }
  return []
}

export const adaptResolutionOptions = (value: unknown): WorkflowResolutionOptions => {
  const fail = (): never => { throw new Error('尺寸规则目录缺失或不完整，请重新读取；不能使用本地默认尺寸') }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fail()
  const raw = value as Record<string, any>
  const limits = raw.limits || {}
  const result: WorkflowResolutionOptions = {
    nativeAdapter: raw.native_adapter, configuredAdapter: raw.configured_adapter, algorithm: raw.algorithm,
    modes: raw.modes, nativeSizes: raw.native_sizes, presets: raw.presets,
    limits: { minDimension: limits.min_dimension, maxDimension: limits.max_dimension, multiple: limits.multiple,
      maxPixels: limits.max_pixels, maxCandidates: limits.max_candidates },
  }
  if (result.nativeAdapter !== 'comfy.flux_kontext_image_scale/v1' || result.configuredAdapter !== 'comfy.image_scale_candidates/v1'
    || typeof result.algorithm !== 'string' || !result.algorithm
    || Object.values(result.limits).some(v => !Number.isSafeInteger(v) || v < 1)
    || result.limits.maxDimension < result.limits.minDimension
    || !Array.isArray(result.modes) || result.modes.length !== 2
    || result.modes.map(row => row.value).sort().join(',') !== 'configured,native'
    || result.modes.some(row => typeof row.label !== 'string' || !row.label)
    || !Array.isArray(result.nativeSizes) || !Array.isArray(result.presets) || !result.presets.length) return fail()
  if (resolutionPolicyErrors({adapter: result.configuredAdapter, sizes: result.nativeSizes}, result).length
    || resolutionPolicyErrors({adapter: result.configuredAdapter, sizes: result.presets.map(row => ({width: row.width, height: row.height}))}, result).length
    || result.presets.some(row => typeof row.label !== 'string' || !row.label)) return fail()
  return result
}
