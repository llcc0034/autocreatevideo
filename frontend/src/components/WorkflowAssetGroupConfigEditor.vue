<script setup lang="ts">
import { computed } from 'vue'
import type { JsonObject, ModelProfileVersion, WorkflowProjectorOptions, WorkflowProjectorRecommendation } from '@/services/platformTypes'
import { cloneWorkflowProjectorDefinition, isJsonObject } from '@/services/workflowProjectorDefinition'
import WorkflowAssetContractSource from './WorkflowAssetContractSource.vue'
import type { ProjectorWorkflowContext } from '@/services/workflowProjectorContext'

const props = defineProps<{ modelValue: JsonObject; options: WorkflowProjectorOptions; recommendation?: WorkflowProjectorRecommendation | null; disabled?: boolean; readOnly?: boolean; workflowContext?: ProjectorWorkflowContext; modelProfiles?: ModelProfileVersion[]; modelProfilesError?: string; initialModelProfileId?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: JsonObject] }>()
const rows = (value: unknown): JsonObject[] => Array.isArray(value) ? value.filter(isJsonObject) : []
const names = (value: unknown): string[] => Array.isArray(value) ? value.map(String) : []
const projection = computed(() => props.modelValue.projection as JsonObject)
const groups = computed(() => rows((projection.value.config as JsonObject)?.groups))
const roles = computed(() => isJsonObject(props.modelValue.node_roles) ? props.modelValue.node_roles : {})
const uniqueRoles = computed(() => Object.keys(roles.value).filter((key) => (roles.value[key] as JsonObject).cardinality === 'one'))
const slots = computed(() => rows(props.modelValue.asset_slots))
const slot = (row: JsonObject) => slots.value.find((value) => value.key === row.key)
const inputOptions = (role: unknown) => {
  const node = roles.value[String(role)] as JsonObject | undefined
  return props.recommendation?.catalog.inputsByNodeType[String(node?.node_type || '')] || []
}
const text = (event: Event) => (event.target as HTMLInputElement).value
const change = (index: number, field: string, value: unknown) => {
  if (props.disabled) return
  const definition = cloneWorkflowProjectorDefinition(props.modelValue)
  const next = rows(((definition.projection as JsonObject).config as JsonObject).groups)
  next[index] = { ...next[index], [field]: value }
  if (field === 'wiring') {
    next[index]!.reducer_role = ''
    next[index]!.reducer_inputs = value === 'reduce' ? ['', ''] : []
    next[index]!.input_names = []
  }
  ;((definition.projection as JsonObject).config as JsonObject).groups = next
  emit('update:modelValue', definition)
}
const changeSlot = (index: number, field: string, value: string) => {
  if (props.disabled) return
  const definition = cloneWorkflowProjectorDefinition(props.modelValue)
  const nextSlots = rows(definition.asset_slots)
  const nextGroups = rows(((definition.projection as JsonObject).config as JsonObject).groups)
  const row = nextSlots.find((value) => value.key === nextGroups[index]?.key)
  if (!row) return
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
  if (props.disabled) return
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
  if (props.disabled) return
  const definition = cloneWorkflowProjectorDefinition(props.modelValue)
  definition.asset_slots = slots.value.filter((row) => row.key !== groups.value[index]?.key)
  ;((definition.projection as JsonObject).config as JsonObject).groups = groups.value.filter((_, i) => i !== index)
  emit('update:modelValue', definition)
}
</script>

