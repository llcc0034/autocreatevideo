<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import { AlertTriangle, ArrowDown, ArrowUp, Braces, Plus, Trash2 } from 'lucide-vue-next'
import CatalogChoiceField from '@/components/CatalogChoiceField.vue'
import ModelConditioningContractEditor from '@/components/ModelConditioningContractEditor.vue'
import ModelNodeIdleOverridesEditor from '@/components/ModelNodeIdleOverridesEditor.vue'
import {
  applyResourceDefaults,
  getPath,
  resourceEditorFields,
  recipeStageRequiresRouting,
  recipeStageModes,
  recipeStageOptions,
  setPath,
  standardRecipeStages,
  validateRegistryResource,
  type RegistryEditorDependencies,
  type RegistryFieldSchema,
  type RegistryResource,
} from '@/services/registryResourceSchema'
import type { JsonObject, ModelDeployment } from '@/services/platformTypes'
import { driverLabel, mediaKindLabel, modeLabel, stageLabel } from '@/services/presentationLabels'

const props = defineProps<{
  resource: RegistryResource
  modelValue: JsonObject
  dependencies: RegistryEditorDependencies
  recipeVariantLimit?: number | null
  disabled?: boolean
}>()
const cloneJson = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T
const emit = defineEmits<{
  'update:modelValue': [value: JsonObject]
  validity: [value: { valid: boolean; errors: string[]; warnings: string[] }]
}>()

const jsonDrafts = reactive<Record<string, string>>({})
const jsonErrors = reactive<Record<string, string>>({})
const conditioningErrors = reactive<{ values: string[] }>({ values: [] })
const nodeIdleErrors = reactive<{ values: string[] }>({ values: [] })
const fields = computed(() => resourceEditorFields(props.resource, props.modelValue, props.dependencies))
const normalFields = computed(() => fields.value.filter((field) => !field.advanced))
const advancedFields = computed(() => fields.value.filter((field) => field.advanced))
const recipeEditorOptions = computed(() => props.dependencies.recipeEditorOptions)
const recipeModeCatalog = computed(() => recipeStageModes(recipeEditorOptions.value))
const recipeStageCatalog = computed(() => recipeStageOptions(recipeEditorOptions.value))
const arrayTagPaths = new Set([
  'driver_config.terminal_success', 'driver_config.terminal_failure', 'driver_config.terminal_cancelled',
  'runtime_config.terminal_success', 'runtime_config.terminal_failure', 'runtime_config.terminal_cancelled',
  'driver_config.artifact_allowed_hosts', 'runtime_config.artifact_allowed_hosts',
  'specification.capabilities', 'impact_modules', 'value_schema.enum',
])
watch(() => [
  props.resource,
  props.modelValue.driver_type,
  props.modelValue.provider,
  props.modelValue.profile_version,
  props.modelValue.modality,
  props.modelValue.profile_key,
] as const, () => {
  const withDefaults = applyResourceDefaults(props.resource, props.modelValue, props.dependencies)
  if (JSON.stringify(withDefaults) !== JSON.stringify(props.modelValue)) emit('update:modelValue', withDefaults)
}, { immediate: true })
watch(() => [props.resource, props.modelValue] as const, () => {
  for (const field of advancedFields.value) {
    if (field.kind === 'node-idle-overrides') continue
    jsonDrafts[field.path] = JSON.stringify(getPath(props.modelValue, field.path) ?? (field.path === 'initial_value' ? null : {}), null, 2)
    jsonErrors[field.path] = ''
  }
}, { immediate: true, deep: true })

