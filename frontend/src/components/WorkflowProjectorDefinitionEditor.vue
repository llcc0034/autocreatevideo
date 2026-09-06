<script setup lang="ts">
import { computed, getCurrentInstance, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { AlertTriangle, Braces, Plus, Trash2 } from 'lucide-vue-next'
import WorkflowAssetGroupConfigEditor from './WorkflowAssetGroupConfigEditor.vue'
import WorkflowAssetContractSource from './WorkflowAssetContractSource.vue'
import WorkflowResolutionPolicyEditor from './WorkflowResolutionPolicyEditor.vue'
import WorkflowParameterRangeEditor from './WorkflowParameterRangeEditor.vue'
import WorkflowClassifiedReferenceEditor from './WorkflowClassifiedReferenceEditor.vue'
import { assetGroupForSlot } from '@/services/workflowAssetContractSource'
import { useWorkflowProjectorContext } from '@/services/workflowProjectorContext'
import type {
  JsonObject,
  ModelProfileVersion,
  WorkflowProjectorEditorField,
  WorkflowProjectorOptions,
  WorkflowProjectorRecommendation,
  WorkflowProjectorVersion,
} from '@/services/platformTypes'
import {
  cloneWorkflowProjectorDefinition,
  getWorkflowProjectorPath,
  isJsonObject,
  parseWorkflowProjectorJson,
  setWorkflowProjectorPath,
  workflowProjectorDefinitionErrors,
  workflowProjectorDefinitionSummary,
  workflowProjectorEngine,
} from '@/services/workflowProjectorDefinition'

const props = defineProps<{
  modelValue: JsonObject
  options: WorkflowProjectorOptions
  engineKey: string
  engineVersion: number
  recommendation?: WorkflowProjectorRecommendation | null
  disabled?: boolean
  readOnly?: boolean
  modelProfiles?: ModelProfileVersion[]
  modelProfilesError?: string
  initialModelProfileId?: string
  workflowContextId?: string
  projectorVersion?: WorkflowProjectorVersion
}>()
const emit = defineEmits<{
  'update:modelValue': [value: JsonObject]
  validity: [value: { valid: boolean; errors: string[] }]
}>()

const activeSectionKey = ref(props.options.definitionEditor.sections[0]?.key || '')
const disabled = computed(() => props.disabled || props.readOnly)
const { context: workflowContext, reload: reloadWorkflowContext } = useWorkflowProjectorContext(() => ({
  workflowId: props.readOnly ? props.workflowContextId : undefined, projector: props.projectorVersion,
}))
const localError = ref('')
const jsonText = ref('')
const jsonDirty = ref(false)
const jsonError = ref('')
const recommendationListPrefix = `wp-recommendation-${getCurrentInstance()?.uid ?? 0}`
const nodeTypeListId = `${recommendationListPrefix}-node-types`
const engine = computed(() => workflowProjectorEngine(
  props.options, props.engineKey, props.engineVersion,
))
const activeSection = computed(() => props.options.definitionEditor.sections.find(
  (section) => section.key === activeSectionKey.value,
) || props.options.definitionEditor.sections[0])
const summary = computed(() => workflowProjectorDefinitionSummary(props.modelValue))
const groupedAssetEditor = computed(() => engine.value?.projectionFields.some(field => ['asset_group_list', 'classified_reference_config'].includes(field.control)))
const projectionSection = computed(() => props.options.definitionEditor.sections.find(section => section.fields.some(field => field.control === 'projection_config')))
const validationErrors = computed(() => {
  const errors = workflowProjectorDefinitionErrors(
    props.modelValue, props.options, props.engineKey, props.engineVersion,
  )
  if (jsonDirty.value) errors.push(jsonError.value || '高级 JSON 有未应用的修改')
  if (localError.value) errors.push(localError.value)
  return [...new Set(errors.filter(Boolean))]
})

watch(validationErrors, (errors) => emit('validity', { valid: errors.length === 0, errors }), { immediate: true })
watch(() => props.options.definitionEditor.sections, (sections) => {
  if (!sections.some((section) => section.key === activeSectionKey.value)) {
    activeSectionKey.value = sections[0]?.key || ''
  }
}, { deep: true })
watch(() => props.modelValue, (value) => {
  if (!jsonDirty.value) jsonText.value = JSON.stringify(value, null, 2)
}, { immediate: true, deep: true })

const eventText = (event: Event) => (event.target as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement).value
const eventNumber = (event: Event) => Number(eventText(event))
const eventChecked = (event: Event) => (event.target as HTMLInputElement).checked
const text = (value: unknown) => value === undefined || value === null ? '' : String(value)
const integer = (value: unknown) => Number.isSafeInteger(value) ? Number(value) : 0
const bool = (value: unknown) => value === true
const choiceValue = (choices: ReadonlyArray<{ value: string }>, preferred: string) => (
  choices.some((choice) => choice.value === preferred) ? preferred : choices[0]?.value || ''
)
const objectAt = (path: string): JsonObject => {
  const value = getWorkflowProjectorPath(props.modelValue, path)
  return isJsonObject(value) ? value : {}
}
const arrayAt = (path: string): JsonObject[] => {
  const value = getWorkflowProjectorPath(props.modelValue, path)
  return Array.isArray(value) ? value.filter(isJsonObject) : []
}
const stringArrayAt = (path: string): string[] => {
  const value = getWorkflowProjectorPath(props.modelValue, path)
  return Array.isArray(value) ? value.map(String) : []
}
const update = (path: string, value: unknown) => {
  if (disabled.value) return
  localError.value = ''
  emit('update:modelValue', setWorkflowProjectorPath(props.modelValue, path, value))
}
const nextKey = (path: string, prefix: string) => {
  const existing = objectAt(path)
  let index = 1
  while (Object.prototype.hasOwnProperty.call(existing, `${prefix}_${index}`)) index += 1
  return `${prefix}_${index}`
}
const addMapRow = (path: string, prefix: string, value: unknown) => {
  const key = nextKey(path, prefix)
  update(path, { ...objectAt(path), [key]: value })
}
const removeMapRow = (path: string, key: string) => {
  const next = { ...objectAt(path) }
  delete next[key]
  update(path, next)
}
const updateMapValue = (path: string, key: string, value: unknown) => {
  update(path, { ...objectAt(path), [key]: value })
}
const updateNestedMapValue = (path: string, key: string, field: string, value: unknown) => {
  const current = objectAt(path)
  const row = isJsonObject(current[key]) ? current[key] as JsonObject : {}
  update(path, { ...current, [key]: { ...row, [field]: value } })
}
const roleReferences = (role: string) => {
  const edgeRefs = arrayAt('required_edges').filter((row) => row.from_role === role || row.to_role === role).length
  const parameterRefs = arrayAt('parameter_policies').filter((row) => row.node_role === role).length
  const groupRefs = arrayAt('projection.config.groups').filter((row) => [row.source_role,row.output_role,row.reducer_role,...(Array.isArray(row.branch_roles)?row.branch_roles:[]),...(Array.isArray(row.target_roles)?row.target_roles:[])].includes(role)).length
  return edgeRefs + parameterRefs + groupRefs
}
const renameMapKey = (path: string, oldKey: string, nextKeyValue: string) => {
  const value = nextKeyValue.trim()
  if (!value || value === oldKey) return
  const current = objectAt(path)
  if (Object.prototype.hasOwnProperty.call(current, value)) {
    localError.value = `名称 ${value} 已存在，未执行重命名。`
    return
  }
  if (path === 'node_roles' && roleReferences(oldKey)) {
    localError.value = `角色 ${oldKey} 仍被必要连接或参数策略引用；请先调整引用，再重命名。`
    return
  }
  const next: JsonObject = {}
  Object.entries(current).forEach(([key, row]) => { next[key === oldKey ? value : key] = row })
  update(path, next)
}
const removeRole = (role: string) => {
  const references = roleReferences(role)
  if (references) {
    localError.value = `角色 ${role} 仍有 ${references} 处必要连接或参数策略引用，不能删除。`
    return
  }
  removeMapRow('node_roles', role)
}
const addListRow = (path: string, value: JsonObject) => update(path, [...arrayAt(path), value])
const removeListRow = (path: string, index: number) => update(path, arrayAt(path).filter((_, row) => row !== index))
const updateListRow = (path: string, index: number, field: string, value: unknown) => {
  const rows = cloneWorkflowProjectorDefinition({ rows: arrayAt(path) }).rows as JsonObject[]
  rows[index] = { ...rows[index], [field]: value }
  update(path, rows)
}
const roleKeys = computed(() => Object.keys(objectAt('node_roles')))
const recommendationNodeTypes = computed(() => props.recommendation?.catalog.nodeTypes.map((row) => row.value) || [])
const inputListId = (nodeType: string) => `${recommendationListPrefix}-inputs-${nodeType.replace(/[^a-zA-Z0-9_-]+/g, '-')}`
const inputsForNodeType = (nodeType: unknown) => props.recommendation?.catalog.inputsByNodeType[String(nodeType || '')] || []
const inputListForNodeType = (nodeType: unknown) => inputsForNodeType(nodeType).length ? inputListId(String(nodeType)) : undefined
const roleNodeType = (role: unknown) => {
  const row = objectAt('node_roles')[String(role || '')]
  return isJsonObject(row) ? text(row.node_type) : ''
}
const scalarType = (value: unknown) => value === null ? 'null'
  : typeof value === 'boolean' ? 'boolean'
    : typeof value === 'number' ? Number.isInteger(value) ? 'integer' : 'number'
      : 'string'
const defaultScalar = (kind: string): unknown => kind === 'null' ? null
  : kind === 'boolean' ? false
    : kind === 'integer' || kind === 'number' ? 0 : ''
const changeListScalarType = (path: string, index: number, kind: string) => {
  updateListRow(path, index, 'value', defaultScalar(kind))
}
const changeParameterPolicy = (path: string, index: number, event: Event) => {
  const policy = eventText(event)
  const rows = cloneWorkflowProjectorDefinition({ rows: arrayAt(path) }).rows as JsonObject[]
  rows[index] = {
    ...rows[index],
    policy,
    value: policy === 'fixed' ? rows[index]?.value ?? null : null,
  }
  if (!['runtime', 'object_info'].includes(policy)) delete rows[index].constraints
  update(path, rows)
}
const updateListScalar = (path: string, index: number, value: unknown, event: Event) => {
  const kind = scalarType(value)
  const raw = eventText(event)
  if (['integer', 'number'].includes(kind) && !raw.trim()) {
    localError.value = '数值不能为空；如需空值，请先把值类型改为 null。'
    return
  }
  const next = kind === 'boolean' ? raw === 'true'
    : kind === 'integer' ? Number.parseInt(raw, 10)
      : kind === 'number' ? Number.parseFloat(raw) : raw
  if (typeof next === 'number' && !Number.isFinite(next)) {
    localError.value = '请输入有限的数字。'
    return
  }
  updateListRow(path, index, 'value', next)
}
const setFallback = (event: Event) => {
  const enabled = eventChecked(event)
  if (enabled) {
    const detector = objectAt('detector')
    const hasFingerprint = Object.keys(isJsonObject(detector.node_counts) ? detector.node_counts : {}).length
      || Object.keys(isJsonObject(detector.optional_node_counts) ? detector.optional_node_counts : {}).length
      || (Array.isArray(detector.input_equals) ? detector.input_equals.length : 0)
    if (hasFingerprint && !window.confirm('切换为兜底方案会清空必需节点、可选节点和关键输入条件。是否继续？')) {
      ;(event.target as HTMLInputElement).checked = false
      return
    }
    update('detector', {
      ...detector,
      fallback: true,
      node_counts: {},
      optional_node_counts: {},
      input_equals: [],
    })
    return
  }
  update('detector.fallback', false)
}
const selectSection = (key: string) => {
  if (activeSectionKey.value === 'advanced' && jsonDirty.value && key !== 'advanced') {
    if (!window.confirm('高级 JSON 还有未应用修改。离开会放弃这些文本修改，是否继续？')) return
    jsonDirty.value = false
    jsonError.value = ''
    jsonText.value = JSON.stringify(props.modelValue, null, 2)
  }
  activeSectionKey.value = key
  localError.value = ''
}
const editJson = (event: Event) => {
  if (disabled.value) return
  jsonText.value = eventText(event)
  jsonDirty.value = true
  jsonError.value = parseWorkflowProjectorJson(jsonText.value).error
}
const applyJson = () => {
  if (disabled.value) return
  const parsed = parseWorkflowProjectorJson(jsonText.value)
  jsonError.value = parsed.error
  if (!parsed.value) return
  jsonDirty.value = false
  emit('update:modelValue', parsed.value)
}
const resetJson = () => {
  jsonText.value = JSON.stringify(props.modelValue, null, 2)
  jsonDirty.value = false
  jsonError.value = ''
}
const fieldValue = (field: WorkflowProjectorEditorField) => getWorkflowProjectorPath(props.modelValue, field.path)
</script>

<template>
  <section class="wp-definition-editor" :aria-busy="props.disabled">
    <div v-if="readOnly" class="wp-recommendation-note" aria-live="polite">
      <template v-if="workflowContext.state==='ready'">
        <div><strong>工作流绑定模型：{{ workflowContext.model.profileKey }} v{{ workflowContext.model.version }}</strong><small>{{ workflowContext.workflow.templateName }} v{{ workflowContext.workflow.version }} · 数量取自该工作流冻结合同；这里不会更换模型或重新验收。</small></div>
        <RouterLink :to="`/settings/workflows/${workflowContext.workflow.id}`">返回工作流合同</RouterLink>
      </template>
      <p v-else-if="workflowContext.state==='loading'" role="status">正在读取工作流绑定模型与冻结合同…</p>
      <template v-else-if="workflowContext.state==='error'"><p role="alert">{{ workflowContext.message }}</p><button type="button" class="button ghost small" @click="reloadWorkflowContext">重试读取上下文</button></template>
      <div v-else><strong>已发布规则 · 只读查看</strong><small>Projector 不绑定单个模型；模型对照是可选的，不改变已发布规则。具体模型与数量以工作流版本的冻结合同为准。</small></div>
    </div>
    <div v-if="recommendation" class="wp-recommendation-note">
      <div><strong>已应用候选扫描建议</strong><small>候选、实时 object_info 与转换后图已绑定到本次未保存草稿；所有图族语义仍需人工复核。</small></div>
      <code>建议 SHA {{ recommendation.recommendationSha256.slice(0,16) }} · 图 SHA {{ recommendation.sourceEvidence.convertedGraphSha256.slice(0,16) }}</code>
    </div>
    <header class="wp-definition-summary">
      <div>
        <span>当前执行解释器</span>
        <strong>{{ engine?.label || `${engineKey}/v${engineVersion}` }}</strong>
        <small>{{ engine?.description || '服务端未返回该 Engine 的表单合同，页面已失败关闭。' }}</small>
      </div>
      <dl>
        <div><dt>节点角色</dt><dd>{{ summary.roles }}</dd></div>
        <div><dt>必要连接</dt><dd>{{ summary.edges }}</dd></div>
        <div><dt>参数</dt><dd>{{ summary.parameters }}</dd></div>
        <div><dt>模型 / 素材</dt><dd>{{ summary.modelSelectors }} / {{ summary.assetSlots }}</dd></div>
      </dl>
    </header>

    <div class="wp-definition-workbench">
      <nav aria-label="工作流合同方案配置分区">
        <button
          v-for="section in options.definitionEditor.sections"
          :key="section.key"
          type="button"
          :class="{ active: activeSectionKey===section.key }"
          :aria-current="activeSectionKey===section.key?'step':undefined"
          @click="selectSection(section.key)"
        >
          <strong>{{ section.label }}</strong>
          <small>{{ section.description }}</small>
        </button>
      </nav>

      <div v-if="activeSection" class="wp-section-panel">
        <header>
          <div><strong>{{ activeSection.label }}</strong><small>{{ activeSection.description }}</small></div>
          <code>Schema v{{ options.definitionSchemaVersion }}</code>
        </header>

        <div class="wp-fields">
          <article v-for="field in activeSection.fields" :key="field.path" class="wp-field-card">
            <header><div><strong>{{ field.label }}</strong><small>{{ field.help }}</small></div><code>{{ field.path }}</code></header>

            <div v-if="field.control==='readonly'" class="wp-readonly-value">{{ text(fieldValue(field)) || '由系统维护' }}</div>

            <label v-else-if="field.control==='integer'" class="wp-inline-field">
              <span>整数值</span>
              <input type="number" :min="options.definitionEditor.limits.priorityMin" :max="options.definitionEditor.limits.priorityMax" :value="integer(fieldValue(field))" :disabled="disabled" @input="update(field.path,eventNumber($event))"/>
            </label>

            <div v-else-if="field.control==='boolean'" class="wp-switch-row">
              <label><input type="checkbox" :checked="bool(fieldValue(field))" :disabled="disabled" @change="field.path==='detector.fallback'?setFallback($event):update(field.path,eventChecked($event))"/><span>{{ bool(fieldValue(field))?'已启用':'未启用' }}</span></label>
            </div>

            <div v-else-if="field.control==='node_count_map'" class="wp-repeat-list">
              <article v-for="([key,value],index) in Object.entries(objectAt(field.path))" :key="key" class="wp-repeat-row compact">
                <label><span>节点类型</span><input :list="recommendation?nodeTypeListId:undefined" :value="key" :disabled="disabled" @change="renameMapKey(field.path,key,eventText($event))"/></label>
                <label><span>精确数量</span><input type="number" min="0" :max="options.definitionEditor.limits.nodeCountMax" :value="integer(value)" :disabled="disabled" @input="updateMapValue(field.path,key,eventNumber($event))"/></label>
                <button type="button" class="wp-icon-button" :disabled="disabled" :aria-label="`删除必需节点 ${index+1}`" @click="removeMapRow(field.path,key)"><Trash2 :size="15"/></button>
              </article>
              <button type="button" class="button ghost small" :disabled="disabled" @click="addMapRow(field.path,'NodeType',1)"><Plus :size="13"/>添加必需节点</button>
            </div>

            <div v-else-if="field.control==='node_range_map'" class="wp-repeat-list">
              <article v-for="([key,value],index) in Object.entries(objectAt(field.path))" :key="key" class="wp-repeat-row range">
                <label><span>节点类型</span><input :list="recommendation?nodeTypeListId:undefined" :value="key" :disabled="disabled" @change="renameMapKey(field.path,key,eventText($event))"/></label>
                <label><span>最小</span><input type="number" min="0" :max="options.definitionEditor.limits.nodeCountMax" :value="integer(isJsonObject(value)?value.min:0)" :disabled="disabled" @input="updateNestedMapValue(field.path,key,'min',eventNumber($event))"/></label>
                <label><span>最大</span><input type="number" min="0" :max="options.definitionEditor.limits.nodeCountMax" :value="integer(isJsonObject(value)?value.max:0)" :disabled="disabled" @input="updateNestedMapValue(field.path,key,'max',eventNumber($event))"/></label>
                <button type="button" class="wp-icon-button" :disabled="disabled" :aria-label="`删除可选节点 ${index+1}`" @click="removeMapRow(field.path,key)"><Trash2 :size="15"/></button>
              </article>
              <button type="button" class="button ghost small" :disabled="disabled" @click="addMapRow(field.path,'OptionalNode',{min:0,max:1})"><Plus :size="13"/>添加可选节点</button>
            </div>

            <div v-else-if="field.control==='input_equals_list'" class="wp-repeat-list">
              <article v-for="(row,index) in arrayAt(field.path)" :key="index" class="wp-repeat-row fingerprint">
                <label><span>节点类型</span><input :list="recommendation?nodeTypeListId:undefined" :value="text(row.node_type)" :disabled="disabled" @input="updateListRow(field.path,index,'node_type',eventText($event))"/></label>
                <label><span>输入名</span><input :list="inputListForNodeType(row.node_type)" :value="text(row.input_name)" :disabled="disabled" @input="updateListRow(field.path,index,'input_name',eventText($event))"/></label>
                <label><span>值类型</span><select :value="scalarType(row.value)" :disabled="disabled" @change="changeListScalarType(field.path,index,eventText($event))"><option v-for="option in options.definitionEditor.choices.scalarTypes" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
                <label v-if="scalarType(row.value)!=='null'"><span>期望值</span><select v-if="scalarType(row.value)==='boolean'" :value="String(row.value)" :disabled="disabled" @change="updateListScalar(field.path,index,row.value,$event)"><option value="true">true</option><option value="false">false</option></select><input v-else :type="['integer','number'].includes(scalarType(row.value))?'number':'text'" :step="scalarType(row.value)==='number'?'any':undefined" :value="text(row.value)" :disabled="disabled" @input="updateListScalar(field.path,index,row.value,$event)"/></label>
                <div v-else class="wp-null-value">null</div>
                <button type="button" class="wp-icon-button" :disabled="disabled" aria-label="删除关键输入条件" @click="removeListRow(field.path,index)"><Trash2 :size="15"/></button>
              </article>
              <button type="button" class="button ghost small" :disabled="disabled||arrayAt(field.path).length>=options.definitionEditor.limits.inputEqualsMaxItems" @click="addListRow(field.path,{node_type:'',input_name:'',value:null})"><Plus :size="13"/>添加输入条件</button>
            </div>

            <div v-else-if="field.control==='output_map'" class="wp-repeat-list">
              <article v-for="([key,value],index) in Object.entries(objectAt(field.path))" :key="key" class="wp-repeat-row compact">
                <label><span>输出节点类型</span><input :list="recommendation?nodeTypeListId:undefined" :value="key" :disabled="disabled" @change="renameMapKey(field.path,key,eventText($event))"/></label>
                <label><span>产物类型</span><select :value="text(value)" :disabled="disabled" @change="updateMapValue(field.path,key,eventText($event))"><option v-for="option in options.definitionEditor.choices.outputKinds" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
                <button type="button" class="wp-icon-button" :disabled="disabled" :aria-label="`删除输出节点 ${index+1}`" @click="removeMapRow(field.path,key)"><Trash2 :size="15"/></button>
              </article>
              <button type="button" class="button ghost small" :disabled="disabled" @click="addMapRow(field.path,'OutputNode',choiceValue(options.definitionEditor.choices.outputKinds,'image'))"><Plus :size="13"/>添加输出节点</button>
            </div>

            <div v-else-if="field.control==='model_selector_list'" class="wp-repeat-list">
              <article v-for="(row,index) in arrayAt(field.path)" :key="index" class="wp-repeat-row six">
                <label><span>节点类型</span><input :list="recommendation?nodeTypeListId:undefined" :value="text(row.node_type)" :disabled="disabled" @input="updateListRow(field.path,index,'node_type',eventText($event))"/></label>
                <label><span>模型输入名</span><input :list="inputListForNodeType(row.node_type)" :value="text(row.input_name)" :disabled="disabled" @input="updateListRow(field.path,index,'input_name',eventText($event))"/></label>
                <label><span>文件角色</span><select :value="text(row.role)" :disabled="disabled" @change="updateListRow(field.path,index,'role',eventText($event))"><option v-for="option in options.definitionEditor.choices.artifactRoles" :key="option.value" :value="option.value">{{ option.label }} · {{ option.value }}</option></select></label>
                <label><span>库存目录</span><input :value="text(row.inventory_folder)" :disabled="disabled" @input="updateListRow(field.path,index,'inventory_folder',eventText($event))"/></label>
                <label><span>库存节点类型</span><input :list="recommendation?nodeTypeListId:undefined" :value="text(row.inventory_node_type)" :disabled="disabled" @input="updateListRow(field.path,index,'inventory_node_type',eventText($event))"/></label>
                <label><span>库存输入名</span><input :list="inputListForNodeType(row.inventory_node_type)" :value="text(row.inventory_input_name)" :disabled="disabled" @input="updateListRow(field.path,index,'inventory_input_name',eventText($event))"/></label>
                <button type="button" class="wp-icon-button" :disabled="disabled" aria-label="删除模型 selector" @click="removeListRow(field.path,index)"><Trash2 :size="15"/></button>
              </article>
              <button type="button" class="button ghost small" :disabled="disabled||arrayAt(field.path).length>=options.definitionEditor.limits.modelSelectorsMaxItems" @click="addListRow(field.path,{node_type:'',input_name:'',role:choiceValue(options.definitionEditor.choices.artifactRoles,'other'),inventory_folder:'',inventory_node_type:'',inventory_input_name:''})"><Plus :size="13"/>添加模型文件识别</button>
            </div>

            <div v-else-if="field.control==='node_role_map'" class="wp-repeat-list">
              <article v-for="([key,value]) in Object.entries(objectAt(field.path))" :key="key" class="wp-repeat-row role">
                <label><span>语义角色</span><input :value="key" :disabled="disabled" @change="renameMapKey(field.path,key,eventText($event))"/></label>
                <label><span>节点类型</span><input :list="recommendation?nodeTypeListId:undefined" :value="text(isJsonObject(value)?value.node_type:'')" :disabled="disabled" @input="updateNestedMapValue(field.path,key,'node_type',eventText($event))"/></label>
                <label><span>基数</span><select :value="text(isJsonObject(value)?value.cardinality:'')" :disabled="disabled" @change="updateNestedMapValue(field.path,key,'cardinality',eventText($event))"><option v-for="option in options.definitionEditor.choices.cardinalities" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
                <button type="button" class="wp-icon-button" :disabled="disabled" aria-label="删除节点角色" @click="removeRole(key)"><Trash2 :size="15"/></button>
              </article>
              <button type="button" class="button ghost small" :disabled="disabled" @click="addMapRow(field.path,'role',{node_type:'',cardinality:choiceValue(options.definitionEditor.choices.cardinalities,'one')})"><Plus :size="13"/>添加节点角色</button>
            </div>

            <div v-else-if="field.control==='edge_list'" class="wp-repeat-list">
              <article v-for="(row,index) in arrayAt(field.path)" :key="index" class="wp-repeat-row edge">
                <label><span>来源角色</span><select :value="text(row.from_role)" :disabled="disabled" @change="updateListRow(field.path,index,'from_role',eventText($event))"><option value="">请选择</option><option v-for="role in roleKeys" :key="role" :value="role">{{ role }}</option></select></label>
                <label><span>输出索引</span><input type="number" min="0" :value="integer(row.output_index)" :disabled="disabled" @input="updateListRow(field.path,index,'output_index',eventNumber($event))"/></label>
                <label><span>目标角色</span><select :value="text(row.to_role)" :disabled="disabled" @change="updateListRow(field.path,index,'to_role',eventText($event))"><option value="">请选择</option><option v-for="role in roleKeys" :key="role" :value="role">{{ role }}</option></select></label>
                <label><span>目标输入名</span><input :list="inputListForNodeType(roleNodeType(row.to_role))" :value="text(row.input_name)" :disabled="disabled" @input="updateListRow(field.path,index,'input_name',eventText($event))"/></label>
                <button type="button" class="wp-icon-button" :disabled="disabled" aria-label="删除必要连接" @click="removeListRow(field.path,index)"><Trash2 :size="15"/></button>
              </article>
              <button type="button" class="button ghost small" :disabled="disabled||arrayAt(field.path).length>=options.definitionEditor.limits.requiredEdgesMaxItems" @click="addListRow(field.path,{from_role:roleKeys[0]||'',output_index:0,to_role:roleKeys[1]||roleKeys[0]||'',input_name:''})"><Plus :size="13"/>添加必要连接</button>
            </div>

            <div v-else-if="field.control==='parameter_policy_list'" class="wp-repeat-list">
              <article v-for="(row,index) in arrayAt(field.path)" :key="index" class="wp-repeat-row parameter">
                <label><span>语义参数 Key</span><input :value="text(row.semantic_key)" :disabled="disabled" @input="updateListRow(field.path,index,'semantic_key',eventText($event))"/></label>
                <label><span>节点角色</span><select :value="text(row.node_role)" :disabled="disabled" @change="updateListRow(field.path,index,'node_role',eventText($event))"><option value="">请选择</option><option v-for="role in roleKeys" :key="role" :value="role">{{ role }}</option></select></label>
                <label><span>输入名</span><input :list="inputListForNodeType(roleNodeType(row.node_role))" :value="text(row.input_name)" :disabled="disabled" @input="updateListRow(field.path,index,'input_name',eventText($event))"/></label>
                <label><span>治理策略</span><select :value="text(row.policy)" :disabled="disabled" @change="changeParameterPolicy(field.path,index,$event)"><option v-for="option in options.definitionEditor.choices.parameterPolicies" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
                <template v-if="row.policy==='fixed'">
                  <label><span>固定值类型</span><select :value="scalarType(row.value)" :disabled="disabled" @change="changeListScalarType(field.path,index,eventText($event))"><option v-for="option in options.definitionEditor.choices.scalarTypes" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
                  <label v-if="scalarType(row.value)!=='null'"><span>固定值</span><select v-if="scalarType(row.value)==='boolean'" :value="String(row.value)" :disabled="disabled" @change="updateListScalar(field.path,index,row.value,$event)"><option value="true">true</option><option value="false">false</option></select><input v-else :type="['integer','number'].includes(scalarType(row.value))?'number':'text'" :step="scalarType(row.value)==='number'?'any':undefined" :value="text(row.value)" :disabled="disabled" @input="updateListScalar(field.path,index,row.value,$event)"/></label>
                </template>
                <div v-else class="wp-policy-source">运行值不在 Projector 中保存</div>
                <button type="button" class="wp-icon-button" :disabled="disabled" aria-label="删除参数策略" @click="removeListRow(field.path,index)"><Trash2 :size="15"/></button>
                <WorkflowParameterRangeEditor v-if="['runtime','object_info'].includes(text(row.policy))" :model-value="row.constraints" :disabled="disabled" @update:model-value="updateListRow(field.path,index,'constraints',$event)"/>
              </article>
              <button type="button" class="button ghost small" :disabled="disabled||arrayAt(field.path).length>=options.definitionEditor.limits.parameterPoliciesMaxItems" @click="addListRow(field.path,{semantic_key:'',node_role:roleKeys[0]||'',input_name:'',policy:choiceValue(options.definitionEditor.choices.parameterPolicies,'runtime'),value:null})"><Plus :size="13"/>添加参数策略</button>
            </div>

            <div v-else-if="field.control==='asset_slot_list'" class="wp-repeat-list">
              <template v-if="groupedAssetEditor">
                <p class="wp-readonly-value">动态素材只在“{{ projectionSection?.label }}”统一编辑。这里是同一份素材槽声明的只读摘要，接线规则按 Key 引用它，不是第二份合同。</p>
                <article v-for="(row,index) in arrayAt(field.path)" :key="index" class="wp-readonly-value">
                  <strong>{{ text(row.key)||'未命名素材槽' }}</strong> · {{ options.definitionEditor.choices.mediaKinds.find(choice=>choice.value===row.media_kind)?.label || text(row.media_kind) }} · {{ options.definitionEditor.choices.contractSources.find(choice=>choice.value===row.contract_source)?.label || '未选择来源' }}
                  <p v-if="!assetGroupForSlot(modelValue,row.key)" role="alert">没有唯一的同 Key 动态组，请在统一编辑入口修复声明与接线关系。</p>
                </article>
                <p v-if="!arrayAt(field.path).length" class="wp-readonly-value">尚未创建动态素材组。</p>
                <button v-if="projectionSection" type="button" class="button ghost small" @click="selectSection(projectionSection.key)">{{ readOnly?'查看动态素材组':'编辑动态素材组' }}</button>
              </template>
              <template v-else>
              <article v-for="(row,index) in arrayAt(field.path)" :key="index" class="wp-repeat-row asset">
                <label><span>素材槽 Key</span><input :value="text(row.key)" :disabled="disabled" @input="updateListRow(field.path,index,'key',eventText($event))"/></label>
                <label><span>媒体类型</span><select :value="text(row.media_kind)" :disabled="disabled" @change="updateListRow(field.path,index,'media_kind',eventText($event))"><option v-for="option in options.definitionEditor.choices.mediaKinds" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
                <label class="wp-checkbox-field"><span>数量</span><input type="checkbox" :checked="bool(row.dynamic)" :disabled="disabled" @change="updateListRow(field.path,index,'dynamic',eventChecked($event))"/><em>{{ bool(row.dynamic)?'动态':'固定' }}</em></label>
                <label><span>合同来源</span><select :value="text(row.contract_source)" :disabled="disabled" @change="updateListRow(field.path,index,'contract_source',eventText($event))"><option v-for="option in options.definitionEditor.choices.contractSources" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
                <button type="button" class="wp-icon-button" :disabled="disabled" aria-label="删除素材槽" @click="removeListRow(field.path,index)"><Trash2 :size="15"/></button>
                <WorkflowAssetContractSource style="grid-column:1/-1" :slot="row" :definition="modelValue" :recommendation="recommendation" :model-profiles="modelProfiles" :model-profiles-error="modelProfilesError" :initial-model-profile-id="initialModelProfileId" :read-only="readOnly" :workflow-context="workflowContext"/>
              </article>
              <button type="button" class="button ghost small" :disabled="disabled||arrayAt(field.path).length>=options.definitionEditor.limits.assetSlotsMaxItems" @click="addListRow(field.path,{key:'',media_kind:choiceValue(options.definitionEditor.choices.mediaKinds,'image'),dynamic:false,contract_source:choiceValue(options.definitionEditor.choices.contractSources,'workflow')})"><Plus :size="13"/>添加素材槽</button>
              </template>
            </div>

            <dl v-else-if="field.control==='adapter_contract'" class="wp-contract-facts">
              <div><dt>Compiler</dt><dd><code>{{ engine?.adapterContract.compiler || '不可用' }}</code></dd></div>
              <div><dt>Output</dt><dd><code>{{ engine?.adapterContract.output || '不可用' }}</code></dd></div>
              <div><dt>Dynamic</dt><dd><code>{{ engine?.adapterContract.dynamic || '不可用' }}</code></dd></div>
              <small>这些 Key 是受审代码能力，不是可自由替换的业务参数。</small>
            </dl>

            <div v-else-if="field.control==='projection_config'" class="wp-projection-editor">
              <div class="wp-readonly-line"><span>Projection kind</span><code>{{ engine?.projectionKind || text(objectAt('projection').kind) }}</code></div>
              <p v-if="!engine?.projectionFields.length">当前引擎未开放额外配置。projection kind 与 Adapter 是系统执行能力身份，只读展示。</p>
              <article v-for="projectionField in engine?.projectionFields||[]" :key="projectionField.path" class="wp-projection-field">
                <header><strong>{{ projectionField.label }}</strong><small>{{ projectionField.help }}</small></header>
                <div v-if="projectionField.control==='readonly'" class="wp-readonly-value">{{ text(fieldValue(projectionField)) }}</div>
                <WorkflowClassifiedReferenceEditor v-else-if="projectionField.control==='classified_reference_config'" :model-value="modelValue" :options="options" :recommendation="recommendation" :disabled="disabled" :read-only="readOnly" :workflow-context="workflowContext" :model-profiles="modelProfiles" :model-profiles-error="modelProfilesError" :initial-model-profile-id="initialModelProfileId" @update:model-value="emit('update:modelValue',$event)"/>
                <WorkflowAssetGroupConfigEditor v-else-if="projectionField.control==='asset_group_list'" :model-value="modelValue" :options="options" :recommendation="recommendation" :disabled="disabled" :read-only="readOnly" :workflow-context="workflowContext" :model-profiles="modelProfiles" :model-profiles-error="modelProfilesError" :initial-model-profile-id="initialModelProfileId" @update:model-value="emit('update:modelValue',$event)"/>
                <WorkflowResolutionPolicyEditor v-else-if="projectionField.control==='image_resolution'" :config="objectAt('projection.config')" :options="options.imageResolution" :disabled="disabled" :read-only="readOnly" @update:config="update('projection.config',$event)"/>
                <div v-else-if="projectionField.control==='string_list'" class="wp-repeat-list">
                  <article v-for="(value,index) in stringArrayAt(projectionField.path)" :key="index" class="wp-repeat-row string-row"><input :value="value" :disabled="disabled" @input="update(projectionField.path,stringArrayAt(projectionField.path).map((item,row)=>row===index?eventText($event):item))"/><button type="button" class="wp-icon-button" :disabled="disabled" aria-label="删除提示词模板" @click="update(projectionField.path,stringArrayAt(projectionField.path).filter((_,row)=>row!==index))"><Trash2 :size="15"/></button></article>
                  <button type="button" class="button ghost small" :disabled="disabled" @click="update(projectionField.path,[...stringArrayAt(projectionField.path),''])"><Plus :size="13"/>添加模板 Key</button>
                </div>
              </article>
            </div>

            <div v-else-if="field.control==='advanced_json'" class="wp-json-editor">
              <div class="wp-json-warning"><Braces :size="16"/><p><strong>高级完整视图</strong>{{ readOnly?'此版本配置只读，可选择并复制 JSON。':'只有点击“应用到参数表单”后才会替换草稿；非法 JSON 不会覆盖最后一个有效版本。' }}</p></div>
              <textarea aria-label="Projector 完整配置 JSON" :value="jsonText" rows="26" spellcheck="false" autocomplete="off" :readonly="readOnly" :disabled="props.disabled" @input="editJson"/>
              <div v-if="!readOnly" class="wp-json-actions"><span :class="jsonError?'error':'ok'">{{ jsonError || (jsonDirty?'JSON 可解析，等待应用':'与参数表单同步') }}</span><button type="button" class="button ghost small" :disabled="disabled||!jsonDirty" @click="resetJson">放弃文本修改</button><button type="button" class="button primary small" :disabled="disabled||!jsonDirty||Boolean(jsonError)" @click="applyJson">应用到参数表单</button></div>
            </div>

            <div v-else class="wp-unsupported"><AlertTriangle :size="16"/><span>服务端返回了当前页面不支持的控件 {{ field.control }}；已失败关闭，不能保存。</span></div>
          </article>
        </div>
      </div>
    </div>

    <div v-if="validationErrors.length" class="wp-validation" role="alert">
      <AlertTriangle :size="16"/>
      <div><strong>还有 {{ validationErrors.length }} 项需要处理</strong><ul><li v-for="item in validationErrors.slice(0,6)" :key="item">{{ item }}</li></ul><small v-if="validationErrors.length>6">另有 {{ validationErrors.length-6 }} 项；服务端保存时仍会执行完整结构、秘密字段与冲突校验。</small></div>
    </div>
    <datalist v-if="recommendation" :id="nodeTypeListId"><option v-for="nodeType in recommendationNodeTypes" :key="nodeType" :value="nodeType"/></datalist>
    <datalist v-for="(inputs,nodeType) in recommendation?.catalog.inputsByNodeType||{}" :id="inputListId(nodeType)" :key="nodeType"><option v-for="input in inputs" :key="input" :value="input"/></datalist>
  </section>
</template>

<style scoped>
.wp-definition-editor{display:grid;gap:12px;min-width:0}.wp-recommendation-note{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:11px 13px;border:1px solid rgba(74,222,128,.28);border-radius:9px;background:rgba(34,197,94,.055)}.wp-recommendation-note>div{display:grid;gap:3px}.wp-recommendation-note strong{color:#b9e5cf;font-size:11px}.wp-recommendation-note small{color:#87a99a;font-size:9px;line-height:1.5}.wp-recommendation-note code{color:#80a794;font-size:9px}.wp-definition-summary{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:18px;align-items:center;padding:14px 16px;border:1px solid rgba(96,165,250,.22);border-radius:10px;background:linear-gradient(135deg,rgba(59,130,246,.075),rgba(9,12,16,.88) 58%)}.wp-definition-summary>div{min-width:0;display:grid;gap:5px}.wp-definition-summary span{color:#7f8d9b;font-size:10px;text-transform:uppercase;letter-spacing:.08em}.wp-definition-summary strong{color:#e9eef4;font-size:14px}.wp-definition-summary small{max-width:760px;color:#91a0ae;font-size:11px;line-height:1.55}.wp-definition-summary dl{display:grid;grid-template-columns:repeat(4,minmax(70px,1fr));gap:6px;margin:0}.wp-definition-summary dl div{min-width:0;padding:7px 9px;border:1px solid #29333d;border-radius:7px;background:#0b1015}.wp-definition-summary dt{color:#73808d;font-size:9px}.wp-definition-summary dd{margin:4px 0 0;color:#cad5df;font:12px Consolas,monospace}.wp-definition-workbench{min-height:510px;display:grid;grid-template-columns:220px minmax(0,1fr);overflow:hidden;border:1px solid #2a333d;border-radius:10px;background:#090d12}.wp-definition-workbench>nav{display:grid;align-content:start;border-right:1px solid #2a333d;background:#0b1016}.wp-definition-workbench>nav button{min-width:0;padding:12px 13px;display:grid;gap:4px;border:0;border-bottom:1px solid #202933;border-left:3px solid transparent;background:transparent;color:inherit;text-align:left;cursor:pointer}.wp-definition-workbench>nav button:hover{background:#111821}.wp-definition-workbench>nav button.active{border-left-color:#e4a853;background:rgba(228,168,83,.075)}.wp-definition-workbench>nav strong{color:#c5d0da;font-size:11px}.wp-definition-workbench>nav small{color:#70808f;font-size:9px;line-height:1.45}.wp-section-panel{min-width:0;display:grid;grid-template-rows:auto minmax(0,1fr)}.wp-section-panel>header{padding:12px 15px;display:flex;align-items:center;justify-content:space-between;gap:12px;border-bottom:1px solid #2a333d;background:#0c1117}.wp-section-panel>header>div{min-width:0;display:grid;gap:4px}.wp-section-panel>header strong{color:#dde5ec;font-size:13px}.wp-section-panel>header small{color:#83909d;font-size:10px;line-height:1.45}.wp-section-panel>header code{flex:0 0 auto;color:#8090a0;font-size:10px}.wp-fields{min-width:0;padding:12px;display:grid;align-content:start;gap:10px}.wp-field-card{min-width:0;display:grid;gap:11px;padding:13px;border:1px solid #27313b;border-radius:9px;background:#0b1015}.wp-field-card>header,.wp-projection-field>header,.wp-dynamic-group>header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.wp-field-card>header>div,.wp-projection-field>header{min-width:0;display:grid;gap:4px}.wp-field-card>header strong,.wp-projection-field>header strong,.wp-dynamic-group>header strong{color:#d5dee7;font-size:11px}.wp-field-card>header small,.wp-projection-field>header small,.wp-dynamic-group>header small{color:#7d8b98;font-size:9px;line-height:1.5}.wp-field-card>header code{flex:0 0 auto;max-width:40%;overflow-wrap:anywhere;color:#64717e;font-size:9px}.wp-field-card input,.wp-field-card select,.wp-field-card textarea{min-width:0;width:100%;box-sizing:border-box;border:1px solid #303b46;border-radius:7px;background:#080c10;color:#d9e2ea;padding:8px 9px;font-size:11px}.wp-field-card input:focus,.wp-field-card select:focus,.wp-field-card textarea:focus{outline:2px solid rgba(228,168,83,.45);border-color:#a7793d}.wp-field-card input:disabled,.wp-field-card select:disabled,.wp-field-card textarea:disabled{opacity:.62;cursor:not-allowed}.wp-inline-field{max-width:260px}.wp-inline-field,.wp-repeat-row label,.wp-grid-three label{min-width:0;display:grid;gap:5px}.wp-inline-field span,.wp-repeat-row label>span,.wp-grid-three label>span{color:#82909d;font-size:9px}.wp-readonly-value,.wp-readonly-line,.wp-null-value,.wp-policy-source{padding:9px 10px;border:1px solid #28333d;border-radius:7px;background:#080c11;color:#aab7c3;font:10px/1.5 Consolas,monospace}.wp-switch-row label{display:flex;align-items:center;gap:8px;color:#b7c2cc;font-size:11px}.wp-switch-row input,.wp-checkbox-field input{width:15px;height:15px;padding:0}.wp-repeat-list{display:grid;gap:8px}.wp-repeat-row{position:relative;min-width:0;padding:10px 44px 10px 10px;display:grid;gap:8px;border:1px solid #252f39;border-radius:8px;background:#090d12}.wp-repeat-row.compact{grid-template-columns:minmax(190px,1.4fr) minmax(140px,.6fr)}.wp-repeat-row.range{grid-template-columns:minmax(190px,1.4fr) repeat(2,minmax(100px,.5fr))}.wp-repeat-row.fingerprint{grid-template-columns:repeat(2,minmax(150px,1fr)) minmax(110px,.6fr) minmax(150px,1fr)}.wp-repeat-row.six{grid-template-columns:repeat(3,minmax(150px,1fr))}.wp-repeat-row.role{grid-template-columns:repeat(3,minmax(150px,1fr))}.wp-repeat-row.edge{grid-template-columns:minmax(140px,1fr) 100px minmax(140px,1fr) minmax(150px,1fr)}.wp-repeat-row.parameter{grid-template-columns:repeat(3,minmax(140px,1fr))}.wp-repeat-row.asset{grid-template-columns:repeat(4,minmax(130px,1fr))}.wp-repeat-row.target{grid-template-columns:repeat(5,minmax(120px,1fr))}.wp-repeat-row.string-row{grid-template-columns:minmax(0,1fr);padding-right:44px}.wp-icon-button{position:absolute;right:8px;top:50%;width:30px;height:30px;display:grid;place-items:center;transform:translateY(-50%);border:1px solid #3a3135;border-radius:7px;background:#151014;color:#d27b84;cursor:pointer}.wp-icon-button:disabled{opacity:.45;cursor:not-allowed}.wp-null-value,.wp-policy-source{align-self:end}.wp-checkbox-field{grid-template-columns:auto auto 1fr;align-items:center;align-content:end}.wp-checkbox-field>span{grid-column:1/-1}.wp-checkbox-field em{color:#9aa8b4;font-size:10px;font-style:normal}.wp-contract-facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0}.wp-contract-facts div{min-width:0;padding:9px;border:1px solid #28323c;border-radius:7px;background:#080d11}.wp-contract-facts dt{color:#778592;font-size:9px}.wp-contract-facts dd{min-width:0;margin:5px 0 0;overflow-wrap:anywhere;color:#b7c7d5;font-size:10px}.wp-contract-facts>small{grid-column:1/-1;color:#798692;font-size:9px}.wp-projection-editor,.wp-projection-field{display:grid;gap:10px}.wp-projection-editor>p{margin:0;color:#85939f;font-size:10px;line-height:1.6}.wp-readonly-line{display:flex;justify-content:space-between;gap:12px}.wp-projection-field{padding:10px;border:1px solid #242f39;border-radius:8px;background:#090e13}.wp-grid-three{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.wp-dynamic-group{display:grid;gap:8px;padding:10px;border:1px solid #26313b;border-radius:7px}.wp-json-editor{display:grid;gap:9px}.wp-json-warning{display:flex;gap:9px;align-items:flex-start;padding:10px;border:1px solid rgba(96,165,250,.26);border-radius:8px;background:rgba(59,130,246,.055);color:#9db5c7}.wp-json-warning svg{flex:0 0 auto}.wp-json-warning p{margin:0;font-size:10px;line-height:1.55}.wp-json-warning strong{display:block;margin-bottom:2px;color:#c6d7e5}.wp-json-editor textarea{resize:vertical;font:11px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}.wp-json-actions{display:flex;align-items:center;justify-content:flex-end;flex-wrap:wrap;gap:8px}.wp-json-actions>span{margin-right:auto;color:#7e8d99;font-size:10px}.wp-json-actions>span.error{color:#ed858d}.wp-json-actions>span.ok{color:#7bc6ad}.wp-unsupported,.wp-validation{display:flex;align-items:flex-start;gap:9px;padding:11px;border:1px solid rgba(239,115,123,.36);border-radius:8px;background:rgba(239,115,123,.06);color:#e18a91;font-size:10px;line-height:1.55}.wp-validation>svg,.wp-unsupported>svg{flex:0 0 auto}.wp-validation>div{display:grid;gap:5px}.wp-validation strong{color:#ef9aa1}.wp-validation ul{margin:0;padding-left:18px}.wp-validation small{color:#a87479}.wp-validation:empty{display:none}
@media(max-width:980px){.wp-definition-summary{grid-template-columns:1fr}.wp-definition-workbench{grid-template-columns:1fr}.wp-definition-workbench>nav{display:flex;overflow-x:auto;border-right:0;border-bottom:1px solid #2a333d}.wp-definition-workbench>nav button{flex:0 0 165px;border-left:0;border-bottom:3px solid transparent}.wp-definition-workbench>nav button.active{border-bottom-color:#e4a853}.wp-repeat-row.compact,.wp-repeat-row.range,.wp-repeat-row.fingerprint,.wp-repeat-row.six,.wp-repeat-row.role,.wp-repeat-row.edge,.wp-repeat-row.parameter,.wp-repeat-row.asset,.wp-repeat-row.target{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:680px){.wp-definition-summary dl,.wp-contract-facts,.wp-grid-three,.wp-repeat-row.compact,.wp-repeat-row.range,.wp-repeat-row.fingerprint,.wp-repeat-row.six,.wp-repeat-row.role,.wp-repeat-row.edge,.wp-repeat-row.parameter,.wp-repeat-row.asset,.wp-repeat-row.target{grid-template-columns:1fr}.wp-section-panel>header{align-items:flex-start;flex-direction:column}.wp-field-card>header{flex-direction:column}.wp-field-card>header code{max-width:100%}.wp-json-actions{align-items:stretch;flex-direction:column}.wp-json-actions>span{margin-right:0}.wp-json-actions .button{width:100%}}
</style>
