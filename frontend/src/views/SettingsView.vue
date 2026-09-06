<script setup lang="ts">
import { workflowResolutionSummary } from '@/services/workflowResolution'
import WorkflowResolutionInspector from '@/components/WorkflowResolutionInspector.vue'
import WorkflowConfigurationLinks from '@/components/WorkflowConfigurationLinks.vue'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { LocationQueryRaw } from 'vue-router'
import { AlertTriangle, Archive, BookOpenCheck, Boxes, Check, ChevronDown, Copy, Cpu, Download, Eye, FileClock, Gauge, GitBranch, KeyRound, LoaderCircle, Play, Plus, Power, RefreshCw, Save, Server, Settings2, ShieldAlert, ShieldCheck, Trash2 } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import ConfigValueEditor from '@/components/ConfigValueEditor.vue'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import ProductionRecipeFlow from '@/components/ProductionRecipeFlow.vue'
import RegistryRelationSummary from '@/components/RegistryRelationSummary.vue'
import { useRegistryRelations, relationOverviewUrl, relationKinds, type RelationKind } from '@/services/registryRelations'
import PromptProfileSettings from '@/components/PromptProfileSettings.vue'
import ModelPromptBindings from '@/components/ModelPromptBindings.vue'
import RegistryResourceEditor from '@/components/RegistryResourceEditor.vue'
import RegistryDeletionReviewModal from '@/components/RegistryDeletionReviewModal.vue'
import SettingsListFilter from '@/components/SettingsListFilter.vue'
import SettingsPrimaryNav from '@/components/SettingsPrimaryNav.vue'
import WorkflowProjectorDefinitionEditor from '@/components/WorkflowProjectorDefinitionEditor.vue'
import WorkflowProjectorRecommendationStep from '@/components/WorkflowProjectorRecommendationStep.vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { parseConfigValue, type ConfigResourceOption as ConfigEditorResourceOption } from '@/services/configValueSchema'
import { platformApi } from '@/services/platformApi'
import { recipeWorkflowVersionIds } from '@/services/productionRecipeFlow'
import { repository, type ProductionCapability } from '@/services/repository'
import { assertSavedWorkflowBatchResultMatchesRequest, groupSavedWorkflowBatches, mergeSavedWorkflowBatchResults, type SavedWorkflowBatchAggregate } from '@/services/savedWorkflowBatch'
import { classifySavedWorkflowModelContext, type SavedWorkflowModelContext, type SavedWorkflowModelContextKind } from '@/services/savedWorkflowModelContext'
import {
  readSavedWorkflowDraftSession,
  attestationMatchesSavedWorkflowReview,
  createSavedWorkflowHydrationGuard,
  reconcileSavedWorkflowReviewHashes,
  savedWorkflowDraftMatchesSource,
  SAVED_WORKFLOW_STORAGE_ERROR,
  writeSavedWorkflowDraftSession,
  type SavedWorkflowDraftSessionItem,
} from '@/services/savedWorkflowDraftSession'
import { applyResourceDefaults, buildRegistryResourcePayload, promptNormalizationValidationErrors, type RegistryResource } from '@/services/registryResourceSchema'
import { runtimeEvidenceState } from '@/services/runtimeHostSelection'
import { buildSystemConfigChangeSet, effectiveSourceLabel } from '@/services/settingsRegistry'
import { filterSettingsList } from '@/services/settingsListFilter'
import { cloneWorkflowProjectorDefinition } from '@/services/workflowProjectorDefinition'
import { commitLatestRequest } from '@/services/latestRequestCommit'
import { buildModelRegistryFamilies, modelArtifactAvailabilityEvidence, modelDeploymentWorkflowSmokeUrl, type ModelRegistryFamily, type ModelRegistryVersionGroup } from '@/services/modelRegistryPresentation'
import ModelDeploymentExecutionSummary from '@/components/ModelDeploymentExecutionSummary.vue'
import ModelCapabilityContract from '@/components/ModelCapabilityContract.vue'
import { useModelPromptBindings } from '@/services/useModelPromptBindings'
import type { AcceptanceSmokeDeploymentRow, ComfySavedWorkflowBatchItemInput, ComfySavedWorkflowItem, ComfySavedWorkflowPreview, ConfigDefinition, ConfigDefinitionOptions, ConfigRevision, HostCapabilitySnapshot, JsonObject, LegacyWorkflowTemplate, ModelArtifact, ModelDeployment, ModelDeploymentSmokeAttempt, ModelProfileRegistryOptions, ModelProfileVersion, PlatformLifecycleAuditEvent, ProductionReadiness, ProductionRecipe, ProductionRecipeEditorOptions, ProviderProfile, RecommendationCandidate, RecommendationSnapshot, RegistryDeletionResourceType, ResolvedConfiguration, RuntimeHost, RuntimeHostProbeAttempt, RuntimeReadiness, SecretAuditEvent, SecretBinding, SmokeResolutionOutcome, WorkflowProjector, WorkflowProjectorOptions, WorkflowProjectorRecommendation, WorkflowProjectorVersion, WorkflowTemplateCanvasExport, WorkflowTemplateVersion } from '@/services/platformTypes'
import { useAuthStore } from '@/stores/auth'
import { settingsSectionCatalog, settingsSectionById, type SettingsSectionId } from '@/content/settingsSectionCatalog'
import { artifactRoleLabel, driverLabel, freshnessLabel, mediaKindLabel, mediaKindListLabel, operationLabel, operationListLabel, reasonCodeLabel, reasonCodeTitle, scopeLabel, valueTypeLabel } from '@/services/presentationLabels'

type SettingsTab = SettingsSectionId
type RegistryEntity = ProviderProfile | ModelProfileVersion | ModelDeployment | ModelArtifact | WorkflowTemplateVersion | WorkflowProjector | WorkflowProjectorVersion | ProductionRecipe | ConfigDefinition | ConfigRevision | RuntimeHost | HostCapabilitySnapshot | SecretBinding | SecretAuditEvent | PlatformLifecycleAuditEvent | ModelDeploymentSmokeAttempt
type RegistryAction = 'publish-model' | 'retire-model' | 'delete-provider' | 'delete-model' | 'deployment-status' | 'retire-deployment' | 'delete-deployment' | 'delete-artifact' | 'verify-artifact' | 'delete-workflow' | 'retire-workflow' | 'workflow-projector-status' | 'publish-workflow-projector' | 'discard-workflow-projector' | 'retire-workflow-projector' | 'publish-recipe' | 'delete-recipe' | 'retire-recipe' | 'delete-definition' | 'save-defaults' | 'publish-revision' | 'activate-revision' | 'host-status' | 'deployment-probe' | 'deployment-smoke'
const route = useRoute(); const router = useRouter()
const auth = useAuthStore()
const tabs = settingsSectionCatalog
const allowedTabs = new Set(tabs.map((item) => item.id)); const initialTab = String(route.query.section || 'models')
const tab = ref<SettingsTab>(allowedTabs.has(initialTab as SettingsTab) ? initialTab as SettingsTab : 'models')
const currentSettingsSection = computed(() => settingsSectionById(tab.value))
const currentSectionWriteRequiresSuperuser = computed(() => currentSettingsSection.value.writeRequiresSuperuser === true)
const canWriteCurrentSection = computed(() => !currentSectionWriteRequiresSuperuser.value || auth.user?.isSuperuser === true)
const settingsGuideHref = computed(() => `/manual/settings#${currentSettingsSection.value.guideAnchor}`)
const loading = ref(false); const saving = ref(false); const error = ref(''); const notice = ref('')
const sectionErrors = ref<Record<string, string>>({})
const providers = ref<ProviderProfile[]>([]); const modelProfiles = ref<ModelProfileVersion[]>([]); const deployments = ref<ModelDeployment[]>([]); const modelArtifacts = ref<ModelArtifact[]>([])
const deploymentAcceptanceRows = ref<Record<string, AcceptanceSmokeDeploymentRow> | null>(null)
const modelPromptContext = ref<{ profile: ModelProfileVersion; deployment: ModelDeployment | null } | null>(null)
watch(() => route.fullPath, () => { modelPromptContext.value = null })
const modelProfileOptions = ref<ModelProfileRegistryOptions | null>(null)
const workflowVersions = ref<WorkflowTemplateVersion[]>([]); const legacyWorkflowTemplates = ref<LegacyWorkflowTemplate[]>([]); const recipes = ref<ProductionRecipe[]>([])
const workflowProjectors = ref<WorkflowProjector[]>([])
const workflowProjectorVersions = ref<WorkflowProjectorVersion[]>([])
const workflowProjectorOptions = ref<WorkflowProjectorOptions | null>(null)
const workflowProjectorBusy = ref('')
const workflowProjectorError = ref('')
const workflowProjectorCatalogElement = ref<HTMLDetailsElement | null>(null)
const workflowProjectorCatalogOpen = ref(true)
const workflowProjectorEditorOpen = ref(false)
const workflowProjectorEditorVersion = ref<WorkflowProjectorVersion | null>(null)
const workflowProjectorReadOnly = ref(false)
const workflowProjectorDefinition = ref<JsonObject>({})
const workflowProjectorDefinitionValidity = ref({ valid: false, errors: [] as string[] })
const workflowProjectorEditorDraft = reactive({
  engine: '',
  providerDriverType: '',
  operation: '',
  outputKind: '' as WorkflowProjectorVersion['outputKind'],
})
const workflowProjectorCreateOpen = ref(false)
const workflowProjectorCreateMode = ref<'recommendation' | 'copy' | 'blank'>('recommendation')
const workflowProjectorCreateModes = [
  { key: 'recommendation', label: '从工作流推荐', help: '基于已扫描候选生成配置', recommended: true },
  { key: 'copy', label: '复制已有方案', help: '复制一个版本后独立调整' },
  { key: 'blank', label: '空白新建', help: '选择引擎后手动配置' },
] as const
const workflowProjectorCreateCandidateKey = ref('')
const workflowProjectorResumeFromScan = ref(false)
const workflowProjectorCreateFamilyId = ref('')
const workflowProjectorCreateSeedVersionId = ref('')
const workflowProjectorCreateAppliedSeedId = ref('')
const workflowProjectorCreateSeedFingerprint = ref('')
const workflowProjectorCreateDefinition = ref<JsonObject>({})
const workflowProjectorCreateDefinitionValidity = ref({ valid: false, errors: [] as string[] })
const workflowProjectorRecommendation = ref<WorkflowProjectorRecommendation | null>(null)
const workflowProjectorRecommendationSourcePath = ref('')
const workflowProjectorCreateFamily = reactive({
  key: '',
  name: '',
  description: '',
})
const workflowProjectorCreateVersion = reactive({
  engine: '',
  providerDriverType: '',
  operation: '',
  outputKind: '' as WorkflowProjectorVersion['outputKind'],
})
const recipeEditorOptions = ref<ProductionRecipeEditorOptions | null>(null)
const recipeWorkflowDetails = ref<Record<string, WorkflowTemplateVersion>>({})
const recipeWorkflowDetailStates = ref<Record<string, 'loading' | 'loaded' | 'error'>>({})
const recipeWorkflowDetailErrors = ref<Record<string, string>>({})
const recipeWorkflowCatalog = computed(() => {
  const byId = new Map(workflowVersions.value.map((item) => [item.id, item]))
  for (const item of Object.values(recipeWorkflowDetails.value)) byId.set(item.id, item)
  return [...byId.values()]
})
const workflowExportOpen = ref(false); const workflowExportBusyId = ref(''); const workflowExportError = ref(''); const workflowExport = ref<WorkflowTemplateCanvasExport | null>(null)
const workflowExportVersion = ref<WorkflowTemplateVersion | null>(null); const workflowExportDeploymentId = ref('')
type SavedWorkflowDraft = {
  selected: boolean
  importKind: 'template_version' | 'execution_snapshot' | 'ignored'
  primaryOutputNodeId: string
  modelProfileVersionId: string
  useExistingTemplate: boolean
  templateId: string
  newTemplateName: string
  newTemplateSlug: string
  outputKind: 'image' | 'video' | 'audio' | 'text'
  reviewObjectInfoSchemaSha256: string
  reviewConvertedGraphSha256: string
  reviewParameterSchemaSha256: string
  reviewStructuralContractSha256: string
  reviewRequiresConfirmation: boolean
  projectorSuccessorRequested: boolean
  preview: ComfySavedWorkflowPreview | null
  previewError: string
  attestation: RuntimeHostProbeAttempt | null
}
const savedWorkflowItems = ref<ComfySavedWorkflowItem[]>([])
const showOtherSavedWorkflowResults = ref(false)
const savedWorkflowSources = ref<Array<{
  sourceScopeSha256: string
  sourceDeploymentId: string
  sourceLabel: string
  deploymentCount: number
  status: 'succeeded' | 'failed'
  itemCount: number
  errorCode: string
  errorMessage: string
}>>([])
const savedWorkflowScanState = ref<'idle' | 'loading' | 'fresh' | 'partial' | 'failed'>('idle')
const savedWorkflowScanActionable = computed(() => ['fresh', 'partial'].includes(savedWorkflowScanState.value))
const savedWorkflowBatchMaxItems = ref<number | null>(null)
const savedWorkflowDrafts = reactive<Record<string, SavedWorkflowDraft>>({})
const savedWorkflowBusy = ref('')
const savedWorkflowResult = ref<SavedWorkflowBatchAggregate | null>(null)
const savedWorkflowError = ref('')
const savedWorkflowFocusedIssueKey = ref('')
const savedWorkflowAttestationTimers = new Map<string, number>()
const savedWorkflowAttestationAttempts = ref<RuntimeHostProbeAttempt[]>([])
const savedWorkflowHydrationGuard = createSavedWorkflowHydrationGuard()
const savedWorkflowPreservedSessionItems = new Map<string, SavedWorkflowDraftSessionItem>()
const definitions = ref<ConfigDefinition[]>([]); const revisions = ref<ConfigRevision[]>([]); const resolved = ref<ResolvedConfiguration | null>(null); const configDraft = ref<Record<string, string>>({})
const configDefinitionOptions = ref<ConfigDefinitionOptions | null>(null)
const savedWorkflowDirectoryConfigKey = 'workflow.comfyui_saved_workflow_directory'
const savedWorkflowScanSecondsConfigKey = 'security.comfy.import.scan_max_seconds'
const savedWorkflowScanItemsConfigKey = 'security.comfy.import.scan_max_items'
const savedWorkflowRequestBytesConfigKey = 'security.comfy.import.request_max_bytes'
const savedWorkflowDirectory = computed(() => {
  const value = resolved.value?.values[savedWorkflowDirectoryConfigKey]
  return typeof value === 'string' ? value : ''
})
const savedWorkflowScanMaxSeconds = computed(() => {
  const value = resolved.value?.values[savedWorkflowScanSecondsConfigKey]
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
})
const savedWorkflowScanMaxItems = computed(() => {
  const value = resolved.value?.values[savedWorkflowScanItemsConfigKey]
  return Number.isSafeInteger(value) && Number(value) > 0 ? Number(value) : null
})
const savedWorkflowRequestMaxBytes = computed(() => {
  const value = resolved.value?.values[savedWorkflowRequestBytesConfigKey]
  return Number.isSafeInteger(value) && Number(value) > 0 ? Number(value) : null
})
const configResourceOptions = ref<Record<string, ConfigEditorResourceOption[]>>({})
const configOriginal = ref<Record<string, string>>({}); const configDirty = ref(new Set<string>()); const configUnsets = ref(new Set<string>())
const settingsListSearch = ref(''); const showRetiredSettingsRecords = ref(false)
type WorkflowModelContextState = 'none' | 'loading' | 'invalid' | 'missing' | 'unavailable' | 'provider_inactive' | 'not_applicable' | 'ready'
const workflowModelContextCatalogLoaded = ref(false)
const workflowModelContextRaw = computed(() => route.query.model_profile_version)
const workflowModelContextRequested = computed(() => workflowModelContextRaw.value !== undefined)
const workflowModelContextQueryError = computed(() => {
  if (!workflowModelContextRequested.value) return ''
  const raw = workflowModelContextRaw.value
  if (typeof raw !== 'string' || !raw.trim()) return '模型版本上下文必须是唯一且非空的 ModelProfileVersion UUID。'
  if (!/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(raw.trim())) return '模型版本上下文不是有效的 ModelProfileVersion UUID。'
  return ''
})
const workflowModelContextId = computed(() => workflowModelContextQueryError.value ? '' : String(workflowModelContextRaw.value || '').trim().toLocaleLowerCase('en-US'))
const workflowModelContextProfile = computed(() => (
  workflowModelContextId.value
    ? modelProfiles.value.find((item) => item.id === workflowModelContextId.value) || null
    : null
))
const workflowModelContextProvider = computed(() => {
  const profile = workflowModelContextProfile.value
  return profile ? providers.value.find((item) => item.id === profile.provider) || null : null
})
const workflowModelContextState = computed<WorkflowModelContextState>(() => {
  if (!workflowModelContextRequested.value) return 'none'
  if (workflowModelContextQueryError.value) return 'invalid'
  if (!workflowModelContextCatalogLoaded.value || loading.value) return 'loading'
  if (sectionErrors.value.modelProfiles || sectionErrors.value.providers || sectionErrors.value.workflows) return 'unavailable'
  if (!workflowModelContextProfile.value) return 'missing'
  if (!workflowModelContextProvider.value) return 'unavailable'
  if (!workflowModelContextProvider.value.isActive) return 'provider_inactive'
  if (workflowModelContextProvider.value.driverType !== 'comfyui') return 'not_applicable'
  return 'ready'
})
const workflowModelContextFailure = computed(() => {
  if (workflowModelContextState.value === 'invalid') return `${workflowModelContextQueryError.value} 页面已停止读取工作流列表，不会回退到全部模型、同族版本或名称相似版本。`
  if (workflowModelContextState.value === 'missing') return `没有找到精确模型版本 ${workflowModelContextId.value}；页面不会改选其他版本。`
  if (workflowModelContextState.value === 'unavailable') {
    if (sectionErrors.value.modelProfiles) return `模型版本目录读取失败：${sectionErrors.value.modelProfiles}。工作流上下文已失败关闭。`
    if (sectionErrors.value.providers) return `Provider 目录读取失败：${sectionErrors.value.providers}。工作流上下文已失败关闭。`
    if (sectionErrors.value.workflows) return `精确工作流列表读取失败：${sectionErrors.value.workflows}。页面不会把空结果伪装成没有关联。`
    return '模型版本引用的 Provider 不存在；工作流上下文已失败关闭。'
  }
  if (workflowModelContextState.value === 'provider_inactive') return `Provider ${workflowModelContextProvider.value?.name || workflowModelContextProvider.value?.key} 已停用；历史关联仍可读取，但扫描、证明、验收和生命周期操作已关闭。`
  if (workflowModelContextState.value === 'not_applicable') return `当前模型使用 ${driverLabel(workflowModelContextProvider.value?.driverType || '')}，不适用 ComfyUI 工作流关联。`
  return ''
})
const workflowModelContextCanImport = computed(() => (
  workflowModelContextCatalogLoaded.value
  && !sectionErrors.value.workflows
  && !sectionErrors.value.modelProfiles
  && !sectionErrors.value.providers
  && (
    workflowModelContextState.value === 'none'
    || (workflowModelContextState.value === 'ready' && ['draft', 'published'].includes(workflowModelContextProfile.value?.status || ''))
  )
))
const workflowModelContextReadOnly = computed(() => (
  workflowModelContextState.value === 'provider_inactive'
  || (workflowModelContextState.value === 'ready' && workflowModelContextProfile.value?.status === 'retired')
))
const workflowVersionListOptions = () => workflowModelContextId.value
  ? { modelProfileVersion: workflowModelContextId.value }
  : {}
const savedWorkflowModelContext = computed<SavedWorkflowModelContext | null>(() => {
  const profile = workflowModelContextState.value === 'ready' ? workflowModelContextProfile.value : null
  if (!profile) return null
  return {
    modelProfileVersionId: profile.id,
    providerId: profile.provider,
    workflowVersionIds: new Set(workflowVersions.value.map((item) => item.id.toLocaleLowerCase('en-US'))),
  }
})
const savedWorkflowContextKind = (item: ComfySavedWorkflowItem): SavedWorkflowModelContextKind => {
  const context = savedWorkflowModelContext.value
  return context ? classifySavedWorkflowModelContext(item, context) : 'exact'
}
const savedWorkflowIsCurrentModelItem = (item: ComfySavedWorkflowItem) => savedWorkflowContextKind(item) !== 'diagnostic'
const savedWorkflowIsContextDiagnostic = (item: ComfySavedWorkflowItem) => Boolean(
  savedWorkflowModelContext.value && !savedWorkflowIsCurrentModelItem(item),
)
const savedWorkflowIsContextReadOnly = (item: ComfySavedWorkflowItem) => Boolean(
  savedWorkflowModelContext.value
  && savedWorkflowContextKind(item) === 'diagnostic',
)
const currentModelSavedWorkflowItems = computed(() => (
  savedWorkflowModelContext.value
    ? savedWorkflowItems.value.filter(savedWorkflowIsCurrentModelItem)
    : savedWorkflowItems.value
))
const otherSavedWorkflowItems = computed(() => (
  savedWorkflowModelContext.value
    ? savedWorkflowItems.value.filter((item) => !savedWorkflowIsCurrentModelItem(item))
    : []
))
const visibleSavedWorkflowItems = computed(() => (
  savedWorkflowModelContext.value && !showOtherSavedWorkflowResults.value
    ? currentModelSavedWorkflowItems.value
    : savedWorkflowItems.value
))
const currentModelSavedWorkflowExactCount = computed(() => currentModelSavedWorkflowItems.value.filter(
  (item) => savedWorkflowContextKind(item) === 'exact',
).length)
const currentModelSavedWorkflowEligibleCount = computed(() => currentModelSavedWorkflowItems.value.filter(
  (item) => savedWorkflowContextKind(item) === 'eligible_candidate',
).length)
const currentModelSavedWorkflowReviewCount = computed(() => currentModelSavedWorkflowItems.value.filter(
  (item) => savedWorkflowContextKind(item) === 'review_candidate',
).length)
const currentModelSavedWorkflowContractBlockedCount = computed(() => currentModelSavedWorkflowItems.value.filter(
  (item) => savedWorkflowContextKind(item) === 'contract_blocked_candidate',
).length)
const savedWorkflowContextKindLabel = (item: ComfySavedWorkflowItem) => ({
  exact: '精确关联',
  eligible_candidate: '证据兼容候选',
  review_candidate: '合同兼容 · Live Validate 待就绪',
  contract_blocked_candidate: '当前模型候选 · 结构合同受阻',
  diagnostic: '其他扫描结果 · 只读诊断',
}[savedWorkflowContextKind(item)])
const providerManagementOpen = ref(false)
const selectedModelFamilyKey = ref('')
const selectedModelProfileId = ref('')
const expandedRecipeIds = ref(new Set<string>())
const readinessReasonFilter = ref(''); const recipeListAnchor = ref<HTMLElement | null>(null)
const secretBindings = ref<SecretBinding[]>([]); const secretAudit = ref<SecretAuditEvent[]>([])
const lifecycleAudit = ref<PlatformLifecycleAuditEvent[]>([]); const lifecycleObjectFilter = ref(''); const lifecycleActionFilter = ref('')
const secretOpen = ref(false); const secretMode = ref<'create'|'rotate'|'status'>('create'); const secretTarget = ref<SecretBinding | null>(null)
const secretError = ref('')
const secretDraft = reactive({ key: '', label: '', purpose: 'provider_credential', secretValue: '', reason: '', isActive: true })
const runtimeHosts = ref<RuntimeHost[]>([]); const selectedHostId = ref(''); const snapshot = ref<HostCapabilitySnapshot | null>(null); const readiness = ref<RuntimeReadiness | null>(null); const recommendation = ref<RecommendationSnapshot | null>(null); const runtimeBusy = ref('')
const runtimeProbeAttempts = ref<Record<string, RuntimeHostProbeAttempt>>({}); const runtimeProbeErrors = ref<Record<string, string>>({}); const runtimeProbeBusyId = ref(''); const runtimeProbePollInFlight = ref(false); let runtimeProbePollTimer = 0
const latestHostSnapshots = ref<Record<string, HostCapabilitySnapshot>>({})
const latestHostSnapshotStates = ref<Record<string, { state: 'ready'|'missing'|'error'; message: string }>>({})
const selectedSnapshotState = ref<'idle'|'ready'|'missing'|'error'>('idle')
const productionReadiness = ref<ProductionReadiness | null>(null)
const runtimeEvidenceNow = ref(Date.now()); let runtimeEvidenceTimer = 0
type RegistryEditorIntent = 'create' | 'edit' | 'next-model-version' | 'create-child'
const editorOpen = ref(false); const editorResource = ref<RegistryResource>('provider'); const editorId = ref(''); const editorPayload = ref<JsonObject>({}); const editorOriginalPayload = ref<JsonObject>({}); const editorError = ref('')
const editorIntent = ref<RegistryEditorIntent>('create'); const editorSourceId = ref('')
const editorConfigHasValues = ref(false); const editorConfigMutableFields = ref<string[]>([])
const editorValidity = ref<{ valid: boolean; errors: string[]; warnings: string[] }>({ valid: false, errors: [], warnings: [] })
const editorCredentialId = ref(''); const secretSelections = ref<Record<string, string>>({})
const detailOpen = ref(false); const detailKind = ref(''); const detailTitle = ref(''); const detailEntity = ref<unknown>(null)
const detailRequestKey = ref('')
const actionOpen = ref(false); const actionKind = ref<RegistryAction>('publish-model'); const actionId = ref(''); const actionTitle = ref(''); const actionDescription = ref(''); const actionError = ref(''); const actionOperation = ref(''); const actionReferenceId = ref(''); const actionEnable = ref(false)
const actionReasonCode = ref('')
const deploymentSmokeAttempts = ref<Record<string, ModelDeploymentSmokeAttempt>>({}); const deploymentSmokeErrors = ref<Record<string, string>>({}); const deploymentSmokePollInFlight = ref(false); const deploymentSmokeBusyId = ref(''); let deploymentSmokePollTimer = 0
const smokeResolutionDeployment = ref<ModelDeployment | null>(null)
const smokeResolutionOutcome = ref<SmokeResolutionOutcome>('confirmed_not_submitted')
const smokeResolutionNote = ref('')
const smokeResolutionProviderRequestId = ref('')
const smokeResolutionActualCost = ref('')
const smokeResolutionCurrency = ref('')
const smokeResolutionError = ref('')
const smokeResolutionSaving = ref(false)
const productionCapability = ref<ProductionCapability | null>(null)
const productionCapabilityError = ref('')
const decisionOpen = ref(false); const decisionCandidate = ref<RecommendationCandidate | null>(null); const decisionKind = ref<'accept'|'override'|'reject'>('accept'); const decisionOverrideText = ref('{}'); const decisionReason = ref('系统设置中心人工决策'); const decisionError = ref('')
const editorTemplates: Record<RegistryResource, JsonObject> = {
  provider: { key: '', name: '', driver_type: '', modalities: '', operations: '', driver_config: {}, is_active: true },
  model: { profile_key: '', provider: '', model_identifier: '', modality: '', specification: {} },
  deployment: { key: '', name: '', profile_version: '', runtime_host: null, runtime_config: {}, priority: 100, is_default: false, status: 'active' },
  artifact: { profile_version: '', role: '', logical_name: '', filename: '', inventory_folder: '', inventory_node_type: '', inventory_input_name: '', storage_uri: '', sha256: '', size_bytes: null, status: 'declared' },
  recipe: { recipe_key: '', name: '', definition: { stages: [] } },
  'config-definition': { key: '', domain: '', description: '', impact_modules: [], value_schema: {}, initial_value: null },
}
const bytes = (value: number | null | undefined) => { const amount = Number(value); if (!Number.isFinite(amount)) return '—'; if (amount >= 1024 ** 3) return `${(amount / 1024 ** 3).toFixed(1)} GB`; if (amount >= 1024 ** 2) return `${(amount / 1024 ** 2).toFixed(1)} MB`; if (amount >= 1024) return `${(amount / 1024).toFixed(1)} KB`; return `${amount} B` }
type PromptNormalizationCapacity = { maxInputBytes: number; inputByteWarningThreshold: number | null; maxOutputTokens: number; outputTokenWarningThreshold: number | null; maxItemsPerBatch: number; timeoutSeconds: number }
const promptNormalizationCapacity = (profile: ModelProfileVersion): PromptNormalizationCapacity | null => {
  const capabilities = profile.specification?.capabilities
  if (!Array.isArray(capabilities) || !capabilities.includes('prompt.normalize')) return null
  const value = profile.specification?.prompt_normalization
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const contract = value as JsonObject
  if (promptNormalizationValidationErrors(contract).length) return null
  const maxInputBytes = contract.max_input_bytes
  const inputByteWarningThreshold = contract.input_byte_warning_threshold
  const maxOutputTokens = contract.max_output_tokens
  const outputTokenWarningThreshold = contract.output_token_warning_threshold
  const maxItemsPerBatch = contract.max_items_per_batch
  const timeoutSeconds = contract.timeout_seconds
  const isFiniteNumber = (candidate: unknown): candidate is number => (
    typeof candidate === 'number' && Number.isFinite(candidate)
  )
  if (!isFiniteNumber(maxInputBytes)
    || !isFiniteNumber(maxOutputTokens)
    || !isFiniteNumber(maxItemsPerBatch)
    || !isFiniteNumber(timeoutSeconds)) return null
  if (inputByteWarningThreshold !== undefined && !isFiniteNumber(inputByteWarningThreshold)) return null
  if (outputTokenWarningThreshold !== undefined && !isFiniteNumber(outputTokenWarningThreshold)) return null
  return { maxInputBytes, inputByteWarningThreshold: inputByteWarningThreshold ?? null, maxOutputTokens, outputTokenWarningThreshold: outputTokenWarningThreshold ?? null, maxItemsPerBatch, timeoutSeconds }
}
const promptNormalizationCapacitySummary = (profile: ModelProfileVersion): string => {
  const capabilities = profile.specification?.capabilities
  if (!Array.isArray(capabilities) || !capabilities.includes('prompt.normalize')) return ''
  const capacity = promptNormalizationCapacity(profile)
  if (!capacity) return 'prompt.normalize · 容量合同缺失或无效'
  return `prompt.normalize · ${capacity.maxInputBytes} B input hard${capacity.inputByteWarningThreshold === null ? '' : ` / ${capacity.inputByteWarningThreshold} B input advisory`} / ${capacity.maxItemsPerBatch} items / ${capacity.maxOutputTokens} tokens hard${capacity.outputTokenWarningThreshold === null ? '' : ` / ${capacity.outputTokenWarningThreshold} tokens advisory`} / ${capacity.timeoutSeconds}s`
}
const dateTime = (value: string) => value ? new Date(value).toLocaleString('zh-CN') : '—'
const compactDuration = (milliseconds: number) => {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return '未知'
  const seconds = Math.round(milliseconds / 1000)
  const minutes = Math.floor(seconds / 60)
  return minutes ? `${minutes} 分 ${seconds % 60} 秒` : `${seconds} 秒`
}
const runtimeProbeDurationLabel = (attempt: RuntimeHostProbeAttempt) => {
  if (!attempt.startedAt) return '尚未开始'
  const startedAt = Date.parse(attempt.startedAt)
  const endedAt = attempt.finishedAt ? Date.parse(attempt.finishedAt) : Date.now()
  return `${attempt.finishedAt ? '耗时' : '已运行'} ${compactDuration(endedAt - startedAt)}`
}
const message = (cause: unknown) => apiErrorSummary(cause, '请求失败，服务端没有返回可读错误')
const copyObservedHash = async (value: string) => {
  if (!value) return
  try { await navigator.clipboard.writeText(value); notice.value = '已复制完整 SHA-256。' }
  catch (cause) { error.value = `复制 SHA-256 失败：${message(cause)}` }
}
const pretty = (value: unknown) => JSON.stringify(value, null, 2) ?? ''; const sourceFor = (key: string) => resolved.value?.sources[key]
type CaptureRequest = <T>(key: string, promise: Promise<T>, commit: (value: T) => void) => Promise<void>
const capture = async <T>(key: string, promise: Promise<T>, commit: (value: T) => void, isCurrent: () => boolean = () => true) => {
  await commitLatestRequest(
    promise,
    isCurrent,
    (value) => {
      commit(value)
      const next = { ...sectionErrors.value }; delete next[key]; sectionErrors.value = next
    },
    (cause) => { sectionErrors.value = { ...sectionErrors.value, [key]: message(cause) } },
  )
}
const deploymentSmokeTerminalStatuses = new Set(['succeeded', 'failed', 'cancelled', 'uncertain'])
const deploymentSmokeActiveStatuses = new Set(['queued', 'submitting', 'submitted', 'cancel_requested'])
const deploymentSmokeFor = (deploymentId: string) => deploymentSmokeAttempts.value[deploymentId] || null
const clearDeploymentSmokePoll = () => { if (deploymentSmokePollTimer) window.clearTimeout(deploymentSmokePollTimer); deploymentSmokePollTimer = 0 }
const scheduleDeploymentSmokePoll = () => {
  clearDeploymentSmokePoll()
  if (tab.value === 'models' && Object.values(deploymentSmokeAttempts.value).some((item) => deploymentSmokeActiveStatuses.has(item.status))) {
    deploymentSmokePollTimer = window.setTimeout(() => void refreshDeploymentSmokeAttempts(), 2500)
  }
}
const restoreDeploymentSmokeAttempts = async (isCurrent: () => boolean = () => true) => {
  clearDeploymentSmokePoll()
  try {
    const attempts = await platformApi.listDeploymentSmokeAttempts()
    if (!isCurrent()) return
    const latest: Record<string, ModelDeploymentSmokeAttempt> = {}
    for (const attempt of attempts) if (!latest[attempt.deployment]) latest[attempt.deployment] = attempt
    deploymentSmokeAttempts.value = latest
    deploymentSmokeErrors.value = Object.fromEntries(
      Object.values(latest)
        .filter((attempt) => deploymentSmokeTerminalStatuses.has(attempt.status) && attempt.status !== 'succeeded')
        .map((attempt) => [
          attempt.deployment,
          attempt.errorMessage || attempt.errorCode || `烟测状态：${attempt.status}`,
        ]),
    )
    const next = { ...sectionErrors.value }; delete next.deploymentSmokeAttempts; sectionErrors.value = next
    scheduleDeploymentSmokePoll()
  } catch (cause) {
    if (isCurrent()) sectionErrors.value = { ...sectionErrors.value, deploymentSmokeAttempts: message(cause) }
  }
}
const refreshDeploymentSmokeAttempts = async () => {
  if (deploymentSmokePollInFlight.value) return
  const active = Object.values(deploymentSmokeAttempts.value).filter((item) => deploymentSmokeActiveStatuses.has(item.status))
  if (!active.length) { clearDeploymentSmokePoll(); return }
  deploymentSmokePollInFlight.value = true
  try {
    const refreshed = await Promise.all(active.map((item) => platformApi.getDeploymentSmokeAttempt(item.id)))
    const next = { ...deploymentSmokeAttempts.value }; const failures = { ...deploymentSmokeErrors.value }
    let completed = false
    for (const attempt of refreshed) {
      next[attempt.deployment] = attempt
      if (deploymentSmokeTerminalStatuses.has(attempt.status)) {
        completed = true
        failures[attempt.deployment] = attempt.status === 'succeeded' ? '' : attempt.errorMessage || attempt.errorCode || `烟测状态：${attempt.status}`
      }
    }
    deploymentSmokeAttempts.value = next; deploymentSmokeErrors.value = failures
    if (completed) {
      deployments.value = await platformApi.listDeployments()
      await capture('deploymentAcceptance', platformApi.listAcceptanceSmokeDeploymentRows(), (rows) => {
        deploymentAcceptanceRows.value = Object.fromEntries(rows.map(row => [row.deployment.id, row]))
      })
    }
  } catch (cause) {
    sectionErrors.value = { ...sectionErrors.value, deploymentSmokeAttempts: `刷新烟测任务失败：${message(cause)}` }
  } finally {
    deploymentSmokePollInFlight.value = false
    scheduleDeploymentSmokePoll()
  }
}
const cancelDeploymentSmoke = async (attempt: ModelDeploymentSmokeAttempt) => {
  if (!deploymentSmokeActiveStatuses.has(attempt.status) || deploymentSmokeBusyId.value) return
  deploymentSmokeBusyId.value = attempt.id
  try {
    const updated = await platformApi.cancelDeploymentSmokeAttempt(attempt.id)
    deploymentSmokeAttempts.value = { ...deploymentSmokeAttempts.value, [updated.deployment]: updated }
    notice.value = updated.status === 'cancelled' ? '烟测已在提交前取消。' : '取消请求已持久化，等待 worker 核对远端状态。'
    scheduleDeploymentSmokePoll()
  } catch (cause) {
    deploymentSmokeErrors.value = { ...deploymentSmokeErrors.value, [attempt.deployment]: message(cause) }
  } finally { deploymentSmokeBusyId.value = '' }
}
const runtimeProbeTerminalStatuses = new Set(['succeeded', 'failed', 'cancelled'])
const runtimeProbeActiveStatuses = new Set(['queued', 'running', 'cancel_requested'])
const runtimeProbeFor = (hostId: string) => runtimeProbeAttempts.value[hostId] || null
const clearRuntimeProbePoll = () => { if (runtimeProbePollTimer) window.clearTimeout(runtimeProbePollTimer); runtimeProbePollTimer = 0 }
const scheduleRuntimeProbePoll = () => {
  clearRuntimeProbePoll()
  if (tab.value === 'runtime' && Object.values(runtimeProbeAttempts.value).some((item) => runtimeProbeActiveStatuses.has(item.status))) {
    runtimeProbePollTimer = window.setTimeout(() => void refreshRuntimeProbeAttempts(), 2500)
  }
}
const restoreRuntimeProbeAttempts = async (isCurrent: () => boolean = () => true) => {
  clearRuntimeProbePoll()
  try {
    const attempts = await platformApi.listRuntimeHostProbeAttempts({ probe_kind: 'capability' })
    if (!isCurrent()) return
    const latest: Record<string, RuntimeHostProbeAttempt> = {}
    for (const attempt of attempts) if (!latest[attempt.host]) latest[attempt.host] = attempt
    runtimeProbeAttempts.value = latest
    runtimeProbeErrors.value = Object.fromEntries(
      Object.values(latest)
        .filter((attempt) => runtimeProbeTerminalStatuses.has(attempt.status) && attempt.status !== 'succeeded')
        .map((attempt) => [attempt.host, attempt.errorMessage || attempt.errorCode || `探测状态：${attempt.status}`]),
    )
    const next = { ...sectionErrors.value }; delete next.runtimeProbeAttempts; sectionErrors.value = next
    scheduleRuntimeProbePoll()
  } catch (cause) {
    if (isCurrent()) sectionErrors.value = { ...sectionErrors.value, runtimeProbeAttempts: message(cause) }
  }
}
const refreshRuntimeProbeAttempts = async () => {
  if (runtimeProbePollInFlight.value) return
  const active = Object.values(runtimeProbeAttempts.value).filter((item) => runtimeProbeActiveStatuses.has(item.status))
  if (!active.length) { clearRuntimeProbePoll(); return }
  runtimeProbePollInFlight.value = true
  try {
    const refreshed = await Promise.all(active.map((item) => platformApi.getRuntimeHostProbeAttempt(item.id)))
    const nextAttempts = { ...runtimeProbeAttempts.value }; const failures = { ...runtimeProbeErrors.value }
    let completed = false; let selectedCompleted = false
    for (const attempt of refreshed) {
      nextAttempts[attempt.host] = attempt
      if (runtimeProbeTerminalStatuses.has(attempt.status)) {
        completed = true; selectedCompleted ||= attempt.host === selectedHostId.value
        failures[attempt.host] = attempt.status === 'succeeded' ? '' : attempt.errorMessage || attempt.errorCode || `探测状态：${attempt.status}`
      }
    }
    runtimeProbeAttempts.value = nextAttempts; runtimeProbeErrors.value = failures
    if (completed) {
      runtimeHosts.value = await platformApi.listRuntimeHosts()
      await loadLatestSnapshots()
      if (selectedCompleted) await loadSelectedHostSnapshot()
    }
    const next = { ...sectionErrors.value }; delete next.runtimeProbeAttempts; sectionErrors.value = next
  } catch (cause) {
    sectionErrors.value = { ...sectionErrors.value, runtimeProbeAttempts: `刷新主机探测任务失败：${message(cause)}` }
  } finally {
    runtimeProbePollInFlight.value = false
    scheduleRuntimeProbePoll()
  }
}
const cancelRuntimeProbe = async (attempt: RuntimeHostProbeAttempt) => {
  if (!runtimeProbeActiveStatuses.has(attempt.status) || runtimeProbeBusyId.value) return
  runtimeProbeBusyId.value = attempt.id
  try {
    const updated = await platformApi.cancelRuntimeHostProbeAttempt(attempt.id)
    runtimeProbeAttempts.value = { ...runtimeProbeAttempts.value, [updated.host]: updated }
    notice.value = updated.status === 'cancelled' ? '主机探测已在执行前取消。' : '取消请求已持久化；worker 会在安全分块检查点停止。'
    scheduleRuntimeProbePoll()
  } catch (cause) {
    runtimeProbeErrors.value = { ...runtimeProbeErrors.value, [attempt.host]: message(cause) }
  } finally { runtimeProbeBusyId.value = '' }
}
const loadLatestSnapshots = async (isCurrent: () => boolean = () => true) => {
  const failures: string[] = []
  const states: Record<string, { state: 'ready'|'missing'|'error'; message: string }> = {}
  const entries = await Promise.all(runtimeHosts.value.map(async (host) => {
    try {
      const latest = await platformApi.getLatestRuntimeSnapshot(host.id)
      states[host.id] = { state: 'ready', message: '已读取最近快照' }
      return [host.id, latest] as const
    }
    catch (cause) {
      const status = (cause as { response?: { status?: number } })?.response?.status
      if (status === 404) states[host.id] = { state: 'missing', message: '尚未生成主机能力快照，请先探测。' }
      else {
        const failure = message(cause)
        states[host.id] = { state: 'error', message: failure }
        failures.push(`${host.name || host.key}: ${failure}`)
      }
      return null
    }
  }))
  if (!isCurrent()) return
  latestHostSnapshots.value = Object.fromEntries(entries.filter((item): item is readonly [string, HostCapabilitySnapshot] => Boolean(item)))
  latestHostSnapshotStates.value = states
  const next = { ...sectionErrors.value }
  if (failures.length) next.runtimeSnapshots = `读取主机快照失败：${failures.join('；')}`
  else delete next.runtimeSnapshots
  sectionErrors.value = next
}
const loadModels = async (captureRequest: CaptureRequest = capture, isCurrent: () => boolean = () => true) => {
  modelProfileOptions.value = null
  providers.value = []
  modelProfiles.value = []
  deployments.value = []
  modelArtifacts.value = []
  runtimeHosts.value = []
  deploymentAcceptanceRows.value = null
  await Promise.all([
    captureRequest('providers', platformApi.listProviders(), (value) => { providers.value = value }),
    captureRequest('modelProfileOptions', platformApi.getModelProfileRegistryOptions(), (value) => { modelProfileOptions.value = value }),
    captureRequest('modelProfiles', platformApi.listModelProfiles(), (value) => { modelProfiles.value = value }),
    captureRequest('deployments', platformApi.listDeployments(), (value) => { deployments.value = value }),
    captureRequest('modelArtifacts', platformApi.listModelArtifacts(), (value) => { modelArtifacts.value = value }),
    captureRequest('runtimeHosts', platformApi.listRuntimeHosts(), (value) => { runtimeHosts.value = value }),
    captureRequest('deploymentAcceptance', platformApi.listAcceptanceSmokeDeploymentRows(), (value) => {
      deploymentAcceptanceRows.value = Object.fromEntries(value.map((row) => [row.deployment.id, row]))
    }),
    ...(auth.user?.isSuperuser ? [captureRequest('secretBindings', platformApi.listSecretBindings(), (value) => { secretBindings.value = value })] : []),
  ])
  if (!isCurrent()) return
  await Promise.all([loadLatestSnapshots(isCurrent), restoreDeploymentSmokeAttempts(isCurrent)])
}
const loadDefaults = async (captureRequest: CaptureRequest = capture, isCurrent: () => boolean = () => true) => {
  configDefinitionOptions.value = null
  await Promise.all([
    captureRequest('definitions', platformApi.listConfigDefinitions(), (value) => { definitions.value = value }),
    captureRequest('configDefinitionOptions', platformApi.getConfigDefinitionOptions(), (value) => { configDefinitionOptions.value = value }),
    captureRequest('revisions', platformApi.listConfigRevisions(), (value) => { revisions.value = value }),
    captureRequest('resolved', platformApi.resolveConfiguration(), (value) => { resolved.value = value }),
    ...(auth.user?.isSuperuser ? [captureRequest('secretBindings', platformApi.listSecretBindings(), (value) => { secretBindings.value = value })] : []),
  ])
  if (!isCurrent()) return
  const resourceDefinitions = definitions.value.filter((item) => (
    item.valueSchema?.resource_ref && typeof item.valueSchema.resource_ref === 'object'
  ))
  const optionEntries = await Promise.all(resourceDefinitions.map(async (item) => {
    try {
      const options = await platformApi.listConfigResourceOptions(item.id)
      return [item.key, options.map((option) => ({
        value: option.value,
        label: option.label,
        disabled: !option.isSelectable,
        reason: option.isSelectable ? '' : option.reasonCode || option.state,
      }))] as const
    } catch (cause) {
      if (isCurrent()) sectionErrors.value = {
        ...sectionErrors.value,
        [`configResource:${item.key}`]: message(cause),
      }
      return [item.key, []] as const
    }
  }))
  if (!isCurrent()) return
  configResourceOptions.value = Object.fromEntries(optionEntries)
  const drafts = Object.fromEntries(definitions.value.filter((item) => item.isActive && !item.isSecret).map((item) => {
    const hasResolvedValue = Boolean(resolved.value) && Object.prototype.hasOwnProperty.call(resolved.value?.values || {}, item.key)
    const value = hasResolvedValue
      ? resolved.value?.values[item.key]
      : item.configurationClass === 'security_boundary' ? undefined : item.initialValue
    return [item.key, value === undefined ? '' : pretty(value)]
  }))
  configDraft.value = drafts; configOriginal.value = { ...drafts }; configDirty.value = new Set(); configUnsets.value = new Set(); secretSelections.value = {}
}
const loadSelectedHostSnapshot = async (isCurrent: () => boolean = () => true) => {
  const hostId = selectedHostId.value
  snapshot.value = null; readiness.value = null; recommendation.value = null
  selectedSnapshotState.value = 'idle'
  if (!hostId) return
  try {
    const latest = await platformApi.getLatestRuntimeSnapshot(hostId)
    if (!isCurrent() || selectedHostId.value !== hostId) return
    snapshot.value = latest; latestHostSnapshots.value = { ...latestHostSnapshots.value, [hostId]: latest }
    const nextReadiness = await platformApi.runtimeReadiness(hostId, latest.id)
    if (!isCurrent() || selectedHostId.value !== hostId) return
    readiness.value = nextReadiness
    selectedSnapshotState.value = 'ready'
    const next = { ...sectionErrors.value }; delete next.runtimeSnapshot; sectionErrors.value = next
  } catch (cause) {
    if (!isCurrent() || selectedHostId.value !== hostId) return
    const status = (cause as { response?: { status?: number } })?.response?.status
    selectedSnapshotState.value = status === 404 ? 'missing' : 'error'
    sectionErrors.value = { ...sectionErrors.value, runtimeSnapshot: status === 404 ? '所选主机尚无能力快照，请执行探测。' : message(cause) }
  }
}
const loadRuntime = async (captureRequest: CaptureRequest = capture, isCurrent: () => boolean = () => true) => {
  await Promise.all([
    captureRequest('runtimeHosts', platformApi.listRuntimeHosts(), (value) => { runtimeHosts.value = value }),
    captureRequest('resolved', platformApi.resolveConfiguration(), (value) => { resolved.value = value }),
    restoreRuntimeProbeAttempts(isCurrent),
  ])
  if (!isCurrent()) return
  const selectableHosts = runtimeHosts.value.filter((item) => !['retired', 'inactive', 'disabled'].includes(item.status.toLocaleLowerCase('en-US')))
  if (!selectableHosts.some((item) => item.id === selectedHostId.value)) selectedHostId.value = selectableHosts[0]?.id || ''
  await Promise.resolve(); await loadSelectedHostSnapshot(isCurrent)
}
let settingsLoadGeneration = 0
let savedWorkflowScanGeneration = 0
const load = async () => {
  const generation = ++settingsLoadGeneration
  const requestedTab = tab.value
  if (requestedTab === 'workflows') resetSavedWorkflowScanView()
  const workflowContextSnapshot = requestedTab === 'workflows'
    ? JSON.stringify(route.query.model_profile_version ?? null)
    : ''
  const loadStillCurrent = () => generation === settingsLoadGeneration && tab.value === requestedTab
  const workflowLoadStillCurrent = () => (
    loadStillCurrent()
    && JSON.stringify(route.query.model_profile_version ?? null) === workflowContextSnapshot
  )
  const requestStillCurrent = () => requestedTab === 'workflows' ? workflowLoadStillCurrent() : loadStillCurrent()
  const captureCurrent: CaptureRequest = (key, promise, commit) => capture(key, promise, commit, requestStillCurrent)
  const captureWorkflow: CaptureRequest = (key, promise, commit) => capture(key, promise, commit, workflowLoadStillCurrent)
  loading.value = true; error.value = ''; notice.value = ''; sectionErrors.value = {}
  productionCapability.value = null; productionCapabilityError.value = ''
  try {
    const capability = await repository.getProductionCapability()
    if (requestStillCurrent()) productionCapability.value = capability
  } catch (cause) {
    if (requestStillCurrent()) productionCapabilityError.value = message(cause)
  }
  if (!requestStillCurrent()) return
  try {
    if (requestedTab === 'models') await loadModels(captureCurrent, requestStillCurrent)
    else if (requestedTab === 'workflows') {
      const contextRequested = workflowModelContextRequested.value
      const contextId = workflowModelContextId.value
      const contextQueryError = workflowModelContextQueryError.value
      resolved.value = null
      workflowVersions.value = []
      legacyWorkflowTemplates.value = []
      modelProfiles.value = []
      deployments.value = []
      providers.value = []
      modelProfileOptions.value = null
      workflowProjectors.value = []
      workflowProjectorVersions.value = []
      workflowProjectorOptions.value = null
      workflowProjectorError.value = ''
      savedWorkflowAttestationAttempts.value = []
      workflowModelContextCatalogLoaded.value = false
      if (!contextQueryError) {
        await Promise.all([
          captureWorkflow('workflows', platformApi.listWorkflowVersions(contextId ? { modelProfileVersion: contextId } : {}), (value) => { workflowVersions.value = value }),
          captureWorkflow('modelProfiles', platformApi.listModelProfiles(), (value) => { modelProfiles.value = value }),
          captureWorkflow('providers', platformApi.listProviders(), (value) => { providers.value = value }),
          ...(auth.user?.isSuperuser ? [
            captureWorkflow('workflowProjectors', platformApi.listWorkflowProjectors(), (value) => { workflowProjectors.value = value }),
            captureWorkflow('workflowProjectorVersions', platformApi.listWorkflowProjectorVersions(), (value) => { workflowProjectorVersions.value = value }),
            captureWorkflow('workflowProjectorOptions', platformApi.getWorkflowProjectorOptions(), (value) => { workflowProjectorOptions.value = value }),
          ] : []),
        ])
      }
      if (!workflowLoadStillCurrent()) return
      const targetProfile = contextId ? modelProfiles.value.find((item) => item.id === contextId) || null : null
      const targetProvider = targetProfile ? providers.value.find((item) => item.id === targetProfile.provider) || null : null
      const baseCatalogHealthy = !sectionErrors.value.workflows && !sectionErrors.value.modelProfiles && !sectionErrors.value.providers
      const contextAllowsImporterCatalog = !contextRequested || Boolean(
        targetProfile
        && targetProvider?.isActive
        && targetProvider.driverType === 'comfyui'
        && ['draft', 'published'].includes(targetProfile.status),
      )
      if (!contextQueryError && baseCatalogHealthy && contextAllowsImporterCatalog) {
        await Promise.all([
          captureWorkflow('legacyWorkflowTemplates', platformApi.listLegacyWorkflowTemplates(), (value) => { legacyWorkflowTemplates.value = value }),
          captureWorkflow('deployments', platformApi.listDeployments(), (value) => { deployments.value = value }),
          captureWorkflow('modelProfileOptions', platformApi.getModelProfileRegistryOptions(), (value) => { modelProfileOptions.value = value }),
          captureWorkflow('resolved', platformApi.resolveConfiguration(), (value) => { resolved.value = value }),
          captureWorkflow(
            'savedWorkflowAttestations',
            platformApi.listRuntimeHostProbeAttempts({ probe_kind: 'draft_model_artifact_attestation' }),
            (value) => { savedWorkflowAttestationAttempts.value = value },
          ),
        ])
      }
      if (!workflowLoadStillCurrent()) return
      workflowModelContextCatalogLoaded.value = true
      if (targetProfile?.status === 'retired') showRetiredSettingsRecords.value = true
    }
    else if (requestedTab === 'prompts') { /* PromptProfileSettings owns its scoped server reads. */ }
    else if (requestedTab === 'routing') {
      recipes.value = []
      recipeEditorOptions.value = null
      await Promise.all([
        captureCurrent('recipes', platformApi.listRecipes(), (value) => { recipes.value = value }),
        captureCurrent('recipeEditorOptions', platformApi.getRecipeEditorOptions(), (value) => { recipeEditorOptions.value = value }),
        captureCurrent('modelProfiles', platformApi.listModelProfiles(), (value) => { modelProfiles.value = value }),
        captureCurrent('deployments', platformApi.listDeployments(), (value) => { deployments.value = value }),
        captureCurrent('workflows', platformApi.listWorkflowVersions(), (value) => { workflowVersions.value = value }),
        ...(auth.user?.isSuperuser ? [captureCurrent('productionReadiness', platformApi.getProductionReadiness(), (value) => { productionReadiness.value = value })] : []),
      ])
      if (requestStillCurrent() && !auth.user?.isSuperuser) productionReadiness.value = null
    }
    else if (requestedTab === 'defaults' || requestedTab === 'security') await loadDefaults(captureCurrent, requestStillCurrent)
    else if (requestedTab === 'runtime') await loadRuntime(captureCurrent, requestStillCurrent)
    else {
      await Promise.all([
        captureCurrent('providers', platformApi.listProviders(), (value) => { providers.value = value }),
        captureCurrent('deployments', platformApi.listDeployments(), (value) => { deployments.value = value }),
        captureCurrent('revisions', platformApi.listConfigRevisions(), (value) => { revisions.value = value }),
        captureCurrent('lifecycleAudit', platformApi.listPlatformLifecycleAuditEvents(), (value) => { lifecycleAudit.value = value }),
        ...(auth.user?.isSuperuser ? [
          captureCurrent('secretBindings', platformApi.listSecretBindings(), (value) => { secretBindings.value = value }),
          captureCurrent('secretAudit', platformApi.listSecretAuditEvents(), (value) => { secretAudit.value = value }),
        ] : []),
      ])
      if (requestStillCurrent() && !auth.user?.isSuperuser) { secretBindings.value = []; secretAudit.value = [] }
    }
  } finally { if (requestStillCurrent()) loading.value = false }
}
const selectTab = async (value: SettingsTab) => {
  const query: LocationQueryRaw = { ...route.query, section: value }
  if (value !== 'workflows') {
    delete query.model_profile_version
    delete query.intent
  }
  tab.value = value
  await router.replace({ query })
}
const clearWorkflowModelContext = async () => {
  const query: LocationQueryRaw = { ...route.query, section: 'workflows' }
  delete query.model_profile_version
  delete query.intent
  await router.replace({ query })
}
const openWorkflowDirectoryConfiguration = async () => {
  await selectTab('defaults')
  await nextTick()
  settingsListSearch.value = savedWorkflowDirectoryConfigKey
}
const openWorkflowBudgetConfiguration = async () => {
  await selectTab('security')
  await nextTick()
  settingsListSearch.value = savedWorkflowScanSecondsConfigKey
}
const clearSavedWorkflowAttestationPolls = () => {
  for (const timer of savedWorkflowAttestationTimers.values()) window.clearTimeout(timer)
  savedWorkflowAttestationTimers.clear()
}
watch([() => route.query.section, () => route.query.model_profile_version], ([value, context], [, previousContext]) => {
  const next = String(value || 'models')
  const normalized = allowedTabs.has(next as SettingsTab) ? next as SettingsTab : 'models'
  if (normalized !== tab.value) {
    tab.value = normalized
    return
  }
  if (normalized === 'workflows' && context !== previousContext) {
    workflowVersions.value = []
    workflowModelContextCatalogLoaded.value = false
    resetSavedWorkflowScanView()
    settingsListSearch.value = ''
    showRetiredSettingsRecords.value = false
    void load()
  }
})
watch(tab, () => {
  if (tab.value !== 'models') clearDeploymentSmokePoll()
  if (tab.value !== 'runtime') clearRuntimeProbePoll()
  if (tab.value !== 'workflows') resetSavedWorkflowScanView()
  settingsListSearch.value = ''
  showRetiredSettingsRecords.value = false
  if (tab.value === 'workflows') workflowModelContextCatalogLoaded.value = false
  void load()
})
onMounted(() => { void load(); runtimeEvidenceTimer = window.setInterval(() => { runtimeEvidenceNow.value = Date.now() }, 1000) })
onBeforeUnmount(() => { window.clearInterval(runtimeEvidenceTimer); clearDeploymentSmokePoll(); clearRuntimeProbePoll(); clearSavedWorkflowAttestationPolls() })
const editorLockedPaths = computed(() => {
  if (editorResource.value === 'provider' && editorId.value) return ['key']
  if (editorResource.value === 'model') return editorIntent.value === 'create'
    ? ['version']
    : ['profile_key', 'version']
  if (['deployment', 'artifact'].includes(editorResource.value) && (editorId.value || editorIntent.value === 'create-child')) return ['profile_version']
  if (editorResource.value === 'recipe') return editorId.value
    ? ['recipe_key', 'version', 'definition.schema_version']
    : ['version', 'definition.schema_version']
  if (editorResource.value === 'config-definition') {
    const paths = ['configuration_class']
    if (editorId.value) paths.push('key')
    else paths.push('is_active', 'needs_review', 'review_reason')
    if (editorConfigHasValues.value) {
      const structuralPaths = [
        'domain', 'value_type', 'restart_required', 'allowed_scopes',
        'allowed_default_sources', 'is_required', 'is_secret',
        'value_schema', 'initial_value',
      ]
      paths.push(...structuralPaths.filter((path) => !editorConfigMutableFields.value.includes(path)))
    }
    if (tab.value === 'security') paths.push('domain', 'allowed_scopes', 'allowed_default_sources', 'is_required', 'is_secret')
    return [...new Set(paths)]
  }
  return []
})
const editorDependencies = computed(() => ({
  providers: providers.value,
  modelProfiles: modelProfiles.value,
  deployments: deployments.value,
  runtimeHosts: runtimeHosts.value,
  workflowVersions: workflowVersions.value,
  recipes: recipes.value,
  configDefinitions: definitions.value,
  modelProfileOptions: modelProfileOptions.value,
  executionMonitoringLimits: productionCapability.value ? {
    maxSeconds: productionCapability.value.effectiveLimits.executionMonitoringMaxIdleTimeoutSeconds,
    maxOverrides: productionCapability.value.effectiveLimits.executionMonitoringMaxNodeTypeOverrides,
  } : null,
  recipeEditorOptions: recipeEditorOptions.value,
  configDefinitionOptions: configDefinitionOptions.value,
  lockedPaths: editorLockedPaths.value,
}))
const editorResourceLabel = computed(() => ({
  provider: '服务提供方（Provider）', model: '模型版本（Model Profile Version）',
  deployment: '运行实例（Model Deployment）', artifact: '模型文件与证明（Model Artifact）',
  recipe: '生产配方（Production Recipe）', 'config-definition': '配置定义（Config Definition）',
})[editorResource.value])
const openEditor = (resource: RegistryResource, existing?: ProviderProfile | ModelProfileVersion | ModelDeployment | ModelArtifact | ProductionRecipe | ConfigDefinition) => {
  if (['provider', 'model', 'artifact'].includes(resource) && !modelProfileOptions.value) {
    error.value = '服务端模型选项目录尚未读取成功；为避免回退为自由文本，当前不能打开该编辑器。'
    return
  }
  if (resource === 'deployment' && existing && 'runtimeConfig' in existing && existing.status === 'retired') {
    error.value = '已退役的运行实例是只读生命周期记录；如需恢复服务，请创建新的运行实例。'
    return
  }
  if (resource === 'recipe' && (!recipeEditorOptions.value || sectionErrors.value.recipes)) {
    error.value = '配方列表或服务端编辑目录尚未完整读取；为避免重复登记 Recipe Key、使用陈旧目录或技术默认值，当前不能打开编辑器。'
    return
  }
  if (resource === 'config-definition') {
    if (!configDefinitionOptions.value) {
      error.value = '服务端配置定义目录尚未读取成功；为避免回退为自由文本，当前不能打开编辑器。'
      return
    }
    if (!canWriteCurrentSection.value) {
      error.value = `只有超级管理员可以修改${currentSettingsSection.value.label}；当前页面为只读。`
      return
    }
    if (existing && 'configurationClass' in existing && existing.configurationClass === 'unknown') {
      error.value = `配置定义 ${existing.key} 的 configuration_class 缺失或未知，已失败关闭；请先修复服务端数据合同。`
      return
    }
  }
  editorResource.value = resource
  editorId.value = existing?.id || ''
  editorIntent.value = existing ? 'edit' : 'create'
  editorSourceId.value = ''
  editorConfigHasValues.value = resource === 'config-definition' && Boolean(existing && 'hasValues' in existing && existing.hasValues)
  editorConfigMutableFields.value = resource === 'config-definition' && existing && 'mutableFields' in existing ? [...existing.mutableFields] : []
  let payload: JsonObject = JSON.parse(JSON.stringify(editorTemplates[resource])) as JsonObject
  if (resource === 'config-definition' && !existing && tab.value === 'security') payload = {
    ...payload,
    domain: 'security',
    configuration_class: 'security_boundary',
    allowed_scopes: 'system',
    allowed_default_sources: '',
    is_required: true,
    is_secret: false,
  }
  editorCredentialId.value = existing ? '__keep__' : ''
  if (resource === 'provider' && existing && 'driverType' in existing) payload = {
    key: existing.key, name: existing.name, driver_type: existing.driverType,
    modalities: existing.modalities, operations: existing.operations,
    driver_config: existing.driverConfig, is_active: existing.isActive,
  }
  if (resource === 'model' && existing && 'profileKey' in existing) payload = {
    profile_key: existing.profileKey, version: existing.version, provider: existing.provider,
    model_identifier: existing.modelIdentifier, modality: existing.modality, specification: existing.specification,
  }
  if (resource === 'deployment' && existing && 'runtimeConfig' in existing) payload = {
    key: existing.key, name: existing.name, profile_version: existing.profileVersion,
    runtime_host: existing.runtimeHost || null, runtime_config: existing.runtimeConfig,
    priority: existing.priority, is_default: existing.isDefault, status: existing.status,
  }
  if (resource === 'recipe' && existing && 'recipeKey' in existing) payload = {
    recipe_key: existing.recipeKey, version: existing.version, name: existing.name,
    definition: existing.definition,
  }
  if (resource === 'artifact' && existing && 'logicalName' in existing) payload = {
    profile_version: existing.profileVersion, role: existing.role, logical_name: existing.logicalName,
    filename: existing.filename, inventory_folder: existing.inventoryFolder,
    inventory_node_type: existing.inventoryNodeType, inventory_input_name: existing.inventoryInputName,
    storage_uri: existing.storageUri, sha256: existing.sha256,
    size_bytes: existing.sizeBytes,
  }
  if (resource === 'config-definition' && existing && 'valueType' in existing) payload = {
    key: existing.key, domain: existing.domain, configuration_class: existing.configurationClass,
    value_type: existing.valueType, description: existing.description,
    unit: existing.unit, impact_modules: existing.impactModules, restart_required: existing.restartRequired,
    allowed_scopes: existing.allowedScopes, allowed_default_sources: existing.allowedDefaultSources,
    is_required: existing.isRequired, is_secret: existing.isSecret, is_active: existing.isActive,
    needs_review: existing.needsReview, review_reason: existing.reviewReason,
    value_schema: existing.valueSchema, initial_value: existing.initialValue,
  }
  editorPayload.value = applyResourceDefaults(resource, payload, editorDependencies.value)
  editorOriginalPayload.value = JSON.parse(JSON.stringify(editorPayload.value)) as JsonObject
  editorValidity.value = { valid: false, errors: [], warnings: [] }
  editorError.value = ''
  editorOpen.value = true
}
const openModelArtifactDraft = (profile: ModelProfileVersion) => {
  if (profile.status !== 'draft') {
    error.value = '模型文件声明只能添加到草稿版本；已发布版本如需变更文件，请先新建模型版本。'
    return
  }
  openEditor('artifact')
  editorIntent.value = 'create-child'
  editorPayload.value = { ...editorPayload.value, profile_version: profile.id }
}
const openModelDeploymentDraft = (profile: ModelProfileVersion) => {
  if (profile.status !== 'published') {
    error.value = '运行实例只能绑定已发布的模型版本。'
    return
  }
  openEditor('deployment')
  editorIntent.value = 'create-child'
  editorPayload.value = { ...editorPayload.value, profile_version: profile.id }
}
const latestModelProfileForFamily = (family: ModelRegistryFamily) => modelProfiles.value
  .filter((item) => item.profileKey === family.key)
  .sort((left, right) => right.version - left.version)[0] || family.versions[0]?.profile
const openNextModelVersionDraft = (family: ModelRegistryFamily) => {
  const source = latestModelProfileForFamily(family)
  if (!source) return
  const sourceProvider = providers.value.find((item) => item.id === source.provider)
  if (!providers.value.some((item) => item.isActive)) {
    error.value = '当前没有已启用的服务连接；请先在“管理服务连接”中恢复或新增 Provider。'
    return
  }
  openEditor('model')
  editorIntent.value = 'next-model-version'
  editorSourceId.value = source.id
  editorPayload.value = {
    ...editorPayload.value,
    profile_key: source.profileKey,
    provider: sourceProvider?.isActive ? source.provider : '',
    model_identifier: source.modelIdentifier,
    modality: source.modality,
    specification: JSON.parse(JSON.stringify(source.specification)) as JsonObject,
  }
}
const submitEditor = async () => {
  if (!editorValidity.value.valid) {
    editorError.value = `请先修复表单中的 ${editorValidity.value.errors.length} 个阻断项。`
    return
  }
  saving.value = true; editorError.value = ''
  try {
    const parsed = buildRegistryResourcePayload(editorResource.value, editorPayload.value, editorDependencies.value)
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('提交内容必须是 JSON 对象')
    if (editorResource.value === 'model') {
      delete parsed.version
      if (editorId.value || editorIntent.value === 'next-model-version') delete parsed.profile_key
    }
    if (editorResource.value === 'recipe') {
      if (!recipeEditorOptions.value || sectionErrors.value.recipes) throw new Error('配方列表或服务端编辑目录已失效，请重新读取后再保存。')
      if (!parsed.definition || typeof parsed.definition !== 'object' || Array.isArray(parsed.definition)) throw new Error('配方定义必须是结构化对象。')
      ;(parsed.definition as JsonObject).schema_version = recipeEditorOptions.value.definitionSchemaVersion
      if (editorId.value) delete parsed.recipe_key
    }
    if (editorResource.value === 'config-definition') {
      if (!canWriteCurrentSection.value) throw new Error(`只有超级管理员可以修改${currentSettingsSection.value.label}；当前页面为只读。`)
      const expectedClass = tab.value === 'security' ? 'security_boundary' : 'standard'
      if (parsed.configuration_class !== expectedClass) {
        throw new Error(`当前分区只允许保存 ${expectedClass} 类配置；不能跨分区创建或重新分类。`)
      }
      if (!editorId.value) {
        delete parsed.is_active
        delete parsed.needs_review
        delete parsed.review_reason
      }
    }
    if (['provider', 'deployment'].includes(editorResource.value)) {
      delete parsed.credential_id
      if (auth.user?.isSuperuser && editorCredentialId.value === '__clear__') parsed.credential_id = null
      else if (auth.user?.isSuperuser && editorCredentialId.value && editorCredentialId.value !== '__keep__') parsed.credential_id = editorCredentialId.value
    }
    let requestPayload = parsed
    if (['config-definition', 'recipe'].includes(editorResource.value) && editorId.value) {
      const original = buildRegistryResourcePayload(editorResource.value, editorOriginalPayload.value, editorDependencies.value)
      if (editorResource.value === 'recipe') delete original.recipe_key
      requestPayload = Object.fromEntries(Object.entries(parsed).filter(([key, value]) => (
        JSON.stringify(value) !== JSON.stringify(original[key])
      ))) as JsonObject
      if (!Object.keys(requestPayload).length) throw new Error(editorResource.value === 'recipe' ? '未检测到需要提交的生产配方变更。' : '未检测到需要提交的配置定义变更。')
    }
    const reasonCode = editorId.value ? 'settings.update' : 'settings.create'
    if (editorResource.value === 'provider') editorId.value ? await platformApi.updateProvider(editorId.value, requestPayload, reasonCode) : await platformApi.createProvider(requestPayload, reasonCode)
    else if (editorResource.value === 'model') {
      if (editorId.value) await platformApi.updateModelProfile(editorId.value, requestPayload, reasonCode)
      else if (editorIntent.value === 'next-model-version' && editorSourceId.value) await platformApi.createNextModelProfile(editorSourceId.value, requestPayload, reasonCode)
      else await platformApi.createModelProfile(requestPayload, reasonCode)
    }
    else if (editorResource.value === 'deployment') editorId.value ? await platformApi.updateDeployment(editorId.value, requestPayload, reasonCode) : await platformApi.createDeployment(requestPayload, reasonCode)
    else if (editorResource.value === 'artifact') editorId.value ? await platformApi.updateModelArtifact(editorId.value, requestPayload, reasonCode) : await platformApi.createModelArtifact(requestPayload, reasonCode)
    else if (editorResource.value === 'recipe') editorId.value ? await platformApi.updateRecipe(editorId.value, requestPayload, reasonCode) : await platformApi.createRecipe(requestPayload, reasonCode)
    else editorId.value ? await platformApi.updateConfigDefinition(editorId.value, requestPayload, reasonCode) : await platformApi.createConfigDefinition(requestPayload, reasonCode)
    const action = editorId.value ? '更新' : '保存'
    editorOpen.value = false; await load(); notice.value = `服务端已${action}，列表已重新读取。`
  } catch (cause) { editorError.value = message(cause) } finally { saving.value = false }
}
const createRecipeSuccessor = async (item: ProductionRecipe) => {
  if (!['published', 'retired'].includes(item.status) || saving.value) return
  saving.value = true; error.value = ''; notice.value = ''
  try {
    const draft = await platformApi.cloneRecipeSuccessor(item.id, item.definitionSha256, 'settings.clone_successor')
    await load()
    const refreshed = recipes.value.find((candidate) => candidate.id === draft.id) || draft
    notice.value = `服务端已创建 ${refreshed.recipeKey} v${refreshed.version} 后继草稿；版本号与配方族由系统维护。`
    openEditor('recipe', refreshed)
  } catch (cause) { error.value = message(cause) }
  finally { saving.value = false }
}
const showDetail = (kind: string, title: string, entity: unknown) => { detailRequestKey.value = ''; detailKind.value = kind; detailTitle.value = title; detailEntity.value = entity; detailOpen.value = true }
const detailPayload = computed<unknown>(() => detailEntity.value ? JSON.parse(JSON.stringify(detailEntity.value)) : {})
const detailWorkflowInputContract = computed(() => detailKind.value === '工作流版本'
  ? (detailEntity.value as WorkflowTemplateVersion | null)?.inputContract : undefined)
const detailWorkflow = computed(() => detailKind.value === '工作流版本' ? detailEntity.value as WorkflowTemplateVersion | null : null)
const detailRecipe = computed<ProductionRecipe | null>(() => (
  detailKind.value === '生产配方' && detailEntity.value && typeof detailEntity.value === 'object' && 'definition' in detailEntity.value
    ? detailEntity.value as ProductionRecipe
    : null
))
const hydrateRecipeWorkflowDetails = async (item: ProductionRecipe) => {
  const workflowIds = recipeWorkflowVersionIds(item.definition)
  const missingIds = workflowIds.filter((id) => (
    !recipeWorkflowDetails.value[id] && recipeWorkflowDetailStates.value[id] !== 'loading'
  ))
  if (!missingIds.length) return
  recipeWorkflowDetailStates.value = {
    ...recipeWorkflowDetailStates.value,
    ...Object.fromEntries(missingIds.map((id) => [id, 'loading' as const])),
  }
  await Promise.all(missingIds.map(async (id) => {
    try {
      const detail = await platformApi.getWorkflowVersionForAudit(id)
      recipeWorkflowDetails.value = { ...recipeWorkflowDetails.value, [id]: detail }
      recipeWorkflowDetailStates.value = { ...recipeWorkflowDetailStates.value, [id]: 'loaded' }
      const nextErrors = { ...recipeWorkflowDetailErrors.value }; delete nextErrors[id]; recipeWorkflowDetailErrors.value = nextErrors
    } catch (cause) {
      recipeWorkflowDetailStates.value = { ...recipeWorkflowDetailStates.value, [id]: 'error' }
      recipeWorkflowDetailErrors.value = { ...recipeWorkflowDetailErrors.value, [id]: message(cause) }
    }
  }))
}
const showRecipeDetail = (item: ProductionRecipe) => {
  showDetail('生产配方', `${item.name} · v${item.version}`, item)
  void hydrateRecipeWorkflowDetails(item)
}
const trackRecipeExpanded = (item: ProductionRecipe, event: Event) => {
  const next = new Set(expandedRecipeIds.value)
  if ((event.currentTarget as HTMLDetailsElement).open) {
    next.add(item.id)
    void hydrateRecipeWorkflowDetails(item)
  } else next.delete(item.id)
  expandedRecipeIds.value = next
}
const detailModelProfile = computed<ModelProfileVersion | null>(() => (
  detailKind.value === '模型版本' && detailEntity.value && typeof detailEntity.value === 'object'
    ? detailEntity.value as ModelProfileVersion
    : null
))
const detailPromptNormalizationSummary = computed(() => (
  detailModelProfile.value ? promptNormalizationCapacitySummary(detailModelProfile.value) : ''
))
const workflowExportNeedsExplicitDeployment = computed(() => Boolean(
  workflowExportVersion.value?.status === 'draft'
  && !workflowExportVersion.value.validatedDeployment,
))
const workflowExportDeploymentOptions = computed(() => {
  const version = workflowExportVersion.value
  if (!version) return []
  return deployments.value.filter((item) => (
    item.status === 'active'
    && item.providerDriver === 'comfyui'
    && providers.value.some((provider) => (
      provider.id === item.providerProfileId
      && provider.isActive
      && provider.driverType === 'comfyui'
    ))
    && modelProfiles.value.some((profile) => (
      profile.id === item.profileVersion
      && profile.status === 'published'
    ))
    && (!version.provider || item.providerProfileId === version.provider)
    && (!version.modelProfileVersion || item.profileVersion === version.modelProfileVersion)
  ))
})
const showDeploymentDetail = async (item: ModelDeployment) => {
  const title = item.name || item.key
  showDetail('Deployment', title, { ...item, capabilityEvidenceState: 'loading' })
  const requestKey = `deployment:${item.id}`; detailRequestKey.value = requestKey
  try {
    const evidence = await platformApi.listDeploymentEvidence(item.id)
    if (detailOpen.value && detailRequestKey.value === requestKey) detailEntity.value = { ...item, capabilityEvidence: evidence }
    const next = { ...sectionErrors.value }; delete next.deployments; sectionErrors.value = next
  } catch (cause) {
    const failure = message(cause)
    if (detailOpen.value && detailRequestKey.value === requestKey) detailEntity.value = { ...item, capabilityEvidenceError: failure }
    sectionErrors.value = { ...sectionErrors.value, deployments: failure }
  }
}
const showWorkflowDetail = async (item: WorkflowTemplateVersion) => {
  const title = `${item.templateName || item.templateSlug} · v${item.version}`
  showDetail('工作流版本', title, { ...item, persistedEvidenceState: 'loading' })
  const requestKey = `workflow:${item.id}`; detailRequestKey.value = requestKey
  try {
    const detail = await platformApi.getWorkflowVersion(item.id)
    if (detailOpen.value && detailRequestKey.value === requestKey) detailEntity.value = detail
    const next = { ...sectionErrors.value }; delete next.workflows; sectionErrors.value = next
  } catch (cause) {
    const failure = message(cause)
    if (detailOpen.value && detailRequestKey.value === requestKey) detailEntity.value = { ...item, persistedEvidenceError: failure }
    sectionErrors.value = { ...sectionErrors.value, workflows: failure }
  }
}
const loadWorkflowExport = async () => {
  const version = workflowExportVersion.value
  if (!version || workflowExportBusyId.value) return
  if (workflowExportNeedsExplicitDeployment.value && !workflowExportDeploymentId.value) {
    workflowExportError.value = '未校验草稿必须显式选择 active 且绑定兼容的 ComfyUI deployment。'
    return
  }
  workflowExport.value = null; workflowExportError.value = ''; workflowExportBusyId.value = version.id
  try { workflowExport.value = await platformApi.getWorkflowTemplateCanvasExport(version.id, workflowExportDeploymentId.value) }
  catch (cause) { workflowExportError.value = message(cause) }
  finally { workflowExportBusyId.value = '' }
}
const prepareWorkflowImport = (item: WorkflowTemplateVersion) => {
  workflowExportVersion.value = item; workflowExport.value = null; workflowExportError.value = ''
  workflowExportDeploymentId.value = item.validatedDeployment || ''; workflowExportOpen.value = true
  if (!workflowExportNeedsExplicitDeployment.value) void loadWorkflowExport()
}
const selectWorkflowExportDeployment = () => { workflowExport.value = null; workflowExportError.value = '' }
const closeWorkflowExport = () => { if (workflowExportBusyId.value) return; workflowExportOpen.value = false; workflowExportError.value = ''; workflowExport.value = null; workflowExportVersion.value = null; workflowExportDeploymentId.value = '' }
const downloadWorkflowCanvas = () => {
  const payload = workflowExport.value
  if (!payload?.importReady) return
  const blob = new Blob([JSON.stringify(payload.workflow, null, 2)], { type: 'application/json;charset=utf-8' })
  const objectUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = objectUrl; anchor.download = payload.filename; anchor.style.display = 'none'
  document.body.appendChild(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(objectUrl)
  notice.value = '已下载 ComfyUI UI 画布 JSON；请在 ComfyUI 中拖入该文件或从工作流（Workflow）菜单打开。'
}
const workflowProjectorVersionsFor = (projectorId: string) => workflowProjectorVersions.value
  .filter((item) => item.projector === projectorId)
  .sort((left, right) => right.version - left.version)
const workflowProjectorEngineValue = (key: string, version: number) => `${key}\u0000${version}`
const workflowProjectorCreateEngine = computed(() => workflowProjectorOptions.value?.engines.find((item) => (
  workflowProjectorEngineValue(item.key, item.version) === workflowProjectorCreateVersion.engine
)) || null)
const workflowProjectorEditorEngine = computed(() => workflowProjectorOptions.value?.engines.find((item) => (
  workflowProjectorEngineValue(item.key, item.version) === workflowProjectorEditorDraft.engine
)) || null)
const workflowProjectorSeedVersions = computed(() => [...workflowProjectorVersions.value].sort((left, right) => (
  Number(right.isCurrent) - Number(left.isCurrent)
  || Number(right.status === 'published') - Number(left.status === 'published')
  || right.version - left.version
  || left.projectorKey.localeCompare(right.projectorKey)
)))
const workflowProjectorMutationBlockedReason = computed(() => {
  if (workflowProjectorBusy.value) return '另一个 Projector 操作正在处理中，请等待完成。'
  if (saving.value) return '页面正在保存另一项配置，请等待完成。'
  return ''
})
const workflowProjectorCreateBlockedReason = computed(() => {
  if (workflowProjectorMutationBlockedReason.value) return workflowProjectorMutationBlockedReason.value
  if (!workflowProjectorOptions.value) return 'Projector 表单合同尚未读取；请点击页面右上角“重新读取”。'
  if (!workflowProjectorOptions.value.engines.length) return '服务端没有返回已注册引擎，不能初始化新方案。'
  return ''
})
const syncWorkflowProjectorCatalogOpen = (event: Event) => {
  workflowProjectorCatalogOpen.value = (event.currentTarget as HTMLDetailsElement).open
}
const revealWorkflowProjectorCatalog = async () => {
  workflowProjectorCatalogOpen.value = true
  await nextTick()
  const catalog = workflowProjectorCatalogElement.value
  if (!catalog) return
  catalog.open = true
  catalog.scrollIntoView({ behavior: 'smooth', block: 'start' })
  catalog.querySelector<HTMLElement>('summary')?.focus({ preventScroll: true })
}
const reloadWorkflowProjectorCatalog = async () => {
  const [families, versions, options] = await Promise.all([
    platformApi.listWorkflowProjectors(),
    platformApi.listWorkflowProjectorVersions(),
    platformApi.getWorkflowProjectorOptions(),
  ])
  workflowProjectors.value = families
  workflowProjectorVersions.value = versions
  workflowProjectorOptions.value = options
}
const currentWorkflowProjectorCreateSeedFingerprint = () => JSON.stringify({
  definition: workflowProjectorCreateDefinition.value,
  engine: workflowProjectorCreateVersion.engine,
  providerDriverType: workflowProjectorCreateVersion.providerDriverType,
  operation: workflowProjectorCreateVersion.operation,
  outputKind: workflowProjectorCreateVersion.outputKind,
})
const workflowProjectorCreateHasEdits = () => Boolean(
  workflowProjectorCreateDefinitionValidity.value.errors.some(item => item.includes('高级 JSON'))
  || (workflowProjectorCreateSeedFingerprint.value && currentWorkflowProjectorCreateSeedFingerprint() !== workflowProjectorCreateSeedFingerprint.value),
)
const confirmWorkflowProjectorReinitialize = () => !workflowProjectorCreateHasEdits()
  || window.confirm('重新初始化会覆盖当前未保存的图结构、参数、素材和产物类型；保留方案名称与说明。是否继续？')
const clearWorkflowProjectorInitialization = () => {
  workflowProjectorCreateSeedVersionId.value = ''
  workflowProjectorCreateAppliedSeedId.value = ''
  workflowProjectorCreateSeedFingerprint.value = ''
  workflowProjectorCreateDefinition.value = {}
  workflowProjectorCreateDefinitionValidity.value = { valid: false, errors: [] }
  Object.assign(workflowProjectorCreateVersion, { engine: '', providerDriverType: '', operation: '', outputKind: '' })
  workflowProjectorRecommendation.value = null
  workflowProjectorRecommendationSourcePath.value = ''
  workflowProjectorError.value = ''
}
const changeWorkflowProjectorCreateMode = (mode: 'recommendation' | 'copy' | 'blank') => {
  if (mode === workflowProjectorCreateMode.value || workflowProjectorBusy.value || savedWorkflowBusy.value) return
  if (!confirmWorkflowProjectorReinitialize()) return
  clearWorkflowProjectorInitialization()
  workflowProjectorCreateMode.value = mode
}
const navigateWorkflowProjectorCreateMode = async (event: KeyboardEvent) => {
  if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  if (workflowProjectorBusy.value || savedWorkflowBusy.value) return
  const modes = workflowProjectorCreateModes
  const current = modes.findIndex(item => item.key === workflowProjectorCreateMode.value)
  const index = event.key === 'Home' ? 0 : event.key === 'End' ? modes.length - 1
    : (current + (['ArrowLeft','ArrowUp'].includes(event.key) ? -1 : 1) + modes.length) % modes.length
  const group = event.currentTarget as HTMLElement
  changeWorkflowProjectorCreateMode(modes[index]!.key)
  await nextTick()
  group.querySelector<HTMLElement>('[aria-checked="true"]')?.focus()
}
const initializeBlankWorkflowProjector = (event: Event) => {
  const select = event.target as HTMLSelectElement
  const engine = workflowProjectorOptions.value?.engines.find(row => workflowProjectorEngineValue(row.key, row.version) === select.value)
  if (!engine || workflowProjectorBusy.value || savedWorkflowBusy.value) return
  if (!confirmWorkflowProjectorReinitialize()) { select.value = workflowProjectorCreateVersion.engine; return }
  clearWorkflowProjectorInitialization()
  const template = engine.creationTemplate
  Object.assign(workflowProjectorCreateVersion, { engine: workflowProjectorEngineValue(engine.key,engine.version), providerDriverType: template.providerDriverType, operation: template.operation, outputKind: template.outputKind })
  workflowProjectorCreateDefinition.value = cloneWorkflowProjectorDefinition(template.definition)
  workflowProjectorCreateAppliedSeedId.value = `blank:${engine.key}:${engine.version}`
  workflowProjectorCreateSeedFingerprint.value = currentWorkflowProjectorCreateSeedFingerprint()
}
const workflowProjectorRecommendationCandidates = computed(() => savedWorkflowItems.value.filter(item => savedWorkflowCanRecommendProjector(item)))
const workflowProjectorRecommendationChoices = computed(() => workflowProjectorRecommendationCandidates.value.map(item => ({
  key: savedWorkflowKey(item), label: `${savedWorkflowSourceFor(item)?.sourceLabel || ''} · ${item.sourcePath}`,
})))
const workflowProjectorRecommendationUnavailableReason = computed(() => {
  if (!auth.user?.isSuperuser) return '需要超级管理员权限才能生成 Projector 推荐。'
  if (!workflowModelContextCanImport.value) return workflowModelContextFailure.value || '工作流目录或模型上下文尚不可用，请返回工作流页查看读取状态；也可选择其他初始化方式。'
  return ''
})
const workflowProjectorCreateInitialized = computed(() => Boolean(
  workflowProjectorCreateAppliedSeedId.value && workflowProjectorCreateEngine.value
  && (workflowProjectorCreateMode.value !== 'recommendation' || (workflowProjectorRecommendation.value
    && workflowProjectorRecommendationCandidates.value.some(item => savedWorkflowKey(item) === workflowProjectorCreateCandidateKey.value
      && item.candidateSourceId === workflowProjectorRecommendation.value?.sourceEvidence.candidateSourceId
      && item.sourceSha256 === workflowProjectorRecommendation.value?.sourceEvidence.sourceSha256
      && item.sourceScopeSha256 === workflowProjectorRecommendation.value?.sourceEvidence.sourceScopeSha256
      && savedWorkflowDrafts[savedWorkflowKey(item)]?.primaryOutputNodeId === workflowProjectorRecommendation.value?.sourceEvidence.primaryCanvasNodeId))),
))
const requestProjectorRecommendationFromCreate = async () => {
  const item = workflowProjectorRecommendationCandidates.value.find(row => savedWorkflowKey(row) === workflowProjectorCreateCandidateKey.value)
  if (item) await recommendWorkflowProjectorFromCandidate(item)
}
const goToProjectorCandidateScan = async () => {
  if (workflowProjectorBusy.value || savedWorkflowBusy.value || workflowProjectorRecommendationUnavailableReason.value) return
  const importer = document.getElementById('saved-workflow-importer') as HTMLDetailsElement | null
  if (!importer) { workflowProjectorError.value = '扫描区暂不可用；本页内容未丢失，请关闭弹窗后重新读取工作流页。'; return }
  workflowProjectorResumeFromScan.value = true
  workflowProjectorCreateOpen.value = false
  await nextTick()
  // BaseModal restores its trigger focus after the close render; move focus only after that tick.
  await nextTick()
  importer.open = true
  importer.scrollIntoView({behavior:'smooth',block:'start'})
  importer.querySelector<HTMLElement>('[data-projector-scan-action]')?.focus({preventScroll:true})
}
const applyWorkflowProjectorCreateSeed = () => {
  const seed = workflowProjectorSeedVersions.value.find((item) => item.id === workflowProjectorCreateSeedVersionId.value)
  if (!seed) return
  workflowProjectorCreateVersion.engine = workflowProjectorEngineValue(seed.engineKey, seed.engineVersion)
  workflowProjectorCreateVersion.providerDriverType = seed.providerDriverType
  workflowProjectorCreateVersion.operation = seed.operation
  workflowProjectorCreateVersion.outputKind = seed.outputKind
  workflowProjectorCreateDefinition.value = cloneWorkflowProjectorDefinition(seed.definition)
  workflowProjectorCreateDefinitionValidity.value = { valid: false, errors: [] }
  workflowProjectorRecommendation.value = null
  workflowProjectorRecommendationSourcePath.value = ''
  workflowProjectorCreateAppliedSeedId.value = seed.id
  workflowProjectorCreateSeedFingerprint.value = currentWorkflowProjectorCreateSeedFingerprint()
}
const changeWorkflowProjectorCreateSeed = (event: Event) => {
  if (workflowProjectorBusy.value || savedWorkflowBusy.value) return
  const select = event.target as HTMLSelectElement
  if (!confirmWorkflowProjectorReinitialize()) { select.value = workflowProjectorCreateSeedVersionId.value; return }
  workflowProjectorCreateSeedVersionId.value = select.value
  applyWorkflowProjectorCreateSeed()
}
const closeWorkflowProjectorCreate = () => {
  if (workflowProjectorBusy.value || savedWorkflowBusy.value) return
  if ((workflowProjectorCreateHasEdits() || workflowProjectorCreateFamily.key || workflowProjectorCreateFamily.name || workflowProjectorCreateFamily.description)
    && !window.confirm('关闭将放弃此表单尚未提交的修改，不会删除任何已创建的方案或版本。是否继续？')) return
  workflowProjectorResumeFromScan.value = false
  workflowProjectorCreateOpen.value = false
  workflowProjectorCreateFamilyId.value = ''
  workflowProjectorCreateSeedVersionId.value = ''
  workflowProjectorCreateAppliedSeedId.value = ''
  workflowProjectorCreateSeedFingerprint.value = ''
  workflowProjectorCreateDefinition.value = {}
  workflowProjectorCreateDefinitionValidity.value = { valid: false, errors: [] }
  workflowProjectorRecommendation.value = null
  workflowProjectorRecommendationSourcePath.value = ''
  Object.assign(workflowProjectorCreateFamily, { key: '', name: '', description: '' })
  Object.assign(workflowProjectorCreateVersion, { engine: '', providerDriverType: '', operation: '', outputKind: '' })
  workflowProjectorError.value = ''
}
const openWorkflowProjectorCreate = (projector: WorkflowProjector | null = null) => {
  if (workflowProjectorCreateBlockedReason.value) { workflowProjectorError.value = workflowProjectorCreateBlockedReason.value; return }
  if (workflowProjectorResumeFromScan.value && !projector) { workflowProjectorCreateOpen.value = true; return }
  clearWorkflowProjectorInitialization()
  workflowProjectorCreateMode.value = 'recommendation'
  workflowProjectorCreateCandidateKey.value = ''
  workflowProjectorResumeFromScan.value = false
  workflowProjectorCreateFamilyId.value = projector?.id || ''
  workflowProjectorRecommendation.value = null
  workflowProjectorRecommendationSourcePath.value = ''
  Object.assign(workflowProjectorCreateFamily, {
    key: projector?.key || '',
    name: projector?.name || '',
    description: projector?.description || '',
  })
  workflowProjectorCreateAppliedSeedId.value = ''
  workflowProjectorCreateSeedFingerprint.value = ''
  workflowProjectorError.value = ''
  workflowProjectorEditorOpen.value = false
  workflowProjectorCreateOpen.value = true
}
const submitWorkflowProjectorCreate = async () => {
  if (workflowProjectorBusy.value || savedWorkflowBusy.value) return
  if (!workflowProjectorCreateInitialized.value) {
    workflowProjectorError.value = '请先完成初始化；推荐所用的候选和主输出必须与当前证据一致。'
    return
  }
  const key = workflowProjectorCreateFamily.key.trim()
  const name = workflowProjectorCreateFamily.name.trim()
  if (!workflowProjectorCreateFamilyId.value && !/^[a-z][a-z0-9_.-]{1,99}$/.test(key)) {
    workflowProjectorError.value = '方案族 key 必须以小写字母开头，仅含小写字母、数字、点、下划线或连字符，长度 2–100。'
    return
  }
  if (!name) {
    workflowProjectorError.value = '请填写方案族名称。'
    return
  }
  const engine = workflowProjectorCreateEngine.value
  if (!engine) {
    workflowProjectorError.value = '请选择服务端注册的 Projector 引擎版本。'
    return
  }
  if (!workflowProjectorCreateDefinitionValidity.value.valid) {
    workflowProjectorError.value = workflowProjectorCreateDefinitionValidity.value.errors[0] || '请先完成 Projector 参数配置。'
    return
  }
  const definition = cloneWorkflowProjectorDefinition(workflowProjectorCreateDefinition.value)
  const busyKey = workflowProjectorCreateFamilyId.value ? `create-v1:${workflowProjectorCreateFamilyId.value}` : `create-family:${key}`
  workflowProjectorBusy.value = busyKey
  workflowProjectorError.value = ''
  let familyCreated = false
  let createdFamily: WorkflowProjector | null = null
  try {
    let created: WorkflowProjectorVersion
    try {
      if (!workflowProjectorCreateFamilyId.value) {
        const family = await platformApi.createWorkflowProjector({
          key,
          name,
          description: workflowProjectorCreateFamily.description.trim(),
          is_active: true,
        }, 'planned_change')
        createdFamily = family
        workflowProjectorCreateFamilyId.value = family.id
        workflowProjectorCreateFamily.key = family.key
        workflowProjectorCreateFamily.name = family.name
        workflowProjectorCreateFamily.description = family.description
        familyCreated = true
      }
      created = await platformApi.createWorkflowProjectorVersion({
        projector: workflowProjectorCreateFamilyId.value,
        engine_key: engine.key,
        engine_version: engine.version,
        provider_driver_type: workflowProjectorCreateVersion.providerDriverType,
        operation: workflowProjectorCreateVersion.operation,
        output_kind: workflowProjectorCreateVersion.outputKind,
        definition,
      }, 'planned_change')
    } catch (cause) {
      if (familyCreated) {
        try { await reloadWorkflowProjectorCatalog() } catch { /* 原始创建错误优先展示。 */ }
        workflowProjectorError.value = `方案族已创建，但 v1 草稿未创建；请修正下方 definition 后重试，页面不会伪装为完整成功。${message(cause)}`
      } else workflowProjectorError.value = message(cause)
      return
    }

    let refreshError = ''
    try {
      await reloadWorkflowProjectorCatalog()
      const next = { ...sectionErrors.value }; delete next.workflowProjectorRefresh; sectionErrors.value = next
    } catch (cause) {
      refreshError = message(cause)
      if (createdFamily && !workflowProjectors.value.some((item) => item.id === createdFamily?.id)) {
        workflowProjectors.value = [...workflowProjectors.value, createdFamily]
      }
      if (!workflowProjectorVersions.value.some((item) => item.id === created.id)) {
        workflowProjectorVersions.value = [...workflowProjectorVersions.value, created]
      }
      sectionErrors.value = {
        ...sectionErrors.value,
        workflowProjectorRefresh: `草稿已创建，但目录刷新失败：${refreshError}`,
      }
    }
    workflowProjectorResumeFromScan.value = false
    workflowProjectorCreateOpen.value = false
    workflowProjectorCreateFamilyId.value = ''
    workflowProjectorCreateSeedVersionId.value = ''
    workflowProjectorCreateAppliedSeedId.value = ''
    workflowProjectorCreateSeedFingerprint.value = ''
    workflowProjectorCreateDefinition.value = {}
    workflowProjectorCreateDefinitionValidity.value = { valid: false, errors: [] }
    workflowProjectorRecommendation.value = null
    workflowProjectorRecommendationSourcePath.value = ''
    notice.value = refreshError
      ? `${created.projectorKey} v${created.version} 草稿已创建；目录刷新失败，已保留本次服务端返回结果，点击“重新读取”即可同步。`
      : `${created.projectorKey} v${created.version} 草稿已创建；发布前不会参与新候选匹配。`
    openWorkflowProjectorEditor(created)
  } finally {
    workflowProjectorBusy.value = ''
  }
}
const workflowProjectorEditorContextId = ref<string>()
const openWorkflowProjectorEditor = (version: WorkflowProjectorVersion, readOnly = false, contextId?: string) => {
  if (!readOnly && version.status !== 'draft') return
  workflowProjectorEditorContextId.value = readOnly ? contextId : undefined
  workflowProjectorReadOnly.value = readOnly
  workflowProjectorEditorVersion.value = version
  workflowProjectorDefinition.value = cloneWorkflowProjectorDefinition(version.definition)
  workflowProjectorDefinitionValidity.value = { valid: false, errors: [] }
  workflowProjectorEditorDraft.engine = workflowProjectorEngineValue(version.engineKey, version.engineVersion)
  workflowProjectorEditorDraft.providerDriverType = version.providerDriverType
  workflowProjectorEditorDraft.operation = version.operation
  workflowProjectorEditorDraft.outputKind = version.outputKind
  workflowProjectorError.value = ''
  workflowProjectorCreateOpen.value = false
  workflowProjectorEditorOpen.value = true
}
const closeWorkflowProjectorEditor = () => {
  if (workflowProjectorBusy.value) return
  projectorNavigationSerial++
  workflowProjectorEditorContextId.value = undefined
  workflowProjectorEditorOpen.value = false
  workflowProjectorEditorVersion.value = null
  workflowProjectorDefinition.value = {}
  workflowProjectorDefinitionValidity.value = { valid: false, errors: [] }
  workflowProjectorError.value = ''
  if (route.query.projector_version) {
    const query = { ...route.query }; delete query.projector_version
    void router.replace({ path: '/settings', query })
  }
}
let projectorNavigationSerial = 0
const projectorNavigationError = ref('')
const projectorNavigationLoading = ref(false)
const workflowReturnId = computed(() => typeof route.query.workflow_context === 'string' && /^[0-9a-f-]{36}$/i.test(route.query.workflow_context) ? route.query.workflow_context : '')
const followWorkflowProjectorRoute = async () => {
  const serial = ++projectorNavigationSerial
  projectorNavigationError.value = ''; projectorNavigationLoading.value = false
  if (tab.value !== 'workflows' || !route.query.projector_version) return
  const id = route.query.projector_version
  const contextId = typeof route.query.workflow_context === 'string' ? route.query.workflow_context : route.query.workflow_context ? 'invalid' : undefined
  if (typeof id !== 'string' || !/^[0-9a-f-]{36}$/i.test(id)) { projectorNavigationError.value = 'Projector 版本链接无效'; return }
  workflowProjectorEditorOpen.value = false
  projectorNavigationLoading.value = true
  try {
    const version = await platformApi.getWorkflowProjectorVersion(id)
    if (serial !== projectorNavigationSerial) return
    openWorkflowProjectorEditor(version, true, contextId)
  } catch (cause) {
    if (serial === projectorNavigationSerial) projectorNavigationError.value = `无法读取指定 Projector 配置：${message(cause)}`
  } finally { if (serial === projectorNavigationSerial) projectorNavigationLoading.value = false }
}
watch([tab, () => route.query.projector_version, () => route.query.workflow_context], () => { void followWorkflowProjectorRoute() }, { immediate: true })
const manageViewedWorkflowProjector = async () => {
  const projectorId = workflowProjectorEditorVersion.value?.projector
  closeWorkflowProjectorEditor()
  workflowProjectorCatalogOpen.value = true
  await nextTick()
  if (projectorId) document.getElementById(`workflow-projector-${projectorId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
const createNextWorkflowProjectorDraft = async (source: WorkflowProjectorVersion) => {
  if (workflowProjectorBusy.value) return
  workflowProjectorBusy.value = `clone:${source.id}`; workflowProjectorError.value = ''
  try {
    const draft = await platformApi.createNextWorkflowProjectorVersion(source.id, {}, 'planned_change')
    await reloadWorkflowProjectorCatalog()
    openWorkflowProjectorEditor(draft)
    notice.value = `${draft.projectorKey} v${draft.version} 后继草稿已创建；当前 published 版本未变化。`
  } catch (cause) { workflowProjectorError.value = message(cause) }
  finally { workflowProjectorBusy.value = '' }
}
const saveWorkflowProjectorDraft = async () => {
  const version = workflowProjectorEditorVersion.value
  if (!version || workflowProjectorReadOnly.value || version.status !== 'draft' || workflowProjectorBusy.value) return
  workflowProjectorBusy.value = `save:${version.id}`; workflowProjectorError.value = ''
  try {
    const engine = workflowProjectorEditorEngine.value
    if (!engine) throw new Error('请选择服务端注册的 Projector 引擎版本。')
    if (!workflowProjectorDefinitionValidity.value.valid) {
      throw new Error(workflowProjectorDefinitionValidity.value.errors[0] || '请先完成 Projector 参数配置。')
    }
    const definition = cloneWorkflowProjectorDefinition(workflowProjectorDefinition.value)
    const updated = await platformApi.updateWorkflowProjectorVersion(version.id, {
      engine_key: engine.key,
      engine_version: engine.version,
      provider_driver_type: workflowProjectorEditorDraft.providerDriverType,
      operation: workflowProjectorEditorDraft.operation,
      output_kind: workflowProjectorEditorDraft.outputKind,
      definition,
    }, 'configuration_correction')
    await reloadWorkflowProjectorCatalog()
    workflowProjectorEditorOpen.value = false
    workflowProjectorEditorVersion.value = null
    workflowProjectorDefinition.value = {}
    workflowProjectorDefinitionValidity.value = { valid: false, errors: [] }
    notice.value = `${updated.projectorKey} v${updated.version} 草稿已保存；尚未影响导入匹配。`
  } catch (cause) { workflowProjectorError.value = message(cause) }
  finally { workflowProjectorBusy.value = '' }
}
const savedWorkflowKey = (item: ComfySavedWorkflowItem) => item.sourceKey
const savedWorkflowPreservedSessionKey = (item: ComfySavedWorkflowItem) => `${item.sourceDeploymentId}:${item.sourcePath}`
const savedWorkflowProfileCandidate = (item: ComfySavedWorkflowItem, profileId: string) => (
  item.modelProfileCandidates.find((row) => row.modelProfileVersionId === profileId)
)
const savedWorkflowProfilesFor = (item: ComfySavedWorkflowItem, draft: SavedWorkflowDraft) => modelProfiles.value.filter((profile) => {
  const candidate = savedWorkflowProfileCandidate(item, profile.id)
  if (workflowModelContextRequested.value && (
    workflowModelContextState.value !== 'ready'
    || profile.id !== workflowModelContextId.value
  )) return false
  if (profile.provider !== item.sourceProviderId || !candidate || !draft.primaryOutputNodeId) return false
  const output = savedWorkflowOutputCandidates(item, draft).find(
    (row) => row.canvasNodeId === draft.primaryOutputNodeId,
  )
  return Boolean(
    output
    && !candidate.rejectionCodes.includes('model_profile_modality_mismatch')
    && [output.suggestedOutputKind, 'multimodal'].includes(candidate.modality),
  )
})
const savedWorkflowSourceFor = (item: ComfySavedWorkflowItem) => savedWorkflowSources.value.find((source) => source.sourceScopeSha256 === item.sourceScopeSha256)
const savedWorkflowActionLabel = (code: string) => ({
  select_primary_output: '在下方选择服务端识别出的唯一主输出',
  select_model_profile: '在下方选择证据完整的模型能力版本',
  register_model_profile: '登记该工作流实际使用的模型并补齐文件证明',
  attest_or_publish_model_profile: '完成模型文件证明并发布模型能力版本后重新扫描',
  review_source_provenance: '核对该来源的历史模板归属，解决冲突后重新扫描',
  restore_model_deployment: '恢复同来源范围内的模型运行实例后重新扫描',
  fix_in_comfyui: '回到 ComfyUI 修复输出或结构并重新保存，再扫描',
  review_workflow_contract: '查看匹配诊断并生成合同草稿，按具体结构或尺寸阻断处理；无需重复登记已有模型',
  review_execution_snapshot: '核对任务关联；未关联快照不会自动登记',
}[code] || code)
const savedWorkflowBaseName = (item: ComfySavedWorkflowItem) => {
  const parts = item.sourcePath.split('/')
  return (parts[parts.length - 1] || item.sourcePath).replace(/\.json$/i, '')
}
const savedWorkflowSlug = (item: ComfySavedWorkflowItem) => {
  const basename = savedWorkflowBaseName(item) || 'saved-workflow'
  const normalized = basename.normalize('NFKD').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
  return (normalized || `saved-${item.sourceSha256.slice(0, 12)}`).slice(0, 100)
}
const savedWorkflowName = (item: ComfySavedWorkflowItem) => savedWorkflowBaseName(item) || item.sourcePath
const persistSavedWorkflowDraftSession = () => {
  const userId = auth.user?.id || ''
  if (
    !userId
    || savedWorkflowHydrationGuard.active()
  ) return
  const successfulDeploymentIds = new Set(
    savedWorkflowSources.value
      .filter((source) => source.status === 'succeeded')
      .map((source) => source.sourceDeploymentId),
  )
  if (!successfulDeploymentIds.size) return
  const itemsByDeployment = new Map<string, Record<string, SavedWorkflowDraftSessionItem>>(
    [...successfulDeploymentIds].map((deploymentId) => [deploymentId, {}]),
  )
  for (const item of savedWorkflowItems.value) {
    if (!successfulDeploymentIds.has(item.sourceDeploymentId)) continue
    const draft = savedWorkflowDrafts[savedWorkflowKey(item)]
    if (!draft || (item.imported && !item.contractUpgradeRequired)) continue
    const items = itemsByDeployment.get(item.sourceDeploymentId) || {}
    const preserved = savedWorkflowPreservedSessionItems.get(savedWorkflowPreservedSessionKey(item))
    if (preserved) {
      items[item.sourcePath] = preserved
      itemsByDeployment.set(item.sourceDeploymentId, items)
      continue
    }
    if (savedWorkflowIsContextReadOnly(item)) continue
    items[item.sourcePath] = {
      sourceScopeSchemaVersion: item.sourceScopeSchemaVersion,
      sourceScopeSha256: item.sourceScopeSha256,
      sourceSha256: item.sourceSha256,
      selected: draft.selected,
      importKind: draft.importKind,
      primaryOutputNodeId: draft.primaryOutputNodeId,
      modelProfileVersionId: draft.modelProfileVersionId,
      useExistingTemplate: draft.useExistingTemplate,
      templateId: draft.templateId,
      newTemplateName: draft.newTemplateName,
      newTemplateSlug: draft.newTemplateSlug,
      outputKind: draft.outputKind,
      objectInfoSchemaSha256: draft.reviewObjectInfoSchemaSha256,
      convertedGraphSha256: draft.reviewConvertedGraphSha256,
      parameterSchemaSha256: draft.reviewParameterSchemaSha256,
      structuralContractSha256: draft.reviewStructuralContractSha256,
    }
    itemsByDeployment.set(item.sourceDeploymentId, items)
  }
  const persisted = [...itemsByDeployment.entries()].every(([deploymentId, items]) => (
    writeSavedWorkflowDraftSession(window.localStorage, userId, deploymentId, items)
  ))
  if (!persisted) savedWorkflowError.value = SAVED_WORKFLOW_STORAGE_ERROR
  else if (savedWorkflowError.value === SAVED_WORKFLOW_STORAGE_ERROR) savedWorkflowError.value = ''
}
const outputCandidatesFor = (draft: SavedWorkflowDraft) => draft.preview?.outputCandidates || []
const savedWorkflowOutputCandidates = (item: ComfySavedWorkflowItem, draft: SavedWorkflowDraft) => (
  outputCandidatesFor(draft).length ? outputCandidatesFor(draft) : item.outputCandidates
)
const selectedSavedWorkflowOutputCandidate = (item: ComfySavedWorkflowItem, draft: SavedWorkflowDraft) => (
  savedWorkflowOutputCandidates(item, draft).find((row) => row.canvasNodeId === draft.primaryOutputNodeId) || null
)
const savedWorkflowOutputKindLabel = (kind: string) => kind ? mediaKindLabel(kind) : '待 Projector 解析'
const confirmSavedWorkflowOutputKind = (draft: SavedWorkflowDraft) => {
  // 业务产物语义不改变候选图或刚取得的 object_info 证据；只撤销批量提升勾选。
  draft.selected = false
}
const resetSavedWorkflowReview = (draft: SavedWorkflowDraft, previewError = '') => {
  draft.selected = false
  draft.preview = null
  draft.attestation = null
  draft.reviewObjectInfoSchemaSha256 = ''
  draft.reviewConvertedGraphSha256 = ''
  draft.reviewParameterSchemaSha256 = ''
  draft.reviewStructuralContractSha256 = ''
  draft.reviewRequiresConfirmation = true
  draft.previewError = previewError
}
const applyWorkflowModelContextToSavedDraft = (
  item: ComfySavedWorkflowItem,
  draft: SavedWorkflowDraft,
  options: { restoredConflict?: boolean; preservedSessionItem?: SavedWorkflowDraftSessionItem } = {},
) => {
  if (workflowModelContextState.value !== 'ready' || workflowModelContextProfile.value?.status === 'retired') return
  const targetId = workflowModelContextId.value
  if (!targetId || draft.modelProfileVersionId === targetId) return
  if (item.matchStatus === 'matched') {
    draft.selected = false
    return
  }
  if (options.restoredConflict && draft.modelProfileVersionId) {
    if (options.preservedSessionItem) savedWorkflowPreservedSessionItems.set(savedWorkflowPreservedSessionKey(item), options.preservedSessionItem)
    draft.modelProfileVersionId = ''
    draft.selected = false
    resetSavedWorkflowReview(draft, '已恢复的导入会话绑定其他模型版本；本页未自动覆盖。请确认当前精确模型版本并显式重新生成平台执行合同草稿。')
    return
  }
  const targetIsEligible = savedWorkflowProfilesFor(item, draft).some((profile) => profile.id === targetId)
  if (targetIsEligible) {
    draft.modelProfileVersionId = targetId
    resetSavedWorkflowReview(draft)
  } else if (draft.modelProfileVersionId) {
    draft.modelProfileVersionId = ''
    draft.selected = false
    resetSavedWorkflowReview(draft)
  }
}
const savedWorkflowContextIssue = (item: ComfySavedWorkflowItem, draft: SavedWorkflowDraft) => {
  if (workflowModelContextState.value !== 'ready') return ''
  const targetId = workflowModelContextId.value
  if (savedWorkflowContextKind(item) === 'exact') return ''
  if (workflowModelContextProfile.value?.provider !== item.sourceProviderId) return '该工作流来源属于其他 Provider；页面不会跨 Provider 预选或导入。'
  if (item.matchStatus === 'matched' && item.matchedModelProfileVersionId !== targetId) return `该文件已匹配其他精确模型版本 ${item.matchedModelProfileVersionId || '未知'}；不可在当前上下文原地改挂。请保存新的 ComfyUI revision 后重新扫描。`
  const candidate = savedWorkflowProfileCandidate(item, targetId)
  if (!candidate) return '服务端扫描结果未把当前精确模型版本列为候选；页面不会改选同族或名称相似版本。'
  if (savedWorkflowContextKind(item) === 'contract_blocked_candidate') return `服务端已识别当前精确模型的受管文件依赖、兼容主输出和动态工作流特征，但此 saved revision 未通过受控结构合同：${candidate.rejectionCodes.join('、')}。它不会继承既有已发布工作流的关联；请修复后导入为后继版本。`
  if (!draft.primaryOutputNodeId) return '请先选择服务端识别的主输出；页面随后才会校验并预选当前精确模型版本。'
  if (!savedWorkflowProfilesFor(item, draft).some((profile) => profile.id === targetId)) return `当前精确模型版本与所选主输出不兼容：${candidate.rejectionCodes.join('、') || '服务端未确认可用候选'}。`
  if (draft.modelProfileVersionId !== targetId) return '请确认当前精确模型版本；确认后必须显式重新生成平台执行合同草稿。'
  return ''
}
const applySavedWorkflowPreview = (
  draft: SavedWorkflowDraft,
  preview: ComfySavedWorkflowPreview,
  options: { confirmReview?: boolean } = {},
) => {
  draft.preview = preview
  if (options.confirmReview !== false) {
    draft.reviewObjectInfoSchemaSha256 = preview.objectInfoSchemaSha256
    draft.reviewConvertedGraphSha256 = preview.convertedGraphSha256
    draft.reviewParameterSchemaSha256 = preview.parameterSchemaSha256
    draft.reviewStructuralContractSha256 = preview.structuralContractSha256
    draft.reviewRequiresConfirmation = !preview.contractDraftId
  }
  const candidates = outputCandidatesFor(draft)
  if (!draft.primaryOutputNodeId && candidates.length === 1) {
    draft.primaryOutputNodeId = candidates[0].canvasNodeId
    if (candidates[0].suggestedOutputKind) draft.outputKind = candidates[0].suggestedOutputKind
    return
  }
  const selected = candidates.find((candidate) => candidate.canvasNodeId === draft.primaryOutputNodeId)
  if (selected?.suggestedOutputKind) draft.outputKind = selected.suggestedOutputKind
}
const savedWorkflowAttemptTimestamp = (attempt: RuntimeHostProbeAttempt) => (
  Date.parse(attempt.createdAt || attempt.updatedAt || '') || 0
)
const restoreSavedWorkflowAttestationsAfterScan = async (isCurrent: () => boolean = () => true) => {
  for (const item of savedWorkflowItems.value) {
    if (!isCurrent()) return
    if (savedWorkflowIsContextReadOnly(item)) continue
    const draft = savedWorkflowDrafts[savedWorkflowKey(item)]
    if (!draft || (item.imported && !item.contractUpgradeRequired) || item.errorCode) continue
    if (!draft.modelProfileVersionId || !draft.primaryOutputNodeId) continue
    const persistedReview = {
      objectInfoSchemaSha256: draft.reviewObjectInfoSchemaSha256,
      convertedGraphSha256: draft.reviewConvertedGraphSha256,
      parameterSchemaSha256: draft.reviewParameterSchemaSha256,
      structuralContractSha256: draft.reviewStructuralContractSha256,
    }
    const preview = draft.preview
    if (!preview) continue
    const reconciliation = reconcileSavedWorkflowReviewHashes(persistedReview, {
      objectInfoSchemaSha256: preview.objectInfoSchemaSha256,
      convertedGraphSha256: preview.convertedGraphSha256,
      parameterSchemaSha256: preview.parameterSchemaSha256,
      structuralContractSha256: preview.structuralContractSha256,
    })
    if (reconciliation !== 'exact') {
      draft.reviewRequiresConfirmation = true
      draft.previewError = reconciliation === 'changed'
        ? '保存的结构审阅证据已变化；当前草稿尚未确认，请显式点击“重新生成平台执行合同草稿”复核后再继续。'
        : '旧导入会话缺少完整结构审阅哈希；请显式点击“重新生成平台执行合同草稿”复核后再继续。'
      continue
    }
    draft.reviewRequiresConfirmation = !preview.contractDraftId
    draft.previewError = ''
    const review = {
      deploymentId: item.sourceDeploymentId,
      sourceScopeSchemaVersion: item.sourceScopeSchemaVersion,
      sourceScopeSha256: item.sourceScopeSha256,
      sourcePath: item.sourcePath,
      sourceSha256: item.sourceSha256,
      modelProfileVersionId: draft.modelProfileVersionId,
      primaryOutputNodeId: draft.primaryOutputNodeId,
      objectInfoSchemaSha256: preview.objectInfoSchemaSha256,
      convertedGraphSha256: preview.convertedGraphSha256,
    }
    const attempt = savedWorkflowAttestationAttempts.value
      .filter((row) => attestationMatchesSavedWorkflowReview(row.requestSnapshot, review))
      .sort((left, right) => savedWorkflowAttemptTimestamp(right) - savedWorkflowAttemptTimestamp(left))[0]
    if (!attempt) continue
    draft.attestation = attempt
    if (runtimeProbeActiveStatuses.has(attempt.status)) {
      const timer = window.setTimeout(() => void refreshSavedWorkflowAttestation(item), 2500)
      savedWorkflowAttestationTimers.set(savedWorkflowKey(item), timer)
    }
  }
}
const resetSavedWorkflowDrafts = () => {
  for (const key of Object.keys(savedWorkflowDrafts)) delete savedWorkflowDrafts[key]
  savedWorkflowPreservedSessionItems.clear()
}
const resetSavedWorkflowScanView = () => {
  clearSavedWorkflowAttestationPolls()
  savedWorkflowItems.value = []
  savedWorkflowSources.value = []
  savedWorkflowScanState.value = 'idle'
  savedWorkflowBatchMaxItems.value = null
  savedWorkflowResult.value = null
  savedWorkflowError.value = ''
  showOtherSavedWorkflowResults.value = false
  resetSavedWorkflowDrafts()
}
const scanSavedWorkflows = async () => {
  if (loading.value || !workflowModelContextCanImport.value || savedWorkflowBusy.value) return
  if (!savedWorkflowDirectory.value) {
    savedWorkflowScanState.value = 'failed'
    savedWorkflowError.value = `缺少有效系统配置 ${savedWorkflowDirectoryConfigKey}；请先配置扫描目录。`
    return
  }
  const scanGeneration = ++savedWorkflowScanGeneration
  const loadGenerationSnapshot = settingsLoadGeneration
  const contextSnapshot = JSON.stringify(route.query.model_profile_version ?? null)
  const scanStillCurrent = () => (
    scanGeneration === savedWorkflowScanGeneration
    && settingsLoadGeneration === loadGenerationSnapshot
    && tab.value === 'workflows'
    && JSON.stringify(route.query.model_profile_version ?? null) === contextSnapshot
    && !loading.value
    && workflowModelContextCanImport.value
  )
  showOtherSavedWorkflowResults.value = false
  savedWorkflowBusy.value = 'scan'; savedWorkflowError.value = ''; savedWorkflowResult.value = null
  savedWorkflowScanState.value = 'loading'
  savedWorkflowBatchMaxItems.value = null
  clearSavedWorkflowAttestationPolls()
  let scanResultCommitted = false
  let hydrationCompleted = false
  try {
    await savedWorkflowHydrationGuard.run(async () => {
    const scan = await platformApi.scanComfySavedWorkflows()
    if (!scanStillCurrent()) return
    scanResultCommitted = true
    savedWorkflowItems.value = scan.items
    savedWorkflowSources.value = scan.sources
    savedWorkflowScanState.value = scan.status === 'partial' ? 'partial' : scan.status === 'failed' ? 'failed' : 'fresh'
    savedWorkflowBatchMaxItems.value = scan.effectiveBatchMaxItems
    resetSavedWorkflowDrafts()
    for (const item of scan.items) {
      const persisted = readSavedWorkflowDraftSession(
        window.localStorage,
        auth.user?.id || '',
        item.sourceDeploymentId,
      )
      const suggested = item.suggestedImportKind || 'ignored'
      const upgradeTemplate = legacyWorkflowTemplates.value.find((row) => row.id === item.templateId)
      const upgradeOutputKind = ['image', 'video', 'audio', 'text'].includes(upgradeTemplate?.outputKind || '')
        ? upgradeTemplate?.outputKind as SavedWorkflowDraft['outputKind']
        : 'image'
      const previous = persisted?.items[item.sourcePath]
      const matchesPersistedRevision = savedWorkflowDraftMatchesSource(previous, item)
      const templateSuggestion = item.suggestedTemplate
      const autoPreview = item.preview
      savedWorkflowDrafts[savedWorkflowKey(item)] = {
        selected: false,
        importKind: suggested,
        primaryOutputNodeId: item.matchedPrimaryOutputNodeId,
        modelProfileVersionId: item.matchedModelProfileVersionId,
        useExistingTemplate: templateSuggestion?.useExisting ?? item.contractUpgradeRequired,
        templateId: templateSuggestion?.templateId || (item.contractUpgradeRequired ? item.templateId : ''),
        newTemplateName: templateSuggestion?.name || savedWorkflowName(item),
        newTemplateSlug: templateSuggestion?.slug || savedWorkflowSlug(item),
        outputKind: (item.matchedOutputKind || templateSuggestion?.outputKind || (item.contractUpgradeRequired ? upgradeOutputKind : 'image')) as SavedWorkflowDraft['outputKind'],
        reviewObjectInfoSchemaSha256: autoPreview?.objectInfoSchemaSha256 || '',
        reviewConvertedGraphSha256: autoPreview?.convertedGraphSha256 || '',
        reviewParameterSchemaSha256: autoPreview?.parameterSchemaSha256 || '',
        reviewStructuralContractSha256: autoPreview?.structuralContractSha256 || '',
        reviewRequiresConfirmation: !autoPreview?.contractDraftId,
        projectorSuccessorRequested: false,
        preview: autoPreview,
        previewError: '',
        attestation: null,
      }
      if (matchesPersistedRevision && previous && item.matchStatus !== 'matched') {
        Object.assign(savedWorkflowDrafts[savedWorkflowKey(item)], {
          selected: (!item.imported || item.contractUpgradeRequired) && !item.errorCode && previous.selected,
          importKind: previous.importKind,
          primaryOutputNodeId: previous.primaryOutputNodeId,
          modelProfileVersionId: previous.modelProfileVersionId,
          useExistingTemplate: previous.useExistingTemplate,
          templateId: previous.templateId,
          newTemplateName: previous.newTemplateName,
          newTemplateSlug: previous.newTemplateSlug,
          outputKind: previous.outputKind,
          reviewObjectInfoSchemaSha256: previous.objectInfoSchemaSha256,
          reviewConvertedGraphSha256: previous.convertedGraphSha256,
          reviewParameterSchemaSha256: previous.parameterSchemaSha256,
          reviewStructuralContractSha256: previous.structuralContractSha256,
        })
      }
      if (item.contractUpgradeRequired) {
        Object.assign(savedWorkflowDrafts[savedWorkflowKey(item)], {
          selected: false,
          importKind: 'template_version',
          useExistingTemplate: true,
          templateId: item.templateId,
        })
      }
      applyWorkflowModelContextToSavedDraft(
        item,
        savedWorkflowDrafts[savedWorkflowKey(item)],
        {
          restoredConflict: Boolean(
            matchesPersistedRevision
            && previous?.modelProfileVersionId
            && previous.modelProfileVersionId !== workflowModelContextId.value,
          ),
          preservedSessionItem: previous,
        },
      )
      if (savedWorkflowIsContextReadOnly(item)) {
        if (matchesPersistedRevision && previous) {
          savedWorkflowPreservedSessionItems.set(savedWorkflowPreservedSessionKey(item), previous)
        }
        savedWorkflowDrafts[savedWorkflowKey(item)].selected = false
      }
      if (!savedWorkflowPromotable(item)) {
        savedWorkflowDrafts[savedWorkflowKey(item)].selected = false
      }
    }
    await restoreSavedWorkflowAttestationsAfterScan(scanStillCurrent)
    if (!scanStillCurrent()) return
    notice.value = savedWorkflowModelContext.value
      ? `已扫描 ${scan.succeededSourceCount}/${scan.sourceCount} 个 ComfyUI 工作流来源，发现 ${scan.items.length} 个文件；当前模型精确关联 ${currentModelSavedWorkflowExactCount.value} 个，证据兼容候选 ${currentModelSavedWorkflowEligibleCount.value} 个，Live Validate 待就绪 ${currentModelSavedWorkflowReviewCount.value} 个，结构合同受阻候选 ${currentModelSavedWorkflowContractBlockedCount.value} 个，其他扫描结果 ${otherSavedWorkflowItems.value.length} 个（默认折叠）。`
      : `已扫描 ${scan.succeededSourceCount}/${scan.sourceCount} 个 ComfyUI 工作流来源，发现 ${scan.items.length} 个文件；自动匹配 ${scan.matchedCount} 个，待处理 ${scan.reviewCount} 个。`
    if (scan.status === 'partial') {
      savedWorkflowError.value = `有 ${scan.failedSourceCount} 个来源扫描失败；失败来源不会显示为空目录，本次仅允许处理成功来源。`
    }
    hydrationCompleted = true
    })
  } catch (cause) {
    if (!scanStillCurrent()) return
    if (scanResultCommitted) resetSavedWorkflowScanView()
    savedWorkflowScanState.value = 'failed'
    savedWorkflowError.value = `本次扫描失败，旧结果已标记为不可操作：${message(cause)}`
  }
  finally {
    const currentAtCompletion = scanStillCurrent()
    const ownsBusyState = scanGeneration === savedWorkflowScanGeneration && savedWorkflowBusy.value === 'scan'
    if (ownsBusyState && !currentAtCompletion) resetSavedWorkflowScanView()
    if (ownsBusyState) savedWorkflowBusy.value = ''
    if (hydrationCompleted && currentAtCompletion) {
      persistSavedWorkflowDraftSession()
    }
  }
}
const savedWorkflowOperationGuard = (
  item: ComfySavedWorkflowItem,
  draft: SavedWorkflowDraft,
  options: { allowCandidateAdmission?: boolean } = {},
) => {
  const key = savedWorkflowKey(item)
  const scanGenerationSnapshot = savedWorkflowScanGeneration
  const loadGenerationSnapshot = settingsLoadGeneration
  const contextSnapshot = JSON.stringify(route.query.model_profile_version ?? null)
  return () => (
    scanGenerationSnapshot === savedWorkflowScanGeneration
    && loadGenerationSnapshot === settingsLoadGeneration
    && tab.value === 'workflows'
    && JSON.stringify(route.query.model_profile_version ?? null) === contextSnapshot
    && savedWorkflowScanActionable.value
    && workflowModelContextCanImport.value
    && savedWorkflowDrafts[key] === draft
    && savedWorkflowItems.value.some((row) => row.sourceKey === item.sourceKey && row.sourceSha256 === item.sourceSha256)
    && (options.allowCandidateAdmission || !savedWorkflowIsContextReadOnly(item))
  )
}
const savedWorkflowCanManageCandidate = (item: ComfySavedWorkflowItem) => Boolean(
  savedWorkflowScanActionable.value
  && !item.candidateManaged
  && !item.errorCode
  && item.suggestedImportKind === 'template_version'
  && (!savedWorkflowModelContext.value || item.sourceProviderId === savedWorkflowModelContext.value.providerId)
)
const manageSavedWorkflowCandidate = async (item: ComfySavedWorkflowItem) => {
  const draft = savedWorkflowDrafts[savedWorkflowKey(item)]
  if (!draft || !savedWorkflowCanManageCandidate(item) || savedWorkflowBusy.value) return
  const operationStillCurrent = savedWorkflowOperationGuard(item, draft, { allowCandidateAdmission: true })
  const busyKey = `candidate:${item.sourcePath}`
  savedWorkflowBusy.value = busyKey; draft.previewError = ''; savedWorkflowResult.value = null
  const request: ComfySavedWorkflowBatchItemInput = {
    sourcePath: item.sourcePath,
    expectedSha256: item.sourceSha256,
    expectedSourceScopeSchemaVersion: item.sourceScopeSchemaVersion,
    expectedSourceScopeSha256: item.sourceScopeSha256,
    importKind: 'candidate',
  }
  try {
    const result = await platformApi.batchImportComfySavedWorkflows(item.sourceDeploymentId, [request])
    assertSavedWorkflowBatchResultMatchesRequest(item.sourceDeploymentId, [request], result)
    if (!operationStillCurrent()) return
    const row = result.items[0]
    if (!row || row.result === 'failed' || !row.sourceId) {
      throw new Error(row?.detail || row?.code || '候选纳管未返回不可变来源记录')
    }
    item.candidateManaged = true
    item.candidateSourceId = row.sourceId
    resetSavedWorkflowReview(draft)
    notice.value = `已纳管不可变候选：${item.sourcePath}。下一步生成平台执行合同草稿。`
  } catch (cause) {
    if (operationStillCurrent()) draft.previewError = message(cause)
  } finally {
    if (savedWorkflowBusy.value === busyKey) savedWorkflowBusy.value = ''
  }
}
const previewSavedWorkflow = async (item: ComfySavedWorkflowItem) => {
  const draft = savedWorkflowDrafts[savedWorkflowKey(item)]
  if (savedWorkflowIsContextReadOnly(item) || !item.candidateManaged || !savedWorkflowScanActionable.value || !draft || draft.importKind !== 'template_version' || savedWorkflowBusy.value) return
  const operationStillCurrent = savedWorkflowOperationGuard(item, draft)
  const busyKey = `preview:${item.sourcePath}`
  draft.reviewRequiresConfirmation = true
  savedWorkflowBusy.value = busyKey; draft.previewError = ''; draft.attestation = null
  try {
    const preview = await platformApi.previewComfySavedWorkflow(item.sourceDeploymentId, {
      sourcePath: item.sourcePath,
      expectedSha256: item.sourceSha256,
      expectedSourceScopeSchemaVersion: item.sourceScopeSchemaVersion,
      expectedSourceScopeSha256: item.sourceScopeSha256,
      primaryOutputNodeIds: draft.primaryOutputNodeId ? [draft.primaryOutputNodeId] : [],
      modelProfileVersion: draft.modelProfileVersionId || null,
    })
    if (!operationStillCurrent()) return
    applySavedWorkflowPreview(draft, preview)
  } catch (cause) {
    if (operationStillCurrent()) draft.previewError = message(cause)
  } finally {
    if (savedWorkflowBusy.value === busyKey) savedWorkflowBusy.value = ''
  }
}
const savedWorkflowCanRecommendProjector = (item: ComfySavedWorkflowItem) => {
  const draft = savedWorkflowDrafts[savedWorkflowKey(item)]
  return Boolean(
    auth.user?.isSuperuser
    && savedWorkflowScanActionable.value
    && workflowModelContextCanImport.value
    && !savedWorkflowIsContextReadOnly(item)
    && !item.errorCode
    && item.suggestedImportKind === 'template_version'
    && item.candidateManaged
    && item.candidateSourceId
    && draft?.primaryOutputNodeId
    && draft.importKind === 'template_version',
  )
}
const savedWorkflowCanReprojectAsSuccessor = (item: ComfySavedWorkflowItem) => {
  const draft = savedWorkflowDrafts[savedWorkflowKey(item)]
  return Boolean(
    savedWorkflowCanRecommendProjector(item)
    && item.imported
    && !item.contractUpgradeRequired
    && item.workflowVersionId
    && item.templateId
    && item.matchedModelProfileVersionId
    && item.matchedOutputKind
    && draft,
  )
}
const recommendWorkflowProjectorFromCandidate = async (item: ComfySavedWorkflowItem) => {
  const draft = savedWorkflowDrafts[savedWorkflowKey(item)]
  if (
    !draft || !savedWorkflowCanRecommendProjector(item)
    || savedWorkflowBusy.value || workflowProjectorBusy.value
  ) return
  if (!workflowProjectorOptions.value) {
    draft.previewError = 'Projector 表单合同尚未读取；请点击页面右上角“重新读取”后再生成建议。'
    return
  }
  if (!confirmWorkflowProjectorReinitialize()) return
  const operationStillCurrent = savedWorkflowOperationGuard(item, draft)
  const busyKey = `projector-recommendation:${item.sourcePath}`
  savedWorkflowBusy.value = busyKey
  draft.previewError = ''
  try {
    const recommendation = await platformApi.recommendWorkflowProjector({
      candidate_source_id: item.candidateSourceId,
      expected_source_scope_schema_version: item.sourceScopeSchemaVersion,
      expected_source_scope_sha256: item.sourceScopeSha256,
      expected_source_sha256: item.sourceSha256,
      primary_output_node_id: draft.primaryOutputNodeId,
      output_kind: draft.outputKind,
    })
    if (!operationStillCurrent()) return
    if (
      recommendation.sourceEvidence.candidateSourceId !== item.candidateSourceId
      || recommendation.sourceEvidence.sourceScopeSha256 !== item.sourceScopeSha256
      || recommendation.sourceEvidence.sourceSha256 !== item.sourceSha256
      || recommendation.sourceEvidence.primaryCanvasNodeId !== draft.primaryOutputNodeId
      || recommendation.outputKind !== draft.outputKind
    ) throw new Error('服务端 Projector 建议与当前候选、主输出或产物类型不一致，请重新扫描。')

    if (!workflowProjectorOptions.value.engines.some(engine => engine.key === recommendation.engineKey && engine.version === recommendation.engineVersion)) {
      throw new Error('推荐使用的引擎未在当前服务端目录注册，请重新读取目录。')
    }
    if (!workflowProjectorCreateOpen.value && !workflowProjectorResumeFromScan.value) {
      workflowProjectorCreateFamilyId.value = ''
      Object.assign(workflowProjectorCreateFamily, { key: '', name: '', description: '' })
    }
    clearWorkflowProjectorInitialization()
    workflowProjectorCreateMode.value = 'recommendation'
    workflowProjectorResumeFromScan.value = false
    workflowProjectorCreateCandidateKey.value = savedWorkflowKey(item)
    workflowProjectorRecommendation.value = recommendation
    workflowProjectorRecommendationSourcePath.value = item.sourcePath
    workflowProjectorCreateVersion.engine = workflowProjectorEngineValue(
      recommendation.engineKey, recommendation.engineVersion,
    )
    workflowProjectorCreateVersion.providerDriverType = recommendation.providerDriverType
    workflowProjectorCreateVersion.operation = recommendation.operation
    workflowProjectorCreateVersion.outputKind = recommendation.outputKind
    workflowProjectorCreateDefinition.value = cloneWorkflowProjectorDefinition(recommendation.definition)
    workflowProjectorCreateDefinitionValidity.value = { valid: false, errors: [] }
    workflowProjectorCreateAppliedSeedId.value = `recommendation:${recommendation.recommendationSha256}`
    workflowProjectorCreateSeedFingerprint.value = currentWorkflowProjectorCreateSeedFingerprint()
    workflowProjectorError.value = ''
    workflowProjectorEditorOpen.value = false
    workflowProjectorCreateOpen.value = true
    notice.value = '候选扫描建议已填入未保存的 Projector 表单；请命名方案族并逐项复核，保存后仍只是草稿。'
  } catch (cause) {
    if (operationStillCurrent()) {
      draft.previewError = apiErrorSummary(cause)
      if (workflowProjectorCreateOpen.value) workflowProjectorError.value = draft.previewError
    }
  } finally {
    if (savedWorkflowBusy.value === busyKey) savedWorkflowBusy.value = ''
  }
}
const reprojectSavedWorkflowAsSuccessor = async (item: ComfySavedWorkflowItem) => {
  const draft = savedWorkflowDrafts[savedWorkflowKey(item)]
  if (!draft || !savedWorkflowCanReprojectAsSuccessor(item) || savedWorkflowBusy.value) return
  draft.importKind = 'template_version'
  draft.useExistingTemplate = true
  draft.templateId = item.templateId
  draft.modelProfileVersionId = item.matchedModelProfileVersionId
  draft.outputKind = item.matchedOutputKind as SavedWorkflowDraft['outputKind']
  draft.projectorSuccessorRequested = true
  resetSavedWorkflowReview(draft)
  notice.value = '正在用当前已发布 Projector 为同一候选生成后继合同草稿；现有工作流版本不会被改写。'
  await previewSavedWorkflow(item)
  if (draft.preview?.contractDraftId) {
    notice.value = '后继合同草稿已生成；请核对页面显示的 Projector 身份，确认后勾选并提升为原模板的新版本。'
  }
}
const refreshSavedWorkflowContract = (item: ComfySavedWorkflowItem) => {
  if (savedWorkflowIsContextReadOnly(item)) return
  const draft = savedWorkflowDrafts[savedWorkflowKey(item)]
  if (draft?.primaryOutputNodeId) {
    const candidate = savedWorkflowOutputCandidates(item, draft).find(
      (row) => row.canvasNodeId === draft.primaryOutputNodeId,
    )
    if (candidate?.suggestedOutputKind) draft.outputKind = candidate.suggestedOutputKind
  }
  if (draft) resetSavedWorkflowReview(draft)
  const eligibleProfiles = draft ? savedWorkflowProfilesFor(item, draft) : []
  if (draft && workflowModelContextState.value === 'ready' && !draft.modelProfileVersionId) {
    const target = eligibleProfiles.find((row) => row.id === workflowModelContextId.value)
    if (target) {
      draft.modelProfileVersionId = target.id
      resetSavedWorkflowReview(draft)
    }
  }
  if (draft && !eligibleProfiles.some((row) => row.id === draft.modelProfileVersionId)) {
    draft.modelProfileVersionId = ''
    resetSavedWorkflowReview(draft)
  }
  if (draft?.modelProfileVersionId && draft.modelProfileVersionId === workflowModelContextId.value) {
    savedWorkflowPreservedSessionItems.delete(savedWorkflowPreservedSessionKey(item))
  }
}
const openSavedWorkflowModelDraft = (item: ComfySavedWorkflowItem) => {
  if (savedWorkflowIsContextReadOnly(item) || !savedWorkflowScanActionable.value || savedWorkflowBusy.value) return
  openEditor('model')
  if (item.sourceProviderId) editorPayload.value = { ...editorPayload.value, provider: item.sourceProviderId }
}
const refreshSavedWorkflowAttestation = async (item: ComfySavedWorkflowItem) => {
  const key = savedWorkflowKey(item)
  const timer = savedWorkflowAttestationTimers.get(key)
  if (timer) window.clearTimeout(timer)
  savedWorkflowAttestationTimers.delete(key)
  if (savedWorkflowIsContextReadOnly(item)) {
    return
  }
  const draft = savedWorkflowDrafts[key]
  const attemptId = draft?.attestation?.id
  if (!draft || !attemptId) return
  const operationStillCurrent = savedWorkflowOperationGuard(item, draft)
  const refreshStillCurrent = () => operationStillCurrent() && draft.attestation?.id === attemptId
  let refreshedAttempt: RuntimeHostProbeAttempt | null = null
  try {
    refreshedAttempt = await platformApi.getRuntimeHostProbeAttempt(attemptId)
    if (!refreshStillCurrent()) return
    if (runtimeProbeActiveStatuses.has(refreshedAttempt.status)) {
      draft.attestation = refreshedAttempt
      const timer = window.setTimeout(() => void refreshSavedWorkflowAttestation(item), 2500)
      savedWorkflowAttestationTimers.set(key, timer)
      return
    }
    if (refreshedAttempt.status === 'succeeded') {
      const refreshedProfiles = await platformApi.listModelProfiles()
      if (!refreshStillCurrent()) return
      draft.attestation = refreshedAttempt
      modelProfiles.value = refreshedProfiles
      notice.value = `模型文件证明已完成：${refreshedAttempt.id}。请审核并发布该模型版本，再显式重新生成平台执行合同草稿。`
    } else {
      draft.attestation = refreshedAttempt
      draft.previewError = refreshedAttempt.errorMessage || refreshedAttempt.errorCode || `模型证明状态：${refreshedAttempt.status}`
    }
  } catch (cause) {
    if (!refreshStillCurrent()) return
    if (refreshedAttempt) draft.attestation = refreshedAttempt
    draft.previewError = `刷新模型证明任务失败：${message(cause)}`
  }
}
const attestSavedWorkflowModels = async (item: ComfySavedWorkflowItem) => {
  const draft = savedWorkflowDrafts[savedWorkflowKey(item)]
  const profile = draft
    ? savedWorkflowProfilesFor(item, draft).find((row) => row.id === draft.modelProfileVersionId)
    : undefined
  if (
    savedWorkflowIsContextReadOnly(item)
    || !savedWorkflowScanActionable.value
    || !draft?.preview
    || draft.preview.modelDependencies.length === 0
    || !profile
    || profile.status !== 'draft'
    || !draft.primaryOutputNodeId
    || savedWorkflowBusy.value
  ) return
  const operationStillCurrent = savedWorkflowOperationGuard(item, draft)
  const busyKey = `attest:${item.sourcePath}`
  persistSavedWorkflowDraftSession()
  savedWorkflowBusy.value = busyKey; draft.previewError = ''
  try {
    const attempt = await platformApi.attestComfySavedWorkflowModelArtifacts(
      item.sourceDeploymentId,
      {
        sourcePath: item.sourcePath,
        expectedSha256: item.sourceSha256,
        expectedSourceScopeSchemaVersion: item.sourceScopeSchemaVersion,
        expectedSourceScopeSha256: item.sourceScopeSha256,
        primaryOutputNodeIds: [draft.primaryOutputNodeId],
        expectedObjectInfoSchemaSha256: draft.preview.objectInfoSchemaSha256,
        expectedConvertedGraphSha256: draft.preview.convertedGraphSha256,
        modelProfileVersion: profile.id,
      },
    )
    if (!operationStillCurrent() || draft.modelProfileVersionId !== profile.id) return
    draft.attestation = attempt
    savedWorkflowAttestationAttempts.value = [
      attempt,
      ...savedWorkflowAttestationAttempts.value.filter((row) => row.id !== attempt.id),
    ]
    notice.value = `模型文件证明任务 ${draft.attestation.id} 已进入持久队列。`
    void refreshSavedWorkflowAttestation(item)
  } catch (cause) {
    if (operationStillCurrent()) draft.previewError = message(cause)
  } finally {
    if (savedWorkflowBusy.value === busyKey) savedWorkflowBusy.value = ''
  }
}
const hardSavedWorkflowBlockers = (draft: SavedWorkflowDraft) => draft.preview?.blockers || []
const savedWorkflowDomId = (item: ComfySavedWorkflowItem) => (
  `saved-workflow-${item.sourceScopeSha256.slice(0, 10)}-${item.sourceSha256.slice(0, 12)}`
)
const savedWorkflowRecoveryLabel = (draft: SavedWorkflowDraft, code: string, detail = '') => {
  if (
    code === 'required_dimension_multiple_unresolved'
    && detail.includes('aspect_megapixels')
  ) {
    return '当前尺寸策略没有生产运行适配器；请改用显式 width/height 后保存新 revision 并重新扫描，或先发布受审的尺寸运行适配器'
  }
  return draft.preview?.recoveryActions.find((action) => action.code === code)?.label
    || '按阻断说明修复后重新检查合同'
}
const savedWorkflowStepState = (
  item: ComfySavedWorkflowItem,
  draft: SavedWorkflowDraft,
  step: number,
) => {
  if (item.imported && !item.contractUpgradeRequired) return 'complete'
  if (step === 1) return item.candidateManaged ? 'complete' : 'current'
  if (step === 2) {
    if (!item.candidateManaged) return 'pending'
    if (!draft.preview?.contractDraftId) return 'current'
    return hardSavedWorkflowBlockers(draft).length ? 'blocked' : 'complete'
  }
  if (savedWorkflowPromotable(item)) return 'current'
  if (hardSavedWorkflowBlockers(draft).length || draft.preview?.actionability === 'candidate_only') return 'blocked'
  return 'pending'
}
const savedWorkflowStepLabel = (
  item: ComfySavedWorkflowItem,
  draft: SavedWorkflowDraft,
  step: number,
) => {
  if (item.imported && !item.contractUpgradeRequired && !draft.projectorSuccessorRequested) return '已完成'
  if (step === 1) return item.candidateManaged ? '候选已纳管' : '需要纳管候选'
  if (step === 2) {
    if (!item.candidateManaged) return '等待步骤 1'
    if (!draft.preview?.contractDraftId) return '需要生成合同'
    const blockerCount = hardSavedWorkflowBlockers(draft).length
    return blockerCount ? `已生成，${blockerCount} 项阻断` : '执行合同已就绪'
  }
  if (savedWorkflowPromotable(item)) return '可以加入提升'
  const blockerCount = hardSavedWorkflowBlockers(draft).length
  if (blockerCount) return `等待解决 ${blockerCount} 项阻断`
  if (!draft.preview?.contractDraftId) return '等待步骤 2'
  return '尚未满足提升条件'
}
const savedWorkflowContractProjection = (draft: SavedWorkflowDraft) => {
  const contract = draft.preview?.suggestedInputContract
  if (!contract || typeof contract !== 'object' || Array.isArray(contract)) return ''
  const compiler = contract.compiler
  const projector = contract.contract_projector
  const conditioning = contract.conditioning_contract
  const compilerRow = compiler && typeof compiler === 'object' && !Array.isArray(compiler) ? compiler as JsonObject : null
  const projectorRow = projector && typeof projector === 'object' && !Array.isArray(projector) ? projector as JsonObject : null
  const conditioningRow = conditioning && typeof conditioning === 'object' && !Array.isArray(conditioning) ? conditioning as JsonObject : null
  const modes = Array.isArray(conditioningRow?.modes)
    ? conditioningRow.modes.flatMap((row) => row && typeof row === 'object' && !Array.isArray(row) && typeof (row as JsonObject).key === 'string' ? [String((row as JsonObject).key)] : [])
    : []
  const compilerLabel = compilerRow?.id ? `${String(compilerRow.id)}/v${String(compilerRow.version ?? '—')}` : '未识别'
  const projectorLabel = projectorRow?.id ? `${String(projectorRow.id)}/v${String(projectorRow.version ?? '—')}` : 'generic server projection'
  return `合同解释器 ${compilerLabel} · 服务端投影 ${projectorLabel}${modes.length ? ` · 动态模式 ${modes.join('、')}` : ' · 固定槽位'}`
}
const savedWorkflowPromotable = (item: ComfySavedWorkflowItem) => {
  const draft = savedWorkflowDrafts[savedWorkflowKey(item)]
  if (!['fresh', 'partial'].includes(savedWorkflowScanState.value) || savedWorkflowBatchMaxItems.value === null || !draft || (item.imported && !item.contractUpgradeRequired && !draft.projectorSuccessorRequested) || item.errorCode) return false
  if (savedWorkflowIsContextReadOnly(item)) return false
  if (draft.importKind !== 'template_version') return true
  const profile = modelProfiles.value.find((row) => row.id === draft.modelProfileVersionId)
  const selectedTemplate = legacyWorkflowTemplates.value.find((row) => row.id === draft.templateId)
  const targetReady = item.contractUpgradeRequired || draft.projectorSuccessorRequested
    ? Boolean(item.templateId && draft.useExistingTemplate && draft.templateId === item.templateId && selectedTemplate?.outputKind === draft.outputKind)
    : Boolean(draft.useExistingTemplate ? selectedTemplate?.outputKind === draft.outputKind : (
      draft.newTemplateName.trim()
      && /^[a-z][a-z0-9-]{1,99}$/.test(draft.newTemplateSlug.trim())
    ))
  return Boolean(
    item.candidateManaged
    && item.candidateSourceId
    && !draft.reviewRequiresConfirmation
    && draft.preview?.contractDraftId
    && (draft.preview?.contractDraftRevision || 0) > 0
    && draft.preview?.actionability === 'ready_for_executable_import'
    && draft.preview?.convertedGraphSha256
    && draft.preview.parameterSchemaSha256
    && draft.preview.structuralContractSha256
    && draft.reviewObjectInfoSchemaSha256 === draft.preview.objectInfoSchemaSha256
    && draft.reviewConvertedGraphSha256 === draft.preview.convertedGraphSha256
    && draft.reviewParameterSchemaSha256 === draft.preview.parameterSchemaSha256
    && draft.reviewStructuralContractSha256 === draft.preview.structuralContractSha256
    && draft.primaryOutputNodeId
    && draft.preview.primaryOutputNodeIds.length === 1
    && draft.preview.primaryOutputNodeIds[0] === draft.primaryOutputNodeId
    && (!workflowModelContextRequested.value || (
      workflowModelContextState.value === 'ready'
      && draft.modelProfileVersionId === workflowModelContextId.value
    ))
    && profile
    && ['draft', 'published'].includes(profile.status)
    && targetReady
    && hardSavedWorkflowBlockers(draft).length === 0
  )
}
const savedWorkflowReady = (item: ComfySavedWorkflowItem) => Boolean(
  savedWorkflowDrafts[savedWorkflowKey(item)]?.selected && savedWorkflowPromotable(item),
)
const promotableSavedWorkflowItems = computed(() => currentModelSavedWorkflowItems.value.filter(savedWorkflowPromotable))
const readySavedWorkflowItems = computed(() => savedWorkflowItems.value.filter(savedWorkflowReady))
const selectedSavedWorkflowCount = computed(() => currentModelSavedWorkflowItems.value.filter((item) => (
  savedWorkflowDrafts[savedWorkflowKey(item)]?.selected
  && (!item.imported || item.contractUpgradeRequired || savedWorkflowDrafts[savedWorkflowKey(item)]?.projectorSuccessorRequested)
)).length)
const blockedSavedWorkflowItems = computed(() => currentModelSavedWorkflowItems.value.filter((item) => {
  const draft = savedWorkflowDrafts[savedWorkflowKey(item)]
  return Boolean(
    draft
    && (!item.imported || item.contractUpgradeRequired || draft.projectorSuccessorRequested)
    && (hardSavedWorkflowBlockers(draft).length || draft.preview?.actionability === 'candidate_only'),
  )
}))
const savedWorkflowManagedCount = computed(() => currentModelSavedWorkflowItems.value.filter((item) => item.candidateManaged).length)
const savedWorkflowContractCount = computed(() => currentModelSavedWorkflowItems.value.filter((item) => (
  Boolean(savedWorkflowDrafts[savedWorkflowKey(item)]?.preview?.contractDraftId)
)).length)
const savedWorkflowDiscoveryState = computed(() => {
  const total = currentModelSavedWorkflowItems.value.length
  if (!total) return 'pending'
  return savedWorkflowManagedCount.value === total ? 'complete' : 'current'
})
const savedWorkflowContractState = computed(() => {
  if (!savedWorkflowManagedCount.value) return 'pending'
  if (blockedSavedWorkflowItems.value.length) return 'blocked'
  return savedWorkflowContractCount.value === savedWorkflowManagedCount.value ? 'complete' : 'current'
})
const focusFirstSavedWorkflowBlocker = async () => {
  const first = blockedSavedWorkflowItems.value[0]
  if (!first) return
  savedWorkflowFocusedIssueKey.value = savedWorkflowKey(first)
  await nextTick()
  const target = document.getElementById(savedWorkflowDomId(first))
  target?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  target?.focus({ preventScroll: true })
  window.setTimeout(() => {
    if (savedWorkflowFocusedIssueKey.value === savedWorkflowKey(first)) savedWorkflowFocusedIssueKey.value = ''
  }, 2400)
}
const savedWorkflowBatchPayload = (item: ComfySavedWorkflowItem): ComfySavedWorkflowBatchItemInput => {
  const draft = savedWorkflowDrafts[savedWorkflowKey(item)]
  if (draft.importKind !== 'template_version') return {
    sourcePath: item.sourcePath,
    expectedSha256: item.sourceSha256,
    expectedSourceScopeSchemaVersion: item.sourceScopeSchemaVersion,
    expectedSourceScopeSha256: item.sourceScopeSha256,
    importKind: draft.importKind,
  }
  return {
    sourcePath: item.sourcePath,
    expectedSha256: item.sourceSha256,
    expectedSourceScopeSchemaVersion: item.sourceScopeSchemaVersion,
    expectedSourceScopeSha256: item.sourceScopeSha256,
    importKind: draft.importKind,
    primaryOutputNodeIds: [draft.primaryOutputNodeId],
    expectedObjectInfoSchemaSha256: draft.preview?.objectInfoSchemaSha256 || '',
    expectedConvertedGraphSha256: draft.preview?.convertedGraphSha256 || '',
    expectedParameterSchemaSha256: draft.preview?.parameterSchemaSha256 || '',
    expectedStructuralContractSha256: draft.preview?.structuralContractSha256 || '',
    contractDraft: draft.preview?.contractDraftId || null,
    template: draft.useExistingTemplate ? draft.templateId : null,
    newTemplate: draft.useExistingTemplate ? null : {
      name: draft.newTemplateName.trim(),
      slug: draft.newTemplateSlug.trim(),
      outputKind: draft.outputKind,
      description: `从 ComfyUI userdata ${item.sourcePath} 导入`,
    },
    modelProfileVersion: draft.modelProfileVersionId,
  }
}
const batchImportSavedWorkflows = async () => {
  if (!readySavedWorkflowItems.value.length || savedWorkflowBusy.value) return
  savedWorkflowBusy.value = 'import'; savedWorkflowError.value = ''; savedWorkflowResult.value = null
  try {
    const batches = groupSavedWorkflowBatches(
      readySavedWorkflowItems.value.map((item) => ({
        sourceDeploymentId: item.sourceDeploymentId,
        item: savedWorkflowBatchPayload(item),
      })),
      savedWorkflowBatchMaxItems.value,
    )
    let aggregate: SavedWorkflowBatchAggregate | null = null
    let completedBatches = 0
    for (const batch of batches) {
      try {
        const result = await platformApi.batchImportComfySavedWorkflows(
          batch.deploymentId,
          batch.items,
        )
        assertSavedWorkflowBatchResultMatchesRequest(
          batch.deploymentId,
          batch.items,
          result,
        )
        aggregate = mergeSavedWorkflowBatchResults(aggregate, result)
        savedWorkflowResult.value = aggregate
        completedBatches += 1
        for (const imported of result.items.filter((row) => row.result !== 'failed')) {
          const source = savedWorkflowItems.value.find((row) => (
            row.sourceDeploymentId === imported.sourceDeploymentId
            && row.sourcePath === imported.sourcePath
            && row.sourceSha256 === imported.sourceSha256
          ))
          if (!source) continue
          source.imported = true
          source.contractUpgradeRequired = false
          source.sourceId = imported.sourceId
          source.workflowVersionId = imported.workflowVersionId
          const draft = savedWorkflowDrafts[savedWorkflowKey(source)]
          if (draft) draft.projectorSuccessorRequested = false
        }
        persistSavedWorkflowDraftSession()
        notice.value = `正在分批导入：已完成 ${completedBatches}/${batches.length} 批。`
      } catch (cause) {
        savedWorkflowError.value = completedBatches > 0
          ? `已完成 ${completedBatches}/${batches.length} 批；后续批次未提交：${message(cause)}`
          : message(cause)
        break
      }
    }
    if (completedBatches > 0) workflowVersions.value = await platformApi.listWorkflowVersions(workflowVersionListOptions())
    if (completedBatches === batches.length && aggregate) {
      notice.value = `导入完成：按 ${batches.length} 批处理，新增 ${aggregate.createdCount}，复用 ${aggregate.reusedCount}，失败 ${aggregate.failedCount}。`
    }
  } catch (cause) { savedWorkflowError.value = message(cause) }
  finally { savedWorkflowBusy.value = '' }
}
watch(
  [savedWorkflowItems, () => savedWorkflowDrafts],
  () => persistSavedWorkflowDraftSession(),
  { deep: true },
)
const closeAction = (force = false) => { if (saving.value && !force) return; actionOpen.value = false; actionError.value = ''; actionOperation.value = ''; actionReferenceId.value = ''; actionReasonCode.value = '' }
const openAction = (kind: RegistryAction, id: string, title: string, description: string, options: { operation?: string; referenceId?: string; enable?: boolean } = {}) => {
  if (kind === 'publish-revision' || kind === 'activate-revision') {
    const revision = revisions.value.find((item) => item.id === id)
    if (!revision || revision.changesSecurityBoundaries === null) {
      error.value = '该配置修订缺少 changes_security_boundaries 分类事实，已失败关闭；修复服务端响应后才能发布或激活。'
      return
    }
    if (revision?.changesSecurityBoundaries === true && auth.user?.isSuperuser !== true) {
      error.value = '该配置修订会改变安全边界，只有超级管理员可以发布或激活。'
      return
    }
  }
  actionKind.value = kind; actionId.value = id; actionTitle.value = title; actionDescription.value = description
  actionOperation.value = options.operation || ''; actionReferenceId.value = options.referenceId || ''; actionEnable.value = options.enable === true
  actionReasonCode.value = ''; actionError.value = ''; actionOpen.value = true
}
const profileFor = (id: string) => modelProfiles.value.find((item) => item.id === id)
const artifactEditable = (item: ModelArtifact) => profileFor(item.profileVersion)?.status === 'draft'
const artifactEvidenceFor = (item: ModelArtifact) => modelArtifactAvailabilityEvidence(
  item,
  Object.values(latestHostSnapshots.value),
  deployments.value,
  runtimeEvidenceNow.value,
  modelProfiles.value,
  providers.value,
)
const runtimeHostLabel = (id: string) => {
  const host = runtimeHosts.value.find((item) => item.id === id)
  return host?.name || host?.key || id.slice(0, 8)
}
const artifactLatestAvailabilityLabel = (item: ModelArtifact) => {
  const evidence = artifactEvidenceFor(item).latest
  if (!evidence) return '最新主机可用性：尚无主机观测，请先运行主机探测'
  const expired = Date.parse(evidence.snapshot.expiresAt) <= runtimeEvidenceNow.value
  return `最新主机可用性：${runtimeHostLabel(evidence.snapshot.host)} · ${evidence.availability.status} · ${dateTime(evidence.snapshot.capturedAt)} · ${evidence.exactContract ? 'size/hash 精确一致' : 'size/hash 与声明不一致'}${expired ? ' · 快照已过期' : ''}`
}
const artifactVerificationTitle = (item: ModelArtifact) => artifactEvidenceFor(item).verificationCandidate
  ? '使用最新且未过期的精确主机快照核验'
  : '当前没有可用于核验的最新精确主机快照；请先修复文件事实并重新探测运行主机'
const deploymentSmokeOperations = (item: ModelDeployment) => {
  if (item.providerDriver === 'comfyui') return []
  const capabilities = profileFor(item.profileVersion)?.specification.capabilities
  const declared = Array.isArray(capabilities) ? capabilities.map(String) : []
  return declared.filter((value) => ['image.text_to_image', 'video.text_to_video', 'audio.text_to_audio', 'prompt.normalize', 'script.generate', 'script.structure', 'visual_rules.generate'].includes(value))
}
const deploymentAcceptanceFor = (deploymentId: string) => deploymentAcceptanceRows.value?.[deploymentId] || null
const workflowSmokeUrlForDeployment = (item: ModelDeployment) => modelDeploymentWorkflowSmokeUrl(
  deploymentAcceptanceFor(item.id),
  item.id,
)
const modelProfileRetirementBlocked = (group: ModelRegistryVersionGroup) => group.deployments.some((item) => item.status === 'active' || item.status === 'error')
const actionDeployment = computed(() => deployments.value.find((item) => item.id === actionId.value) || null)
const actionSmokeOperations = computed(() => actionDeployment.value ? deploymentSmokeOperations(actionDeployment.value) : [])
const actionSmokeBindingId = ref('')
const actionSmokeNeedsProfile = computed(() => ['prompt.normalize', 'script.generate', 'script.structure', 'visual_rules.generate'].includes(actionOperation.value))
const actionSmokeProfiles = computed(() => deploymentAcceptanceFor(actionId.value)?.deploymentSmoke.operations.find(item => item.operation === actionOperation.value)?.promptProfiles || [])
const actionSmokeSelectionValid = computed(() => !actionSmokeNeedsProfile.value || actionSmokeProfiles.value.some(item => item.promptProfile.prompt_profile_binding_id === actionSmokeBindingId.value))
watch([actionId, actionOperation, actionOpen], () => { actionSmokeBindingId.value = '' })
const smokeResolutionValidation = computed(() => {
  const submission = smokeResolutionDeployment.value?.latestSmoke?.unresolvedSubmission
  const costText = smokeResolutionActualCost.value.trim()
  const cost = costText === '' ? null : Number(costText)
  if (!submission) return '没有可消歧的 smoke submission。'
  const limits = productionCapability.value?.effectiveLimits
  if (!limits) return productionCapabilityError.value || '服务端审计说明限制不可用，不能提交消歧结论。'
  const length = smokeResolutionNote.value.trim().length
  if (length < limits.auditNoteMinCharacters || length > limits.auditNoteMaxCharacters) return `消歧说明必须为 ${limits.auditNoteMinCharacters}–${limits.auditNoteMaxCharacters} 个字符。`
  if (cost !== null && (!Number.isFinite(cost) || cost < 0)) return '实际费用必须是有限、非负数字。'
  if (cost !== null && cost > 0 && !smokeResolutionCurrency.value.trim()) return '实际费用大于 0 时必须填写币种。'
  return ''
})
const pendingConfigReview = computed(() => definitions.value
  .filter((item) => configDirty.value.has(item.key) || configUnsets.value.has(item.key))
  .map((item) => ({
    key: item.key,
    action: configUnsets.value.has(item.key) ? 'restore_inheritance' : 'set_system_value',
    previous_source: effectiveSourceLabel(sourceFor(item.key)),
    previous_value: item.isSecret ? '[受管凭据，不回显]' : resolved.value?.values[item.key],
    next_value: configUnsets.value.has(item.key) ? '[继承下层来源]' : item.isSecret ? '[替换为所选受管凭据]' : (() => { try { return JSON.parse(configDraft.value[item.key] ?? '') } catch { return '[无效 JSON]' } })(),
    restart_required: item.restartRequired,
  })))
const invalidConfigDrafts = computed(() => Object.fromEntries(definitions.value
  .filter((item) => item.isActive && !item.isSecret && configDirty.value.has(item.key) && !configUnsets.value.has(item.key))
  .map((item) => [item.key, parseConfigValue(item, configDraft.value[item.key] ?? '').error])
  .filter(([, failure]) => Boolean(failure))))
const actionEntity = computed<RegistryEntity | null>(() => {
  const rows: RegistryEntity[] = [...providers.value, ...modelProfiles.value, ...deployments.value, ...modelArtifacts.value, ...workflowVersions.value, ...workflowProjectors.value, ...workflowProjectorVersions.value, ...recipes.value, ...definitions.value, ...revisions.value, ...runtimeHosts.value]
  return rows.find((item) => item.id === actionId.value) || null
})
const actionReviewPayload = computed(() => actionKind.value === 'save-defaults' ? pendingConfigReview.value : actionEntity.value)
const deletionResourceTypes: Partial<Record<RegistryAction, RegistryDeletionResourceType>> = {
  'delete-provider': 'provider_profile',
  'delete-model': 'model_profile_version',
  'delete-deployment': 'model_deployment',
  'delete-artifact': 'model_artifact',
  'delete-workflow': 'workflow_template_version',
  'delete-recipe': 'production_recipe_version',
  'delete-definition': 'config_definition',
}
const actionDeletionResourceType = computed(() => deletionResourceTypes[actionKind.value] || null)
const actionIsDeletion = computed(() => actionDeletionResourceType.value !== null)
const destructiveActions = new Set<RegistryAction>(['retire-model', 'delete-provider', 'delete-model', 'retire-deployment', 'delete-deployment', 'delete-artifact', 'delete-workflow', 'retire-workflow', 'discard-workflow-projector', 'retire-workflow-projector', 'delete-recipe', 'retire-recipe', 'delete-definition'])
const auditReasonActions = new Set<RegistryAction>(['publish-model', 'retire-model', 'delete-provider', 'delete-model', 'deployment-status', 'retire-deployment', 'delete-deployment', 'delete-artifact', 'delete-workflow', 'retire-workflow', 'workflow-projector-status', 'publish-workflow-projector', 'discard-workflow-projector', 'retire-workflow-projector', 'publish-recipe', 'delete-recipe', 'retire-recipe', 'delete-definition', 'save-defaults', 'publish-revision', 'activate-revision', 'host-status'])
const actionReasonRequired = computed(() => auditReasonActions.has(actionKind.value))
const lifecycleObjectTypes = computed(() => [...new Set(lifecycleAudit.value.map((item) => item.objectType))].sort())
const lifecycleActions = computed(() => [...new Set(lifecycleAudit.value.map((item) => item.action))].sort())
const lifecycleAuditByFacet = computed(() => lifecycleAudit.value.filter((item) => (
  (!lifecycleObjectFilter.value || item.objectType === lifecycleObjectFilter.value)
  && (!lifecycleActionFilter.value || item.action === lifecycleActionFilter.value)
)))
const lifecycleAuditListState = computed(() => filterSettingsList(lifecycleAuditByFacet.value, {
  search: settingsListSearch.value, showRetired: showRetiredSettingsRecords.value,
  isRetired: () => false,
  searchableValues: (item) => [item.objectType, item.objectId, item.action, item.reasonCode, item.actorUsername, item.beforeSha256, item.afterSha256, item.createdAt],
}))
const filteredLifecycleAudit = computed(() => lifecycleAuditListState.value.items)
const actionConfirmLabel = computed(() => {
  if (actionKind.value.startsWith('delete-')) return '确认删除'
  if (actionKind.value.startsWith('retire-')) return '确认退役'
  if (actionKind.value === 'deployment-smoke') return '确认并执行真实烟测'
  if (actionKind.value === 'deployment-probe') return '确认探测'
  if (actionKind.value === 'verify-artifact') return '确认写入核验证据'
  if (actionKind.value === 'deployment-status') return actionEnable.value ? '确认重新启用' : '确认停用'
  if (actionKind.value === 'host-status') return actionEnable.value ? '确认启用' : '确认停用'
  if (actionKind.value === 'workflow-projector-status') return actionEnable.value ? '确认启用方案族' : '确认停用方案族'
  if (actionKind.value === 'publish-workflow-projector') return '确认发布方案版本'
  if (actionKind.value === 'discard-workflow-projector') return '确认放弃草稿'
  return '确认提交'
})
const openArtifactVerification = (item: ModelArtifact) => {
  const candidate = artifactEvidenceFor(item).verificationCandidate
  if (!candidate) {
    error.value = '没有最新、未过期且 available，并与声明 size/hash 精确一致的主机可用性证据；请到“运行主机”执行探测后再核验。'
    return
  }
  openAction('verify-artifact', item.id, `核验模型文件 · ${item.logicalName || item.filename}`, `将使用 ${runtimeHostLabel(candidate.snapshot.host)} 的最新主机快照 ${candidate.snapshot.id.slice(0, 8)}；available 状态、文件大小与 SHA-256 均和声明精确一致，快照与操作者会持久化。`, { referenceId: candidate.snapshot.id })
}
const canModifyConfigDefinition = (item: ConfigDefinition) => canWriteCurrentSection.value && item.isActive && item.configurationClass === currentConfigurationClass.value
const canWriteConfigRevision = (item: ConfigRevision) => item.changesSecurityBoundaries === false || (item.changesSecurityBoundaries === true && auth.user?.isSuperuser === true)
const markConfigDirty = (key: string) => { const item = definitions.value.find((row) => row.key === key); if (!item || !canModifyConfigDefinition(item)) return; const next = new Set(configDirty.value); next.add(key); configDirty.value = next; const unsets = new Set(configUnsets.value); unsets.delete(key); configUnsets.value = unsets }
const restoreConfigInheritance = (key: string) => { const item = definitions.value.find((row) => row.key === key); if (!item || !canModifyConfigDefinition(item) || item.configurationClass !== 'standard') return; const next = new Set(configUnsets.value); next.add(key); configUnsets.value = next; const dirty = new Set(configDirty.value); dirty.delete(key); configDirty.value = dirty }
const undoConfigChange = (key: string) => { configDraft.value[key] = configOriginal.value[key] ?? ''; const dirty = new Set(configDirty.value); dirty.delete(key); configDirty.value = dirty; const unsets = new Set(configUnsets.value); unsets.delete(key); configUnsets.value = unsets }
const saveDefaults = () => {
  try {
    if (!canWriteCurrentSection.value) throw new Error(`只有超级管理员可以修改${currentSettingsSection.value.label}；当前页面为只读。`)
    if (Object.keys(invalidConfigDrafts.value).length) throw new Error('存在未通过本地结构校验的配置项，请先修正。')
    const changes = buildSystemConfigChangeSet({ definitions: definitions.value, draftText: configDraft.value, dirtyKeys: configDirty.value, unsetKeys: configUnsets.value, secretSelections: secretSelections.value, isSuperuser: auth.user?.isSuperuser === true })
    if (!Object.keys(changes.values).length && !changes.unsetKeys.length) throw new Error('没有待发布的配置变更')
    openAction(
      'save-defaults',
      '',
      tab.value === 'security' ? '核对并发布安全边界' : '核对并发布系统配置',
      `仅提交 ${Object.keys(changes.values).length} 个修改项和 ${changes.unsetKeys.length} 个恢复继承项；未编辑的 model/workflow 默认值不会被提升为 system override。`,
    )
  } catch (cause) { error.value = message(cause) }
}
const runAction = async () => {
  saving.value = true; actionError.value = ''; error.value = ''
  try {
    if (actionKind.value === 'publish-revision' || actionKind.value === 'activate-revision') {
      const revision = revisions.value.find((item) => item.id === actionId.value)
      if (!revision || revision.changesSecurityBoundaries === null) throw new Error('该配置修订缺少 changes_security_boundaries 分类事实，已失败关闭；修复服务端响应后才能发布或激活。')
      if (revision?.changesSecurityBoundaries === true && auth.user?.isSuperuser !== true) throw new Error('该配置修订会改变安全边界，只有超级管理员可以发布或激活。')
    }
    const reasonCode = actionReasonCode.value.trim()
    if (actionReasonRequired.value && !reasonCode) throw new Error('请选择本次高风险操作的审计原因代码。')
    if (actionKind.value === 'publish-model') await platformApi.publishModelProfile(actionId.value, reasonCode)
    else if (actionKind.value === 'retire-model') await platformApi.retireModelProfile(actionId.value, reasonCode)
    else if (actionKind.value === 'deployment-status') await platformApi.updateDeployment(actionId.value, { status: actionEnable.value ? 'active' : 'disabled' }, reasonCode)
    else if (actionKind.value === 'retire-deployment') await platformApi.retireDeployment(actionId.value, reasonCode)
    else if (actionKind.value === 'verify-artifact') await platformApi.verifyModelArtifact(actionId.value, actionReferenceId.value)
    else if (actionKind.value === 'retire-workflow') await platformApi.retireWorkflow(actionId.value, reasonCode)
    else if (actionKind.value === 'workflow-projector-status') await platformApi.updateWorkflowProjector(actionId.value, { is_active: actionEnable.value }, reasonCode)
    else if (actionKind.value === 'publish-workflow-projector') await platformApi.publishWorkflowProjectorVersion(actionId.value, reasonCode)
    else if (actionKind.value === 'discard-workflow-projector') await platformApi.discardWorkflowProjectorVersion(actionId.value, reasonCode)
    else if (actionKind.value === 'retire-workflow-projector') await platformApi.retireWorkflowProjectorVersion(actionId.value, reasonCode)
    else if (actionKind.value === 'publish-recipe') await platformApi.publishRecipe(actionId.value, reasonCode)
    else if (actionKind.value === 'retire-recipe') await platformApi.retireRecipe(actionId.value, reasonCode)
    else if (actionKind.value === 'publish-revision') await platformApi.publishConfigRevision(actionId.value, reasonCode)
    else if (actionKind.value === 'activate-revision') await platformApi.activateConfigRevision(actionId.value, reasonCode)
    else if (actionKind.value === 'host-status') await platformApi.setRuntimeHostStatus(actionId.value, actionEnable.value, reasonCode)
    else if (actionKind.value === 'deployment-probe') await platformApi.probeDeployment(actionId.value)
    else if (actionKind.value === 'deployment-smoke') {
      if (!actionSmokeSelectionValid.value) throw new Error('请明确选择本次烟测的提示词策略版本')
      const attempt = await platformApi.smokeTestDeployment(actionId.value, actionOperation.value, actionSmokeNeedsProfile.value ? actionSmokeBindingId.value : undefined)
      deploymentSmokeAttempts.value = { ...deploymentSmokeAttempts.value, [attempt.deployment]: attempt }
      deploymentSmokeErrors.value = { ...deploymentSmokeErrors.value, [attempt.deployment]: '' }
      closeAction(true); notice.value = `真实烟测任务已创建并进入 ${attempt.status}；页面将持续读取持久状态。`
      scheduleDeploymentSmokePoll()
      return
    }
    else if (actionKind.value === 'save-defaults') {
      const changes = buildSystemConfigChangeSet({ definitions: definitions.value, draftText: configDraft.value, dirtyKeys: configDirty.value, unsetKeys: configUnsets.value, secretSelections: secretSelections.value, isSuperuser: auth.user?.isSuperuser === true })
      const draft = await platformApi.createConfigRevision({ scopeType: 'system', values: changes.values, unsetKeys: changes.unsetKeys, changeSummary: currentSectionWriteRequiresSuperuser.value ? '通过系统设置中心核对并发布安全边界' : '通过系统设置中心核对并发布默认值与限制', reasonCode })
      await platformApi.publishConfigRevision(draft.id, reasonCode)
    }
    closeAction(true); await load(); notice.value = '操作已由服务端完成，当前分区已重新读取。'
  } catch (cause) { actionError.value = message(cause) } finally { saving.value = false }
}
const onRegistryDeletionCompleted = async () => {
  closeAction(true)
  await load()
  notice.value = '删除已按复核计划原子完成，当前分区已重新读取。'
}
const openSmokeResolution = (deployment: ModelDeployment) => {
  if (!auth.user?.isStaff || !deployment.latestSmoke?.unresolvedSubmission) return
  smokeResolutionDeployment.value = deployment
  smokeResolutionOutcome.value = 'confirmed_not_submitted'
  smokeResolutionNote.value = ''
  smokeResolutionProviderRequestId.value = ''
  smokeResolutionActualCost.value = ''
  smokeResolutionCurrency.value = ''
  smokeResolutionError.value = ''
}
const closeSmokeResolution = () => {
  if (smokeResolutionSaving.value) return
  smokeResolutionDeployment.value = null
  smokeResolutionProviderRequestId.value = ''
  smokeResolutionError.value = ''
}
watch(smokeResolutionOutcome, (outcome) => {
  smokeResolutionError.value = ''
  if (outcome === 'confirmed_not_submitted') {
    smokeResolutionProviderRequestId.value = ''
    smokeResolutionActualCost.value = ''
    smokeResolutionCurrency.value = ''
  }
})
const submitSmokeResolution = async () => {
  const deployment = smokeResolutionDeployment.value
  const submission = deployment?.latestSmoke?.unresolvedSubmission
  if (!deployment || !submission || smokeResolutionValidation.value) return
  smokeResolutionSaving.value = true
  smokeResolutionError.value = ''
  try {
    await platformApi.resolveDeploymentSmokeUncertain(deployment.id, {
      smokeAttemptId: submission.id,
      outcome: smokeResolutionOutcome.value,
      note: smokeResolutionNote.value.trim(),
      providerRequestId: smokeResolutionProviderRequestId.value.trim(),
      actualCost: smokeResolutionActualCost.value.trim() || null,
      costCurrency: smokeResolutionCurrency.value.trim().toUpperCase(),
    })
    smokeResolutionDeployment.value = null
    smokeResolutionProviderRequestId.value = ''
    await loadModels()
    notice.value = '消歧证据已追加；这不代表烟测通过。请重新执行一次新的烟测并以新证据为准。'
  } catch (cause) {
    smokeResolutionError.value = message(cause)
  } finally {
    smokeResolutionSaving.value = false
  }
}
const selectedHost = computed(() => runtimeHosts.value.find((item) => item.id === selectedHostId.value) || null)
const selectedProbeAttempt = computed(() => runtimeProbeFor(selectedHostId.value))
const selectedProbeActive = computed(() => Boolean(selectedProbeAttempt.value && runtimeProbeActiveStatuses.has(selectedProbeAttempt.value.status)))
const clearRuntimeEvidence = () => { snapshot.value = null; readiness.value = null; recommendation.value = null; selectedSnapshotState.value = 'idle'; notice.value = '' }
watch(selectedHostId, () => { clearRuntimeEvidence(); if (tab.value === 'runtime') void loadSelectedHostSnapshot() })
const evidenceState = computed(() => runtimeEvidenceState(selectedHostId.value, snapshot.value, readiness.value, recommendation.value, runtimeEvidenceNow.value))
const runtimeCapabilityItems = computed(() => Object.entries(readiness.value?.capabilities || {}))
const runtimeCapabilitySummary = computed(() => {
  const items = runtimeCapabilityItems.value
  const ready = items.filter(([, item]) => item.state === 'ready').length
  const blocked = items.filter(([, item]) => item.state === 'blocked').length
  const notRequired = items.filter(([, item]) => item.state === 'not_required').length
  return { total: items.length, ready, blocked, notRequired, unknown: items.length - ready - blocked - notRequired }
})
const runtimeActiveHostCount = computed(() => runtimeHosts.value.filter((item) => item.status === 'active').length)
const runtimeOverviewState = computed(() => {
  if (selectedProbeActive.value) return selectedProbeAttempt.value?.status || 'running'
  if (!selectedHost.value) return 'unconfigured'
  if (selectedSnapshotState.value === 'error') return 'error'
  if (!snapshot.value || !readiness.value) return 'unknown'
  if (runtimeCapabilitySummary.value.blocked) return 'blocked'
  if (runtimeCapabilitySummary.value.unknown) return 'unknown'
  return runtimeCapabilitySummary.value.total ? 'ready' : 'unknown'
})
const runtimeOverviewSummary = computed(() => {
  const hostName = selectedHost.value?.name || selectedHost.value?.key || '所选主机'
  if (!selectedHost.value) return '尚无可选择的运行主机。'
  if (selectedProbeActive.value) return `${hostName} 正在探测；下方暂展示最近已完成快照，完成后会刷新。`
  if (selectedSnapshotState.value === 'error') return `${hostName} 的最近快照读取失败，当前无法判断生产能力。`
  if (!snapshot.value || !readiness.value) return `${hostName} 尚无可用的能力快照，当前无法判断生产能力。`
  const summary = runtimeCapabilitySummary.value
  if (!summary.total) return `${hostName} 的快照未返回能力项，当前无法判断生产能力。`
  if (summary.blocked) return `${hostName} 当前 ${summary.ready}/${summary.total} 项能力就绪，${summary.blocked} 项阻断。`
  if (summary.unknown) return `${hostName} 当前 ${summary.ready}/${summary.total} 项能力就绪，${summary.unknown} 项无法判断。`
  return `${hostName} 当前 ${summary.ready}/${summary.total} 项能力就绪${summary.notRequired ? `，${summary.notRequired} 项无需配置` : ''}。`
})
const canRecommend = computed(() => evidenceState.value.readinessCurrent && !runtimeBusy.value && !selectedProbeActive.value)
const canDecide = computed(() => evidenceState.value.recommendationCurrent && !runtimeBusy.value)
const probe = async () => {
  const hostId = selectedHost.value?.id || ''
  if (!hostId) { error.value = '尚无已登记运行主机'; return }
  if (selectedProbeActive.value) { error.value = '当前主机已有未完成探测，请等待终态或先请求取消'; return }
  runtimeProbeBusyId.value = hostId; error.value = ''; recommendation.value = null
  try {
    const attempt = await platformApi.createRuntimeHostProbe(hostId)
    if (selectedHostId.value !== hostId) return
    if (attempt.host !== hostId) throw new Error('服务端探测任务不属于当前选择的主机')
    runtimeProbeAttempts.value = { ...runtimeProbeAttempts.value, [hostId]: attempt }
    runtimeProbeErrors.value = { ...runtimeProbeErrors.value, [hostId]: '' }
    notice.value = attempt.status === 'queued' ? '主机探测已进入持久队列；离开或刷新页面后仍可恢复。' : `已恢复现有主机探测任务：${attempt.status}`
    scheduleRuntimeProbePoll()
  } catch (cause) { error.value = message(cause) } finally { runtimeProbeBusyId.value = '' }
}
const recommend = async () => {
  const hostId = selectedHost.value?.id || ''; const snapshotId = snapshot.value?.id || ''
  if (!hostId || !snapshotId || !evidenceState.value.readinessCurrent) { error.value = '请先对当前选择的主机完成有效探测'; return }
  recommendation.value = null; runtimeBusy.value = 'recommend'; error.value = ''
  try {
    const next = await platformApi.recommendRuntime(hostId, { snapshotId })
    if (selectedHostId.value !== hostId || snapshot.value?.id !== snapshotId) return
    if (next.hostSnapshot !== snapshotId) throw new Error('服务端推荐与当前探测快照不一致')
    recommendation.value = next; notice.value = '推荐已基于当前快照生成；不会自动覆盖当前配置。'
  } catch (cause) { error.value = message(cause) } finally { runtimeBusy.value = '' }
}
const openDecision = (candidate: RecommendationCandidate, decision: 'accept'|'override'|'reject') => {
  decisionCandidate.value = candidate; decisionKind.value = decision; decisionOverrideText.value = '{}'; decisionReason.value = '系统设置中心人工决策'; decisionError.value = ''; decisionOpen.value = true
}
const closeDecision = (force = false) => { if (runtimeBusy.value && !force) return; decisionOpen.value = false; decisionCandidate.value = null; decisionError.value = '' }
const submitDecision = async () => {
  const candidate = decisionCandidate.value; const decision = decisionKind.value
  if (!candidate) return
  if (!recommendation.value || !evidenceState.value.recommendationCurrent) { decisionError.value = '推荐已过期或不属于当前主机快照，请重新探测并生成推荐'; return }
  const recommendationId = recommendation.value.id; const snapshotId = snapshot.value?.id
  runtimeBusy.value = 'decision'; decisionError.value = ''; error.value = ''
  try {
    let overrideValues: JsonObject = {}
    if (decision === 'override') {
      const configuration = JSON.parse(decisionOverrideText.value) as JsonObject
      if (!configuration || Array.isArray(configuration) || typeof configuration !== 'object') throw new Error('覆盖配置必须是 JSON 对象')
      overrideValues = { configuration }
    }
    const currentEvidence = runtimeEvidenceState(selectedHostId.value, snapshot.value, readiness.value, recommendation.value, Date.now())
    if (!snapshotId || !currentEvidence.recommendationCurrent || recommendation.value?.id !== recommendationId || recommendation.value.hostSnapshot !== snapshotId || snapshot.value?.id !== snapshotId || snapshot.value.host !== selectedHostId.value) throw new Error('主机或快照已变化，旧推荐不能提交')
    await platformApi.decideRecommendation(recommendationId, { candidateId: candidate.id, decision, scopeType: 'system', overrideValues, reason: decisionReason.value.trim() })
    const refreshed = await platformApi.getRecommendation(recommendationId)
    if (snapshot.value?.id === snapshotId && refreshed.hostSnapshot === snapshotId) recommendation.value = refreshed
    closeDecision(true)
    notice.value = decision === 'reject' ? '已记录拒绝决策。' : '已记录决策并创建可审核的草稿，未修改已发布配方。'
  } catch (cause) { decisionError.value = message(cause) } finally { runtimeBusy.value = '' }
}
const isRetiredLifecycleState = (value: unknown) => ['retired', 'inactive', 'disabled'].includes(String(value || '').toLocaleLowerCase('en-US'))
const providerListState = computed(() => filterSettingsList(providers.value, {
  search: settingsListSearch.value, showRetired: showRetiredSettingsRecords.value,
  isRetired: (item) => !item.isActive,
  searchableValues: (item) => [item.key, item.name, item.driverType, item.modalities, item.operations],
}))
const modelRegistryFamilies = computed(() => buildModelRegistryFamilies(
  modelProfiles.value,
  modelArtifacts.value,
  deployments.value,
  providers.value,
  { search: settingsListSearch.value, showRetired: showRetiredSettingsRecords.value },
))
const allModelRegistryFamilyCount = computed(() => new Set(modelProfiles.value.map((item) => item.profileKey)).size)
const modelRegistryVisibleRecordCount = computed(() => providerListState.value.items.length + modelRegistryFamilies.value.reduce(
  (count, family) => count + family.versions.reduce(
    (versionCount, group) => versionCount + 1 + group.artifacts.length + group.deployments.length,
    0,
  ),
  0,
))
const modelRegistryHiddenRetiredRecordCount = computed(() => (
  providers.value.filter((item) => !item.isActive).length
  + modelProfiles.value.filter((item) => item.status === 'retired').length
  + deployments.value.filter((item) => item.status === 'retired' || item.status === 'disabled' || profileFor(item.profileVersion)?.status === 'retired').length
  + modelArtifacts.value.filter((item) => profileFor(item.profileVersion)?.status === 'retired').length
))
const activeDeploymentsUnderRetiredProfiles = computed(() => deployments.value.filter((item) => (
  (item.status === 'active' || item.status === 'error') && profileFor(item.profileVersion)?.status === 'retired'
)))
const modelFamilySummary = (family: ModelRegistryFamily) => {
  const profiles = family.versions.map((item) => item.profile)
  const deploymentsForFamily = family.versions.flatMap((item) => item.deployments)
  return {
    draft: profiles.filter((item) => item.status === 'draft').length,
    published: profiles.filter((item) => item.status === 'published').length,
    retired: profiles.filter((item) => item.status === 'retired').length,
    active: deploymentsForFamily.filter((item) => item.status === 'active').length,
    error: deploymentsForFamily.filter((item) => item.status === 'error').length,
    deployments: deploymentsForFamily.length,
    artifacts: family.versions.reduce((count, item) => count + item.artifacts.length, 0),
  }
}
const modelFamilyIdentifiers = (family: ModelRegistryFamily) => [...new Set(
  family.versions.map((item) => item.profile.modelIdentifier).filter(Boolean),
)].join(' / ')
const modelFamilyCanCreateVersion = (family: ModelRegistryFamily) => {
  const latest = latestModelProfileForFamily(family)
  return Boolean(modelProfileOptions.value && latest && latest.status !== 'draft' && providers.value.some((item) => item.isActive))
}
const modelFamilyNextVersionTitle = (family: ModelRegistryFamily) => {
  const latest = latestModelProfileForFamily(family)
  if (latest?.status === 'draft') return '当前最高版本已是草稿，请直接编辑该版本'
  if (!modelProfileOptions.value) return '服务端模型选项目录尚未就绪'
  if (!providers.value.some((item) => item.isActive)) return '请先启用至少一个服务连接'
  return '复制实际最高版本合同为新的可编辑草稿'
}
const modelArtifactSummary = (items: readonly ModelArtifact[]) => ({
  available: items.filter((item) => item.status === 'available').length,
  declared: items.filter((item) => item.status === 'declared').length,
  attention: items.filter((item) => item.status === 'missing' || item.status === 'invalid').length,
})
const modelDeploymentSummary = (items: readonly ModelDeployment[]) => ({
  active: items.filter((item) => item.status === 'active').length,
  disabled: items.filter((item) => item.status === 'disabled').length,
  error: items.filter((item) => item.status === 'error').length,
  retired: items.filter((item) => item.status === 'retired').length,
})
const modelCapabilityList = (profile: ModelProfileVersion) => Array.isArray(profile.specification.capabilities)
  ? profile.specification.capabilities.map(String)
  : []
const modelCapabilityCompactLabel = (profile: ModelProfileVersion) => {
  const values = modelCapabilityList(profile)
  if (!values.length) return '尚未声明'
  const visible = values.slice(0, 2).map((value) => operationLabel(value)).join(' / ')
  return values.length > 2 ? `${visible} / +${values.length - 2}` : visible
}
const providerForModelProfile = (profile: ModelProfileVersion) => providers.value.find((item) => item.id === profile.provider) || null
const modelWorkflowContextAvailability = (profile: ModelProfileVersion) => {
  const provider = providerForModelProfile(profile)
  if (sectionErrors.value.providers || !provider) return 'unavailable' as const
  if (provider.driverType !== 'comfyui') return 'not_applicable' as const
  return provider.isActive ? 'available' as const : 'historical' as const
}
const modelWorkflowContextLabel = (profile: ModelProfileVersion) => {
  if (modelWorkflowContextAvailability(profile) === 'historical') return '查看历史工作流'
  if (profile.status === 'draft') return '从工作流提取并证明'
  if (profile.status === 'retired') return '查看历史工作流'
  return '管理工作流关联'
}
const modelWorkflowContextTitle = (profile: ModelProfileVersion) => {
  const availability = modelWorkflowContextAvailability(profile)
  if (availability === 'historical') return `Provider 已停用；进入工作流分区只读查看精确模型版本 ${profile.id} 的历史关联`
  if (availability === 'unavailable') return 'Provider 目录不可用或引用缺失，无法安全判断工作流入口'
  if (availability === 'not_applicable') return '当前 Provider 不是 ComfyUI，工作流关联不适用'
  return `进入工作流分区并锁定精确模型版本 ${profile.id}；不会自动扫描、校验、Smoke 或发布`
}
const modelWorkflowContextRoute = (profile: ModelProfileVersion) => ({
  path: '/settings',
  query: {
    section: 'workflows',
    model_profile_version: profile.id,
    intent: profile.status === 'draft' ? 'attest' : profile.status === 'retired' ? 'history' : 'associate',
  },
})
const workflowModelContextGuidance = computed(() => {
  const profile = workflowModelContextProfile.value
  if (!profile) return ''
  if (profile.status === 'draft') return '扫描后只会在服务端把此精确草稿列为可用候选时预选；文件证明完成后仍须先发布模型，再重新预览并导入正式工作流版本。'
  if (profile.status === 'retired') return '当前为只读历史上下文；页面已显示退役记录，不允许从此上下文继续扫描或导入。'
  return '当前列表只读取直接绑定此精确模型版本的工作流；扫描、预览、导入、Validate、Smoke 与发布仍需分别显式执行。'
})
const modelSearchMatches = (values: unknown[]) => {
  const query = settingsListSearch.value.trim().toLocaleLowerCase('zh-CN')
  return Boolean(query && values.flat(Infinity).map((value) => String(value ?? '')).join(' ').toLocaleLowerCase('zh-CN').includes(query))
}
const modelCapabilitySearchHit = (group: ModelRegistryVersionGroup) => modelSearchMatches([
  group.profile.profileKey,
  group.profile.modelIdentifier,
  group.profile.modality,
  group.profile.status,
  modelCapabilityList(group.profile),
])
const modelArtifactSearchHit = (group: ModelRegistryVersionGroup) => modelSearchMatches(
  group.artifacts.flatMap((item) => [item.logicalName, item.filename, item.storageUri, item.inventoryFolder, item.role, item.status]),
)
const modelInformationSearchHit = (group: ModelRegistryVersionGroup) => modelCapabilitySearchHit(group) || modelArtifactSearchHit(group)
const modelDeploymentSearchHit = (group: ModelRegistryVersionGroup) => modelSearchMatches(
  group.deployments.flatMap((item) => [item.key, item.name, item.status, item.providerDriver, item.runtimeHost, item.latestSmoke?.status, item.latestSmoke?.unresolvedSubmission?.status]),
)
type ModelSection = 'capabilities' | 'artifacts' | 'deployments'
const selectedModelSection = ref<ModelSection>('capabilities')
const selectedModelFamily = computed<ModelRegistryFamily | null>(() => (
  modelRegistryFamilies.value.find((family) => family.key === selectedModelFamilyKey.value)
  || null
))
const selectedModelVersionGroup = computed<ModelRegistryVersionGroup | null>(() => {
  const family = selectedModelFamily.value
  if (!family) return null
  return family.versions.find((group) => group.profile.id === selectedModelProfileId.value)
    || null
})
const selectModelFamily = (family: ModelRegistryFamily) => {
  const firstVersion = family.versions[0]
  if (!firstVersion) return
  selectedModelFamilyKey.value = family.key
  selectedModelProfileId.value = firstVersion.profile.id
  selectedModelSection.value = modelDeploymentSearchHit(firstVersion) && !modelInformationSearchHit(firstVersion)
    ? 'deployments'
    : modelArtifactSearchHit(firstVersion) && !modelCapabilitySearchHit(firstVersion)
      ? 'artifacts'
      : 'capabilities'
}
const selectModelVersion = (group: ModelRegistryVersionGroup) => {
  selectedModelProfileId.value = group.profile.id
  selectedModelSection.value = modelDeploymentSearchHit(group) && !modelInformationSearchHit(group)
    ? 'deployments'
    : modelArtifactSearchHit(group) && !modelCapabilitySearchHit(group)
      ? 'artifacts'
      : 'capabilities'
}
watch(modelRegistryFamilies, (families) => {
  if (!selectedModelFamilyKey.value && families[0]) selectModelFamily(families[0])
}, { immediate: true })
const focusModelDeploymentFromRoute = async () => {
  if (tab.value !== 'models') return
  const deploymentId = String(route.query.deployment || '')
  const profileId = String(route.query.model_profile_version || '')
  const targetProfile = modelProfiles.value.find((profile) => profile.id === profileId)
  const targetDeployment = deployments.value.find((deployment) => deployment.id === deploymentId)
  if ((workflowReturnId.value || route.query.relation_context) && (targetProfile?.status === 'retired' || ['retired', 'disabled'].includes(targetDeployment?.status || ''))) showRetiredSettingsRecords.value = true
  if (!deploymentId) {
    if (!profileId) return
    const family = modelRegistryFamilies.value.find((candidate) => candidate.versions.some((group) => group.profile.id === profileId))
    if (!family) { selectedModelFamilyKey.value = ''; selectedModelProfileId.value = ''; return }
    selectedModelFamilyKey.value = family.key
    selectedModelProfileId.value = profileId
    if (route.query.focus === 'deployments') selectedModelSection.value = 'deployments'
    return
  }
  const family = modelRegistryFamilies.value.find((candidate) => candidate.versions.some((group) => (
    group.deployments.some((deployment) => deployment.id === deploymentId)
  )))
  const group = family?.versions.find((candidate) => candidate.deployments.some((deployment) => deployment.id === deploymentId))
  if (!family || !group) { selectedModelFamilyKey.value = ''; selectedModelProfileId.value = ''; return }
  selectedModelFamilyKey.value = family.key
  selectedModelProfileId.value = group.profile.id
  selectedModelSection.value = 'deployments'
  await nextTick()
  document.getElementById(`model-deployment-${deploymentId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
watch([() => route.query.deployment, () => route.query.model_profile_version, () => route.query.focus, tab, modelRegistryFamilies], () => { void focusModelDeploymentFromRoute() }, { immediate: true })
const workflowListState = computed(() => filterSettingsList(workflowVersions.value, {
  search: settingsListSearch.value, showRetired: showRetiredSettingsRecords.value,
  isRetired: (item) => isRetiredLifecycleState(item.status),
  searchableValues: (item) => [item.templateName, item.templateSlug, item.version, item.outputKind, item.status, item.validationState, item.smokeTestState],
}))
const productionReadinessByRecipeId = computed(() => new Map(
  (productionReadiness.value?.recipes || []).map((item) => [item.id, item]),
))
const readinessFilteredRecipes = computed(() => readinessReasonFilter.value
  ? recipes.value.filter((item) => productionReadinessByRecipeId.value.get(item.id)?.reasonCodes.includes(readinessReasonFilter.value))
  : recipes.value)
const recipeListState = computed(() => filterSettingsList(readinessFilteredRecipes.value, {
  search: settingsListSearch.value, showRetired: showRetiredSettingsRecords.value,
  isRetired: (item) => isRetiredLifecycleState(item.status),
  searchableValues: (item) => {
    const readiness = productionReadinessByRecipeId.value.get(item.id)
    return [item.recipeKey, item.name, item.version, item.status, item.definitionSha256, readiness?.state, readiness?.reasonCodes]
  },
}))
const runtimeHostListState = computed(() => filterSettingsList(runtimeHosts.value, {
  search: settingsListSearch.value, showRetired: showRetiredSettingsRecords.value,
  isRetired: (item) => isRetiredLifecycleState(item.status),
  searchableValues: (item) => [item.key, item.name, item.status, item.fingerprintSha256],
}))
const revisionsForCurrentSection = computed(() => {
  if (tab.value === 'security') return revisions.value.filter((item) => item.changesSecurityBoundaries === true)
  if (tab.value === 'defaults') return revisions.value.filter((item) => item.changesSecurityBoundaries === false)
  return revisions.value
})
const unknownConfigRevisionClassifications = computed(() => revisions.value.filter((item) => item.changesSecurityBoundaries === null))
const revisionListState = computed(() => filterSettingsList(revisionsForCurrentSection.value, {
  search: settingsListSearch.value, showRetired: showRetiredSettingsRecords.value,
  isRetired: () => false,
  searchableValues: (item) => [item.revision, item.scopeType, item.status, item.activationState, item.changeSummary, item.valuesSha256, item.changesSecurityBoundaries === true ? '安全边界 security boundary' : item.changesSecurityBoundaries === false ? '普通配置 standard configuration' : '无法分类 unknown fail closed'],
}))
const secretBindingListState = computed(() => filterSettingsList(secretBindings.value, {
  search: settingsListSearch.value, showRetired: showRetiredSettingsRecords.value,
  isRetired: (item) => !item.isActive,
  searchableValues: (item) => [item.key, item.label, item.purpose, item.configured, item.currentVersion],
}))
const secretAuditListState = computed(() => filterSettingsList(secretAudit.value, {
  search: settingsListSearch.value, showRetired: showRetiredSettingsRecords.value,
  isRetired: () => false,
  searchableValues: (item) => [item.action, item.bindingKey, item.actorUsername, item.reason, item.createdAt],
}))
const currentConfigurationClass = computed<'standard' | 'security_boundary'>(() => tab.value === 'security' ? 'security_boundary' : 'standard')
const configurationDefinitionsInClass = computed(() => definitions.value.filter((item) => item.configurationClass === currentConfigurationClass.value))
const unknownConfigurationDefinitions = computed(() => definitions.value.filter((item) => item.configurationClass === 'unknown'))
const configDefinitionListState = computed(() => filterSettingsList(configurationDefinitionsInClass.value, {
  search: settingsListSearch.value, showRetired: showRetiredSettingsRecords.value,
  isRetired: (item) => !item.isActive,
  searchableValues: (item) => [item.key, item.domain, item.description, item.unit, item.impactModules, item.valueType, item.allowedScopes],
}))
const configurationDefinitionsForSection = computed(() => configDefinitionListState.value.items)
const groupedDefinitions = computed(() => { const result = new Map<string, ConfigDefinition[]>(); for (const item of configurationDefinitionsForSection.value) result.set(item.domain || 'other', [...(result.get(item.domain || 'other') || []), item]); return [...result.entries()] })
const credentialRows = computed(() => [...providers.value.map((item) => ({ owner: `Provider · ${item.name}`, configured: item.credentialConfigured, lifecycle: item.isActive ? 'active' : 'inactive', usable: item.credentialConfigured && item.isActive })), ...deployments.value.map((item) => ({ owner: `Deployment · ${item.name}`, configured: item.credentialConfigured, lifecycle: item.status, usable: item.credentialConfigured && item.status === 'active' }))])
const credentialRowListState = computed(() => filterSettingsList(credentialRows.value, {
  search: settingsListSearch.value, showRetired: showRetiredSettingsRecords.value,
  isRetired: (item) => isRetiredLifecycleState(item.lifecycle),
  searchableValues: (item) => [item.owner, item.lifecycle, item.configured ? '已绑定受管凭据 configured' : '未配置凭据 unconfigured', item.usable ? '可用 usable' : '不可用 blocked'],
}))
const settingsListFilterSummary = computed(() => {
  if (tab.value === 'models') return {
    visibleCount: modelRegistryVisibleRecordCount.value,
    totalCount: providers.value.length + modelProfiles.value.length + deployments.value.length + modelArtifacts.value.length,
    hiddenRetiredCount: modelRegistryHiddenRetiredRecordCount.value,
  }
  if (tab.value === 'workflows') return { visibleCount: workflowListState.value.items.length, totalCount: workflowVersions.value.length, hiddenRetiredCount: workflowListState.value.hiddenRetiredCount }
  if (tab.value === 'routing') return { visibleCount: recipeListState.value.items.length, totalCount: recipes.value.length, hiddenRetiredCount: recipeListState.value.hiddenRetiredCount }
  if (tab.value === 'defaults' || tab.value === 'security') return {
    visibleCount: configDefinitionListState.value.items.length + revisionListState.value.items.length,
    totalCount: configurationDefinitionsInClass.value.length + revisionsForCurrentSection.value.length,
    hiddenRetiredCount: configDefinitionListState.value.hiddenRetiredCount,
  }
  if (tab.value === 'runtime') return { visibleCount: runtimeHostListState.value.items.length, totalCount: runtimeHosts.value.length, hiddenRetiredCount: runtimeHostListState.value.hiddenRetiredCount }
  if (tab.value === 'governance') return {
    visibleCount: secretBindingListState.value.items.length + credentialRowListState.value.items.length + lifecycleAuditListState.value.items.length + revisionListState.value.items.length + secretAuditListState.value.items.length,
    totalCount: secretBindings.value.length + credentialRows.value.length + lifecycleAuditByFacet.value.length + revisions.value.length + secretAudit.value.length,
    hiddenRetiredCount: secretBindingListState.value.hiddenRetiredCount + credentialRowListState.value.hiddenRetiredCount,
  }
  return { visibleCount: 0, totalCount: 0, hiddenRetiredCount: 0 }
})
const productionReadinessState = computed(() => sectionErrors.value.productionReadiness ? 'error' : productionReadiness.value?.status || 'unconfigured')
const productionReadinessReportState = computed<'loaded' | 'unavailable' | 'error'>(() => (
  sectionErrors.value.productionReadiness ? 'error' : productionReadiness.value ? 'loaded' : 'unavailable'
))
const productionReadinessSummary = computed(() => {
  const items = productionReadiness.value?.recipes || []
  const ready = items.filter((item) => item.state === 'ready').length
  const blocked = items.filter((item) => item.state === 'blocked').length
  const reasonCounts = new Map<string, number>()
  for (const item of items) {
    for (const reasonCode of item.reasonCodes) reasonCounts.set(reasonCode, (reasonCounts.get(reasonCode) || 0) + 1)
  }
  return {
    total: items.length,
    ready,
    blocked,
    unknown: items.length - ready - blocked,
    reasonGroups: [...reasonCounts.entries()].map(([reasonCode, count]) => ({ reasonCode, count })).sort((left, right) => right.count - left.count || left.reasonCode.localeCompare(right.reasonCode)),
  }
})
const readinessReasonAction = (reasonCode: string): { label: string; section: SettingsTab } => {
  if (reasonCode.includes('recipe')) return { label: '查看匹配版本', section: 'routing' }
  if (reasonCode.includes('workflow')) return { label: '检查工作流版本', section: 'workflows' }
  if (reasonCode.includes('model') || reasonCode.includes('deployment') || reasonCode.includes('artifact')) return { label: '检查模型与部署', section: 'models' }
  if (reasonCode.includes('config') || reasonCode.includes('secret')) return { label: '检查默认值与凭据', section: 'defaults' }
  return { label: '检查运行主机', section: 'runtime' }
}
const drillReadinessReason = async (reasonCode: string) => {
  const action = readinessReasonAction(reasonCode)
  readinessReasonFilter.value = action.section === 'routing' ? reasonCode : ''
  settingsListSearch.value = ''
  await selectTab(action.section)
  if (action.section === 'routing') {
    await nextTick()
    recipeListAnchor.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
}
const openRecipePublish = (item: ProductionRecipe) => openAction(
  'publish-recipe',
  item.id,
  `核对并发布 · ${item.name} v${item.version}`,
  '服务端将复验脚本模式、阶段顺序、路由部署/工作流（route deployment/workflow）和交付闭环（delivery closure）；失败不会发布。',
)
const openRecipeDelete = (item: ProductionRecipe) => openAction(
  'delete-recipe',
  item.id,
  `删除配方草稿 · ${item.name}`,
  '仅无运行、推荐或其他审计引用的草稿（draft）可删除。',
)
const openRecipeRetire = (item: ProductionRecipe) => openAction(
  'retire-recipe',
  item.id,
  `退役配方 · ${item.name}`,
  '退役后不再用于新的生产运行（ProductionRun）；历史运行继续引用不可变版本。',
)
const modelRelations = useRegistryRelations('model', () => tab.value==='models' && selectedModelVersionGroup.value ? [selectedModelVersionGroup.value.profile] : [])
const deploymentRelations = useRegistryRelations('deployment', () => tab.value==='models' && selectedModelSection.value==='deployments' ? selectedModelVersionGroup.value?.deployments || [] : [])
const deploymentPromptBindings = useModelPromptBindings(() => tab.value==='models' && selectedModelSection.value==='deployments' ? selectedModelVersionGroup.value?.profile || null : null)
const projectorRelations = useRegistryRelations('projector', () => tab.value==='workflows' && workflowProjectorCatalogOpen.value ? workflowProjectorVersions.value : [])
const recipeRelations = useRegistryRelations('recipe', () => tab.value==='routing' ? recipeListState.value.items : [])
const relationReturnUrl = computed(() => {
  const [kind,id] = String(route.query.relation_context || '').split(':')
  return kind && Object.prototype.hasOwnProperty.call(relationKinds,kind) && id && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(id) ? relationOverviewUrl(kind as RelationKind,id) : ''
})
const recipeNavigationError = ref('')
watch([() => route.query.recipe_version, recipes, loading, tab], async () => {
  recipeNavigationError.value = ''
  if (tab.value!=='routing' || loading.value || !route.query.recipe_version) return
  const item = recipes.value.find(item=>item.id===route.query.recipe_version)
  if (!item) { detailOpen.value = false; recipeNavigationError.value = '指定配方版本不存在或目录尚未完整读取，请返回关联总览重新选择。'; return }
  showRetiredSettingsRecords.value = true
  showRecipeDetail(item)
  await nextTick()
  document.getElementById(`recipe-${item.id}`)?.scrollIntoView({behavior:'smooth',block:'center'})
}, {immediate:true})
const closeSecret = (force = false) => { if (saving.value && !force) return; secretDraft.secretValue = ''; secretDraft.reason = ''; secretError.value = ''; secretOpen.value = false; secretTarget.value = null }
const openSecret = (mode: 'create'|'rotate'|'status', target?: SecretBinding) => {
  secretMode.value = mode; secretTarget.value = target || null
  secretError.value = ''
  Object.assign(secretDraft, { key: '', label: '', purpose: 'provider_credential', secretValue: '', reason: '', isActive: target ? !target.isActive : true })
  secretOpen.value = true
}
const submitSecret = async () => {
  saving.value = true; secretError.value = ''
  try {
    if (secretMode.value === 'create') await platformApi.createSecretBinding({ key: secretDraft.key, label: secretDraft.label, purpose: secretDraft.purpose, secretValue: secretDraft.secretValue, reason: secretDraft.reason })
    else if (secretMode.value === 'rotate' && secretTarget.value) await platformApi.rotateSecretBinding(secretTarget.value.id, secretDraft.secretValue, secretDraft.reason)
    else if (secretTarget.value) await platformApi.setSecretBindingStatus(secretTarget.value.id, secretDraft.isActive, secretDraft.reason)
    closeSecret(true); await load(); notice.value = '受管凭据操作已完成；秘密正文未写入浏览器存储或审计。'
  } catch (cause) { secretError.value = message(cause) } finally { secretDraft.secretValue = ''; saving.value = false }
}
</script>

<template>
  <div class="platform-page settings-center">
    <header class="platform-page-header"><div><span class="eyebrow">平台控制中心（Platform Control Plane）</span><h2>系统设置</h2><p>模型、ComfyUI 工作流、生产配方、默认参数和主机推荐均以服务端配置修订版本（revision）为准。</p></div><div class="platform-row-actions"><RouterLink class="button ghost small" :to="settingsGuideHref" target="_blank"><BookOpenCheck :size="14"/>当前分区图文指南</RouterLink><button class="button ghost small" :disabled="loading" @click="load"><RefreshCw :size="14" :class="{spin:loading}"/>重新读取</button></div></header>
    <SettingsPrimaryNav/>
    <div v-if="relationReturnUrl" class="platform-row-actions"><RouterLink class="button ghost small" :to="relationReturnUrl">返回关联总览</RouterLink><small>配置仍在当前对象页维护；总览只读。</small></div>
    <p v-if="recipeNavigationError" class="platform-alert danger" role="alert">{{ recipeNavigationError }}</p>
    <div v-if="workflowReturnId" class="platform-row-actions"><RouterLink class="button ghost small" :to="`/settings/workflows/${workflowReturnId}`">返回来源工作流</RouterLink><small>正在查看工作流的关联配置；跳转不会更改绑定或触发生成。</small></div>
    <p v-if="projectorNavigationLoading" role="status">正在读取工作流指定的 Projector 版本…</p>
    <div v-if="projectorNavigationError" class="platform-alert danger" role="alert"><span>{{ projectorNavigationError }}</span><button type="button" class="button ghost small" @click="followWorkflowProjectorRoute">重试读取配置</button></div>
    <SettingsListFilter
      v-if="tab!=='prompts'&&tab!=='routing'&&tab!=='runtime'"
      v-model:search="settingsListSearch"
      v-model:show-retired="showRetiredSettingsRecords"
      :visible-count="settingsListFilterSummary.visibleCount"
      :total-count="settingsListFilterSummary.totalCount"
      :hidden-retired-count="settingsListFilterSummary.hiddenRetiredCount"
      :placeholder="`搜索${currentSettingsSection.label}中的名称、机器键、类型、说明或状态`"
    />
    <div v-if="error" class="platform-alert danger"><AlertTriangle :size="17"/><div><strong>设置未能完整读取或保存</strong><p>{{ error }}</p></div><button class="button ghost small" @click="load">重试</button></div>
    <div v-if="notice" class="platform-alert success"><Check :size="17"/><p>{{ notice }}</p></div>
    <div v-if="(tab==='defaults'||tab==='security'||tab==='governance')&&unknownConfigRevisionClassifications.length" class="platform-alert danger"><AlertTriangle :size="15"/><div><strong>检测到 {{ unknownConfigRevisionClassifications.length }} 条无法判断是否改动安全边界的配置修订，已失败关闭</strong><p>这些 Revision 不会进入默认值或安全边界发布列表，凭据与审计页也不会提供发布/激活按钮。请修复服务端 changes_security_boundaries 响应后重新读取。</p></div></div>
    <div v-if="loading" class="platform-loading"><LoaderCircle class="spin" :size="22"/>正在读取服务端设置…</div>

    <template v-else-if="tab==='models'">
      <section class="platform-toolbar model-registry-toolbar">
        <div><strong>模型（Models）</strong><span>按模型统一管理能力版本、模型文件与运行实例；版本、文件、实例和 Probe/Smoke 证据仍分别记录，页面不会把它们合成一个“可用”状态。</span></div>
        <div>
          <button class="button ghost small" type="button" aria-haspopup="dialog" aria-controls="model-provider-management" :aria-expanded="providerManagementOpen" @click="providerManagementOpen=!providerManagementOpen"><Server :size="13"/>管理服务连接</button>
          <button class="button primary small" type="button" :disabled="!modelProfileOptions||!providers.some((item)=>item.isActive)" :title="!modelProfileOptions?'模型选项目录读取失败':!providers.some((item)=>item.isActive)?'请先启用服务连接':'创建全新模型版本族，版本号由服务端分配'" @click="openEditor('model')"><Plus :size="13"/>新建模型</button>
        </div>
      </section>
      <div v-if="sectionErrors.modelProfileOptions" class="platform-alert danger"><AlertTriangle :size="15"/><p>模型选项目录读取失败，新增/编辑已失败关闭且不会回退为自由文本：{{ sectionErrors.modelProfileOptions }}</p></div>

      <BaseModal :open="providerManagementOpen" title="服务连接（Provider）" eyebrow="连接与驱动合同 · 独立管理" width="980px" @close="providerManagementOpen=false">
        <div id="model-provider-management" class="model-provider-management">
        <div class="model-section-heading"><p>Provider 只负责连接与驱动合同，不与模型能力版本合并。</p><button class="button ghost small" type="button" @click="providerManagementOpen=false;openEditor('provider')"><Plus :size="13"/>新增服务连接</button></div>
        <p v-if="sectionErrors.providers" class="platform-inline-error">{{ sectionErrors.providers }}</p>
        <div class="platform-dense-list">
          <article v-for="item in providerListState.items" :key="item.id">
            <div><strong>{{ item.name || item.key }}</strong><small>{{ driverLabel(item.driverType) }} · {{ mediaKindListLabel(item.modalities) }} · {{ operationListLabel(item.operations) }}</small></div>
            <PlatformStatusPill :status="item.isActive?'active':'inactive'"/>
            <span class="platform-row-actions"><button class="button ghost tiny" @click="providerManagementOpen=false;showDetail('服务提供方（Provider）',item.name||item.key,item)"><Eye :size="11"/>详情</button><button class="button ghost tiny" @click="providerManagementOpen=false;openEditor('provider',item)">编辑连接</button><button v-if="!item.isSystem&&!item.isActive" class="button danger tiny" @click="providerManagementOpen=false;openAction('delete-provider',item.id,`删除服务提供方（Provider）· ${item.name}`, '仅未被模型或工作流引用且已停用的服务提供方可删除。')"><Trash2 :size="11"/>删除</button></span>
          </article>
          <p v-if="!providerListState.items.length&&!sectionErrors.providers" class="platform-empty">当前筛选下没有服务连接。</p>
        </div>
        </div>
      </BaseModal>

      <div v-if="sectionErrors.modelProfiles||sectionErrors.deployments||sectionErrors.modelArtifacts||sectionErrors.runtimeSnapshots||sectionErrors.deploymentSmokeAttempts||sectionErrors.deploymentAcceptance" class="model-registry-errors">
        <p v-if="sectionErrors.modelProfiles" class="platform-inline-error">模型版本读取失败：{{ sectionErrors.modelProfiles }}</p>
        <p v-if="sectionErrors.deployments" class="platform-inline-error">运行实例读取失败：{{ sectionErrors.deployments }}</p>
        <p v-if="sectionErrors.modelArtifacts" class="platform-inline-error">模型文件读取失败：{{ sectionErrors.modelArtifacts }}</p>
        <p v-if="sectionErrors.runtimeSnapshots" class="platform-inline-error">主机证据读取失败：{{ sectionErrors.runtimeSnapshots }}</p>
        <p v-if="sectionErrors.deploymentSmokeAttempts" class="platform-inline-error">Smoke 任务读取失败：{{ sectionErrors.deploymentSmokeAttempts }}</p>
        <p v-if="sectionErrors.deploymentAcceptance" class="platform-inline-error">实例验收数据读取失败：{{ sectionErrors.deploymentAcceptance }}</p>
      </div>
      <div v-if="activeDeploymentsUnderRetiredProfiles.length" class="platform-alert warning"><AlertTriangle :size="15"/><p>检测到 {{ activeDeploymentsUnderRetiredProfiles.length }} 个启用或异常运行实例仍绑定已退役模型版本，这是需要管理员受控修复的遗留生命周期冲突。所属版本已冻结，普通实例编辑不能用于停用；页面不会把这些记录视为当前可用运行实例。</p></div>

      <div v-if="modelRegistryFamilies.length||selectedModelFamilyKey" class="model-registry-workbench" :aria-label="`${modelRegistryFamilies.length} / ${allModelRegistryFamilyCount} 个模型`">
        <aside class="model-family-browser" aria-label="模型目录">
          <header><span><strong>模型目录</strong><small>选择模型后在右侧管理，不再向页面下方展开</small></span><b>{{ modelRegistryFamilies.length }} / {{ allModelRegistryFamilyCount }}</b></header>
          <div class="model-family-browser-list">
            <button
              v-for="family in modelRegistryFamilies"
              :key="family.key"
              type="button"
              :class="{active:selectedModelFamily?.key===family.key}"
              :aria-pressed="selectedModelFamily?.key===family.key"
              aria-controls="model-family-inspector"
              @click="selectModelFamily(family)"
            >
              <span class="model-family-browser-identity"><Boxes :size="16"/><span><strong>{{ family.key }}</strong><small>{{ modelFamilyIdentifiers(family) || '尚未填写模型标识' }}</small><em>{{ family.providerNames.join(' / ') }}</em></span></span>
              <span class="model-family-browser-status">
                <span><em v-if="modelFamilySummary(family).published" class="published">已发布 {{ modelFamilySummary(family).published }}</em><em v-if="modelFamilySummary(family).draft" class="draft">草稿 {{ modelFamilySummary(family).draft }}</em><em v-if="modelFamilySummary(family).error" class="error">异常 {{ modelFamilySummary(family).error }}</em></span>
                <small>{{ family.versions.length }} 版本 · {{ modelFamilySummary(family).deployments }} 实例</small>
              </span>
            </button>
            <p v-if="!modelRegistryFamilies.length" class="platform-empty model-browser-empty">当前筛选没有匹配模型。</p>
          </div>
        </aside>

        <section v-for="family in selectedModelFamily?[selectedModelFamily]:[]" :key="family.key" id="model-family-inspector" class="model-family-inspector" aria-label="模型详情工作区">
          <header class="model-inspector-header">
            <div><span class="eyebrow">模型详情（Model Inspector）</span><strong>{{ family.key }}</strong><small>{{ modelFamilyIdentifiers(family) || '尚未填写模型标识' }} · {{ family.providerNames.join(' / ') }}</small></div>
            <button class="button ghost small" type="button" :disabled="!modelFamilyCanCreateVersion(family)" :title="modelFamilyNextVersionTitle(family)" @click="openNextModelVersionDraft(family)"><Plus :size="13"/>新建下一版本</button>
          </header>
          <div class="model-family-overview" aria-label="模型状态摘要">
            <span class="draft">草稿 {{ modelFamilySummary(family).draft }}</span><span class="published">已发布 {{ modelFamilySummary(family).published }}</span><span v-if="showRetiredSettingsRecords" class="retired">已退役 {{ modelFamilySummary(family).retired }}</span><span class="active">启用实例 {{ modelFamilySummary(family).active }}</span><span :class="{error:modelFamilySummary(family).error}">异常实例 {{ modelFamilySummary(family).error }}</span><span>文件 {{ modelFamilySummary(family).artifacts }}</span>
          </div>
          <nav class="model-version-tabs" aria-label="模型版本">
            <button v-for="versionGroup in family.versions" :key="versionGroup.profile.id" type="button" :aria-pressed="selectedModelVersionGroup?.profile.id===versionGroup.profile.id" aria-controls="model-version-workspace" :class="{active:selectedModelVersionGroup?.profile.id===versionGroup.profile.id}" @click="selectModelVersion(versionGroup)">
              <span><strong>v{{ versionGroup.profile.version }}</strong><small>{{ versionGroup.profile.modelIdentifier }}</small></span><PlatformStatusPill :status="versionGroup.profile.status"/>
            </button>
          </nav>

          <template v-for="group in selectedModelVersionGroup?[selectedModelVersionGroup]:[]" :key="group.profile.id">

            <article id="model-version-workspace" class="model-version-card model-version-detail">
              <header>
                <div class="model-version-identity">
                  <button class="registry-title-link" type="button" @click="showDetail('模型版本',`${group.profile.profileKey} · v${group.profile.version}`,group.profile)"><strong>v{{ group.profile.version }} · {{ group.profile.modelIdentifier }}</strong></button>
                  <small>{{ providerForModelProfile(group.profile)?.name || providerForModelProfile(group.profile)?.key || '未知 Provider' }} · {{ mediaKindLabel(group.profile.modality) }} · 能力合同 {{ bytes(group.profile.specificationSizeBytes) }}</small>
                </div>
                <span class="model-lifecycle-fact"><small>版本状态</small><PlatformStatusPill :status="group.profile.status"/></span>
                <div class="platform-row-actions model-version-actions">
                  <RouterLink v-if="['available','historical'].includes(modelWorkflowContextAvailability(group.profile))" class="button ghost tiny" :to="modelWorkflowContextRoute(group.profile)" :title="modelWorkflowContextTitle(group.profile)"><GitBranch :size="11"/>{{ modelWorkflowContextLabel(group.profile) }}</RouterLink>
                  <span v-else-if="modelWorkflowContextAvailability(group.profile)==='not_applicable'" class="platform-muted" :title="modelWorkflowContextTitle(group.profile)">工作流不适用</span>
                  <button v-else class="button ghost tiny" type="button" disabled :title="modelWorkflowContextTitle(group.profile)"><GitBranch :size="11"/>工作流状态不可判断</button>
                  <button class="button ghost tiny" type="button" @click="modelPromptContext={profile:group.profile,deployment:null}"><BookOpenCheck :size="11"/>提示词绑定</button>
                  <RegistryRelationSummary :row="modelRelations.rows.value[group.profile.id]" :loading="modelRelations.loading.value" :error="modelRelations.error.value" @retry="modelRelations.reload" condensed/>
                  <button class="button ghost tiny" @click="showDetail('模型版本',`${group.profile.profileKey} · v${group.profile.version}`,group.profile)"><Eye :size="11"/>详情</button>
                  <button v-if="group.profile.status==='draft'" class="button ghost tiny" @click="openEditor('model',group.profile)">继续编辑</button>
                  <button v-if="group.profile.status==='draft'" class="button primary tiny" :disabled="Boolean(sectionErrors.modelArtifacts)" :title="sectionErrors.modelArtifacts?'模型文件目录读取失败，不能安全核对发布':''" @click="openAction('publish-model',group.profile.id,`核对并发布 · ${group.profile.profileKey} v${group.profile.version}`, '发布后模型规格（specification）与文件关联不可原地修改；后续变更必须新建版本。')">核对并发布</button>
                  <details v-if="group.profile.status==='draft'||group.profile.status==='published'" class="model-more-menu" @click.stop>
                    <summary class="button ghost tiny">更多</summary>
                    <div>
                      <button v-if="group.profile.status==='draft'" class="button danger tiny" @click="openAction('delete-model',group.profile.id,`删除草稿 · ${group.profile.profileKey} v${group.profile.version}`, '仅无引用草稿可删除。')"><Trash2 :size="11"/>删除草稿</button>
                      <button v-if="group.profile.status==='published'" class="button ghost tiny" :disabled="Boolean(sectionErrors.deployments)||modelProfileRetirementBlocked(group)" :title="sectionErrors.deployments?'运行实例目录读取失败，不能安全退役':modelProfileRetirementBlocked(group)?'请先停用该版本下所有启用或异常运行实例':'仍有当前工作流引用时服务端会拒绝'" @click="openAction('retire-model',group.profile.id,`退役 · ${group.profile.profileKey} v${group.profile.version}`, '必须先停用启用或异常的运行实例；仍有当前工作流引用时服务端会拒绝。')"><Archive :size="11"/>退役版本</button>
                    </div>
                  </details>
                  <span v-if="group.profile.status==='retired'" class="platform-muted">只读历史版本</span>
                </div>
              </header>


              <div class="model-version-sections">
                <nav class="model-section-tabs" role="tablist" aria-label="版本详情分类">
                  <button :id="`model-capabilities-tab-${group.profile.id}`" type="button" role="tab" :aria-controls="`model-capabilities-panel-${group.profile.id}`" :aria-selected="selectedModelSection==='capabilities'" :class="{active:selectedModelSection==='capabilities'}" @click="selectedModelSection='capabilities'"><span>能力合同</span><b>{{ modelCapabilityList(group.profile).length }} 项</b></button>
                  <button :id="`model-artifacts-tab-${group.profile.id}`" type="button" role="tab" :aria-controls="`model-artifacts-panel-${group.profile.id}`" :aria-selected="selectedModelSection==='artifacts'" :class="{active:selectedModelSection==='artifacts'}" @click="selectedModelSection='artifacts'"><span>文件与证明</span><b>{{ group.artifacts.length }} 个 · 异常 {{ modelArtifactSummary(group.artifacts).attention }}</b></button>
                  <button :id="`model-deployments-tab-${group.profile.id}`" type="button" role="tab" :aria-controls="`model-deployments-panel-${group.profile.id}`" :aria-selected="selectedModelSection==='deployments'" :class="{active:selectedModelSection==='deployments'}" @click="selectedModelSection='deployments'"><span>运行实例</span><b>{{ group.deployments.length }} 个 · 异常 {{ modelDeploymentSummary(group.deployments).error }}</b></button>
                </nav>

                <section v-show="selectedModelSection==='capabilities'" :id="`model-capabilities-panel-${group.profile.id}`" class="model-section-panel model-capability-panel" role="tabpanel" :aria-labelledby="`model-capabilities-tab-${group.profile.id}`">
                  <header><span><strong>能力合同</strong><small>只读 · 当前选中的模型版本</small></span><button class="button ghost tiny" @click="showDetail('模型版本',`${group.profile.profileKey} · v${group.profile.version}`,group.profile)"><Eye :size="11"/>查看完整 JSON</button></header>
                  <ModelCapabilityContract v-if="selectedModelSection==='capabilities'" :profile="group.profile" :options="modelProfileOptions" />
                </section>

                <section v-show="selectedModelSection==='artifacts'" :id="`model-artifacts-panel-${group.profile.id}`" class="model-section-panel model-artifact-panel" role="tabpanel" :aria-labelledby="`model-artifacts-tab-${group.profile.id}`">
                  <header><span><strong>文件与证明（Model Artifact）</strong><small>声明与主机观测分别记录，不合并为单一可用状态</small></span><button v-if="group.profile.status==='draft'" class="button ghost tiny" @click="openModelArtifactDraft(group.profile)"><Plus :size="12"/>新增文件声明</button></header>
                  <div class="platform-dense-list model-nested-list model-information-artifact-list">
                    <article v-for="item in group.artifacts" :key="item.id">
                      <div><strong>{{ item.logicalName || item.filename }}</strong><small>{{ artifactRoleLabel(item.role) }} · {{ bytes(item.sizeBytes) }} · 声明相对路径 {{ item.filename }}<template v-if="item.inventoryFolder"> · 库存目录 {{ item.inventoryFolder }}</template></small><small>{{ artifactLatestAvailabilityLabel(item) }}</small><small v-if="item.verifiedAt">目录核验：{{ dateTime(item.verifiedAt) }}</small><small v-if="group.profile.status==='published'">已随发布版本冻结；路径、文件或摘要变化必须新建模型版本。</small><small v-else-if="group.profile.status==='retired'">所属模型版本已退役，本记录只读并保留历史证据。</small></div>
                      <PlatformStatusPill :status="item.status"/>
                      <span class="platform-row-actions"><button class="button ghost tiny" @click="showDetail('模型文件',item.logicalName||item.filename,item)"><Eye :size="11"/>详情</button><button v-if="artifactEditable(item)" class="button ghost tiny" @click="openEditor('artifact',item)">编辑声明</button><button v-if="artifactEditable(item)" class="button danger tiny" @click="openAction('delete-artifact',item.id,`删除模型文件关联 · ${item.logicalName}`, '仅草稿模型版本下、从未核验且没有主机观测或工作流引用的声明可删除。')"><Trash2 :size="11"/>删除草稿声明</button><button v-if="group.profile.status==='published'" class="button primary tiny" :disabled="!artifactEvidenceFor(item).verificationCandidate" :title="artifactVerificationTitle(item)" @click="openArtifactVerification(item)"><Check :size="11"/>按最新精确快照核验</button></span>
                    </article>
                    <p v-if="!group.artifacts.length" class="platform-empty model-compact-empty">{{ providerForModelProfile(group.profile)?.driverType==='comfyui'?'尚未声明本地模型文件；草稿版本可在这里新增。':'远端或托管模型无本地文件声明，明确为不适用（N/A）。' }}</p>
                  </div>
                </section>

                <section v-show="selectedModelSection==='deployments'" :id="`model-deployments-panel-${group.profile.id}`" class="model-section-panel model-deployment-panel" role="tabpanel" :aria-labelledby="`model-deployments-tab-${group.profile.id}`">
                    <div class="model-section-heading"><p>ComfyUI 按对应工作流分别验收；提示词策略按操作与模式选择。实例启用不等于烟测通过。</p><button v-if="group.profile.status==='published'" class="button ghost small" @click="openModelDeploymentDraft(group.profile)"><Plus :size="13"/>新增运行实例</button></div>
                    <div class="platform-dense-list model-nested-list model-instance-list">
                      <article v-for="item in group.deployments" :id="`model-deployment-${item.id}`" :key="item.id" :class="{'model-deployment-deep-focus':route.query.deployment===item.id&&['probe','smoke'].includes(String(route.query.focus||''))}">
                        <div><strong>{{ item.name || item.key }}</strong><small>{{ driverLabel(item.providerDriver) }} · 优先级（priority）{{ item.priority }}<template v-if="item.isDefault"> · 默认路由候选</template><template v-if="item.runtimeHost"> · 主机 {{ runtimeHostLabel(item.runtimeHost) }}</template></small><small v-if="deploymentSmokeFor(item.id)">持久任务 {{ deploymentSmokeFor(item.id)?.id.slice(0,8) }} · {{ operationLabel(deploymentSmokeFor(item.id)?.operation) }} · {{ freshnessLabel(deploymentSmokeFor(item.id)?.status) }} · 更新于 {{ dateTime(deploymentSmokeFor(item.id)?.updatedAt || '') }}</small><small v-if="deploymentSmokeErrors[item.id]" class="danger">{{ deploymentSmokeErrors[item.id] }}</small><small v-if="item.latestSmoke?.unresolvedSubmission&&!deploymentSmokeActiveStatuses.has(deploymentSmokeFor(item.id)?.status || '')" class="danger">{{ freshnessLabel(item.latestSmoke.unresolvedSubmission.status) }} · 提交状态待人工消歧 · 请求摘要（request）{{ item.latestSmoke.unresolvedSubmission.requestSha256.slice(0,12) }}</small></div>
                        <div class="model-instance-statuses"><span><small>实例状态</small><PlatformStatusPill :status="item.status"/></span></div>
                        <div class="platform-row-actions model-instance-actions">
                          <button class="button ghost tiny" @click="showDeploymentDetail(item)"><Eye :size="11"/>详情与证据</button>
                          <RegistryRelationSummary :row="deploymentRelations.rows.value[item.id]" :loading="deploymentRelations.loading.value" :error="deploymentRelations.error.value" @retry="deploymentRelations.reload" condensed/>
                          <RouterLink v-if="item.providerDriver==='comfyui'" class="button ghost tiny" :to="workflowSmokeUrlForDeployment(item)"><ShieldCheck :size="11"/>工作流验收</RouterLink>
                          <RouterLink v-else class="button ghost tiny" :to="{ path: '/settings/acceptance-smoke', query: { subject: 'deployment', deployment_id: item.id } }"><ShieldCheck :size="11"/>查看该实例验收</RouterLink>
                          <button v-if="item.status!=='retired'&&item.latestSmoke?.unresolvedSubmission&&auth.user?.isStaff&&!deploymentSmokeActiveStatuses.has(deploymentSmokeFor(item.id)?.status || '')" class="button danger tiny" @click="openSmokeResolution(item)"><ShieldCheck :size="11"/>处理不确定提交</button>
                          <button v-else-if="item.status!=='retired'&&deploymentSmokeFor(item.id)&&deploymentSmokeActiveStatuses.has(deploymentSmokeFor(item.id)?.status || '')" class="button danger tiny" :disabled="Boolean(deploymentSmokeBusyId)" @click="cancelDeploymentSmoke(deploymentSmokeFor(item.id)!)">{{ deploymentSmokeBusyId===deploymentSmokeFor(item.id)?.id?'取消中…':'取消任务' }}</button>
                          <button v-else-if="group.profile.status==='published'&&item.status==='active'&&deploymentSmokeOperations(item).length&&!item.latestSmoke?.unresolvedSubmission" class="button primary tiny" @click="openAction('deployment-smoke',item.id,`真实烟测 · ${item.name}`, '会提交真实低成本请求并可能产生费用；文本、图片和视频烟测都会先创建持久任务，再由页面轮询服务端终态。', {operation:deploymentSmokeOperations(item)[0]})"><Play :size="11"/>真实烟测（Smoke）</button>
                          <button v-else-if="group.profile.status==='published'&&item.status==='disabled'" class="button ghost tiny" @click="openAction('deployment-status',item.id,`重新启用运行实例 · ${item.name}`, '重新启用会再次校验运行合同；成功后才会恢复进入新生产路由。', {enable:true})"><Power :size="11"/>重新启用</button>
                          <details v-if="deploymentSmokeFor(item.id)||item.status==='disabled'||(group.profile.status==='published'&&item.status!=='retired')" class="model-more-menu" @click.stop>
                            <summary class="button ghost tiny">更多</summary>
                            <div>
                              <button v-if="deploymentSmokeFor(item.id)" class="button ghost tiny" @click="showDetail('运行实例烟测任务',`${item.name || item.key} · ${deploymentSmokeFor(item.id)?.status}`,deploymentSmokeFor(item.id))"><FileClock :size="11"/>烟测任务</button>
                              <button v-if="group.profile.status==='published'&&item.status!=='retired'" class="button ghost tiny" @click="openEditor('deployment',item)">编辑运行配置</button>
                              <button v-if="group.profile.status==='published'&&item.status==='active'&&item.providerDriver==='async_rest'" class="button ghost tiny" @click="openAction('deployment-probe',item.id,`探测 · ${item.name}`, '执行受控远端健康探测并追加不可变能力快照（capability snapshot）。')"><Gauge :size="11"/>探测（Probe）</button>
                              <button v-if="group.profile.status==='published'&&(item.status==='active'||item.status==='error')" class="button ghost tiny" @click="openAction('deployment-status',item.id,`停用运行实例 · ${item.name}`, '停用后不再进入新生产路由；历史运行与验证证据保持不变。', {enable:false})"><Power :size="11"/>停用</button>
                              <button v-if="item.status==='disabled'" class="button ghost tiny" @click="openAction('retire-deployment',item.id,`退役并保留历史 · ${item.name}`, '退役是不可逆的只读生命周期状态；存在当前配方或已发布工作流引用时服务端会拒绝。')"><Archive :size="11"/>退役保留历史</button>
                              <button v-if="item.status==='disabled'" class="button danger tiny" @click="openAction('delete-deployment',item.id,`复核运行实例删除 · ${item.name}`, '先完整读取配方、工作流、烟测、主机可用性与运行依赖；只有空记录才可删除，有证据时请改用退役。')"><Trash2 :size="11"/>删除复核</button>
                            </div>
                          </details>
                          <span v-if="group.profile.status!=='published'&&item.status!=='retired'" class="platform-muted">所属版本已退役；普通配置操作已关闭</span>
                          <span v-if="item.status==='retired'" class="platform-muted">只读历史实例</span>
                        </div>
                        <ModelDeploymentExecutionSummary v-if="tab==='models' && selectedModelSection==='deployments'" :deployment="item" :profile="group.profile"
                          :acceptance="deploymentAcceptanceFor(item.id)" :acceptance-error="sectionErrors.deploymentAcceptance" :acceptance-loading="loading"
                          :bindings="deploymentPromptBindings.rows.value" :bindings-loading="deploymentPromptBindings.loading.value" :bindings-error="deploymentPromptBindings.error.value"
                          @show-bindings="modelPromptContext={profile:group.profile,deployment:item}" @retry-bindings="deploymentPromptBindings.reload"/>
                      </article>
                      <p v-if="!group.deployments.length" class="platform-empty">{{ group.profile.status==='published'?'该版本还没有运行实例；创建后仍需分别完成适用的 Probe、文件核验和真实 Smoke。':'只有已发布版本可以创建运行实例。' }}</p>
                    </div>
                </section>
              </div>
            </article>
          </template>
          <div v-if="!selectedModelVersionGroup" class="model-inspector-state">
            <Boxes :size="24"/><h3>当前版本不在筛选结果中</h3><p>请选择上方仍可见的版本，或清除搜索后返回原版本。</p><button v-if="settingsListSearch" type="button" class="button ghost small" @click="settingsListSearch=''">清除搜索</button>
          </div>
        </section>
        <section v-if="!selectedModelFamily" id="model-family-inspector" class="model-family-inspector model-inspector-state" aria-live="polite">
          <Boxes :size="26"/><h3>{{ settingsListSearch?'当前模型不在筛选结果中':'当前模型已不在可见目录中' }}</h3><p>{{ settingsListSearch?'清除搜索后可返回原模型，也可以从左侧选择另一项。':'请从左侧重新选择模型；页面不会静默跳到其他对象。' }}</p><button v-if="settingsListSearch" type="button" class="button ghost small" @click="settingsListSearch=''">清除搜索</button>
        </section>
      </div>
      <p v-if="!modelRegistryFamilies.length&&!selectedModelFamilyKey&&!sectionErrors.modelProfiles" class="platform-empty model-registry-empty">{{ settingsListSearch?'没有匹配当前搜索的模型、版本、文件或运行实例。':'尚未登记模型；先确认服务连接，再新建模型草稿。' }}</p>
    </template>

    <template v-else-if="tab==='workflows'">
      <section class="platform-toolbar"><div><strong>ComfyUI 工作流版本（Workflow Version）</strong><span>发现并纳管不可变候选 → 生成平台执行合同草稿 → 提升为工作流版本草稿 → Live Validate → 真实 Smoke → 发布与生产路由；每层证据独立记录。</span></div><button v-if="auth.user?.isSuperuser" type="button" class="button ghost small" aria-controls="workflow-projector-catalog" :aria-expanded="workflowProjectorCatalogOpen" @click="revealWorkflowProjectorCatalog"><Settings2 :size="13"/>{{ workflowProjectorCatalogOpen?'定位 Projector 目录':'展开 Projector 目录' }}</button></section>
      <details v-if="auth.user?.isSuperuser" id="workflow-projector-catalog" ref="workflowProjectorCatalogElement" class="platform-card" :open="workflowProjectorCatalogOpen" @toggle="syncWorkflowProjectorCatalogOpen">
        <summary><span><Settings2 :size="16"/>工作流合同方案目录（Workflow Projector）</span><b>{{ workflowProjectors.length }} 个方案族 · {{ workflowProjectorVersions.length }} 个版本</b><ChevronDown :size="14"/></summary>
        <section class="workflow-projector-guide">
          <header><div><strong>它是“图结构解释规则”，不是本地工作流文件目录</strong><p>Projector 识别一类 ComfyUI 图，并把节点、连接和输入转换为平台可冻结的参数、模型文件、素材槽与输出合同。它不下载模型、不创建 Deployment，也不保存某次 Smoke 的参数或素材。</p></div><ShieldCheck :size="20"/></header>
          <div class="workflow-projector-guide-grid">
            <article><span>01 · 识别图族</span><strong>Detector</strong><p>根据节点数量、可选节点、关键输入和必要连接匹配真实图结构，不按文件名猜测。</p></article>
            <article><span>02 · 生成合同</span><strong>Parameters & Assets</strong><p>提取主输出、语义参数、模型文件角色、固定或动态素材槽，并保留无法解释的 blocker。</p></article>
            <article><span>03 · 连接执行</span><strong>Engine & Adapters</strong><p>引用代码中已注册的受审执行原语；Engine 唯一决定 adapter 与 projection 类型，不能注入任意代码。</p></article>
            <article><span>04 · 冻结审计</span><strong>Version & SHA</strong><p>合同草稿冻结精确 Projector 版本、Definition SHA 和 Adapter 身份；后继版本不会改写历史工作流。</p></article>
          </div>
          <div class="workflow-projector-flow" aria-label="Projector 与工作流导入流程">
            <span>受控目录 JSON</span><b>→</b><span>不可变候选</span><b>→</b><span>自动匹配 current published Projector</span><b>→</b><span>合同草稿</span><b>→</b><span>提升工作流版本</span><b>→</b><span>Live Validate · Smoke · 发布</span>
          </div>
          <p class="workflow-projector-guide-note">扫描纳管不要求 Projector；生成可提升的执行合同时才必须唯一命中。未匹配或歧义仍保留 candidate-only 草稿。发布新 Projector 后，来源字节未变可直接从同一候选重新生成草稿，无需重新扫描。</p>
        </section>
        <div class="platform-row-actions"><button type="button" class="button primary small" aria-haspopup="dialog" :disabled="Boolean(workflowProjectorCreateBlockedReason)" :title="workflowProjectorCreateBlockedReason||'打开新建方案族与 v1 草稿表单'" @click="openWorkflowProjectorCreate()"><Plus :size="13"/>{{ workflowProjectorResumeFromScan?'继续新建方案':'新建方案族与 v1 草稿' }}</button><small v-if="workflowProjectorCreateBlockedReason" class="platform-inline-error">暂不可新建：{{ workflowProjectorCreateBlockedReason }}</small><small v-else>支持从工作流推荐、复制已有版本或按注册引擎空白新建；创建 v1 不会自动发布。</small></div>
        <p v-if="sectionErrors.workflowProjectors||sectionErrors.workflowProjectorVersions||sectionErrors.workflowProjectorOptions||sectionErrors.workflowProjectorRefresh||workflowProjectorError" class="platform-inline-error">{{ sectionErrors.workflowProjectors || sectionErrors.workflowProjectorVersions || sectionErrors.workflowProjectorOptions || sectionErrors.workflowProjectorRefresh || workflowProjectorError }}</p>
        <div class="platform-dense-list">
          <article v-for="projector in workflowProjectors" :key="projector.id" :id="`workflow-projector-${projector.id}`">
            <div><strong>{{ projector.name }}</strong><small><code>{{ projector.key }}</code> · {{ projector.isActive?'启用':'停用' }} · {{ projector.description || '无说明' }}</small><div v-for="version in workflowProjectorVersionsFor(projector.id)" :key="version.id"><small>v{{ version.version }} · {{ version.status }}<template v-if="version.isCurrent"> · current</template> · {{ version.engineKey }}/v{{ version.engineVersion }} · {{ version.operation }} · {{ version.outputKind || '由主输出决定' }} · SHA {{ version.definitionSha256.slice(0,12) }}</small><RegistryRelationSummary :row="projectorRelations.rows.value[version.id]" :loading="projectorRelations.loading.value" :error="projectorRelations.error.value" @retry="projectorRelations.reload"/></div></div>
            <PlatformStatusPill :status="projector.isActive?'active':'disabled'"/>
            <span class="platform-row-actions">
              <button class="button ghost tiny" :disabled="Boolean(workflowProjectorBusy)||saving" @click="openAction('workflow-projector-status',projector.id,`${projector.isActive?'停用':'启用'}工作流合同方案族 · ${projector.name}`, `${projector.isActive?'停用后，新候选不会再使用该方案生成合同草稿；':'启用后，该方案已发布的 current 版本可参与新候选匹配；'}历史工作流冻结的 Projector 版本与 SHA 不会改写。`, {enable:!projector.isActive})">{{ projector.isActive?'停用方案族':'启用方案族' }}</button>
              <button v-if="projector.isActive&&!workflowProjectorVersionsFor(projector.id).length" type="button" class="button primary tiny" :disabled="Boolean(workflowProjectorCreateBlockedReason)" @click="openWorkflowProjectorCreate(projector)">创建 v1 草稿</button>
              <template v-for="version in workflowProjectorVersionsFor(projector.id)" :key="`action-${version.id}`">
                <button type="button" class="button ghost tiny" aria-haspopup="dialog" @click="openWorkflowProjectorEditor(version,true)"><Eye :size="11"/>查看 v{{ version.version }} 配置</button>
                <button v-if="version.status==='draft'" type="button" class="button ghost tiny" aria-haspopup="dialog" :disabled="Boolean(workflowProjectorMutationBlockedReason)" :title="workflowProjectorMutationBlockedReason||`打开 ${version.projectorKey} v${version.version} 草稿编辑表单`" @click="openWorkflowProjectorEditor(version)">编辑 v{{ version.version }} 草稿</button>
                <button v-if="version.status==='draft'" class="button primary tiny" :disabled="Boolean(workflowProjectorBusy)||saving" @click="openAction('publish-workflow-projector',version.id,`发布工作流合同方案 · ${version.projectorKey} v${version.version}`, '发布会复验 definition、已注册 adapter 与重叠冲突；成功后该版本成为 current。既有工作流继续使用各自冻结的 Projector 版本与 SHA。')">发布 v{{ version.version }}</button>
                <button v-if="version.status==='draft'" class="button danger tiny" :disabled="Boolean(workflowProjectorBusy)||saving" @click="openAction('discard-workflow-projector',version.id,`放弃工作流合同方案草稿 · ${version.projectorKey} v${version.version}`, '只删除该方案族最高、未发布且没有工作流合同草稿引用的版本；已发布版本、current 版本与历史工作流冻结事实不受影响。')"><Trash2 :size="11"/>放弃 v{{ version.version }} 草稿</button>
                <button v-if="version.isCurrent" class="button ghost tiny" :disabled="Boolean(workflowProjectorBusy)||saving||workflowProjectorVersionsFor(projector.id)[0]?.id!==version.id" @click="createNextWorkflowProjectorDraft(version)">创建后继草稿</button>
                <button v-if="version.status==='published'&&!version.isCurrent" class="button ghost tiny" :disabled="Boolean(workflowProjectorBusy)||saving" @click="openAction('retire-workflow-projector',version.id,`退役工作流合同方案 · ${version.projectorKey} v${version.version}`, '退役后该版本不再用于生成新的平台执行合同草稿；历史工作流中的冻结引用仍可审计和执行。')">退役 v{{ version.version }}</button>
              </template>
            </span>
          </article>
          <p v-if="!workflowProjectors.length&&!sectionErrors.workflowProjectors" class="platform-empty">尚无可管理方案；服务端迁移会初始化 Generic 与 MiniMax H3 的受审 v1 版本。</p>
        </div>
      </details>
      <div
        v-if="workflowModelContextRequested"
        class="platform-alert workflow-model-context"
        :class="['not_applicable','provider_inactive'].includes(workflowModelContextState)?'warning':workflowModelContextState==='ready'||workflowModelContextState==='loading'?'info':'danger'"
        aria-live="polite"
      >
        <GitBranch :size="16"/>
        <div v-if="workflowModelContextState==='loading'"><strong>正在核对精确模型版本上下文</strong><p><code>{{ workflowModelContextId || '无效 ID' }}</code>；在模型与 Provider 目录完成前，页面不会显示未限定的工作流或开放导入操作。</p></div>
        <div v-else-if="workflowModelContextState==='ready'&&workflowModelContextProfile">
          <strong>当前精确模型版本 · {{ workflowModelContextProfile.profileKey }} v{{ workflowModelContextProfile.version }}</strong>
          <p>{{ workflowModelContextProfile.modelIdentifier }} · {{ workflowModelContextProvider?.name || workflowModelContextProvider?.key }} · 已读取 {{ workflowVersions.length }} 个直接关联工作流版本</p>
          <small><code>{{ workflowModelContextProfile.id }}</code> · {{ workflowModelContextGuidance }} 点击模型页入口本身不会自动扫描、证明、Validate、Smoke、发布或生成。</small>
        </div>
        <div v-else><strong>工作流上下文不可用</strong><p>{{ workflowModelContextFailure }}</p></div>
        <button class="button ghost tiny" type="button" @click="clearWorkflowModelContext">清除模型上下文</button>
      </div>
      <details v-if="workflowModelContextCanImport" id="saved-workflow-importer" class="platform-card saved-workflow-importer" :aria-busy="savedWorkflowScanState==='loading'" open>
        <summary><span><Download :size="16"/>从 ComfyUI 已保存工作流批量导入</span><b v-if="savedWorkflowModelContext">{{ visibleSavedWorkflowItems.length }} / {{ savedWorkflowItems.length }}</b><b v-else>{{ savedWorkflowItems.length }}</b><ChevronDown :size="14"/></summary>
        <div v-if="workflowProjectorResumeFromScan" class="platform-alert info projector-scan-return" role="status">
          <div><strong>正在为新方案选择工作流</strong><p>表单内容已保留。扫描后纳管目标候选、确认主输出，再点击“生成专用 Projector 建议”；也可返回表单选择候选。关闭或刷新页面会丢失未保存内容。</p></div>
          <button type="button" class="button ghost small" :disabled="Boolean(savedWorkflowBusy)||Boolean(workflowProjectorBusy)" @click="openWorkflowProjectorCreate()">返回新建方案</button>
        </div>
        <div class="saved-workflow-controls">
          <button data-projector-scan-action class="button primary small" :disabled="loading||Boolean(savedWorkflowBusy)||!savedWorkflowDirectory" @click="scanSavedWorkflows"><RefreshCw :size="13" :class="{spin:savedWorkflowBusy==='scan'}"/>{{ savedWorkflowBusy==='scan'?(savedWorkflowModelContext?'正在扫描并筛选当前模型候选…':'正在扫描全部来源…'):(savedWorkflowModelContext?'扫描目录并筛选当前模型候选':'扫描已配置的 ComfyUI 工作流目录') }}</button>
          <span v-if="savedWorkflowScanState!=='idle'" aria-live="polite">已读取 {{ savedWorkflowSources.filter((row)=>row.status==='succeeded').length }}/{{ savedWorkflowSources.length }} 个来源 · {{ savedWorkflowItems.length }} 个文件</span>
        </div>
        <section v-if="savedWorkflowScanState!=='idle'" class="saved-workflow-process" aria-label="工作流导入进度">
          <article :class="savedWorkflowDiscoveryState">
            <span>1</span><div><strong>发现并纳管</strong><small>{{ savedWorkflowManagedCount }}/{{ currentModelSavedWorkflowItems.length }} 个当前候选已纳管</small></div>
          </article>
          <article :class="savedWorkflowContractState">
            <span>2</span><div><strong>生成执行合同</strong><small>{{ savedWorkflowContractCount }} 个已生成 · {{ currentModelSavedWorkflowContractBlockedCount }} 个需处理</small></div>
          </article>
          <article :class="promotableSavedWorkflowItems.length?'current':blockedSavedWorkflowItems.length?'blocked':'pending'">
            <span>3</span><div><strong>提升为工作流草稿</strong><small>{{ promotableSavedWorkflowItems.length }} 个已就绪<template v-if="blockedSavedWorkflowItems.length"> · {{ blockedSavedWorkflowItems.length }} 个需处理</template></small></div>
          </article>
        </section>
        <footer v-if="currentModelSavedWorkflowItems.length" class="saved-workflow-footer">
          <div><strong>步骤 3 · 提升工作流草稿</strong><span id="saved-workflow-promote-help">{{ promotableSavedWorkflowItems.length }} 项已就绪 · {{ selectedSavedWorkflowCount }} 项已加入提升 · {{ blockedSavedWorkflowItems.length }} 项需处理<template v-if="savedWorkflowBatchMaxItems === null"> · 服务端未返回批量上限</template></span></div>
          <span class="platform-row-actions">
            <button v-if="blockedSavedWorkflowItems.length" class="button ghost small" type="button" :disabled="Boolean(savedWorkflowBusy)" @click="focusFirstSavedWorkflowBlocker"><AlertTriangle :size="13"/>定位第一个阻断项（{{ blockedSavedWorkflowItems.length }}）</button>
            <button class="button primary small" aria-describedby="saved-workflow-promote-help" :disabled="!readySavedWorkflowItems.length||Boolean(savedWorkflowBusy)" @click="batchImportSavedWorkflows"><GitBranch :size="13"/>{{ savedWorkflowBusy==='import'?'正在提升工作流草稿…':readySavedWorkflowItems.length?`提升已加入的就绪项（${readySavedWorkflowItems.length}）`:'选择已就绪项后提升' }}</button>
          </span>
        </footer>
        <div class="saved-workflow-scan-config">
          <span><strong>扫描范围</strong> <code>userdata/{{ savedWorkflowDirectory || '未配置' }}</code></span>
          <span><strong>同步预算</strong> {{ savedWorkflowScanMaxSeconds ?? '未读取' }} 秒 · {{ savedWorkflowScanMaxItems ?? '未读取' }} 文件 · {{ savedWorkflowRequestMaxBytes ? bytes(savedWorkflowRequestMaxBytes) : '未读取' }}</span>
          <div class="platform-row-actions"><button class="button ghost tiny" type="button" @click="openWorkflowDirectoryConfiguration"><Settings2 :size="12"/>配置扫描目录</button><button class="button ghost tiny" type="button" @click="openWorkflowBudgetConfiguration"><ShieldAlert :size="12"/>{{ auth.user?.isSuperuser?'复核扫描预算':'查看扫描预算' }}</button></div>
        </div>
        <div v-if="sectionErrors.resolved" class="platform-alert danger"><AlertTriangle :size="15"/><p>无法读取扫描配置：{{ sectionErrors.resolved }}</p></div>
        <div v-else-if="!savedWorkflowDirectory" class="platform-alert warning"><AlertTriangle :size="15"/><p>尚未配置 {{ savedWorkflowDirectoryConfigKey }}，扫描已禁用；请先配置 ComfyUI userdata 相对目录。</p></div>
        <div v-if="savedWorkflowSources.some((row)=>row.status==='failed')" class="saved-workflow-source-errors" aria-live="polite"><p v-for="source in savedWorkflowSources.filter((row)=>row.status==='failed')" :key="source.sourceDeploymentId" class="platform-inline-error">{{ source.sourceLabel }} · {{ source.errorCode }} · {{ source.errorMessage }}</p></div>
        <details class="saved-workflow-help"><summary><BookOpenCheck :size="13"/>了解导入边界</summary><p>此处只纳管不可变工作流候选并生成执行合同；不选择某次 Smoke 或 ProductionRun 的具体参数与素材。<code>autoprompt/</code> 中的任务导出只登记为执行快照来源。</p></details>
        <div v-if="savedWorkflowModelContext&&savedWorkflowScanState!=='idle'&&savedWorkflowItems.length" class="saved-workflow-context-filter" aria-live="polite">
          <div><strong>当前模型扫描视图</strong><span>主列表仅显示精确关联、证据兼容或有明确阻断的候选；不会按文件名或相似模型臆测关联。</span></div>
          <button
            v-if="otherSavedWorkflowItems.length"
            class="button ghost tiny"
            type="button"
            :aria-expanded="showOtherSavedWorkflowResults"
            aria-controls="saved-workflow-scan-results"
            @click="showOtherSavedWorkflowResults=!showOtherSavedWorkflowResults"
          >{{ showOtherSavedWorkflowResults?'收起其他扫描结果':`显示其他扫描结果（${otherSavedWorkflowItems.length}）` }}</button>
        </div>
        <div v-if="savedWorkflowError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ savedWorkflowError }}</p></div>
        <p v-if="savedWorkflowScanActionable&&!savedWorkflowItems.length" class="platform-empty">扫描已完成，成功读取的受控目录中没有工作流文件；若存在失败来源，请查看上方错误。</p>
        <p v-if="savedWorkflowModelContext&&savedWorkflowScanActionable&&savedWorkflowItems.length&&!currentModelSavedWorkflowItems.length&&!showOtherSavedWorkflowResults" class="platform-empty">全目录扫描已完成，但没有发现当前精确模型版本的关联、证据兼容候选、可继续证明的模型草稿或结构合同受阻候选。可展开其他扫描结果排查，或清除模型上下文后重新扫描全局列表；页面不会按名称猜测关联。</p>
        <div v-if="savedWorkflowItems.length" id="saved-workflow-scan-results" class="saved-workflow-list">
          <article
            v-for="item in visibleSavedWorkflowItems"
            :id="savedWorkflowDomId(item)"
            :key="item.sourceKey"
            tabindex="-1"
            class="saved-workflow-row"
            :class="{'saved-workflow-row-diagnostic':savedWorkflowIsContextDiagnostic(item),'saved-workflow-row-contract-blocked':savedWorkflowContextKind(item)==='contract_blocked_candidate','saved-workflow-row-focused':savedWorkflowFocusedIssueKey===savedWorkflowKey(item)}"
          >
            <header>
              <label class="saved-workflow-select">
                <input
                  v-model="savedWorkflowDrafts[savedWorkflowKey(item)].selected"
                  type="checkbox"
                  aria-label="加入步骤 3 批量提升"
                  :title="savedWorkflowPromotable(item)?'加入步骤 3 批量提升':'先完成当前阻断项，合同就绪后才可加入提升'"
                  :disabled="!savedWorkflowScanActionable||Boolean(savedWorkflowBusy)||savedWorkflowIsContextReadOnly(item)||(item.suggestedImportKind==='template_version'&&!savedWorkflowPromotable(item))||(item.imported&&!item.contractUpgradeRequired&&!savedWorkflowDrafts[savedWorkflowKey(item)].projectorSuccessorRequested)||Boolean(item.errorCode)"
                />
                <span><strong>{{ item.sourcePath }}</strong><small>{{ savedWorkflowSourceFor(item)?.sourceLabel || 'ComfyUI' }} · {{ item.suggestedImportKind==='execution_snapshot'?'任务执行快照（只登记来源追踪 / provenance）':'用户模板' }} · {{ bytes(item.sizeBytes) }}</small><small v-if="savedWorkflowModelContext" class="saved-workflow-context-kind">{{ savedWorkflowContextKindLabel(item) }}</small></span>
              </label>
              <span class="platform-row-actions"><PlatformStatusPill :status="item.errorCode?'failed':item.contractUpgradeRequired?'upgrade_required':item.imported&&!savedWorkflowDrafts[savedWorkflowKey(item)].projectorSuccessorRequested?'imported':savedWorkflowPromotable(item)?'ready':item.actionability==='candidate_only'?'blocked':item.matchStatus"/><button v-if="savedWorkflowCanManageCandidate(item)" class="button primary tiny" :disabled="Boolean(savedWorkflowBusy)" @click="manageSavedWorkflowCandidate(item)"><Archive :size="11"/>步骤 1 · 纳管候选</button></span>
            </header>
            <div v-if="item.suggestedImportKind==='template_version'" class="saved-workflow-stepper" aria-label="当前候选的三步状态">
              <section v-for="step in [1,2,3]" :key="step" :class="savedWorkflowStepState(item,savedWorkflowDrafts[savedWorkflowKey(item)],step)">
                <span>{{ step }}</span><div><strong>{{ step===1?'纳管候选':step===2?'生成合同':'提升草稿' }}</strong><small>{{ savedWorkflowStepLabel(item,savedWorkflowDrafts[savedWorkflowKey(item)],step) }}</small></div>
              </section>
            </div>
            <p v-if="item.contractUpgradeRequired" class="platform-inline-error">已导入版本仍使用旧结构合同、旧烟测值/素材绑定或过期的服务端 projector；必须把同一源文件正式重导为原模板的新版本。不会旁路修改旧版本。</p>
            <p v-if="item.errorCode" class="platform-inline-error">{{ item.errorCode }} · {{ item.errorMessage }}</p>
            <div v-if="item.matchStatus!=='matched'" class="platform-alert warning saved-workflow-match-summary">
              <AlertTriangle :size="14"/>
              <div>
                <strong>自动关联尚未就绪</strong>
                <p>{{ savedWorkflowContextIssue(item,savedWorkflowDrafts[savedWorkflowKey(item)]) || item.matchReasonCodes.map(reasonCodeTitle).join('、') || '尚无唯一可验证的匹配候选。' }}</p>
                <small v-for="action in item.matchActions" :key="action.code">你需要：{{ savedWorkflowActionLabel(action.code) }}</small>
                <details v-if="item.matchReasonCodes.length||item.modelProfileCandidates.some((row)=>row.rejectionCodes.length)" class="saved-workflow-inline-diagnostics">
                  <summary>查看匹配诊断</summary>
                  <code v-if="item.matchReasonCodes.length">{{ item.matchStatus }} · {{ item.matchReasonCodes.join('、') }}</code>
                  <small v-for="candidate in item.modelProfileCandidates.filter((row)=>row.rejectionCodes.length)" :key="candidate.modelProfileVersionId">{{ candidate.profileKey }} v{{ candidate.version }}：{{ candidate.rejectionCodes.map(reasonCodeLabel).join('、') }}</small>
                </details>
              </div>
              <button v-if="item.matchActions.some((row)=>row.code==='register_model_profile'||row.code==='attest_or_publish_model_profile')" class="button ghost small" :disabled="!savedWorkflowScanActionable||Boolean(savedWorkflowBusy)||savedWorkflowIsContextReadOnly(item)||!modelProfileOptions" @click="openSavedWorkflowModelDraft(item)"><Plus :size="13"/>登记所需模型</button>
            </div>
            <div v-if="item.imported&&!item.contractUpgradeRequired&&!savedWorkflowDrafts[savedWorkflowKey(item)].projectorSuccessorRequested" class="platform-alert info saved-workflow-projector-refinement">
              <Settings2 :size="15"/>
              <div>
                <strong>当前工作流已冻结自动匹配的 Projector</strong>
                <p>已登记来源 {{ item.sourceId.slice(0,8) }}<template v-if="item.workflowVersionId"> · 工作流 {{ item.workflowVersionId.slice(0,8) }}</template>。可以从同一不可变候选生成专用建议并人工调整；发布方案后，再显式生成原模板的后继合同，现有版本不会改写。</p>
                <span class="platform-row-actions">
                  <button v-if="savedWorkflowCanRecommendProjector(item)" class="button ghost small" type="button" :disabled="Boolean(savedWorkflowBusy)||Boolean(workflowProjectorBusy)" @click="recommendWorkflowProjectorFromCandidate(item)"><Settings2 :size="13"/>基于此候选生成专用 Projector 建议</button>
                  <button v-if="savedWorkflowCanReprojectAsSuccessor(item)" class="button primary small" type="button" :disabled="Boolean(savedWorkflowBusy)||Boolean(workflowProjectorBusy)" @click="reprojectSavedWorkflowAsSuccessor(item)"><GitBranch :size="13"/>用当前已发布 Projector 生成后继合同</button>
                </span>
              </div>
            </div>
            <template v-if="savedWorkflowDrafts[savedWorkflowKey(item)].importKind==='template_version'&&(!item.imported||item.contractUpgradeRequired||savedWorkflowDrafts[savedWorkflowKey(item)].projectorSuccessorRequested)">
              <div v-if="item.imported&&savedWorkflowDrafts[savedWorkflowKey(item)].projectorSuccessorRequested" class="platform-alert info"><GitBranch :size="14"/><div><strong>正在创建原模板的后继工作流版本</strong><p>步骤 2 会读取当前已发布 Projector 并冻结新的合同；核对下方 Projector 身份后再勾选提升。原工作流版本保持不变。</p></div></div>
              <div class="saved-workflow-fields">
                <label>主输出画布节点（Canvas Node）<select v-model="savedWorkflowDrafts[savedWorkflowKey(item)].primaryOutputNodeId" :disabled="!savedWorkflowScanActionable||Boolean(savedWorkflowBusy)||item.matchStatus==='matched'" @change="refreshSavedWorkflowContract(item)"><option value="">请选择服务端返回的主输出候选</option><option v-for="candidate in savedWorkflowOutputCandidates(item,savedWorkflowDrafts[savedWorkflowKey(item)])" :key="candidate.canvasNodeId" :value="candidate.canvasNodeId">{{ candidate.canvasNodeId }} · {{ candidate.nodeType }} · {{ savedWorkflowOutputKindLabel(candidate.suggestedOutputKind) }}</option></select><small v-if="savedWorkflowOutputCandidates(item,savedWorkflowDrafts[savedWorkflowKey(item)]).some((candidate)=>!candidate.suggestedOutputKind)">“待 Projector 解析”仍可用于生成 candidate-only 合同草稿；补齐并发布匹配的 Projector 后重新生成，才可能提升为工作流版本草稿。</small></label>
                <label>{{ workflowModelContextRequested?'目标模型能力版本（精确上下文）':'目标模型能力版本（合同草稿可暂不选择）' }}<select v-model="savedWorkflowDrafts[savedWorkflowKey(item)].modelProfileVersionId" :disabled="!savedWorkflowScanActionable||Boolean(savedWorkflowBusy)||item.matchStatus==='matched'||!savedWorkflowDrafts[savedWorkflowKey(item)].primaryOutputNodeId" @change="refreshSavedWorkflowContract(item)"><option value="">{{ workflowModelContextRequested?'请先选择主输出；可确认当前精确模型版本，也可暂留待处理':'暂不绑定模型版本（合同草稿保留待处理项）' }}</option><option v-for="profile in savedWorkflowProfilesFor(item,savedWorkflowDrafts[savedWorkflowKey(item)])" :key="profile.id" :value="profile.id">{{ profile.profileKey }} · v{{ profile.version }} · {{ profile.status === 'draft' ? '待证明/发布' : '已发布' }}</option></select><small>模型版本不是生成合同草稿的前置条件；提升为可验收的工作流版本前仍需补齐精确模型绑定。</small></label>
                <button class="button ghost small" :disabled="!savedWorkflowScanActionable||Boolean(savedWorkflowBusy)||!item.candidateManaged||!savedWorkflowDrafts[savedWorkflowKey(item)].primaryOutputNodeId" :title="item.candidateManaged?'候选与主输出就绪后即可显式生成；模型版本可稍后处理，未匹配项会保留在合同草稿中':'请先纳管为不可变候选'" @click="previewSavedWorkflow(item)"><Eye :size="13"/>{{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.contractDraftId?'步骤 2 · 重新生成合同草稿':'步骤 2 · 生成合同草稿' }}</button>
                <button v-if="savedWorkflowCanRecommendProjector(item)" class="button ghost small" type="button" :disabled="Boolean(savedWorkflowBusy)||Boolean(workflowProjectorBusy)" title="系统会从当前候选生成未保存建议；保存仍只是草稿，发布后才参与新的合同匹配" @click="recommendWorkflowProjectorFromCandidate(item)"><Settings2 :size="13"/>生成专用 Projector 建议</button>
              </div>
              <div class="saved-workflow-fields">
                <label>模板目标（自动）<strong>{{ savedWorkflowDrafts[savedWorkflowKey(item)].useExistingTemplate?'追加既有模板':'新建模板' }}</strong><small>{{ savedWorkflowDrafts[savedWorkflowKey(item)].useExistingTemplate?savedWorkflowDrafts[savedWorkflowKey(item)].templateId:savedWorkflowDrafts[savedWorkflowKey(item)].newTemplateName }} · {{ savedWorkflowDrafts[savedWorkflowKey(item)].outputKind }}</small></label>
                <label v-if="savedWorkflowDrafts[savedWorkflowKey(item)].primaryOutputNodeId&&!selectedSavedWorkflowOutputCandidate(item,savedWorkflowDrafts[savedWorkflowKey(item)])?.suggestedOutputKind">平台产物类型（需确认）<select v-model="savedWorkflowDrafts[savedWorkflowKey(item)].outputKind" :disabled="Boolean(savedWorkflowBusy)" @change="confirmSavedWorkflowOutputKind(savedWorkflowDrafts[savedWorkflowKey(item)])"><option value="image">图片 · image</option><option value="video">视频 · video</option><option value="audio">音频 · audio</option><option value="text">文本 · text</option></select><small>结构目录无法替你决定业务产物语义；该选择会进入建议与后续合同复核，且不会丢弃刚取得的结构证据。</small></label>
              </div>
              <section
                v-if="savedWorkflowDrafts[savedWorkflowKey(item)].preview"
                class="saved-workflow-contract-outcome"
                :class="hardSavedWorkflowBlockers(savedWorkflowDrafts[savedWorkflowKey(item)]).length?'blocked':'ready'"
                aria-live="polite"
              >
                <header>
                  <div><strong>{{ hardSavedWorkflowBlockers(savedWorkflowDrafts[savedWorkflowKey(item)]).length?'步骤 2 已生成合同，但暂不能进入步骤 3':'步骤 2 合同检查通过' }}</strong><small v-if="!hardSavedWorkflowBlockers(savedWorkflowDrafts[savedWorkflowKey(item)]).length">{{ savedWorkflowPromotable(item)?'勾选行首复选框后，可加入批量提升。':'请完成页面上方的剩余前置条件。' }}</small></div>
                  <PlatformStatusPill :status="hardSavedWorkflowBlockers(savedWorkflowDrafts[savedWorkflowKey(item)]).length?'blocked':'ready'"/>
                </header>
                <div v-if="hardSavedWorkflowBlockers(savedWorkflowDrafts[savedWorkflowKey(item)]).length" class="saved-workflow-blocker-list">
                  <article v-for="blocker in hardSavedWorkflowBlockers(savedWorkflowDrafts[savedWorkflowKey(item)])" :key="`${blocker.code}:${blocker.detail}`">
                    <AlertTriangle :size="14"/>
                    <div><strong>{{ reasonCodeTitle(blocker.code) }}</strong><p>{{ blocker.detail }}</p><small>你需要：{{ savedWorkflowRecoveryLabel(savedWorkflowDrafts[savedWorkflowKey(item)],blocker.code,blocker.detail) }}</small><code>{{ blocker.code }}</code></div>
                  </article>
                  <button
                    v-if="hardSavedWorkflowBlockers(savedWorkflowDrafts[savedWorkflowKey(item)]).some((blocker)=>['required_dimension_multiple_unresolved','resolution_execution_adapter_unavailable'].includes(blocker.code))"
                    class="button ghost small"
                    type="button"
                    :disabled="Boolean(savedWorkflowBusy)"
                    @click="scanSavedWorkflows"
                  ><RefreshCw :size="13"/>已修复源工作流，重新扫描</button>
                </div>
              </section>
              <p v-if="workflowResolutionSummary(savedWorkflowDrafts[savedWorkflowKey(item)].preview?.suggestedInputContract)" class="muted">{{ workflowResolutionSummary(savedWorkflowDrafts[savedWorkflowKey(item)].preview?.suggestedInputContract) }}</p>
              <WorkflowResolutionInspector :contract="savedWorkflowDrafts[savedWorkflowKey(item)].preview?.suggestedInputContract" :options="workflowProjectorOptions?.imageResolution"/>
              <details v-if="savedWorkflowDrafts[savedWorkflowKey(item)].preview" class="saved-workflow-technical">
                <summary>查看合同技术证据 · 参数 {{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.configurableParameters.length }} · 模型文件 {{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.modelDependencies.length }} · 素材槽 {{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.assetDependencies.length }}</summary>
                <div class="saved-workflow-evidence">
                <small><strong>平台执行合同草稿</strong> {{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.contractDraftId ? `${savedWorkflowDrafts[savedWorkflowKey(item)].preview?.contractDraftId} · revision ${savedWorkflowDrafts[savedWorkflowKey(item)].preview?.contractDraftRevision}` : '尚未持久化' }} · 准入层级 {{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.admissionTier }} · {{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.actionability }}</small>
                <small>节点目录信息（object_info）{{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.objectInfoSchemaSha256.slice(0,12) }} · 转换后图（converted graph）{{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.convertedGraphSha256.slice(0,12)||'待选择主输出' }} · API 主输出 {{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.primaryApiNodeIds.join(', ')||'—' }}</small>
                <small>{{ savedWorkflowContractProjection(savedWorkflowDrafts[savedWorkflowKey(item)]) }}</small>
                <small>模型依赖 {{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.modelDependencies.length }} · 素材槽位 {{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.assetDependencies.length }} · 语义绑定（Semantic Bindings）{{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.suggestedSemanticBindings.length }}</small>
                <small>参数 Schema {{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.parameterSchemaSha256.slice(0,12)||'—' }} · 结构合同 {{ savedWorkflowDrafts[savedWorkflowKey(item)].preview?.structuralContractSha256.slice(0,12)||'—' }}。具体参数值与 Asset 不属于版本合同。</small>
                <div v-if="savedWorkflowDrafts[savedWorkflowKey(item)].preview?.configurableParameters.length" class="saved-workflow-parameters">
                  <div v-for="parameter in savedWorkflowDrafts[savedWorkflowKey(item)].preview?.configurableParameters" :key="parameter.semanticKey" class="saved-workflow-parameter">
                    <strong>{{ parameter.semanticKey }} · {{ parameter.type }}</strong>
                    <small>可变值范围 {{ parameter.min ?? '—' }}–{{ parameter.max ?? '—' }}<template v-if="parameter.multipleOf"> · {{ parameter.multipleOf }} 的倍数</template><template v-if="parameter.alignmentModulus"> · 合法值 {{ parameter.alignmentRemainder ?? 0 }} + {{ parameter.alignmentModulus }}k</template><template v-if="parameter.options.length"> · 允许 {{ parameter.options.join(', ') }}</template><template v-if="parameter.selectionGroup"> · 联动 {{ parameter.selectionGroup }}</template></small>
                    <small>当前画布值 {{ parameter.sourceValue }} · 节点目录默认值（object_info）{{ parameter.defaultValue ?? '—' }} · {{ parameter.target.nodeType }}.{{ parameter.target.inputName }} @ API {{ parameter.target.nodeId }}</small>
                  </div>
                </div>
                <ul v-if="savedWorkflowDrafts[savedWorkflowKey(item)].preview?.modelDependencies.length">
                  <li
                    v-for="dependency in savedWorkflowDrafts[savedWorkflowKey(item)].preview?.modelDependencies"
                    :key="`${dependency.nodeId}:${dependency.inputName}`"
                  >
                    {{ artifactRoleLabel(dependency.role) }} · {{ dependency.filename }} · 文件证据（artifact evidence）
                    {{ dependency.artifactMatches.filter((row)=>row.status==='available'&&row.verifiedSnapshot).length }}
                  </li>
                </ul>
                <div
                  v-for="dependency in savedWorkflowDrafts[savedWorkflowKey(item)].preview?.assetDependencies"
                  :key="`slot:${dependency.nodeId}:${dependency.inputName}`"
                  class="saved-workflow-fields"
                >
                  <label>
                    素材槽合同（Asset Slot）· {{ dependency.assetCategory }} · 槽位 {{ dependency.inputSlot }}
                    <small>{{ dependency.suggestedSemanticKey }} · {{ mediaKindLabel(dependency.mediaKind) }} · 数量 {{ dependency.minItems }}–{{ dependency.maxItems }} · {{ dependency.isRequired?'必填':'可选' }}</small>
                    <small>绑定目标 API {{ dependency.nodeId }}.{{ dependency.inputName }}；具体素材（Asset）在每次烟测或生产运行中按此槽位提交，可在基数范围内更换。</small>
                  </label>
                </div>
                <div v-if="savedWorkflowDrafts[savedWorkflowKey(item)].preview?.liveValidateRequirements.length" class="platform-alert info">
                  <ShieldCheck :size="14"/><div><strong>Live Validate 前置项（不阻止提升为工作流草稿）</strong><p v-for="requirement in savedWorkflowDrafts[savedWorkflowKey(item)].preview?.liveValidateRequirements" :key="`${requirement.code}:${requirement.detail}`">{{ reasonCodeTitle(requirement.code) }} · {{ requirement.detail }}</p></div>
                </div>
                </div>
              </details>
              <p v-if="savedWorkflowDrafts[savedWorkflowKey(item)].previewError" class="platform-inline-error">{{ savedWorkflowDrafts[savedWorkflowKey(item)].previewError }}</p>
              <div class="platform-row-actions" v-if="savedWorkflowDrafts[savedWorkflowKey(item)].preview?.convertedGraphSha256||savedWorkflowDrafts[savedWorkflowKey(item)].attestation">
                <button
                  v-if="savedWorkflowDrafts[savedWorkflowKey(item)].preview?.modelDependencies.length&&modelProfiles.find((row)=>row.id===savedWorkflowDrafts[savedWorkflowKey(item)].modelProfileVersionId)?.status==='draft'"
                  class="button ghost small"
                  :disabled="!savedWorkflowScanActionable||Boolean(savedWorkflowBusy)||runtimeProbeActiveStatuses.has(savedWorkflowDrafts[savedWorkflowKey(item)].attestation?.status||'')"
                  @click="attestSavedWorkflowModels(item)"
                >
                  <ShieldCheck :size="13"/>{{ runtimeProbeActiveStatuses.has(savedWorkflowDrafts[savedWorkflowKey(item)].attestation?.status||'')?'证明进行中':'证明所需模型文件' }}
                </button>
                <button
                  v-if="savedWorkflowDrafts[savedWorkflowKey(item)].attestation?.status==='succeeded'&&modelProfiles.find((row)=>row.id===savedWorkflowDrafts[savedWorkflowKey(item)].modelProfileVersionId)?.status==='draft'"
                  class="button primary small"
                  :disabled="!savedWorkflowScanActionable||Boolean(savedWorkflowBusy)"
                  @click="openAction('publish-model',savedWorkflowDrafts[savedWorkflowKey(item)].modelProfileVersionId,'审核并发布已证明的模型版本','发布门禁会复验每个 artifact 的精确 attestation closure、size/hash、source deployment 与新鲜 host snapshot。')"
                >审核并发布模型版本</button>
                <small v-if="savedWorkflowDrafts[savedWorkflowKey(item)].attestation">
                  attestation {{ savedWorkflowDrafts[savedWorkflowKey(item)].attestation?.id.slice(0,8) }} ·
                  {{ savedWorkflowDrafts[savedWorkflowKey(item)].attestation?.status }} ·
                  {{ savedWorkflowDrafts[savedWorkflowKey(item)].attestation?.progressMarker }}
                </small>
              </div>
            </template>
            <p v-else-if="savedWorkflowDrafts[savedWorkflowKey(item)].selected" class="platform-empty">此文件将只登记为 {{ savedWorkflowDrafts[savedWorkflowKey(item)].importKind }} 来源；不会创建 WorkflowTemplateVersion，也不会提交 ComfyUI 生成任务。</p>
          </article>
        </div>
        <div v-if="savedWorkflowResult" class="saved-workflow-results" aria-live="polite"><strong>批量结果 · {{ savedWorkflowResult.status }}</strong><p>新增 {{ savedWorkflowResult.createdCount }} · 复用 {{ savedWorkflowResult.reusedCount }} · 失败 {{ savedWorkflowResult.failedCount }}</p><ul><li v-for="row in savedWorkflowResult.items" :key="`${row.sourceDeploymentId}:${row.sourcePath}`"><PlatformStatusPill :status="row.result"/> {{ row.sourcePath }}<template v-if="row.code"> · {{ row.code }} · {{ row.detail }}</template></li></ul></div>
      </details>
      <p v-if="sectionErrors.workflows" class="platform-inline-error">{{ sectionErrors.workflows }}</p>
      <div v-if="workflowModelContextCatalogLoaded&&['none','ready','provider_inactive'].includes(workflowModelContextState)" class="platform-table-wrap"><table class="platform-table"><thead><tr><th>模板 / 版本</th><th>关联配置</th><th>产物</th><th>状态</th><th>在线校验</th><th>真实烟测</th><th>证据</th><th>操作</th></tr></thead><tbody>
        <tr v-for="item in workflowListState.items" :key="item.id"><td><strong>{{ item.templateName || item.templateSlug }}</strong><small>v{{ item.version }} · {{ item.graphSha256.slice(0,10) }}</small></td><td><WorkflowConfigurationLinks :workflow="item" compact/></td><td>{{ mediaKindLabel(item.outputKind) }}</td><td><PlatformStatusPill :status="item.status"/></td><td><PlatformStatusPill :status="item.validationState"/></td><td><PlatformStatusPill :status="item.smokeTestState"/></td><td><small>{{ item.executionContractSha256?`执行合同指纹 ${item.executionContractSha256.slice(0,12)}`:'尚无执行合同指纹' }}</small></td><td><span class="platform-row-actions"><button class="button ghost tiny" @click="showWorkflowDetail(item)"><Eye :size="11"/>详情与证据</button><template v-if="!workflowModelContextReadOnly"><RouterLink class="button ghost tiny" :to="`/settings/workflows/${item.id}`">验收与发布</RouterLink><button class="button ghost tiny" :disabled="Boolean(workflowExportBusyId)" @click="prepareWorkflowImport(item)"><Download :size="11"/>{{ workflowExportBusyId===item.id?'生成画布中…':'在 ComfyUI 中打开…' }}</button><button v-if="item.status==='draft'" class="button danger tiny" @click="openAction('delete-workflow',item.id,`删除工作流草稿 · ${item.templateName} v${item.version}`, '仅未被引用的草稿（draft）可删除；工作流不支持原地修改（PATCH）。')"><Trash2 :size="11"/></button><button v-if="item.status==='published'" class="button ghost tiny" @click="openAction('retire-workflow',item.id,`退役工作流 · ${item.templateName} v${item.version}`, '草稿或已发布配方仍引用时服务端会拒绝。')"><Archive :size="11"/>退役</button></template><span v-else class="platform-muted">只读历史</span></span></td></tr>
      </tbody></table><p v-if="!workflowListState.items.length&&!sectionErrors.workflows" class="platform-empty">{{ workflowModelContextRequested?'当前精确模型版本没有符合筛选条件的关联工作流版本。':'当前筛选下没有工作流版本。' }}</p></div>
    </template>

    <PromptProfileSettings v-else-if="tab==='prompts'" :key="String(route.query.model_profile_version||'')+String(route.query.prompt_profile_version||'')" :model-profile-version-id="String(route.query.model_profile_version||'')" :prompt-profile-version-id="String(route.query.prompt_profile_version||'')" :binding-id="String(route.query.binding||'')" :include-inactive="route.query.include_inactive==='1'"/>

    <template v-else-if="tab==='routing'">
      <section class="platform-card production-readiness production-readiness-overview">
        <header>
          <ShieldCheck :size="18"/>
          <div><strong>配方生产就绪概览</strong><small>平台级结论归属于配方与路由；首屏只给总体数量，原因按需下钻。</small></div>
          <PlatformStatusPill v-if="auth.user?.isSuperuser" :status="productionReadinessState"/>
          <span v-else class="production-readiness-unavailable">就绪事实未读取</span>
        </header>
        <template v-if="!auth.user?.isSuperuser">
          <div class="production-readiness-metrics">
            <article><span>当前可见版本</span><strong>{{ recipes.length }}</strong></article>
            <article><span>published 生命周期</span><strong>{{ recipes.filter(item=>item.status==='published').length }}</strong></article>
            <article><span>draft 生命周期</span><strong>{{ recipes.filter(item=>item.status==='draft').length }}</strong></article>
          </div>
          <p class="production-readiness-scope">当前账号无权读取平台级生产就绪诊断；以上只统计可见版本生命周期，不代表依赖健康或可生产。</p>
        </template>
        <div v-else-if="sectionErrors.productionReadiness" class="platform-alert danger">
          <AlertTriangle :size="15"/>
          <div><strong>就绪概览读取失败</strong><p>{{ sectionErrors.productionReadiness }}</p></div>
        </div>
        <div v-else-if="productionReadinessState==='unconfigured'" class="platform-alert warning">
          <AlertTriangle :size="15"/>
          <div><strong>尚未配置可发布生产配方</strong><p>原因代码：{{ (productionReadiness?.reasonCodes || ['published_recipe_missing']).map(reasonCodeLabel).join('、') }}</p></div>
        </div>
        <template v-else>
          <div class="production-readiness-metrics">
            <article><span>全部 published 版本</span><strong>{{ productionReadinessSummary.total }}</strong></article>
            <article><span>生产就绪</span><strong>{{ productionReadinessSummary.ready }}</strong></article>
            <article><span>生产阻断</span><strong>{{ productionReadinessSummary.blocked }}</strong></article>
            <article v-if="productionReadinessSummary.unknown"><span>无法判断</span><strong>{{ productionReadinessSummary.unknown }}</strong></article>
          </div>
          <p class="production-readiness-scope">口径为全部仍处于 published 的不可变版本；较早版本只有退役后才不再计入。具体版本及退役操作在下方配方清单中处理。</p>
          <details v-if="productionReadinessSummary.reasonGroups.length" class="production-readiness-reasons">
            <summary><span>按阻断原因下钻 · {{ productionReadinessSummary.reasonGroups.length }} 类</span><ChevronDown :size="14"/></summary>
            <div class="platform-dense-list">
              <article v-for="group in productionReadinessSummary.reasonGroups" :key="group.reasonCode">
                <div><strong>{{ reasonCodeLabel(group.reasonCode) }}</strong><small>{{ group.reasonCode }}</small></div>
                <b>{{ group.count }} 个版本</b>
                <button class="button ghost tiny" @click="drillReadinessReason(group.reasonCode)">{{ readinessReasonAction(group.reasonCode).label }}</button>
              </article>
            </div>
          </details>
        </template>
      </section>
      <section ref="recipeListAnchor" class="platform-toolbar">
        <div><strong>生产配方（Production Recipe）</strong><span>列表只概览阶段声明顺序；审核详情按阶段展示输入合同、模型 / Deployment / Workflow、输出与下一声明阶段，某次运行值仍在 ProductionRun 中审计。</span></div>
        <button class="button primary small" :disabled="!recipeEditorOptions||!!sectionErrors.recipes" :title="recipeEditorOptions&&!sectionErrors.recipes?'':'配方列表或编辑目录读取失败'" @click="openEditor('recipe')"><Plus :size="13"/>新建草稿配方</button>
      </section>
      <SettingsListFilter
        v-model:search="settingsListSearch"
        v-model:show-retired="showRetiredSettingsRecords"
        :visible-count="settingsListFilterSummary.visibleCount"
        :total-count="settingsListFilterSummary.totalCount"
        :hidden-retired-count="settingsListFilterSummary.hiddenRetiredCount"
        :placeholder="`搜索${currentSettingsSection.label}中的名称、机器键、类型、说明或状态`"
      />
      <div v-if="readinessReasonFilter" class="platform-alert info recipe-readiness-filter">
        <div><strong>正在按生产阻断原因筛选</strong><p>{{ reasonCodeLabel(readinessReasonFilter) }} · <code>{{ readinessReasonFilter }}</code></p></div>
        <button class="button ghost tiny" @click="readinessReasonFilter=''">清除原因筛选</button>
      </div>
      <p v-if="sectionErrors.recipes" class="platform-inline-error">{{ sectionErrors.recipes }}</p>
      <div class="platform-card-grid recipes">
        <details v-for="item in recipeListState.items" :key="item.id" :id="`recipe-${item.id}`" class="platform-card" @toggle="trackRecipeExpanded(item,$event)">
          <summary>
            <span><GitBranch :size="16"/>{{ item.name || item.recipeKey }} · v{{ item.version }}<small v-if="recipeRelations.rows.value[item.id]">{{ recipeRelations.rows.value[item.id]!.groups.map(group=>`${group.label} ${group.count}`).join(' · ') }}</small></span>
            <PlatformStatusPill :status="item.status"/>
            <span class="recipe-summary-readiness"><small>生产就绪</small><PlatformStatusPill v-if="productionReadinessByRecipeId.get(item.id)" :status="productionReadinessByRecipeId.get(item.id)!.state"/><em v-else>{{ item.status==='published'?'无法判断':'不适用' }}</em></span>
            <ChevronDown :size="14"/>
          </summary>
          <div class="recipe-detail">
            <RegistryRelationSummary :row="recipeRelations.rows.value[item.id]" :loading="recipeRelations.loading.value" :error="recipeRelations.error.value" @retry="recipeRelations.reload"/>
            <code>{{ item.recipeKey }} · {{ item.definitionSha256.slice(0,12) }} · {{ bytes(item.definitionSizeBytes) }}</code>
            <ProductionRecipeFlow v-if="expandedRecipeIds.has(item.id)" :recipe="item" :deployments="deployments" :model-profiles="modelProfiles" :workflows="recipeWorkflowCatalog" :workflow-detail-states="recipeWorkflowDetailStates" :workflow-detail-errors="recipeWorkflowDetailErrors" :recipe-editor-options="recipeEditorOptions" compact/>
            <div class="platform-row-actions">
              <button class="button ghost small" @click="showRecipeDetail(item)"><Eye :size="12"/>审核详情</button>
              <button v-if="item.status==='draft'" class="button ghost small" @click="openEditor('recipe',item)">编辑草稿</button>
              <button v-if="item.status==='draft'" class="button primary small" @click="openRecipePublish(item)">核对并发布</button>
              <button v-if="item.status==='draft'" class="button danger small" @click="openRecipeDelete(item)"><Trash2 :size="12"/>删除</button>
              <button v-if="['published','retired'].includes(item.status)" class="button ghost small" :disabled="saving" @click="createRecipeSuccessor(item)"><Copy :size="12"/>创建后继草稿</button>
              <button v-if="item.status==='published'" class="button ghost small" @click="openRecipeRetire(item)"><Archive :size="12"/>退役</button>
            </div>
          </div>
        </details>
        <p v-if="!recipeListState.items.length&&!sectionErrors.recipes" class="platform-empty">当前筛选下没有配方。普通用户只能读取已发布配方，写入权限由服务端判定。</p>
      </div>
    </template>

    <template v-else-if="tab==='defaults'||tab==='security'">
      <section class="platform-toolbar">
        <div><strong>{{ tab==='security'?'安全边界（Security Boundaries）':'最终生效配置（Effective Configuration）' }}</strong><span>{{ tab==='security'?'平台绝对保护值，命中后失败关闭（fail closed）；只有超级管理员可修改，普通平台管理员只读。':'只提交人工修改项（dirty keys）；“恢复继承”通过未设置键（unset_keys）删除系统覆盖（system override），不会复制模型或工作流默认值。' }}</span></div>
        <div><button v-if="canWriteCurrentSection" class="button ghost small" :disabled="!configDefinitionOptions" :title="configDefinitionOptions?'':'配置定义目录读取失败'" @click="openEditor('config-definition')"><Plus :size="13"/>新增定义</button><button v-if="canWriteCurrentSection" class="button primary small" :disabled="saving||(!configDirty.size&&!configUnsets.size)||Boolean(Object.keys(invalidConfigDrafts).length)" @click="saveDefaults"><Save :size="13"/>{{ currentSectionWriteRequiresSuperuser?'核对并发布安全边界':'核对并发布变更' }}</button></div>
      </section>
      <div v-if="currentSectionWriteRequiresSuperuser&&!canWriteCurrentSection" class="platform-alert warning"><ShieldAlert :size="15"/><p>当前账号可以查看安全边界及其生效来源，但修改、恢复继承、发布和激活均由服务端限制为超级管理员。</p></div>
      <div v-if="unknownConfigurationDefinitions.length" class="platform-alert danger"><AlertTriangle :size="15"/><div><strong>检测到 {{ unknownConfigurationDefinitions.length }} 条无法分类的配置定义，已失败关闭</strong><p>这些记录不会混入普通配置或安全边界，也不能在页面修改。请修复服务端 configuration_class 后重新读取。</p><ul><li v-for="item in unknownConfigurationDefinitions" :key="item.id"><code>{{ item.key }}</code> · {{ item.configurationClassRaw || '缺失 configuration_class' }}</li></ul></div></div>
      <p v-if="sectionErrors.definitions||sectionErrors.resolved" class="platform-inline-error">{{ sectionErrors.definitions || sectionErrors.resolved }}</p>
      <div class="resolved-fingerprint"><FileClock :size="15"/><span><strong>{{ resolved ? `当前分区共 ${configurationDefinitionsInClass.length} 项 · 待改 ${configDirty.size} · 待恢复 ${configUnsets.size}` : '未能解析最终生效配置' }}</strong><small>{{ resolved?.sha256 || '必须修复服务端返回的缺失键（missing keys）后才能创建生产运行（ProductionRun）' }}</small></span></div>
      <details v-for="[domain,items] in groupedDefinitions" :key="domain" class="platform-card config-domain" open>
        <summary><span><Settings2 :size="16"/>{{ domain }}</span><b>{{ items.length }}</b><ChevronDown :size="14"/></summary>
        <div class="config-fields"><label v-for="item in items" :key="item.id" :class="{secret:item.isSecret,changed:configDirty.has(item.key),unset:configUnsets.has(item.key),inactive:!item.isActive}"><span><strong>{{ item.key }}</strong><em>{{ valueTypeLabel(item.valueType) }} · {{ String(item.allowedScopes).split(',').map(scopeLabel).join('、') }}<template v-if="item.restartRequired"> · 重启生效</template><template v-if="!item.isActive"> · 已停用/退役</template></em></span><template v-if="!item.isSecret"><ConfigValueEditor v-model="configDraft[item.key]" :definition="item" :resource-options="configResourceOptions[item.key]||[]" :disabled="configUnsets.has(item.key)||!canModifyConfigDefinition(item)" @change="markConfigDirty(item.key)"/><p v-if="sectionErrors[`configResource:${item.key}`]" class="platform-inline-error">依赖选项读取失败：{{ sectionErrors[`configResource:${item.key}`] }}</p></template><template v-else><select v-if="auth.user?.isSuperuser" v-model="secretSelections[item.key]" :disabled="configUnsets.has(item.key)||!canModifyConfigDefinition(item)" @change="markConfigDirty(item.key)"><option value="">保持当前绑定</option><option v-for="secret in secretBindings.filter((row)=>row.isActive&&row.configured)" :key="secret.id" :value="secret.id">{{ secret.label || secret.key }} · v{{ secret.currentVersion }}</option></select><div v-else class="secret-value"><KeyRound :size="14"/>秘密值不回传浏览器；仅超级管理员可替换受管凭据。</div></template><div class="platform-row-actions"><button type="button" class="button ghost tiny" @click="showDetail('配置定义',item.key,item)"><Eye :size="11"/>详情</button><button v-if="canWriteCurrentSection" type="button" class="button ghost tiny" @click="openEditor('config-definition',item)">编辑定义</button><button v-if="canModifyConfigDefinition(item)&&item.configurationClass==='standard'&&sourceFor(item.key)?.source==='system'&&!configUnsets.has(item.key)" type="button" class="button ghost tiny" @click="restoreConfigInheritance(item.key)">恢复继承</button><button v-if="configDirty.has(item.key)||configUnsets.has(item.key)" type="button" class="button ghost tiny" @click="undoConfigChange(item.key)">撤销</button><button v-if="canWriteCurrentSection&&item.configurationClass==='standard'&&!item.isRequired&&!item.isActive" type="button" class="button danger tiny" @click="openAction('delete-definition',item.id,`删除配置定义 · ${item.key}`, '已有配置修订（revision）使用时服务端会拒绝；应保留历史并停用。')"><Trash2 :size="11"/></button></div><small><template v-if="item.needsReview">需要人工确认：{{ item.reviewReason || '缺少产品初始值' }} · </template>{{ item.description || '服务端未提供说明' }} · 来源 {{ effectiveSourceLabel(sourceFor(item.key)) }}</small></label></div>
      </details>
      <p v-if="!groupedDefinitions.length&&!sectionErrors.definitions" class="platform-empty">{{ settingsListSearch?'没有匹配当前搜索的配置定义。':'当前分类没有可显示的启用配置定义；可勾选“显示已退役/停用记录”复核历史记录。' }}</p>
      <details class="platform-card" open><summary><span><FileClock :size="16"/>配置修订（Revision）</span><b>{{ revisionListState.items.length }}</b><ChevronDown :size="14"/></summary><p v-if="sectionErrors.revisions" class="platform-inline-error">{{ sectionErrors.revisions }}</p><div class="platform-dense-list"><article v-for="item in revisionListState.items" :key="item.id"><div><strong>{{ scopeLabel(item.scopeType) }} · 修订号（revision）{{ item.revision }}</strong><small>{{ item.changeSummary || '无变更说明' }} · {{ item.valuesSha256.slice(0,12) }}</small></div><PlatformStatusPill :status="item.activationState||item.status"/><span class="platform-row-actions"><button class="button ghost tiny" @click="showDetail('配置修订',`修订号 ${item.revision}`,item)"><Eye :size="11"/>详情</button><button v-if="item.status==='draft'&&canWriteCurrentSection" class="button primary tiny" @click="openAction('publish-revision',item.id,`发布配置修订 ${item.revision}`, '服务端将复验过期基线（stale base）、必需值、安全边界与秘密绑定权限。')">发布</button><button v-if="(item.activationState==='pending_activation'||item.activationState==='failed')&&item.activationAvailable&&canWriteCurrentSection" class="button primary tiny" @click="openAction('activate-revision',item.id,`激活配置修订 ${item.revision}`, '仅新启动进程（boot）可激活需重启配置（restart_required）；服务端会再次复验基线、安全边界与秘密绑定权限。')">激活</button></span></article><p v-if="!revisionListState.items.length&&!sectionErrors.revisions" class="platform-empty">当前筛选下没有配置修订。</p></div></details>
    </template>

    <template v-else-if="tab==='runtime'">
      <section class="platform-card runtime-overview">
        <header>
          <Gauge :size="18"/>
          <div><strong>运行主机总体情况</strong><small>{{ runtimeOverviewSummary }}</small></div>
          <PlatformStatusPill :status="runtimeOverviewState"/>
        </header>
        <div class="runtime-overview-metrics">
          <article>
            <span>运行主机</span>
            <strong>{{ runtimeActiveHostCount }} / {{ runtimeHosts.length }} 启用</strong>
            <small>{{ runtimeHosts.length ? '只统计服务端登记事实' : '尚无已登记主机' }}</small>
          </article>
          <article>
            <span>所选主机</span>
            <strong>{{ selectedHost?.name || selectedHost?.key || '未选择' }}</strong>
            <small>{{ selectedHost ? freshnessLabel(selectedHost.status) : '无法判断' }}</small>
          </article>
          <article>
            <span>最近能力快照</span>
            <strong>{{ snapshot ? dateTime(snapshot.capturedAt) : '尚无快照' }}</strong>
            <small>{{ snapshot ? freshnessLabel(snapshot.probeStatus) : selectedSnapshotState==='error'?'读取失败':'等待探测' }}</small>
          </article>
          <article>
            <span>生效配置基线</span>
            <strong>{{ resolved ? `${Object.keys(resolved.values).length} 项` : '未能解析' }}</strong>
            <small>{{ resolved?.sha256 ? resolved.sha256.slice(0,16) : '配置缺失' }}</small>
          </article>
        </div>
        <section v-if="runtimeCapabilityItems.length" class="readiness-grid runtime-readiness-overview" aria-label="所选主机生产能力结论">
          <article v-for="[key,item] in runtimeCapabilityItems" :key="key">
            <span>{{ operationLabel(key) }}</span>
            <PlatformStatusPill :status="item.state"/>
            <strong>{{ item.deploymentCount }} 个部署</strong>
            <p>{{ item.reason }}</p>
          </article>
        </section>
        <p v-else class="platform-empty runtime-overview-empty">尚无可用于总体判断的主机能力结论；请在下方选择并探测主机。</p>
      </section>
      <SettingsListFilter
        v-model:search="settingsListSearch"
        v-model:show-retired="showRetiredSettingsRecords"
        :visible-count="settingsListFilterSummary.visibleCount"
        :total-count="settingsListFilterSummary.totalCount"
        :hidden-retired-count="settingsListFilterSummary.hiddenRetiredCount"
        :placeholder="`搜索${currentSettingsSection.label}中的名称、机器键、类型、说明或状态`"
      />
      <section class="platform-toolbar">
        <div><strong>主机探测与推荐</strong><span>总体结论在上方；这里选择主机并执行探测，推荐与决策必须属于同一当前快照。</span></div>
        <div>
          <button class="button ghost small" :disabled="Boolean(runtimeBusy)||Boolean(runtimeProbeBusyId)||selectedProbeActive||!selectedHost||selectedHost.status==='disabled'" @click="probe"><Cpu :size="13"/>{{ selectedProbeActive?'探测进行中':runtimeProbeBusyId?'正在入队…':'探测所选主机' }}</button>
          <button v-if="selectedProbeActive&&selectedProbeAttempt" class="button danger small" :disabled="Boolean(runtimeProbeBusyId)" @click="cancelRuntimeProbe(selectedProbeAttempt)">{{ selectedProbeAttempt.status==='cancel_requested'?'等待安全取消':'取消探测' }}</button>
          <button class="button primary small" :disabled="!canRecommend" @click="recommend"><Gauge :size="13"/>{{ runtimeBusy==='recommend'?'计算中…':'生成当前快照推荐' }}</button>
        </div>
      </section>
      <p v-if="sectionErrors.runtimeHosts||sectionErrors.runtimeSnapshot||sectionErrors.runtimeProbeAttempts" class="platform-inline-error">{{ sectionErrors.runtimeHosts || sectionErrors.runtimeSnapshot || sectionErrors.runtimeProbeAttempts }}</p>
      <section v-if="selectedProbeAttempt" class="platform-card runtime-probe-attempt">
        <header><Cpu :size="18"/><div><strong>持久主机探测 · {{ selectedProbeAttempt.id }}</strong><small>{{ selectedProbeAttempt.trigger }} · {{ selectedProbeAttempt.progressMarker }} · {{ runtimeProbeDurationLabel(selectedProbeAttempt) }} · 更新于 {{ dateTime(selectedProbeAttempt.updatedAt) }}</small></div><PlatformStatusPill :status="selectedProbeAttempt.status"/></header>
        <p v-if="selectedProbeActive">任务由平台维护执行器（platform-maintenance worker）执行；刷新或离开页面不会丢失。取消请求会在文件分块检查点生效，不会由浏览器中断请求线程。</p>
        <p v-if="selectedProbeAttempt.heartbeatAt">执行器心跳（worker heartbeat）：{{ dateTime(selectedProbeAttempt.heartbeatAt) }}</p>
        <p v-if="selectedProbeAttempt.finalSnapshot">终态快照：<code>{{ selectedProbeAttempt.finalSnapshot }}</code></p>
        <p v-if="runtimeProbeErrors[selectedHostId]" class="platform-inline-error">{{ runtimeProbeErrors[selectedHostId] }}</p>
      </section>
      <div class="platform-runtime-grid">
        <section class="platform-card runtime-summary">
          <header><Server :size="18"/><div><strong>事实 · 运行主机</strong><small>{{ runtimeHostListState.items.length }} 个当前可见主机</small></div></header>
          <div class="platform-dense-list runtime-host-list">
            <label v-for="host in runtimeHostListState.items" :key="host.id" class="runtime-host-option" :class="{selected:selectedHostId===host.id}">
              <input v-model="selectedHostId" type="radio" name="runtime-host" :value="host.id"/>
              <span><strong>{{ host.name || host.key }}</strong><small>最近上报 {{ dateTime(host.lastSeenAt) }}<template v-if="runtimeProbeFor(host.id)"> · 主机探测（probe）{{ freshnessLabel(runtimeProbeFor(host.id)?.status) }}</template></small></span>
               <PlatformStatusPill :status="host.status"/><span class="platform-row-actions"><button class="button ghost tiny" type="button" @click.prevent="showDetail('运行主机',host.name||host.key,host)"><Eye :size="11"/>详情</button><button class="button ghost tiny" type="button" @click.prevent="openAction('host-status',host.id,`${host.status==='disabled'?'启用':'停用'}主机 · ${host.name}`,host.status==='disabled'?'启用后先进入不可用（unavailable），必须重新探测（probe）才会成为启用（active）。':'仍有启用的模型部署（active deployment）时服务端会拒绝停用。',{enable:host.status==='disabled'})"><Power :size="11"/>{{ host.status==='disabled'?'启用':'停用' }}</button></span>
            </label>
            <p v-if="!runtimeHostListState.items.length" class="platform-empty">当前筛选下没有已登记主机。</p>
          </div>
        </section>
        <section class="platform-card runtime-summary">
          <header><Cpu :size="18"/><div><strong>事实 · 所选主机最近探测</strong><small>{{ snapshot ? dateTime(snapshot.capturedAt) : '本页尚未探测' }}</small></div><PlatformStatusPill v-if="snapshot" :status="snapshot.probeStatus"/></header>
           <dl v-if="snapshot">
            <div><dt>主机 / 快照</dt><dd><code>{{ selectedHost?.key }} / {{ snapshot.id }}</code></dd></div>
             <div><dt>处理器（CPU）</dt><dd>{{ snapshot.cpuName || snapshot.cpuArchitecture }} · {{ snapshot.logicalCpuCount }} 个逻辑线程（threads）</dd></div>
             <div><dt>图形处理器（GPU）</dt><dd>{{ snapshot.gpuCount }}</dd></div><div><dt>内存</dt><dd>{{ bytes(snapshot.memoryTotalBytes) }}</dd></div>
            <div><dt>磁盘可用</dt><dd>{{ bytes(snapshot.diskFreeBytes) }}</dd></div><div><dt>FFmpeg</dt><dd>{{ snapshot.ffmpegAvailable?'可用':'不可用' }}</dd></div>
            <div><dt>ComfyUI</dt><dd>{{ snapshot.comfyuiVersion || '未发现' }}</dd></div>
           </dl>
           <button v-if="snapshot" class="button ghost tiny" @click="showDetail('主机能力快照',`快照（snapshot）${snapshot.revision}`,snapshot)"><Eye :size="11"/>查看完整快照事实</button>
          <details v-if="snapshot?.modelAvailability.length" class="runtime-model-facts">
            <summary>下钻 · 模型文件观测证据 {{ snapshot.modelAvailability.length }} 项</summary>
            <article v-for="fact in snapshot.modelAvailability" :key="fact.id">
              <div><strong>{{ freshnessLabel(fact.status) }} · 模型文件（artifact）{{ fact.artifact || '未绑定' }}</strong><small>{{ fact.observationSource }} · {{ fact.reason }}</small></div>
              <dl>
                <div><dt>实测文件大小（observed_size_bytes）</dt><dd><code>{{ fact.observedSizeBytes ?? '未观测' }}</code><small v-if="fact.observedSizeBytes!==null">{{ bytes(fact.observedSizeBytes) }}</small></dd></div>
                <div><dt>实测内容指纹（observed_sha256）</dt><dd><code>{{ fact.observedSha256 || '未观测' }}</code><button v-if="fact.observedSha256" class="button ghost tiny" type="button" @click="copyObservedHash(fact.observedSha256)"><Copy :size="11"/>复制完整 SHA</button></dd></div>
              </dl>
            </article>
          </details>
          <p v-else-if="selectedSnapshotState==='missing'" class="platform-empty">所选主机尚无快照（404）；请点击“探测所选主机”创建第一份服务端事实。</p>
          <p v-else-if="selectedSnapshotState==='error'" class="platform-inline-error">快照接口读取失败；这不是“尚未探测”，请修复接口错误后重试。</p>
          <p v-else class="platform-empty">选择主机后点击“探测所选主机”获取服务端事实。</p>
        </section>
      </div>
      <details v-if="recommendation" class="platform-card recommendation" open>
        <summary><span><Gauge :size="16"/>推荐候选 · 主机快照（snapshot）{{ recommendation.hostSnapshot }}</span><PlatformStatusPill :status="recommendation.status"/><ChevronDown :size="14"/></summary>
        <p v-if="recommendation.reason">{{ recommendation.reason }}</p>
        <div class="platform-dense-list"><article v-for="item in recommendation.candidates" :key="item.id"><div><strong>{{ operationLabel(item.capabilityKey) }} · {{ item.deploymentKey }}</strong><small>{{ reasonCodeLabel(item.reasonCode) }}<template v-if="item.workflowVersion"> · 工作流（workflow）{{ item.workflowVersion.slice(0,8) }}</template></small></div><PlatformStatusPill :status="item.status"/><span class="platform-row-actions"><button class="button ghost tiny" :disabled="!canDecide||item.status==='unavailable'" @click="openDecision(item,'accept')">接受</button><button class="button ghost tiny" :disabled="!canDecide||item.status==='unavailable'" @click="openDecision(item,'override')">覆盖</button><button class="button ghost tiny" :disabled="!canDecide" @click="openDecision(item,'reject')">拒绝</button></span></article><p v-if="!recommendation.candidates.length" class="platform-empty">当前快照没有候选项。</p></div>
        <small>接受/覆盖只创建草稿配置修订（revision）或草稿配方；切换主机或重新探测会立即使本推荐失效。</small>
      </details>
    </template>

    <template v-else>
      <section class="platform-toolbar"><div><strong>受管凭据与配置审计</strong><span>数据库只保存 AES-256-GCM 密文；浏览器与 API 永不回传秘密正文。</span></div><button v-if="auth.user?.isSuperuser" class="button primary small" @click="openSecret('create')"><Plus :size="13"/>新增受管凭据</button></section>
      <div class="platform-governance-grid">
        <section v-if="auth.user?.isSuperuser" class="platform-card"><header><KeyRound :size="18"/><div><strong>受管凭据（SecretBinding）</strong><small>{{ secretBindingListState.items.length }} 个当前可见受管凭据；已配置（configured）不等于可调用</small></div></header><p v-if="sectionErrors.secretBindings" class="platform-inline-error">{{ sectionErrors.secretBindings }}</p><div class="platform-dense-list"><article v-for="item in secretBindingListState.items" :key="item.id"><div><strong>{{ item.label || item.key }}</strong><small>{{ item.key }} · 用途 {{ item.purpose }} · 版本 v{{ item.currentVersion || '—' }}</small></div><PlatformStatusPill :status="item.isActive&&item.configured?'configured':'blocked'"/><span class="platform-row-actions"><button class="button ghost tiny" @click="showDetail('受管凭据',item.label||item.key,item)"><Eye :size="11"/>元数据</button><button class="button ghost tiny" :disabled="!item.isActive" @click="openSecret('rotate',item)">轮换</button><button class="button ghost tiny" @click="openSecret('status',item)">{{ item.isActive?'停用':'启用' }}</button></span></article><p v-if="!secretBindingListState.items.length&&!sectionErrors.secretBindings" class="platform-empty">当前筛选下没有受管凭据。秘密只能由超级管理员创建。</p></div></section>
        <section class="platform-card"><header><KeyRound :size="18"/><div><strong>绑定与生命周期</strong><small>{{ credentialRowListState.items.length }} 个当前可见服务提供方 / 模型部署（Provider / Deployment）</small></div></header><div class="platform-dense-list"><article v-for="item in credentialRowListState.items" :key="item.owner"><div><strong>{{ item.owner }}</strong><small>{{ item.configured ? '已绑定受管凭据' : '尚未配置凭据' }} · 所有者状态（owner）{{ item.lifecycle }}<template v-if="item.configured&&!item.usable"> · 当前不可用</template></small></div><PlatformStatusPill :status="item.usable?'configured':'blocked'"/></article><p v-if="!credentialRowListState.items.length" class="platform-empty">当前筛选下没有服务提供方或模型部署（Provider / Deployment）。</p></div></section>
        <section class="platform-card lifecycle-audit-card">
          <header><ShieldCheck :size="18"/><div><strong>平台生命周期审计</strong><small>{{ filteredLifecycleAudit.length }} / {{ lifecycleAudit.length }} 条；平台管理员（staff）只读</small></div></header>
          <p v-if="sectionErrors.lifecycleAudit" class="platform-inline-error">{{ sectionErrors.lifecycleAudit }}</p>
          <div class="lifecycle-audit-filters">
            <label>对象类型<select v-model="lifecycleObjectFilter"><option value="">全部</option><option v-for="value in lifecycleObjectTypes" :key="value" :value="value">{{ value }}</option></select></label>
            <label>动作<select v-model="lifecycleActionFilter"><option value="">全部</option><option v-for="value in lifecycleActions" :key="value" :value="value">{{ value }}</option></select></label>
          </div>
          <div class="platform-dense-list">
            <article v-for="item in filteredLifecycleAudit" :key="item.id">
              <div><strong>操作（{{ item.action }}）· 对象类型（{{ item.objectType }}）</strong><small>{{ item.actorUsername || '系统（system）' }} · {{ reasonCodeLabel(item.reasonCode) }} · {{ dateTime(item.createdAt) }}</small><small>对象（object）{{ item.objectId }} · {{ item.beforeSha256.slice(0,12) }} → {{ item.afterSha256.slice(0,12) }}</small></div>
              <button class="button ghost tiny" @click="showDetail('平台生命周期审计',`${item.action} · ${item.objectType}`,item)"><Eye :size="11"/>安全摘要</button>
            </article>
            <p v-if="!filteredLifecycleAudit.length&&!sectionErrors.lifecycleAudit" class="platform-empty">当前筛选条件下暂无生命周期审计记录。</p>
          </div>
        </section>
        <section class="platform-card"><header><FileClock :size="18"/><div><strong>配置修订（Revisions）</strong><small>{{ revisionListState.items.length }} 条当前可见记录</small></div></header><p v-if="sectionErrors.revisions" class="platform-inline-error">{{ sectionErrors.revisions }}</p><div class="platform-dense-list revisions"><article v-for="item in revisionListState.items" :key="item.id"><div><strong>{{ scopeLabel(item.scopeType) }} · 修订号（revision）{{ item.revision }}</strong><small>{{ item.changeSummary || '无变更说明' }} · {{ dateTime(item.createdAt) }}<template v-if="item.changesSecurityBoundaries===true"> · 安全边界变更（仅 superuser 可发布/激活）</template><template v-else-if="item.changesSecurityBoundaries===null"> · 安全分类事实缺失（失败关闭）</template><template v-if="item.activationError"> · {{ item.activationError }}</template><template v-else-if="(item.activationState==='pending_activation'||item.activationState==='failed')&&!item.activationAvailable"> · 需由重启后的新进程激活</template></small></div><PlatformStatusPill :status="item.activationState||item.status"/><span class="platform-row-actions"><button class="button ghost tiny" @click="showDetail('配置修订',`修订号 ${item.revision}`,item)"><Eye :size="11"/>详情</button><button v-if="item.status==='draft'&&canWriteConfigRevision(item)" class="button primary tiny" @click="openAction('publish-revision',item.id,`发布修订 ${item.revision}`, '服务端会复验过期基线（stale base）与秘密绑定权限。')">发布</button><button v-if="(item.activationState==='pending_activation'||item.activationState==='failed')&&item.activationAvailable&&canWriteConfigRevision(item)" class="button primary tiny" @click="openAction('activate-revision',item.id,`激活修订 ${item.revision}`, '重启后的新进程将复验并激活该配置修订（revision）。')">激活</button></span></article><p v-if="!revisionListState.items.length&&!sectionErrors.revisions" class="platform-empty">当前筛选下没有配置修订（revision）。</p></div></section>
        <section v-if="auth.user?.isSuperuser" class="platform-card"><header><ShieldCheck :size="18"/><div><strong>密钥审计</strong><small>{{ secretAuditListState.items.length }} 条当前可见不可变记录</small></div></header><p v-if="sectionErrors.secretAudit" class="platform-inline-error">{{ sectionErrors.secretAudit }}</p><div class="platform-dense-list"><article v-for="item in secretAuditListState.items" :key="item.id"><div><strong>{{ item.action }} · {{ item.bindingKey }}</strong><small>{{ item.actorUsername || 'system' }} · {{ item.reason || '无敏感正文' }} · {{ dateTime(item.createdAt) }}</small></div><button class="button ghost tiny" @click="showDetail('密钥审计',`${item.action} · ${item.bindingKey}`,item)"><Eye :size="11"/>详情</button></article><p v-if="!secretAuditListState.items.length&&!sectionErrors.secretAudit" class="platform-empty">当前筛选下没有密钥审计记录。</p></div></section>
      </div>
    </template>

    <BaseModal
      :open="workflowProjectorCreateOpen"
      :dismissible="!workflowProjectorBusy&&!savedWorkflowBusy"
      :title="workflowProjectorCreateFamilyId?`创建 ${workflowProjectorCreateFamily.key} 的 v1 草稿`:'新建工作流合同方案'"
      eyebrow="结构化 Projector 配置 · 创建不等于发布"
      width="1240px"
      @close="closeWorkflowProjectorCreate"
    >
      <div class="workflow-projector-modal">
        <div class="workflow-projector-intro"><div><strong>本次会产生两个对象</strong><p>一个长期稳定的方案族，以及一个尚未发布的 v1 草稿。只有草稿另行复核并发布后，才会参与新候选的自动结构匹配。</p></div><span>Draft</span></div>
        <div v-if="workflowProjectorRecommendation" class="platform-alert warning"><AlertTriangle :size="15"/><div><strong>候选扫描建议尚未保存，也不会自动发布</strong><p>{{ workflowProjectorRecommendationSourcePath }} · 主输出 {{ workflowProjectorRecommendation.sourceEvidence.primaryCanvasNodeId }} / {{ workflowProjectorRecommendation.sourceEvidence.primaryNodeType }} · 建议 SHA {{ workflowProjectorRecommendation.recommendationSha256.slice(0,16) }}</p><ul><li v-for="warning in workflowProjectorRecommendation.warnings" :key="warning">{{ warning }}</li></ul></div></div>
        <section class="workflow-projector-form-section">
          <header><span>1</span><div><strong>方案身份</strong><small>key 是图族的长期身份；名称和说明帮助管理员理解适用边界。</small></div></header>
          <div class="workflow-projector-form-grid">
            <label><span>方案族 key *</span><input v-model="workflowProjectorCreateFamily.key" :disabled="Boolean(workflowProjectorBusy)||Boolean(workflowProjectorCreateFamilyId)" placeholder="例如 minimax_h3.custom_ref2va" autocomplete="off"/><small>创建后不可修改；仅允许小写字母、数字、点、下划线与连字符。</small></label>
            <label><span>方案族名称 *</span><input v-model="workflowProjectorCreateFamily.name" :disabled="Boolean(workflowProjectorBusy)||Boolean(workflowProjectorCreateFamilyId)" placeholder="面向管理员的名称" autocomplete="off"/><small>描述它解释的工作流图族，而不是某个模型实例。</small></label>
            <label class="wide"><span>方案族说明</span><textarea v-model="workflowProjectorCreateFamily.description" rows="3" :disabled="Boolean(workflowProjectorBusy)||Boolean(workflowProjectorCreateFamilyId)" placeholder="适用图族、边界与变更原因"/></label>
          </div>
        </section>
        <section class="workflow-projector-form-section">
          <header><span>2</span><div><strong>初始化方式与执行引擎</strong><small>三种方式进入同一个编辑器；创建的是独立方案。执行引擎身份由系统注册，不是可自由拼接的业务参数。</small></div></header>
          <div class="workflow-projector-mode-choices" role="radiogroup" aria-label="方案初始化方式" @keydown="navigateWorkflowProjectorCreateMode">
            <button v-for="mode in workflowProjectorCreateModes" :key="mode.key" type="button" role="radio" :aria-checked="workflowProjectorCreateMode===mode.key" :tabindex="workflowProjectorCreateMode===mode.key?0:-1" :disabled="Boolean(workflowProjectorBusy)||Boolean(savedWorkflowBusy)" @click="changeWorkflowProjectorCreateMode(mode.key)">
              <span class="mode-indicator" aria-hidden="true"/><span><strong>{{ mode.label }}<small v-if="'recommended' in mode">推荐</small></strong><span>{{ mode.help }}</span></span>
            </button>
          </div>
          <template v-if="workflowProjectorCreateMode==='recommendation'">
            <WorkflowProjectorRecommendationStep :scan-state="savedWorkflowScanState" :scanned-count="savedWorkflowItems.length" :candidates="workflowProjectorRecommendationChoices" :selected-key="workflowProjectorCreateCandidateKey" :busy="Boolean(savedWorkflowBusy)||Boolean(workflowProjectorBusy)" :recommending="savedWorkflowBusy.startsWith('projector-recommendation:')" :unavailable-reason="workflowProjectorRecommendationUnavailableReason" @select="workflowProjectorCreateCandidateKey=$event" @recommend="requestProjectorRecommendationFromCreate" @scan="goToProjectorCandidateScan"/>
            <p v-if="workflowProjectorRecommendation&&!workflowProjectorCreateInitialized" class="platform-inline-error">候选选择或证据已变化，请重新生成对应推荐后再创建。</p>
          </template>
          <template v-else-if="workflowProjectorCreateMode==='copy'">
            <label class="workflow-projector-seed-field"><span>复制哪个已有版本</span><select :value="workflowProjectorCreateSeedVersionId" :disabled="Boolean(workflowProjectorBusy)||!workflowProjectorSeedVersions.length" @change="changeWorkflowProjectorCreateSeed"><option value="" disabled>请选择版本</option><option v-for="seed in workflowProjectorSeedVersions" :key="seed.id" :value="seed.id">{{ seed.projectorKey }} v{{ seed.version }} · {{ seed.status }}<template v-if="seed.isCurrent"> · current</template> · SHA {{ seed.definitionSha256.slice(0,12) }}</option></select><small>复制完整配置作为初值，不建立运行时继承关系；新方案仍需独立复核。</small></label>
            <p v-if="!workflowProjectorSeedVersions.length" class="platform-empty">暂无已有版本可复制，仍可使用空白新建。</p>
          </template>
          <template v-else>
            <label class="workflow-projector-seed-field"><span>选择已注册执行引擎</span><select :value="workflowProjectorCreateVersion.engine" :disabled="Boolean(workflowProjectorBusy)" @change="initializeBlankWorkflowProjector"><option value="" disabled>请选择引擎</option><option v-for="engine in workflowProjectorOptions?.engines||[]" :key="engine.key" :value="workflowProjectorEngineValue(engine.key,engine.version)">{{ engine.label }} · {{ engine.key }}/v{{ engine.version }}</option></select><small>使用服务端提供的空白骨架，不复制任何现有版本的节点、参数、模型或素材规则。需填写输出和业务规则后才能保存。</small></label>
          </template>
          <dl v-if="workflowProjectorCreateEngine" class="workflow-projector-contract-facts">
            <div><dt>Engine</dt><dd>{{ workflowProjectorCreateEngine?.label || '未解析' }}<code>{{ workflowProjectorCreateEngine?`${workflowProjectorCreateEngine.key}/v${workflowProjectorCreateEngine.version}`:'' }}</code></dd></div>
            <div><dt>Provider Driver</dt><dd>{{ workflowProjectorCreateVersion.providerDriverType || '未解析' }}</dd></div>
            <div><dt>Operation</dt><dd>{{ workflowProjectorCreateVersion.operation || '未解析' }}</dd></div>
            <div><dt>Adapter 合同</dt><dd><code>{{ workflowProjectorCreateEngine?`${workflowProjectorCreateEngine.adapterContract.compiler} · ${workflowProjectorCreateEngine.adapterContract.output} · ${workflowProjectorCreateEngine.adapterContract.dynamic}`:'未解析' }}</code></dd></div>
          </dl>
          <label v-if="workflowProjectorCreateEngine" class="workflow-projector-output-field"><span>版本固定产物类型</span><select v-model="workflowProjectorCreateVersion.outputKind" :disabled="Boolean(workflowProjectorBusy)||Boolean(savedWorkflowBusy)||!workflowProjectorOptions"><option v-for="option in workflowProjectorOptions?.outputKinds||[]" :key="option.value||'auto'" :value="option.value">{{ option.label }}<template v-if="option.value"> · {{ option.value }}</template></option></select><small>留空表示由主输出节点决定；非空时必须与 Definition 的输出映射一致。</small></label>
        </section>
        <section v-if="workflowProjectorOptions&&workflowProjectorCreateEngine" class="workflow-projector-form-section definition">
          <header><span>3</span><div><strong>图结构与执行合同参数</strong><small>默认按分区编辑；完整 JSON 仅位于最后的高级复核区，并与参数项使用同一个草稿。</small></div></header>
          <WorkflowProjectorDefinitionEditor :key="`create:${workflowProjectorCreateAppliedSeedId}`" v-model="workflowProjectorCreateDefinition" :options="workflowProjectorOptions" :engine-key="workflowProjectorCreateEngine.key" :engine-version="workflowProjectorCreateEngine.version" :recommendation="workflowProjectorRecommendation" :disabled="Boolean(workflowProjectorBusy)||Boolean(savedWorkflowBusy)" :model-profiles="modelProfiles" :model-profiles-error="sectionErrors.modelProfiles" :initial-model-profile-id="workflowModelContextId" @validity="workflowProjectorCreateDefinitionValidity=$event"/>
        </section>
        <p v-else class="platform-empty">完成上方初始化后，在这里编辑同一份配置草稿。未初始化或字段未完成时不能提交。</p>
        <div v-if="workflowProjectorCreateFamilyId" class="platform-alert warning"><AlertTriangle :size="15"/><p>方案族已经存在。当前提交只会重试创建 v1 草稿，不会重复创建或改写方案族；若 v1 仍失败，页面会保留服务端错误。</p></div>
        <div v-if="workflowProjectorError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ workflowProjectorError }}</p></div>
      </div>
      <template #footer><button class="button ghost" :disabled="Boolean(workflowProjectorBusy)||Boolean(savedWorkflowBusy)" @click="closeWorkflowProjectorCreate">取消</button><button class="button primary" :disabled="Boolean(workflowProjectorBusy)||Boolean(savedWorkflowBusy)||!workflowProjectorOptions||!workflowProjectorCreateFamily.name.trim()||(!workflowProjectorCreateFamilyId&&!workflowProjectorCreateFamily.key.trim())||!workflowProjectorCreateInitialized||!workflowProjectorCreateVersion.providerDriverType||!workflowProjectorCreateVersion.operation||!workflowProjectorCreateDefinitionValidity.valid" @click="submitWorkflowProjectorCreate">{{ workflowProjectorBusy?'服务端创建与校验中…':workflowProjectorCreateFamilyId?'创建 v1 草稿':'创建方案族与 v1 草稿' }}</button></template>
    </BaseModal>

    <BaseModal
      :open="workflowProjectorEditorOpen"
      :dismissible="!workflowProjectorBusy"
      :title="workflowProjectorEditorVersion?`${workflowProjectorReadOnly?'查看':'编辑'}工作流合同方案 · ${workflowProjectorEditorVersion.projectorKey} v${workflowProjectorEditorVersion.version}`:'工作流合同方案'"
      :eyebrow="workflowProjectorReadOnly?'版本配置 · 只读':'结构化 Projector 草稿 · 发布后不可修改'"
      width="1240px"
      @close="closeWorkflowProjectorEditor"
    >
      <div v-if="workflowProjectorEditorVersion" class="workflow-projector-modal">
        <div class="workflow-projector-intro"><div><strong>{{ workflowProjectorReadOnly?'当前版本配置（只读）':'当前只编辑草稿版本' }}</strong><p>{{ workflowProjectorReadOnly?'可逐区查看节点、参数、素材和完整 JSON。调整已发布方案需创建后继草稿。':'保存仍不会参与新候选匹配；发布动作会再次校验 Definition、注册 Adapter 和方案冲突。' }}</p></div><span>v{{ workflowProjectorEditorVersion.version }} · {{ workflowProjectorEditorVersion.status }}{{ workflowProjectorEditorVersion.isCurrent?' · current':'' }}</span></div>
        <section class="workflow-projector-form-section">
          <header><span>1</span><div><strong>执行边界</strong><small>Engine、Provider 与 operation 沿用草稿身份；Adapter 和 projection 类型由 Engine 自动锁定。</small></div></header>
          <dl class="workflow-projector-contract-facts">
            <div><dt>Engine</dt><dd>{{ workflowProjectorEditorEngine?.label || '未解析' }}<code>{{ workflowProjectorEditorEngine?`${workflowProjectorEditorEngine.key}/v${workflowProjectorEditorEngine.version}`:'' }}</code></dd></div>
            <div><dt>Provider Driver</dt><dd>{{ workflowProjectorEditorDraft.providerDriverType || '未解析' }}</dd></div>
            <div><dt>Operation</dt><dd>{{ workflowProjectorEditorDraft.operation || '未解析' }}</dd></div>
            <div><dt>当前证据</dt><dd><code>SHA {{ workflowProjectorEditorVersion.definitionSha256.slice(0,16) }} · {{ workflowProjectorEditorVersion.definitionSizeBytes }} B</code></dd></div>
          </dl>
          <label class="workflow-projector-output-field"><span>版本固定产物类型</span><select v-model="workflowProjectorEditorDraft.outputKind" :disabled="workflowProjectorReadOnly||Boolean(workflowProjectorBusy)||!workflowProjectorOptions"><option v-for="option in workflowProjectorOptions?.outputKinds||[]" :key="option.value||'auto'" :value="option.value">{{ option.label }}<template v-if="option.value"> · {{ option.value }}</template></option></select><small>留空表示由主输出节点决定；保存后服务端会核对 Definition 输出映射。</small></label>
        </section>
        <section v-if="workflowProjectorOptions&&workflowProjectorEditorEngine" class="workflow-projector-form-section definition">
          <header><span>2</span><div><strong>图结构与执行合同参数</strong><small>{{ workflowProjectorReadOnly ? '当前版本只读；完整 JSON 可在最后一个分区查看和复制。' : '按业务含义编辑；高级 JSON 默认收起在最后一个分区。' }}</small></div></header>
          <WorkflowProjectorDefinitionEditor :key="`${workflowProjectorEditorVersion.id}:${workflowProjectorReadOnly}`" v-model="workflowProjectorDefinition" :options="workflowProjectorOptions" :engine-key="workflowProjectorEditorEngine.key" :engine-version="workflowProjectorEditorEngine.version" :read-only="workflowProjectorReadOnly" :disabled="Boolean(workflowProjectorBusy)" :model-profiles="modelProfiles" :model-profiles-error="sectionErrors.modelProfiles" :initial-model-profile-id="workflowModelContextId" :workflow-context-id="workflowProjectorEditorContextId" :projector-version="workflowProjectorEditorVersion" @validity="workflowProjectorDefinitionValidity=$event"/>
        </section>
        <div v-else class="platform-alert danger"><AlertTriangle :size="15"/><div><p>当前 Engine 的结构化展示目录不可用，可核对以下版本原始配置。</p><pre>{{ JSON.stringify(workflowProjectorDefinition,null,2) }}</pre></div></div>
        <div v-if="workflowProjectorError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ workflowProjectorError }}</p></div>
      </div>
      <template #footer><button v-if="workflowProjectorReadOnly&&auth.user?.isSuperuser" class="button ghost" :disabled="Boolean(workflowProjectorBusy)" @click="manageViewedWorkflowProjector">管理此方案族 / 配置后继草稿</button><button class="button ghost" :disabled="Boolean(workflowProjectorBusy)" @click="closeWorkflowProjectorEditor">{{ workflowProjectorReadOnly?'关闭':'取消' }}</button><button v-if="!workflowProjectorReadOnly" class="button primary" :disabled="Boolean(workflowProjectorBusy)||!workflowProjectorOptions||!workflowProjectorEditorEngine||!workflowProjectorEditorDraft.providerDriverType||!workflowProjectorEditorDraft.operation||!workflowProjectorDefinitionValidity.valid" @click="saveWorkflowProjectorDraft">{{ workflowProjectorBusy? '服务端校验并保存中…':'保存 Projector 草稿' }}</button></template>
    </BaseModal>

    <BaseModal :open="Boolean(modelPromptContext)" title="模型与提示词策略关联" eyebrow="运行实例 · 模型版本 · 提示词绑定" width="1100px" @close="modelPromptContext=null">
      <ModelPromptBindings v-if="modelPromptContext" :profile="modelPromptContext.profile" :deployment="modelPromptContext.deployment"/>
      <template #footer><button class="button ghost" @click="modelPromptContext=null">关闭</button></template>
    </BaseModal>

    <BaseModal :open="editorOpen" :dismissible="!saving" :title="editorIntent==='next-model-version'?`新增下一模型版本`:`${editorId?'编辑':'新增'} ${editorResourceLabel}`" eyebrow="结构化字段（Schema-driven）· 服务端校验" width="980px" @close="editorOpen=false">
      <div class="registry-editor-modal"><p><template v-if="editorIntent==='next-model-version'">已继承源版本合同；模型版本族标识保持锁定，保存时由服务端原子分配下一版本号。</template><template v-else>常用参数已拆分为类型化字段；必填、默认值、影响和前置依赖会就近提示。</template> 折叠的高级 JSON 只承载驱动或合同扩展，服务端仍执行最终校验且不会本地伪成功。<template v-if="editorId&&['provider','deployment'].includes(editorResource)"> 受管凭据不会回显；默认保持现有绑定。</template></p><div v-if="editorResource==='artifact'&&editorId" class="platform-alert warning"><AlertTriangle :size="15"/><p>这里只调整草稿中的声明相对路径、库存映射或摘要，不会移动真实模型文件；任何定位或 size/hash 变化都会使旧核验证据失效，保存后必须重新运行主机探测。</p></div><label v-if="auth.user?.isSuperuser&&['provider','deployment'].includes(editorResource)" class="editor-credential">受管凭据<select v-model="editorCredentialId"><option :value="editorId?'__keep__':''">{{ editorId?'保持现有绑定':'不绑定凭据' }}</option><option v-if="editorId" value="__clear__">解除现有绑定</option><option v-for="secret in secretBindings.filter((row)=>row.isActive&&row.configured)" :key="secret.id" :value="secret.id">{{ secret.label || secret.key }} · v{{ secret.currentVersion }}</option></select><small>凭据正文永不进入本表单；这里只保存受管凭据引用（SecretBinding）。</small></label><RegistryResourceEditor v-model="editorPayload" :resource="editorResource" :dependencies="editorDependencies" :recipe-variant-limit="productionCapability?.effectiveLimits.maxVariants" :disabled="saving" @validity="editorValidity=$event"/><div v-if="editorError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ editorError }}</p></div></div>
      <template #footer><button class="button ghost" :disabled="saving" @click="editorOpen=false">取消</button><button class="button primary" :disabled="saving||!editorValidity.valid" @click="submitEditor">{{ saving?'服务端保存中…':editorId?'提交更新':editorIntent==='next-model-version'?'创建下一版本草稿':'提交到服务端' }}</button></template>
    </BaseModal>
    <BaseModal :open="detailOpen" :dismissible="!saving" :title="detailTitle" :eyebrow="`${detailKind} · 服务端事实（Server Facts）`" :width="detailRecipe?'1180px':'760px'" @close="detailOpen=false">
      <div class="registry-detail">
        <WorkflowConfigurationLinks v-if="detailWorkflow" :workflow="detailWorkflow" @navigate="detailOpen=false"/>
        <WorkflowResolutionInspector :contract="detailWorkflowInputContract" :options="workflowProjectorOptions?.imageResolution"/>
        <p>以下内容来自当前服务端记录。配方详情优先展示工作流输入合同、当前可解析的模型与部署、工作流输出，以及阶段声明顺序；某次生产的实际输入值只在对应 ProductionRun 中冻结和审计。</p>
        <ProductionRecipeFlow v-if="detailRecipe" :recipe="detailRecipe" :deployments="deployments" :model-profiles="modelProfiles" :workflows="recipeWorkflowCatalog" :workflow-detail-states="recipeWorkflowDetailStates" :workflow-detail-errors="recipeWorkflowDetailErrors" :recipe-editor-options="recipeEditorOptions" :readiness="productionReadinessByRecipeId.get(detailRecipe.id)||null" :readiness-report-state="productionReadinessReportState" show-raw/>
        <template v-else><div v-if="detailPromptNormalizationSummary" class="platform-alert info"><Gauge :size="15"/><div><strong>提示词标准化容量（Prompt normalization capacity）</strong><p>{{ detailPromptNormalizationSummary }}</p><small>{{ detailModelProfile?.status==='draft'?'草稿可在类型化字段或同一 specification 完整 JSON 中修改；发布后必须新建版本。':'发布态只读；修改容量或请求扩展必须新建更高版本，并对新目标重跑真实烟测（Smoke）。' }}</small></div></div><pre>{{ pretty(detailPayload) }}</pre></template>
      </div>
      <template #footer><button class="button primary" @click="detailOpen=false">关闭</button></template>
    </BaseModal>

    <BaseModal :open="workflowExportOpen" :dismissible="!workflowExportBusyId" title="在 ComfyUI 中打开（下载导入）" eyebrow="只读 · 不向其他页面直接注入" width="720px" @close="closeWorkflowExport">
      <div class="workflow-export-review">
        <div v-if="workflowExportNeedsExplicitDeployment&&!workflowExport" class="workflow-export-deployment"><label>ComfyUI 模型部署（Deployment）*<select v-model="workflowExportDeploymentId" :disabled="Boolean(workflowExportBusyId)" @change="selectWorkflowExportDeployment"><option value="">请选择已启用（active）且绑定兼容的部署</option><option v-for="item in workflowExportDeploymentOptions" :key="item.id" :value="item.id">{{ item.name || item.key }} · {{ item.runtimeHost ? `主机（host）${item.runtimeHost.slice(0,8)}` : '远端（remote）' }}</option></select></label><small v-if="workflowExportDeploymentOptions.length">未校验草稿没有持久化节点目录证据；必须显式选择，系统不会回退默认 ComfyUI。</small><div v-else class="platform-alert danger"><AlertTriangle :size="15"/><p>没有已启用（active）且与该工作流（workflow）的服务提供方（Provider）/模型版本精确兼容的 ComfyUI 部署；请先完成部署配置。</p></div></div>
        <div v-else-if="workflowExportVersion?.validatedDeployment&&!workflowExport" class="platform-alert info"><ShieldCheck :size="15"/><p>该版本已绑定通过校验的部署（validated deployment）<code>{{ workflowExportVersion.validatedDeployment }}</code>；导出只能读取此部署，不能临时切换。</p></div>
        <div v-if="workflowExportBusyId" class="platform-loading"><LoaderCircle class="spin" :size="20"/>正在从绑定的 ComfyUI 部署读取节点目录信息（object_info），并生成可导入的界面画布（UI canvas）…</div>
        <div v-else-if="workflowExportError" class="platform-alert danger"><AlertTriangle :size="15"/><div><strong>无法生成可导入画布</strong><p>{{ workflowExportError }}</p></div></div>
        <template v-else-if="workflowExport">
          <p>{{ workflowExport.openReason }}</p>
          <div class="platform-alert warning"><AlertTriangle :size="15"/><p>浏览器不能把本页数据直接注入另一个 ComfyUI 页面。本操作只下载服务端转换且校验的 UI 画布 JSON，不会提交生产任务。</p></div>
          <dl><div><dt>文件</dt><dd>{{ workflowExport.filename }}</dd></div><div><dt>节点目录部署</dt><dd>{{ workflowExport.deploymentName }} · <code>{{ workflowExport.deploymentId }}</code></dd></div><div><dt>部署来源</dt><dd>{{ workflowExport.deploymentSource==='validated'?'持久化验证证据':'草稿显式选择' }}</dd></div><div><dt>打开方式</dt><dd>下载后导入（download_import）</dd></div><div><dt>直接打开</dt><dd>不支持（机器标志：direct_open_supported=false）</dd></div><div><dt>原图 SHA-256</dt><dd><code>{{ workflowExport.graphSha256 }}</code></dd></div></dl>
          <ol><li>点击“下载 UI 画布 JSON”。</li><li>打开你已配置的 ComfyUI。</li><li>将 JSON 拖入画布，或从工作流（Workflow）菜单打开该文件。</li></ol>
        </template>
      </div>
      <template #footer><button class="button ghost" :disabled="Boolean(workflowExportBusyId)" @click="closeWorkflowExport">关闭</button><button v-if="!workflowExport" class="button primary" :disabled="Boolean(workflowExportBusyId)||(workflowExportNeedsExplicitDeployment&&(!workflowExportDeploymentId||!workflowExportDeploymentOptions.length))" @click="loadWorkflowExport"><RefreshCw :size="13"/>{{ workflowExportBusyId?'读取中…':'读取绑定部署并生成' }}</button><button v-else class="button primary" :disabled="!workflowExport.importReady||Boolean(workflowExportBusyId)" @click="downloadWorkflowCanvas"><Download :size="13"/>下载 UI 画布 JSON</button></template>
    </BaseModal>

    <RegistryDeletionReviewModal
      v-if="actionDeletionResourceType"
      :open="actionOpen"
      :resource-type="actionDeletionResourceType"
      :object-id="actionId"
      :title="actionTitle"
      @close="closeAction()"
      @completed="onRegistryDeletionCompleted"
    />

    <BaseModal :open="actionOpen && !actionIsDeletion" :dismissible="!saving" :title="actionTitle" eyebrow="提交前复核（Review Before Commit）" @close="closeAction()">
<div class="registry-action-review"><p>{{ actionDescription }}</p><dl><div><dt>对象 ID</dt><dd><code>{{ actionId || '新系统修订（new system revision）' }}</code></dd></div><div v-if="actionReferenceId"><dt>证据 / 引用</dt><dd><code>{{ actionReferenceId }}</code></dd></div></dl><pre v-if="actionReviewPayload" class="action-review-json">{{ pretty(actionReviewPayload) }}</pre><label v-if="actionReasonRequired">审计原因代码<select v-model="actionReasonCode"><option value="">请选择稳定原因代码</option><option value="planned_change">计划变更</option><option value="configuration_correction">配置纠正</option><option value="incident_response">故障处置</option><option value="lifecycle_cleanup">生命周期清理</option><option value="validation_completed">验证完成</option></select></label><label v-if="actionKind==='deployment-smoke'">烟测操作（operation）<select v-model="actionOperation" :disabled="saving"><option v-for="operation in actionSmokeOperations" :key="operation" :value="operation">{{ operationLabel(operation) }}</option></select></label><label v-if="actionKind==='deployment-smoke' && actionSmokeNeedsProfile">本次烟测使用的提示词策略<select v-model="actionSmokeBindingId" :disabled="saving"><option value="">请选择策略版本</option><option v-for="item in actionSmokeProfiles" :key="String(item.promptProfile.prompt_profile_binding_id)" :value="String(item.promptProfile.prompt_profile_binding_id)">{{ item.promptProfile.prompt_profile_name }} · v{{ item.promptProfile.prompt_profile_version }} · {{ item.promptProfile.mode || '通用模式' }}</option></select><small>只验收所选策略与当前实例的组合，不会自动测试其他策略。</small><small v-if="!actionSmokeProfiles.length">暂无可选策略，或验收目录未读取成功；请先配置对应能力的已发布策略并重新读取。</small></label><div v-if="actionError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ actionError }}</p></div></div>
      <p v-if="saving && actionKind==='deployment-smoke'" class="platform-alert" role="status">正在准备并创建烟测任务；完成后将显示任务状态，由后台执行。请勿重复提交。</p>
<template #footer><button class="button ghost" :disabled="saving" @click="closeAction()">取消</button><button class="button" :class="destructiveActions.has(actionKind)?'danger':'primary'" :disabled="saving||(actionKind==='deployment-smoke'&&(!actionOperation||!actionSmokeSelectionValid))||(actionReasonRequired&&!actionReasonCode)" @click="runAction">{{ saving?(actionKind==='deployment-smoke'?'正在创建烟测任务…':'服务端处理中…'):actionConfirmLabel }}</button></template>
    </BaseModal>

    <BaseModal :open="Boolean(smokeResolutionDeployment)" :dismissible="!smokeResolutionSaving" title="管理员消歧部署烟测" eyebrow="仅追加的烟测消歧记录（Append-only）" width="700px" @close="closeSmokeResolution">
      <div v-if="smokeResolutionDeployment?.latestSmoke?.unresolvedSubmission" class="registry-action-review">
        <p>请先核对服务提供方（Provider）控制台和账单。提交只追加不可变消歧证据，不会把此结论计为烟测通过（smoke passed）；完成后必须重新执行新的烟测。</p>
        <dl><div><dt>模型部署（Deployment）</dt><dd>{{ smokeResolutionDeployment.name || smokeResolutionDeployment.key }}</dd></div><div><dt>操作能力（Operation）</dt><dd>{{ operationLabel(smokeResolutionDeployment.latestSmoke.unresolvedSubmission.operation) }}</dd></div><div><dt>请求 SHA-256</dt><dd><code>{{ smokeResolutionDeployment.latestSmoke.unresolvedSubmission.requestSha256 }}</code></dd></div><div><dt>目标 SHA-256</dt><dd><code>{{ smokeResolutionDeployment.latestSmoke.unresolvedSubmission.targetSha256 }}</code></dd></div><div><dt>服务商请求 ID（request ID）证据</dt><dd><code>{{ smokeResolutionDeployment.latestSmoke.unresolvedSubmission.providerRequestIdSha256 || '尚无内容指纹（hash）' }}</code></dd></div></dl>
        <label>核验结论<select v-model="smokeResolutionOutcome"><option value="confirmed_not_submitted">确认远端未提交</option><option value="confirmed_remote_submitted">确认远端已提交</option><option value="confirmed_remote_failed">确认远端失败</option></select></label>
        <label>核验说明（{{ productionCapability?.effectiveLimits.auditNoteMinCharacters || '—' }}–{{ productionCapability?.effectiveLimits.auditNoteMaxCharacters || '—' }} 个字符）<textarea v-model="smokeResolutionNote" rows="4" :maxlength="productionCapability?.effectiveLimits.auditNoteMaxCharacters" :disabled="!productionCapability" placeholder="说明查阅的控制台、账单时间范围和可复核结论"></textarea></label>
        <label v-if="smokeResolutionOutcome!=='confirmed_not_submitted'">服务商请求编号（Provider request ID，可选，仅提交、不回显）<input v-model="smokeResolutionProviderRequestId" type="password" maxlength="191" autocomplete="off"/></label>
        <div v-if="smokeResolutionOutcome!=='confirmed_not_submitted'" class="platform-form-grid"><label>实际费用（可选）<input v-model="smokeResolutionActualCost" inputmode="decimal" placeholder="0"/></label><label>币种（费用大于 0 必填）<input v-model="smokeResolutionCurrency" maxlength="12" placeholder="USD"/></label></div>
        <div v-if="smokeResolutionValidation" class="platform-alert warning"><AlertTriangle :size="15"/><p>{{ smokeResolutionValidation }}</p></div>
        <div v-if="smokeResolutionError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ smokeResolutionError }}</p></div>
      </div>
      <template #footer><button class="button ghost" :disabled="smokeResolutionSaving" @click="closeSmokeResolution">取消</button><button class="button primary" :disabled="smokeResolutionSaving||Boolean(smokeResolutionValidation)" @click="submitSmokeResolution">{{ smokeResolutionSaving?'提交中…':'追加消歧记录' }}</button></template>
    </BaseModal>

    <BaseModal :open="decisionOpen" :dismissible="!Boolean(runtimeBusy)" :title="decisionKind==='accept'?'接受推荐':decisionKind==='override'?'覆盖并接受推荐':'拒绝推荐'" eyebrow="运行时推荐决策（Runtime Recommendation）" @close="closeDecision()">
      <div class="registry-action-review"><p>候选：<strong>{{ operationLabel(decisionCandidate?.capabilityKey) }} · {{ decisionCandidate?.deploymentKey }}</strong></p><p>提交前会再次核对当前主机、快照、推荐有效期与候选证据；接受或覆盖只创建草稿，不会自动发布或激活。</p><label v-if="decisionKind==='override'">覆盖配置（configuration JSON）<textarea v-model="decisionOverrideText" rows="8" spellcheck="false"/></label><label>审计说明<textarea v-model="decisionReason" rows="3"/></label><div v-if="decisionError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ decisionError }}</p></div></div>
      <template #footer><button class="button ghost" :disabled="Boolean(runtimeBusy)" @click="closeDecision()">取消</button><button class="button primary" :disabled="Boolean(runtimeBusy)||!decisionReason.trim()" @click="submitDecision">{{ runtimeBusy==='decision'?'服务端处理中…':'确认提交决策' }}</button></template>
    </BaseModal>

    <BaseModal :open="secretOpen" :dismissible="!saving" :title="secretMode==='create'?'新增受管凭据':secretMode==='rotate'?'轮换受管凭据':'变更凭据状态'" eyebrow="超级管理员（Super Admin）· 加密保存" @close="closeSecret()">
      <div class="secret-form"><p v-if="secretTarget">目标：<strong>{{ secretTarget.label || secretTarget.key }}</strong></p><template v-if="secretMode==='create'"><label>唯一键（key）<input v-model="secretDraft.key" autocomplete="off" placeholder="provider.deepseek.primary"/></label><label>显示名称<input v-model="secretDraft.label" autocomplete="off"/></label><label>用途<input v-model="secretDraft.purpose" autocomplete="off"/></label></template><label v-if="secretMode!=='status'">秘密正文<input v-model="secretDraft.secretValue" type="password" autocomplete="new-password"/></label><p v-else>确认后将{{ secretDraft.isActive?'启用':'停用' }}该绑定；停用会让引用它的运行能力在条件不满足时直接阻断（fail closed）。</p><label>审计说明<textarea v-model="secretDraft.reason" rows="3"/></label><div v-if="secretError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ secretError }}</p></div></div>
      <template #footer><button class="button ghost" :disabled="saving" @click="closeSecret()">取消</button><button class="button primary" :disabled="saving||!secretDraft.reason.trim()||(secretMode!=='status'&&!secretDraft.secretValue)||(secretMode==='create'&&(!secretDraft.key||!secretDraft.label))" @click="submitSecret">确认</button></template>
    </BaseModal>
  </div>
</template>

<style scoped>
.workflow-projector-mode-choices{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.workflow-projector-mode-choices button{display:flex;gap:10px;align-items:center;text-align:left;min-width:0;min-height:60px;padding:10px 12px;border:1px solid var(--border-strong);border-radius:6px;color:var(--text);background:var(--surface);cursor:pointer}.workflow-projector-mode-choices button[aria-checked=true]{border-color:var(--accent)}.workflow-projector-mode-choices button:disabled{opacity:.55;cursor:not-allowed}.workflow-projector-mode-choices button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.workflow-projector-mode-choices button>span:last-child{display:grid;gap:4px;min-width:0}.workflow-projector-mode-choices strong{font-size:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}.workflow-projector-mode-choices strong small{font-size:10px;color:var(--text-secondary);font-weight:normal}.workflow-projector-mode-choices strong+span{color:var(--text-secondary);font-size:11px;overflow-wrap:anywhere}.mode-indicator{width:14px;height:14px;border:1px solid var(--border-strong);border-radius:50%;flex-shrink:0}.workflow-projector-mode-choices [aria-checked=true] .mode-indicator{border:4px solid var(--accent)}.projector-scan-return{align-items:center;justify-content:space-between}.projector-scan-return>div{min-width:0}.projector-scan-return button{flex-shrink:0}@media(max-width:640px){.workflow-projector-mode-choices{grid-template-columns:1fr}.projector-scan-return{flex-direction:column;align-items:stretch}.projector-scan-return button{justify-content:center}}
.workflow-projector-modal [aria-pressed="true"]{border-color:#dca14b;background:rgba(228,168,83,.16);color:#f3c681;box-shadow:inset 0 -2px #dca14b}
.workflow-model-context{margin-bottom:16px;grid-template-columns:auto minmax(0,1fr) auto;align-items:start}.workflow-model-context>div{min-width:0;display:grid;gap:5px}.workflow-model-context p{margin:0}.workflow-model-context small{color:#8f9aa8;line-height:1.55;overflow-wrap:anywhere}.workflow-model-context code{overflow-wrap:anywhere}.workflow-model-context>.button{align-self:center}
.saved-workflow-importer{margin-bottom:16px;overflow:visible}.saved-workflow-controls,.saved-workflow-fields,.saved-workflow-footer{display:flex;flex-wrap:wrap;gap:10px;align-items:end;padding:12px}.saved-workflow-scan-config{display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px;margin:0 12px 12px;padding:10px 12px;border:1px solid #252b34;border-radius:8px;background:#0a0d11;color:#8f9aa8;font-size:11px}.saved-workflow-scan-config .platform-row-actions{margin-left:auto}.saved-workflow-scan-config code{color:#d7dee7}.saved-workflow-controls label,.saved-workflow-fields label{display:grid;gap:5px;min-width:220px;flex:1;color:#8f9aa8;font-size:10px}.saved-workflow-controls select,.saved-workflow-fields select,.saved-workflow-fields input{min-height:36px;padding:7px 9px;border:1px solid #2a3039;border-radius:7px;background:#0b0e12;color:#d7dee7}.saved-workflow-list{border-top:1px solid #252b34}.saved-workflow-row{display:grid;gap:10px;padding:13px;border-bottom:1px solid #252b34}.saved-workflow-row>header,.saved-workflow-select{display:flex;gap:10px;align-items:center}.saved-workflow-row>header{justify-content:space-between}.saved-workflow-select span,.saved-workflow-evidence{display:grid;gap:5px}.saved-workflow-select small,.saved-workflow-evidence small{color:#788391}.saved-workflow-evidence{padding:10px 12px;border:1px solid #252b34;border-radius:8px;background:#0a0d11}.saved-workflow-evidence ul,.saved-workflow-results ul{margin:0;padding-left:20px;color:#aeb7c3}.saved-workflow-footer{justify-content:space-between}.saved-workflow-results{display:grid;gap:7px;padding:12px;border-top:1px solid #252b34}
.saved-workflow-context-filter{display:flex;align-items:center;justify-content:space-between;gap:16px;margin:0 12px 12px;padding:10px 12px;border:1px solid rgba(102,185,208,.28);border-radius:8px;background:rgba(102,185,208,.055)}.saved-workflow-context-filter>div{min-width:0;display:grid;gap:4px}.saved-workflow-context-filter strong{color:#b8d6df;font-size:11px}.saved-workflow-context-filter span{color:#879aa6;font-size:10px;line-height:1.55}.saved-workflow-context-filter>.button{flex:0 0 auto}.saved-workflow-context-kind{width:max-content;padding:2px 6px;border:1px solid rgba(102,185,208,.3);border-radius:999px;color:#91c5d4!important}.saved-workflow-row-contract-blocked{background:rgba(217,147,54,.035)}.saved-workflow-row-contract-blocked .saved-workflow-context-kind{border-color:rgba(217,147,54,.45);color:#e0aa61!important}.saved-workflow-row-diagnostic{background:rgba(255,255,255,.012)}.saved-workflow-row-diagnostic .saved-workflow-context-kind{border-color:#39434e;color:#84909c!important}.saved-workflow-row-diagnostic>.platform-alert{opacity:.82}
.saved-workflow-parameters{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px;margin:6px 0}.saved-workflow-parameter{display:grid;gap:5px;padding:9px;border:1px solid #252b34;border-radius:7px;background:#080b0f}.saved-workflow-parameter label{display:grid;gap:5px;color:#b8c2cf;font-size:10px}.saved-workflow-parameter select,.saved-workflow-parameter input{min-height:34px;padding:6px 8px;border:1px solid #2a3039;border-radius:6px;background:#0b0e12;color:#d7dee7}.saved-workflow-controls>span{color:#84909c;font-size:10px}
.saved-workflow-process{
  display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:0 12px 12px;
  overflow:hidden;border:1px solid #2a313a;border-radius:8px;background:#0a0e12;
}
.saved-workflow-process article{
  min-width:0;padding:11px 12px;display:grid;grid-template-columns:auto minmax(0,1fr);
  align-items:center;gap:9px;border-right:1px solid #2a313a;
}
.saved-workflow-process article:last-child{border-right:0}
.saved-workflow-process article>span{
  width:24px;height:24px;display:grid;place-items:center;border:1px solid #39434e;
  border-radius:50%;color:#83909d;font:10px Consolas,monospace;
}
.saved-workflow-process article>div{min-width:0;display:grid;gap:3px}
.saved-workflow-process strong{color:#bbc5cf;font-size:10px}
.saved-workflow-process small{color:#74808c;font-size:9px;line-height:1.45}
.saved-workflow-process article.complete>span{border-color:rgba(104,197,167,.5);color:#83d7bb}
.saved-workflow-process article.current{background:rgba(102,185,208,.055)}
.saved-workflow-process article.current>span{border-color:rgba(102,185,208,.55);color:#91c5d4}
.saved-workflow-process article.blocked{background:rgba(217,147,54,.055)}
.saved-workflow-process article.blocked>span{border-color:rgba(217,147,54,.58);color:#e0aa61}
.saved-workflow-process article.pending{opacity:.62}
.saved-workflow-help{margin:0 12px 12px;border:1px solid #252b34;border-radius:7px;background:#090d11}
.saved-workflow-help>summary,.saved-workflow-technical>summary{
  padding:9px 11px;display:flex;align-items:center;gap:7px;color:#94a1ad;
  font-size:10px;cursor:pointer;list-style:none;
}
.saved-workflow-help>summary::-webkit-details-marker,.saved-workflow-technical>summary::-webkit-details-marker,.saved-workflow-inline-diagnostics>summary::-webkit-details-marker{display:none}
.saved-workflow-help>p{margin:0;padding:0 11px 10px;color:#798590;font-size:10px;line-height:1.6}
.saved-workflow-stepper{
  display:grid;grid-template-columns:repeat(3,minmax(0,1fr));overflow:hidden;
  border:1px solid #29313a;border-radius:7px;background:#090d11;
}
.saved-workflow-stepper section{
  min-width:0;padding:9px 10px;display:grid;grid-template-columns:auto minmax(0,1fr);
  align-items:center;gap:8px;border-right:1px solid #29313a;
}
.saved-workflow-stepper section:last-child{border-right:0}
.saved-workflow-stepper section>span{
  width:21px;height:21px;display:grid;place-items:center;border:1px solid #39434e;
  border-radius:50%;color:#83909d;font:9px Consolas,monospace;
}
.saved-workflow-stepper section>div{min-width:0;display:grid;gap:3px}
.saved-workflow-stepper strong{color:#bbc5cf;font-size:10px}
.saved-workflow-stepper small{color:#74808c;font-size:9px;line-height:1.45}
.saved-workflow-stepper section.complete>span{border-color:rgba(104,197,167,.5);color:#83d7bb}
.saved-workflow-stepper section.current{background:rgba(102,185,208,.055)}
.saved-workflow-stepper section.current>span{border-color:rgba(102,185,208,.55);color:#91c5d4}
.saved-workflow-stepper section.blocked{background:rgba(217,147,54,.055)}
.saved-workflow-stepper section.blocked>span{border-color:rgba(217,147,54,.58);color:#e0aa61}
.saved-workflow-stepper section.pending{opacity:.62}
.saved-workflow-row{outline:2px solid transparent;outline-offset:-2px;transition:background .18s ease,outline-color .18s ease}
.saved-workflow-row-focused{outline-color:rgba(230,167,90,.76);background:rgba(230,167,90,.055)}
.saved-workflow-match-summary{align-items:flex-start}
.saved-workflow-match-summary>div{min-width:0;display:grid;gap:5px}
.saved-workflow-match-summary p{margin:0;color:#a7b1bc;line-height:1.55}
.saved-workflow-match-summary small{color:#d0a86f}
.saved-workflow-projector-refinement .platform-row-actions{justify-content:flex-start;margin-top:8px}
.saved-workflow-inline-diagnostics{margin-top:4px;padding-top:6px;border-top:1px solid rgba(217,147,54,.2)}
.saved-workflow-inline-diagnostics>summary{color:#9d8a70;font-size:9px;cursor:pointer}
.saved-workflow-inline-diagnostics code,.saved-workflow-inline-diagnostics small{display:block;margin-top:5px;overflow-wrap:anywhere;color:#81786e;font-size:9px}
.saved-workflow-contract-outcome{
  margin:0 12px;overflow:hidden;border:1px solid #2b333c;border-radius:8px;background:#090d11;
}
.saved-workflow-contract-outcome>header{
  padding:11px 12px;display:flex;align-items:center;justify-content:space-between;gap:12px;
}
.saved-workflow-contract-outcome>header>div{min-width:0;display:grid;gap:4px}
.saved-workflow-contract-outcome>header strong{color:#cbd4dc;font-size:11px}
.saved-workflow-contract-outcome>header small{color:#82909c;font-size:9px}
.saved-workflow-contract-outcome.blocked{border-color:rgba(217,147,54,.4);background:rgba(217,147,54,.035)}
.saved-workflow-contract-outcome.blocked>header strong{color:#e1b273}
.saved-workflow-contract-outcome.ready{border-color:rgba(104,197,167,.32);background:rgba(104,197,167,.025)}
.saved-workflow-blocker-list{display:grid;gap:8px;padding:0 12px 12px}
.saved-workflow-blocker-list>article{
  padding:10px;display:grid;grid-template-columns:auto minmax(0,1fr);gap:9px;
  border:1px solid rgba(217,147,54,.22);border-radius:7px;background:#0b0e12;
}
.saved-workflow-blocker-list>article>svg{margin-top:2px;color:#dda85f}
.saved-workflow-blocker-list>article>div{min-width:0;display:grid;gap:5px}
.saved-workflow-blocker-list strong{color:#d7c2a6;font-size:10px}
.saved-workflow-blocker-list p{margin:0;color:#a7a29a;font-size:10px;line-height:1.55}
.saved-workflow-blocker-list small{color:#e0ad68;font-size:10px;line-height:1.55}
.saved-workflow-blocker-list code{width:max-content;max-width:100%;overflow-wrap:anywhere;color:#736c64;font-size:8px}
.saved-workflow-blocker-list>.button{justify-self:start}
.saved-workflow-technical{margin:0 12px;border:1px solid #252b34;border-radius:8px;background:#080c10}
.saved-workflow-technical>summary{justify-content:space-between;color:#87939f}
.saved-workflow-technical[open]>summary{border-bottom:1px solid #252b34}
.saved-workflow-technical .saved-workflow-evidence{border:0;border-radius:0;background:#080c10}
.saved-workflow-footer{align-items:center;border-top:1px solid #2a313a;background:#0a0e12;position:sticky;top:0;z-index:3}
.saved-workflow-footer>div{min-width:0;display:grid;gap:4px}
.saved-workflow-footer>div strong{color:#c9d2db;font-size:11px}
.saved-workflow-footer>div span{color:#7e8994;font-size:9px}
@media(max-width:900px){.workflow-model-context{grid-template-columns:auto minmax(0,1fr)}.workflow-model-context>.button{grid-column:1/-1;width:100%;justify-content:center}.saved-workflow-controls,.saved-workflow-fields,.saved-workflow-footer,.saved-workflow-row>header,.saved-workflow-scan-config,.saved-workflow-context-filter{align-items:stretch;flex-direction:column}.saved-workflow-scan-config .platform-row-actions{margin-left:0}.saved-workflow-row .platform-row-actions{align-items:stretch;flex-direction:column;flex-wrap:wrap}.saved-workflow-controls .button,.saved-workflow-footer .button,.saved-workflow-row .platform-row-actions .button,.saved-workflow-scan-config .button,.saved-workflow-context-filter>.button{width:100%;justify-content:center}.saved-workflow-select{min-width:0;align-items:flex-start}.saved-workflow-select span,.saved-workflow-select strong,.saved-workflow-select small,.saved-workflow-evidence small{min-width:0;overflow-wrap:anywhere}.saved-workflow-fields label{min-width:0;width:100%}}
@media(max-width:900px){
  .saved-workflow-process,.saved-workflow-stepper{grid-template-columns:1fr}
  .saved-workflow-process article,.saved-workflow-stepper section{border-right:0;border-bottom:1px solid #29313a}
  .saved-workflow-process article:last-child,.saved-workflow-stepper section:last-child{border-bottom:0}
  .saved-workflow-contract-outcome,.saved-workflow-technical{margin:0}
  .saved-workflow-footer{position:static}
}
.registry-title-link{display:block;margin:0;padding:0;border:0;background:transparent;color:inherit;text-align:left;cursor:pointer}.registry-title-link:hover strong,.registry-title-link:focus-visible strong{color:#f4b04d;text-decoration:underline;text-underline-offset:3px}.registry-title-link:focus-visible{outline:2px solid #d99336;outline-offset:3px;border-radius:3px}
.model-registry-toolbar{border-color:rgba(230,167,90,.2);background:linear-gradient(135deg,rgba(230,167,90,.055),#0c0f13 42%)}
.model-provider-management{overflow:hidden;border:1px solid var(--border);border-radius:8px}.model-section-heading{min-height:48px;padding:9px 12px;display:flex;align-items:center;justify-content:space-between;gap:12px;border-bottom:1px solid var(--border);background:rgba(255,255,255,.018)}.model-section-heading p{margin:0;color:#96a1ad;font-size:11px;line-height:1.55}.model-section-heading .button{flex:0 0 auto}
.model-registry-errors{display:grid;border:1px solid rgba(239,115,123,.22);border-radius:8px;overflow:hidden}.model-registry-errors .platform-inline-error{margin:0;border-bottom:1px solid rgba(239,115,123,.14)}.model-registry-errors .platform-inline-error:last-child{border-bottom:0}
.model-registry-workbench{height:clamp(540px,calc(100vh - 330px),760px);min-height:0;display:grid;grid-template-columns:minmax(280px,32%) minmax(0,1fr);overflow:hidden;border:1px solid #27303a;border-radius:8px;background:#090c10}.model-family-browser{min-width:0;min-height:0;display:flex;flex-direction:column;border-right:1px solid #27303a;background:#0b0f14}.model-family-browser>header{min-height:64px;padding:12px 14px;display:flex;align-items:center;justify-content:space-between;gap:12px;border-bottom:1px solid #27303a}.model-family-browser>header>span{min-width:0;display:grid;gap:4px}.model-family-browser>header strong{color:var(--text);font-size:13px}.model-family-browser>header small{overflow:hidden;color:var(--text-secondary);font-size:11px;text-overflow:ellipsis;white-space:nowrap}.model-family-browser>header>b{flex:0 0 auto;color:#aeb9c5;font:11px Consolas,monospace}.model-family-browser-list{min-height:0;display:grid;align-content:start;gap:7px;padding:8px;overflow:auto}.model-family-browser-list>button{min-width:0;min-height:76px;padding:10px 11px;display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:10px;border:1px solid transparent;border-left:3px solid transparent;border-radius:7px;background:#0e1319;color:inherit;text-align:left;cursor:pointer}.model-family-browser-list>button:hover{border-color:#33404c;background:#121922}.model-family-browser-list>button.active{border-color:rgba(230,167,90,.38);border-left-color:var(--accent);background:rgba(230,167,90,.085);box-shadow:inset 0 0 0 1px rgba(230,167,90,.04)}.model-family-browser-list>button:focus-visible,.model-version-tabs>button:focus-visible,.model-section-tabs>button:focus-visible{outline:2px solid rgba(230,167,90,.72);outline-offset:-2px}.model-family-browser-identity{min-width:0;display:flex;align-items:flex-start;gap:9px}.model-family-browser-identity>svg{flex:0 0 auto;margin-top:2px;color:#7f91a3}.model-family-browser-list>button.active .model-family-browser-identity>svg{color:var(--accent-bright)}.model-family-browser-identity>span{min-width:0;display:grid;gap:3px}.model-family-browser-identity strong,.model-family-browser-identity small,.model-family-browser-identity em{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.model-family-browser-identity strong{color:var(--text);font-size:12px}.model-family-browser-identity small{color:var(--text-secondary);font-size:11px}.model-family-browser-identity em{color:#8392a0;font-size:10px;font-style:normal}.model-family-browser-status{min-width:92px;display:grid;justify-items:end;gap:7px}.model-family-browser-status>span{display:flex;justify-content:flex-end;flex-wrap:wrap;gap:4px}.model-family-browser-status em{padding:2px 5px;border:1px solid #34404b;border-radius:4px;color:#9aa7b4;font-size:10px;font-style:normal}.model-family-browser-status em.published{border-color:rgba(104,197,167,.34);color:#83d7bb}.model-family-browser-status em.draft{border-color:rgba(230,167,90,.34);color:#efbd7d}.model-family-browser-status em.error{border-color:rgba(239,115,123,.42);color:#f18b92}.model-family-browser-status>small{color:#84919e;font-size:10px;white-space:nowrap}.model-browser-empty{min-height:120px}
.model-family-inspector{min-width:0;min-height:0;display:flex;flex-direction:column;overflow:hidden;background:#090c10}.model-inspector-header{min-height:72px;padding:12px 14px;display:flex;align-items:center;justify-content:space-between;gap:16px;border-bottom:1px solid #27303a}.model-inspector-header>div{min-width:0;display:grid;gap:3px}.model-inspector-header .eyebrow{color:#b98951;font-size:10px}.model-inspector-header strong{overflow:hidden;color:#f0f3f6;font-size:14px;text-overflow:ellipsis;white-space:nowrap}.model-inspector-header small{overflow:hidden;color:#91a0ae;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.model-family-overview{min-height:42px;padding:7px 12px;display:flex;align-items:center;flex-wrap:wrap;gap:6px;border-bottom:1px solid #27303a;background:#0b1015}.model-family-overview>span{padding:3px 7px;border:1px solid #34404b;border-radius:4px;color:#9ba7b3;font:10px Consolas,monospace}.model-family-overview>span.draft{border-color:rgba(230,167,90,.32);color:#efbd7d}.model-family-overview>span.published,.model-family-overview>span.active{border-color:rgba(104,197,167,.32);color:#83d7bb}.model-family-overview>span.error{border-color:rgba(239,115,123,.38);color:#f18b92}.model-family-overview>span.retired{color:#7f8b96}
.model-version-tabs{min-height:62px;display:flex;gap:6px;padding:7px 10px;overflow-x:auto;border-bottom:1px solid #27303a;background:#0c1117}.model-version-tabs>button{min-width:155px;min-height:46px;padding:7px 9px;display:flex;align-items:center;justify-content:space-between;gap:8px;border:1px solid #2b3540;border-radius:6px;background:#0e141b;color:inherit;text-align:left;cursor:pointer}.model-version-tabs>button:hover{border-color:#40505f}.model-version-tabs>button.active{border-color:rgba(102,185,208,.55);background:rgba(102,185,208,.08)}.model-version-tabs>button>span{min-width:0;display:grid;gap:3px}.model-version-tabs strong{color:#dfe8f0;font-size:12px}.model-version-tabs small{max-width:125px;overflow:hidden;color:#8292a1;font-size:10px;text-overflow:ellipsis;white-space:nowrap}
.model-version-card{position:relative;margin:0;border:0;border-radius:0;background:#090c10;overflow:hidden}.model-version-detail{min-height:0;flex:1;display:flex;flex-direction:column}.model-version-card>header{min-height:66px;padding:10px 12px;display:grid;grid-template-columns:minmax(0,1fr) auto auto;align-items:center;gap:12px;border-bottom:1px solid #27303a}.model-version-identity{min-width:0;display:grid;gap:5px}.model-version-identity strong,.model-version-identity small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.model-version-identity strong{color:#e1e8ef;font-size:12px}.model-version-identity small{color:#8d9aa7;font-size:10px}.model-lifecycle-fact,.model-instance-statuses>span{display:grid;gap:5px;justify-items:start}.model-lifecycle-fact>small,.model-instance-statuses>span>small{color:#8794a1;font-size:10px}.model-instance-statuses>span>em{color:#748493;font-size:9px;font-style:normal;white-space:nowrap}.model-version-actions{flex-wrap:wrap}.model-version-sections{min-height:0;flex:1;display:flex;flex-direction:column}.model-section-tabs{min-height:54px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border-bottom:1px solid #27303a;background:#0b1016}.model-section-tabs>button{min-width:0;padding:8px 12px;display:grid;gap:4px;border-right:1px solid #27303a;border-bottom:2px solid transparent;background:transparent;color:#99a6b2;text-align:left;cursor:pointer}.model-section-tabs>button:last-child{border-right:0}.model-section-tabs>button:hover{background:#10161d;color:#d4dde5}.model-section-tabs>button.active{border-bottom-color:var(--accent);background:rgba(230,167,90,.055);color:#f0d1a7}.model-section-tabs span{font-size:11px;font-weight:600}.model-section-tabs b{overflow:hidden;color:#7f8d9a;font:10px Consolas,monospace;text-overflow:ellipsis;white-space:nowrap}.model-section-tabs>button.active b{color:#bda787}.model-section-panel{min-height:0;flex:1;overflow:auto}.model-section-panel>header{min-height:50px;padding:9px 12px;display:flex;align-items:center;justify-content:space-between;gap:12px;border-bottom:1px solid #27303a;background:#0b0f14}.model-section-panel>header>span{min-width:0;display:grid;gap:3px}.model-section-panel>header strong{color:#dce4eb;font-size:12px}.model-section-panel>header small{color:#8795a2;font-size:10px}.model-capability-panel{padding:14px;align-content:start}.model-capability-panel>header{margin:-14px -14px 14px}.model-capability-panel>p{margin:12px 0 0;color:#9aa7b4;font-size:11px;line-height:1.65}.model-artifact-panel,.model-deployment-panel{padding:0}.model-information-artifact-list{max-height:none;overflow:visible}.model-compact-empty{padding:14px!important;text-align:left!important}
.model-nested-list article{padding:11px 12px;background:#090d12}.model-nested-list article>div:first-child strong{color:#d8e1e9;font-size:11px}.model-nested-list article>div:first-child small{color:#8997a4;font-size:10px;line-height:1.55;white-space:normal}.model-nested-list article>div:first-child small.model-workflow-smoke-summary{color:#8fb9c8}.model-instance-list article{grid-template-columns:minmax(260px,1fr) auto minmax(270px,max-content)}.model-instance-list article.model-deployment-deep-focus{outline:2px solid rgba(230,167,90,.62);outline-offset:-2px;background:rgba(230,167,90,.055)}.model-instance-statuses{min-width:190px!important;display:grid!important;grid-template-columns:repeat(2,minmax(84px,1fr));gap:8px!important}.model-instance-list article>.model-instance-actions{min-width:0;display:flex;flex-direction:row;flex-wrap:wrap;align-items:center;justify-content:flex-end;gap:6px}.model-instance-actions>.button,.model-instance-actions>.model-more-menu>summary{white-space:nowrap}.model-more-menu{position:relative}.model-more-menu>summary{list-style:none;cursor:pointer}.model-more-menu>summary::-webkit-details-marker{display:none}.model-more-menu>div{position:absolute;z-index:30;top:calc(100% + 5px);right:0;min-width:178px;padding:6px;display:grid;gap:4px;border:1px solid #35414d;border-radius:7px;background:#0d1218;box-shadow:0 14px 32px rgba(0,0,0,.42)}.model-more-menu>div .button{width:100%;justify-content:flex-start;white-space:nowrap}.model-registry-empty{border:1px dashed var(--border);border-radius:8px;background:#0a0d11}.model-inspector-state{min-height:260px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:9px;padding:24px;color:#7f8d99;text-align:center}.model-inspector-state h3{margin:0;color:#c7d0d9;font-size:14px}.model-inspector-state p{margin:0;max-width:420px;color:#8e9aa6;font-size:11px;line-height:1.6}
@media(max-width:1200px){.model-registry-workbench{grid-template-columns:minmax(250px,30%) minmax(0,1fr)}.model-instance-list article{grid-template-columns:minmax(0,1fr) auto}.model-instance-list article>.model-instance-actions{grid-column:1/-1;justify-content:flex-start}.model-information-artifact-list article>.platform-row-actions{display:flex;flex-direction:row;flex-wrap:wrap;justify-content:flex-start}}
@media(max-width:980px){.model-registry-workbench{height:auto;max-height:none;grid-template-columns:1fr;grid-template-rows:minmax(210px,32vh) minmax(560px,72vh)}.model-family-browser{border-right:0;border-bottom:1px solid #27303a}.model-version-card>header{grid-template-columns:minmax(0,1fr) auto}.model-version-actions{grid-column:1/-1;justify-content:flex-start}}
@media(max-width:720px){.model-section-heading{align-items:stretch;flex-direction:column}.model-section-heading .button{align-self:flex-start}.model-registry-workbench{grid-template-rows:minmax(220px,38vh) minmax(620px,78vh)}.model-family-browser-list>button{grid-template-columns:minmax(0,1fr)}.model-family-browser-status{width:100%;justify-items:start}.model-family-browser-status>span{justify-content:flex-start}.model-inspector-header{align-items:stretch;flex-direction:column}.model-inspector-header .button{align-self:flex-start}.model-family-overview{overflow-x:auto;flex-wrap:nowrap}.model-family-overview>span{flex:0 0 auto}.model-version-card>header{grid-template-columns:1fr}.model-version-actions{grid-column:1}.model-section-tabs{display:flex;overflow-x:auto}.model-section-tabs>button{min-width:150px;border-right:1px solid #27303a}.model-nested-list article,.model-instance-list article{grid-template-columns:1fr}.model-instance-statuses{min-width:0!important}.model-instance-list article>.model-instance-actions{grid-column:1;justify-content:flex-start}.model-more-menu>div{position:static;margin-top:5px}.model-version-identity strong,.model-version-identity small{white-space:normal}}
.production-readiness-overview{margin-top:0}.production-readiness-metrics,.runtime-overview-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))}.production-readiness-metrics article,.runtime-overview-metrics article{min-width:0;padding:12px;border-right:1px solid #252b34;border-bottom:1px solid #252b34}.production-readiness-metrics article:last-child,.runtime-overview-metrics article:last-child{border-right:0}.production-readiness-metrics span,.runtime-overview-metrics span{display:block;color:#788391;font-size:9px}.production-readiness-metrics strong,.runtime-overview-metrics strong{display:block;margin-top:5px;overflow:hidden;color:#c3cbd4;font-size:13px;text-overflow:ellipsis;white-space:nowrap}.runtime-overview-metrics small{display:block;margin-top:4px;overflow:hidden;color:#69737f;font-size:8px;text-overflow:ellipsis;white-space:nowrap}.production-readiness-scope{margin:0;padding:10px 12px;color:#7f8995;font-size:9px;line-height:1.6}.production-readiness-reasons{border-top:1px solid #252b34}.production-readiness-reasons>summary{padding:10px 12px;display:flex;align-items:center;justify-content:space-between;color:#aeb8c4;font-size:9px;cursor:pointer;list-style:none}.production-readiness-reasons>summary::-webkit-details-marker{display:none}.production-readiness-reasons[open]>summary svg{transform:rotate(180deg)}.production-readiness-reasons .platform-dense-list{border-top:1px solid #252b34}.production-readiness-reasons b{color:#8c96a2;font-size:9px;font-weight:500;white-space:nowrap}
.production-readiness-unavailable{margin-left:auto;color:#7d8792;font-size:9px}.recipe-summary-readiness{flex:0 0 auto!important;display:flex!important;align-items:center;gap:5px;color:#707b86!important}.recipe-summary-readiness>small{font-size:8px}.recipe-summary-readiness>em{padding:3px 7px;border:1px solid #343c46;border-radius:999px;color:#88939d;font:8px Consolas,monospace;font-style:normal}.recipe-readiness-filter{align-items:center;justify-content:space-between}.recipe-readiness-filter code{color:#7e8994}
.runtime-overview>header>div{min-width:0;flex:1}.runtime-overview>header small{white-space:normal;line-height:1.5}.runtime-readiness-overview{padding:12px}.runtime-overview-empty{border-top:1px solid #252b34}.runtime-overview+.settings-list-filter{margin-top:0}
.runtime-probe-attempt{margin-bottom:14px}.runtime-probe-attempt p{margin:8px 0 0;color:#9ca8b7}.runtime-model-facts{margin-top:12px;border-top:1px solid #252b34;padding-top:10px}.runtime-model-facts summary{cursor:pointer;color:#cbd5e1;font-size:11px}.runtime-model-facts article{display:grid;gap:8px;padding:10px 0;border-bottom:1px solid #202630}.runtime-model-facts article>div{display:grid;gap:3px}.runtime-model-facts small{color:#7f8a98}.runtime-model-facts dl{display:grid;gap:7px;margin:0}.runtime-model-facts dl div{display:grid;grid-template-columns:145px minmax(0,1fr);gap:8px;align-items:start}.runtime-model-facts dt{color:#788391;font-size:10px}.runtime-model-facts dd{display:flex;flex-wrap:wrap;gap:7px;align-items:center;min-width:0;margin:0}.runtime-model-facts code{max-width:100%;overflow-wrap:anywhere;user-select:all;color:#dbe5ef;font-size:10px}
@media(max-width:980px){.production-readiness-metrics,.runtime-overview-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.production-readiness-metrics article:nth-child(2n),.runtime-overview-metrics article:nth-child(2n){border-right:0}}
@media(max-width:560px){.production-readiness-metrics,.runtime-overview-metrics{grid-template-columns:1fr}.production-readiness-metrics article,.runtime-overview-metrics article{border-right:0}.production-readiness-reasons .platform-dense-list article{grid-template-columns:minmax(0,1fr) auto}.production-readiness-reasons .button{grid-column:1/-1;justify-self:start}}
.registry-editor-modal{display:grid;gap:12px}.registry-editor-modal>p{margin:0;color:#99a4b1;line-height:1.55}.registry-editor-modal .editor-credential small{color:#74808d;font-size:10px}
.workflow-projector-guide{margin:0 12px 12px;overflow:hidden;border:1px solid rgba(96,165,250,.18);border-radius:9px;background:#090e13}.workflow-projector-guide>header{padding:13px 14px;display:flex;align-items:flex-start;justify-content:space-between;gap:16px;border-bottom:1px solid #26313b;background:linear-gradient(135deg,rgba(59,130,246,.07),transparent 62%)}.workflow-projector-guide>header>div{min-width:0;display:grid;gap:5px}.workflow-projector-guide>header strong{color:#d9e5ee;font-size:12px}.workflow-projector-guide>header p,.workflow-projector-guide article p,.workflow-projector-guide-note{margin:0;color:#8796a3;font-size:10px;line-height:1.6}.workflow-projector-guide>header svg{flex:0 0 auto;color:#7fc4af}.workflow-projector-guide-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-bottom:1px solid #26313b}.workflow-projector-guide-grid article{min-width:0;padding:12px;border-right:1px solid #26313b}.workflow-projector-guide-grid article:last-child{border-right:0}.workflow-projector-guide-grid article span{display:block;color:#6f8292;font-size:8px;text-transform:uppercase;letter-spacing:.07em}.workflow-projector-guide-grid article strong{display:block;margin:5px 0;color:#b9c9d6;font-size:10px}.workflow-projector-flow{padding:10px 12px;display:flex;align-items:center;flex-wrap:wrap;gap:7px;border-bottom:1px solid #26313b;background:#0b1117}.workflow-projector-flow span{padding:5px 7px;border:1px solid #2c3945;border-radius:5px;color:#9bb0bf;font-size:9px}.workflow-projector-flow b{color:#687886;font-size:10px}.workflow-projector-guide-note{padding:10px 12px;color:#8293a0}
.workflow-projector-modal{display:grid;gap:12px;min-width:0}.workflow-projector-intro{padding:13px 14px;display:flex;align-items:center;justify-content:space-between;gap:16px;border:1px solid rgba(228,168,83,.22);border-radius:9px;background:linear-gradient(135deg,rgba(228,168,83,.07),#0b0f14 60%)}.workflow-projector-intro>div{min-width:0;display:grid;gap:4px}.workflow-projector-intro strong{color:#eadfcf;font-size:12px}.workflow-projector-intro p{margin:0;color:#909aa5;font-size:10px;line-height:1.55}.workflow-projector-intro>span{flex:0 0 auto;padding:5px 8px;border:1px solid rgba(228,168,83,.34);border-radius:999px;color:#e4ad65;font:9px Consolas,monospace}.workflow-projector-form-section{min-width:0;display:grid;gap:12px;padding:13px;border:1px solid #28323c;border-radius:9px;background:#0a0e13}.workflow-projector-form-section>header{display:flex;align-items:flex-start;gap:9px}.workflow-projector-form-section>header>span{flex:0 0 auto;width:23px;height:23px;display:grid;place-items:center;border:1px solid #3a4651;border-radius:6px;color:#aebbc6;font:10px Consolas,monospace}.workflow-projector-form-section>header>div{min-width:0;display:grid;gap:3px}.workflow-projector-form-section>header strong{color:#d9e2ea;font-size:11px}.workflow-projector-form-section>header small{color:#7f8d99;font-size:9px;line-height:1.5}.workflow-projector-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.workflow-projector-form-grid label,.workflow-projector-seed-field,.workflow-projector-output-field{min-width:0;display:grid;gap:6px}.workflow-projector-form-grid label.wide{grid-column:1/-1}.workflow-projector-form-grid label>span,.workflow-projector-seed-field>span,.workflow-projector-output-field>span{color:#aeb9c4;font-size:10px}.workflow-projector-form-grid input,.workflow-projector-form-grid textarea,.workflow-projector-seed-field select,.workflow-projector-output-field select{min-width:0;width:100%;box-sizing:border-box;padding:9px 10px;border:1px solid #303a45;border-radius:7px;background:#080c10;color:#dbe3ea}.workflow-projector-form-grid small,.workflow-projector-seed-field small,.workflow-projector-output-field small{color:#71808d;font-size:9px;line-height:1.5}.workflow-projector-contract-facts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:0}.workflow-projector-contract-facts>div{min-width:0;padding:9px 10px;border:1px solid #29343e;border-radius:7px;background:#080d11}.workflow-projector-contract-facts dt{color:#71808d;font-size:8px;text-transform:uppercase;letter-spacing:.06em}.workflow-projector-contract-facts dd{min-width:0;margin:5px 0 0;display:grid;gap:4px;overflow-wrap:anywhere;color:#becbd5;font-size:10px}.workflow-projector-contract-facts code{color:#8497a6;font-size:8px;line-height:1.45}.workflow-projector-output-field{max-width:420px}.workflow-projector-form-section.definition{padding:0;overflow:hidden}.workflow-projector-form-section.definition>header{padding:13px 13px 0}
@media(max-width:1000px){.workflow-projector-guide-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.workflow-projector-guide-grid article:nth-child(2){border-right:0}.workflow-projector-guide-grid article:nth-child(-n+2){border-bottom:1px solid #26313b}.workflow-projector-contract-facts{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:640px){.workflow-projector-guide-grid,.workflow-projector-form-grid,.workflow-projector-contract-facts{grid-template-columns:1fr}.workflow-projector-guide-grid article{border-right:0;border-bottom:1px solid #26313b}.workflow-projector-guide-grid article:last-child{border-bottom:0}.workflow-projector-intro{align-items:flex-start;flex-direction:column}.workflow-projector-form-grid label.wide{grid-column:1}.workflow-projector-flow b{display:none}.workflow-projector-flow span{width:100%}}
.workflow-export-review{display:grid;gap:12px}.workflow-export-review>p{margin:0;color:#aeb7c3;line-height:1.6}.workflow-export-review dl{display:grid;gap:7px;margin:0}.workflow-export-review dl div{display:grid;grid-template-columns:120px minmax(0,1fr);gap:10px}.workflow-export-review dt{color:#788391}.workflow-export-review dd{min-width:0;margin:0;overflow-wrap:anywhere;color:#c8d1db}.workflow-export-review ol{margin:0;padding-left:22px;color:#aeb7c3;line-height:1.8}
.workflow-export-deployment{display:grid;gap:8px;padding:12px;border:1px solid var(--border);border-radius:8px;background:#0b0e12}.workflow-export-deployment label{display:grid;gap:6px;color:#aeb7c3;font-size:10px}.workflow-export-deployment select{width:100%;min-height:38px;padding:8px 10px;border:1px solid var(--border);border-radius:7px;background:#070a0e;color:#c8d1db}.workflow-export-deployment>small{color:#788391;line-height:1.55}
.lifecycle-audit-filters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding:10px 12px;border-bottom:1px solid #252b34}.lifecycle-audit-filters label{display:grid;gap:5px;color:#788391;font-size:9px}.lifecycle-audit-filters select{min-width:0;padding:7px 9px;border:1px solid #2a3039;border-radius:7px;background:#0b0e12;color:#cbd5e1}.lifecycle-audit-card .platform-dense-list{max-height:420px;overflow:auto}
.secret-form,.registry-action-review{display:grid;gap:15px}.secret-form label,.editor-credential,.registry-action-review label{display:grid;gap:7px;color:#aeb7c3}.secret-form input,.secret-form textarea,.editor-credential select,.registry-action-review textarea,.registry-action-review select{width:100%;border:1px solid #2a3039;border-radius:8px;background:#0b0e12;color:#f7f8fa;padding:10px 12px}.secret-form p,.registry-action-review p,.registry-detail p{color:#aeb7c3}.registry-detail pre,.action-review-json{max-height:min(65vh,680px);overflow:auto;margin:0;padding:14px;border:1px solid #252b34;border-radius:9px;background:#090c10;color:#cbd5e1;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;word-break:break-word}.action-review-json{max-height:320px}.registry-action-review dl{display:grid;gap:8px;margin:0}.registry-action-review dl div{display:grid;grid-template-columns:100px 1fr;gap:12px}.registry-action-review dt{color:#788391}.registry-action-review dd{margin:0;overflow-wrap:anywhere}
</style>
