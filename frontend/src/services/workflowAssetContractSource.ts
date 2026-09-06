import type { JsonObject, ModelProfileVersion } from './platformTypes'
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
