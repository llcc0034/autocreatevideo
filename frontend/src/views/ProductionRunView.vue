<script setup lang="ts">
import BudgetCurrencySelect from '@/components/BudgetCurrencySelect.vue'
import ProductionBudgetFields from '@/components/ProductionBudgetFields.vue'
import ScriptRevisionStructurePanel from '@/components/ScriptRevisionStructurePanel.vue'
import BibleImageSelectionEditor from '@/components/BibleImageSelectionEditor.vue'
import { initialBibleImageSelection, reviewedBibleImageBindings, type BibleImageReviewSelection } from '@/services/bibleImageSelections'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AlertTriangle, BookOpen, Check, ChevronDown, CircleStop, FileText, GitBranch, Image, LoaderCircle, Play, Plus, RefreshCw, RotateCcw, Trash2, Upload } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import RecipeExecutionSummary from '@/components/RecipeExecutionSummary.vue'
import RecipeProjectSourceConfirmations from '@/components/RecipeProjectSourceConfirmations.vue'
import type { RecipeProjectSourceConfirmation } from '@/services/platformTypes'
import { recipeExecutionOverview } from '@/services/recipeExecutionOverview'
import ManualProductionCreateForm from '@/components/ManualProductionCreateForm.vue'
import ManualProductionWorkbench from '@/components/ManualProductionWorkbench.vue'
import SeriesStudioView from './SeriesStudioView.vue'
import ProjectVersionWorkspace from '@/components/ProjectVersionWorkspace.vue'
import ProductionWorkspaceHeader from '@/components/ProductionWorkspaceHeader.vue'
import ProductionWorkspaceTabs from '@/components/ProductionWorkspaceTabs.vue'
import ProductionWorkspacePanel from '@/components/ProductionWorkspacePanel.vue'
import TaskDistributionRecovery from '@/components/TaskDistributionRecovery.vue'
import ProductionTextCandidateReview from '@/components/ProductionTextCandidateReview.vue'
import ProductionStructuredTextResults from '@/components/ProductionStructuredTextResults.vue'
import RecipeMaterialFulfillment from '@/components/RecipeMaterialFulfillment.vue'
import RecipeShotReferenceSelection from '@/components/RecipeShotReferenceSelection.vue'
import { runStageLabel } from '@/services/productionStructuredText'
import RecipeReferenceMappingPreview from '@/components/RecipeReferenceMappingPreview.vue'
import RecipeStageInputAmendment from '@/components/RecipeStageInputAmendment.vue'
import { recipeObject, recipeRows } from '@/services/productionRecipeAuthoring'
import RecipeExecutionRouteChoices from '@/components/RecipeExecutionRouteChoices.vue'
import StageExecutionPreparations from '@/components/StageExecutionPreparations.vue'
import ProductionRetryPreview from '@/components/ProductionRetryPreview.vue'
import GeneratedArtifactCompare from '@/components/GeneratedArtifactCompare.vue'
import ProductionReleaseDelivery from '@/components/ProductionReleaseDelivery.vue'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import { apiErrorSummary, apiErrorPresentation } from '@/services/apiErrorPresentation'
import type { ArtifactSelectionProtection, ArtifactSelectionConfirmed } from '@/services/artifactSelectionCommand'
import { presentPlatformStatus, productionRecipeInteractionMode, productionRecipeScriptMode } from '@/services/platformAdapters'
import { platformApi } from '@/services/platformApi'
import { plannedAverageShotDurationMs } from '@/services/productionRunPayload'
import { isDefinitiveCreationRejection, snapshotProductionRunInput } from '@/services/productionOnboarding'
import { productionWorkspaceSections, workspaceSection, workspaceSectionIncludes, workspaceStageSection, workspaceArtifactSection, type ProductionWorkspaceSection } from '@/services/productionWorkspacePresentation'
import { mediaKindLabel, modeLabel, operationLabel, reasonCodeLabel, stageLabel } from '@/services/presentationLabels'
import RecipeBibleReview from '@/components/RecipeBibleReview.vue'
import { parseBibleImageReviewPayload } from '@/services/productionRunReview'
import { productionIssuePresentation, productionRunCanRetry, productionRunBelongsToProject, productionRunRestoreTarget, productionRunUnstartedLabel, productionModeLabel, stageIssuePresentation } from '@/services/productionRunPresentation'
import { reconcileExpandedStageId } from '@/services/productionStageDisclosure'
import { repository, type ProductionCapability } from '@/services/repository'
import RecipeStoryboardReferences from '@/components/RecipeStoryboardReferences.vue'
import type {RecipeReferenceReview} from '@/services/recipeStoryboardReferenceReview'
import type { GeneratedArtifact, GenerationAttempt, JsonObject, ProductionRecipe, ProductionRecipeScriptClassification, ProductionRun, ProductionRunCreateInput, ScriptRevision, ScriptSource, UncertainResolutionOutcome } from '@/services/platformTypes'
import { useAuthStore } from '@/stores/auth'
import { useStudioStore } from '@/stores/studio'
import type { Asset, BibleImageLatestCandidate } from '@/types'

const recordedCostSummaryNote = '已记录实际仅汇总已确认金额；0不表示免费，未结算费用请到调用详情核对。'
const route = useRoute()
const router = useRouter()
const studio = useStudioStore()
const auth = useAuthStore()
const projectId = computed(() => String(route.params.projectId || ''))
const runId = computed(() => String(route.params.runId || ''))
const isScriptManagement = computed(() => !runId.value && route.query.view === 'scripts')
const project = computed(() => studio.projectById(projectId.value))
const loading = ref(false)
const pageReadFailed = ref(false)
const projectInputsProtected = ref(false)
const workspaceInputDisabled = computed(() => manualWorkbenchProtected.value || recipeShotSelectionProtected.value || Boolean(actionBusy.value || pendingContentRefresh.value || pendingRunReceipt.value || artifactSelectionProtection.value))
const productionTab = ref<ProductionWorkspaceSection>(workspaceSection(route.query.step))
watch(() => route.query.step, step => { productionTab.value = workspaceSection(step) })
const manualSection = ref<ProductionWorkspaceSection>('script')
const activeProductionSection = computed(() => productionWorkspaceSections.find(row => row.key === productionTab.value)!)
const visibleStages = computed(() => (currentRun.value?.stages || []).filter(stage => productionTab.value === 'records' || workspaceSectionIncludes(productionTab.value,stage.stageKey)))
const currentStageVisible = computed(() => Boolean(currentStage.value && workspaceStageSection(currentStage.value.stageKey) === productionTab.value))
const inputSection = (step: string): 'overview'|'bible'|'episodes' => step === 'bible' ? 'bible' : step === 'storyboard' ? 'episodes' : 'overview'
const hasProjectInputs = (step: string) => ['visual','bible','storyboard'].includes(step)
const currentStyle = computed(() => studio.styles.find(item => item.id === project.value?.styleId))
const manualWorkbenchProtected = ref(false), recipeShotSelectionProtected = ref(false)
const artifactSelectionProtection = ref<ArtifactSelectionProtection | null>(null)
const manualContextProtected = computed(() => projectInputsProtected.value || manualWorkbenchProtected.value || recipeShotSelectionProtected.value || Boolean(artifactSelectionProtection.value))
function protectArtifactSelection(value: ArtifactSelectionProtection) {
  if (value.runId !== runId.value) return
  const previous = artifactSelectionProtection.value
  if (value.active && (!previous || previous.key === value.key)) artifactSelectionProtection.value = value
  else if (!value.active && previous?.key === value.key && previous.runId === value.runId && previous.scopeId === value.scopeId)
    artifactSelectionProtection.value = null
}
const actionBusy = ref('')
const pendingRunReceipt = ref<{ runId: string; projectId: string; action: string; stageKey: string } | null>(null)
const receiptReadBusy = ref(false)
const reviewRulesReady = ref(true)
const pendingContentRefresh = shallowRef<{
  projectId: string; runId: string; loadSequence: number; session: number; sequence: number; successNotice: string
} | null>(null)
const runActionBlocked = computed(() => Boolean(actionBusy.value || loading.value || pageReadFailed.value || !reviewRulesReady.value || pendingRunReceipt.value || pendingContentRefresh.value || manualContextProtected.value))
const error = ref('')
const notice = ref('')
const sources = ref<ScriptSource[]>([])
const revisions = ref<ScriptRevision[]>([])
const recipes = ref<ProductionRecipe[]>([])
const runs = ref<ProductionRun[]>([])
const currentRun = ref<ProductionRun | null>(null)
const creationSource = ref<'recipe' | 'manual'>(route.query.source === 'manual' ? 'manual' : 'recipe')
const creationRequest = ref<ProductionRunCreateInput | null>(null)
const creationResult = ref<ProductionRun | null>(null)
const manualRunCreated = async (run: ProductionRun) => {
  const expectedProjectId = projectId.value, sequence = productionLoadSequence, session = studio.captureSessionEpoch()
  if (loading.value || runId.value || route.query.view !== 'new' || creationSource.value !== 'manual') return
  if (!run.id || run.project !== expectedProjectId || run.planSource !== 'manual') { error.value = '创建回执与原项目或制作方式不一致，请查看原项目制作记录。'; return }
  try {
    const failure = await router.replace({ name: 'production-run-detail', params: { projectId: expectedProjectId, runId: run.id } })
    if (failure) throw new Error('详情导航未完成，请打开已创建的原记录，不要重新创建。')
  } catch (cause) { if (sequence === productionLoadSequence && projectId.value === expectedProjectId && session === studio.captureSessionEpoch()) error.value = `运行已创建：${message(cause)}` }
}
const productionCapability = ref<ProductionCapability | null>(null)
const productionCapabilityError = ref('')
const productionCapabilityLoading = ref(false)
let capabilityReadSequence = 0
let runRefreshPending = 0
const scriptMode = ref<'paste' | 'file' | 'library' | 'existing'>('paste')
const scriptText = ref('')
const scriptFormat = ref('markdown')
const scriptFile = ref<File | null>(null)
const scriptAssets = ref<Asset[]>([])
const selectedScriptAssetId = ref('')
const previewRevision = ref<ScriptRevision | null>(null)
const selectedRevisionId = ref('')
const selectedRecipeId = ref('')
let recipeCatalogInitialized = false
let recipeDefaultsInitialized = false
const projectSourceConfirmations=ref<RecipeProjectSourceConfirmation[]>([]), projectSourceReady=ref(false)
const episodeCountHint = ref<number | null>(null)
const allowExternalCalls = ref(false)
const configOverridesText = ref('{}')
const idempotencyKey = ref(crypto.randomUUID())
const aiIdea = ref('')
const aiStyleHint = ref('')
const shotsPerEpisode = ref<number | null>(null)
const episodeDurationSeconds = ref<number | null>(null)
const aiRunPlan = computed<{
  episodeCount: number
  shotsPerEpisode: number
  episodeDurationSeconds: number
} | null>(() => {
  const values = [
    episodeCountHint.value,
    shotsPerEpisode.value,
    episodeDurationSeconds.value,
  ]
  if (!values.every((value) => Number.isSafeInteger(value) && Number(value) > 0)) return null
  return {
    episodeCount: episodeCountHint.value as number,
    shotsPerEpisode: shotsPerEpisode.value as number,
    episodeDurationSeconds: episodeDurationSeconds.value as number,
  }
})
const aiPlannedAverageShotDurationMs = computed(() => {
  const plan = aiRunPlan.value
  return plan
    ? plannedAverageShotDurationMs(plan.shotsPerEpisode, plan.episodeDurationSeconds)
    : null
})
const selectedEpisodeIds = ref<string[]>([])
const unattendedConfirmed = ref(false)
const approvalDecision = ref('approve')
const approvalComment = ref('')
const bibleImageSelectionSignature = ref('')
const bibleImageSelections = ref<Record<string, BibleImageReviewSelection>>({})
const bibleImageHistoryByArtifactId = ref<Record<string, BibleImageLatestCandidate>>({})
const bibleImageHistoryLoading = ref(false)
const bibleImageHistoryError = ref('')
const storyboardReferenceReview = ref<RecipeReferenceReview | null>(null)
const storyboardReferenceError = ref('请读取当前确切素材选择')
const storyboardReviewRefreshEpoch = ref(0)
const protectRecipeShotSelection = (value: boolean) => {
  const released = recipeShotSelectionProtected.value && !value
  recipeShotSelectionProtected.value = value
  if (released) storyboardReviewRefreshEpoch.value++
  if (value) storyboardReferenceReview.value = null
}
const approvedEstimatedCost = ref('')
const authorizationCurrency = ref('')
const authorizationExternal = ref(false)
const expandedStageId = ref('')
const stageDisclosureUserSelected = ref(false)
interface PendingRunConfirmation {
  action: 'retry' | 'cancel'
  runId: string
  runStatus: string
  blockedCode: string
  uncertainRemote: boolean
  stageId: string
  stageKey: string
  retryContext: ProductionRun['retryContext']
  estimatedCost: string
  committedCost: string
  actualCost: string
  currency: string
}
const pendingRunConfirmation = ref<PendingRunConfirmation | null>(null)
const pendingRunConfirmationError = ref('')
interface PendingBibleRegeneration {
  requestId: string
  runId: string
  runStatus: string
  stageId: string
  snapshotSha256: string
  entityType: 'character' | 'scene' | 'prop'
  entityId: string
  entityName: string
  existingCandidateCount: number
}
const pendingBibleRegeneration = ref<PendingBibleRegeneration | null>(null)
const pendingBibleRegenerationError = ref('')
const uncertainAttempts = ref<GenerationAttempt[]>([])
const uncertainAttemptsLoading = ref(false)
const uncertainAttemptsError = ref('')
const uncertainResolutionAttempt = ref<GenerationAttempt | null>(null)
const uncertainResolutionSaving = computed(() => actionBusy.value === 'resolve')
const uncertainResolutionError = ref('')
const uncertainResolutionOutcome = ref<UncertainResolutionOutcome>('confirmed_not_submitted')
const uncertainResolutionNote = ref('')
const uncertainResolutionProviderRequestId = ref('')
const uncertainResolutionActualCost = ref('')
const uncertainResolutionCurrency = ref('')
let uncertainLoadSequence = 0
let bibleHistoryLoadSequence = 0
let productionLoadSequence = 0
let scriptReadSequence = 0
let scriptActionSequence = 0
let scriptInputRevision = 0
let runRefreshSequence = 0
watch(() => manualContextProtected.value || Boolean(actionBusy.value), protectedContext => {
  if (protectedContext) runRefreshSequence++
}, { flush: 'sync' })
let runActionSequence = 0
let contentRefreshSequence = 0
let activeProjectScope = ''
let pollTimer: number | undefined

const terminal = new Set(['succeeded', 'failed', 'cancelled'])
const costGateCodes = new Set(['unknown_cost', 'external_call_not_authorized', 'cost_currency_missing',
  'execution_external_authorization_required', 'actual_budget_exceeded'])
