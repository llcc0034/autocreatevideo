import type { PromptTranslationField } from '@/types'

export type BibleVariantEntityKind = 'character' | 'scene' | 'prop'
export type BibleVariantDecision = 'approve' | 'reject' | null
export type BibleAppearanceRole = 'primary' | 'variant'
export interface BibleAppearanceSource {
  binding_id: string; script_revision_id: string; entity_key: string; variant_key: string | null; declaration_sha256: string
}
export interface BibleVariantScope { project_id: string; entity_kind: BibleVariantEntityKind; entity_id: string }
export interface BibleVariantSettingInput {
  name: string
  reference_name: string
  identity_anchor: string
  appearance_anchor: string
  basis_asset_id: string | null
  basis_asset_sha256: string
}
export interface BibleVariantReview {
  id: string; decision: Exclude<BibleVariantDecision, null>; comment: string; decided_by_id: number; created_at: string
}
export interface BibleVariantRevision {
  id: string; revision: number; name: string; reference_name: string; basis_asset_id: string | null
  basis_asset_sha256: string; content_sha256: string; created_by_id: number; created_at: string; review: BibleVariantReview | null
  payload_schema_version: 1 | 2; source_declaration_sha256: string; primary_setting_revision_id: string | null
}
export interface BibleVariantSummary {
  id: string; project_id: string; character_id: string | null; scene_id: string | null; prop_id: string | null
  name: string; reference_name: string; revision: number; current_decision: BibleVariantDecision
  created_by_id: number; created_at: string; updated_at: string
  appearance_role: BibleAppearanceRole; source: BibleAppearanceSource | null
}
export type BibleSettingBody = BibleVariantRevision & Pick<BibleVariantSettingInput, 'identity_anchor' | 'appearance_anchor'>
export interface BibleVariantDetail extends BibleVariantSummary {
  setting: BibleSettingBody
  primary_setting: (BibleSettingBody & { variant_id: string }) | null
}
export interface BibleVariantPage<T> { count: number; next: string | null; previous: string | null; results: T[] }

export type BibleSourceOrigin = 'manual' | 'missing' | 'source_value' | 'configured_literal' | 'configured_default'
export interface BibleSettingSourceField {
  key: 'name' | 'reference_name' | 'identity_anchor' | 'appearance_anchor'; label: string
  declaration_value: string | null; setting_value: string; origin: BibleSourceOrigin
  source_path: string | null; changed_from_declaration: boolean | null
}
export interface BibleSettingSource {
  appearance_id: string; setting_revision_id: string; setting_sha256: string
  source: BibleAppearanceSource | null; document_sha256: string | null; fields: BibleSettingSourceField[]
}

export function sourceOriginLabel(origin: BibleSourceOrigin): string {
  return { manual: '人工登记，无初始声明', missing: '初始声明未提供', source_value: '已保存输出字段',
    configured_literal: '映射配置常量', configured_default: '字段配置默认值' }[origin]
}

export function readSettingSource(value: unknown, detail: BibleVariantDetail): BibleSettingSource {
  const row = object(value)
  if (row.appearance_id !== detail.id || row.setting_revision_id !== detail.setting.id || row.setting_sha256 !== detail.setting.content_sha256) invalid()
  if (detail.source === null ? row.source !== null || row.document_sha256 !== null
    : !row.source || !digest(row.document_sha256) || Object.keys(detail.source).some(key => row.source[key] !== detail.source![key as keyof BibleAppearanceSource])) invalid()
  const keys = ['name', 'reference_name', ...(detail.appearance_role === 'primary' ? ['identity_anchor'] : []), 'appearance_anchor']
  if (!Array.isArray(row.fields) || row.fields.length !== keys.length) invalid()
  row.fields.forEach((raw: unknown, index: number) => {
    const field = object(raw)
    if (field.key !== keys[index] || !string(field.label) || !string(field.setting_value, true)
      || field.setting_value !== detail.setting[field.key as keyof BibleSettingBody]
      || (field.declaration_value !== null && !string(field.declaration_value, true))) invalid()
    if (!['manual', 'missing', 'source_value', 'configured_literal', 'configured_default'].includes(field.origin)) invalid()
    if (['source_value', 'configured_default'].includes(field.origin) ? !string(field.source_path) : field.source_path !== null) invalid()
    if (detail.source === null) {
      if (field.origin !== 'manual' || field.declaration_value !== null || field.changed_from_declaration !== null) invalid()
    } else if (field.origin === 'manual' || (field.origin === 'missing') !== (field.declaration_value === null)
      || field.changed_from_declaration !== (field.setting_value !== (field.declaration_value ?? ''))) invalid()
  })
  return row as BibleSettingSource
}