const update = (path: string, value: unknown) => emit('update:modelValue', setPath(props.modelValue, path, value))
const scalarValue = (field: RegistryFieldSchema): string => {
  const value = getPath(props.modelValue, field.path)
  return value === undefined || value === null ? '' : String(value)
}
const tagValue = (field: RegistryFieldSchema): string => {
  const value = getPath(props.modelValue, field.path)
  return Array.isArray(value) ? value.join(', ') : String(value || '')
}
const multipleValues = (field: RegistryFieldSchema): string[] => {
  const value = getPath(props.modelValue, field.path)
  if (field.multipleValue === 'csv') return String(value || '').split(',').map((item) => item.trim()).filter(Boolean)
  return Array.isArray(value) ? value.map(String).filter(Boolean) : []
}
const toggleMultiple = (field: RegistryFieldSchema, optionValue: string, event: Event) => {
  const selected = new Set(multipleValues(field))
  if ((event.target as HTMLInputElement).checked) selected.add(optionValue)
  else selected.delete(optionValue)
  const ordered = (field.options || []).map((option) => option.value).filter((value) => selected.has(value))
  for (const value of selected) if (!ordered.includes(value)) ordered.push(value)
  update(field.path, field.multipleValue === 'csv' ? ordered.join(',') : ordered)
}
const addCustomMultiple = (field: RegistryFieldSchema, event: Event) => {
  const input = event.target as HTMLInputElement
  const value = input.value.trim()
  if (!value || multipleValues(field).includes(value)) return
  const next = [...multipleValues(field), value]
  update(field.path, field.multipleValue === 'csv' ? next.join(',') : next)
  input.value = ''
}
const readonlyValue = (field: RegistryFieldSchema): string => scalarValue(field) || field.placeholder || '由系统自动维护'
const updateText = (field: RegistryFieldSchema, event: Event) => update(field.path, (event.target as HTMLInputElement | HTMLTextAreaElement).value)
const updateNumber = (field: RegistryFieldSchema, event: Event) => {
  const raw = (event.target as HTMLInputElement).value
  update(field.path, raw === '' ? undefined : Number(raw))
}
const updateSelect = (field: RegistryFieldSchema, event: Event) => {
  const raw = (event.target as HTMLSelectElement).value
  update(field.path, field.path === 'runtime_host' && raw === '' ? null : raw)
}
const updateTags = (field: RegistryFieldSchema, event: Event) => {
  const raw = (event.target as HTMLInputElement).value
  const values = raw.split(',').map((item) => item.trim()).filter(Boolean)
  update(field.path, arrayTagPaths.has(field.path) ? values : values.join(','))
}
const mappingValue = (field: RegistryFieldSchema): JsonObject => {
  const value = getPath(props.modelValue, field.path)
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}
}
const mappingEntries = (field: RegistryFieldSchema) => Object.entries(mappingValue(field))
const availableMappingKeys = (field: RegistryFieldSchema) => (field.options || []).filter(
  (option) => !Object.prototype.hasOwnProperty.call(mappingValue(field), option.value),
)
const addMappingEntry = (field: RegistryFieldSchema) => {
  const option = availableMappingKeys(field)[0]
  if (!option) return
  update(field.path, { ...mappingValue(field), [option.value]: option.value })
}
const updateMappingKey = (field: RegistryFieldSchema, oldKey: string, newKey: string) => {
  if (!newKey || newKey === oldKey) return
  const next = { ...mappingValue(field) }
  const target = next[oldKey]
  delete next[oldKey]
  next[newKey] = target
  update(field.path, next)
}
const updateMappingTarget = (field: RegistryFieldSchema, key: string, target: string) => {
  update(field.path, { ...mappingValue(field), [key]: target })
}
const removeMappingEntry = (field: RegistryFieldSchema, key: string) => {
  const next = { ...mappingValue(field) }
  delete next[key]
  update(field.path, next)
}
const updateJson = (field: RegistryFieldSchema, event: Event) => {
  const raw = (event.target as HTMLTextAreaElement).value
  jsonDrafts[field.path] = raw
  try {
    const value = JSON.parse(raw)
    jsonErrors[field.path] = ''
    update(field.path, value)
  } catch {
    jsonErrors[field.path] = `${field.label}不是合法 JSON，尚未写入表单草稿`
  }
}

