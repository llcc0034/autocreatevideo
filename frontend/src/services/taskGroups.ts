import type { Workflow, WorkflowStatus } from '../types/index.ts'

export type TaskListFilter = 'all' | 'active' | 'completed' | 'failed'
export type TaskListGroupKind = 'legacy_batch' | 'production_run_episode'

interface TaskListEntryBase {
  key: string
  status: WorkflowStatus
  submittedAt?: string
  lastActivityAt?: string
}

export interface TaskListTaskEntry extends TaskListEntryBase {
  kind: 'task'
  task: Workflow
}

export interface TaskListGroupEntry extends TaskListEntryBase {
  kind: 'group'
  groupKind: TaskListGroupKind
  groupId: string
  productionRunId?: string
  batchId?: string
  projectId: string
  episodeId?: string
  episodeNumber?: number
  episodeTitle?: string
  tasks: Workflow[]
  currentTasks: Workflow[]
  unitCount: number
  attemptCount: number
  completedCount: number
  activeCount: number
  failedCount: number
  completionPercent: number
  indeterminate: boolean
}

export type TaskListEntry = TaskListTaskEntry | TaskListGroupEntry

const timestamp = (value?: string | null): number => {
  if (!value) return 0
  const parsed = new Date(value).getTime()
  return Number.isFinite(parsed) ? parsed : 0
}

export const workflowSubmittedTimestamp = (workflow: Workflow): number => (
  timestamp(
    workflow.queuedAt
    || workflow.createdAt
    || workflow.submissionSubmittedAt
    || workflow.submissionPreparedAt,
  )
)

export const workflowActivityTimestamp = (workflow: Workflow): number => Math.max(
  timestamp(workflow.finishedAt),
  timestamp(workflow.startedAt),
  timestamp(workflow.queuedAt),
  timestamp(workflow.createdAt),
  timestamp(workflow.submissionSubmittedAt),
  timestamp(workflow.submissionPreparedAt),
)

const isoAt = (workflows: Workflow[], resolver: (workflow: Workflow) => number, mode: 'min' | 'max'): string | undefined => {
  const values = workflows.map(resolver).filter((value) => value > 0)
  if (!values.length) return undefined
  return new Date(mode === 'min' ? Math.min(...values) : Math.max(...values)).toISOString()
}

export const taskListGroupKey = (workflow: Workflow): string => {
  if (workflow.batchId) return `legacy-batch:${workflow.batchId}`
  if (workflow.productionRunId && workflow.episodeId) {
    return `production-run:${workflow.productionRunId}:episode:${workflow.episodeId}`
  }
  return ''
}

const taskUnitKey = (workflow: Workflow, groupKind: TaskListGroupKind): string => {
  if (groupKind === 'legacy_batch') {
    return workflow.batchIndex && workflow.batchIndex > 0
      ? `batch-index:${workflow.batchIndex}`
      : `task:${workflow.id}`
  }
  if (!workflow.shotId) return `task:${workflow.id}`
  const variant = workflow.productionVariantNumber && workflow.productionVariantNumber > 0
    ? workflow.productionVariantNumber
    : 1
  return `shot:${workflow.shotId}:variant:${variant}`
}

export const isWorkflowActive = (workflow: Workflow): boolean => (
  ['queued', 'running', 'paused'].includes(workflow.status)
  && !(workflow.isVirtualProductionUnit && workflow.status === 'paused')
)

const currentGroupTasks = (tasks: Workflow[], groupKind: TaskListGroupKind): Workflow[] => {
  const current = new Map<string, Workflow>()
  for (const task of tasks) {
    const key = taskUnitKey(task, groupKind)
    const previous = current.get(key)
    const order = workflowActivityTimestamp(task) - workflowActivityTimestamp(previous || task)
    if (!previous || order > 0 || (order === 0 && task.id.localeCompare(previous.id) > 0)) {
      current.set(key, task)
    }
  }
  return [...current.values()]
}

const taskDisplayOrder = (left: Workflow, right: Workflow, groupKind: TaskListGroupKind): number => {
  if (groupKind === 'legacy_batch') {
    const slotOrder = Number(left.batchIndex || 0) - Number(right.batchIndex || 0)
    if (slotOrder) return slotOrder
  } else {
    const shotOrder = Number(left.shotNumber || 0) - Number(right.shotNumber || 0)
    if (shotOrder) return shotOrder
    const variantOrder = Number(left.productionVariantNumber || 1) - Number(right.productionVariantNumber || 1)
    if (variantOrder) return variantOrder
  }
  const activityOrder = workflowActivityTimestamp(right) - workflowActivityTimestamp(left)
  return activityOrder || left.id.localeCompare(right.id)
}

