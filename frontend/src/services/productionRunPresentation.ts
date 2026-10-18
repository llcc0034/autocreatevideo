import type { ProductionRun, StageRun } from './platformTypes'
import { apiErrorSummary } from './apiErrorPresentation'

const ACTIONS: Record<string, string> = {
  execution_retry_wait: '失败调用正在等待按本轮配置自动重试；成功项不会重跑。',
  execution_retry_running: '失败调用已进入重试队列或正在重试，成功项保留。',
  execution_retry_exhausted: '自动重试次数已耗尽，等待人工处理。查看失败任务，修复原因后可确认重试失败项，已成功项保留。',
  execution_retry_requires_action: '此失败不能安全自动重试，请查看原任务并人工处理；远端结果未知时先恢复查询，不重复提交。',
  assembly_failed: '装配自动恢复已停止，等待人工处理；请核对原任务后重试失败的装配。',
  assembly_partial_failure: '部分装配失败，等待人工处理；已成功的成片保留，不会重做。',
  execution_preparation_review_required: '输入准备需要人工处理。请打开对应准备，查看失败的辅助生成、保留响应或人工填写字段；已成功内容不需要重做。',
  execution_candidates_incomplete: '候选调用未完成，请查看失败任务及重试影响；已成功的任务和产物保留。',
  waiting_external_task: '正在等待外部任务/执行器完成，运行会自动继续。',
  waiting_material_recovery: '素材候选全部明确失败，等待人工恢复。请先查看原任务，再打开“重试当前阶段”核对范围；确认前不会新增调用，有成功候选的单元不会重发。',
  execution_candidate_selection_required: '本步骤等待人工选择通用文本候选；展开当前阶段查看结果并选择，再继续 / 检查阶段。失败调用的实际返回仍可查看，不会自动重试。',
  route_selection_required: '请选择本次运行冻结的候选执行实例并确认。',
  routing_not_configured: '请到系统设置创建配方后继草稿，补齐执行实例并发布后，新建 ProductionRun。',
  unknown_cost: '请填写阶段预估费用和币种并提交授权。',
  external_call_not_authorized: '请明确允许本轮外部模型调用；如有资源提醒，请到对应执行准备中核对处理。',
  cost_currency_missing: '请补齐费用币种后重新提交授权。',
  uncertain_remote_response: '远端结果不确定；禁止普通重试，请由管理员核对 Provider 控制台后追加消歧证据。',
  actual_cost_reconciliation_required: '请由管理员核对实际费用并追加消歧证据。',
  stage_approval_required: '请核对本页冻结审核证据，再批准或拒绝当前阶段。',
  manual_stage_confirmation_required: '请先完成页面提示的人工操作，再提交阶段审批。',
  existing_storyboard_requires_manual_resolution: '项目已有剧集/分镜；系统不会自动覆盖，请改用下游配方或新建项目。',
  driver_contract_missing: '当前阶段没有正式执行适配器；请修复配方或驱动合同后新建运行。',
}

export type ProductionIssueTone = 'info' | 'warning' | 'danger'

export type ProductionIssuePresentation = {
  code: string
  summary: string
  tone: ProductionIssueTone
  isWaiting: boolean
}

export const productionIssuePresentation = (code: string, reason: unknown, status = ''): ProductionIssuePresentation => {
  const safeCode = /^[a-zA-Z][a-zA-Z0-9_.-]{0,95}$/.test(code) ? code : 'production_run_blocked'
  const isWaiting = ['waiting_external_task', 'execution_retry_wait', 'execution_retry_running'].includes(safeCode)
  const isBlocked = ['blocked', 'failed'].includes(status)
  const isAwaitingApproval = status === 'awaiting_approval'
  return {
    code: safeCode,
    summary: (safeCode === 'stage_validation_failed' && typeof reason === 'string'
      ? apiErrorSummary({response:{data:{detail:reason}}}, '阶段输入校验失败，请核对当前阶段来源配置。')
      : ['execution_retry_wait', 'execution_retry_running', 'execution_retry_exhausted', 'execution_retry_requires_action', 'assembly_failed', 'assembly_partial_failure'].includes(safeCode)
      && typeof reason === 'string' && reason ? reason : ACTIONS[safeCode]) || (isBlocked && reason
      ? '服务端阻止了当前操作；请按原因代码检查阶段配置、证据或运行状态后重试。'
      : isAwaitingApproval
        ? '当前阶段正在等待人工确认；请按原因代码处理。'
        : '当前阶段尚未完成；运行会在条件满足后继续。'),
    tone: isWaiting ? 'info' : isBlocked ? 'danger' : isAwaitingApproval || status === 'awaiting_input' ? 'warning' : 'info',
    isWaiting,
  }
}

export const productionRunCanRetry = (run: Pick<ProductionRun, 'status' | 'blockedCode'>): boolean =>
  ['blocked', 'failed'].includes(run.status) || run.blockedCode === 'execution_resource_confirmation_required'
  || run.status === 'awaiting_input' && ['waiting_material_recovery', 'execution_retry_exhausted', 'execution_retry_requires_action', 'assembly_failed', 'assembly_partial_failure'].includes(run.blockedCode)

export const actionableProductionRuns = (runs: ProductionRun[]): ProductionRun[] => runs.filter((run) => !['succeeded', 'cancelled'].includes(run.status))
export const productionRunUnstartedLabel = (run: ProductionRun): string => run.status === 'succeeded'
  ? run.planSource === 'manual' ? '本轮成片已封存' : '已完成全部阶段' : run.status === 'cancelled' ? '本轮已取消'
  : run.planSource === 'manual' ? '等待选择人工制作步骤' : '尚未进入执行阶段'
export const restorableProductionRunId = (runs: ProductionRun[]): string => {
  const actionable = actionableProductionRuns(runs)
  return actionable.length === 1 ? actionable[0].id : ''
}
export const productionRunRestoreTarget = (
  runs: ProductionRun[],
  requestedRunId: string,
  requestedView: unknown,
): string => {
  const views = Array.isArray(requestedView) ? requestedView : [requestedView]
  if (requestedRunId || views.includes('list') || views.includes('new')) return ''
  return restorableProductionRunId(runs)
}

export const productionModeLabel = (run: ProductionRun): string => run.planSource === 'manual' ? '无配方人工制作' : '按配方生产'
export const projectRunSelection = (runs: ProductionRun[], requestedId = ''): string => {
  if (requestedId) return runs.some(run => run.id === requestedId) ? requestedId : ''
  return runs.length === 1 ? runs[0]!.id : restorableProductionRunId(runs)
}
export const productionRunBelongsToProject = (run: ProductionRun, projectId: string): boolean => Boolean(projectId && run.project === projectId)
export const stageIssuePresentation = (stage: StageRun): ProductionIssuePresentation => productionIssuePresentation(stage.reasonCode || stage.status, stage.reason, stage.status)
