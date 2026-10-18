import { resolutionPolicyErrors } from './workflowResolution'
import { parameterRangeErrors } from './workflowParameterRanges'
import type {
  JsonObject,
  WorkflowProjectorEngineOption,
  WorkflowProjectorOptions,
} from '@/services/platformTypes'

export const isJsonObject = (value: unknown): value is JsonObject => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
)

const cloneWorkflowProjectorValue = (value: unknown): unknown => {
  if (Array.isArray(value)) return value.map(cloneWorkflowProjectorValue)
  if (isJsonObject(value)) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => (
      [key, cloneWorkflowProjectorValue(item)]
    )))
  }
  return value
}

export const cloneWorkflowProjectorDefinition = (value: JsonObject): JsonObject => (
  cloneWorkflowProjectorValue(value) as JsonObject
)

export const getWorkflowProjectorPath = (value: JsonObject, path: string): unknown => {
  if (!path || path === '$') return value
  return path.split('.').reduce<unknown>((current, part) => (
    isJsonObject(current) ? current[part] : undefined
  ), value)
}

export const setWorkflowProjectorPath = (
  value: JsonObject,
  path: string,
  nextValue: unknown,
): JsonObject => {
  const next = cloneWorkflowProjectorDefinition(value)
  const parts = path.split('.').filter(Boolean)
  if (!parts.length) return next
  let cursor: JsonObject = next
  parts.slice(0, -1).forEach((part) => {
    if (!isJsonObject(cursor[part])) cursor[part] = {}
    cursor = cursor[part] as JsonObject
  })
  cursor[parts[parts.length - 1]] = nextValue
  return next
}

export const workflowProjectorEngine = (
  options: WorkflowProjectorOptions,
  key: string,
  version: number,
): WorkflowProjectorEngineOption | undefined => options.engines.find(
  (item) => item.key === key && item.version === version,
)

const hasObject = (value: JsonObject, key: string) => isJsonObject(value[key])
const hasArray = (value: JsonObject, key: string) => Array.isArray(value[key])
const hasExactKeys = (value: JsonObject, keys: string[]) => (
  Object.keys(value).length === keys.length && keys.every((key) => key in value)
)
const nonEmptyText = (value: unknown) => typeof value === 'string' && Boolean(value.trim())
const safeScalar = (value: unknown) => (
  value === null || typeof value === 'string' || typeof value === 'boolean'
  || (typeof value === 'number' && Number.isFinite(value))
)
const choiceValues = (values: Array<{ value: string }>) => new Set(values.map((row) => row.value))

