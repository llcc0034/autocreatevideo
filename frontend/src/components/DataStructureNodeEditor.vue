<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { StructureNode, StructureAuthoringOptions } from '@/services/dataStructures'
import type { FieldDefinitionVersion } from '@/services/fieldDefinitionVersions'
import type { JsonObject } from '@/services/platformTypes'
import FieldDefinitionVersionSelect from './FieldDefinitionVersionSelect.vue'
import StructuredJsonEditor from './StructuredJsonEditor.vue'

const props = defineProps<{ node: StructureNode; nodes: StructureNode[]; options: StructureAuthoringOptions; disabled?: boolean }>()
const emit = defineEmits<{ change: [StructureNode]; draftValidity: [string[]] }>()
const definition = ref<FieldDefinitionVersion | null>(null)
const definitionAvailable = ref(false)
const constraintErrors = ref<string[]>([]), defaultErrors = ref<string[]>([]), positionErrors = ref<string[]>([])
const positionDraft = ref(String(props.node.position))
watch(() => props.node.position, () => { positionDraft.value = String(props.node.position) })
const partial = computed(() => Boolean(constraintErrors.value.length || defaultErrors.value.length || positionErrors.value.length))
// Directory refresh emits null while loading. Keep the matching editor mounted
// so an unfinished numeric/JSON draft is never replaced with the saved value.
function receiveDefinition(row: FieldDefinitionVersion | null) {
  definitionAvailable.value = Boolean(row && row.id === props.node.field_definition_version)
  if (definitionAvailable.value) definition.value = row
}
watch(() => props.node.field_definition_version, () => { definition.value = null; definitionAvailable.value = false })
const constraintSchema = computed(() => {
  const schema = JSON.parse(JSON.stringify(props.options.constraint_schemas[definition.value?.value_type || ''] || {})) as JsonObject
  const properties = schema.properties as Record<string, JsonObject> | undefined
  if (properties?.enum?.['x-field-enum']) properties.enum.items = { type: 'string', enum: definition.value?.value_options || [] }
  return schema
})
const valueSchema = computed(() => {
  const schema = {...props.options.value_schemas[definition.value?.value_type || '']}
  if (definition.value?.value_type === 'enum') schema.enum = definition.value.value_options
  return props.node.allow_null ? {anyOf: [schema, {type: 'null'}]} : schema
})
const parents = computed(() => {
  const descendants = new Set([props.node.node_key])
  let changed = true
  while (changed) {
    changed = false
    for (const row of props.nodes) if (row.parent_key !== null && descendants.has(row.parent_key) && !descendants.has(row.node_key)) { descendants.add(row.node_key); changed = true }
  }
  return props.nodes.filter(row => !descendants.has(row.node_key))
})
function change(patch: Partial<StructureNode>) { if (!props.disabled) emit('change', {...props.node, ...patch}) }
function validity(kind: 'constraints' | 'default' | 'position', errors: string[]) {
  if (kind === 'constraints') constraintErrors.value = errors
  else if (kind === 'default') defaultErrors.value = errors
  else positionErrors.value = errors
  emit('draftValidity', [...constraintErrors.value, ...defaultErrors.value, ...positionErrors.value])
}
function updatePosition(raw: string) {
  if (props.disabled) return
  positionDraft.value = raw
  const value = Number(raw)
  if (!raw.trim() || !Number.isSafeInteger(value) || value < 0) { validity('position', ['同层排序须为非负整数，空白不会自动改为0']); return }
  validity('position', []); change({position: value})
}
function toggleDefault(enabled: boolean) {
  change({has_default: enabled, default_value: enabled ? props.node.default_value : null})
  if (!enabled) validity('default', [])
}
</script>

