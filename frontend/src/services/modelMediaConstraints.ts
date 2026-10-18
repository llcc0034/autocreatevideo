import type { JsonObject, MediaConstraintsEditorOptions } from './platformTypes'

const record = (value: unknown): value is JsonObject => !!value && typeof value === 'object' && !Array.isArray(value)
const positive = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T

export function adaptMediaConstraintsOptions(raw: unknown): MediaConstraintsEditorOptions {
  const invalid = () => new Error('媒体约束字段目录缺失或无效，请重新读取系统设置')
  if (!record(raw) || raw.schema_version !== 1 || !Array.isArray(raw.resolution_fields) || !raw.resolution_fields.length || !Array.isArray(raw.resolution_pairs)) throw invalid()
  const bounds = (value: unknown) => {
    if (!record(value) || !positive(value.minimum) || !positive(value.maximum) || value.minimum > value.maximum) throw invalid()
    return {minimum: value.minimum, maximum: value.maximum}
  }
  const fields = raw.resolution_fields.map(value => {
    if (!record(value) || typeof value.key !== 'string' || !/^[a-z][a-z0-9_]*$/.test(value.key) || typeof value.label !== 'string' || !value.label.trim() || typeof value.unit !== 'string' || !value.unit.trim()) throw invalid()
    return {key:value.key, label:value.label, unit:value.unit, ...bounds(value)}
  })
  const keys = new Set(fields.map(field => field.key))
  if (keys.size !== fields.length) throw invalid()
  const pairs = raw.resolution_pairs.map(pair => {
    if (!Array.isArray(pair) || pair.length !== 2 || pair[0] === pair[1] || !pair.every(key => typeof key === 'string' && keys.has(key))) throw invalid()
    return pair as [string, string]
  })
  if (!record(raw.fps) || !positive(raw.fps.maximum_items)) throw invalid()
  return {schemaVersion:1, resolutionFields:fields, resolutionPairs:pairs,
    fps:{...bounds(raw.fps), maximumItems:raw.fps.maximum_items}, durationMs:bounds(raw.duration_ms)}
}

export function mediaConstraintsErrors(value: unknown, options?: MediaConstraintsEditorOptions | null): string[] {
  if (!options) return ['媒体约束字段目录未读取，不能保存模型配置']
  if (value === undefined || value === null) return []
  if (!record(value)) return ['媒体约束必须是对象；可明确清除此草稿的约束后重新填写']
  const errors: string[] = []
  const unknown = (row: JsonObject, keys: string[], label: string) => {
    for (const key of Object.keys(row)) if (!keys.includes(key)) errors.push(`${label}包含未登记字段：${key}`)
  }
  unknown(value, ['schema_version', 'resolution', 'fps', 'duration_ms'], '媒体约束')
  if (value.schema_version !== options.schemaVersion) errors.push('媒体约束结构版本无效')
  const section = (key: string): JsonObject => {
    if (value[key] === undefined) return {}
    if (!record(value[key])) {errors.push(`${key}必须是对象`);return {}}
    return value[key]
  }
  const resolution = section('resolution'), duration = section('duration_ms'), fps = section('fps')
  const within = (v:unknown, bounds:{minimum:number;maximum:number}) => Number.isSafeInteger(v) && Number(v) >= bounds.minimum && Number(v) <= bounds.maximum
  const beforeResolution = errors.length
  unknown(resolution, options.resolutionFields.map(field=>field.key), '分辨率')
  for (const field of options.resolutionFields) if (Object.prototype.hasOwnProperty.call(resolution, field.key) && !within(resolution[field.key],field)) errors.push(`${field.label}须为${field.minimum}–${field.maximum}的整数${field.unit}`)
  for (const [minimum,maximum] of options.resolutionPairs) if (typeof resolution[minimum]==='number' && typeof resolution[maximum]==='number' && resolution[minimum]>resolution[maximum]) errors.push(`${options.resolutionFields.find(field=>field.key===minimum)?.label}不能大于${options.resolutionFields.find(field=>field.key===maximum)?.label}`)
  if (errors.length === beforeResolution) {
    // The v1 media schema has two axes and monotone upper side limits. This
    // witness is only a form check, never a default or a rewrite of the draft.
    // The server repeats the authoritative check on create/edit/publish.
    const smallest = ['width','height'].map(axis => {
      const lower = Number(resolution[`minimum_${axis}`] ?? 1), multiple = Number(resolution[`${axis}_multiple`] ?? 1)
      const candidate = Math.ceil(lower / multiple) * multiple, upper = resolution[`maximum_${axis}`]
      if (typeof upper === 'number' && candidate > upper) errors.push(`${axis==='width'?'宽度':'高度'}范围内没有满足倍数的尺寸：最小可用值 ${candidate} 超过上限 ${upper}`)
      return candidate
    })
    for (const [key, candidate, label] of [
      ['maximum_short_side',Math.min(...smallest),'短边上限'], ['maximum_long_side',Math.max(...smallest),'长边上限'],
    ] as const) if (typeof resolution[key] === 'number' && candidate > resolution[key]) errors.push(`${label}与宽高范围／倍数冲突：最小可满足边长 ${candidate} 超过上限 ${resolution[key]}`)
  }
  unknown(fps, ['allowed'], '帧率')
  const allowed = fps.allowed === undefined ? [] : fps.allowed
  if (!Array.isArray(allowed)) errors.push('帧率须为数值列表')
  else {
    if (allowed.length > options.fps.maximumItems) errors.push(`帧率最多${options.fps.maximumItems}项`)
    if (allowed.some(item=>!within(item,options.fps))) errors.push(`帧率须为${options.fps.minimum}–${options.fps.maximum}的整数`)
    if (new Set(allowed).size !== allowed.length) errors.push('帧率不能重复')
  }
  unknown(duration, ['minimum','maximum'], '时长')
  if (Object.keys(duration).length) {
    for (const key of ['minimum','maximum']) if (!within(duration[key],options.durationMs)) errors.push(`最小与最大时长须同时填写${options.durationMs.minimum}–${options.durationMs.maximum}的整数毫秒`)
    if (typeof duration.minimum==='number' && typeof duration.maximum==='number' && duration.minimum>duration.maximum) errors.push('最小时长不能大于最大时长')
  }
  return [...new Set(errors)]
}

