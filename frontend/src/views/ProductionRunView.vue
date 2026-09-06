<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AlertTriangle, ArrowLeft, BookOpen, Check, ChevronDown, CircleStop, FileText, GitBranch, Image, LoaderCircle, Play, Plus, RefreshCw, RotateCcw, Trash2, Upload } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import GeneratedArtifactCompare from '@/components/GeneratedArtifactCompare.vue'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { presentPlatformStatus, productionRecipeInteractionMode, productionRecipeScriptMode } from '@/services/platformAdapters'
import { platformApi } from '@/services/platformApi'
import { plannedAverageShotDurationMs } from '@/services/productionRunPayload'
import { mediaKindLabel, modeLabel, operationLabel, reasonCodeLabel, stageLabel } from '@/services/presentationLabels'
import { parseBibleImageReviewPayload, parseBibleReviewPayload, parsePromptNormalizationRetryRepairs, parsePromptNormalizationReviewPayload, parseScriptReviewPayload, parseStoryboardReviewPayload, parseVisualRulesReviewPayload } from '@/services/productionRunReview'
import { actionableProductionRuns, productionIssuePresentation, productionRunBelongsToProject, productionRunRestoreTarget, stageIssuePresentation } from '@/services/productionRunPresentation'
import { reconcileExpandedStageId } from '@/services/productionStageDisclosure'
import { repository, type ProductionCapability } from '@/services/repository'
import { isProductionReferenceAsset, REFERENCE_VIDEO_CONTENT_SAFETY_NOTICE, referenceVideoOptionLabel, referenceVideoUsableForOwner } from '@/services/referenceVideoAssets'
import { conditioningModeByKey } from '@/services/conditioningCapabilities'
import {
  createUnattendedReferenceInputDraft,
  createUnattendedReferenceInputDrafts,
  referencePairingRuleForPurpose,
  selectedUnattendedReferenceInputs,
  unattendedReferenceInputDraftError,
} from '@/services/unattendedReferenceInputs'
import type { UnattendedReferenceInputDraft } from '@/services/unattendedReferenceInputs'
import {
  buildStoryboardShotReferenceBindings,
  emptyStoryboardReferenceDraft,
  hydrateStoryboardReferenceDraft,
  storyboardDraftAssetIds as referenceDraftAssetIds,
  validateStoryboardFrameDraft,
  validateStoryboardMediaDraft,
} from '@/services/storyboardReferenceBindings'
import type { StoryboardAudioPurpose, StoryboardShotReferenceDraft, StoryboardVideoPurpose } from '@/services/storyboardReferenceBindings'
import type { GeneratedArtifact, GenerationAttempt, JsonObject, ProductionPromptProfileRequirement, ProductionRecipe, ProductionRecipeMediaRouteOption, ProductionRecipeScriptClassification, ProductionRun, ScriptRevision, ScriptSource, StoryboardShotReferenceSelectionInput, UncertainResolutionOutcome } from '@/services/platformTypes'
import { useAuthStore } from '@/stores/auth'
import { useStudioStore } from '@/stores/studio'
import type { Asset, BibleImageLatestCandidate, Shot as ProjectShot } from '@/types'

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
const recipeBiblePromptProfileRequirement = ref<ProductionPromptProfileRequirement | null>(null)
const recipeMediaRoutesLoading = ref(false)
const recipeMediaRoutesError = ref('')
const runs = ref<ProductionRun[]>([])
const currentRun = ref<ProductionRun | null>(null)
const productionCapability = ref<ProductionCapability | null>(null)
const productionCapabilityError = ref('')
const scriptMode = ref<'paste' | 'file' | 'library' | 'existing'>('paste')
const scriptText = ref('')
const scriptFormat = ref('markdown')
const scriptFile = ref<File | null>(null)
const scriptAssets = ref<Asset[]>([])
const selectedScriptAssetId = ref('')
const previewRevision = ref<ScriptRevision | null>(null)
const selectedRevisionId = ref('')
const selectedRecipeId = ref('')
const episodeCountHint = ref<number | null>(null)
const allowExternalCalls = ref<boolean | null>(null)
const budgetLimit = ref('')
const maxGenerationTasks = ref<number | null>(null)
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
const workflowSelectionMode = ref<'uniform' | 'per_shot'>('uniform')
const uniformWorkflowRouteIndex = ref<number | null>(null)
const shotWorkflowRouteIndexes = ref<Record<string, number | null>>({})
const episodeWorkflowRouteIndexes = ref<Record<string, number | null>>({})
const uniformPromptProfileBindingIds = ref<Record<string, string>>({})
const shotPromptProfileBindingIds = ref<Record<string, string>>({})
const bibleImagePromptProfileBindingId = ref('')
const unattendedConfirmed = ref(false)
const unattendedReferenceInputs = ref<UnattendedReferenceInputDraft[]>([])
const managedReferenceVideoAssets = ref<Asset[]>([])
const managedReferenceVideoAssetsError = ref('')
const approvalDecision = ref('approve')
const approvalComment = ref('')
const adoptGeneratedMetadata = ref(false)
const normalizationEdits = ref<Record<string, string>>({})
const normalizationReviewed = ref<Record<string, boolean>>({})
const normalizationEditSignature = ref('')
const visualRulesEdits = ref({ negativePrompt: '', colorScript: '', continuitySummary: '' })
const visualRulesReviewed = ref({ negativePrompt: false, colorScript: false, continuitySummary: false })
const visualRulesEditSignature = ref('')
const bibleImageSelectionSignature = ref('')
interface BibleImageReviewSelection {
  selectedArtifactIds: string[]
  selectedAssetIds: string[]
  anchorArtifactId: string
  anchorAssetId: string
}
const bibleImageSelections = ref<Record<string, BibleImageReviewSelection>>({})
const bibleImageHistoryByArtifactId = ref<Record<string, BibleImageLatestCandidate>>({})
const bibleImageHistoryLoading = ref(false)
const bibleImageHistoryError = ref('')
const bibleLibraryExpanded = ref<Record<string, boolean>>({})
const bibleLibrarySearches = ref<Record<string, string>>({})
const bibleLibraryLimits = ref<Record<string, number>>({})
const storyboardReferenceSignature = ref('')
const storyboardReferenceDrafts = ref<Record<string, StoryboardShotReferenceDraft>>({})
const storyboardAssetsLoading = ref(false)
const storyboardAssetsError = ref('')
const storyboardUploadingReference = ref('')
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
const normalizationAttempts = ref<GenerationAttempt[]>([])
const normalizationAttemptsLoading = ref(false)
const normalizationAttemptsError = ref('')
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
let normalizationAttemptLoadSequence = 0
let recipeMediaRouteLoadSequence = 0
let storyboardAssetLoadSequence = 0
let bibleHistoryLoadSequence = 0
let productionLoadSequence = 0
let scriptReadSequence = 0
let scriptActionSequence = 0
let revisionLoadSequence = 0
let runRefreshSequence = 0
let activeProjectScope = ''
let pollTimer: number | undefined

