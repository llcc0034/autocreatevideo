import type { ComfySavedWorkflowBatchItemInput, ComfySavedWorkflowPreview, WorkflowTemplateVersion } from './platformTypes'
import type { SavedWorkflowDraftStorage } from './savedWorkflowDraftSession'
import { ref } from 'vue'

// This is a browser-side unresolved intent, never proof of a server write.
export interface SavedWorkflowPendingImport {
  id: string
  startedAt: string
  deploymentId: string
  input: ComfySavedWorkflowBatchItemInput
  contractRequest?: { candidateSourceId: string }
}
export function assertSavedWorkflowPreviewReceipt(pending: SavedWorkflowPendingImport, preview: ComfySavedWorkflowPreview) {
  const receipt = preview.requestReceipt, input = pending.input, request = receipt?.request
  if (!receipt || !request || receipt.requestKey !== pending.id
    || receipt.candidateSourceId !== pending.contractRequest?.candidateSourceId
    || request.deploymentId !== pending.deploymentId || request.sourcePath !== input.sourcePath
    || request.expectedSha256 !== input.expectedSha256
    || request.expectedSourceScopeSchemaVersion !== input.expectedSourceScopeSchemaVersion
    || request.expectedSourceScopeSha256 !== input.expectedSourceScopeSha256
    || request.modelProfileVersion !== input.modelProfileVersion
    || request.primaryOutputNodeIds.length !== 1 || request.primaryOutputNodeIds[0] !== input.primaryOutputNodeIds?.[0]
    || preview.sourcePath !== input.sourcePath || preview.sourceSha256 !== input.expectedSha256
    || preview.primaryOutputNodeIds.length !== 1 || preview.primaryOutputNodeIds[0] !== input.primaryOutputNodeIds?.[0]
    || !preview.contractDraftId || !(preview.contractDraftRevision > 0)
    || !preview.suggestedInputContract || typeof preview.suggestedInputContract !== 'object'
    || (preview.actionability === 'ready_for_executable_import' && (
      !Object.keys(preview.suggestedInputContract).length || !sha(preview.objectInfoSchemaSha256)
      || !sha(preview.convertedGraphSha256) || !sha(preview.parameterSchemaSha256) || !sha(preview.structuralContractSha256)))) {
    throw new Error('合同回执、完整合同或原请求身份不一致；保留原请求，不重新生成。')
  }
}
export interface SavedWorkflowSourceRecord {
  id: string
  deploymentId: string
  scopeVersion: number
  scopeSha256: string
  path: string
  sha256: string
  kind: string
  versionId: string
  importContractSha256: string
  report: Record<string, unknown>
}
const key = (userId: string) => `autoprompt.workflow-import-pending.v1.${encodeURIComponent(userId)}`
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const sha = (value: unknown) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const text = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 1000 && !/[\u0000-\u001f\u007f]/.test(value)
const storageError = () => new Error('无法读取或保存未确认的工作流导入请求；已停止新提交。请检查浏览器站点存储，不能清空记录后盲目重试。')