const recipeDefinition = computed<JsonObject>(() => {
  const value = getPath(props.modelValue, 'definition')
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : { schema_version: recipeEditorOptions.value?.definitionSchemaVersion, stages: [] }
})
const recipeStages = computed<JsonObject[]>(() => Array.isArray(recipeDefinition.value.stages) ? recipeDefinition.value.stages as JsonObject[] : [])
const setStages = (stages: JsonObject[]) => update('definition.stages', stages)
const installStandardStages = (mode: 'ai' | 'confirmed') => setStages(standardRecipeStages(mode, recipeEditorOptions.value))
const addStage = () => setStages([...recipeStages.value, { key: '', mode: '', approval: '' }])
const removeStage = (index: number) => setStages(recipeStages.value.filter((_, row) => row !== index))
const moveStage = (index: number, delta: number) => {
  const target = index + delta
  if (target < 0 || target >= recipeStages.value.length) return
  const stages = cloneJson(recipeStages.value)
  ;[stages[index], stages[target]] = [stages[target], stages[index]]
  setStages(stages)
}
const updateStage = (index: number, key: string, value: unknown) => {
  const stages = cloneJson(recipeStages.value)
  stages[index] = { ...stages[index], [key]: value }
  if (key === 'key') {
    const allowedModes = recipeModeCatalog.value[String(value)] || []
    if (!allowedModes.includes(String(stages[index].mode || ''))) stages[index].mode = allowedModes.length === 1 ? allowedModes[0] : ''
    if (stages[index].mode === 'review') stages[index].approval = 'required'
  }
  if (key === 'key' || key === 'mode') {
    if (recipeStageRequiresRouting(stages[index], recipeEditorOptions.value) && (!stages[index].routing || typeof stages[index].routing !== 'object')) {
      stages[index].routing = { mode: '', routes: [], evaluator: null }
    }
    if (!recipeStageRequiresRouting(stages[index], recipeEditorOptions.value)) delete stages[index].routing
  }
  setStages(stages)
}
const stageModeOptions = (stage: JsonObject) => recipeModeCatalog.value[String(stage.key || '')] || []
const routingFor = (stage: JsonObject): JsonObject => stage.routing && typeof stage.routing === 'object' && !Array.isArray(stage.routing) ? stage.routing as JsonObject : { mode: '', routes: [], evaluator: null }
const routesFor = (stage: JsonObject): JsonObject[] => Array.isArray(routingFor(stage).routes) ? routingFor(stage).routes as JsonObject[] : []
const updateRouting = (stageIndex: number, key: string, value: unknown) => {
  const stage = recipeStages.value[stageIndex]
  updateStage(stageIndex, 'routing', { ...routingFor(stage), [key]: value })
}
const addRoute = (stageIndex: number) => {
  const stage = recipeStages.value[stageIndex]
  const routes = [...routesFor(stage), { model_deployment_id: '', workflow_version_id: null }]
  updateRouting(stageIndex, 'routes', routes)
}
const updateRoute = (stageIndex: number, routeIndex: number, key: string, value: unknown) => {
  const stage = recipeStages.value[stageIndex]
  const routes = cloneJson(routesFor(stage))
  routes[routeIndex] = { ...routes[routeIndex], [key]: value || null }
  updateRouting(stageIndex, 'routes', routes)
}
const removeRoute = (stageIndex: number, routeIndex: number) => {
  const stage = recipeStages.value[stageIndex]
  updateRouting(stageIndex, 'routes', routesFor(stage).filter((_, index) => index !== routeIndex))
}
const activeDeployments = computed(() => props.dependencies.deployments.filter((item) => item.status === 'active'))
const capabilitiesForDeployment = (deployment: ModelDeployment) => {
  const profile = props.dependencies.modelProfiles.find((item) => item.id === deployment.profileVersion)
  const capabilities = profile?.specification?.capabilities
  return new Set(Array.isArray(capabilities) ? capabilities.map(String) : [])
}
const deploymentsForStage = (stage: JsonObject) => activeDeployments.value.filter((deployment) => {
  const key = String(stage.key || '')
  if (key === 'media_generation') return ['comfyui', 'async_rest'].includes(deployment.providerDriver)
  const requiredCapability = key === 'script_generate' ? 'script.generate'
    : key === 'script_structure' ? 'script.structure'
      : key === 'visual_rules_generate' ? 'visual_rules.generate'
        : key === 'script_normalize' ? 'prompt.normalize' : ''
  return !requiredCapability || capabilitiesForDeployment(deployment).has(requiredCapability)
})
const recipeVariantLimit = computed(() => Number.isSafeInteger(props.recipeVariantLimit) && Number(props.recipeVariantLimit) > 0
  ? Number(props.recipeVariantLimit) : null)