const message = (cause: unknown) => apiErrorSummary(cause, '服务端请求失败')
const dateTime = (value: string) => value ? new Date(value).toLocaleString('zh-CN') : '—'
// Catalog membership is not a selection: only the visible, explicitly read
// revision may enter a new run. Editing its source invalidates this preview.
const selectedRevision = computed(() => previewRevision.value?.id === selectedRevisionId.value && previewRevision.value?.content?.trim() ? previewRevision.value : null)
const projectEpisodes = computed(() => project.value?.episodes || [])
const currentStage = computed(() => currentRun.value?.stages?.find((item) => item.stageKey === currentRun.value?.currentStageKey))
const currentStageSources = computed(() => {
  const stage = recipeRows(recipeObject(recipeObject(currentRun.value?.executionPlan?.value.recipe).definition).stages)
    .find(row => row.key === currentStage.value?.stageKey)
  return recipeRows(recipeObject(stage?.routing).routes).map((row,index) => ({index,sources:recipeObject(row.input_sources)}))
})
const manualPreparationStageId = ref('')
const selectedManualPreparationStage = computed(() => currentRun.value?.stages?.find(stage => stage.id === manualPreparationStageId.value))
const bibleConfirmation = ref<JsonObject | null>(null)
const selectedRecipe = computed(() => recipes.value.find((item) => item.id === selectedRecipeId.value))
const recipeSelectionIssue = computed(() => {
  if (loading.value || pageReadFailed.value || selectedRecipe.value) return ''
  if (!recipes.value.length) return '暂无可见的已发布配方，请到系统设置完成配方配置与发布，或选择无配方人工制作。'
  return selectedRecipeId.value ? '所选配方已不可用，请重新选择。' : '请先选择一个已发布配方。'
})
const selectedScriptMode = computed<ProductionRecipeScriptClassification>(() => productionRecipeScriptMode(selectedRecipe.value?.definition || {}))
const isUnattendedRecipe = computed(() => (
  productionRecipeInteractionMode(selectedRecipe.value?.definition || {}) === 'unattended'
))
const recipeContractError = computed(() => selectedRecipe.value ? recipeExecutionOverview(selectedRecipe.value.definition).error : '')
const isAiScriptRecipe = computed(() => selectedScriptMode.value === 'ai')
const isConfirmedScriptRecipe = computed(() => selectedScriptMode.value === 'confirmed_revision')
const isDownstreamRecipe = computed(() => selectedScriptMode.value === 'downstream')
const isInvalidRecipe = computed(() => Boolean(selectedRecipe.value) && selectedScriptMode.value === 'invalid')
const unattendedCreationError = computed(() => isUnattendedRecipe.value && !unattendedConfirmed.value
  ? '请明确确认由配方冻结的审核策略继续自动生产。' : '')
const costGate = computed(() => costGateCodes.has(currentRun.value?.blockedCode || ''))
const isUncertainRemote = computed(() => currentRun.value?.blockedCode === 'uncertain_remote_response' || currentStage.value?.reasonCode === 'uncertain_remote_response')
const needsAttemptResolution = computed(() => ['uncertain_remote_response', 'actual_cost_reconciliation_required'].some((code) => currentRun.value?.blockedCode === code || currentStage.value?.reasonCode === code))
const bibleImageReview = computed(() => parseBibleImageReviewPayload(currentRun.value?.stages || []))
const blockedPresentation = computed(() => productionIssuePresentation(
  currentRun.value?.blockedCode || currentRun.value?.status || '',
  currentRun.value?.blockedReason,
  currentRun.value?.status || '',
))
const canCreate = computed(() => Boolean(projectSourceReady.value && !loading.value && !pageReadFailed.value && !runId.value && route.query.view === 'new' && creationSource.value === 'recipe' && !creationRequest.value && !creationResult.value && productionCapability.value && selectedRecipe.value && !actionBusy.value && !isInvalidRecipe.value && !recipeContractError.value && !unattendedCreationError.value && (
  isAiScriptRecipe.value
    ? projectEpisodes.value.length === 0 && Boolean(aiIdea.value.trim() && aiRunPlan.value)
    : isConfirmedScriptRecipe.value
      ? selectedRevision.value?.status === 'confirmed'
      : projectEpisodes.value.length > 0 && selectedEpisodeIds.value.length > 0
)))
const uncertainResolutionOptions = computed<Array<{ value: UncertainResolutionOutcome; label: string }>>(() => {
  const attempt = uncertainResolutionAttempt.value
  if (!attempt) return []
  if (attempt.errorCode === 'actual_cost_reconciliation_required') return [{ value: 'actual_cost_confirmed', label: '已核对实际费用与产物' }]
  const values: Array<{ value: UncertainResolutionOutcome; label: string }> = [
    { value: 'confirmed_not_submitted', label: '确认远端未提交' },
    { value: 'confirmed_remote_failed', label: '确认远端失败' },
  ]
  if (['image.text_to_image', 'video.text_to_video'].includes(attempt.operation)) values.splice(1, 0, { value: 'confirmed_remote_submitted', label: '确认远端已提交并恢复轮询' })
  return values
})
const uncertainResolutionValidation = computed(() => {
  const attempt = uncertainResolutionAttempt.value
  if (!attempt) return '没有待消歧 attempt。'
  const note = uncertainResolutionNote.value.trim()
  const outcome = uncertainResolutionOutcome.value
  const costText = uncertainResolutionActualCost.value.trim()
  const cost = costText === '' ? null : Number(costText)
  const limits = productionCapability.value?.effectiveLimits
  if (!limits) return productionCapabilityError.value || '服务端审计说明限制不可用，不能提交消歧结论。'
  if (note.length < limits.auditNoteMinCharacters || note.length > limits.auditNoteMaxCharacters) return `消歧说明必须为 ${limits.auditNoteMinCharacters}–${limits.auditNoteMaxCharacters} 个字符。`
  if (outcome === 'confirmed_remote_submitted' && !uncertainResolutionProviderRequestId.value.trim()) return '确认远端已提交时必须填写 Provider request ID。'
  if ((outcome === 'confirmed_remote_failed' || outcome === 'actual_cost_confirmed') && (cost === null || !Number.isFinite(cost) || cost < 0)) return '该结果必须填写有限、非负的实际费用（可填 0）。'
  if (cost !== null && cost > 0 && !uncertainResolutionCurrency.value.trim()) return '实际费用大于 0 时必须填写币种。'
  return ''
})
const artifactGroups = computed(() => {
  const groups = new Map<string, GeneratedArtifact[]>()
  const bibleStageIds = new Set((currentRun.value?.stages || []).filter((stage) => stage.stageKey === 'bible_image_generation').map((stage) => stage.id))
  for (const item of currentRun.value?.artifacts || []) {
    if (bibleStageIds.has(item.stageRun) && currentRun.value?.planSource !== 'recipe') continue
    const mediaKind = item.kind === 'image' || item.kind === 'video' || item.kind === 'assembly' || item.mimeType.startsWith('image/') || item.mimeType.startsWith('video/')
    if (!mediaKind) continue
    const key = item.variantGroup || `artifact:${item.id}`
    groups.set(key, [...(groups.get(key) || []), item])
  }
  return [...groups.entries()].map(([key, items]) => ({ key, materialSelection: bibleStageIds.has(items[0]!.stageRun), items: items.sort((a, b) => a.variantIndex - b.variantIndex) }))
})
const artifactGroupSection = (group: typeof artifactGroups.value[number]): ProductionWorkspaceSection => workspaceArtifactSection(group.items[0]!, currentRun.value?.stages)
const bibleEntityKey = (entityType: string, entityId: string) => `${entityType}:${entityId}`
const eventChecked = (event: Event) => (event.target as HTMLInputElement).checked
const eventValue = (event: Event) => (event.target as HTMLInputElement | HTMLSelectElement).value
const bibleImageBindingsPayload = () => reviewedBibleImageBindings(bibleImageReview.value.value?.entities || [],
  bibleImageSelections.value, studio.assets, bibleImageHistoryByArtifactId.value,
  productionCapability.value?.effectiveLimits.maxBibleReferenceAssetsPerEntity ?? 0)
const internalNamePlaceholder = /^(?:S[1-9][0-9]*|(?:character|scene|prop):[a-z]+-[0-9a-f]+)$/i
const reviewApprovalError = computed(() => {
  if (currentStage.value?.stageKey === 'bible_review' && !bibleConfirmation.value) return '请先读取并逐项核对完整 Bible 声明、主图与变体设定。'
  if (currentStage.value?.stageKey === 'bible_image_review') {
    if (bibleImageReview.value.error) return bibleImageReview.value.error
    if (!productionCapability.value) return productionCapabilityError.value || '正在读取参考图审核数量限制。'
    if (bibleImageHistoryLoading.value) return '正在读取本项目冻结候选对应的历史预览与完整性证据。'
    if (bibleImageHistoryError.value) return bibleImageHistoryError.value
    const payload = bibleImageReview.value.value
    if (!payload) return 'Bible 参考图审核证据不可用。'
    try { bibleImageBindingsPayload() } catch (cause) { return message(cause) }
  }
  if (currentStage.value?.stageKey === 'storyboard_review') {
    if (storyboardReferenceError.value) return storyboardReferenceError.value
    if (!storyboardReferenceReview.value) return '请重新读取当前确切素材选择后再批准。'
  }
  return ''
})
const costAuthorizationError = computed(() => {
  const code = currentRun.value?.blockedCode || ''
  const estimate = approvedEstimatedCost.value.trim()
  const currency = authorizationCurrency.value.trim().toUpperCase()
  const decimal = /^(?:0|[1-9]\d*)(?:\.\d{1,4})?$/
  if (['external_call_not_authorized', 'execution_external_authorization_required'].includes(code) && !authorizationExternal.value) return '当前阻断要求明确允许本轮外部模型调用。'
  if (code === 'unknown_cost' && (!estimate || !decimal.test(estimate))) return '未知费用阶段必须填写批准的阶段预估费用。'
  if ((code === 'unknown_cost' || code === 'cost_currency_missing' || Boolean(estimate)) && !/^[A-Z][A-Z0-9]{2,11}$/.test(currency)) return '请填写 3–12 位大写费用币种代码。'
  return ''
})

