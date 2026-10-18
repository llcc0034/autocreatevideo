<script setup lang="ts">
import ProductionWorkspaceTabs from './ProductionWorkspaceTabs.vue'
import ProductionWorkspacePanel from './ProductionWorkspacePanel.vue'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { isDefinitiveCreationRejection } from '@/services/productionOnboarding'
import { buildManualScriptAdoption, type ManualActionOptions } from '@/services/productionManual'
import type { JsonObject, ProductionRun, ScriptRevision, ScriptSource, StageRun } from '@/services/platformTypes'
import { stageLabel } from '@/services/presentationLabels'
import { productionWorkspaceSections, workspaceSectionIncludes, type ProductionWorkspaceSection } from '@/services/productionWorkspacePresentation'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import BaseModal from './BaseModal.vue'
import ManualGenerationPreparation from './ManualGenerationPreparation.vue'
import ManualMediaAdoption from './ManualMediaAdoption.vue'
import ManualMediaSelectionReview from './ManualMediaSelectionReview.vue'
import ProductionTextCandidateReview from './ProductionTextCandidateReview.vue'
import ManualBibleReview from './ManualBibleReview.vue'
import ManualBibleImageReview from './ManualBibleImageReview.vue'
import ManualStoryboardAdoption from './ManualStoryboardAdoption.vue'
import ManualStoryboardReferenceReview from './ManualStoryboardReferenceReview.vue'
import ManualShotReferenceSelection from './ManualShotReferenceSelection.vue'
import ManualAssemblyReview from './ManualAssemblyReview.vue'
import ManualDeliveryReview from './ManualDeliveryReview.vue'
import ManualContentChangeImpact from './ManualContentChangeImpact.vue'
import ManualExecutionActivity from './ManualExecutionActivity.vue'
import ManualScriptIdentity from './ManualScriptIdentity.vue'
import ManualScriptStructureCheck from './ManualScriptStructureCheck.vue'
import { taskCenterTarget } from '@/services/taskCenter'

