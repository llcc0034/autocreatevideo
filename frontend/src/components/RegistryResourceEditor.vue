<script setup lang="ts">
import { computed, defineAsyncComponent, reactive, ref, useId, watch } from 'vue'
import { AlertTriangle, Braces, Plus, Trash2 } from 'lucide-vue-next'
import RegistryAuthoringFrame from './RegistryAuthoringFrame.vue'
import DeploymentParameterSource from './DeploymentParameterSource.vue'
import { isModelResource, registryAuthorSections, registryAuthorFieldSection } from '@/services/registryAuthoringPresentation'
import type { AuthoringIssue } from '@/services/configurationAuthoring'
import ProductionRecipeEditor from '@/components/ProductionRecipeEditor.vue'
import CatalogChoiceField from '@/components/CatalogChoiceField.vue'
import ModelConditioningContractEditor from '@/components/ModelConditioningContractEditor.vue'
import ModelTextExecutionEditor from '@/components/ModelTextExecutionEditor.vue'
import ModelScriptContractsEditor from '@/components/ModelScriptContractsEditor.vue'
import ModelAsyncMediaEditor from '@/components/ModelAsyncMediaEditor.vue'
import { assertAsyncMediaUnchanged } from '@/services/modelAsyncMedia'
import { assertConditioningUnchanged } from '@/services/modelReferenceTopology'
import { assertScriptContractsUnchanged } from '@/services/modelScriptContracts'
import ModelChatRequestOptionsEditor from '@/components/ModelChatRequestOptionsEditor.vue'
import { assertChatOptionsUnchanged } from '@/services/modelChatRequestOptions'
import { assertTextParametersUnchanged } from '@/services/modelTextExecution'
import ModelMediaConstraintsEditor from '@/components/ModelMediaConstraintsEditor.vue'
const ModelVisualRulesOutputEditor = defineAsyncComponent(() => import('@/components/ModelVisualRulesOutputEditor.vue'))
import { assertVisualRulesAdvisoriesUnchanged } from '@/services/visualRulesOutput'
import { assertMediaConstraintsUnchanged } from '@/services/modelMediaConstraints'
import ModelNodeIdleOverridesEditor from '@/components/ModelNodeIdleOverridesEditor.vue'
import {
  applyResourceDefaults,
  getPath,
  resourceEditorFields,
  setPath,
  validateRegistryResource,
  type RegistryEditorDependencies,
  type RegistryFieldSchema,
  type RegistryResource,
} from '@/services/registryResourceSchema'
import type { JsonObject } from '@/services/platformTypes'

const props = defineProps<{
  resource: RegistryResource
  modelValue: JsonObject
  dependencies: RegistryEditorDependencies
  disabled?: boolean
  authoring?: boolean
  authorStatus?: string
  authorIssues?: AuthoringIssue[]
}>()
const emit = defineEmits<{
  'update:modelValue': [value: JsonObject]
  validity: [value: { valid: boolean; errors: string[]; warnings: string[] }]
}>()

