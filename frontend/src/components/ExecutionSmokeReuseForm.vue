<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { isAxiosError } from 'axios'
import { executionPreparationApi } from '@/services/executionPreparationApi'
import { executionRegistryApi } from '@/services/executionRegistryApi'
import { executionReuseSmokeRequest, type ExecutionPreparationSummary, type ExecutionSmokeReuseFixture, type ExecutionSmokeRequest } from '@/services/executionPreparation'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import StructuredJsonEditor from './StructuredJsonEditor.vue'

const props = defineProps<{ preparation: ExecutionPreparationSummary; disabled: boolean }>()
const emit = defineEmits<{ busy: [value: boolean]; dirty: [value: boolean]; created: [preparationId: string] }>()
const source = ref<ExecutionSmokeReuseFixture | null>(null), busy = ref(false), error = ref('')
const allowExternal = ref(false), acknowledged = ref(false)
// Preserve the exact body and idempotency key after any uncertain response.
// A second click retries that request, never silently allocates another Smoke.
const submitted = ref<{ key: string; input: ExecutionSmokeRequest; versionId: string } | null>(null)
const stale = computed(() => source.value !== null && (source.value.preparationId !== props.preparation.id
  || source.value.expectedRevision !== props.preparation.revision || source.value.versionId !== props.preparation.executionInstanceVersion))
watch(busy, value => emit('busy', value))
watch(() => source.value !== null || submitted.value !== null, value => emit('dirty', value))
async function preview() {
  if (props.disabled || busy.value || submitted.value) return
  busy.value = true; error.value = ''; acknowledged.value = false; source.value = null
  allowExternal.value = false
  try {
    const result = await executionPreparationApi.smokeFixture(props.preparation.id, props.preparation.revision)
    if (result.preparationId !== props.preparation.id || result.expectedRevision !== props.preparation.revision
      || result.versionId !== props.preparation.executionInstanceVersion) throw new Error('复用输入与当前准备不一致，请重新读取')
    source.value = result
  } catch (cause) { error.value = apiErrorSummary(cause) }
  finally { busy.value = false }
}
function close() {
  if (busy.value || submitted.value) return
  source.value = null; error.value = ''; acknowledged.value = false; allowExternal.value = false
}
async function submit() {
  if (props.disabled || busy.value || (!submitted.value && (!source.value || stale.value || !acknowledged.value))) return
  error.value = ''
  try {
    if (!submitted.value) submitted.value = { key: crypto.randomUUID(), versionId: source.value!.versionId,
      input: executionReuseSmokeRequest(source.value!, allowExternal.value) }
    busy.value = true
    const result = await executionRegistryApi.smoke(submitted.value.versionId, submitted.value.input, submitted.value.key)
    if (result.preparation.executionInstanceVersion !== submitted.value.versionId) throw new Error('烟测结果不属于指定实例版本，请保留本次请求并重试确认')
    submitted.value = null; source.value = null
    busy.value = false
    emit('created', result.preparation.id)
  } catch (cause) {
    error.value = apiErrorSummary(cause)
    if (isAxiosError(cause) && [400, 401, 403, 404, 409, 422].includes(cause.response?.status ?? 0)) {
      // An explicit request rejection can be reviewed and corrected. Network
      // errors, timeouts and 5xx retain the submitted body/key for safe retry.
      submitted.value = null; acknowledged.value = false
    }
  }
  finally { busy.value = false }
}
</script>

<template>
  <section class="reuse-smoke" :aria-busy="busy">
    <h4>用相同输入做一次新烟测</h4>
    <p>保留原素材、已解析参数（含原种子）和明确采用的已确认字段。不复制原任务授权，也不会把历史成功当成新的通过证据。</p>
    <button type="button" class="button ghost small" :disabled="disabled || busy || !!submitted" @click="preview">{{ busy && !submitted ? '正在读取…' : '读取并复核本次输入' }}</button>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <template v-if="source">
      <p>来源准备修订 {{ source.expectedRevision }} · 复用 {{ source.fields.length }} 项已确认字段。缺少的必填字段仍需按原规则准备或人工填写。</p>
      <p v-if="stale" class="platform-alert danger">原准备已经变化，请重新读取并复核。不会自动换用新输入。</p>
      <details><summary>查看原输入与字段选择</summary><StructuredJsonEditor :model-value="source.fixture" label="原始输入（只读）" disabled fixed-object/><ul><li v-for="row in source.fields" :key="row.field_value_id">{{ row.unit_key }} / {{ row.field_key }} · {{ row.field_value_id }}</li></ul></details>
      <p>新烟测使用当前系统资源策略，超额等待人工确认；不继承原调用授权，也不会自动重试。</p>
      <label class="reuse-check"><input v-model="allowExternal" type="checkbox" :disabled="disabled || busy || !!submitted"/>允许这次烟测调用外部模型，可能产生实际费用</label>
      <label class="reuse-check"><input v-model="acknowledged" type="checkbox" :disabled="disabled || busy || !!submitted"/>我已复核相同输入、字段选择及本次授权，确认创建新的真实烟测</label>
      <p v-if="submitted">已提交请求，尚未确认结果。重试将使用同一请求标识，不创建另一份烟测；请勿换一个入口重复提交。请求标识：{{ submitted.key }}</p>
      <div class="reuse-actions"><button type="button" class="button primary small" :disabled="disabled || busy || (!submitted && (stale || !acknowledged))" @click="submit">{{ busy ? '正在提交…' : submitted ? '重试确认同一请求' : '创建新的真实烟测' }}</button><button type="button" class="button ghost small" :disabled="disabled || busy || !!submitted" @click="close">取消复核</button></div>
    </template>
  </section>
</template>

<style scoped>
.reuse-smoke{display:grid;gap:10px;border-top:1px solid var(--border);padding-top:12px}.reuse-smoke h4,.reuse-smoke p{margin:0}.reuse-smoke p,.reuse-smoke li,.reuse-smoke label{font-size:12px;line-height:1.7;overflow-wrap:anywhere}.reuse-smoke label{display:grid;gap:8px}.reuse-smoke .reuse-check{display:flex;gap:8px;align-items:flex-start}.reuse-check input{width:auto;flex:none;margin-top:5px}.reuse-actions{display:flex;gap:8px;flex-wrap:wrap}.reuse-smoke summary{cursor:pointer}
</style>
