import type { GeneratedArtifact, JsonObject, ProductionRun } from './platformTypes'
import { readTextCandidateDetail } from './productionTextCandidates'
import { recipeExecutionOverview } from './recipeExecutionOverview'
import { stageLabel } from './presentationLabels'
import { asRecord } from './projectVersionContents'

// Presentation of the existing project writer, not a second field-definition catalog.
export const projectVisualFields = [
  { key: 'negative_prompt', label: '全局负面提示词', path: 'project.production_settings.negative_prompt', type: '文本' },
  { key: 'color_script', label: '色彩脚本', path: 'project.production_settings.color_script', type: '文本' },
  { key: 'continuity_summary', label: '连续性摘要', path: 'project.continuity_summary', type: '文本' },
] as const

export function visualReviewEvidence(run: ProductionRun, projectId: string) {
  if (run.project !== projectId) throw Error('制作版本与当前项目不一致，不能连接字段来源。')
  const stages = run.stages?.filter(row => row.stageKey === 'visual_rules_review')
  if (!stages) throw Error('尚未读取本轮阶段，不能判断字段来源。')
  if (stages.length > 1) throw Error('本轮影视规则采用记录不唯一，请核对任务记录。')
  const stage = stages[0]
  if (!stage || stage.status !== 'succeeded') return null
  const snapshot = stage.outputSnapshot
  if (run.planSource === 'recipe' && (snapshot.formal_object_type !== 'visual_rules'
    || asRecord(snapshot.canonical).project_id !== projectId)) throw Error('影视规则快照归属不完整，不能连接来源。')
  const review = asRecord(run.planSource === 'recipe' ? snapshot.review_payload : snapshot)
  if (review.kind !== 'visual_rules_review_decision' || !Object.keys(asRecord(review.reviewed_rules)).length) {
    throw Error('本轮缺少完整字段采用凭据，不从项目现值补造来源。')
  }
  const rules = asRecord(review.reviewed_rules)
  if (run.planSource === 'recipe' && JSON.stringify(asRecord(asRecord(snapshot.canonical).rules)) !== JSON.stringify(rules)) {
    // Compare field values without relying on object key order.
    const canonical = asRecord(asRecord(snapshot.canonical).rules)
    if (Object.keys(canonical).length !== Object.keys(rules).length || Object.keys(rules).some(key => canonical[key] !== rules[key])) {
      throw Error('本轮正式规则与采用记录不一致，不能连接来源。')
    }
  }
  return { stage, review, rules }
}

export function visualLineageContract(run: ProductionRun, projectId: string, artifact: GeneratedArtifact) {
  const evidence = visualReviewEvidence(run, projectId)
  if (!evidence) throw Error('本轮尚未采用影视规则。')
  const { review, rules } = evidence
  const source = run.stages?.find(row => row.id === review.generation_stage_id && row.stageKey === 'visual_rules_generate')
  if (!source || source.status !== 'succeeded' || source.ordinal >= evidence.stage.ordinal
    || artifact.id !== review.artifact_id || artifact.sha256 !== review.artifact_sha256
    || artifact.generationAttempt !== review.attempt_id || artifact.status !== 'selected'
    || artifact.textCandidate?.preparation_id !== review.preparation_id || artifact.textCandidate?.result_sha256 !== review.result_sha256) {
    throw Error('采用记录与原生成产物不一致，不能展示字段定义。')
  }
  const detail = readTextCandidateDetail(artifact, run.id, source.id)
  if (detail.operation !== 'visual_rules.generate' || !detail.reviewContract || detail.adoptionBlockReason) {
    throw Error(detail.adoptionBlockReason || '原产物缺少冻结字段定义，不读取当前目录替代。')
  }
  if (Object.keys(rules).length !== detail.reviewContract.fields.length
    || detail.reviewContract.fields.some(field => typeof rules[field.key] !== 'string')) throw Error('采用字段与原输出定义不一致。')
  return detail.reviewContract
}

export interface FieldConsumer { stageKey: string; stageName: string; versionId: string; inputKey: string; target: string; route: string }

// Traverse only declared mapping nodes. Literal JSON containing selector-looking text is not a binding.
function selectorTargets(value: unknown, selector: string, target: string): string[] {
  const node = asRecord(value)
  if (node.kind === 'context_field') return node.selector_key === selector ? [target] : []
  if (node.kind === 'structured') return selectorTargets(node.template, selector, target)
  if (node.kind === 'object') return Object.entries(asRecord(node.fields)).flatMap(([key, child]) => selectorTargets(child, selector, `${target}[${JSON.stringify(key)}]`))
  if (node.kind === 'array') return (Array.isArray(node.items) ? node.items : []).flatMap((child, index) => selectorTargets(child, selector, `${target}[${index}]`))
  if (node.kind === 'each') return ['source', 'item'].flatMap(key => selectorTargets(node[key], selector, `${target}.${key}`))
  if (node.kind === 'unit_map') return selectorTargets(node.value, selector, `${target}.当前单元`)
  return []
}

export function fieldConsumers(run: ProductionRun, selector: string): { rows: FieldConsumer[]; note: string } {
  if (run.planSource === 'manual') return { rows: [], note: '人工制作按每次执行准备选择输入；此处没有冻结配方映射，请到本轮任务查看实际来源。' }
  const definition = asRecord(asRecord(run.executionPlan?.value?.recipe).definition)
  const overview = recipeExecutionOverview(definition)
  if (overview.error) return { rows: [], note: `未能读取本轮绑定：${overview.error}` }
  const rows: FieldConsumer[] = []
  for (const stage of definition.stages as JsonObject[]) {
    for (const group of ['routing', 'variant_routing']) {
      const routes = asRecord(stage[group]).routes
      if (!Array.isArray(routes)) continue
      const runtime = run.stages?.find(row => row.stageKey === stage.key)
      const selected = group === 'routing' ? runtime?.selectedRouteIndex : runtime?.selectedVariantRouteIndex
      routes.forEach((value, index) => {
        const route = asRecord(value)
        for (const [inputKey, source] of Object.entries(asRecord(route.input_sources))) {
          for (const target of selectorTargets(source, selector, inputKey)) rows.push({
            stageKey: String(stage.key), stageName: typeof stage.name === 'string' ? stage.name : stageLabel(String(stage.key)),
            versionId: String(route.execution_instance_version_id), inputKey, target,
            route: `${group === 'variant_routing' ? '变体' : '主'}路线 ${index + 1} · ${selected === index ? '本轮已选路线' : selected == null ? '未记录路线选择' : '本轮未选路线'}`,
          })
        }
      })
    }
  }
  return { rows, note: rows.length ? '以下为本轮冻结配方的显式绑定，不代表模型实际已读取。实际输入、转换与提示词以执行准备记录为准。'
    : '本轮冻结配方中未找到该来源的显式绑定；这不等于所有调用都未使用，请核对执行准备。' }
}

export function lineageTaskTarget(run: ProductionRun, stageId?: string, preparationId?: string) {
  return { path: '/workflows', query: { category: 'production', production_run_id: run.id,
    ...(stageId ? { stage_run_id: stageId } : {}), ...(preparationId ? { kind: 'preparation', id: preparationId } : {}) } }
}
