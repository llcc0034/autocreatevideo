<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AlertTriangle, ArrowLeft, Check, Download, FileCheck2, Layers3, LoaderCircle, Play, RefreshCw, Send, X } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import WorkflowExecutionInputEditor from '@/components/WorkflowExecutionInputEditor.vue'
import WorkflowResolutionInspector from '@/components/WorkflowResolutionInspector.vue'
import WorkflowConfigurationLinks from '@/components/WorkflowConfigurationLinks.vue'
import { relationOverviewUrl } from '@/services/registryRelations'
import {
  apiErrorPresentation,
  apiErrorSummary,
  type ApiErrorPresentation,
  type ApiErrorRecoveryAction,
} from '@/services/apiErrorPresentation'
import { platformApi } from '@/services/platformApi'
import { repository, type ProductionCapability } from '@/services/repository'
import { useAuthStore } from '@/stores/auth'
import type {
  AcceptanceSmokeWorkflowRow,
  ComfyWorkflowParameterScalar,
  JsonObject,
  ModelDeployment,
  ModelProfileVersion,
  ProviderProfile,
  WorkflowSmokeAssetCandidate,
  WorkflowSmokeAttempt,
  WorkflowSmokeDynamicReferencesSchema,
  WorkflowSmokeResolutionOutcome,
  WorkflowTemplateVersion,
} from '@/services/platformTypes'
import {
  buildWorkflowExecutionInput,
  createWorkflowExecutionInputDraft,
  validateWorkflowExecutionInputDraft,
  type WorkflowExecutionInputDraft,
} from '@/services/workflowExecutionInputs'
import {
  workflowAcceptanceEvidenceState,
  workflowAcceptancePhase,
  type WorkflowAcceptanceEvidenceState,
} from '@/services/workflowAcceptance'

type SmokePictureCategory = 'character' | 'scene' | 'prop' | 'continuity_frame'
interface SmokePictureReferenceDraft { key: string; assetId: string; category: SmokePictureCategory }
interface SmokeVideoReferenceDraft { key: string; assetId: string; purpose: string; useEmbeddedAudio: boolean; audioAssetId: string; audioPurpose: string }
interface SmokeAudioReferenceDraft { key: string; assetId: string; purpose: string }

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const workflowId = computed(() => String(route.params.workflowVersionId || ''))
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const loading = ref(false)
const busy = ref('')
const error = ref('')
const errorDetails = ref<ApiErrorPresentation | null>(null)
const notice = ref('')
const savedVersion = ref<WorkflowTemplateVersion | null>(null)
const deployments = ref<ModelDeployment[]>([])
const providers = ref<ProviderProfile[]>([])
const modelProfiles = ref<ModelProfileVersion[]>([])
const deploymentId = ref('')
const productionCapability = ref<ProductionCapability | null>(null)
const productionCapabilityError = ref('')
const contractResult = ref<JsonObject | null>(null)
const smokeResult = ref<JsonObject | null>(null)
const validationRequestFailed = ref(false)
const smokeRequestFailed = ref(false)
const evidenceRefreshError = ref('')
const publishOpen = ref(false)
const acceptanceFreshness = ref<AcceptanceSmokeWorkflowRow | null>(null)
const acceptanceFreshnessLoading = ref(false)
const acceptanceFreshnessError = ref('')

const smokeAttempt = ref<WorkflowSmokeAttempt | null>(null)
const smokeAttemptError = ref('')
const smokePollInFlight = ref(false)
let smokePollTimer: number | undefined
const smokeResolutionOutcome = ref<WorkflowSmokeResolutionOutcome>('confirmed_remote_failed')
const smokeResolutionNote = ref('')
const smokeResolutionProviderRequestId = ref('')
const smokeAssetCandidates = ref<WorkflowSmokeAssetCandidate[]>([])
const smokeAssetCandidatesLoaded = ref(false)
const smokeAssetCandidatesLoading = ref(false)
const smokeAssetCandidatesError = ref('')
const smokeFixtureValues = ref<Record<string, ComfyWorkflowParameterScalar>>({})
const smokeFixtureAssetIds = ref<Record<string, string>>({})
const smokeFixtureVersionId = ref('')
const smokePictureReferences = ref<SmokePictureReferenceDraft[]>([])
const smokeVideoReferences = ref<SmokeVideoReferenceDraft[]>([])
const smokeAudioReferences = ref<SmokeAudioReferenceDraft[]>([])

const message = (cause: unknown) => apiErrorSummary(cause, '请求失败')
const clearError = () => {
  error.value = ''
  errorDetails.value = null
}
const showError = (cause: unknown) => {
  const presentation = apiErrorPresentation(cause, '请求失败，请按处理建议重试。')
  error.value = presentation.message
  errorDetails.value = presentation
}
const technicalErrorFields = computed(() => Object.entries(errorDetails.value?.fields || {}))
const asRecord = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
)
const shortHash = (value: string) => value ? value.slice(0, 12) : '—'
const compatibleDeployments = computed(() => {
  const version = savedVersion.value
  if (!version) return []
  return deployments.value.filter((item) => item.status === 'active'
    && item.providerDriver === 'comfyui'
    && item.providerProfileId === version.provider
    && item.profileVersion === version.modelProfileVersion
    && providers.value.some((provider) => provider.id === item.providerProfileId && provider.isActive && provider.driverType === 'comfyui')
    && modelProfiles.value.some((profile) => profile.id === item.profileVersion && profile.status === 'published'))
})
const selectedDeployment = computed(() => compatibleDeployments.value.find((item) => item.id === deploymentId.value) || null)
const providerName = computed(() => providers.value.find((item) => item.id === savedVersion.value?.provider)?.name || savedVersion.value?.provider || '—')
const modelProfileName = computed(() => {
  const profile = modelProfiles.value.find((item) => item.id === savedVersion.value?.modelProfileVersion)
  return profile ? `${profile.profileKey} · v${profile.version}` : savedVersion.value?.modelProfileVersion || '—'
})
const graphNodes = computed(() => Object.entries(savedVersion.value?.graph || {}).map(([id, raw]) => {
  const node = asRecord(raw)
  const inputs = asRecord(node.inputs)
  return {
    id,
    classType: String(node.class_type || 'Unknown'),
    inputCount: Object.keys(inputs).length,
    linkCount: Object.values(inputs).filter((value) => Array.isArray(value) && value.length === 2).length,
  }
}).sort((left, right) => left.id.localeCompare(right.id, undefined, { numeric: true })))
const inputContract = computed(() => asRecord(savedVersion.value?.inputContract))
const workflowCapability = computed(() => String(inputContract.value.capability || '—'))
const workflowMode = computed(() => String(inputContract.value.workflow_mode || '—'))
const smokeFixtureSchema = computed(() => savedVersion.value?.smokeFixtureSchema || null)
const requestedFocus = computed<'validate' | 'smoke' | 'publish'>(() => {
  const value = String(route.query.focus || '')
  return value === 'smoke' || value === 'publish' ? value : 'validate'
})
const requestedDeploymentId = computed(() => String(route.query.deployment_id || ''))
const acceptanceEvidenceState = computed<WorkflowAcceptanceEvidenceState>(() => (
  acceptanceFreshnessLoading.value || acceptanceFreshnessError.value
    ? 'unavailable'
    : workflowAcceptanceEvidenceState(acceptanceFreshness.value?.validation)
))
const acceptanceSmokeFresh = computed(() => (
  !acceptanceFreshnessLoading.value
  && !acceptanceFreshnessError.value
  && acceptanceFreshness.value?.smoke.fresh === true
))
const referenceModes = computed(() => {
  const schema = smokeFixtureSchema.value
  if (!schema) return [] as Array<{ key: string; value: WorkflowSmokeDynamicReferencesSchema }>
  const modes = new Map<string, WorkflowSmokeDynamicReferencesSchema>()
  if (schema.dynamicReferences) modes.set(schema.dynamicReferences.mode || 'default', schema.dynamicReferences)
  Object.entries(schema.dynamicReferenceModes).forEach(([key, value]) => modes.set(key, value))
  return [...modes.entries()].map(([key, value]) => ({ key, value }))
})
const runtimeInputCount = computed(() => (
  (smokeFixtureSchema.value?.parameters.length || 0)
  + (smokeFixtureSchema.value?.assetSlots.length || 0)
  + referenceModes.value.length
))