const groupStatus = (
  current: Workflow[],
  activeCount: number,
  failedCount: number,
  completedCount: number,
  unitCount: number,
): WorkflowStatus => {
  if (activeCount) {
    if (current.some((task) => task.status === 'running')) return 'running'
    if (current.some((task) => task.status === 'queued')) return 'queued'
    return 'paused'
  }
  if (failedCount) return 'failed'
  if (unitCount > 0 && completedCount === unitCount) return 'completed'
  return 'paused'
}

const buildGroupEntry = (key: string, tasks: Workflow[]): TaskListGroupEntry => {
  const anchor = tasks[0]!
  const groupKind: TaskListGroupKind = anchor.batchId ? 'legacy_batch' : 'production_run_episode'
  const orderedTasks = tasks.slice().sort((left, right) => taskDisplayOrder(left, right, groupKind))
  const currentTasks = currentGroupTasks(orderedTasks, groupKind)
    .sort((left, right) => taskDisplayOrder(left, right, groupKind))
  const expectedLegacyUnits = groupKind === 'legacy_batch'
    ? Math.max(...tasks.map((task) => Number(task.batchSize || 0)), 0)
    : 0
  const unitCount = Math.max(expectedLegacyUnits, currentTasks.length)
  const completedCount = currentTasks.filter((task) => task.status === 'completed').length
  const activeCount = currentTasks.filter(isWorkflowActive).length
  const failedCount = currentTasks.filter((task) => task.status === 'failed').length
  const status = groupStatus(currentTasks, activeCount, failedCount, completedCount, unitCount)
  const groupId = groupKind === 'legacy_batch'
    ? String(anchor.batchId)
    : `${anchor.productionRunId}:${anchor.episodeId}`
  return {
    kind: 'group', key, groupKind, groupId, status,
    ...(anchor.productionRunId ? { productionRunId: anchor.productionRunId } : {}),
    ...(anchor.batchId ? { batchId: anchor.batchId } : {}),
    projectId: anchor.projectId,
    episodeId: anchor.episodeId,
    episodeNumber: anchor.episodeNumber,
    episodeTitle: anchor.episodeTitle,
    tasks: orderedTasks,
    currentTasks,
    unitCount,
    attemptCount: tasks.filter((task) => !task.isVirtualProductionUnit).length,
    completedCount,
    activeCount,
    failedCount,
    completionPercent: unitCount ? Math.round(completedCount / unitCount * 100) : 0,
    indeterminate: activeCount > 0,
    submittedAt: isoAt(tasks, workflowSubmittedTimestamp, 'min'),
    lastActivityAt: isoAt(tasks, workflowActivityTimestamp, 'max'),
  }
}

export const buildTaskListEntries = (workflows: Workflow[]): TaskListEntry[] => {
  const groups = new Map<string, Workflow[]>()
  const entries: TaskListEntry[] = []
  for (const workflow of workflows) {
    const groupKey = taskListGroupKey(workflow)
    if (!groupKey) {
      entries.push({
        kind: 'task', key: `task:${workflow.id}`, task: workflow,
        status: workflow.status,
        submittedAt: isoAt([workflow], workflowSubmittedTimestamp, 'min'),
        lastActivityAt: isoAt([workflow], workflowActivityTimestamp, 'max'),
      })
      continue
    }
    const tasks = groups.get(groupKey) || []
    tasks.push(workflow)
    groups.set(groupKey, tasks)
  }
  for (const [key, tasks] of groups) entries.push(buildGroupEntry(key, tasks))
  return entries.sort((left, right) => {
    const activityOrder = timestamp(right.lastActivityAt) - timestamp(left.lastActivityAt)
    return activityOrder || right.key.localeCompare(left.key)
  })
}

export const taskListEntryIsActive = (entry: TaskListEntry): boolean => (
  entry.kind === 'group' ? entry.activeCount > 0 : isWorkflowActive(entry.task)
)

export const taskListEntryMatchesFilter = (entry: TaskListEntry, filter: TaskListFilter): boolean => (
  filter === 'all'
  || (filter === 'active' ? taskListEntryIsActive(entry) : entry.status === filter)
)

export const taskListFilterCounts = (entries: TaskListEntry[]): Record<TaskListFilter, number> => ({
  all: entries.length,
  active: entries.filter(taskListEntryIsActive).length,
  completed: entries.filter((entry) => entry.status === 'completed').length,
  failed: entries.filter((entry) => entry.status === 'failed').length,
})
