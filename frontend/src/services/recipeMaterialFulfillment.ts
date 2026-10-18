export type MaterialFulfillmentSource = 'existing' | 'upload'
export type MaterialFulfillmentChoice = {referenceVersionId: string}
  | {seriesId: string; assetId: string; source: MaterialFulfillmentSource}
export interface MaterialFulfillmentReceipt {
  id: string; referenceVersionId: string; seriesId: string; assetId: string
  source: MaterialFulfillmentSource | 'generated'; targetSha256: string
  selectionMode: 'manual' | 'automatic'; selectedById: number | null; createdAt: string
}
export interface MaterialFulfillmentContext {
  sourceMode: MaterialFulfillmentSource | 'generate'
  stageRunId: string; unitKey: string; requirementKey: string; runPlanSha256: string
  appearanceId: string; settingRevisionId: string; appearanceRole: 'primary' | 'variant'
  referenceName: string; primaryAppearanceId: string | null
  canSelect: boolean; reason: string; selection: MaterialFulfillmentReceipt | null
}

const invalid = (): never => { throw new Error('素材履约信息缺失或与本次需求不一致，请重新读取；不会自动选择或提交模型调用') }
const object = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid()
  return value as Record<string, unknown>
}
const text = (value: unknown): string => typeof value === 'string' && value.trim() ? value : invalid()
const uuid = (value: unknown): string => typeof value === 'string' && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(value) ? value : invalid()
const sha = (value: unknown): string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value) ? value : invalid()

export function adaptMaterialFulfillmentReceipt(value: unknown): MaterialFulfillmentReceipt {
  const row = object(value)
  const validActor = row.selection_mode === 'manual'
    ? Number.isSafeInteger(row.selected_by_id) && Number(row.selected_by_id) > 0
    : row.selection_mode === 'automatic' && row.selected_by_id === null
  if ((row.source !== 'existing' && row.source !== 'upload' && row.source !== 'generated') || !validActor
      || typeof row.created_at !== 'string' || !Number.isFinite(Date.parse(row.created_at))) return invalid()
  return { id: uuid(row.id), referenceVersionId: uuid(row.reference_version_id), seriesId: uuid(row.series_id),
    assetId: uuid(row.asset_id), source: row.source, targetSha256: sha(row.target_sha256),
    selectionMode: row.selection_mode as MaterialFulfillmentReceipt['selectionMode'],
    selectedById: row.selected_by_id === null ? null : Number(row.selected_by_id), createdAt: row.created_at }
}

export function adaptMaterialFulfillmentContext(value: unknown, stageRunId: string, unitKey: string): MaterialFulfillmentContext {
  const row = object(value), appearanceId = uuid(row.appearance_id), settingRevisionId = uuid(row.setting_revision_id)
  if (!['generate','existing','upload'].includes(String(row.source_mode))) return invalid()
  if (row.stage_run_id !== stageRunId || row.unit_key !== unitKey || unitKey !== `appearance:${appearanceId}`
      || row.requirement_key !== `setting:${settingRevisionId}` || typeof row.can_select !== 'boolean' || typeof row.reason !== 'string'
      || !['primary', 'variant'].includes(String(row.appearance_role)) || row.can_select === Boolean(row.reason)) return invalid()
  const primaryAppearanceId = row.primary_appearance_id === null ? null : uuid(row.primary_appearance_id)
  if ((row.appearance_role === 'primary') !== (primaryAppearanceId === null) || primaryAppearanceId === appearanceId) return invalid()
  const selection = row.selection === null ? null : adaptMaterialFulfillmentReceipt(row.selection)
  if (selection && row.can_select) return invalid()
  return { stageRunId: uuid(row.stage_run_id), unitKey, requirementKey: text(row.requirement_key),
    sourceMode: row.source_mode as MaterialFulfillmentContext['sourceMode'],
    runPlanSha256: sha(row.run_plan_sha256), appearanceId, settingRevisionId,
    appearanceRole: row.appearance_role as 'primary' | 'variant', referenceName: text(row.reference_name),
    primaryAppearanceId, canSelect: row.can_select, reason: row.reason, selection }
}

export function materialFulfillmentPayload(context: MaterialFulfillmentContext,
    choice: MaterialFulfillmentChoice) {
  if (!context.canSelect || context.selection) return invalid()
  const requirement = {stage_run_id: context.stageRunId, unit_key: context.unitKey,
    expected_run_plan_sha256: context.runPlanSha256, requirement_key: context.requirementKey}
  if ('referenceVersionId' in choice) {
    if (Object.keys(choice).length !== 1) return invalid()
    return {...requirement, reference_version_id: uuid(choice.referenceVersionId)}
  }
  if (Object.keys(choice).length !== 3 || !['existing', 'upload'].includes(choice.source)
    || context.sourceMode !== 'generate' && choice.source !== context.sourceMode) return invalid()
  // Only the observed immutable requirement and explicit library IDs travel;
  // no setting text, actor, fake generated provenance, file, or implicit default.
  return { ...requirement,
    series_id: uuid(choice.seriesId), asset_id: uuid(choice.assetId), source: choice.source }
}