const props = defineProps<{ run: ProductionRun; projectName?: string; currentRules?: Record<string, string | undefined>; initialSection?: string; inputsProtected?: boolean }>()
const emit = defineEmits<{ changed: []; contentChanged: []; cancel: []; history: []; inspectPreparations: [stageId: string]; contextProtected: [value: boolean]; sectionChanged: [section: ProductionWorkspaceSection] }>()
const options = ref<ManualActionOptions | null>(null), busy = ref(false), error = ref(''), notice = ref('')
const sources = ref<ScriptSource[]>([]), revisions = ref<ScriptRevision[]>([]), sourceId = ref(''), revisionId = ref(''), comment = ref('')
const requestKey = ref(crypto.randomUUID()), pendingRequest = ref<JsonObject | null>(null)
const adoptionBusy = ref(false), adoptionError = ref('')
const adoptionProtected = computed(() => adoptionBusy.value || Boolean(pendingRequest.value))
const selectedRevision = computed(() => revisions.value.find(row => row.id === revisionId.value))
const alreadyAdopted = computed(() => Boolean(selectedRevision.value && options.value?.currentScript?.id === selectedRevision.value.id))
const stageActivity = (id: string) => options.value?.executionActivity?.find(row => row.stageId === id)
function inspectStage(stageId: string) { section.value = 'records'; emit('inspectPreparations', stageId) }
const terminal = computed(() => ['succeeded','failed','cancelled'].includes(props.run.status))
const initialSection = (): ProductionWorkspaceSection => productionWorkspaceSections.find(row => row.key === props.initialSection)?.key || 'script'
const section = ref<ProductionWorkspaceSection>(initialSection())
watch(() => props.initialSection, () => { section.value = initialSection() })
watch(section, value => emit('sectionChanged', value), {immediate:true})
const activeSection = computed(() => productionWorkspaceSections.find(row => row.key === section.value)!)
const sectionStages = computed(() => props.run.stages?.filter(stage => workspaceSectionIncludes(section.value, stage.stageKey)) ?? [])
const sectionGenerationActions = computed(() => options.value?.actions.filter(action => action.action === 'generate' && workspaceSectionIncludes(section.value, action.stageKey)) ?? [])
const canAct = (stageKey: string, action: string) => !busy.value && !props.inputsProtected && Boolean(options.value?.actions.some(row => row.stageKey === stageKey && row.action === action))
const generationOpen = ref(false), generationBusy = ref(false), generationDirty = ref(false)
const mediaOpen = ref(false), mediaBusy = ref(false)
const mediaReviewOpen = ref(false), mediaReviewBusy = ref(false)
const scriptReviewOpen = ref(false), scriptReviewBusy = ref(false)
const textReviewContext = ref<{run: ProductionRun; stage: StageRun; options: ManualActionOptions} | null>(null)
const bibleOpen = ref(false), bibleBusy = ref(false)
const bibleContext = ref<{options: ManualActionOptions} | null>(null), bibleContentChanged = ref(false)
const bibleImageOpen = ref(false), bibleImageBusy = ref(false)
const bibleImageContext = ref<{ options: ManualActionOptions } | null>(null)
const bibleImageRetained = ref({ dirty: false, pending: false })
const bibleImageProtected = computed(() => bibleImageOpen.value || bibleImageRetained.value.dirty || bibleImageRetained.value.pending)
const shotSelectionOpen = ref(false), shotSelectionBusy = ref(false)
const shotSelectionContext = ref<{options: ManualActionOptions} | null>(null)
const shotSelectionRetained = ref({dirty: false, pending: false})
const shotSelectionProtected = computed(() => shotSelectionOpen.value || shotSelectionRetained.value.dirty || shotSelectionRetained.value.pending)
const storyboardOpen = ref(false), storyboardBusy = ref(false)
const storyboardReferenceOpen = ref(false), storyboardReferenceBusy = ref(false)
const assemblyOpen = ref(false), assemblyBusy = ref(false), assemblyPending = ref(false)
const assemblyContext = ref<{options: ManualActionOptions} | null>(null)
function openAssembly() {
  if (!options.value || assemblyBusy.value) return
  assemblyContext.value = {options: options.value}; assemblyOpen.value = true
}
// Whole-page reads may unmount the workbench on failure. Protect both open
// editors and a closed review that still owns a draft or an unknown command.
watch(() => adoptionProtected.value || bibleOpen.value || generationOpen.value || bibleImageProtected.value || shotSelectionProtected.value || storyboardReferenceBusy.value || assemblyOpen.value || assemblyPending.value, value => emit('contextProtected', value), { flush: 'sync' })
const deliveryOpen = ref(false), deliveryBusy = ref(false)
const impactOpen = ref(false)
const textReviewKey = ref<'script_review' | 'visual_rules_review'>('script_review')
const scriptReviewStage = computed(() => props.run.stages?.find(row => row.stageKey === textReviewKey.value))
const generationContext = ref<{ run: ProductionRun; options: ManualActionOptions; stageKey: string } | null>(null)
const canClosePreparation = () => !shotSelectionBusy.value && !storyboardReferenceBusy.value && !deliveryBusy.value && !assemblyBusy.value && !bibleImageBusy.value && !storyboardBusy.value && !bibleBusy.value && !scriptReviewBusy.value && !mediaReviewBusy.value && !mediaBusy.value && !generationBusy.value
  && (!generationDirty.value || window.confirm('当前执行准备表单有未保存内容，确定离开？'))
const canLeave = () => !assemblyPending.value && !adoptionBusy.value && canClosePreparation()
  && (!pendingRequest.value || window.confirm('剧本采用结果尚未确认，可能已经保存。离开会丢失原请求，请先重试同一采用请求核对；仍要离开吗？'))
  && (!(shotSelectionRetained.value.dirty || shotSelectionRetained.value.pending) || window.confirm(shotSelectionRetained.value.pending
    ? '选材请求结果未知，可能已保存。离开会丢失原请求，请先继续单镜选材核对；仍要离开吗？'
    : '单镜选材有未提交选择，离开会丢弃草稿；已上传入库的文件保留。确定离开？'))
  && (!(bibleImageRetained.value.dirty || bibleImageRetained.value.pending) || window.confirm(bibleImageRetained.value.pending
    ? '参考图审核结果尚未确认，可能已经写入。离开会丢失本页原请求，请先继续参考图审核核对；仍要离开吗？'
    : '参考图审核有未提交的选择或说明，离开此页将丢弃。确定离开？'))
