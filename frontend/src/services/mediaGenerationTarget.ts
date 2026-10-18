import type { JsonObject } from './platformTypes'

export interface MediaTargetSetting {
  appearanceId: string; revisionId: string; revision: number; sha256: string
}
export type MediaTargetDifference = { key: string; sha256: string } & (
  { kind: 'manual_channel' | 'logical_input' } | { kind: 'reference_field'; unitKey: string; fieldKey: string }
  | { kind: 'structured_field'; fieldKey: string })
export interface MediaGenerationTarget {
  mediaType: 'image' | 'video' | 'audio'
  versionId: string; definitionSha256: string; inputsSha256: string
  candidates: { count: number; evaluator: null | 'first_succeeded' }
  setting: MediaTargetSetting | null
  appearance: null | {
    role: 'primary' | 'variant'
    primary: null | { versionId: string; seriesId: string; assetId: string; sha256: string }
    primaryInput: null | { inputKey: string; itemKey: string }
    difference: MediaTargetDifference | null
  }
}
export type MediaGenerationTargetState = { kind: 'missing' } | { kind: 'invalid'; message: string }
  | { kind: 'recorded'; target: MediaGenerationTarget }

const invalid = () => new Error('生成目标记录缺失、格式异常或与本次实例版本不一致，请重新读取；不能推断生成目标。')
const has = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key)
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw invalid()
  return value as Record<string, unknown>
}
function string(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) throw invalid()
  return value
}
function id(value: unknown): string {
  const result = string(value)
  if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(result)) throw invalid()
  return result
}
function digest(value: unknown): string {
  const result = string(value)
  if (!/^[0-9a-f]{64}$/.test(result)) throw invalid()
  return result
}
function positive(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) throw invalid()
  return value
}
function sourceKey(value: unknown): string {
  const result = string(value)
  if (result.length > 191) throw invalid()
  return result
}
function differenceSource(value: unknown): MediaTargetDifference {
  const data = record(value), reference = data.kind === 'reference_field', structured = data.kind === 'structured_field'
  if ((!reference && !structured && data.kind !== 'manual_channel' && data.kind !== 'logical_input')
    || Object.keys(data).sort().join(',') !== (reference ? 'field_key,key,kind,unit_key,value_sha256'
      : structured ? 'field_key,key,kind,value_sha256' : 'key,kind,value_sha256')) throw invalid()
  const common = { key: sourceKey(data.key), sha256: digest(data.value_sha256) }
  return reference ? { ...common, kind: 'reference_field', unitKey: sourceKey(data.unit_key), fieldKey: sourceKey(data.field_key) }
    : structured ? { ...common, kind: 'structured_field', fieldKey: sourceKey(data.field_key) }
    : { ...common, kind: data.kind as 'manual_channel' | 'logical_input' }
}
function setting(value: unknown): MediaTargetSetting | null {
  if (value === null) return null
  const data = record(value)
  return { appearanceId: id(data.appearance_id), revisionId: id(data.setting_revision_id),
    revision: positive(data.revision), sha256: digest(data.content_sha256) }
}

// Presentation only. The original detail API verifies the immutable contract;
// never rebuild a target from current settings, Asset metadata or UI defaults.
export function readMediaGenerationTarget(resolvedInputs: JsonObject, versionId: string | null): MediaGenerationTargetState {
  if (!has(resolvedInputs, 'media_generation_target')) return { kind: 'missing' }
  try {
    const data = record(resolvedInputs.media_generation_target)
    if (data.schema_version !== 1 || data.source_mode !== 'generate' || typeof data.media_type !== 'string'
      || !['image', 'video', 'audio'].includes(data.media_type)) throw invalid()
    const targetVersionId = id(data.execution_instance_version_id)
    if (targetVersionId !== versionId) throw invalid()
    const candidates = record(data.candidates), evaluator = candidates.evaluator
    if (evaluator !== null && evaluator !== 'first_succeeded') throw invalid()
    const target: MediaGenerationTarget = {
      mediaType: data.media_type as MediaGenerationTarget['mediaType'], versionId: targetVersionId,
      definitionSha256: digest(data.definition_sha256), inputsSha256: digest(data.inputs_sha256),
      candidates: { count: positive(candidates.count), evaluator }, setting: setting(data.setting), appearance: null,
    }
    if (has(data, 'appearance')) {
      const value = record(data.appearance)
      if (target.mediaType !== 'image' || (value.role !== 'primary' && value.role !== 'variant')
        || JSON.stringify(setting(value.setting)) !== JSON.stringify(target.setting)) throw invalid()
      if (value.role === 'primary') {
        if (value.primary !== null || value.primary_input !== null || has(value, 'difference')) throw invalid()
        target.appearance = { role: 'primary', primary: null, primaryInput: null, difference: null }
      } else {
        const primary = record(value.primary), input = record(value.primary_input)
        target.appearance = { role: 'variant',
          primary: { versionId: id(primary.version_id), seriesId: id(primary.series_id),
            assetId: id(primary.asset_id), sha256: digest(primary.sha256) },
          primaryInput: { inputKey: string(input.input_key), itemKey: string(input.item_key) },
          difference: differenceSource(value.difference) }
      }
    }
    return { kind: 'recorded', target }
  } catch { return { kind: 'invalid', message: invalid().message } }
}
