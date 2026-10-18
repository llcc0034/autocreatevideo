<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { numericInputDrafts } from '@/services/numericInputDrafts'
import type { JsonObject, ModelProfileVersion, WorkflowProjectorOptions, WorkflowProjectorRecommendation } from '@/services/platformTypes'
import { cloneWorkflowProjectorDefinition, isJsonObject } from '@/services/workflowProjectorDefinition'
import WorkflowAssetContractSource from './WorkflowAssetContractSource.vue'
import type { ProjectorWorkflowContext } from '@/services/workflowProjectorContext'

const props = defineProps<{ modelValue: JsonObject; options: WorkflowProjectorOptions; recommendation?: WorkflowProjectorRecommendation | null; disabled?: boolean; readOnly?: boolean; workflowContext?: ProjectorWorkflowContext; modelProfiles?: ModelProfileVersion[]; modelProfilesError?: string; initialModelProfileId?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: JsonObject]; validity: [errors: string[]] }>()
const disabled = computed(() => props.disabled || props.readOnly)
const rows = (value: unknown): JsonObject[] => Array.isArray(value) ? value.filter(isJsonObject) : []
const names = (value: unknown): string[] => Array.isArray(value) ? value.map(String) : []
const projection = computed(() => props.modelValue.projection as JsonObject)
const groups = computed(() => rows((projection.value.config as JsonObject)?.groups))
const roles = computed(() => isJsonObject(props.modelValue.node_roles) ? props.modelValue.node_roles : {})
const uniqueRoles = computed(() => Object.keys(roles.value).filter((key) => (roles.value[key] as JsonObject).cardinality === 'one'))
const slots = computed(() => rows(props.modelValue.asset_slots))
const slot = (row: JsonObject) => slots.value.find((value) => value.key === row.key)
let nextId = 0
const groupIds = ref<number[]>([])
watch(() => groups.value.length, length => { groupIds.value = Array.from({length}, (_, index) => groupIds.value[index] ?? nextId++) }, { immediate: true, flush: 'sync' })
const numericLabels: Record<string,string> = { output_index: '模板输出索引', reducer_output_index: '合并输出索引', minimum_items: '最少素材数', maximum_items: '最多素材数' }
const numberKey = (index: number, field: string) => JSON.stringify([groupIds.value[index], field])
const numberLabel = (index: number, field: string) => `动态素材组 ${index + 1} · ${numericLabels[field]}`
const numbers = numericInputDrafts(() => groups.value.flatMap((row,index) => [
  'output_index', ...(row.wiring === 'reduce' ? ['reducer_output_index'] : []),
  ...(slot(row)?.contract_source === 'workflow' ? ['minimum_items','maximum_items'] : []),
].map(field => ({key: numberKey(index,field), label: numberLabel(index,field)}))), () => disabled.value)
watch(numbers.errors, errors => emit('validity',errors), { immediate: true })
const numeric = (index: number, field: string, event: Event) => {
  if (numbers.capture(numberKey(index,field),event)) change(index,field,text(event) === '' ? null : Number(text(event)))
}
const inputOptions = (role: unknown) => {
  const node = roles.value[String(role)] as JsonObject | undefined
  return props.recommendation?.catalog.inputsByNodeType[String(node?.node_type || '')] || []
}
const text = (event: Event) => (event.target as HTMLInputElement).value
const selectQuantity = (key: unknown, value: { conditioning_mode: string; model_input: string }) => {
  if (props.disabled || props.readOnly || (props.workflowContext && props.workflowContext.state !== 'none')) return
  const definition = cloneWorkflowProjectorDefinition(props.modelValue)
  const matches = rows(((definition.projection as JsonObject).config as JsonObject).groups).filter(row => row.key === key)
  if (matches.length !== 1 || slots.value.find(row => row.key === key)?.contract_source !== 'model_profile') return
  Object.assign(matches[0]!, value, { minimum_items: null, maximum_items: null })
  emit('update:modelValue', definition)
}
const change = (index: number, field: string, value: unknown) => {
  if (disabled.value) return
  const definition = cloneWorkflowProjectorDefinition(props.modelValue)
  const next = rows(((definition.projection as JsonObject).config as JsonObject).groups)
  next[index] = { ...next[index], [field]: value }
  if (field === 'wiring') {
    numbers.discard(numberKey(index,'reducer_output_index'))
    next[index]!.reducer_role = ''
    next[index]!.reducer_inputs = value === 'reduce' ? ['', ''] : []
    next[index]!.input_names = []
  }
  ;((definition.projection as JsonObject).config as JsonObject).groups = next
  emit('update:modelValue', definition)
}
const changeSlot = (index: number, field: string, value: string) => {
  if (disabled.value) return
  const definition = cloneWorkflowProjectorDefinition(props.modelValue)
  const nextSlots = rows(definition.asset_slots)
  const nextGroups = rows(((definition.projection as JsonObject).config as JsonObject).groups)
  const row = nextSlots.find((value) => value.key === nextGroups[index]?.key)
  if (!row) return
  if (field === 'contract_source') for (const field of ['minimum_items','maximum_items']) numbers.discard(numberKey(index,field))
  row[field] = value
  if (field === 'key') nextGroups[index]!.key = value
  if (field === 'contract_source') Object.assign(nextGroups[index]!, value === 'workflow'
    ? { minimum_items: 1, maximum_items: 1, conditioning_mode: '', model_input: '' }
    : { minimum_items: null, maximum_items: null, conditioning_mode: '', model_input: '' })
  emit('update:modelValue', definition)
}
const toggleRole = (index: number, field: string, role: string, event: Event) => {
  const current = names(groups.value[index]?.[field])
  change(index, field, (event.target as HTMLInputElement).checked ? [...current, role] : current.filter((value) => value !== role))
}
const add = () => {
  if (disabled.value) return
  const definition = cloneWorkflowProjectorDefinition(props.modelValue)
  let i = 1
  while (slots.value.some((row) => row.key === `references_${i}`)) i++
  const key = `references_${i}`
  definition.asset_slots = [...slots.value, { key, media_kind: 'image', dynamic: true, contract_source: 'workflow' }]
  ;((definition.projection as JsonObject).config as JsonObject).groups = [...groups.value, {
    key, source_role: '', branch_roles: [], output_role: '', output_index: 0,
    target_roles: [], input_names: [], wiring: props.options.definitionEditor.choices.assetGroupWirings?.[0]?.value || '',
    reducer_role: '', reducer_inputs: [], reducer_output_index: 0,
    minimum_items: 1, maximum_items: 1, conditioning_mode: '', model_input: '',
  }]
  emit('update:modelValue', definition)
}
const remove = (index: number) => {
  if (disabled.value) return
  groupIds.value.splice(index,1)
  const definition = cloneWorkflowProjectorDefinition(props.modelValue)
  definition.asset_slots = slots.value.filter((row) => row.key !== groups.value[index]?.key)
  ;((definition.projection as JsonObject).config as JsonObject).groups = groups.value.filter((_, i) => i !== index)
  emit('update:modelValue', definition)
}
</script>

