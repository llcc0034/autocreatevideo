import type { JsonObject } from './platformTypes'
import { adaptExecutionPage } from './executionRegistry'

const invalid = (key: string): never => { throw new Error(`组合烟测响应不完整：${key}`) }
const object = (v: unknown): JsonObject => v !== null && typeof v === 'object' && !Array.isArray(v) ? v as JsonObject : invalid('object')
const text = (r: JsonObject, key: string): string => typeof r[key] === 'string' ? r[key] as string : invalid(key)
const nullable = (r: JsonObject, key: string) => r[key] === null ? null : text(r, key)
const integer = (r: JsonObject, key: string): number => Number.isSafeInteger(r[key]) && (r[key] as number) > 0 ? r[key] as number : invalid(key)

export function adaptExecutionEvidence(value: unknown) {
  const raw = object(value), s = object(raw.summary), identity = object(s.identity)
  if (identity.scope !== 'instance_smoke') invalid('scope')
  if (!['missing', 'failed', 'fresh', 'expired'].includes(text(s, 'evidence_state'))) invalid('evidence_state')
  if (!['manual', 'profile', 'none'].includes(text(s, 'prompt_mode'))) invalid('prompt_mode')
  const summary = {
    identity: { versionId: text(identity, 'version_id'), definitionSha256: text(identity, 'definition_sha256'),
      targetFingerprint: text(identity, 'target_fingerprint'), operation: text(identity, 'operation'), mode: text(identity, 'mode') },
    readAt: text(s, 'read_at'), evidenceState: text(s, 'evidence_state'), latestCompletedAttemptId: nullable(s, 'latest_completed_attempt_id'),
    finishedAt: nullable(s, 'finished_at'), expiresAt: nullable(s, 'expires_at'), deploymentId: text(s, 'deployment_id'),
    workflowVersionId: nullable(s, 'workflow_version_id'), promptMode: text(s, 'prompt_mode'),
    promptProfileVersionId: nullable(s, 'prompt_profile_version_id'), policies: object(s.policies),
  }
  const missing = summary.evidenceState === 'missing'
  if (missing !== (summary.latestCompletedAttemptId === null) || missing !== (summary.finishedAt === null)
      || (['fresh', 'expired'].includes(summary.evidenceState) && summary.expiresAt === null)
      || (summary.promptMode === 'profile') !== (summary.promptProfileVersionId !== null)) invalid('summary identity')
  const page = adaptExecutionPage(value, (item: unknown) => {
    const r = object(item)
    const kind = text(r, 'attempt_kind')
    if (kind !== (summary.workflowVersionId === null ? 'deployment_smoke' : 'workflow_smoke')) invalid('attempt_kind')
    return { attemptId: text(r, 'attempt_id'), attemptKind: kind, status: text(r, 'status'), createdAt: text(r, 'created_at'),
      updatedAt: text(r, 'updated_at'), submissionStartedAt: nullable(r, 'submission_started_at'), finishedAt: nullable(r, 'finished_at'),
      expiresAt: nullable(r, 'evidence_expires_at'), targetSha256: text(r, 'target_sha256'), errorCode: text(r, 'error_code'),
      estimatedCost: nullable(r, 'estimated_cost'), actualCost: nullable(r, 'actual_cost'), currency: text(r, 'cost_currency'),
      preparationId: text(r, 'preparation_id'), preparationStatus: text(r, 'preparation_status'),
      preparationRevision: integer(r, 'preparation_revision'), preparationReasonCode: text(r, 'preparation_reason_code'),
      requestSha256: nullable(r, 'request_sha256') }
  })
  return { ...page, summary }
}

export type ExecutionEvidence = ReturnType<typeof adaptExecutionEvidence>

export function hasActiveSmokeAttempt(evidence: ExecutionEvidence | null): boolean {
  return evidence?.results.some(row => ['preparing', 'queued', 'submitting', 'submitted', 'running', 'cancel_requested'].includes(row.status)) ?? false
}

export function canRequestSmokeCancellation(row: ExecutionEvidence['results'][number]): boolean {
  if (row.submissionStartedAt === null) return ['preparing', 'queued'].includes(row.status)
  // Synchronous text calls cannot be withdrawn once submitted. Workflow
  // cancellation is delegated to the original queue, not a UI terminal state.
  return row.attemptKind === 'workflow_smoke' && ['submitting', 'submitted', 'running'].includes(row.status)
}
