<script setup lang="ts">
import ExecutionResponseDiagnostics from './ExecutionResponseDiagnostics.vue'
import ExecutionConfigurationReplacement from './ExecutionConfigurationReplacement.vue'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { taskCenterTarget } from '@/services/taskCenter'
import { executionPreparationApi } from '@/services/executionPreparationApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { preparationFieldConfirmInput, preparationFieldWait, type ExecutionAuxiliaryBatch, type ExecutionFieldDetail, type ExecutionFieldSummary, type ExecutionPreparationDetail, type ExecutionPreparationFieldSlot } from '@/services/executionPreparation'
import PlatformStatusPill from './PlatformStatusPill.vue'
import ReadonlyDataValue from './ReadonlyDataValue.vue'
import ExecutionPreparationFacts from './ExecutionPreparationFacts.vue'
import MediaGenerationTargetSummary from './MediaGenerationTargetSummary.vue'
import ExecutionSmokeReuseForm from './ExecutionSmokeReuseForm.vue'
import ExecutionReturnedText from './ExecutionReturnedText.vue'
import ExecutionPromptDisplay from './ExecutionPromptDisplay.vue'
import ExecutionStorageMetrics from './ExecutionStorageMetrics.vue'
import ExecutionFieldBulkReview from './ExecutionFieldBulkReview.vue'
import ExecutionQualityNotices from './ExecutionQualityNotices.vue'
import ManualPrimaryRetry from './ManualPrimaryRetry.vue'
import type { ProductionRun } from '@/services/platformTypes'

const props = defineProps<{ preparationId: string; versionId: string | null; purpose: string; allowSmoke?: boolean; refreshToken?: string; allowPreparationNavigation?: boolean; manualRun?: ProductionRun }>()
const emit = defineEmits<{ changed: []; busy: [value: boolean]; dirty: [value: boolean]; smokeCreated: [preparationId: string]; openParent: [preparationId: string] }>()
const preparation = ref<ExecutionPreparationDetail | null>(null), loading = ref(false), busy = ref(false)
const uncertainMediaAcknowledged = ref(false)
watch(() => [preparation.value?.id, preparation.value?.revision, preparation.value?.mediaUncertainRetry?.taskUpdatedAt], () => { uncertainMediaAcknowledged.value = false })
async function retryUncertainMedia() {
  const current = preparation.value, recovery = current?.mediaUncertainRetry
  if (!current || !recovery?.canRetry || !uncertainMediaAcknowledged.value || busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await executionPreparationApi.retryUncertainMedia(current.id, current.revision, recovery)
    if (props.preparationId !== current.id) return
    await load()
    notice.value = '已创建一次关联尝试并进入原队列；旧调用仍保留未决结果与费用，不代表生成成功。'
    emit('changed')
  } catch (cause) { if (props.preparationId === current.id) error.value = apiErrorSummary(cause) }
  finally { busy.value = false }
}
async function recoverMediaArchive() {
  const current = preparation.value, recovery = current?.mediaArchiveRecovery
  if (!current || !recovery?.canRecover || busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await executionPreparationApi.recoverMediaArchive(current.id, current.revision, recovery)
    if (props.preparationId !== current.id) return
    await load()
    notice.value = '原任务已排队重新验收并归档已保存回执，不会重新生成；请刷新查看真实处理结果。'
    emit('changed')
  } catch (cause) { if (props.preparationId === current.id) error.value = apiErrorSummary(cause) }
  finally { busy.value = false }
}
const fieldWait = computed(() => preparationFieldWait(preparation.value))
const error = ref(''), notice = ref(''), needsRefresh = ref(false)
const history = ref<{ count: number; next: string | null; previous: string | null; results: ExecutionFieldSummary[] } | null>(null)
const historyPage = ref(1), historyLoading = ref(false), historyError = ref(''), historyOpen = ref(false)
const field = ref<ExecutionFieldDetail | null>(null), fieldLoading = ref(false), fieldError = ref('')
const slotKey = ref(''), text = ref(''), replaceAcknowledged = ref(false), continueAcknowledged = ref(false)
const reviewSlotKey = ref(''), editing = ref(false)
const bulkReviewOpen = ref(false)
async function bulkReviewed(count: number) {
  bulkReviewOpen.value = false; busy.value = false
  notice.value = `已确认并采用 ${count} 个字段；后续执行仍遵循原运行授权与资源检查。`
  await load()
  if (reviewSlot.value) await openSlot(reviewSlot.value)
  emit('changed')
}
const smokeDirty = ref(false)
const primaryRetryDirty = ref(false)
const configurationReplacementDirty = ref(false)
const resourceAcknowledged = ref(false)
const recoveryText = ref(''), recoveryAcknowledged = ref(false)
const pendingCancellation = ref<{ id: string; revision: number } | null>(null)
const pendingBatchRetry = ref<{ id: string; revision: number; batch: ExecutionAuxiliaryBatch } | null>(null)
const returned = ref<ExecutionPreparationDetail | null>(null), returnedLoading = ref(false), returnedError = ref('')
let serial = 0, historySerial = 0, fieldSerial = 0, returnedSerial = 0
let appliedRefreshToken: string | undefined
onBeforeUnmount(() => { serial++; historySerial++; fieldSerial++; returnedSerial++ })
const key = (unit: string, name: string) => JSON.stringify([unit, name])
const chosenSlot = computed(() => preparation.value?.fieldSlots.find(row => key(row.unitKey, row.fieldKey) === slotKey.value))
const reviewSlot = computed(() => preparation.value?.fieldSlots.find(row => key(row.unitKey, row.fieldKey) === reviewSlotKey.value))
const reviewBatch = computed(() => preparation.value?.auxiliaryBatches.find(row => reviewSlot.value && row.fieldKeys.includes(reviewSlot.value.fieldKey)))
const auxiliaryResponseVisible = computed(() => preparation.value?.presentation?.kind === 'auxiliary'
  && Boolean(preparation.value.textResponseReview || preparation.value.presentation.task?.errorCode === 'execution_response_invalid'))