const smokeExecutionInputDraft = computed<WorkflowExecutionInputDraft>({
  get: () => ({
    values: smokeFixtureValues.value,
    assetIds: smokeFixtureAssetIds.value,
    pictures: smokePictureReferences.value,
    videos: smokeVideoReferences.value,
    audios: smokeAudioReferences.value,
  }),
  set: (value) => {
    smokeFixtureValues.value = Object.fromEntries(
      Object.entries(value.values).filter((row): row is [string, ComfyWorkflowParameterScalar] => row[1] !== null),
    )
    smokeFixtureAssetIds.value = { ...value.assetIds }
    smokePictureReferences.value = value.pictures
    smokeVideoReferences.value = value.videos
    smokeAudioReferences.value = value.audios
  },
})
const smokeTerminalStatuses = new Set(['succeeded', 'failed', 'cancelled', 'uncertain'])
const smokeActive = computed(() => Boolean(smokeAttempt.value && !smokeTerminalStatuses.has(smokeAttempt.value.status)))
const smokeCancellable = computed(() => Boolean(smokeAttempt.value && ['queued', 'submitting', 'running'].includes(smokeAttempt.value.status)))
const smokeResolutionRequired = computed(() => smokeAttempt.value?.resolutionRequired === true)
const smokeDraftRequiresAssetCandidates = computed(() => {
  const schema = smokeFixtureSchema.value
  if (!schema) return false
  const dynamic = schema.dynamicReferenceModes[
    String(smokeFixtureValues.value.CONDITIONING_MODE || '')
  ] || schema.dynamicReferences
  return schema.assetSlots.some((slot) => (
    slot.required || Boolean(smokeFixtureAssetIds.value[slot.semanticKey])
  )) || Boolean(
    dynamic && (
      dynamic.pictures.minItems > 0
      || dynamic.videos.minItems > 0
      || dynamic.audios.minItems > 0
      || smokePictureReferences.value.length
      || smokeVideoReferences.value.length
      || smokeAudioReferences.value.length
    )
  )
})
const smokeResolutionValidationError = computed(() => {
  if (!smokeResolutionRequired.value) return ''
  if (!auth.user?.isStaff) return '只有平台管理员可以提交人工消歧结论。'
  const limits = productionCapability.value?.effectiveLimits
  if (!limits) return '服务端审计说明限制不可用，不能提交消歧结论。'
  const length = smokeResolutionNote.value.trim().length
  if (length < limits.auditNoteMinCharacters || length > limits.auditNoteMaxCharacters) {
    return `消歧说明必须为 ${limits.auditNoteMinCharacters}–${limits.auditNoteMaxCharacters} 个字符。`
  }
  if (smokeResolutionOutcome.value === 'confirmed_not_submitted' && smokeResolutionProviderRequestId.value.trim()) {
    return '确认未提交时不能填写 Provider request ID。'
  }
  return ''
})
const smokeFixtureValidationError = computed(() => {
  const schema = smokeFixtureSchema.value
  if (!schema?.available) return `服务端未提供可用的本次烟测 fixture 合同（${schema?.reasonCode || 'workflow_fixture_contract_missing'}）`
  if (smokeDraftRequiresAssetCandidates.value && !smokeAssetCandidatesLoaded.value) {
    return smokeAssetCandidatesError.value || '烟测素材候选尚未读取'
  }
  return validateWorkflowExecutionInputDraft(schema, smokeExecutionInputDraft.value, smokeAssetCandidates.value)
})
const acceptancePhase = computed(() => workflowAcceptancePhase(
  savedVersion.value,
  acceptanceEvidenceState.value,
  acceptanceSmokeFresh.value,
))
const smokeInputDisabled = computed(() => (
  !acceptancePhase.value.canConfigureSmoke || smokeActive.value || Boolean(busy.value)
))
const smokeStartBlockReason = computed(() => {
  const version = savedVersion.value
  if (!version) return '工作流版本尚未读取。'
  if (version.status === 'published') return '该版本已发布；如需重新验收，请从 ComfyUI 保存新 revision 后重新导入。'
  if (version.status === 'retired') return '该版本已停用，不能再创建 Smoke。'
  if (version.validationState === 'passed' && acceptanceEvidenceState.value !== 'fresh') {
    if (acceptanceEvidenceState.value === 'drifted') return '历史 Live Validate 已通过，但当前验收目标配置已漂移；请先重新执行 Live Validate。'
    if (acceptanceEvidenceState.value === 'missing') return '历史状态缺少可核验的当前目标证据；请先重新执行 Live Validate。'
    return '当前验收证据新鲜度不可用；系统已失败关闭，请重新读取或重新执行 Live Validate。'
  }
  if (!acceptancePhase.value.canStartSmoke) return '请先完成 Live Validate；成功后即可编辑本次 Smoke 输入。'
  if (!deploymentId.value) return '缺少与 Live Validate 相同的验收部署。'
  if (deploymentId.value !== version.validatedDeployment) return '当前选择的部署与 Live Validate 冻结部署不一致；请先使用此部署重新校验。'
  if (validationRequestFailed.value) return '当前部署尚未取得有效 Live Validate 证据，请先重新校验。'
  if (evidenceRefreshError.value) return '持久验证证据读取失败，请先刷新页面或重试校验。'
  if (smokeResolutionRequired.value) return '当前存在待人工消歧的 UNCERTAIN attempt；请先冻结核验结论。'
  if (smokeActive.value) return '已有 Smoke 正在执行，请等待完成或请求取消。'
  if (busy.value) return '当前操作尚未完成，请稍候。'
  if (smokeFixtureValidationError.value) return smokeFixtureValidationError.value
  return ''
})
const smokeStartBlocked = computed(() => Boolean(smokeStartBlockReason.value))
const smokeStepMessage = computed(() => (
  smokeStartBlockReason.value
  || (savedVersion.value?.smokeTestState === 'passed' && !acceptanceSmokeFresh.value
    ? acceptanceFreshness.value?.smoke.message || acceptanceFreshness.value?.smoke.reasonCode
    : '')
  || savedVersion.value?.smokeTestMessage
  || '使用下方实际输入 fixture'
))
const smokeInputStateLabel = computed(() => {
  if (!acceptancePhase.value.canConfigureSmoke) return '待校验'
  if (smokeInputDisabled.value) return '暂不可编辑'
  return smokeFixtureValidationError.value ? '待补全' : '可提交'
})
const validationDisplayState = computed(() => {
  if (validationRequestFailed.value || evidenceRefreshError.value || acceptanceFreshnessError.value) return 'error'
  if (savedVersion.value?.validationState === 'passed' && acceptanceEvidenceState.value !== 'fresh') return acceptanceEvidenceState.value
  return savedVersion.value?.validationState || ''
})
const validationGateMessage = computed(() => {
  if (validationRequestFailed.value) {
    return errorDetails.value?.recovery?.summary || '本次 Live Validate 未通过；请查看页面上方的失败原因和处理建议。'
  }
  if (savedVersion.value?.validationState === 'failed') {
    return '上次 Live Validate 未通过；点击“校验”可重新检查当前状态并取得处理建议。'
  }
  if (savedVersion.value?.validationState === 'passed' && acceptanceEvidenceState.value !== 'fresh') {
    if (acceptanceFreshnessLoading.value) return '正在读取当前目标证据新鲜度；读取完成前 Smoke 与发布保持关闭。'
    return acceptanceFreshness.value?.validation.message
      || acceptanceFreshness.value?.validation.reasonCode
      || '历史验证存在，但当前目标证据不可用或已经漂移。'
  }
  return savedVersion.value?.validationMessage || '检查节点、端口、模型和运行目标'
})
const smokeDisplayState = computed(() => {
  if (smokeRequestFailed.value || evidenceRefreshError.value || acceptanceFreshnessError.value) return 'error'
  if (savedVersion.value?.smokeTestState === 'passed' && !acceptanceSmokeFresh.value) {
    return acceptanceFreshness.value?.smoke.state || 'stale'
  }
  return savedVersion.value?.smokeTestState || ''
})
const publishBlocked = computed(() => (
  !savedVersion.value
  || !acceptancePhase.value.canPublish
  || validationRequestFailed.value
  || smokeRequestFailed.value
  || Boolean(evidenceRefreshError.value)
  || acceptanceEvidenceState.value !== 'fresh'
  || !acceptanceSmokeFresh.value
  || savedVersion.value.validationState !== 'passed'
  || savedVersion.value.smokeTestState !== 'passed'
))
const readinessConclusion = computed(() => {
  const version = savedVersion.value
  if (!version) return '版本不可用'
  if (version.status === 'published' && (acceptanceEvidenceState.value !== 'fresh' || !acceptanceSmokeFresh.value)) return '当前版本已发布并冻结，但当前验收或 Smoke 证据已漂移或不可用，不能据此判断新生产可用'
  if (version.status === 'published') return '当前版本已发布并冻结，可供素材生成与 ProductionRun 选择'
  if (version.status === 'retired') return '当前版本已停用；如需继续使用，请导入新的 ComfyUI revision'
  if (!compatibleDeployments.value.length) return '没有与冻结模型合同匹配的 active ComfyUI 部署'
  if (version.validationState === 'passed' && acceptanceEvidenceState.value !== 'fresh') return acceptanceEvidenceState.value === 'drifted'
    ? '历史在线校验已通过，但当前验收目标配置已漂移；下一步重新 Live Validate'
    : '当前验收目标证据不可用；Smoke 与发布已失败关闭'
  if (version.validationState !== 'passed' || validationRequestFailed.value) return '已导入固定模板，下一步执行 Live Validate'
  if (version.smokeTestState !== 'passed' || smokeRequestFailed.value) return '在线校验已通过，下一步使用实际输入执行 Smoke'
  if (!acceptanceSmokeFresh.value) return '历史 Workflow Smoke 已通过，但当前证据已漂移或不可用；下一步重新执行 Smoke'
  if (acceptancePhase.value.canPublish) return '验证证据已齐全，可以核对并发布'
  return `当前版本状态：${version.status}`
})
const readinessTone = computed(() => (
  error.value || validationRequestFailed.value || smokeRequestFailed.value || !compatibleDeployments.value.length || (savedVersion.value?.validationState === 'passed' && acceptanceEvidenceState.value !== 'fresh') || (savedVersion.value?.smokeTestState === 'passed' && !acceptanceSmokeFresh.value)
    ? 'danger'
    : savedVersion.value?.status === 'published' || acceptancePhase.value.canPublish ? 'success' : 'info'
))

