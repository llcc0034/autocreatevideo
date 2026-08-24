import type { ProductionRun, StageRun } from './platformTypes'

const ACTIONS: Record<string, string> = {
  route_selection_required: '请选择本次运行冻结的候选路由并确认。',
  routing_not_configured: '请到系统设置补齐已发布配方的路由后，新建 ProductionRun。',
  unknown_cost: '请填写阶段预估费用和币种并提交授权。',
  budget_limit_exceeded: '请核对预算影响，调整运行预算上限后重新授权。',
  external_budget_missing: '外部调用必须先设置本次运行预算。',
  external_call_not_authorized: '请明确允许本阶段外发并设置预算，或改用本地路由。',
  cost_currency_missing: '请补齐费用币种后重新提交授权。',
  uncertain_remote_response: '远端结果不确定；禁止普通重试，请由管理员核对 Provider 控制台后追加消歧证据。',
  actual_cost_reconciliation_required: '请由管理员核对实际费用并追加消歧证据。',
  stage_approval_required: '请核对本页冻结审核证据，再批准或拒绝当前阶段。',
  manual_stage_confirmation_required: '请先完成页面提示的人工操作，再提交阶段审批。',
  existing_storyboard_requires_manual_resolution: '项目已有剧集/分镜；系统不会自动覆盖，请改用下游配方或新建项目。',
  driver_contract_missing: '当前阶段没有正式执行适配器；请修复配方或驱动合同后新建运行。',
}

export const productionIssuePresentation = (code: string, reason: unknown): { code: string; summary: string } => {
  const safeCode = /^[a-zA-Z][a-zA-Z0-9_.-]{0,95}$/.test(code) ? code : 'production_run_blocked'
  return {
    code: safeCode,
    summary: ACTIONS[safeCode] || (reason ? '服务端阻止了当前操作；请按原因代码检查阶段配置、证据或运行状态后重试。' : '当前阶段尚未完成；请刷新并按原因代码处理。'),
  }
}

export const actionableProductionRuns = (runs: ProductionRun[]): ProductionRun[] => runs.filter((run) => !['succeeded', 'cancelled'].includes(run.status))
export const restorableProductionRunId = (runs: ProductionRun[]): string => {
  const actionable = actionableProductionRuns(runs)
  return actionable.length === 1 ? actionable[0].id : ''
}
export const productionRunBelongsToProject = (run: ProductionRun, projectId: string): boolean => Boolean(projectId && run.project === projectId)
export const stageIssuePresentation = (stage: StageRun): { code: string; summary: string } => productionIssuePresentation(stage.reasonCode || stage.status, stage.reason)