const stageVariantCount = (stage: JsonObject): number | null => {
  const value = routingFor(stage).variant_count
  return Number.isSafeInteger(value) && Number(value) > 0 ? Number(value) : null
}
const deploymentFor = (route: JsonObject): ModelDeployment | undefined => props.dependencies.deployments.find((item) => item.id === route.model_deployment_id)
const workflowOptions = (route: JsonObject) => {
  const deployment = deploymentFor(route)
  return props.dependencies.workflowVersions.filter((item) => item.status === 'published' && item.isCurrent
    && item.smokeTestState === 'passed'
    && (!deployment || !item.modelProfileVersion || item.modelProfileVersion === deployment.profileVersion))
}
const recipeErrors = computed(() => {
  if (props.resource !== 'recipe') return []
  const failures: string[] = []
  const keys = recipeStages.value.map((stage) => String(stage.key || '').trim())
  if (keys.some((key) => !key)) failures.push('所有生产阶段都必须填写阶段 Key')
  if (new Set(keys).size !== keys.length) failures.push('生产阶段 Key 不能重复')
  const unknownKeys = keys.filter((key) => key && !recipeModeCatalog.value[key])
  if (unknownKeys.length) failures.push(`当前服务端没有阶段执行器：${[...new Set(unknownKeys)].join(', ')}`)
  recipeStages.value.forEach((stage) => {
    const allowed = recipeModeCatalog.value[String(stage.key || '')]
    if (allowed && !allowed.includes(String(stage.mode || ''))) failures.push(`阶段 ${stage.key} 不支持执行方式 ${stage.mode || '空'}`)
  })
  const order = recipeStageCatalog.value.map((item) => item.value)
  const comparable = keys.filter((key) => order.includes(key))
  if (comparable.some((key, index) => index > 0 && order.indexOf(key) < order.indexOf(comparable[index - 1]))) failures.push('主阶段顺序与正式生产链不一致')
  recipeStages.value.forEach((stage, stageIndex) => {
    if (!recipeStageRequiresRouting(stage, recipeEditorOptions.value)) return
    const routes = routesFor(stage)
    const eligibleDeploymentIds = new Set(deploymentsForStage(stage).map((item) => item.id))
    const variantLimit = recipeVariantLimit.value
    if (!variantLimit) failures.push('服务端生产能力未声明 effective_limits.max_variants，不能编辑或保存包含路由的生产配方。')
    if (!routes.length) failures.push(`阶段 ${stage.key || stageIndex + 1} 至少需要一个 route`)
    if (routingFor(stage).mode === 'fixed' && routes.length !== 1) failures.push(`阶段 ${stage.key || stageIndex + 1} 使用 fixed 时只能配置一个 route`)
    const variantCount = stageVariantCount(stage)
    if (variantCount === null) failures.push(`阶段 ${stage.key || stageIndex + 1} 必须填写正整数 variant_count`)
    else if (variantLimit && variantCount > variantLimit) failures.push(`阶段 ${stage.key || stageIndex + 1} 的 variant_count 超过服务端上限 ${variantLimit}`)
    routes.forEach((route, routeIndex) => {
      const deployment = deploymentFor(route)
      if (!deployment || deployment.status !== 'active') failures.push(`阶段 ${stage.key || stageIndex + 1} 的 route ${routeIndex + 1} 必须选择 active Deployment`)
      else if (!eligibleDeploymentIds.has(deployment.id)) failures.push(`阶段 ${stage.key || stageIndex + 1} 的 route ${routeIndex + 1} 所选 Deployment 缺少对应能力或驱动类型不兼容`)
      if (deployment?.providerDriver === 'comfyui' && !route.workflow_version_id) failures.push(`阶段 ${stage.key || stageIndex + 1} 的 ComfyUI route ${routeIndex + 1} 必须选择已发布工作流`)
    })
  })
  return failures
})
const validation = computed(() => {
  const base = validateRegistryResource(props.resource, props.modelValue, props.dependencies)
  const errors = [...base.errors, ...Object.values(jsonErrors).filter(Boolean)]
  if (props.resource === 'model' && (['image','video','multimodal'].includes(String(props.modelValue.modality)) || getPath(props.modelValue,'specification.conditioning_contract'))) errors.push(...conditioningErrors.values)
  if (advancedFields.value.some(field => field.kind === 'node-idle-overrides')) errors.push(...nodeIdleErrors.values)
  if (props.resource === 'recipe') errors.push(...recipeErrors.value)
  return { errors: [...new Set(errors)], warnings: base.warnings }
})

watch(validation, (value) => emit('validity', { valid: value.errors.length === 0, ...value }), { immediate: true, deep: true })
</script>