export const workflowProjectorDefinitionErrors = (
  definition: JsonObject,
  options: WorkflowProjectorOptions,
  engineKey: string,
  engineVersion: number,
): string[] => {
  const errors: string[] = []
  if (options.editorSchemaVersion !== 1) {
    errors.push(`当前页面不支持服务端表单 Schema v${options.editorSchemaVersion}`)
    return errors
  }
  const engine = workflowProjectorEngine(options, engineKey, engineVersion)
  if (!engine) {
    errors.push(`服务端未提供 Engine ${engineKey || '空'}/v${engineVersion || '空'} 的编辑合同`)
    return errors
  }
  const expectedTopLevelFields = new Set(options.definitionEditor.sections.flatMap((section) => (
    section.fields.filter((field) => field.path !== '$').map((field) => field.path.split('.')[0])
  )))
  const unknownFields = Object.keys(definition).filter((key) => !expectedTopLevelFields.has(key))
  const missingFields = [...expectedTopLevelFields].filter((key) => !(key in definition))
  if (unknownFields.length) errors.push(`存在 Schema v1 不支持的顶层字段：${unknownFields.join('、')}`)
  if (missingFields.length) errors.push(`缺少 Definition 字段：${missingFields.join('、')}`)
  if (definition.schema_version !== options.definitionSchemaVersion) {
    errors.push(`Definition Schema 必须为 v${options.definitionSchemaVersion}`)
  }
  const priority = definition.priority
  const { priorityMin, priorityMax } = options.definitionEditor.limits
  if (!Number.isSafeInteger(priority) || Number(priority) < priorityMin || Number(priority) > priorityMax) {
    errors.push(`匹配优先级必须是 ${priorityMin}–${priorityMax} 的整数`)
  }
  for (const key of ['detector', 'output_nodes', 'node_roles', 'adapters', 'projection']) {
    if (!hasObject(definition, key)) errors.push(`${key} 必须是对象`)
  }
  for (const key of ['model_selectors', 'required_edges', 'parameter_policies', 'asset_slots']) {
    if (!hasArray(definition, key)) errors.push(`${key} 必须是列表`)
  }
  const detector = isJsonObject(definition.detector) ? definition.detector : {}
  if (!hasExactKeys(detector, ['fallback', 'allow_other_nodes', 'node_counts', 'optional_node_counts', 'input_equals'])) {
    errors.push('工作流识别字段集合不完整或包含未知字段')
  }
  if (typeof detector.fallback !== 'boolean' || typeof detector.allow_other_nodes !== 'boolean') {
    errors.push('工作流识别的兜底和额外节点开关必须是布尔值')
  }
  const nodeCounts = isJsonObject(detector.node_counts) ? detector.node_counts : null
  const optionalCounts = isJsonObject(detector.optional_node_counts) ? detector.optional_node_counts : null
  const inputEquals = Array.isArray(detector.input_equals) ? detector.input_equals : null
  if (!nodeCounts || !optionalCounts || !inputEquals) {
    errors.push('工作流识别必须包含必需节点、可选节点和关键输入条件')
  } else {
    for (const [nodeType, count] of Object.entries(nodeCounts)) {
      if (!nodeType.trim() || !Number.isSafeInteger(count) || Number(count) < 0 || Number(count) > options.definitionEditor.limits.nodeCountMax) {
        errors.push('必需节点必须填写节点类型及合法整数数量')
        break
      }
    }
    for (const [nodeType, value] of Object.entries(optionalCounts)) {
      const row = isJsonObject(value) ? value : {}
      if (
        !nodeType.trim() || nodeType in nodeCounts || !hasExactKeys(row, ['min', 'max'])
        || !Number.isSafeInteger(row.min) || !Number.isSafeInteger(row.max)
        || Number(row.min) < 0 || Number(row.max) < Number(row.min)
        || Number(row.max) > options.definitionEditor.limits.nodeCountMax
      ) {
        errors.push('可选节点必须填写不与必需节点重复的合法数量范围')
        break
      }
    }
    if (inputEquals.length > options.definitionEditor.limits.inputEqualsMaxItems) {
      errors.push(`关键输入条件不能超过 ${options.definitionEditor.limits.inputEqualsMaxItems} 项`)
    }
    if (inputEquals.some((value) => {
      const row = isJsonObject(value) ? value : {}
      return !hasExactKeys(row, ['node_type', 'input_name', 'value'])
        || !nonEmptyText(row.node_type) || !nonEmptyText(row.input_name) || !safeScalar(row.value)
    })) errors.push('关键输入条件存在空字段、未知字段或非 JSON 标量值')
    if (detector.fallback && (
      Object.keys(nodeCounts).length || Object.keys(optionalCounts).length || inputEquals.length
    )) errors.push('兜底方案不能同时保留节点或输入指纹')
  }
  const outputNodes = isJsonObject(definition.output_nodes) ? definition.output_nodes : {}
  if (!Object.keys(outputNodes).length) errors.push('至少需要一个主输出节点')
  const outputKinds = choiceValues(options.definitionEditor.choices.outputKinds)
  if (Object.entries(outputNodes).some(([nodeType, kind]) => !nodeType.trim() || !outputKinds.has(String(kind)))) {
    errors.push('主输出节点存在空节点类型或未知产物类型')
  }

  const selectors = Array.isArray(definition.model_selectors) ? definition.model_selectors : []
  if (selectors.length > options.definitionEditor.limits.modelSelectorsMaxItems) {
    errors.push(`模型文件识别不能超过 ${options.definitionEditor.limits.modelSelectorsMaxItems} 项`)
  }
  const artifactRoles = choiceValues(options.definitionEditor.choices.artifactRoles)
  const selectorTargets = new Set<string>()
  if (selectors.some((value) => {
    const row = isJsonObject(value) ? value : {}
    const target = `${String(row.node_type || '')}\u0000${String(row.input_name || '')}`
    const invalid = !hasExactKeys(row, [
      'node_type', 'input_name', 'role', 'inventory_folder', 'inventory_node_type', 'inventory_input_name',
    ]) || !nonEmptyText(row.node_type) || !nonEmptyText(row.input_name)
      || !artifactRoles.has(String(row.role)) || selectorTargets.has(target)
      || ['inventory_folder', 'inventory_node_type', 'inventory_input_name'].some((key) => typeof row[key] !== 'string')
    selectorTargets.add(target)
    return invalid
  })) errors.push('模型文件识别存在空字段、重复目标、未知角色或字段集合错误')

  const nodeRoles = isJsonObject(definition.node_roles) ? definition.node_roles : {}
  const cardinalities = choiceValues(options.definitionEditor.choices.cardinalities)
  if (Object.entries(nodeRoles).some(([role, value]) => {
    const row = isJsonObject(value) ? value : {}
    return !role.trim() || !hasExactKeys(row, ['node_type', 'cardinality'])
      || !nonEmptyText(row.node_type) || !cardinalities.has(String(row.cardinality))
  })) errors.push('节点角色存在空值、未知基数或字段集合错误')

  const edges = Array.isArray(definition.required_edges) ? definition.required_edges : []
  if (edges.length > options.definitionEditor.limits.requiredEdgesMaxItems) {
    errors.push(`必要连接不能超过 ${options.definitionEditor.limits.requiredEdgesMaxItems} 项`)
  }
  if (edges.some((value) => {
    const row = isJsonObject(value) ? value : {}
    const from = String(row.from_role || '')
    const to = String(row.to_role || '')
    const fromRow = isJsonObject(nodeRoles[from]) ? nodeRoles[from] as JsonObject : {}
    const toRow = isJsonObject(nodeRoles[to]) ? nodeRoles[to] as JsonObject : {}
    return !hasExactKeys(row, ['from_role', 'output_index', 'to_role', 'input_name'])
      || !(from in nodeRoles) || !(to in nodeRoles)
      || fromRow.cardinality !== 'one' || toRow.cardinality !== 'one'
      || !Number.isSafeInteger(row.output_index) || Number(row.output_index) < 0
      || !nonEmptyText(row.input_name)
  })) errors.push('必要连接存在空字段、无效角色、非唯一角色或非法输出索引')

  const policies = Array.isArray(definition.parameter_policies) ? definition.parameter_policies : []
  if (policies.length > options.definitionEditor.limits.parameterPoliciesMaxItems) {
    errors.push(`参数策略不能超过 ${options.definitionEditor.limits.parameterPoliciesMaxItems} 项`)
  }
  const policyKinds = choiceValues(options.definitionEditor.choices.parameterPolicies)
  const semanticKeys = new Set<string>()
  const policyTargets = new Set<string>()
  if (policies.some((value) => {
    const row = isJsonObject(value) ? value : {}
    const semanticKey = String(row.semantic_key || '')
    const target = `${String(row.node_role || '')}\u0000${String(row.input_name || '')}`
    const policy = String(row.policy || '')
    const { constraints, ...basePolicy } = row
    if (constraints !== undefined) {
      errors.push(...parameterRangeErrors(constraints))
      if (isJsonObject(constraints) && Object.keys(constraints).length && !['runtime','object_info'].includes(policy)) errors.push('只有运行时传入或 object_info 策略可以配置参数范围')
    }
    const invalid = !hasExactKeys(basePolicy, ['semantic_key', 'node_role', 'input_name', 'policy', 'value'])
      || !semanticKey.trim() || semanticKeys.has(semanticKey) || policyTargets.has(target)
      || !(String(row.node_role || '') in nodeRoles) || !nonEmptyText(row.input_name)
      || !policyKinds.has(policy) || !safeScalar(row.value)
      || (policy !== 'fixed' && row.value !== null)
    semanticKeys.add(semanticKey); policyTargets.add(target)
    return invalid
  })) errors.push('参数策略存在空字段、重复项、未知策略、非法值或非固定策略夹带值')

  const slots = Array.isArray(definition.asset_slots) ? definition.asset_slots : []
  if (slots.length > options.definitionEditor.limits.assetSlotsMaxItems) {
    errors.push(`素材槽不能超过 ${options.definitionEditor.limits.assetSlotsMaxItems} 项`)
  }
  const mediaKinds = choiceValues(options.definitionEditor.choices.mediaKinds)
  const contractSources = choiceValues(options.definitionEditor.choices.contractSources)
  const slotKeys = new Set<string>()
  if (slots.some((value) => {
    const row = isJsonObject(value) ? value : {}
    const key = String(row.key || '')
    const invalid = !hasExactKeys(row, ['key', 'media_kind', 'dynamic', 'contract_source'])
      || !key.trim() || slotKeys.has(key) || !mediaKinds.has(String(row.media_kind))
      || typeof row.dynamic !== 'boolean' || !contractSources.has(String(row.contract_source))
    slotKeys.add(key)
    return invalid
  })) errors.push('素材槽存在空 Key、重复 Key、未知类型或字段集合错误')

  const adapters = isJsonObject(definition.adapters) ? definition.adapters : {}
  if (!hasExactKeys(adapters, ['compiler', 'output', 'dynamic'])) {
    errors.push('Adapter 合同字段集合错误')
  }
  for (const kind of ['compiler', 'output', 'dynamic'] as const) {
    if (adapters[kind] !== engine.adapterContract[kind]) {
      errors.push(`${kind} Adapter 必须由 Engine 固定为 ${engine.adapterContract[kind]}`)
    }
  }
  const projection = isJsonObject(definition.projection) ? definition.projection : {}
  if (!hasExactKeys(projection, ['kind', 'config'])) errors.push('projection 字段集合错误')
  if (projection.kind !== engine.projectionKind) {
    errors.push(`projection.kind 必须由 Engine 固定为 ${engine.projectionKind}`)
  }
  if (!isJsonObject(projection.config)) errors.push('projection.config 必须是对象')
  else if (engine.projectionKind === 'generic_fixed') {
    if (Object.keys(projection.config).some(key => !['image_resolution','capability'].includes(key))) errors.push('Generic 固定引擎只支持执行能力与图片尺寸配置')
    const capability = projection.config.capability
    if ('capability' in projection.config && !options.definitionEditor.choices.executionCapabilities.some(row => row.value === capability)) errors.push('请选择已登记的执行能力')
    if ('image_resolution' in projection.config) errors.push(...resolutionPolicyErrors(projection.config.image_resolution, options.imageResolution))
  }
  else if (engine.projectionKind === 'generic_references') {
    if (Object.keys(projection.config).some(key => !['groups', 'capability'].includes(key))) errors.push('通用动态引擎只支持执行能力与动态素材组配置')
    const capability = projection.config.capability
    if ('capability' in projection.config && !options.definitionEditor.choices.executionCapabilities.some(row => row.value === capability)) errors.push('请选择已登记的执行能力')
    const groups = isJsonObject(projection.config) && Array.isArray(projection.config.groups) ? projection.config.groups : []
    const keys = new Set<string>()
    if (!groups.length || groups.length !== slots.length) errors.push('动态素材组须非空，且与素材槽逐项对应；请在执行引擎配置中添加动态素材组')
    for (const value of groups) {
      const row = isJsonObject(value) ? value : {}
      const key = String(row.key || '')
      const slot = slots.find((value) => isJsonObject(value) && value.key === key) as JsonObject | undefined
      const list = (field: string) => Array.isArray(row[field]) ? row[field]!.map(String) : []
      const branches = list('branch_roles')
      const targets = list('target_roles')
      if (!/^[a-z][a-z0-9_]{0,47}$/.test(key) || keys.has(key) || !slot?.dynamic
        || !branches.includes(String(row.source_role)) || !branches.includes(String(row.output_role)) || !targets.length
        || targets.some(role => branches.includes(role)) || !list('input_names').length) errors.push(`${key || '动态素材组'}：请补齐唯一 Key、模板角色、目标角色和有序端口`)
      keys.add(key)
      if (!Number.isSafeInteger(row.output_index) || Number(row.output_index) < 0
        || (row.wiring === 'reduce' && (!Number.isSafeInteger(row.reducer_output_index) || Number(row.reducer_output_index) < 0))) errors.push(`${key}：模板与合并输出索引须为非负整数`)
      if (slot?.contract_source === 'workflow' && (!Number.isSafeInteger(row.minimum_items) || !Number.isSafeInteger(row.maximum_items)
        || Number(row.minimum_items) < 0 || Number(row.maximum_items) < Math.max(1, Number(row.minimum_items))
        || (row.wiring === 'ordered_ports' && Number(row.maximum_items) !== list('input_names').length))) errors.push(`${key}：数量范围无效，顺序接线的最大数量须等于端口数`)
      if (slot?.contract_source === 'model_profile' && (!row.conditioning_mode || !row.model_input)) errors.push(`${key}：请指定模型版本合同的 mode/input 数量来源`)
      if (slot?.contract_source === 'model_profile' && row.model_input
        && !options.definitionEditor.choices.modelQuantityInputs?.some(input => input.value === row.model_input && input.mediaKind === slot.media_kind)) errors.push(`${key}：模型数量输入未注册或与素材类型不匹配，请在执行引擎表单重新选择`)
      if (row.wiring === 'reduce' && (!row.reducer_role || list('reducer_inputs').length !== 2 || list('reducer_inputs').some(name => !name) || list('input_names').length !== 1)) errors.push(`${key}：合并接线须选择合并节点、两个输入和一个目标端口`)
    }
  }
  else if (engine.projectionKind === 'generic_classified') {
    const config = projection.config
    if (!config.capability || !config.conditioning_mode || !config.target_role) errors.push('分类参考配置须选择能力、模式和目标角色')
    const groups = isJsonObject(config.dynamic_groups) ? config.dynamic_groups : {}
    if (Object.keys(groups).length !== 4 || slots.length !== 4) errors.push('请在分类参考接线中配置四类素材及数量来源；不支持的类别设置上限为 0')
    for (const [key,value] of Object.entries(groups)) {
      const row = isJsonObject(value) ? value : {}
      const slot = slots.find(value=>isJsonObject(value)&&value.key===key) as JsonObject | undefined
      if (!slot?.dynamic || !row.mode_input) errors.push(`${key}：缺少动态素材槽或模型数量来源`)
      if (Boolean(row.prefix) === Boolean(Array.isArray(row.input_names)&&row.input_names.length)) errors.push(`${key}：请选择端口前缀或端口列表，不能同时填写`)
      if (slot?.contract_source==='workflow' && (!Number.isSafeInteger(row.minimum_items)||!Number.isSafeInteger(row.maximum_items)||Number(row.minimum_items)<0||Number(row.maximum_items)<Number(row.minimum_items))) errors.push(`${key}：Projector 数量范围无效`)
    }
  }
  return [...new Set(errors)]
}

