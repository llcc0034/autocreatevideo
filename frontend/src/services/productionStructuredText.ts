import type { GeneratedArtifact, JsonObject, ProductionRun, StageRun } from './platformTypes'
import { stageLabel } from './presentationLabels'

const record = (value: unknown): JsonObject => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('通用文本合同缺少完整对象，请重新读取')
  return value as JsonObject
}
const text = (value: unknown): string => {
  if (typeof value !== 'string' || !value.trim()) throw new Error('通用文本合同缺少必要标识，请重新读取')
  return value
}

// Display metadata comes from this Run's plan, not the current recipe catalog.
export function structuredTextStep(run: ProductionRun, stage: StageRun) {
  if (run.planSource !== 'recipe') return null
  const rows = record(record(run.executionPlan?.value.recipe).definition).stages
  if (!Array.isArray(rows)) throw new Error('冻结配方缺少步骤清单')
  const row = record(rows[stage.ordinal - 1])
  if (row.key !== stage.stageKey) throw new Error('冻结步骤顺序或身份与运行不一致')
  if (row.kind !== 'structured_text') return null
  const evaluator = record(row.routing).evaluator
  if (evaluator !== null && evaluator !== 'first_succeeded') throw new Error('冻结候选选择方式不受支持')
  return { name: text(row.name), key: stage.stageKey, evaluator }
}

export function runStageLabel(run: ProductionRun, stage: StageRun): string {
  try { return structuredTextStep(run, stage)?.name ?? stageLabel(stage.stageKey) }
  catch { return `${stage.stageKey}（冻结步骤信息读取失败）` }
}

export function readStructuredTextDetail(artifact: GeneratedArtifact, runId: string, stageId: string) {
  if (artifact.productionRun !== runId || artifact.stageRun !== stageId || artifact.role !== 'structured_text_candidate'
    || !['available', 'selected'].includes(artifact.status)) throw new Error('候选归属、职责或状态已变化，请刷新清单')
  const detail = record(artifact.textCandidate), candidate = record(detail.candidate)
  const output = record(detail.output_contract), structure = record(output.data_structure)
  if (detail.operation !== 'text.structured' || candidate.schema_version !== 1 || !Object.prototype.hasOwnProperty.call(candidate, 'value')
    || candidate.output_version_id !== text(output.id) || candidate.output_contract_sha256 !== text(output.sha256)
    || candidate.structure_version_id !== text(structure.id)) throw new Error('候选结果与原输出结构不一致')
  const defaults = candidate.defaults, warnings = detail.structure_warnings
  if (!Array.isArray(defaults) || !Array.isArray(warnings)) throw new Error('候选默认值或提醒信息不完整')
  return { value: candidate.value, schema: record(detail.json_schema), output, preparationId: text(detail.preparation_id),
    defaults: defaults.map(item => {
      const row = record(item)
      if (!Object.prototype.hasOwnProperty.call(row, 'value') || row.structure_version_id !== structure.id) throw new Error('默认值缺少实际来源')
      return { path: text(row.path), value: row.value, structureVersionId: text(row.structure_version_id) }
    }),
    warnings: warnings.map(item => { const row = record(item); return { path: text(row.path), message: text(row.message) } }) }
}

export function candidateSelectionLabel(stage: StageRun, artifact: GeneratedArtifact): string {
  const fact = [...stage.artifactSelectionDecisions].filter(row => row.variantGroup === artifact.variantGroup)
    .sort((a, b) => b.revision - a.revision)[0]
  if (!fact || fact.artifact !== artifact.id) return artifact.status === 'selected' ? '已选择；选择记录尚未读取，请刷新运行' : '尚未选择'
  if (fact.selectionMode === 'automatic' && fact.selectedBy === null) return '配方自动选择（不是人工审核）'
  if (fact.selectionMode === 'manual' && fact.selectedBy) return '人工选择（不是剧本审核）'
  return '选择记录不完整，请刷新运行'
}
