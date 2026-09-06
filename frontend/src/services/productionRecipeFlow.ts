import { modeLabel, stageLabel } from '@/services/presentationLabels'
import { recipeStageModes, recipeStageOptions, recipeStageRequiresRouting } from '@/services/registryResourceSchema'
import type {
  JsonObject,
  ModelDeployment,
  ModelProfileVersion,
  ProductionRecipeEditorOptions,
  WorkflowModelRequirement,
  WorkflowSemanticBinding,
  WorkflowTemplateVersion,
} from '@/services/platformTypes'

export type ProductionRecipeFlowPhase = 'script' | 'preproduction' | 'production' | 'delivery' | 'extension'
export type WorkflowDetailState = 'loaded' | 'summary' | 'loading' | 'error' | 'missing' | 'not_applicable'

export interface ProductionRecipeFlowInput {
  semanticKey: string
  valueType: string
  required: boolean
  nodeId: string
  inputName: string
  constraints: string[]
}

export interface ProductionRecipeFlowDynamicInput {
  modeKey: string
  modeLabel: string
  semanticKey: string
  minimumItems: number | null
  maximumItems: number | null
}

export interface ProductionRecipeFlowModelRequirement {
  role: string
  alias: string
  expectedFilename: string
  expectedSha256: string
  required: boolean
}

export interface ProductionRecipeFlowOutput {
  key: string
  kind: string
  nodeId: string
  required: boolean
}

export interface ProductionRecipeFlowRoute {
  id: string
  index: number
  modelDeploymentId: string
  modelDeploymentLabel: string
  modelDeploymentStatus: string
  providerDriver: string
  modelProfileVersionId: string
  modelProfileLabel: string
  modelProfileStatus: string
  modelIdentifier: string
  modelModality: string
  providerKey: string
  workflowVersionId: string
  workflowVersionLabel: string
  workflowStatus: string
  workflowOutputKind: string
  workflowDetailState: WorkflowDetailState
  workflowDetailError: string
  workflowAuditWarning: string
  inputContractSchemaVersion: number | null
  inputContractSha256: string
  semanticBindingCount: number
  inputs: ProductionRecipeFlowInput[]
  dynamicInputs: ProductionRecipeFlowDynamicInput[]
  modelRequirements: ProductionRecipeFlowModelRequirement[]
  outputs: ProductionRecipeFlowOutput[]
  issues: string[]
}

export interface ProductionRecipeFlowStage {
  id: string
  index: number
  key: string
  label: string
  phase: ProductionRecipeFlowPhase
  phaseLabel: string
  mode: string
  modeLabel: string
  approval: string
  approvalLabel: string
  hasRouting: boolean
  routingRequired: boolean
  routingMode: string
  routingModeLabel: string
  variantCount: number | null
  evaluator: unknown
  decisionPolicy: string
  routes: ProductionRecipeFlowRoute[]
  previousDeclaration: { index: number; key: string; label: string } | null
  nextDeclaration: { index: number; key: string; label: string } | null
  extras: JsonObject
  issues: string[]
}

export interface ProductionRecipeFlowProjection {
  schemaVersion: number | null
  interactionMode: string
  stages: ProductionRecipeFlowStage[]
  stageCount: number
  routeCount: number
  reviewGateCount: number
  manualStageCount: number
  firstStageLabel: string
  lastStageLabel: string
  topLevelExtras: JsonObject
  issues: string[]
}

type WorkflowLookup = Pick<WorkflowTemplateVersion,
  'id' | 'templateName' | 'templateSlug' | 'version' | 'status' | 'outputKind' | 'modelProfileVersion'
  | 'inputContractSha256' | 'semanticBindingCount' | 'inputContract' | 'semanticBindings' | 'modelRequirements' | 'smokeFixtureSchemaError'>

type RecipeFlowLookups = {
  deployments?: Pick<ModelDeployment, 'id' | 'name' | 'key' | 'profileVersion' | 'providerDriver' | 'status'>[]
  modelProfiles?: Pick<ModelProfileVersion, 'id' | 'profileKey' | 'version' | 'providerKey' | 'modelIdentifier' | 'modality' | 'status'>[]
  workflows?: WorkflowLookup[]
  workflowDetailStates?: Record<string, 'loading' | 'loaded' | 'error'>
  workflowDetailErrors?: Record<string, string>
  recipeEditorOptions?: ProductionRecipeEditorOptions | null
}

const asRecord = (value: unknown): JsonObject | null => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : null
)
const asString = (value: unknown) => typeof value === 'string' ? value.trim() : ''
const asInteger = (value: unknown) => Number.isSafeInteger(value) ? Number(value) : null
const asBoolean = (value: unknown) => value === true
const shortId = (value: string) => value ? value.slice(0, 8) : '未提供'
const hasOwn = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key)