const diagnosticTarget = computed(() => {
  const current = preparation.value, taskId = current?.responseDiagnostic?.taskId
  if (!current || !taskId) return null
  if (current.stageRun) return taskCenterTarget({ kind: 'preparation', id: current.id, attemptId: taskId })
  if ([current.assetJob, current.deploymentSmoke, current.workflowSmoke].includes(taskId)) {
    return taskCenterTarget({ kind: 'preparation', id: current.id })
  }
  return null
})
async function loadReturnedText() {
  const batch = reviewBatch.value, parent = preparation.value
  if (!batch?.preparationId || !batch.taskId || !parent || busy.value || hasUnsavedDraft.value || needsRefresh.value) return
  const request = ++returnedSerial
  returnedLoading.value = true; returnedError.value = ''; returned.value = null
  try {
    const data = await executionPreparationApi.get(batch.preparationId)
    if (request !== returnedSerial) return
    if (data.id !== batch.preparationId || data.stageRun !== parent.stageRun || data.purpose !== parent.purpose
        || data.presentation?.kind !== 'auxiliary' || data.presentation.parent?.id !== parent.id
        || data.presentation.task?.id !== batch.taskId || (data.textResponseReview && data.textResponseReview.attemptId !== batch.taskId)
        || (data.responseDiagnostic?.taskId && data.responseDiagnostic.taskId !== batch.taskId)) throw new Error('实际返回与当前镜头或辅助调用不一致，请重新读取')
    returned.value = data
  } catch (cause) { if (request === returnedSerial) returnedError.value = apiErrorSummary(cause) }
  finally { if (request === returnedSerial) returnedLoading.value = false }
}
const fieldLabel = (unit: string, name: string) => preparation.value?.fieldSlots.find(row => row.unitKey === unit && row.fieldKey === name)?.fieldLabel || name
const selectionFor = (slot: ExecutionPreparationFieldSlot) => preparation.value?.fieldSelections.find(row => row.unitKey === slot.unitKey && row.fieldKey === slot.fieldKey)
function languageLabel(language: string): string {
  if (!language) return '未明确指定'
  if (language === 'any') return '不限语言'
  if (language.startsWith('bilingual:')) return language.slice(10).split('+').map(languageLabel).join(' + ')
  try { return new Intl.DisplayNames(['zh-CN'], { type: 'language' }).of(language) || language } catch { return language }
}
async function openSlot(slot: ExecutionPreparationFieldSlot) {
  if (busy.value || hasUnsavedDraft.value) return
  returnedSerial++; returned.value = null; returnedError.value = ''; returnedLoading.value = false
  reviewSlotKey.value = key(slot.unitKey, slot.fieldKey); editing.value = false; slotKey.value = ''
  const id = selectionFor(slot)?.fieldValue || slot.latestValueId
  if (id) await openField(id)
  else { fieldSerial++; field.value = null; fieldLoading.value = false; fieldError.value = '' }
}
function editField() {
  if (!editable.value || !reviewSlot.value || hasUnsavedDraft.value) return
  slotKey.value = reviewSlotKey.value; text.value = field.value?.text ?? ''; editing.value = true
}
const editable = computed(() => preparation.value !== null && preparation.value.requestFrozenAt === null
  && preparation.value.status !== 'cancelled' && !loading.value && !busy.value && !needsRefresh.value)
const resourcePending = computed(() => preparation.value?.reasonCode === 'execution_resource_confirmation_required')
const manualFailedPrimary = computed(() => props.manualRun?.planSource === 'manual'
  && preparation.value?.requestFrozenAt !== null && preparation.value?.presentation?.kind === 'primary'
  && preparation.value.presentation.task?.status === 'failed')
const hasUnsavedDraft = computed(() => text.value !== '' || smokeDirty.value || recoveryText.value !== '' || primaryRetryDirty.value || configurationReplacementDirty.value)
async function primaryRetried() { notice.value = ''; await load(); emit('changed') }
const canRetryBatch = computed(() => editable.value && !hasUnsavedDraft.value && !pendingCancellation.value)
const canAdvance = computed(() => !manualFailedPrimary.value && (editable.value || (resourcePending.value && !loading.value && !busy.value && !needsRefresh.value))
  && preparation.value?.status !== 'cancelled' && !hasUnsavedDraft.value && !pendingBatchRetry.value && !pendingCancellation.value)
const currentSelection = computed(() => field.value && preparation.value?.fieldSelections.find(row => row.unitKey === field.value!.unitKey && row.fieldKey === field.value!.fieldKey))
const replacing = computed(() => currentSelection.value && currentSelection.value.fieldValue !== field.value?.id)
const localField = computed(() => field.value?.preparation === props.preparationId)
const time = (value: string | null) => value === null ? '尚无记录' : new Date(value).toLocaleString()
function discardDraft() {
  if (!busy.value && window.confirm('清空将丢弃此字段尚未保存的正文，是否继续？')) text.value = ''
}
watch(busy, value => emit('busy', value))
watch(hasUnsavedDraft, value => { emit('dirty', value); if (value) continueAcknowledged.value = false })

