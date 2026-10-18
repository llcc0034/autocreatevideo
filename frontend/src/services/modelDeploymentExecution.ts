import type { AcceptanceSmokeDeploymentRow, AcceptanceSmokeWorkflowRow } from './platformTypes'

export type ExecutionSignal = { label: string; tone: 'linked' | 'info' | 'warning' | 'danger' | 'muted'; description: string }

/** Present server-owned acceptance facts, never infer acceptance from active/published. */
export function deploymentWorkflowSignal(row: AcceptanceSmokeDeploymentRow | null, error = ''): ExecutionSignal {
  if (error) return { label: '读取失败', tone: 'danger', description: error }
  if (!row) return { label: '待读取', tone: 'muted', description: '验收数据尚未返回，不代表没有烟测记录。' }
  const summary = row.workflowAssociationSummary
  if (row.deployment.targetState !== 'current' || row.deployment.profileContractState !== 'current') {
    return { label: '合同待处理', tone: 'warning', description: '部署或模型合同无效，请查看验收详情。' }
  }
  if (summary.unresolvedSmokeCount) return {
    label: '提交待确认', tone: 'warning',
    description: `${summary.unresolvedSmokeCount} 项提交状态尚未确定，不能将历史通过视为当前验收结论。`,
  }
  if (summary.freshSmokeCount) return {
    label: `${summary.freshSmokeCount} 个工作流通过`, tone: 'linked',
    description: '仅指在此部署上具有当前有效烟测证据的工作流；其他工作流仍需分别验收。',
  }
  return { label: '暂无有效通过', tone: 'warning', description: '请查看具体工作流的未执行、失败或证据失效原因。' }
}

export function workflowExecutionSignal(row: AcceptanceSmokeWorkflowRow, deploymentId: string): ExecutionSignal {
  if (!row.association.validatedDeploymentMatches) return {
    label: row.association.hasSmokeAttempt ? '历史执行' : '仅同模型候选', tone: 'muted',
    description: '此工作流当前未校验绑定到该部署，不能作为该部署已通过的工作流。',
  }
  if (row.smoke.latestAttempt?.resolutionRequired) return {
    label: '提交待确认', tone: 'warning', description: row.smoke.message,
  }
  if (row.validation.fresh && row.smoke.fresh && row.smoke.evidenceDeploymentId === deploymentId) return {
    label: '烟测通过', tone: 'linked', description: '当前工作流版本、部署和验收证据匹配；可查看完整验收记录。',
  }
  if (row.smoke.state === 'failed') return { label: '烟测失败', tone: 'danger', description: row.smoke.message }
  if (row.smoke.state === 'not_run') return { label: '未烟测', tone: 'muted', description: row.smoke.message }
  return { label: '需重新验收', tone: 'warning', description: row.validation.fresh ? row.smoke.message : row.validation.message }
}

export function workflowExecutionUrl(row: AcceptanceSmokeWorkflowRow, deploymentId: string): string {
  return `/settings/acceptance-smoke?subject=workflow&workflow_id=${encodeURIComponent(row.workflow.id)}&deployment_id=${encodeURIComponent(deploymentId)}`
}