const invalid = (): never => { throw new Error('变体设定响应不完整或归属不符，不能显示为已保存或已确认。') }
const object = (value: unknown): Record<string, any> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, any> : invalid()
const string = (value: unknown, empty = false): value is string => typeof value === 'string' && (empty || !!value)
const positive = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0
const digest = (value: unknown): boolean => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
function reviewRecord(value: unknown): BibleVariantReview | null {
  if (value === null) return null
  const row = object(value)
  if (!['approve', 'reject'].includes(row.decision) || !string(row.id) || !string(row.comment, true) || !positive(row.decided_by_id) || !string(row.created_at)) invalid()
  return row as BibleVariantReview
}
export function readVariantSummary(value: unknown): BibleVariantSummary {
  const row = object(value)
  for (const key of ['id', 'project_id', 'name', 'reference_name', 'created_at', 'updated_at']) if (!string(row[key])) invalid()
  if (!positive(row.created_by_id)) invalid()
  const ids = [row.character_id, row.scene_id, row.prop_id]
  if (ids.some(id => id !== null && !string(id)) || ids.filter(id => id !== null).length !== 1 || !positive(row.revision) || ![null, 'approve', 'reject'].includes(row.current_decision)) invalid()
  if (!['primary', 'variant'].includes(row.appearance_role)) invalid()
  if (row.source !== null) {
    const source = object(row.source)
    for (const key of ['binding_id', 'script_revision_id', 'entity_key']) if (!string(source[key])) invalid()
    if (!digest(source.declaration_sha256) || (row.appearance_role === 'primary' ? source.variant_key !== null : !string(source.variant_key))) invalid()
  }
  return row as BibleVariantSummary
}
export function readVariantRevision(value: unknown): BibleVariantRevision {
  const row = object(value)
  for (const key of ['id', 'name', 'reference_name', 'created_at']) if (!string(row[key])) invalid()
  if (!positive(row.created_by_id)) invalid()
  if (!positive(row.revision) || !digest(row.content_sha256)) invalid()
  if (![1, 2].includes(row.payload_schema_version) || (row.source_declaration_sha256 !== '' && !digest(row.source_declaration_sha256))
    || (row.primary_setting_revision_id !== null && !string(row.primary_setting_revision_id))) invalid()
  if (row.basis_asset_id === null ? row.basis_asset_sha256 !== '' : !string(row.basis_asset_id) || !digest(row.basis_asset_sha256)) invalid()
  reviewRecord(row.review)
  return row as BibleVariantRevision
}
export function readVariantDetail(value: unknown): BibleVariantDetail {
  const row = readVariantSummary(value), { setting, primary_setting } = object(value)
  readVariantRevision(setting)
  if (!string(setting.identity_anchor, true) || !string(setting.appearance_anchor, true) || setting.revision > row.revision) invalid()
  if (setting.revision === row.revision && (setting.name !== row.name || setting.reference_name !== row.reference_name || (setting.review?.decision ?? null) !== row.current_decision)) invalid()
  if (setting.primary_setting_revision_id === null) {
    if (primary_setting !== null || row.appearance_role === 'variant') invalid()
  } else {
    const primary = object(primary_setting)
    readVariantRevision(primary)
    if (row.appearance_role !== 'variant' || primary.id !== setting.primary_setting_revision_id || !string(primary.variant_id)
      || setting.identity_anchor !== ''
      || primary.variant_id === row.id || primary.primary_setting_revision_id !== null
      || !string(primary.identity_anchor, true) || !string(primary.appearance_anchor, true)) invalid()
  }
  return { ...row, setting, primary_setting }
}
export function readVariantPage<T>(value: unknown, readRow: (value: unknown) => T): BibleVariantPage<T> {
  const row = object(value)
  if (!Number.isSafeInteger(row.count) || row.count < 0 || !Array.isArray(row.results) || row.results.length > 50 || row.results.length > row.count) invalid()
  if ((row.next !== null && !string(row.next)) || (row.previous !== null && !string(row.previous))) invalid()
  return { count: row.count, next: row.next, previous: row.previous, results: row.results.map(readRow) }
}
export function requireVariantScope(row: BibleVariantSummary, scope: BibleVariantScope): BibleVariantSummary {
  if (row.project_id !== scope.project_id || row[`${scope.entity_kind}_id`] !== scope.entity_id) invalid()
  return row
}

