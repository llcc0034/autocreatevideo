<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { executionRegistryApi } from '@/services/executionRegistryApi'
import { executionRequestFormContract, executionRuleDescription, type ExecutionRequestFormContract } from '@/services/executionRequestForm'
import { recipeObject as obj, recipeRows as rows, recipeContextFieldOptions, recipeSourceKindOptions, recipeInputSourceIssue } from '@/services/productionRecipeAuthoring'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import RecipeReferenceMappingEditor from './RecipeReferenceMappingEditor.vue'
import RecipeMediaSelectionEditor from './RecipeMediaSelectionEditor.vue'
import type { MediaSourceSelection } from '@/services/inputSourcePolicies'
import RecipeStructureMappingEditor from './RecipeStructureMappingEditor.vue'
import ExecutionParameterEvidence from './ExecutionParameterEvidence.vue'
import { mappingErrors, mappingHasMaterialDifference } from '@/services/recipeStructureMapping'
import { shotFieldNodes, useShotFieldCatalog } from '@/services/shotFieldMapping'
import type { JsonObject, ProductionRecipeEditorOptions } from '@/services/platformTypes'
import type { ExecutionInput } from '@/services/executionRegistry'
import type { MediaAppearanceInputOptions } from '@/services/mediaAppearanceInputs'

const props = defineProps<{ modelValue: JsonObject; options: ProductionRecipeEditorOptions; priorStages: JsonObject[]; stageKey?: string; appearanceRole?: string; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [JsonObject]; validity: [string[]] }>()
const contract = ref<ExecutionRequestFormContract | null>(null), error = ref(''), loading = ref(false)
const appearanceOptions = ref<MediaAppearanceInputOptions>()
const materialOptions = computed(()=>props.stageKey==='bible_image_generation'&&props.appearanceRole==='variant'?appearanceOptions.value:undefined)
const structureNodes = computed(()=>props.options.structureNodeOptions.filter(row=>
  (row.value!=='material_difference'||Boolean(materialOptions.value)&&priorKeys.value.includes('bible_review'))
  &&(row.value!=='shot_field'||props.stageKey==='media_generation')))
const jsonErrors = ref<Record<string, string>>({})
const mappingDraftErrors = ref<Record<string, string[]>>({})
let sequence = 0
async function load() {
  const current = ++sequence; loading.value = true; error.value = ''; contract.value = null; appearanceOptions.value=undefined
  try {
    const raw = await executionRegistryApi.contract(String(props.modelValue.execution_instance_version_id))
    const value = executionRequestFormContract(raw)
    if (current !== sequence) return
    if (value.sha256 !== props.modelValue.expected_definition_sha256) throw new Error('实例合同摘要不一致，请重新选择精确版本')
    contract.value = value
    appearanceOptions.value = raw.appearance_inputs
  } catch (e) { if (current === sequence) error.value = apiErrorSummary(e) }
  finally { if (current === sequence) loading.value = false }
}
watch([() => props.modelValue.execution_instance_version_id, () => props.modelValue.expected_definition_sha256], () => { jsonErrors.value = {}; void load() }, { immediate: true })
const sources = computed(() => obj(props.modelValue.input_sources))
const shotCatalog = useShotFieldCatalog(()=>Object.values(sources.value).filter(row=>obj(row).kind==='structured').map(row=>obj(obj(row).template)))
function source(key: string) { return obj(sources.value[key]) }
const priorKeys = computed(() => props.priorStages.map(row => String(row.key)))
function sourceChoices(input: ExecutionInput) { return contract.value ? recipeSourceKindOptions(contract.value, input, props.options, priorKeys.value, materialOptions.value) : [] }
function sourceIssue(input: ExecutionInput) {
  if (!contract.value) return ''
  const issue=recipeInputSourceIssue(contract.value,input,props.options,priorKeys.value,String(source(input.key).kind||''),materialOptions.value)
  if(issue)return issue
  const selected=source(input.key)
  if(selected.kind==='structured'&&shotFieldNodes(obj(selected.template)).length
    &&(props.stageKey!=='media_generation'||!priorKeys.value.includes('storyboard_review')||!input.semantic_contract.allowed_scope_kinds.includes('shot')))return '独立字段需要媒体步骤、前置分镜审核及实例开放镜头范围；原映射未修改。'
  if(selected.kind==='structured'&&mappingHasMaterialDifference(obj(selected.template))&&!materialOptions.value) return '当前设定差异仅用于变体执行实例；保存的结构映射未修改，请重新配置。'
  if(selected.kind==='material_primary' && (typeof selected.item_key!=='string'||!selected.item_key.trim()
    || (input.semantic_contract.allowed_purposes.length ? !input.semantic_contract.allowed_purposes.includes(String(selected.purpose)) : selected.purpose!==null))) return '请配置本轮主图的使用标识和合同允许用途。'
  if(selected.kind==='selected_output' && !textOutputChoices(input).some(row=>row.outputKey===selected.output_key&&row.stageKey===selected.stage_key&&row.scopeKind===obj(selected.scope).kind)) return '已保存的步骤输出、类型或范围不在当前目录；原映射未修改，请重新选择。'
  return ''
}
function textOutputChoices(input: ExecutionInput) {return props.options.textOutputOptions.filter(row=>row.kind===input.kind&&priorKeys.value.includes(row.stageKey)&&input.semantic_contract.allowed_scope_kinds.includes(row.scopeKind))}
function sourceSupported(input: ExecutionInput) { return sourceChoices(input).some(row => row.value === source(input.key).kind) }
const sourceErrors = computed(() => (contract.value?.inputs || []).filter(input => source(input.key).kind).map(input => {
  const issue = sourceIssue(input); return issue ? `${input.key}：${issue}` : ''
}).filter(Boolean))
// Parent rerenders recreate priorStages; equal messages must not emit another
// validity array and start a parent/child reactive update loop.
watch(() => JSON.stringify([error.value, loading.value ? '正在读取实例合同' : '', ...Object.values(jsonErrors.value), ...sourceErrors.value,
  ...Object.entries(mappingDraftErrors.value).filter(([key])=>source(key).kind==='structured').flatMap(([,issues])=>issues)].filter(Boolean)),
  value => emit('validity', JSON.parse(value) as string[]), { immediate: true })
