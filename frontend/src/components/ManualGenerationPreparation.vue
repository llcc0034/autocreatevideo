<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { executionRegistryApi } from '@/services/executionRegistryApi'
import { platformApi } from '@/services/platformApi'
import { isDefinitiveCreationRejection } from '@/services/productionOnboarding'
import { buildManualGeneration, manualGenerationTarget, manualScopeLabels as scopeLabels, validateManualGenerationContract, type ManualActionOptions, type ManualScopeKind, type ManualTarget } from '@/services/productionManual'
import { emptyExecutionRequestDraft, executionRequestFormContract, prepareExecutionSelectedSources, type ExecutionRequestDraft, type ExecutionRequestFormContract, type ExecutionSelectedMediaSource, type ExecutionReviewedShotSource } from '@/services/executionRequestForm'
import { readManualStoryboardReferences, manualGenerationShotSources } from '@/services/productionManualStoryboardReview'
import { readManualMediaReview } from '@/services/productionManualMedia'
import type { ExecutionVersion } from '@/services/executionRegistry'
import type { JsonObject, ProductionRun } from '@/services/platformTypes'
import { stageLabel } from '@/services/presentationLabels'
import ExecutionRequestInputEditor from './ExecutionRequestInputEditor.vue'
import ExecutionPreparationWorkbench from './ExecutionPreparationWorkbench.vue'