const resetSmokeResolutionDraft = () => {
  smokeResolutionOutcome.value = 'confirmed_remote_failed'
  smokeResolutionNote.value = ''
  smokeResolutionProviderRequestId.value = ''
}
watch(smokeResolutionOutcome, (outcome) => {
  if (outcome === 'confirmed_not_submitted') smokeResolutionProviderRequestId.value = ''
})
const loadSmokeAssetCandidates = async () => {
  if (smokeAssetCandidatesLoading.value) return
  const schema = smokeFixtureSchema.value
  smokeAssetCandidatesError.value = ''
  if (!schema?.available || (!schema.assetSlots.length && !schema.dynamicReferences && !Object.keys(schema.dynamicReferenceModes).length)) {
    smokeAssetCandidates.value = []
    smokeAssetCandidatesLoaded.value = true
    return
  }
  smokeAssetCandidatesLoading.value = true
  smokeAssetCandidatesLoaded.value = false
  try {
    smokeAssetCandidates.value = await platformApi.listWorkflowSmokeAssetCandidates()
    smokeAssetCandidatesLoaded.value = true
  } catch (cause) {
    smokeAssetCandidates.value = []
    smokeAssetCandidatesError.value = `烟测素材候选读取失败：${message(cause)}`
  } finally {
    smokeAssetCandidatesLoading.value = false
  }
}
const initializeSmokeFixture = async (version: WorkflowTemplateVersion, force = false) => {
  if (force || smokeFixtureVersionId.value !== version.id) {
    smokeFixtureVersionId.value = version.id
    smokeExecutionInputDraft.value = createWorkflowExecutionInputDraft(version.smokeFixtureSchema || null)
    smokeAssetCandidatesLoaded.value = !version.smokeFixtureSchema?.assetSlots.length
      && !version.smokeFixtureSchema?.dynamicReferences
      && !Object.keys(version.smokeFixtureSchema?.dynamicReferenceModes || {}).length
  }
  if ((version.smokeFixtureSchema?.assetSlots.length
    || version.smokeFixtureSchema?.dynamicReferences
    || Object.keys(version.smokeFixtureSchema?.dynamicReferenceModes || {}).length)
    && !smokeAssetCandidatesLoaded.value) void loadSmokeAssetCandidates()
}
const smokeFixtureRequest = () => buildWorkflowExecutionInput(smokeFixtureSchema.value!, smokeExecutionInputDraft.value)
const loadAcceptanceFreshness = async (version: WorkflowTemplateVersion) => {
  acceptanceFreshnessLoading.value = true
  acceptanceFreshnessError.value = ''
  acceptanceFreshness.value = null
  try {
    const page = await platformApi.getAcceptanceSmokePage({
      subject: 'workflow',
      page: 1,
      pageSize: 1,
      workflowId: version.id,
      deploymentId: version.validatedDeployment || undefined,
    })
    const row = page.results.find((item): item is AcceptanceSmokeWorkflowRow => (
      item.subject === 'workflow' && item.workflow.id === version.id
    ))
    if (!row) throw new Error('验收聚合接口未返回当前 Workflow；当前证据新鲜度无法判断。')
    acceptanceFreshness.value = row
  } catch (cause) {
    acceptanceFreshnessError.value = `当前目标证据读取失败：${message(cause)}`
  } finally {
    acceptanceFreshnessLoading.value = false
  }
}
const focusAcceptanceStep = async () => {
  await nextTick()
  const targetId = requestedFocus.value === 'smoke' ? 'workflow-smoke-input' : 'workflow-acceptance-gates'
  document.getElementById(targetId)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
const clearSmokePoll = () => {
  if (smokePollTimer) window.clearTimeout(smokePollTimer)
  smokePollTimer = undefined
}
const scheduleSmokePoll = () => {
  clearSmokePoll()
  if (smokeActive.value) smokePollTimer = window.setTimeout(() => void refreshSmokeAttempt(), 2500)
}
const refreshSavedEvidence = async () => {
  if (!savedVersion.value) return
  try {
    const refreshed = await platformApi.getWorkflowVersion(savedVersion.value.id)
    savedVersion.value = refreshed
    await initializeSmokeFixture(refreshed)
    await loadAcceptanceFreshness(refreshed)
    contractResult.value = refreshed.validationEvidence || null
    smokeResult.value = refreshed.smokeTestEvidence || null
    evidenceRefreshError.value = ''
  } catch (cause) {
    contractResult.value = null
    smokeResult.value = null
    evidenceRefreshError.value = `持久证据刷新失败：${message(cause)}`
    throw cause
  }
}
const refreshSmokeAttempt = async () => {
  const attemptId = smokeAttempt.value?.id
  if (!attemptId || smokePollInFlight.value) return
  smokePollInFlight.value = true
  try {
    const refreshed = await platformApi.getWorkflowSmokeAttempt(attemptId)
    if (smokeAttempt.value?.id !== attemptId) return
    smokeAttempt.value = refreshed
    smokeAttemptError.value = ''
    if (smokeTerminalStatuses.has(refreshed.status)) {
      clearSmokePoll()
      await refreshSavedEvidence()
      if (refreshed.status === 'succeeded') notice.value = '真实烟测任务已成功；服务端不可变证据已重新读取。'
      else smokeAttemptError.value = refreshed.errorMessage || refreshed.errorCode || `烟测任务状态：${refreshed.status}`
    }
  } catch (cause) {
    smokeAttemptError.value = `烟测任务刷新失败：${message(cause)}`
  } finally {
    smokePollInFlight.value = false
    if (smokeActive.value) scheduleSmokePoll()
  }
}
const restoreSmokeAttempt = async () => {
  clearSmokePoll()
  smokeAttempt.value = null
  smokeAttemptError.value = ''
  if (!savedVersion.value || !deploymentId.value) return false
  try {
    const attempts = await platformApi.listWorkflowSmokeAttempts({ workflow_version: savedVersion.value.id, deployment: deploymentId.value })
    smokeAttempt.value = attempts.find((item) => item.resolutionRequired) || attempts[0] || null
    resetSmokeResolutionDraft()
    if (smokeActive.value) scheduleSmokePoll()
    return true
  } catch (cause) {
    smokeAttemptError.value = `烟测任务恢复失败：${message(cause)}`
    return false
  }
}
const syncDeploymentEvidence = () => {
  const evidenceMatches = Boolean(savedVersion.value && deploymentId.value === savedVersion.value.validatedDeployment)
  validationRequestFailed.value = !evidenceMatches
  smokeRequestFailed.value = !evidenceMatches
  contractResult.value = evidenceMatches ? savedVersion.value?.validationEvidence || null : null
  smokeResult.value = evidenceMatches ? savedVersion.value?.smokeTestEvidence || null : null
}
const changeDeployment = async () => {
  syncDeploymentEvidence()
  await restoreSmokeAttempt()
}
const load = async () => {
  loading.value = true
  clearError()
  try {
    if (!uuidPattern.test(workflowId.value)) throw new Error('工作流版本 ID 无效；新版本只能从已配置的 ComfyUI 工作流目录扫描导入。')
    const [version, catalog, capability] = await Promise.all([
      platformApi.getWorkflowVersion(workflowId.value),
      Promise.all([platformApi.listDeployments(), platformApi.listProviders(), platformApi.listModelProfiles()]),
      repository.getProductionCapability().catch((cause) => {
        productionCapabilityError.value = message(cause)
        return null
      }),
    ])
    savedVersion.value = version
    ;[deployments.value, providers.value, modelProfiles.value] = catalog
    productionCapability.value = capability
    contractResult.value = version.validationEvidence || null
    smokeResult.value = version.smokeTestEvidence || null
    deploymentId.value = compatibleDeployments.value.some((item) => item.id === requestedDeploymentId.value)
      ? requestedDeploymentId.value
      : compatibleDeployments.value.some((item) => item.id === version.validatedDeployment)
        ? version.validatedDeployment
        : compatibleDeployments.value[0]?.id || ''
    syncDeploymentEvidence()
    await Promise.all([initializeSmokeFixture(version, true), restoreSmokeAttempt(), loadAcceptanceFreshness(version)])
    await focusAcceptanceStep()
  } catch (cause) {
    showError(cause)
  } finally {
    loading.value = false
  }
}
const validateVersion = async () => {
  if (!savedVersion.value || !deploymentId.value || !acceptancePhase.value.canLiveValidate) return
  busy.value = 'validate'
  clearError()
  validationRequestFailed.value = true
  contractResult.value = null
  try {
    const result = await platformApi.validateWorkflow(savedVersion.value.id, deploymentId.value)
    savedVersion.value = result.version
    await initializeSmokeFixture(result.version)
    await loadAcceptanceFreshness(result.version)
    contractResult.value = result.version.validationEvidence || result.contractReport
    smokeResult.value = result.version.smokeTestEvidence || null
    validationRequestFailed.value = false
    evidenceRefreshError.value = ''
    notice.value = '服务端 Live Validate 已完成；冻结模板结构未被修改。'
  } catch (cause) {
    showError(cause)
    await refreshSavedEvidence().catch(() => undefined)
  } finally {
    busy.value = ''
  }
}
const handleRecoveryAction = async (action: ApiErrorRecoveryAction) => {
  if (action.kind === 'retry_live_validate') {
    await validateVersion()
    return
  }
  if (action.kind === 'open_settings' && action.section) {
    await router.push({ path: '/settings', query: { section: action.section } })
  }
}
const smoke = async () => {
  if (!savedVersion.value) return
  if (smokeStartBlockReason.value) {
    smokeAttemptError.value = smokeStartBlockReason.value
    return
  }
  busy.value = 'smoke-start'
  clearError()
  smokeAttemptError.value = ''
  notice.value = ''
  smokeRequestFailed.value = true
  smokeResult.value = null
  try {
    smokeAttempt.value = await platformApi.createWorkflowSmokeAttempt(savedVersion.value.id, deploymentId.value, smokeFixtureRequest())
    smokeRequestFailed.value = false
    evidenceRefreshError.value = ''
    notice.value = '真实低成本烟测已进入持久队列；本次参数和参考素材已冻结到 attempt。'
    scheduleSmokePoll()
  } catch (cause) {
    smokeAttemptError.value = message(cause)
    await refreshSavedEvidence().catch(() => undefined)
  } finally {
    busy.value = ''
  }
}
const cancelSmoke = async () => {
  if (!smokeAttempt.value || !smokeCancellable.value) return
  busy.value = 'smoke-cancel'
  smokeAttemptError.value = ''
  try {
    smokeAttempt.value = await platformApi.cancelWorkflowSmokeAttempt(smokeAttempt.value.id)
    notice.value = smokeAttempt.value.status === 'cancelled' ? '烟测已在提交前取消。' : '取消请求已持久化，等待 worker 核对远端状态。'
    scheduleSmokePoll()
  } catch (cause) {
    smokeAttemptError.value = message(cause)
  } finally {
    busy.value = ''
  }
}
const resolveSmokeUncertain = async () => {
  if (!smokeAttempt.value || !smokeResolutionRequired.value) return
  if (smokeResolutionValidationError.value) {
    smokeAttemptError.value = smokeResolutionValidationError.value
    return
  }
  busy.value = 'smoke-resolve'
  smokeAttemptError.value = ''
  notice.value = ''
  try {
    await platformApi.resolveWorkflowSmokeAttempt(smokeAttempt.value.id, {
      outcome: smokeResolutionOutcome.value,
      note: smokeResolutionNote.value.trim(),
      providerRequestId: smokeResolutionProviderRequestId.value.trim(),
    })
    resetSmokeResolutionDraft()
    if (await restoreSmokeAttempt()) notice.value = '人工消歧证据已冻结；仍需创建新 attempt 取得通过证据。'
  } catch (cause) {
    smokeAttemptError.value = message(cause)
  } finally {
    busy.value = ''
  }
}
const downloadSavedComfyCanvas = async () => {
  const version = savedVersion.value
  if (!version) return
  const exportDeploymentId = version.validatedDeployment || deploymentId.value
  if (!exportDeploymentId) {
    showError(new Error('未校验草稿必须先显式选择 active 且兼容的 ComfyUI deployment。'))
    return
  }
  busy.value = 'canvas-export'
  clearError()
  notice.value = ''
  try {
    const payload = await platformApi.getWorkflowTemplateCanvasExport(version.id, exportDeploymentId)
    if (!payload.importReady || payload.directOpenSupported || payload.openMode !== 'download_import') {
      throw new Error(payload.openReason || '服务端未返回可安全导入的 ComfyUI UI 画布。')
    }
    const blob = new Blob([JSON.stringify(payload.workflow, null, 2)], { type: 'application/json;charset=utf-8' })
    const objectUrl = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = objectUrl
    anchor.download = payload.filename
    anchor.style.display = 'none'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    URL.revokeObjectURL(objectUrl)
    notice.value = '已生成并下载只读 ComfyUI UI 画布；如需修改节点或默认参数，请在 ComfyUI 保存新 revision 后重新扫描导入。'
  } catch (cause) {
    showError(cause)
  } finally {
    busy.value = ''
  }
}
const publish = async () => {
  if (publishBlocked.value || !savedVersion.value) return
  busy.value = 'publish'
  clearError()
  try {
    savedVersion.value = await platformApi.publishWorkflow(savedVersion.value.id)
    publishOpen.value = false
    notice.value = `v${savedVersion.value.version} 已发布，可供素材生成和 ProductionRun 选择。`
  } catch (cause) {
    showError(cause)
  } finally {
    busy.value = ''
  }
}
onMounted(load)
watch(requestedFocus, () => { if (savedVersion.value) void focusAcceptanceStep() })
onBeforeUnmount(clearSmokePoll)
</script>

<template>
  <div class="platform-page workflow-version-review">
    <header class="platform-page-header"><div><button class="platform-back" @click="router.push({ path: '/settings/acceptance-smoke', query: { subject: 'workflow' } })"><ArrowLeft :size="14"/>返回验收与烟测中心</button><span class="eyebrow">ComfyUI 工作流版本 / WORKFLOW VERSION</span><h2>{{ savedVersion ? `${savedVersion.templateName} · v${savedVersion.version}` : '工作流版本验收' }}</h2><p>版本只能从平台配置的 ComfyUI 已保存工作流目录导入；本页只负责核对合同、在线校验、真实烟测和发布，不修改节点图或创建新版本。</p></div><PlatformStatusPill v-if="savedVersion" :status="savedVersion.status"/></header>
    <div v-if="error" class="platform-alert danger workflow-error-guidance" role="alert" aria-live="assertive">
      <AlertTriangle :size="17"/>
      <div>
        <strong>{{ errorDetails?.recovery?.title || '操作未完成' }}</strong>
        <p>{{ errorDetails?.recovery?.summary || error }}</p>
        <section v-if="errorDetails?.recovery" class="workflow-error-recovery" aria-label="处理建议">
          <b>处理建议</b>
          <ol><li v-for="step in errorDetails.recovery.steps" :key="step">{{ step }}</li></ol>
          <div class="platform-row-actions">
            <button v-for="action in errorDetails.recovery.actions" :key="`${action.kind}-${action.section || ''}`" class="button ghost small" :disabled="Boolean(busy)" @click="handleRecoveryAction(action)">
              <RefreshCw v-if="action.kind==='retry_live_validate'" :size="13"/>{{ action.label }}
            </button>
          </div>
        </section>
        <details v-if="errorDetails?.code || technicalErrorFields.length" class="workflow-error-technical">
          <summary>技术信息（供排查）</summary>
          <code v-if="errorDetails?.code">{{ errorDetails.code }}</code>
          <dl><div v-for="entry in technicalErrorFields" :key="entry[0]"><dt>{{ entry[0] }}</dt><dd>{{ entry[1].join('；') }}</dd></div></dl>
        </details>
      </div>
    </div>
    <div v-if="notice" class="platform-alert success"><Check :size="17"/><p>{{ notice }}</p></div>
    <div v-if="loading" class="platform-loading"><LoaderCircle class="spin" :size="22"/>读取工作流版本…</div>
    <template v-else-if="savedVersion">
      <WorkflowConfigurationLinks :workflow="savedVersion"/>
      <RouterLink class="button ghost small" :to="relationOverviewUrl('workflow',savedVersion.id)">查看关联全貌与配方引用</RouterLink>
      <WorkflowResolutionInspector :contract="inputContract"/>
      <section class="workflow-overview" :class="`tone-${readinessTone}`" aria-live="polite"><div class="workflow-overview-conclusion"><span>总体结论</span><strong>{{ readinessConclusion }}</strong><small>{{ selectedDeployment ? `验收部署：${selectedDeployment.name || selectedDeployment.key}` : '尚无兼容的验收部署' }}</small></div><dl><div><dt>固定节点</dt><dd>{{ graphNodes.length }}</dd><small>导入后不可在平台修改</small></div><div><dt>运行输入</dt><dd>{{ runtimeInputCount }}</dd><small>参数与素材按任务填写</small></div><div><dt>语义绑定</dt><dd>{{ savedVersion.semanticBindingCount }}</dd><small>随版本冻结</small></div><div><dt>模型文件</dt><dd>{{ savedVersion.modelRequirementCount }}</dd><small>随版本验收</small></div></dl></section>
      <section class="workflow-callout-grid"><article><Layers3 :size="16"/><div><strong>模板固定</strong><p>节点拓扑、节点默认值、语义绑定和模型要求来自已保存的 ComfyUI revision。结构变化必须回 ComfyUI 保存并重新扫描导入。</p></div></article><article><FileCheck2 :size="16"/><div><strong>执行任务冻结</strong><p>素材生成任务（AssetGenerationJob）与 ProductionRun 可按合同调整提示词、参数和动态参考素材；任务创建后会冻结完整节点参数与本次素材绑定，不回写模板。</p></div></article></section>
      <div v-if="acceptanceFreshnessLoading" class="platform-alert info"><LoaderCircle class="spin" :size="15"/><p>正在读取当前验收目标证据；完成前 Smoke 与发布保持关闭。</p></div>
      <div v-else-if="acceptanceFreshnessError" class="platform-alert danger"><AlertTriangle :size="15"/><div><strong>当前验收证据不可用，已失败关闭</strong><p>{{ acceptanceFreshnessError }}；仍可人工触发 Live Validate，成功后系统会重新读取。</p></div><button class="button ghost small" type="button" @click="savedVersion&&loadAcceptanceFreshness(savedVersion)"><RefreshCw :size="12"/>重试读取</button></div>
      <div v-else-if="savedVersion.validationState==='passed'&&acceptanceEvidenceState!=='fresh'" class="platform-alert danger"><AlertTriangle :size="15"/><div><strong>{{ acceptanceEvidenceState==='drifted'?'历史 Live Validate 已通过，但当前目标已漂移':'历史验证缺少当前可用证据' }}</strong><p>{{ acceptanceFreshness?.validation.message || acceptanceFreshness?.validation.reasonCode || '请重新执行 Live Validate；系统不会把历史 passed 当作当前可用。' }}</p><small v-if="acceptanceFreshness?.validation.storedTargetSha256||acceptanceFreshness?.validation.currentTargetSha256">历史 target <code>{{ acceptanceFreshness?.validation.storedTargetSha256 || '—' }}</code><br/>当前 target <code>{{ acceptanceFreshness?.validation.currentTargetSha256 || '—' }}</code></small></div></div>
      <div v-if="!acceptanceFreshnessLoading&&!acceptanceFreshnessError&&acceptanceEvidenceState==='fresh'&&savedVersion.smokeTestState==='passed'&&!acceptanceSmokeFresh" class="platform-alert danger"><AlertTriangle :size="15"/><div><strong>历史 Workflow Smoke 已通过，但当前证据已漂移或不可核验</strong><p>{{ acceptanceFreshness?.smoke.message || acceptanceFreshness?.smoke.reasonCode || '请使用本页实际输入重新执行 Workflow Smoke；发布门禁已失败关闭。' }}</p></div></div>
      <section id="workflow-acceptance-gates" class="workflow-publish-bar" aria-label="工作流版本验收门禁"><div><span><strong>1. 已扫描导入</strong><small>固定模板 v{{ savedVersion.version }} · {{ shortHash(savedVersion.graphSha256) }}</small></span><PlatformStatusPill status="passed"/></div><div><span><strong>2. Live Validate</strong><small>{{ validationGateMessage }}</small></span><PlatformStatusPill :status="validationDisplayState"/><button class="button ghost small" :disabled="!acceptancePhase.canLiveValidate||!deploymentId||Boolean(busy)" @click="validateVersion"><Check :size="13"/>{{ busy==='validate'?'校验中…':'重新校验' }}</button></div><div><span><strong>3. Smoke Test</strong><small>{{ smokeStepMessage }}</small></span><PlatformStatusPill v-if="smokeAttempt" :status="smokeAttempt.status"/><PlatformStatusPill v-else :status="smokeDisplayState"/><button class="button ghost small" :disabled="smokeStartBlocked" :title="smokeStartBlocked ? smokeStartBlockReason : '使用下方实际输入创建持久 Smoke 任务'" @click="smoke"><Play :size="13"/>{{ busy==='smoke-start'?'创建中…':smokeActive?'烟测执行中':'创建烟测任务' }}</button></div><div><span><strong>4. 发布</strong><small>只发布服务端已通过验证并留存证据的版本</small></span><button class="button primary small" :disabled="publishBlocked||Boolean(busy)" @click="publishOpen=true"><Send :size="13"/>核对并发布</button></div></section>
      <section class="workflow-review-toolbar"><label><span>Live 验收部署</span><select v-model="deploymentId" :disabled="!acceptancePhase.canLiveValidate||Boolean(busy)" @change="changeDeployment"><option value="">请选择 active 且合同匹配的 ComfyUI 部署</option><option v-for="item in compatibleDeployments" :key="item.id" :value="item.id">{{ item.name || item.key }} · active</option></select></label><button class="button ghost small" :disabled="Boolean(busy)||(!savedVersion.validatedDeployment&&!deploymentId)" title="仅生成只读 UI 画布文件，不会注入 ComfyUI 或提交生产任务" @click="downloadSavedComfyCanvas"><Download :size="13"/>{{ busy==='canvas-export'?'生成中…':'下载 ComfyUI 画布' }}</button></section>
      <div v-if="!compatibleDeployments.length" class="platform-alert danger"><AlertTriangle :size="15"/><p>没有同时满足 Provider active、模型能力版本 published、ComfyUI driver 和冻结绑定的验收部署；系统不会回退到其他部署。</p></div>
      <section class="workflow-contract-summary"><header><div><strong>冻结合同概览</strong><small>只读信息按需展开；不能在平台侧人工填写技术字段或覆盖版本</small></div><span>{{ providerName }} · {{ modelProfileName }}</span></header><div class="workflow-contract-facts"><div><span>生产能力</span><strong>{{ workflowCapability }}</strong></div><div><span>工作流模式</span><strong>{{ workflowMode }}</strong></div><div><span>编译器</span><strong>{{ savedVersion.compilerId || '—' }} · v{{ savedVersion.compilerVersion || '—' }}</strong></div><div><span>执行合同</span><strong><code>{{ shortHash(savedVersion.executionContractSha256) }}</code></strong></div></div>
        <details><summary><span>可调整参数</span><b>{{ smokeFixtureSchema?.parameters.length || 0 }}</b></summary><div class="workflow-readonly-list"><article v-for="item in smokeFixtureSchema?.parameters || []" :key="item.semanticKey"><div><strong>{{ item.semanticKey }}</strong><small>{{ item.type }} · {{ item.required ? '必填' : '可选' }}<template v-if="item.selectionGroup"> · {{ item.selectionGroup }}</template></small></div><span v-if="item.options.length">{{ item.options.join(' / ') }}</span><span v-else-if="item.alignmentModulus">{{ item.min ?? '—' }} – {{ item.max ?? '—' }} · {{ item.alignmentRemainder ?? 0 }} + {{ item.alignmentModulus }}k</span><span v-else-if="item.min!==null||item.max!==null">{{ item.min ?? '—' }} – {{ item.max ?? '—' }}</span><span v-else>推荐 {{ item.recommendedValue ?? '—' }}</span></article><p v-if="!smokeFixtureSchema?.parameters.length" class="platform-empty">该版本未声明可调整参数；执行任务仍会冻结完整节点输入。</p></div></details>
        <details><summary><span>固定素材槽与动态参考</span><b>{{ (smokeFixtureSchema?.assetSlots.length || 0) + referenceModes.length }}</b></summary><div class="workflow-readonly-list"><article v-for="item in smokeFixtureSchema?.assetSlots || []" :key="item.semanticKey"><div><strong>{{ item.semanticKey }}</strong><small>{{ item.mediaKind }} · slot {{ item.inputSlot }} · {{ item.required ? '必填' : '可选' }}</small></div><span>{{ item.minItems }}–{{ item.maxItems }} 个</span></article><article v-for="item in referenceModes" :key="item.key"><div><strong>动态参考 · {{ item.key }}</strong><small>图片 {{ item.value.pictures.minItems }}–{{ item.value.pictures.maxItems }} · 视频 {{ item.value.videos.minItems }}–{{ item.value.videos.maxItems }} · 音频 {{ item.value.audios.minItems }}–{{ item.value.audios.maxItems }}</small></div><span>最多 {{ item.value.maximumPhysicalAssets }} 个实际素材</span></article><p v-if="!smokeFixtureSchema?.assetSlots.length&&!referenceModes.length" class="platform-empty">该版本未声明参考素材输入。</p></div></details>
        <details><summary><span>节点、语义绑定与模型文件证明</span><b>{{ graphNodes.length }} / {{ savedVersion.semanticBindingCount }} / {{ savedVersion.modelRequirementCount }}</b></summary><div class="workflow-evidence-columns"><div><strong>节点清单</strong><ul><li v-for="node in graphNodes" :key="node.id"><code>{{ node.id }}</code> · {{ node.classType }} · {{ node.inputCount }} inputs / {{ node.linkCount }} links</li></ul></div><div><strong>语义绑定</strong><ul><li v-for="binding in savedVersion.semanticBindings || []" :key="binding.id"><code>{{ binding.semanticKey }}</code> → {{ binding.nodeId }}.{{ binding.inputName }} · {{ binding.valueType }}</li><li v-if="!savedVersion.semanticBindings?.length">无</li></ul></div><div><strong>模型文件要求</strong><ul><li v-for="requirement in savedVersion.modelRequirements || []" :key="requirement.id">{{ requirement.alias || requirement.role }} · {{ requirement.expectedFilename || '受管 artifact' }} · {{ requirement.isRequired ? '必需' : '可选' }}</li><li v-if="!savedVersion.modelRequirements?.length">无本地模型文件要求</li></ul></div></div></details>
      </section>
      <details id="workflow-smoke-input" class="workflow-smoke-section" :open="requestedFocus==='smoke'||(acceptancePhase.canConfigureSmoke&&savedVersion.smokeTestState!=='passed')"><summary><span><strong>本次 Smoke 输入</strong><small>与素材新增、ProductionRun 使用同一类输入合同；只冻结到本次 attempt</small></span><em>{{ smokeInputStateLabel }}</em></summary><section class="workflow-smoke-fixture"><div v-if="!smokeFixtureSchema?.available" class="platform-alert danger"><AlertTriangle :size="14"/><p>fixture 合同不可用：{{ smokeFixtureSchema?.reasonCode || 'workflow_fixture_contract_missing' }}。请回 ComfyUI 修正来源并重新扫描导入。</p></div><template v-else><p v-if="!acceptancePhase.canConfigureSmoke" class="platform-inline-error">{{ smokeStartBlockReason }}</p><WorkflowExecutionInputEditor v-model="smokeExecutionInputDraft" :schema="smokeFixtureSchema" :assets="smokeAssetCandidates" :assets-loading="smokeAssetCandidatesLoading" :assets-error="smokeAssetCandidatesError" :disabled="smokeInputDisabled" @refresh-assets="loadSmokeAssetCandidates"/><p v-if="smokeFixtureValidationError" class="platform-inline-error">{{ smokeFixtureValidationError }}</p></template></section></details>
      <section v-if="smokeAttempt" class="platform-card workflow-smoke-attempt"><header><div><strong>持久烟测任务</strong><small>{{ smokeAttempt.id }} · deployment {{ smokeAttempt.deployment }}</small></div><PlatformStatusPill :status="smokeAttempt.status"/></header><dl><div><dt>Execution contract</dt><dd><code>{{ smokeAttempt.executionContractSha256 }}</code></dd></div><div><dt>Target</dt><dd><code>{{ smokeAttempt.targetSha256 }}</code></dd></div><div><dt>Fixture</dt><dd><code>{{ smokeAttempt.fixtureSha256 }}</code></dd></div><div><dt>Fixture parameters</dt><dd>schema v{{ smokeAttempt.fixtureValuesSchemaVersion }} · {{ smokeAttempt.fixtureValuesSizeBytes }} bytes · 原文不回显</dd></div><div><dt>Poll / remote</dt><dd>{{ smokeAttempt.pollCount }} · {{ smokeAttempt.remoteStatus || '尚未提交' }}</dd></div><div><dt>Hard / idle deadline</dt><dd>{{ smokeAttempt.hardDeadlineAt || '—' }} / {{ smokeAttempt.idleDeadlineAt || '—' }}</dd></div></dl><div class="workflow-smoke-fixture-summary"><div><strong>参数事实</strong><ul><li><code>{{ smokeAttempt.fixtureValuesSha256 }}</code></li><li>查询接口只返回哈希与大小，避免提示词或参数原文扩散。</li></ul></div><div><strong>素材事实</strong><ul><li v-for="binding in smokeAttempt.assetBindings" :key="binding.semanticKey"><code>{{ binding.semanticKey }}</code> · slot {{ binding.inputSlot }} · {{ binding.mimeType }} · {{ binding.checksum.slice(0,12) }}</li><li v-if="!smokeAttempt.assetBindings.length">本次未绑定素材</li></ul></div></div>
        <div v-if="smokeResolutionRequired" class="workflow-smoke-resolution"><div class="platform-alert warning"><AlertTriangle :size="15"/><div><strong>远端提交状态待管理员消歧</strong><p>请核对 Provider 控制台和账单后追加不可变结论。确认远端成功也不等于 smoke passed。</p></div></div><template v-if="auth.user?.isStaff"><label><span>终态结论</span><select v-model="smokeResolutionOutcome" :disabled="Boolean(busy)"><option value="confirmed_not_submitted">确认远端未提交</option><option value="confirmed_remote_failed">确认远端失败</option><option value="confirmed_remote_succeeded">确认远端成功</option></select></label><label><span>核验说明（{{ productionCapability?.effectiveLimits.auditNoteMinCharacters || '—' }}–{{ productionCapability?.effectiveLimits.auditNoteMaxCharacters || '—' }} 个字符）</span><textarea v-model="smokeResolutionNote" :disabled="Boolean(busy)||!productionCapability" rows="4" :maxlength="productionCapability?.effectiveLimits.auditNoteMaxCharacters" placeholder="说明核对的控制台、账单时间范围与可复核结论"></textarea></label><label v-if="smokeResolutionOutcome!=='confirmed_not_submitted'"><span>Provider request ID（可选，仅提交、不回显）</span><input v-model="smokeResolutionProviderRequestId" :disabled="Boolean(busy)" type="password" maxlength="191" autocomplete="off"/></label><p v-if="productionCapabilityError" class="platform-inline-error">审计限制读取失败：{{ productionCapabilityError }}</p><p v-if="smokeResolutionValidationError" class="platform-inline-error">{{ smokeResolutionValidationError }}</p><button class="button primary small" :disabled="Boolean(busy)||Boolean(smokeResolutionValidationError)" @click="resolveSmokeUncertain">{{ busy==='smoke-resolve'?'提交中…':'冻结人工消歧结论' }}</button></template><p v-else class="platform-inline-error">只有平台管理员可以提交消歧结论。</p></div>
        <div v-else-if="smokeAttempt.resolutions.length" class="workflow-smoke-resolution-summary"><strong>已冻结人工结论</strong><span>{{ smokeAttempt.resolutions[0].outcome }} · revision {{ smokeAttempt.resolutions[0].revision }} · {{ smokeAttempt.resolutions[0].evidenceSha256.slice(0,12) }}</span><small>该结论不计为 smoke passed；需以新 attempt 的服务端证据为准。</small></div><div class="platform-row-actions"><button class="button ghost tiny" :disabled="smokePollInFlight" @click="refreshSmokeAttempt"><RefreshCw :size="11"/>刷新任务</button><button v-if="smokeCancellable" class="button danger tiny" :disabled="Boolean(busy)" @click="cancelSmoke"><X :size="11"/>{{ busy==='smoke-cancel'?'取消中…':'请求取消' }}</button></div></section>
      <div v-if="smokeAttemptError" class="platform-alert danger"><AlertTriangle :size="15"/><div><strong>烟测任务未完成</strong><p>{{ smokeAttemptError }}</p></div></div><div v-if="evidenceRefreshError" class="platform-alert danger"><AlertTriangle :size="15"/><div><strong>持久证据不可用</strong><p>{{ evidenceRefreshError }}</p></div></div><details v-if="contractResult||smokeResult" class="platform-card workflow-results"><summary>服务端验证证据</summary><pre v-if="contractResult">{{ JSON.stringify(contractResult,null,2) }}</pre><pre v-if="smokeResult">{{ JSON.stringify(smokeResult,null,2) }}</pre></details>
    </template>
    <BaseModal :open="publishOpen" :dismissible="!Boolean(busy)" :title="`核对并发布 · ${savedVersion?.templateName || '工作流'} v${savedVersion?.version || ''}`" eyebrow="不可变工作流发布 / IMMUTABLE WORKFLOW RELEASE" @close="publishOpen=false"><div class="workflow-publish-review"><p>发布后模板节点、参数合同、绑定和证据均不可覆盖；修改必须回 ComfyUI 保存新 revision，再从配置目录扫描导入。</p><dl><div><dt>Validated deployment</dt><dd><code>{{ savedVersion?.validatedDeployment || '—' }}</code></dd></div><div><dt>Graph hash</dt><dd><code>{{ savedVersion?.graphSha256 || '—' }}</code></dd></div><div><dt>Execution hash</dt><dd><code>{{ savedVersion?.executionContractSha256 || '—' }}</code></dd></div><div><dt>Validation evidence</dt><dd><code>{{ savedVersion?.validationEvidenceSha256 || '—' }}</code></dd></div><div><dt>Smoke evidence</dt><dd><code>{{ savedVersion?.smokeTestEvidenceSha256 || '—' }}</code></dd></div></dl><div v-if="error" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ error }}</p></div></div><template #footer><button class="button ghost" :disabled="Boolean(busy)" @click="publishOpen=false">取消</button><button class="button primary" :disabled="publishBlocked||Boolean(busy)" @click="publish">{{ busy==='publish'?'发布中…':'确认发布不可变版本' }}</button></template></BaseModal>
  </div>
