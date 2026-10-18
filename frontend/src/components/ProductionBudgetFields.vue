<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, unwrap } from '@/services/api'
import { apiErrorSummary } from '@/services/apiErrorPresentation'

defineProps<{ external: boolean; disabled?: boolean }>()
const emit = defineEmits<{ 'update:external': [boolean] }>()
const policy = ref<{ budget_by_currency: Record<string, string>; call_count: number; output_length: number } | null>(null)
const loading = ref(false), error = ref('')
async function load() {
  loading.value = true; error.value = ''
  try {
    const raw = unwrap<{ resource_policy: { values: typeof policy.value } }>((await api.get('/production-runs/budget-options/')).data)
    const value = raw?.resource_policy?.values
    if (!value || !Number.isSafeInteger(value.call_count) || value.call_count < 1 || !Number.isSafeInteger(value.output_length) || value.output_length < 1
      || !value.budget_by_currency || typeof value.budget_by_currency !== 'object' || Array.isArray(value.budget_by_currency)
      || Object.entries(value.budget_by_currency).some(([key, amount]) => !/^[A-Z][A-Z0-9]{2,11}$/.test(key) || typeof amount !== 'string')) throw new Error('系统资源策略返回不完整')
    policy.value = value
  } catch (cause) { error.value = apiErrorSummary(cause); policy.value = null }
  finally { loading.value = false }
}
onMounted(load)
</script>
<template>
  <section class="resource-policy" aria-label="系统资源提醒与调用授权">
    <div class="policy-header"><strong>系统资源提醒</strong><button type="button" class="button small" :disabled="loading" @click="load">{{ loading ? '正在读取…' : '刷新策略' }}</button></div>
    <p v-if="error" role="alert">{{ error }}，请重新读取；不会使用旧配方额度代替。</p>
    <template v-else-if="policy">
      <p v-if="Object.keys(policy.budget_by_currency).length">每轮累计费用提醒：<span v-for="(amount, code) in policy.budget_by_currency" :key="code">{{ amount }} {{ code }}　</span></p>
      <p v-else>尚未配置币种预算提醒；付费请求会显示具体费用，等待人工确认。</p>
      <p>每轮调用次数提醒：{{ policy.call_count }} 次 · 新建文本请求输出额度：{{ policy.output_length }} token</p>
    </template>
    <p class="help">统一在“系统设置 → 默认值与限制 → execution.resources.review_policy”调整。超额保留任务，确认后另行继续；不同币种不换算，已提交请求不改写。</p>
    <label class="consent"><input type="checkbox" :checked="external" :disabled="disabled" @change="emit('update:external', ($event.target as HTMLInputElement).checked)"/>允许本轮调用本机以外的模型服务</label>
    <p class="help">外部服务包括局域网，不等于收费服务。授权不会自动批准超额或重试。</p>
  </section>
</template>
<style scoped>
.resource-policy{display:grid;gap:10px;min-width:0}.resource-policy p{margin:0;overflow-wrap:anywhere}.policy-header{display:flex;align-items:center;justify-content:space-between;gap:12px}.consent{display:flex;align-items:center;gap:10px}.consent input{width:18px;height:18px;flex-shrink:0}.help{font-size:12px;color:var(--text-muted);line-height:1.6}
</style>
