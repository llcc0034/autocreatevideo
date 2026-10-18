import type { ExecutionPreparationDetail } from './executionPreparation'
import type { JsonObject, ProductionRun } from './platformTypes'
import type { ManualActionOptions } from './productionManual'

const object = (value: unknown): JsonObject => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('原准备缺少明确制作范围，请重新读取')
  return value as JsonObject
}

export function manualPrimaryRetryBlockedReason(prep: ExecutionPreparationDetail): string {
  const task = prep.presentation?.kind === 'primary' ? prep.presentation.task : null
  if (task?.status === 'unknown' || task?.errorCode === 'uncertain_remote_response') {
    return '远端结果不确定，不能创建普通重试；请先核对原调用状态。'
  }
  if (task?.errorCode === 'actual_cost_reconciliation_required') {
    return '原调用实际费用尚未核对，不能创建普通重试；请先核对原调用证据。'
  }
  return ''
}

export function buildManualPrimaryRetry(run: ProductionRun, prep: ExecutionPreparationDetail,
  options: ManualActionOptions, idempotencyKey: string): JsonObject {
  const blockedReason = manualPrimaryRetryBlockedReason(prep)
  if (blockedReason) throw new Error(blockedReason)
  const stage = run.stages?.find(row => row.id === prep.stageRun)
  const requested = object(object(prep.intent.request).requested), scope = object(requested.scope)
  const action = options.actions.find(row => row.stageKey === stage?.stageKey && row.action === 'retry')
  if (run.planSource !== 'manual' || run.id !== options.runId || !run.executionPlan
    || run.executionPlan.sha256 !== options.runPlanSha256 || requested.run_plan_sha256 !== options.runPlanSha256
    || !stage || requested.stage_key !== stage.stageKey || !action
    || !action.scopeKinds.includes(scope.kind as never) || typeof scope.id !== 'string' || !scope.id
    || !['running', 'blocked', 'failed', 'awaiting_approval'].includes(stage.status)
    || !['running', 'blocked', 'failed', 'awaiting_input', 'awaiting_approval'].includes(run.status)) {
    throw new Error('原运行、阶段或重试范围已变化，请重新读取；不会新建配方')
  }
  if (prep.purpose !== 'production' || prep.presentation?.kind !== 'primary'
    || prep.presentation.task?.status !== 'failed' || !prep.presentation.task.id
    || !prep.requestFrozenAt || prep.status === 'cancelled' || !Number.isSafeInteger(prep.revision) || prep.revision < 1
    || !idempotencyKey.trim()) throw new Error('只能重试已明确失败且保留原请求的主调用')
  return { action: 'retry', stage_key: stage.stageKey, unit_key: prep.unitKey,
    scope: { kind: scope.kind, id: scope.id }, expected_run_plan_sha256: options.runPlanSha256,
    expected_selection_revision: null, idempotency_key: idempotencyKey, payload: {
      preparation_id: prep.id, expected_preparation_revision: prep.revision,
      expected_attempt_id: prep.presentation.task.id,
    } }
}

export function verifyManualPrimaryRetryReceipt(receipt: JsonObject, request: JsonObject): void {
  if (receipt.action !== 'retry' || receipt.result_kind !== 'preparation' || !receipt.id
    || receipt.target_id !== object(request.payload).preparation_id) {
    throw new Error('未返回原准备的有效重试回执，请核对同一请求；不要新建重试请求')
  }
}
