<script setup lang="ts">
import { computed, nextTick } from 'vue'
import DataStructureVersionSelect from './DataStructureVersionSelect.vue'
import { shotFieldOptions, shotFieldSchema, useShotFieldCatalog } from '@/services/shotFieldMapping'
import { pathObject as obj } from '@/services/structurePaths'
import type { JsonObject } from '@/services/platformTypes'

const props = defineProps<{modelValue: JsonObject; disabled?: boolean}>()
const emit = defineEmits<{'update:modelValue': [JsonObject]}>()
const catalog = useShotFieldCatalog(() => [props.modelValue])
const field = computed(() => obj(props.modelValue.field))
const structureId = computed(() => typeof field.value.structure_version_id === 'string' ? field.value.structure_version_id : '')
const state = computed(() => catalog.states[structureId.value])
const choices = computed(() => state.value?.schema ? shotFieldOptions(state.value.schema) : [])
const selected = computed(() => shotFieldSchema(props.modelValue, catalog.states))
function changeVersion(id: string) {
  if (props.disabled || id === structureId.value) return
  if (field.value.node_key && !window.confirm('更换结构版本将清除此项字段及子路径选择，不影响其他映射。是否继续？')) return
  emit('update:modelValue', {kind: 'shot_field', scope: {kind: 'shot'}, field: {structure_version_id: id, node_key: '', field_definition_version_id: ''}})
}
function changeField(key: string) {
  if (props.disabled || state.value?.loading || state.value?.error) return
  const rows = choices.value.filter(row => row.nodeKey === key)
  if (rows.length !== 1) return
  if (field.value.node_key === key && field.value.field_definition_version_id === rows[0]!.definitionId) return
  emit('update:modelValue', {kind: 'shot_field', scope: {kind: 'shot'}, field: {structure_version_id: structureId.value,
    node_key: key, field_definition_version_id: rows[0]!.definitionId}})
}
async function chooseField(event: Event) {
  const target = event.target as HTMLSelectElement
  changeField(target.value)
  await nextTick()
  target.value = selected.value.error ? '__unavailable__' : String(field.value.node_key)
}
</script>
<template>
  <div class="shot-field-select">
    <DataStructureVersionSelect :model-value="structureId" :disabled="disabled" @update:model-value="changeVersion"/>
    <p v-if="state?.loading" role="status">正在读取独立字段目录…</p>
    <p v-if="state?.error" role="alert">{{ state.error }} <button type="button" @click="catalog.load(structureId)">重读字段目录</button></p>
    <label v-if="structureId">独立分镜字段
      <select :value="selected.error ? '__unavailable__' : field.node_key" :disabled="disabled || !state?.schema || state.loading" @change="chooseField">
        <option value="__unavailable__">{{ field.node_key ? '已保存字段不可用，请核对确切版本' : '请选择字段，不自动取第一项' }}</option>
        <option v-for="row in choices" :key="row.nodeKey" :value="row.nodeKey">{{ row.label }}</option>
      </select>
    </label>
    <p v-if="state?.schema && !choices.length" role="alert">此结构没有可选择的字段定义；不能按名称或默认字段代替。</p>
    <p v-if="selected.error && !state?.loading && !state?.error" role="alert">{{ selected.error }}</p>
    <small>运行时读取当前镜头已审核的原字段或确切人工修订；这里只配置来源，组合顺序与提示词格式仍由策略定义。</small>
    <details v-if="field.node_key"><summary>查看字段身份</summary><pre>{{ field }}</pre></details>
  </div>
</template>
<style scoped>
.shot-field-select{display:grid;gap:8px;min-width:0}.shot-field-select label{display:grid;gap:5px;min-width:0}.shot-field-select select{width:100%;min-width:0;min-height:36px;background:var(--bg-input,#090c10);color:inherit;border:1px solid var(--border,#303743);padding:6px}.shot-field-select p{margin:0;overflow-wrap:anywhere}.shot-field-select pre{white-space:pre-wrap;overflow-wrap:anywhere}.shot-field-select button{min-height:36px}
</style>