const controlPrefix=useId()
const hierarchical=computed(()=>Boolean(props.authoring && isModelResource(props.resource)))
const sections=computed(()=>hierarchical.value?registryAuthorSections(props.resource):[{key:'all',title:'',description:''}])
const inSection=(field: RegistryFieldSchema,key:string)=>key==='all'||registryAuthorFieldSection(props.resource,field)===key
const jsonDrafts = reactive<Record<string, string>>({})
const jsonErrors = reactive<Record<string, string>>({})
const jsonDraftBases = reactive<Record<string, string>>({})
const conditioningErrors = reactive<{ values: string[] }>({ values: [] })
const textExecutionErrors = reactive<{ values: string[] }>({ values: [] })
const scriptContractErrors = reactive<{ values: string[] }>({ values: [] })
const asyncMediaErrors = reactive<{ values: string[] }>({ values: [] })
const modelProvider = computed(() => props.dependencies.providers.find(row=>row.id===props.modelValue.provider))
const chatOptionErrors = reactive<Record<string, string[]>>({})
const mediaConstraintsErrors = reactive<{ values: string[] }>({ values: [] })
const showVisualRules = computed(() => props.resource === 'model' && (Array.isArray(getPath(props.modelValue,'specification.capabilities')) && (getPath(props.modelValue,'specification.capabilities') as unknown[]).includes('visual_rules.generate') || getPath(props.modelValue,'specification.visual_rules.quality_advisories') !== undefined))
const nodeIdleErrors = reactive<{ values: string[] }>({ values: [] })
const fields = computed(() => resourceEditorFields(props.resource, props.modelValue, props.dependencies))
const normalFields = computed(() => fields.value.filter((field) => !field.advanced && field.kind !== 'chat-options'))
const chatFields = computed(() => fields.value.filter((field) => field.kind === 'chat-options'))
const advancedFields = computed(() => fields.value.filter((field) => field.advanced))
const jsonFields = computed(() => advancedFields.value.filter((field) => field.kind === 'json'))
const currentJsonField = (field: RegistryFieldSchema) => jsonFields.value.find((current) => current.path === field.path)
const jsonBase = (path: string) => JSON.stringify(getPath(props.modelValue, path)) ?? 'undefined'
const jsonConflict = (field: RegistryFieldSchema) => `${field.label}的同一配置已由表单或其他操作修改；请先撤销此 JSON 草稿并载入当前配置，再重新编辑`
const loadJsonDraft = (field: RegistryFieldSchema) => {
  jsonDrafts[field.path] = JSON.stringify(getPath(props.modelValue, field.path) ?? (field.path === 'initial_value' ? null : {}), null, 2)
  jsonErrors[field.path] = ''
  delete jsonDraftBases[field.path]
}
const syncJsonDrafts = () => {
  for (const path of Object.keys(jsonDrafts)) {
    if (!jsonFields.value.some((field) => field.path === path)) {
      delete jsonDrafts[path]
      delete jsonErrors[path]
      delete jsonDraftBases[path]
    }
  }
  for (const field of jsonFields.value) {
    if (!jsonErrors[field.path]) loadJsonDraft(field)
    else if (jsonDraftBases[field.path] !== jsonBase(field.path)) jsonErrors[field.path] = jsonConflict(field)
  }
}
watch([() => props.resource, () => props.modelValue.id], () => {
  for (const path of Object.keys(jsonDrafts)) {
    delete jsonDrafts[path]
    delete jsonErrors[path]
    delete jsonDraftBases[path]
  }
  syncJsonDrafts()
}, { flush: 'sync' })
const numberDrafts = reactive<Record<string, { raw: string; error: string }>>({})
const numberFields = computed(() => normalFields.value.filter((field) => field.kind === 'number'))
const currentNumberField = (field: RegistryFieldSchema) => numberFields.value.find((current) => current.path === field.path)
watch(numberFields, (active) => {
  for (const path of Object.keys(numberDrafts)) {
    if (!active.some((field) => field.path === path)) delete numberDrafts[path]
  }
}, { flush: 'sync' })
watch([() => props.resource, () => props.modelValue.id], () => {
  for (const path of Object.keys(numberDrafts)) delete numberDrafts[path]
}, { flush: 'sync' })
const arrayTagPaths = new Set([
  'driver_config.terminal_success', 'driver_config.terminal_failure', 'driver_config.terminal_cancelled',
  'runtime_config.terminal_success', 'runtime_config.terminal_failure', 'runtime_config.terminal_cancelled',
  'driver_config.artifact_allowed_hosts', 'runtime_config.artifact_allowed_hosts',
  'specification.capabilities', 'impact_modules', 'value_schema.enum',
])
watch([
  () => props.resource,
  () => props.modelValue.driver_type,
  () => props.modelValue.provider,
  () => props.modelValue.profile_version,
  () => props.modelValue.modality,
  () => props.modelValue.profile_key,
], () => {
  const withDefaults = applyResourceDefaults(props.resource, props.modelValue, props.dependencies)
  if (JSON.stringify(withDefaults) !== JSON.stringify(props.modelValue)) emit('update:modelValue', withDefaults)
}, { immediate: true })
watch([jsonFields, () => props.modelValue], syncJsonDrafts, { immediate: true, deep: true })

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
  const active = currentNumberField(field)
  if (props.disabled || !active) return
  const input = event.target as HTMLInputElement
  const raw = input.value
  if (input.validity.badInput || (raw !== '' && !Number.isFinite(Number(raw)))) {
    numberDrafts[field.path] = { raw, error: `${active.label}含未完成数字或非有效有限数字；原配置保留，请修正或撤销后再保存` }
    return
  }
  delete numberDrafts[field.path]
  update(field.path, raw === '' ? undefined : Number(raw))
}
const numberValue = (field: RegistryFieldSchema) => numberDrafts[field.path]?.raw ?? scalarValue(field)
const discardNumberDraft = (field: RegistryFieldSchema) => {
  if (!props.disabled && currentNumberField(field)) delete numberDrafts[field.path]
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
  const active = currentJsonField(field)
  if (props.disabled || !active) return
  const raw = (event.target as HTMLTextAreaElement).value
  jsonDrafts[field.path] = raw
  if (Object.prototype.hasOwnProperty.call(jsonDraftBases, field.path) && jsonDraftBases[field.path] !== jsonBase(field.path)) {
    jsonErrors[field.path] = jsonConflict(active)
    return
  }
  jsonDraftBases[field.path] = jsonBase(field.path)
  try {
    const value = JSON.parse(raw)
    if (props.resource === 'model' && field.path === 'specification') assertMediaConstraintsUnchanged(getPath(props.modelValue,field.path), value)
    if (props.resource === 'model' && field.path === 'specification') assertChatOptionsUnchanged(getPath(props.modelValue,field.path), value)
    if (props.resource === 'model' && field.path === 'specification') assertTextParametersUnchanged(getPath(props.modelValue,field.path), value)
    if (props.resource === 'model' && field.path === 'specification') assertScriptContractsUnchanged(getPath(props.modelValue,field.path), value, props.dependencies.modelProfileOptions?.scriptContracts)
    if (props.resource === 'model' && field.path === 'specification') assertAsyncMediaUnchanged(getPath(props.modelValue,field.path), value)
    if (props.resource === 'model' && field.path === 'specification') assertConditioningUnchanged(getPath(props.modelValue,field.path), value)
    if (props.resource === 'model' && field.path === 'specification') assertVisualRulesAdvisoriesUnchanged(getPath(props.modelValue,field.path), value)
    jsonErrors[field.path] = ''
    delete jsonDraftBases[field.path]
    update(field.path, value)
  } catch (error) {
    jsonErrors[field.path] = error instanceof SyntaxError ? `${active.label}不是合法 JSON，尚未写入表单草稿` : String(error)
  }
}
const discardJsonDraft = (field: RegistryFieldSchema) => {
  const active = currentJsonField(field)
  if (!props.disabled && active) loadJsonDraft(active)
}
const updateAdvancedText = (field: RegistryFieldSchema, event: Event) => {
  if (!props.disabled && advancedFields.value.some((current) => current.path === field.path && current.kind === 'text')) updateText(field, event)
}