export const parseWorkflowProjectorJson = (
  text: string,
  baseline?: JsonObject,
): { value: JsonObject | null; error: string } => {
  try {
    const value: unknown = JSON.parse(text)
    if (!isJsonObject(value)) return { value: null, error: '完整 Definition 必须是 JSON 对象' }
    const before = baseline && isJsonObject(baseline.projection) ? baseline.projection : {}
    const after = isJsonObject(value.projection) ? value.projection : {}
    if (baseline && (before.kind === 'generic_references' || after.kind === 'generic_references')) {
      const canonical = (item: unknown): string => {
        if (Array.isArray(item)) return `[${item.map(canonical).join(',')}]`
        if (isJsonObject(item)) return `{${Object.keys(item).sort().map(key => `${JSON.stringify(key)}:${canonical(item[key])}`).join(',')}}`
        return JSON.stringify(item) ?? 'undefined'
      }
      const protectedFields = (definition: JsonObject, projection: JsonObject) => ({
        kind: projection.kind, asset_slots: definition.asset_slots,
        groups: isJsonObject(projection.config) ? projection.config.groups : undefined,
      })
      if (canonical(protectedFields(baseline, before)) !== canonical(protectedFields(value, after))) {
        return { value: null, error: '动态素材组、槽声明和数量映射只能在执行引擎表单修改，完整 JSON 仅核对这些字段。' }
      }
    }
    return { value, error: '' }
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : 'JSON 无法解析'
    return { value: null, error: `JSON 尚未应用：${message}` }
  }
}

export const workflowProjectorDefinitionSummary = (definition: JsonObject) => ({
  requiredNodes: isJsonObject((definition.detector as JsonObject | undefined)?.node_counts)
    ? Object.keys((definition.detector as JsonObject).node_counts as JsonObject).length : 0,
  optionalNodes: isJsonObject((definition.detector as JsonObject | undefined)?.optional_node_counts)
    ? Object.keys((definition.detector as JsonObject).optional_node_counts as JsonObject).length : 0,
  roles: isJsonObject(definition.node_roles) ? Object.keys(definition.node_roles).length : 0,
  edges: Array.isArray(definition.required_edges) ? definition.required_edges.length : 0,
  parameters: Array.isArray(definition.parameter_policies) ? definition.parameter_policies.length : 0,
  modelSelectors: Array.isArray(definition.model_selectors) ? definition.model_selectors.length : 0,
  assetSlots: Array.isArray(definition.asset_slots) ? definition.asset_slots.length : 0,
})
