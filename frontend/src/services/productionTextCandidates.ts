import type { GeneratedArtifact, JsonObject, StageRun } from './platformTypes'
import { readVisualRuleReviewContract } from './visualRuleReviewContract'

const record = (value: unknown): JsonObject => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('候选详情缺少完整对象，请重新读取')
  return value as JsonObject
}
const text = (value: unknown): string => {
  if (typeof value !== 'string' || !value.trim()) throw new Error('候选详情缺少必需文本，请核对原任务')
  return value
}
export const textCandidateSource = (stage: StageRun, stages: StageRun[]) => {
  const reference = record(stage.inputSnapshot.candidate_source)
  const source = stages.find(item => item.id === reference.stage_id)
  if (!source || source.stageKey !== reference.stage_key || source.outputSnapshotSha256 !== reference.output_snapshot_sha256
      || source.status !== 'succeeded' || source.ordinal >= stage.ordinal) throw new Error('审核来源与本轮生成阶段不一致，请刷新运行')
  return source
}
export const readTextCandidateDetail = (artifact: GeneratedArtifact, runId: string, sourceId: string) => {
  if (artifact.productionRun !== runId || artifact.stageRun !== sourceId || !['available', 'selected'].includes(artifact.status)
      || !/^[0-9a-f]{64}$/.test(artifact.sha256) || !/^[0-9a-f]{64}$/.test(artifact.manifestMetadataSha256)) throw new Error('候选归属、状态或摘要已变化，请刷新清单')
  const detail = record(artifact.textCandidate)
  const operation = text(detail.operation)
  if (!Object.prototype.hasOwnProperty.call(detail, 'candidate') || detail.candidate === undefined) throw new Error('候选详情缺少字段值，请重新读取')
  if (!Array.isArray(detail.warnings) || detail.warnings.some(value => typeof value !== 'string')) throw new Error('候选提示信息不完整')
  const reviewContract = operation === 'visual_rules.generate' && detail.review_contract != null
    ? readVisualRuleReviewContract(detail.review_contract) : null
  if (operation === 'visual_rules.generate') {
    const candidate = record(detail.candidate)
    if (reviewContract) {
      if (Object.keys(candidate).length !== reviewContract.fields.length) throw new Error('候选字段与冻结审核合同不一致')
      for (const field of reviewContract.fields) {
        if (typeof candidate[field.key] !== 'string' || !field.allow_blank && !String(candidate[field.key]).trim()) throw new Error('候选字段与冻结审核合同不一致')
      }
    } else if (!detail.adoption_block_reason) throw new Error('候选缺少冻结审核合同，不能补造可采用字段')
  } else if (operation === 'script.generate' || operation === 'script.structure') {
    text(detail.text)
  } else throw new Error('此产物不属于剧本或影视规则候选')
  const advice = detail.quality_advisories == null ? null : record(detail.quality_advisories)
  if (advice) {
    if (advice.schema_version !== 1) throw new Error('候选建议合同版本不受支持')
    for (const key of ['negative_prompt_max_chars', 'color_script_max_chars', 'continuity_summary_max_chars']) {
      const maximum = advice[key]
      if (maximum !== undefined && (typeof maximum !== 'number' || !Number.isSafeInteger(maximum) || maximum <= 0)) throw new Error('候选建议长度无效')
    }
  }
  const common = { reviewContract, text: typeof detail.text === 'string' ? detail.text : null, warnings: detail.warnings as string[], qualityAdvisories: advice,
    jsonSchema: detail.json_schema == null ? null : record(detail.json_schema),
    adoptionBlockReason: typeof detail.adoption_block_reason === 'string' ? detail.adoption_block_reason : '' }
  return operation === 'visual_rules.generate'
    ? { ...common, operation: 'visual_rules.generate' as const, candidate: record(detail.candidate) }
    : { ...common, operation: operation as 'script.generate' | 'script.structure', candidate: detail.candidate }
}
