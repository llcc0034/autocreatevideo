import { reactive } from 'vue'
import { apiErrorSummary } from './apiErrorPresentation'
import { isDefinitiveCreationRejection } from './productionOnboarding'

export type ArtifactSelectionBody = { artifact_id: string; material_selection?: { expected_decision_id: string | null; expected_dependencies_sha256: string } }
export type ArtifactSelectionReceipt = { id: string; action: 'select'; created_at: string; result_kind: 'artifact_selection'; target_id: string }
export type ArtifactSelectionContext = { runId: string; scopeId: string }
export type ArtifactSelectionProtection = ArtifactSelectionContext & { key: string; active: boolean }
export type ArtifactSelectionConfirmed = ArtifactSelectionContext & { receipt: ArtifactSelectionReceipt }
const uuid = (value: unknown) => typeof value === 'string' && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value)
export function readArtifactSelectionReceipt(value: unknown): ArtifactSelectionReceipt {
  const row = value as Partial<ArtifactSelectionReceipt> | null
  if (!row || row.action !== 'select' || row.result_kind !== 'artifact_selection' || !uuid(row.id) || !uuid(row.target_id)
      || typeof row.created_at !== 'string' || !Number.isFinite(Date.parse(row.created_at)))
    throw Error('未返回正式选择回执；请核对原选择请求，不要另发新选择')
  return row as ArtifactSelectionReceipt
}

// This is transient request recovery, never a second source of selected-artifact facts.
export function createArtifactSelectionCommand(api: (runId: string, body: ArtifactSelectionBody, key: string) => Promise<unknown>,
  context: () => ArtifactSelectionContext, protection: (value: ArtifactSelectionProtection) => void,
  confirmed: (value: ArtifactSelectionConfirmed) => void) {
  const state = reactive({ pending: null as (ArtifactSelectionContext & {key: string; body: ArtifactSelectionBody}) | null,
    submitting: false, error: '', notice: '' })
  let serial = 0
  const copy = <T>(value: T): T => JSON.parse(JSON.stringify(value))
  function reset() {
    ++serial
    if (state.pending) protection({...state.pending, active: false})
    state.pending = null; state.submitting = false; state.error = ''; state.notice = ''
  }
  async function submit(body?: ArtifactSelectionBody) {
    if (state.submitting || (!state.pending && !body)) return
    const retrying = !!state.pending, token = serial
    state.pending ??= {...context(), key: crypto.randomUUID(), body: copy(body!)}
    const request = copy(state.pending), current = () => token === serial
      && context().runId === request.runId && context().scopeId === request.scopeId
    state.submitting = true; state.error = ''; state.notice = ''
    protection({...request, active: true})
    try {
      const receipt = readArtifactSelectionReceipt(await api(request.runId, copy(request.body), request.key))
      if (!current()) return
      state.pending = null
      state.notice = '原选择回执已确认；当前选择以重新读取的服务端事实为准，不代表审核采用或生成成功。'
      protection({...request, active: false})
      confirmed({runId: request.runId, scopeId: request.scopeId, receipt})
    } catch (cause) {
      if (current()) {
        state.error = apiErrorSummary(cause, '选择结果尚未确认')
        const status = (cause as {response?: {status?: number}})?.response?.status
        if (!retrying && (isDefinitiveCreationRejection(cause) || status === 409)) {
          state.pending = null; protection({...request, active: false})
        }
      }
    } finally { if (current()) state.submitting = false }
  }
  return {state, submit, reset}
}
