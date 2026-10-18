import type { JsonObject, ProductionRecipeEditorOptions } from './platformTypes'
import { recipeObject as object, recipeRows as rows, recipeStageKind, recipeRoutingGroups, recipeStageOption } from './productionRecipeAuthoring'
import { modeLabel, stageLabel } from './presentationLabels'

// Presentation of saved declarations only. This is not a validator, resolver or executor.
export function recipeConfigurationSteps(definition: JsonObject, options?: ProductionRecipeEditorOptions | null) {
  const stages = rows(definition.stages)
  const label = (stage: JsonObject, index: number) => `${index + 1} · ${String(stage.name ?? stageLabel(recipeStageKind(stage)) ?? stage.key)}`
  function sourcesOf(stage: JsonObject) {
    type Source = { target: string; source: JsonObject; role: string; index: number }
    function declaredSources(value: unknown, target: string, role: string, index: number): Source[] {
      const node = object(value), entry = (child: unknown, path: string) => declaredSources(child, path, role, index)
      // Descend only the declared mapping grammar, never arbitrary literal business content.
      if (node.kind === 'structured') return entry(node.template, target)
      if (node.kind === 'object') return Object.entries(object(node.fields)).flatMap(([key, child]) => entry(child, `${target}.${key}`))
      if (node.kind === 'array') return rows(node.items).flatMap((child, at) => entry(child, `${target}[${at}]`))
      if (node.kind === 'each') return [...entry(node.source, `${target}[]`), ...entry(node.item, `${target}[item]`)]
      if (node.kind === 'unit_map') return entry(node.value, `${target}[unit]`)
      return ['selected_output', 'reviewed_references'].includes(String(node.kind)) ? [{ target, source: node, role, index }] : []
    }
    const inputs = (['routing', 'variant_routing'] as const).flatMap(key => rows(object(stage[key]).routes).flatMap((route, index) =>
      Object.entries(object(route.input_sources)).flatMap(([target, source]) => declaredSources(source, target, key, index))))
    for (const key of ['assembly_output', 'assembly_timing', 'assembly_media', 'delivery_metadata']) {
      if (stage[key] !== undefined) {
        inputs.push(...declaredSources(object(stage[key]).source, key, 'mapping', -1))
        inputs.push(...declaredSources(object(object(stage[key]).mapping).node, key, 'mapping', -1))
      }
    }
    if (object(stage.shot_reference_selection).source_stage_key !== undefined) inputs.push({ target: '逐镜参考素材', source: { stage_key: object(stage.shot_reference_selection).source_stage_key }, role: 'reference', index: -1 })
    return inputs
  }
  return stages.map((stage, index) => ({
    stage, index, label: label(stage, index), kind: recipeStageKind(stage), mode: stage.mode === undefined ? '未声明执行方式' : modeLabel(stage.mode),
    expectsBinding: recipeStageOption(stage, options)?.routingModes.includes(String(stage.mode)) && recipeRoutingGroups(stage).length > 0,
    approval: options?.approvalOptions.find(row => row.value === stage.approval)?.label ?? String(stage.approval ?? '未声明审核方式'),
    // Keep unexpected saved bindings visible, even when they conflict with the stage mode/source.
    groups: (['routing', 'variant_routing'] as const).filter(key => stage[key] !== undefined).map(key => ({
      key, label: key === 'variant_routing' ? '变体' : recipeStageKind(stage) === 'bible_image_generation' ? '主图' : '执行',
      routing: object(stage[key]), routes: rows(object(stage[key]).routes),
    })),
    consumers: stages.flatMap((next, nextIndex) => sourcesOf(next).filter(input => input.source.stage_key === stage.key && stage.key !== undefined)
      .map(input => ({ label: label(next, nextIndex), target: input.target, output: input.source.output_key,
        orderValid: nextIndex > index && stages.filter(row => row.key === stage.key).length === 1, role: input.role, index: input.index }))),
  }))
}

export function recipeSourceSummary(value: unknown, options?: ProductionRecipeEditorOptions | null): string {
  const source = object(value), kind = source.kind
  const name = options?.sourceKindOptions.find(row => row.value === kind)?.label ?? String(kind ?? '来源未声明')
  if (kind === 'literal') return `${name}（原值在详情中保留）`
  if (kind === 'selected_output') return `${name} · ${String(source.stage_key ?? '步骤未声明')} → ${String(source.output_key ?? '输出未声明')}`
  if (kind === 'context_field') return `${name} · ${String(source.selector_key ?? '字段未声明')}`
  if (kind === 'reviewed_references') return `${name} · ${String(source.stage_key ?? '审核步骤未声明')}`
  return name
}

export function recipeOutputSourceSummary(value: unknown, options?: ProductionRecipeEditorOptions | null): string {
  const rule = object(value)
  if (rule.source !== undefined && rule.source_text !== undefined) return '来源冲突：同时记录上游来源与原文，未选择其中任何一项'
  if (rule.source !== undefined) return recipeSourceSummary(rule.source, options)
  if (rule.source_text === undefined) return '原文来源缺失；尚未提供内容'
  if (typeof rule.source_text !== 'string') return '原文类型无效；应为文本'
  return rule.source_text === '' ? '原文为空；尚未填写内容' : `已填写原文（${rule.source_text.length} 字符，保留原值与空白）`
}
