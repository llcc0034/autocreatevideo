import { api } from './api'
import type { ExecutionRequestDraft } from './executionRequestForm'
import type { MediaAppearanceInputOptions } from './mediaAppearanceInputs'

export interface AssetPrimaryTarget {
  role: 'primary'; setting_revision_id: string | null; primary_version_id: null; primary_input: null
}
export type AssetDifferenceSource = { kind: 'manual_channel' | 'logical_input'; key: string }
  | { kind: 'structured_field'; key: string; field_key: string }
  | { kind: 'reference_field'; key: string; field_key: string; unit_key: string }
export interface AssetVariantTarget {
  role: 'variant'; setting_revision_id: string | null; primary_version_id: string
  primary_input: { input_key: string; item_key: string }
  difference_input: AssetDifferenceSource | null
}
export type AssetAppearanceTarget = AssetPrimaryTarget | AssetVariantTarget
export function variantTarget(): AssetVariantTarget {
  return { role: 'variant', setting_revision_id: null, primary_version_id: '', primary_input: { input_key: '', item_key: '' }, difference_input: null }
}
export function referenceUnitKeys(draft: ExecutionRequestDraft, key: string): string[] {
  const literal = draft.literals[key]
  if (!literal?.enabled || draft.contexts[key]) return []
  const units = (literal.value as { ordered_units?: unknown } | null)?.ordered_units
  if (!Array.isArray(units) || units.some(row => !row || typeof row.unit_key !== 'string' || !row.unit_key.trim())) return []
  const keys = units.map(row => row.unit_key as string)
  return new Set(keys).size === keys.length ? keys : []
}
export function appearanceTargetErrors(target: AssetAppearanceTarget | null, draft: ExecutionRequestDraft,
  options: MediaAppearanceInputOptions | null, primaryAssetId: string | null): string[] {
  if (!target || target.role === 'primary') return []
  const errors: string[] = []
  if (!uuid(target.primary_version_id) || !primaryAssetId) errors.push('请选择主图系列中的确切图片版本')
  const use = target.primary_input
  const matches = (draft.media[use.input_key] ?? []).filter(row => row.itemKey === use.item_key)
  if (!options?.primary_inputs.some(row => row.input_key === use.input_key) || matches.length !== 1
    || !primaryAssetId || matches[0]?.assetId !== primaryAssetId || matches[0]?.selectedOutput || matches[0]?.videoAudio) {
    errors.push('请选择下方原图片输入中与确切主图版本相同的素材项；不会自动替换图片')
  }
  const source = target.difference_input
  if (!source || !options?.difference_sources.some(row => row.kind === source.kind && row.key === source.key
    && (!('field_key' in row) || ('field_key' in source && row.field_key === source.field_key)))) {
    errors.push('请选择实例声明的原差异来源')
  } else if (source.kind === 'manual_channel' && !draft.channels[source.key]?.trim()) {
    errors.push('请在所选原人工通道填写差异正文')
  } else if (source.kind !== 'manual_channel') {
    if (!draft.literals[source.key]?.enabled) errors.push('请提供所选差异的原输入')
    else if (source.kind === 'reference_field' && !referenceUnitKeys(draft, source.key).includes(source.unit_key)) {
      errors.push('请选择原参考输入中的确切单元；上下文来源尚无可选单元，不猜测或补造标识')
    }
  }
  return errors
}
export interface AssetAppearanceOption {
  setting_revision_id: string; appearance_id: string; project_id: string; project_title: string
  entity_name: string; name: string; reference_name: string; revision: number
}
export interface AssetAppearancePage {
  count: number; next: string | null; previous: string | null; results: AssetAppearanceOption[]
}
const uuid = (v: unknown) => typeof v === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v)
export function primaryTarget(setting: AssetAppearanceOption | null): AssetPrimaryTarget {
  return { role: 'primary', setting_revision_id: setting?.setting_revision_id ?? null, primary_version_id: null, primary_input: null }
}
export function readAppearancePage(value: unknown): AssetAppearancePage {
  const page = value as AssetAppearancePage
  if (!page || !Number.isSafeInteger(page.count) || page.count < 0 || !Array.isArray(page.results)
    || ![page.next, page.previous].every(v => v === null || (typeof v === 'string' && !!v))
    || page.results.length > page.count || page.results.some(row => !row
      || ![row.setting_revision_id, row.appearance_id, row.project_id].every(uuid)
      || !Number.isSafeInteger(row.revision) || row.revision < 1
      || ![row.project_title, row.entity_name, row.name, row.reference_name].every(v => typeof v === 'string'))
    || new Set(page.results.map(row => row.setting_revision_id)).size !== page.results.length) {
    throw Error('外观设定目录不完整，请重试；不会按名称或默认值选择。')
  }
  return page
}
export const assetAppearanceApi = {
  async options(page: number, search: string, role: 'primary' | 'variant') {
    return readAppearancePage((await api.get('/asset-generation-jobs/appearance-options/', { params: { page, search, role } })).data)
  },
}