export function updateMediaConstraint(value: unknown, options: MediaConstraintsEditorOptions, group: 'resolution'|'duration_ms'|'fps', key:string, next:unknown): JsonObject {
  const allowed = group==='resolution' ? options.resolutionFields.map(field=>field.key) : group==='duration_ms' ? ['minimum','maximum'] : ['allowed']
  if (!allowed.includes(key)) throw new Error('不能写入未登记的媒体约束字段')
  if (value != null && !record(value)) throw new Error('媒体约束格式异常，请明确清除此草稿的约束后重填')
  const contract = clone(value ?? {schema_version:options.schemaVersion}) as JsonObject
  if (contract[group] !== undefined && !record(contract[group])) throw new Error('媒体约束分组格式异常，不能静默覆盖')
  const row = {...(contract[group] as JsonObject || {})}
  if (next===undefined) delete row[key]
  else row[key]=clone(next)
  contract[group]=row
  return contract
}

// A shortcut over the original four range fields, never a second stored mode.
export function fixedResolutionFields(options?: MediaConstraintsEditorOptions | null) {
  if (!options) return null
  const dimensions = ['width', 'height'] as const
  const fields = dimensions.map(dimension => {
    const minimumKey = `minimum_${dimension}`, maximumKey = `maximum_${dimension}`
    const lower = options.resolutionFields.find(field => field.key === minimumKey)
    const upper = options.resolutionFields.find(field => field.key === maximumKey)
    if (!lower || !upper || lower.unit !== upper.unit
      || !options.resolutionPairs.some(pair => pair[0] === minimumKey && pair[1] === maximumKey)) return null
    const minimum = Math.max(lower.minimum, upper.minimum), maximum = Math.min(lower.maximum, upper.maximum)
    if (minimum > maximum) return null
    return {dimension, minimumKey, maximumKey, minimum, maximum, unit:lower.unit}
  })
  if (fields.some(field => !field)) return null
  return fields as NonNullable<typeof fields[number]>[]
}

export function updateFixedResolution(value: unknown, options: MediaConstraintsEditorOptions, width: unknown, height: unknown): JsonObject {
  const fields = fixedResolutionFields(options)
  if (!fields) throw new Error('宽高成对字段目录不完整，不能设置固定宽高')
  let contract = value
  for (const field of fields) {
    const next = field.dimension === 'width' ? width : height
    if (!positive(next) || next < field.minimum || next > field.maximum) throw new Error(`固定${field.dimension === 'width' ? '宽度' : '高度'}须为${field.minimum}–${field.maximum}的整数${field.unit}`)
    contract = updateMediaConstraint(contract, options, 'resolution', field.minimumKey, next)
    contract = updateMediaConstraint(contract, options, 'resolution', field.maximumKey, next)
  }
  const errors = mediaConstraintsErrors(contract, options)
  if (errors.length) throw new Error(errors.join('；'))
  return contract as JsonObject
}

export function assertMediaConstraintsUnchanged(before:unknown, after:unknown) {
  const stable = (value:unknown):string => record(value) ? JSON.stringify(Object.keys(value).sort().map(key=>[key,stable(value[key])])) : JSON.stringify(value)
  if (stable(record(before)?before.media_constraints:undefined) !== stable(record(after)?after.media_constraints:undefined)) throw new Error('媒体约束请在“尺寸、帧率与时长”表单调整，完整 JSON 不接受重复编辑此字段')
}
