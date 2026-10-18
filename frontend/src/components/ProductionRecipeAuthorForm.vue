<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import ProductionRecipeEditor from './ProductionRecipeEditor.vue'
import CatalogChoiceField from './CatalogChoiceField.vue'
import { getPath, resourceEditorFields, setPath, validateRegistryResource, type RegistryEditorDependencies } from '@/services/registryResourceSchema'
import type { JsonObject } from '@/services/platformTypes'
import type { AuthoringIssue } from '@/services/configurationAuthoring'

const props = defineProps<{ modelValue: JsonObject; dependencies: RegistryEditorDependencies; disabled?: boolean; status: string; issues: AuthoringIssue[] }>()
const emit = defineEmits<{ 'update:modelValue': [JsonObject]; validity: [{ valid: boolean; errors: string[]; warnings: string[] }]; submit: []; 'pending-input': [] }>()
const identity = useId(), definitionErrors = ref<string[]>([])
const fields = computed(() => resourceEditorFields('recipe', props.modelValue, props.dependencies))
const validation = computed(() => {
  const base = validateRegistryResource('recipe', props.modelValue, props.dependencies)
  const errors = [...new Set([...base.errors, ...definitionErrors.value])]
  return { valid: errors.length === 0, errors, warnings: base.warnings }
})
watch(validation, value => emit('validity', value), { immediate: true, deep: true })
function update(path: string, value: unknown) {
  if (props.disabled) return
  const next = JSON.parse(JSON.stringify(props.modelValue)) as JsonObject
  emit('update:modelValue', setPath(next, path, value))
}
function text(path: string) { const value = getPath(props.modelValue, path); return value == null ? '' : String(value) }
</script>

<template>
  <form class="recipe-author-form" @input="!disabled && $emit('pending-input')" @change="!disabled && $emit('pending-input')" @submit.prevent="!disabled && $emit('submit')">
    <ProductionRecipeEditor :model-value="modelValue.definition as JsonObject" :options="dependencies.recipeEditorOptions"
      :disabled="disabled" :author-status="status" :author-issues="issues"
      @update:model-value="update('definition',$event)" @validity="definitionErrors=$event">
      <template #identity>
        <slot name="source" />
        <div class="recipe-identity-fields">
          <div v-for="field in fields" :key="field.path" :data-authoring-field="field.path">
            <label :for="`${identity}-${field.path}`">{{ field.label }}<small v-if="field.required && field.kind!=='readonly'"> · 必填</small></label>
            <output v-if="field.kind==='readonly'" :id="`${identity}-${field.path}`">{{ text(field.path) || field.placeholder || '尚未提供' }}</output>
            <CatalogChoiceField v-else-if="field.kind==='catalog'" :input-id="`${identity}-${field.path}`" :input-label="field.label" :model-value="text(field.path)"
              :options="field.options || []" :allow-custom="field.allowCustom" :disabled="disabled" :pattern="field.pattern" :custom-placeholder="field.placeholder"
              @update:model-value="update(field.path,$event)" />
            <input v-else :id="`${identity}-${field.path}`" :aria-label="field.label" :value="text(field.path)" :disabled="disabled" :required="field.required"
              @input="update(field.path,($event.target as HTMLInputElement).value)" />
            <small>{{ field.description }}</small>
          </div>
        </div>
      </template>
      <template #feedback><slot name="feedback" /></template>
      <template #actions>
        <p>保存草稿不会发布配方、改绑现有运行或启动生成。版本号与结构合同由原服务端维护。</p>
        <details v-if="validation.errors.length"><summary>尚有 {{ validation.errors.length }} 项需补充</summary><ul><li v-for="error in validation.errors" :key="error">{{ error }}</li></ul></details>
        <slot name="actions" />
      </template>
    </ProductionRecipeEditor>
  </form>
</template>

<style scoped>
.recipe-author-form{min-width:0;--config-sticky-offset:calc(-1 * var(--config-space-xl))}.recipe-identity-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--config-space-lg)}.recipe-identity-fields>div{display:grid;gap:var(--config-space-sm);min-width:0}.recipe-identity-fields label{font-weight:600}.recipe-identity-fields small{font-size:var(--config-help-size);color:var(--text-secondary)}.recipe-identity-fields output{font-size:var(--config-body-size);overflow-wrap:anywhere}.recipe-author-form details{flex-basis:100%;font-size:var(--config-body-size)}@media(max-width:640px){.recipe-identity-fields{grid-template-columns:1fr}}
</style>