export function readSavedWorkflowPending(storage: SavedWorkflowDraftStorage, userId: string): SavedWorkflowPendingImport[] {
  try {
    const raw = storage.getItem(key(userId))
    if (raw === null) return []
    const ledger = record(JSON.parse(raw))
    if (ledger.schemaVersion !== 1 || ledger.userId !== userId || !Array.isArray(ledger.items)) throw storageError()
    const items = ledger.items as SavedWorkflowPendingImport[]
    for (const value of items) {
      const row = record(value), input = record(row.input)
      if (!text(row.id) || !text(row.deploymentId) || !text(row.startedAt) || !Number.isFinite(Date.parse(row.startedAt))
        || !text(input.sourcePath) || !sha(input.expectedSha256) || !sha(input.expectedSourceScopeSha256)
        || !Number.isSafeInteger(input.expectedSourceScopeSchemaVersion) || Number(input.expectedSourceScopeSchemaVersion) < 1
        || !['template_version', 'candidate', 'ignored', 'execution_snapshot'].includes(String(input.importKind))
        || (row.contractRequest !== undefined && (input.importKind !== 'template_version'
          || !text(record(row.contractRequest).candidateSourceId)
          || !(input.modelProfileVersion === null || text(input.modelProfileVersion))
          || !Array.isArray(input.primaryOutputNodeIds) || input.primaryOutputNodeIds.length !== 1 || !text(input.primaryOutputNodeIds[0])))
        || (input.importKind === 'template_version' && row.contractRequest === undefined && (!text(input.contractDraft) || !text(input.modelProfileVersion)
          || !sha(input.expectedStructuralContractSha256) || !sha(input.expectedConvertedGraphSha256) || !sha(input.expectedObjectInfoSchemaSha256) || !sha(input.expectedParameterSchemaSha256)
          || !Array.isArray(input.primaryOutputNodeIds) || input.primaryOutputNodeIds.length !== 1 || !text(input.primaryOutputNodeIds[0])
          || (input.successorOf != null && !text(input.successorOf))
          || (!text(input.template) && !text(record(input.newTemplate).slug))))) throw storageError()
    }
    if (new Set(items.map(row => row.id)).size !== items.length) throw storageError()
    return items
  } catch { throw storageError() }
}

export function changeSavedWorkflowPending(storage: SavedWorkflowDraftStorage, userId: string, update: (items: SavedWorkflowPendingImport[]) => SavedWorkflowPendingImport[]) {
  const items = update(readSavedWorkflowPending(storage, userId))
  try {
    storage.setItem(key(userId), JSON.stringify({ schemaVersion: 1, userId, items }))
    return readSavedWorkflowPending(storage, userId)
  } catch { throw storageError() }
}

export function savedWorkflowPendingCovers(pending: SavedWorkflowPendingImport, scopeVersion: number, path: string, scopeSha256: string) {
  // Do not evade an unresolved request by changing the source content or target.
  return pending.input.expectedSourceScopeSchemaVersion === scopeVersion && pending.input.sourcePath === path && pending.input.expectedSourceScopeSha256 === scopeSha256
}

export function adaptSavedWorkflowSource(value: unknown): SavedWorkflowSourceRecord {
  const row = record(value)
  if (!text(row.id) || !text(row.source_deployment) || !text(row.source_path) || !sha(row.source_sha256)
    || !sha(row.source_scope_sha256) || !Number.isSafeInteger(row.source_scope_schema_version) || !text(row.import_kind)) {
    throw new Error('工作流来源回读缺少精确身份；不能解除未知结果')
  }
  return { id: row.id, deploymentId: row.source_deployment, scopeVersion: Number(row.source_scope_schema_version),
    scopeSha256: String(row.source_scope_sha256), path: row.source_path, sha256: String(row.source_sha256),
    kind: row.import_kind, versionId: typeof row.workflow_version === 'string' ? row.workflow_version : '',
    importContractSha256: typeof row.import_contract_sha256 === 'string' ? row.import_contract_sha256 : '', report: record(row.conversion_report) }
}

export function savedWorkflowSourceMatches(pending: SavedWorkflowPendingImport, row: SavedWorkflowSourceRecord) {
  const input = pending.input, report = row.report
  if (row.scopeVersion !== input.expectedSourceScopeSchemaVersion
    || row.scopeSha256 !== input.expectedSourceScopeSha256 || row.path !== input.sourcePath
    || row.sha256 !== input.expectedSha256 || row.kind !== input.importKind) return false
  if (input.importKind !== 'template_version') return !row.versionId
  return Boolean(row.versionId && report.promotion_source === 'comfy_workflow_contract_draft'
    && report.contract_draft_id === input.contractDraft
    && (report.successor_of || null) === (input.successorOf || null)
    && report.structural_contract_sha256 === input.expectedStructuralContractSha256
    && report.source_converted_graph_sha256 === input.expectedConvertedGraphSha256
    && report.object_info_schema_sha256 === input.expectedObjectInfoSchemaSha256
    && report.primary_output_node_id === input.primaryOutputNodeIds?.[0])
}