const props = defineProps<{ run: ProductionRun; options: ManualActionOptions; initialStageKey?: string }>()
const emit = defineEmits<{ busy: [value: boolean]; dirty: [value: boolean]; changed: [] }>()
const stages = computed(() => props.options.actions.filter(row => row.action === 'generate'))
const stageKey = ref(''), versionId = ref(''), page = ref(1), versions = ref<ExecutionVersion[]>([]), hasNext = ref(false)
const contract = ref<ExecutionRequestFormContract | null>(null), draft = ref<ExecutionRequestDraft | null>(null)
const loading = ref(false), inputBusy = ref(false), workbenchBusy = ref(false), inputErrors = ref<string[]>([]), error = ref('')
const pending = ref<JsonObject | null>(null), preparationId = ref(''), rejected = ref(false)
let requestKey = crypto.randomUUID()
const loadedStage = ref('')
const selectedSources = ref<ExecutionSelectedMediaSource[]>([]), sourceNotice = ref('尚未读取本轮已审核素材。也可直接从素材库选择或上传。')
const reviewedShotSources = ref<ExecutionReviewedShotSource[]>([]), shotSourceNotice = ref('尚未读取本镜已审核用途。读取不会确认或采用任何业务结果。'), shotSourceLoading = ref(false)
let shotSourceSequence = 0
const selectedAction = computed(() => stages.value.find(row => row.stageKey === stageKey.value))
const targetKind = ref<ManualScopeKind | ''>(''), targetKey = ref(''), targets = ref<ManualTarget[]>([])
const targetPage = ref(1), targetCount = ref(0), targetHasNext = ref(false), loadedTargetKind = ref<ManualScopeKind | ''>(''), loadedTargetKey = ref('')
const selectedTarget = computed(() => targets.value.find(row => row.unitKey === targetKey.value))
const uniqueTarget = computed(() => targetCount.value === 1 && targets.value.length === 1 && !targetHasNext.value && targetPage.value === 1)
const targetLabel = computed(() => targetKind.value ? `选择${scopeLabels[targetKind.value]}` : '本次制作对象')
const busy = computed(() => loading.value || inputBusy.value || workbenchBusy.value || shotSourceLoading.value)
const locked = computed(() => busy.value || Boolean(pending.value))
let serial = 0
let disposed = false
watch(busy, value => emit('busy', value))
watch(draft, () => { if (draft.value) emit('dirty', true) }, { deep: true })
onBeforeUnmount(() => { disposed = true; serial++; shotSourceSequence++; emit('busy', false) })
watch(() => [props.run.id, props.run.executionPlan?.sha256, stageKey.value, selectedTarget.value?.unitKey], () => {
  shotSourceSequence++; reviewedShotSources.value = []; shotSourceLoading.value = false
  shotSourceNotice.value = '尚未读取本镜已审核用途。读取不会确认或采用任何业务结果。'
})
function requestedSelection(event: Event | undefined, current: string) {
  if (!event) return current
  const select = event.target as HTMLSelectElement
  const requested = select.value
  // A native select changes before its change handler runs. Keep both DOM and
  // reactive context on the confirmed choice until the discard is accepted.
  select.value = current
  return requested
}
async function loadVersions(targetPage = 1, requestedStage = stageKey.value) {
  if (locked.value) return
  if (draft.value && !window.confirm('切换步骤或实例列表会清空本次尚未提交的输入，确定继续？')) { stageKey.value = loadedStage.value; return }
  stageKey.value = requestedStage
  if (loadedStage.value !== stageKey.value) {
    targetKind.value = ''; targetKey.value = ''; targets.value = []; targetCount.value = 0
    loadedTargetKind.value = ''; loadedTargetKey.value = ''
  }
  loadedStage.value = stageKey.value
  const action = stages.value.find(row => row.stageKey === stageKey.value)
  contract.value = null; draft.value = null; versionId.value = ''; versions.value = []; inputErrors.value = []; error.value = ''; rejected.value = false
  if (!action) return
  const token = ++serial; loading.value = true; page.value = targetPage; hasNext.value = false
  try {
    const result = await executionRegistryApi.versions(targetPage, { status: 'published', ...(action.operation ? { operation: action.operation } : {}) })
    if (token !== serial) return
    versions.value = action.operation ? result.results.filter(row => row.workflow_version_id === null) : result.results
    hasNext.value = result.next !== null
  } catch (e) { if (token === serial) error.value = apiErrorSummary(e) }
  finally { if (token === serial) loading.value = false }
}
async function changeStage(event?: Event) {
  const requestedStage = requestedSelection(event, stageKey.value)
  await loadVersions(1, requestedStage)
  if (disposed || requestedStage !== stageKey.value || loading.value) return
  if (!error.value && selectedAction.value?.scopeKinds.length === 1 && !targetKind.value) {
    targetKind.value = selectedAction.value.scopeKinds[0]!
    await loadTargets()
  }
}
async function loadTargets(nextPage = 1, event?: Event) {
  const kind = requestedSelection(event, targetKind.value) as ManualScopeKind | ''
  if (locked.value) return
  if (draft.value && !window.confirm('切换制作对象目录会清空尚未提交的输入，确定继续？')) { targetKind.value = loadedTargetKind.value; return }
  targetKind.value = kind
  contract.value = null; draft.value = null; versionId.value = ''; inputErrors.value = []; error.value = ''; rejected.value = false
  targetKey.value = ''; loadedTargetKey.value = ''; targets.value = []; targetCount.value = 0; targetHasNext.value = false
  loadedTargetKind.value = kind; targetPage.value = nextPage
  if (!kind) return
  const token = ++serial; loading.value = true
  try {
    const result = await platformApi.listProductionTargets(props.run.id, kind, nextPage)
    if (token !== serial) return
    if (result.runId !== props.run.id || result.runPlanSha256 !== props.options.runPlanSha256
      || result.results.some(row => row.scope.kind !== kind || (kind === 'project' && row.scope.id !== props.run.project))) throw new Error('制作对象目录与本轮计划不一致，请刷新工作台')
    targets.value = result.results; targetCount.value = result.count; targetHasNext.value = result.next !== null
    if (uniqueTarget.value) {
      targetKey.value = result.results[0]!.unitKey
      loadedTargetKey.value = targetKey.value
    }
  } catch (e) { if (token === serial) error.value = apiErrorSummary(e) }
  finally { if (token === serial) loading.value = false }
}
function selectTarget(event?: Event) {
  const requested = requestedSelection(event, targetKey.value)
  if (locked.value) return
  if (draft.value && !window.confirm('切换制作对象会清空尚未提交的输入，确定继续？')) { targetKey.value = loadedTargetKey.value; return }
  targetKey.value = requested
  loadedTargetKey.value = targetKey.value
  contract.value = null; draft.value = null; versionId.value = ''; inputErrors.value = []; error.value = ''; rejected.value = false
}
async function loadContract(event?: Event) {
  const requested = requestedSelection(event, versionId.value)
  if (locked.value || !selectedTarget.value) return
  if (draft.value && !window.confirm('切换实例会清空本次尚未提交的输入，确定继续？')) { versionId.value = contract.value!.versionId; return }
  versionId.value = requested
  const id = versionId.value; contract.value = null; draft.value = null; error.value = ''; inputErrors.value = []; rejected.value = false
  if (!id) return
  const token = ++serial; loading.value = true
  try {
    const source = await executionRegistryApi.contract(id), form = executionRequestFormContract(source)
    if (token !== serial) return
    if (form.versionId !== id) throw new Error('实例合同与所选版本不一致')
    validateManualGenerationContract(selectedAction.value, form)
    contract.value = form; draft.value = emptyExecutionRequestDraft(form)
  } catch (e) { if (token === serial) error.value = apiErrorSummary(e) }
  finally { if (token === serial) loading.value = false }
}
async function create() {
  if (busy.value || !contract.value || !draft.value || !selectedTarget.value || (!pending.value && inputErrors.value.length)) return
  const token = serial, retrying = Boolean(pending.value)
  loading.value = true; error.value = ''; rejected.value = false
  try {
    if (!pending.value) {
      if (Object.values(draft.value.media).flat().some(row=>row.reviewedShot && !reviewedShotSources.value.some(source=>
        JSON.stringify(source)===JSON.stringify(row.reviewedShot)))) throw new Error('本镜用途目录已失效，请重新读取并选择；不会降级为直接素材')
      if (Object.values(draft.value.media).flat().some(row => row.selectedOutput && !selectedSources.value.some(source =>
        source.artifactId === row.selectedOutput!.artifactId && source.decisionId === row.selectedOutput!.decisionId
        && source.runId === row.selectedOutput!.runId))) throw new Error('已选素材不在当前审核清单，请重新读取并选择；不会自动替换为新结果')
      pending.value = buildManualGeneration({ projectId: props.run.project, options: props.options,
        stageKey: stageKey.value, target: selectedTarget.value, idempotencyKey: requestKey, contract: contract.value,
        draft: await prepareExecutionSelectedSources(draft.value) })
    }
    const response = await platformApi.performManualProductionAction(props.run.id, pending.value)
    if (token !== serial) return
    preparationId.value = manualGenerationTarget(response)
    pending.value = null; emit('dirty', false); emit('changed')
  } catch (e) {
    if (token === serial) {
      error.value = apiErrorSummary(e)
      // An explicit rejection rolls back the initial command. A prior unknown
      // result still needs same-key reconciliation, even if its retry fails.
      if (!retrying && pending.value && isDefinitiveCreationRejection(e)) {
        pending.value = null; requestKey = crypto.randomUUID(); rejected.value = true
      }
    }
  }
  finally { if (token === serial) loading.value = false }
}
async function loadReviewedSources() {
  if (locked.value) return
  const token = ++serial; loading.value = true; error.value = ''; rejected.value = false; selectedSources.value = []
  try {
    const detail = readManualMediaReview(await platformApi.getManualMediaReview(props.run.id), props.run)
    if (token !== serial) return
    if (detail.approval.status !== 'current') {
      sourceNotice.value = detail.approval.status === 'changed' ? '采用结果已变化，请先重新完成选片审核。' : '请先采用素材并完成选片审核，再将其用作生成输入。'
      return
    }
    selectedSources.value = detail.items.map(row => ({ runId: props.run.id, artifactId: row.id, decisionId: row.decisionId,
      kind: row.kind, unitKey: row.unitKey, revision: row.revision }))
    sourceNotice.value = `已读取 ${detail.items.length} 项已审核素材。请在对应输入槽选择；提交时还会核对当前选择和文件。`
  } catch (e) { if (token === serial) { error.value = apiErrorSummary(e); sourceNotice.value = '本轮已审核素材读取失败，不能沿用旧目录。' } }
  finally { if (token === serial) loading.value = false }
}
async function loadShotSources() {
  if (locked.value || stageKey.value !== 'media_generation' || selectedTarget.value?.scope.kind !== 'shot') return
  const token = ++shotSourceSequence, shotId = selectedTarget.value.scope.id
  shotSourceLoading.value = true; reviewedShotSources.value = []; error.value = ''
  try {
    const detail = readManualStoryboardReferences(await platformApi.getManualStoryboardReview(props.run.id), props.run)
    if (token !== shotSourceSequence) return
    reviewedShotSources.value = manualGenerationShotSources(detail, shotId)
    shotSourceNotice.value = `已读取本镜 ${reviewedShotSources.value.length} 项用途。请在实例输入槽显式选择；接口用途仍独立配置。`
  } catch (cause) {
    if (token === shotSourceSequence) { error.value = apiErrorSummary(cause); shotSourceNotice.value = '本镜已审核用途不可用，未沿用旧目录。' }
  } finally { if (token === shotSourceSequence) shotSourceLoading.value = false }
}
watch(() => props.initialStageKey, (key) => {
  if (!key) return
  if (!stages.value.some(row => row.stageKey === key)) { error.value = '当前步骤未开放生成操作，请重新读取制作状态。'; return }
  stageKey.value = key; void changeStage()
}, { immediate: true })
</script>

