import type { ModelProfileRegistryChoice } from './platformTypes'

// Presentation labels only. Limits, defaults and supported modes always come from the version.
const fieldLabels: Record<string, string> = {
  schema_version: '合同结构版本', model_family: '模型族', capabilities: '声明能力',
  conditioning_contract: '素材能力合同', media_constraints: '媒体规格限制',
  prompt_normalization: '提示词标准化容量', visual_rules: '影视规则生成容量',
  series_concept: '剧集概念合同', script_structure: '剧本结构化合同',
  execution_monitoring: '执行监控', async_media: '异步媒体合同',
  key: '标识', label: '名称', description: '说明', adapter_key: '解释器',
  input_kind: '素材形态', inputs: '素材数量', modes: '参考模式',
  requires_entity_reference_bible: '需要实体参考素材库', supports_native_audio: '支持原生音频输出',
  total_images: '图片总数', first_frame: '首帧图片', last_frame: '尾帧图片', entity_images: '参考图片',
  reference_videos: '参考视频', paired_video_audios: '视频配对音频', reference_audios: '参考音频',
  minimum_items: '最少数量', maximum_items: '最多数量',
  dynamic_reference_topology: '用途、预处理与配对规则', videos: '视频', audios: '音频',
  purposes: '允许用途', default_purpose_key: '默认用途标识', preprocessing: '预处理',
  pairing_rules: '配对规则', mutual_exclusion_rules: '互斥规则',
  source_media_kind: '来源媒体类型', target_media_kind: '目标媒体类型', media_kind: '媒体类型',
  source_purpose_keys: '来源用途标识', target_purpose_keys: '目标用途标识', purpose_keys: '用途标识',
  default_target_purpose_key: '默认目标用途标识', minimum_targets_per_source: '每份来源最少配对数',
  maximum_targets_per_source: '每份来源最多配对数', maximum_distinct_purposes: '最多不同用途数',
  allow_embedded_source: '允许使用视频内嵌音频', allow_external_target: '允许外部配对素材',
  source_and_external_mutually_exclusive: '内嵌与外部来源互斥', requires_source_fps: '要求来源帧率',
  minimum_duration_ms: '最短时长（毫秒）', maximum_duration_ms: '最长时长（毫秒）', target_fps: '目标帧率（FPS）',
  resolution: '分辨率', minimum_width: '最小宽度（像素）', maximum_width: '最大宽度（像素）',
  minimum_height: '最小高度（像素）', maximum_height: '最大高度（像素）',
  width_multiple: '宽度倍数（像素）', height_multiple: '高度倍数（像素）',
  maximum_short_side: '短边上限（像素）', maximum_long_side: '长边上限（像素）',
  fps: '帧率（FPS）', duration_ms: '时长（毫秒）', allowed: '允许值', minimum: '最小值', maximum: '最大值',
  default_idle_timeout_seconds: '默认静默时限（秒）', node_type_overrides: '特定节点等待时限（秒）',
  max_input_bytes: '最大输入（字节）', input_byte_warning_threshold: '输入建议阈值（字节）',
  max_output_tokens: '最大输出（Token）', output_token_warning_threshold: '输出建议阈值（Token）',
  max_items_per_batch: '每批最多字段数', timeout_seconds: '请求超时（秒）',
  chat_request_options: '受控请求选项', quality_advisories: '质量建议',
  smoke_inputs: '烟测输入声明', request_schema: '请求结构',
}

export const capabilityFieldLabel = (key: string, choices?: readonly ModelProfileRegistryChoice[]): string =>
  choices?.find(choice => choice.value === key)?.label || (Object.prototype.hasOwnProperty.call(fieldLabels, key) ? fieldLabels[key] : key) || key

export const isContractObject = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)

export const contractFieldsExcept = (value: unknown, fields: readonly string[]): Record<string, unknown> =>
  isContractObject(value) ? Object.fromEntries(Object.entries(value).filter(([key]) => !fields.includes(key))) : {}

export function contractScalar(value: unknown): string {
  if (value === undefined) return '未声明'
  if (value === null) return '空值（null）'
  if (value === true) return '是'
  if (value === false) return '否'
  if (value === '') return '空字符串'
  return String(value)
}

export function quantityDeclaration(value: unknown, total = false): string {
  if (!isContractObject(value)) return value === undefined ? '未声明' : '数量声明格式异常'
  const minimum = value.minimum_items
  const maximum = value.maximum_items
  if (minimum === undefined || maximum === undefined) return '数量声明不完整'
  if (typeof minimum !== 'number' || typeof maximum !== 'number'
    || !Number.isSafeInteger(minimum) || !Number.isSafeInteger(maximum) || minimum < 0 || minimum > maximum) return '数量声明无效'
  if (maximum === 0) return '不支持'
  if (total) return '合计约束（不是独立素材槽）'
  if (minimum === maximum) return '固定数量'
  return minimum === 0 ? '可选 · 动态数量' : '动态数量'
}
