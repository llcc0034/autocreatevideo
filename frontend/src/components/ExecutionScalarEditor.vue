<script setup lang="ts">
import type { ExecutionScalar } from '@/services/executionRegistry'
import { useId } from 'vue'
const inputId = useId()
const props = defineProps<{ modelValue: ExecutionScalar; valueType: string; label: string; disabled?: boolean; optional?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: ExecutionScalar] }>()
function numberValue(event: Event) {
  const input = event.target as HTMLInputElement
  input.setCustomValidity('')
  if (input.value === '') { emit('update:modelValue', null); return }
  input.setCustomValidity(props.valueType === 'integer' && !Number.isSafeInteger(input.valueAsNumber) ? '此整数超出页面能准确表示的范围' : '')
  if (input.checkValidity() && Number.isFinite(input.valueAsNumber)) emit('update:modelValue', input.valueAsNumber)
}
</script>

<template>
  <div class="execution-scalar field">
    <label :for="inputId">{{ label }}</label>
    <select v-if="valueType==='boolean'" :id="inputId" :value="modelValue === null ? '' : String(modelValue)" :disabled="disabled" :required="!optional" @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value === '' ? null : ($event.target as HTMLSelectElement).value === 'true')">
      <option value="">{{ optional ? '不覆盖来源值' : '请选择' }}</option><option value="true">是</option><option value="false">否</option>
    </select>
    <input v-else-if="valueType==='integer'||valueType==='number'" :id="inputId" type="number" :step="valueType==='integer'?1:'any'" :value="modelValue" :disabled="disabled" :required="!optional" @input="numberValue" />
    <template v-else>
      <label v-if="optional" class="execution-scalar-flag"><input type="checkbox" :checked="modelValue!==null" :disabled="disabled" @change="emit('update:modelValue', ($event.target as HTMLInputElement).checked ? '' : null)" />设置文字值（允许空文字）</label>
      <input :id="inputId" type="text" :value="modelValue" :disabled="disabled||(optional&&modelValue===null)" @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)" />
    </template>
  </div>
</template>

<style scoped>
.execution-scalar{display:grid;gap:6px;min-width:0}.execution-scalar-flag{display:flex;align-items:center;gap:6px;font-size:12px}.execution-scalar-flag input{width:auto}
</style>
