<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { executionRegistryApi } from '@/services/executionRegistryApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { buildExecutionRequestFixture, emptyExecutionRequestDraft, executionRequestFormContract, resourceSmokeRequestFormContract,
  type ExecutionRequestDraft, type ExecutionRequestFormContract } from '@/services/executionRequestForm'
import { type ExecutionSmokeRequest } from '@/services/executionPreparation'
import ExecutionRequestInputEditor from './ExecutionRequestInputEditor.vue'
import ExecutionFieldSmokeSource from './ExecutionFieldSmokeSource.vue'
import ExecutionSmokeSourceContext from './ExecutionSmokeSourceContext.vue'
import type { SmokeSourceContext } from '@/services/executionSmokeSourceContext'
import type { FieldSmokeSource } from '@/services/executionFieldSmoke'
import { resourceSmokeApi, type ResourceSmokeContext } from '@/services/executionResourceSmoke'
import { readAuthSession } from '@/services/authRefresh'
import { isDefinitiveCreationRejection } from '@/services/productionOnboarding'
import BaseModal from './BaseModal.vue'

const props = defineProps<{ versionId?: string; resource?: ResourceSmokeContext }>()
const emit = defineEmits<{ busy: [value: boolean]; dirty: [value: boolean]; locked: [value: boolean]; created: [preparationId: string] }>()
const contract = ref<ExecutionRequestFormContract<string | null> | null>(null), draft = ref<ExecutionRequestDraft | null>(null)
const loading = ref(false), submitting = ref(false), uploading = ref(false), error = ref(''), inputErrors = ref<string[]>([])
const allowExternal = ref(false), reviewed = ref(false), initial = ref('null')
const pending = ref<{ key: string; versionId: string | null; resource?: ResourceSmokeContext; input: ExecutionSmokeRequest } | null>(null)
const fieldSourceBusy = ref(false), fieldSourceReady = ref(false)
const sourceContext=ref<SmokeSourceContext|null>(null), sourceContextBusy=ref(false)
const usesReviewedSources=computed(()=>Object.values(draft.value?.contexts??{}).some(row=>row && contract.value?.contextFields.some(field=>field.selector_key===row.selectorKey && field.revision_kind==='visual_rules_approval')))
const isFieldLeaf = computed(() => ['prompt.fields.generate','prompt.fields.translate'].includes(contract.value?.operation ?? ''))
const busy = computed(() => loading.value || submitting.value || uploading.value || fieldSourceBusy.value || sourceContextBusy.value)
const inputTouched = ref(false)
const dirty = computed(() => !!pending.value || JSON.stringify(draft.value) !== initial.value || allowExternal.value || !!sourceContext.value || inputTouched.value && inputErrors.value.length > 0)
const formGeneration = ref(0)
const readState = ref<'loading'|'ready'|'error'>('loading'), loadedScope = ref('')
const scope = () => JSON.stringify([props.versionId, props.resource, typeof localStorage === 'undefined' ? '' : readAuthSession(localStorage).userId])
const reloadDiscard = ref<{ fingerprint: string; targetLabel: string } | null>(null)
const reloadFingerprint = () => JSON.stringify([scope(), draft.value, allowExternal.value, sourceContext.value, inputErrors.value, reviewed.value, busy.value, pending.value])
watch(reloadFingerprint, () => { reloadDiscard.value = null }, { flush: 'sync' })
watch(() => !!pending.value || !!reloadDiscard.value, value => emit('locked', value), { flush: 'sync' })
let sequence = 0, submissionSequence = 0, disposed = false, pendingScope = ''
onBeforeUnmount(() => { disposed = true; sequence++; submissionSequence++; reloadDiscard.value = null })
watch(busy, value => emit('busy', value))
watch(dirty, value => emit('dirty', value))
watch([draft, allowExternal, sourceContext], () => { if (!pending.value) reviewed.value = false }, { deep: true })
watch(usesReviewedSources,value=>{if(!value)sourceContext.value=null})
async function load() {
  if (disposed || pending.value || busy.value || reloadDiscard.value) return
  if (draft.value && (dirty.value || reviewed.value)) {
    reloadDiscard.value = { fingerprint: reloadFingerprint(), targetLabel: props.resource
      ? `${props.resource.target.kind} · ${props.resource.target.id} · ${props.resource.selection.operation}` : `执行实例版本 · ${props.versionId}` }
    return
  }
  await readContract()
}
async function confirmReloadDiscard() {
  const confirmation = reloadDiscard.value; reloadDiscard.value = null
  if (!confirmation || disposed || confirmation.fingerprint !== reloadFingerprint()) return
  await readContract()
}
async function readContract() {
  if (disposed || pending.value || busy.value) return
  const request = ++sequence, requestedScope = scope()
  loading.value = true; readState.value = 'loading'; error.value = ''; reviewed.value = false
  try {
    if (Boolean(props.versionId) === Boolean(props.resource)) throw new Error('请明确选择执行实例或基础资源，不能混用两类验收')
    const result = props.resource ? await resourceSmokeApi.contract(props.resource) : await executionRegistryApi.contract(props.versionId!)
    if (disposed || request !== sequence || requestedScope !== scope()) return
    if (result.version_id !== (props.versionId ?? null)) throw new Error('输入合同与当前验收对象不一致')
    const nextContract = 'scope' in result ? resourceSmokeRequestFormContract(result) : executionRequestFormContract(result)
    const nextDraft = emptyExecutionRequestDraft(nextContract)
    contract.value = nextContract; draft.value = nextDraft; initial.value = JSON.stringify(draft.value)
    loadedScope.value = requestedScope; readState.value = 'ready'
    allowExternal.value = false; reviewed.value = false; inputErrors.value = []; inputTouched.value = false
    fieldSourceReady.value = false
    sourceContext.value=null
    formGeneration.value++
  } catch (cause) { if (!disposed && request === sequence && requestedScope === scope()) { error.value = apiErrorSummary(cause); readState.value = 'error' } }
  finally { if (!disposed && request === sequence) {
    loading.value = false
    if (requestedScope !== scope()) { readState.value = 'error'; error.value = '验收对象或登录身份已变化，请重新读取当前输入合同。' }
  } }
}
async function submit() {
  if (disposed || busy.value || reloadDiscard.value || readState.value !== 'ready' || loadedScope.value !== scope()
    || !contract.value || !draft.value || (!pending.value && (!reviewed.value || inputErrors.value.length || usesReviewedSources.value&&!sourceContext.value || isFieldLeaf.value&&!fieldSourceReady.value))) return
  if (pending.value && pendingScope !== scope()) { error.value = '原请求属于其他验收对象或登录身份，请返回原上下文核对；不会另建请求。'; return }
  const wasPending = !!pending.value, request = ++submissionSequence, requestedScope = scope()
  error.value = ''
  try {
    if (!pending.value) pendingScope = requestedScope
    if (!pending.value) pending.value = { key: crypto.randomUUID(), versionId: props.versionId ?? null,
      ...(props.resource ? { resource: { target: {...props.resource.target}, selection: {...props.resource.selection} } } : {}), input: {
      expectedDefinitionSha256: contract.value.sha256, fixture: buildExecutionRequestFixture(contract.value, draft.value),
      fieldValues: [], allowExternalCalls: allowExternal.value,
      ...(sourceContext.value ? {reviewedSourceContext:{run_id:sourceContext.value.run_id,stage_key:sourceContext.value.stage_key,run_plan_sha256:sourceContext.value.run_plan_sha256}} : {}),
    } }
    submitting.value = true
    const result = pending.value.resource ? await resourceSmokeApi.create(pending.value.resource, pending.value.input, pending.value.key)
      : await executionRegistryApi.smoke(pending.value.versionId!, pending.value.input, pending.value.key)
    if (disposed || request !== submissionSequence || requestedScope !== scope()) return
    if (result.preparation.executionInstanceVersion !== pending.value.versionId) throw new Error('返回的烟测准备不属于当前版本，请重试确认同一请求')
    pending.value = null; submitting.value = false
    emit('created', result.preparation.id)
  } catch (cause) {
    if (disposed || request !== submissionSequence || requestedScope !== scope()) return
    error.value = apiErrorSummary(cause)
    if (!wasPending && isDefinitiveCreationRejection(cause)) { pending.value = null; reviewed.value = false }
  } finally { if (!disposed && request === submissionSequence) submitting.value = false }
}
function invalidateFieldSource() {
  fieldSourceReady.value = false; reviewed.value = false
  if (draft.value && contract.value) for (const key of contract.value.serverOwnedInputs) {
    if (draft.value.literals[key]) draft.value.literals[key]!.value = {}
  }
}
function applyFieldSource(source: FieldSmokeSource<string | null>) {
  if (!contract.value || !draft.value || source.execution_instance_version_id!==(props.versionId ?? null)
    || source.definition_sha256!==contract.value.sha256 || !contract.value.serverOwnedInputs.includes(source.input_key)
    || !draft.value.literals[source.input_key] || !source.source) {
    error.value='字段测试输入与所选实例不一致'; invalidateFieldSource(); return
  }
  draft.value.literals[source.input_key] = { enabled:true,value:source.source.value }
  fieldSourceReady.value=true; reviewed.value=false
}
watch(() => [props.versionId, props.resource], () => {
  sequence++; reloadDiscard.value = null; loading.value = false; readState.value = 'error'; reviewed.value = false
  if (pending.value) {
    if (scope() === loadedScope.value && scope() === pendingScope) readState.value = 'ready'
    error.value = '原烟测请求结果尚未确认，请保留原请求标识并返回原验收对象核对。'; return
  }
  void readContract()
}, { immediate: true, deep: true })
</script>

