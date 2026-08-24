import type { HostCapabilitySnapshot, RecommendationSnapshot, RuntimeReadiness } from './platformTypes'

export interface RuntimeEvidenceState {
  snapshotCurrent: boolean
  readinessCurrent: boolean
  recommendationCurrent: boolean
}

export const runtimeEvidenceState = (
  selectedHostId: string,
  snapshot: HostCapabilitySnapshot | null,
  readiness: RuntimeReadiness | null,
  recommendation: RecommendationSnapshot | null,
  now = Date.now(),
): RuntimeEvidenceState => {
  const expiresAt = snapshot ? Date.parse(snapshot.expiresAt) : Number.NaN
  const snapshotCurrent = Boolean(
    selectedHostId
    && snapshot
    && snapshot.host === selectedHostId
    && Number.isFinite(expiresAt)
    && expiresAt > now,
  )
  const readinessCurrent = Boolean(
    snapshotCurrent
    && readiness
    && readiness.snapshotId === snapshot?.id,
  )
  const recommendationCurrent = Boolean(
    readinessCurrent
    && recommendation
    && recommendation.hostSnapshot === snapshot?.id,
  )
  return { snapshotCurrent, readinessCurrent, recommendationCurrent }
}
