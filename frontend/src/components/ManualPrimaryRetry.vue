<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { ExecutionPreparationDetail } from '@/services/executionPreparation'
import type { JsonObject, ProductionRun } from '@/services/platformTypes'
import type { ManualActionOptions } from '@/services/productionManual'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { isDefinitiveCreationRejection } from '@/services/productionOnboarding'
import { buildManualPrimaryRetry, manualPrimaryRetryBlockedReason, verifyManualPrimaryRetryReceipt } from '@/services/manualPrimaryRetry'

const props = defineProps<{ run: ProductionRun; preparation: ExecutionPreparationDetail; disabled?: boolean }>()
const emit = defineEmits<{ changed: []; busy: [value: boolean]; dirty: [value: boolean] }>()
const options = ref<ManualActionOptions | null>(null), pending = ref<JsonObject | null>(null)
const loading = ref(false), submitting = ref(false), acknowledged = ref(false)
const error = ref(''), notice = ref('')
let serial = 0
const failed = computed(() => props.preparation.presentation?.kind === 'primary' && props.preparation.presentation.task?.status === 'failed')
const blockedReason = computed(() => manualPrimaryRetryBlockedReason(props.preparation))
const contextError = computed(() => {
  if (blockedReason.value) return blockedReason.value
  if (!options.value) return '尚未读取重试动作'
  try { buildManualPrimaryRetry(props.run, props.preparation, options.value, 'preview-only'); return '' }
  catch (cause) { return apiErrorSummary(cause) }
})
watch(pending, value => emit('dirty', Boolean(value)))
watch(submitting, value => emit('busy', value))
onBeforeUnmount(() => { serial++ })
async function load() {
  if (submitting.value || pending.value || blockedReason.value) return
  const token = ++serial, runId = props.run.id
  loading.value = true; options.value = null; error.value = ''; acknowledged.value = false
  try {
    const value = await platformApi.getManualProductionActions(runId)
    if (token === serial) options.value = value
  } catch (cause) { if (token === serial) error.value = apiErrorSummary(cause) }
  finally { if (token === serial) loading.value = false }
}
async function submit() {
  if (submitting.value || props.disabled || (!pending.value && (!acknowledged.value || contextError.value))) return
  const token = serial, replaying = Boolean(pending.value), runId = props.run.id
  submitting.value = true; error.value = ''; notice.value = ''
  try {
    if (!pending.value) pending.value = buildManualPrimaryRetry(props.run, props.preparation, options.value!, crypto.randomUUID())
    const response = await platformApi.performManualProductionAction(runId, pending.value)
    if (token !== serial) return
    verifyManualPrimaryRetryReceipt(response, pending.value)
    pending.value = null; acknowledged.value = false
    notice.value = '已核对原准备的重试回执。回执不代表调用已提交或成功，请查看刷新后的主调用与阻断原因。'
    emit('changed')
  } catch (cause) {
    if (token !== serial) return
    error.value = apiErrorSummary(cause)
    if (!replaying && pending.value && isDefinitiveCreationRejection(cause)) {
      pending.value = null; acknowledged.value = false; options.value = null
      emit('changed')
    }
  } finally { if (token === serial) submitting.value = false }
}
watch([() => props.run.id, () => props.preparation.id], () => {
  serial++; pending.value = null; options.value = null; acknowledged.value = false
  loading.value = false; submitting.value = false; error.value = ''; notice.value = ''
  if (failed.value) void load()
}, { immediate: true })
watch([() => props.preparation.presentation?.task?.id, () => props.preparation.presentation?.task?.status,
  () => props.preparation.presentation?.task?.errorCode], () => {
  acknowledged.value = false
  if (failed.value && !pending.value) void load()
})
</script>

<template>
  <section v-if="failed || blockedReason || pending || notice" class="manual-primary-retry" aria-label="主调用恢复">
    <h4>{{ blockedReason ? '主调用状态待核对' : '重试此失败主调用' }}</h4>
    <p v-if="blockedReason" role="status">{{ blockedReason }}</p>
    <p v-else>沿用本次准备中已冻结的提示词、素材与参数，只新增一次主调用。不会重跑辅助字段、重新抽取种子或重做已成功的其他调用。</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="error" class="platform-alert" role="alert">{{ error }}</p>
    <p v-if="pending" role="status">上次提交结果尚未核实。请核对同一请求，不会创建新的重试意图。</p>
    <template v-else-if="failed && !blockedReason">
      <p v-if="loading" role="status">正在读取原运行的重试动作…</p>
      <p v-else-if="contextError" role="status">{{ contextError }}</p>
      <label v-else><input v-model="acknowledged" type="checkbox" :disabled="disabled || submitting">确认重试当前失败调用；可能提交一次模型调用。</label>
      <button v-if="error || contextError" type="button" class="button ghost small" :disabled="disabled || loading || submitting" @click="load">重新读取重试动作</button>
    </template>
    <button v-if="(failed && !blockedReason) || pending" type="button" class="button primary small"
      :disabled="disabled || submitting || loading || (!pending && (!acknowledged || !!contextError))" @click="submit">
      {{ submitting ? '正在核对…' : pending ? '核对同一重试请求' : '重试此失败主调用' }}
    </button>
  </section>
</template>

<style scoped>
.manual-primary-retry { margin: 1rem 0; padding: 1rem; border: 1px solid var(--border-color, #38424d); border-radius: 8px; overflow-wrap: anywhere; }
h4 { margin: 0 0 .5rem; } label { display: flex; gap: .5rem; align-items: start; margin: .75rem 0; }
input { flex: none; } button { margin: .4rem .5rem .4rem 0; white-space: normal; }
</style>