</template>

<style scoped>
.workflow-error-guidance{align-items:flex-start}.workflow-error-guidance>div{min-width:0;flex:1}.workflow-error-guidance p{margin:4px 0 0;line-height:1.6}.workflow-error-recovery{margin-top:10px;padding-top:9px;border-top:1px solid rgba(211,107,115,.24)}.workflow-error-recovery>b{color:#d7b1b4;font-size:9px}.workflow-error-recovery ol{margin:7px 0 9px;padding-left:20px;color:#b8a3a5;font-size:9px;line-height:1.65}.workflow-error-recovery .platform-row-actions{display:flex;flex-wrap:wrap;gap:7px}.workflow-error-technical{margin-top:9px;color:#8c7779;font-size:8px}.workflow-error-technical summary{cursor:pointer}.workflow-error-technical>code{display:inline-block;margin-top:7px;padding:3px 6px;border:1px solid rgba(211,107,115,.2);border-radius:4px}.workflow-error-technical dl{margin:7px 0 0;display:grid;gap:5px}.workflow-error-technical dl>div{display:grid;grid-template-columns:minmax(100px,180px) minmax(0,1fr);gap:7px}.workflow-error-technical dt{color:#957f81}.workflow-error-technical dd{margin:0;overflow-wrap:anywhere;color:#ad9598}
.workflow-overview{display:grid;grid-template-columns:minmax(300px,1.35fr) minmax(0,2fr);border:1px solid var(--border);border-left:3px solid #6686a7;border-radius:8px;background:#0c0f13;overflow:hidden}.workflow-overview.tone-danger{border-left-color:#d36b73}.workflow-overview.tone-success{border-left-color:#56c693}.workflow-overview-conclusion{padding:12px 14px;display:grid;align-content:center;gap:4px;border-right:1px solid var(--border)}.workflow-overview-conclusion>span{color:#6f7983;font-size:8px}.workflow-overview-conclusion>strong{color:#c2c9d0;font-size:13px}.workflow-overview-conclusion>small{overflow:hidden;color:#727d87;font-size:9px;text-overflow:ellipsis;white-space:nowrap}.workflow-overview dl{margin:0;display:grid;grid-template-columns:repeat(4,minmax(0,1fr))}.workflow-overview dl>div{min-width:0;padding:10px 12px;border-right:1px solid var(--border)}.workflow-overview dl>div:last-child{border-right:0}.workflow-overview dt{color:#69737d;font-size:8px}.workflow-overview dd{margin:4px 0;color:#b6bec6;font:12px Consolas,monospace}.workflow-overview dl small{display:block;color:#646e78;font-size:8px}
.workflow-callout-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:8px}.workflow-callout-grid article{padding:10px 12px;display:flex;align-items:flex-start;gap:9px;border:1px solid var(--border);border-radius:8px;background:#0c0f13;color:#7f91a3}.workflow-callout-grid strong{color:#b6bec7;font-size:10px}.workflow-callout-grid p{margin:3px 0 0;color:#737e89;font-size:8px;line-height:1.55}.workflow-publish-bar{display:grid!important;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));align-items:stretch!important}.workflow-publish-bar>div{min-height:74px;display:flex!important;align-items:flex-start!important;flex-wrap:wrap;gap:7px}.workflow-publish-bar>div>span{min-width:130px;flex:1}.workflow-publish-bar .button{align-self:flex-end;margin-left:auto}
.workflow-review-toolbar{display:flex;align-items:flex-end;flex-wrap:wrap;gap:8px;padding:9px;border:1px solid var(--border);border-radius:8px;background:#0c0f13}.workflow-review-toolbar label{min-width:min(420px,100%);display:grid;gap:5px;color:#8e98a3;font-size:8px}.workflow-review-toolbar select{min-height:34px;padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:#070a0e;color:#c9d1db}
.workflow-contract-summary,.workflow-smoke-section{border:1px solid var(--border);border-radius:8px;background:#0c0f13;overflow:hidden}.workflow-contract-summary>header{padding:10px 12px;display:flex;align-items:center;justify-content:space-between;gap:10px;border-bottom:1px solid var(--border)}.workflow-contract-summary>header>div{display:grid;gap:3px}.workflow-contract-summary>header strong{color:#b7c0c8;font-size:10px}.workflow-contract-summary>header small,.workflow-contract-summary>header>span{color:#6d7781;font-size:8px}.workflow-contract-facts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-bottom:1px solid var(--border)}.workflow-contract-facts>div{min-width:0;padding:9px 11px;display:grid;gap:4px;border-right:1px solid var(--border)}.workflow-contract-facts>div:last-child{border-right:0}.workflow-contract-facts span{color:#66717c;font-size:7px}.workflow-contract-facts strong{overflow:hidden;color:#9ea8b2;font-size:9px;text-overflow:ellipsis;white-space:nowrap}.workflow-contract-summary>details+details{border-top:1px solid var(--border)}.workflow-contract-summary summary,.workflow-smoke-section>summary{padding:9px 11px;display:flex;align-items:center;justify-content:space-between;gap:10px;color:#9da6af;font-size:9px;cursor:pointer}.workflow-contract-summary summary b,.workflow-smoke-section summary em{color:#8f7659;font-size:8px;font-style:normal}.workflow-readonly-list{display:grid;gap:6px;padding:0 10px 10px}.workflow-readonly-list article{padding:8px 9px;display:flex;align-items:center;justify-content:space-between;gap:10px;border:1px solid rgba(255,255,255,.055);border-radius:6px;background:#090c10}.workflow-readonly-list article>div{display:grid;gap:3px}.workflow-readonly-list strong{color:#aeb7c0;font-size:9px}.workflow-readonly-list small,.workflow-readonly-list article>span{color:#737e88;font-size:8px}.workflow-evidence-columns{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;padding:0 10px 10px}.workflow-evidence-columns>div{min-width:0;padding:9px;border:1px solid var(--border);border-radius:7px;background:#090c10}.workflow-evidence-columns strong{color:#909aa4;font-size:8px}.workflow-evidence-columns ul{max-height:230px;margin:7px 0 0;padding-left:16px;overflow:auto;color:#8a949e;font-size:8px;line-height:1.65}.workflow-evidence-columns code{overflow-wrap:anywhere}
.workflow-smoke-fixture{padding:10px}.workflow-smoke-attempt{padding-bottom:10px}.workflow-smoke-attempt>header{padding:10px}.workflow-smoke-attempt>header>div{display:flex;min-width:0;flex-direction:column;gap:3px}.workflow-smoke-attempt>header small{overflow:hidden;color:#68727a;font:8px Consolas,monospace;text-overflow:ellipsis}.workflow-smoke-attempt dl{margin:0;padding:0 10px 9px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.workflow-smoke-attempt dt{color:#68727b;font-size:7px}.workflow-smoke-attempt dd{margin:3px 0 0;overflow:hidden;color:#9da5ad;font-size:8px;text-overflow:ellipsis;white-space:nowrap}.workflow-smoke-attempt>.platform-row-actions{padding:0 10px}.workflow-smoke-fixture-summary{padding:0 10px 9px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.workflow-smoke-fixture-summary>div{min-width:0;padding:8px;border:1px solid var(--border);border-radius:7px;background:#090c10}.workflow-smoke-fixture-summary strong{color:#858f99;font-size:8px}.workflow-smoke-fixture-summary ul{margin:6px 0 0;padding-left:16px;color:#9da5ad;font-size:8px;line-height:1.6}.workflow-smoke-fixture-summary code{overflow-wrap:anywhere}.workflow-smoke-resolution{margin:0 10px 10px;padding:10px;display:grid;gap:9px;border:1px solid rgba(210,150,71,.28);border-radius:7px;background:#0e0d0a}.workflow-smoke-resolution>.platform-alert{margin:0}.workflow-smoke-resolution label{min-width:0;display:grid;gap:5px;color:#9da5ad;font-size:8px}.workflow-smoke-resolution input,.workflow-smoke-resolution select,.workflow-smoke-resolution textarea{min-width:0;width:100%;padding:7px 8px;border:1px solid var(--border);border-radius:6px;background:#070a0e;color:#c9d1db;font-size:9px}.workflow-smoke-resolution input,.workflow-smoke-resolution select{min-height:34px}.workflow-smoke-resolution textarea{resize:vertical}.workflow-smoke-resolution>.button{justify-self:start}.workflow-smoke-resolution-summary{margin:0 10px 10px;padding:9px;display:grid;gap:4px;border:1px solid var(--border);border-radius:7px;background:#090c10;color:#89939c;font-size:8px}.workflow-smoke-resolution-summary strong{color:#aeb6bf}.workflow-smoke-resolution-summary small{color:#69737e}
@media(max-width:1100px){.workflow-overview{grid-template-columns:1fr}.workflow-overview-conclusion{border-right:0;border-bottom:1px solid var(--border)}.workflow-contract-facts{grid-template-columns:repeat(2,minmax(0,1fr))}.workflow-evidence-columns{grid-template-columns:1fr}}@media(max-width:620px){.workflow-error-technical dl>div{grid-template-columns:1fr}.workflow-error-recovery .button{flex:1}.workflow-overview dl,.workflow-contract-facts,.workflow-smoke-attempt dl,.workflow-smoke-fixture-summary{grid-template-columns:1fr}.workflow-overview dl>div,.workflow-contract-facts>div{border-right:0;border-bottom:1px solid var(--border)}.workflow-readonly-list article{align-items:flex-start;flex-direction:column}}
</style>