const resetProjectScopedState = (nextProjectId: string) => {
  creationSource.value = route.query.source === 'manual' ? 'manual' : 'recipe'
  activeProjectScope = nextProjectId
  scriptReadSequence += 1
  scriptActionSequence += 1
  scriptInputRevision += 1
  runRefreshSequence += 1
  uncertainLoadSequence += 1
  storyboardReferenceReview.value = null
  storyboardReviewRefreshEpoch.value++
  bibleHistoryLoadSequence += 1
  sources.value = []
  revisions.value = []
  scriptAssets.value = []
  selectedScriptAssetId.value = ''
  previewRevision.value = null
  selectedRevisionId.value = ''
  recipes.value = []
  selectedRecipeId.value = ''
  recipeCatalogInitialized = false
  recipeDefaultsInitialized = false
  episodeCountHint.value = null
  shotsPerEpisode.value = null
  episodeDurationSeconds.value = null
  aiIdea.value = ''
  aiStyleHint.value = ''
  configOverridesText.value = '{}'
  allowExternalCalls.value = false
  projectSourceConfirmations.value = []
  projectSourceReady.value = false
  scriptText.value = ''
  scriptFile.value = null
  runs.value = []
  currentRun.value = null
  selectedEpisodeIds.value = []
  unattendedConfirmed.value = false
  expandedStageId.value = ''
  pendingRunConfirmation.value = null
  pendingBibleRegeneration.value = null
  pendingBibleRegenerationError.value = ''
  actionBusy.value = ''
  idempotencyKey.value = crypto.randomUUID()
}
const readScripts = async (expectedProjectId = projectId.value) => {
  const sequence = ++scriptReadSequence
  const session = studio.captureSessionEpoch(), loadSequence = productionLoadSequence
  const sourceRows = await platformApi.listScriptSources(expectedProjectId)
  const details = await Promise.all(sourceRows.filter((item) => item.latestRevisionId).map((item) => platformApi.getScriptRevision(item.latestRevisionId)))
  if (sequence !== scriptReadSequence || session !== studio.captureSessionEpoch() || loadSequence !== productionLoadSequence
    || projectId.value !== expectedProjectId || activeProjectScope !== expectedProjectId) return false
  sources.value = sourceRows
  revisions.value = details.sort((a, b) => b.revision - a.revision)
  if (!revisions.value.some((item) => item.id === selectedRevisionId.value)) {
    selectedRevisionId.value = ''
    previewRevision.value = null
  }
  return true
}
const loadBibleImageHistory = async () => {
  const sequence = ++bibleHistoryLoadSequence
  bibleImageHistoryByArtifactId.value = {}
  bibleImageHistoryError.value = ''
  if (currentStage.value?.stageKey !== 'bible_image_review' || bibleImageReview.value.error) return
  bibleImageHistoryLoading.value = true
  try {
    const [result] = await Promise.all([
      repository.getBibleImageCandidates(projectId.value),
      studio.refreshAssets(),
    ])
    if (sequence !== bibleHistoryLoadSequence || currentStage.value?.stageKey !== 'bible_image_review') return
    bibleImageHistoryByArtifactId.value = Object.fromEntries(
      result.candidates.flatMap((entity) => entity.candidateHistory).map((candidate) => [candidate.artifactId, candidate]),
    )
  } catch (cause) {
    if (sequence === bibleHistoryLoadSequence) bibleImageHistoryError.value = `参考图候选或素材库读取失败：${message(cause)}`
  } finally {
    if (sequence === bibleHistoryLoadSequence) bibleImageHistoryLoading.value = false
  }
}
const load = async () => {
  if (actionBusy.value || (manualContextProtected.value && currentRun.value?.id === runId.value)) return
  pendingContentRefresh.value = null
  const expectedProjectId = projectId.value
  const expectedRunId = runId.value
  if (activeProjectScope !== expectedProjectId) resetProjectScopedState(expectedProjectId)
  const sequence = ++productionLoadSequence
  const session = studio.captureSessionEpoch()
  runRefreshSequence += 1
  const isCurrent = () => sequence === productionLoadSequence
    && session === studio.captureSessionEpoch()
    && projectId.value === expectedProjectId
    && runId.value === expectedRunId
    && activeProjectScope === expectedProjectId
  loading.value = true
  pageReadFailed.value = false
  error.value = ''
  notice.value = ''
  currentRun.value = null
  productionCapability.value = null
  productionCapabilityError.value = ''
  productionCapabilityLoading.value = false
  capabilityReadSequence += 1
  try {
    // Existing work needs its own project and run, not the creation catalog.
    const [loadedProject, loadedRun, runRows] = await Promise.all([
      // Keep the adapter's style-ID directory dependency, but do not wait
      // for navigation summaries or task history before starting the run read.
      studio.loadStyles().then(() => isCurrent() ? studio.loadProject(expectedProjectId, true) : null),
      expectedRunId ? platformApi.getProductionRun(expectedRunId) : Promise.resolve(null),
      !expectedRunId && route.query.view !== 'new' && !isScriptManagement.value
        ? platformApi.listProductionRuns(expectedProjectId) : Promise.resolve([]),
    ])
    if (!isCurrent()) return
    if (!loadedProject || loadedProject.id !== expectedProjectId) throw new Error('未能读取当前项目，请重新读取。')
    if (expectedRunId) {
      if (!loadedRun || loadedRun.id !== expectedRunId || !productionRunBelongsToProject(loadedRun, expectedProjectId)) throw new Error('返回的 ProductionRun 与当前项目或运行不一致，已拒绝显示或操作。')
      currentRun.value = loadedRun
      reviewRulesReady.value = true
      pendingRunReceipt.value = null
      pendingRunConfirmation.value = null
      pendingBibleRegeneration.value = null
      return
    }
    runs.value = runRows
    if (isScriptManagement.value) {
      // Script import/field editing is project work, not recipe/video admission.
      const [libraryScripts] = await Promise.all([
        repository.getAssets({ type: 'script' }), readScripts(expectedProjectId),
      ])
      if (!isCurrent()) return
      scriptAssets.value = libraryScripts.filter(asset => asset.isActive === true && asset.backendKind === 'script')
      return
    }
    const restoredRunId = productionRunRestoreTarget(runRows, expectedRunId, route.query.view)
    if (restoredRunId) {
      await router.replace({ name: 'production-run-detail', params: { projectId: expectedProjectId, runId: restoredRunId }, query: typeof route.query.step==='string'?{step:route.query.step}:{} })
      return
    }
    if (route.query.view !== 'new' || creationSource.value === 'manual') return
    if (creationRequest.value) return
    const capabilityRequest = repository.getProductionCapability(expectedProjectId)
      .then((value) => ({ value, error: '' }))
      .catch((cause) => ({ value: null, error: message(cause) }))
    const [recipeRows, libraryScripts, capability] = await Promise.all([
      platformApi.listRecipes('published'),
      repository.getAssets({ type: 'script' }),
      capabilityRequest,
      readScripts(expectedProjectId),
    ])
    if (!isCurrent()) return
    recipes.value = recipeRows
    scriptAssets.value = libraryScripts.filter((asset) => asset.isActive === true && asset.backendKind === 'script')
    productionCapability.value = capability.value
    productionCapabilityError.value = capability.error
    // Refresh current contracts without replacing this project's local draft.
    // A deliberate empty value or a vanished selection is not a default request.
    if (!recipeDefaultsInitialized && capability.value) {
      episodeCountHint.value = capability.value.creationDefaults.episodeCount
      shotsPerEpisode.value = capability.value.creationDefaults.shotsPerEpisode
      episodeDurationSeconds.value = capability.value.creationDefaults.episodeDurationSeconds
      recipeDefaultsInitialized = true
    }
    if (!recipeCatalogInitialized) {
      selectedRecipeId.value = recipeRows[0]?.id || ''
      selectedEpisodeIds.value = projectEpisodes.value.map((item) => item.id)
      recipeCatalogInitialized = true
    }
  } catch (cause) {
    if (isCurrent()) { error.value = message(cause); pageReadFailed.value = true }
  } finally {
    if (isCurrent()) loading.value = false
  }
}
const loadReviewCapability = async () => {
  if (productionCapabilityLoading.value || productionCapability.value) return
  const sequence = ++capabilityReadSequence
  const expectedProjectId = projectId.value
  const expectedRunId = runId.value
  const session = studio.captureSessionEpoch()
  const isCurrent = () => sequence === capabilityReadSequence && expectedProjectId === projectId.value
    && expectedRunId === runId.value && session === studio.captureSessionEpoch()
  productionCapabilityLoading.value = true
  productionCapabilityError.value = ''
  try {
    const result = await repository.getProductionCapability(expectedProjectId)
    if (isCurrent()) productionCapability.value = result
  } catch (cause) { if (isCurrent()) productionCapabilityError.value = message(cause) }
  finally { if (isCurrent()) productionCapabilityLoading.value = false }
}
watch(() => Boolean(currentRun.value?.planSource === 'recipe'
  && (currentStage.value?.stageKey === 'bible_image_review' || needsAttemptResolution.value)), needed => {
  if (needed) void loadReviewCapability()
})
const currentReviewRules = computed(() => project.value && reviewRulesReady.value ? {
  negative_prompt: project.value.bible.negativePrompt, color_script: project.value.bible.colorScript,
  continuity_summary: project.value.continuitySummary,
} : {})
let contentRead: Promise<void> | null = null
const flushSelectedContentRefresh = (): Promise<void> | undefined => {
  if (contentRead) return contentRead
  const pending = pendingContentRefresh.value
  if (!pending) return
  const isCurrent = () => pendingContentRefresh.value === pending
    && pending.sequence === contentRefreshSequence && pending.loadSequence === productionLoadSequence
    && pending.session === studio.captureSessionEpoch()
    && projectId.value === pending.projectId && runId.value === pending.runId
  if (!isCurrent()) { pendingContentRefresh.value = null; return }
  if (manualContextProtected.value || actionBusy.value || loading.value) return
  const readSequence = ++runRefreshSequence
  const read = async () => {
    try {
      // Own a fresh pair of reads. A poll's skipped/stale boolean is not a receipt.
      // Settle both before retrying so Pinia cannot reuse this in-flight project read.
      const [runRead, projectRead] = await Promise.allSettled([
        platformApi.getProductionRun(pending.runId), studio.loadProject(pending.projectId, true),
      ])
      if (!isCurrent()) return
      const failures = [runRead, projectRead].filter(result => result.status === 'rejected')
      if (failures.length) throw new Error(failures.map(result => message(result.reason)).join('；'))
      if (runRead.status !== 'fulfilled' || projectRead.status !== 'fulfilled') return
      const refreshed = runRead.value
      if (!refreshed || refreshed.id !== pending.runId || !productionRunBelongsToProject(refreshed, pending.projectId)) {
        throw new Error('返回的 ProductionRun 与当前项目或运行不一致，已拒绝显示或操作。')
      }
      if (!projectRead.value || projectRead.value.id !== pending.projectId) throw new Error('原项目事实未能完整读取。')
      // Opening a new editor invalidates successful reads, not the committed adoption.
      // Keep its mounted draft and reread once the protection is released.
      if (readSequence !== runRefreshSequence || manualContextProtected.value || actionBusy.value) return
      currentRun.value = refreshed
      pendingContentRefresh.value = null
      pageReadFailed.value = false
      reviewRulesReady.value = true
      error.value = ''
      notice.value = pending.successNotice
    } catch (cause) {
      if (isCurrent()) {
        pendingContentRefresh.value = null
        // Keep the mounted editor; stale rules still block further approval.
        error.value = `选择或采用回执已确认，但原运行或项目内容刷新失败：${message(cause)}；请重新读取，不要重复提交。`
      }
    }
  }
  contentRead = read().finally(() => {
    contentRead = null
    void flushSelectedContentRefresh()
  })
  return contentRead
}
watch([manualContextProtected, actionBusy, loading], () => { void flushSelectedContentRefresh() })
const refreshSelectedContent = async (successNotice = '') => {
  if (!runId.value || currentRun.value?.id !== runId.value) return
  pendingContentRefresh.value = {
    projectId: projectId.value, runId: runId.value, loadSequence: productionLoadSequence,
    session: studio.captureSessionEpoch(), sequence: ++contentRefreshSequence, successNotice,
  }
  runRefreshSequence++ // Invalidate any pre-adoption poll, even while an editor delays these reads.
  reviewRulesReady.value = false
  notice.value = ''
  // Child approval emits changed before its finally releases the busy event.
  await nextTick()
  await flushSelectedContentRefresh()
}
const refreshAdoptedContent = () => refreshSelectedContent()
const refreshRun = async () => {
  const expectedRunId = runId.value
  const expectedProjectId = projectId.value
  if (!expectedRunId || actionBusy.value || manualContextProtected.value || pendingContentRefresh.value) return false
  const sequence = ++runRefreshSequence
  const loadSequence = productionLoadSequence
  const session = studio.captureSessionEpoch()
  const isCurrent = () => sequence === runRefreshSequence
    && loadSequence === productionLoadSequence
    && session === studio.captureSessionEpoch()
    && runId.value === expectedRunId
    && projectId.value === expectedProjectId
    && activeProjectScope === expectedProjectId
  runRefreshPending += 1
  try {
    const refreshed = await platformApi.getProductionRun(expectedRunId)
    if (!isCurrent()) return false
    if (refreshed.id !== expectedRunId || !productionRunBelongsToProject(refreshed, expectedProjectId)) throw new Error('返回的 ProductionRun 与当前项目或运行不一致，已拒绝显示或操作。')
    currentRun.value = refreshed
    return true
  } catch (cause) {
    if (isCurrent()) {
      // A failed recovery read does not erase the last known facts or authorize replay.
      if (!pendingRunReceipt.value) { currentRun.value = null; pageReadFailed.value = true }
      error.value = message(cause)
    }
    return false
  }
  finally { runRefreshPending -= 1 }
}
const recoverRunReceipt = async () => {
  const pending = pendingRunReceipt.value
  if (!pending || actionBusy.value || loading.value || runRefreshPending || receiptReadBusy.value) return
  const sequence = productionLoadSequence
  const session = studio.captureSessionEpoch()
  const sameContext = () => sequence === productionLoadSequence
    && session === studio.captureSessionEpoch() && runId.value === pending.runId && projectId.value === pending.projectId
  const isCurrent = () => pendingRunReceipt.value === pending && sameContext()
  receiptReadBusy.value = true
  try {
    if (!await refreshRun() || !isCurrent()) return
    const refreshedProject = await studio.loadProject(pending.projectId, true)
    if (!isCurrent()) return
    if (!refreshedProject || refreshedProject.id !== pending.projectId) throw new Error('原项目事实未能读取。')
    pendingRunReceipt.value = null
    pendingRunConfirmation.value = null
    pendingRunConfirmationError.value = ''
    pendingBibleRegeneration.value = null
    pendingBibleRegenerationError.value = ''
    error.value = ''
    notice.value = '已重新读取原运行与项目事实。未重放提交；请核对实际状态后重新选择操作，读取成功不代表上次操作成功。'
  } catch (cause) { if (isCurrent()) error.value = `原运行已读取，但项目事实刷新失败：${message(cause)}` }
  finally { if (sameContext()) receiptReadBusy.value = false }
}
const loadUncertainAttempts = async () => {
  const sequence = ++uncertainLoadSequence
  const stageId = currentStage.value?.id || ''
  uncertainAttempts.value = []
  uncertainAttemptsError.value = ''
  if (!needsAttemptResolution.value || !stageId) return
  uncertainAttemptsLoading.value = true
  try {
    const rows = await platformApi.listGenerationAttempts({ stage_run: stageId })
    if (sequence !== uncertainLoadSequence || currentStage.value?.id !== stageId) return
    uncertainAttempts.value = rows.filter((item) => ['uncertain_remote_response', 'actual_cost_reconciliation_required'].includes(item.errorCode))
    if (!uncertainAttempts.value.length) uncertainAttemptsError.value = '服务端没有返回与当前阻断对应的安全 attempt 投影；不能人工猜测或重试。'
  } catch (cause) {
    if (sequence === uncertainLoadSequence) uncertainAttemptsError.value = message(cause)
  } finally {
    if (sequence === uncertainLoadSequence) uncertainAttemptsLoading.value = false
  }
}
const schedulePoll = () => {
  if (pollTimer) window.clearInterval(pollTimer)
  pollTimer = undefined
  if (currentRun.value && !terminal.has(currentRun.value.status)) pollTimer = window.setInterval(() => {
    if (!loading.value && !runRefreshPending && !actionBusy.value && !pendingRunReceipt.value && !manualContextProtected.value) void refreshRun()
  }, 6000)
}
watch(() => currentRun.value?.status, schedulePoll)
watch(selectedRecipeId, () => { unattendedConfirmed.value = false })
watch([scriptMode, scriptText, scriptFormat, scriptFile, selectedScriptAssetId], () => {
  scriptInputRevision++
  selectedRevisionId.value = ''
  previewRevision.value = null
}, { flush: 'sync' })
watch(selectedRevisionId, () => { previewRevision.value = null }, { flush: 'sync' })
watch(() => selectedRecipe.value?.id, () => {
  allowExternalCalls.value = false
})
watch(() => `${currentRun.value?.id || ''}:${currentStage.value?.id || ''}:${(currentRun.value?.stages || []).map((item) => item.id).join(',')}`, (signature, previous) => {
  const runChanged = signature.split(':', 1)[0] !== String(previous || '').split(':', 1)[0]
  if (runChanged) stageDisclosureUserSelected.value = false
  expandedStageId.value = reconcileExpandedStageId(
    currentRun.value?.stages || [],
    currentRun.value?.currentStageKey || '',
    expandedStageId.value,
    stageDisclosureUserSelected.value,
  )
}, { immediate: true })
watch(() => `${currentRun.value?.id || ''}:${currentRun.value?.blockedCode || ''}:${currentStage.value?.id || ''}`, () => {
  authorizationExternal.value = currentRun.value?.allowExternalCalls === true
  authorizationCurrency.value = currentStage.value?.approvedCostCurrency || currentRun.value?.costCurrency || ''
  approvedEstimatedCost.value = currentStage.value?.approvedEstimatedCost || ''
}, { immediate: true })
watch(() => `${currentRun.value?.id || ''}:${currentRun.value?.blockedCode || ''}:${currentStage.value?.id || ''}`, () => { void loadUncertainAttempts() }, { immediate: true })
watch(uncertainResolutionOutcome, (outcome) => {
  uncertainResolutionError.value = ''
  if (outcome === 'confirmed_not_submitted') {
    uncertainResolutionProviderRequestId.value = ''
    uncertainResolutionActualCost.value = ''
    uncertainResolutionCurrency.value = ''
  }
})
watch(() => currentStage.value?.stageKey === 'bible_image_review' && !bibleImageReview.value.error
  ? `${currentRun.value?.id}:${bibleImageReview.value.value?.snapshotSha256}`
  : '', (signature) => {
  if (signature && signature === bibleImageSelectionSignature.value) return
  bibleImageSelectionSignature.value = signature
  bibleImageSelections.value = {}
  bibleImageHistoryByArtifactId.value = {}
  bibleImageHistoryError.value = ''
  if (!signature || !bibleImageReview.value.value) return
  bibleImageSelections.value = Object.fromEntries(
    bibleImageReview.value.value.entities.map((entity) => [
      bibleEntityKey(entity.entityType, entity.entityId),
      initialBibleImageSelection(entity),
    ]),
  )
  void loadBibleImageHistory()
}, { immediate: true })
watch(() => `${currentRun.value?.id || ''}:${currentStage.value?.id || ''}`, () => {
  approvalDecision.value = 'approve'
  approvalComment.value = ''
})
watch(() => [projectId.value, runId.value, route.query.view, route.query.source], () => {
  artifactSelectionProtection.value = null
  runActionSequence += 1
  actionBusy.value = ''
  pendingRunReceipt.value = null
  receiptReadBusy.value = false
  creationRequest.value = null
  creationResult.value = null
  idempotencyKey.value = crypto.randomUUID()
  contentRefreshSequence += 1
  pendingContentRefresh.value = null
  reviewRulesReady.value = true
  pendingRunConfirmation.value = null
  pendingRunConfirmationError.value = ''
  pendingBibleRegeneration.value = null
  pendingBibleRegenerationError.value = ''
  uncertainResolutionAttempt.value = null
  uncertainResolutionError.value = ''
  creationSource.value = route.query.source === 'manual' ? 'manual' : 'recipe'
  void load()
})
onMounted(load)
onBeforeUnmount(() => {
  contentRefreshSequence += 1
  pendingContentRefresh.value = null
  runActionSequence += 1
  capabilityReadSequence += 1
  productionLoadSequence += 1
  scriptReadSequence += 1
  scriptActionSequence += 1
  scriptInputRevision += 1
  runRefreshSequence += 1
  if (pollTimer) window.clearInterval(pollTimer)
})

