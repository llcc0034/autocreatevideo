import {reactive} from 'vue'
import type {JsonObject, ProductionRun} from './platformTypes'
import type {ManualActionOptions} from './productionManual'
import {apiErrorSummary} from './apiErrorPresentation'
import {isDefinitiveCreationRejection} from './productionOnboarding'
import {buildShotReferenceSelection, readShotReferences, type ReferenceSelection, type ShotReferences, type ShotReferenceUse} from './productionManualShotReferences'

// A page-local collection of drafts and original per-shot commands, not a new
// queue or selection store. Each HTTP command retains the original transaction.
export type BatchStatus = 'draft'|'ready'|'conflict'|'blocked'|'saving'|'confirmed'|'unknown'|'rejected'
export interface BatchUse {
  use: ShotReferenceUse; selection: ReferenceSelection; checked: boolean; beforeLabel: string
}
export interface BatchShot {
  shotId: string; label: string; detail: ShotReferences; uses: BatchUse[]; status: BatchStatus
  error: string; request: JsonObject|null; receipt: JsonObject|null
}
const copy = <T>(v: T): T => JSON.parse(JSON.stringify(v))
function basis(detail: ShotReferences) {
  return JSON.stringify([detail.run_id, detail.run_plan_sha256, detail.uses_sha256,
    'selection_rule' in detail ? detail.source_snapshot_sha256 : [detail.storyboard_adoption_revision, detail.storyboard_adoption_sha256]])
}
export function createShotReferenceBatch(api: {
  read(runId: string, shotId: string): Promise<unknown>; submit(runId: string, request: JsonObject): Promise<JsonObject>
}, run: () => ProductionRun, options: () => ManualActionOptions|undefined, changed: () => void) {
  const state = reactive({shots: [] as BatchShot[], previewed: false, loading: false, submitting: false, error: ''})
  const context = () => `${run().id}:${run().project}:${run().planSource}:${run().executionPlan?.sha256}`
  let origin = '', serial = 0
  const pending = () => state.shots.some(shot => shot.status === 'unknown')
  const locked = () => state.loading || state.submitting || pending()
  function invalidate() {
    state.previewed = false; state.error = ''
    for (const shot of state.shots) if (shot.status === 'ready') {shot.status = 'draft'; shot.request = null}
  }
  function add(detail: ShotReferences, label: string, selection: ReferenceSelection, beforeLabel: string) {
    if (locked()) throw Error('批量读取、保存或原请求待核对时不能改变目标')
    if (state.shots.length && origin !== context()) throw Error('运行上下文已变化；不能将旧批次带入新运行')
    buildShotReferenceSelection(detail, run(), options(), 'preview-validation', selection)
    let shot = state.shots.find(row => row.shotId === detail.shot_id)
    if (shot && (shot.status !== 'draft' && shot.status !== 'ready')) throw Error('请先移除此镜头的旧结果，再重新读取并选择')
    if (shot && basis(shot.detail) !== basis(detail)) throw Error('同镜用途基线已变化，请移除此镜头草稿并重新读取')
    invalidate(); origin = context()
    if (!shot) {
      shot = reactive({shotId: detail.shot_id, label, detail: copy(detail), uses: [], status: 'draft', error: '', request: null, receipt: null}) as BatchShot
      state.shots.push(shot)
    }
    const use = detail.state.uses.find(row => row.id === selection.choice.use_id)!
    const row = {use: copy(use), selection: copy(selection), checked: true, beforeLabel}
    const index = shot.uses.findIndex(item => item.use.id === use.id)
    if (index < 0) shot.uses.push(row); else shot.uses[index] = row
    shot.uses.sort((a,b) => a.use.position - b.use.position)
  }
  function check(shot: BatchShot, useId: string|null, checked: boolean) {
    if (locked() || !['draft','ready'].includes(shot.status)) return
    invalidate()
    for (const row of shot.uses) if (useId === null || row.use.id === useId) row.checked = checked
  }
  function remove(shot: BatchShot) {
    if (locked()) return
    invalidate(); state.shots = state.shots.filter(row => row !== shot)
  }
  async function preview() {
    if (locked()) return
    if (origin !== context()) {state.error = '运行上下文已变化，不能预览或提交旧批次'; return}
    invalidate(); state.loading = true
    const token = ++serial, ctx = context(), currentRun = copy(run())
    for (const shot of state.shots) {
      if (shot.status !== 'draft' || !shot.uses.some(row => row.checked)) continue
      try {
        const current = readShotReferences(await api.read(currentRun.id, shot.shotId), currentRun, shot.shotId)
        if (token !== serial || ctx !== context()) break
        if (basis(current) !== basis(shot.detail)) {
          shot.status = 'conflict'; shot.error = '用途、选择或创作来源已变化。本镜未提交；请移除后重读，不自动覆盖。'; continue
        }
        const requests = shot.uses.filter(row => row.checked).map(row => buildShotReferenceSelection(current, currentRun, options(), crypto.randomUUID(), row.selection))
        // All selected uses share one observed baseline and one shot command.
        const request = requests[0]!
        const payload = request.payload as JsonObject
        payload.choices = requests.flatMap(row => (row.payload as JsonObject).choices as JsonObject[])
        shot.request = copy(request); shot.status = 'ready'; shot.error = ''
      } catch (cause) {
        if (token !== serial || ctx !== context()) break
        shot.status = 'blocked'; shot.error = apiErrorSummary(cause)
      }
    }
    if (token !== serial) return
    state.loading = false
    if (ctx !== context()) {invalidate(); state.error = '运行上下文已变化，预览已失效'; return}
    state.previewed = true
  }
  async function send(shot: BatchShot, retry: boolean) {
    try {
      const receipt = await api.submit(shot.detail.run_id, copy(shot.request!))
      if (receipt.action !== 'select' || receipt.result_kind !== 'shot_reference_choices' || receipt.target_id !== shot.shotId)
        throw Error('未返回本镜正式选材回执，请核对原请求，不能另发新选择')
      shot.receipt = copy(receipt); shot.status = 'confirmed'; shot.error = ''; shot.request = null
      changed()
    } catch (cause) {
      shot.error = apiErrorSummary(cause)
      const status = (cause as {response?: {status?: number}})?.response?.status
      if (!retry && (isDefinitiveCreationRejection(cause) || status === 409)) {
        shot.status = status === 409 ? 'conflict' : 'rejected'; shot.request = null
      } else shot.status = 'unknown' // Even a later 4xx cannot disprove the first request.
    }
  }
  async function submit() {
    if (locked() || !state.previewed || origin !== context()) return
    if (['succeeded','failed','cancelled'].includes(run().status)) {invalidate(); state.error = '本轮已结束，不能提交新选择'; return}
    state.submitting = true
    try {
      for (const shot of state.shots) {
        if (origin !== context()) {state.error = '运行上下文改变，剩余镜头未提交'; break}
        if (shot.status !== 'ready' || !shot.request) continue
        shot.status = 'saving'; await send(shot, false)
      }
    } finally {state.submitting = false; state.previewed = false}
  }
  async function retry(shot: BatchShot) {
    if (state.loading || state.submitting || shot.status !== 'unknown' || !shot.request || origin !== context()) return
    state.submitting = true
    try {await send(shot, true)} finally {state.submitting = false}
  }
  return {state, pending, locked, add, check, remove, preview, submit, retry, dispose() {serial++}}
}
