import type { ConditioningContractEditorOptions, JsonObject } from './platformTypes'
export const contractObject = (value: unknown): JsonObject => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}
export const contractRows = (value: unknown): JsonObject[] => Array.isArray(value) ? value.map(contractObject) : []
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T

export function newConditioningMode(options: ConditioningContractEditorOptions, kind: string, existing: JsonObject[]): JsonObject {
  const choice = options.inputKinds.find(row => row.value === kind)
  if (!choice) throw new Error('请选择服务端支持的素材模式')
  const mode = clone(options.modeTemplate)
  let index = 1
  while (existing.some(row => row.key === `mode_${index}`)) index++
  return { ...mode, key: `mode_${index}`, label: choice.label, input_kind: kind }
}

export function updateConditioningQuantity(mode: JsonObject, slot: string, field: string, value: number): JsonObject {
  const next = clone(mode)
  const inputs = contractObject(next.inputs)
  next.inputs = { ...inputs, [slot]: { ...contractObject(inputs[slot]), [field]: value } }
  // Topology group cardinality is a mirror required by the frozen schema, not another authoring source.
  const topology = contractObject(next.dynamic_reference_topology)
  const group = slot === 'reference_videos' ? 'videos' : slot === 'reference_audios' ? 'audios' : ''
  if (group && topology[group]) topology[group] = { ...contractObject(topology[group]), [field]: value }
  return next
}

export function conditioningContractErrors(value: unknown, options?: ConditioningContractEditorOptions | null): string[] {
  if (value === undefined || value === null) return []
  if (!options) return ['模型素材合同目录尚未读取，不能编辑或保存这份合同']
  const contract = contractObject(value)
  const modes = contractRows(contract.modes)
  const errors: string[] = []
  if (contract.schema_version !== options.schemaVersion || contract.adapter_key !== options.adapterKey || !modes.length || modes.length > options.maximumModes) errors.push('素材合同版本、解释器或模式数量无效')
  const keys = new Set<string>()
  modes.forEach((mode, index) => {
    const label = `素材模式 ${index + 1}`
    const key = String(mode.key || '')
    if (!new RegExp(options.identifierPattern).test(key) || keys.has(key) || !String(mode.label || '').trim()) errors.push(`${label}需要唯一标识和名称`)
    keys.add(key)
    if (!options.inputKinds.some(row => row.value === mode.input_kind)) errors.push(`${label}的输入类型未注册`)
    const inputs = contractObject(mode.inputs)
    for (const slot of options.inputSlots) {
      const bounds = contractObject(inputs[slot.value])
      if (!Number.isSafeInteger(bounds.minimum_items) || !Number.isSafeInteger(bounds.maximum_items) || Number(bounds.minimum_items) < 0 || Number(bounds.minimum_items) > Number(bounds.maximum_items) || Number(bounds.maximum_items) > options.maximumItems) errors.push(`${label}：${slot.label}数量应为 0–${options.maximumItems} 内的整数，且最小值不大于最大值`)
    }
    const maximum = (slot: string) => Number(contractObject(inputs[slot]).maximum_items || 0)
    if (mode.input_kind === 'text_only' && options.inputSlots.some(slot => maximum(slot.value) > 0)) errors.push(`${label}为纯文本，不应启用素材槽`)
    if (mode.input_kind === 'frame_slots' && ['entity_images','reference_videos','paired_video_audios','reference_audios'].some(slot => maximum(slot) > 0)) errors.push(`${label}首尾帧模式不能配置其他参考素材`)
    if (maximum('total_images') > maximum('first_frame') + maximum('last_frame') + maximum('entity_images')) errors.push(`${label}图片总数上限超过各图片槽上限之和`)
    if (maximum('paired_video_audios') > maximum('reference_videos')) errors.push(`${label}配对音频上限不能超过参考视频上限`)
    if (mode.requires_entity_reference_bible && !maximum('entity_images')) errors.push(`${label}需要实体参考素材时必须启用参考图片槽`)
    if (['reference_videos','paired_video_audios','reference_audios'].some(slot => maximum(slot) > 0) && !mode.dynamic_reference_topology) errors.push(`${label}启用了视频／音频，必须配置预处理与配对规则；可先从已有完整合同复制`)
  })
  return errors
}
