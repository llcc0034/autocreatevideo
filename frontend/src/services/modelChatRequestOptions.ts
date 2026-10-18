import type { ChatRequestOptionsEditorOptions, JsonObject } from './platformTypes'

export const chatObject = (value: unknown): value is JsonObject => !!value && typeof value === 'object' && !Array.isArray(value)
const obj = (value: unknown): JsonObject => chatObject(value) ? value : {}
export const chatExtensions = (value: unknown, owned: string[]): JsonObject => Object.fromEntries(Object.entries(obj(value)).filter(([key]) => !owned.includes(key)))

export function adaptChatRequestOptions(value: unknown): ChatRequestOptionsEditorOptions {
  const raw = obj(value), choices = raw.response_format_types
  if (raw.schema_version !== 1 || typeof raw.temperature_minimum !== 'number' || !Number.isFinite(raw.temperature_minimum)
    || !Array.isArray(choices) || !choices.length || choices.some(row => !chatObject(row) || typeof row.value !== 'string' || !row.value.trim() || typeof row.label !== 'string' || !row.label.trim())
    || new Set(choices.map(row => obj(row).value)).size !== choices.length
    || typeof raw.assembly_description !== 'string' || !raw.assembly_description.trim()
    || typeof raw.boundary_description !== 'string' || !raw.boundary_description.trim()) throw new Error('文本请求选项目录不完整，请重新读取服务端目录')
  return {schemaVersion: 1, temperatureMinimum: raw.temperature_minimum,
    responseFormatTypes: choices.map(row => ({value: String(obj(row).value), label: String(obj(row).label)})),
    assemblyDescription: raw.assembly_description, boundaryDescription: raw.boundary_description}
}

export function commonChatOptionErrors(value: unknown, options?: ChatRequestOptionsEditorOptions | null): string[] {
  if (!options) return ['文本请求选项目录未读取，不能保存']
  if (!chatObject(value)) return ['请求选项须显式声明为对象，可使用空对象']
  const errors: string[] = [], temperature = value.temperature, format = value.response_format
  if (temperature !== undefined && temperature !== null && (typeof temperature !== 'number' || !Number.isFinite(temperature) || temperature < options.temperatureMinimum)) errors.push(`temperature 须为不小于 ${options.temperatureMinimum} 的有限数值，或显式 null`)
  if (format !== undefined && format !== null && (!chatObject(format) || typeof format.type !== 'string' || !format.type.trim())) errors.push('response_format 须含非空 type，或显式 null')
  if(chatObject(format)) {
    const metadata=format.json_schema
    if(Object.prototype.hasOwnProperty.call(format,'schema')||(chatObject(metadata)&&Object.prototype.hasOwnProperty.call(metadata,'schema')))errors.push('输出 Schema 只在业务输出维护，模型不能重复定义；请明确移除内联 schema')
    if(format.type!=='json_schema'&&Object.prototype.hasOwnProperty.call(format,'json_schema'))errors.push('json_schema 附加选项不适用当前格式，请明确移除')
    if(format.type==='json_schema'&&metadata!==undefined&&(!chatObject(metadata)||Object.keys(metadata).some(key=>!['name','strict','description','schema'].includes(key))))errors.push('json_schema 只配置 name、strict、description，结构由业务输出派生')
    if(chatObject(metadata)) {
      if(metadata.name!==undefined&&(typeof metadata.name!=='string'||!/^[A-Za-z0-9_-]{1,64}$/.test(metadata.name)))errors.push('json_schema.name 须为1至64位字母、数字、下划线或短横线')
      if(metadata.strict!==undefined&&typeof metadata.strict!=='boolean')errors.push('json_schema.strict 须为布尔值')
      if(metadata.description!==undefined&&typeof metadata.description!=='string')errors.push('json_schema.description 须为字符串')
    }
  }
  return errors
}

export function replaceChatExtensions(value: unknown, extras: unknown, owned: string[]): JsonObject {
  if (!chatObject(value) || !chatObject(extras)) throw new Error('扩展项必须是对象')
  if (owned.some(key => Object.prototype.hasOwnProperty.call(extras, key))) throw new Error('通用字段请在上方固定表单调整，不得在扩展项中重复配置')
  return {...Object.fromEntries(Object.entries(value).filter(([key]) => owned.includes(key))), ...extras}
}

// Operation names contain dots: never treat them as generic path segments.
export function assertChatOptionsUnchanged(before: unknown, after: unknown) {
  const project = (value: unknown) => {
    const spec = obj(value)
    return {prompt_normalization: obj(spec.prompt_normalization).chat_request_options,
      visual_rules: obj(spec.visual_rules).chat_request_options,
      operations: Object.fromEntries(Object.entries(obj(obj(spec.text_execution).operations)).filter(([, policy]) => obj(policy).chat_request_options !== undefined).map(([key, policy]) => [key, obj(policy).chat_request_options]))}
  }
  const stable = (value: unknown): string | undefined => chatObject(value) ? JSON.stringify(Object.keys(value).sort().map(key => [key, stable(value[key])])) : Array.isArray(value) ? JSON.stringify(value.map(stable)) : JSON.stringify(value)
  if (stable(project(before)) !== stable(project(after))) throw new Error('文本请求选项请在对应能力的固定表单及扩展项调整，完整 JSON 不接受重复编辑')
}
