import type { JsonObject, ProductionRecipeEditorOptions } from './platformTypes'
import type { ExecutionRequestFormContract } from './executionRequestForm'
import type { ExecutionInput } from './executionRegistry'
import { mappingSourceChoices } from './recipeStructureMapping'
import type { MediaAppearanceInputOptions } from './mediaAppearanceInputs'

export function recipeContextFieldOptions(contract: ExecutionRequestFormContract, input: ExecutionInput, priorStages: string[]) {
  const source = contract.sourceConstraints[input.key]
  if (!source?.kinds.includes('context_field')) return []
  return mappingSourceChoices(contract.contextFields, {}, input.semantic_contract.allowed_scope_kinds, priorStages).filter(field => field.kind === input.kind
    && (source.selectors === null || source.selectors.includes(field.selector_key)))
}

export function recipeSourceKindOptions(contract: ExecutionRequestFormContract, input: ExecutionInput,
  options: ProductionRecipeEditorOptions, priorStages: string[], material?: MediaAppearanceInputOptions) {
  const materialKinds = material && priorStages.includes('bible_review') ? options.sourceKindOptions.filter(row =>
    row.value === 'material_primary' && material.primary_inputs.some(use=>use.input_key===input.key)
      && Boolean(input.media_source_policy?.allowed_sources.includes('asset_library'))
      && (input.media_source_policy?.default_source==='asset_library'||input.media_source_policy?.allow_override)
      && input.min_items<=1 && input.max_items>=1 && input.semantic_contract.allowed_scope_kinds.includes('request')
      && input.semantic_contract.paired_with_input_key===null
    || row.value === 'material_difference' && material.difference_sources.some(use=>use.kind==='logical_input'&&use.key===input.key)) : []
  if (!['text', 'json'].includes(input.kind)) return [...materialKinds, ...options.sourceKindOptions.filter(row =>
    row.value === 'reviewed_references' || row.value === 'media_selection' && Boolean(input.media_source_policy)
    && input.semantic_contract.allowed_scope_kinds.includes('request') && input.semantic_contract.paired_with_input_key === null)]
  const kinds = contract.sourceConstraints[input.key]?.kinds || []
  return [...materialKinds, ...options.sourceKindOptions.filter(row => {
    if (!kinds.includes(row.value)) return false
    if (row.value === 'literal') return true
    if (row.value === 'structured') return input.kind === 'json'
    if (row.value === 'project_snapshot') return input.kind === 'text' && input.semantic_contract.allowed_scope_kinds.includes('project')
    if (row.value === 'context_field') return recipeContextFieldOptions(contract, input, priorStages).length > 0
    if (row.value === 'selected_output') return options.textOutputOptions.some(output => output.kind === input.kind
      && priorStages.includes(output.stageKey) && input.semantic_contract.allowed_scope_kinds.includes(output.scopeKind))
    return false
  })]
}

export function recipeInputSourceIssue(contract: ExecutionRequestFormContract, input: ExecutionInput,
  options: ProductionRecipeEditorOptions, priorStages: string[], selectedKind: string, material?: MediaAppearanceInputOptions) {
  const choices = recipeSourceKindOptions(contract, input, options, priorStages, material)
  if (selectedKind && !choices.some(row => row.value === selectedKind)) return '保存的来源不满足此精确输入合同或当前阶段范围；请重新配置，不会自动替换或删除。'
  if (choices.length) return ''
  return '当前配方没有适用于此输入合同和阶段范围的来源；请核对实例合同与上游审核阶段，可保留草稿，暂勿发布。'
}

