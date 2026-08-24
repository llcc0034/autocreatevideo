<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AlertTriangle, ArrowLeft, Check, ChevronDown, CircleStop, FileText, GitBranch, LoaderCircle, Play, RefreshCw, RotateCcw, Upload } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import GeneratedArtifactCompare from '@/components/GeneratedArtifactCompare.vue'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { productionRecipeScriptMode } from '@/services/platformAdapters'
import { platformApi } from '@/services/platformApi'
import { parseBibleReviewPayload, parsePromptNormalizationReviewPayload, parseScriptReviewPayload, parseStoryboardReviewPayload } from '@/services/productionRunReview'
import { actionableProductionRuns, productionIssuePresentation, productionRunBelongsToProject, restorableProductionRunId, stageIssuePresentation } from '@/services/productionRunPresentation'
import { reconcileExpandedStageId } from '@/services/productionStageDisclosure'
import type { GeneratedArtifact, GenerationAttempt, JsonObject, ProductionRecipe, ProductionRecipeMediaRouteOption, ProductionRecipeScriptClassification, ProductionRun, ScriptRevision, ScriptSource, UncertainResolutionOutcome } from '@/services/platformTypes'
import { useAuthStore } from '@/stores/auth'
import { useStudioStore } from '@/stores/studio'
import type { Shot as ProjectShot } from '@/types'

const route = useRoute()
const router = useRouter()
const studio = useStudioStore()
const auth = useAuthStore()
const projectId = computed(() => String(route.params.projectId || ''))
const runId = computed(() => String(route.params.runId || ''))
const project = computed(() => studio.projectById(projectId.value))
const loading = ref(false)
const actionBusy = ref('')
const error = ref('')
const notice = ref('')
const sources = ref<ScriptSource[]>([])
const revisions = ref<ScriptRevision[]>([])
const recipes = ref<ProductionRecipe[]>([])
const recipeMediaRoutes = ref<ProductionRecipeMediaRouteOption[]>([])
const recipeMediaRoutesLoading = ref(false)
const recipeMediaRoutesError = ref('')
const runs = ref<ProductionRun[]>([])
const currentRun = ref<ProductionRun | null>(null)
const scriptMode = ref<'paste' | 'file' | 'existing'>('paste')
const scriptText = ref('')
const scriptFormat = ref('markdown')
const scriptFile = ref<File | null>(null)
const previewRevision = ref<ScriptRevision | null>(null)
const selectedRevisionId = ref('')
const selectedRecipeId = ref('')
const episodeCountHint = ref(1)
const allowExternalCalls = ref<boolean | null>(null)
const budgetLimit = ref('')
const maxGenerationTasks = ref<number | null>(null)
const configOverridesText = ref('{}')
const idempotencyKey = ref(crypto.randomUUID())
const aiIdea = ref('')
const aiStyleHint = ref('')
const shotsPerEpisode = ref(8)
const episodeDurationSeconds = ref(90)
const selectedEpisodeIds = ref<string[]>([])
const workflowSelectionMode = ref<'uniform' | 'per_shot'>('uniform')
const uniformWorkflowRouteIndex = ref<number | null>(null)
const shotWorkflowRouteIndexes = ref<Record<string, number | null>>({})
const episodeWorkflowRouteIndexes = ref<Record<string, number | null>>({})
const approvalDecision = ref('approve')
const approvalComment = ref('')
const adoptGeneratedMetadata = ref(false)
const normalizationEdits = ref<Record<string, string>>({})
const normalizationReviewed = ref<Record<string, boolean>>({})
const normalizationEditSignature = ref('')
const routeIndex = ref<number | null>(null)
const approvedEstimatedCost = ref('')
const authorizationCurrency = ref('')
const authorizationBudget = ref('')
const authorizationExternal = ref(false)
const reviewRevision = ref<ScriptRevision | null>(null)
const reviewRevisionLoading = ref(false)
const reviewRevisionError = ref('')
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
  estimatedCost: string
  committedCost: string
  actualCost: string
  currency: string
}
const pendingRunConfirmation = ref<PendingRunConfirmation | null>(null)
const pendingRunConfirmationError = ref('')
const uncertainAttempts = ref<GenerationAttempt[]>([])
const uncertainAttemptsLoading = ref(false)
const uncertainAttemptsError = ref('')
const uncertainResolutionAttempt = ref<GenerationAttempt | null>(null)
const uncertainResolutionSaving = ref(false)
const uncertainResolutionError = ref('')
const uncertainResolutionOutcome = ref<UncertainResolutionOutcome>('confirmed_not_submitted')
const uncertainResolutionNote = ref('')
const uncertainResolutionProviderRequestId = ref('')
const uncertainResolutionActualCost = ref('')
const uncertainResolutionCurrency = ref('')
let reviewLoadSequence = 0
let uncertainLoadSequence = 0
let recipeMediaRouteLoadSequence = 0
let pollTimer: number | undefined