<template>
  <div class="asset-group-config">
    <p>选择来源工作流中的一条素材预处理分支作为模板。模型、CLIP、VAE 等共享节点不应放进逐项模板。接线配置只生成草稿；需重新生成工作流合同并验收。</p>
    <article v-for="(row,index) in groups" :key="index">
      <header><strong>动态素材组 {{ index + 1 }}</strong><button type="button" :disabled="disabled" @click="remove(index)">删除此组及槽声明</button></header>
      <div class="fields">
        <label>组 Key<input :value="row.key" :disabled="disabled" @change="changeSlot(index,'key',text($event))"/></label>
        <label>媒体类型<select :value="slot(row)?.media_kind" :disabled="disabled" @change="changeSlot(index,'media_kind',text($event))"><option v-for="choice in options.definitionEditor.choices.mediaKinds" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label>
        <label>接线方式<select :value="row.wiring" :disabled="disabled" @change="change(index,'wiring',text($event))"><option v-for="choice in options.definitionEditor.choices.assetGroupWirings || []" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label>
        <label>素材加载角色<select :value="row.source_role" :disabled="disabled" @change="change(index,'source_role',text($event))"><option value="">请选择</option><option v-for="role in uniqueRoles" :key="role">{{ role }}</option></select></label>
        <label>模板输出角色<select :value="row.output_role" :disabled="disabled" @change="change(index,'output_role',text($event))"><option value="">请选择</option><option v-for="role in names(row.branch_roles)" :key="role">{{ role }}</option></select></label>
        <label>模板输出索引<input type="number" min="0" :value="row.output_index" :disabled="disabled" @input="change(index,'output_index',Number(text($event)))"/></label>
      </div>
      <fieldset :disabled="disabled"><legend>逐项模板节点（加载与预处理）</legend><label v-for="role in uniqueRoles" :key="role" class="check"><input type="checkbox" :checked="names(row.branch_roles).includes(role)" @change="toggleRole(index,'branch_roles',role,$event)"/>{{ role }}</label></fieldset>
      <fieldset :disabled="disabled"><legend>接收素材的目标角色（多个同类节点可一起接入）</legend><label v-for="role in Object.keys(roles)" :key="role" class="check"><input type="checkbox" :checked="names(row.target_roles).includes(role)" @change="toggleRole(index,'target_roles',role,$event)"/>{{ role }}</label></fieldset>
      <div class="fields">
        <label>目标输入名（按顺序，每行一个）<textarea :value="names(row.input_names).join('\n')" :disabled="disabled" rows="3" @change="change(index,'input_names',text($event).split(/\r?\n/).map(v=>v.trim()).filter(Boolean))"/><small>扫描到的候选：{{ inputOptions(names(row.target_roles)[0]).join('、') || '尚未读取，请使用候选推荐加载实时节点目录' }}</small></label>
        <template v-if="row.wiring==='reduce'">
          <label>合并节点角色<select :value="row.reducer_role" :disabled="disabled" @change="change(index,'reducer_role',text($event))"><option value="">请选择</option><option v-for="role in uniqueRoles" :key="role">{{ role }}</option></select></label>
          <label v-for="(_,portIndex) in [0,1]" :key="portIndex">合并输入 {{ portIndex+1 }}<select :value="names(row.reducer_inputs)[portIndex]" :disabled="disabled" @change="change(index,'reducer_inputs',[0,1].map(i=>i===portIndex?text($event):(names(row.reducer_inputs)[i]||'')))"><option value="">请选择</option><option v-for="input in inputOptions(row.reducer_role)" :key="input" :value="input">{{ input }}</option></select><input v-if="!inputOptions(row.reducer_role).length" :value="names(row.reducer_inputs)[portIndex]" :disabled="disabled" aria-label="合并输入名" @change="change(index,'reducer_inputs',[0,1].map(i=>i===portIndex?text($event):(names(row.reducer_inputs)[i]||'')))"/></label>
          <label>合并输出索引<input type="number" min="0" :value="row.reducer_output_index" :disabled="disabled" @input="change(index,'reducer_output_index',Number(text($event)))"/></label>
        </template>
        <label>数量合同来源<select :value="slot(row)?.contract_source" :disabled="disabled" @change="changeSlot(index,'contract_source',text($event))"><option v-for="choice in options.definitionEditor.choices.contractSources" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select></label>
        <template v-if="slot(row)?.contract_source==='workflow'"><label>最少素材数<input type="number" min="0" :value="row.minimum_items" :disabled="disabled" @input="change(index,'minimum_items',Number(text($event)))"/></label><label>最多素材数<input type="number" min="1" :value="row.maximum_items" :disabled="disabled" @input="change(index,'maximum_items',Number(text($event)))"/></label></template>
        <template v-else><label>模型合同 conditioning mode<input :value="row.conditioning_mode" :disabled="disabled" @change="change(index,'conditioning_mode',text($event))"/></label><label>模型模式输入 Key<input :value="row.model_input" :disabled="disabled" @change="change(index,'model_input',text($event))"/></label></template>
      </div>
      <WorkflowAssetContractSource v-if="slot(row)" :slot="slot(row)!" :group="row" :definition="modelValue" :recommendation="recommendation" :model-profiles="modelProfiles" :model-profiles-error="modelProfilesError" :initial-model-profile-id="initialModelProfileId" :read-only="readOnly" :workflow-context="workflowContext"/>
      <small>顺序接线：最多数量须等于端口数，超出最少数量的端口必须可省略。逐项合并：来源合并节点的两个输入须连接同一模板输出；上限至少为 2。不能仅提高数量绕过节点能力。</small>
    </article>
    <button type="button" :disabled="disabled||groups.length>=options.definitionEditor.limits.assetSlotsMaxItems" @click="add">添加动态素材组</button>
  </div>
</template>

<style scoped>
.asset-group-config,article{display:grid;gap:12px;min-width:0}article{border:1px solid #303b46;border-radius:8px;padding:12px}header{display:flex;justify-content:space-between;gap:8px}.fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}label{display:grid;gap:6px;font-size:11px;color:#b7c7d5}input,select,textarea,button{min-width:0;max-width:100%;box-sizing:border-box;background:#0b1015;color:#d9e2ea;border:1px solid #354350;border-radius:6px;padding:8px}button{cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}fieldset{display:flex;flex-wrap:wrap;gap:12px;border:1px solid #354350}legend,p,small{font-size:11px;color:#92a3b3;line-height:1.6}.check{display:flex;align-items:center}textarea{resize:vertical}input:focus,select:focus,textarea:focus,button:focus-visible{outline:2px solid #d8a051}@media(max-width:700px){.fields{grid-template-columns:1fr}}
</style>