<template>
  <div class="registry-resource-editor">
    <div class="registry-form-grid">
      <div v-for="field in normalFields" :key="field.path" class="registry-field" :class="{wide:field.kind==='textarea'||field.kind==='multiselect'}">
        <span class="registry-field-title"><strong>{{ field.label }}</strong><i v-if="field.required">必填</i></span>
        <textarea v-if="field.kind==='textarea'" :value="scalarValue(field)" rows="3" :placeholder="field.placeholder" :disabled="disabled" @input="updateText(field,$event)"></textarea>
        <select v-else-if="field.kind==='select'" :value="scalarValue(field)" :disabled="disabled" @change="updateSelect(field,$event)">
          <option value="">{{ field.required?'请选择前置项':'不设置 / 继承' }}</option>
          <option v-for="option in field.options || []" :key="option.value" :value="option.value" :disabled="option.disabled">{{ option.label }}</option>
        </select>
        <CatalogChoiceField v-else-if="field.kind==='catalog'" :model-value="scalarValue(field)" :options="field.options || []" :allow-custom="field.allowCustom" :disabled="disabled" :pattern="field.pattern" :custom-placeholder="field.placeholder || '填写新的稳定值'" @update:model-value="update(field.path,$event)"/>
        <div v-else-if="field.kind==='multiselect'" class="registry-multiselect" role="group" :aria-label="field.label">
          <label v-for="option in field.options || []" :key="option.value" :class="{selected:multipleValues(field).includes(option.value),disabled:option.disabled}"><input type="checkbox" :checked="multipleValues(field).includes(option.value)" :disabled="disabled||option.disabled" @change="toggleMultiple(field,option.value,$event)"/><span>{{ option.label }}</span><small v-if="option.hint">{{ option.hint }}</small></label>
          <p v-if="!field.options?.length">当前前置选择下没有可用项；请检查服务端目录、Provider 和主模态。</p>
          <input v-if="field.allowCustom" class="registry-multiselect-custom" type="text" :placeholder="field.placeholder || '登记新值后按 Enter'" :disabled="disabled" @keydown.enter.prevent="addCustomMultiple(field,$event)" @change="addCustomMultiple(field,$event)"/>
        </div>
        <output v-else-if="field.kind==='readonly'" class="registry-readonly">{{ readonlyValue(field) }}</output>
        <label v-else-if="field.kind==='boolean'" class="registry-switch"><input type="checkbox" :checked="getPath(modelValue,field.path)===true" :disabled="disabled" @change="update(field.path,($event.target as HTMLInputElement).checked)"/><span>{{ getPath(modelValue,field.path)===true?'启用':'停用' }}</span></label>
        <input v-else-if="field.kind==='number'" type="number" :value="scalarValue(field)" :min="field.min" :max="field.max" :step="field.step || 'any'" :placeholder="field.placeholder" :disabled="disabled" @input="updateNumber(field,$event)"/>
        <input v-else-if="field.kind==='tags'" type="text" :value="tagValue(field)" :placeholder="field.placeholder || '多个值用逗号分隔'" :disabled="disabled" @input="updateTags(field,$event)"/>
        <div v-else-if="field.kind==='mapping'" class="registry-mapping">
          <div v-for="([key,target]) in mappingEntries(field)" :key="key" class="registry-mapping-row">
            <select :value="key" :disabled="disabled" @change="updateMappingKey(field,key,($event.target as HTMLSelectElement).value)"><option v-for="option in field.options || []" :key="option.value" :value="option.value" :disabled="option.value!==key&&Object.prototype.hasOwnProperty.call(mappingValue(field),option.value)">{{ option.label }}</option></select>
            <span>→</span><input type="text" :value="String(target || '')" placeholder="远程字段名" :disabled="disabled" @input="updateMappingTarget(field,key,($event.target as HTMLInputElement).value)"/>
            <button type="button" aria-label="删除字段映射" :disabled="disabled" @click.prevent="removeMappingEntry(field,key)"><Trash2 :size="12"/></button>
          </div>
          <button type="button" class="button ghost tiny" :disabled="disabled||!availableMappingKeys(field).length" @click.prevent="addMappingEntry(field)"><Plus :size="11"/>添加语义映射</button>
        </div>
        <input v-else type="text" :value="scalarValue(field)" :pattern="field.pattern" :placeholder="field.placeholder" :disabled="disabled" @input="updateText(field,$event)"/>
        <small>{{ field.description }}</small>
        <em>影响：{{ field.impact }}<template v-if="field.defaultValue!==undefined"> · 默认 {{ JSON.stringify(field.defaultValue) }}</template></em>
      </div>
    </div>

    <ModelConditioningContractEditor v-if="resource==='model' && (['image','video','multimodal'].includes(String(modelValue.modality)) || getPath(modelValue,'specification.conditioning_contract'))" :model-value="getPath(modelValue,'specification.conditioning_contract')" :options="dependencies.modelProfileOptions?.conditioningContract" :model-profiles="dependencies.modelProfiles" :disabled="disabled" @update:model-value="update('specification.conditioning_contract',$event)" @validity="conditioningErrors.values=$event"/>

    <section v-if="resource==='recipe'" class="recipe-structure-editor">
      <header>
        <div><strong>生产阶段与路由（Stages & Routes）</strong><small>下拉选项只来自当前服务端记录；缺少启用的模型部署（active Deployment）或已发布工作流（Workflow）时会阻断保存。</small></div>
        <span class="registry-row-actions"><button type="button" class="button ghost tiny" :disabled="disabled" @click="installStandardStages('ai')">套用 AI 起稿链路</button><button type="button" class="button ghost tiny" :disabled="disabled" @click="installStandardStages('confirmed')">套用已确认剧本链路</button><button type="button" class="button ghost tiny" :disabled="disabled" @click="addStage"><Plus :size="11"/>添加阶段</button></span>
      </header>
      <div class="registry-prerequisite"><AlertTriangle :size="15"/><span>当前结构化编辑器用于人工辅助配方。全自动模式（unattended）必须复核并选用已审核发布的 schema v2 配方；不要在不了解合同的情况下直接改高级 JSON。</span></div>
      <div v-if="!recipeVariantLimit" class="registry-prerequisite"><AlertTriangle :size="15"/><span>服务端尚未提供 effective_limits.max_variants；为避免使用本地变体上限，当前不能编辑或保存包含路由的生产配方。</span></div>
      <div v-if="!activeDeployments.length" class="registry-prerequisite"><AlertTriangle :size="15"/><span>尚无启用的模型部署（active Deployment）。请先发布模型版本并创建可用部署，再配置需要模型调用的阶段。</span></div>
      <div class="recipe-stage-list">
        <article v-for="(stage,stageIndex) in recipeStages" :key="`${stageIndex}:${stage.key}`" class="recipe-stage-card">
          <header><strong>{{ stageIndex + 1 }} · {{ stage.key ? stageLabel(stage.key) : '未命名阶段' }}</strong><span class="registry-row-actions"><button type="button" aria-label="上移阶段" :disabled="disabled||stageIndex===0" @click="moveStage(stageIndex,-1)"><ArrowUp :size="12"/></button><button type="button" aria-label="下移阶段" :disabled="disabled||stageIndex===recipeStages.length-1" @click="moveStage(stageIndex,1)"><ArrowDown :size="12"/></button><button type="button" aria-label="删除阶段" :disabled="disabled" @click="removeStage(stageIndex)"><Trash2 :size="12"/></button></span></header>
          <div class="recipe-stage-fields"><label>阶段标识（Stage Key）<select :value="String(stage.key || '')" :disabled="disabled" @change="updateStage(stageIndex,'key',($event.target as HTMLSelectElement).value)"><option value="">请选择已注册执行阶段</option><option v-for="option in recipeStageCatalog" :key="option.value" :value="option.value">{{ option.label }}</option></select></label><label>执行方式（Mode）<select :value="String(stage.mode || '')" :disabled="disabled" @change="updateStage(stageIndex,'mode',($event.target as HTMLSelectElement).value)"><option value="">请选择服务端允许的执行方式</option><option v-for="mode in stageModeOptions(stage)" :key="mode" :value="mode">{{ modeLabel(mode) }}</option></select></label><label>审核门禁（Approval Gate）<select :value="String(stage.approval || '')" :disabled="disabled" @change="updateStage(stageIndex,'approval',($event.target as HTMLSelectElement).value)"><option value="">请选择服务端允许的审核门禁</option><option v-for="option in recipeEditorOptions?.approvalOptions || []" :key="option.value" :value="option.value">{{ option.label }}</option></select></label></div>
          <section v-if="recipeStageRequiresRouting(stage,recipeEditorOptions)" class="recipe-routes">
            <header><label>路由策略（Routing Mode）<select :value="String(routingFor(stage).mode || '')" :disabled="disabled" @change="updateRouting(stageIndex,'mode',($event.target as HTMLSelectElement).value)"><option value="">请选择路由策略</option><option v-for="option in recipeEditorOptions?.routingModeOptions || []" :key="option.value" :value="option.value">{{ modeLabel(option.value) }}</option></select></label><label>变体数<input type="number" min="1" :max="recipeVariantLimit || undefined" step="1" :value="stageVariantCount(stage) ?? ''" :disabled="disabled||!recipeVariantLimit" @input="updateRouting(stageIndex,'variant_count',($event.target as HTMLInputElement).value === '' ? undefined : Number(($event.target as HTMLInputElement).value))"/><small v-if="recipeVariantLimit">服务端上限 {{ recipeVariantLimit }}</small></label><button type="button" class="button ghost tiny" :disabled="disabled||!deploymentsForStage(stage).length||!recipeVariantLimit" @click="addRoute(stageIndex)"><Plus :size="11"/>添加路由（Route）</button></header>
            <div v-if="!deploymentsForStage(stage).length" class="registry-prerequisite"><AlertTriangle :size="14"/><span>没有启用且能力匹配的模型部署（active Deployment）；请先配置 {{ stageLabel(stage.key) }} 所需模型能力与驱动。</span></div>
            <div v-for="(route,routeIndex) in routesFor(stage)" :key="routeIndex" class="recipe-route-row"><label>模型部署（Deployment）<select :value="String(route.model_deployment_id || '')" :disabled="disabled" @change="updateRoute(stageIndex,routeIndex,'model_deployment_id',($event.target as HTMLSelectElement).value)"><option value="">请先选择能力匹配的启用部署</option><option v-for="item in deploymentsForStage(stage)" :key="item.id" :value="item.id">{{ item.name || item.key }} · {{ driverLabel(item.providerDriver) }}</option></select></label><label>工作流（Workflow）<select :value="String(route.workflow_version_id || '')" :disabled="disabled" @change="updateRoute(stageIndex,routeIndex,'workflow_version_id',($event.target as HTMLSelectElement).value)"><option value="">文本 / 异步接口（async REST）不绑定工作流</option><option v-for="item in workflowOptions(route)" :key="item.id" :value="item.id">{{ item.templateName || item.templateSlug }} v{{ item.version }} · {{ mediaKindLabel(item.outputKind) }}</option></select></label><button type="button" aria-label="删除路由" :disabled="disabled" @click="removeRoute(stageIndex,routeIndex)"><Trash2 :size="12"/></button></div>
            <p v-if="!routesFor(stage).length">尚未配置路由（route）；该阶段发布时会被服务端阻断。</p>
          </section>
        </article>
        <p v-if="!recipeStages.length" class="registry-empty">请选择一条标准链路，或手工添加阶段。空阶段列表（stages）无法发布。</p>
      </div>
    </section>

    <details v-for="field in advancedFields" :key="field.path" class="registry-advanced">
      <summary><span><Braces v-if="field.kind!=='node-idle-overrides'" :size="14"/><strong>{{ field.label }}</strong></span><small>{{ field.kind==='node-idle-overrides' ? '可选 · 通常无需设置' : '高级 JSON · 同一对象完整视图' }}</small></summary>
      <ModelNodeIdleOverridesEditor v-if="field.kind==='node-idle-overrides'" :model-value="getPath(modelValue,field.path)" :provider-id="String(modelValue.provider || '')" :deployments="dependencies.deployments" :limits="dependencies.executionMonitoringLimits" :disabled="disabled" @update:model-value="update(field.path,$event)" @validity="nodeIdleErrors.values=$event"/>
      <template v-else>
        <p>{{ field.description }} <em>影响：{{ field.impact }}</em></p>
        <textarea :value="jsonDrafts[field.path]" rows="10" spellcheck="false" :disabled="disabled" @input="updateJson(field,$event)"></textarea>
        <div v-if="jsonErrors[field.path]" class="registry-json-error"><AlertTriangle :size="14"/>{{ jsonErrors[field.path] }}</div>
      </template>
    </details>

    <div v-if="validation.errors.length" class="registry-validation danger"><AlertTriangle :size="16"/><div><strong>保存前必须修复 {{ validation.errors.length }} 项</strong><ul><li v-for="item in validation.errors" :key="item">{{ item }}</li></ul></div></div>
    <div v-if="validation.warnings.length" class="registry-validation warning"><AlertTriangle :size="16"/><div><strong>配置提示</strong><ul><li v-for="item in validation.warnings" :key="item">{{ item }}</li></ul></div></div>
  </div>
