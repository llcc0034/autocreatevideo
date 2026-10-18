import { reactive } from 'vue'
import type { ProductionRun, StageRun } from './platformTypes'
import type { Asset } from '../types'
import type { AssetReferenceSeries, AssetReferenceVersion, ReferenceSeriesPage, ReferenceSeriesQuery, ReferenceVersionPage } from './assetReferenceApi'
import type { MaterialFulfillmentContext, MaterialFulfillmentReceipt, MaterialFulfillmentSource, MaterialFulfillmentChoice } from './recipeMaterialFulfillment'
import { materialFulfillmentPayload } from './recipeMaterialFulfillment'
import { inputAssetCompatible, resolveReferenceInputAsset } from './referenceInputSelection'
import { apiErrorSummary } from './apiErrorPresentation'
import { isDefinitiveCreationRejection } from './productionOnboarding'

export interface MaterialRequirementOption { unitKey: string; requirementKey: string; name: string; role: 'primary' | 'variant' }
const record = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('本轮素材需求不完整，请刷新运行；不会从当前 Bible 猜测需求')
  return value as Record<string, unknown>
}
const identity = (value: unknown): string => {
  if (typeof value !== 'string' || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(value)) throw new Error('本轮素材需求身份无效')
  return value
}
// This is a view of the original frozen Stage snapshot, never today's Bible.
export function materialRequirementOptions(run: ProductionRun, stage: StageRun): MaterialRequirementOption[] | null {
  if (run.planSource !== 'recipe' || stage.stageKey !== 'bible_image_generation') return null
  if (!stage.inputSnapshotSha256) throw new Error('本步骤尚未冻结素材需求，请等待上游与阶段准备完成后刷新运行')
  const input = record(stage.inputSnapshot), requirements = record(input.material_requirements)
  if (!/^[a-f0-9]{64}$/.test(run.executionPlan?.sha256 || '') || requirements.run_plan_sha256 !== run.executionPlan?.sha256 || !Array.isArray(requirements.targets) || !Array.isArray(input.units)
    || requirements.targets.length !== input.units.length) throw new Error('素材需求与本轮冻结计划不一致，请刷新运行')
  const targets = requirements.targets.map(record), seen = new Set<string>()
  return input.units.map(value => {
    const unit = record(value), matches = targets.filter(row => row.requirement_key === unit.requirement_key)
    if (matches.length !== 1) throw new Error('素材需求不是唯一目标')
    const target = matches[0]!, setting = record(target.setting)
    const unitKey = `appearance:${identity(setting.appearance_id)}`, requirementKey = `setting:${identity(setting.setting_revision_id)}`
    if (unit.unit_key !== unitKey || unit.requirement_key !== requirementKey || seen.has(unitKey)
      || !['primary','variant'].includes(String(unit.appearance_role)) || unit.appearance_role !== target.appearance_role
      || typeof target.reference_name !== 'string' || !target.reference_name.trim()) throw new Error('素材单元与确切设定需求不一致')
    seen.add(unitKey)
    return { unitKey, requirementKey, name: target.reference_name, role: unit.appearance_role as 'primary' | 'variant' }
  })
}

export function materialFulfillmentRunAvailable(run: ProductionRun, stage: StageRun): boolean {
  return stage.status === 'running' && (run.status === 'running' || (run.status === 'awaiting_input'
    && run.currentStageKey === stage.stageKey && run.blockedCode === 'waiting_material_recovery'
    && stage.reasonCode === run.blockedCode))
}

