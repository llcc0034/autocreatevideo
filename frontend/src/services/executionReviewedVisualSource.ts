import type { JsonObject } from './platformTypes'
import { readVisualRuleReviewContract, type VisualRuleReviewContract, type VisualRuleReviewField } from './visualRuleReviewContract'

export interface ReviewedVisualSource { label: string; content: string; revision: number; decisionId: string; attemptId: string; artifactId: string; field: string; outputVersion: VisualRuleReviewContract['output']; fieldContract: VisualRuleReviewField }
export function readReviewedVisualSource(value: JsonObject, runId: string, stageKey: string, projectId: string, selector: string): ReviewedVisualSource {
  const object = (v: unknown): JsonObject => {
    if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('审核来源记录不完整')
    return v as JsonObject
  }
  const text = (v: unknown) => { if (typeof v !== 'string' || !v) throw new Error('审核来源身份缺失'); return v }
  const digest = (v: unknown) => { if (typeof v !== 'string' || !/^[a-f0-9]{64}$/.test(v)) throw new Error('审核来源摘要无效') }
  if (value.run_id !== runId || value.project_id !== projectId || value.stage_key !== stageKey || !Array.isArray(value.sources)) throw new Error('审核来源与当前运行、项目或步骤不一致')
  digest(value.run_plan_sha256)
  const matches = value.sources.map(object).filter(row => row.selector_key === selector)
  if (matches.length !== 1) throw new Error('没有唯一匹配的已审核输出字段')
  const row = matches[0]!, review = object(row.review), output = object(row.output)
  const contract = readVisualRuleReviewContract(value.review_contract)
  const fieldContract = contract.fields.find(field => field.source_selector === selector)
  const identity = object(output.business_output)
  if (!fieldContract || fieldContract.key !== output.field || fieldContract.label !== row.label
    || Object.entries(contract.output).some(([key, value]) => identity[key] !== value)) throw new Error('审核来源与原生产输出版本不一致')
  digest(row.content_sha256); digest(review.snapshot_sha256); digest(output.reviewed_rules_sha256)
  if (typeof row.content !== 'string' || !Number.isSafeInteger(review.revision) || Number(review.revision) < 1
    || output.protocol !== 'reviewed_visual_rules/v1' || output.schema_version !== 3 || selector !== `reviewed_visual_rules.${output.field}`) throw new Error('审核来源类型或输出协议不一致')
  return { label: text(row.label), content: row.content, revision: Number(review.revision), decisionId: text(review.decision_id),
    attemptId: text(output.attempt_id), artifactId: text(output.artifact_id), field: text(output.field), outputVersion: contract.output, fieldContract }
}