<template>
  <fieldset class="node-editor" :disabled="disabled">
    <legend>节点配置 · {{ node.node_key }}</legend>
    <div class="node-grid">
      <label :data-authoring-field="`nodes.${node.node_key}.node_key`">节点标识<input :value="node.node_key" @input="change({node_key: ($event.target as HTMLInputElement).value})" /></label>
      <label :data-authoring-field="`nodes.${node.node_key}.parent_key`">父节点<select :value="node.parent_key ?? ''" @change="change({parent_key: ($event.target as HTMLSelectElement).value || null})"><option value="">根节点</option><option v-for="row in parents" :key="row.node_key" :value="row.node_key">{{ row.node_key }}{{ row.property_name ? ` · ${row.property_name}` : '' }}</option></select></label>
      <label :data-authoring-field="`nodes.${node.node_key}.property_name`">对象属性名（根／列表元素留空）<input :value="node.property_name" @input="change({property_name: ($event.target as HTMLInputElement).value})" /></label>
      <label :data-authoring-field="`nodes.${node.node_key}.position`">同层排序<input type="number" min="0" step="1" :value="positionDraft" @input="updatePosition(($event.target as HTMLInputElement).value)" /></label>
    </div>
    <div :data-authoring-field="`nodes.${node.node_key}.field_definition_version`"><FieldDefinitionVersionSelect global :disabled="disabled || partial" :model-value="node.field_definition_version || ''" @update:model-value="change({field_definition_version: $event || null})" @selected="receiveDefinition" /></div>
    <div class="switches">
      <label><input type="checkbox" :checked="node.required" @change="change({required: ($event.target as HTMLInputElement).checked})" />缺键时必填</label>
      <label><input type="checkbox" :checked="node.allow_null" @change="change({allow_null: ($event.target as HTMLInputElement).checked})" />允许 null</label>
      <label><input type="checkbox" :checked="node.allow_blank" @change="change({allow_blank: ($event.target as HTMLInputElement).checked})" />允许空字符串</label>
      <label><input type="checkbox" :checked="node.allow_empty" @change="change({allow_empty: ($event.target as HTMLInputElement).checked})" />允许空集合</label>
      <label><input type="checkbox" :checked="node.has_default" @change="toggleDefault(($event.target as HTMLInputElement).checked)" />缺键时采用明确默认值</label>
    </div>
    <p>根节点和列表元素不设置属性名、必填或缺键默认；对象／列表可以包含子节点，列表只设置一个元素节点。0、false、null和空值不会被当作缺键。</p>
    <template v-if="definition">
      <p v-if="!definitionAvailable" role="status">字段定义正在重读或未能读取；原约束与未完成输入保留，暂不编辑，不使用其他版本代替。</p>
      <StructuredJsonEditor :model-value="node.constraints" :schemas="[constraintSchema]" label="使用约束（可选；只列当前类型支持项）" :disabled="disabled || !definitionAvailable" @update:model-value="change({constraints: $event as JsonObject})" @draft-validity="validity('constraints', $event)" />
      <StructuredJsonEditor v-if="node.has_default" :model-value="node.default_value" :schemas="[valueSchema]" label="缺键默认值（明确配置，不是试填样例）" :disabled="disabled || !definitionAvailable" @update:model-value="change({default_value: $event})" @draft-validity="validity('default', $event)" />
    </template>
    <p v-else>选择并读取字段版本后可配置其约束与默认值；已填写的内容保留，不自动按新类型转换。</p>
  </fieldset>
</template>

<style scoped>
.node-editor{display:grid;gap:var(--config-space-md);min-width:0;border:1px solid var(--border);padding:var(--config-space-md);font:inherit}.node-editor legend{font-weight:600;font-size:var(--config-section-size)}.node-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--config-space-md)}.node-editor label{display:grid;gap:var(--config-space-xs);min-width:0}.node-editor :deep(input:not([type=checkbox])),.node-editor :deep(select){box-sizing:border-box;min-width:0;width:100%;min-height:44px;font:inherit;background:var(--surface);color:inherit;border:1px solid var(--border-strong);border-radius:var(--config-radius);padding:var(--config-space-sm)}.node-editor p{margin:0;overflow-wrap:anywhere}.switches{display:flex;gap:var(--config-space-md);flex-wrap:wrap}.switches label{display:flex;align-items:center;min-height:44px}@media(max-width:640px){.node-grid{grid-template-columns:1fr}}
</style>