function verify(data: ExecutionPreparationDetail) {
  if (data.id !== props.preparationId || data.executionInstanceVersion !== props.versionId || data.purpose !== props.purpose
    || (props.versionId === null) !== (props.purpose === 'resource_smoke')) {
    throw new Error('准备详情与当前任务、实例版本或用途不一致')
  }
  return data
}
async function load() {
  const request = ++serial
  returnedSerial++; returned.value = null; returnedError.value = ''; returnedLoading.value = false
  appliedRefreshToken = props.refreshToken
  loading.value = true; error.value = ''; continueAcknowledged.value = false; resourceAcknowledged.value = false; pendingCancellation.value = null
  pendingBatchRetry.value = null
  try {
    const data = await executionPreparationApi.get(props.preparationId)
    if (request !== serial) return
    preparation.value = verify(data); needsRefresh.value = false
    if (!reviewSlotKey.value && data.fieldSlots[0]) void openSlot(data.fieldSlots[0])
    if (historyOpen.value) await loadHistory(historyPage.value)
  } catch (cause) { if (request === serial) { error.value = apiErrorSummary(cause); needsRefresh.value = true } }
  finally { if (request === serial) loading.value = false }
}
async function loadHistory(page = 1) {
  const request = ++historySerial, id = props.preparationId
  historyOpen.value = true; historyLoading.value = true; historyError.value = ''
  try {
    const result = await executionPreparationApi.fields(id, page)
    if (request !== historySerial) return
    if (result.results.some((row: ExecutionFieldSummary) => row.preparation !== id)) throw new Error('字段历史不属于当前准备')
    history.value = result; historyPage.value = page
  } catch (cause) { if (request === historySerial) historyError.value = apiErrorSummary(cause) }
  finally { if (request === historySerial) historyLoading.value = false }
}
async function openField(id: string) {
  const request = ++fieldSerial
  field.value = null; fieldLoading.value = true; fieldError.value = ''; replaceAcknowledged.value = false
  try {
    const result = await executionPreparationApi.field(id)
    if (request !== fieldSerial) return
    if (result.id !== id) throw new Error('字段详情身份与选择不一致')
    if (!preparation.value?.fieldSlots.some(row => row.unitKey === result.unitKey && row.fieldKey === result.fieldKey)
      || (result.preparation !== props.preparationId && !preparation.value.fieldSelections.some(row => row.fieldValue === id))) throw new Error('字段详情不属于当前准备或已采用来源')
    reviewSlotKey.value = key(result.unitKey, result.fieldKey)
    field.value = result
  } catch (cause) { if (request === fieldSerial) fieldError.value = apiErrorSummary(cause) }
  finally { if (request === fieldSerial) fieldLoading.value = false }
}
async function createField() {
  if (!editable.value || !preparation.value || !chosenSlot.value) return
  const slot = chosenSlot.value, current = preparation.value
  let request = serial
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await executionPreparationApi.createField({ preparationId: current.id,
      unitKey: slot.unitKey, fieldKey: slot.fieldKey, text: text.value, revision: slot.nextRevision,
      expectedPreparationRevision: current.revision })
    if (request !== serial || current.id !== props.preparationId) return
    if (result.field.preparation !== props.preparationId || result.preparation.id !== props.preparationId) throw new Error('保存结果不属于当前准备，请重新读取')
    text.value = ''; editing.value = false; field.value = result.field; replaceAcknowledged.value = false
    notice.value = '已保存待确认修订，尚未采用，也没有触发模型调用。'
    const refresh = load(); request = serial; await refresh
    if (request === serial && current.id === props.preparationId) emit('changed')
  } catch (cause) { if (request === serial) { error.value = apiErrorSummary(cause); needsRefresh.value = true } }
  finally { if (request === serial) busy.value = false }
}
async function review(action: 'confirm' | 'select' | 'reject') {
  if (!editable.value || !preparation.value || !field.value || !localField.value || hasUnsavedDraft.value) return
  const viewed = field.value, current = preparation.value
  let request = serial
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const input = preparationFieldConfirmInput(current, viewed, action === 'select', replaceAcknowledged.value)
    const result = action === 'reject'
      ? await executionPreparationApi.rejectField(viewed.id, input)
      : await executionPreparationApi.confirmField(viewed.id, input)
    if (request !== serial || current.id !== props.preparationId) return
    if (result.field.id !== viewed.id || result.preparation.id !== props.preparationId) throw new Error('复核结果不属于当前字段，请重新读取')
    field.value = result.field; replaceAcknowledged.value = false
    notice.value = action === 'select' ? '已确认并采用此修订；系统会按原任务规则检查后续执行条件。采用本身不代表主调用已提交。'
      : action === 'confirm' ? '已确认此修订，尚未将它选入本次请求。' : '已拒绝此修订，可以另存一份新的修订。'
    const refresh = load(); request = serial; await refresh
    if (request === serial && current.id === props.preparationId) emit('changed')
  } catch (cause) { if (request === serial) { error.value = apiErrorSummary(cause); needsRefresh.value = true } }
  finally { if (request === serial) busy.value = false }
}
async function advance() {
  if (!canAdvance.value || !preparation.value || !continueAcknowledged.value) return
  const current = preparation.value, request = serial
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await executionPreparationApi.continue(current.id, current.revision)
    if (request !== serial || current.id !== props.preparationId) return
    preparation.value = verify(result)
    notice.value = '服务端已处理继续请求。是否排队、等待审核或阻断，以当前准备状态为准。'
    needsRefresh.value = false; continueAcknowledged.value = false; emit('changed')
    if (historyOpen.value) await loadHistory(historyPage.value)
  } catch (cause) { if (request === serial) { error.value = apiErrorSummary(cause); needsRefresh.value = true } }
  finally { if (request === serial) busy.value = false }
}
async function confirmResources() {
  const current = preparation.value, review = current?.resourceReview
  if (!current || !review || !review.policyCurrent || review.confirmed || !resourceAcknowledged.value || busy.value || needsRefresh.value || hasUnsavedDraft.value) return
  const request = serial
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await executionPreparationApi.confirmResources(current.id, current.revision, review.fingerprint)
    if (request !== serial || current.id !== props.preparationId) return
    preparation.value = verify(result)
    resourceAcknowledged.value = false; continueAcknowledged.value = false
    notice.value = manualFailedPrimary.value
      ? '已记录本次资源确认，尚未执行模型。核对后点击“重试此失败主调用”。'
      : '已记录本次资源确认，尚未执行模型。核对后点击“检查并继续”；费用或策略改变时会再次提醒。'
    emit('changed')
  } catch (cause) { if (request === serial) { error.value = apiErrorSummary(cause); needsRefresh.value = true } }
  finally { if (request === serial) busy.value = false }
}
async function completeResponse() {
  const current = preparation.value, review = current?.textResponseReview
  if (!current || !review?.editable || !recoveryText.value.trim() || !recoveryAcknowledged.value || busy.value || needsRefresh.value) return
  const request = serial
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await executionPreparationApi.completeTextResponse(current.id, current.revision, review.id, review.originalSha256, recoveryText.value)
    if (request !== serial || current.id !== props.preparationId) return
    if (result.textResponseReview?.id !== review.id || !result.textResponseReview.confirmedAt
        || result.textResponseReview.correctedText !== recoveryText.value
        || result.textResponseReview.originalSha256 !== review.originalSha256) {
      needsRefresh.value = true
      throw new Error('补正回执与本次修订不一致；请刷新核对，不会伪装保存成功。')
    }
    preparation.value = verify(result)
    recoveryText.value = ''; recoveryAcknowledged.value = false
    notice.value = review.assetJobId
      ? '人工修订已保存为本任务结果；未调用模型、未自动采用。请返回素材任务查看结果或剧本素材。'
      : '人工补全已通过校验并保存为候选；未调用模型，未自动采用。请回到候选审核继续。'
    emit('changed')
  } catch (cause) { if (request === serial) error.value = apiErrorSummary(cause) }
  finally { if (request === serial) busy.value = false }
}
function requestBatchRetry(batch: ExecutionAuxiliaryBatch) {
  if (!canRetryBatch.value || !preparation.value || pendingBatchRetry.value) return
  const current = preparation.value.auxiliaryBatches.find(row => row.fieldBatchKey === batch.fieldBatchKey)
  if (!current?.canRetry) return
  pendingBatchRetry.value = { id: preparation.value.id, revision: preparation.value.revision,
    batch: { ...current, fieldKeys: [...current.fieldKeys] } }
  continueAcknowledged.value = false
}
async function retryBatch() {
  if (!canRetryBatch.value || !preparation.value || !pendingBatchRetry.value) return
  const confirmed = pendingBatchRetry.value
  const batch = preparation.value.auxiliaryBatches.find(row => row.fieldBatchKey === confirmed.batch.fieldBatchKey)
  if (confirmed.id !== props.preparationId || confirmed.revision !== preparation.value.revision
    || !batch?.canRetry || batch.attemptNumber !== confirmed.batch.attemptNumber || batch.taskId !== confirmed.batch.taskId
    || batch.preparationId !== confirmed.batch.preparationId || batch.status !== confirmed.batch.status
    || batch.requiresUncertainAcknowledgement !== confirmed.batch.requiresUncertainAcknowledgement) {
    pendingBatchRetry.value = null; error.value = '准备或辅助批次已变化，请重新读取后再次确认重试。'; needsRefresh.value = true; return
  }
  const request = serial
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = batch.requiresUncertainAcknowledgement && batch.taskId
      ? await executionPreparationApi.retryFieldBatch(confirmed.id, confirmed.revision, batch.fieldBatchKey,
        batch.attemptNumber + 1, batch.taskId)
      : await executionPreparationApi.retryFieldBatch(confirmed.id, confirmed.revision, batch.fieldBatchKey, batch.attemptNumber + 1)
    if (request !== serial || confirmed.id !== props.preparationId) return
    preparation.value = verify(result); pendingBatchRetry.value = null
    notice.value = '已处理此批次重试请求，执行结果以任务状态为准。'; emit('changed')
  } catch (cause) { if (request === serial) { error.value = apiErrorSummary(cause); needsRefresh.value = true } }
  finally { if (request === serial) busy.value = false }
}
function requestCancellation() {
  if (!preparation.value?.cancellation?.allowed || loading.value || busy.value || needsRefresh.value || text.value || smokeDirty.value || recoveryText.value) return
  pendingCancellation.value = { id: preparation.value.id, revision: preparation.value.revision }
  pendingBatchRetry.value = null; continueAcknowledged.value = false
}
async function cancelPreparation() {
  if (!preparation.value?.cancellation?.allowed || loading.value || busy.value || needsRefresh.value || text.value || smokeDirty.value || recoveryText.value) return
  const confirmed = pendingCancellation.value
  if (!confirmed) return
  if (confirmed.id !== props.preparationId || confirmed.revision !== preparation.value.revision) {
    pendingCancellation.value = null; error.value = '准备已变化，请重新读取后再次确认停止。'; needsRefresh.value = true; return
  }
  const id = props.preparationId, request = serial
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await executionPreparationApi.cancel(id, confirmed.revision)
    if (request !== serial || props.preparationId !== id) return
    preparation.value = verify(result)
    if (result.status !== 'cancelled') throw new Error('服务端未确认准备停止，请重新读取原任务')
    pendingCancellation.value = null
    notice.value = '此准备已停止，不代表远端任务已结束；请在原调用中核对取消结果和费用。'
    emit('changed')
  } catch (cause) { if (request === serial) { error.value = apiErrorSummary(cause); needsRefresh.value = true } }
  finally { if (request === serial) busy.value = false }
}
watch(() => [props.preparationId, props.versionId, props.purpose], () => {
  returnedSerial++; returned.value = null; returnedError.value = ''; returnedLoading.value = false
  recoveryText.value = ''; recoveryAcknowledged.value = false; resourceAcknowledged.value = false
  busy.value = false; primaryRetryDirty.value = false; pendingCancellation.value = null; pendingBatchRetry.value = null
  historySerial++; fieldSerial++; preparation.value = null; history.value = null; historyOpen.value = false; historyPage.value = 1
  field.value = null; fieldError.value = ''; text.value = ''; slotKey.value = ''; smokeDirty.value = false; notice.value = ''; void load()
  reviewSlotKey.value = ''; editing.value = false
}, { immediate: true })
watch([() => props.refreshToken, loading, busy, hasUnsavedDraft], () => {
  if (props.refreshToken === undefined || props.refreshToken === appliedRefreshToken
      || loading.value || busy.value || hasUnsavedDraft.value || needsRefresh.value) return
  void load()
})
</script>