</template>

<style scoped>
.registry-resource-editor{display:grid;gap:12px}.registry-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.registry-field{display:grid;align-content:start;gap:6px;min-width:0;padding:11px;border:1px solid #252b34;border-radius:8px;background:#0b0e12}.registry-field.wide{grid-column:1/-1}.registry-field-title{display:flex;align-items:center;justify-content:space-between;gap:8px}.registry-field-title strong{color:#d7dde5;font-size:12px}.registry-field-title i{padding:2px 5px;border-radius:4px;background:rgba(217,147,54,.14);color:#e3a350;font-size:9px;font-style:normal}.registry-field input,.registry-field select,.registry-field textarea,.recipe-stage-fields input,.recipe-stage-fields select,.recipe-routes input,.recipe-routes select{min-width:0;width:100%;border:1px solid #303743;border-radius:7px;background:#070a0e;color:#f0f3f7;padding:9px 10px}.registry-field small{color:#87919e;font-size:10px;line-height:1.5}.registry-field em{color:#66717e;font-size:9px;font-style:normal;line-height:1.45}.registry-switch{display:flex;align-items:center;gap:8px;min-height:37px}.registry-switch input{width:auto}.registry-mapping{display:grid;gap:6px}.registry-mapping-row{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr) auto;align-items:center;gap:5px}.registry-mapping-row>button{display:grid;place-items:center;width:34px;height:34px;border:1px solid #303743;border-radius:6px;background:#0b0f14;color:#9aa5b2;cursor:pointer}.recipe-structure-editor{display:grid;gap:9px;border:1px solid #2b323c;border-radius:8px;background:#090c10;overflow:hidden}.recipe-structure-editor>header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px;border-bottom:1px solid #252b34}.recipe-structure-editor>header>div{display:grid;gap:3px}.recipe-structure-editor>header small{color:#77818d;font-size:9px}.registry-row-actions{display:flex;flex-wrap:wrap;gap:5px}.registry-row-actions>button:not(.button),.recipe-route-row>button{display:grid;place-items:center;width:30px;height:30px;border:1px solid #2d3440;border-radius:6px;background:#0d1116;color:#9aa5b2;cursor:pointer}.registry-row-actions>button:disabled,.recipe-route-row>button:disabled{opacity:.4;cursor:not-allowed}.registry-prerequisite,.registry-validation{display:flex;gap:9px;align-items:flex-start;margin:0 10px;padding:10px;border:1px solid rgba(226,163,80,.35);border-radius:7px;background:rgba(226,163,80,.07);color:#d8b37b;font-size:11px}.recipe-stage-list{display:grid;gap:8px;padding:0 10px 10px}.recipe-stage-card{border:1px solid #252c35;border-radius:7px;background:#0c1015;overflow:hidden}.recipe-stage-card>header{display:flex;align-items:center;justify-content:space-between;padding:8px 9px;border-bottom:1px solid #252c35}.recipe-stage-card>header strong{font-size:10px;color:#adb7c3}.recipe-stage-fields{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;padding:9px}.recipe-stage-fields label,.recipe-routes label{display:grid;gap:4px;color:#77818d;font-size:9px}.recipe-routes{display:grid;gap:7px;padding:9px;border-top:1px solid #252c35;background:#090c10}.recipe-routes>header{display:grid;grid-template-columns:minmax(0,1fr) 110px auto;align-items:end;gap:7px}.recipe-route-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) auto;align-items:end;gap:7px}.recipe-routes p,.registry-empty{margin:0;color:#9b7d58;font-size:10px}.registry-advanced{border:1px solid #292f38;border-radius:8px;background:#090c10}.registry-advanced summary{display:flex;align-items:center;justify-content:space-between;padding:10px 11px;cursor:pointer}.registry-advanced summary span{display:flex;align-items:center;gap:7px}.registry-advanced summary strong{font-size:11px;color:#aeb8c4}.registry-advanced summary small{color:#68737f;font-size:9px}.registry-advanced>p{margin:0;padding:0 11px 8px;color:#7f8995;font-size:10px}.registry-advanced>p em{color:#b38a58;font-style:normal}.registry-advanced>textarea{width:calc(100% - 22px);margin:0 11px 11px;padding:10px;border:1px solid #303743;border-radius:7px;background:#06090d;color:#c7d0db;font:11px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}.registry-json-error{display:flex;gap:6px;align-items:center;margin:0 11px 11px;color:#ff9393;font-size:10px}.registry-validation{margin:0}.registry-validation.danger{border-color:rgba(202,82,82,.38);background:rgba(202,82,82,.08);color:#ff9c9c}.registry-validation ul{margin:5px 0 0;padding-left:18px}.registry-validation li+li{margin-top:3px}
.registry-readonly{min-height:38px;padding:9px 10px;border:1px dashed #303743;border-radius:7px;background:#080b0f;color:#aeb8c4;font:11px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace}.registry-multiselect{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:6px}.registry-multiselect>label{min-width:0;display:grid;grid-template-columns:auto minmax(0,1fr);align-items:start;gap:3px 7px;padding:8px;border:1px solid #2b323c;border-radius:7px;background:#080b0f;color:#9ba6b2;cursor:pointer}.registry-multiselect>label.selected{border-color:rgba(103,157,219,.42);background:rgba(103,157,219,.08);color:#b8d2ed}.registry-multiselect>label.disabled{opacity:.48;cursor:not-allowed}.registry-multiselect input{width:auto;margin-top:2px;padding:0}.registry-multiselect label>small{grid-column:2;color:#77818d;font-size:9px;line-height:1.4}.registry-multiselect>p{grid-column:1/-1;margin:0;padding:9px;border:1px dashed #303743;border-radius:7px;color:#b98b59;font-size:10px}
.registry-multiselect>.registry-multiselect-custom{grid-column:1/-1;width:100%;padding:9px 10px;margin:0}
@media(max-width:760px){.registry-form-grid,.recipe-stage-fields{grid-template-columns:1fr}.registry-field.wide{grid-column:auto}.recipe-structure-editor>header{align-items:flex-start;flex-direction:column}.recipe-routes>header,.recipe-route-row{grid-template-columns:1fr}}
</style>
