import type {
  AcceptanceSmokeAssociation,
  AcceptanceSmokeDeploymentRow,
  AcceptanceSmokeValidationSummary,
  AcceptanceSmokeWorkflowRow,
  WorkflowTemplateVersion,
} from './platformTypes'

export type WorkflowAcceptanceEvidenceState = 'fresh' | 'drifted' | 'missing' | 'unavailable'

export interface WorkflowAcceptancePhase {
  canLiveValidate: boolean
  canConfigureSmoke: boolean
  canStartSmoke: boolean
  canPublish: boolean
}

type WorkflowAcceptanceFacts = Pick<
  WorkflowTemplateVersion,
  'status' | 'validationState' | 'smokeTestState'
>

/**
 * Keep the browser lifecycle gate aligned with the server-owned workflow
 * state machine: draft -> validated -> published, with retired terminal.
 */
export const workflowAcceptancePhase = (
  version: WorkflowAcceptanceFacts | null | undefined,
  evidenceState: WorkflowAcceptanceEvidenceState = 'fresh',
  smokeEvidenceFresh = true,
): WorkflowAcceptancePhase => {
  const mutableAcceptance = version?.status === 'draft' || version?.status === 'validated'
  const validatedAcceptance = version?.status === 'validated'
    && version.validationState === 'passed'
    && evidenceState === 'fresh'
  return {
    canLiveValidate: mutableAcceptance,
    canConfigureSmoke: validatedAcceptance,
    canStartSmoke: validatedAcceptance,
    canPublish: validatedAcceptance && version.smokeTestState === 'passed' && smokeEvidenceFresh,
  }
}

export const workflowAcceptanceEvidenceState = (
  validation: Pick<AcceptanceSmokeValidationSummary, 'state' | 'fresh' | 'evidenceSha256'> | null | undefined,
): WorkflowAcceptanceEvidenceState => {
  if (!validation) return 'unavailable'
  const state = validation.state.trim().toLowerCase()
  if (validation.fresh && Boolean(validation.evidenceSha256) && ['current', 'fresh', 'passed', 'valid'].includes(state)) return 'fresh'
  if (['drifted', 'stale', 'target_drifted', 'configuration_drifted'].includes(state)) return 'drifted'
  if (['missing', 'not_run', 'unvalidated'].includes(state) || !validation.evidenceSha256) return 'missing'
  return 'unavailable'
}

export const workflowAssociationLabel = (
  association: Pick<
    AcceptanceSmokeAssociation,
    'level' | 'hasSmokeAttempt' | 'validatedDeploymentMatches' | 'profileMatches'
  >,
): string => {
  if (association.level === 'executed' || association.hasSmokeAttempt) return '已有精确 Workflow Smoke 执行历史'
  if (association.validatedDeploymentMatches) return '已冻结验收实例'
  if (association.profileMatches) return '仅模型合同兼容'
  return association.level || '尚无实例关联'
}

export const deploymentHealthDisplayStatus = (
  row: Pick<AcceptanceSmokeDeploymentRow, 'health'>,
): string => {
  if (!row.health) return 'unavailable'
  if (row.health.fresh) return 'fresh'
  if (!row.health.targetMatches) return 'drifted'
  return row.health.status === 'failed' ? 'failed' : 'stale'
}

export const workflowAcceptanceNeedsAttention = (
  row: Pick<AcceptanceSmokeWorkflowRow, 'validation' | 'smoke'>,
): boolean => (
  workflowAcceptanceEvidenceState(row.validation) !== 'fresh'
  || !row.smoke.fresh
  || row.smoke.latestAttempt?.resolutionRequired === true
)

export const deploymentAcceptanceNeedsAttention = (
  row: Pick<AcceptanceSmokeDeploymentRow, 'deployment' | 'health' | 'deploymentSmoke' | 'workflowAssociationSummary'>,
): boolean => {
  const applicable = row.deploymentSmoke.operations.filter((item) => item.applicable)
  return row.workflowAssociationSummary.driftedValidationCount > 0
    || row.workflowAssociationSummary.unresolvedSmokeCount > 0
    || (row.deployment.status === 'active' && (!row.health || !row.health.fresh))
    || (
      row.deploymentSmoke.applicable
      && (
        !applicable.length
        || applicable.some((item) => (
          item.freshPassedSnapshot?.fresh !== true
          || item.latestAttempt?.resolutionRequired
          || ['failed', 'uncertain'].includes(item.latestAttempt?.status || '')
        ))
      )
    )
}
