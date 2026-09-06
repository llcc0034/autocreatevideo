import { onBeforeUnmount, shallowRef, watch } from 'vue'
import type { ModelProfileVersion, WorkflowProjectorVersion, WorkflowTemplateVersion, JsonObject } from './platformTypes'
import { platformApi } from './platformApi'
import { apiErrorSummary } from './apiErrorPresentation'
import { isJsonObject } from './workflowProjectorDefinition'

export type ProjectorWorkflowContext =
  | { state: 'none' | 'loading' }
  | { state: 'error'; message: string }
  | { state: 'ready'; workflow: WorkflowTemplateVersion; model: ModelProfileVersion }

/** Read-only context: a URL is not evidence of a frozen relationship. */
export function useWorkflowProjectorContext(input: () => { workflowId?: string; projector?: WorkflowProjectorVersion }) {
  const context = shallowRef<ProjectorWorkflowContext>({ state: 'none' })
  let serial = 0
  const reload = async () => {
    const request = ++serial
    const { workflowId, projector } = input()
    context.value = { state: workflowId ? 'loading' : 'none' }
    if (!workflowId) return
    try {
      if (!projector || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(workflowId)) throw new Error('工作流查看上下文无效')
      const workflow = await platformApi.getWorkflowVersionForAudit(workflowId)
      if (request !== serial) return
      const identity = workflow.inputContract?.contract_projector
      if (workflow.id !== workflowId || !isJsonObject(identity)
        || identity.version_id !== projector.id || identity.definition_sha256 !== projector.definitionSha256
        || identity.id !== projector.projectorKey || identity.version !== projector.version) {
        throw new Error('该工作流的冻结 Projector 引用与当前查看版本不一致，请返回工作流核对')
      }
      if (!workflow.modelProfileVersion) throw new Error('该工作流尚未绑定精确模型版本，无法展示绑定合同')
      const model = await platformApi.getModelProfile(workflow.modelProfileVersion)
      if (request !== serial) return
      if (model.id !== workflow.modelProfileVersion) throw new Error('返回的模型版本与工作流绑定不一致')
      context.value = { state: 'ready', workflow, model }
    } catch (cause) {
      if (request === serial) context.value = { state: 'error', message: apiErrorSummary(cause, '工作流查看上下文读取失败') }
    }
  }
  watch(() => [input().workflowId, input().projector?.id, input().projector?.definitionSha256], () => { void reload() }, { immediate: true })
  onBeforeUnmount(() => { serial++ })
  return { context, reload }
}

/** Never replace a missing frozen range with current model data. */
export function frozenAssetQuantity(context: ProjectorWorkflowContext | undefined, slotKey: unknown) {
  if (context?.state !== 'ready') return null
  const contract = context.workflow.inputContract
  const classified = contract?.classified_references
  let row: JsonObject | undefined
  let path = ''
  if (isJsonObject(classified) && isJsonObject(classified.groups)) {
    const value = classified.groups[String(slotKey)]
    if (isJsonObject(value)) { row = value; path = `input_contract.classified_references.groups.${String(slotKey)}` }
  } else if (Array.isArray(contract?.asset_groups)) {
    const matches = contract.asset_groups.filter(value => isJsonObject(value) && value.key === slotKey)
    if (matches.length === 1) { row = matches[0] as JsonObject; path = `input_contract.asset_groups[key=${String(slotKey)}]` }
  }
  if (!row || !Number.isSafeInteger(row.min_items) || !Number.isSafeInteger(row.max_items)
    || Number(row.min_items) < 0 || Number(row.max_items) < Number(row.min_items)) return null
  return { minimum: Number(row.min_items), maximum: Number(row.max_items), path }
}

export function projectorModelComparison(config: JsonObject, model: ModelProfileVersion | null, busy = false, error = '') {
  if (busy) return { state: 'loading', label: '正在加载对照模型' }
  if (error) return { state: 'error', label: '对照模型读取失败' }
  if (!model) return { state: 'none', label: '未加载对照模型' }
  const capabilities = model.specification.capabilities
  const contract = model.specification.conditioning_contract
  const modes = isJsonObject(contract) && Array.isArray(contract.modes) ? contract.modes.filter(isJsonObject) : []
  const matches = modes.filter(mode => mode.key === config.conditioning_mode && mode.input_kind === 'classified_references')
  const match = Array.isArray(capabilities) && capabilities.includes(config.capability) && matches.length === 1
  return { state: match ? 'matched' : 'mismatch', label: match ? '能力与模式：与所选模型一致' : '能力或模式：与所选模型不匹配' }
}