const chooseFile = (event: Event) => { scriptFile.value = (event.target as HTMLInputElement).files?.[0] || null }
const setStructuredEditBusy = (busy: boolean) => {
  if (busy && !actionBusy.value) actionBusy.value = 'script-fields'
  else if (!busy && actionBusy.value === 'script-fields') actionBusy.value = ''
}
const acceptStructuredRevision = (revision: ScriptRevision, basisId: string) => {
  if (previewRevision.value?.id !== basisId || previewRevision.value.source !== revision.source) return
  selectedRevisionId.value = revision.id
  previewRevision.value = revision
  revisions.value = [revision, ...revisions.value.filter(item => item.id !== revision.id)]
  notice.value = '字段草稿已另存为新预览修订并解析；原修订不变，尚未确认或采用。'
}
const canReadScriptInput = () => !actionBusy.value && !loading.value && !pageReadFailed.value
  && !creationRequest.value && !creationResult.value && !runId.value
  && (isScriptManagement.value || (route.query.view === 'new' && creationSource.value === 'recipe' && isConfirmedScriptRecipe.value))
const scriptInputContext = () => {
  const sequence = ++scriptActionSequence, loadSequence = productionLoadSequence
  const expectedProjectId = projectId.value, session = studio.captureSessionEpoch(), inputRevision = scriptInputRevision
  const expectedView = route.query.view, expectedSource = creationSource.value
  const isCurrent = () => sequence === scriptActionSequence && loadSequence === productionLoadSequence
    && session === studio.captureSessionEpoch() && projectId.value === expectedProjectId && activeProjectScope === expectedProjectId
    && !runId.value && route.query.view === expectedView && creationSource.value === expectedSource
    && (isScriptManagement.value || (route.query.view === 'new' && creationSource.value === 'recipe'))
  return { expectedProjectId, isCurrent, matchesInput: () => isCurrent() && inputRevision === scriptInputRevision }
}
const acceptScriptPreview = (result: { source: ScriptSource; revision: ScriptRevision }, expectedProjectId: string) => {
  if (result.source.project !== expectedProjectId || !result.source.id || !result.revision.id
    || result.revision.source !== result.source.id || !result.revision.content?.trim() || result.revision.status !== 'preview') {
    throw new Error('剧本预览回执与当前项目或来源不一致，请到已有版本核对，不会使用旧版本创建。')
  }
  selectedRevisionId.value = result.revision.id
  previewRevision.value = result.revision
  revisions.value = [result.revision, ...revisions.value.filter(item => item.id !== result.revision.id)]
}
const createScriptPreview = async () => {
  if (!canReadScriptInput() || (scriptMode.value !== 'paste' && scriptMode.value !== 'file')
    || (scriptMode.value === 'file' ? !scriptFile.value : !scriptText.value.trim())) return
  const { expectedProjectId, isCurrent, matchesInput } = scriptInputContext()
  actionBusy.value = 'script'
  previewRevision.value = null
  error.value = ''
  notice.value = ''
  try {
    const result = scriptMode.value === 'file' && scriptFile.value
      ? await platformApi.uploadScript(expectedProjectId, scriptFile.value)
      : await platformApi.createPastedScript(expectedProjectId, scriptText.value, scriptFormat.value)
    if (!matchesInput()) return
    acceptScriptPreview(result, expectedProjectId)
    notice.value = '服务端已生成预览 revision。请核对正文后再确认。'
    await readScripts(expectedProjectId)
  } catch (cause) { if (matchesInput()) error.value = message(cause) } finally { if (isCurrent() && actionBusy.value === 'script') actionBusy.value = '' }
}
const importLibraryScript = async () => {
  const assetId = selectedScriptAssetId.value
  if (!canReadScriptInput() || scriptMode.value !== 'library' || !assetId) return
  const { expectedProjectId, isCurrent, matchesInput } = scriptInputContext()
  actionBusy.value = 'script-asset'; error.value = ''; notice.value = ''
  previewRevision.value = null
  try {
    const result = await platformApi.importScriptAsset(expectedProjectId, assetId)
    if (!matchesInput()) return
    acceptScriptPreview(result, expectedProjectId)
    notice.value = '剧本素材已复制为当前项目的 preview revision；仍需核对并人工确认后才能生产。'
    await readScripts(expectedProjectId)
  } catch (cause) { if (matchesInput()) error.value = message(cause) } finally { if (isCurrent() && actionBusy.value === 'script-asset') actionBusy.value = '' }
}
const selectExistingRevision = async () => {
  const revisionId = selectedRevisionId.value
  if (!canReadScriptInput() || scriptMode.value !== 'existing') return
  const listed = revisions.value.find(item => item.id === revisionId)
  previewRevision.value = null
  if (!revisionId || !listed) return
  const { isCurrent, matchesInput } = scriptInputContext()
  const matchesSelection = () => matchesInput() && selectedRevisionId.value === revisionId
  actionBusy.value = 'revision'
  error.value = ''
  try {
    const revision = await platformApi.getScriptRevision(revisionId)
    if (!matchesSelection()) return
    if (revision.id !== revisionId || revision.source !== listed.source || revision.contentSha256 !== listed.contentSha256 || !revision.content?.trim()) {
      throw new Error('剧本详情与所选版本不一致或缺少正文，请重新读取目录后选择。')
    }
    previewRevision.value = revision
  } catch (cause) {
    if (matchesSelection()) error.value = message(cause)
  } finally {
    if (isCurrent() && actionBusy.value === 'revision') actionBusy.value = ''
  }
}
const confirmRevision = async () => {
  if (!canReadScriptInput()) return
  const original = selectedRevision.value, revisionId = selectedRevisionId.value
  if (!original || original.status !== 'preview') {
    error.value = '当前剧本预览与所选 revision 不一致，请重新选择并读取完整正文后再确认。'
    return
  }
  const { isCurrent, matchesInput } = scriptInputContext()
  const matchesSelection = () => matchesInput() && selectedRevisionId.value === revisionId && previewRevision.value === original
  actionBusy.value = 'confirm'
  error.value = ''
  try {
    const confirmed = await platformApi.confirmScriptRevision(revisionId)
    if (!matchesSelection()) return
    if (confirmed.id !== original.id || confirmed.source !== original.source || confirmed.contentSha256 !== original.contentSha256
      || confirmed.content !== original.content || confirmed.status !== 'confirmed') {
      throw new Error('确认回执与已预览剧本不一致，请从已有版本核对；当前不允许创建生产运行。')
    }
    previewRevision.value = confirmed
    revisions.value = revisions.value.map((item) => item.id === confirmed.id ? confirmed : item)
    notice.value = '服务端已确认该剧本 revision，可用于 ProductionRun。'
  } catch (cause) {
    if (matchesSelection()) error.value = message(cause)
  } finally {
    if (isCurrent() && actionBusy.value === 'confirm') actionBusy.value = ''
  }
}
const createRun = async () => {
  if (actionBusy.value || loading.value || pageReadFailed.value || runId.value || route.query.view !== 'new' || creationSource.value !== 'recipe') return
  if (!creationRequest.value && !creationResult.value && !canCreate.value) return
  const expectedProjectId = projectId.value, loadSequence = productionLoadSequence
  const sequence = ++runActionSequence, session = studio.captureSessionEpoch()
  const isCurrent = () => sequence === runActionSequence && loadSequence === productionLoadSequence
    && session === studio.captureSessionEpoch() && projectId.value === expectedProjectId && !runId.value
  actionBusy.value = 'create'
  error.value = ''
  notice.value = ''
  try {
    if (!creationRequest.value && !creationResult.value) {
      const mode = selectedScriptMode.value
      if (mode === 'invalid') throw new Error('所选配方没有唯一、合法的剧本输入模式')
      let overrides: JsonObject
      try { overrides = JSON.parse(configOverridesText.value) as JsonObject } catch { throw new Error('本次配置覆盖必须是合法 JSON 对象') }
      if (!overrides || Array.isArray(overrides) || typeof overrides !== 'object') throw new Error('本次配置覆盖必须是 JSON 对象')
      if (!productionCapability.value) throw new Error(productionCapabilityError.value || '服务端有效安全上限不可用，已停止创建生产运行。')
      const plan = aiRunPlan.value
      if (mode === 'ai' && !plan) throw new Error('服务端项目创建策略未提供有效的集数、每集镜数或单集时长，不能创建生产运行。')
      creationRequest.value = snapshotProductionRunInput({
        planSource: 'recipe',
        projectId: expectedProjectId,
        scriptMode: mode,
        scriptRevisionId: selectedRevisionId.value,
        recipeVersionId: selectedRecipeId.value,
        projectSourceConfirmations: projectSourceConfirmations.value,
        idempotencyKey: idempotencyKey.value,
        allowExternalCalls: allowExternalCalls.value,
        configOverrides: overrides,
        idea: aiIdea.value.trim(),
        styleHint: aiStyleHint.value.trim(),
        episodeCount: plan?.episodeCount,
        shotsPerEpisode: plan?.shotsPerEpisode,
        episodeDurationSeconds: plan?.episodeDurationSeconds,
        episodeIds: selectedEpisodeIds.value,
        unattendedConfirmed: isUnattendedRecipe.value ? unattendedConfirmed.value : undefined,
      })
    }
    if (!creationResult.value) {
      const request = creationRequest.value!
      let created: ProductionRun
      try { created = await platformApi.createProductionRun(request) }
      catch (cause) {
        if (isCurrent() && isDefinitiveCreationRejection(cause)) { creationRequest.value = null; idempotencyKey.value = crypto.randomUUID() }
        throw cause
      }
      if (!isCurrent()) return
      if (!created.id || created.project !== expectedProjectId || created.planSource !== 'recipe') throw new Error('创建回执与原项目或制作方式不一致，请核对原请求。')
      creationResult.value = created
    }
    const failure = await router.replace({ name: 'production-run-detail', params: { projectId: expectedProjectId, runId: creationResult.value.id } })
    if (failure) throw new Error('详情导航未完成，请重新打开已创建的原记录。')
  } catch (cause) { if (isCurrent()) error.value = message(cause) }
  finally { if (isCurrent() && actionBusy.value === 'create') actionBusy.value = '' }
}
const runAction = async (name: string, request: () => Promise<ProductionRun>, localError?: { value: string }): Promise<boolean> => {
  const original = currentRun.value
  const expectedProjectId = projectId.value
  if (runActionBlocked.value || !original || original.id !== runId.value || !productionRunBelongsToProject(original, expectedProjectId)) return false
  const sequence = ++runActionSequence
  const loadSequence = productionLoadSequence
  const session = studio.captureSessionEpoch()
  const isCurrent = () => sequence === runActionSequence && loadSequence === productionLoadSequence
    && session === studio.captureSessionEpoch() && runId.value === original.id && projectId.value === expectedProjectId
    && currentRun.value?.id === original.id
  // Reads started before submission cannot restore the pre-action run over its receipt.
  runRefreshSequence += 1
  actionBusy.value = name
  error.value = ''
  if (localError) localError.value = ''
  notice.value = ''
  let receiptConfirmed = false
  try {
    const result = await request()
    if (!isCurrent()) return false
    if (!result || result.id !== original.id || !productionRunBelongsToProject(result, expectedProjectId)) throw new Error('操作回执与原项目或运行不一致。')
    receiptConfirmed = true
    currentRun.value = result
    const refreshedProject = await studio.loadProject(expectedProjectId, true)
    if (!isCurrent()) return false
    if (!refreshedProject || refreshedProject.id !== expectedProjectId) throw new Error('原项目事实未能读取。')
    notice.value = '服务端 action 已完成，运行与项目事实均已重新读取。'
    return true
  } catch (cause) {
    if (!isCurrent()) return false
    if (!receiptConfirmed && name === 'approve' && apiErrorPresentation(cause).code === 'storyboard_reference_selection_changed') {
      storyboardReferenceReview.value = null
      storyboardReferenceError.value = message(cause)
      error.value = message(cause)
      return false
    }
    pendingRunReceipt.value = { runId: original.id, projectId: expectedProjectId, action: name, stageKey: original.currentStageKey || '' }
    const detail = receiptConfirmed
      ? `服务端操作已返回，但项目事实刷新失败：${message(cause)}`
      : `提交未能确认：${message(cause)}。请先重新读取原运行，不能据此判定未提交或自动重试。`
    if (localError) localError.value = detail
    error.value = detail
    return false
  } finally { if (isCurrent() && actionBusy.value === name) actionBusy.value = '' }
}
const decide = () => {
  const run = currentRun.value
  const stage = currentStage.value
  if (!run || !stage) return
  if (approvalDecision.value === 'approve' && reviewApprovalError.value) { error.value = reviewApprovalError.value; return }
  const expectedSourceSnapshotSha256 = stage.stageKey === 'storyboard_review' && approvalDecision.value === 'approve'
    ? storyboardReferenceReview.value?.sourceSnapshotSha256
    : stage.stageKey === 'bible_image_review' && approvalDecision.value === 'approve'
      ? bibleImageReview.value.value?.snapshotSha256
      : undefined
  const bibleImageBindings = stage.stageKey === 'bible_image_review' && approvalDecision.value === 'approve'
    ? bibleImageBindingsPayload().map((binding) => ({
        entityType: binding.entity_type, entityId: binding.entity_id,
        selectedArtifactIds: binding.selected_artifact_ids,
        selectedLibraryAssets: binding.selected_library_assets.map(asset => ({ assetId: asset.asset_id, expectedSha256: asset.expected_sha256 })),
        anchorArtifactId: binding.anchor_artifact_id || undefined, anchorLibraryAssetId: binding.anchor_library_asset_id || undefined,
      }))
    : undefined
  const expectedSelectionSha256 = stage.stageKey === 'storyboard_review' && approvalDecision.value === 'approve'
    ? storyboardReferenceReview.value?.selectionSha256 : undefined
  void runAction('approve', () => platformApi.approveProductionRun(run.id, {
    stageKey: stage.stageKey,
    decision: approvalDecision.value,
    bibleReview: stage.stageKey === 'bible_review' && approvalDecision.value === 'approve' ? bibleConfirmation.value || undefined : undefined,
    comment: approvalComment.value,
    expectedSourceSnapshotSha256,
    bibleImageBindings,
    expectedSelectionSha256,
  }))
}
const continueRun = () => { if (currentRun.value) void runAction('continue', () => platformApi.continueProductionRun(currentRun.value!.id)) }
const requestBibleRegeneration = (entity: { entityType: 'character' | 'scene' | 'prop'; entityId: string; entityName: string; candidates: unknown[] }) => {
  if (runActionBlocked.value) return
  const run = currentRun.value
  const stage = currentStage.value
  const review = bibleImageReview.value.value
  if (!run || run.status !== 'awaiting_approval' || stage?.stageKey !== 'bible_image_review' || !review || bibleImageReview.value.error) {
    error.value = '当前运行不在 Bible 参考图审核阶段，不能重新生成候选。'
    return
  }
  pendingBibleRegenerationError.value = ''
  pendingBibleRegeneration.value = {
    requestId: crypto.randomUUID(),
    runId: run.id,
    runStatus: run.status,
    stageId: stage.id,
    snapshotSha256: review.snapshotSha256,
    entityType: entity.entityType,
    entityId: entity.entityId,
    entityName: entity.entityName,
    existingCandidateCount: entity.candidates.length,
  }
}
const closeBibleRegeneration = () => {
  if (actionBusy.value) return
  pendingBibleRegeneration.value = null
  pendingBibleRegenerationError.value = ''
}
const confirmBibleRegeneration = async () => {
  const pending = pendingBibleRegeneration.value
  const run = currentRun.value
  const stage = currentStage.value
  const review = bibleImageReview.value.value
  const currentEntity = review?.entities.find((entity) => entity.entityType === pending?.entityType && entity.entityId === pending?.entityId)
  if (!pending || !run || run.id !== pending.runId || run.status !== pending.runStatus || run.status !== 'awaiting_approval' || stage?.id !== pending.stageId || stage.stageKey !== 'bible_image_review' || !review || review.snapshotSha256 !== pending.snapshotSha256 || !currentEntity) {
    pendingBibleRegenerationError.value = '运行、审核快照或目标实体已变化，请依据最新状态重新确认。'
    return
  }
  const succeeded = await runAction('regenerate-bible-images', () => platformApi.regenerateBibleImages(pending.runId, {
    requestId: pending.requestId,
    expectedSourceSnapshotSha256: pending.snapshotSha256,
    entities: [{ entityType: pending.entityType, entityId: pending.entityId }],
  }), pendingBibleRegenerationError)
  if (!succeeded) return
  closeBibleRegeneration()
  notice.value = `已为“${pending.entityName}”创建新的候选准备；通过字段审核和运行准入后才调用模型。已有候选保留，生成完成后一起审核。`
}
const requestRunConfirmation = (action: 'retry' | 'cancel') => {
  if (runActionBlocked.value) return
  const run = currentRun.value
  if (!run) return
  pendingRunConfirmationError.value = ''
  retryPreviewReady.value = false
  pendingRunConfirmation.value = {
    action,
    runId: run.id,
    runStatus: run.status,
    blockedCode: run.blockedCode,
    uncertainRemote: isUncertainRemote.value,
    stageId: currentStage.value?.id || '',
    stageKey: currentStage.value?.stageKey || run.currentStageKey || '—',
    retryContext: run.retryContext ? { ...run.retryContext, expectedAttemptIds: [...run.retryContext.expectedAttemptIds],
      ...(run.retryContext.expectedAssemblyJobIds === undefined ? {} : { expectedAssemblyJobIds: [...run.retryContext.expectedAssemblyJobIds] }) } : null,
    estimatedCost: run.estimatedCostTotal,
    committedCost: run.committedCostTotal,
    actualCost: run.actualCostTotal,
    currency: run.costCurrency || '币种未定',
  }
}
const closeRunConfirmation = () => { if (!actionBusy.value) { pendingRunConfirmation.value = null; pendingRunConfirmationError.value = '' } }
const retryPreviewReady = ref(false)
const confirmRunAction = async () => {
  const pending = pendingRunConfirmation.value
  const run = currentRun.value
  if (!pending || !run) return
  if (run.id !== pending.runId || run.status !== pending.runStatus || run.blockedCode !== pending.blockedCode || isUncertainRemote.value !== pending.uncertainRemote || (pending.stageId && currentStage.value?.id !== pending.stageId)) {
    pendingRunConfirmationError.value = '运行或当前阶段已变化，请依据最新状态重新确认。'
    return
  }
  if (pending.action === 'retry') {
    if (!retryPreviewReady.value) { pendingRunConfirmationError.value = '请先读取并处理重试影响中的提示。'; return }
    if (isUncertainRemote.value) { pendingRunConfirmationError.value = '远端结果状态不确定，不能普通重试；请先由管理员核验。'; return }
    const context = pending.retryContext
    if (!context || JSON.stringify(context) !== JSON.stringify(run.retryContext)) {
      pendingRunConfirmationError.value = '失败调用范围缺失或已变化，请重新读取并确认。'; return
    }
    if (await runAction('retry', () => platformApi.retryProductionRun(pending.runId, context), pendingRunConfirmationError)) closeRunConfirmation()
    return
  }
  if (await runAction('cancel', () => platformApi.cancelProductionRun(pending.runId), pendingRunConfirmationError)) closeRunConfirmation()
}
const toggleStageDisclosure = (stageId: string) => {
  if (artifactSelectionProtection.value) return
  stageDisclosureUserSelected.value = true
  expandedStageId.value = expandedStageId.value === stageId ? '' : stageId
}
const selectRoute = (choice: {stageKey:string;routeIndex:number;appearanceRole:'primary'|'variant'}) => {
  const run = currentRun.value
  if (!run) return
  void runAction('route', () => platformApi.selectProductionRoute(run.id, choice))
}
const authorizeCost = () => {
  const run = currentRun.value
  const stage = currentStage.value
  if (!run || !stage || costAuthorizationError.value) return
  void runAction('cost', () => platformApi.authorizeProductionCost(run.id, {
    stageKey: stage.stageKey,
    allowExternalCalls: authorizationExternal.value,
    approvedEstimatedCost: approvedEstimatedCost.value.trim() || null,
    currency: authorizationCurrency.value.trim().toUpperCase(),
    comment: approvalComment.value,
  }))
}
const saveManualBudget = async () => {
  const run = currentRun.value
  if (!run || run.planSource !== 'manual') return
  const succeeded = await runAction('cost', () => platformApi.authorizeProductionCost(run.id, {
    stageKey: '', allowExternalCalls: authorizationExternal.value,
    currency: '',
    comment: '',
  }))
  if (succeeded) notice.value = '本轮调用设置已保存，未发起模型调用。请在“任务与记录”中打开已有准备，核对后点击“检查并继续此任务”。'
}
const acknowledgeActualCost = async () => {
  const run = currentRun.value
  const stage = currentStage.value
  if (!run || !stage || run.planSource !== 'manual' || run.blockedCode !== 'actual_budget_exceeded') return
  const succeeded = await runAction('cost', () => platformApi.authorizeProductionCost(run.id, {
    stageKey: stage.stageKey, currency: run.costCurrency,
    comment: '已核对实际费用提醒；保留现有候选，不重新生成或自动采用。',
  }))
  if (succeeded) notice.value = '费用提醒已确认。已有候选保留，请继续审核内容；未重新生成或自动采用。'
}
const artifactSelected = (value: ArtifactSelectionConfirmed) => {
  if (value.runId !== runId.value) return
  return refreshSelectedContent('原选择回执已确认，当前运行与项目事实已重新读取。')
}
const openUncertainResolution = (attempt: GenerationAttempt) => {
  if (runActionBlocked.value) return
  uncertainResolutionAttempt.value = attempt
  uncertainResolutionOutcome.value = attempt.errorCode === 'actual_cost_reconciliation_required' ? 'actual_cost_confirmed' : 'confirmed_not_submitted'
  uncertainResolutionNote.value = ''
  uncertainResolutionProviderRequestId.value = ''
  uncertainResolutionActualCost.value = attempt.actualCost || ''
  uncertainResolutionCurrency.value = attempt.costCurrency || currentRun.value?.costCurrency || ''
  uncertainResolutionError.value = ''
}
const closeUncertainResolution = () => {
  if (uncertainResolutionSaving.value) return
  uncertainResolutionAttempt.value = null
  uncertainResolutionProviderRequestId.value = ''
  uncertainResolutionError.value = ''
}
const submitUncertainResolution = async () => {
  const run = currentRun.value
  const attempt = uncertainResolutionAttempt.value
  if (!run || !attempt || uncertainResolutionValidation.value) return
  const outcome = uncertainResolutionOutcome.value
  const succeeded = await runAction('resolve', () => platformApi.resolveProductionRunUncertain(run.id, {
      attemptId: attempt.id,
      outcome,
      note: uncertainResolutionNote.value.trim(),
      providerRequestId: uncertainResolutionProviderRequestId.value.trim(),
      actualCost: uncertainResolutionActualCost.value.trim() || null,
      costCurrency: uncertainResolutionCurrency.value.trim().toUpperCase(),
    }), uncertainResolutionError)
  if (succeeded) {
    uncertainResolutionAttempt.value = null
    uncertainResolutionProviderRequestId.value = ''
    notice.value = outcome === 'confirmed_remote_submitted'
      ? '已追加人工消歧证据并恢复远端轮询；这不是 smoke/pass 证明，请继续观察服务端终态。'
      : '已追加人工消歧证据；该记录不代表生成成功，页面已读取服务端最新状态。'
    await loadUncertainAttempts()
  }
}
</script>

