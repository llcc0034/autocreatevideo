import { ref } from 'vue'
import { platformApi } from './platformApi'
import { apiErrorSummary } from './apiErrorPresentation'
import type { WorkflowProjector, WorkflowProjectorVersion } from './platformTypes'

export interface ProjectorWriteIntent {
  kind: 'family' | 'v1' | 'update' | 'clone'
  key: string
  familyId: string
  id: string
  sourceId: string
}
export type ProjectorRecoveryRow = WorkflowProjector | WorkflowProjectorVersion
export function projectorRecoveryMatches(row: ProjectorRecoveryRow, target: ProjectorWriteIntent) {
  if (!row.id) return false
  if (target.kind === 'family') return 'key' in row && row.key === target.key
  return 'projector' in row && row.projector === target.familyId && row.projectorKey === target.key
    && (!target.id || row.id === target.id) && row.id !== target.sourceId
}
export function useProjectorAuthoringRecovery(scope: () => string) {
  const intent = ref<ProjectorWriteIntent | null>(null), busy = ref(false), error = ref(''), checked = ref(false)
  const records = ref<ProjectorRecoveryRow[]>([])
  let serial = 0
  function reset() { serial++; intent.value = null; busy.value = false; error.value = ''; checked.value = false; records.value = [] }
  function mark(target: ProjectorWriteIntent) { reset(); intent.value = { ...target } }
  async function read(id?: string): Promise<ProjectorRecoveryRow | null> {
    if (!intent.value || busy.value) return null
    const target = intent.value, origin = scope(), generation = ++serial
    const current = () => generation === serial && intent.value === target && origin === scope()
    busy.value = true; error.value = ''
    try {
      const rows = target.kind === 'family' ? await platformApi.listWorkflowProjectors()
        : target.kind === 'update' ? [await platformApi.getWorkflowProjectorVersion(target.id)]
        : await platformApi.listWorkflowProjectorVersions()
      if (!current()) return null
      if (target.kind === 'update' && !projectorRecoveryMatches(rows[0]!, target)) throw Error('返回记录与原保存目标身份不一致，未载入。')
      records.value = rows.filter(row => projectorRecoveryMatches(row, target)); checked.value = true
      if (!id) return null
      const candidate = records.value.find(row => row.id === id)
      if (!candidate) throw Error('该精确记录不在本次核对范围，请重新读取。')
      const row = 'projector' in candidate ? await platformApi.getWorkflowProjectorVersion(id) : candidate
      if (!current()) return null
      if (row.id !== id || !projectorRecoveryMatches(row, target) || 'status' in row && row.status !== 'draft') throw Error('记录身份或状态已变化，未载入；请重新核对。')
      return row
    } catch (cause) { if (current()) error.value = apiErrorSummary(cause, '只读核对失败'); return null }
    finally { if (current()) busy.value = false }
  }
  return { intent, busy, error, checked, records, reset, mark, read }
}