function updateSource(key: string, value?: JsonObject) {
  const next = { ...sources.value }; if (value === undefined) delete next[key]; else next[key] = value
  if (value?.kind !== 'media_selection') delete jsonErrors.value[key]
  emit('update:modelValue', { ...props.modelValue, input_sources: next })
}
function setKind(key: string, kind: string) {
  if (!kind) return updateSource(key)
  if (kind === 'project_snapshot') return updateSource(key, {kind})
  if (kind === 'material_difference') return updateSource(key, {kind})
  if (kind === 'material_primary') return updateSource(key, {kind,item_key:key,purpose:null})
  if (kind === 'reviewed_references') return updateSource(key,{kind,stage_key:'',scope:{kind:''},groups:[]})
  if (kind === 'media_selection') return updateSource(key,{kind,selection:{source:contract.value?.inputs.find(row=>row.key===key)?.media_source_policy?.default_source||'',items:[]}})
  if (kind === 'structured') return updateSource(key,{kind,template:{}})
  updateSource(key, kind === 'literal' ? { kind, value: '' } : kind === 'context_field' ? { kind, selector_key: '', scope: { kind: '' } }
    : { kind, stage_key: '', output_key: '', scope: { kind: '' }, selection_policy: 'reviewed_selected' })
}
function setContext(key: string, selector: string) {
  const choice = contract.value?.contextFields.find(row => row.selector_key === selector)
  updateSource(key, { kind: 'context_field', selector_key: selector, scope: { kind: choice?.scope_kind || '' } })
}
function setTextOutput(key: string, outputKey: string) {
  const input=contract.value?.inputs.find(row=>row.key===key)
  const choice = input && textOutputChoices(input).find(row=>row.outputKey===outputKey&&row.stageKey===source(key).stage_key)
  if (!choice) return
  updateSource(key, { kind: 'selected_output', stage_key: choice.stageKey, output_key: choice.outputKey,
    scope: { kind: choice.scopeKind }, selection_policy: 'reviewed_selected' })
}
function setLiteral(key: string, kind: string, text: string) {
  try { updateSource(key, { kind: 'literal', value: kind === 'json' ? JSON.parse(text) : text }) }
  catch { jsonErrors.value[key] = `${key} 的固定 JSON 内容无效，未写入草稿` }
}
const policies = computed(() => rows(props.modelValue.parameter_policies))
const selectedParameter = ref('')
watch([() => props.modelValue.execution_instance_version_id, () => props.modelValue.expected_definition_sha256], () => { selectedParameter.value='' })
const canOverrideParameter = (rule: JsonObject) => !Object.prototype.hasOwnProperty.call(rule,'const') && rule.seed_policy!=='generate_once'
const addableParameters = computed(() => (contract.value?.parameters || []).filter(row=>canOverrideParameter(row.rule)&&!policy(row.key)))
const configuredParameters = computed(() => (contract.value?.parameters || []).filter(row=>policy(row.key)||row.key===selectedParameter.value))
const inheritedParameters = computed(() => (contract.value?.parameters || []).filter(row=>!policy(row.key)&&row.key!==selectedParameter.value))
const unknownPolicies = computed(() => policies.value.filter(row=>!contract.value?.parameters.some(parameter=>parameter.key===row.key)))
function mediaValidity(key:string,issues:string[]){if(issues.length)jsonErrors.value[key]=issues.join('；');else delete jsonErrors.value[key]}
function mediaSelection(key:string){return source(key).selection as unknown as MediaSourceSelection}
const policy = (key: string) => policies.value.find(row => row.key === key)
function setPolicy(key: string, value: JsonObject | null) {
  if (readOnly.value) return
  const parameter=contract.value?.parameters.find(row=>row.key===key)
  if (value && (!parameter||!canOverrideParameter(parameter.rule))) return
  emit('update:modelValue', { ...props.modelValue, parameter_policies: [...policies.value.filter(row => row.key !== key), ...(value ? [value] : [])] })
  if (selectedParameter.value===key) selectedParameter.value=''
}
function setPolicyKind(key: string, kind: string, rule: JsonObject) {
  if (!kind) return setPolicy(key, null)
  setPolicy(key, kind === 'locked_source'
    ? { key, policy: kind, source: { kind: 'context_scalar', selector_key: '', scope: { kind: 'project' } } }
    : { key, policy: kind, value: policy(key)?.value ?? rule.const ?? rule.default ?? null })
}
function setParameterSource(key: string, selector: string) {
  if (!selector) return setPolicyKind(key, 'locked_source', {})
  const option = props.options.parameterSourceOptions.find(row => row.selectorKey === selector)
  if (!option || !parameterSources(option.type, key).includes(option)) return
  setPolicy(key, { key, policy: 'locked_source', source: { kind: option.sourceKind, selector_key: selector, scope: { kind: option.scopeKind } } })
}
function parameterSources(type: string, key?: string) {
  return props.options.parameterSourceOptions.filter(row => (row.type === type || type === 'number' && row.type === 'integer')
    && (!row.parameterKey || row.parameterKey === key && Boolean(contract.value?.framePolicy))
    && (!row.stageKey || row.stageKey === props.stageKey))
}
const readOnly = computed(() => props.disabled || loading.value || Boolean(error.value))
function structureSchema(key:string){return contract.value?.structureSchemas?.[key]?.[0] || {}}
function structureDefaults(key:string){return contract.value?.inputs.find(row=>row.key===key)?.data_structure_version_id?[structureSchema(key)]:[]}
watch(()=>[sources.value,contract.value,props.options.textOutputOptions,priorKeys.value,shotCatalog.states] as const,()=>{
  for(const [key,row] of Object.entries(sources.value))if(obj(row).kind==='structured'){
    const schemas=contract.value?.structureSchemas?.[key]||[]
    const input=contract.value?.inputs.find(row=>row.key===key)
    const outputs=props.options.textOutputOptions.filter(row=>priorKeys.value.includes(row.stageKey)&&input?.semantic_contract.allowed_scope_kinds.includes(row.scopeKind))
    const errors=(schemas.length?schemas:[{}]).flatMap(schema=>mappingErrors(obj(obj(row).template),schema,key,structureDefaults(key),{fields:contract.value?.contextFields||[],outputs,shotFields:shotCatalog.states}))
    if(errors.length)jsonErrors.value[key]=[...new Set(errors)].join('；');else delete jsonErrors.value[key]
  }
},{deep:true})
</script>