<template>
  <div class="platform-page production-run-page unified-production-studio">
    <ProductionWorkspaceHeader :title="project?.title || '项目'" mode="operate" :run="currentRun" :gradient="currentStyle?.gradient" :cover-url="project?.coverUrl" @back="router.push({path:`/projects/${projectId}`,query:{run:currentRun?.id || (typeof route.query.run==='string' ? route.query.run : undefined),section:currentRun?.planSource==='manual' ? manualSection : productionTab}})">
      <button class="button ghost small" :disabled="loading || Boolean(actionBusy) || receiptReadBusy || manualContextProtected" @click="load"><RefreshCw :size="14" :class="{spin:loading}"/>重新读取</button>
    </ProductionWorkspaceHeader>
    <section v-if="pendingRunReceipt" class="platform-alert warning" role="alert" aria-label="原运行提交待核对">
      <AlertTriangle :size="17"/><div><strong>先核对原运行，勿重复提交</strong>
        <p>原运行 {{ pendingRunReceipt.runId }}<template v-if="pendingRunReceipt.stageKey"> · {{ stageLabel(pendingRunReceipt.stageKey) }}</template>。当前显示上次已知事实；提交结果或项目刷新尚未核对。关闭确认框不会解除保护。</p>
        <button class="button ghost small" :disabled="receiptReadBusy || loading || Boolean(actionBusy)" @click="recoverRunReceipt">{{ receiptReadBusy ? '正在读取原运行…' : '重新读取原运行（不重试）' }}</button>
      </div>
    </section>
    <div v-if="pendingContentRefresh" class="platform-alert warning" role="status"><LoaderCircle :size="17"/><p>采用已提交，原运行与项目事实待刷新。{{ manualContextProtected || actionBusy ? '当前操作结束或面板关闭后自动重新读取；保留当前编辑内容。' : '正在重新读取。' }}不要重复采用；读取完成前不使用旧事实继续审核。</p></div>
    <div v-if="error" class="platform-alert danger"><AlertTriangle :size="17"/><div><strong>操作或读取需核对</strong><p>{{ error }}</p></div></div><div v-if="notice" class="platform-alert success"><Check :size="17"/><p>{{ notice }}</p></div><div v-if="loading" class="platform-loading"><LoaderCircle class="spin" :size="22"/>正在读取项目生产状态…</div>

    <section v-else-if="pageReadFailed || (runId && !currentRun)" class="platform-card" role="alert">
      <h3>工作台数据未能读取</h3><p>请点击“重新读取”重试。当前没有展示旧记录，也没有新建另一轮制作。</p>
    </section>
    <template v-else-if="!currentRun">
      <section v-if="route.query.view!=='new' && !isScriptManagement" class="platform-card production-step">
        <ProjectVersionWorkspace :project-id="projectId" :title="project?.title || '项目'" :runs="runs" :loading="loading" :error="error" open-workspace @retry="load"/>
        <div class="segmented"><RouterLink class="button primary" :to="{name:'production-run',params:{projectId},query:{view:'new',source:'recipe'}}">新建配方生产</RouterLink><RouterLink class="button ghost" :to="{name:'production-run',params:{projectId},query:{view:'new',source:'manual'}}">新建无配方制作</RouterLink></div>
      </section>
      <template v-if="route.query.view==='new' || isScriptManagement">
      <div class="platform-alert info"><p>{{ isScriptManagement ? '项目剧本管理：导入、解析和字段另存复用原剧本修订流程；不会创建运行、调用模型或自动采用。' : '正在新建独立的一轮制作，不是继续已有运行。' }}</p><RouterLink :to="isScriptManagement && typeof route.query.run==='string' ? {name:'production-run-detail',params:{projectId,runId:route.query.run},query:{step:'script'}} : {name:'project-detail',params:{projectId}}">{{isScriptManagement && route.query.run ? '返回原版本生产工作台' : '返回制作版本列表'}}</RouterLink></div>
      <div v-if="!isScriptManagement && creationSource==='recipe' && productionCapabilityError" class="platform-alert danger"><AlertTriangle :size="17"/><div><strong>生产配置不可用</strong><p>{{ productionCapabilityError }}。服务端有效限制与创建策略恢复前，不能创建新 ProductionRun。</p></div></div>
      <div v-if="!isScriptManagement" class="segmented" aria-label="制作方式"><RouterLink class="button" :class="{active:creationSource==='recipe'}" :to="{query:{...route.query,source:'recipe'}}">按配方生产</RouterLink><RouterLink class="button" :class="{active:creationSource==='manual'}" :to="{query:{...route.query,source:'manual'}}">无配方人工制作</RouterLink></div>
      <ManualProductionCreateForm v-if="!isScriptManagement && creationSource==='manual'" :key="projectId" :project-id="projectId" :episodes="projectEpisodes" @created="manualRunCreated"/>
      <div v-else>
      <section v-if="creationRequest && actionBusy !== 'create'" class="platform-alert warning" role="status">
        <div><strong>{{ creationResult ? '运行已创建，等待打开原记录' : '创建回执待核对' }}</strong>
          <p v-if="creationResult">原运行 {{ creationResult.id }}。再次打开不会重新创建或启动另一轮。</p>
          <p v-else>原输入与请求标识已保留。恢复会再次提交同一请求：已创建则返回原记录，未创建则创建并可能开始配方生产；这不是只读查询。</p>
          <p>原请求标识：{{ creationRequest.idempotencyKey }}</p>
          <button type="button" class="button ghost small" :disabled="Boolean(actionBusy) || loading || pageReadFailed" @click="createRun">{{ creationResult ? '打开已创建的原运行（不再创建）' : '按原请求恢复创建' }}</button>
          <RouterLink :to="{name:'production-run',params:{projectId},query:{view:'list'}}">查看原项目制作记录</RouterLink>
        </div>
      </section>
      <fieldset class="production-setup-grid" :disabled="Boolean(creationRequest) || Boolean(actionBusy)">
        <section v-if="!isScriptManagement && isAiScriptRecipe" class="platform-card production-step"><header><span>01</span><div><strong>AI 生成剧本</strong><small>模型只生成预览版本（preview）；剧本审核阶段（script_review）批准后才确认并进入一致性设定集（Bible）。</small></div></header><div v-if="projectEpisodes.length" class="platform-alert danger production-inline-alert"><AlertTriangle :size="15"/><p>项目已有 {{ projectEpisodes.length }} 集正式剧集（Episode），AI 剧本配方已禁用，避免覆盖已有下游结构。请选择下游生产配方（downstream recipe）。</p></div><label class="platform-field"><span>故事想法</span><textarea v-model="aiIdea" rows="8" placeholder="描述核心冲突、人物与连续剧走向…"></textarea></label><label class="platform-field"><span>风格提示</span><textarea v-model="aiStyleHint" rows="3" placeholder="可选：视觉、节奏、受众等"></textarea></label><div class="platform-form-grid"><label class="platform-field"><span>集数</span><input v-model.number="episodeCountHint" type="number" min="1"/></label><label class="platform-field"><span>每集镜数</span><input v-model.number="shotsPerEpisode" type="number" min="1"/></label><label class="platform-field"><span>单集时长（秒）</span><input v-model.number="episodeDurationSeconds" type="number" min="1"/></label></div><small v-if="aiPlannedAverageShotDurationMs" class="production-duration-hint">计划平均每镜 {{ aiPlannedAverageShotDurationMs }}ms；计划原样冻结，由配方字段映射传入所选实例；实际返回按所选输出结构及语义映射核验。</small></section>
        <template v-else-if="isScriptManagement || isConfirmedScriptRecipe">
        <section class="platform-card production-step"><header><span>01</span><div><strong>导入或选择剧本</strong><small>上传、粘贴或素材库剧本都只生成预览版本（preview revision），确认后才可生产。</small></div></header><div v-if="projectEpisodes.length" class="platform-alert warning production-inline-alert"><AlertTriangle :size="15"/><p>项目已有 {{ projectEpisodes.length }} 集正式剧集（Episode）；仍可从已确认版本（revision）创建新运行。服务端会按本次配方显式处理，现有正式对象不会被页面静默覆盖。</p></div><div class="segmented"><button :class="{active:scriptMode==='paste'}" @click="scriptMode='paste'">粘贴</button><button :class="{active:scriptMode==='file'}" @click="scriptMode='file'">上传文件</button><button :class="{active:scriptMode==='library'}" @click="scriptMode='library'">素材库剧本</button><button :class="{active:scriptMode==='existing'}" @click="scriptMode='existing'">已有版本</button></div><template v-if="scriptMode==='paste'"><label class="platform-field"><span>格式</span><select v-model="scriptFormat"><option value="plain">纯文本</option><option value="markdown">Markdown</option><option value="fountain">Fountain</option></select></label><label class="platform-field"><span>剧本文本</span><textarea v-model="scriptText" rows="10" placeholder="粘贴完整剧本…"></textarea></label><button class="button primary small" :disabled="!scriptText.trim()||Boolean(actionBusy)" @click="createScriptPreview"><FileText :size="13"/>{{ actionBusy==='script'?'生成预览中…':'生成服务端预览' }}</button></template><template v-else-if="scriptMode==='file'"><label class="script-drop"><Upload :size="22"/><strong>{{ scriptFile?.name || '选择 txt / md / fountain / docx' }}</strong><small>PDF 当前由服务端明确拒绝，不会静默提取。</small><input type="file" accept=".txt,.md,.markdown,.fountain,.docx,text/plain,text/markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document" @change="chooseFile"/></label><button class="button primary small" :disabled="!scriptFile||Boolean(actionBusy)" @click="createScriptPreview">{{ actionBusy==='script'?'上传解析中…':'上传并生成预览' }}</button></template><template v-else-if="scriptMode==='library'"><label class="platform-field"><span>素材库剧本</span><select v-model="selectedScriptAssetId" :disabled="Boolean(actionBusy)"><option value="">请选择</option><option v-for="asset in scriptAssets" :key="asset.id" :value="asset.id">{{ asset.name }} · {{ asset.checksum?.slice(0,10) }}</option></select><small>导入只会创建项目级预览版本（preview revision），不会覆盖既有确认版本。</small></label><button class="button primary small" :disabled="!selectedScriptAssetId||Boolean(actionBusy)" @click="importLibraryScript"><FileText :size="13"/>{{ actionBusy==='script-asset'?'导入中…':'导入并生成预览' }}</button></template><template v-else><label class="platform-field"><span>已有最新版本（latest revision）</span><select v-model="selectedRevisionId" :disabled="Boolean(actionBusy)" @change="selectExistingRevision"><option value="">请选择</option><option v-for="item in revisions" :key="item.id" :value="item.id">版本（revision）{{ item.revision }} · {{ presentPlatformStatus(item.status).label }} · {{ item.contentSha256.slice(0,10) }}</option></select></label><button class="button ghost small" :disabled="!selectedRevisionId||Boolean(actionBusy)" @click="selectExistingRevision">重新读取所选剧本</button></template>
          <p class="production-duration-hint">切换来源或修改正文、格式、文件、素材后，需重新生成预览并确认；目录中的已确认版本不会自动选用。</p><div v-if="previewRevision" class="script-preview" aria-label="本次选用剧本预览"><header><div><strong>Revision {{ previewRevision.revision }}</strong><small>{{ previewRevision.contentSizeBytes }} 字节 · {{ previewRevision.contentSha256 }}</small></div><PlatformStatusPill :status="previewRevision.status"/></header><pre>{{ previewRevision.content || '服务端列表响应不含正文；请重新选择以读取详情。' }}</pre><button v-if="previewRevision.status!=='confirmed'" class="button primary small" :disabled="Boolean(actionBusy)||previewRevision.id!==selectedRevisionId" @click="confirmRevision"><Check :size="13"/>{{ actionBusy==='confirm'?'确认中…':'确认该剧本版本' }}</button></div>
        </section>
        <ScriptRevisionStructurePanel v-if="previewRevision" :key="previewRevision.id" :revision-id="previewRevision.id" :content-sha256="previewRevision.contentSha256" :status="previewRevision.status" :disabled="Boolean(actionBusy)" @edit-busy="setStructuredEditBusy" @revision-created="acceptStructuredRevision"/>
        </template>
        <section v-else-if="isDownstreamRecipe" class="platform-card production-step"><header><span>01</span><div><strong>沿用现有分集</strong><small>下游生产配方（downstream recipe）从已有正式剧集（Episode） 开始，不接收剧本版本（revision） 或 AI 剧本输入。</small></div></header><div v-if="projectEpisodes.length" class="platform-alert success production-inline-alert"><Check :size="15"/><p>已发现 {{ projectEpisodes.length }} 集正式剧集（Episode），可直接选择生产范围。</p></div><div v-else class="platform-alert danger production-inline-alert"><AlertTriangle :size="15"/><p>当前项目没有 Episode，下游生产配方（downstream recipe）不能创建。请先用 AI 或已确认版本配方（confirmed revision recipe）建立正式分集。</p></div></section>
        <section v-else-if="!selectedRecipe" class="platform-card production-step"><header><span>01</span><div><strong>选择已发布配方</strong></div></header><div class="platform-alert info production-inline-alert" role="status"><p>{{ recipeSelectionIssue }}</p><RouterLink to="/settings">打开系统设置</RouterLink></div></section>
        <section v-else class="platform-card production-step"><header><span>01</span><div><strong>配方剧本模式无效</strong><small>配方必须且只能声明 AI、已确认版本（confirmed revision）或剧本输入/人工下游（script_input/manual downstream）模式。</small></div></header><div class="platform-alert danger production-inline-alert"><AlertTriangle :size="15"/><p>所选配方包含未知剧本模式或同时声明了冲突模式，已禁止创建。请在系统设置修正并发布后继版本。</p></div></section>
        <section v-if="!isScriptManagement" class="platform-card production-step"><header><span>02</span><div><strong>选择配方与运行边界</strong><small>资源提醒由当前系统配置统一管理，配方不再配置独立预算。</small></div></header><label class="platform-field"><span>已发布配方（Published Recipe）</span><select v-model="selectedRecipeId"><option value="">请选择</option><option v-for="item in recipes" :key="item.id" :value="item.id">{{ item.name || item.recipeKey }} · v{{ item.version }}</option></select></label><details v-if="isDownstreamRecipe&&projectEpisodes.length" class="production-overrides"><summary>生产选集 · {{ selectedEpisodeIds.length }} / {{ projectEpisodes.length }}<ChevronDown :size="13"/></summary><label v-for="episode in projectEpisodes" :key="episode.id" class="platform-field"><span>第 {{ episode.number }} 集 · {{ episode.title }}</span><input v-model="selectedEpisodeIds" type="checkbox" :value="episode.id"/></label><small>至少选择一个已有剧集；服务端创建时会去重、排序并冻结，不会自动使用全部剧集。</small></details><RecipeExecutionSummary v-if="selectedRecipe" :recipe="selectedRecipe"/><RecipeProjectSourceConfirmations v-if="selectedRecipe" v-model="projectSourceConfirmations" :recipe="selectedRecipe" :project-id="projectId" :disabled="Boolean(actionBusy)||Boolean(creationRequest)" @ready="projectSourceReady=$event"/><ProductionBudgetFields v-model:external="allowExternalCalls"/><details class="production-overrides"><summary>本次配置覆盖（高级）<ChevronDown :size="13"/></summary><textarea v-model="configOverridesText" rows="8" spellcheck="false"></textarea></details><div class="run-create-summary"><GitBranch :size="17"/><span><strong>{{ recipeSelectionIssue || (isInvalidRecipe ? '配方剧本模式无效，不能创建' : isAiScriptRecipe ? projectEpisodes.length ? '已有 Episode，不能重新生成 AI 剧本' : 'AI 剧本将先生成预览' : isConfirmedScriptRecipe ? selectedRevision?.status==='confirmed' ? projectEpisodes.length ? '已有 Episode；将使用已确认 revision 创建新运行' : '剧本已确认' : '请先选择并读取已确认剧本，或生成预览后确认' : projectEpisodes.length ? '从现有 Episode 继续生产' : '缺少可生产的 Episode') }}</strong><small>幂等键 {{ idempotencyKey }}</small></span></div><button class="button primary production-create" :disabled="!canCreate" @click="createRun"><Play :size="15"/>{{ actionBusy==='create'?'创建并执行中…':'创建生产运行（ProductionRun）' }}</button>
        </section>
        <section v-if="!isScriptManagement && isUnattendedRecipe" class="platform-card production-step unattended-run-panel">
          <header><span>确认</span><div><strong>全自动生产确认</strong><small>执行实例与输入来源由精确配方版本决定，提交后仍须通过服务端准入检查。</small></div></header>
          <label class="platform-checkbox"><input v-model="unattendedConfirmed" type="checkbox"/><span>我确认由配方冻结的审核策略自动审核和继续；未通过检查时明确阻断。</span></label>
          <p v-if="unattendedCreationError" role="alert">{{ unattendedCreationError }}</p>
        </section>
      </fieldset>
      </div>
      </template>
    </template>

    <template v-else-if="currentRun.planSource==='manual'">
      <section v-if="currentRun.status==='awaiting_approval' && currentRun.blockedCode==='actual_budget_exceeded'" class="platform-card" aria-label="实际费用提醒">
        <strong>实际费用待核对 · 生成结果已保留</strong>
        <p>{{ currentRun.blockedReason }}</p>
        <p>本轮已记录 {{ currentRun.actualCostTotal }} {{ currentRun.costCurrency }}。确认仅记录费用核对，不重新生成、不采用候选；后续调用仍按系统策略检查。</p>
        <button class="button primary small" type="button" :disabled="runActionBlocked || !currentStage" @click="acknowledgeActualCost">已核对费用，保留现有结果</button>
      </section>
      <ManualProductionWorkbench :key="currentRun.id" :run="currentRun" :initial-section="typeof route.query.step==='string'?route.query.step:undefined" :inputs-protected="projectInputsProtected" :project-name="project?.title" :current-rules="currentReviewRules" @section-changed="manualSection=$event" @context-protected="manualWorkbenchProtected = $event" @changed="refreshRun" @content-changed="refreshAdoptedContent" @inspect-preparations="manualPreparationStageId = $event" @cancel="requestRunConfirmation('cancel')" @history="router.push({name:'project-detail',params:{projectId}})">
      <template #project-inputs="{section}"><SeriesStudioView v-if="project" :input-project="project" :input-run-id="currentRun.id" :input-run="currentRun" :input-section="inputSection(section)" :input-active="hasProjectInputs(section)" :input-disabled="workspaceInputDisabled" @context-protected="projectInputsProtected=$event" @saved="refreshAdoptedContent"/></template>
      <template #budget><div v-if="!terminal.has(currentRun.status)" class="manual-budget-editor">
        <p>保存只更新本轮外部调用授权，不会调用模型、批准内容或自动重试。资源提醒值在系统设置中调整；进行中的调用结束后才能调整授权。</p>
        <ProductionBudgetFields v-model:external="authorizationExternal" :disabled="Boolean(actionBusy)"/>

        <button type="button" class="button primary small" :disabled="runActionBlocked" @click="saveManualBudget">保存本轮调用设置（不执行模型）</button>
      </div></template>
      <template #recovery><TaskDistributionRecovery :run="currentRun"/></template>
      <template #records><section class="manual-preparation-records" aria-label="找回本轮执行准备">
        <label class="platform-field"><span>查看哪个步骤的执行准备</span><select v-model="manualPreparationStageId" :disabled="Boolean(actionBusy)"><option value="">请选择本轮步骤</option><option v-for="stage in currentRun.stages" :key="stage.id" :value="stage.id">{{ stageLabel(stage.stageKey) }}</option></select></label>
        <p>无配方制作可以同时准备多个步骤。刷新或关闭弹窗后，可在这里找回原请求；不会重新创建或自动执行。</p>
        <StageExecutionPreparations v-if="selectedManualPreparationStage" :stage-id="selectedManualPreparationStage.id" :run-id="currentRun.id" :manual-run="currentRun" @changed="refreshRun" @busy="actionBusy = $event ? 'preparation' : ''"/>
      </section></template>
      </ManualProductionWorkbench>
    </template>
    <template v-else>
      <ProductionWorkspacePanel>
      <ProductionWorkspaceTabs v-model="productionTab" :items="productionWorkspaceSections" label="生产工作台栏目"/>
      <p v-if="projectInputsProtected" class="platform-alert warning" role="status">项目输入正在编辑或保存；制作操作与自动刷新已暂停，请返回对应栏目保存或放弃编辑。</p>
      <SeriesStudioView v-if="project" :key="currentRun.id" :input-project="project" :input-run-id="currentRun.id" :input-run="currentRun" :input-section="inputSection(productionTab)" :input-active="hasProjectInputs(productionTab)" :input-disabled="workspaceInputDisabled" @context-protected="projectInputsProtected=$event" @saved="refreshAdoptedContent"/>
      <div class="production-progress">
      <div v-if="productionCapabilityLoading" class="platform-alert info" role="status">正在读取本环节的审核限制；制作记录已加载，限制就绪后才能提交相关审核。</div>
      <div v-if="productionCapabilityError" class="platform-alert danger" role="alert"><p>审核限制读取失败：{{ productionCapabilityError }}</p><button class="button ghost small" :disabled="productionCapabilityLoading" @click="loadReviewCapability">重试读取审核限制</button></div>
      <TaskDistributionRecovery v-show="productionTab==='records'" :run="currentRun"/>
      <section class="run-command-bar"><div><PlatformStatusPill :status="currentRun.status"/><span><strong>{{ currentStage ? runStageLabel(currentRun,currentStage) : productionRunUnstartedLabel(currentRun) }}</strong><small>{{ currentRun.id }} · 已调用 {{ currentRun.generationTaskCount }} 次 · 额度按当前系统策略检查</small></span></div><div><button class="button ghost small" @click="router.push({ name: 'project-detail', params: { projectId } })"><GitBranch :size="13"/>全部制作记录</button><button v-if="!terminal.has(currentRun.status)&&!isUncertainRemote" class="button primary small" :disabled="runActionBlocked" @click="continueRun"><Play :size="13"/>{{ actionBusy==='continue'?'继续中…':'继续 / 检查阶段' }}</button><button v-if="productionRunCanRetry(currentRun)&&!isUncertainRemote" class="button ghost small" :disabled="runActionBlocked" @click="requestRunConfirmation('retry')"><RotateCcw :size="13"/>重试当前阶段</button><button v-if="!terminal.has(currentRun.status)" class="button danger small" :disabled="runActionBlocked" @click="requestRunConfirmation('cancel')"><CircleStop :size="13"/>取消运行</button></div></section>
      <div v-if="currentRun.blockedReason||currentRun.blockedCode" class="platform-alert" :class="blockedPresentation.tone"><LoaderCircle v-if="blockedPresentation.isWaiting" class="spin" :size="17"/><AlertTriangle v-else :size="17"/><div><strong>{{ blockedPresentation.code }}</strong><p>{{ blockedPresentation.summary }}</p></div></div>
      <header class="workspace-section-heading"><h3>{{ activeProductionSection.label }}</h3><p>按本轮配方推进；仅在配置要求或异常时提供处理操作。已保存内容可在只读版本中核对。</p><RouterLink class="button ghost small" :to="{path:`/projects/${projectId}`,query:{run:currentRun.id,section:productionTab}}">查看本栏已保存内容（只读）</RouterLink><button v-if="currentStage && !currentStageVisible" class="button ghost small" @click="productionTab=workspaceStageSection(currentStage.stageKey)">前往当前阶段 · {{runStageLabel(currentRun,currentStage)}}</button></header>
      <p v-if="productionTab!=='records' && !visibleStages.length" class="platform-empty">本轮配方未配置此栏目对应阶段；不要求补做，也不会创建额外调用。</p>
      <section v-if="needsAttemptResolution" class="platform-card review-evidence"><header><div><strong>远端提交 / 费用待人工消歧</strong><small>仅显示后端安全投影；人工消歧会追加审计证据，不会伪装为生成通过。</small></div></header><div v-if="uncertainAttemptsLoading" class="platform-loading compact"><LoaderCircle class="spin" :size="18"/>正在读取安全的生成尝试（attempt）投影…</div><div v-else-if="uncertainAttemptsError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ uncertainAttemptsError }}</p></div><div v-else class="platform-dense-list"><article v-for="attempt in uncertainAttempts" :key="attempt.id"><div><strong>{{ operationLabel(attempt.operation) }} · 第 {{ attempt.attemptNumber }} 次尝试（attempt）</strong><small>{{ attempt.errorCode }} · 请求摘要（request hash） {{ attempt.requestSha256.slice(0,12) }} · {{ dateTime(attempt.finishedAt || attempt.updatedAt) }}</small></div><PlatformStatusPill :status="attempt.status"/><button v-if="auth.user?.isStaff" class="button primary tiny" :disabled="runActionBlocked" @click="openUncertainResolution(attempt)">管理员消歧</button><small v-else>仅管理员可提交消歧结论</small></article></div></section>
      <StageExecutionPreparations v-show="currentStageVisible" v-if="currentStage" :stage-id="currentStage.id" :run-id="currentRun.id" @changed="refreshRun" @busy="actionBusy = $event ? 'preparation' : ''"/>
      <RecipeStageInputAmendment v-show="currentStageVisible" v-if="currentStage?.inputAmendmentPolicy==='explicit_project_text_input_sources_v1' && currentRun.executionPlan" :key="currentStage.id" :run-id="currentRun.id" :stage-id="currentStage.id" :plan-sha="currentRun.executionPlan.sha256" :sources="currentStageSources" project-text :disabled="runActionBlocked" />
      <ProductionTextCandidateReview v-show="currentStageVisible" v-if="currentRun.status==='awaiting_approval'&&currentStage&&['script_review','visual_rules_review'].includes(currentStage.stageKey)" :key="currentRun.id + ':' + currentStage.id" :run="currentRun" :stage="currentStage" :current-rules="currentReviewRules" @changed="refreshAdoptedContent" @busy="actionBusy = $event ? 'text-review' : ''"/>
      <RecipeReferenceMappingPreview v-show="productionTab==='storyboard'" :run="currentRun"/>
      <RecipeBibleReview v-show="productionTab==='bible'" v-if="currentRun.status==='awaiting_approval' && currentStage?.stageKey==='bible_review'" :key="currentRun.id" :run="currentRun" :disabled="runActionBlocked" @confirmation="bibleConfirmation = $event" />
      <section v-show="productionTab==='bible'" v-if="currentRun.status==='awaiting_approval'&&currentStage?.stageKey==='bible_image_review'" class="platform-card review-evidence">
        <header><div><strong>一致性设定集（Bible）参考图多来源审核</strong><small>每个实体可混合选择模型候选与素材库图片，并必须从全部已选图片中指定唯一主锚点（anchor）；未选模型版本仍保留在运行历史。</small></div></header>
        <div v-if="bibleImageReview.error" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ bibleImageReview.error }}</p></div>
        <div v-else-if="bibleImageReview.value" class="review-evidence-body">
          <dl class="review-hashes"><div><dt>生成阶段</dt><dd>{{ bibleImageReview.value.generationStageId }}</dd></div><div><dt>候选快照 SHA256</dt><dd>{{ bibleImageReview.value.snapshotSha256 }}</dd></div><div><dt>实体数量</dt><dd>{{ bibleImageReview.value.entities.length }}</dd></div></dl>
          <div v-if="bibleImageHistoryLoading" class="platform-loading compact"><LoaderCircle class="spin" :size="18"/>正在读取跨运行（Run）候选的完整性与预览证据…</div>
          <div v-else-if="bibleImageHistoryError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ bibleImageHistoryError }}</p></div>
          <BibleImageSelectionEditor v-model="bibleImageSelections" :key="bibleImageSelectionSignature"
            :entities="bibleImageReview.value.entities" :assets="studio.assets" :history="bibleImageHistoryByArtifactId"
            :loading="bibleImageHistoryLoading" :disabled="Boolean(actionBusy) || bibleImageHistoryLoading || Boolean(bibleImageHistoryError)">
            <template #entity-actions="{ entity }"><button class="button ghost tiny" type="button" @click="requestBibleRegeneration(entity)"><RefreshCw :size="13"/>重新生成候选</button></template>
          </BibleImageSelectionEditor>
        </div>
      </section>
      <RecipeStoryboardReferences v-show="productionTab==='storyboard'" v-if="currentRun.status==='awaiting_approval'&&currentStage?.stageKey==='storyboard_review'" :key="currentRun.id + ':' + currentStage.id" :run="currentRun" :stage-id="currentStage.id" :refresh-epoch="storyboardReviewRefreshEpoch" :disabled="runActionBlocked" @ready="storyboardReferenceReview=$event" @error="storyboardReferenceError=$event"/>
      <RecipeShotReferenceSelection v-show="productionTab==='storyboard'" v-if="currentRun.planSource === 'recipe' && currentRun.stages?.some(stage => stage.stageKey === 'storyboard_review')" :key="'shot-selection:' + currentRun.id + ':' + currentRun.executionPlan?.sha256" :run="currentRun" :disabled="runActionBlocked" @context-protected="protectRecipeShotSelection" @changed="refreshRun"/>
      <RecipeExecutionRouteChoices :stage-keys="visibleStages.map(stage=>stage.stageKey)" :run="currentRun" :busy="runActionBlocked" @select="selectRoute"/>
      <section v-if="['awaiting_approval','blocked'].includes(currentRun.status)&&currentStage&&costGate" class="platform-card">
        <strong>本轮费用与外部调用授权</strong><p>本轮所有阶段共用；当前阻断：{{ currentRun.blockedCode }}。</p>
        <ProductionBudgetFields v-model:external="authorizationExternal" :disabled="Boolean(actionBusy)"/>
        <label class="platform-field"><span>当前阶段预估费用授权（仅未知费用时填写）</span><input v-model="approvedEstimatedCost" inputmode="decimal"/></label>
        <BudgetCurrencySelect v-if="currentRun.blockedCode==='unknown_cost'||currentRun.blockedCode==='cost_currency_missing'||approvedEstimatedCost" v-model="authorizationCurrency" :disabled="Boolean(actionBusy)"/>
        <p v-if="costAuthorizationError" class="gate-error">{{ costAuthorizationError }}</p>
        <button class="button primary small" :disabled="runActionBlocked || Boolean(costAuthorizationError)" @click="authorizeCost">{{ actionBusy==='cost'?'提交中…':'确认本轮授权并继续处理当前阻断' }}</button>
      </section>
      <section v-show="currentStageVisible" v-else-if="currentRun.status==='awaiting_approval'&&currentStage&&currentRun.blockedCode!=='route_selection_required'&&!['script_review','visual_rules_review'].includes(currentStage.stageKey)" class="approval-gate">
        <div><strong>阶段审批 · {{ currentStage.stageKey }}</strong><p>审批结果会持久化；正式对象不完整时服务端仍会拒绝继续。</p><p v-if="reviewApprovalError" class="gate-error">{{ reviewApprovalError }}</p></div>
        <select v-model="approvalDecision"><option value="approve">批准并继续</option><option value="reject">拒绝并终止</option></select>
        <input v-model="approvalComment" placeholder="审批说明（拒绝时请说明原因）"/>
        <button class="button primary small" :disabled="runActionBlocked || (approvalDecision==='approve'&&Boolean(reviewApprovalError))" @click="decide">{{ actionBusy==='approve'?'提交中…':'提交审批' }}</button>
      </section>
      <section class="run-stage-strip" :aria-label="productionTab==='records'?'全部生产阶段':'本栏生产阶段'">
        <article v-for="stage in currentRun.stages" v-show="visibleStages.some(item=>item.id===stage.id)" :key="stage.id" :class="{current:stage.stageKey===currentRun.currentStageKey,expanded:expandedStageId===stage.id}">
          <button class="run-stage-toggle" type="button" :disabled="Boolean(actionBusy)||Boolean(artifactSelectionProtection)" :aria-expanded="expandedStageId===stage.id" :aria-controls="`stage-details-${stage.id}`" @click="toggleStageDisclosure(stage.id)">
            <header><span>{{ String(stage.ordinal).padStart(2,'0') }}</span><PlatformStatusPill :status="stage.status"/></header><strong>{{ runStageLabel(currentRun,stage) }}</strong><small title="阶段累计执行计数；模型阶段包含多个主调用和辅助调用，不是某个任务的重试次数。">{{ modeLabel(stage.mode) }} · 阶段执行计数 {{ stage.attemptCount }}（非单任务重试次数）</small><ChevronDown :size="14"/>
          </button>
          <div v-if="expandedStageId===stage.id" :id="`stage-details-${stage.id}`" class="run-stage-details">
            <ProductionStructuredTextResults :key="currentRun.id + ':' + stage.id" :run="currentRun" :stage="stage" :disabled="runActionBlocked" @changed="artifactSelected" @protection="protectArtifactSelection"/>
            <RecipeMaterialFulfillment :key="'material:' + currentRun.id + ':' + stage.id" :run="currentRun" :stage="stage" :disabled="runActionBlocked" @changed="refreshRun" @busy="actionBusy = $event ? 'material-fulfillment' : ''"/>
            <p v-if="stage.reason||stage.reasonCode"><code>{{ stageIssuePresentation(stage).code }}</code> · {{ stageIssuePresentation(stage).summary }}</p><p v-else>本阶段没有服务端原因说明。</p>
            <div v-if="stage.decisionPolicy" class="stage-policy-evidence"><strong>决策策略</strong><code>{{ stage.decisionPolicy }}</code><small>策略摘要（SHA-256） · {{ stage.decisionPolicySha256 }}</small></div>
            <details v-if="stage.approvalDecisionFacts.length" class="stage-policy-decisions"><summary>审批审计事实 · {{ stage.approvalDecisionFacts.length }}</summary><article v-for="fact in stage.approvalDecisionFacts" :key="fact.id"><strong>{{ fact.actor==='recipe_policy'?'配方策略自动决策':'人工决策' }} · {{ fact.decision }}</strong><small>修订版（revision）{{ fact.revision }} · {{ dateTime(fact.createdAt) }} · {{ fact.decisionPolicy || '无策略' }}</small><code>{{ fact.outputSnapshotSha256 }}</code></article></details>
            <dl><div><dt>输入快照</dt><dd><code>{{ stage.inputSnapshotSha256 || '—' }}</code></dd></div><div><dt>输出快照</dt><dd><code>{{ stage.outputSnapshotSha256 || '—' }}</code></dd></div><div><dt>已批费用</dt><dd>{{ stage.approvedEstimatedCost || '—' }} {{ stage.approvedCostCurrency }}</dd></div></dl>
          </div>
        </article>
      </section>
      <section v-if="productionTab==='records'" class="run-facts"><div><span>剧本版本（revision）</span><code>{{ currentRun.scriptRevision }}</code></div><div><span>生产配方（Recipe）</span><code>{{ currentRun.recipeVersion }}</code></div><div><span>配置快照</span><code>{{ currentRun.configSnapshotSha256 }}</code></div><div><span>费用小计（预估 / 承诺 / 已记录实际）</span><strong>{{ currentRun.estimatedCostTotal }} / {{ currentRun.committedCostTotal }} / {{ currentRun.actualCostTotal }} {{ currentRun.costCurrency }}</strong><small class="run-cost-note">{{ recordedCostSummaryNote }}</small></div><div><span>发行包（ReleaseBundle）</span><code>{{ currentRun.releaseBundleId || '尚未封存' }}</code></div><div><span>结束</span><strong>{{ dateTime(currentRun.finishedAt) }}</strong></div></section>
      <ProductionReleaseDelivery v-if="productionTab==='delivery' && currentRun.releaseBundleId" :bundle-id="currentRun.releaseBundleId" :run-id="currentRun.id" :project-id="currentRun.project"/>
      <section v-show="productionTab==='media' || productionTab==='delivery' || productionTab==='bible'" class="artifact-groups"><header><div><strong>{{productionTab==='bible'?'参考图候选':productionTab==='delivery'?'装配产物':'媒体候选与选片'}}</strong><span>同组 2–4 个版本可并排比较；候选选择与阶段审核分别处理，不等同于质量验收。</span></div></header><GeneratedArtifactCompare v-for="group in artifactGroups" v-show="artifactGroupSection(group)===productionTab" :key="currentRun.id + ':' + group.key" :run-id="currentRun.id" :scope-id="group.key" :disabled="runActionBlocked" :artifacts="group.items" :material-selection="group.materialSelection" @selected="artifactSelected" @protection="protectArtifactSelection"/><p v-if="!artifactGroups.some(group=>artifactGroupSection(group)===productionTab)" class="platform-empty">本栏目尚未归档可比较的媒体产物。</p></section>
      <BaseModal :open="Boolean(pendingBibleRegeneration)" :dismissible="!Boolean(actionBusy)" title="确认重新生成 Bible 候选" eyebrow="追加候选 / APPEND CANDIDATES" width="680px" @close="closeBibleRegeneration">
        <dl v-if="pendingBibleRegeneration" class="run-confirmation-details"><div><dt>目标实体</dt><dd>{{ pendingBibleRegeneration.entityName }} · {{ pendingBibleRegeneration.entityType }}</dd></div><div><dt>生产运行（Run）</dt><dd><code>{{ pendingBibleRegeneration.runId }}</code></dd></div><div><dt>审核快照（SHA-256）</dt><dd><code>{{ pendingBibleRegeneration.snapshotSha256 }}</code></dd></div><div><dt>当前候选</dt><dd>{{ pendingBibleRegeneration.existingCandidateCount }} 个；本操作只追加新候选，不删除或覆盖现有候选。</dd></div><div><dt>执行实例</dt><dd>使用本运行已冻结的 Bible 执行实例版本及输入来源、参数规则；不能在此临时更换模型或策略。</dd></div><div><dt>费用与外发</dt><dd>确认后创建新的执行准备，字段审核与运行准入通过后才创建模型调用，可能产生新费用。已有候选保留，生成完成后一起审核。</dd></div></dl>
        <p class="platform-empty">随机种子：支持种子时，新候选在合法范围内抽取并冻结未被本运行该实体使用的种子；失败重试、刷新和重复提交不重抽。固定种子或合法值用尽会阻止追加，需调整版本；新种子不保证画面明显不同。</p>
        <div v-if="pendingBibleRegenerationError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ pendingBibleRegenerationError }}</p></div>
        <template #footer><button class="button ghost" :disabled="Boolean(actionBusy)" @click="closeBibleRegeneration">返回</button><button class="button primary" :disabled="runActionBlocked" @click="confirmBibleRegeneration">{{ actionBusy==='regenerate-bible-images'?'正在提交…':'确认追加生成' }}</button></template>
      </BaseModal>
      <BaseModal :open="Boolean(uncertainResolutionAttempt)" :dismissible="!uncertainResolutionSaving" title="管理员消歧远端尝试（attempt）" eyebrow="仅追加的消歧记录 / APPEND-ONLY RESOLUTION" width="700px" @close="closeUncertainResolution">
        <div v-if="uncertainResolutionAttempt" class="registry-action-review">
          <p>请在服务提供方（Provider）控制台或账单中核对后再提交。该操作只追加不可变消歧记录；不会把人工结论展示为烟测/通过（smoke/pass）。</p>
          <dl><div><dt>操作能力（Operation）</dt><dd>{{ operationLabel(uncertainResolutionAttempt.operation) }}</dd></div><div><dt>错误代码</dt><dd><code>{{ uncertainResolutionAttempt.errorCode }}</code></dd></div><div><dt>请求快照摘要（SHA-256）</dt><dd><code>{{ uncertainResolutionAttempt.requestSnapshotSha256 }}</code></dd></div><div><dt>服务提供方请求标识证据（Provider request ID）</dt><dd><code>{{ uncertainResolutionAttempt.providerRequestIdSha256 || '尚无摘要（hash）' }}</code></dd></div></dl>
          <label class="platform-field"><span>核验结论</span><select v-model="uncertainResolutionOutcome"><option v-for="option in uncertainResolutionOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
          <label class="platform-field"><span>核验说明（{{ productionCapability?.effectiveLimits.auditNoteMinCharacters || '—' }}–{{ productionCapability?.effectiveLimits.auditNoteMaxCharacters || '—' }} 个字符）</span><textarea v-model="uncertainResolutionNote" :disabled="!productionCapability" rows="4" :maxlength="productionCapability?.effectiveLimits.auditNoteMaxCharacters" placeholder="说明查阅了哪个控制台、账单时间范围和可复核结论"></textarea></label>
          <label v-if="uncertainResolutionOutcome==='confirmed_remote_submitted'" class="platform-field"><span>服务提供方请求标识（Provider request ID，仅提交，页面不回显）</span><input v-model="uncertainResolutionProviderRequestId" type="password" maxlength="191" autocomplete="off"/></label>
          <div v-if="uncertainResolutionOutcome==='confirmed_remote_failed'||uncertainResolutionOutcome==='actual_cost_confirmed'" class="platform-form-grid"><label class="platform-field"><span>实际费用（可填 0）</span><input v-model="uncertainResolutionActualCost" inputmode="decimal" placeholder="0"/></label><label class="platform-field"><span>币种（费用大于 0 必填）</span><input v-model="uncertainResolutionCurrency" maxlength="12" placeholder="USD"/></label></div>
          <div v-if="uncertainResolutionValidation" class="platform-alert warning"><AlertTriangle :size="15"/><p>{{ uncertainResolutionValidation }}</p></div>
          <div v-if="uncertainResolutionError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ uncertainResolutionError }}</p></div>
        </div>
        <template #footer><button class="button ghost" :disabled="uncertainResolutionSaving" @click="closeUncertainResolution">取消</button><button class="button primary" :disabled="runActionBlocked || Boolean(uncertainResolutionValidation)" @click="submitUncertainResolution">{{ uncertainResolutionSaving?'提交中…':'追加消歧记录' }}</button></template>
      </BaseModal>
      </div>
      </ProductionWorkspacePanel>
    </template>
    <BaseModal :open="Boolean(pendingRunConfirmation)" :dismissible="!Boolean(actionBusy)" :title="pendingRunConfirmation?.action==='retry'?'确认重试当前阶段':'确认取消运行'" eyebrow="生产运行确认 / PRODUCTION RUN CONFIRMATION" width="660px" @close="closeRunConfirmation">
      <dl v-if="pendingRunConfirmation" class="run-confirmation-details">
        <div><dt>生产运行（Run）</dt><dd><code>{{ pendingRunConfirmation.runId }}</code></dd></div>
        <div><dt>当前阶段</dt><dd>{{ pendingRunConfirmation.stageKey }}</dd></div>
        <div v-if="pendingRunConfirmation.action==='retry'"><dt>本次确认范围</dt>
          <dd v-if="pendingRunConfirmation.retryContext?.expectedAssemblyJobIds">{{ pendingRunConfirmation.retryContext.expectedAssemblyJobIds.length }} 个失败装配任务；输入 SHA：{{ pendingRunConfirmation.retryContext.expectedInputSha256 || '未冻结' }}。</dd>
          <dd v-else>{{ pendingRunConfirmation.retryContext?.expectedAttemptIds.length ?? '未读取' }} 个失败主调用；无模型主调用时按原阶段流程重试。</dd>
        </div>
        <div><dt>影响</dt><dd v-if="pendingRunConfirmation.action==='retry' && pendingRunConfirmation.stageKey==='assembly'">仅重新装配本次确认的失败任务，保留原选片、顺序及消费输入；成功及在途任务不重跑，不重新生成媒体。结果未知时先重新读取原任务。</dd>
          <dd v-else>{{ pendingRunConfirmation.action==='retry'?'仅重试本次确认的失败调用，复用原提示词、素材和参数（种子不变）；已成功候选不重跑。':'服务端将请求停止未完成阶段；已外发请求可能仍需等待远端终态，晚到结果不会自动采用。' }}</dd>
        </div>
        <div><dt>费用提示</dt><dd>预估 {{ pendingRunConfirmation.estimatedCost }} / 已承诺 {{ pendingRunConfirmation.committedCost }} / 已记录实际 {{ pendingRunConfirmation.actualCost }} {{ pendingRunConfirmation.currency }}。{{ recordedCostSummaryNote }}{{ pendingRunConfirmation.action==='retry'?(pendingRunConfirmation.stageKey==='assembly'?'本次仅装配，不提交模型调用。':'重试可能增加费用。'):'取消不代表已发生费用会撤销。' }}</dd></div>
      </dl>
      <ProductionRetryPreview v-if="pendingRunConfirmation?.action==='retry' && pendingRunConfirmation.retryContext" :run-id="pendingRunConfirmation.runId" :context="pendingRunConfirmation.retryContext" @ready="retryPreviewReady=$event"/>
      <div v-if="pendingRunConfirmationError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ pendingRunConfirmationError }}</p></div>
      <template #footer><button class="button ghost" :disabled="Boolean(actionBusy)" @click="closeRunConfirmation">返回</button><button v-if="pendingRunConfirmation" class="button" :class="pendingRunConfirmation.action==='cancel'?'danger':'primary'" :disabled="runActionBlocked || (pendingRunConfirmation.action==='retry' && !retryPreviewReady)" @click="confirmRunAction">{{ actionBusy ? '服务端处理中…' : pendingRunConfirmation.action==='retry'?'确认重试':'确认取消' }}</button></template>
    </BaseModal>
  </div>