const terminal = new Set(['succeeded', 'failed', 'cancelled'])
const costGateCodes = new Set(['unknown_cost', 'budget_limit_exceeded', 'external_budget_missing', 'external_call_not_authorized', 'cost_currency_missing'])
const message = (cause: unknown) => apiErrorSummary(cause, '服务端请求失败')
const dateTime = (value: string) => value ? new Date(value).toLocaleString('zh-CN') : '—'
const selectedRevision = computed(() => revisions.value.find((item) => item.id === selectedRevisionId.value) || (previewRevision.value?.id === selectedRevisionId.value ? previewRevision.value : null))
const projectMasterScript = computed(() => project.value?.masterScript.trim() || '')
const projectEpisodes = computed(() => project.value?.episodes || [])
const currentStage = computed(() => currentRun.value?.stages?.find((item) => item.stageKey === currentRun.value?.currentStageKey))
const selectedRecipe = computed(() => recipes.value.find((item) => item.id === selectedRecipeId.value))
const selectedScriptMode = computed<ProductionRecipeScriptClassification>(() => productionRecipeScriptMode(selectedRecipe.value?.definition || {}))
const recipeHasMediaGeneration = computed(() => {
  const stages = selectedRecipe.value?.definition?.stages
  return Array.isArray(stages) && stages.some((value) => (
    value && typeof value === 'object' && !Array.isArray(value)
    && String((value as JsonObject).key || '') === 'media_generation'
  ))
})
const isAiScriptRecipe = computed(() => selectedScriptMode.value === 'ai')
const isConfirmedScriptRecipe = computed(() => selectedScriptMode.value === 'confirmed_revision')
const isDownstreamRecipe = computed(() => selectedScriptMode.value === 'downstream')
const isInvalidRecipe = computed(() => selectedScriptMode.value === 'invalid')
const workflowEpisodes = computed(() => {
  if (!isDownstreamRecipe.value || !selectedEpisodeIds.value.length) return projectEpisodes.value
  const selected = new Set(selectedEpisodeIds.value)
  return projectEpisodes.value.filter((episode) => selected.has(episode.id))
})
const workflowShotRows = computed(() => workflowEpisodes.value.flatMap((episode) => (
  episode.shots.map((shot) => ({ episode, shot }))
)))
const routeLabel = (option: ProductionRecipeMediaRouteOption) => {
  const output = option.outputKinds.length ? option.outputKinds.join('/') : '输出未声明'
  const state = option.isSelectable ? output : `阻断：${option.reasons.join('；') || '依赖不可用'}`
  return `Route ${option.routeIndex} · ${option.workflowLabel} · ${option.deploymentLabel} · ${state}`
}
const shotHasReferences = (shot: ProjectShot) => {
  const bindings = shot.referenceBindings
  return Boolean(bindings && (
    bindings.firstFrameAssetId
    || bindings.lastFrameAssetId
    || bindings.firstFrameReferenceAssetId
    || bindings.scene
    || bindings.characters.length
    || bindings.props.length
    || bindings.videos.length
    || bindings.audios.length
  ))
}
const routeSupportsShot = (option: ProductionRecipeMediaRouteOption, shot: ProjectShot) => (
  option.isSelectable
  && option.outputKinds.includes(shot.mediaType)
  && (shot.mediaType !== 'video' || !option.h3Modes.length || option.h3Modes.includes(shot.mode))
  && !(option.driverType === 'async_rest' && shot.mediaType === 'video' && shotHasReferences(shot))
)
const routeSupportsEpisode = (option: ProductionRecipeMediaRouteOption, episode: typeof workflowEpisodes.value[number]) => (
  option.isSelectable && episode.shots.every((shot) => routeSupportsShot(option, shot))
)
const routeSupportsUniform = (option: ProductionRecipeMediaRouteOption) => (
  option.isSelectable && workflowShotRows.value.every(({ shot }) => routeSupportsShot(option, shot))
)
const uniformWorkflowOptions = computed(() => recipeMediaRoutes.value.filter((option) => (
  routeSupportsUniform(option)
)))
const episodeRouteCanApply = (episode: typeof workflowEpisodes.value[number]) => {
  const index = episodeWorkflowRouteIndexes.value[episode.id]
  const option = recipeMediaRoutes.value.find((item) => item.routeIndex === index)
  return Boolean(option && routeSupportsEpisode(option, episode))
}
const workflowSelectionError = computed(() => {
  if (!selectedRecipeId.value || !recipeHasMediaGeneration.value) return ''
  if (recipeMediaRoutesLoading.value) return '正在核对配方媒体 route 的工作流与部署依赖。'
  if (recipeMediaRoutesError.value) return `媒体 route 依赖读取失败：${recipeMediaRoutesError.value}`
  const selectable = new Set(recipeMediaRoutes.value.filter((item) => item.isSelectable).map((item) => item.routeIndex))
  if (!recipeMediaRoutes.value.length) return '所选配方没有 media_generation route，请先在系统设置发布完整配方。'
  if (!selectable.size) return '所选配方的媒体 route 均存在依赖缺口；请按下方阻断原因修复工作流或部署。'
  if (workflowSelectionMode.value === 'uniform') {
    if (uniformWorkflowRouteIndex.value === null || !selectable.has(uniformWorkflowRouteIndex.value)) return '请选择依赖完整的整集统一工作流。'
    const option = recipeMediaRoutes.value.find((item) => item.routeIndex === uniformWorkflowRouteIndex.value)
    const incompatible = option ? workflowShotRows.value.filter(({ shot }) => !routeSupportsShot(option, shot)) : []
    if (incompatible.length) return `当前统一工作流与 ${incompatible.length} 个镜头的输出类型或 H3 模式不兼容。`
    return ''
  }
  if (!workflowShotRows.value.length) return '逐镜头模式要求项目已有正式 Shot；AI/导入尚未拆分时只能选择整集统一工作流。'
  const missing = workflowShotRows.value.filter(({ shot }) => {
    const index = shotWorkflowRouteIndexes.value[shot.id]
    const option = recipeMediaRoutes.value.find((item) => item.routeIndex === index)
    return index === null || index === undefined || !option || !routeSupportsShot(option, shot)
  })
  return missing.length ? `还有 ${missing.length} 个镜头未选择兼容且依赖完整的工作流。` : ''
})
const applyEpisodeWorkflow = (episodeId: string) => {
  const routeIndex = episodeWorkflowRouteIndexes.value[episodeId]
  if (routeIndex === null || routeIndex === undefined) return
  const episode = workflowEpisodes.value.find((item) => item.id === episodeId)
  const option = recipeMediaRoutes.value.find((item) => item.routeIndex === routeIndex)
  if (!episode || !option || !routeSupportsEpisode(option, episode)) return
  shotWorkflowRouteIndexes.value = {
    ...shotWorkflowRouteIndexes.value,
    ...Object.fromEntries(episode.shots.map((shot) => [shot.id, routeIndex])),
  }
}
const routeGate = computed(() => currentRun.value?.blockedCode === 'route_selection_required')
const costGate = computed(() => costGateCodes.has(currentRun.value?.blockedCode || ''))
const isUncertainRemote = computed(() => currentRun.value?.blockedCode === 'uncertain_remote_response' || currentStage.value?.reasonCode === 'uncertain_remote_response')
const needsAttemptResolution = computed(() => ['uncertain_remote_response', 'actual_cost_reconciliation_required'].some((code) => currentRun.value?.blockedCode === code || currentStage.value?.reasonCode === code))
const scriptReview = computed(() => parseScriptReviewPayload(currentRun.value?.stages || []))
const bibleReview = computed(() => parseBibleReviewPayload(currentRun.value?.stages || []))
const storyboardReview = computed(() => parseStoryboardReviewPayload(currentRun.value?.stages || []))
const normalizationReview = computed(() => parsePromptNormalizationReviewPayload(currentRun.value?.stages || []))
const blockedPresentation = computed(() => productionIssuePresentation(currentRun.value?.blockedCode || currentRun.value?.status || '', currentRun.value?.blockedReason))
const actionableRuns = computed(() => actionableProductionRuns(runs.value))
const routeOptions = computed(() => {
  const routes = currentStage.value?.routing?.routes
  if (!Array.isArray(routes)) return []
  return routes.flatMap((value, fallbackIndex) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return []
    const row = value as JsonObject
    const parsedIndex = Number(row.index)
    return [{
      index: Number.isInteger(parsedIndex) ? parsedIndex : fallbackIndex,
      driverType: String(row.driver_type || '未声明'),
      providerId: String(row.provider_id || ''),
      modelProfileVersionId: String(row.model_profile_version_id || ''),
      modelDeploymentId: String(row.model_deployment_id || ''),
      workflowVersionId: String(row.workflow_version_id || ''),
      workflowOutputKind: String(row.workflow_output_kind || ''),
      workflowH3Modes: Array.isArray(row.workflow_h3_modes) ? row.workflow_h3_modes.map(String).join(', ') : '',
      isExternal: row.is_external === true,
    }]
  })
})
const canCreate = computed(() => Boolean(selectedRecipeId.value && !actionBusy.value && !isInvalidRecipe.value && !workflowSelectionError.value && (
  isAiScriptRecipe.value
    ? projectEpisodes.value.length === 0 && Boolean(aiIdea.value.trim() && episodeCountHint.value > 0 && shotsPerEpisode.value > 0 && episodeDurationSeconds.value > 0)
    : isConfirmedScriptRecipe.value
      ? selectedRevision.value?.status === 'confirmed'
      : projectEpisodes.value.length > 0
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
  if (!note) return '请填写远端控制台、账单或人工核验依据。'
  if (outcome === 'confirmed_remote_submitted' && !uncertainResolutionProviderRequestId.value.trim()) return '确认远端已提交时必须填写 Provider request ID。'
  if ((outcome === 'confirmed_remote_failed' || outcome === 'actual_cost_confirmed') && (cost === null || !Number.isFinite(cost) || cost < 0)) return '该结果必须填写有限、非负的实际费用（可填 0）。'
  if (cost !== null && cost > 0 && !uncertainResolutionCurrency.value.trim()) return '实际费用大于 0 时必须填写币种。'
  return ''
})
const artifactGroups = computed(() => {
  const groups = new Map<string, GeneratedArtifact[]>()
  for (const item of currentRun.value?.artifacts || []) {
    const mediaKind = item.kind === 'image' || item.kind === 'video' || item.kind === 'assembly' || item.mimeType.startsWith('image/') || item.mimeType.startsWith('video/')
    if (!mediaKind) continue
    const key = item.variantGroup || `artifact:${item.id}`
    groups.set(key, [...(groups.get(key) || []), item])
  }
  return [...groups.entries()].map(([key, items]) => ({ key, items: items.sort((a, b) => a.variantIndex - b.variantIndex) }))
})
const internalNamePlaceholder = /^(?:S[1-9][0-9]*|(?:character|scene|prop):[a-z]+-[0-9a-f]+)$/i
const normalizationCandidateError = (requestId: string, fieldKey: string) => {
  const value = normalizationEdits.value[requestId]
  if (!value?.trim()) return '英文候选不能为空。'
  if (value.length > 8000) return '英文候选不能超过 8000 个字符。'
  if (fieldKey.endsWith('.name') && internalNamePlaceholder.test(value.trim())) return '名称仍是内部占位符，请改为可读英文名称。'
  return ''
}
const normalizationReviewedCount = computed(() => normalizationReview.value.value?.candidates.filter((candidate) => normalizationReviewed.value[candidate.requestId]).length || 0)
const restoreNormalizationCandidate = (requestId: string) => {
  const candidate = normalizationReview.value.value?.candidates.find((item) => item.requestId === requestId)
  if (!candidate) return
  normalizationEdits.value[requestId] = candidate.translatedText
  normalizationReviewed.value[requestId] = false
}
const reviewApprovalError = computed(() => {
  if (currentStage.value?.stageKey === 'script_review') {
    if (scriptReview.value.error) return scriptReview.value.error
    if (reviewRevisionLoading.value) return '正在读取完整 ScriptRevision 正文，请稍候。'
    if (reviewRevisionError.value) return reviewRevisionError.value
    if (!reviewRevision.value?.content?.trim()) return 'ScriptRevision 正文为空，不能批准。'
  }
  if (currentStage.value?.stageKey === 'bible_review') {
    if (bibleReview.value.error) return bibleReview.value.error
    if (bibleReview.value.value?.scriptRevisionId !== currentRun.value?.scriptRevision) return 'Bible 候选对应的 ScriptRevision 与本次运行不一致。'
  }
  if (currentStage.value?.stageKey === 'storyboard_review' && storyboardReview.value.error) return storyboardReview.value.error
  if (currentStage.value?.stageKey === 'script_normalize_review') {
    if (normalizationReview.value.error) return normalizationReview.value.error
    const payload = normalizationReview.value.value
    if (!payload) return '提示词标准化审核证据不可用。'
    if (payload.candidates.some((candidate) => !normalizationReviewed.value[candidate.requestId])) return `请逐项核对全部英文候选（${normalizationReviewedCount.value}/${payload.candidates.length}）。`
    for (const candidate of payload.candidates) {
      const candidateError = normalizationCandidateError(candidate.requestId, candidate.fieldKey)
      if (candidateError) return `${candidate.label || candidate.fieldKey}：${candidateError}`
    }
  }
  return ''
})
const costAuthorizationError = computed(() => {
  const code = currentRun.value?.blockedCode || ''
  const budget = authorizationBudget.value.trim()
  const estimate = approvedEstimatedCost.value.trim()
  const currency = authorizationCurrency.value.trim().toUpperCase()
  const decimal = /^(?:0|[1-9]\d*)(?:\.\d{1,4})?$/
  if (code === 'external_call_not_authorized' && !authorizationExternal.value) return '当前阻断要求明确允许本阶段外发。'
  if ((code === 'external_budget_missing' || code === 'budget_limit_exceeded' || authorizationExternal.value) && (!budget || !decimal.test(budget))) return '请填写合法的新运行预算上限（最多 4 位小数）。'
  if (code === 'unknown_cost' && (!estimate || !decimal.test(estimate))) return '未知费用阶段必须填写批准的阶段预估费用。'
  if ((code === 'unknown_cost' || code === 'cost_currency_missing' || Boolean(estimate)) && !/^[A-Z][A-Z0-9]{2,11}$/.test(currency)) return '请填写 3–12 位大写费用币种代码。'
  return ''
})

const readScripts = async () => {
  sources.value = await platformApi.listScriptSources(projectId.value)
  const details = await Promise.all(sources.value.filter((item) => item.latestRevisionId).map((item) => platformApi.getScriptRevision(item.latestRevisionId)))
  revisions.value = details.sort((a, b) => b.revision - a.revision)
  if (!selectedRevisionId.value) selectedRevisionId.value = revisions.value.find((item) => item.status === 'confirmed')?.id || revisions.value[0]?.id || ''
}
const loadRecipeMediaRouteOptions = async () => {
  const sequence = ++recipeMediaRouteLoadSequence
  const recipe = selectedRecipe.value
  recipeMediaRoutes.value = []
  recipeMediaRoutesError.value = ''
  if (!recipe || !recipeHasMediaGeneration.value) return
  recipeMediaRoutesLoading.value = true
  try {
    const catalog = await platformApi.getRecipeMediaRouteOptions(recipe.id)
    if (sequence !== recipeMediaRouteLoadSequence || selectedRecipe.value?.id !== recipe.id) return
    if (catalog.recipeVersionId !== recipe.id || catalog.recipeDefinitionSha256 !== recipe.definitionSha256) {
      throw new Error('服务端 route 目录与当前配方版本或定义 hash 不一致')
    }
    recipeMediaRoutes.value = catalog.routes
  } catch (cause) {
    if (sequence === recipeMediaRouteLoadSequence) recipeMediaRoutesError.value = message(cause)
  } finally {
    if (sequence === recipeMediaRouteLoadSequence) recipeMediaRoutesLoading.value = false
  }
}
const load = async () => {
  loading.value = true
  error.value = ''
  notice.value = ''
  currentRun.value = null
  try {
    await studio.initialize()
    await studio.loadProject(projectId.value)
    if (!selectedEpisodeIds.value.length) selectedEpisodeIds.value = projectEpisodes.value.map((item) => item.id)
    const [recipeRows, runRows] = await Promise.all([platformApi.listRecipes('published'), platformApi.listProductionRuns(projectId.value)])
    recipes.value = recipeRows
    runs.value = runRows
    if (!runId.value) {
      const restoredRunId = restorableProductionRunId(runRows)
      if (restoredRunId) {
        await router.replace({ name: 'production-run-detail', params: { projectId: projectId.value, runId: restoredRunId } })
        return
      }
    }
    if (!selectedRecipeId.value) selectedRecipeId.value = recipeRows[0]?.id || ''
    await readScripts()
    if (runId.value) {
      const loadedRun = await platformApi.getProductionRun(runId.value)
      if (!productionRunBelongsToProject(loadedRun, projectId.value)) throw new Error('该 ProductionRun 不属于当前项目，已拒绝显示或操作。')
      currentRun.value = loadedRun
    } else currentRun.value = null
  } catch (cause) {
    error.value = message(cause)
  } finally {
    loading.value = false
  }
}
const refreshRun = async () => {
  if (!runId.value) return
  try {
    const refreshed = await platformApi.getProductionRun(runId.value)
    if (!productionRunBelongsToProject(refreshed, projectId.value)) throw new Error('该 ProductionRun 不属于当前项目，已拒绝显示或操作。')
    currentRun.value = refreshed
  } catch (cause) { currentRun.value = null; error.value = message(cause) }
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
  if (currentRun.value && !terminal.has(currentRun.value.status)) pollTimer = window.setInterval(() => void refreshRun(), 6000)
}
watch(() => currentRun.value?.status, schedulePoll)
watch(() => `${currentStage.value?.id || ''}:${routeOptions.value.map((item) => item.index).join(',')}`, () => {
  const selected = currentStage.value?.selectedRouteIndex
  routeIndex.value = selected !== null && selected !== undefined ? selected : routeOptions.value[0]?.index ?? null
}, { immediate: true })
watch(selectedRecipeId, () => { void loadRecipeMediaRouteOptions() }, { immediate: true })
watch(() => `${selectedRecipeId.value}:${recipeMediaRoutes.value.map((item) => `${item.routeIndex}:${item.isSelectable}`).join(',')}:${workflowShotRows.value.map(({ shot }) => `${shot.id}:${shot.mediaType}:${shot.mode}`).join(',')}`, () => {
  const indexes = new Set(recipeMediaRoutes.value.filter((item) => item.isSelectable).map((item) => item.routeIndex))
  if (uniformWorkflowRouteIndex.value === null || !uniformWorkflowOptions.value.some((item) => item.routeIndex === uniformWorkflowRouteIndex.value)) {
    uniformWorkflowRouteIndex.value = uniformWorkflowOptions.value[0]?.routeIndex ?? null
  }
  shotWorkflowRouteIndexes.value = Object.fromEntries(
    Object.entries(shotWorkflowRouteIndexes.value).filter(([, index]) => index !== null && indexes.has(Number(index))),
  )
  episodeWorkflowRouteIndexes.value = Object.fromEntries(
    Object.entries(episodeWorkflowRouteIndexes.value).filter(([, index]) => index !== null && indexes.has(Number(index))),
  )
}, { immediate: true })
watch(() => workflowShotRows.value.map(({ shot }) => shot.id).join(','), () => {
  const ids = new Set(workflowShotRows.value.map(({ shot }) => shot.id))
  shotWorkflowRouteIndexes.value = Object.fromEntries(
    Object.entries(shotWorkflowRouteIndexes.value).filter(([shotId]) => ids.has(shotId)),
  )
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
  authorizationBudget.value = currentRun.value?.budgetLimit || ''
  authorizationCurrency.value = currentRun.value?.costCurrency || currentStage.value?.approvedCostCurrency || ''
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
watch(() => currentStage.value?.stageKey === 'script_review' && !scriptReview.value.error ? `${currentRun.value?.id}:${scriptReview.value.value?.scriptRevisionId}:${scriptReview.value.value?.contentSha256}` : '', async (signature) => {
  const sequence = ++reviewLoadSequence
  reviewRevision.value = null
  reviewRevisionError.value = ''
  if (!signature || !scriptReview.value.value) return
  reviewRevisionLoading.value = true
  try {
    const revision = await platformApi.getScriptRevision(scriptReview.value.value.scriptRevisionId)
    if (sequence !== reviewLoadSequence) return
    if (revision.id !== currentRun.value?.scriptRevision || revision.id !== scriptReview.value.value.scriptRevisionId) throw new Error('服务端 ScriptRevision 与本次运行审核快照不一致。')
    if (revision.contentSha256 !== scriptReview.value.value.contentSha256) throw new Error('服务端 ScriptRevision 正文 hash 与审核快照不一致。')
    if (!revision.content?.trim()) throw new Error('服务端未返回可审核的完整剧本正文。')
    reviewRevision.value = revision
  } catch (cause) {
    if (sequence === reviewLoadSequence) reviewRevisionError.value = `完整剧本读取失败：${message(cause)}`
  } finally {
    if (sequence === reviewLoadSequence) reviewRevisionLoading.value = false
  }
}, { immediate: true })
watch(() => currentStage.value?.stageKey === 'script_normalize_review' && !normalizationReview.value.error
  ? `${currentRun.value?.id}:${normalizationReview.value.value?.snapshotSha256}:${normalizationReview.value.value?.resultSha256}`
  : '', (signature) => {
  if (signature && signature === normalizationEditSignature.value) return
  normalizationEditSignature.value = signature
  normalizationEdits.value = {}
  normalizationReviewed.value = {}
  if (!signature || !normalizationReview.value.value) return
  normalizationEdits.value = Object.fromEntries(
    normalizationReview.value.value.candidates.map((candidate) => [candidate.requestId, candidate.translatedText]),
  )
  normalizationReviewed.value = Object.fromEntries(
    normalizationReview.value.value.candidates.map((candidate) => [candidate.requestId, false]),
  )
}, { immediate: true })
watch(() => `${currentRun.value?.id || ''}:${currentStage.value?.id || ''}`, () => {
  approvalDecision.value = 'approve'
  approvalComment.value = ''
  adoptGeneratedMetadata.value = false
})
watch(() => route.params.runId, load)
onMounted(load)
onBeforeUnmount(() => { if (pollTimer) window.clearInterval(pollTimer) })

const chooseFile = (event: Event) => { scriptFile.value = (event.target as HTMLInputElement).files?.[0] || null; previewRevision.value = null }
const createScriptPreview = async () => {
  actionBusy.value = 'script'
  error.value = ''
  notice.value = ''
  try {
    const result = scriptMode.value === 'file' && scriptFile.value
      ? await platformApi.uploadScript(projectId.value, scriptFile.value)
      : await platformApi.createPastedScript(projectId.value, scriptText.value, scriptFormat.value)
    previewRevision.value = result.revision
    selectedRevisionId.value = result.revision.id
    revisions.value = [result.revision, ...revisions.value.filter((item) => item.id !== result.revision.id)]
    notice.value = '服务端已生成预览 revision。请核对正文后再确认。'
    await readScripts()
  } catch (cause) { error.value = message(cause) } finally { actionBusy.value = '' }
}
const importProjectMasterScript = async () => {
  if (!projectMasterScript.value) return
  actionBusy.value = 'project-script'
  error.value = ''
  notice.value = ''
  try {
    const result = await platformApi.createPastedScript(projectId.value, projectMasterScript.value, 'plain')
    previewRevision.value = result.revision
    selectedRevisionId.value = result.revision.id
    revisions.value = [result.revision, ...revisions.value.filter((item) => item.id !== result.revision.id)]
    notice.value = '已把当前项目母稿复制为服务端 preview revision；这不是新的 AI 生成结果，请核对并确认。'
    await readScripts()
  } catch (cause) { error.value = message(cause) } finally { actionBusy.value = '' }
}
const selectExistingRevision = async () => {
  if (!selectedRevisionId.value) return
  actionBusy.value = 'revision'
  try { previewRevision.value = await platformApi.getScriptRevision(selectedRevisionId.value) } catch (cause) { error.value = message(cause) } finally { actionBusy.value = '' }
}
const confirmRevision = async () => {
  if (!selectedRevisionId.value) return
  actionBusy.value = 'confirm'
  error.value = ''
  try {
    const confirmed = await platformApi.confirmScriptRevision(selectedRevisionId.value)
    previewRevision.value = confirmed
    revisions.value = revisions.value.map((item) => item.id === confirmed.id ? confirmed : item)
    notice.value = '服务端已确认该剧本 revision，可用于 ProductionRun。'
  } catch (cause) { error.value = message(cause) } finally { actionBusy.value = '' }
}
const createRun = async () => {
  if (!canCreate.value) return
  const mode = selectedScriptMode.value
  if (mode === 'invalid') { error.value = '所选配方没有唯一、合法的剧本输入模式'; return }
  actionBusy.value = 'create'
  error.value = ''
  notice.value = ''
  try {
    let overrides: JsonObject
    try { overrides = JSON.parse(configOverridesText.value) as JsonObject } catch { throw new Error('本次配置覆盖必须是合法 JSON 对象') }
    if (!overrides || Array.isArray(overrides) || typeof overrides !== 'object') throw new Error('本次配置覆盖必须是 JSON 对象')
    if (allowExternalCalls.value === true && !budgetLimit.value.trim()) throw new Error('允许外发模型调用时必须填写本次预算上限')
    const created = await platformApi.createProductionRun({
      projectId: projectId.value,
      scriptMode: mode,
      scriptRevisionId: selectedRevisionId.value,
      recipeVersionId: selectedRecipeId.value,
      idempotencyKey: idempotencyKey.value,
      allowExternalCalls: allowExternalCalls.value,
      budgetLimit: budgetLimit.value.trim() || null,
      maxGenerationTasks: maxGenerationTasks.value,
      configOverrides: overrides,
      idea: aiIdea.value.trim(),
      styleHint: aiStyleHint.value.trim(),
      episodeCount: episodeCountHint.value,
      shotsPerEpisode: shotsPerEpisode.value,
      episodeDurationSeconds: episodeDurationSeconds.value,
      episodeIds: selectedEpisodeIds.value,
      workflowSelection: !recipeHasMediaGeneration.value
        ? null
        : workflowSelectionMode.value === 'uniform'
        ? {
            schemaVersion: 1,
            mode: 'uniform',
            routeIndex: uniformWorkflowRouteIndex.value as number,
          }
        : {
            schemaVersion: 1,
            mode: 'per_shot',
            shotRoutes: workflowShotRows.value.map(({ shot }) => ({
              shotId: shot.id,
              routeIndex: shotWorkflowRouteIndexes.value[shot.id] as number,
            })),
          },
    })
    currentRun.value = created
    try { await studio.loadProject(projectId.value, true) }
    catch (cause) { error.value = `ProductionRun 已创建，但项目事实刷新失败：${message(cause)}` }
    idempotencyKey.value = crypto.randomUUID()
    await router.replace({ name: 'production-run-detail', params: { projectId: projectId.value, runId: created.id } })
    if (!error.value) notice.value = 'ProductionRun 已由服务端创建，当前状态与项目事实已重新读取。'
  } catch (cause) { error.value = message(cause) } finally { actionBusy.value = '' }
}
const runAction = async (name: string, request: () => Promise<ProductionRun>, localError?: { value: string }): Promise<boolean> => {
  actionBusy.value = name
  error.value = ''
  if (localError) localError.value = ''
  notice.value = ''
  try {
    currentRun.value = await request()
    try {
      await studio.loadProject(projectId.value, true)
      notice.value = '服务端 action 已完成，运行与项目事实均已重新读取。'
    } catch (cause) {
      error.value = `服务端 action 已完成，但项目事实刷新失败：${message(cause)}`
    }
    return true
  } catch (cause) {
    if (localError) localError.value = message(cause)
    else error.value = message(cause)
    return false
  } finally { actionBusy.value = '' }
}
const decide = () => {
  const run = currentRun.value
  const stage = currentStage.value
  if (!run || !stage) return
  if (approvalDecision.value === 'approve' && reviewApprovalError.value) { error.value = reviewApprovalError.value; return }
  const candidateOverrides = stage.stageKey === 'script_normalize_review' && approvalDecision.value === 'approve'
    ? Object.fromEntries((normalizationReview.value.value?.candidates || []).map((candidate) => [candidate.requestId, normalizationEdits.value[candidate.requestId] || '']))
    : undefined
  const expectedSourceSnapshotSha256 = stage.stageKey === 'storyboard_review' && approvalDecision.value === 'approve'
    ? storyboardReview.value.value?.snapshotSha256
    : stage.stageKey === 'script_normalize_review' && approvalDecision.value === 'approve'
      ? normalizationReview.value.value?.snapshotSha256
      : undefined
  void runAction('approve', () => platformApi.approveProductionRun(run.id, {
    stageKey: stage.stageKey,
    decision: approvalDecision.value,
    comment: approvalComment.value,
    candidateOverrides,
    expectedSourceSnapshotSha256,
    adoptGeneratedMetadata: (
      stage.stageKey === 'script_review' && approvalDecision.value === 'approve'
        ? adoptGeneratedMetadata.value
        : undefined
    ),
  }))
}
const continueRun = () => { if (currentRun.value) void runAction('continue', () => platformApi.continueProductionRun(currentRun.value!.id)) }
const requestRunConfirmation = (action: 'retry' | 'cancel') => {
  const run = currentRun.value
  if (!run) return
  pendingRunConfirmationError.value = ''
  pendingRunConfirmation.value = {
    action,
    runId: run.id,
    runStatus: run.status,
    blockedCode: run.blockedCode,
    uncertainRemote: isUncertainRemote.value,
    stageId: currentStage.value?.id || '',
    stageKey: currentStage.value?.stageKey || run.currentStageKey || '—',
    estimatedCost: run.estimatedCostTotal,
    committedCost: run.committedCostTotal,
    actualCost: run.actualCostTotal,
    currency: run.costCurrency || '币种未定',
  }
}
const closeRunConfirmation = () => { if (!actionBusy.value) { pendingRunConfirmation.value = null; pendingRunConfirmationError.value = '' } }
const confirmRunAction = async () => {
  const pending = pendingRunConfirmation.value
  const run = currentRun.value
  if (!pending || !run) return
  if (run.id !== pending.runId || run.status !== pending.runStatus || run.blockedCode !== pending.blockedCode || isUncertainRemote.value !== pending.uncertainRemote || (pending.stageId && currentStage.value?.id !== pending.stageId)) {
    pendingRunConfirmationError.value = '运行或当前阶段已变化，请依据最新状态重新确认。'
    return
  }
  if (pending.action === 'retry') {
    if (isUncertainRemote.value) { pendingRunConfirmationError.value = '远端结果状态不确定，不能普通重试；请先由管理员核验。'; return }
    if (await runAction('retry', () => platformApi.retryProductionRun(pending.runId), pendingRunConfirmationError)) closeRunConfirmation()
    return
  }
  if (await runAction('cancel', () => platformApi.cancelProductionRun(pending.runId), pendingRunConfirmationError)) closeRunConfirmation()
}
const toggleStageDisclosure = (stageId: string) => {
  stageDisclosureUserSelected.value = true
  expandedStageId.value = expandedStageId.value === stageId ? '' : stageId
}
const selectRoute = () => {
  const run = currentRun.value
  const stage = currentStage.value
  if (!run || !stage || routeIndex.value === null) return
  void runAction('route', () => platformApi.selectProductionRoute(run.id, { stageKey: stage.stageKey, routeIndex: routeIndex.value! }))
}
const authorizeCost = () => {
  const run = currentRun.value
  const stage = currentStage.value
  if (!run || !stage || costAuthorizationError.value) return
  void runAction('cost', () => platformApi.authorizeProductionCost(run.id, {
    stageKey: stage.stageKey,
    allowExternalCalls: authorizationExternal.value,
    budgetLimit: authorizationBudget.value.trim() || null,
    approvedEstimatedCost: approvedEstimatedCost.value.trim() || null,
    currency: authorizationCurrency.value.trim().toUpperCase(),
    comment: approvalComment.value,
  }))
}
const artifactSelected = async () => { await Promise.all([refreshRun(), studio.loadProject(projectId.value, true)]); notice.value = '服务端已更新同组 selected artifact，项目事实已刷新。' }
const openUncertainResolution = (attempt: GenerationAttempt) => {
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
  uncertainResolutionSaving.value = true
  uncertainResolutionError.value = ''
  try {
    currentRun.value = await platformApi.resolveProductionRunUncertain(run.id, {
      attemptId: attempt.id,
      outcome: uncertainResolutionOutcome.value,
      note: uncertainResolutionNote.value.trim(),
      providerRequestId: uncertainResolutionProviderRequestId.value.trim(),
      actualCost: uncertainResolutionActualCost.value.trim() || null,
      costCurrency: uncertainResolutionCurrency.value.trim().toUpperCase(),
    })
    uncertainResolutionAttempt.value = null
    uncertainResolutionProviderRequestId.value = ''
    notice.value = uncertainResolutionOutcome.value === 'confirmed_remote_submitted'
      ? '已追加人工消歧证据并恢复远端轮询；这不是 smoke/pass 证明，请继续观察服务端终态。'
      : '已追加人工消歧证据；该记录不代表生成成功，页面已读取服务端最新状态。'
    await loadUncertainAttempts()
    try { await studio.loadProject(projectId.value, true) }
    catch (cause) { error.value = `消歧记录已提交，但项目事实刷新失败：${message(cause)}` }
  } catch (cause) {
    uncertainResolutionError.value = message(cause)
  } finally {
    uncertainResolutionSaving.value = false
  }
}
</script>

<template>
  <div class="platform-page production-run-page">
    <header class="platform-page-header"><div><button class="platform-back" @click="router.push(`/projects/${projectId}`)"><ArrowLeft :size="14"/>返回项目</button><span class="eyebrow">PROJECT PRODUCTION WORKSPACE</span><h2>{{ project?.title || '项目' }} · 一键生产</h2><p>脚本确认、配方选择、阶段审批、生成重试与版本选片都走 ProductionRun 服务端状态机。</p></div><button class="button ghost small" :disabled="loading" @click="load"><RefreshCw :size="14" :class="{spin:loading}"/>重新读取</button></header>
    <div v-if="error" class="platform-alert danger"><AlertTriangle :size="17"/><div><strong>操作未完成</strong><p>{{ error }}</p></div></div><div v-if="notice" class="platform-alert success"><Check :size="17"/><p>{{ notice }}</p></div><div v-if="loading" class="platform-loading"><LoaderCircle class="spin" :size="22"/>正在读取项目生产状态…</div>

    <template v-else-if="!currentRun">
      <div v-if="actionableRuns.length>1" class="platform-alert warning"><AlertTriangle :size="17"/><div><strong>存在 {{ actionableRuns.length }} 条可继续运行</strong><p>系统不会擅自选择。请从下方历史列表进入明确的 ProductionRun，或确认后再创建独立新运行。</p></div></div>
      <div class="production-setup-grid">
        <section v-if="isAiScriptRecipe" class="platform-card production-step"><header><span>01</span><div><strong>AI 生成剧本</strong><small>模型只生成 preview；script_review 批准后才确认并进入 Bible。</small></div></header><div v-if="projectEpisodes.length" class="platform-alert danger production-inline-alert"><AlertTriangle :size="15"/><p>项目已有 {{ projectEpisodes.length }} 集正式 Episode，AI 剧本配方已禁用，避免覆盖已有下游结构。请选择 downstream 配方。</p></div><label class="platform-field"><span>故事想法</span><textarea v-model="aiIdea" rows="8" maxlength="6000" placeholder="描述核心冲突、人物与连续剧走向…"></textarea></label><label class="platform-field"><span>风格提示</span><textarea v-model="aiStyleHint" rows="3" maxlength="2000" placeholder="可选：视觉、节奏、受众等"></textarea></label><div class="platform-form-grid"><label class="platform-field"><span>集数</span><input v-model.number="episodeCountHint" type="number" min="1" max="100"/></label><label class="platform-field"><span>每集镜数</span><input v-model.number="shotsPerEpisode" type="number" min="1" max="100"/></label><label class="platform-field"><span>单集时长（秒）</span><input v-model.number="episodeDurationSeconds" type="number" min="1" max="3600"/></label></div></section>
        <template v-else-if="isConfirmedScriptRecipe">
        <section class="platform-card production-step"><header><span>01</span><div><strong>导入或选择剧本</strong><small>项目母稿、上传或粘贴都只生成 preview revision，确认后才可生产。</small></div></header><div v-if="projectEpisodes.length" class="platform-alert warning production-inline-alert"><AlertTriangle :size="15"/><p>项目已有 {{ projectEpisodes.length }} 集正式 Episode；仍可从已确认 revision 创建新运行。服务端会按本次配方显式处理，现有正式对象不会被页面静默覆盖。</p></div><button v-if="projectMasterScript" class="button ghost small" :disabled="Boolean(actionBusy)" @click="importProjectMasterScript"><FileText :size="13"/>{{ actionBusy==='project-script'?'导入中…':'导入当前项目母稿' }}</button><div class="segmented"><button :class="{active:scriptMode==='paste'}" @click="scriptMode='paste'">粘贴</button><button :class="{active:scriptMode==='file'}" @click="scriptMode='file'">上传文件</button><button :class="{active:scriptMode==='existing'}" @click="scriptMode='existing'">已有版本</button></div><template v-if="scriptMode==='paste'"><label class="platform-field"><span>格式</span><select v-model="scriptFormat"><option value="plain">纯文本</option><option value="markdown">Markdown</option><option value="fountain">Fountain</option></select></label><label class="platform-field"><span>剧本文本</span><textarea v-model="scriptText" rows="10" placeholder="粘贴完整剧本…"></textarea></label><button class="button primary small" :disabled="!scriptText.trim()||Boolean(actionBusy)" @click="createScriptPreview"><FileText :size="13"/>{{ actionBusy==='script'?'生成预览中…':'生成服务端预览' }}</button></template><template v-else-if="scriptMode==='file'"><label class="script-drop"><Upload :size="22"/><strong>{{ scriptFile?.name || '选择 txt / md / fountain / docx' }}</strong><small>PDF 当前由服务端明确拒绝，不会静默提取。</small><input type="file" accept=".txt,.md,.markdown,.fountain,.docx,text/plain,text/markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document" @change="chooseFile"/></label><button class="button primary small" :disabled="!scriptFile||Boolean(actionBusy)" @click="createScriptPreview">{{ actionBusy==='script'?'上传解析中…':'上传并生成预览' }}</button></template><template v-else><label class="platform-field"><span>已有 latest revision</span><select v-model="selectedRevisionId" @change="selectExistingRevision"><option value="">请选择</option><option v-for="item in revisions" :key="item.id" :value="item.id">revision {{ item.revision }} · {{ item.status }} · {{ item.contentSha256.slice(0,10) }}</option></select></label></template>
          <div v-if="previewRevision" class="script-preview"><header><div><strong>Revision {{ previewRevision.revision }}</strong><small>{{ previewRevision.contentSizeBytes }} 字节 · {{ previewRevision.contentSha256 }}</small></div><PlatformStatusPill :status="previewRevision.status"/></header><pre>{{ previewRevision.content || '服务端列表响应不含正文；请重新选择以读取详情。' }}</pre><button v-if="previewRevision.status!=='confirmed'" class="button primary small" :disabled="Boolean(actionBusy)" @click="confirmRevision"><Check :size="13"/>{{ actionBusy==='confirm'?'确认中…':'确认该剧本版本' }}</button></div>
        </section>
        </template>
        <section v-else-if="isDownstreamRecipe" class="platform-card production-step"><header><span>01</span><div><strong>沿用现有分集</strong><small>downstream 配方从已有 Episode 开始，不接收剧本 revision 或 AI 剧本输入。</small></div></header><div v-if="projectEpisodes.length" class="platform-alert success production-inline-alert"><Check :size="15"/><p>已发现 {{ projectEpisodes.length }} 集 Episode，可直接选择生产范围。</p></div><div v-else class="platform-alert danger production-inline-alert"><AlertTriangle :size="15"/><p>当前项目没有 Episode，downstream 配方不能创建。请先用 AI 或 confirmed revision 配方建立正式分集。</p></div></section>
        <section v-else class="platform-card production-step"><header><span>01</span><div><strong>配方剧本模式无效</strong><small>配方必须且只能声明 AI、confirmed revision 或 script_input/manual downstream 模式。</small></div></header><div class="platform-alert danger production-inline-alert"><AlertTriangle :size="15"/><p>当前配方为空、包含未知剧本模式或同时声明了冲突模式，已禁止创建。</p></div></section>
        <section class="platform-card production-step"><header><span>02</span><div><strong>选择配方与运行边界</strong><small>所有空值交给服务端 effective 配置解析。</small></div></header><label class="platform-field"><span>Published Recipe</span><select v-model="selectedRecipeId"><option value="">请选择</option><option v-for="item in recipes" :key="item.id" :value="item.id">{{ item.name || item.recipeKey }} · v{{ item.version }}</option></select></label><details v-if="isDownstreamRecipe&&projectEpisodes.length" class="production-overrides"><summary>生产选集 · {{ selectedEpisodeIds.length || '全部' }} / {{ projectEpisodes.length }}<ChevronDown :size="13"/></summary><label v-for="episode in projectEpisodes" :key="episode.id" class="platform-field"><span>第 {{ episode.number }} 集 · {{ episode.title }}</span><input v-model="selectedEpisodeIds" type="checkbox" :value="episode.id"/></label><small>不勾选任何集等同全部；服务端创建时会去重、排序并冻结。</small></details><div v-if="recipeHasMediaGeneration" class="workflow-selection-panel"><div class="workflow-selection-heading"><div><strong>镜头工作流分配</strong><small>只可选择所选 published recipe 的媒体 route；创建时服务端复验输出类型、H3 模式、发布/current、smoke 与部署绑定。</small></div><div class="segmented"><button type="button" :class="{active:workflowSelectionMode==='uniform'}" @click="workflowSelectionMode='uniform'">整集统一</button><button type="button" :class="{active:workflowSelectionMode==='per_shot'}" :disabled="!workflowShotRows.length" @click="workflowSelectionMode='per_shot'">逐镜头</button></div></div><label v-if="workflowSelectionMode==='uniform'" class="platform-field"><span>整集统一工作流 *</span><select v-model.number="uniformWorkflowRouteIndex" :disabled="!recipeMediaRoutes.length"><option :value="null">请选择</option><option v-for="option in recipeMediaRoutes" :key="option.routeIndex" :value="option.routeIndex" :disabled="!routeSupportsUniform(option)">{{ routeLabel(option) }}</option></select><small>AI/导入在 Shot 尚未物化时使用此模式；生成后的每个镜头会固定使用该 route。</small></label><div v-else class="workflow-shot-table"><article v-for="episode in workflowEpisodes" :key="episode.id" class="workflow-episode-group"><header><div><strong>第 {{ episode.number }} 集 · {{ episode.title }}</strong><small>{{ episode.shots.length }} 个镜头</small></div><div class="workflow-episode-apply"><select v-model.number="episodeWorkflowRouteIndexes[episode.id]"><option :value="null">选择本集 route</option><option v-for="option in recipeMediaRoutes" :key="option.routeIndex" :value="option.routeIndex" :disabled="!routeSupportsEpisode(option,episode)">{{ routeLabel(option) }}</option></select><button type="button" class="button ghost tiny" :disabled="!episodeRouteCanApply(episode)" @click="applyEpisodeWorkflow(episode.id)">应用到本集</button></div></header><label v-for="shot in episode.shots" :key="shot.id" class="workflow-shot-row"><span><strong>S{{ shot.order }} · {{ shot.title || '未命名镜头' }}</strong><small>{{ shot.mediaType }} · {{ shot.mode }}</small></span><select v-model.number="shotWorkflowRouteIndexes[shot.id]"><option :value="null">请选择工作流</option><option v-for="option in recipeMediaRoutes" :key="option.routeIndex" :value="option.routeIndex" :disabled="!routeSupportsShot(option,shot)">{{ routeLabel(option) }}</option></select></label></article></div><div v-if="workflowSelectionError" class="platform-alert warning production-inline-alert" role="alert"><AlertTriangle :size="15"/><p>{{ workflowSelectionError }}</p></div><details v-if="recipeMediaRoutes.some((item)=>!item.isSelectable)" class="workflow-route-gaps"><summary>查看 route 依赖缺口</summary><p v-for="item in recipeMediaRoutes.filter((route)=>!route.isSelectable)" :key="item.routeIndex"><strong>Route {{ item.routeIndex }}</strong> · {{ item.reasons.join('；') }}</p></details></div><label class="platform-field"><span>任务上限（留空继承）</span><input v-model.number="maxGenerationTasks" type="number" min="1" placeholder="system effective"/></label><label class="platform-field"><span>外部模型调用</span><select v-model="allowExternalCalls"><option :value="null">继承系统配置</option><option :value="false">禁止</option><option :value="true">允许（必须预算）</option></select></label><label v-if="allowExternalCalls===true" class="platform-field"><span>本次预算上限</span><input v-model="budgetLimit" inputmode="decimal" placeholder="例如 20.0000"/></label><details class="production-overrides"><summary>本次配置覆盖（高级）<ChevronDown :size="13"/></summary><textarea v-model="configOverridesText" rows="8" spellcheck="false"></textarea></details><div class="run-create-summary"><GitBranch :size="17"/><span><strong>{{ isInvalidRecipe ? '配方剧本模式无效，不能创建' : isAiScriptRecipe ? projectEpisodes.length ? '已有 Episode，不能重新生成 AI 剧本' : 'AI 剧本将先生成预览' : isConfirmedScriptRecipe ? selectedRevision?.status==='confirmed' ? projectEpisodes.length ? '已有 Episode；将使用已确认 revision 创建新运行' : '剧本已确认' : '请先确认剧本' : projectEpisodes.length ? '从现有 Episode 继续生产' : '缺少可生产的 Episode' }}</strong><small>幂等键 {{ idempotencyKey }}</small></span></div><button class="button primary production-create" :disabled="!canCreate" @click="createRun"><Play :size="15"/>{{ actionBusy==='create'?'创建并执行中…':'创建 ProductionRun' }}</button>
        </section>
      </div>
      <details v-if="runs.length" class="platform-card previous-runs"><summary><span><GitBranch :size="16"/>历史 ProductionRun</span><b>{{ runs.length }}</b><ChevronDown :size="14"/></summary><div class="platform-dense-list"><RouterLink v-for="item in runs" :key="item.id" :to="`/projects/${projectId}/production/${item.id}`"><div><strong>{{ item.currentStageKey || '已完成全部阶段' }}</strong><small>{{ dateTime(item.createdAt) }} · {{ item.generationTaskCount }}/{{ item.maxGenerationTasks }} tasks</small></div><PlatformStatusPill :status="item.status"/></RouterLink></div></details>
    </template>

    <template v-else>
      <section class="run-command-bar"><div><PlatformStatusPill :status="currentRun.status"/><span><strong>{{ currentRun.currentStageKey || '全部阶段完成' }}</strong><small>{{ currentRun.id }} · {{ currentRun.generationTaskCount }}/{{ currentRun.maxGenerationTasks }} generation tasks</small></span></div><div><button class="button ghost small" @click="router.push(`/projects/${projectId}/production`)"><GitBranch :size="13"/>新建 / 历史</button><button v-if="!terminal.has(currentRun.status)&&!isUncertainRemote" class="button primary small" :disabled="Boolean(actionBusy)" @click="continueRun"><Play :size="13"/>{{ actionBusy==='continue'?'继续中…':'继续 / 检查阶段' }}</button><button v-if="['blocked','failed'].includes(currentRun.status)&&!isUncertainRemote" class="button ghost small" :disabled="Boolean(actionBusy)" @click="requestRunConfirmation('retry')"><RotateCcw :size="13"/>重试当前阶段</button><button v-if="!terminal.has(currentRun.status)" class="button danger small" :disabled="Boolean(actionBusy)" @click="requestRunConfirmation('cancel')"><CircleStop :size="13"/>取消运行</button></div></section>
      <div v-if="currentRun.blockedReason||currentRun.blockedCode" class="platform-alert" :class="currentRun.status==='awaiting_approval'?'warning':'danger'"><AlertTriangle :size="17"/><div><strong>{{ blockedPresentation.code }}</strong><p>{{ blockedPresentation.summary }}</p></div></div>
      <section v-if="needsAttemptResolution" class="platform-card review-evidence"><header><div><strong>远端提交 / 费用待人工消歧</strong><small>仅显示后端安全投影；人工消歧会追加审计证据，不会伪装为生成通过。</small></div></header><div v-if="uncertainAttemptsLoading" class="platform-loading compact"><LoaderCircle class="spin" :size="18"/>正在读取安全 attempt 投影…</div><div v-else-if="uncertainAttemptsError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ uncertainAttemptsError }}</p></div><div v-else class="platform-dense-list"><article v-for="attempt in uncertainAttempts" :key="attempt.id"><div><strong>{{ attempt.operation }} · attempt {{ attempt.attemptNumber }}</strong><small>{{ attempt.errorCode }} · request hash {{ attempt.requestSha256.slice(0,12) }} · {{ dateTime(attempt.finishedAt || attempt.updatedAt) }}</small></div><PlatformStatusPill :status="attempt.status"/><button v-if="auth.user?.isStaff" class="button primary tiny" @click="openUncertainResolution(attempt)">管理员消歧</button><small v-else>仅管理员可提交消歧结论</small></article></div></section>
      <section v-if="currentRun.status==='awaiting_approval'&&currentStage?.stageKey==='script_review'" class="platform-card review-evidence">
        <header><div><strong>剧本审核证据</strong><small>只读取前序 script_generate 或 script_structure 的不可变候选，并与 ScriptRevision 详情交叉核验。</small></div></header>
        <div v-if="scriptReview.error" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ scriptReview.error }}</p></div>
        <div v-else-if="reviewRevisionLoading" class="platform-loading compact"><LoaderCircle class="spin" :size="18"/>正在读取完整剧本正文…</div>
        <div v-else-if="reviewRevisionError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ reviewRevisionError }}</p></div>
        <div v-else-if="scriptReview.value&&reviewRevision" class="review-evidence-body">
          <div class="review-headline"><div><span>标题</span><strong>{{ scriptReview.value.title || '未命名' }}</strong></div><div><span>Logline</span><p>{{ scriptReview.value.logline || '未提供' }}</p></div></div>
          <dl class="review-hashes"><div><dt>来源阶段</dt><dd>{{ scriptReview.value.sourceStageKey }}</dd></div><div><dt>提交语义</dt><dd>{{ scriptReview.value.submissionSemantics || '模型生成' }}</dd></div><div><dt>Revision</dt><dd>{{ scriptReview.value.scriptRevisionId }}</dd></div><div><dt>正文 SHA256</dt><dd>{{ scriptReview.value.contentSha256 }}</dd></div><div><dt>模型结果 SHA256</dt><dd>{{ scriptReview.value.resultSha256 || '无模型提交' }}</dd></div><div><dt>阶段快照 SHA256</dt><dd>{{ scriptReview.value.snapshotSha256 }}</dd></div></dl>
          <div v-if="scriptReview.value.warnings.length" class="platform-alert warning"><AlertTriangle :size="14"/><div><strong>生成警告</strong><ul><li v-for="warning in scriptReview.value.warnings" :key="warning">{{ warning }}</li></ul></div></div>
          <div class="review-script"><span>完整正文 · {{ reviewRevision.contentSizeBytes }} 字节</span><pre>{{ reviewRevision.content }}</pre></div>
        </div>
      </section>
      <section v-if="currentRun.status==='awaiting_approval'&&currentStage?.stageKey==='bible_review'" class="platform-card review-evidence"><header><div><strong>Bible 候选审核证据</strong><small>候选来自前序 bible_materialize 冻结快照；本页不从现有 Bible 表反推。</small></div></header><div v-if="bibleReview.error" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ bibleReview.error }}</p></div><div v-else-if="bibleReview.value" class="review-evidence-body"><dl class="review-hashes"><div><dt>候选 SHA256</dt><dd>{{ bibleReview.value.candidateSha256 }}</dd></div><div><dt>来源 SHA256</dt><dd>{{ bibleReview.value.sourceFingerprint }}</dd></div><div><dt>剧本正文 SHA256</dt><dd>{{ bibleReview.value.scriptContentSha256 }}</dd></div><div><dt>阶段快照 SHA256</dt><dd>{{ bibleReview.value.snapshotSha256 }}</dd></div></dl><div class="bible-review-grid"><section v-for="(items,key) in bibleReview.value.collections" :key="key"><header><strong>{{ key }}</strong><span>{{ items.length }}</span></header><article v-for="item in items" :key="item.candidateId"><div><strong>{{ item.name }}</strong><small>{{ item.entityType }} · 出现 {{ item.occurrence }} 次 · {{ item.recommended ? '建议纳入' : '未建议' }}</small></div><p>{{ item.description || '无描述' }}</p><em>{{ item.visualIdentityEn || '无视觉身份描述' }}</em><code>{{ item.reviewedContentSha256 }}</code></article></section></div><details class="bible-diagnostics"><summary>解析诊断 · {{ bibleReview.value.diagnostics.length }}</summary><pre>{{ JSON.stringify(bibleReview.value.diagnostics, null, 2) }}</pre></details></div></section>
      <section v-if="currentRun.status==='awaiting_approval'&&currentStage?.stageKey==='storyboard_review'" class="platform-card review-evidence"><header><div><strong>分镜冻结证据</strong><small>只读取前序 storyboard 的服务端 canonical 快照与 hash；本页不重新计算或伪造 hash。</small></div></header><div v-if="storyboardReview.error" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ storyboardReview.error }}</p></div><div v-else-if="storyboardReview.value" class="review-evidence-body"><dl class="review-hashes"><div><dt>Canonical SHA256</dt><dd>{{ storyboardReview.value.canonicalSha256 }}</dd></div><div><dt>阶段快照 SHA256</dt><dd>{{ storyboardReview.value.snapshotSha256 }}</dd></div><div><dt>规模</dt><dd>{{ storyboardReview.value.episodeCount }} 集 · {{ storyboardReview.value.shotCount }} 镜</dd></div><div><dt>媒体 / 音频</dt><dd>{{ storyboardReview.value.videoCount }} 视频 · {{ storyboardReview.value.imageCount }} 图片 · {{ storyboardReview.value.audioShotCount }} 音频镜头</dd></div><div><dt>叙事总时长</dt><dd>{{ (storyboardReview.value.durationMs/1000).toFixed(1) }} 秒</dd></div></dl><div class="platform-dense-list"><article v-for="episode in storyboardReview.value.episodes" :key="episode.id"><div><strong>第 {{ episode.number }} 集 · {{ episode.title || '未命名' }}</strong><small>{{ episode.shotCount }} 镜 · {{ episode.videoCount }} 视频 · {{ episode.imageCount }} 图片 · {{ episode.audioShotCount }} 音频 · {{ (episode.durationMs/1000).toFixed(1) }} 秒</small></div></article></div></div></section>
      <section v-if="currentRun.status==='awaiting_approval'&&currentStage?.stageKey==='script_normalize_review'" class="platform-card review-evidence"><header><div><strong>英文执行字段人工审核</strong><small>来源与 hash 来自前序 script_normalize 不可变快照；只有逐项确认后的整批候选才会写入正式 PromptFieldTranslation。</small></div></header><div v-if="normalizationReview.error" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ normalizationReview.error }}</p></div><div v-else-if="normalizationReview.value" class="review-evidence-body"><dl class="review-hashes"><div><dt>Attempt</dt><dd>{{ normalizationReview.value.attemptId || '无需提交' }}</dd></div><div><dt>模型结果 SHA256</dt><dd>{{ normalizationReview.value.resultSha256 }}</dd></div><div><dt>阶段快照 SHA256</dt><dd>{{ normalizationReview.value.snapshotSha256 }}</dd></div><div><dt>Provider / Model</dt><dd>{{ normalizationReview.value.provider || '—' }} / {{ normalizationReview.value.model || '—' }}</dd></div><div><dt>人工核对进度</dt><dd>{{ normalizationReviewedCount }} / {{ normalizationReview.value.candidates.length }}</dd></div></dl><div v-if="!normalizationReview.value.candidates.length" class="platform-alert"><Check :size="15"/><p>当前正式对象没有需要规范化的导演字段；本阶段未调用外部模型，可直接批准。</p></div><div v-else class="platform-dense-list"><article v-for="candidate in normalizationReview.value.candidates" :key="candidate.requestId"><div><strong>{{ candidate.label || candidate.fieldKey }}</strong><small>{{ candidate.scopeType }} · {{ candidate.fieldKey }} · request {{ candidate.requestId }}</small><label class="platform-field"><span>原始字段 {{ candidate.label || candidate.fieldKey }}</span><textarea :value="candidate.sourceText" rows="2" readonly :aria-label="`原始字段 ${candidate.label || candidate.fieldKey}`"></textarea></label><label class="platform-field"><span>英文候选 {{ candidate.label || candidate.fieldKey }}</span><textarea v-model="normalizationEdits[candidate.requestId]" rows="3" maxlength="8000" :aria-invalid="Boolean(normalizationCandidateError(candidate.requestId,candidate.fieldKey))" :aria-label="`英文候选 ${candidate.label || candidate.fieldKey}`"></textarea><small v-if="normalizationCandidateError(candidate.requestId,candidate.fieldKey)" class="gate-error">{{ normalizationCandidateError(candidate.requestId,candidate.fieldKey) }}</small></label><div><label><input v-model="normalizationReviewed[candidate.requestId]" type="checkbox"/>已核对该项来源与英文候选</label><button class="button ghost tiny" type="button" @click="restoreNormalizationCandidate(candidate.requestId)">恢复模型候选</button></div></div></article></div></div></section>
      <section v-if="currentRun.status==='awaiting_approval'&&currentStage&&routeGate" class="approval-gate route-approval-gate"><div><strong>选择显式路由 · {{ currentStage.stageKey }}</strong><p>候选来自本次运行冻结配方；选择会记录实际 Provider、模型部署与工作流版本。</p></div><div v-if="routeOptions.length" class="route-option-list"><label v-for="option in routeOptions" :key="option.index" :class="{selected:routeIndex===option.index}"><input v-model="routeIndex" type="radio" name="production-route" :value="option.index"/><span><strong>Route {{ option.index }} · {{ option.driverType }}</strong><small>Provider {{ option.providerId || '—' }}</small><small>模型版本 {{ option.modelProfileVersionId || '—' }}</small><small>模型部署 {{ option.modelDeploymentId || '—' }}</small><small>工作流 {{ option.workflowVersionId || '未绑定' }}</small><small>输出 {{ option.workflowOutputKind || '未声明' }}<template v-if="option.workflowH3Modes"> · H3 {{ option.workflowH3Modes }}</template></small><em>{{ option.isExternal ? '外部调用' : '本地 / 内网调用' }}</em></span></label></div><div v-else class="platform-alert danger"><AlertTriangle :size="15"/><p>服务端没有返回冻结 route 候选，无法提交人工选择。</p></div><button class="button primary small" :disabled="Boolean(actionBusy)||routeIndex===null||!routeOptions.length" @click="selectRoute">{{ actionBusy==='route'?'提交中…':'确认所选路由' }}</button></section>
      <section v-else-if="currentRun.status==='awaiting_approval'&&currentStage&&costGate" class="approval-gate"><div><strong>费用与外发授权 · {{ currentStage.stageKey }}</strong><p>阻断代码 {{ currentRun.blockedCode }}；必填项由该阻断原因决定，并从运行预算与币种预填。</p><p v-if="costAuthorizationError" class="gate-error">{{ costAuthorizationError }}</p></div><select v-model="authorizationExternal"><option :value="true">允许本阶段外发</option><option :value="false">禁止外发</option></select><div><input v-model="authorizationBudget" placeholder="新的运行预算上限"/><input v-model="approvedEstimatedCost" placeholder="批准的阶段预估费用"/><input v-model="authorizationCurrency" maxlength="12" placeholder="USD"/></div><button class="button primary small" :disabled="Boolean(actionBusy)||Boolean(costAuthorizationError)" @click="authorizeCost">{{ actionBusy==='cost'?'提交中…':'提交授权' }}</button></section>
      <section v-else-if="currentRun.status==='awaiting_approval'&&currentStage" class="approval-gate">
        <div><strong>阶段审批 · {{ currentStage.stageKey }}</strong><p>审批结果会持久化；正式对象不完整时服务端仍会拒绝继续。</p><p v-if="reviewApprovalError" class="gate-error">{{ reviewApprovalError }}</p></div>
        <select v-model="approvalDecision"><option value="approve">批准并继续</option><option value="reject">拒绝并终止</option></select>
        <label v-if="currentStage.stageKey==='script_review'&&approvalDecision==='approve'" class="platform-checkbox"><input v-model="adoptGeneratedMetadata" type="checkbox"/><span>采用冻结候选的项目标题与梗概（运行创建后的人工修改会被保留，不会覆盖）</span></label>
        <input v-model="approvalComment" placeholder="审批说明（拒绝时请说明原因）"/>
        <button class="button primary small" :disabled="Boolean(actionBusy)||(approvalDecision==='approve'&&Boolean(reviewApprovalError))" @click="decide">{{ actionBusy==='approve'?'提交中…':'提交审批' }}</button>
      </section>
      <section class="run-stage-strip" aria-label="生产阶段">
        <article v-for="stage in currentRun.stages" :key="stage.id" :class="{current:stage.stageKey===currentRun.currentStageKey,expanded:expandedStageId===stage.id}">
          <button class="run-stage-toggle" type="button" :aria-expanded="expandedStageId===stage.id" :aria-controls="`stage-details-${stage.id}`" @click="toggleStageDisclosure(stage.id)">
            <header><span>{{ String(stage.ordinal).padStart(2,'0') }}</span><PlatformStatusPill :status="stage.status"/></header><strong>{{ stage.stageKey }}</strong><small>{{ stage.mode }} · attempt {{ stage.attemptCount }}</small><ChevronDown :size="14"/>
          </button>
          <div v-if="expandedStageId===stage.id" :id="`stage-details-${stage.id}`" class="run-stage-details">
            <p v-if="stage.reason||stage.reasonCode"><code>{{ stageIssuePresentation(stage).code }}</code> · {{ stageIssuePresentation(stage).summary }}</p><p v-else>本阶段没有服务端原因说明。</p>
            <dl><div><dt>输入快照</dt><dd><code>{{ stage.inputSnapshotSha256 || '—' }}</code></dd></div><div><dt>输出快照</dt><dd><code>{{ stage.outputSnapshotSha256 || '—' }}</code></dd></div><div><dt>已批费用</dt><dd>{{ stage.approvedEstimatedCost || '—' }} {{ stage.approvedCostCurrency }}</dd></div></dl>
          </div>
        </article>
      </section>
      <section class="run-facts"><div><span>剧本 revision</span><code>{{ currentRun.scriptRevision }}</code></div><div><span>Recipe</span><code>{{ currentRun.recipeVersion }}</code></div><div><span>配置快照</span><code>{{ currentRun.configSnapshotSha256 }}</code></div><div><span>费用（预估 / 承诺 / 实际）</span><strong>{{ currentRun.estimatedCostTotal }} / {{ currentRun.committedCostTotal }} / {{ currentRun.actualCostTotal }} {{ currentRun.costCurrency }}</strong></div><div><span>ReleaseBundle</span><code>{{ currentRun.releaseBundleId || '尚未封存' }}</code></div><div><span>结束</span><strong>{{ dateTime(currentRun.finishedAt) }}</strong></div></section>
      <section class="artifact-groups"><header><div><strong>媒体产物与选片</strong><span>仅展示 image、video 与 assembly；同组 2–4 个版本可并排比较。</span></div></header><GeneratedArtifactCompare v-for="group in artifactGroups" :key="group.key" :run-id="currentRun.id" :artifacts="group.items" @selected="artifactSelected"/><p v-if="!artifactGroups.length" class="platform-empty">当前阶段尚未归档可比较的图片、视频或分集母版。</p></section>
      <BaseModal :open="Boolean(pendingRunConfirmation)" :dismissible="!Boolean(actionBusy)" :title="pendingRunConfirmation?.action==='retry'?'确认重试当前阶段':'确认取消运行'" eyebrow="PRODUCTION RUN CONFIRMATION" width="660px" @close="closeRunConfirmation">
        <dl v-if="pendingRunConfirmation" class="run-confirmation-details"><div><dt>Run</dt><dd><code>{{ pendingRunConfirmation.runId }}</code></dd></div><div><dt>当前阶段</dt><dd>{{ pendingRunConfirmation.stageKey }}</dd></div><div><dt>影响</dt><dd>{{ pendingRunConfirmation.action==='retry'?'服务端将为失败单元创建新的可审计 attempt；已成功产物仍按服务端状态保留。':'服务端将请求停止未完成阶段；已外发请求可能仍需等待远端终态，晚到结果不会自动采用。' }}</dd></div><div><dt>费用提示</dt><dd>预估 {{ pendingRunConfirmation.estimatedCost }} / 已承诺 {{ pendingRunConfirmation.committedCost }} / 已发生 {{ pendingRunConfirmation.actualCost }} {{ pendingRunConfirmation.currency }}。{{ pendingRunConfirmation.action==='retry'?'重试可能增加费用。':'取消不代表已发生费用会撤销。' }}</dd></div></dl>
        <div v-if="pendingRunConfirmationError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ pendingRunConfirmationError }}</p></div>
        <template #footer><button class="button ghost" :disabled="Boolean(actionBusy)" @click="closeRunConfirmation">返回</button><button v-if="pendingRunConfirmation" class="button" :class="pendingRunConfirmation.action==='cancel'?'danger':'primary'" :disabled="Boolean(actionBusy)" @click="confirmRunAction">{{ actionBusy ? '服务端处理中…' : pendingRunConfirmation.action==='retry'?'确认重试':'确认取消' }}</button></template>
      </BaseModal>
      <BaseModal :open="Boolean(uncertainResolutionAttempt)" :dismissible="!uncertainResolutionSaving" title="管理员消歧远端 attempt" eyebrow="APPEND-ONLY RESOLUTION" width="700px" @close="closeUncertainResolution">
        <div v-if="uncertainResolutionAttempt" class="registry-action-review">
          <p>请在 Provider 控制台或账单中核对后再提交。该操作只追加不可变消歧记录；不会把人工结论展示为 smoke/pass。</p>
          <dl><div><dt>Operation</dt><dd>{{ uncertainResolutionAttempt.operation }}</dd></div><div><dt>错误代码</dt><dd><code>{{ uncertainResolutionAttempt.errorCode }}</code></dd></div><div><dt>请求快照 SHA256</dt><dd><code>{{ uncertainResolutionAttempt.requestSnapshotSha256 }}</code></dd></div><div><dt>Provider request ID 证据</dt><dd><code>{{ uncertainResolutionAttempt.providerRequestIdSha256 || '尚无 hash' }}</code></dd></div></dl>
          <label class="platform-field"><span>核验结论</span><select v-model="uncertainResolutionOutcome"><option v-for="option in uncertainResolutionOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
          <label class="platform-field"><span>核验说明（必填）</span><textarea v-model="uncertainResolutionNote" rows="4" maxlength="4000" placeholder="说明查阅了哪个控制台、账单时间范围和可复核结论"></textarea></label>
          <label v-if="uncertainResolutionOutcome==='confirmed_remote_submitted'" class="platform-field"><span>Provider request ID（仅提交，页面不回显）</span><input v-model="uncertainResolutionProviderRequestId" type="password" maxlength="191" autocomplete="off"/></label>
          <div v-if="uncertainResolutionOutcome==='confirmed_remote_failed'||uncertainResolutionOutcome==='actual_cost_confirmed'" class="platform-form-grid"><label class="platform-field"><span>实际费用（可填 0）</span><input v-model="uncertainResolutionActualCost" inputmode="decimal" placeholder="0"/></label><label class="platform-field"><span>币种（费用大于 0 必填）</span><input v-model="uncertainResolutionCurrency" maxlength="12" placeholder="USD"/></label></div>
          <div v-if="uncertainResolutionValidation" class="platform-alert warning"><AlertTriangle :size="15"/><p>{{ uncertainResolutionValidation }}</p></div>
          <div v-if="uncertainResolutionError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ uncertainResolutionError }}</p></div>
        </div>
        <template #footer><button class="button ghost" :disabled="uncertainResolutionSaving" @click="closeUncertainResolution">取消</button><button class="button primary" :disabled="uncertainResolutionSaving||Boolean(uncertainResolutionValidation)" @click="submitUncertainResolution">{{ uncertainResolutionSaving?'提交中…':'追加消歧记录' }}</button></template>
      </BaseModal>
    </template>
  </div>
</template>