const recipeErrors = ref<string[]>([])
const validation = computed(() => {
  const base = validateRegistryResource(props.resource, props.modelValue, props.dependencies)
  const errors = [...base.errors, ...Object.values(jsonErrors).filter(Boolean), ...Object.values(numberDrafts).map((draft) => draft.error)]
  if (props.resource === 'model') errors.push(...textExecutionErrors.values)
  if (props.resource === 'model') errors.push(...scriptContractErrors.values)
  if (props.resource === 'model') errors.push(...asyncMediaErrors.values)
  if (props.resource === 'model') errors.push(...chatFields.value.flatMap(field => chatOptionErrors[field.path] || []))
  if (props.resource === 'model' && (['image','video','multimodal'].includes(String(props.modelValue.modality)) || getPath(props.modelValue,'specification.media_constraints') != null)) errors.push(...mediaConstraintsErrors.values)
  if (props.resource === 'model' && (['image','video','multimodal'].includes(String(props.modelValue.modality)) || getPath(props.modelValue,'specification.conditioning_contract'))) errors.push(...conditioningErrors.values)
  if (advancedFields.value.some(field => field.kind === 'node-idle-overrides')) errors.push(...nodeIdleErrors.values)
  if (props.resource === 'recipe') errors.push(...recipeErrors.value)
  return { errors: [...new Set(errors)], warnings: base.warnings }
})

watch(validation, (value) => emit('validity', { valid: value.errors.length === 0, ...value }), { immediate: true, deep: true })
</script>