<template>
  <section class="recipe-route-config">
    <p v-if="loading" role="status">正在读取精确实例合同…</p>
    <p v-if="error" role="alert">{{ error }} <button type="button" class="button ghost tiny" @click="load">重新读取</button></p>
    <template v-if="contract">
      <p>{{ contract.operation }} · {{ contract.mode || '无模式' }} · 提示词来源：{{ contract.promptMode }}<br><small>精确版本 {{ contract.versionId }} · {{ contract.sha256 }}</small></p>
      <p v-if="contract.promptMode==='manual'" class="warning">此实例要求人工完整提示词。配方不会替您补写；自动生产应选择策略组装或明确无需提示词的实例。</p>
      <h5>输入来源</h5>
      <p v-if="!contract.inputs.length">此实例没有用户输入槽位。</p>
      <article v-for="input in contract.inputs" :key="input.key">
        <label>{{ input.key }} · {{ input.kind }} · {{ input.required ? '必需' : '可选' }} · 数量 {{ input.min_items }}–{{ input.max_items }}
          <select :value="source(input.key).kind || ''" :disabled="readOnly" @change="setKind(input.key,($event.target as HTMLSelectElement).value)">
            <option value="">尚未配置（可保留草稿）</option>
            <option v-if="source(input.key).kind && !sourceSupported(input)" :value="source(input.key).kind" disabled>已保存但不可用：{{ source(input.key).kind }}</option>
            <option v-for="option in sourceChoices(input)" :key="option.value" :value="option.value">{{ option.label }}</option>
          </select>
        </label>
        <p v-if="sourceIssue(input)" role="alert">{{ sourceIssue(input) }}</p>
        <p v-if="source(input.key).kind==='project_snapshot'">来源由该精确输入合同声明。创建本轮制作时预览实际项目内容并确认；本轮只用确认快照，不读取最新项目替换它。</p>
        <template v-if="sourceSupported(input)">
        <template v-if="source(input.key).kind==='material_primary'">
          <p>使用此变体依赖的本轮已选主图版本。只有选择和登记就绪后才能准备，不改用素材库其他版本。</p>
          <label>主图使用标识<input :disabled="readOnly" :value="source(input.key).item_key" @input="updateSource(input.key,{...source(input.key),item_key:($event.target as HTMLInputElement).value})" /></label>
          <label v-if="input.semantic_contract.allowed_purposes.length">主图用途<select :disabled="readOnly" :value="source(input.key).purpose??''" @change="updateSource(input.key,{...source(input.key),purpose:($event.target as HTMLSelectElement).value})"><option value="">请选择用途</option><option v-for="option in (contract.purposes[input.kind]||[]).filter(row=>input.semantic_contract.allowed_purposes.includes(row.value))" :key="option.value" :value="option.value">{{option.label}}</option></select></label>
        </template>
        <p v-if="source(input.key).kind==='material_difference'">使用当前已审核变体设定的差异文字，首次准备后冻结；不另填、不读取其他变体或最新正文。</p>
        <RecipeReferenceMappingEditor v-if="source(input.key).kind==='reviewed_references'" :model-value="source(input.key)" :input="input" :options="options" :prior-stages="priorStages" :purpose-options="contract.purposes[input.key]" :disabled="readOnly" @update:model-value="updateSource(input.key,$event)"/>
        <RecipeMediaSelectionEditor v-if="source(input.key).kind==='media_selection'" :model-value="mediaSelection(input.key)" :input="input" :disabled="readOnly"
          @update:model-value="updateSource(input.key,{kind:'media_selection',selection:$event as unknown as JsonObject})" @validity="mediaValidity(input.key,$event)"/>
        <template v-if="source(input.key).kind==='structured'">
          <p>为每项选择来源或明确固定值。有结构默认值的字段可省略，准备时按所引用的确切结构补齐；已有空值不会被替换，模板原文不变。</p>
          <RecipeStructureMappingEditor :model-value="obj(source(input.key).template)" :schema="structureSchema(input.key)" :default-schemas="structureDefaults(input.key)" :label="input.key" :nodes="structureNodes" :fields="contract.contextFields" :outputs="options.textOutputOptions" :scopes="input.semantic_contract.allowed_scope_kinds" :prior-stages="priorStages.map(row=>String(row.key))" :disabled="readOnly" @draft-validity="mappingDraftErrors[input.key]=$event" @update:model-value="updateSource(input.key,{kind:'structured',template:$event})"/>
        </template>
        <label v-if="source(input.key).kind==='literal'">固定内容
          <textarea :disabled="readOnly" :value="input.kind==='json' ? JSON.stringify(source(input.key).value,null,2) : String(source(input.key).value??'')" @input="setLiteral(input.key,input.kind,($event.target as HTMLTextAreaElement).value)"/>
        </label>
        <label v-if="source(input.key).kind==='context_field'">受控内容来源（取值时点见说明）
          <select :disabled="readOnly" :value="source(input.key).selector_key" @change="setContext(input.key,($event.target as HTMLSelectElement).value)"><option value="">请选择字段</option><option v-for="field in recipeContextFieldOptions(contract,input,priorStages.map(row=>String(row.key)))" :key="field.selector_key" :value="field.selector_key">{{ field.label }}</option></select>
          <small>{{ contract.contextFields.find(f=>f.selector_key===source(input.key).selector_key)?.description || '按当前制作对象读取本轮已确认内容；未审核或已变更时拒绝，不读取最新候选。' }}</small>
        </label>
        <div v-if="source(input.key).kind==='selected_output'" class="grid">
          <label>上游步骤<select :disabled="readOnly" :value="source(input.key).stage_key" @change="updateSource(input.key,{kind:'selected_output',stage_key:($event.target as HTMLSelectElement).value,output_key:'',scope:{kind:''},selection_policy:'reviewed_selected'})"><option value="">请选择更早的步骤</option><option v-if="source(input.key).stage_key&&!priorKeys.includes(String(source(input.key).stage_key))" :value="source(input.key).stage_key" disabled>已保存步骤不可用</option><option v-for="(stage,index) in priorStages" :key="String(stage.key)" :value="String(stage.key)">{{ index+1 }} · {{ stage.name || stage.key }}</option></select></label>
          <label v-if="['text','json'].includes(input.kind)">已采用内容
            <select :disabled="readOnly" :value="source(input.key).output_key" @change="setTextOutput(input.key,($event.target as HTMLSelectElement).value)">
              <option value="">请选择目录输出</option>
              <option v-if="source(input.key).output_key&&!textOutputChoices(input).some(row=>row.stageKey===source(input.key).stage_key&&row.outputKey===source(input.key).output_key)" :value="source(input.key).output_key" disabled>已保存输出不可用</option>
              <option v-for="option in textOutputChoices(input).filter(row=>row.stageKey===source(input.key).stage_key)" :key="option.outputKey" :value="option.outputKey">{{ option.label }}</option>
            </select>
            <small>由服务器目录确定内容类型和范围；未列出的输出尚未接入。</small>
          </label>
          <p v-else role="alert">此媒体来源没有逐项语义映射，请重新配置为已审核参考清单；不会沿用旧输出标识。</p>
          <label>作用范围<select :disabled="readOnly" :value="obj(source(input.key).scope).kind" @change="updateSource(input.key,{...source(input.key),scope:{kind:($event.target as HTMLSelectElement).value}})"><option value="">请选择范围</option><option v-for="scope in options.scopeOptions" :key="scope.value" :value="scope.value">{{ scope.label }}</option></select></label>
          <small>只使用按配置明确选择的输出，不选择最新候选；实际类型和来源在准备时再次核对。</small>
        </div>
        </template>
        <p v-if="jsonErrors[input.key]" role="alert">{{ jsonErrors[input.key] }}</p>
      </article>
      <h5>本步骤参数（默认继承实例）</h5>
      <p>无需重复填写模型、工作流或实例已有的参数。这里只保存本步骤的默认值、固定值或取值来源；不改变字段定义或目标能力。</p>
      <label v-if="!readOnly&&addableParameters.length">仅选择需要单独配置的参数
        <select v-model="selectedParameter" :disabled="readOnly"><option value="">全部沿用实例，或选择一项</option><option v-for="parameter in addableParameters" :key="parameter.key" :value="parameter.key">{{ parameter.key }}</option></select>
      </label>
      <p v-if="!policies.length">未配置步骤覆盖；沿用精确实例合同。合同未提供默认值的必填参数仍需明确取值。</p>
      <article v-for="saved in unknownPolicies" :key="String(saved.key)">
        <p role="alert">已保存参数 {{ saved.key }} 不在当前实例合同中；原配置保留，不会静默丢弃或猜测对应字段。</p>
        <button v-if="!readOnly" type="button" class="button ghost small" @click="setPolicy(String(saved.key),null)">移除此项步骤覆盖</button>
      </article>
      <article v-for="parameter in configuredParameters" :key="parameter.key">
        <template v-if="!canOverrideParameter(parameter.rule)">
          <strong>{{ parameter.key }} · 由实例合同固定</strong>
          <p role="alert">此参数不允许步骤覆盖。已保存配置未改动，请明确移除覆盖后沿用实例。</p>
        </template>
        <template v-else>
        <label>{{ parameter.key }} <small>{{ executionRuleDescription(parameter.rule) }}</small>
          <select :disabled="readOnly||parameter.rule.seed_policy==='generate_once'" :value="policy(parameter.key)?.policy || ''" @change="setPolicyKind(parameter.key,($event.target as HTMLSelectElement).value,parameter.rule)"><option value="">遵循实例合同</option><option v-for="option in options.parameterPolicyOptions.filter(row=>row.value!=='locked_source'||parameterSources(parameter.rule.type,parameter.key).length)" :key="option.value" :value="option.value">{{ option.label }}</option></select>
        </label>
        <label v-if="policy(parameter.key)?.policy==='locked_source'">数值来源
          <select :disabled="readOnly" :value="obj(policy(parameter.key)?.source).selector_key || ''" @change="setParameterSource(parameter.key,($event.target as HTMLSelectElement).value)">
            <option value="">请选择数值来源</option>
            <option v-for="source in parameterSources(parameter.rule.type,parameter.key)" :key="source.selectorKey" :value="source.selectorKey">{{ source.label }}（{{ source.unit }}）</option>
          </select>
          <small>{{ options.parameterSourceOptions.find(row=>row.selectorKey===obj(policy(parameter.key)?.source).selector_key)?.description || '选择明确来源后，首次执行准备会固定实际值。' }}</small>
          <small v-if="obj(policy(parameter.key)?.source).selector_key && !parameterSources(parameter.rule.type,parameter.key).some(row=>row.selectorKey===obj(policy(parameter.key)?.source).selector_key)" role="alert">保存的来源不适用于当前实例配置，请重新选择；不会改用默认值。</small>
        </label>
        <label v-else-if="policy(parameter.key)">参数值
          <select v-if="parameter.rule.enum || parameter.rule.type==='boolean'" :disabled="readOnly" :value="JSON.stringify(policy(parameter.key)?.value)" @change="setPolicy(parameter.key,{...policy(parameter.key),value:JSON.parse(($event.target as HTMLSelectElement).value)})"><option value="null">请选择</option><option v-for="value in parameter.rule.enum || [true,false]" :key="String(value)" :value="JSON.stringify(value)">{{ value }}</option></select>
          <input v-else :type="['integer','number'].includes(parameter.rule.type)?'number':'text'" :step="parameter.rule.type==='integer'?1:'any'" :min="parameter.rule.minimum" :max="parameter.rule.maximum" :disabled="readOnly" :value="policy(parameter.key)?.value??''" @input="setPolicy(parameter.key,{...policy(parameter.key),value:($event.target as HTMLInputElement).value===''?null:parameter.rule.type==='string'?($event.target as HTMLInputElement).value:Number(($event.target as HTMLInputElement).value)})"/>
        </label>
        </template>
        <button v-if="!readOnly&&policy(parameter.key)" type="button" class="button ghost small" @click="setPolicy(parameter.key,null)">恢复继承实例（移除本步骤覆盖）</button>
      </article>
      <details v-if="inheritedParameters.length">
        <summary>沿用实例的参数 · {{ inheritedParameters.length }} 项（只读）</summary>
        <article v-for="parameter in inheritedParameters" :key="parameter.key">
          <strong>{{ parameter.key }}</strong>
          <ExecutionParameterEvidence v-if="parameter.evidence" :entry="parameter.evidence"/>
          <p v-else>{{ executionRuleDescription(parameter.rule) }}</p>
        </article>
      </details>
    </template>
  </section>
</template>

<style scoped>
.recipe-route-config article{min-width:0;grid-template-columns:minmax(0,1fr)}.recipe-route-config label{overflow-wrap:anywhere}
.recipe-route-config{display:grid;gap:10px;min-width:0}.recipe-route-config article{border-top:1px solid var(--border,#2b323c);padding:10px 0;display:grid;gap:8px}.recipe-route-config p,.recipe-route-config small{overflow-wrap:anywhere;color:var(--text-secondary,#94a3b8);margin:0}.recipe-route-config .warning,[role=alert]{color:#e7b669}.recipe-route-config h5{margin:5px 0}.recipe-route-config label{display:grid;gap:6px;min-width:0}.recipe-route-config input,.recipe-route-config select,.recipe-route-config textarea{width:100%;min-width:0;min-height:36px;padding:8px;border:1px solid #303743;border-radius:6px;background:#090c10;color:#e4e9ee}.recipe-route-config textarea{min-height:74px}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.grid small{grid-column:1/-1}@media(max-width:760px){.grid{grid-template-columns:1fr}}
</style>