</template>

<style scoped>
.workspace-section-heading{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.workspace-section-heading h3{font-size:18px;margin:0}.workspace-section-heading p{width:100%;margin:0;color:var(--text-muted)}
.production-progress{display:grid;gap:20px;min-width:0}

.run-cost-note{color:var(--text-secondary);font-size:12px;line-height:1.6;overflow-wrap:anywhere}
.production-setup-grid{min-width:0;margin:0;padding:0;border:0}
.production-duration-hint{display:block;margin:10px 12px;color:var(--text-secondary);font-size:12px;line-height:1.6;overflow-wrap:anywhere}
.script-preview>header{display:flex;align-items:flex-start;flex-wrap:wrap;gap:8px}.script-preview>header>div{display:grid;flex:1 1 220px;min-width:0;gap:4px}.script-preview>header small{color:var(--text-secondary);font:10px/1.6 Consolas,monospace;overflow-wrap:anywhere}
.manual-budget-editor,.manual-preparation-records{display:grid;gap:12px;min-width:0}.manual-budget-editor>.button{justify-self:start}.manual-preparation-records>p{font-size:13px;color:var(--text-secondary)}
.visual-rule-review-list{display:grid;gap:12px}.visual-rule-review-list>article{display:grid;gap:11px;padding:14px;border:1px solid #28333d;border-radius:9px;background:#0a0f13}.visual-rule-review-list>article>header,.visual-rule-review-list>article>footer{display:flex;align-items:center;justify-content:space-between;gap:12px}.visual-rule-review-list>article>header small{color:#78848d;font:9px Consolas,monospace}.visual-rule-review-list>article>footer label{display:flex;align-items:center;gap:7px;color:#c4cbd0;font-size:10px}.visual-rule-columns{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.visual-rule-columns textarea{height:132px;resize:vertical}.visual-rule-columns label:first-child textarea{color:#8d979f;background:#080c10}.visual-rule-columns label:nth-child(2) textarea{color:#a9c8ba;background:#08100d}@media(max-width:1000px){.visual-rule-columns{grid-template-columns:1fr}}
.advisory-warning{color:#d7a65b;font-size:9px;line-height:1.5}
.storyboard-reference-review>header{align-items:flex-start}.storyboard-reference-episode{display:grid;gap:12px;padding:14px;border:1px solid #28333d;border-radius:9px;background:#0a0f13}.storyboard-reference-episode>header small{display:block;margin-top:4px;color:#7d8992;font-size:10px}.storyboard-reference-shot{display:grid;gap:12px;padding:12px;border:1px solid #222d35;border-radius:8px;background:#080c10}.storyboard-reference-shot>header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.storyboard-reference-shot>header>div{display:grid;gap:4px}.storyboard-reference-shot>header small{color:#7d8992;font-size:10px}.storyboard-reference-shot>header code{max-width:45%;overflow:hidden;color:#687682;font-size:9px;text-overflow:ellipsis}.storyboard-reference-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}.storyboard-media-binding{display:grid;grid-column:1/-1;gap:10px;padding:12px;border:1px solid #27343d;border-radius:8px;background:#0a0f13}.storyboard-media-binding>header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.storyboard-media-binding>header>div{display:grid;gap:4px}.storyboard-media-binding>header small{color:#7d8992;font-size:10px;line-height:1.5}.file-button{position:relative;overflow:hidden}.file-button input{position:absolute;inset:0;cursor:pointer;opacity:0}.storyboard-media-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:7px}.storyboard-media-options>label{display:grid;grid-template-columns:auto 1fr;gap:3px 7px;padding:8px;border:1px solid #27323a;border-radius:6px;color:#bdc5ca;font-size:10px}.storyboard-media-options>label small{grid-column:2;color:#74808a;font-size:9px}.storyboard-media-options>label.invalid{opacity:.5}.storyboard-video-binding{display:grid;gap:9px;padding:10px;border:1px solid rgba(230,167,90,.18);border-radius:7px;background:rgba(230,167,90,.025)}.storyboard-video-binding>header{display:flex;align-items:center;justify-content:space-between;gap:10px}.storyboard-video-binding>header strong{overflow:hidden;color:#c6b292;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.storyboard-media-controls,.storyboard-audio-bindings{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:9px}.storyboard-reference-previews{display:flex;grid-column:1/-1;gap:10px;overflow-x:auto}.storyboard-reference-previews figure{display:grid;flex:0 0 130px;gap:5px;margin:0}.storyboard-reference-previews img,.storyboard-media-kind{width:130px;aspect-ratio:1/1;border-radius:6px;object-fit:cover}.storyboard-media-kind{display:flex;align-items:center;justify-content:center;border:1px solid #2b3841;background:#111820;color:#708491;font:10px Consolas,monospace}.storyboard-reference-previews figcaption{overflow:hidden;color:#89949d;font-size:9px;text-overflow:ellipsis;white-space:nowrap}
.unattended-run-panel{grid-column:1/-1}.unattended-confirmation{align-items:flex-start;padding:11px;border:1px solid rgba(230,167,90,.3);border-radius:7px;background:rgba(230,167,90,.04)}
.stage-policy-evidence{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 9px;padding:8px;border:1px solid #28333d;border-radius:7px;background:#0a0f13}.stage-policy-evidence strong{font-size:10px}.stage-policy-evidence code{overflow-wrap:anywhere}.stage-policy-evidence small{grid-column:1/-1;color:#73808a;font-size:9px;overflow-wrap:anywhere}.stage-policy-decisions{padding:8px;border:1px solid #28333d;border-radius:7px}.stage-policy-decisions summary{cursor:pointer;font-size:10px}.stage-policy-decisions article{display:grid;gap:3px;padding-top:8px}.stage-policy-decisions article small,.stage-policy-decisions article code{color:#73808a;font-size:9px;overflow-wrap:anywhere}
</style>