onBeforeRouteLeave(canLeave)
onBeforeRouteUpdate(canLeave)
function openGeneration(stageKey = '') {
  if (props.inputsProtected || busy.value || terminal.value || !options.value) return
  if (stageKey && !canAct(stageKey, 'generate')) return
  generationContext.value = { run: props.run, options: options.value, stageKey }
  generationDirty.value = false; generationOpen.value = true
}
function closeGeneration() {
  if (!canClosePreparation()) return
  generationOpen.value = false; generationContext.value = null; generationDirty.value = false
}
function openBibleReview() {
  if (!canAct('bible_review', 'approve') || !options.value) return
  bibleContext.value = {options: options.value}; bibleContentChanged.value = false; bibleOpen.value = true
}
function closeBibleReview() {
  if (bibleBusy.value) return
  bibleOpen.value = false; bibleContext.value = null
  // The child rereads its own successful write. Refresh the parent only after
  // closing, so adopting a source cannot destroy the next review's selections.
  if (bibleContentChanged.value) { bibleContentChanged.value = false; emit('contentChanged') }
}
function openBibleImageReview() {
  if (bibleImageContext.value) { bibleImageOpen.value = true; return }
  if (!canAct('bible_image_review', 'approve') || !options.value) return
  // Keep the observed action contract while background polling reloads options.
  // The child receives live run status; the server rechecks permissions and CAS.
  bibleImageContext.value = { options: options.value }
  bibleImageOpen.value = true
}
function closeBibleImageReview() {
  if (!bibleImageBusy.value) bibleImageOpen.value = false
}
function openShotSelection() {
  if (shotSelectionContext.value) {shotSelectionOpen.value = true; return}
  if (!canAct('storyboard_review', 'select') || !options.value) return
  shotSelectionContext.value = {options: options.value}; shotSelectionOpen.value = true
}
function closeShotSelection() {if (!shotSelectionBusy.value) shotSelectionOpen.value = false}
watch([() => props.run.id, () => props.run.project, () => props.run.executionPlan?.sha256], () => {
  bibleOpen.value = false; bibleContext.value = null; bibleContentChanged.value = false; bibleBusy.value = false
  shotSelectionOpen.value = false; shotSelectionContext.value = null; shotSelectionBusy.value = false
  shotSelectionRetained.value = {dirty: false, pending: false}
  bibleImageOpen.value = false; bibleImageContext.value = null; bibleImageBusy.value = false
  bibleImageRetained.value = { dirty: false, pending: false }
}, { flush: 'sync' })
let sequence = 0
async function load() {
  // Keep the same-run editor mounted while polling. Actions remain disabled
  // while busy; a failed read clears its authority rather than using old options.
  const token = ++sequence; busy.value = true; error.value = ''
  try {
    const [state, scripts] = await Promise.all([platformApi.getManualProductionActions(props.run.id), platformApi.listScriptSources(props.run.project)])
    if (token !== sequence) return
    if (state.runId !== props.run.id || state.runPlanSha256 !== props.run.executionPlan?.sha256) throw new Error('人工动作与冻结执行计划不一致，请刷新运行详情')
    options.value = state; sources.value = scripts
  } catch (e) { if (token === sequence) {options.value = null; error.value = apiErrorSummary(e)} }
  finally { if (token === sequence) busy.value = false }
}
async function loadRevisions() {
  const token = ++sequence; revisionId.value = ''; revisions.value = []; error.value = ''
  if (!sourceId.value) return
  busy.value = true
  try {
    const rows = await platformApi.listConfirmedScriptRevisions(sourceId.value)
    if (token === sequence) revisions.value = rows.filter(row => row.status === 'confirmed' && row.confirmedAt)
  } catch (e) { if (token === sequence) error.value = apiErrorSummary(e) }
  finally { if (token === sequence) busy.value = false }
}
async function adopt() {
  if (props.inputsProtected || busy.value || terminal.value || !options.value || (!pendingRequest.value && (!selectedRevision.value || alreadyAdopted.value))) return
  const expectedRun = props.run.id, token = sequence, retrying = Boolean(pendingRequest.value)
  adoptionBusy.value = true; busy.value = true; adoptionError.value = ''; notice.value = ''
  try {
    if (!pendingRequest.value) pendingRequest.value = buildManualScriptAdoption({ projectId: props.run.project, options: options.value,
      idempotencyKey: requestKey.value, revisionId: selectedRevision.value!.id, contentSha256: selectedRevision.value!.contentSha256, comment: comment.value })
    await platformApi.performManualProductionAction(props.run.id, pendingRequest.value)
    if (token !== sequence || expectedRun !== props.run.id) return
    pendingRequest.value = null; requestKey.value = crypto.randomUUID()
    await load()
    adoptionBusy.value = false
    notice.value = '剧本已明确采用；其他步骤没有自动执行。'; emit('changed')
  } catch (e) {
    if (expectedRun === props.run.id) {
      adoptionError.value = apiErrorSummary(e)
      // A first, definite rejection did not commit. An uncertain original
      // request keeps its exact identity even if a retry is later rejected.
      if (!retrying && isDefinitiveCreationRejection(e)) {
        pendingRequest.value = null; requestKey.value = crypto.randomUUID()
      }
    }
  }
  finally { if (expectedRun === props.run.id) { busy.value = false; adoptionBusy.value = false } }
}
async function reselect() {
  // Explicitly reread the committed selection before constructing a new intent.
  await load()
  if (!error.value) { pendingRequest.value = null; requestKey.value = crypto.randomUUID() }
}
async function scriptReviewed() {
  closeTextReview(); emit('contentChanged'); await load()
}
function closeTextReview() { scriptReviewOpen.value = false; textReviewContext.value = null }
function openTextReview(key: 'script_review' | 'visual_rules_review') {
  if (busy.value || !options.value?.actions.some(row => row.stageKey === key && row.action === 'approve')) return
  textReviewKey.value = key
  if (!scriptReviewStage.value) return
  // Parent polling clears its loading state. Keep this explicit review context
  // mounted; the original command still verifies the observed adoption revision.
  textReviewContext.value = {run:props.run,stage:scriptReviewStage.value,options:options.value}
  scriptReviewOpen.value = true
}
watch(() => props.run.id, closeTextReview)
watch(() => props.run.id, () => { section.value = initialSection(); options.value = null; pendingRequest.value = null; adoptionError.value = ''; adoptionBusy.value = false; sourceId.value = ''; revisions.value = []; revisionId.value = ''; void load() }, { immediate: true })
// Parent refresh replaces the run object after a preparation completes. Reread
// original call facts too, but keep this round's unsaved selection/comment.
// A background options refresh must not invalidate an in-flight source/version
// read sharing this editor's request sequence. The next idle poll rereads it.
watch(() => props.run, (current, previous) => { if (current.id === previous?.id && !busy.value && !props.inputsProtected && !adoptionProtected.value && !bibleOpen.value) void load() })
onBeforeUnmount(() => { sequence++; emit('contextProtected', false) })
</script>