<template>
  <div class="asset-group-config projector-reference-editor">
    <p>先配置每组素材的类型与数量来源；具体文件由每次调用提供。节点角色和端口在“实现详情”复核，保存规则不代表工作流已验收。</p>
    <p v-for="error in numbers.errors.value" :key="error" role="alert">{{ error }}</p>
    <article v-for="(row,index) in groups" :key="groupIds[index]">
      <header><strong>动态素材组 {{ index + 1 }}</strong><button type="button" :disabled="disabled" @click="remove(index)">删除此组及槽声明</button></header>
      <div class="fields">
        <label>组 Key<input :value="row.key" :disabled="disabled" @change="changeSlot(index,'key',text($event))"/></label>
        <label>媒体类型<select :value="slot(row)?.media_kind" :disabled="disabled" @change="changeSlot(index,'media_kind',text($event))"><option v-for="choice in options.definitionEditor.choices.mediaKinds" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label>
        <label>数量合同来源<select :value="slot(row)?.contract_source" :disabled="disabled" @change="changeSlot(index,'contract_source',text($event))"><option v-for="choice in options.definitionEditor.choices.contractSources" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label>
        <template v-if="slot(row)?.contract_source==='workflow'"><label>最少素材数<input type="number" min="0" :value="numbers.value(numberKey(index,'minimum_items'),row.minimum_items)" :aria-label="numberLabel(index,'minimum_items')" :aria-invalid="numbers.invalid(numberKey(index,'minimum_items'))" :disabled="disabled" @input="numeric(index,'minimum_items',$event)"/><button v-if="numbers.invalid(numberKey(index,'minimum_items'))" type="button" :disabled="disabled" :aria-label="`撤销未完成输入：${numberLabel(index,'minimum_items')}`" @click="numbers.discard(numberKey(index,'minimum_items'))">撤销未完成输入</button></label><label>最多素材数<input type="number" min="1" :value="numbers.value(numberKey(index,'maximum_items'),row.maximum_items)" :aria-label="numberLabel(index,'maximum_items')" :aria-invalid="numbers.invalid(numberKey(index,'maximum_items'))" :disabled="disabled" @input="numeric(index,'maximum_items',$event)"/><button v-if="numbers.invalid(numberKey(index,'maximum_items'))" type="button" :disabled="disabled" :aria-label="`撤销未完成输入：${numberLabel(index,'maximum_items')}`" @click="numbers.discard(numberKey(index,'maximum_items'))">撤销未完成输入</button></label></template>
        <template v-else><label>当前模型模式<output>{{ row.conditioning_mode || '尚未选择' }}</output></label><label>当前数量输入 Key<output>{{ row.model_input || '尚未选择' }}</output></label></template>
      </div>
      <WorkflowAssetContractSource v-if="slot(row)" :slot="slot(row)!" :group="row" :definition="modelValue" :recommendation="recommendation" :model-profiles="modelProfiles" :model-profiles-error="modelProfilesError" :initial-model-profile-id="initialModelProfileId" :read-only="readOnly" :disabled="disabled" :workflow-context="workflowContext" :quantity-catalog="options.definitionEditor.choices.modelQuantityInputs" allow-quantity-selection @select-quantity="selectQuantity(row.key,$event)"/>
      <small>顺序接线：最多数量须等于端口数，超出最少数量的端口必须可省略。逐项合并：来源合并节点的两个输入须连接同一模板输出；上限至少为 2。不能仅提高数量绕过节点能力。</small>
      <details class="reference-implementation">
        <summary>实现详情 · 动态素材组 {{ index + 1 }} 的节点与接线</summary>
        <p>素材加载 → 逐项预处理 → 目标输入；模型、CLIP、VAE 等共享节点不放进逐项模板。此处复核来源图的实现规则，不选择本次素材。</p>
        <div class="fields">
        <label>接线方式<select :value="row.wiring" :disabled="disabled" @change="change(index,'wiring',text($event))"><option v-for="choice in options.definitionEditor.choices.assetGroupWirings || []" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label>
        <label>素材加载角色<select :value="row.source_role" :disabled="disabled" @change="change(index,'source_role',text($event))"><option value="">请选择</option><option v-for="role in uniqueRoles" :key="role">{{ role }}</option></select></label>
        <label>模板输出角色<select :value="row.output_role" :disabled="disabled" @change="change(index,'output_role',text($event))"><option value="">请选择</option><option v-for="role in names(row.branch_roles)" :key="role">{{ role }}</option></select></label>
        <label>模板输出索引<input type="number" min="0" :value="numbers.value(numberKey(index,'output_index'),row.output_index)" :aria-label="numberLabel(index,'output_index')" :aria-invalid="numbers.invalid(numberKey(index,'output_index'))" :disabled="disabled" @input="numeric(index,'output_index',$event)"/><button v-if="numbers.invalid(numberKey(index,'output_index'))" type="button" :disabled="disabled" :aria-label="`撤销未完成输入：${numberLabel(index,'output_index')}`" @click="numbers.discard(numberKey(index,'output_index'))">撤销未完成输入</button></label>
      </div>
      <fieldset :disabled="disabled"><legend>逐项模板节点（加载与预处理）</legend><label v-for="role in uniqueRoles" :key="role" class="check"><input type="checkbox" :checked="names(row.branch_roles).includes(role)" @change="toggleRole(index,'branch_roles',role,$event)"/>{{ role }}</label></fieldset>
      <fieldset :disabled="disabled"><legend>接收素材的目标角色（多个同类节点可一起接入）</legend><label v-for="role in Object.keys(roles)" :key="role" class="check"><input type="checkbox" :checked="names(row.target_roles).includes(role)" @change="toggleRole(index,'target_roles',role,$event)"/>{{ role }}</label></fieldset>
      <div class="fields">
        <label>目标输入名（按顺序，每行一个）<textarea :value="names(row.input_names).join('\n')" :disabled="disabled" rows="3" @change="change(index,'input_names',text($event).split(/\r?\n/).map(v=>v.trim()).filter(Boolean))"/><small>扫描到的候选：{{ inputOptions(names(row.target_roles)[0]).join('、') || '尚未读取，请使用候选推荐加载实时节点目录' }}</small></label>
        <template v-if="row.wiring==='reduce'">
          <label>合并节点角色<select :value="row.reducer_role" :disabled="disabled" @change="change(index,'reducer_role',text($event))"><option value="">请选择</option><option v-for="role in uniqueRoles" :key="role">{{ role }}</option></select></label>
          <label v-for="(_,portIndex) in [0,1]" :key="portIndex">合并输入 {{ portIndex+1 }}<select :value="names(row.reducer_inputs)[portIndex]" :disabled="disabled" @change="change(index,'reducer_inputs',[0,1].map(i=>i===portIndex?text($event):(names(row.reducer_inputs)[i]||'')))"><option value="">请选择</option><option v-for="input in inputOptions(row.reducer_role)" :key="input" :value="input">{{ input }}</option></select><input v-if="!inputOptions(row.reducer_role).length" :value="names(row.reducer_inputs)[portIndex]" :disabled="disabled" aria-label="合并输入名" @change="change(index,'reducer_inputs',[0,1].map(i=>i===portIndex?text($event):(names(row.reducer_inputs)[i]||'')))"/></label>
          <label>合并输出索引<input type="number" min="0" :value="numbers.value(numberKey(index,'reducer_output_index'),row.reducer_output_index)" :aria-label="numberLabel(index,'reducer_output_index')" :aria-invalid="numbers.invalid(numberKey(index,'reducer_output_index'))" :disabled="disabled" @input="numeric(index,'reducer_output_index',$event)"/><button v-if="numbers.invalid(numberKey(index,'reducer_output_index'))" type="button" :disabled="disabled" :aria-label="`撤销未完成输入：${numberLabel(index,'reducer_output_index')}`" @click="numbers.discard(numberKey(index,'reducer_output_index'))">撤销未完成输入</button></label>
        </template>
      </div>
      </details>
    </article>
    <button type="button" :disabled="disabled||groups.length>=options.definitionEditor.limits.assetSlotsMaxItems" @click="add">添加动态素材组</button>
  </div>
</template>

<style src="./projectorReferenceEditor.css"></style>
