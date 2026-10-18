import { api } from './api'
import type { JsonObject } from './platformTypes'
export interface FieldSmokeRule {
  field_key: string; generation_instruction: string; language_policy: string
  generation_context_selectors: string[]; max_chars: number
}
export interface FieldSmokeSource<VersionId extends string | null = string> {
  execution_instance_version_id: VersionId; definition_sha256: string; input_key: string
  profile_version_id: string; profile_contract_sha256: string; rules: FieldSmokeRule[]
  source?: { kind: 'literal'; value: JsonObject }
}
export interface FieldSmokeItem {
  field_key: string; source_text: string; source_language: string; target_language: string
  context_facts: Record<string,string>; protected_tokens: string[]
}
const invalid = (): never => { throw new Error('字段烟测来源响应不完整，请重新读取') }
export function adaptFieldSmokeSource(value: unknown, requireSource = false): FieldSmokeSource {
  const result = adaptPreparedFieldSmokeSource(value, requireSource)
  if (result.execution_instance_version_id === null) return invalid()
  return result as FieldSmokeSource
}
export function adaptResourceFieldSmokeSource(value: unknown, requireSource = false): FieldSmokeSource<null> {
  const result = adaptPreparedFieldSmokeSource(value, requireSource)
  if (result.execution_instance_version_id !== null) return invalid()
  return result as FieldSmokeSource<null>
}
function adaptPreparedFieldSmokeSource(value: unknown, requireSource: boolean): FieldSmokeSource<string | null> {
  if (!value || typeof value!=='object' || Array.isArray(value)) return invalid()
  const r=value as Record<string,unknown>
  if ((r.execution_instance_version_id !== null && (typeof r.execution_instance_version_id !== 'string' || !r.execution_instance_version_id))
    || ['input_key','profile_version_id'].some(key=>typeof r[key]!=='string'||!r[key])
    || ['definition_sha256','profile_contract_sha256'].some(key=>typeof r[key]!=='string'||!/^[a-f0-9]{64}$/.test(r[key] as string))
    || !Array.isArray(r.rules) || !r.rules.length) return invalid()
  for (const item of r.rules) {
    if (!item || typeof item!=='object' || ['field_key','generation_instruction','language_policy'].some(key=>typeof item[key]!=='string')
      || !Array.isArray(item.generation_context_selectors) || item.generation_context_selectors.some((key:unknown)=>typeof key!=='string')
      || !Number.isSafeInteger(item.max_chars) || item.max_chars<1) return invalid()
  }
  if (requireSource) {
    const s=r.source as Record<string,unknown>|undefined
    if (!s||s.kind!=='literal'||!s.value||typeof s.value!=='object'||Array.isArray(s.value)) return invalid()
  }
  return r as unknown as FieldSmokeSource<string | null>
}
const path=(id:string)=>`/model-execution-instance-versions/${encodeURIComponent(id)}/field-smoke-source/`
export const executionFieldSmokeApi = {
  async read(id:string,profileId:string) { return adaptFieldSmokeSource((await api.get(path(id),{ params:{profile_version_id:profileId} })).data) },
  async build(source:FieldSmokeSource,items:FieldSmokeItem[]) {
    return adaptFieldSmokeSource((await api.post(path(source.execution_instance_version_id),{
      profile_version_id:source.profile_version_id, expected_definition_sha256:source.definition_sha256,
      expected_profile_contract_sha256:source.profile_contract_sha256, items,
    })).data,true)
  },
}
