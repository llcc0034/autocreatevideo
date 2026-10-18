import type { TaskWorkflowExport } from '../types/index.ts'

export function adaptTaskWorkflowExport(value: unknown): TaskWorkflowExport {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('工作流导出响应不完整')
  const raw = value as Record<string, unknown>
  const text = (key: string, allowBlank = false): string => {
    const item = raw[key]
    if (typeof item !== 'string' || (!allowBlank && !item)) throw new Error(`工作流导出缺少 ${key}`)
    return item
  }
  const flag = (key: string): boolean => {
    if (typeof raw[key] !== 'boolean') throw new Error(`工作流导出缺少 ${key}`)
    return raw[key] as boolean
  }
  const count = (key: string): number => {
    if (!Number.isInteger(raw[key]) || (raw[key] as number) < 0) throw new Error(`工作流导出缺少 ${key}`)
    return raw[key] as number
  }
  if (!raw.workflow || typeof raw.workflow !== 'object' || Array.isArray(raw.workflow)) throw new Error('服务端未返回有效的工作流 JSON')
  if (raw.format !== 'comfyui_api' && raw.format !== 'comfyui_ui') throw new Error('工作流导出格式不受支持')
  if (raw.graph_state !== 'prepared' && raw.graph_state !== 'staged') throw new Error('工作流暂存状态缺失')
  if (!Array.isArray(raw.available_formats) || raw.available_formats.some(item => !['comfyui_api', 'comfyui_ui'].includes(item))) throw new Error('工作流可用导出格式缺失')
  return {
    taskId: text('task_id'), attemptId: text('attempt_id'), preparationId: text('preparation_id'),
    executionInstanceVersionId: text('execution_instance_version_id'), workflowVersionId: text('workflow_version_id'),
    requestSha256: text('request_sha256'), definitionSha256: text('definition_sha256'),
    graphState: raw.graph_state, workflowJson: raw.workflow as Record<string, unknown>,
    contentHash: text('content_hash'), filename: text('filename'), format: raw.format,
    importReady: flag('import_ready'), comfyuiUrl: text('comfyui_url', true), safeToOpen: flag('safe_to_open'),
    directOpenSupported: flag('direct_open_supported'), openMode: text('open_mode'),
    downloadUrl: text('download_url'), apiDownloadUrl: text('api_download_url'), uiDownloadUrl: text('ui_download_url'),
    availableFormats: raw.available_formats as Array<'comfyui_api' | 'comfyui_ui'>,
    containsStagedPlaceholders: flag('contains_staged_placeholders'), stagedPlaceholderCount: count('staged_placeholder_count'),
    missingReceiptCount: count('missing_receipt_count'), openReason: text('open_reason'),
  }
}