const phaseFor = (key: string): ProductionRecipeFlowPhase => {
  if (key.startsWith('script_')) return 'script'
  if (key.startsWith('bible_') || key.startsWith('visual_rules_') || key.startsWith('storyboard')) return 'preproduction'
  if (['prompt_compile', 'media_generation', 'artifact_selection'].includes(key)) return 'production'
  if (['assembly', 'delivery'].includes(key)) return 'delivery'
  return 'extension'
}

const phaseLabels: Record<ProductionRecipeFlowPhase, string> = {
  script: '脚本',
  preproduction: '前期制作',
  production: '媒体生产',
  delivery: '装配交付',
  extension: '扩展阶段',
}

const approvalLabels: Record<string, string> = {
  auto: '自动流转',
  required: '必须人工审核',
  on_warning: '警告时审核',
  policy: '配方策略决策',
}

const routingModeLabels: Record<string, string> = {
  fixed: '固定路由',
  ordered_fallback: '顺序回退',
  manual_choice: '人工选择',
}

const stageLabelSafe = (key: string) => {
  if (!key) return '未命名阶段'
  const label = stageLabel(key)
  return label === '未识别阶段' ? key : label
}

const compactValue = (value: unknown): string => {
  if (typeof value === 'string') return value.length > 28 ? `${value.slice(0, 27)}…` : value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (value === null) return 'null'
  return ''
}

const parameterConstraints = (definition: JsonObject): string[] => {
  const result: string[] = []
  const minimum = definition.min ?? definition.minimum
  const maximum = definition.max ?? definition.maximum
  if (minimum !== undefined || maximum !== undefined) result.push(`范围 ${minimum ?? '不限'}–${maximum ?? '不限'}`)
  const minimumItems = definition.min_items ?? definition.minimum_items
  const maximumItems = definition.max_items ?? definition.maximum_items
  if (minimumItems !== undefined || maximumItems !== undefined) result.push(`数量 ${minimumItems ?? 0}–${maximumItems ?? '不限'}`)
  const multipleOf = definition.multiple_of ?? definition.multipleOf
  if (multipleOf !== undefined) result.push(`步长 ${multipleOf}`)
  const options = Array.isArray(definition.options) ? definition.options : []
  if (options.length) {
    const values = options.slice(0, 4).map(compactValue).filter(Boolean)
    result.push(options.length <= 4 ? `选项 ${values.join(' / ')}` : `${options.length} 个选项`)
  }
  if (hasOwn(definition, 'default')) {
    const rendered = compactValue(definition.default)
    if (rendered) result.push(`合同默认 ${rendered}`)
  }
  return result
}

const workflowInputs = (workflow: WorkflowLookup | undefined): ProductionRecipeFlowInput[] => {
  const contract = asRecord(workflow?.inputContract)
  const parameters = asRecord(contract?.parameters) || {}
  const bindings = Array.isArray(workflow?.semanticBindings) ? workflow.semanticBindings : []
  const bindingByKey = new Map(bindings.map((binding: WorkflowSemanticBinding) => [binding.semanticKey, binding]))
  const keys = [...new Set([...Object.keys(parameters), ...bindings.map((binding: WorkflowSemanticBinding) => binding.semanticKey)])]
  return keys
    .filter((key) => !key.startsWith('model.') && !key.startsWith('output.'))
    .map((key) => {
      const definition = asRecord(parameters[key]) || {}
      const binding = bindingByKey.get(key)
      return {
        semanticKey: key,
        valueType: asString(definition.type) || binding?.valueType || '未声明',
        required: typeof definition.required === 'boolean' ? definition.required : Boolean(binding?.isRequired),
        nodeId: binding?.nodeId || '',
        inputName: binding?.inputName || '',
        constraints: parameterConstraints(definition),
      }
    })
}

const workflowDynamicInputs = (workflow: WorkflowLookup | undefined): ProductionRecipeFlowDynamicInput[] => {
  const contract = asRecord(workflow?.inputContract)
  const conditioning = asRecord(contract?.conditioning_contract)
  const modes = Array.isArray(conditioning?.modes) ? conditioning.modes : []
  return modes.flatMap((modeValue) => {
    const mode = asRecord(modeValue)
    const inputs = asRecord(mode?.inputs) || {}
    return Object.entries(inputs).flatMap(([semanticKey, inputValue]) => {
      const input = asRecord(inputValue)
      if (!input) return []
      return [{
        modeKey: asString(mode?.key),
        modeLabel: asString(mode?.label) || asString(mode?.key) || '未命名条件模式',
        semanticKey,
        minimumItems: asInteger(input.minimum_items),
        maximumItems: asInteger(input.maximum_items),
      }]
    })
  })
}