export const recipeObject = (v: unknown): JsonObject => v && typeof v === 'object' && !Array.isArray(v) ? v as JsonObject : {}
export const recipeRows = (v: unknown): JsonObject[] => Array.isArray(v) ? v.map(recipeObject) : []
export const recipeStageKind = (stage: JsonObject): string => String(stage.kind ?? stage.key ?? '')
export type RecipeRoutingKey = 'routing' | 'variant_routing'
export function recipeMaterialSource(stage: JsonObject, role: 'primary' | 'variant'): string {
  return stage.material_sources === undefined ? 'generate' : String(recipeObject(stage.material_sources)[role] ?? '')
}
export function withRecipeMaterialSource(stage: JsonObject, role: 'primary' | 'variant', source: string,
    options: ProductionRecipeEditorOptions): JsonObject {
  if (recipeStageKind(stage) !== 'bible_image_generation' || stage.mode !== 'existing_pipeline'
      || !options.materialSourceOptions.some(row => row.value === source)) throw new Error('请选择当前素材阶段已接通的成图来源')
  const next: JsonObject = {...stage, material_sources: {primary: recipeMaterialSource(stage, 'primary'),
    variant: recipeMaterialSource(stage, 'variant'), [role]: source}}
  if (source !== 'generate') delete next[role === 'primary' ? 'routing' : 'variant_routing']
  // A source change discards that role's old selector, not the other role's policy.
  if (next.material_selection !== undefined) {
    const policies = {...recipeObject(next.material_selection)}
    delete policies[role]
    if (Object.keys(policies).length) next.material_selection = policies
    else delete next.material_selection
  }
  return next
}
export function recipeMaterialSelectionPolicy(stage: JsonObject, role: 'primary' | 'variant'): string {
  return String(recipeObject(stage.material_selection)[role] ?? 'manual')
}
export function withRecipeMaterialSelectionPolicy(stage: JsonObject, role: 'primary' | 'variant', policy: string,
    options: ProductionRecipeEditorOptions): JsonObject {
  if (recipeStageKind(stage) !== 'bible_image_generation' || stage.mode !== 'existing_pipeline'
      || !['existing','upload'].includes(recipeMaterialSource(stage, role))
      || !options.materialSelectionOptions?.some(row => row.value === policy)) throw new Error('请选择已接通的成图选材策略')
  return {...stage, material_selection: {...recipeObject(stage.material_selection), [role]: policy}}
}
export function recipeRoutingGroups(stage: JsonObject): {key:RecipeRoutingKey;label:string}[] {
  return recipeStageKind(stage)==='bible_image_generation'
    ? ([{key:'routing',label:'主图'},{key:'variant_routing',label:'变体'}] as {key:RecipeRoutingKey;label:string}[])
      .filter(group => recipeMaterialSource(stage, group.key==='routing'?'primary':'variant') === 'generate')
    : [{key:'routing',label:'执行'}]
}
export function recipeSourceCatalogDefinition(definition: JsonObject): JsonObject {
  // Only output identity/order affects the catalog; typing input values must not reload it.
  return {...definition, stages: recipeRows(definition.stages).map(stage => {
    const {field_review: _review, ...declaration} = stage
    return {...declaration, ...Object.fromEntries(recipeRoutingGroups(stage).filter(group=>stage[group.key]!==undefined).map(group=>[group.key, {
      routes: recipeRows(recipeObject(stage[group.key]).routes).map(route=>({
        execution_instance_version_id: route.execution_instance_version_id,
        expected_definition_sha256: route.expected_definition_sha256,
      })),
    }]))}
  })}
}
export function recipeStageOption(stage: JsonObject, options?: ProductionRecipeEditorOptions | null) {
  return options?.stageOptions.find(row => row.value === recipeStageKind(stage))
}
export function recipeShotReferenceErrors(stage: JsonObject, prior: JsonObject[], options: ProductionRecipeEditorOptions): string[] {
  if (stage.shot_reference_selection === undefined) return []
  const rule = recipeObject(stage.shot_reference_selection)
  const option = options.shotReferenceSourceOptions?.find(row => row.value === rule.source)
  if (stage.key !== 'storyboard_review' || stage.mode !== 'review'
      || Object.keys(rule).sort().join(',') !== 'allow_override,selection,source,source_stage_key') return ['逐镜选材规则仅用于分镜参考步骤，必须完整配置']
  const errors: string[] = []
  if (!option || !option.selectionOptions.some(row => row.value === rule.selection)) errors.push('请选择素材来源及其选择方式')
  if (typeof rule.allow_override !== 'boolean') errors.push('请明确是否允许替换本镜已有选择')
  if (option && (option.upstreamStageKey === null ? rule.source_stage_key !== null
    : rule.source_stage_key !== option.upstreamStageKey || !prior.some(row => row.key === rule.source_stage_key && row.mode === 'existing_pipeline'))) errors.push('请选择本配方之前的素材结果阶段；已有/上传来源不配置上游')
  return errors
}
export function recipeStageWithKind(stage: JsonObject, kind: string, options: ProductionRecipeEditorOptions, newKey: () => string): JsonObject {
  if (recipeStageKind(stage) === kind) return stage
  const meta = options.stageOptions.find(row => row.value === kind)
  // A deliberate duty change starts an unconfigured step; unrelated route
  // contracts must not silently be reused for a different operation.
  return meta?.repeatable ? { key: newKey(), kind, name: meta.label, mode: meta.modes.length === 1 ? meta.modes[0]! : '', approval: 'auto' }
    : { key: kind, mode: '', approval: '' }
}
export function moveRecipeStage(stages: JsonObject[], index: number, direction: number, options: ProductionRecipeEditorOptions): JsonObject[] | null {
  const target = index + direction
  if (target < 0 || target >= stages.length) return null
  const value = [...stages]
  ;[value[index], value[target]] = [value[target]!, value[index]!]
  const singletonKeys = (rows: JsonObject[]) => rows.filter(row => !recipeStageOption(row, options)?.repeatable).map(row => row.key)
  return JSON.stringify(singletonKeys(stages)) === JSON.stringify(singletonKeys(value)) ? value : null
}
export function recipeRouteForContract(contract: ExecutionRequestFormContract): JsonObject {
  return { execution_instance_version_id: contract.versionId, expected_definition_sha256: contract.sha256,
    input_sources: {}, parameter_policies: [] }
}
export function recipeAuthoringErrors(value: unknown, options: ProductionRecipeEditorOptions | null | undefined): string[] {
  const definition = recipeObject(value), errors: string[] = []
  if (!options) return ['配方目录尚未读取，不能保存']
  if (definition.schema_version !== options.definitionSchemaVersion || definition.schema_version !== 3) return ['仅支持 schema3 配方；旧合同不会自动转换']
  if (Object.keys(definition).some(k => !['schema_version', 'interaction_mode', 'stages'].includes(k))) errors.push('配方包含未声明字段')
  if (!options.interactionModeOptions.some(o => o.value === definition.interaction_mode)) errors.push('请选择制作方式')
  const stages = recipeRows(definition.stages), seen = new Set<string>(), order = options.stageOptions.filter(o => !o.repeatable).map(o => o.value)
  if (!stages.length) errors.push('请至少配置一个生产阶段')
  let previous = -1
  for (const stage of stages) {
    const key = String(stage.key ?? ''), meta = recipeStageOption(stage, options)
    for(const authorKey of ['assembly_output','assembly_timing','assembly_media','delivery_metadata']) if(stage[authorKey]!==undefined) {
      const rule=recipeObject(stage[authorKey]), mapping=recipeObject(rule.mapping)
      const source=recipeObject(rule.source), selected=rule.source!==undefined
      const sourceValid=selected
        ? Object.keys(source).sort().join(',')==='kind,output_key,scope,selection_policy,stage_key'
          &&source.kind==='selected_output'&&source.selection_policy==='reviewed_selected'
          &&Object.keys(recipeObject(source.scope)).join(',')==='kind'&&recipeObject(source.scope).kind==='project'&&seen.has(String(source.stage_key))
          &&typeof source.output_key==='string'&&Boolean(source.output_key)
        : typeof rule.source_text==='string'&&Boolean(rule.source_text)
      if(stage.key!==(authorKey==='delivery_metadata'?'delivery':'assembly')||stage.mode!=='existing_pipeline'
        ||Object.keys(rule).sort().join(',')!==`expected_contract_sha256,mapping,output_version_id,${selected?'source':'source_text'}`
        ||typeof rule.output_version_id!=='string'||!rule.output_version_id
        ||typeof rule.expected_contract_sha256!=='string'||!/^[0-9a-f]{64}$/.test(rule.expected_contract_sha256)
        ||!sourceValid
        ||Object.keys(mapping).sort().join(',')!=='node,schema_version'||mapping.schema_version!==1
        ||!recipeObject(mapping.node).kind) errors.push(`${authorKey==='delivery_metadata'?'交付字段':authorKey==='assembly_media'?'已采用片段':authorKey==='assembly_timing'?'装配时序':'装配输出'}需要确切版本、摘要、明确原文或上游来源与完整M1映射`)
    }
    if (!key || key !== key.trim() || key.length > 100 || !meta || seen.has(key) || !meta.modes.includes(String(stage.mode))) errors.push(`阶段 ${key || '未选择'} 重复、顺序或执行方式无效`)
    if (meta?.repeatable) {
      if (!stage.kind || options.stageOptions.some(row => row.value === key)) errors.push(`步骤 ${key} 需要独立身份`)
      if (typeof stage.name !== 'string' || !stage.name.trim() || stage.name.length > 160) errors.push(`步骤 ${key} 需要1至160字符的显示名称`)
      if (stage.approval !== 'auto' || stage.decision_policy !== undefined) errors.push(`步骤 ${key} 按候选选择推进，不另设阶段审核`)
    } else {
      if (stage.kind !== undefined || stage.name !== undefined || order.indexOf(key) < previous) errors.push(`阶段 ${key} 的顺序或身份无效`)
      previous = order.indexOf(key)
    }
    errors.push(...recipeShotReferenceErrors(stage, stages.slice(0, seen.size), options))
    seen.add(key)
    if (stage.material_sources !== undefined) {
      const sources = recipeObject(stage.material_sources)
      if (recipeStageKind(stage) !== 'bible_image_generation' || stage.mode !== 'existing_pipeline'
          || Object.keys(sources).sort().join(',') !== 'primary,variant') errors.push('成图来源必须明确配置素材阶段的主图和变体')
      for (const role of ['primary','variant'] as const) {
        if (!options.materialSourceOptions?.some(row => row.value === sources[role])) errors.push('成图来源未接通或目录不完整')
        if (sources[role] !== 'generate' && stage[role==='primary'?'routing':'variant_routing'] !== undefined) errors.push('已有/上传成图不能保留对应闲置执行实例')
      }
    }
    if (stage.material_selection !== undefined) {
      const policies = recipeObject(stage.material_selection)
      if (stage.material_sources === undefined || !Object.keys(policies).length
          || Object.keys(policies).some(role => !['primary','variant'].includes(role)
            || !['existing','upload'].includes(recipeMaterialSource(stage, role as 'primary'|'variant'))
            || !options.materialSelectionOptions?.some(row => row.value === policies[role]))) errors.push('成图选材策略无效；生成候选的选择方式由对应执行实例配置')
    }
    if (!options.approvalOptions.some(o => o.value === stage.approval)) errors.push(`阶段 ${key} 未选择审核方式`)
    if (definition.interaction_mode === 'unattended') {
      if (['manual', 'skip'].includes(String(stage.mode)) || ['required', 'on_warning'].includes(String(stage.approval))) errors.push(`无人值守的 ${key} 不能等待人工操作`)
      if (stage.mode === 'review' && (stage.approval !== 'policy' || !meta?.decisionPolicy || stage.decision_policy !== meta.decisionPolicy)) errors.push(`阶段 ${key} 需要明确的自动审核策略`)
    } else if (stage.approval === 'policy' || stage.decision_policy !== undefined) errors.push(`阶段 ${key} 的自动审核仅适用于无人值守`)
    if (stage.variant_routing !== undefined && (recipeStageKind(stage)!=='bible_image_generation' || stage.mode!=='existing_pipeline')) errors.push(`阶段 ${key} 不能配置变体执行实例`)
    for (const {key:routingKey,label} of recipeRoutingGroups(stage)) {
      if (stage[routingKey] === undefined) continue
      if (!meta?.routingModes.includes(String(stage.mode))) errors.push(`阶段 ${key} 不能配置执行实例`)
      const routing = recipeObject(stage[routingKey]), routes = recipeRows(routing.routes)
      if (meta?.repeatable && !meta.candidateEvaluatorOptions.some(row => row.value === routing.evaluator)) errors.push(`步骤 ${key} 未选择有效的候选选择方式`)
      if (meta?.repeatable && definition.interaction_mode === 'unattended' && routing.evaluator === null) errors.push(`无人值守的 ${key} 不能等待人工选择候选`)
      if (!options.routingModeOptions.some(o => o.value === routing.mode)) errors.push(`阶段 ${key} ${label}未选择实例执行方式`)
      if (definition.interaction_mode === 'unattended' && routing.mode === 'manual_choice') errors.push('无人值守不能在运行时人工选择实例')
      if (!Number.isSafeInteger(routing.variant_count) || Number(routing.variant_count) < 1) errors.push(`阶段 ${key} 需要正整数候选数`)
      if (routing.mode === 'fixed' && routes.length > 1) errors.push('单一实例模式只能配置一个执行实例')
      for (const route of routes) {
        if (Object.keys(route).sort().join(',') !== ['execution_instance_version_id', 'expected_definition_sha256', 'input_sources', 'parameter_policies'].sort().join(',')) errors.push('实例配置只接受执行实例版本、摘要、输入来源与参数策略')
        if (!route.execution_instance_version_id || !/^[a-f0-9]{64}$/.test(String(route.expected_definition_sha256))) errors.push('实例配置缺少精确版本或摘要')
      }
    }
  }
  return [...new Set(errors)]
}