type Choice = MaterialFulfillmentChoice
interface Gateway {
  getMaterialFulfillmentContext(runId: string, stageId: string, unitKey: string): Promise<MaterialFulfillmentContext>
  fulfillMaterial(runId: string, context: MaterialFulfillmentContext, choice: Choice): Promise<MaterialFulfillmentReceipt>
  listSeries(query: ReferenceSeriesQuery): Promise<ReferenceSeriesPage>
  listVersions(query: {series_id: string; page: number}): Promise<ReferenceVersionPage>
  getVersion(id: string): Promise<AssetReferenceVersion>
  getAsset(id: string): Promise<Asset>
}
export function createMaterialFulfillmentForm(api: Gateway) {
  const state = reactive({ context: null as MaterialFulfillmentContext | null, directory: null as ReferenceSeriesPage | null,
    series: null as AssetReferenceSeries | null, asset: null as Asset | null, source: null as MaterialFulfillmentSource | null,
    receipt: null as MaterialFulfillmentReceipt | null, error: '', loading: false, seriesLoading: false, submitting: false,
    unknown: false, page: 1, versions: null as ReferenceVersionPage | null, version: null as AssetReferenceVersion | null,
    versionPage: 1, versionsLoading: false, choosingVersion: false, versionsError: '' })
  let sequence = 0, seriesSequence = 0, versionSequence = 0
  let target: {runId: string; stageId: string; planSha: string; option: MaterialRequirementOption} | null = null
  let pending: {runId: string; context: MaterialFulfillmentContext; choice: Choice} | null = null
  const editable = () => !!state.context?.canSelect && !state.loading && !state.submitting && !state.unknown && !state.receipt
    && !state.versionsLoading && !state.choosingVersion
  function clearVersions() {
    versionSequence++
    if (state.version) { state.asset = null; state.source = null }
    Object.assign(state, {versions: null, version: null, versionPage: 1, versionsLoading: false,
      choosingVersion: false, versionsError: ''})
  }
  function reset() {
    clearVersions()
    sequence++; seriesSequence++; target = null; pending = null
    Object.assign(state, { context: null, directory: null, series: null, asset: null, source: null, receipt: null,
      error: '', loading: false, seriesLoading: false, unknown: false, page: 1 })
  }
  function matches(receipt: MaterialFulfillmentReceipt, choice: Choice) {
    if ('referenceVersionId' in choice) return receipt.referenceVersionId === choice.referenceVersionId
    return receipt.seriesId === choice.seriesId && receipt.assetId === choice.assetId && receipt.source === choice.source
  }
  async function inspect(runId: string, stageId: string, planSha: string, option: MaterialRequirementOption) {
    if (state.submitting || state.unknown) return
    reset(); target = {runId, stageId, planSha, option}; await refresh()
  }
  async function refresh() {
    if (!target || state.loading || state.submitting) return
    const expected = target, token = ++sequence
    // A reread of this same target must not force another upload after a rejected
    // association. The old Asset is only a draft; the fresh writer revalidates it.
    state.loading = true; state.context = null; state.directory = null; state.series = null
    clearVersions()
    state.error = ''; seriesSequence++; state.seriesLoading = false
    try {
      const ctx = await api.getMaterialFulfillmentContext(expected.runId, expected.stageId, expected.option.unitKey)
      if (token !== sequence) return
      if (ctx.runPlanSha256 !== expected.planSha || ctx.requirementKey !== expected.option.requirementKey
        || ctx.unitKey !== expected.option.unitKey || ctx.stageRunId !== expected.stageId || ctx.appearanceRole !== expected.option.role
        || ctx.referenceName !== expected.option.name) throw new Error('读取期间本轮需求发生变化，请刷新运行')
      state.context = ctx
      if (state.source && ctx.sourceMode !== 'generate' && ctx.sourceMode !== state.source) {
        state.asset = null; state.source = null
      }
      if (ctx.selection) {
        if (pending && !matches(ctx.selection, pending.choice)) throw new Error('服务端已保存另一项选择；保留原事实，请刷新运行核对')
        state.receipt = ctx.selection; state.unknown = false; pending = null
      }
    } catch (cause) { if (token === sequence) { state.context = null; state.error = apiErrorSummary(cause) } }
    finally { if (token === sequence) state.loading = false }
  }
  async function loadSeries(page = 1) {
    if (!editable()) return
    const ctx = state.context!, token = ++seriesSequence
    state.seriesLoading = true; state.directory = null; state.series = null; state.error = ''
    clearVersions()
    try {
      const result = await api.listSeries({page, search: '', role: ctx.appearanceRole, bible_variant_id: ctx.appearanceId})
      if (token !== seriesSequence) return
      if (result.results.some(row => row.role !== ctx.appearanceRole || row.bible_variant_id !== ctx.appearanceId)) throw new Error('系列目录与所选外观不一致，不能使用本次结果')
      state.directory = result; state.page = page
    } catch (cause) { if (token === seriesSequence) state.error = apiErrorSummary(cause) }
    finally { if (token === seriesSequence) state.seriesLoading = false }
  }
  function chooseSeries(series: AssetReferenceSeries) {
    if (!editable() || state.seriesLoading || !state.directory?.results.some(row => row.id === series.id)) return
    clearVersions(); state.series = series; state.error = ''
  }
  function versionReason(version: AssetReferenceVersion): string {
    if (!state.context || !state.series || version.series_id !== state.series.id) return '版本不属于所选系列'
    if (version.setting_revision_id !== state.context.settingRevisionId) return '设定修订与本轮需求不一致'
    if (!['existing','upload','generated'].includes(version.source)) return '原始来源未识别'
    if (state.context.sourceMode === 'upload' && version.source !== 'upload') return '本项要求正式上传来源'
    if (!version.asset.is_active || !version.asset.mime_type.startsWith('image/')) return '原素材已停用或不是图片'
    return ''
  }
  async function loadVersions(page = 1) {
    if (!editable() || !state.series || state.seriesLoading) return
    const seriesId = state.series.id
    clearVersions(); const token = versionSequence
    state.versionsLoading = true; state.versionPage = page
    try {
      const result = await api.listVersions({series_id: seriesId, page})
      if (token !== versionSequence) return
      if (result.results.some(version => version.series_id !== seriesId)) throw new Error('版本目录不属于所选系列')
      state.versions = result
    } catch (cause) { if (token === versionSequence) state.versionsError = apiErrorSummary(cause) }
    finally { if (token === versionSequence) state.versionsLoading = false }
  }
  async function chooseVersion(version: AssetReferenceVersion) {
    if (!editable() || !state.versions?.results.some(row => row.id === version.id)) return
    const reason = versionReason(version)
    if (reason) { state.versionsError = reason; return }
    const expected = JSON.parse(JSON.stringify(version)) as AssetReferenceVersion, token = ++versionSequence
    state.choosingVersion = true; state.versionsError = ''; state.version = null; state.asset = null; state.source = null
    try {
      const asset = await resolveReferenceInputAsset(api, expected, 'image')
      if (token !== versionSequence) return
      state.version = expected; state.asset = asset; state.error = ''
    } catch (cause) { if (token === versionSequence) state.versionsError = apiErrorSummary(cause) }
    finally { if (token === versionSequence) state.choosingVersion = false }
  }
  function chooseAsset(asset: Asset, source: 'asset_library' | 'asset_upload') {
    if (!editable()) return
    versionSequence++; state.version = null
    const mode = source === 'asset_upload' ? 'upload' : 'existing'
    if (!inputAssetCompatible(asset, 'image') || (state.context!.sourceMode !== 'generate' && mode !== state.context!.sourceMode)) {
      state.asset = null; state.source = null
      state.error = '所选素材的类型、文件证据或来源不符合本项要求；未提交履约'; return
    }
    state.asset = asset; state.source = mode; state.error = ''
  }
  async function submit(): Promise<boolean> {
    if (state.submitting || state.loading) return false
    if (!pending) {
      if (!editable() || !target || !state.series || !state.asset || (!state.version && !state.source)) return false
      const choice: Choice = state.version ? {referenceVersionId: state.version.id}
        : {seriesId: state.series.id, assetId: state.asset.id, source: state.source!}
      materialFulfillmentPayload(state.context!, choice)
      pending = {runId: target.runId, context: JSON.parse(JSON.stringify(state.context)), choice}
    }
    const request = pending, token = sequence, wasUnknown = state.unknown
    state.submitting = true; state.error = ''
    try {
      const result = await api.fulfillMaterial(request.runId, request.context, {...request.choice})
      if (token !== sequence) return false
      if (!matches(result, request.choice)) throw new Error('履约回执与提交不一致，请核对原请求')
      state.receipt = result; pending = null; state.unknown = false; return true
    } catch (cause) {
      if (token === sequence) {
        state.error = apiErrorSummary(cause)
        state.unknown = wasUnknown || !isDefinitiveCreationRejection(cause)
        if (!state.unknown) { pending = null; state.context = null; state.directory = null; state.series = null }
      }
      return false
    } finally { state.submitting = false }
  }
  return {state, reset, inspect, refresh, loadSeries, chooseSeries, chooseAsset, loadVersions, chooseVersion, versionReason, submit}
}