const terminal = new Set(['succeeded', 'failed', 'cancelled'])
const costGateCodes = new Set(['unknown_cost', 'budget_limit_exceeded', 'external_budget_missing', 'external_call_not_authorized', 'cost_currency_missing'])
const message = (cause: unknown) => apiErrorSummary(cause, '服务端请求失败')
const dateTime = (value: string) => value ? new Date(value).toLocaleString('zh-CN') : '—'
const selectedRevision = computed(() => revisions.value.find((item) => item.id === selectedRevisionId.value) || (previewRevision.value?.id === selectedRevisionId.value ? previewRevision.value : null))
const projectMasterScript = computed(() => project.value?.masterScript.trim() || '')
const projectEpisodes = computed(() => project.value?.episodes || [])
const currentStage = computed(() => currentRun.value?.stages?.find((item) => item.stageKey === currentRun.value?.currentStageKey))
const manualBibleReview = computed(() => (
  currentRun.value?.status === 'awaiting_approval'
  && currentStage.value?.stageKey === 'bible_review'
  && currentStage.value?.mode === 'manual'
))
const manualBibleCounts = computed(() => ({
  characters: project.value?.bible.characters.length || 0,
  scenes: project.value?.bible.scenes.length || 0,
  props: project.value?.bible.props.length || 0,
}))
const missingManualBibleLabels = computed(() => [
  manualBibleCounts.value.characters ? '' : '人物',
  manualBibleCounts.value.scenes ? '' : '场景',
  manualBibleCounts.value.props ? '' : '道具',
].filter(Boolean))
const selectedRecipe = computed(() => recipes.value.find((item) => item.id === selectedRecipeId.value))
const selectedScriptMode = computed<ProductionRecipeScriptClassification>(() => productionRecipeScriptMode(selectedRecipe.value?.definition || {}))
const isUnattendedRecipe = computed(() => (
  productionRecipeInteractionMode(selectedRecipe.value?.definition || {}) === 'unattended'
))
const unattendedConditioningMode = computed(() => {
  const routeOption = recipeMediaRoutes.value.find((item) => item.routeIndex === uniformWorkflowRouteIndex.value)
  return conditioningModeByKey(routeOption?.conditioningModes || [], project.value?.defaultMode)
})
const unattendedReferenceVideoInput = computed(() => unattendedConditioningMode.value?.inputs.referenceVideos || ({
  minimumItems: 0,
  maximumItems: 0,
}))
const unattendedReferenceVideoSelectable = computed(() => unattendedReferenceVideoInput.value.maximumItems > 0)
const unattendedReferenceVideoRequired = computed(() => unattendedReferenceVideoInput.value.minimumItems > 0)
const unattendedReferenceTopology = computed(() => unattendedConditioningMode.value?.dynamicReferenceTopology || null)
const unattendedReferenceVideoPurposes = computed(() => unattendedReferenceTopology.value?.videos.purposes || [])
const unattendedReferenceVideoPreprocessing = computed(() => unattendedReferenceTopology.value?.preprocessing.videos || null)
const unattendedReferenceVideoDurationLabel = computed(() => {
  const preprocessing = unattendedReferenceVideoPreprocessing.value
  return preprocessing
    ? `${preprocessing.minimumDurationMs / 1000}–${preprocessing.maximumDurationMs / 1000} 秒`
    : '未声明'
})
const unattendedReferenceVideos = computed(() => (
  managedReferenceVideoAssets.value.filter((asset) => (
    referenceVideoUsableForOwner(
      asset,
      auth.user?.id || '',
      unattendedReferenceVideoPreprocessing.value,
    )
  ))
))
const unattendedReferenceVideoLabel = referenceVideoOptionLabel
const selectedUnattendedReferences = computed(() => (
  selectedUnattendedReferenceInputs(unattendedReferenceInputs.value)
))
const unattendedReferenceAssetIds = computed(() => new Set(
  selectedUnattendedReferences.value.map((item) => item.assetId),
))
const unattendedAvailableReferenceAssetIds = computed(() => new Set(
  unattendedReferenceVideos.value.map((asset) => asset.id),
))
const unattendedReferencePurposeDefinition = (purpose: string) => (
  unattendedReferenceVideoPurposes.value.find((item) => item.key === purpose)
)
const unattendedReferencePairing = (purpose: string) => (
  referencePairingRuleForPurpose(unattendedReferenceTopology.value, purpose)
)
const unattendedReferenceAssetDisabled = (assetId: string, rowKey: string) => (
  unattendedReferenceInputs.value.some((item) => (
    item.rowKey !== rowKey && item.assetId === assetId
  ))
)
const addUnattendedReferenceInput = () => {
  if (unattendedReferenceInputs.value.length >= unattendedReferenceVideoInput.value.maximumItems) return
  unattendedReferenceInputs.value.push(createUnattendedReferenceInputDraft(
    unattendedReferenceTopology.value?.videos.defaultPurposeKey || '',
  ))
}
const removeUnattendedReferenceInput = (rowKey: string) => {
  unattendedReferenceInputs.value = unattendedReferenceInputs.value.filter((item) => item.rowKey !== rowKey)
}
const unattendedCreationError = computed(() => {
  if (!isUnattendedRecipe.value) return ''
  const mode = unattendedConditioningMode.value
  if (!mode) return '所选工作流未声明项目默认的条件输入模式。'
  const stageKeys = new Set(
    (Array.isArray(selectedRecipe.value?.definition?.stages) ? selectedRecipe.value.definition.stages : [])
      .filter((value): value is JsonObject => Boolean(value && typeof value === 'object' && !Array.isArray(value)))
      .map((value) => String(value.key || '')),
  )
  if (mode.requiresEntityReferenceBible && (!stageKeys.has('bible_image_generation') || !stageKeys.has('bible_image_review'))) return '当前全自动配方缺少该条件模式要求的 Bible 参考图生成/审核阶段。'
  const existingBibleCount = project.value
    ? project.value.bible.characters.length + project.value.bible.scenes.length + project.value.bible.props.length
    : 0
  if (existingBibleCount) return '真正全自动的新剧本运行要求角色、场景和道具 Bible 均为空；系统会从本次剧本生成新 Bible。请新建空项目，或改用人工/半自动配方处理现有 Bible。'
  const topology = unattendedReferenceTopology.value
  if (unattendedReferenceVideoSelectable.value && (!topology || !topology.videos.purposes.length)) return '所选工作流未冻结动态参考视频用途合同，不能创建运行。'
  if (selectedUnattendedReferences.value.length && !unattendedReferenceVideoSelectable.value) return '所选工作流未声明动态参考视频输入，请清空该素材。'
  if (unattendedReferenceVideoRequired.value || selectedUnattendedReferences.value.length) {
    if (managedReferenceVideoAssetsError.value) return managedReferenceVideoAssetsError.value
    if (unattendedReferenceVideoRequired.value && !unattendedReferenceVideos.value.length) return `所选工作流明确要求动态参考视频，但素材库没有通过 ${unattendedReferenceVideoDurationLabel.value} 合同校验的受管视频；请先上传并完成媒体探测。`
  }
  const referenceError = unattendedReferenceInputDraftError({
    drafts: unattendedReferenceInputs.value,
    cardinality: unattendedReferenceVideoInput.value,
    availableAssetIds: unattendedAvailableReferenceAssetIds.value,
    topology,
  })
  if (referenceError) return referenceError
  if (!unattendedConfirmed.value) return '请明确确认由已发布决策策略全程自动审核并继续。'
  return ''
})
const recipeHasMediaGeneration = computed(() => {
  const stages = selectedRecipe.value?.definition?.stages
  return Array.isArray(stages) && stages.some((value) => (
    value && typeof value === 'object' && !Array.isArray(value)
    && String((value as JsonObject).key || '') === 'media_generation'
  ))
})
const recipeHasBibleImageGeneration = computed(() => {
  const stages = selectedRecipe.value?.definition?.stages
  return Array.isArray(stages) && stages.some((value) => (
    value && typeof value === 'object' && !Array.isArray(value)
    && String((value as JsonObject).key || '') === 'bible_image_generation'
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
  && (shot.mediaType !== 'video' || Boolean(conditioningModeByKey(option.conditioningModes, shot.mode)))
  && !(option.driverType === 'async_rest' && shot.mediaType === 'video' && shotHasReferences(shot))
)
const routeSupportsEpisode = (option: ProductionRecipeMediaRouteOption, episode: typeof workflowEpisodes.value[number]) => (
  option.isSelectable && episode.shots.every((shot) => routeSupportsShot(option, shot))
)
const routeSupportsUniform = (option: ProductionRecipeMediaRouteOption) => (
  option.isSelectable && (
    workflowShotRows.value.length
      ? workflowShotRows.value.every(({ shot }) => routeSupportsShot(option, shot))
      : option.outputKinds.includes('video')
        && Boolean(conditioningModeByKey(option.conditioningModes, project.value?.defaultMode))
  )
)
const promptContractKey = (operation: string, mode: string) => JSON.stringify([operation, mode])
const promptRequirementForShot = (option: ProductionRecipeMediaRouteOption, shot: ProjectShot) => {
  const operation = shot.mediaType === 'image' ? 'image.generate' : 'video.generate'
  const mode = shot.mediaType === 'video' ? shot.mode : ''
  return option.promptProfileRequirements.find((item) => item.operation === operation && item.mode === mode)
}
const promptChoicesForShot = (shot: ProjectShot) => {
  const option = recipeMediaRoutes.value.find((item) => item.routeIndex === shotWorkflowRouteIndexes.value[shot.id])
  return option ? promptRequirementForShot(option, shot)?.choices || [] : []
}
const uniformPromptProfileRequirements = computed(() => {
  const option = recipeMediaRoutes.value.find((item) => item.routeIndex === uniformWorkflowRouteIndex.value)
  if (!option) return []
  const contracts = workflowShotRows.value.length
    ? workflowShotRows.value.map(({ shot }) => ({
        operation: shot.mediaType === 'image' ? 'image.generate' : 'video.generate',
        mode: shot.mediaType === 'video' ? shot.mode : '',
      }))
    : [{ operation: 'video.generate', mode: project.value?.defaultMode || '' }]
  const seen = new Set<string>()
  return contracts.flatMap(({ operation, mode }) => {
    const key = promptContractKey(operation, mode)
    if (seen.has(key)) return []
    seen.add(key)
    const requirement = option.promptProfileRequirements.find((item) => item.operation === operation && item.mode === mode)
    return requirement ? [requirement] : []
  })
})
const promptProfileChoiceLabel = (choice: ProductionPromptProfileRequirement['choices'][number]) => (
  `${choice.bindingName || choice.bindingKey} · ${choice.profileName || choice.profileKey} v${choice.profileVersion} · ${choice.bindingScope} · ${choice.contractSha256.slice(0, 8)}`
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
  const recipeStageKeys = new Set(
    (Array.isArray(selectedRecipe.value?.definition?.stages) ? selectedRecipe.value.definition.stages : [])
      .filter((value): value is JsonObject => Boolean(value && typeof value === 'object' && !Array.isArray(value)))
      .map((value) => String(value.key || '')),
  )
  if (
    recipeMediaRoutes.value.some((item) => item.conditioningModes.some((mode) => mode.requiresEntityReferenceBible))
    && (!recipeStageKeys.has('bible_image_generation') || !recipeStageKeys.has('bible_image_review'))
  ) return '当前配方的条件模式要求实体参考 Bible，但缺少 Bible 参考图生成/审核闭环；请在系统设置中发布完整配方版本。'
  if (workflowSelectionMode.value === 'uniform') {
    if (uniformWorkflowRouteIndex.value === null || !selectable.has(uniformWorkflowRouteIndex.value)) return '请选择依赖完整的整集统一工作流。'
    const option = recipeMediaRoutes.value.find((item) => item.routeIndex === uniformWorkflowRouteIndex.value)
    const incompatible = option ? workflowShotRows.value.filter(({ shot }) => !routeSupportsShot(option, shot)) : []
    if (incompatible.length) return `当前统一工作流与 ${incompatible.length} 个镜头的输出类型或条件输入模式不兼容。`
    const expectedCount = new Set((workflowShotRows.value.length
      ? workflowShotRows.value.map(({ shot }) => promptContractKey(
          shot.mediaType === 'image' ? 'image.generate' : 'video.generate',
          shot.mediaType === 'video' ? shot.mode : '',
        ))
      : [promptContractKey('video.generate', project.value?.defaultMode || '')]
    )).size
    if (uniformPromptProfileRequirements.value.length !== expectedCount) return '所选 route 缺少当前镜头合同可选的 PromptProfile binding。'
    const missingProfile = uniformPromptProfileRequirements.value.some((item) => (
      !item.choices.some((choice) => choice.bindingId === uniformPromptProfileBindingIds.value[promptContractKey(item.operation, item.mode)])
    ))
    if (missingProfile) return '请为整集 route 的每个 operation/mode 显式选择 PromptProfile 策略。'
    if (recipeHasBibleImageGeneration.value && !recipeBiblePromptProfileRequirement.value?.choices.some((choice) => choice.bindingId === bibleImagePromptProfileBindingId.value)) return 'Bible 参考图必须显式选择可执行的 PromptProfile 策略。'
    return ''
  }
  if (!workflowShotRows.value.length) return '逐镜头模式要求项目已有正式 Shot；AI/导入尚未拆分时只能选择整集统一工作流。'
  const missing = workflowShotRows.value.filter(({ shot }) => {
    const index = shotWorkflowRouteIndexes.value[shot.id]
    const option = recipeMediaRoutes.value.find((item) => item.routeIndex === index)
    const requirement = option ? promptRequirementForShot(option, shot) : undefined
    return index === null || index === undefined || !option || !routeSupportsShot(option, shot)
      || !requirement?.choices.some((choice) => choice.bindingId === shotPromptProfileBindingIds.value[shot.id])
  })
  if (missing.length) return `还有 ${missing.length} 个镜头未显式选择兼容的工作流与 PromptProfile 策略。`
  if (recipeHasBibleImageGeneration.value && !recipeBiblePromptProfileRequirement.value?.choices.some((choice) => choice.bindingId === bibleImagePromptProfileBindingId.value)) return 'Bible 参考图必须显式选择可执行的 PromptProfile 策略。'
  return ''
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
const bibleImageReview = computed(() => parseBibleImageReviewPayload(currentRun.value?.stages || []))
const storyboardReview = computed(() => parseStoryboardReviewPayload(
  currentRun.value?.stages || [],
  productionCapability.value?.effectiveLimits,
))
const visualRulesReview = computed(() => parseVisualRulesReviewPayload(currentRun.value?.stages || []))
const normalizationReview = computed(() => parsePromptNormalizationReviewPayload(
  currentRun.value?.stages || [],
  productionCapability.value?.effectiveLimits,
))
const normalizationRetryRepairs = computed(() => parsePromptNormalizationRetryRepairs(
  normalizationAttempts.value,
  normalizationReview.value.value?.validationFailures || [],
))
const normalizationRetryRepairByFailure = computed(() => new Map(
  (normalizationRetryRepairs.value.value || []).map((repair) => [repair.failureAttemptId, repair]),
))
const normalizationHumanFallback = computed(() => (
  normalizationReview.value.value?.reviewOrigin === 'human_after_validation_retry_exhausted'
))
const normalizationHumanOnly = computed(() => (
  ['human_only_recipe', 'human_only_external_disabled'].includes(normalizationReview.value.value?.reviewOrigin || '')
))
const normalizationHumanOnlyRecipe = computed(() => normalizationReview.value.value?.reviewOrigin === 'human_only_recipe')
const normalizationManualEntry = computed(() => normalizationHumanFallback.value || normalizationHumanOnly.value)
const blockedPresentation = computed(() => productionIssuePresentation(
  currentRun.value?.blockedCode || currentRun.value?.status || '',
  currentRun.value?.blockedReason,
  currentRun.value?.status || '',
))
const actionableRuns = computed(() => actionableProductionRuns(runs.value))
const routeConditioningLabels = (row: JsonObject): string => {
  const contract = row.conditioning_contract
  if (!contract || typeof contract !== 'object' || Array.isArray(contract)) return ''
  const modes = (contract as JsonObject).modes
  if (!Array.isArray(modes)) return ''
  return modes.flatMap((item) => item && typeof item === 'object' && !Array.isArray(item)
    ? [String((item as JsonObject).label || (item as JsonObject).key || '')]
    : []).filter(Boolean).join(', ')
}
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
      workflowConditioningModes: routeConditioningLabels(row),
      isExternal: row.is_external === true,
    }]
  })
})
const canCreate = computed(() => Boolean(productionCapability.value && selectedRecipeId.value && !actionBusy.value && !isInvalidRecipe.value && !workflowSelectionError.value && !unattendedCreationError.value && (
  isAiScriptRecipe.value
    ? projectEpisodes.value.length === 0 && Boolean(aiIdea.value.trim() && aiRunPlan.value)
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
    if (bibleStageIds.has(item.stageRun)) continue
    const mediaKind = item.kind === 'image' || item.kind === 'video' || item.kind === 'assembly' || item.mimeType.startsWith('image/') || item.mimeType.startsWith('video/')
    if (!mediaKind) continue
    const key = item.variantGroup || `artifact:${item.id}`
    groups.set(key, [...(groups.get(key) || []), item])
  }
  return [...groups.entries()].map(([key, items]) => ({ key, items: items.sort((a, b) => a.variantIndex - b.variantIndex) }))
})
const bibleCandidatePreview = (artifactId: string, expectedSha256: string) => {
  const history = bibleImageHistoryByArtifactId.value[artifactId]
  return history
    && history.sha256 === expectedSha256
    && history.sourceCurrent
    && history.integrityStatus === 'valid'
    ? history.previewUrl
    : ''
}
const bibleEntityKey = (entityType: string, entityId: string) => `${entityType}:${entityId}`
const bibleSelectionFor = (entityType: string, entityId: string) => (
  bibleImageSelections.value[bibleEntityKey(entityType, entityId)]
  || { selectedArtifactIds: [], selectedAssetIds: [], anchorArtifactId: '', anchorAssetId: '' }
)
const eventChecked = (event: Event) => (event.target as HTMLInputElement).checked
const eventValue = (event: Event) => (event.target as HTMLInputElement | HTMLSelectElement).value
const toggleBibleArtifact = (entityType: string, entityId: string, artifactId: string, checked: boolean) => {
  const key = bibleEntityKey(entityType, entityId)
  const current = bibleSelectionFor(entityType, entityId)
  const selected = new Set(current.selectedArtifactIds)
  if (checked) selected.add(artifactId)
  else selected.delete(artifactId)
  bibleImageSelections.value = {
    ...bibleImageSelections.value,
    [key]: {
      selectedArtifactIds: [...selected],
      selectedAssetIds: current.selectedAssetIds,
      anchorArtifactId: selected.has(current.anchorArtifactId) ? current.anchorArtifactId : '',
      anchorAssetId: current.anchorAssetId,
    },
  }
}
const setBibleAnchor = (entityType: string, entityId: string, artifactId: string) => {
  const key = bibleEntityKey(entityType, entityId)
  const current = bibleSelectionFor(entityType, entityId)
  if (!current.selectedArtifactIds.includes(artifactId)) return
  bibleImageSelections.value = {
    ...bibleImageSelections.value,
    [key]: { ...current, anchorArtifactId: artifactId, anchorAssetId: '' },
  }
}
const storyboardShots = computed(() => storyboardReview.value.value?.episodes.flatMap((episode) => episode.shots) || [])
const storyboardAssetById = computed(() => new Map(studio.assets.map((asset) => [asset.id, asset])))
const bibleLibraryAssetsFor = (entityType: 'character' | 'scene' | 'prop') => studio.assets.filter((asset) => (
  asset.type === entityType
  && asset.isActive !== false
  && asset.metadata?.usage !== 'project_cover'
  && asset.mimeType?.startsWith('image/')
  && /^[0-9a-f]{64}$/i.test(asset.checksum || '')
  && Boolean(asset.thumbnail || asset.mediaUrl)
))
const BIBLE_LIBRARY_PAGE_SIZE = 12
const bibleLibrarySearchFor = (entityType: string, entityId: string) => (
  bibleLibrarySearches.value[bibleEntityKey(entityType, entityId)] || ''
)
const filteredBibleLibraryAssetsFor = (
  entityType: 'character' | 'scene' | 'prop',
  entityId: string,
) => {
  const query = bibleLibrarySearchFor(entityType, entityId).trim().toLocaleLowerCase('zh-CN')
  const selected = new Set(bibleSelectionFor(entityType, entityId).selectedAssetIds)
  return bibleLibraryAssetsFor(entityType)
    .filter((asset) => !query || `${asset.name} ${asset.backendKind || ''} ${asset.id}`.toLocaleLowerCase('zh-CN').includes(query))
    .sort((left, right) => {
      const selectedOrder = Number(selected.has(right.id)) - Number(selected.has(left.id))
      return selectedOrder || left.name.localeCompare(right.name, 'zh-CN')
    })
}
const visibleBibleLibraryAssetsFor = (
  entityType: 'character' | 'scene' | 'prop',
  entityId: string,
) => {
  const key = bibleEntityKey(entityType, entityId)
  return filteredBibleLibraryAssetsFor(entityType, entityId).slice(
    0,
    bibleLibraryLimits.value[key] || BIBLE_LIBRARY_PAGE_SIZE,
  )
}
const bibleLibraryIsExpanded = (entityType: string, entityId: string) => Boolean(
  bibleLibraryExpanded.value[bibleEntityKey(entityType, entityId)],
)
const setBibleLibraryExpanded = (entityType: string, entityId: string, expanded: boolean) => {
  const key = bibleEntityKey(entityType, entityId)
  bibleLibraryExpanded.value = { ...bibleLibraryExpanded.value, [key]: expanded }
  if (expanded && !bibleLibraryLimits.value[key]) {
    bibleLibraryLimits.value = { ...bibleLibraryLimits.value, [key]: BIBLE_LIBRARY_PAGE_SIZE }
  }
}
const setBibleLibrarySearch = (entityType: string, entityId: string, value: string) => {
  const key = bibleEntityKey(entityType, entityId)
  bibleLibrarySearches.value = { ...bibleLibrarySearches.value, [key]: value }
  bibleLibraryLimits.value = { ...bibleLibraryLimits.value, [key]: BIBLE_LIBRARY_PAGE_SIZE }
}
const showMoreBibleLibraryAssets = (entityType: string, entityId: string) => {
  const key = bibleEntityKey(entityType, entityId)
  bibleLibraryLimits.value = {
    ...bibleLibraryLimits.value,
    [key]: (bibleLibraryLimits.value[key] || BIBLE_LIBRARY_PAGE_SIZE) + BIBLE_LIBRARY_PAGE_SIZE,
  }
}
const bibleLibraryAsset = (assetId: string) => studio.assets.find((asset) => asset.id === assetId)
const toggleBibleLibraryAsset = (entityType: 'character' | 'scene' | 'prop', entityId: string, assetId: string, checked: boolean) => {
  const key = bibleEntityKey(entityType, entityId)
  const current = bibleSelectionFor(entityType, entityId)
  const selected = new Set(current.selectedAssetIds)
  if (checked) selected.add(assetId)
  else selected.delete(assetId)
  bibleImageSelections.value = {
    ...bibleImageSelections.value,
    [key]: {
      selectedArtifactIds: current.selectedArtifactIds,
      selectedAssetIds: [...selected],
      anchorArtifactId: current.anchorArtifactId,
      anchorAssetId: selected.has(current.anchorAssetId) ? current.anchorAssetId : '',
    },
  }
}
const setBibleLibraryAnchor = (entityType: 'character' | 'scene' | 'prop', entityId: string, assetId: string) => {
  const key = bibleEntityKey(entityType, entityId)
  const current = bibleSelectionFor(entityType, entityId)
  if (!current.selectedAssetIds.includes(assetId)) return
  bibleImageSelections.value = {
    ...bibleImageSelections.value,
    [key]: { ...current, anchorArtifactId: '', anchorAssetId: assetId },
  }
}
const storyboardApprovedAssets = computed(() => storyboardReview.value.value?.approvedBibleAssets || [])
const approvedAssetsForEntity = (entityType: 'character' | 'scene' | 'prop', entityId: string) => (
  storyboardApprovedAssets.value.filter((item) => item.entityType === entityType && item.entityId === entityId)
)
const approvedFrameAssets = computed(() => storyboardApprovedAssets.value)
const storyboardAsset = (assetId: string): Asset | undefined => storyboardAssetById.value.get(assetId)
const storyboardMediaDurationMs = (asset: Asset | undefined) => {
  const value = Number(asset?.metadata?.duration_ms)
  return Number.isInteger(value) ? value : 0
}
const storyboardReferenceVideos = computed(() => studio.assets.filter((asset) => (
  asset.type === 'video' && asset.backendKind === 'reference_video' && isProductionReferenceAsset(asset)
)))
const storyboardReferenceAudios = computed(() => studio.assets.filter((asset) => (
  asset.type === 'audio' && asset.backendKind === 'reference_audio'
)))
const storyboardMediaUsable = (
  assetId: string,
  kind: 'video' | 'audio',
  shotId = '',
  usage: 'paired' | 'standalone' = 'standalone',
) => {
  const asset = storyboardAsset(assetId)
  const durationMs = storyboardMediaDurationMs(asset)
  const topology = storyboardShots.value.find((item) => item.id === shotId)
    ?.conditioningModeCapability?.dynamicReferenceTopology
  const preprocessing = kind === 'video'
    ? topology?.preprocessing.videos
    : usage === 'paired'
      ? topology?.preprocessing.pairedVideoAudios
      : topology?.preprocessing.referenceAudios
  const sourceFps = Number(asset?.metadata?.source_fps)
  const requiresSourceFps = kind === 'video'
    ? Boolean(topology?.preprocessing.videos?.requiresSourceFps)
    : false
  return Boolean(
    asset
    && asset.isActive !== false
    && asset.type === kind
    && asset.backendKind === `reference_${kind}`
    && asset.mimeType?.startsWith(`${kind}/`)
    && /^[0-9a-f]{64}$/i.test(asset.checksum || '')
    && durationMs > 0
    && (!shotId || Boolean(preprocessing))
    && (!preprocessing || (
      durationMs >= preprocessing.minimumDurationMs
      && durationMs <= preprocessing.maximumDurationMs
      && (!requiresSourceFps || sourceFps > 0)
    )),
  )
}
const storyboardHasEmbeddedAudio = (assetId: string) => storyboardAsset(assetId)?.hasEmbeddedAudio === true
const storyboardAssetLabel = (assetId: string) => {
  const asset = storyboardAsset(assetId)
  const fact = storyboardApprovedAssets.value.find((item) => item.assetId === assetId)
  if (fact) return `${asset?.name || fact.entityName || '参考图'} · ${fact.anchor ? 'anchor' : 'reference'} · ${assetId.slice(0, 8)}`
  const durationMs = storyboardMediaDurationMs(asset)
  return `${asset?.name || '参考素材'} · ${durationMs ? `${(durationMs / 1000).toFixed(1)}s · ` : ''}${assetId.slice(0, 8)}`
}
const bibleEntityName = (entityType: 'character' | 'scene' | 'prop', entityId: string) => {
  const collection = entityType === 'character'
    ? project.value?.bible.characters
    : entityType === 'scene'
      ? project.value?.bible.scenes
      : project.value?.bible.props
  return collection?.find((item) => item.id === entityId)?.name || entityId
}
const storyboardDraftFor = (shotId: string) => storyboardReferenceDrafts.value[shotId] || emptyStoryboardReferenceDraft()
const storyboardDraftAssetIds = (shotId: string) => {
  return referenceDraftAssetIds(storyboardDraftFor(shotId))
}
const selectedStoryboardAssetUsable = (assetId: string) => {
  const fact = storyboardApprovedAssets.value.find((item) => item.assetId === assetId)
  const asset = storyboardAsset(assetId)
  return Boolean(
    fact
    && asset
    && asset.isActive !== false
    && asset.checksum === fact.checksum
    && asset.mimeType === fact.mimeType
    && (asset.thumbnail || asset.mediaUrl),
  )
}
const storyboardVideoSelected = (shotId: string, assetId: string) => (
  storyboardDraftFor(shotId).videos.some((item) => item.assetId === assetId)
)
const storyboardAudioSelected = (shotId: string, assetId: string) => (
  storyboardDraftFor(shotId).audios.some((item) => item.assetId === assetId)
)
const storyboardPairedAudioOrdinal = (shotId: string, videoIndex: number) => (
  storyboardDraftFor(shotId).videos
    .slice(0, videoIndex + 1)
    .filter((item) => item.useEmbeddedAudio || item.audioAssetId).length
)
const storyboardStandaloneAudioOrdinal = (shotId: string, audioIndex: number) => (
  storyboardDraftFor(shotId).videos.filter((item) => item.useEmbeddedAudio || item.audioAssetId).length
  + audioIndex + 1
)
const storyboardInputLabel = (shotId: string, assetId: string) => {
  const shot = storyboardShots.value.find((item) => item.id === shotId)
  const draft = storyboardDraftFor(shotId)
  const pictures = [
    draft.firstFrameReferenceAssetId,
    ...(shot?.visualReferenceCharacterIds || []).map((id) => draft.characterAssetIds[id] || ''),
    ...(shot?.sceneId ? [draft.sceneAssetId] : []),
    ...(shot?.propIds || []).map((id) => draft.propAssetIds[id] || ''),
  ].filter(Boolean)
  const pictureIndex = pictures.indexOf(assetId)
  if (pictureIndex >= 0) return `Picture ${pictureIndex + 1}`
  const videoIndex = draft.videos.findIndex((item) => item.assetId === assetId)
  if (videoIndex >= 0) return `Video ${videoIndex + 1}`
  for (const [index, video] of draft.videos.entries()) {
    if (video.audioAssetId === assetId) return `Audio ${storyboardPairedAudioOrdinal(shotId, index)}`
  }
  const audioIndex = draft.audios.findIndex((item) => item.assetId === assetId)
  return audioIndex >= 0 ? `Audio ${storyboardStandaloneAudioOrdinal(shotId, audioIndex)}` : 'Asset'
}
const storyboardUsedMediaAssetIds = (shotId: string, exceptVideoAssetId = '') => {
  const draft = storyboardDraftFor(shotId)
  return new Set([
    ...draft.videos.flatMap((item) => (
      item.assetId === exceptVideoAssetId ? [item.assetId] : [item.assetId, item.audioAssetId || '']
    )),
    ...draft.audios.map((item) => item.assetId),
  ].filter(Boolean))
}
const storyboardShot = (shotId: string) => storyboardShots.value.find((item) => item.id === shotId)
const storyboardReferenceTopology = (shotId: string) => (
  storyboardShot(shotId)?.conditioningModeCapability?.dynamicReferenceTopology || null
)
const storyboardVideoPurposes = (shotId: string) => storyboardReferenceTopology(shotId)?.videos.purposes || []
const storyboardAudioPurposes = (shotId: string) => storyboardReferenceTopology(shotId)?.audios.purposes || []
const storyboardPairingRule = (shotId: string, purpose: string) => (
  storyboardReferenceTopology(shotId)?.pairingRules.find((rule) => rule.sourcePurposeKeys.includes(purpose))
)
const storyboardPairedAudioPurposes = (shotId: string, videoPurpose: string) => {
  const rule = storyboardPairingRule(shotId, videoPurpose)
  const allowed = new Set(rule?.targetPurposeKeys || [])
  return storyboardAudioPurposes(shotId).filter((purpose) => allowed.has(purpose.key))
}
const storyboardPurposeDescription = (shotId: string, kind: 'video' | 'audio', purpose: string) => (
  (kind === 'video' ? storyboardVideoPurposes(shotId) : storyboardAudioPurposes(shotId))
    .find((item) => item.key === purpose)?.description || '该用途未在当前工作流合同中声明。'
)
const storyboardPreprocessingLabel = (
  shotId: string,
  kind: 'video' | 'paired_audio' | 'reference_audio',
) => {
  const preprocessing = kind === 'video'
    ? storyboardReferenceTopology(shotId)?.preprocessing.videos
    : kind === 'paired_audio'
      ? storyboardReferenceTopology(shotId)?.preprocessing.pairedVideoAudios
      : storyboardReferenceTopology(shotId)?.preprocessing.referenceAudios
  return preprocessing
    ? `${preprocessing.minimumDurationMs / 1000}–${preprocessing.maximumDurationMs / 1000} 秒 · ${preprocessing.adapterKey}`
    : '未启用'
}
const storyboardVideoContract = (shotId: string) => (
  storyboardShot(shotId)?.conditioningModeCapability?.inputs.referenceVideos
  || { minimumItems: 0, maximumItems: 0 }
)
const storyboardVideoRangeLabel = (shotId: string) => {
  const contract = storyboardVideoContract(shotId)
  return `${contract.minimumItems}–${contract.maximumItems}`
}
const storyboardVideoContractHint = (shotId: string) => {
  const contract = storyboardVideoContract(shotId)
  if (storyboardShot(shotId)?.conditioningModeCapability?.inputs.referenceVideos.maximumItems && !storyboardReferenceTopology(shotId)) return '当前工作流缺少动态参考用途合同，已关闭选择。'
  if (contract.maximumItems === 0) return '所选 Workflow 不接受动态参考视频。'
  if (contract.minimumItems > 0) return `所选 Workflow 要求选择 ${contract.minimumItems}–${contract.maximumItems} 条动态参考视频。`
  return `所选 Workflow 允许选择 0–${contract.maximumItems} 条动态参考视频，可留空。`
}
const toggleStoryboardVideo = (shotId: string, assetId: string, checked: boolean) => {
  const draft = storyboardDraftFor(shotId)
  const existing = draft.videos.findIndex((item) => item.assetId === assetId)
  if (!checked) {
    if (existing >= 0) draft.videos.splice(existing, 1)
    return
  }
  if (existing >= 0) return
  const maximumItems = storyboardVideoContract(shotId).maximumItems
  if (maximumItems === 0) {
    studio.notify('所选 Workflow 不接受动态参考视频', 'error')
    return
  }
  if (draft.videos.length >= maximumItems) {
    studio.notify(`当前 Workflow 每个镜头最多选择 ${maximumItems} 个参考视频`, 'error')
    return
  }
  if (storyboardUsedMediaAssetIds(shotId).has(assetId)) {
    studio.notify('同一素材不能重复承担多个输入槽位', 'error')
    return
  }
  const purpose = storyboardReferenceTopology(shotId)?.videos.defaultPurposeKey || ''
  if (!purpose) {
    studio.notify('当前工作流未声明参考视频默认用途，已禁止选择', 'error')
    return
  }
  draft.videos.push({ assetId, purpose, useEmbeddedAudio: false })
}
const toggleStoryboardAudio = (shotId: string, assetId: string, checked: boolean) => {
  const draft = storyboardDraftFor(shotId)
  const existing = draft.audios.findIndex((item) => item.assetId === assetId)
  if (!checked) {
    if (existing >= 0) draft.audios.splice(existing, 1)
    return
  }
  if (existing >= 0) return
  const topology = storyboardReferenceTopology(shotId)
  if (!topology) {
    studio.notify('当前工作流未冻结动态参考音频合同，已禁止选择', 'error')
    return
  }
  if (draft.audios.length >= topology.audios.maximumItems) {
    studio.notify(`当前工作流最多选择 ${topology.audios.maximumItems} 个独立参考音频`, 'error')
    return
  }
  if (storyboardUsedMediaAssetIds(shotId).has(assetId)) {
    studio.notify('该音频已作为视频配套音频或其他输入使用', 'error')
    return
  }
  if (!topology.audios.defaultPurposeKey) {
    studio.notify('当前工作流未声明独立参考音频默认用途，已禁止选择', 'error')
    return
  }
  draft.audios.push({ assetId, purpose: topology.audios.defaultPurposeKey })
}
const storyboardVideoPurposeDisabled = (shotId: string, videoAssetId: string, purpose: StoryboardVideoPurpose) => {
  const topology = storyboardReferenceTopology(shotId)
  if (!topology || !topology.videos.purposes.some((item) => item.key === purpose)) return true
  const purposes = storyboardDraftFor(shotId).videos.map((item) => (
    item.assetId === videoAssetId ? purpose : item.purpose
  ))
  return topology.mutualExclusionRules.some((rule) => {
    if (rule.mediaKind !== 'video') return false
    const used = new Set(purposes.filter((key) => rule.purposeKeys.includes(key)))
    return used.size > rule.maximumDistinctPurposes
  })
}
const setStoryboardVideoPurpose = (shotId: string, videoAssetId: string, event: Event) => {
  const item = storyboardDraftFor(shotId).videos.find((value) => value.assetId === videoAssetId)
  const select = event.target as HTMLSelectElement
  const purpose = select.value as StoryboardVideoPurpose
  if (!item || !storyboardVideoPurposes(shotId).some((definition) => definition.key === purpose)) return
  if (storyboardVideoPurposeDisabled(shotId, videoAssetId, purpose)) {
    select.value = item.purpose
    studio.notify('该用途组合违反当前工作流配置的互斥规则', 'error')
    return
  }
  item.purpose = purpose
  const rule = storyboardPairingRule(shotId, purpose)
  if ((item.useEmbeddedAudio || item.audioAssetId) && !rule?.targetPurposeKeys.includes(item.audioPurpose || '')) {
    item.audioPurpose = rule?.defaultTargetPurposeKey
  }
}
const setStoryboardEmbeddedAudio = (shotId: string, videoAssetId: string, checked: boolean) => {
  const item = storyboardDraftFor(shotId).videos.find((value) => value.assetId === videoAssetId)
  if (!item) return
  const rule = storyboardPairingRule(shotId, item.purpose)
  if (checked && !rule?.allowEmbeddedSource) {
    studio.notify('当前视频用途合同不允许使用内嵌音轨', 'error')
    return
  }
  if (checked && !storyboardHasEmbeddedAudio(videoAssetId)) {
    studio.notify('该视频未检测到可用内嵌音轨，请选择外部配套音频', 'error')
    return
  }
  item.useEmbeddedAudio = checked
  if (checked) {
    item.audioAssetId = undefined
    item.audioPurpose = rule?.defaultTargetPurposeKey
  } else if (!item.audioAssetId) item.audioPurpose = undefined
}
const setStoryboardPairedAudio = (shotId: string, videoAssetId: string, audioAssetId: string) => {
  const item = storyboardDraftFor(shotId).videos.find((value) => value.assetId === videoAssetId)
  if (!item) return
  const rule = storyboardPairingRule(shotId, item.purpose)
  if (audioAssetId && !rule?.allowExternalTarget) {
    studio.notify('当前视频用途合同不允许外部配套音频', 'error')
    return
  }
  if (audioAssetId && storyboardUsedMediaAssetIds(shotId, videoAssetId).has(audioAssetId)) {
    studio.notify('该音频已占用其他输入槽位', 'error')
    return
  }
  item.useEmbeddedAudio = false
  item.audioAssetId = audioAssetId || undefined
  item.audioPurpose = audioAssetId && rule
    ? rule.targetPurposeKeys.includes(item.audioPurpose || '')
      ? item.audioPurpose
      : rule.defaultTargetPurposeKey
    : undefined
}
const setStoryboardPairedAudioPurpose = (shotId: string, videoAssetId: string, value: string) => {
  const item = storyboardDraftFor(shotId).videos.find((row) => row.assetId === videoAssetId)
  const rule = item ? storyboardPairingRule(shotId, item.purpose) : undefined
  if (item && (item.useEmbeddedAudio || item.audioAssetId) && rule?.targetPurposeKeys.includes(value)) {
    item.audioPurpose = value as StoryboardAudioPurpose
  }
}
const setStoryboardAudioPurpose = (shotId: string, assetId: string, value: string) => {
  const item = storyboardDraftFor(shotId).audios.find((row) => row.assetId === assetId)
  if (item && storyboardAudioPurposes(shotId).some((purpose) => purpose.key === value)) {
    item.purpose = value as StoryboardAudioPurpose
  }
}
const uploadStoryboardReference = async (
  shotId: string,
  kind: 'video' | 'audio',
  event: Event,
  pairedVideoAssetId = '',
) => {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file || !project.value) return
  storyboardUploadingReference.value = `${shotId}:${kind}:${pairedVideoAssetId}`
  try {
    const asset = await studio.uploadReferenceAsset(
      file,
      kind === 'video' ? 'reference_video' : 'reference_audio',
      project.value.styleId,
    )
    if (kind === 'video') toggleStoryboardVideo(shotId, asset.id, true)
    else if (pairedVideoAssetId) setStoryboardPairedAudio(shotId, pairedVideoAssetId, asset.id)
    else toggleStoryboardAudio(shotId, asset.id, true)
  } catch (cause) {
    studio.notify(message(cause), 'error')
  } finally {
    storyboardUploadingReference.value = ''
    input.value = ''
  }
}
const storyboardShotReferenceBindingsPayload = computed<StoryboardShotReferenceSelectionInput[]>(() => (
  storyboardShots.value.map((shot) => ({ shotId: shot.id, referenceBindings: buildStoryboardShotReferenceBindings(shot, storyboardDraftFor(shot.id)) }))
))
const storyboardReferenceError = computed(() => {
  if (storyboardAssetsLoading.value) return '正在读取本 Run 已批准参考图的正式 Asset，请稍候。'
  if (storyboardAssetsError.value) return storyboardAssetsError.value
  for (const fact of storyboardApprovedAssets.value) {
    if (!selectedStoryboardAssetUsable(fact.assetId)) return `${fact.entityName} 的已批准参考图缺少可校验预览、MIME 或 checksum，请刷新后重试。`
  }
  for (const shot of storyboardShots.value) {
    const draft = storyboardDraftFor(shot.id)
    const frameError = validateStoryboardFrameDraft(shot, draft, selectedStoryboardAssetUsable)
    if (frameError) return frameError
    const mediaError = validateStoryboardMediaDraft(
      shot,
      draft,
      (assetId, kind, usage) => storyboardMediaUsable(
        assetId,
        kind,
        shot.id,
        usage || 'standalone',
      ),
      storyboardHasEmbeddedAudio,
    )
    if (mediaError) return mediaError
    const videoContract = shot.conditioningModeCapability?.inputs.referenceVideos
      || { minimumItems: 0, maximumItems: 0 }
    if (
      draft.videos.length < videoContract.minimumItems
      || draft.videos.length > videoContract.maximumItems
    ) return `镜头 ${shot.number}：所选 Workflow 要求 ${videoContract.minimumItems}–${videoContract.maximumItems} 条动态参考视频，当前选择 ${draft.videos.length} 条。`
    if (shot.mediaType === 'image' || shot.conditioningModeCapability?.inputKind !== 'classified_references') continue
    if (shot.conditioningModeCapability.inputKind === 'classified_references') {
      const formalBibleRequired = shot.conditioningModeCapability.requiresEntityReferenceBible
      if (formalBibleRequired && draft.firstFrameReferenceAssetId && !selectedStoryboardAssetUsable(draft.firstFrameReferenceAssetId)) {
        return `镜头 ${shot.number}：首帧连续参考必须来自本 Run 已批准的精确图片 Asset。`
      }
      for (const entityId of formalBibleRequired ? shot.visualReferenceCharacterIds : []) {
        const assetId = draft.characterAssetIds[entityId] || ''
        if (!approvedAssetsForEntity('character', entityId).some((item) => item.assetId === assetId) || !selectedStoryboardAssetUsable(assetId)) return `镜头 ${shot.number}：请为角色 ${bibleEntityName('character', entityId)} 选择本 Run 已批准的精确 Asset。`
      }
      if (formalBibleRequired && shot.sceneId) {
        if (!approvedAssetsForEntity('scene', shot.sceneId).some((item) => item.assetId === draft.sceneAssetId) || !selectedStoryboardAssetUsable(draft.sceneAssetId)) return `镜头 ${shot.number}：请为场景 ${bibleEntityName('scene', shot.sceneId)} 选择本 Run 已批准的精确 Asset。`
      }
      for (const entityId of formalBibleRequired ? shot.propIds : []) {
        const assetId = draft.propAssetIds[entityId] || ''
        if (!approvedAssetsForEntity('prop', entityId).some((item) => item.assetId === assetId) || !selectedStoryboardAssetUsable(assetId)) return `镜头 ${shot.number}：请为道具 ${bibleEntityName('prop', entityId)} 选择本 Run 已批准的精确 Asset。`
      }
    }
  }
  return ''
})
const internalNamePlaceholder = /^(?:S[1-9][0-9]*|(?:character|scene|prop):[a-z]+-[0-9a-f]+)$/i
const normalizationCandidateError = (requestId: string, fieldKey: string) => {
  const value = normalizationEdits.value[requestId]
  if (!value?.trim()) return `${normalizationManualEntry.value ? '人工英文定稿' : '英文候选'}不能为空。`
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
type VisualRuleReviewKey = keyof typeof visualRulesEdits.value
const visualRuleDefinitions: Array<{ key: VisualRuleReviewKey; label: string }> = [
  { key: 'negativePrompt', label: '全局负面提示词' },
  { key: 'colorScript', label: '色彩脚本' },
  { key: 'continuitySummary', label: '连续性摘要' },
]
const visualRuleAdvisoryMaximum = (key: VisualRuleReviewKey) => {
  const advisories = visualRulesReview.value.value?.qualityAdvisories
  if (!advisories) return null
  if (key === 'negativePrompt') return advisories.negativePromptMaxChars
  if (key === 'colorScript') return advisories.colorScriptMaxChars
  return advisories.continuitySummaryMaxChars
}
const visualRuleWarning = (key: VisualRuleReviewKey) => {
  const maximum = visualRuleAdvisoryMaximum(key)
  const value = visualRulesEdits.value[key] || ''
  return maximum !== null && value.length > maximum
    ? `当前 ${value.length} 字符，超过冻结质量建议 ${maximum} 字符；仅提醒，不阻断提交，绝对边界由服务端校验。`
    : ''
}
const visualRuleCurrentValue = (key: VisualRuleReviewKey) => {
  if (key === 'negativePrompt') return project.value?.bible.negativePrompt || ''
  if (key === 'colorScript') return project.value?.bible.colorScript || ''
  return project.value?.continuitySummary || ''
}
const visualRuleCandidateValue = (key: VisualRuleReviewKey) => visualRulesReview.value.value?.candidate[key] || ''
const visualRuleError = (key: VisualRuleReviewKey) => {
  const value = visualRulesEdits.value[key]
  if (!value.trim()) return '最终值不能为空。'
  if (/[^\x09\x0A\x0D\x20-\x7E]/.test(value)) return '正式执行字段只接受可打印英文字符；中文说明请写在审批备注。'
  return ''
}
const visualRulesReviewedCount = computed(() => visualRuleDefinitions.filter((item) => visualRulesReviewed.value[item.key]).length)
const restoreVisualRuleCandidate = (key: VisualRuleReviewKey) => {
  visualRulesEdits.value[key] = visualRuleCandidateValue(key)
  visualRulesReviewed.value[key] = false
}
const reviewApprovalError = computed(() => {
  if (currentStage.value?.stageKey === 'script_review') {
    if (scriptReview.value.error) return scriptReview.value.error
    if (reviewRevisionLoading.value) return '正在读取完整 ScriptRevision 正文，请稍候。'
    if (reviewRevisionError.value) return reviewRevisionError.value
    if (!reviewRevision.value?.content?.trim()) return 'ScriptRevision 正文为空，不能批准。'
  }
  if (currentStage.value?.stageKey === 'bible_review') {
    if (manualBibleReview.value) {
      if (missingManualBibleLabels.value.length) return `请先在项目 Bible 中至少各创建 1 个人物、场景和道具；当前缺少：${missingManualBibleLabels.value.join('、')}。`
    } else {
      if (bibleReview.value.error) return bibleReview.value.error
      if (bibleReview.value.value?.scriptRevisionId !== currentRun.value?.scriptRevision) return 'Bible 候选对应的 ScriptRevision 与本次运行不一致。'
    }
  }
  if (currentStage.value?.stageKey === 'bible_image_review') {
    if (bibleImageReview.value.error) return bibleImageReview.value.error
    if (bibleImageHistoryLoading.value) return '正在读取本项目冻结候选对应的历史预览与完整性证据。'
    if (bibleImageHistoryError.value) return bibleImageHistoryError.value
    const payload = bibleImageReview.value.value
    if (!payload) return 'Bible 参考图审核证据不可用。'
    for (const entity of payload.entities) {
      const selection = bibleSelectionFor(entity.entityType, entity.entityId)
      if (!selection.selectedArtifactIds.length && !selection.selectedAssetIds.length) return `${entity.entityName}：请至少勾选一张模型候选或素材库图片。`
      const validArtifactAnchor = Boolean(selection.anchorArtifactId && selection.selectedArtifactIds.includes(selection.anchorArtifactId))
      const validAssetAnchor = Boolean(selection.anchorAssetId && selection.selectedAssetIds.includes(selection.anchorAssetId))
      if (validArtifactAnchor === validAssetAnchor) return `${entity.entityName}：请从已勾选图片中指定唯一 anchor。`
      const allowed = new Set(entity.candidates.map((item) => item.artifactId))
      if (selection.selectedArtifactIds.some((id) => {
        const candidate = entity.candidates.find((item) => item.artifactId === id)
        return !allowed.has(id) || !candidate || !bibleCandidatePreview(id, candidate.sha256)
      })) return `${entity.entityName}：选择包含不属于冻结快照或完整性证据不可用的候选，请刷新。`
      const allowedAssets = new Set(bibleLibraryAssetsFor(entity.entityType).map((item) => item.id))
      if (selection.selectedAssetIds.some((id) => !allowedAssets.has(id))) return `${entity.entityName}：素材库选择已停用、分类不符或缺少图片完整性证据，请刷新。`
    }
  }
  if (currentStage.value?.stageKey === 'storyboard_review') {
    if (storyboardReview.value.error) return storyboardReview.value.error
    if (storyboardReferenceError.value) return storyboardReferenceError.value
  }
  if (currentStage.value?.stageKey === 'visual_rules_review') {
    if (visualRulesReview.value.error) return visualRulesReview.value.error
    const payload = visualRulesReview.value.value
    if (!payload) return '全剧影视规则审核证据不可用。'
    if (payload.scriptRevisionId !== currentRun.value?.scriptRevision) return '候选对应的 ScriptRevision 与本次运行不一致。'
    for (const definition of visualRuleDefinitions) {
      if (!visualRulesReviewed.value[definition.key]) return `请核对“${definition.label}”的当前值、候选与最终值（${visualRulesReviewedCount.value}/3）。`
      const fieldError = visualRuleError(definition.key)
      if (fieldError) return `${definition.label}：${fieldError}`
    }
  }
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

const resetProjectScopedState = (nextProjectId: string) => {
  activeProjectScope = nextProjectId
  scriptReadSequence += 1
  scriptActionSequence += 1
  revisionLoadSequence += 1
  runRefreshSequence += 1
  reviewLoadSequence += 1
  uncertainLoadSequence += 1
  storyboardAssetLoadSequence += 1
  bibleHistoryLoadSequence += 1
  sources.value = []
  revisions.value = []
  scriptAssets.value = []
  selectedScriptAssetId.value = ''
  previewRevision.value = null
  selectedRevisionId.value = ''
  scriptText.value = ''
  scriptFile.value = null
  runs.value = []
  currentRun.value = null
  selectedEpisodeIds.value = []
  uniformWorkflowRouteIndex.value = null
  shotWorkflowRouteIndexes.value = {}
  episodeWorkflowRouteIndexes.value = {}
  uniformPromptProfileBindingIds.value = {}
  shotPromptProfileBindingIds.value = {}
  bibleImagePromptProfileBindingId.value = ''
  unattendedConfirmed.value = false
  unattendedReferenceInputs.value = []
  managedReferenceVideoAssets.value = []
  managedReferenceVideoAssetsError.value = ''
  visualRulesEditSignature.value = ''
  visualRulesEdits.value = { negativePrompt: '', colorScript: '', continuitySummary: '' }
  visualRulesReviewed.value = { negativePrompt: false, colorScript: false, continuitySummary: false }
  reviewRevision.value = null
  expandedStageId.value = ''
  pendingRunConfirmation.value = null
  pendingBibleRegeneration.value = null
  pendingBibleRegenerationError.value = ''
  actionBusy.value = ''
  idempotencyKey.value = crypto.randomUUID()
}
const readScripts = async (expectedProjectId = projectId.value) => {
  const sequence = ++scriptReadSequence
  const sourceRows = await platformApi.listScriptSources(expectedProjectId)
  const details = await Promise.all(sourceRows.filter((item) => item.latestRevisionId).map((item) => platformApi.getScriptRevision(item.latestRevisionId)))
  if (sequence !== scriptReadSequence || projectId.value !== expectedProjectId || activeProjectScope !== expectedProjectId) return false
  sources.value = sourceRows
  revisions.value = details.sort((a, b) => b.revision - a.revision)
  if (!revisions.value.some((item) => item.id === selectedRevisionId.value)) {
    selectedRevisionId.value = revisions.value.find((item) => item.status === 'confirmed')?.id || revisions.value[0]?.id || ''
    if (previewRevision.value?.id !== selectedRevisionId.value) previewRevision.value = null
  }
  return true
}
const loadStoryboardApprovedAssets = async () => {
  const sequence = ++storyboardAssetLoadSequence
  storyboardAssetsError.value = ''
  if (currentStage.value?.stageKey !== 'storyboard_review' || storyboardReview.value.error) return
  storyboardAssetsLoading.value = true
  try {
    await studio.refreshAssets()
    if (sequence !== storyboardAssetLoadSequence || currentStage.value?.stageKey !== 'storyboard_review') return
  } catch (cause) {
    if (sequence === storyboardAssetLoadSequence) storyboardAssetsError.value = `已批准参考图读取失败：${message(cause)}`
  } finally {
    if (sequence === storyboardAssetLoadSequence) storyboardAssetsLoading.value = false
  }
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
const loadRecipeMediaRouteOptions = async () => {
  const sequence = ++recipeMediaRouteLoadSequence
  const recipe = selectedRecipe.value
  recipeMediaRoutes.value = []
  recipeBiblePromptProfileRequirement.value = null
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
    recipeBiblePromptProfileRequirement.value = catalog.bibleImageGeneration
  } catch (cause) {
    if (sequence === recipeMediaRouteLoadSequence) recipeMediaRoutesError.value = message(cause)
  } finally {
    if (sequence === recipeMediaRouteLoadSequence) recipeMediaRoutesLoading.value = false
  }
}
const load = async () => {
  const expectedProjectId = projectId.value
  const expectedRunId = runId.value
  if (activeProjectScope !== expectedProjectId) resetProjectScopedState(expectedProjectId)
  const sequence = ++productionLoadSequence
  runRefreshSequence += 1
  const isCurrent = () => sequence === productionLoadSequence
    && projectId.value === expectedProjectId
    && runId.value === expectedRunId
    && activeProjectScope === expectedProjectId
  loading.value = true
  error.value = ''
  notice.value = ''
  currentRun.value = null
  productionCapability.value = null
  productionCapabilityError.value = ''
  try {
    await studio.initialize()
    await studio.loadProject(expectedProjectId)
    if (!isCurrent()) return
    if (!selectedEpisodeIds.value.length) selectedEpisodeIds.value = projectEpisodes.value.map((item) => item.id)
    const managedReferenceVideoRequest = repository.getAssets({ type: 'video' })
      .then((assets) => ({ assets, error: '' }))
      .catch((cause) => ({ assets: [] as Asset[], error: `受管参考视频读取失败：${message(cause)}` }))
    const capabilityRequest = repository.getProductionCapability(expectedProjectId)
      .then((value) => ({ value, error: '' }))
      .catch((cause) => ({ value: null, error: message(cause) }))
    const [recipeRows, runRows, libraryScripts, managedVideos, capability] = await Promise.all([
      platformApi.listRecipes('published'),
      platformApi.listProductionRuns(expectedProjectId),
      repository.getAssets({ type: 'script' }),
      managedReferenceVideoRequest,
      capabilityRequest,
    ])
    if (!isCurrent()) return
    recipes.value = recipeRows
    runs.value = runRows
    scriptAssets.value = libraryScripts.filter((asset) => asset.isActive === true && asset.backendKind === 'script')
    managedReferenceVideoAssets.value = managedVideos.assets
    managedReferenceVideoAssetsError.value = managedVideos.error
    productionCapability.value = capability.value
    productionCapabilityError.value = capability.error
    if (!expectedRunId && capability.value) {
      episodeCountHint.value = capability.value.creationDefaults.episodeCount
      shotsPerEpisode.value = capability.value.creationDefaults.shotsPerEpisode
      episodeDurationSeconds.value = capability.value.creationDefaults.episodeDurationSeconds
      maxGenerationTasks.value = capability.value.creationDefaults.maxGenerationTasks
    }
    const restoredRunId = productionRunRestoreTarget(runRows, expectedRunId, route.query.view)
    if (restoredRunId) {
      await router.replace({ name: 'production-run-detail', params: { projectId: expectedProjectId, runId: restoredRunId } })
      return
    }
    if (!recipeRows.some((item) => item.id === selectedRecipeId.value)) selectedRecipeId.value = recipeRows[0]?.id || ''
    await readScripts(expectedProjectId)
    if (!isCurrent()) return
    if (expectedRunId) {
      const loadedRun = await platformApi.getProductionRun(expectedRunId)
      if (!isCurrent()) return
      if (!productionRunBelongsToProject(loadedRun, expectedProjectId)) throw new Error('该 ProductionRun 不属于当前项目，已拒绝显示或操作。')
      currentRun.value = loadedRun
    } else currentRun.value = null
  } catch (cause) {
    if (isCurrent()) error.value = message(cause)
  } finally {
    if (isCurrent()) loading.value = false
  }
}
const refreshRun = async () => {
  const expectedRunId = runId.value
  const expectedProjectId = projectId.value
  if (!expectedRunId) return
  const sequence = ++runRefreshSequence
  const isCurrent = () => sequence === runRefreshSequence
    && runId.value === expectedRunId
    && projectId.value === expectedProjectId
    && activeProjectScope === expectedProjectId
  try {
    const refreshed = await platformApi.getProductionRun(expectedRunId)
    if (!isCurrent()) return
    if (!productionRunBelongsToProject(refreshed, expectedProjectId)) throw new Error('该 ProductionRun 不属于当前项目，已拒绝显示或操作。')
    currentRun.value = refreshed
  } catch (cause) { if (isCurrent()) { currentRun.value = null; error.value = message(cause) } }
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
const loadNormalizationAttempts = async () => {
  const sequence = ++normalizationAttemptLoadSequence
  const normalizeStage = currentRun.value?.stages?.find((stage) => stage.stageKey === 'script_normalize')
  const stageId = normalizeStage?.id || ''
  normalizationAttempts.value = []
  normalizationAttemptsError.value = ''
  if (!normalizationHumanFallback.value || currentStage.value?.stageKey !== 'script_normalize_review' || !stageId) return
  normalizationAttemptsLoading.value = true
  try {
    const rows = await platformApi.listGenerationAttempts({ stage_run: stageId })
    if (sequence !== normalizationAttemptLoadSequence
      || currentRun.value?.stages?.find((stage) => stage.stageKey === 'script_normalize')?.id !== stageId) return
    normalizationAttempts.value = rows.filter((attempt) => attempt.operation === 'prompt.normalize')
    if (!normalizationAttempts.value.length) {
      normalizationAttemptsError.value = '服务端没有返回提示词标准化 attempt 的安全投影；修复链审计不可用。'
    }
  } catch (cause) {
    if (sequence === normalizationAttemptLoadSequence) normalizationAttemptsError.value = message(cause)
  } finally {
    if (sequence === normalizationAttemptLoadSequence) normalizationAttemptsLoading.value = false
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
watch(selectedRecipeId, () => {
  unattendedConfirmed.value = false
  unattendedReferenceInputs.value = []
  void loadRecipeMediaRouteOptions()
}, { immediate: true })
watch([
  uniformWorkflowRouteIndex,
  () => unattendedReferenceVideoInput.value.minimumItems,
  () => unattendedReferenceVideoInput.value.maximumItems,
  () => unattendedReferenceTopology.value?.videos.defaultPurposeKey || '',
], () => {
  unattendedReferenceInputs.value = createUnattendedReferenceInputDrafts(
    unattendedReferenceVideoInput.value,
    unattendedReferenceTopology.value?.videos.defaultPurposeKey || '',
  )
})
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
  uniformPromptProfileBindingIds.value = Object.fromEntries(
    Object.entries(uniformPromptProfileBindingIds.value).filter(([key, bindingId]) => (
      uniformPromptProfileRequirements.value.some((item) => (
        promptContractKey(item.operation, item.mode) === key
        && item.choices.some((choice) => choice.bindingId === bindingId)
      ))
    )),
  )
  shotPromptProfileBindingIds.value = Object.fromEntries(
    Object.entries(shotPromptProfileBindingIds.value).filter(([shotId, bindingId]) => {
      const row = workflowShotRows.value.find(({ shot }) => shot.id === shotId)
      const routeOption = recipeMediaRoutes.value.find((item) => item.routeIndex === shotWorkflowRouteIndexes.value[shotId])
      return Boolean(row && routeOption && promptRequirementForShot(routeOption, row.shot)?.choices.some((choice) => choice.bindingId === bindingId))
    }),
  )
  if (!recipeBiblePromptProfileRequirement.value?.choices.some((choice) => choice.bindingId === bibleImagePromptProfileBindingId.value)) bibleImagePromptProfileBindingId.value = ''
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
watch(() => `${currentRun.value?.id || ''}:${currentStage.value?.id || ''}:${normalizationReview.value.value?.snapshotSha256 || ''}`, () => { void loadNormalizationAttempts() }, { immediate: true })
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
watch(() => currentStage.value?.stageKey === 'visual_rules_review' && !visualRulesReview.value.error
  ? `${currentRun.value?.id}:${visualRulesReview.value.value?.snapshotSha256}:${visualRulesReview.value.value?.candidateSha256}`
  : '', (signature) => {
  if (signature && signature === visualRulesEditSignature.value) return
  visualRulesEditSignature.value = signature
  visualRulesEdits.value = { negativePrompt: '', colorScript: '', continuitySummary: '' }
  visualRulesReviewed.value = { negativePrompt: false, colorScript: false, continuitySummary: false }
  const candidate = visualRulesReview.value.value?.candidate
  if (!signature || !candidate) return
  visualRulesEdits.value = { ...candidate }
}, { immediate: true })
watch(() => currentStage.value?.stageKey === 'bible_image_review' && !bibleImageReview.value.error
  ? `${currentRun.value?.id}:${bibleImageReview.value.value?.snapshotSha256}`
  : '', (signature) => {
  if (signature && signature === bibleImageSelectionSignature.value) return
  bibleImageSelectionSignature.value = signature
  bibleImageSelections.value = {}
  bibleImageHistoryByArtifactId.value = {}
  bibleImageHistoryError.value = ''
  bibleLibraryExpanded.value = {}
  bibleLibrarySearches.value = {}
  bibleLibraryLimits.value = {}
  if (!signature || !bibleImageReview.value.value) return
  bibleImageSelections.value = Object.fromEntries(
    bibleImageReview.value.value.entities.map((entity) => [
      bibleEntityKey(entity.entityType, entity.entityId),
      { selectedArtifactIds: [], selectedAssetIds: [], anchorArtifactId: '', anchorAssetId: '' },
    ]),
  )
  void loadBibleImageHistory()
}, { immediate: true })
watch(() => currentStage.value?.stageKey === 'storyboard_review' && !storyboardReview.value.error
  ? `${currentRun.value?.id}:${storyboardReview.value.value?.snapshotSha256}:${storyboardReview.value.value?.approvedBibleAssets.map((item) => item.assetId).join(',')}`
  : '', (signature) => {
  if (signature && signature === storyboardReferenceSignature.value) return
  storyboardReferenceSignature.value = signature
  storyboardReferenceDrafts.value = {}
  storyboardAssetsError.value = ''
  if (!signature || !storyboardReview.value.value) return
  storyboardReferenceDrafts.value = Object.fromEntries(
    storyboardReview.value.value.episodes.flatMap((episode) => episode.shots).map((shot) => [
      shot.id,
      hydrateStoryboardReferenceDraft(shot.referenceBindings),
    ]),
  )
  void loadStoryboardApprovedAssets()
}, { immediate: true })
watch(() => `${currentRun.value?.id || ''}:${currentStage.value?.id || ''}`, () => {
  approvalDecision.value = 'approve'
  approvalComment.value = ''
  adoptGeneratedMetadata.value = false
})
watch(() => [projectId.value, runId.value], () => { void load() })
onMounted(load)
onBeforeUnmount(() => {
  productionLoadSequence += 1
  scriptReadSequence += 1
  scriptActionSequence += 1
  revisionLoadSequence += 1
  runRefreshSequence += 1
  if (pollTimer) window.clearInterval(pollTimer)
})

const chooseFile = (event: Event) => { scriptFile.value = (event.target as HTMLInputElement).files?.[0] || null; previewRevision.value = null }
const createScriptPreview = async () => {
  const expectedProjectId = projectId.value
  const sequence = ++scriptActionSequence
  const isCurrent = () => sequence === scriptActionSequence && projectId.value === expectedProjectId && activeProjectScope === expectedProjectId
  actionBusy.value = 'script'
  error.value = ''
  notice.value = ''
  try {
    const result = scriptMode.value === 'file' && scriptFile.value
      ? await platformApi.uploadScript(expectedProjectId, scriptFile.value)
      : await platformApi.createPastedScript(expectedProjectId, scriptText.value, scriptFormat.value)
    if (!isCurrent()) return
    previewRevision.value = result.revision
    selectedRevisionId.value = result.revision.id
    revisions.value = [result.revision, ...revisions.value.filter((item) => item.id !== result.revision.id)]
    notice.value = '服务端已生成预览 revision。请核对正文后再确认。'
    await readScripts(expectedProjectId)
  } catch (cause) { if (isCurrent()) error.value = message(cause) } finally { if (isCurrent() && actionBusy.value === 'script') actionBusy.value = '' }
}
const importLibraryScript = async () => {
  const assetId = selectedScriptAssetId.value
  if (!assetId) return
  const expectedProjectId = projectId.value
  const sequence = ++scriptActionSequence
  const isCurrent = () => sequence === scriptActionSequence && projectId.value === expectedProjectId && activeProjectScope === expectedProjectId
  actionBusy.value = 'script-asset'; error.value = ''; notice.value = ''
  try {
    const result = await platformApi.importScriptAsset(expectedProjectId, assetId)
    if (!isCurrent()) return
    previewRevision.value = result.revision
    selectedRevisionId.value = result.revision.id
    revisions.value = [result.revision, ...revisions.value.filter((item) => item.id !== result.revision.id)]
    notice.value = '剧本素材已复制为当前项目的 preview revision；仍需核对并人工确认后才能生产。'
    await readScripts(expectedProjectId)
  } catch (cause) { if (isCurrent()) error.value = message(cause) } finally { if (isCurrent() && actionBusy.value === 'script-asset') actionBusy.value = '' }
}
const importProjectMasterScript = async () => {
  if (!projectMasterScript.value) return
  const expectedProjectId = projectId.value
  const sequence = ++scriptActionSequence
  const isCurrent = () => sequence === scriptActionSequence && projectId.value === expectedProjectId && activeProjectScope === expectedProjectId
  actionBusy.value = 'project-script'
  error.value = ''
  notice.value = ''
  try {
    const result = await platformApi.createPastedScript(projectId.value, projectMasterScript.value, 'plain')
    if (!isCurrent()) return
    previewRevision.value = result.revision
    selectedRevisionId.value = result.revision.id
    revisions.value = [result.revision, ...revisions.value.filter((item) => item.id !== result.revision.id)]
    notice.value = '已把当前项目母稿复制为服务端 preview revision；这不是新的 AI 生成结果，请核对并确认。'
    await readScripts(expectedProjectId)
  } catch (cause) { if (isCurrent()) error.value = message(cause) } finally { if (isCurrent() && actionBusy.value === 'project-script') actionBusy.value = '' }
}
const selectExistingRevision = async () => {
  const revisionId = selectedRevisionId.value
  const expectedProjectId = projectId.value
  const sequence = ++revisionLoadSequence
  if (!revisionId) { previewRevision.value = null; return }
  actionBusy.value = 'revision'
  error.value = ''
  try {
    const revision = await platformApi.getScriptRevision(revisionId)
    if (sequence !== revisionLoadSequence || projectId.value !== expectedProjectId || activeProjectScope !== expectedProjectId || selectedRevisionId.value !== revisionId) return
    previewRevision.value = revision
  } catch (cause) {
    if (sequence === revisionLoadSequence && projectId.value === expectedProjectId && selectedRevisionId.value === revisionId) error.value = message(cause)
  } finally {
    if (sequence === revisionLoadSequence && actionBusy.value === 'revision') actionBusy.value = ''
  }
}
const confirmRevision = async () => {
  const revisionId = selectedRevisionId.value
  if (!revisionId || previewRevision.value?.id !== revisionId) {
    error.value = '当前剧本预览与所选 revision 不一致，请重新选择并读取完整正文后再确认。'
    return
  }
  const expectedProjectId = projectId.value
  const sequence = ++revisionLoadSequence
  actionBusy.value = 'confirm'
  error.value = ''
  try {
    const confirmed = await platformApi.confirmScriptRevision(revisionId)
    if (sequence !== revisionLoadSequence || projectId.value !== expectedProjectId || activeProjectScope !== expectedProjectId || selectedRevisionId.value !== revisionId) return
    previewRevision.value = confirmed
    revisions.value = revisions.value.map((item) => item.id === confirmed.id ? confirmed : item)
    notice.value = '服务端已确认该剧本 revision，可用于 ProductionRun。'
  } catch (cause) {
    if (sequence === revisionLoadSequence && projectId.value === expectedProjectId && selectedRevisionId.value === revisionId) error.value = message(cause)
  } finally {
    if (sequence === revisionLoadSequence && actionBusy.value === 'confirm') actionBusy.value = ''
  }
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
    if (!productionCapability.value) throw new Error(productionCapabilityError.value || '服务端有效安全上限不可用，已停止创建生产运行。')
    const plan = aiRunPlan.value
    if (mode === 'ai' && !plan) throw new Error('服务端项目创建策略未提供有效的集数、每集镜数或单集时长，不能创建生产运行。')
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
      episodeCount: plan?.episodeCount,
      shotsPerEpisode: plan?.shotsPerEpisode,
      episodeDurationSeconds: plan?.episodeDurationSeconds,
      episodeIds: selectedEpisodeIds.value,
      workflowSelection: !recipeHasMediaGeneration.value
        ? null
        : workflowSelectionMode.value === 'uniform'
        ? {
            schemaVersion: 2,
            mode: 'uniform',
            routeIndex: uniformWorkflowRouteIndex.value as number,
            promptProfileSelections: uniformPromptProfileRequirements.value.map((item) => ({
              operation: item.operation,
              mode: item.mode,
              promptProfileBindingId: uniformPromptProfileBindingIds.value[promptContractKey(item.operation, item.mode)] || '',
            })),
          }
        : {
            schemaVersion: 2,
            mode: 'per_shot',
            shotRoutes: workflowShotRows.value.map(({ shot }) => ({
              shotId: shot.id,
              routeIndex: shotWorkflowRouteIndexes.value[shot.id] as number,
              promptProfileBindingId: shotPromptProfileBindingIds.value[shot.id] || '',
            })),
          },
      bibleImagePromptProfileBindingId: recipeHasBibleImageGeneration.value
        ? bibleImagePromptProfileBindingId.value
        : null,
      unattendedConfirmed: isUnattendedRecipe.value ? unattendedConfirmed.value : undefined,
      referenceInputs: isUnattendedRecipe.value
        ? selectedUnattendedReferences.value
        : undefined,
      effectiveLimits: productionCapability.value.effectiveLimits,
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
  const candidateOverrides = approvalDecision.value !== 'approve'
    ? undefined
    : stage.stageKey === 'script_normalize_review'
      ? Object.fromEntries((normalizationReview.value.value?.candidates || []).map((candidate) => [candidate.requestId, normalizationEdits.value[candidate.requestId] || '']))
      : stage.stageKey === 'visual_rules_review'
        ? {
            negative_prompt: visualRulesEdits.value.negativePrompt,
            color_script: visualRulesEdits.value.colorScript,
            continuity_summary: visualRulesEdits.value.continuitySummary,
          }
        : undefined
  const expectedSourceSnapshotSha256 = stage.stageKey === 'storyboard_review' && approvalDecision.value === 'approve'
    ? storyboardReview.value.value?.snapshotSha256
    : stage.stageKey === 'script_normalize_review' && approvalDecision.value === 'approve'
      ? normalizationReview.value.value?.snapshotSha256
      : stage.stageKey === 'visual_rules_review' && approvalDecision.value === 'approve'
        ? visualRulesReview.value.value?.snapshotSha256
        : stage.stageKey === 'bible_image_review' && approvalDecision.value === 'approve'
          ? bibleImageReview.value.value?.snapshotSha256
          : undefined
  const bibleImageBindings = stage.stageKey === 'bible_image_review' && approvalDecision.value === 'approve'
    ? (bibleImageReview.value.value?.entities || []).map((entity) => {
        const selection = bibleSelectionFor(entity.entityType, entity.entityId)
        return {
          entityType: entity.entityType,
          entityId: entity.entityId,
          selectedArtifactIds: selection.selectedArtifactIds,
          selectedLibraryAssets: selection.selectedAssetIds.map((assetId) => ({
            assetId,
            expectedSha256: bibleLibraryAsset(assetId)?.checksum || '',
          })),
          anchorArtifactId: selection.anchorArtifactId || undefined,
          anchorLibraryAssetId: selection.anchorAssetId || undefined,
        }
      })
    : undefined
  const shotReferenceBindings = stage.stageKey === 'storyboard_review' && approvalDecision.value === 'approve'
    ? storyboardShotReferenceBindingsPayload.value
    : undefined
  void runAction('approve', () => platformApi.approveProductionRun(run.id, {
    stageKey: stage.stageKey,
    decision: approvalDecision.value,
    comment: approvalComment.value,
    candidateOverrides,
    expectedSourceSnapshotSha256,
    bibleImageBindings,
    shotReferenceBindings,
    adoptGeneratedMetadata: (
      stage.stageKey === 'script_review' && approvalDecision.value === 'approve'
        ? adoptGeneratedMetadata.value
        : undefined
    ),
  }))
}
const continueRun = () => { if (currentRun.value) void runAction('continue', () => platformApi.continueProductionRun(currentRun.value!.id)) }
const requestBibleRegeneration = (entity: { entityType: 'character' | 'scene' | 'prop'; entityId: string; entityName: string; candidates: unknown[] }) => {
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
  notice.value = `已提交“${pending.entityName}”的 Bible 候选重新生成；旧候选不会删除，生成完成后会与新候选一起进入审核。`
}
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
    <header class="platform-page-header"><div><button class="platform-back" @click="router.push(`/projects/${projectId}`)"><ArrowLeft :size="14"/>返回项目</button><span class="eyebrow">项目生产工作台 / PROJECT PRODUCTION WORKSPACE</span><h2>{{ project?.title || '项目' }} · 一键生产</h2><p>脚本确认、配方选择、阶段审批、生成重试与版本选片都走生产运行（ProductionRun）服务端状态机。</p></div><button class="button ghost small" :disabled="loading" @click="load"><RefreshCw :size="14" :class="{spin:loading}"/>重新读取</button></header>
    <div v-if="error" class="platform-alert danger"><AlertTriangle :size="17"/><div><strong>操作未完成</strong><p>{{ error }}</p></div></div><div v-if="notice" class="platform-alert success"><Check :size="17"/><p>{{ notice }}</p></div><div v-if="loading" class="platform-loading"><LoaderCircle class="spin" :size="22"/>正在读取项目生产状态…</div>

    <template v-else-if="!currentRun">
      <div v-if="actionableRuns.length>1" class="platform-alert warning"><AlertTriangle :size="17"/><div><strong>存在 {{ actionableRuns.length }} 条可继续运行</strong><p>系统不会擅自选择。请从下方历史列表进入明确的 ProductionRun，或确认后再创建独立新运行。</p></div></div>
      <div v-if="productionCapabilityError" class="platform-alert danger"><AlertTriangle :size="17"/><div><strong>生产配置不可用</strong><p>{{ productionCapabilityError }}。服务端有效限制与创建策略恢复前，不能创建新 ProductionRun。</p></div></div>
      <div class="production-setup-grid">
        <section v-if="isAiScriptRecipe" class="platform-card production-step"><header><span>01</span><div><strong>AI 生成剧本</strong><small>模型只生成预览版本（preview）；剧本审核阶段（script_review）批准后才确认并进入一致性设定集（Bible）。</small></div></header><div v-if="projectEpisodes.length" class="platform-alert danger production-inline-alert"><AlertTriangle :size="15"/><p>项目已有 {{ projectEpisodes.length }} 集正式剧集（Episode），AI 剧本配方已禁用，避免覆盖已有下游结构。请选择下游生产配方（downstream recipe）。</p></div><label class="platform-field"><span>故事想法</span><textarea v-model="aiIdea" rows="8" placeholder="描述核心冲突、人物与连续剧走向…"></textarea></label><label class="platform-field"><span>风格提示</span><textarea v-model="aiStyleHint" rows="3" placeholder="可选：视觉、节奏、受众等"></textarea></label><div class="platform-form-grid"><label class="platform-field"><span>集数</span><input v-model.number="episodeCountHint" type="number" min="1"/></label><label class="platform-field"><span>每集镜数</span><input v-model.number="shotsPerEpisode" type="number" min="1"/></label><label class="platform-field"><span>单集时长（秒）</span><input v-model.number="episodeDurationSeconds" type="number" min="1"/></label></div><small v-if="aiPlannedAverageShotDurationMs" class="production-duration-hint">计划平均每镜 {{ aiPlannedAverageShotDurationMs }}ms；创建时服务端会与所选文本模型的镜头时长策略求交，并把结果冻结到本次运行。</small></section>
        <template v-else-if="isConfirmedScriptRecipe">
        <section class="platform-card production-step"><header><span>01</span><div><strong>导入或选择剧本</strong><small>项目母稿、上传、粘贴或素材库剧本都只生成预览版本（preview revision），确认后才可生产。</small></div></header><div v-if="projectEpisodes.length" class="platform-alert warning production-inline-alert"><AlertTriangle :size="15"/><p>项目已有 {{ projectEpisodes.length }} 集正式剧集（Episode）；仍可从已确认版本（revision）创建新运行。服务端会按本次配方显式处理，现有正式对象不会被页面静默覆盖。</p></div><button v-if="projectMasterScript" class="button ghost small" :disabled="Boolean(actionBusy)" @click="importProjectMasterScript"><FileText :size="13"/>{{ actionBusy==='project-script'?'导入中…':'导入当前项目母稿' }}</button><div class="segmented"><button :class="{active:scriptMode==='paste'}" @click="scriptMode='paste'">粘贴</button><button :class="{active:scriptMode==='file'}" @click="scriptMode='file'">上传文件</button><button :class="{active:scriptMode==='library'}" @click="scriptMode='library'">素材库剧本</button><button :class="{active:scriptMode==='existing'}" @click="scriptMode='existing'">已有版本</button></div><template v-if="scriptMode==='paste'"><label class="platform-field"><span>格式</span><select v-model="scriptFormat"><option value="plain">纯文本</option><option value="markdown">Markdown</option><option value="fountain">Fountain</option></select></label><label class="platform-field"><span>剧本文本</span><textarea v-model="scriptText" rows="10" placeholder="粘贴完整剧本…"></textarea></label><button class="button primary small" :disabled="!scriptText.trim()||Boolean(actionBusy)" @click="createScriptPreview"><FileText :size="13"/>{{ actionBusy==='script'?'生成预览中…':'生成服务端预览' }}</button></template><template v-else-if="scriptMode==='file'"><label class="script-drop"><Upload :size="22"/><strong>{{ scriptFile?.name || '选择 txt / md / fountain / docx' }}</strong><small>PDF 当前由服务端明确拒绝，不会静默提取。</small><input type="file" accept=".txt,.md,.markdown,.fountain,.docx,text/plain,text/markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document" @change="chooseFile"/></label><button class="button primary small" :disabled="!scriptFile||Boolean(actionBusy)" @click="createScriptPreview">{{ actionBusy==='script'?'上传解析中…':'上传并生成预览' }}</button></template><template v-else-if="scriptMode==='library'"><label class="platform-field"><span>素材库剧本</span><select v-model="selectedScriptAssetId" :disabled="Boolean(actionBusy)"><option value="">请选择</option><option v-for="asset in scriptAssets" :key="asset.id" :value="asset.id">{{ asset.name }} · {{ asset.checksum?.slice(0,10) }}</option></select><small>导入只会创建项目级预览版本（preview revision），不会覆盖既有确认版本。</small></label><button class="button primary small" :disabled="!selectedScriptAssetId||Boolean(actionBusy)" @click="importLibraryScript"><FileText :size="13"/>{{ actionBusy==='script-asset'?'导入中…':'导入并生成预览' }}</button></template><template v-else><label class="platform-field"><span>已有最新版本（latest revision）</span><select v-model="selectedRevisionId" :disabled="Boolean(actionBusy)" @change="selectExistingRevision"><option value="">请选择</option><option v-for="item in revisions" :key="item.id" :value="item.id">版本（revision）{{ item.revision }} · {{ presentPlatformStatus(item.status).label }} · {{ item.contentSha256.slice(0,10) }}</option></select></label></template>
          <div v-if="previewRevision" class="script-preview"><header><div><strong>Revision {{ previewRevision.revision }}</strong><small>{{ previewRevision.contentSizeBytes }} 字节 · {{ previewRevision.contentSha256 }}</small></div><PlatformStatusPill :status="previewRevision.status"/></header><pre>{{ previewRevision.content || '服务端列表响应不含正文；请重新选择以读取详情。' }}</pre><button v-if="previewRevision.status!=='confirmed'" class="button primary small" :disabled="Boolean(actionBusy)||previewRevision.id!==selectedRevisionId" @click="confirmRevision"><Check :size="13"/>{{ actionBusy==='confirm'?'确认中…':'确认该剧本版本' }}</button></div>
        </section>
        </template>
        <section v-else-if="isDownstreamRecipe" class="platform-card production-step"><header><span>01</span><div><strong>沿用现有分集</strong><small>下游生产配方（downstream recipe）从已有正式剧集（Episode） 开始，不接收剧本版本（revision） 或 AI 剧本输入。</small></div></header><div v-if="projectEpisodes.length" class="platform-alert success production-inline-alert"><Check :size="15"/><p>已发现 {{ projectEpisodes.length }} 集正式剧集（Episode），可直接选择生产范围。</p></div><div v-else class="platform-alert danger production-inline-alert"><AlertTriangle :size="15"/><p>当前项目没有 Episode，下游生产配方（downstream recipe）不能创建。请先用 AI 或已确认版本配方（confirmed revision recipe）建立正式分集。</p></div></section>
        <section v-else class="platform-card production-step"><header><span>01</span><div><strong>配方剧本模式无效</strong><small>配方必须且只能声明 AI、已确认版本（confirmed revision）或剧本输入/人工下游（script_input/manual downstream）模式。</small></div></header><div class="platform-alert danger production-inline-alert"><AlertTriangle :size="15"/><p>当前配方为空、包含未知剧本模式或同时声明了冲突模式，已禁止创建。</p></div></section>
        <section class="platform-card production-step"><header><span>02</span><div><strong>选择配方与运行边界</strong><small>所有空值交给服务端最终生效配置（effective configuration）解析。</small></div></header><label class="platform-field"><span>已发布配方（Published Recipe）</span><select v-model="selectedRecipeId"><option value="">请选择</option><option v-for="item in recipes" :key="item.id" :value="item.id">{{ item.name || item.recipeKey }} · v{{ item.version }}</option></select></label><details v-if="isDownstreamRecipe&&projectEpisodes.length" class="production-overrides"><summary>生产选集 · {{ selectedEpisodeIds.length || '全部' }} / {{ projectEpisodes.length }}<ChevronDown :size="13"/></summary><label v-for="episode in projectEpisodes" :key="episode.id" class="platform-field"><span>第 {{ episode.number }} 集 · {{ episode.title }}</span><input v-model="selectedEpisodeIds" type="checkbox" :value="episode.id"/></label><small>不勾选任何集等同全部；服务端创建时会去重、排序并冻结。</small></details><div v-if="recipeHasMediaGeneration" class="workflow-selection-panel"><div class="workflow-selection-heading"><div><strong>镜头工作流分配</strong><small>只可选择所选已发布配方（published recipe）的媒体路由（route）；创建时服务端复验输出类型、条件模式、发布状态、当前版本（current）、烟测（smoke）与部署绑定。</small></div><div class="segmented"><button type="button" :class="{active:workflowSelectionMode==='uniform'}" @click="workflowSelectionMode='uniform'">整集统一</button><button type="button" :class="{active:workflowSelectionMode==='per_shot'}" :disabled="!workflowShotRows.length" @click="workflowSelectionMode='per_shot'">逐镜头</button></div></div><label v-if="workflowSelectionMode==='uniform'" class="platform-field"><span>整集统一工作流 *</span><select v-model.number="uniformWorkflowRouteIndex" :disabled="!recipeMediaRoutes.length"><option :value="null">请选择</option><option v-for="option in recipeMediaRoutes" :key="option.routeIndex" :value="option.routeIndex" :disabled="!routeSupportsUniform(option)">{{ routeLabel(option) }}</option></select><small>AI/导入在镜头（Shot）尚未物化时使用此模式；生成后的每个镜头会固定使用该路由（route）。</small></label><div v-else class="workflow-shot-table"><article v-for="episode in workflowEpisodes" :key="episode.id" class="workflow-episode-group"><header><div><strong>第 {{ episode.number }} 集 · {{ episode.title }}</strong><small>{{ episode.shots.length }} 个镜头</small></div><div class="workflow-episode-apply"><select v-model.number="episodeWorkflowRouteIndexes[episode.id]"><option :value="null">选择本集路由（route）</option><option v-for="option in recipeMediaRoutes" :key="option.routeIndex" :value="option.routeIndex" :disabled="!routeSupportsEpisode(option,episode)">{{ routeLabel(option) }}</option></select><button type="button" class="button ghost tiny" :disabled="!episodeRouteCanApply(episode)" @click="applyEpisodeWorkflow(episode.id)">应用到本集</button></div></header><label v-for="shot in episode.shots" :key="shot.id" class="workflow-shot-row"><span><strong>S{{ shot.order }} · {{ shot.title || '未命名镜头' }}</strong><small>{{ shot.mediaType }} · {{ shot.mode }}</small></span><select v-model.number="shotWorkflowRouteIndexes[shot.id]"><option :value="null">请选择工作流</option><option v-for="option in recipeMediaRoutes" :key="option.routeIndex" :value="option.routeIndex" :disabled="!routeSupportsShot(option,shot)">{{ routeLabel(option) }}</option></select></label></article></div><div v-if="workflowSelectionError" class="platform-alert warning production-inline-alert" role="alert"><AlertTriangle :size="15"/><p>{{ workflowSelectionError }}</p></div><details v-if="recipeMediaRoutes.some((item)=>!item.isSelectable)" class="workflow-route-gaps"><summary>查看路由（route）依赖缺口</summary><p v-for="item in recipeMediaRoutes.filter((route)=>!route.isSelectable)" :key="item.routeIndex"><strong>路由（Route）{{ item.routeIndex }}</strong> · {{ item.reasons.join('；') }}</p></details></div><label class="platform-field"><span>任务上限（留空继承）</span><input v-model.number="maxGenerationTasks" type="number" min="1" placeholder="继承系统生效值（system effective）"/></label><label class="platform-field"><span>外部模型调用</span><select v-model="allowExternalCalls"><option :value="null">继承系统配置</option><option :value="false">禁止</option><option :value="true">允许（必须预算）</option></select></label><label v-if="allowExternalCalls===true" class="platform-field"><span>本次预算上限</span><input v-model="budgetLimit" inputmode="decimal" placeholder="例如 20.0000"/></label><details class="production-overrides"><summary>本次配置覆盖（高级）<ChevronDown :size="13"/></summary><textarea v-model="configOverridesText" rows="8" spellcheck="false"></textarea></details><div class="run-create-summary"><GitBranch :size="17"/><span><strong>{{ isInvalidRecipe ? '配方剧本模式无效，不能创建' : isAiScriptRecipe ? projectEpisodes.length ? '已有 Episode，不能重新生成 AI 剧本' : 'AI 剧本将先生成预览' : isConfirmedScriptRecipe ? selectedRevision?.status==='confirmed' ? projectEpisodes.length ? '已有 Episode；将使用已确认 revision 创建新运行' : '剧本已确认' : '请先确认剧本' : projectEpisodes.length ? '从现有 Episode 继续生产' : '缺少可生产的 Episode' }}</strong><small>幂等键 {{ idempotencyKey }}</small></span></div><button class="button primary production-create" :disabled="!canCreate" @click="createRun"><Play :size="15"/>{{ actionBusy==='create'?'创建并执行中…':'创建生产运行（ProductionRun）' }}</button>
        </section>
        <section v-if="isUnattendedRecipe" class="platform-card production-step unattended-run-panel">
          <header><span title="全自动（Unattended Automation）">全自动</span><div><strong>确认全自动运行</strong><small>自动审核确认始终需要人工勾选；动态参考视频是否必选，由当前选中 WorkflowTemplateVersion 的输入基数合同决定。</small></div></header>
          <div v-if="unattendedReferenceVideoSelectable" class="unattended-reference-editor">
            <div class="unattended-reference-editor-heading"><div><strong>受管动态参考视频 {{ unattendedReferenceVideoRequired ? '*' : '（可选）' }}</strong><small>当前合同允许 {{ unattendedReferenceVideoInput.minimumItems }}–{{ unattendedReferenceVideoInput.maximumItems }} 条；已选择 {{ unattendedReferenceAssetIds.size }} 条。素材须满足 {{ unattendedReferenceVideoDurationLabel }} 及合同 adapter 的真实文件复探。{{ REFERENCE_VIDEO_CONTENT_SAFETY_NOTICE }}</small></div><button type="button" class="button ghost tiny" :disabled="unattendedReferenceInputs.length>=unattendedReferenceVideoInput.maximumItems" @click="addUnattendedReferenceInput"><Plus :size="12"/>添加视频</button></div>
            <p v-if="!unattendedReferenceInputs.length" class="unattended-reference-empty">当前合同允许不选择动态视频；将由 Bible 图片与提示词驱动。</p>
            <article v-for="(item,index) in unattendedReferenceInputs" :key="item.rowKey" class="unattended-reference-row">
              <header><strong>动态视频 {{ index + 1 }}</strong><button type="button" class="button ghost tiny" :disabled="unattendedReferenceInputs.length<=unattendedReferenceVideoInput.minimumItems" @click="removeUnattendedReferenceInput(item.rowKey)"><Trash2 :size="12"/>移除</button></header>
              <label class="platform-field"><span>受管视频 *</span><select v-model="item.assetId" :disabled="!unattendedReferenceVideos.length"><option value="">请选择通过合同校验的视频</option><option v-for="asset in unattendedReferenceVideos" :key="asset.id" :value="asset.id" :disabled="unattendedReferenceAssetDisabled(asset.id,item.rowKey)">{{ unattendedReferenceVideoLabel(asset) }}</option></select></label>
              <label class="platform-field"><span>视频用途 *</span><select v-model="item.purpose" :disabled="!unattendedReferenceVideoPurposes.length"><option value="">工作流未声明默认用途</option><option v-for="purpose in unattendedReferenceVideoPurposes" :key="purpose.key" :value="purpose.key">{{ purpose.label }}（{{ purpose.key }}）</option></select><small>{{ unattendedReferencePurposeDefinition(item.purpose)?.description || '用途名称、说明与默认值来自当前工作流合同。' }}</small></label>
              <label v-if="unattendedReferencePairing(item.purpose)?.allowEmbeddedSource" class="platform-checkbox"><input v-model="item.useEmbeddedAudio" type="checkbox"/><span>使用该视频的内嵌音轨</span></label>
            </article>
          </div>
          <div v-else class="platform-alert success production-inline-alert"><Check :size="15"/><p>当前工作流未声明动态参考视频输入，创建时不要求也不会提交视频素材。</p></div>
          <dl class="unattended-contract-facts"><div><dt>工作流合同</dt><dd>{{ unattendedReferenceVideoRequired ? `动态视频至少 ${unattendedReferenceVideoInput.minimumItems} 条` : unattendedReferenceVideoSelectable ? '动态视频可选' : '无动态视频输入' }}</dd></div><div><dt>参考音轨</dt><dd>按每条视频用途的配对规则显式选择</dd></div><div><dt>审核方式</dt><dd>已发布配方策略（recipe policy）自动决策，异常时阻断</dd></div></dl>
          <label class="platform-checkbox unattended-confirmation"><input v-model="unattendedConfirmed" type="checkbox"/><span>我确认本次运行将从新剧本开始，并由已发布决策策略全程自动审核和继续；策略或证据异常时应阻断，不回退到人工默认批准。</span></label>
          <div v-if="managedReferenceVideoAssetsError&&!unattendedReferenceVideoRequired&&!selectedUnattendedReferences.length" class="platform-alert warning production-inline-alert" role="status"><AlertTriangle :size="15"/><p>可选视频列表读取失败，但当前工作流不要求视频；仍可不带视频继续。{{ managedReferenceVideoAssetsError }}</p></div>
          <div v-if="unattendedCreationError" class="platform-alert warning production-inline-alert" role="alert"><AlertTriangle :size="15"/><p>{{ unattendedCreationError }}</p></div>
          <div v-else class="platform-alert success production-inline-alert"><Check :size="15"/><p>全自动确认与当前工作流输入合同已满足；服务端会按已选 WorkflowTemplateVersion 再次校验并冻结事实。</p></div>
        </section>
        <section v-if="recipeHasMediaGeneration" class="platform-card production-step prompt-profile-selection-panel">
          <header><span title="提示词策略（Prompt Profile）">策略</span><div><strong>显式选择提示词策略（PromptProfile）</strong><small>每个选择按绑定标识（binding ID）、策略版本（Profile）与合同摘要（hash）冻结；优先级（priority）仅作目录信息。</small></div></header>
          <template v-if="workflowSelectionMode==='uniform'">
            <label v-for="requirement in uniformPromptProfileRequirements" :key="promptContractKey(requirement.operation,requirement.mode)" class="platform-field">
              <span>{{ requirement.operation }}{{ requirement.mode ? ` / ${requirement.mode}` : '' }} *</span>
              <select v-model="uniformPromptProfileBindingIds[promptContractKey(requirement.operation,requirement.mode)]">
                <option value="">请显式选择策略</option>
                <option v-for="choice in requirement.choices" :key="choice.bindingId" :value="choice.bindingId">{{ promptProfileChoiceLabel(choice) }}</option>
              </select>
            </label>
          </template>
          <template v-else>
            <label v-for="({shot}) in workflowShotRows" :key="`profile-${shot.id}`" class="platform-field workflow-shot-profile-row">
              <span>S{{ shot.order }} · {{ shot.mediaType }} / {{ shot.mode }}</span>
              <select v-model="shotPromptProfileBindingIds[shot.id]" :disabled="!promptChoicesForShot(shot).length">
                <option value="">请选择 PromptProfile</option>
                <option v-for="choice in promptChoicesForShot(shot)" :key="choice.bindingId" :value="choice.bindingId">{{ promptProfileChoiceLabel(choice) }}</option>
              </select>
            </label>
          </template>
          <label v-if="recipeHasBibleImageGeneration" class="platform-field">
            <span>Bible 参考图 PromptProfile *</span>
            <select v-model="bibleImagePromptProfileBindingId" :disabled="!recipeBiblePromptProfileRequirement?.choices.length">
              <option value="">请显式选择策略</option>
              <option v-for="choice in recipeBiblePromptProfileRequirement?.choices || []" :key="choice.bindingId" :value="choice.bindingId">{{ promptProfileChoiceLabel(choice) }}</option>
            </select>
          </label>
        </section>
      </div>
      <details v-if="runs.length" class="platform-card previous-runs"><summary><span><GitBranch :size="16"/>历史生产运行（ProductionRun）</span><b>{{ runs.length }}</b><ChevronDown :size="14"/></summary><div class="platform-dense-list"><RouterLink v-for="item in runs" :key="item.id" :to="`/projects/${projectId}/production/${item.id}`"><div><strong>{{ item.currentStageKey ? stageLabel(item.currentStageKey) : '已完成全部阶段' }}</strong><small>{{ dateTime(item.createdAt) }} · {{ item.generationTaskCount }}/{{ item.maxGenerationTasks }} 个生成任务</small></div><PlatformStatusPill :status="item.status"/></RouterLink></div></details>
    </template>

    <template v-else>
      <section class="run-command-bar"><div><PlatformStatusPill :status="currentRun.status"/><span><strong>{{ currentRun.currentStageKey ? stageLabel(currentRun.currentStageKey) : '全部阶段完成' }}</strong><small>{{ currentRun.id }} · {{ currentRun.generationTaskCount }}/{{ currentRun.maxGenerationTasks }} 个生成任务</small></span></div><div><button class="button ghost small" @click="router.push({ name: 'production-run', params: { projectId }, query: { view: 'list' } })"><GitBranch :size="13"/>新建 / 历史</button><button v-if="!terminal.has(currentRun.status)&&!isUncertainRemote" class="button primary small" :disabled="Boolean(actionBusy)" @click="continueRun"><Play :size="13"/>{{ actionBusy==='continue'?'继续中…':'继续 / 检查阶段' }}</button><button v-if="['blocked','failed'].includes(currentRun.status)&&!isUncertainRemote" class="button ghost small" :disabled="Boolean(actionBusy)" @click="requestRunConfirmation('retry')"><RotateCcw :size="13"/>重试当前阶段</button><button v-if="!terminal.has(currentRun.status)" class="button danger small" :disabled="Boolean(actionBusy)" @click="requestRunConfirmation('cancel')"><CircleStop :size="13"/>取消运行</button></div></section>
      <div v-if="currentRun.blockedReason||currentRun.blockedCode" class="platform-alert" :class="blockedPresentation.tone"><LoaderCircle v-if="blockedPresentation.isWaiting" class="spin" :size="17"/><AlertTriangle v-else :size="17"/><div><strong>{{ blockedPresentation.code }}</strong><p>{{ blockedPresentation.summary }}</p></div></div>
      <section v-if="needsAttemptResolution" class="platform-card review-evidence"><header><div><strong>远端提交 / 费用待人工消歧</strong><small>仅显示后端安全投影；人工消歧会追加审计证据，不会伪装为生成通过。</small></div></header><div v-if="uncertainAttemptsLoading" class="platform-loading compact"><LoaderCircle class="spin" :size="18"/>正在读取安全的生成尝试（attempt）投影…</div><div v-else-if="uncertainAttemptsError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ uncertainAttemptsError }}</p></div><div v-else class="platform-dense-list"><article v-for="attempt in uncertainAttempts" :key="attempt.id"><div><strong>{{ operationLabel(attempt.operation) }} · 第 {{ attempt.attemptNumber }} 次尝试（attempt）</strong><small>{{ attempt.errorCode }} · 请求摘要（request hash） {{ attempt.requestSha256.slice(0,12) }} · {{ dateTime(attempt.finishedAt || attempt.updatedAt) }}</small></div><PlatformStatusPill :status="attempt.status"/><button v-if="auth.user?.isStaff" class="button primary tiny" @click="openUncertainResolution(attempt)">管理员消歧</button><small v-else>仅管理员可提交消歧结论</small></article></div></section>
      <section v-if="currentRun.status==='awaiting_approval'&&currentStage?.stageKey==='script_review'" class="platform-card review-evidence">
        <header><div><strong>剧本审核证据</strong><small>只读取前序 script_generate 或 script_structure 的不可变候选，并与 ScriptRevision 详情交叉核验。</small></div></header>
        <div v-if="scriptReview.error" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ scriptReview.error }}</p></div>
        <div v-else-if="reviewRevisionLoading" class="platform-loading compact"><LoaderCircle class="spin" :size="18"/>正在读取完整剧本正文…</div>
        <div v-else-if="reviewRevisionError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ reviewRevisionError }}</p></div>
        <div v-else-if="scriptReview.value&&reviewRevision" class="review-evidence-body">
          <div class="review-headline"><div><span>标题</span><strong>{{ scriptReview.value.title || '未命名' }}</strong></div><div><span>故事梗概（Logline）</span><p>{{ scriptReview.value.logline || '未提供' }}</p></div></div>
          <dl class="review-hashes"><div><dt>来源阶段</dt><dd>{{ scriptReview.value.sourceStageKey }}</dd></div><div><dt>提交语义</dt><dd>{{ scriptReview.value.submissionSemantics || '模型生成' }}</dd></div><div><dt>剧本版本（Revision）</dt><dd>{{ scriptReview.value.scriptRevisionId }}</dd></div><div><dt>正文摘要（SHA-256）</dt><dd>{{ scriptReview.value.contentSha256 }}</dd></div><div><dt>模型结果摘要（SHA-256）</dt><dd>{{ scriptReview.value.resultSha256 || '无模型提交' }}</dd></div><div><dt>阶段快照摘要（SHA-256）</dt><dd>{{ scriptReview.value.snapshotSha256 }}</dd></div></dl>
          <div v-if="scriptReview.value.warnings.length" class="platform-alert warning"><AlertTriangle :size="14"/><div><strong>生成警告</strong><ul><li v-for="warning in scriptReview.value.warnings" :key="warning">{{ warning }}</li></ul></div></div>
          <div class="review-script"><span>完整正文 · {{ reviewRevision.contentSizeBytes }} 字节</span><pre>{{ reviewRevision.content }}</pre></div>
        </div>
      </section>
      <section v-if="manualBibleReview" class="platform-card review-evidence manual-bible-review"><header><div><strong>手工一致性 Bible 确认</strong><small>本阶段使用 bible_review/manual，不会从剧本猜测、提取或自动生成实体。</small></div></header><div class="platform-alert" :class="missingManualBibleLabels.length?'warning':'success'" role="status"><component :is="missingManualBibleLabels.length?AlertTriangle:Check" :size="15"/><div><strong>{{ missingManualBibleLabels.length ? 'Bible 尚未达到最小人工合同' : '人物、场景和道具三类实体已齐全' }}</strong><p>请在当前项目的“一致性 Bible”中至少各创建 1 个人物、1 个场景和 1 个道具，保存并人工核对后再批准本阶段。平台不会用启发式规则从剧本自动补齐。</p></div></div><dl class="manual-bible-counts"><div :class="{missing:!manualBibleCounts.characters}"><dt>人物</dt><dd>{{ manualBibleCounts.characters }}</dd></div><div :class="{missing:!manualBibleCounts.scenes}"><dt>场景</dt><dd>{{ manualBibleCounts.scenes }}</dd></div><div :class="{missing:!manualBibleCounts.props}"><dt>道具</dt><dd>{{ manualBibleCounts.props }}</dd></div></dl><RouterLink class="button primary small manual-bible-link" :to="{name:'project-detail',params:{projectId},query:{tab:'bible'}}"><BookOpen :size="14"/>打开当前项目 Bible</RouterLink></section>
      <section v-else-if="currentRun.status==='awaiting_approval'&&currentStage?.stageKey==='bible_review'" class="platform-card review-evidence"><header><div><strong>一致性设定集（Bible）候选审核证据</strong><small>候选来自前序 设定集生成阶段（bible_materialize） 冻结快照；本页不从现有一致性设定（Bible）表反推。</small></div></header><div v-if="bibleReview.error" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ bibleReview.error }}</p></div><div v-else-if="bibleReview.value" class="review-evidence-body"><dl class="review-hashes"><div><dt>候选摘要（SHA-256）</dt><dd>{{ bibleReview.value.candidateSha256 }}</dd></div><div><dt>来源摘要（SHA-256）</dt><dd>{{ bibleReview.value.sourceFingerprint }}</dd></div><div><dt>剧本正文摘要（SHA-256）</dt><dd>{{ bibleReview.value.scriptContentSha256 }}</dd></div><div><dt>阶段快照摘要（SHA-256）</dt><dd>{{ bibleReview.value.snapshotSha256 }}</dd></div></dl><div class="bible-review-grid"><section v-for="(items,key) in bibleReview.value.collections" :key="key"><header><strong>{{ key }}</strong><span>{{ items.length }}</span></header><article v-for="item in items" :key="item.candidateId"><div><strong>{{ item.name }}</strong><small>{{ item.entityType }} · 出现 {{ item.occurrence }} 次 · {{ item.recommended ? '建议纳入' : '未建议' }}</small></div><p>{{ item.description || '无描述' }}</p><em>{{ item.visualIdentityEn || '无视觉身份描述' }}</em><code>{{ item.reviewedContentSha256 }}</code></article></section></div><details class="bible-diagnostics"><summary>解析诊断 · {{ bibleReview.value.diagnostics.length }}</summary><pre>{{ JSON.stringify(bibleReview.value.diagnostics, null, 2) }}</pre></details></div></section>
      <section v-if="currentRun.status==='awaiting_approval'&&currentStage?.stageKey==='visual_rules_review'" class="platform-card review-evidence visual-rules-review">
        <header><div><strong>全剧影视规则候选审核</strong><small>模型只产出候选。请逐项对照当前正式值，并确认最终英文值；批准后服务端才会一次性写入三项规则。</small></div></header>
        <div v-if="visualRulesReview.error" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ visualRulesReview.error }}</p></div>
        <div v-else-if="visualRulesReview.value" class="review-evidence-body">
          <dl class="review-hashes"><div><dt>剧本版本（Revision）</dt><dd>{{ visualRulesReview.value.scriptRevisionId }}</dd></div><div><dt>剧本正文摘要（SHA-256）</dt><dd>{{ visualRulesReview.value.scriptContentSha256 }}</dd></div><div><dt>Bible 摘要（SHA-256）</dt><dd>{{ visualRulesReview.value.confirmedBibleSha256 }}</dd></div><div><dt>候选摘要（SHA-256）</dt><dd>{{ visualRulesReview.value.candidateSha256 }}</dd></div><div><dt>服务提供方 / 模型（Provider / Model）</dt><dd>{{ visualRulesReview.value.provider }} / {{ visualRulesReview.value.model }}</dd></div><div><dt>人工核对进度</dt><dd>{{ visualRulesReviewedCount }} / 3</dd></div></dl>
          <div v-if="visualRulesReview.value.warnings.length" class="platform-alert warning"><AlertTriangle :size="14"/><div><strong>生成提示</strong><ul><li v-for="warning in visualRulesReview.value.warnings" :key="warning">{{ warning }}</li></ul></div></div>
          <div class="visual-rule-review-list">
            <article v-for="definition in visualRuleDefinitions" :key="definition.key">
              <header><strong>{{ definition.label }}</strong><small>{{ definition.key }} · {{ visualRuleAdvisoryMaximum(definition.key) === null ? '未配置长度建议' : `冻结建议 ≤ ${visualRuleAdvisoryMaximum(definition.key)} 字符（只提醒）` }}</small></header>
              <div class="visual-rule-columns"><label class="platform-field"><span>当前正式值</span><textarea :value="visualRuleCurrentValue(definition.key)" rows="5" readonly></textarea></label><label class="platform-field"><span>模型候选</span><textarea :value="visualRuleCandidateValue(definition.key)" rows="5" readonly></textarea></label><label class="platform-field"><span>审核后的最终值</span><textarea v-model="visualRulesEdits[definition.key]" rows="5" :aria-invalid="Boolean(visualRuleError(definition.key))"></textarea><small v-if="visualRuleError(definition.key)" class="gate-error">{{ visualRuleError(definition.key) }}</small><small v-else-if="visualRuleWarning(definition.key)" class="advisory-warning">{{ visualRuleWarning(definition.key) }}</small></label></div>
              <footer><label><input v-model="visualRulesReviewed[definition.key]" type="checkbox"/>已核对当前值、模型候选与最终值</label><button class="button ghost tiny" type="button" @click="restoreVisualRuleCandidate(definition.key)">恢复模型候选</button></footer>
            </article>
          </div>
        </div>
      </section>
      <section v-if="currentRun.status==='awaiting_approval'&&currentStage?.stageKey==='bible_image_review'" class="platform-card review-evidence">
        <header><div><strong>一致性设定集（Bible）参考图多来源审核</strong><small>每个实体可混合选择模型候选与素材库图片，并必须从全部已选图片中指定唯一主锚点（anchor）；未选模型版本仍保留在运行历史。</small></div></header>
        <div v-if="bibleImageReview.error" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ bibleImageReview.error }}</p></div>
        <div v-else-if="bibleImageReview.value" class="review-evidence-body">
          <dl class="review-hashes"><div><dt>生成阶段</dt><dd>{{ bibleImageReview.value.generationStageId }}</dd></div><div><dt>候选快照 SHA256</dt><dd>{{ bibleImageReview.value.snapshotSha256 }}</dd></div><div><dt>实体数量</dt><dd>{{ bibleImageReview.value.entities.length }}</dd></div></dl>
          <div v-if="bibleImageHistoryLoading" class="platform-loading compact"><LoaderCircle class="spin" :size="18"/>正在读取跨运行（Run）候选的完整性与预览证据…</div>
          <div v-else-if="bibleImageHistoryError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ bibleImageHistoryError }}</p></div>
          <section v-for="entity in bibleImageReview.value.entities" :key="`${entity.entityType}:${entity.entityId}`" class="bible-image-review-entity">
            <header><div><strong>{{ entity.entityName }}</strong><small>{{ entity.entityType }} · {{ entity.entityId }}</small></div><div class="bible-image-review-actions"><code>{{ entity.sourceSnapshotSha256 }}</code><button class="button ghost tiny" type="button" :disabled="Boolean(actionBusy)" @click="requestBibleRegeneration(entity)"><RefreshCw :size="13"/>使用冻结 Bible 模型重新生成</button></div></header>
            <div class="bible-image-source-heading"><strong>本次及历史模型候选</strong><small>来自冻结的 Bible 模型、提示词策略和生成尝试（Attempt）</small></div>
            <div class="bible-image-review-candidates">
              <article v-for="candidate in entity.candidates" :key="candidate.artifactId" :class="{selected:bibleSelectionFor(entity.entityType,entity.entityId).selectedArtifactIds.includes(candidate.artifactId)}">
                <img v-if="bibleCandidatePreview(candidate.artifactId,candidate.sha256)" :src="bibleCandidatePreview(candidate.artifactId,candidate.sha256)" :alt="`${entity.entityName} 参考图候选 ${candidate.variantIndex}`" loading="lazy" decoding="async"/>
                <div v-else-if="bibleImageHistoryLoading" class="bible-image-review-placeholder" role="status"><LoaderCircle class="spin" :size="28"/><span>正在校验预览…</span></div>
                <div v-else class="bible-image-review-placeholder"><Image :size="28"/><span>预览不可用，禁止盲选</span></div>
                <label><input type="checkbox" :checked="bibleSelectionFor(entity.entityType,entity.entityId).selectedArtifactIds.includes(candidate.artifactId)" :disabled="!bibleCandidatePreview(candidate.artifactId,candidate.sha256)" @change="toggleBibleArtifact(entity.entityType,entity.entityId,candidate.artifactId,eventChecked($event))"/><span>绑定此版本</span></label>
                <label><input type="radio" :name="`bible-anchor-${entity.entityId}`" :checked="bibleSelectionFor(entity.entityType,entity.entityId).anchorArtifactId===candidate.artifactId" :disabled="!bibleSelectionFor(entity.entityType,entity.entityId).selectedArtifactIds.includes(candidate.artifactId)" @change="setBibleAnchor(entity.entityType,entity.entityId,candidate.artifactId)"/><span>设为主锚点（anchor）</span></label>
                <small>{{ candidate.historical ? '历史运行（Run）复审' : '本次运行（Run）新候选' }} · 版本 {{ candidate.variantIndex }} · {{ presentPlatformStatus(candidate.status).label }}</small><code>{{ candidate.sha256 }}</code>
              </article>
            </div>
            <div class="bible-image-source-heading"><strong>素材库图片</strong><small>可与模型候选混合选择；服务端会复验所有者、分类、MIME、文件大小与内容摘要（checksum）</small></div>
            <template v-if="bibleLibraryAssetsFor(entity.entityType).length">
              <div class="bible-library-controls">
                <label class="platform-field"><span>按名称或类型筛选素材库</span><input :value="bibleLibrarySearchFor(entity.entityType,entity.entityId)" type="search" placeholder="例如：黑色门禁卡" @input="setBibleLibrarySearch(entity.entityType,entity.entityId,eventValue($event))"/></label>
                <button class="button ghost tiny" type="button" @click="setBibleLibraryExpanded(entity.entityType,entity.entityId,!bibleLibraryIsExpanded(entity.entityType,entity.entityId))">{{ bibleLibraryIsExpanded(entity.entityType,entity.entityId)?'收起素材库':`展开素材库（${bibleLibraryAssetsFor(entity.entityType).length}）` }}</button>
              </div>
              <div v-if="bibleLibraryIsExpanded(entity.entityType,entity.entityId)&&filteredBibleLibraryAssetsFor(entity.entityType,entity.entityId).length" class="bible-image-review-candidates bible-library-candidates">
                <article v-for="asset in visibleBibleLibraryAssetsFor(entity.entityType,entity.entityId)" :key="asset.id" :class="{selected:bibleSelectionFor(entity.entityType,entity.entityId).selectedAssetIds.includes(asset.id)}">
                  <img :src="asset.thumbnail||asset.mediaUrl" :alt="`${entity.entityName} 素材库参考图 ${asset.name}`" loading="lazy" decoding="async"/>
                  <label><input type="checkbox" :checked="bibleSelectionFor(entity.entityType,entity.entityId).selectedAssetIds.includes(asset.id)" @change="toggleBibleLibraryAsset(entity.entityType,entity.entityId,asset.id,eventChecked($event))"/><span>绑定素材库图片</span></label>
                  <label><input type="radio" :name="`bible-anchor-${entity.entityId}`" :checked="bibleSelectionFor(entity.entityType,entity.entityId).anchorAssetId===asset.id" :disabled="!bibleSelectionFor(entity.entityType,entity.entityId).selectedAssetIds.includes(asset.id)" @change="setBibleLibraryAnchor(entity.entityType,entity.entityId,asset.id)"/><span>设为主锚点（anchor）</span></label>
                  <small>素材库 · {{ asset.name }} · {{ asset.backendKind || asset.type }}</small><code>{{ asset.checksum }}</code>
                </article>
              </div>
              <div v-else-if="bibleLibraryIsExpanded(entity.entityType,entity.entityId)" class="platform-empty bible-library-empty">没有匹配当前筛选词的图片。</div>
              <div v-if="bibleLibraryIsExpanded(entity.entityType,entity.entityId)" class="bible-library-pagination">
                <small>已显示 {{ visibleBibleLibraryAssetsFor(entity.entityType,entity.entityId).length }} / {{ filteredBibleLibraryAssetsFor(entity.entityType,entity.entityId).length }} 张</small>
                <button v-if="visibleBibleLibraryAssetsFor(entity.entityType,entity.entityId).length<filteredBibleLibraryAssetsFor(entity.entityType,entity.entityId).length" class="button ghost tiny" type="button" @click="showMoreBibleLibraryAssets(entity.entityType,entity.entityId)">再显示 {{ BIBLE_LIBRARY_PAGE_SIZE }} 张</button>
              </div>
            </template>
            <div v-else class="platform-empty bible-library-empty">当前素材库没有分类、图片完整性与预览均可用的{{ entity.entityType }}图片。<RouterLink to="/assets" target="_blank">打开素材库</RouterLink></div>
          </section>
        </div>
      </section>
      <section v-if="currentRun.status==='awaiting_approval'&&currentStage?.stageKey==='storyboard_review'" class="platform-card review-evidence storyboard-reference-review">
        <header><div><strong>分镜审核与逐镜精确参考素材</strong><small>图片必须来自本 Run 已批准版本；视频/音频用途、数量、配对、互斥与预处理均按每个镜头所选 Workflow 的冻结条件合同显示。提交顺序即冻结顺序。</small></div><button class="button ghost tiny" type="button" :disabled="storyboardAssetsLoading" @click="loadStoryboardApprovedAssets"><RefreshCw :size="13"/>刷新正式素材</button></header>
        <div v-if="storyboardReview.error" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ storyboardReview.error }}</p></div>
        <div v-else-if="storyboardReview.value" class="review-evidence-body">
          <dl class="review-hashes"><div><dt>规范内容摘要（Canonical SHA-256）</dt><dd>{{ storyboardReview.value.canonicalSha256 }}</dd></div><div><dt>阶段快照摘要（SHA-256）</dt><dd>{{ storyboardReview.value.snapshotSha256 }}</dd></div><div><dt>规模</dt><dd>{{ storyboardReview.value.episodeCount }} 集 · {{ storyboardReview.value.shotCount }} 镜</dd></div><div><dt>可用素材</dt><dd>{{ storyboardReview.value.approvedBibleAssets.length }} 图 · {{ storyboardReferenceVideos.filter((item)=>storyboardMediaUsable(item.id,'video')).length }} 视频 · {{ storyboardReferenceAudios.filter((item)=>storyboardMediaUsable(item.id,'audio')).length }} 音频</dd></div></dl>
          <div v-if="storyboardAssetsLoading" class="platform-loading compact"><LoaderCircle class="spin" :size="18"/>正在校验正式 Asset 与预览…</div>
          <div v-else-if="storyboardAssetsError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ storyboardAssetsError }}</p></div>
          <section v-for="episode in storyboardReview.value.episodes" :key="episode.id" class="storyboard-reference-episode">
            <header><div><strong>第 {{ episode.number }} 集 · {{ episode.title || '未命名' }}</strong><small>{{ episode.shotCount }} 镜 · {{ (episode.durationMs/1000).toFixed(1) }} 秒</small></div></header>
            <article v-for="shot in episode.shots" :key="shot.id" class="storyboard-reference-shot">
              <header><div><strong>镜头 {{ shot.number }} · {{ shot.title || '未命名' }}</strong><small>{{ shot.mediaType }} · {{ shot.conditioningModeCapability?.label || shot.conditioningMode }} · {{ (shot.durationMs/1000).toFixed(1) }} 秒</small></div><code>{{ shot.id }}</code></header>
              <div v-if="shot.mediaType==='image'||shot.conditioningModeCapability?.inputKind==='text_only'" class="platform-alert"><Check :size="15"/><p>{{ shot.mediaType==='image' ? '图片镜头不接受视频参考绑定。' : '当前条件模式为纯文本输入，参考绑定固定为空。' }}</p></div>
              <div v-else-if="storyboardReferenceDrafts[shot.id]" class="storyboard-reference-fields">
                <label v-if="shot.conditioningModeCapability?.inputKind==='frame_slots'&&shot.conditioningModeCapability.inputs.firstFrame.maximumItems>0" class="platform-field"><span>首帧 Asset（{{ shot.conditioningModeCapability.inputs.firstFrame.minimumItems>0?'必选':'可选' }}）</span><select v-model="storyboardReferenceDrafts[shot.id].firstFrameAssetId"><option value="">{{ shot.conditioningModeCapability.inputs.firstFrame.minimumItems>0?'请选择本 Run 已批准图片':'不使用首帧' }}</option><option v-for="fact in approvedFrameAssets" :key="`first:${shot.id}:${fact.assetId}`" :value="fact.assetId" :disabled="!selectedStoryboardAssetUsable(fact.assetId)">{{ storyboardAssetLabel(fact.assetId) }}</option></select></label>
                <label v-if="shot.conditioningModeCapability?.inputKind==='frame_slots'&&shot.conditioningModeCapability.inputs.lastFrame.maximumItems>0" class="platform-field"><span>尾帧 Asset（{{ shot.conditioningModeCapability.inputs.lastFrame.minimumItems>0?'必选':'可选' }}）</span><select v-model="storyboardReferenceDrafts[shot.id].lastFrameAssetId"><option value="">{{ shot.conditioningModeCapability.inputs.lastFrame.minimumItems>0?'请选择本 Run 已批准图片':'不使用尾帧' }}</option><option v-for="fact in approvedFrameAssets" :key="`last:${shot.id}:${fact.assetId}`" :value="fact.assetId" :disabled="!selectedStoryboardAssetUsable(fact.assetId)">{{ storyboardAssetLabel(fact.assetId) }}</option></select></label>
                <template v-if="shot.conditioningModeCapability?.inputKind==='classified_references'">
                  <label v-if="shot.requiresFirstFrameContinuity||storyboardReferenceDrafts[shot.id].firstFrameReferenceAssetId" class="platform-field"><span>首帧连续参考（可选）</span><select v-model="storyboardReferenceDrafts[shot.id].firstFrameReferenceAssetId"><option value="">由运行时连续性任务生成或不使用</option><option v-for="fact in approvedFrameAssets" :key="`continuity:${shot.id}:${fact.assetId}`" :value="fact.assetId" :disabled="!selectedStoryboardAssetUsable(fact.assetId)">{{ storyboardAssetLabel(fact.assetId) }}</option></select></label>
                  <label v-for="entityId in shot.visualReferenceCharacterIds" :key="`character:${shot.id}:${entityId}`" class="platform-field"><span>角色 · {{ bibleEntityName('character',entityId) }}</span><select v-model="storyboardReferenceDrafts[shot.id].characterAssetIds[entityId]"><option value="">请选择该角色本 Run 版本</option><option v-for="fact in approvedAssetsForEntity('character',entityId)" :key="fact.assetId" :value="fact.assetId" :disabled="!selectedStoryboardAssetUsable(fact.assetId)">{{ storyboardAssetLabel(fact.assetId) }}</option></select></label>
                  <label v-if="shot.sceneId" class="platform-field"><span>场景 · {{ bibleEntityName('scene',shot.sceneId) }}</span><select v-model="storyboardReferenceDrafts[shot.id].sceneAssetId"><option value="">请选择该场景本 Run 版本</option><option v-for="fact in approvedAssetsForEntity('scene',shot.sceneId)" :key="fact.assetId" :value="fact.assetId" :disabled="!selectedStoryboardAssetUsable(fact.assetId)">{{ storyboardAssetLabel(fact.assetId) }}</option></select></label>
                  <label v-for="entityId in shot.propIds" :key="`prop:${shot.id}:${entityId}`" class="platform-field"><span>道具 · {{ bibleEntityName('prop',entityId) }}</span><select v-model="storyboardReferenceDrafts[shot.id].propAssetIds[entityId]"><option value="">请选择该道具本 Run 版本</option><option v-for="fact in approvedAssetsForEntity('prop',entityId)" :key="fact.assetId" :value="fact.assetId" :disabled="!selectedStoryboardAssetUsable(fact.assetId)">{{ storyboardAssetLabel(fact.assetId) }}</option></select></label>
                  <section class="storyboard-media-binding">
                    <header><div><strong>参考视频（{{ storyboardVideoRangeLabel(shot.id) }}）</strong><small>{{ storyboardVideoContractHint(shot.id) }} 预处理：{{ storyboardPreprocessingLabel(shot.id,'video') }}。{{ REFERENCE_VIDEO_CONTENT_SAFETY_NOTICE }}</small></div><label v-if="storyboardVideoContract(shot.id).maximumItems>0" class="button ghost tiny file-button"><Upload :size="13"/>{{ storyboardUploadingReference===`${shot.id}:video:`?'上传中…':'上传视频' }}<input type="file" accept="video/mp4,video/quicktime,video/webm" :disabled="Boolean(storyboardUploadingReference)" @change="uploadStoryboardReference(shot.id,'video',$event)"/></label></header>
                    <div class="storyboard-media-options">
                      <label v-for="asset in storyboardReferenceVideos" :key="`video-option:${shot.id}:${asset.id}`" :class="{invalid:!storyboardMediaUsable(asset.id,'video',shot.id)}"><input type="checkbox" :checked="storyboardVideoSelected(shot.id,asset.id)" :disabled="storyboardVideoContract(shot.id).maximumItems===0||!storyboardMediaUsable(asset.id,'video',shot.id)||(!storyboardVideoSelected(shot.id,asset.id)&&storyboardReferenceDrafts[shot.id].videos.length>=storyboardVideoContract(shot.id).maximumItems)" @change="toggleStoryboardVideo(shot.id,asset.id,eventChecked($event))"/><span>{{ storyboardAssetLabel(asset.id) }}</span><small>{{ storyboardHasEmbeddedAudio(asset.id)?'含内嵌音轨':'无内嵌音轨' }}</small></label>
                      <p v-if="!storyboardReferenceVideos.length" class="platform-empty">素材库暂无参考视频，可在此上传。</p>
                    </div>
                    <article v-for="(videoBinding,videoIndex) in storyboardReferenceDrafts[shot.id].videos" :key="`video-binding:${shot.id}:${videoBinding.assetId}`" class="storyboard-video-binding">
                      <header><strong>Video {{ videoIndex+1 }} · {{ storyboardAssetLabel(videoBinding.assetId) }}</strong><button type="button" class="button ghost tiny" @click="toggleStoryboardVideo(shot.id,videoBinding.assetId,false)">移除</button></header>
                      <div class="storyboard-media-controls">
                        <label class="platform-field"><span>视频用途</span><select :value="videoBinding.purpose" @change="setStoryboardVideoPurpose(shot.id,videoBinding.assetId,$event)"><option v-for="purpose in storyboardVideoPurposes(shot.id)" :key="purpose.key" :value="purpose.key" :disabled="storyboardVideoPurposeDisabled(shot.id,videoBinding.assetId,purpose.key)">{{ purpose.label }}（{{ purpose.key }}）</option></select><small>{{ storyboardPurposeDescription(shot.id,'video',videoBinding.purpose) }}</small></label>
                        <label class="platform-checkbox"><input type="checkbox" :checked="videoBinding.useEmbeddedAudio" :disabled="!storyboardPairingRule(shot.id,videoBinding.purpose)?.allowEmbeddedSource||!storyboardHasEmbeddedAudio(videoBinding.assetId)" @change="setStoryboardEmbeddedAudio(shot.id,videoBinding.assetId,eventChecked($event))"/><span>使用视频内嵌原音</span></label>
                        <label class="platform-field"><span>外部配套音频</span><select :value="videoBinding.audioAssetId||''" :disabled="videoBinding.useEmbeddedAudio||!storyboardPairingRule(shot.id,videoBinding.purpose)?.allowExternalTarget" @change="setStoryboardPairedAudio(shot.id,videoBinding.assetId,eventValue($event))"><option value="">不绑定外部音频</option><option v-for="asset in storyboardReferenceAudios" :key="`paired:${videoBinding.assetId}:${asset.id}`" :value="asset.id" :disabled="!storyboardMediaUsable(asset.id,'audio',shot.id,'paired')||(storyboardUsedMediaAssetIds(shot.id,videoBinding.assetId).has(asset.id)&&videoBinding.audioAssetId!==asset.id)">{{ storyboardAssetLabel(asset.id) }}</option></select><small>预处理：{{ storyboardPreprocessingLabel(shot.id,'paired_audio') }}</small></label>
                        <label v-if="videoBinding.useEmbeddedAudio||videoBinding.audioAssetId" class="platform-field"><span>配套 Audio {{ storyboardPairedAudioOrdinal(shot.id,videoIndex) }} 用途</span><select :value="videoBinding.audioPurpose||''" @change="setStoryboardPairedAudioPurpose(shot.id,videoBinding.assetId,eventValue($event))"><option v-for="purpose in storyboardPairedAudioPurposes(shot.id,videoBinding.purpose)" :key="purpose.key" :value="purpose.key">{{ purpose.label }}（{{ purpose.key }}）</option></select><small>{{ storyboardPurposeDescription(shot.id,'audio',videoBinding.audioPurpose||'') }}</small></label>
                        <label class="button ghost tiny file-button"><Upload :size="13"/>{{ storyboardUploadingReference===`${shot.id}:audio:${videoBinding.assetId}`?'上传中…':'上传并绑定外部音频' }}<input type="file" accept="audio/wav,audio/mpeg,audio/flac,audio/mp4,audio/ogg" :disabled="Boolean(storyboardUploadingReference)" @change="uploadStoryboardReference(shot.id,'audio',$event,videoBinding.assetId)"/></label>
                      </div>
                    </article>
                  </section>
                  <section class="storyboard-media-binding">
                    <header><div><strong>独立参考音频（{{ shot.conditioningModeCapability.inputs.referenceAudios.minimumItems }}–{{ shot.conditioningModeCapability.inputs.referenceAudios.maximumItems }}）</strong><small>独立音频按当前选择顺序排在视频配套音频之后；预处理：{{ storyboardPreprocessingLabel(shot.id,'reference_audio') }}。</small></div><label v-if="shot.conditioningModeCapability.inputs.referenceAudios.maximumItems>0" class="button ghost tiny file-button"><Upload :size="13"/>{{ storyboardUploadingReference===`${shot.id}:audio:`?'上传中…':'上传音频' }}<input type="file" accept="audio/wav,audio/mpeg,audio/flac,audio/mp4,audio/ogg" :disabled="Boolean(storyboardUploadingReference)" @change="uploadStoryboardReference(shot.id,'audio',$event)"/></label></header>
                    <div class="storyboard-media-options">
                      <label v-for="asset in storyboardReferenceAudios" :key="`audio-option:${shot.id}:${asset.id}`" :class="{invalid:!storyboardMediaUsable(asset.id,'audio',shot.id,'standalone')}"><input type="checkbox" :checked="storyboardAudioSelected(shot.id,asset.id)" :disabled="!storyboardMediaUsable(asset.id,'audio',shot.id,'standalone')||(!storyboardAudioSelected(shot.id,asset.id)&&(storyboardReferenceDrafts[shot.id].audios.length>=shot.conditioningModeCapability.inputs.referenceAudios.maximumItems||storyboardUsedMediaAssetIds(shot.id).has(asset.id)))" @change="toggleStoryboardAudio(shot.id,asset.id,eventChecked($event))"/><span>{{ storyboardAssetLabel(asset.id) }}</span></label>
                      <p v-if="!storyboardReferenceAudios.length" class="platform-empty">素材库暂无参考音频，可在此上传。</p>
                    </div>
                    <div v-if="storyboardReferenceDrafts[shot.id].audios.length" class="storyboard-audio-bindings"><label v-for="(audioBinding,audioIndex) in storyboardReferenceDrafts[shot.id].audios" :key="`audio-binding:${shot.id}:${audioBinding.assetId}`" class="platform-field"><span>独立音频（Audio）{{ storyboardStandaloneAudioOrdinal(shot.id,audioIndex) }} · {{ storyboardAssetLabel(audioBinding.assetId) }}</span><select :value="audioBinding.purpose" @change="setStoryboardAudioPurpose(shot.id,audioBinding.assetId,eventValue($event))"><option v-for="purpose in storyboardAudioPurposes(shot.id)" :key="purpose.key" :value="purpose.key">{{ purpose.label }}（{{ purpose.key }}）</option></select><small>{{ storyboardPurposeDescription(shot.id,'audio',audioBinding.purpose) }}</small></label></div>
                  </section>
                </template>
                <div class="storyboard-reference-previews"><figure v-for="assetId in storyboardDraftAssetIds(shot.id)" :key="assetId"><img v-if="storyboardAsset(assetId)?.thumbnail" :src="storyboardAsset(assetId)?.thumbnail" :alt="storyboardAssetLabel(assetId)"/><span v-else class="storyboard-media-kind">{{ storyboardAsset(assetId)?.type?.toUpperCase()||'素材（Asset）' }}</span><figcaption><strong>{{ storyboardInputLabel(shot.id,assetId) }}</strong> · {{ storyboardAssetLabel(assetId) }}</figcaption></figure></div>
              </div>
            </article>
          </section>
        </div>
      </section>
      <section v-if="currentRun.status==='awaiting_approval'&&currentStage?.stageKey==='script_normalize_review'" class="platform-card review-evidence">
        <header><div><strong>英文执行字段人工审核</strong><small>来源与摘要（hash）来自前序剧本规范化阶段（script_normalize）的不可变快照；只有逐项确认后的整批内容才会写入正式提示词字段翻译（PromptFieldTranslation）。</small></div></header>
        <div v-if="normalizationReview.error" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ normalizationReview.error }}</p></div>
        <div v-else-if="normalizationReview.value" class="review-evidence-body">
          <div v-if="normalizationHumanFallback" class="platform-alert warning"><AlertTriangle :size="15"/><p><template v-if="normalizationReview.value.stopReason==='retry_limit_reached'">模型结果已连续 {{ normalizationReview.value.retryPolicy?.attemptsUsed }} 次未通过硬合同校验，达到本次运行冻结上限（首次生成 + {{ normalizationReview.value.retryPolicy?.retryLimit }} 次重试）。</template><template v-else>第 {{ normalizationReview.value.retryPolicy?.attemptsUsed }} 次硬合同校验失败后，服务端无法从已验证审计中构造安全修复反馈；为避免盲目重放付费请求，系统在冻结上限前转人工。</template>已成功批次保留，失败输出未作为候选；请只补全待定稿字段并人工确认。</p></div>
          <div v-if="normalizationHumanOnly" class="platform-alert"><Check :size="15"/><p>{{ normalizationHumanOnlyRecipe ? '本阶段按配方中的 script_normalize/manual 纯人工方式执行' : '本次已冻结运行按历史纯人工来源执行' }}，未调用服务提供方（Provider）或模型。系统已按本次运行冻结的 PromptProfile 列出待定稿字段；请逐项填写英文定稿并确认。</p></div>
          <div v-if="normalizationReview.value.advisories.length" class="platform-alert warning"><AlertTriangle :size="15"/><div><strong>建议值审计（不阻断）</strong><ul><li v-for="(advisory,index) in normalizationReview.value.advisories" :key="`${advisory.code}:${advisory.batchIndex}:${index}`"><template v-if="advisory.code==='recommended_input_bytes_exceeded'">批次 {{ advisory.batchIndex + 1 }} 输入 {{ advisory.measured }} B，超过建议阈值 {{ advisory.recommendedMaximum }} B，但未达到硬上限 {{ advisory.hardMaximum }} B；候选仍会生成，请人工判断是否精简合同。</template><template v-else-if="advisory.code==='recommended_output_tokens_exceeded'">批次 {{ advisory.batchIndex + 1 }} 输出 {{ advisory.measured }} tokens，超过建议阈值 {{ advisory.recommendedMaximum }}，但未达到硬上限 {{ advisory.hardMaximum }}；请人工判断是否优化策略。</template><template v-else>字段 {{ advisory.fieldKey }} 实测 {{ advisory.measured }} {{ advisory.lengthUnit }}，建议范围 {{ advisory.minimum ?? '不限' }}–{{ advisory.maximum ?? '不限' }}；候选仍可审核并继续。</template></li></ul></div></div>
          <dl class="review-hashes">
            <div><dt>最新生成尝试（Attempt）</dt><dd>{{ normalizationReview.value.attemptId || '无需提交' }}</dd></div>
            <div><dt>{{ normalizationHumanOnly ? '纯人工输入证据摘要' : normalizationHumanFallback ? '人工接管证据摘要' : '模型结果摘要' }}（SHA-256）</dt><dd>{{ normalizationReview.value.resultSha256 }}</dd></div>
            <div><dt>阶段快照摘要（SHA-256）</dt><dd>{{ normalizationReview.value.snapshotSha256 }}</dd></div>
            <div><dt>服务提供方 / 模型（Provider / Model）</dt><dd>{{ normalizationReview.value.provider || '—' }} / {{ normalizationReview.value.model || '—' }}</dd></div>
            <div><dt>人工核对进度</dt><dd>{{ normalizationReviewedCount }} / {{ normalizationReview.value.candidates.length }}</dd></div>
          </dl>
          <details v-if="normalizationHumanFallback" class="stage-policy-decisions">
            <summary>查看历次校验失败与安全修复审计</summary>
            <div v-if="normalizationAttemptsLoading" class="platform-loading compact"><LoaderCircle class="spin" :size="16"/>正在读取只读修复链…</div>
            <div v-else-if="normalizationAttemptsError" class="platform-alert danger"><AlertTriangle :size="14"/><p>{{ normalizationAttemptsError }}</p></div>
            <div v-else-if="normalizationRetryRepairs.error" class="platform-alert danger"><AlertTriangle :size="14"/><p>{{ normalizationRetryRepairs.error }}</p></div>
            <article v-for="failure in normalizationReview.value.validationFailures" :key="failure.attemptId">
              <strong>第 {{ failure.attemptNumber }} 次 · {{ failure.errorCode }}</strong>
              <small>受控原因码：{{ failure.reasonCode || '未提供安全分类' }} · 批次 {{ failure.batchIndex + 1 }} · route {{ failure.routeIndex ?? '—' }}</small>
              <small v-if="failure.validationContext">字段 {{ failure.validationContext.fieldKey }} · 实测 {{ failure.validationContext.measured ?? '未知' }} · 允许 {{ failure.validationContext.minimum ?? '不限' }}–{{ failure.validationContext.maximum ?? '不限' }} {{ failure.validationContext.lengthUnit }} · 请求项 {{ failure.validationContext.requestIndex === null ? '未提供' : failure.validationContext.requestIndex + 1 }}</small>
              <small v-else>没有通过白名单校验的数值上下文；页面不会展示模型原文或未知诊断文本。</small>
              <code>attempt {{ failure.attemptId }}</code>
              <code>resolution {{ failure.resolutionId }} · evidence {{ failure.resolutionEvidenceSha256 }}</code>
              <template v-if="normalizationRetryRepairByFailure.get(failure.attemptId)">
                <small>下一次 attempt 已使用服务端派生的安全重试反馈（retry_feedback）；反馈正文不可查看或编辑。</small>
                <code>next attempt {{ normalizationRetryRepairByFailure.get(failure.attemptId)?.retryAttemptNumber }} · {{ normalizationRetryRepairByFailure.get(failure.attemptId)?.retryAttemptId }}</code>
                <code>retry_feedback SHA-256 {{ normalizationRetryRepairByFailure.get(failure.attemptId)?.retryFeedbackSha256 }}</code>
              </template>
              <small v-else>下一次 attempt 未使用安全重试反馈：可能是最后一次失败，或该失败没有可反校验的长度上下文。</small>
            </article>
          </details>
          <div v-if="!normalizationReview.value.candidates.length" class="platform-alert"><Check :size="15"/><p>当前正式对象没有需要规范化的导演字段；本阶段未调用外部模型，可直接批准。</p></div>
          <div v-else class="platform-dense-list"><article v-for="candidate in normalizationReview.value.candidates" :key="candidate.requestId"><div><strong>{{ candidate.label || candidate.fieldKey }}</strong><small>{{ candidate.scopeType }} · {{ candidate.fieldKey }} · 请求（request）{{ candidate.requestId }}</small><label class="platform-field"><span>原始字段 {{ candidate.label || candidate.fieldKey }}</span><textarea :value="candidate.sourceText" rows="2" readonly :aria-label="`原始字段 ${candidate.label || candidate.fieldKey}`"></textarea></label><label class="platform-field"><span>{{ normalizationManualEntry ? '人工英文定稿' : '英文候选' }} {{ candidate.label || candidate.fieldKey }}</span><textarea v-model="normalizationEdits[candidate.requestId]" rows="3" :placeholder="normalizationHumanOnly ? '本阶段不调用模型，请在此填写英文定稿' : normalizationHumanFallback ? '模型候选未通过校验，请在此填写英文定稿' : ''" :aria-invalid="Boolean(normalizationCandidateError(candidate.requestId,candidate.fieldKey))" :aria-label="`${normalizationManualEntry ? '人工英文定稿' : '英文候选'} ${candidate.label || candidate.fieldKey}`"></textarea><small v-if="normalizationCandidateError(candidate.requestId,candidate.fieldKey)" class="gate-error">{{ normalizationCandidateError(candidate.requestId,candidate.fieldKey) }}</small></label><div><label><input v-model="normalizationReviewed[candidate.requestId]" type="checkbox"/>已核对该项来源与英文内容</label><button class="button ghost tiny" type="button" @click="restoreNormalizationCandidate(candidate.requestId)">{{ normalizationManualEntry ? '清空人工填写' : '恢复模型候选' }}</button></div></div></article></div>
        </div>
      </section>
      <section v-if="currentRun.status==='awaiting_approval'&&currentStage&&routeGate" class="approval-gate route-approval-gate"><div><strong>选择显式路由 · {{ currentStage.stageKey }}</strong><p>候选来自本次运行冻结配方；选择会记录实际服务提供方（Provider）、模型部署与工作流版本。</p></div><div v-if="routeOptions.length" class="route-option-list"><label v-for="option in routeOptions" :key="option.index" :class="{selected:routeIndex===option.index}"><input v-model="routeIndex" type="radio" name="production-route" :value="option.index"/><span><strong>路由（Route）{{ option.index }} · {{ option.driverType }}</strong><small>服务提供方（Provider）{{ option.providerId || '—' }}</small><small>模型版本 {{ option.modelProfileVersionId || '—' }}</small><small>模型部署 {{ option.modelDeploymentId || '—' }}</small><small>工作流 {{ option.workflowVersionId || '未绑定' }}</small><small>输出 {{ option.workflowOutputKind || '未声明' }}<template v-if="option.workflowConditioningModes"> · 条件模式 {{ option.workflowConditioningModes }}</template></small><em>{{ option.isExternal ? '外部调用' : '本地 / 内网调用' }}</em></span></label></div><div v-else class="platform-alert danger"><AlertTriangle :size="15"/><p>服务端没有返回冻结路由（route）候选，无法提交人工选择。</p></div><button class="button primary small" :disabled="Boolean(actionBusy)||routeIndex===null||!routeOptions.length" @click="selectRoute">{{ actionBusy==='route'?'提交中…':'确认所选路由' }}</button></section>
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
            <header><span>{{ String(stage.ordinal).padStart(2,'0') }}</span><PlatformStatusPill :status="stage.status"/></header><strong>{{ stageLabel(stage.stageKey) }}</strong><small>{{ modeLabel(stage.mode) }} · 第 {{ stage.attemptCount }} 次尝试（attempt）</small><ChevronDown :size="14"/>
          </button>
          <div v-if="expandedStageId===stage.id" :id="`stage-details-${stage.id}`" class="run-stage-details">
            <p v-if="stage.reason||stage.reasonCode"><code>{{ stageIssuePresentation(stage).code }}</code> · {{ stageIssuePresentation(stage).summary }}</p><p v-else>本阶段没有服务端原因说明。</p>
            <div v-if="stage.decisionPolicy" class="stage-policy-evidence"><strong>决策策略</strong><code>{{ stage.decisionPolicy }}</code><small>策略摘要（SHA-256） · {{ stage.decisionPolicySha256 }}</small></div>
            <details v-if="stage.approvalDecisionFacts.length" class="stage-policy-decisions"><summary>审批审计事实 · {{ stage.approvalDecisionFacts.length }}</summary><article v-for="fact in stage.approvalDecisionFacts" :key="fact.id"><strong>{{ fact.actor==='recipe_policy'?'配方策略自动决策':'人工决策' }} · {{ fact.decision }}</strong><small>修订版（revision）{{ fact.revision }} · {{ dateTime(fact.createdAt) }} · {{ fact.decisionPolicy || '无策略' }}</small><code>{{ fact.outputSnapshotSha256 }}</code></article></details>
            <dl><div><dt>输入快照</dt><dd><code>{{ stage.inputSnapshotSha256 || '—' }}</code></dd></div><div><dt>输出快照</dt><dd><code>{{ stage.outputSnapshotSha256 || '—' }}</code></dd></div><div><dt>已批费用</dt><dd>{{ stage.approvedEstimatedCost || '—' }} {{ stage.approvedCostCurrency }}</dd></div></dl>
          </div>
        </article>
      </section>
      <section class="run-facts"><div><span>剧本版本（revision）</span><code>{{ currentRun.scriptRevision }}</code></div><div><span>生产配方（Recipe）</span><code>{{ currentRun.recipeVersion }}</code></div><div><span>配置快照</span><code>{{ currentRun.configSnapshotSha256 }}</code></div><div><span>费用（预估 / 承诺 / 实际）</span><strong>{{ currentRun.estimatedCostTotal }} / {{ currentRun.committedCostTotal }} / {{ currentRun.actualCostTotal }} {{ currentRun.costCurrency }}</strong></div><div><span>发行包（ReleaseBundle）</span><code>{{ currentRun.releaseBundleId || '尚未封存' }}</code></div><div><span>结束</span><strong>{{ dateTime(currentRun.finishedAt) }}</strong></div></section>
      <section class="artifact-groups"><header><div><strong>媒体产物与选片</strong><span>仅展示 image、video 与 assembly；同组 2–4 个版本可并排比较。</span></div></header><GeneratedArtifactCompare v-for="group in artifactGroups" :key="group.key" :run-id="currentRun.id" :artifacts="group.items" @selected="artifactSelected"/><p v-if="!artifactGroups.length" class="platform-empty">当前阶段尚未归档可比较的图片、视频或分集母版。</p></section>
      <BaseModal :open="Boolean(pendingBibleRegeneration)" :dismissible="!Boolean(actionBusy)" title="确认重新生成 Bible 候选" eyebrow="追加候选 / APPEND CANDIDATES" width="680px" @close="closeBibleRegeneration">
        <dl v-if="pendingBibleRegeneration" class="run-confirmation-details"><div><dt>目标实体</dt><dd>{{ pendingBibleRegeneration.entityName }} · {{ pendingBibleRegeneration.entityType }}</dd></div><div><dt>生产运行（Run）</dt><dd><code>{{ pendingBibleRegeneration.runId }}</code></dd></div><div><dt>审核快照（SHA-256）</dt><dd><code>{{ pendingBibleRegeneration.snapshotSha256 }}</code></dd></div><div><dt>当前候选</dt><dd>{{ pendingBibleRegeneration.existingCandidateCount }} 个；本操作只追加新候选，不删除或覆盖现有候选。</dd></div><div><dt>模型与策略</dt><dd>服务端将复用本运行已冻结的 Bible 模型路由、提示词策略与来源快照；不能在此临时改用其他模型。</dd></div><div><dt>费用与外发</dt><dd>重新生成会创建新的可审计生成尝试（Attempt），并可能新增模型调用费用。确认后运行将返回生成阶段，完成后新旧候选会一起进入审核。</dd></div></dl>
        <div v-if="pendingBibleRegenerationError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ pendingBibleRegenerationError }}</p></div>
        <template #footer><button class="button ghost" :disabled="Boolean(actionBusy)" @click="closeBibleRegeneration">返回</button><button class="button primary" :disabled="Boolean(actionBusy)" @click="confirmBibleRegeneration">{{ actionBusy==='regenerate-bible-images'?'正在提交…':'确认追加生成' }}</button></template>
      </BaseModal>
      <BaseModal :open="Boolean(pendingRunConfirmation)" :dismissible="!Boolean(actionBusy)" :title="pendingRunConfirmation?.action==='retry'?'确认重试当前阶段':'确认取消运行'" eyebrow="生产运行确认 / PRODUCTION RUN CONFIRMATION" width="660px" @close="closeRunConfirmation">
        <dl v-if="pendingRunConfirmation" class="run-confirmation-details"><div><dt>生产运行（Run）</dt><dd><code>{{ pendingRunConfirmation.runId }}</code></dd></div><div><dt>当前阶段</dt><dd>{{ pendingRunConfirmation.stageKey }}</dd></div><div><dt>影响</dt><dd>{{ pendingRunConfirmation.action==='retry'?'服务端将为失败单元创建新的可审计生成尝试（attempt）；已成功产物仍按服务端状态保留。':'服务端将请求停止未完成阶段；已外发请求可能仍需等待远端终态，晚到结果不会自动采用。' }}</dd></div><div><dt>费用提示</dt><dd>预估 {{ pendingRunConfirmation.estimatedCost }} / 已承诺 {{ pendingRunConfirmation.committedCost }} / 已发生 {{ pendingRunConfirmation.actualCost }} {{ pendingRunConfirmation.currency }}。{{ pendingRunConfirmation.action==='retry'?'重试可能增加费用。':'取消不代表已发生费用会撤销。' }}</dd></div></dl>
        <div v-if="pendingRunConfirmationError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ pendingRunConfirmationError }}</p></div>
        <template #footer><button class="button ghost" :disabled="Boolean(actionBusy)" @click="closeRunConfirmation">返回</button><button v-if="pendingRunConfirmation" class="button" :class="pendingRunConfirmation.action==='cancel'?'danger':'primary'" :disabled="Boolean(actionBusy)" @click="confirmRunAction">{{ actionBusy ? '服务端处理中…' : pendingRunConfirmation.action==='retry'?'确认重试':'确认取消' }}</button></template>
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
        <template #footer><button class="button ghost" :disabled="uncertainResolutionSaving" @click="closeUncertainResolution">取消</button><button class="button primary" :disabled="uncertainResolutionSaving||Boolean(uncertainResolutionValidation)" @click="submitUncertainResolution">{{ uncertainResolutionSaving?'提交中…':'追加消歧记录' }}</button></template>
      </BaseModal>
    </template>
  </div>