<template>
  <ProductionWorkspacePanel class="manual-workbench">
    <header class="manual-heading"><div><h3>无配方人工制作</h3><p>{{ projectName || '当前项目' }} · 本轮 {{ run.id.slice(0,8) }}</p><p v-if="run.status === 'running'">本轮制作尚未结束；是否正在调用模型，请看各步骤的实际调用记录。</p></div><div class="manual-controls"><span>本轮流程</span><PlatformStatusPill :status="run.status"/><button type="button" class="button ghost small" :disabled="inputsProtected || busy" @click="load">刷新制作状态</button><button class="button ghost small" @click="section = 'records'">任务与记录</button><details class="manual-more"><summary>更多</summary><div class="manual-controls"><button class="button ghost small" :disabled="busy || generationBusy" @click="emit('history')">全部制作记录</button><button v-if="!terminal" class="button danger small" :disabled="busy || shotSelectionBusy || deliveryBusy || assemblyBusy || bibleImageBusy || storyboardBusy || bibleBusy || mediaBusy || mediaReviewBusy || scriptReviewBusy || generationBusy" @click="emit('cancel')">取消本轮</button></div></details></div></header>
      <details class="manual-budget"><summary>系统资源提醒与本轮授权 · {{ run.allowExternalCalls ? '已授权外部调用' : '未授权外部调用' }}</summary>
        <p>本轮模型调用：已用 {{ run.generationTaskCount }} 次（主调用、辅助调用和重试共同计数）；提醒值以当前系统策略为准。</p><slot name="budget"/>
    </details>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="shotSelectionProtected" class="platform-alert warning" role="status">单镜选材打开或有未提交／结果未知内容，整页轮询已暂停；面板关闭且无待处理内容后恢复。选择保留在当前页面，不会持久保存到浏览器。</p>
    <p v-if="bibleImageProtected" class="platform-alert warning" role="status">参考图审核打开或仍有未提交／待核对内容，整页运行状态刷新已暂停；显示的是上次读取状态。可继续审核、主动重新读取候选或核对原请求；面板关闭且无待处理内容后恢复。提交仍由服务端复验权限和当前修订。</p>
    <div v-if="error" class="platform-alert danger" role="alert"><span>{{ error }}</span><button type="button" class="button ghost small" :disabled="busy" @click="load">重试读取制作状态</button></div>
    <p v-if="adoptionError" class="platform-alert danger" role="alert">剧本采用未完成：{{ adoptionError }}</p>
    <p v-if="busy" role="status">正在读取或提交人工制作状态…</p>
    <p v-if="inputsProtected" class="platform-alert warning" role="status">项目输入正在编辑或保存；刷新与制作操作已暂停。请回到对应栏目保存或放弃编辑后继续。</p>
    <div class="production-workspace-layout">
      <ProductionWorkspaceTabs v-model="section" :items="productionWorkspaceSections" label="生产工作台栏目"/>
      <div class="manual-main">
        <header class="manual-section-heading"><div><h4>{{ activeSection.label }}</h4><p>按需选择环节；每次生成、采用与审核均由你发起。</p></div><button v-if="section !== 'records'" type="button" class="button ghost small" @click="section = 'records'">查看执行准备</button></header>
        <div v-if="sectionStages.length" class="manual-stage-status" aria-label="当前环节实际状态"><div v-for="stage in sectionStages" :key="stage.id"><span>{{ stageLabel(stage.stageKey).split('（')[0].trim() }}</span><ManualExecutionActivity v-if="stageActivity(stage.id)" :activity="stageActivity(stage.id)" :run-id="run.id" :loading="busy" @inspect="inspectStage(stage.id)"/><PlatformStatusPill v-else-if="options && !busy" :status="stage.status" :bilingual="false"/><span v-else>状态尚未读取</span></div></div>
        <div v-if="sectionGenerationActions.length" class="manual-generation-actions"><p>已有准备请从“查看已有准备与结果”继续；下方按钮创建新的候选。保存准备不会调用模型。</p><div class="manual-controls"><button v-for="action in sectionGenerationActions" :key="action.stageKey" type="button" class="button primary small" :disabled="inputsProtected || busy || terminal" @click="openGeneration(action.stageKey)">新建 {{ stageLabel(action.stageKey).split('（')[0].trim() }} 候选</button></div></div>
        <slot name="project-inputs" :section="section"/>
    <section v-show="section === 'script'" class="manual-script">
      <RouterLink class="button ghost small" :to="{ name: 'production-run', params: { projectId: run.project }, query: { view: 'scripts', run: run.id } }">导入／编辑项目剧本（不调用模型）</RouterLink>
      <h4>本轮采用的剧本</h4>
      <ManualScriptIdentity v-if="options" :options="options"/>
      <ManualScriptStructureCheck :run-id="run.id" :options="options"/>
      <details v-if="options?.scriptAdoption.content && options.scriptAdoption.decisionId !== options.currentScriptDecisionId"><summary>此前的已确认剧本改选记录（不是当前采用）</summary><p>内容修订 {{ options.scriptAdoption.content.revision }} · 修订 ID {{ options.scriptAdoption.content.id }} · 采用记录第 {{ options.scriptAdoption.revision }} 版</p></details>
      <button class="button ghost small" :disabled="inputsProtected || busy || !options?.actions.some(row => row.stageKey === 'script_review' && row.action === 'approve')" @click="openTextReview('script_review')">{{ terminal ? '查看生成的剧本（只读）' : '查看并审核生成的剧本' }}</button>
      <details><summary>如何准备剧本？</summary><p>导入或粘贴只保存原稿。普通文本可先生成结构化候选，再审核采用；已通过结构化校验的 AI 剧本无需重复结构化。审核生成候选后即完成采用，下方用于改选其他已确认剧本；仍可手动创建分镜，不强制使用 AI。</p></details>
      <details class="script-reselection" :open="!options?.currentScript || Boolean(pendingRequest)"><summary>{{ options?.currentScript ? '更换本轮采用的剧本（选择已确认修订）' : '选择已有的已确认剧本' }}</summary>
      <p>仅在需要改用另一内容版本时操作；审核生成候选已经完成采用，不需在这里重复确认。</p>
      <fieldset :disabled="inputsProtected || busy || terminal || Boolean(pendingRequest)">
        <label class="platform-field"><span>原稿来源（来源 ID）</span><select v-model="sourceId" @change="loadRevisions"><option value="">请选择</option><option v-for="source in sources" :key="source.id" :value="source.id">{{ source.originalName || source.sourceFormat }} · 来源 {{ source.id.slice(0,8) }}{{ options?.currentScript?.source_id === source.id ? ' · 当前采用的来源' : '' }}</option></select></label>
        <label class="platform-field"><span>已确认内容版本（修订 ID）</span><select v-model="revisionId"><option value="">请选择</option><option v-for="revision in revisions" :key="revision.id" :value="revision.id">修订 {{ revision.revision }} · {{ revision.id.slice(0,8) }}{{ options?.currentScript?.id === revision.id ? ' · 本轮已采用' : '' }} · {{ revision.confirmedAt }}</option></select></label>
        <label class="platform-field"><span>采用说明（可选）</span><input v-model="comment"></label>
      </fieldset>
      <p v-if="!busy && !error && (!sources.length || (sourceId && !revisions.length))">暂无可选的已确认剧本，请先通过项目剧本管理导入并确认。</p>
      <div class="manual-controls"><button type="button" class="button primary small" :disabled="inputsProtected || busy || terminal || !options || (!pendingRequest && (!selectedRevision || alreadyAdopted))" @click="adopt">{{ pendingRequest ? '重试同一采用请求' : alreadyAdopted ? '本轮已采用此修订' : '采用选中的剧本' }}</button><button type="button" class="button ghost small" :disabled="busy" @click="reselect">重新读取采用状态</button></div>
      </details>
    </section>
    <section v-if="section === 'bible'" class="manual-script">
      <h4>人物、场景与道具</h4><p>从已采用剧本读取候选并核对；参考图生成与主图选择分别处理。</p>
      <div class="manual-controls"><button class="button ghost small" :disabled="!canAct('bible_review','approve')" @click="openBibleReview">读取并审核 Bible 候选</button><button class="button ghost small" :disabled="inputsProtected || !bibleImageContext && !canAct('bible_image_review','approve')" @click="openBibleImageReview">{{ bibleImageContext ? '继续参考图与主图审核' : '选择参考图与唯一主图' }}</button></div>
      <p v-if="bibleImageContext && !bibleImageOpen" role="status">参考图审核状态保留在当前页面{{ bibleImageRetained.pending ? '，原提交结果仍待核对' : bibleImageRetained.dirty ? '，有未提交的选择或说明' : '' }}；可继续审核。离开或刷新浏览器不会持久保存。</p>
    </section>
    <section v-if="section === 'visual'" class="manual-script">
      <h4>影视规则</h4><p v-if="options" role="status" :class="{ 'platform-alert warning': options.visualRulesReview.status === 'changed' }">审核依据：{{ { not_reviewed: '尚未审核', current: '与当前来源一致', changed: '已变化，需重新核对' }[options.visualRulesReview.status] }}。</p>
      <ul v-if="options?.visualRulesReview.reasons.length"><li v-for="reason in options.visualRulesReview.reasons" :key="reason">{{ reason }}</li></ul>
      <button class="button ghost small" :disabled="!canAct('visual_rules_review','approve')" @click="openTextReview('visual_rules_review')">{{ terminal ? '查看影视规则候选（只读）' : '逐项审核影视规则' }}</button>
    </section>
    <section v-if="section === 'storyboard'" class="manual-script"><h4>核对本轮分镜</h4><p>采用项目中已保存的剧集和镜头，再选择与审核参考素材。选材不等于审核，不自动生成媒体。</p><div class="manual-controls"><button class="button ghost small" :disabled="!canAct('storyboard','adopt')" @click="storyboardOpen = true">逐镜核对并采用内容</button><button class="button ghost small" :disabled="inputsProtected || !shotSelectionContext && !canAct('storyboard_review','select')" @click="openShotSelection">{{ shotSelectionContext ? '继续单镜 / 多镜素材选择' : '选择素材 / 上传（单镜 / 多镜）' }}</button><button class="button ghost small" :disabled="!canAct('storyboard_review','approve')" @click="storyboardReferenceOpen = true">逐镜审核参考素材</button></div></section>
    <section v-if="section === 'media'" class="manual-script"><h4>查看与采用媒体候选</h4><p>生成成功不等于已采用。先核对内容与制作对象，再明确采用。</p><div class="manual-controls"><button class="button ghost small" :disabled="!canAct('media_generation','adopt')" @click="mediaOpen = true">查看本轮媒体候选</button><button class="button ghost small" :disabled="!canAct('artifact_selection','approve')" @click="mediaReviewOpen = true">审核本轮已采用媒体</button></div></section>
    <section v-if="section === 'delivery'" class="manual-script">
      <h4>装配与交付</h4><p>逐集核对已审核选片和输出参数，再提交装配；后台完成不代表已经交付。</p>
      <button class="button ghost small" :disabled="busy || !options?.actions.some(row => row.stageKey === 'assembly' && row.action === 'assemble')" @click="openAssembly">查看选片与装配任务</button>
      <button class="button ghost small" :disabled="busy || !options?.actions.some(row => row.stageKey === 'delivery' && row.action === 'deliver')" @click="deliveryOpen = true">查看成片与封存</button>
    </section>
    <section v-if="section === 'records'" class="manual-script">
      <h4>执行准备与历史</h4><p>查看不会重新创建请求或调用模型。已有准备可继续确认字段与提交。</p>
      <slot name="records"/>
      <div class="manual-controls"><button class="button ghost small" :disabled="busy || !options" @click="impactOpen = true">查看改选影响</button><button class="button ghost small" :disabled="inputsProtected || busy || terminal || !options?.actions.some(row => row.action === 'generate')" @click="openGeneration()">选择步骤与执行实例</button><slot name="recovery"/></div>
      <RouterLink class="button ghost small" :to="taskCenterTarget({ category: 'production', runId: run.id })">在任务中心查看本轮任务</RouterLink>
      <p>步骤开放允许追加候选，不代表模型正在调用。准备、主辅调用、审核和采用分别核对；查看记录不会执行或重试。</p>
      <details><summary>本轮阶段与实际状态</summary><div class="manual-stages"><div v-for="stage in run.stages" :key="stage.id"><span>{{ stageLabel(stage.stageKey) }}</span><ManualExecutionActivity v-if="stageActivity(stage.id)" :activity="stageActivity(stage.id)" :run-id="run.id" :loading="busy" @inspect="inspectStage(stage.id)"/><PlatformStatusPill v-else-if="options && !busy" :status="stage.status"/><span v-else>状态尚未读取</span></div></div></details>
    </section>
      </div>
    </div>
    <ManualShotReferenceSelection v-if="shotSelectionContext" :open="shotSelectionOpen" :run="run" :options="shotSelectionContext.options" @close="closeShotSelection" @busy="shotSelectionBusy=$event" @retained="shotSelectionRetained=$event" @changed="emit('contentChanged')"/>
    <BaseModal :open="assemblyOpen" title="人工制作 · 逐集装配" width="1000px" :dismissible="!assemblyBusy && !assemblyPending" @close="!assemblyBusy && !assemblyPending && (assemblyOpen = false)">
      <ManualAssemblyReview v-if="assemblyOpen && assemblyContext" :run="run" :options="assemblyContext.options" @busy="assemblyBusy = $event" @pending="assemblyPending = $event" @changed="emit('changed')"/>
    </BaseModal>
    <BaseModal :open="deliveryOpen" title="人工制作 · 确认成片并封存" width="1000px" :dismissible="!deliveryBusy" @close="deliveryOpen = false">
      <ManualDeliveryReview v-if="deliveryOpen && options" :run="run" :options="options" @busy="deliveryBusy = $event" @changed="emit('changed')"/>
    </BaseModal>
    <BaseModal :open="impactOpen" title="人工制作 · 改选影响（只读）" width="1000px" @close="impactOpen = false">
      <ManualContentChangeImpact v-if="impactOpen && options" :run-id="run.id" :run-plan-sha256="options.runPlanSha256" :stage-keys="options.contentChangeStageKeys"/>
    </BaseModal>
    <BaseModal :open="bibleOpen" title="人工制作 · 人物、场景与道具" width="1000px" :dismissible="!bibleBusy" @close="closeBibleReview">
      <ManualBibleReview v-if="bibleOpen && bibleContext" :run="run" :options="bibleContext.options" @busy="bibleBusy = $event" @changed="bibleContentChanged = true"/>
    </BaseModal>
    <ManualBibleImageReview v-if="bibleImageContext" :open="bibleImageOpen" :run="run" :options="bibleImageContext.options" @close="closeBibleImageReview" @retained="bibleImageRetained = $event" @busy="bibleImageBusy = $event" @changed="emit('contentChanged')"/>
    <BaseModal :open="storyboardOpen" title="人工制作 · 明确采用分镜" width="1050px" :dismissible="!storyboardBusy" @close="storyboardOpen = false">
      <ManualStoryboardAdoption v-if="storyboardOpen && options" :run="run" :options="options" @busy="storyboardBusy = $event" @changed="emit('contentChanged')"/>
    </BaseModal>
    <BaseModal :open="storyboardReferenceOpen" title="人工制作 · 逐镜参考素材审核" width="1050px" :dismissible="!storyboardReferenceBusy" @close="storyboardReferenceOpen = false">
      <ManualStoryboardReferenceReview v-if="storyboardReferenceOpen && options" :run="run" :options="options" @busy="storyboardReferenceBusy = $event" @changed="emit('contentChanged')"/>
    </BaseModal>
    <BaseModal :open="generationOpen" title="人工制作 · 创建执行准备" width="1100px" :dismissible="!generationBusy" @close="closeGeneration">
      <p role="status">此面板打开期间暂停整页状态自动刷新，避免中断填写；关闭后恢复。输入来源仍按需读取，保存与执行仍由服务端核对当前权限和版本。</p>
      <ManualGenerationPreparation v-if="generationContext" :run="generationContext.run" :options="generationContext.options" :initial-stage-key="generationContext.stageKey" @busy="generationBusy = $event" @dirty="generationDirty = $event" @changed="emit('changed')"/>
    </BaseModal>
    <BaseModal :open="mediaOpen" title="人工制作 · 明确采用媒体" width="960px" :dismissible="!mediaBusy" @close="mediaOpen = false">
      <ManualMediaAdoption v-if="mediaOpen && options" :run="run" :options="options" @busy="mediaBusy = $event" @changed="emit('changed')"/>
    </BaseModal>
    <BaseModal :open="mediaReviewOpen" title="人工制作 · 审核已采用媒体" width="960px" :dismissible="!mediaReviewBusy" @close="mediaReviewOpen = false">
      <ManualMediaSelectionReview v-if="mediaReviewOpen && options" :run="run" :options="options" @busy="mediaReviewBusy = $event" @changed="emit('changed')"/>
    </BaseModal>
    <BaseModal :open="scriptReviewOpen" :title="terminal ? (textReviewKey === 'script_review' ? '人工制作 · 剧本候选（只读）' : '人工制作 · 影视规则候选（只读）') : (textReviewKey === 'script_review' ? '人工制作 · 审核剧本候选' : '人工制作 · 逐项审核影视规则')" width="1100px" :dismissible="!scriptReviewBusy" @close="closeTextReview">
      <ProductionTextCandidateReview v-if="scriptReviewOpen && textReviewContext" :run="textReviewContext.run" :stage="textReviewContext.stage" :read-only="terminal" :manual-options="textReviewContext.options" :current-rules="currentRules ?? {}" @busy="scriptReviewBusy = $event" @changed="scriptReviewed"/>
    </BaseModal>
  </ProductionWorkspacePanel>
