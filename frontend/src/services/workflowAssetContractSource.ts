import type { JsonObject, ModelProfileVersion, WorkflowProjectorOptions } from './platformTypes'
import { isJsonObject } from './workflowProjectorDefinition'

export const assetGroupForSlot = (definition: JsonObject, key: unknown): JsonObject | null => {
  const projection = isJsonObject(definition.projection) ? definition.projection : {}
  const config = isJsonObject(projection.config) ? projection.config : {}
  if (projection.kind === 'generic_classified' && isJsonObject(config.dynamic_groups)) {
    const row = config.dynamic_groups[String(key)]
    return isJsonObject(row) ? { ...row, key: String(key), conditioning_mode: config.conditioning_mode, model_input: row.mode_input, target_roles: [config.target_role], configuration_path: 'projection.config.dynamic_groups', compiled_field: 'classified_references' } : null
  }
  const matches = Array.isArray(config.groups)
    ? config.groups.filter((row) => isJsonObject(row) && row.key === key) : []
  return matches.length === 1 ? matches[0] as JsonObject : null
}

/** Preview an exact registered version, never infer a model binding or count. */
export const modelAssetQuantitySource = (profile: ModelProfileVersion | null, group: JsonObject | null) => {
  const raw = profile?.specification.conditioning_contract
  const contract = isJsonObject(raw) ? raw : null
  const modes = contract && Array.isArray(contract.modes) ? contract.modes.filter(isJsonObject) : []
  const matches = modes.filter((row) => row.key === String(group?.conditioning_mode || '').trim())
  const mode = matches.length === 1 ? matches[0]! : null
  const inputs = mode && isJsonObject(mode.inputs) ? mode.inputs : {}
  const rawInput = inputs[String(group?.model_input || '')]
  const input = isJsonObject(rawInput) ? rawInput : null
  let message = ''
  if (!profile) message = '尚未读取精确模型版本，不能展示或推断数量范围。'
  else if (!contract) message = '该模型版本未声明 conditioning_contract，不能作为本组的模型数量来源。'
  else if (!group?.conditioning_mode || !group?.model_input) message = '本组尚未填写模式和输入 Key。下方为该版本实际声明，查看不会自动应用。'
  else if (!mode) message = `该模型版本未唯一声明模式 ${String(group.conditioning_mode)}。`
  else if (!input) message = `模式未声明数量输入 ${String(group?.model_input)}。`
  else if (!Number.isSafeInteger(input.minimum_items) || !Number.isSafeInteger(input.maximum_items)
    || Number(input.minimum_items) < 0 || Number(input.maximum_items) < Number(input.minimum_items)) {
    message = '所选输入缺少有效的 minimum_items / maximum_items，不能作为数量来源。'
  }
  return { contract, modes, mode, input, message }
}

/** Select declared, usable quantities only; this is not model binding or admission. */
export const modelQuantityChoices = (profile: ModelProfileVersion | null, mediaKind: unknown,
  catalog: WorkflowProjectorOptions['definitionEditor']['choices']['modelQuantityInputs'] | undefined) => {
  const choices: Array<{ value: string; modeKey: string; inputKey: string; label: string }> = []
  if (!catalog?.length || new Set(catalog.map(row => row.value)).size !== catalog.length
    || catalog.some(row => !row.value || !row.label || !['image', 'video', 'audio'].includes(row.mediaKind))) {
    return { choices, message: '平台数量输入目录缺失或无效，请重新读取目录；不能手填未知 Key。' }
  }
  if (!profile) return { choices, message: '请先读取精确模型版本。' }
  const raw = profile.specification.conditioning_contract
  if (!isJsonObject(raw) || !Array.isArray(raw.modes) || !raw.modes.length) return { choices, message: '所选模型没有声明数量模式。' }
  const modes = raw.modes
  if (modes.some(mode => !isJsonObject(mode) || typeof mode.key !== 'string' || !mode.key.trim())
    || new Set(modes.map(mode => (mode as JsonObject).key)).size !== modes.length) {
    return { choices, message: '模型模式标识缺失或重复，不能唯一定位数量声明。' }
  }
  for (const mode of modes as JsonObject[]) {
    if (!isJsonObject(mode.inputs)) continue
    for (const entry of catalog.filter(row => row.mediaKind === mediaKind)) {
      const quantity = mode.inputs[entry.value]
      if (!isJsonObject(quantity) || !Number.isSafeInteger(quantity.minimum_items) || !Number.isSafeInteger(quantity.maximum_items)
        || Number(quantity.minimum_items) < 0 || Number(quantity.maximum_items) < Math.max(1, Number(quantity.minimum_items))) continue
      choices.push({ value: JSON.stringify([mode.key, entry.value]), modeKey: String(mode.key), inputKey: entry.value,
        label: `${String(mode.label || mode.key)} · ${String(mode.key)} / ${entry.label} (${entry.value}) · ${quantity.minimum_items}–${quantity.maximum_items} 份` })
    }
  }
  return { choices, message: choices.length ? '' : '没有与本组媒体类型匹配的可用数量输入；0–0、无效范围及未注册输入不能选择。' }
}

/** An unsaved interval comparison, never an execution/port validation result. */
export const classifiedQuantityComparison = (profile: ModelProfileVersion | null, group: JsonObject | null, slot: JsonObject | undefined) => {
  const source = modelAssetQuantitySource(profile, group)
  const range = (value: unknown): { minimum: number; maximum: number } | null => {
    if (!isJsonObject(value) || !Number.isSafeInteger(value.minimum_items) || !Number.isSafeInteger(value.maximum_items)
      || Number(value.minimum_items) < 0 || Number(value.maximum_items) < Number(value.minimum_items)) return null
    return { minimum: Number(value.minimum_items), maximum: Number(value.maximum_items) }
  }
  const model = range(source.input)
  const inputs = source.mode && isJsonObject(source.mode.inputs) ? source.mode.inputs : {}
  const total = group?.model_input === 'entity_images' ? range(inputs.total_images) : null
  const configured = slot?.contract_source === 'model_profile' ? model : slot?.contract_source === 'workflow' ? range(group) : null
  let message = source.message
  if (!message && source.mode?.input_kind !== 'classified_references') message = '所选模型模式不支持分类参考素材。'
  if (!message && !configured) message = '当前数量来源或范围无效，不能比较。'
  if (!message && group?.model_input === 'entity_images' && !total) message = '模型缺少有效的 total_images 总量合同。'
  for (const [name, bound] of [['模型输入', model], ['total_images 图片总量', total]] as const) {
    if (!message && configured && bound && (configured.minimum < bound.minimum || configured.maximum > bound.maximum)) {
      message = `配置范围 ${configured.minimum}–${configured.maximum} 超出${name} ${bound.minimum}–${bound.maximum}；请显式收窄 Projector，模型声明有误则创建修正版本。`
    }
  }
  return { state: !profile ? 'none' : message ? 'conflict' : 'matched', message, model, total, configured }
}
