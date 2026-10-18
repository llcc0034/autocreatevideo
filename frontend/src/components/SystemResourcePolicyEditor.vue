<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { loadBudgetCurrencies, type BudgetCurrencyOption } from '@/services/productionBudget'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
const props = defineProps<{ modelValue: string; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [string] }>()
const currencies = ref<BudgetCurrencyOption[]>([]), error = ref(''), loading = ref(false), selectedCurrency = ref('')
const value = computed(() => {
  try {
    const raw = JSON.parse(props.modelValue)
    return raw && typeof raw === 'object' && !Array.isArray(raw) && raw.budget_by_currency && typeof raw.budget_by_currency === 'object' && !Array.isArray(raw.budget_by_currency)
      ? raw as {budget_by_currency: Record<string,string>; call_count: number|null; output_length: number|null} : null
  } catch { return null }
})
async function load() {
  loading.value = true; error.value = ''
  try { currencies.value = await loadBudgetCurrencies() }
  catch (cause) { currencies.value = []; error.value = apiErrorSummary(cause) }
  finally { loading.value = false }
}
const update = (patch: object) => { if(value.value) emit('update:modelValue', JSON.stringify({...value.value,...patch})) }
function amount(code:string, text:string) { update({budget_by_currency:{...value.value!.budget_by_currency,[code]:text}}) }
function remove(code:string) { const rows={...value.value!.budget_by_currency};delete rows[code];update({budget_by_currency:rows}) }
function add() { if(!selectedCurrency.value || !currencies.value.some(row=>row.value===selectedCurrency.value) || selectedCurrency.value in value.value!.budget_by_currency)return;amount(selectedCurrency.value,'');selectedCurrency.value='' }
onMounted(load)
</script>
<template>
  <fieldset class="system-resources" :disabled="disabled">
    <legend>预算、调用次数与输出额度</legend>
    <p>超过提醒值后保留任务，等待人工确认，不直接判失败。保存草稿不会生效，请按系统配置原有流程发布并启用。</p>
    <template v-if="value">
      <div class="quota-row"><label>每轮调用次数提醒（次）<input type="number" min="1" step="1" :value="value.call_count" @input="update({call_count:($event.target as HTMLInputElement).value===''?null:Number(($event.target as HTMLInputElement).value)})"/></label>
        <label>新建文本请求输出额度（token）<input type="number" min="1" step="1" :value="value.output_length" @input="update({output_length:($event.target as HTMLInputElement).value===''?null:Number(($event.target as HTMLInputElement).value)})"/></label></div>
      <p>调用次数包含主调用、辅助调用及明确发起的重试。新额度不改写已保存请求；旧请求超过当前提醒值时需确认。</p>
      <div v-for="(limit,code) in value.budget_by_currency" :key="code" class="budget-row">
        <label>{{ currencies.find(row=>row.value===code)?.label || code }} · 每轮累计费用提醒<input type="text" inputmode="decimal" :value="limit" @input="amount(String(code),($event.target as HTMLInputElement).value)"/></label>
        <button type="button" class="button ghost small" @click="remove(String(code))">移除此币种提醒</button>
      </div>
      <p v-if="!Object.keys(value.budget_by_currency).length">尚未设置币种。非零费用请求会展示用量并请求人工确认，不代表无限预算。</p>
      <div class="budget-row"><select v-model="selectedCurrency" :disabled="loading || !!error" aria-label="添加预算提醒币种"><option value="">选择币种</option><option v-for="row in currencies" :key="row.value" :value="row.value" :disabled="row.value in value.budget_by_currency">{{ row.label }}</option></select><button type="button" class="button small" :disabled="!selectedCurrency || loading || !!error" @click="add">添加币种</button><button type="button" class="button ghost small" :disabled="loading" @click="load">刷新选项</button></div>
      <p v-if="error" role="alert">{{ error }}</p>
      <p>金额最多 4 位小数；0 也是明确提醒值。不同币种分别配置、不自动换算。外部调用授权不包含在此策略中。</p>
    </template>
    <template v-else><p role="alert">当前配置结构不完整，请修正原始内容，不能用默认值覆盖。</p><textarea :value="modelValue" rows="6" @input="emit('update:modelValue',($event.target as HTMLTextAreaElement).value)"/></template>
  </fieldset>
</template>
<style>
/* This composite editor occupies a complete configuration row. */
.config-fields > label:has(.system-resources){grid-column:1/-1}
.config-fields .system-resources label{padding:0;border:0}
</style>
<style scoped>
.system-resources{display:grid;gap:12px;margin:0;border:1px solid var(--border-color,#30343b);border-radius:10px;min-width:0;padding:16px}.system-resources p{margin:0;color:var(--text-muted);font-size:12px;line-height:1.6}.system-resources label{display:grid;gap:8px;flex:1;min-width:0}.quota-row,.budget-row{display:flex;align-items:end;gap:12px;flex-wrap:wrap}.system-resources input,.system-resources select,.system-resources textarea{background:var(--bg-body,#0b0e12);color:inherit;border:1px solid var(--border-color,#30343b);border-radius:8px;padding:10px;min-width:0}.budget-row select{flex:1}.system-resources legend{font-weight:600}@media(max-width:700px){.quota-row{display:grid}.budget-row label{flex-basis:100%}}
</style>
