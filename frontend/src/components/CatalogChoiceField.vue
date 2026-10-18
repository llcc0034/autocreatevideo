<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { FormCatalogOption } from '@/services/platformTypes'

const props = withDefaults(defineProps<{
  modelValue: string
  options: FormCatalogOption[]
  allowCustom?: boolean
  disabled?: boolean
  emptyLabel?: string
  customLabel?: string
  customPlaceholder?: string
  pattern?: string
  inputId?: string
  inputLabel?: string
}>(), {
  allowCustom: false,
  disabled: false,
  emptyLabel: '请选择，不自动猜测',
  customLabel: '登记新值（高级）',
  customPlaceholder: '填写新的稳定标识',
  pattern: undefined,
})
const emit = defineEmits<{ (event: 'update:modelValue', value: string): void }>()
const CUSTOM = '__catalog_custom__'
const optionValues = computed(() => new Set(props.options.map((item) => item.value)))
const customActive = ref(Boolean(
  props.allowCustom && props.modelValue && !optionValues.value.has(props.modelValue),
))
const selectValue = computed(() => {
  if (customActive.value) return CUSTOM
  return optionValues.value.has(props.modelValue) ? props.modelValue : ''
})

watch(
  () => [props.modelValue, props.options] as const,
  () => {
    if (props.modelValue && optionValues.value.has(props.modelValue)) customActive.value = false
    else if (props.allowCustom && props.modelValue) customActive.value = true
  },
  { deep: true },
)

const select = (event: Event) => {
  const value = (event.target as HTMLSelectElement).value
  if (value === CUSTOM) {
    customActive.value = true
    if (optionValues.value.has(props.modelValue)) emit('update:modelValue', '')
    return
  }
  customActive.value = false
  emit('update:modelValue', value)
}
</script>

<template>
  <div class="catalog-choice-field">
    <select :id="inputId" :aria-label="inputLabel" :value="selectValue" :disabled="disabled" @change="select">
      <option value="">{{ emptyLabel }}</option>
      <option v-for="option in options" :key="option.value" :value="option.value" :disabled="option.disabled" :title="option.hint">{{ option.label }}</option>
      <option v-if="allowCustom" :value="CUSTOM">{{ customLabel }}</option>
    </select>
    <input
      v-if="customActive"
      :value="modelValue"
      type="text"
      :disabled="disabled"
      :pattern="pattern"
      :placeholder="customPlaceholder"
      :aria-label="customLabel"
      @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
    />
  </div>
</template>

<style scoped>
.catalog-choice-field{display:grid;gap:6px}.catalog-choice-field select,.catalog-choice-field input{min-width:0;width:100%}
</style>
