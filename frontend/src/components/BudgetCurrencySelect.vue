<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { loadBudgetCurrencies, type BudgetCurrencyOption } from '@/services/productionBudget'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
defineProps<{ modelValue: string; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [string] }>()
const rows = ref<BudgetCurrencyOption[]>([]), loading = ref(false), error = ref('')
let serial = 0
async function load() {
  const request = ++serial
  loading.value = true; error.value = ''
  try { const result = await loadBudgetCurrencies(); if(request === serial) rows.value = result }
  catch(cause) { if(request === serial) { rows.value = []; error.value = apiErrorSummary(cause) } }
  finally { if(request === serial) loading.value = false }
}
onMounted(load)
onBeforeUnmount(() => { serial++ })
</script>
<template>
  <div>
    <label class="platform-field"><span>本次预估费用币种（不是预算配置）</span>
      <select :value="modelValue" :disabled="disabled || loading || !!error" @change="emit('update:modelValue',($event.target as HTMLSelectElement).value)">
        <option value="">{{ loading ? '正在读取币种…' : '请选择币种' }}</option>
        <option v-if="modelValue && !rows.some(row=>row.value===modelValue)" :value="modelValue" disabled>{{ modelValue }} · 已记录币种</option>
        <option v-for="row in rows" :key="row.value" :value="row.value">{{ row.label }}</option>
      </select>
    </label>
    <p v-if="error" role="alert">{{ error }} <button type="button" class="button ghost small" :disabled="loading" @click="load">重新读取币种</button></p>
  </div>
</template>