// Verify the existing backend import receipt hash; this does not derive a new
// execution contract or accept a locally calculated value as server evidence.
export async function savedWorkflowImportFingerprint(input: ComfySavedWorkflowBatchItemInput, templateId: string, projectionSha256: string) {
  const payload: Record<string, unknown> = {
    ...(input.successorOf ? { successor_of: input.successorOf } : {}), import_kind: input.importKind,
    template_id: templateId, model_profile_version_id: input.modelProfileVersion,
    contract_draft_id: input.contractDraft, contract_draft_projection_sha256: projectionSha256,
    primary_output_node_ids: [...new Set(input.primaryOutputNodeIds)].sort(),
    expected_object_info_schema_sha256: input.expectedObjectInfoSchemaSha256,
    expected_converted_graph_sha256: input.expectedConvertedGraphSha256,
    expected_parameter_schema_sha256: input.expectedParameterSchemaSha256,
    expected_structural_contract_sha256: input.expectedStructuralContractSha256,
  }
  const canonical = JSON.stringify(Object.fromEntries(Object.keys(payload).sort().map(key => [key, payload[key]])))
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonical)))].map(byte => byte.toString(16).padStart(2, '0')).join('')
}

export function savedWorkflowVersionMatches(pending: SavedWorkflowPendingImport, source: SavedWorkflowSourceRecord, version: WorkflowTemplateVersion) {
  const input = pending.input
  return version.id === source.versionId && version.modelProfileVersion === input.modelProfileVersion
    && (input.template ? version.template === input.template : Boolean(input.newTemplate
      && version.templateSlug === input.newTemplate.slug && version.templateName === input.newTemplate.name
      && version.outputKind === input.newTemplate.outputKind))
}

