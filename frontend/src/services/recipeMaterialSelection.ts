export interface RecipeMaterialSelectionContext {
  artifactId: string; stageRunId: string; unitKey: string
  appearanceRole: 'primary' | 'variant'; referenceName: string; candidateNumber: number
  expectedDecisionId: string | null; selectedArtifactId: string | null
  canSelect: boolean; reason: string; dependenciesSha256: string
  dependentPreparations: { id: string; unitKey: string; referenceName: string; status: string;
    revision: number; action: 'reprepare' | 'retain' | 'blocked'; reason: string }[]
}

export const materialDependencyActionLabel = (action: 'reprepare' | 'retain' | 'blocked') =>
  ({ reprepare: '按新主图重新准备', retain: '保留原主图依据', blocked: '暂不能改选' })[action]

export function adaptRecipeMaterialSelectionContext(value: unknown, artifactId: string): RecipeMaterialSelectionContext {
  const invalid = () => { throw new Error('素材选择影响信息不完整或与候选不一致，请重新读取') }
  const record = (value: unknown): Record<string, unknown> => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid()
    return value as Record<string, unknown>
  }
  const text = (value: unknown): string => typeof value === 'string' && value.length > 0 ? value : invalid()
  const nullable = (value: unknown): string | null => value === null ? null : text(value)
  const row = record(value)
  if (row.artifact_id !== artifactId || !['primary', 'variant'].includes(String(row.appearance_role))
      || !Number.isInteger(row.candidate_number) || Number(row.candidate_number) < 1
      || typeof row.can_select !== 'boolean' || typeof row.reason !== 'string'
      || typeof row.dependencies_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(row.dependencies_sha256)
      || !Array.isArray(row.dependent_preparations) || (!row.can_select && !row.reason)) return invalid()
  const dependencies = row.dependent_preparations.map(value => {
    const item = record(value)
    if (!['awaiting_input', 'preparing', 'awaiting_review', 'blocked', 'ready', 'dispatched', 'cancelled'].includes(String(item.status))) return invalid()
    if (!['reprepare', 'retain', 'blocked'].includes(String(item.action))
        || !Number.isInteger(item.revision) || Number(item.revision) < 1
        || item.action === 'retain' && item.status !== 'dispatched'
        || item.action === 'reprepare' && ['dispatched', 'cancelled'].includes(String(item.status))) return invalid()
    return { id: text(item.id), unitKey: text(item.unit_key), referenceName: text(item.reference_name), status: text(item.status),
      revision: Number(item.revision), action: item.action as 'reprepare' | 'retain' | 'blocked', reason: text(item.reason) }
  })
  if (new Set(dependencies.map(item => item.id)).size !== dependencies.length
      || row.appearance_role === 'variant' && dependencies.length
      || row.can_select && (row.reason || dependencies.some(item => item.action === 'blocked'))) return invalid()
  return { artifactId, stageRunId: text(row.stage_run_id), unitKey: text(row.unit_key),
    appearanceRole: row.appearance_role as 'primary' | 'variant', referenceName: text(row.reference_name),
    candidateNumber: Number(row.candidate_number), expectedDecisionId: nullable(row.expected_decision_id),
    selectedArtifactId: nullable(row.selected_artifact_id), canSelect: row.can_select, reason: row.reason,
    dependentPreparations: dependencies, dependenciesSha256: row.dependencies_sha256 }
}

export function materialSelectionPayload(context: RecipeMaterialSelectionContext, artifactId: string) {
  if (!context.canSelect || context.artifactId !== artifactId) throw new Error('请重新查看此候选的改选影响')
  return { expected_decision_id: context.expectedDecisionId, expected_dependencies_sha256: context.dependenciesSha256 }
}