<template>
  <div class="registry-resource-editor" :class="{ 'registry-authoring': hierarchical }">
    <RegistryAuthoringFrame :enabled="hierarchical" :sections="sections" :status="authorStatus || '尚未保存'" :busy="disabled" :issues="authorIssues">
    <template v-for="section in sections" :key="section.key" #[section.key]>
    <slot :name="section.key" />
    <div class="registry-form-grid">
      <div v-for="field in normalFields.filter(field=>inSection(field,section.key))" :key="field.path" :data-authoring-field="field.path" class="registry-field" :class="{wide:field.kind==='textarea'||field.kind==='multiselect'}">
        <span class="registry-field-title"><label :for="`${controlPrefix}-${field.path}`">{{ field.label }}</label><i v-if="field.required">必填</i></span>
        <textarea :id="`${controlPrefix}-${field.path}`" :aria-label="field.label" v-if="field.kind==='textarea'" :value="scalarValue(field)" rows="3" :placeholder="field.placeholder" :disabled="disabled" @input="updateText(field,$event)"></textarea>
        <select :id="`${controlPrefix}-${field.path}`" :aria-label="field.label" v-else-if="field.kind==='select'" :value="scalarValue(field)" :disabled="disabled" @change="updateSelect(field,$event)">
          <option value="">{{ field.required?'请选择前置项':'不设置 / 继承' }}</option>
          <option v-for="option in field.options || []" :key="option.value" :value="option.value" :disabled="option.disabled">{{ option.label }}</option>
        </select>
        <CatalogChoiceField :input-id="`${controlPrefix}-${field.path}`" :input-label="field.label" v-else-if="field.kind==='catalog'" :model-value="scalarValue(field)" :options="field.options || []" :allow-custom="field.allowCustom" :disabled="disabled" :pattern="field.pattern" :custom-placeholder="field.placeholder || '填写新的稳定值'" @update:model-value="update(field.path,$event)"/>
        <div v-else-if="field.kind==='multiselect'" class="registry-multiselect" role="group" :aria-label="field.label">
          <label v-for="option in field.options || []" :key="option.value" :class="{selected:multipleValues(field).includes(option.value),disabled:option.disabled}"><input type="checkbox" :checked="multipleValues(field).includes(option.value)" :disabled="disabled||option.disabled" @change="toggleMultiple(field,option.value,$event)"/><span>{{ option.label }}</span><small v-if="option.hint">{{ option.hint }}</small></label>
          <p v-if="!field.options?.length">当前前置选择下没有可用项；请检查服务端目录、Provider 和主模态。</p>
          <input v-if="field.allowCustom" class="registry-multiselect-custom" type="text" :placeholder="field.placeholder || '登记新值后按 Enter'" :disabled="disabled" @keydown.enter.prevent="addCustomMultiple(field,$event)" @change="addCustomMultiple(field,$event)"/>
        </div>
        <output :id="`${controlPrefix}-${field.path}`" v-else-if="field.kind==='readonly'" class="registry-readonly">{{ readonlyValue(field) }}</output>
        <label v-else-if="field.kind==='boolean'" class="registry-switch"><input :id="`${controlPrefix}-${field.path}`" :aria-label="field.label" type="checkbox" :checked="getPath(modelValue,field.path)===true" :disabled="disabled" @change="update(field.path,($event.target as HTMLInputElement).checked)"/><span>{{ getPath(modelValue,field.path)===true?'启用':'停用' }}</span></label>
        <template v-else-if="field.kind==='number'">
          <input :id="`${controlPrefix}-${field.path}`" type="number" :aria-label="field.label" :aria-invalid="!!numberDrafts[field.path]" :value="numberValue(field)" :min="field.min" :max="field.max" :step="field.step || 'any'" :placeholder="field.placeholder" :disabled="disabled" @input="updateNumber(field,$event)"/>
          <template v-if="numberDrafts[field.path]">
            <small class="registry-number-error" role="alert">{{ numberDrafts[field.path].error }}</small>
            <button type="button" class="button ghost small" :aria-label="`撤销${field.label}的未完成输入`" :disabled="disabled" @click="discardNumberDraft(field)">撤销未完成输入（保留原配置）</button>
          </template>
        </template>
        <input :id="`${controlPrefix}-${field.path}`" :aria-label="field.label" v-else-if="field.kind==='tags'" type="text" :value="tagValue(field)" :placeholder="field.placeholder || '多个值用逗号分隔'" :disabled="disabled" @input="updateTags(field,$event)"/>
        <div v-else-if="field.kind==='mapping'" class="registry-mapping">
          <div v-for="([key,target]) in mappingEntries(field)" :key="key" class="registry-mapping-row">
            <select :value="key" :disabled="disabled" @change="updateMappingKey(field,key,($event.target as HTMLSelectElement).value)"><option v-for="option in field.options || []" :key="option.value" :value="option.value" :disabled="option.value!==key&&Object.prototype.hasOwnProperty.call(mappingValue(field),option.value)">{{ option.label }}</option></select>
            <span>→</span><input type="text" :value="String(target || '')" placeholder="远程字段名" :disabled="disabled" @input="updateMappingTarget(field,key,($event.target as HTMLInputElement).value)"/>
            <button type="button" aria-label="删除字段映射" :disabled="disabled" @click.prevent="removeMappingEntry(field,key)"><Trash2 :size="12"/></button>
          </div>
          <button type="button" class="button ghost tiny" :disabled="disabled||!availableMappingKeys(field).length" @click.prevent="addMappingEntry(field)"><Plus :size="11"/>添加语义映射</button>
        </div>
        <input :id="`${controlPrefix}-${field.path}`" :aria-label="field.label" v-else type="text" :value="scalarValue(field)" :pattern="field.pattern" :placeholder="field.placeholder" :disabled="disabled" @input="updateText(field,$event)"/>
        <small>{{ field.description }}</small>
        <DeploymentParameterSource v-if="hierarchical && resource==='deployment'" :field="field" :payload="modelValue" :dependencies="dependencies" />
        <em>影响：{{ field.impact }}<template v-if="field.defaultValue!==undefined"> · 默认 {{ JSON.stringify(field.defaultValue) }}</template></em>
      </div>
    </div>

    <ModelChatRequestOptionsEditor v-for="field in chatFields.filter(field=>inSection(field,section.key))" :key="field.path" :label="field.label" :source-path="field.path" :model-value="getPath(modelValue,field.path)" :options="dependencies.modelProfileOptions?.chatRequestOptions" :disabled="disabled" @update:model-value="update(field.path,$event)" @validity="chatOptionErrors[field.path]=$event"/>
    <div v-if="section.key==='all' || section.key==='dependencies'">
    <ModelTextExecutionEditor v-if="resource==='model'" :model-value="getPath(modelValue,'specification.text_execution')" :capabilities="getPath(modelValue,'specification.capabilities')" :options="dependencies.modelProfileOptions?.textExecution" :chat-options="dependencies.modelProfileOptions?.chatRequestOptions" :disabled="disabled" @update:model-value="update('specification.text_execution',$event)" @validity="textExecutionErrors.values=$event"/>
    <ModelScriptContractsEditor v-if="resource==='model'" :model-value="getPath(modelValue,'specification')" :options="dependencies.modelProfileOptions?.scriptContracts" :chat-options="dependencies.modelProfileOptions?.chatRequestOptions" :disabled="disabled" @update:model-value="update('specification',$event)" @validity="scriptContractErrors.values=$event"/>
    <ModelAsyncMediaEditor v-if="resource==='model'" :model-value="getPath(modelValue,'specification')" :options="dependencies.modelProfileOptions?.asyncMedia" :required="modelProvider?.driverType==='async_rest'" :request-field-map="modelProvider?.driverConfig.request_field_map" :disabled="disabled" @update:model-value="update('specification',$event)" @validity="asyncMediaErrors.values=$event"/>
    <template v-if="showVisualRules">
      <p>影视规则质量建议已移至「执行实例 → 管理业务输出版本」。新模型版本不再配置此项；新建输出版本后，由执行实例明确选择。</p>
      <ModelVisualRulesOutputEditor v-if="getPath(modelValue,'specification.visual_rules.quality_advisories')!==undefined" :model-value="getPath(modelValue,'specification.visual_rules.quality_advisories')" :options="dependencies.modelProfileOptions?.visualRulesOutput" :disabled="true"/>
      <button v-if="getPath(modelValue,'specification.visual_rules.quality_advisories')!==undefined" type="button" :disabled="disabled" @click="update('specification.visual_rules.quality_advisories',undefined)">从本草稿移除旧质量建议（不修改已发布模型）</button>
    </template>
    <ModelMediaConstraintsEditor v-if="resource==='model' && (['image','video','multimodal'].includes(String(modelValue.modality)) || getPath(modelValue,'specification.media_constraints') != null)" :model-value="getPath(modelValue,'specification.media_constraints')" :options="dependencies.modelProfileOptions?.mediaConstraints" :disabled="disabled" @update:model-value="update('specification.media_constraints',$event)" @validity="mediaConstraintsErrors.values=$event"/>
    <ModelConditioningContractEditor v-if="resource==='model' && (['image','video','multimodal'].includes(String(modelValue.modality)) || getPath(modelValue,'specification.conditioning_contract'))" :model-value="getPath(modelValue,'specification.conditioning_contract')" :options="dependencies.modelProfileOptions?.conditioningContract" :model-profiles="dependencies.modelProfiles" :disabled="disabled" @update:model-value="update('specification.conditioning_contract',$event)" @validity="conditioningErrors.values=$event"/>

    </div>
    <ProductionRecipeEditor v-if="resource==='recipe'" :model-value="getPath(modelValue,'definition') as JsonObject" :options="dependencies.recipeEditorOptions" :disabled="disabled" @update:model-value="update('definition',$event)" @validity="recipeErrors=$event"/>

    <details v-for="field in advancedFields.filter(field=>inSection(field,section.key))" :key="field.path" :data-authoring-field="field.path" class="registry-advanced">
      <summary><span><Braces v-if="field.kind==='json'" :size="14"/><strong>{{ field.label }}</strong></span><small>{{ field.kind==='node-idle-overrides' ? '可选 · 通常无需设置' : field.kind==='json' ? '高级 JSON · 同一对象完整视图' : field.kind==='readonly' ? '只读配置' : '高级配置' }}</small></summary>
      <ModelNodeIdleOverridesEditor v-if="field.kind==='node-idle-overrides'" :model-value="getPath(modelValue,field.path)" :provider-id="String(modelValue.provider || '')" :deployments="dependencies.deployments" :limits="dependencies.executionMonitoringLimits" :disabled="disabled" @update:model-value="update(field.path,$event)" @validity="nodeIdleErrors.values=$event"/>
      <template v-else>
        <p>{{ field.description }} <em>影响：{{ field.impact }}</em></p>
        <output v-if="field.kind==='readonly'" class="registry-readonly">{{ readonlyValue(field) }}</output>
        <input v-else-if="field.kind==='text'" type="text" :aria-label="field.label" :value="scalarValue(field)" :placeholder="field.placeholder" :disabled="disabled" @input="updateAdvancedText(field,$event)"/>
        <template v-else-if="field.kind==='json'">
          <textarea :aria-label="field.label" :aria-invalid="!!jsonErrors[field.path]" :value="jsonDrafts[field.path]" rows="10" spellcheck="false" :disabled="disabled" @input="updateJson(field,$event)"></textarea>
          <template v-if="jsonErrors[field.path]">
            <div class="registry-json-error" role="alert"><AlertTriangle :size="14"/>{{ jsonErrors[field.path] }}</div>
            <button type="button" class="button ghost small registry-json-reset" :aria-label="`撤销${field.label}的 JSON 草稿并载入当前配置`" :disabled="disabled" @click="discardJsonDraft(field)">撤销此 JSON 草稿并载入当前配置</button>
          </template>
        </template>
      </template>
    </details>

    </template>
    <template #feedback><slot name="feedback" />
    <div v-if="validation.errors.length" class="registry-validation danger"><AlertTriangle :size="16"/><div><strong>保存前必须修复 {{ validation.errors.length }} 项</strong><ul><li v-for="item in validation.errors" :key="item">{{ item }}</li></ul></div></div>
    <div v-if="validation.warnings.length" class="registry-validation warning"><AlertTriangle :size="16"/><div><strong>配置提示</strong><ul><li v-for="item in validation.warnings" :key="item">{{ item }}</li></ul></div></div>
    </template>
    <template v-if="$slots.actions" #actions><slot name="actions" /></template>
    </RegistryAuthoringFrame>
  </div>