const workflowOutputs = (workflow: WorkflowLookup | undefined): ProductionRecipeFlowOutput[] => {
  const contract = asRecord(workflow?.inputContract)
  const outputs = asRecord(contract?.outputs) || {}
  return Object.entries(outputs).flatMap(([key, outputValue]) => {
    const output = asRecord(outputValue)
    if (!output) return []
    return [{
      key,
      kind: asString(output.kind) || workflow?.outputKind || '未声明',
      nodeId: asString(output.node_id),
      required: asBoolean(output.required),
    }]
  })
}

const workflowRequirements = (requirements: WorkflowModelRequirement[] | undefined): ProductionRecipeFlowModelRequirement[] => (
  Array.isArray(requirements) ? requirements.map((requirement) => ({
    role: requirement.role,
    alias: requirement.alias,
    expectedFilename: requirement.expectedFilename,
    expectedSha256: requirement.expectedSha256,
    required: requirement.isRequired,
  })) : []
)

export const recipeWorkflowVersionIds = (definitionValue: unknown): string[] => {
  const definition = asRecord(definitionValue)
  const stages = Array.isArray(definition?.stages) ? definition.stages : []
  const ids = stages.flatMap((stageValue) => {
    const stage = asRecord(stageValue)
    const routing = asRecord(stage?.routing)
    const routes = Array.isArray(routing?.routes) ? routing.routes : []
    return routes.map((routeValue) => asString(asRecord(routeValue)?.workflow_version_id)).filter(Boolean)
  })
  return [...new Set(ids)]
}

