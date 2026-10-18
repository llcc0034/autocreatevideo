import type { ComfySavedWorkflowItem } from './platformTypes'

export type SavedWorkflowModelContextKind = 'exact' | 'eligible_candidate' | 'review_candidate' | 'contract_blocked_candidate' | 'diagnostic'

export interface SavedWorkflowModelContext {
  modelProfileVersionId: string
  providerId: string
  workflowVersionIds: ReadonlySet<string>
}

const normalizedId = (value: string) => value.trim().toLocaleLowerCase('en-US')

// An ordinary matched import reuses its frozen target. An explicitly requested
// successor has its own contract and may bind a corrected model version.
export const savedWorkflowModelSelectionLocked = (
  item: Pick<ComfySavedWorkflowItem, 'matchStatus'>,
  draft: { projectorSuccessorRequested: boolean },
) => item.matchStatus === 'matched' && !draft.projectorSuccessorRequested

export const classifySavedWorkflowModelContext = (
  item: ComfySavedWorkflowItem,
  context: SavedWorkflowModelContext,
): SavedWorkflowModelContextKind => {
  const targetProfileId = normalizedId(context.modelProfileVersionId)
  const targetProviderId = normalizedId(context.providerId)
  if (!targetProfileId || !targetProviderId || normalizedId(item.sourceProviderId) !== targetProviderId) return 'diagnostic'

  // A scan error or execution snapshot is never an import candidate. Check this
  // before persisted link evidence so malformed/stale scan data fails closed.
  if (item.errorCode || item.suggestedImportKind === 'execution_snapshot') return 'diagnostic'

  const matchedProfileId = normalizedId(item.matchedModelProfileVersionId)
  if (matchedProfileId) return matchedProfileId === targetProfileId ? 'exact' : 'diagnostic'

  if (item.workflowVersionId && context.workflowVersionIds.has(normalizedId(item.workflowVersionId))) return 'exact'
  if (item.matchStatus === 'matched') return 'diagnostic'

  const candidate = item.modelProfileCandidates.find(
    (row) => normalizedId(row.modelProfileVersionId) === targetProfileId,
  )
  if (!candidate || candidate.rejectionCodes.includes('model_profile_modality_mismatch')) return 'diagnostic'

  const compatibleOutputs = item.outputCandidates.filter((output) => (
    candidate.modality === 'multimodal' || output.suggestedOutputKind === candidate.modality
  ))
  if (!compatibleOutputs.length) return 'diagnostic'

  const compatibleOutputIds = new Set(compatibleOutputs.map((output) => output.canvasNodeId))
  const contractEligible = (
    candidate.eligible
    && candidate.eligibleOutputNodeIds.some((outputId) => compatibleOutputIds.has(outputId))
  )
  if (contractEligible) {
    return candidate.liveValidateReady ? 'eligible_candidate' : 'review_candidate'
  }

  // Admission/actionability are server-owned contract facts.  The browser no
  // longer maintains a family-specific blocker whitelist that drifts whenever
  // a versioned Projector is added or changed.
  return item.actionability === 'candidate_only' || item.analysisStatus === 'blocked'
    ? 'contract_blocked_candidate'
    : 'diagnostic'
}

export const savedWorkflowBelongsToModelContext = (
  item: ComfySavedWorkflowItem,
  context: SavedWorkflowModelContext,
) => classifySavedWorkflowModelContext(item, context) !== 'diagnostic'