</template>

<style scoped>
.visual-rule-review-list{display:grid;gap:12px}.visual-rule-review-list>article{display:grid;gap:11px;padding:14px;border:1px solid #28333d;border-radius:9px;background:#0a0f13}.visual-rule-review-list>article>header,.visual-rule-review-list>article>footer{display:flex;align-items:center;justify-content:space-between;gap:12px}.visual-rule-review-list>article>header small{color:#78848d;font:9px Consolas,monospace}.visual-rule-review-list>article>footer label{display:flex;align-items:center;gap:7px;color:#c4cbd0;font-size:10px}.visual-rule-columns{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.visual-rule-columns textarea{height:132px;resize:vertical}.visual-rule-columns label:first-child textarea{color:#8d979f;background:#080c10}.visual-rule-columns label:nth-child(2) textarea{color:#a9c8ba;background:#08100d}@media(max-width:1000px){.visual-rule-columns{grid-template-columns:1fr}}
.advisory-warning{color:#d7a65b;font-size:9px;line-height:1.5}
.bible-image-review-entity{display:grid;gap:12px;padding:14px;border:1px solid #28333d;border-radius:9px;background:#0a0f13}.bible-image-review-entity>header{display:flex;align-items:flex-start;justify-content:space-between;gap:14px}.bible-image-review-entity>header>div{display:grid;gap:4px}.bible-image-review-entity>header small{color:#7d8992;font-size:10px}.bible-image-review-entity>header code{overflow:hidden;color:#6e7d88;font-size:9px;text-overflow:ellipsis}.bible-image-review-actions{justify-items:end;max-width:55%}.bible-image-review-actions code{max-width:100%}.bible-image-source-heading{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding-top:3px;border-top:1px solid #202a31}.bible-image-source-heading strong{color:#bdc6cc;font-size:10px}.bible-image-source-heading small{color:#717e87;font-size:9px}.bible-image-review-candidates{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}.bible-image-review-candidates article{display:grid;gap:8px;padding:8px;border:1px solid #28323b;border-radius:8px;background:#090d11}.bible-image-review-candidates article.selected{border-color:#5b4c86;box-shadow:0 0 0 1px rgba(146,114,220,.18)}.bible-library-candidates article.selected{border-color:#a2773e;box-shadow:0 0 0 1px rgba(215,155,75,.18)}.bible-image-review-candidates img,.bible-image-review-placeholder{width:100%;aspect-ratio:1/1;border-radius:6px;background:#11171c;object-fit:cover}.bible-image-review-placeholder{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:7px;color:#7b858d;font-size:10px}.bible-image-review-candidates label{display:flex;align-items:center;gap:7px;color:#c4cbd0;font-size:11px}.bible-image-review-candidates small{color:#7d8992;font-size:9px}.bible-image-review-candidates code{overflow:hidden;color:#687682;font-size:8px;text-overflow:ellipsis}.bible-library-controls,.bible-library-pagination{display:flex;align-items:end;justify-content:space-between;gap:12px}.bible-library-controls .platform-field{flex:1;max-width:520px}.bible-library-pagination{align-items:center}.bible-library-pagination small{color:#7d8992;font-size:9px}.bible-library-empty{padding:14px;border:1px dashed #29343c;border-radius:7px}.bible-library-empty a{margin-left:5px;color:#d09a53}@media(max-width:720px){.bible-image-review-entity>header,.bible-library-controls{display:grid}.bible-image-review-actions{justify-items:start;max-width:100%}.bible-library-controls .platform-field{max-width:none}}
.storyboard-reference-review>header{align-items:flex-start}.storyboard-reference-episode{display:grid;gap:12px;padding:14px;border:1px solid #28333d;border-radius:9px;background:#0a0f13}.storyboard-reference-episode>header small{display:block;margin-top:4px;color:#7d8992;font-size:10px}.storyboard-reference-shot{display:grid;gap:12px;padding:12px;border:1px solid #222d35;border-radius:8px;background:#080c10}.storyboard-reference-shot>header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.storyboard-reference-shot>header>div{display:grid;gap:4px}.storyboard-reference-shot>header small{color:#7d8992;font-size:10px}.storyboard-reference-shot>header code{max-width:45%;overflow:hidden;color:#687682;font-size:9px;text-overflow:ellipsis}.storyboard-reference-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}.storyboard-media-binding{display:grid;grid-column:1/-1;gap:10px;padding:12px;border:1px solid #27343d;border-radius:8px;background:#0a0f13}.storyboard-media-binding>header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.storyboard-media-binding>header>div{display:grid;gap:4px}.storyboard-media-binding>header small{color:#7d8992;font-size:10px;line-height:1.5}.file-button{position:relative;overflow:hidden}.file-button input{position:absolute;inset:0;cursor:pointer;opacity:0}.storyboard-media-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:7px}.storyboard-media-options>label{display:grid;grid-template-columns:auto 1fr;gap:3px 7px;padding:8px;border:1px solid #27323a;border-radius:6px;color:#bdc5ca;font-size:10px}.storyboard-media-options>label small{grid-column:2;color:#74808a;font-size:9px}.storyboard-media-options>label.invalid{opacity:.5}.storyboard-video-binding{display:grid;gap:9px;padding:10px;border:1px solid rgba(230,167,90,.18);border-radius:7px;background:rgba(230,167,90,.025)}.storyboard-video-binding>header{display:flex;align-items:center;justify-content:space-between;gap:10px}.storyboard-video-binding>header strong{overflow:hidden;color:#c6b292;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.storyboard-media-controls,.storyboard-audio-bindings{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:9px}.storyboard-reference-previews{display:flex;grid-column:1/-1;gap:10px;overflow-x:auto}.storyboard-reference-previews figure{display:grid;flex:0 0 130px;gap:5px;margin:0}.storyboard-reference-previews img,.storyboard-media-kind{width:130px;aspect-ratio:1/1;border-radius:6px;object-fit:cover}.storyboard-media-kind{display:flex;align-items:center;justify-content:center;border:1px solid #2b3841;background:#111820;color:#708491;font:10px Consolas,monospace}.storyboard-reference-previews figcaption{overflow:hidden;color:#89949d;font-size:9px;text-overflow:ellipsis;white-space:nowrap}
.unattended-run-panel{grid-column:1/-1}.unattended-reference-editor{display:grid;gap:9px}.unattended-reference-editor-heading,.unattended-reference-row>header{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}.unattended-reference-editor-heading>div{display:grid;gap:4px}.unattended-reference-editor-heading small,.unattended-reference-empty{margin:0;color:#7f8a93;font-size:9px;line-height:1.5}.unattended-reference-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:8px;padding:10px;border:1px solid #28333d;border-radius:7px;background:#0a0f13}.unattended-reference-row>header{grid-column:1/-1}.unattended-reference-row>.platform-checkbox{grid-column:1/-1}.unattended-contract-facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0}.unattended-contract-facts>div{display:grid;gap:4px;padding:9px;border:1px solid #28333d;border-radius:7px;background:#0a0f13}.unattended-contract-facts dt{color:#73808a;font-size:9px}.unattended-contract-facts dd{margin:0;color:#c4cbd0;font-size:10px;overflow-wrap:anywhere}.unattended-confirmation{align-items:flex-start;padding:11px;border:1px solid rgba(230,167,90,.3);border-radius:7px;background:rgba(230,167,90,.04)}@media(max-width:720px){.unattended-reference-row,.unattended-contract-facts{grid-template-columns:1fr}}
.stage-policy-evidence{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 9px;padding:8px;border:1px solid #28333d;border-radius:7px;background:#0a0f13}.stage-policy-evidence strong{font-size:10px}.stage-policy-evidence code{overflow-wrap:anywhere}.stage-policy-evidence small{grid-column:1/-1;color:#73808a;font-size:9px;overflow-wrap:anywhere}.stage-policy-decisions{padding:8px;border:1px solid #28333d;border-radius:7px}.stage-policy-decisions summary{cursor:pointer;font-size:10px}.stage-policy-decisions article{display:grid;gap:3px;padding-top:8px}.stage-policy-decisions article small,.stage-policy-decisions article code{color:#73808a;font-size:9px;overflow-wrap:anywhere}
.manual-bible-review{align-content:start}.manual-bible-review .platform-alert>div{display:grid;gap:4px}.manual-bible-review .platform-alert p{margin:0}.manual-bible-counts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0}.manual-bible-counts>div{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px;border:1px solid rgba(77,159,118,.3);border-radius:7px;background:rgba(77,159,118,.05)}.manual-bible-counts>div.missing{border-color:rgba(226,163,80,.38);background:rgba(226,163,80,.06)}.manual-bible-counts dt{color:#8d99a2;font-size:10px}.manual-bible-counts dd{margin:0;color:#d8e0e5;font-weight:700}.manual-bible-link{justify-self:start}@media(max-width:620px){.manual-bible-counts{grid-template-columns:1fr}}
</style>