<template>
  <section class="smoke-create-form" :aria-busy="busy" @input.capture="inputTouched=true">
    <p>{{ resource ? '本次只验收所选基础资源，不代表某个执行实例组合通过。' : '发布实例并不代表组合通过。' }}本次检查实际素材、参数、预算及资源状态；缺少的策略字段可在创建后的准备工作台填写、复核和采用。</p>
    <p v-if="loading" role="status">正在读取当前验收对象的输入合同…</p>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <button type="button" class="button ghost small" :disabled="busy || !!pending || !!reloadDiscard" @click="load">重新读取输入合同</button>
    <p v-if="draft && readState!=='ready'" role="status">原输入保留，但当前合同尚未重新读取成功，禁止提交；重新读取成功后才重置输入和授权。</p>
    <template v-if="contract && draft">
      <p>执行能力 {{ contract.operation }} · {{ contract.mode || '未声明模式' }} · 定义 {{ contract.sha256.slice(0,12) }}</p>
      <ExecutionFieldSmokeSource v-if="isFieldLeaf" :key="formGeneration" :version-id="versionId ?? null" :resource="resource" :definition-sha256="contract.sha256" :disabled="loading||submitting||uploading||!!pending||readState!=='ready'" @busy="fieldSourceBusy=$event" @invalidated="invalidateFieldSource" @built="applyFieldSource"/>
      <p v-if="isFieldLeaf" role="status">{{ fieldSourceReady?'字段测试输入已组装，请继续核对参数和授权。':'请先选择字段来源并组装测试输入。' }}</p>
      <ExecutionSmokeSourceContext v-if="usesReviewedSources && versionId" :key="formGeneration" v-model="sourceContext" :version-id="versionId" :disabled="busy || !!pending || readState!=='ready'" @busy="sourceContextBusy=$event"/>
      <ExecutionRequestInputEditor :key="formGeneration" v-model="draft" :contract="contract" :project-id="sourceContext?.project_id" :run-id="sourceContext?.run_id" :stage-key="sourceContext?.stage_key" :disabled="busy || !!pending || readState!=='ready'" @busy="uploading=$event" @validity="inputErrors=$event"/>
      <ul v-if="inputErrors.length" role="alert"><li v-for="item in inputErrors" :key="item">{{ item }}</li></ul>
      <p>使用当前系统资源策略。超过预算或调用次数提醒值时，保留准备并等待人工确认，不直接判定生成失败。</p>
      <label class="smoke-check"><input v-model="allowExternal" type="checkbox" :disabled="busy || !!pending || readState!=='ready'"/>允许本次调用外部模型，可能产生实际费用</label>
      <label class="smoke-check"><input v-model="reviewed" type="checkbox" :disabled="busy || !!pending || readState!=='ready'"/>已核对输入、实际执行组合及本次授权，确认创建烟测任务</label>
      <p v-if="pending">尚未确认提交结果。重试会使用同一请求体和标识；核对完成前不能更换资源或关闭表单另建任务：{{ pending.key }}</p>
      <button type="button" class="button primary" :disabled="busy || !!reloadDiscard || readState!=='ready' || loadedScope!==scope() || (!pending && (!reviewed || inputErrors.length > 0 || usesReviewedSources&&!sourceContext || isFieldLeaf&&!fieldSourceReady))" @click="submit">{{ submitting ? '正在创建…' : pending ? '重试确认同一请求' : '创建真实烟测任务' }}</button>
    </template>
    <BaseModal :open="!!reloadDiscard" title="重新读取并重置测试输入？" eyebrow="仅读取合同 · 不创建烟测任务" width="600px" @close="reloadDiscard=null">
      <p class="reload-target">{{ reloadDiscard?.targetLabel }}</p><p>读取成功后将按新合同重置本次输入、素材选择、来源上下文和调用授权。读取失败会保留原输入并禁止提交；返回或Escape不改变当前内容。</p>
      <template #footer><button type="button" class="button ghost" @click="reloadDiscard=null">返回继续填写</button><button type="button" class="button danger" :disabled="busy || !!pending" @click="confirmReloadDiscard">确认读取并重置</button></template>
    </BaseModal>
  </section>
</template>

<style scoped>
.smoke-create-form{display:grid;gap:12px;min-width:0}.smoke-create-form>p,.smoke-create-form>label{font-size:12px;line-height:1.7;overflow-wrap:anywhere}.smoke-create-form label{display:grid;gap:6px}.smoke-create-form .smoke-check{display:flex;gap:8px;align-items:flex-start}.smoke-check input{width:auto;margin-top:5px}
.reload-target{overflow-wrap:anywhere}
</style>