export function emptyVariantSetting(): BibleVariantSettingInput {
  return { name: '', reference_name: '', identity_anchor: '', appearance_anchor: '', basis_asset_id: null, basis_asset_sha256: '' }
}
export function variantSettingDraft(detail: BibleVariantDetail): BibleVariantSettingInput {
  const s = detail.setting
  // Copy the viewed immutable revision, never the latest entity or list label.
  return { name: s.name, reference_name: s.reference_name, identity_anchor: s.identity_anchor,
    appearance_anchor: s.appearance_anchor, basis_asset_id: s.basis_asset_id, basis_asset_sha256: s.basis_asset_sha256 }
}
export function variantReviewPayload(detail: BibleVariantDetail, decision: Exclude<BibleVariantDecision, null>, comment: string) {
  if (detail.setting.revision !== detail.revision || detail.setting.review) throw new Error('只能审核当前未决修订；请重新读取。')
  return { expected_revision: detail.setting.revision, expected_sha256: detail.setting.content_sha256, decision, comment }
}
export function variantRevisePayload(detail: BibleVariantDetail, value: BibleVariantSettingInput) {
  if (detail.setting.revision !== detail.revision) throw new Error('历史修订只读；请先打开当前修订再修改。')
  return { ...value, expected_revision: detail.setting.revision }
}
export function variantCreatePayload(value: BibleVariantSettingInput, primary: BibleVariantDetail | null) {
  if (primary && primary.appearance_role !== 'primary') throw new Error('变体只能引用明确的主图设定，不能引用兄弟变体。')
  if (primary && value.identity_anchor) throw new Error('变体身份由主图设定提供，请只填写差异。')
  return { ...value, appearance_role: primary ? 'variant' : 'primary',
    primary_setting_revision_id: primary?.setting.id ?? null,
    primary_setting_sha256: primary?.setting.content_sha256 ?? '' }
}
export function variantDecisionLabel(value: BibleVariantDecision): string {
  switch (value) {
    case 'approve': return '设定已确认（人工）'
    case 'reject': return '设定已拒绝（人工）'
    case null: return '尚未人工核对'
    default: throw new Error('未知设定审核状态，不能显示为已确认。')
  }
}
export function appearanceRoleLabel(value: BibleAppearanceRole): string {
  if (value === 'primary') return '主图设定'
  if (value === 'variant') return '变体设定'
  throw new Error('未知设定角色，不能推定为变体。')
}
export function variantRequestGuard() {
  let epoch = 0
  return { next: () => ++epoch, current: (ticket: number) => ticket === epoch, cancel: () => { epoch++ } }
}

export function primarySettingTranslationFields(detail: BibleVariantDetail, scope: BibleVariantScope): PromptTranslationField[] {
  requireVariantScope(detail, scope)
  if (detail.appearance_role !== 'primary' || detail.setting.revision !== detail.revision) return []
  return ([['identity_anchor', '共同身份锚点'], ['appearance_anchor', '主图外观锚点']] as const)
    .map(([key, label]) => ({ scopeType: scope.entity_kind, scopeId: scope.entity_id,
      fieldKey: `${scope.entity_kind}.primary_setting.${key}`, label,
      sourceText: detail.setting[key], impact: 'continuity' as const,
      help: '来源为当前主图设定修订；译文独立审核，不回写设定正文，也不代表图片已采用。' }))
    .filter(field => field.sourceText.trim())
}
