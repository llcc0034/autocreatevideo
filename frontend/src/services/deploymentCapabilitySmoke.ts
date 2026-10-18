import type { AcceptanceSmokeDeploymentOperation, AcceptanceSmokeDeploymentRow } from './platformTypes'
import type { ExecutionSignal } from './modelDeploymentExecution'

const signals: Record<string, ExecutionSignal> = {
  fresh: { label: '通过且有效', tone: 'linked', description: '此能力的烟测证据与当前部署合同一致，且尚未到期。' },
  expired: { label: '已过期', tone: 'warning', description: '历史烟测通过，但证据已到期，需要重新烟测。' },
  drifted: { label: '配置已变更', tone: 'warning', description: '证据对应的部署配置与当前配置不一致，需要重新烟测。' },
  failed: { label: '烟测失败', tone: 'danger', description: '当前没有此能力的有效通过证据，请查看最近任务。' },
  missing: { label: '未烟测', tone: 'muted', description: '此能力尚无烟测任务或证据。' },
  unverified: { label: '暂无有效证据', tone: 'warning', description: '已有烟测记录，但尚不能确认此能力通过且有效。' },
  unavailable: { label: '无法核验', tone: 'warning', description: '当前模型或部署合同不可核验，请查看验收详情。' },
  uncertain: { label: '提交待确认', tone: 'warning', description: '请先处理此能力的不确定提交，历史通过不能替代确认。' },
  not_applicable: { label: '当前不适用', tone: 'muted', description: '当前合同不支持此项直接烟测；已有记录仅供追溯。' },
}

export function deploymentCapabilitySignal(row: AcceptanceSmokeDeploymentRow, operation: AcceptanceSmokeDeploymentOperation, now: number): ExecutionSignal {
  if (row.deployment.targetState !== 'current' || row.deployment.profileContractState !== 'current') return signals.unavailable!
  if (operation.latestAttempt?.resolutionRequired) return signals.uncertain!
  if (operation.evidenceState === 'fresh') {
    const snapshot = operation.freshPassedSnapshot
    if (!snapshot?.fresh || !snapshot.targetMatches || snapshot.status !== 'passed' || !snapshot.operations.includes(operation.operation)
      || !Number.isFinite(Date.parse(snapshot.expiresAt))) return signals.unavailable!
    // Only invalidate a server-approved pass when its deadline elapses; never
    // turn historical success into a pass in the browser or invent a local TTL.
    if (Date.parse(snapshot.expiresAt) <= now) return signals.expired!
  }
  return signals[operation.evidenceState] || signals.unavailable!
}