<template>
  <section class="preparation-workbench" :aria-busy="loading || busy">
    <header class="workbench-actions"><h3>{{ preparation?.presentation?.kind === 'auxiliary' ? '提示词辅助任务' : '准备、字段与执行' }}</h3><button type="button" class="button ghost small" :disabled="loading || busy" @click="load">{{ loading ? '正在读取…' : '刷新准备状态' }}</button></header>
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}<span v-if="preparation && needsRefresh"> 下方为上次读取的内容，重新读取成功前不能提交操作。</span></p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="preparation">
      <p><PlatformStatusPill :status="preparation.status" :context-label="fieldWait?.label"/> · 准备修订 {{ preparation.revision }} · 更新于 {{ time(preparation.updatedAt) }}</p>
      <p v-if="fieldWait" role="status">{{ fieldWait.message }}</p>
      <p v-if="preparation.reason">{{ preparation.reason }}</p>
      <section v-if="preparation.outcome && preparation.presentation?.kind !== 'auxiliary'" class="review-summary" aria-label="主调用与产物状态">
        <p v-if="preparation.presentation?.task">主调用：<PlatformStatusPill :status="preparation.presentation.task.status"/> · 第 {{ preparation.presentation.task.attemptNumber }} 次
          <span v-if="preparation.presentation.task.errorCode"> · {{ preparation.presentation.task.errorCode }}</span></p>
        <p v-if="preparation.outcome">{{ preparation.outcome.message }}</p>
      </section>
      <ManualPrimaryRetry v-if="manualRun?.planSource === 'manual' && preparation.presentation?.kind === 'primary'"
        :run="manualRun" :preparation="preparation" :disabled="loading || busy || text !== '' || smokeDirty || recoveryText !== '' || needsRefresh"
        @busy="busy = $event" @dirty="primaryRetryDirty = $event" @changed="primaryRetried"/>
      <section v-if="preparation.presentation?.kind === 'auxiliary'" class="review-summary" aria-label="辅助任务结果与正文入口">
        <h4>{{ preparation.presentation.operation === 'prompt.fields.translate' ? '翻译输入字段' : '生成输入字段' }}</h4>
        <p>此任务负责生成或翻译主调用所需的输入字段，不是主调用本身。输入和规则是任务依据，不是生成正文。</p>
        <p v-if="preparation.presentation.task">实际调用结果：<PlatformStatusPill :status="preparation.presentation.task.status"/> · 第 {{ preparation.presentation.task.attemptNumber }} 次尝试
          <span v-if="preparation.presentation.task.errorCode"> · {{ preparation.presentation.task.errorCode }}</span></p>
        <p v-else>尚未读取到已创建的调用。准备状态不代表调用成功。</p>
        <p>生成的字段修订保存在对应主准备中；这里没有独立的字段采用操作。是否需要人工确认以主准备的冻结审核规则为准，辅助成功不等于已采用。</p>
        <p v-if="preparation.outcome">{{ preparation.outcome.message }}</p>
        <template v-if="preparation.presentation.parent">
          <p>对应主准备：{{ preparation.presentation.parent.label }} · <PlatformStatusPill :status="preparation.presentation.parent.status"/></p>
          <button v-if="allowPreparationNavigation" type="button" class="button primary small" :disabled="loading || busy || hasUnsavedDraft || needsRefresh"
            @click="emit('openParent', preparation.presentation.parent.id)">查看生成正文 / 前往主准备审核</button>
          <p v-else>请在原生产运行的“主准备 · 正文审核”打开此对象。</p>
        </template>
        <p v-else role="status">主准备关联尚未核验，无法跳转。请重新读取或查看原调用记录。</p>
        <details><summary>查看辅助任务内部标识</summary><p>自身准备：{{ preparation.id }}</p><p>接收字段的主准备：{{ preparation.presentation.parent?.id ?? '未核验' }}</p><p>实际调用：{{ preparation.presentation.task?.id ?? '未创建' }}</p></details>
      </section>
      <template v-if="preparation.presentation?.kind === 'auxiliary'">
        <ExecutionReturnedText v-if="preparation.textResponseReview" :response="preparation.textResponseReview"/>
        <p v-else-if="preparation.presentation.task?.errorCode === 'execution_response_invalid'" class="platform-alert" role="alert">{{ preparation.responseDiagnostic?.summary || '这次历史调用未留存实际返回正文，无法恢复，不是模型返回了空内容。请在主准备人工补填，或明确重试此批次。' }}</p>
      </template>
      <section v-if="preparation.cancellation" class="workbench-fields" aria-label="停止此执行准备">
        <p>{{ preparation.cancellation.reason }}</p>
        <p v-if="preparation.cancellation.cancelledAt">停止于 {{ time(preparation.cancellation.cancelledAt) }} · 操作人 {{ preparation.cancellation.cancelledBy || '系统动作' }}</p>
        <button v-if="preparation.cancellation.allowed && !pendingCancellation" type="button" class="button danger small"
          :disabled="loading || busy || needsRefresh || !!text || smokeDirty || !!recoveryText" @click="requestCancellation">停止此准备与辅助任务</button>
        <section v-if="pendingCancellation" class="platform-alert danger" aria-label="确认停止当前准备">
          <p>准备 {{ pendingCancellation.id }} · 修订 {{ pendingCancellation.revision }}</p>
          <p>停止此主准备及它已创建的辅助准备。其他候选不受影响。未提交的调用会停止并释放预留；已提交的调用仍可能计费，原任务负责取消或忽略晚到结果。不会删除已保存的字段，也不会自动重做。</p>
          <div class="workbench-actions"><button type="button" class="button ghost small" :disabled="busy" @click="pendingCancellation = null">返回，不停止</button>
            <button type="button" class="button danger small" :disabled="loading || busy || needsRefresh || !!text || smokeDirty || !!recoveryText" @click="cancelPreparation">{{ busy ? '正在停止…' : '确认停止此准备' }}</button></div>
        </section>
        <p v-if="preparation.cancellation.allowed && (text || smokeDirty || recoveryText)">请先保存或明确清空未提交的编辑，再停止准备。</p>
      </section>
      <section v-if="preparation.responseDiagnostic && !auxiliaryResponseVisible" class="platform-alert danger" aria-label="本次文本响应诊断">
        <strong>本次文本响应 · {{ preparation.responseDiagnostic.state === 'recorded' ? '未通过校验' : '诊断不可核验' }}</strong>
        <p>{{ preparation.responseDiagnostic.summary }}</p>
        <p v-if="purpose === 'production'">以上诊断对应原请求。现在文本的新请求输出额度统一在“系统设置 → 默认值与限制 → execution.resources.review_policy”调整，不再修改实例的历史参数。调整不会改变这次已提交请求；需重新生成时，请明确创建新候选。</p>
        <details v-if="preparation.responseDiagnostic.taskId"><summary>查看诊断记录</summary>
          <p>原调用任务：{{ preparation.responseDiagnostic.taskId }} · 记录时间：{{ time(preparation.responseDiagnostic.observedAt) }}</p>
          <RouterLink v-if="diagnosticTarget && !loading && !busy && !hasUnsavedDraft && !needsRefresh" :to="diagnosticTarget">查看这次调用的请求、返回与产物证据</RouterLink>
          <p v-else-if="diagnosticTarget" role="status">请先完成当前编辑或重新读取准备，再查看这次调用。</p>
          <p v-else role="status">诊断任务关联未核验，无法跳转；不会打开其他调用代替。</p>
          <ul><li v-for="item in preparation.responseDiagnostic.details" :key="item.label">{{ item.label }}：{{ item.value }}</li></ul>
          <p>仅展示已记录的响应分类和用量，不代表扣费金额，也不会自动重试。</p>
        </details>
      </section>
      <p v-if="preparation.requestFrozenAt">本次请求已冻结于 {{ time(preparation.requestFrozenAt) }}，字段只读。已提交任务不能通过“继续”重新生成。</p>
      <ExecutionPreparationFacts :key="preparation.id" :resolved-inputs="preparation.resolvedInputs"/>
      <ExecutionConfigurationReplacement v-if="preparation.stageRun && preparation.presentation?.kind === 'primary' && preparation.requestFrozenAt === null"
        :key="`configuration:${preparation.id}`" :preparation-id="preparation.id" :revision="preparation.revision"
        :disabled="loading || busy || text !== '' || smokeDirty || recoveryText !== '' || primaryRetryDirty || needsRefresh"
        @busy="busy = $event" @dirty="configurationReplacementDirty = $event" @applied="primaryRetried"/>
      <MediaGenerationTargetSummary :key="`target:${preparation.id}`" :resolved-inputs="preparation.resolvedInputs" :version-id="preparation.executionInstanceVersion"/>
      <ExecutionPromptDisplay v-if="preparation.presentation?.kind !== 'auxiliary'" :preparation-id="preparation.id" :revision="preparation.revision"
        :frozen="preparation.requestFrozenAt !== null" :disabled="loading || busy || hasUnsavedDraft || needsRefresh"
        :field-labels="Object.fromEntries(preparation.fieldSlots.map(slot => [slot.fieldKey, slot.fieldLabel || slot.fieldKey]))"/>
      <ExecutionStorageMetrics :preparation-id="preparation.id" :revision="preparation.revision"
        :disabled="loading || busy || hasUnsavedDraft || needsRefresh"/>
      <section v-if="preparation.presentation?.kind !== 'auxiliary'" class="review-summary" aria-label="本次字段审核方式">
        <h4>{{ preparation.reviewSummary.label }}</h4><p>{{ preparation.reviewSummary.description }}</p>
        <ReadonlyDataValue :key="preparation.id" :value="preparation.reviewContract" label="查看完整审核规则（技术信息）"/>
      </section>
      <div v-if="preparation.fieldSlots.length" class="workbench-fields">
        <button v-if="preparation.stageRun && preparation.unitKey.startsWith('shot:')" type="button" class="button primary small"
          :disabled="!editable || hasUnsavedDraft" @click="bulkReviewOpen = true">本镜字段一键确认并采用</button>
        <h4>审核提示词组成字段</h4>
        <p>先阅读已保存正文，再明确确认并采用。显示修订不等于采用；新修订不会自动替换已采用内容。</p>
        <nav class="field-tabs" aria-label="选择需要审核的输入字段">
          <button v-for="slot in preparation.fieldSlots" :key="key(slot.unitKey, slot.fieldKey)" type="button" class="button ghost"
            :aria-pressed="reviewSlotKey === key(slot.unitKey, slot.fieldKey)" :disabled="loading || busy || hasUnsavedDraft"
            @click="openSlot(slot)"><strong>{{ slot.fieldLabel || slot.fieldKey }}</strong><small>{{ selectionFor(slot) ? '已采用' : slot.latestValueId ? '待选择修订' : '尚无正文' }}</small></button>
        </nav>
        <p v-if="fieldLoading" role="status">正在读取所选修订正文…</p>
        <p v-if="fieldError" class="platform-alert danger" role="alert">{{ fieldError }}<button type="button" class="button ghost small" :disabled="loading || busy || hasUnsavedDraft" @click="reviewSlot && openSlot(reviewSlot)">重新读取正文</button></p>
        <section v-if="reviewSlot" class="field-review" aria-label="输入字段正文审核">
          <header class="workbench-actions"><h4>{{ reviewSlot.fieldLabel || reviewSlot.fieldKey }}</h4><span>策略目标语言（生成指导）：{{ languageLabel(reviewSlot.effectiveLanguage) }}</span></header>
          <p v-if="reviewSlot.fieldContract.generation_output_semantics === 'contribution'">这是当前模型提示词的组成部分，不是新的剧本。确认后与已冻结的原文、参考关系等内容组装，不覆盖剧本。</p>
          <template v-if="field && !fieldLoading">
            <p><PlatformStatusPill :status="field.status"/> · 修订 {{ field.revision }} · {{ field.originKind === 'model' ? '模型生成' : field.originKind === 'manual' ? '人工填写' : '服务器派生' }} · {{ localField ? '本准备的修订' : '从其他准备复用的只读修订' }}</p>
            <p>此字段正文 {{ Array.from(field.text).length.toLocaleString() }} 字符；不含系统组装的其他段落，完整通道长度请看上方“完整提示词与系统组装”。</p>
            <pre class="field-body">{{ field.text }}</pre>
            <ExecutionQualityNotices :messages="field.warnings.map(warning => warning.message)" />
            <p v-if="currentSelection?.fieldValue === field.id" role="status">此修订已明确选入本次请求。</p>
            <p v-else>当前仅查看修订 {{ field.revision }}，尚未采用这份正文。</p>
            <template v-if="localField && preparation.requestFrozenAt === null && preparation.status !== 'cancelled'">
              <label v-if="replacing" class="workbench-check"><input v-model="replaceAcknowledged" type="checkbox" :disabled="!editable || hasUnsavedDraft"/>确认用当前查看的修订替换本次已采用的另一份修订</label>
              <div class="workbench-actions">
                <button type="button" class="button primary small" :disabled="!editable || hasUnsavedDraft || !['pending','confirmed'].includes(field.status) || currentSelection?.fieldValue === field.id || (!!replacing && !replaceAcknowledged)" @click="review('select')">确认并采用此修订</button>
                <button type="button" class="button ghost small" :disabled="!editable || hasUnsavedDraft || field.status !== 'pending'" @click="review('confirm')">仅确认，不采用</button>
                <button type="button" class="button danger small" :disabled="!editable || hasUnsavedDraft || field.status !== 'pending'" @click="review('reject')">拒绝此修订</button>
              </div>
            </template>
          </template>
          <p v-else-if="!fieldLoading && !fieldError">此字段尚无已保存的可采用修订，不代表模型未返回内容。请查看辅助调用实际返回，或明确人工填写；查看不会生成内容。</p>
          <button v-if="reviewBatch?.preparationId && preparation.stageRun" type="button" class="button ghost small" :disabled="busy || returnedLoading || hasUnsavedDraft || needsRefresh" @click="loadReturnedText">{{ returnedLoading ? '正在读取实际返回…' : '查看辅助调用实际返回（含未通过校验的内容）' }}</button>
          <p v-if="returnedError" class="platform-alert danger" role="alert">{{ returnedError }}</p>
          <ExecutionReturnedText v-if="returned?.textResponseReview" :response="returned.textResponseReview"/>
          <p v-else-if="returned" class="platform-alert" role="status">{{ returned.responseDiagnostic?.summary || '此调用没有留存完整返回原文。已保存的字段修订仍可查看；未保存的原文无法恢复，不会重新生成或伪造内容。' }}</p>
          <button v-if="!editing && preparation.requestFrozenAt === null && preparation.status !== 'cancelled'" type="button" class="button ghost small" :disabled="!editable || hasUnsavedDraft || fieldLoading || !!fieldError" @click="editField">{{ field ? '修改这份正文（另存修订）' : '人工填写此字段' }}</button>
          <form v-if="editing && chosenSlot" @submit.prevent="createField">
            <h4>人工修改 · {{ chosenSlot.fieldLabel || chosenSlot.fieldKey }}</h4>
            <p>{{ chosenSlot.required ? '必须有内容' : '按合同允许留空' }} · 最多 {{ chosenSlot.maxChars }} 字符 · 策略目标语言（生成指导）：{{ languageLabel(chosenSlot.effectiveLanguage) }} · 将另存为修订 {{ chosenSlot.nextRevision }}</p>
            <label>修改后的正文<textarea v-model="text" rows="8" :disabled="!editable" :required="chosenSlot.required"/></label>
            <p>当前 {{ Array.from(text).length }} 字符。保存不会自动确认、采用或调用模型；原修订仍保留。</p>
            <div class="workbench-actions"><button type="submit" class="button primary small" :disabled="!editable">保存待确认修订</button><button type="button" class="button ghost small" :disabled="busy" @click="discardDraft(); if (!text) editing = false">取消修改</button></div>
          </form>
          <details :key="`rule:${reviewSlotKey}`"><summary>生成规则与来源标识（技术信息，不是生成正文）</summary>
            <p>单元：{{ reviewSlot.unitKey }} · 字段：{{ reviewSlot.fieldKey }} · 语言策略：{{ reviewSlot.fieldContract.language_policy }} · 来源语言：{{ reviewSlot.sourceLanguage }} · 项目传入目标语言：{{ reviewSlot.targetLanguage }}</p>
            <p>{{ reviewSlot.instruction }}</p>
            <ReadonlyDataValue :value="reviewSlot.fieldContract" label="查看完整字段规则（技术信息）"/>
            <template v-if="field"><p>批准情况：{{ field.approvalLabel }} · 记录更新于 {{ time(field.updatedAt) }}</p><p v-if="field.reviewedAt">审核于 {{ time(field.reviewedAt) }}</p><p v-if="field.approvalPolicySha256">批准策略摘要：{{ field.approvalPolicySha256 }}</p>
              <button type="button" class="button ghost small" :disabled="busy || fieldLoading || hasUnsavedDraft" @click="openField(field.id)">重新读取此修订</button></template>
          </details>
        </section>
        <button type="button" class="button ghost small" :disabled="busy || historyLoading || hasUnsavedDraft" @click="historyOpen ? historyOpen = false : loadHistory(1)">{{ historyLoading ? '正在读取修订…' : historyOpen ? '收起全部字段修订' : '查看全部字段修订' }}</button>
      </div>
      <p v-else-if="preparation.presentation?.kind !== 'auxiliary'">本次无需额外填写或复核策略字段；已有输入与实际执行状态见本页。</p>
      <ReadonlyDataValue v-else :value="preparation.reviewContract" label="辅助准备的冻结审核配置（技术信息，不代表字段已采用）"/>
      <ExecutionFieldBulkReview v-if="bulkReviewOpen" :preparation-ids="[preparation.id]" @busy="busy = $event"
        @close="bulkReviewOpen = false; busy = false" @confirmed="bulkReviewed"/>
      <section v-if="preparation.auxiliaryBatches.length" class="workbench-fields">
        <h4>字段辅助任务</h4>
        <p>辅助模型只生成或翻译下列字段，结果写回本次准备。是否自动采用取决于本次冻结的审核规则；配方自动重试按冻结配置执行，远端未决不会自动重发。</p>
        <div v-for="batch in preparation.auxiliaryBatches" :key="batch.fieldBatchKey">
          <div class="workbench-actions"><strong>{{ batch.purpose === 'translate_fields' ? '翻译字段' : '生成字段' }} · {{ batch.fieldKeys.join('、') }}</strong><PlatformStatusPill :status="batch.status"/><span>已尝试 {{ batch.attemptNumber }} 次</span>
            <button v-if="batch.canRetry" type="button" class="button ghost small" :disabled="!canRetryBatch || !!pendingBatchRetry" @click="requestBatchRetry(batch)">{{ batch.requiresUncertainAcknowledgement ? '保留原记录并新增尝试' : '重试此批次' }}</button>
          </div>
          <p v-if="batch.reason">{{ batch.reason }}</p>
          <p v-if="batch.status === 'awaiting_review'">请在“任务与记录”打开下方关联的执行准备核对提醒。输出不完整时，可在当前主准备人工填写并确认对应字段；不会重跑已有调用。</p>
          <details v-if="batch.preparationId"><summary>查看任务关联</summary><p>自身执行准备：{{ batch.preparationId }}</p><p>接收字段的主准备：{{ preparation.id }}</p><p>实际调用任务：{{ batch.taskId ?? '尚未派发' }}</p></details>
        </div>
        <p v-if="hasUnsavedDraft">请先保存或明确清空未提交的字段、补全正文或Smoke输入，再重试辅助批次。</p>
        <section v-if="pendingBatchRetry" class="platform-alert" aria-label="确认重试辅助批次">
          <p>主准备 {{ pendingBatchRetry.id }} · 修订 {{ pendingBatchRetry.revision }}</p>
          <p>{{ pendingBatchRetry.batch.purpose === 'translate_fields' ? '翻译字段' : '生成字段' }}：{{ pendingBatchRetry.batch.fieldKeys.join('、') }} · 第 {{ pendingBatchRetry.batch.attemptNumber + 1 }} 次尝试</p>
          <p>批次 {{ pendingBatchRetry.batch.fieldBatchKey }} · 原调用 {{ pendingBatchRetry.batch.taskId }} · 原辅助准备 {{ pendingBatchRetry.batch.preparationId }}</p>
          <p v-if="pendingBatchRetry.batch.requiresUncertainAcknowledgement" role="alert">原调用的远端结果或实际费用仍未知，可能已经计费。此操作保留原状态、记录及费用预留，明确授权新增一次同批次关联的自动辅助调用，可能重复计费；不将原调用改成成功或零费用，不人工补字段。原调用若迟到返回，不会覆盖新尝试的字段。</p>
          <p>仅重试上述批次，沿用主准备及其冻结意图；不会重跑其他批次或已成功的主调用。将按原任务授权核对当前资源策略，可能产生费用，不覆盖已采用字段。若提交结果不明，请刷新原准备核对，不要重复提交。</p>
          <div class="workbench-actions"><button type="button" class="button ghost small" :disabled="busy" @click="pendingBatchRetry = null">返回，不重试</button>
            <button type="button" class="button primary small" :disabled="!canRetryBatch" @click="retryBatch">{{ busy ? '正在提交…' : pendingBatchRetry.batch.requiresUncertainAcknowledgement ? '确认风险并新增尝试' : '确认重试此批次' }}</button></div>
        </section>
      </section>
      <div v-if="historyOpen">
        <p v-if="historyError" class="platform-alert danger" role="alert">{{ historyError }}</p>
        <template v-if="history"><p>字段修订共 {{ history.count }} 条 · 第 {{ historyPage }} 页（新修订不会自动替换已采用内容）</p>
          <ul><li v-for="row in history.results" :key="row.id"><button type="button" class="button ghost small" :disabled="busy || fieldLoading || hasUnsavedDraft" @click="editing = false; openField(row.id)">{{ fieldLabel(row.unitKey, row.fieldKey) }} · 修订 {{ row.revision }}</button><PlatformStatusPill :status="row.status"/></li></ul>
          <nav class="workbench-actions" aria-label="字段修订分页"><button type="button" class="button ghost small" :disabled="busy || historyLoading || !history.previous" @click="loadHistory(historyPage - 1)">上一页</button><button type="button" class="button ghost small" :disabled="busy || historyLoading || !history.next" @click="loadHistory(historyPage + 1)">下一页</button></nav>
        </template>
      </div>
      <section v-if="preparation.mediaUncertainRetry" class="workbench-fields" aria-label="媒体主调用未决处理">
        <h3>保留未决记录，新增一次自动尝试</h3>
        <p>原请求可能已执行或计费，不能确认失败。新尝试复用本次冻结的提示词、图片和参数；旧记录及未知费用保留，迟到结果不会覆盖新结果。已成功素材不会重跑。</p>
        <p v-if="preparation.mediaUncertainRetry.reason" role="alert">{{preparation.mediaUncertainRetry.reason}}</p>
        <label><input v-model="uncertainMediaAcknowledged" type="checkbox" :disabled="busy || !preparation.mediaUncertainRetry.canRetry" />我理解可能重复执行或计费，明确授权新增一次关联尝试</label>
        <button type="button" class="button" :disabled="busy || !preparation.mediaUncertainRetry.canRetry || !uncertainMediaAcknowledged" @click="retryUncertainMedia">确认新增关联尝试</button>
      </section>
      <section v-if="preparation.mediaArchiveRecovery" class="workbench-fields" aria-label="原媒体回执归档恢复">
        <h3>远端已生成 · 本地归档未完成</h3>
        <p>仅重新验收已保存回执对应的原文件，不改输入、不新建模型调用。取消、身份不一致、回执缺失或文件校验失败时不能恢复；原失败状态与回执指纹会保留在运行记录中。</p>
        <p v-if="preparation.mediaArchiveRecovery.reason" role="alert">{{preparation.mediaArchiveRecovery.reason}}</p>
        <button type="button" class="button" :disabled="busy || !preparation.mediaArchiveRecovery.canRecover" @click="recoverMediaArchive">仅归档原回执（不重新生成）</button>
      </section>
      <section v-if="preparation.textResponseReview && preparation.presentation?.kind !== 'auxiliary'" class="workbench-fields" aria-label="模型返回与处理记录">
        <h3>模型返回与处理记录</h3>
        <template v-if="preparation.textResponseReview.confirmedAt">
          <p>已于 {{ time(preparation.textResponseReview.confirmedAt) }} 保存人工处理结果，原响应仍保留；不代表已采用候选。</p>
          <p>修订来源：人工 · 确认人 {{ preparation.textResponseReview.confirmedBy }}</p>
          <details><summary>查看已保存的人工修订</summary><ReadonlyDataValue :value="preparation.textResponseReview.correctedText"/></details>
          <details><summary>原始模型响应的历史校验记录</summary><ReadonlyDataValue :value="preparation.textResponseReview.diagnostics"/></details>
        </template>
        <p v-else-if="preparation.textResponseReview.diagnostics.contract_error_code === 'response_contract_invalid'" role="alert">{{ preparation.textResponseReview.diagnostics.summary }}<br/>校验原因：{{ preparation.textResponseReview.diagnostics.validation_code }}</p>
        <p v-else-if="preparation.textResponseReview.diagnostics.contract_error_code === 'request_text_capacity_rejected'">服务商拒绝了本次输出参数或上下文容量，没有完整正文。原输入仍保留；请在系统设置调整输出额度，或缩减、拆分输入，再在当前制作步骤明确新建候选。不会自动切换模型或付费重试。</p>
        <p v-else-if="preparation.textResponseReview.editable">已保留服务商返回的正文，不会自动重试。可按原请求格式补全，校验后另存人工修订；重新生成是另一次模型调用。</p>
        <p v-else>{{ preparation.textResponseReview.diagnostics.summary }}</p>
        <ExecutionResponseDiagnostics v-if="!preparation.textResponseReview.confirmedAt" :diagnostics="preparation.textResponseReview.diagnostics" />
        <ul v-if="preparation.textResponseReview.notices.length"><li v-for="(item, index) in preparation.textResponseReview.notices" :key="index">{{ item.message }}</li></ul>
        <details><summary>查看保留的原始正文</summary><ReadonlyDataValue :value="preparation.textResponseReview.originalText"/></details>
        <p v-if="!preparation.textResponseReview.editable && !preparation.textResponseReview.confirmedAt">{{ preparation.textResponseReview.readonlyReason || '此响应只读。字段辅助任务请返回接收字段的主准备，人工填写并确认对应字段；人工内容会单独记录来源，不作为模型原输出。' }}</p>
        <template v-else-if="preparation.textResponseReview.editable">
          <p v-if="preparation.textResponseReview.assetJobId">人工补正不调用模型；保存为本素材任务的文本结果或剧本素材，不自动采用或继续其他任务。</p>
          <button type="button" class="button" :disabled="busy || !!recoveryText" @click="recoveryText = preparation.textResponseReview.originalText">将原正文复制到编辑区</button>
          <label class="platform-field"><span>修正或补全后的完整响应（按原请求格式）</span><textarea v-model="recoveryText" rows="10" :disabled="busy" spellcheck="false"/></label>
          <label class="workbench-check"><input v-model="recoveryAcknowledged" type="checkbox" :disabled="busy"/>我已核对补全内容，另存人工修订，不直接采用。</label>
          <button type="button" class="button primary" :disabled="busy || !recoveryAcknowledged || !recoveryText.trim() || needsRefresh" @click="completeResponse">{{ preparation.textResponseReview.assetJobId ? '校验并保存人工修订（不调用模型）' : '校验并保存候选（不调用模型）' }}</button>
        </template>
      </section>
      <section v-if="preparation.resourceReview && resourcePending" class="workbench-fields" aria-label="资源用量待确认">
        <h3>资源用量待确认</h3>
        <p>输入和已有结果已保留。系统提醒值不是任务失败，也不会自动增加调用。</p>
        <ul><li v-for="issue in preparation.resourceReview.issues" :key="issue.code">{{ issue.message }}</li></ul>
        <p v-if="!preparation.resourceReview.policyCurrent">系统策略已更新，请先“{{ manualFailedPrimary ? '重试此失败主调用' : '检查并继续' }}”重新评估。</p>
        <p v-else-if="preparation.resourceReview.confirmed">已确认此请求的用量；点击“{{ manualFailedPrimary ? '重试此失败主调用' : '检查并继续' }}”才会重新核对并恢复原任务。</p>
        <template v-else>
          <label class="workbench-check"><input v-model="resourceAcknowledged" type="checkbox" :disabled="busy || loading || needsRefresh"/>我已核对以上用量及可能产生的费用，同意本次请求超过系统提醒值。</label>
          <button type="button" class="button" :disabled="!resourceAcknowledged || busy || loading || needsRefresh || hasUnsavedDraft" @click="confirmResources">确认本次用量（不执行模型）</button>
        </template>
      </section>
      <section v-if="!manualFailedPrimary && (preparation.requestFrozenAt === null || resourcePending) && preparation.status !== 'cancelled'" class="workbench-fields">
        <p v-if="purpose === 'production'">配方运行若显示“等待输入”，请先完成这里的字段或用量确认，再回到工作台点击“继续 / 检查阶段”，沿用原准备恢复；无配方人工制作在下方继续。</p>
        <label class="workbench-check"><input v-model="continueAcknowledged" type="checkbox" :disabled="!canAdvance"/>我已检查本次输入及采用字段。继续将核对当前系统资源策略及原任务外部调用授权，可能提交模型调用。</label>
        <button type="button" class="button primary" :disabled="!canAdvance || !continueAcknowledged" @click="advance">检查并继续此任务</button>
        <p v-if="hasUnsavedDraft">请先保存或明确清空未提交的字段、补全正文或Smoke输入，避免继续时遗漏输入。</p>
      </section>
      <section v-if="preparation.capacitySample">
        <h4>本次规格观测 · {{ preparation.capacitySample.dimensionsComplete ? '完整规格样本' : '维度尚不完整' }}</h4>
        <p>{{ time(preparation.capacitySample.observedAt) }} · 主调用用时 {{ preparation.capacitySample.elapsedMs }} 毫秒（含远端排队、轮询和结果收集，不是 GPU 计算耗时）</p>
        <p v-if="!preparation.capacitySample.dimensionsComplete">仍有 {{ preparation.capacitySample.missingDimensions.length }} 项维度缺少可靠观测。成功结果不代表容量范围已验证。</p>
        <ReadonlyDataValue :value="preparation.capacitySample.knownDimensions" label="查看已知规格（只读）"/><ul><li v-for="item in preparation.capacitySample.missingDimensions" :key="item">{{ item }}</li></ul>
      </section>
      <ExecutionSmokeReuseForm v-if="allowSmoke && versionId !== null" :key="preparation.id" :preparation="preparation" :disabled="busy || loading || needsRefresh || !!text || !!recoveryText || !!pendingBatchRetry || !!pendingCancellation" @busy="busy = $event" @dirty="smokeDirty = $event" @created="emit('smokeCreated', $event)"/>
      <ReadonlyDataValue v-if="preparation.evaluation" :key="`evaluation:${preparation.id}`" :value="preparation.evaluation" label="准入检查、警告与阻断原因（技术信息）"/>
      <ReadonlyDataValue v-if="preparation.preparedRequest" :key="`request:${preparation.id}`" :value="preparation.preparedRequest" label="最终冻结请求与采用来源（技术信息）"/>
    </template>
  </section>