<template>
  <div class="manual-generation">
    <p v-if="error" class="platform-alert danger" role="alert">{{ error }}</p>
    <p v-if="error && rejected" role="status">本次准备未保存，已保留全部填写内容。请修改后重新提交，无需关闭窗口或重新填写。</p>
    <p v-if="pending && !loading" class="platform-alert" role="status">尚未确认本次准备是否已保存，暂不能修改输入。请点击“重试同一准备请求”核对原结果，避免重复创建；不会调用模型。</p>
    <template v-if="!preparationId">
      <p>制作步骤 → 内容归属 → 执行实例与输入。创建只保存本次准备，不调用模型、不采用结果；之后在同一准备中确认字段和费用，再提交执行。</p>
      <fieldset :disabled="locked">
        <label class="platform-field">制作步骤<select :value="stageKey" @change="changeStage"><option value="">请选择</option><option v-for="item in stages" :key="item.stageKey" :value="item.stageKey">{{ stageLabel(item.stageKey) }}</option></select></label>
        <div v-if="selectedAction" class="manual-target-area">
          <label v-if="selectedAction.scopeKinds.length > 1" class="platform-field">生成内容用于哪里<select :value="targetKind" @change="loadTargets(1, $event)"><option value="">请选择归属类型</option><option v-for="kind in selectedAction.scopeKinds" :key="kind" :value="kind">{{ scopeLabels[kind] }}</option></select></label>
          <p v-else>此步骤用于{{ scopeLabels[selectedAction.scopeKinds[0]!] }}，归属类型已由步骤确定。</p>
          <p v-if="uniqueTarget && selectedTarget" class="manual-target-summary" role="status"><strong>生成内容归属：{{ selectedTarget.label }}</strong><span>已按本轮唯一对象确定，无需重复选择。</span></p>
          <label v-else-if="targetKind && targets.length" class="platform-field">{{ targetLabel }}<select :value="targetKey" @change="selectTarget"><option value="">请选择明确对象</option><option v-for="target in targets" :key="target.unitKey" :value="target.unitKey">{{ target.label }}</option></select></label>
          <p>决定结果属于哪个项目、剧集、镜头或实体；不是输入素材。Bible 参考图的主体输入可按所选对象预填本轮已审核描述，请在下方核对、修改后再保存。</p>
          <div v-if="targetKind" class="manual-form-tools"><template v-if="targetPage > 1 || targetHasNext"><button class="button ghost small" type="button" :disabled="targetPage === 1" @click="loadTargets(targetPage - 1)">上一页对象</button><span>第 {{ targetPage }} 页 · 共 {{ targetCount }} 项</span><button class="button ghost small" type="button" :disabled="!targetHasNext" @click="loadTargets(targetPage + 1)">下一页对象</button></template><button class="button ghost small" type="button" @click="loadTargets(targetPage)">刷新制作对象</button></div>
        </div>
        <label v-if="selectedTarget" class="platform-field">已发布执行实例<select :value="versionId" @change="loadContract"><option value="">请选择执行实例版本</option><option v-for="item in versions" :key="item.id" :value="item.id">{{ item.instance_name }} · v{{ item.version }} · {{ item.operation }}</option></select></label>
        <div v-if="stageKey" class="manual-form-tools"><template v-if="page > 1 || hasNext"><button class="button ghost small" type="button" :disabled="page === 1" @click="loadVersions(page - 1)">上一页实例</button><span>第 {{ page }} 页</span><button class="button ghost small" type="button" :disabled="!hasNext" @click="loadVersions(page + 1)">下一页实例</button></template><button class="button ghost small" type="button" @click="changeStage()">刷新实例</button></div>
      </fieldset>
      <p v-if="targetKind && !loading && !error && !targets.length">本轮范围内没有此类制作对象，请先创建对应{{ scopeLabels[targetKind] }}，再刷新目录。按剧集制作时，角色、场景和道具须已关联本轮镜头。</p>
      <p v-if="selectedAction && !selectedAction.operation">媒体实例按其冻结合同核对输出类型，不根据实例名称猜测用途。未匹配此步骤的实例会明确提示，不能提交。</p>
      <p v-if="stageKey && !loading && !error && !versions.length">本页没有对应的已发布实例。可继续翻页或到模型设置发布所需能力的实例；此处不会改选其他能力。</p>
      <div v-if="contract && draft && stageKey === 'media_generation'">
        <button type="button" class="button" :disabled="locked" @click="loadReviewedSources">读取本轮已审核素材</button><p>{{ sourceNotice }}</p>
        <template v-if="selectedTarget?.scope.kind === 'shot'"><button type="button" class="button" :disabled="locked" @click="loadShotSources">读取本镜已审核用途</button><p role="status">{{shotSourceNotice}}</p></template>
      </div>
      <ExecutionRequestInputEditor v-if="contract && draft" :key="contract.versionId" v-model="draft" :contract="contract" :project-id="run.project" :run-id="run.id" :stage-key="stageKey" :production-scope="selectedTarget?.scope" :selected-sources="stageKey === 'media_generation' ? selectedSources : undefined" :reviewed-shot-sources="stageKey === 'media_generation' && selectedTarget?.scope.kind === 'shot' ? reviewedShotSources : undefined" :disabled="locked" @busy="inputBusy = $event" @validity="inputErrors = $event"/>
      <p v-if="loading" role="status">正在读取或保存准备…</p>
      <p v-if="inputErrors.length" role="alert">{{ inputErrors.join('；') }}</p>
      <div class="manual-generation-footer"><p>下一步：在已保存的准备中确认字段和费用，再提交生成。</p><button type="button" class="button primary" :disabled="busy || !selectedTarget || !contract || !draft || (!pending && Boolean(inputErrors.length))" @click="create">{{ pending ? '重试同一准备请求' : '保存生成准备（不调用模型）' }}</button></div>
    </template>
    <template v-else>
      <p>准备已保存。这里只处理当前候选，不会自动采用，也不会启动下一制作步骤。</p>
      <ExecutionPreparationWorkbench :preparation-id="preparationId" :version-id="versionId" :manual-run="run" purpose="production" @busy="workbenchBusy = $event" @dirty="emit('dirty', $event)" @changed="emit('changed')"/>
    </template>
  </div>
</template>

<style scoped>
.manual-generation .button{min-height:36px;font-size:12px}.manual-generation .platform-field{font-size:13px}
.manual-generation,.manual-generation fieldset{display:grid;gap:16px;min-width:0}.manual-generation fieldset{border:0;padding:0;margin:0}.manual-generation p{margin:0;font-size:13px;line-height:1.6;color:var(--text-secondary)}.manual-generation select{width:100%;min-width:0}.manual-target-area{display:grid;gap:12px;padding:14px;border:1px solid var(--border);border-radius:8px}.manual-target-summary{display:grid;gap:4px}.manual-target-summary strong{color:var(--text-primary)}.manual-form-tools,.manual-generation-footer{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.manual-form-tools{font-size:12px;color:var(--text-secondary)}.manual-generation-footer{border-top:1px solid var(--border);padding-top:16px;justify-content:space-between}.manual-generation-footer .button{width:auto;white-space:normal}@media(max-width:540px){.manual-generation-footer{align-items:stretch;flex-direction:column}}
</style>
