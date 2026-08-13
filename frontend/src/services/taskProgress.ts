import type {
  Workflow,
  WorkflowProgressSource,
  WorkflowProgressTelemetryState,
  WorkflowStatus,
} from '../types/index.ts'

type RawTaskProgress = Record<string, unknown>

export interface AdaptedTaskProgressTelemetry {
  progressSource: WorkflowProgressSource
  comfyProgress?: number
  progressNodeId?: string
  progressUpdatedAt?: string
  progressTelemetryState: WorkflowProgressTelemetryState
  progressTelemetryError?: string
  remoteState?: string
  progressIndeterminate: boolean
}

export type WorkflowProgressMode = 'queued' | 'indeterminate' | 'comfyui' | 'terminal' | 'failed' | 'paused'

export interface WorkflowProgressPresentation {
  mode: WorkflowProgressMode
  indeterminate: boolean
  percent?: number
  heading: string
  valueLabel: string
  compactLabel: string
  summary: string
  note?: string
}

const PROGRESS_SOURCES = new Set<WorkflowProgressSource>(['none', 'submission', 'comfyui', 'terminal'])
const TELEMETRY_STATES = new Set<WorkflowProgressTelemetryState>(['unknown', 'connected', 'receiving', 'unavailable'])

const optionalString = (value: unknown): string | undefined => {
  const normalized = String(value ?? '').trim()
  return normalized || undefined
}

const boundedPercent = (value: unknown): number | undefined => {
  if (value === null || value === undefined || value === '') return undefined
  const numeric = Number(value)
  return Number.isFinite(numeric) ? Math.min(100, Math.max(0, Math.round(numeric))) : undefined
}

/**
 * Normalize the backend progress contract without inferring telemetry from the
 * legacy platform `progress` value. A running legacy response therefore stays
 * indeterminate until the API explicitly identifies a ComfyUI sample.
 */
export const adaptTaskProgressTelemetry = (
  raw: RawTaskProgress,
  status: WorkflowStatus,
): AdaptedTaskProgressTelemetry => {
  const rawSource = String(raw.progress_source ?? '').trim().toLowerCase()
  const progressSource: WorkflowProgressSource = status === 'completed'
    ? 'terminal'
    : PROGRESS_SOURCES.has(rawSource as WorkflowProgressSource)
      ? rawSource as WorkflowProgressSource
      : 'none'
  const rawTelemetryState = String(raw.progress_telemetry_state ?? '').trim().toLowerCase()
  const progressTelemetryState: WorkflowProgressTelemetryState = TELEMETRY_STATES.has(rawTelemetryState as WorkflowProgressTelemetryState)
    ? rawTelemetryState as WorkflowProgressTelemetryState
    : 'unknown'
  const comfyProgress = progressSource === 'comfyui' ? boundedPercent(raw.comfy_progress) : undefined

  return {
    progressSource,
    ...(comfyProgress !== undefined ? { comfyProgress } : {}),
    ...(optionalString(raw.progress_node_id) ? { progressNodeId: optionalString(raw.progress_node_id) } : {}),
    ...(optionalString(raw.progress_updated_at) ? { progressUpdatedAt: optionalString(raw.progress_updated_at) } : {}),
    progressTelemetryState,
    ...(optionalString(raw.progress_telemetry_error) ? { progressTelemetryError: optionalString(raw.progress_telemetry_error) } : {}),
    ...(optionalString(raw.remote_state) ? { remoteState: optionalString(raw.remote_state) } : {}),
    progressIndeterminate: status === 'running' && !(progressSource === 'comfyui' && comfyProgress !== undefined),
  }
}

type ProgressWorkflow = Pick<
  Workflow,
  | 'status'
  | 'progressSource'
  | 'comfyProgress'
  | 'progressTelemetryState'
  | 'progressTelemetryError'
  | 'submissionState'
  | 'remoteState'
>

const indeterminatePresentation = (workflow: ProgressWorkflow): WorkflowProgressPresentation => {
  const submitted = workflow.progressSource === 'submission'
    || ['prepared', 'submitted', 'recovered'].includes(String(workflow.submissionState || '').toLowerCase())
  const initializing = submitted && ['submitted', 'preparing', 'claimed', ''].includes(String(workflow.remoteState || '').toLowerCase())
  const unavailable = workflow.progressTelemetryState === 'unavailable'
  const summary = initializing
    ? '已提交，正在模型初始化'
    : submitted
      ? '已提交，尚未收到量化进度'
      : '执行中，尚未收到量化进度'
  const note = unavailable
    ? `进度遥测暂不可用，任务仍会继续执行${workflow.progressTelemetryError ? `：${workflow.progressTelemetryError}` : '。'}`
    : `${summary}；页面会持续刷新任务状态。`
  return {
    mode: 'indeterminate', indeterminate: true,
    heading: '执行中 / 尚无量化进度', valueLabel: '执行中',
    compactLabel: initializing ? '模型初始化' : '等待量化进度', summary, note,
  }
}

/** Build one honest presentation model shared by every task progress surface. */
export const workflowProgressPresentation = (workflow: ProgressWorkflow): WorkflowProgressPresentation => {
  if (workflow.status === 'completed') return {
    mode: 'terminal', indeterminate: false, percent: 100,
    heading: '任务已完成', valueLabel: '100%', compactLabel: '100%', summary: '生成任务已完成',
  }
  if (workflow.status === 'failed') return {
    mode: 'failed', indeterminate: false,
    heading: '任务执行失败', valueLabel: '失败', compactLabel: '失败', summary: '执行失败 · 点击查看详情',
  }
  if (workflow.status === 'queued') return {
    mode: 'queued', indeterminate: false, percent: 0,
    heading: '等待 ComfyUI 队列调度', valueLabel: '排队中', compactLabel: '排队中', summary: '等待 ComfyUI 队列调度',
  }
  if (workflow.status === 'paused') return {
    mode: 'paused', indeterminate: false,
    heading: '任务已暂停', valueLabel: '已暂停', compactLabel: '已暂停', summary: '任务已暂停',
  }
  const comfyProgress = workflow.progressSource === 'comfyui' ? boundedPercent(workflow.comfyProgress) : undefined
  if (comfyProgress !== undefined) return {
    mode: 'comfyui', indeterminate: false, percent: comfyProgress,
    heading: 'ComfyUI 当前节点采样进度', valueLabel: `${comfyProgress}%`, compactLabel: `采样 ${comfyProgress}%`,
    summary: `ComfyUI 当前节点采样 · ${comfyProgress}%`,
    note: '来自 ComfyUI 当前节点的量化采样进度，不等于整项任务完成度。',
  }
  return indeterminatePresentation(workflow)
}
