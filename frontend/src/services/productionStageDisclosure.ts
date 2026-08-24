import type { StageRun } from './platformTypes'

export const defaultExpandedStageId = (
  stages: StageRun[],
  currentStageKey: string,
): string => stages.find((stage) => stage.stageKey === currentStageKey)?.id || ''

export const reconcileExpandedStageId = (
  stages: StageRun[],
  currentStageKey: string,
  expandedStageId: string,
  userSelected: boolean,
): string => {
  if (userSelected && !expandedStageId) return ''
  if (userSelected && stages.some((stage) => stage.id === expandedStageId)) return expandedStageId
  return defaultExpandedStageId(stages, currentStageKey)
}
