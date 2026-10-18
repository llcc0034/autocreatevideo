import { reactive } from 'vue'
import type { Asset } from '@/types'
import type { AssetReferenceSeries, AssetReferenceVersion } from './assetReferenceApi'
import type { JsonObject, ProductionRun } from './platformTypes'
import type { ManualActionOptions } from './productionManual'
import { inputAssetCompatible } from './referenceInputSelection'
import { isDefinitiveCreationRejection } from './productionOnboarding'
import { apiErrorSummary } from './apiErrorPresentation'
import { readExactReferencePreview } from './exactReferencePreview'

export interface ShotReferenceUse {
  id: string; shot_id: string; use_key: string; position: number; purpose: string; media_kind: 'image'|'video'|'audio'
  setting_revision_id: string|null; source_mode: ''|'existing'|'upload'|'generated'
  reference_version_id: string|null; asset_id: string|null; asset_sha256: string
}
interface ShotReferencesBase {
  run_id: string; stage_key: 'storyboard_review'; run_plan_sha256: string; shot_id: string
  uses_sha256: string
  state: {schema_version: 1; project_id: string; shot_id: string; uses: ShotReferenceUse[]}
  editability: {allowed: boolean; code: string; reason: string}
}
export interface ManualShotReferences extends ShotReferencesBase {
  storyboard_adoption_revision: number; storyboard_adoption_sha256: string|null
}
export interface RecipeReferenceCandidate {
  source: {stage_run_id: string; output_snapshot_sha256: string; field: 'material_generations'|'material_fulfillments'; reference: Record<string, unknown>}
  source_mode: 'generated'|'existing'; reference_version_id: string; version: number
  setting_revision_id: string|null; asset_id: string; asset_sha256: string; name: string; mime_type: string
}
export interface RecipeShotReferences extends ShotReferencesBase {
  source_snapshot_sha256: string
  selection_rule: {rule: {source: 'upstream'|'existing'|'upload'; source_stage_key: string|null; selection: 'manual'|'unique_setting'; allow_override: boolean};
    upstream: {stage_run_id: string; output_snapshot_sha256: string}|null}
  upstream_candidates: RecipeReferenceCandidate[]
}
export type ShotReferences = ManualShotReferences | RecipeShotReferences
export interface ReferenceChoice {
  use_id: string; source_mode: 'existing'|'upload'|'generated'; reference_version_id: string|null; asset_id: string|null; asset_sha256: string
}
export interface ReferenceSelection {asset: Asset; choice: ReferenceChoice; label: string}
export type ReferenceIdentity = {version: AssetReferenceVersion; series: AssetReferenceSeries}
export async function readSavedShotReference(api: {
  getVersion(id: string): Promise<AssetReferenceVersion>; getSeries(id: string): Promise<AssetReferenceSeries>; getAsset(id: string): Promise<Asset>
}, use: ShotReferenceUse): Promise<{asset: Asset; label: string}|null> {
  if (!use.source_mode) return null
  return readExactReferencePreview(api, {assetId: use.asset_id, assetSha256: use.asset_sha256,
    mediaKind: use.media_kind, referenceVersionId: use.reference_version_id})
}
const uuid = (v: unknown) => typeof v === 'string' && /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(v)
const sha = (v: unknown) => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v)
const object = (v: unknown): Record<string, unknown> => {
  if (!v || typeof v !== 'object' || Array.isArray(v)) throw Error('选材基线缺失，请重新读取')
  return v as Record<string, unknown>
}
function readSharedShotReferences(value: unknown, run: ProductionRun, shotId: string) {
  const raw = object(value), state = object(raw.state), edit = object(raw.editability)
  if (raw.run_id !== run.id || raw.run_plan_sha256 !== run.executionPlan?.sha256
    || !sha(raw.run_plan_sha256) || raw.stage_key !== 'storyboard_review' || !uuid(shotId) || raw.shot_id !== shotId
    || state.schema_version !== 1 || state.project_id !== run.project || state.shot_id !== shotId || !Array.isArray(state.uses)
    || !sha(raw.uses_sha256)
    || typeof edit.allowed !== 'boolean' || typeof edit.code !== 'string' || typeof edit.reason !== 'string' || !edit.reason
    || (edit.allowed && edit.code !== 'editable'))
    throw Error('选材基线与当前运行、分镜采用或可编辑状态不一致')
  const ids = new Set(), keys = new Set(), positions = new Set()
  for (const value of state.uses) {
    const use = object(value), selected = use.source_mode !== ''
    if (!uuid(use.id) || ids.has(use.id) || use.shot_id !== shotId || typeof use.use_key !== 'string' || !use.use_key || keys.has(use.use_key)
      || !Number.isSafeInteger(use.position) || Number(use.position) < 0 || positions.has(use.position)
      || typeof use.purpose !== 'string' || !['image','video','audio'].includes(String(use.media_kind))
      || (use.setting_revision_id !== null && !uuid(use.setting_revision_id))
      || !['','existing','upload','generated'].includes(String(use.source_mode))
      || (use.reference_version_id !== null && !uuid(use.reference_version_id)) || (use.asset_id !== null && !uuid(use.asset_id))
      || (selected ? !sha(use.asset_sha256) || Boolean(use.reference_version_id) === Boolean(use.asset_id)
        : use.reference_version_id !== null || use.asset_id !== null || use.asset_sha256 !== '')
      || (use.source_mode === 'generated' && !use.reference_version_id)) throw Error('镜头用途或确切素材来源不完整、重复，不能推断替代')
    ids.add(use.id); keys.add(use.use_key); positions.add(use.position)
  }
  return JSON.parse(JSON.stringify(raw)) as Record<string, unknown>
}
export function readManualShotReferences(value: unknown, run: ProductionRun, shotId: string): ManualShotReferences {
  const raw = readSharedShotReferences(value, run, shotId), edit = object(raw.editability)
  if (run.planSource !== 'manual' || !Number.isSafeInteger(raw.storyboard_adoption_revision) || Number(raw.storyboard_adoption_revision) < 0
    || (raw.storyboard_adoption_revision === 0 ? raw.storyboard_adoption_sha256 !== null : !sha(raw.storyboard_adoption_sha256))
    || (edit.allowed && raw.storyboard_adoption_revision === 0)) throw Error('人工选材缺少本轮真实分镜采用依据')
  return raw as unknown as ManualShotReferences
}
export function readRecipeShotReferences(value: unknown, run: ProductionRun, shotId: string): RecipeShotReferences {
  const raw = readSharedShotReferences(value, run, shotId), configured = object(raw.selection_rule), rule = object(configured.rule)
  if (run.planSource !== 'recipe' || !sha(raw.source_snapshot_sha256) || 'storyboard_adoption_revision' in raw || 'storyboard_adoption_sha256' in raw
    || Object.keys(rule).sort().join(',') !== 'allow_override,selection,source,source_stage_key'
    || Object.keys(configured).sort().join(',') !== 'rule,upstream' || typeof rule.allow_override !== 'boolean'
    || !['upstream','existing','upload'].includes(String(rule.source)) || !Array.isArray(raw.upstream_candidates))
    throw Error('配方选材缺少完整冻结来源，不借用人工采用或推断改选权限')
  const upstream = rule.source === 'upstream' ? object(configured.upstream) : null
  if (upstream ? rule.source_stage_key !== 'bible_image_generation' || !['manual','unique_setting'].includes(String(rule.selection))
      || !uuid(upstream.stage_run_id) || !sha(upstream.output_snapshot_sha256)
    : configured.upstream !== null || rule.source_stage_key !== null || rule.selection !== 'manual' || raw.upstream_candidates.length !== 0)
    throw Error('配方选材上游身份或选择方式与冻结来源不一致')
  const seen = new Set<string>()
  for (const value of raw.upstream_candidates) {
    const row = object(value), source = object(row.source), reference = object(source.reference)
    const identity = source.field === 'material_generations' ? reference.decision_id : reference.fulfillment_id
    const key = `${source.field}:${identity}`
    if (!upstream || source.stage_run_id !== upstream.stage_run_id || source.output_snapshot_sha256 !== upstream.output_snapshot_sha256
      || !['material_generations','material_fulfillments'].includes(String(source.field)) || !uuid(identity) || seen.has(key)
      || row.source_mode !== (source.field === 'material_generations' ? 'generated' : 'existing')
      || !uuid(row.reference_version_id) || reference.reference_version_id !== row.reference_version_id
      || !Number.isSafeInteger(row.version) || Number(row.version) < 1 || !uuid(row.asset_id) || !sha(row.asset_sha256)
      || (row.setting_revision_id !== null && !uuid(row.setting_revision_id)) || typeof row.name !== 'string' || !row.name
      || typeof row.mime_type !== 'string' || !/^(image|video|audio)\//.test(row.mime_type))
      throw Error('本轮上游候选身份、来源或素材摘要不完整，未从素材库补齐')
    seen.add(key)
  }
  return raw as unknown as RecipeShotReferences
}
export function readShotReferences(value: unknown, run: ProductionRun, shotId: string): ShotReferences {
  return run.planSource === 'recipe' ? readRecipeShotReferences(value, run, shotId) : readManualShotReferences(value, run, shotId)
}
export function recipeReferenceCandidateReason(use: ShotReferenceUse, row: RecipeReferenceCandidate): string {
  if (!row.mime_type.startsWith(`${use.media_kind}/`)) return '素材类型不符合本用途'
  if (use.setting_revision_id && row.setting_revision_id && use.setting_revision_id !== row.setting_revision_id) return '不是本用途的确切设定修订'
  return ''
}
export async function selectRecipeUpstreamReference(api: {getVersion(id: string): Promise<AssetReferenceVersion>; getAsset(id: string): Promise<Asset>},
  detail: RecipeShotReferences, use: ShotReferenceUse, candidate: RecipeReferenceCandidate): Promise<ReferenceSelection> {
  if (detail.selection_rule.rule.source !== 'upstream' || !detail.upstream_candidates.some(row => JSON.stringify(row) === JSON.stringify(candidate))
    || recipeReferenceCandidateReason(use, candidate)) throw Error('只能选择本轮冻结上游中符合本用途的确切版本')
  const version = await api.getVersion(candidate.reference_version_id)
  if (version.id !== candidate.reference_version_id || version.version !== candidate.version || version.asset.id !== candidate.asset_id
    || version.setting_revision_id !== candidate.setting_revision_id) throw Error('原上游版本已变化，不寻找替代版本')
  const asset = await api.getAsset(candidate.asset_id)
  if (asset.id !== candidate.asset_id || asset.checksum !== candidate.asset_sha256 || asset.mimeType !== candidate.mime_type
    || !inputAssetCompatible(asset, use.media_kind)) throw Error('原上游素材已失效或指纹变化，不替换文件')
  return {asset, label: `${candidate.name} · 版本 ${candidate.version}`, choice: {use_id: use.id, source_mode: candidate.source_mode,
    reference_version_id: candidate.reference_version_id, asset_id: null, asset_sha256: candidate.asset_sha256}}
}
export function shotReferenceReplacementReason(detail: ShotReferences, use: ShotReferenceUse, choice?: ReferenceChoice): string {
  if ('selection_rule' in detail && use.source_mode && !detail.selection_rule.rule.allow_override
    && (!choice || ['source_mode','reference_version_id','asset_id','asset_sha256'].some(key => use[key as keyof ShotReferenceUse] !== choice[key as keyof ReferenceChoice])))
    return '本轮冻结规则不允许替换已有选择；未选择用途仍可首次选材。'
  return ''
}
export function selectShotReference(use: ShotReferenceUse, asset: Asset, source: 'asset_library'|'asset_upload', reference?: ReferenceIdentity): ReferenceSelection {
  if (!uuid(asset.id) || !inputAssetCompatible(asset, use.media_kind) || !sha(asset.checksum)
    || !['asset_library','asset_upload'].includes(source)) throw Error('素材类型、状态或指纹无效，请重新选择')
  if (reference && (source !== 'asset_library' || !uuid(reference.version.id) || reference.version.series_id !== reference.series.id
    || reference.version.asset.id !== asset.id || !Number.isSafeInteger(reference.version.version) || reference.version.version < 1
    || (use.setting_revision_id && reference.version.setting_revision_id && reference.version.setting_revision_id !== use.setting_revision_id)))
    throw Error('所选版本与素材或本用途设定不一致，未改用直接素材')
  return {asset, choice: {use_id: use.id, source_mode: source === 'asset_upload' ? 'upload' : 'existing',
    reference_version_id: reference?.version.id ?? null, asset_id: reference ? null : asset.id, asset_sha256: asset.checksum!},
    label: reference ? `${reference.series.name} · 版本 ${reference.version.version} · ${asset.name}` : asset.name}
}
export function buildShotReferenceSelection(detail: ShotReferences, run: ProductionRun, options: ManualActionOptions|undefined,
  key: string, selection: ReferenceSelection): JsonObject {
  readShotReferences(detail, run, detail.shot_id)
  const choice = selection.choice, use = detail.state.uses.find(row => row.id === choice.use_id)
  if (!detail.editability.allowed || ['succeeded','failed','cancelled'].includes(run.status) || !use
    || !key.trim() || !['existing','upload','generated'].includes(choice.source_mode) || !sha(choice.asset_sha256)
    || choice.asset_sha256 !== selection.asset.checksum || !inputAssetCompatible(selection.asset, use.media_kind)
    || (choice.reference_version_id ? !uuid(choice.reference_version_id) || choice.asset_id !== null : choice.asset_id !== selection.asset.id))
    throw Error('当前镜头尚不能提交此用途选择，请核对状态及素材')
  if (shotReferenceReplacementReason(detail, use, choice)) throw Error(shotReferenceReplacementReason(detail, use, choice))
  if ('selection_rule' in detail) {
    const source = detail.selection_rule.rule.source
    if (source === 'upstream' ? !detail.upstream_candidates.some(row => row.reference_version_id === choice.reference_version_id
        && row.source_mode === choice.source_mode && row.asset_id === selection.asset.id && row.asset_sha256 === choice.asset_sha256
        && !recipeReferenceCandidateReason(use, row)) : choice.source_mode !== source)
      throw Error('所选素材不在本轮冻结来源范围内，不改用其他来源')
  } else if (!options || options.runId !== run.id || options.runPlanSha256 !== detail.run_plan_sha256
    || !options.actions.some(row => row.stageKey === 'storyboard_review' && row.action === 'select' && row.scopeKinds.includes('shot'))
    || choice.source_mode === 'generated') throw Error('人工选材缺少本轮冻结动作或使用了未授权来源')
  return {action: 'select', stage_key: detail.stage_key, scope: {kind: 'shot', id: detail.shot_id}, unit_key: `shot:${detail.shot_id}`,
    expected_run_plan_sha256: detail.run_plan_sha256, expected_selection_revision: 'selection_rule' in detail ? null : detail.storyboard_adoption_revision, idempotency_key: key,
    payload: {...('selection_rule' in detail ? {expected_source_snapshot_sha256: detail.source_snapshot_sha256}
      : {expected_storyboard_adoption_sha256: detail.storyboard_adoption_sha256}), expected_uses_sha256: detail.uses_sha256,
      choices: [{...choice}]} }
}

// Own only the current editor draft and original command. Saved facts remain on ShotReferenceUse.
export function createManualShotReferenceEditor(api: {
  read(runId: string, shotId: string): Promise<unknown>; submit(runId: string, request: JsonObject): Promise<JsonObject>
}, run: () => ProductionRun, options: () => ManualActionOptions|undefined, changed: () => void) {
  const state = reactive({shotId: '', detail: null as ShotReferences|null, selection: null as ReferenceSelection|null,
    pending: null as JsonObject|null, loading: false, submitting: false, conflict: false, stale: false, error: '', notice: ''})
  let serial = 0
  async function load(shotId: string) {
    if (state.submitting || state.pending) return
    const token = ++serial, context = run(); state.loading = true; state.error = ''; state.stale = true
    if (state.shotId !== shotId) { state.detail = null; state.selection = null; state.notice = '' }
    state.shotId = shotId
    try {
      const detail = readShotReferences(await api.read(context.id, shotId), context, shotId)
      if (token !== serial || run().id !== context.id) return
      state.detail = detail; state.selection = null; state.conflict = false; state.stale = false
    } catch (cause) { if (token === serial) state.error = apiErrorSummary(cause) }
    finally { if (token === serial) state.loading = false }
  }
  async function submit() {
    if (state.submitting || state.loading || (!state.pending && (!state.detail || !state.selection || state.stale || state.conflict))) return
    const token = serial, context = run(), retrying = !!state.pending
    state.submitting = true; state.error = ''; state.notice = ''
    try {
      state.pending ??= buildShotReferenceSelection(state.detail!, context, options(), crypto.randomUUID(), state.selection!)
      const result = await api.submit(context.id, JSON.parse(JSON.stringify(state.pending)))
      if (result.action !== 'select' || result.result_kind !== 'shot_reference_choices' || result.target_id !== state.shotId)
        throw Error('未返回该镜头的正式选材回执；请核对同一请求，不能另发新选择')
      if (token !== serial) return
      state.pending = null; state.selection = null; state.stale = true
      state.notice = '原选材命令已确认。下方重新读取当前值；回执不是当前文件可用、审核通过或请求已使用的证明。'
      changed()
    } catch (cause) {
      if (token === serial) {
        state.error = apiErrorSummary(cause)
        const status = (cause as {response?: {status?: number}})?.response?.status
        if (!retrying && state.pending && (isDefinitiveCreationRejection(cause) || status === 409)) {
          state.pending = null
          if (status === 409) {state.conflict = true; state.stale = true}
        }
      }
    } finally { if (token === serial) state.submitting = false }
    if (token === serial && state.notice) await load(state.shotId)
  }
  return {state, load, submit, dispose() {serial++}}
}