export function useSavedWorkflowRecovery(options: {
  userId: () => string
  scope: () => string
  active: () => boolean
  storage: () => SavedWorkflowDraftStorage
  list: (deploymentId: string, input: ComfySavedWorkflowBatchItemInput) => Promise<SavedWorkflowSourceRecord[]>
  version: (id: string) => Promise<WorkflowTemplateVersion>
  contractReceipt?: (deploymentId: string, requestKey: string) => Promise<ComfySavedWorkflowPreview>
  recoveredContract?: (pending: SavedWorkflowPendingImport, preview: ComfySavedWorkflowPreview) => boolean
  recovered: (pending: SavedWorkflowPendingImport, source: SavedWorkflowSourceRecord) => void
  withLock?: <T>(key: string, work: () => T | Promise<T>) => Promise<T>
}) {
  const pending = ref<SavedWorkflowPendingImport[]>([]), error = ref(''), busy = ref(false)
  const results = ref<Array<{ id: string; path: string; detail: string; versionId: string; preview?: ComfySavedWorkflowPreview }>>([])
  let generation = 0
  const reload = () => {
    generation++; busy.value = false; results.value = []; error.value = ''; pending.value = []
    if (!options.userId()) return
    try { pending.value = readSavedWorkflowPending(options.storage(), options.userId()) }
    catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause) }
  }
  const locked = <T>(userId: string, work: () => T | Promise<T>) => {
    if (options.withLock) return options.withLock(key(userId), work)
    if (!navigator.locks) throw new Error('浏览器不支持跨页面提交协调，已停止导入；请使用支持 Web Locks 的浏览器。')
    return navigator.locks.request(key(userId), work)
  }
  const covers = (scopeVersion: number, path: string, scopeSha256: string) => Boolean(error.value)
    || pending.value.some(row => savedWorkflowPendingCovers(row, scopeVersion, path, scopeSha256))
  const begin = async (deploymentId: string, items: ComfySavedWorkflowBatchItemInput[], contractRequest?: { candidateSourceId: string }) => {
    const userId = options.userId()
    if (!userId) throw new Error('登录身份未就绪，不能提交导入')
    const requestId = crypto.randomUUID()
    const intents = items.map((input, index) => ({ id: `${requestId}:${index}`, startedAt: new Date().toISOString(), deploymentId, input: JSON.parse(JSON.stringify(input)),
      ...(contractRequest ? { contractRequest: { ...contractRequest } } : {}) }))
    try {
      const rows = await locked(userId, () => changeSavedWorkflowPending(options.storage(), userId, old => {
        if (intents.some(row => old.some(prior => savedWorkflowPendingCovers(prior, row.input.expectedSourceScopeSchemaVersion, row.input.sourcePath, row.input.expectedSourceScopeSha256)))) {
          throw new Error('该来源仍有未确认的导入请求')
        }
        return [...old, ...intents]
      }))
      if (options.userId() === userId && options.active()) pending.value = rows
    } catch (cause) { if (options.userId() === userId) reload(); throw cause }
    return { userId, ids: intents.map(row => row.id) }
  }
  const settle = async (ticket: { userId: string; ids: string[] }, current = () => true) => {
    try {
      const rows = await locked(ticket.userId, () => current()
        ? changeSavedWorkflowPending(options.storage(), ticket.userId, old => old.filter(row => !ticket.ids.includes(row.id))) : null)
      if (!rows) return
      if (options.active() && options.userId() === ticket.userId) pending.value = rows
    } catch (cause) {
      if (options.active() && options.userId() === ticket.userId) error.value = cause instanceof Error ? cause.message : String(cause)
    }
  }
  const readback = async () => {
    if (busy.value) return
    reload()
    if (error.value || !pending.value.length) return
    const token = ++generation, scope = options.scope(), userId = options.userId()
    const current = () => token === generation && options.active() && scope === options.scope() && userId === options.userId()
    busy.value = true
    try {
      for (const item of [...pending.value]) {
        if (!current()) return
        try {
          if (item.contractRequest) {
            if (!options.contractReceipt) throw new Error('合同回执只读查询尚未接入，原请求仍保留。')
            const preview = await options.contractReceipt(item.deploymentId, item.id)
            if (!current()) return
            assertSavedWorkflowPreviewReceipt(item, preview)
            const restored = options.recoveredContract?.(item, preview) === true
            if (restored) await settle({ userId, ids: [item.id] }, current)
            if (!current()) return
            results.value.push({ id: item.id, path: item.input.sourcePath, versionId: '', preview,
              detail: restored && !error.value
                ? '已按原请求找回完整合同并恢复当前导入表单；未重新生成、提升或执行。请复核后明确提交提升。'
                : '原请求已确认，完整合同如下。当前表单未载入同一候选或选择已改变；未覆盖输入。请扫描载入同一来源、恢复原模型及主输出后再次只读核对。原请求继续保留。' })
            continue
          }
          const rows = (await options.list(item.deploymentId, item.input)).filter(row => savedWorkflowSourceMatches(item, row))
          if (!current()) return
          if (rows.length !== 1) throw new Error(rows.length ? '匹配到多份来源，无法唯一确认；不会按最新版本选择。' : '未发现与原请求完全匹配的来源，不代表未保存；请稍后继续核对，不会重发。')
          const source = rows[0]!
          if (source.versionId) {
            const version = await options.version(source.versionId)
            if (!current()) return
            if (!savedWorkflowVersionMatches(item, source, version)) throw new Error('来源指向的版本与原模型或保存去向不一致，保留未确认请求。')
            const projection = source.report.contract_draft_projection_sha256
            if (!sha(projection) || !sha(source.importContractSha256)
              || await savedWorkflowImportFingerprint(item.input, version.template, String(projection)) !== source.importContractSha256) {
              throw new Error('完整导入摘要与原请求不一致（含参数 Schema）；保留未确认请求。')
            }
            if (!current()) return
          }
          await settle({ userId, ids: [item.id] }, current)
          if (!current()) return
          if (error.value) return
          options.recovered(item, source)
          results.value.push({ id: item.id, path: item.input.sourcePath, versionId: source.versionId,
            detail: '已核对原请求的精确来源记录；未重新提交，未发布或执行。' })
        } catch (cause) {
          if (!current()) return
          results.value.push({ id: item.id, path: item.input.sourcePath, versionId: '', detail: cause instanceof Error ? cause.message : String(cause) })
        }
      }
    } finally { if (token === generation) busy.value = false }
  }
  return { pending, error, busy, results, reload, covers, begin, settle, readback }
}