</template>

<style scoped>
.preparation-workbench{display:grid;gap:12px;min-width:0}.workbench-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.workbench-fields{border-top:1px solid var(--border);padding-top:12px;display:grid;gap:10px}.preparation-workbench h3,.preparation-workbench h4,.preparation-workbench p{margin:0}.preparation-workbench p,.preparation-workbench li{font-size:12px;line-height:1.7;overflow-wrap:anywhere}.preparation-workbench form,.preparation-workbench label{display:grid;gap:8px}.preparation-workbench .workbench-check{display:flex;align-items:flex-start;gap:8px;font-size:12px;line-height:1.7}.workbench-check input{width:auto;flex:none;margin-top:5px}.preparation-workbench ul{margin:0;padding-left:20px}.preparation-workbench li{margin:5px 0}.preparation-workbench pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.7;border:1px solid var(--border);padding:12px;max-height:380px;overflow:auto}.preparation-workbench summary{cursor:pointer;line-height:1.8}.preparation-workbench textarea{resize:vertical;width:100%;min-width:0}.preparation-workbench select{width:100%;min-width:0}
.review-summary{display:grid;gap:6px;padding:12px 14px;border:1px solid var(--border);border-radius:8px;min-width:0}.review-summary p{color:var(--text-secondary)}.workbench-fields>.button{justify-self:start;width:auto;white-space:normal}.workbench-actions .button{width:auto}.preparation-workbench .button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.field-tabs{display:flex;flex-wrap:wrap;gap:8px}.field-tabs .button{display:grid;gap:4px;text-align:left;min-height:44px;white-space:normal;overflow-wrap:anywhere;max-width:100%}.field-tabs small{font-weight:400;color:var(--text-secondary)}.field-tabs [aria-pressed=true]{border-color:var(--accent);background:var(--bg-elevated)}.field-review{display:grid;gap:12px;min-width:0}.field-review>.button{justify-self:start}.field-review .field-body{font-size:14px;line-height:1.8;max-height:440px;margin:0;background:var(--bg-elevated)}.field-review details{border-top:1px solid var(--border);padding-top:10px}.field-review form{border:1px solid var(--border);padding:12px;border-radius:6px}@media(max-width:640px){.field-tabs .button{flex:1 1 100%}.field-review .workbench-actions{align-items:stretch}.field-review .button{min-height:40px;white-space:normal}}
</style>