</template>

<style scoped>
.manual-stages>div{flex-wrap:wrap;align-items:flex-start;min-width:0}
.manual-workbench .button{min-height:36px;font-size:12px}.manual-stage-status :deep(.platform-status){font-size:11px}.manual-workbench :deep(.platform-field){font-size:12px}
.manual-workbench .manual-heading,.manual-section-heading{display:flex;gap:16px;justify-content:space-between;align-items:center;flex-wrap:wrap}.manual-heading>div:first-child{display:grid;gap:5px}.manual-workbench h3,.manual-workbench h4,.manual-workbench p{margin:0}.manual-workbench h3{font-size:18px}.manual-workbench h4{font-size:15px}.manual-workbench p{font-size:14px;line-height:1.7;color:var(--text-secondary)}.manual-workbench .manual-controls{display:flex;flex-direction:row;flex-wrap:wrap;gap:8px;align-items:center}.manual-controls .button,.manual-script>.button{width:auto;justify-self:start}.manual-budget{padding:12px 0;border-bottom:1px solid var(--border)}.manual-budget[open]{display:grid;gap:12px}.manual-workbench summary{cursor:pointer;font-size:13px;min-height:36px;align-content:center}.manual-more{position:relative}.manual-more[open]>.manual-controls{position:absolute;right:0;top:40px;z-index:5;width:180px;padding:12px;background:var(--bg-panel,#101419);border:1px solid var(--border);border-radius:8px}.production-workspace-layout{display:grid;gap:24px;min-width:0}.manual-main{display:grid;gap:18px;min-width:0}.manual-section-heading>div{display:grid;gap:5px}.manual-stage-status{display:flex;flex-wrap:wrap;gap:8px 18px}.manual-stage-status>div{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:12px;color:var(--text-secondary)}.manual-generation-actions{display:grid;gap:10px}.manual-script{display:grid;gap:12px;border-top:1px solid var(--border);padding-top:16px;min-width:0}.manual-script fieldset{border:0;min-width:0;margin:0;padding:0;display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}.manual-stages{display:grid;gap:12px;margin-top:12px}.manual-stages>div{display:flex;gap:12px;justify-content:space-between}.manual-workbench :deep(.platform-card){min-width:0}.manual-workbench :deep(p){overflow-wrap:anywhere}
@media(max-width:900px){.production-workspace-layout{grid-template-columns:1fr;gap:16px}}@media(max-width:540px){.manual-script fieldset{grid-template-columns:1fr}.manual-heading{align-items:flex-start}.manual-controls .button{max-width:100%;white-space:normal}.manual-main{gap:14px}}
</style>