</template>

<style scoped>
.registry-resource-editor{display:grid;gap:12px}.registry-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.registry-field{display:grid;align-content:start;gap:6px;min-width:0;padding:11px;border:1px solid #252b34;border-radius:8px;background:#0b0e12}.registry-field.wide{grid-column:1/-1}.registry-field-title{display:flex;align-items:center;justify-content:space-between;gap:8px}.registry-field-title label{color:#d7dde5;font-size:var(--config-body-size);font-weight:600}.registry-field-title i{padding:2px 5px;border-radius:4px;background:rgba(217,147,54,.14);color:#e3a350;font-size:var(--config-help-size);font-style:normal}.registry-field input,.registry-field select,.registry-field textarea,.recipe-stage-fields input,.recipe-stage-fields select,.recipe-routes input,.recipe-routes select{min-width:0;width:100%;border:1px solid #303743;border-radius:7px;background:#070a0e;color:#f0f3f7;padding:9px 10px}.registry-field small{color:#87919e;font-size:var(--config-help-size);line-height:1.5}.registry-field em{color:#66717e;font-size:var(--config-help-size);font-style:normal;line-height:1.45}.registry-switch{display:flex;align-items:center;gap:8px;min-height:37px}.registry-switch input{width:auto}.registry-mapping{display:grid;gap:6px}.registry-mapping-row{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr) auto;align-items:center;gap:5px}.registry-mapping-row>button{display:grid;place-items:center;width:34px;height:34px;border:1px solid #303743;border-radius:6px;background:#0b0f14;color:#9aa5b2;cursor:pointer}.recipe-structure-editor{display:grid;gap:9px;border:1px solid #2b323c;border-radius:8px;background:#090c10;overflow:hidden}.recipe-structure-editor>header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px;border-bottom:1px solid #252b34}.recipe-structure-editor>header>div{display:grid;gap:3px}.recipe-structure-editor>header small{color:#77818d;font-size:var(--config-help-size)}.registry-row-actions{display:flex;flex-wrap:wrap;gap:5px}.registry-row-actions>button:not(.button),.recipe-route-row>button{display:grid;place-items:center;width:30px;height:30px;border:1px solid #2d3440;border-radius:6px;background:#0d1116;color:#9aa5b2;cursor:pointer}.registry-row-actions>button:disabled,.recipe-route-row>button:disabled{opacity:.4;cursor:not-allowed}.registry-prerequisite,.registry-validation{display:flex;gap:9px;align-items:flex-start;margin:0 10px;padding:10px;border:1px solid rgba(226,163,80,.35);border-radius:7px;background:rgba(226,163,80,.07);color:#d8b37b;font-size:var(--config-help-size)}.recipe-stage-list{display:grid;gap:8px;padding:0 10px 10px}.recipe-stage-card{border:1px solid #252c35;border-radius:7px;background:#0c1015;overflow:hidden}.recipe-stage-card>header{display:flex;align-items:center;justify-content:space-between;padding:8px 9px;border-bottom:1px solid #252c35}.recipe-stage-card>header strong{font-size:var(--config-help-size);color:#adb7c3}.recipe-stage-fields{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;padding:9px}.recipe-stage-fields label,.recipe-routes label{display:grid;gap:4px;color:#77818d;font-size:var(--config-help-size)}.recipe-routes{display:grid;gap:7px;padding:9px;border-top:1px solid #252c35;background:#090c10}.recipe-routes>header{display:grid;grid-template-columns:minmax(0,1fr) 110px auto;align-items:end;gap:7px}.recipe-route-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) auto;align-items:end;gap:7px}.recipe-routes p,.registry-empty{margin:0;color:#9b7d58;font-size:var(--config-help-size)}.registry-advanced{border:1px solid #292f38;border-radius:8px;background:#090c10}.registry-advanced summary{display:flex;align-items:center;justify-content:space-between;padding:10px 11px;cursor:pointer}.registry-advanced summary span{display:flex;align-items:center;gap:7px}.registry-advanced summary strong{font-size:var(--config-help-size);color:#aeb8c4}.registry-advanced summary small{color:#68737f;font-size:var(--config-help-size)}.registry-advanced>p{margin:0;padding:0 11px 8px;color:#7f8995;font-size:var(--config-help-size)}.registry-advanced>p em{color:#b38a58;font-style:normal}.registry-advanced>textarea{width:calc(100% - 22px);margin:0 11px 11px;padding:10px;border:1px solid #303743;border-radius:7px;background:#06090d;color:#c7d0db;font:var(--config-help-size)/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}.registry-json-error{display:flex;gap:6px;align-items:center;margin:0 11px 11px;color:#ff9393;font-size:var(--config-help-size)}.registry-validation{margin:0}.registry-validation.danger{border-color:rgba(202,82,82,.38);background:rgba(202,82,82,.08);color:#ff9c9c}.registry-validation ul{margin:5px 0 0;padding-left:18px}.registry-validation li+li{margin-top:3px}
.registry-readonly{min-height:38px;padding:9px 10px;border:1px dashed #303743;border-radius:7px;background:#080b0f;color:#aeb8c4;font:var(--config-help-size)/1.6 ui-monospace,SFMono-Regular,Consolas,monospace}.registry-multiselect{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:6px}.registry-multiselect>label{min-width:0;display:grid;grid-template-columns:auto minmax(0,1fr);align-items:start;gap:3px 7px;padding:8px;border:1px solid #2b323c;border-radius:7px;background:#080b0f;color:#9ba6b2;cursor:pointer}.registry-multiselect>label.selected{border-color:rgba(103,157,219,.42);background:rgba(103,157,219,.08);color:#b8d2ed}.registry-multiselect>label.disabled{opacity:.48;cursor:not-allowed}.registry-multiselect input{width:auto;margin-top:2px;padding:0}.registry-multiselect label>small{grid-column:2;color:#77818d;font-size:var(--config-help-size);line-height:1.4}.registry-multiselect>p{grid-column:1/-1;margin:0;padding:9px;border:1px dashed #303743;border-radius:7px;color:#b98b59;font-size:var(--config-help-size)}
.registry-multiselect>.registry-multiselect-custom{grid-column:1/-1;width:100%;padding:9px 10px;margin:0}
.registry-field .registry-number-error{color:#ff9c9c;overflow-wrap:anywhere}
.registry-json-error{overflow-wrap:anywhere;align-items:flex-start}.registry-json-error>svg{flex-shrink:0}.registry-json-reset{margin:0 11px 11px;max-width:calc(100% - 22px);white-space:normal}.registry-advanced>input,.registry-advanced>output{display:block;width:calc(100% - 22px);margin:0 11px 11px;padding:9px 10px;border:1px solid #303743;border-radius:7px;background:#06090d;color:#c7d0db}
@media(max-width:760px){.registry-form-grid,.recipe-stage-fields{grid-template-columns:1fr}.registry-field.wide{grid-column:auto}.recipe-structure-editor>header{align-items:flex-start;flex-direction:column}.recipe-routes>header,.recipe-route-row{grid-template-columns:1fr}}
.registry-authoring{font-family:var(--font);font-size:var(--config-body-size)}
.registry-authoring .registry-field{padding:0;border:0;border-radius:0;background:transparent}
.registry-authoring .registry-form-grid{gap:var(--config-space-lg);margin-bottom:var(--config-space-lg)}
.registry-authoring :deep(input),.registry-authoring :deep(select),.registry-authoring :deep(textarea){font-size:var(--config-body-size)}
.registry-authoring :deep(.text-execution-editor),.registry-authoring :deep(.media-constraints-editor),.registry-authoring :deep(.conditioning-editor){margin-block:var(--config-space-lg);font-size:var(--config-body-size)}
.registry-authoring :deep(.text-execution-editor p),.registry-authoring :deep(.text-execution-editor small),.registry-authoring :deep(.media-constraints-editor p),.registry-authoring :deep(.media-constraints-editor small),.registry-authoring :deep(.conditioning-editor p),.registry-authoring :deep(.conditioning-editor small),.registry-authoring :deep(.conditioning-editor header span){font-size:var(--config-help-size)}
.registry-authoring :deep(.conditioning-editor label){font-size:var(--config-body-size)}
.registry-authoring :deep(.script-contracts-editor),.registry-authoring :deep(.async-media-editor){margin-block:var(--config-space-lg);font-size:var(--config-body-size)}
.registry-authoring :deep(.script-contracts-editor h3),.registry-authoring :deep(.async-media-editor h3){font-size:var(--config-section-size)}
.registry-authoring :deep(.script-contracts-editor h4),.registry-authoring :deep(.async-media-editor h4),.registry-authoring :deep(.async-media-editor label){font-size:var(--config-body-size)}
@media(max-width:640px){.registry-authoring :deep(button),.registry-authoring :deep(input:not([type=checkbox])),.registry-authoring :deep(select){min-height:44px}}
</style>