export const buildProductionRecipeFlow = (
  definitionValue: unknown,
  lookups: RecipeFlowLookups = {},
): ProductionRecipeFlowProjection => {
  const definition = asRecord(definitionValue)
  const issues: string[] = []
  if (!definition) issues.push('配方 definition 不是对象，无法可靠投影阶段。')
  const schemaVersion = asInteger(definition?.schema_version)
  if (definition && schemaVersion === null) issues.push('配方缺少整数 schema_version。')
  else if (schemaVersion !== null && ![1, 2].includes(schemaVersion)) issues.push(`配方 schema v${schemaVersion} 当前不受支持。`)
  const interactionMode = asString(definition?.interaction_mode)
  if (schemaVersion === 2 && !interactionMode) issues.push('schema v2 配方缺少 interaction_mode。')
  const stagesValue = definition?.stages
  const stageValues = Array.isArray(stagesValue) ? stagesValue : []
  if (!Array.isArray(stagesValue)) issues.push('配方 stages 缺失或不是数组。')
  else if (!stageValues.length) issues.push('配方 stages 为空，无法形成生产阶段声明。')
  const deploymentById = new Map((lookups.deployments || []).map((item) => [item.id, item]))
  const modelProfileById = new Map((lookups.modelProfiles || []).map((item) => [item.id, item]))
  const workflowById = new Map((lookups.workflows || []).map((item) => [item.id, item]))
  const stageModeCatalog = recipeStageModes(lookups.recipeEditorOptions)
  const knownStageKeys = new Set(recipeStageOptions(lookups.recipeEditorOptions).map((item) => item.value))
  const seenKeys = new Set<string>()

  const rawStages = stageValues.map((stageValue, index): Omit<ProductionRecipeFlowStage, 'previousDeclaration' | 'nextDeclaration'> => {
    const stage = asRecord(stageValue)
    const stageIssues: string[] = []
    const key = asString(stage?.key)
    if (!stage) stageIssues.push(`第 ${index + 1} 个阶段不是对象。`)
    if (!key) stageIssues.push(`第 ${index + 1} 个阶段缺少 key。`)
    if (key && seenKeys.has(key)) stageIssues.push(`阶段 key ${key} 重复。`)
    if (key && knownStageKeys.size && !knownStageKeys.has(key)) stageIssues.push(`阶段 key ${key} 未在当前执行阶段目录登记。`)
    if (key) seenKeys.add(key)
    const mode = asString(stage?.mode)
    const approval = asString(stage?.approval)
    if (!mode) stageIssues.push(`${stageLabelSafe(key)} 缺少 mode。`)
    else if (key && stageModeCatalog[key]?.length && !stageModeCatalog[key].includes(mode)) stageIssues.push(`${stageLabelSafe(key)} 的 mode ${mode} 不受支持。`)
    if (!approval) stageIssues.push(`${stageLabelSafe(key)} 缺少 approval。`)
    else if (!approvalLabels[approval]) stageIssues.push(`${stageLabelSafe(key)} 的 approval ${approval} 无法识别。`)
    const routingValue = stage?.routing
    const routing = asRecord(routingValue)
    const routingRequired = recipeStageRequiresRouting(stage || {}, lookups.recipeEditorOptions)
    if (routingValue !== undefined && routingValue !== null && !routing) stageIssues.push(`${stageLabelSafe(key)} 的 routing 不是对象。`)
    if (routingRequired && !routing) stageIssues.push(`${stageLabelSafe(key)} 必须声明 routing。`)
    const routesValue = routing?.routes
    if (routing && !Array.isArray(routesValue)) stageIssues.push(`${stageLabelSafe(key)} 的 routing.routes 缺失或不是数组。`)
    if (routingRequired && Array.isArray(routesValue) && !routesValue.length) stageIssues.push(`${stageLabelSafe(key)} 至少需要一个 route。`)
    const routes = (Array.isArray(routesValue) ? routesValue : []).map((routeValue, routeIndex): ProductionRecipeFlowRoute => {
      const route = asRecord(routeValue)
      const routeIssues: string[] = []
      if (!route) routeIssues.push(`路由 ${routeIndex + 1} 不是对象。`)
      const modelDeploymentId = asString(route?.model_deployment_id)
      const workflowVersionId = asString(route?.workflow_version_id)
      if (!modelDeploymentId) routeIssues.push('缺少冻结的模型部署 ID。')
      const deployment = deploymentById.get(modelDeploymentId)
      if (lookups.deployments && modelDeploymentId && !deployment) routeIssues.push('当前部署目录无法解析该模型部署 ID。')
      const workflow = workflowById.get(workflowVersionId)
      if (lookups.workflows && workflowVersionId && !workflow) routeIssues.push('当前工作流目录无法解析该工作流版本 ID。')
      const providerDriver = asString(deployment?.providerDriver)
      const modelProfileVersionId = asString(deployment?.profileVersion) || asString(workflow?.modelProfileVersion)
      const modelProfile = modelProfileById.get(modelProfileVersionId)
      if (lookups.modelProfiles && modelProfileVersionId && !modelProfile) routeIssues.push('当前模型目录无法解析该模型版本 ID。')
      let workflowVersionLabel = '未绑定工作流（部署驱动待确认）'
      if (workflowVersionId) {
        const workflowName = workflow?.templateName || workflow?.templateSlug
        workflowVersionLabel = workflowName
          ? `${workflowName} · v${workflow?.version || '—'} · ${workflow?.outputKind || 'unknown'}`
          : `工作流 ${shortId(workflowVersionId)}`
      } else if (providerDriver === 'comfyui') {
        workflowVersionLabel = '缺少工作流版本'
        routeIssues.push('ComfyUI 路由缺少冻结的工作流版本 ID。')
      } else if (providerDriver) {
        workflowVersionLabel = '服务调用（工作流不适用）'
      } else if (modelDeploymentId) {
        routeIssues.push('当前可见部署目录无法确认驱动，不能判断此路由是否需要工作流。')
      }
      const detailed = Boolean(workflow && (
        hasOwn(workflow, 'inputContract') || hasOwn(workflow, 'semanticBindings') || hasOwn(workflow, 'modelRequirements')
      ))
      const requestedDetailState = workflowVersionId ? lookups.workflowDetailStates?.[workflowVersionId] : undefined
      const workflowDetailState: WorkflowDetailState = !workflowVersionId
        ? 'not_applicable'
        : requestedDetailState === 'error'
          ? 'error'
          : detailed
            ? 'loaded'
            : requestedDetailState === 'loading'
              ? 'loading'
              : workflow
                ? 'summary'
                : 'missing'
      if (workflowDetailState === 'error') routeIssues.push('工作流输入合同详情读取失败。')
      const workflowAuditWarning = asString(workflow?.smokeFixtureSchemaError)
      if (workflowAuditWarning) routeIssues.push(`工作流烟测输入 Schema 不符合当前合同：${workflowAuditWarning}`)
      const inputContract = asRecord(workflow?.inputContract)
      return {
        id: `${index}:${routeIndex}`,
        index: routeIndex,
        modelDeploymentId,
        modelDeploymentLabel: deployment?.name || deployment?.key || `部署 ${shortId(modelDeploymentId)}`,
        modelDeploymentStatus: asString(deployment?.status),
        providerDriver,
        modelProfileVersionId,
        modelProfileLabel: modelProfile ? `${modelProfile.profileKey} · v${modelProfile.version}` : modelProfileVersionId ? `模型版本 ${shortId(modelProfileVersionId)}` : '未声明模型版本',
        modelProfileStatus: asString(modelProfile?.status),
        modelIdentifier: modelProfile?.modelIdentifier || '',
        modelModality: modelProfile?.modality || '',
        providerKey: modelProfile?.providerKey || '',
        workflowVersionId,
        workflowVersionLabel,
        workflowStatus: asString(workflow?.status),
        workflowOutputKind: asString(workflow?.outputKind),
        workflowDetailState,
        workflowDetailError: workflowVersionId ? lookups.workflowDetailErrors?.[workflowVersionId] || '' : '',
        workflowAuditWarning,
        inputContractSchemaVersion: asInteger(inputContract?.schema_version),
        inputContractSha256: asString(workflow?.inputContractSha256),
        semanticBindingCount: Number.isFinite(workflow?.semanticBindingCount) ? Number(workflow?.semanticBindingCount) : 0,
        inputs: detailed ? workflowInputs(workflow) : [],
        dynamicInputs: detailed ? workflowDynamicInputs(workflow) : [],
        modelRequirements: detailed ? workflowRequirements(workflow?.modelRequirements) : [],
        outputs: detailed ? workflowOutputs(workflow) : [],
        issues: routeIssues,
      }
    })
    const extras = Object.fromEntries(Object.entries(stage || {}).filter(([field]) => !['key', 'mode', 'approval', 'routing', 'decision_policy'].includes(field)))
    const phase = phaseFor(key)
    const routingMode = asString(routing?.mode)
    if (routing && !routingMode) stageIssues.push(`${stageLabelSafe(key)} 缺少 routing.mode。`)
    else if (routingMode && !routingModeLabels[routingMode]) stageIssues.push(`${stageLabelSafe(key)} 的 routing.mode ${routingMode} 无法识别。`)
    const variantCount = asInteger(routing?.variant_count)
    if (routing && variantCount === null) stageIssues.push(`${stageLabelSafe(key)} 缺少整数 variant_count。`)
    issues.push(...stageIssues.map((message) => `阶段 ${index + 1}：${message}`))
    for (const route of routes) issues.push(...route.issues.map((message) => `阶段 ${index + 1} / 路由 ${route.index + 1}：${message}`))
    return {
      id: `stage-${index}-${key || 'unknown'}`,
      index,
      key,
      label: stageLabelSafe(key),
      phase,
      phaseLabel: phaseLabels[phase],
      mode,
      modeLabel: mode ? modeLabel(mode) : '未声明执行方式',
      approval,
      approvalLabel: approvalLabels[approval] || approval || '未声明审核门禁',
      hasRouting: Boolean(routing),
      routingRequired,
      routingMode,
      routingModeLabel: routing
        ? routingModeLabels[routingMode] || routingMode || '缺少路由策略'
        : routingRequired ? '缺少路由' : '不适用',
      variantCount,
      evaluator: routing?.evaluator ?? null,
      decisionPolicy: asString(stage?.decision_policy),
      routes,
      extras,
      issues: [...stageIssues, ...routes.flatMap((route) => route.issues)],
    }
  })

  const stages = rawStages.map((stage, index): ProductionRecipeFlowStage => ({
    ...stage,
    previousDeclaration: index > 0 ? {
      index: index - 1,
      key: rawStages[index - 1].key,
      label: rawStages[index - 1].label,
    } : null,
    nextDeclaration: index < rawStages.length - 1 ? {
      index: index + 1,
      key: rawStages[index + 1].key,
      label: rawStages[index + 1].label,
    } : null,
  }))
  if (stages.length && stages[stages.length - 1]?.key !== 'delivery') issues.push('最后一个阶段不是 delivery，当前投影无法证明内容到交付闭环。')
  const topLevelExtras = Object.fromEntries(Object.entries(definition || {}).filter(([field]) => !['schema_version', 'interaction_mode', 'stages'].includes(field)))
  return {
    schemaVersion,
    interactionMode,
    stages,
    stageCount: stages.length,
    routeCount: stages.reduce((total, stage) => total + stage.routes.length, 0),
    reviewGateCount: stages.filter((stage) => stage.mode === 'review' || !['', 'auto'].includes(stage.approval)).length,
    manualStageCount: stages.filter((stage) => stage.mode === 'manual' || stage.approval === 'required').length,
    firstStageLabel: stages[0]?.label || '未声明起点',
    lastStageLabel: stages[stages.length - 1]?.label || '未声明终点',
    topLevelExtras,
    issues,
  }
}
