import type { TaskExecutionIdentity } from '../types/index.ts'

/** Task identity is relational. Neither an old snapshot nor a title supplies it. */
export const adaptTaskExecution = (value: unknown): TaskExecutionIdentity | null => {
  if (value === null) return null
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('任务缺少实际执行关联')
  const row = value as Record<string, unknown>
  const text = (key: string): string => {
    if (typeof row[key] !== 'string' || !row[key]) throw new Error(`任务执行关联缺少 ${key}`)
    return row[key]
  }
  const number = (key: string, minimum: number): number => {
    const result = row[key]
    if (typeof result !== 'number' || !Number.isInteger(result) || result < minimum) throw new Error(`任务执行关联的 ${key} 无效`)
    return result
  }
  return {
    productionRunId: text('production_run_id'), stageRunId: text('stage_run_id'),
    stageKey: text('stage_key'), stageOrdinal: number('stage_ordinal', 0), unitKey: text('unit_key'),
    attemptId: text('attempt_id'), attemptNumber: number('attempt_number', 1),
    preparationId: text('preparation_id'), executionInstanceVersionId: text('execution_instance_version_id'),
    operation: text('operation'),
  }
}
