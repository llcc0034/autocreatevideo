<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AlertTriangle, Archive, Boxes, Check, ChevronDown, Copy, Cpu, Download, Eye, FileClock, FileJson2, FileText, Gauge, GitBranch, KeyRound, LoaderCircle, Play, Plus, Power, RefreshCw, Save, Server, Settings2, ShieldCheck, Trash2, Workflow } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import ConfigValueEditor from '@/components/ConfigValueEditor.vue'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import PromptProfileSettings from '@/components/PromptProfileSettings.vue'
import RegistryResourceEditor from '@/components/RegistryResourceEditor.vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { parseConfigValue, type ConfigResourceOption as ConfigEditorResourceOption } from '@/services/configValueSchema'
import { platformApi } from '@/services/platformApi'
import { applyResourceDefaults, type RegistryResource } from '@/services/registryResourceSchema'
import { runtimeEvidenceState } from '@/services/runtimeHostSelection'
import { buildSystemConfigChangeSet, effectiveSourceLabel } from '@/services/settingsRegistry'
import type { ConfigDefinition, ConfigRevision, HostCapabilitySnapshot, JsonObject, ModelArtifact, ModelDeployment, ModelDeploymentSmokeAttempt, ModelProfileVersion, PlatformLifecycleAuditEvent, ProductionReadiness, ProductionRecipe, ProviderProfile, RecommendationCandidate, RecommendationSnapshot, ResolvedConfiguration, RuntimeHost, RuntimeHostProbeAttempt, RuntimeReadiness, SecretAuditEvent, SecretBinding, SmokeResolutionOutcome, WorkflowTemplateCanvasExport, WorkflowTemplateVersion } from '@/services/platformTypes'
import { useAuthStore } from '@/stores/auth'

type SettingsTab = 'models' | 'workflows' | 'prompts' | 'routing' | 'defaults' | 'runtime' | 'governance'
type RegistryEntity = ProviderProfile | ModelProfileVersion | ModelDeployment | ModelArtifact | WorkflowTemplateVersion | ProductionRecipe | ConfigDefinition | ConfigRevision | RuntimeHost | HostCapabilitySnapshot | SecretBinding | SecretAuditEvent | PlatformLifecycleAuditEvent | ModelDeploymentSmokeAttempt
type RegistryAction = 'publish-model' | 'retire-model' | 'delete-provider' | 'delete-model' | 'delete-deployment' | 'delete-artifact' | 'verify-artifact' | 'delete-workflow' | 'retire-workflow' | 'publish-recipe' | 'delete-recipe' | 'retire-recipe' | 'delete-definition' | 'save-defaults' | 'publish-revision' | 'activate-revision' | 'host-status' | 'deployment-probe' | 'deployment-smoke'
const route = useRoute(); const router = useRouter()
const auth = useAuthStore()
const tabs: Array<{ id: SettingsTab; label: string; hint: string; icon: unknown }> = [
  { id: 'models', label: '模型', hint: 'Provider、版本与部署', icon: Boxes }, { id: 'workflows', label: '工作流', hint: 'ComfyUI 模板版本', icon: Workflow },
  { id: 'prompts', label: '提示词策略', hint: 'PromptProfile 组装', icon: FileText },
  { id: 'routing', label: '配方与路由', hint: '一键生产阶段', icon: GitBranch }, { id: 'defaults', label: '默认值与限制', hint: '唯一配置来源', icon: Settings2 },
  { id: 'runtime', label: '运行主机', hint: '发现、就绪与推荐', icon: Cpu }, { id: 'governance', label: '凭据与审计', hint: '引用与版本记录', icon: ShieldCheck },
]
const allowedTabs = new Set(tabs.map((item) => item.id)); const initialTab = String(route.query.section || 'models')
const tab = ref<SettingsTab>(allowedTabs.has(initialTab as SettingsTab) ? initialTab as SettingsTab : 'models')
const loading = ref(false); const saving = ref(false); const error = ref(''); const notice = ref('')
const sectionErrors = ref<Record<string, string>>({})
const providers = ref<ProviderProfile[]>([]); const modelProfiles = ref<ModelProfileVersion[]>([]); const deployments = ref<ModelDeployment[]>([]); const modelArtifacts = ref<ModelArtifact[]>([])
const workflowVersions = ref<WorkflowTemplateVersion[]>([]); const recipes = ref<ProductionRecipe[]>([])
const workflowExportOpen = ref(false); const workflowExportBusyId = ref(''); const workflowExportError = ref(''); const workflowExport = ref<WorkflowTemplateCanvasExport | null>(null)
const workflowExportVersion = ref<WorkflowTemplateVersion | null>(null); const workflowExportDeploymentId = ref('')
const definitions = ref<ConfigDefinition[]>([]); const revisions = ref<ConfigRevision[]>([]); const resolved = ref<ResolvedConfiguration | null>(null); const configDraft = ref<Record<string, string>>({})
const configResourceOptions = ref<Record<string, ConfigEditorResourceOption[]>>({})
const configOriginal = ref<Record<string, string>>({}); const configDirty = ref(new Set<string>()); const configUnsets = ref(new Set<string>())
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
const editorOpen = ref(false); const editorResource = ref<RegistryResource>('provider'); const editorId = ref(''); const editorPayload = ref<JsonObject>({}); const editorError = ref('')
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
const decisionOpen = ref(false); const decisionCandidate = ref<RecommendationCandidate | null>(null); const decisionKind = ref<'accept'|'override'|'reject'>('accept'); const decisionOverrideText = ref('{}'); const decisionReason = ref('系统设置中心人工决策'); const decisionError = ref('')
const editorTemplates: Record<RegistryResource, JsonObject> = {
  provider: { key: '', name: '', driver_type: '', modalities: '', operations: '', driver_config: {}, is_active: true },
  model: { profile_key: '', version: 1, provider: '', model_identifier: '', modality: '', specification: {} },
  deployment: { key: '', name: '', profile_version: '', runtime_host: null, runtime_config: {}, priority: 100, is_default: false, status: 'active' },
  artifact: { profile_version: '', role: '', logical_name: '', filename: '', storage_uri: '', sha256: '', size_bytes: null, status: 'declared' },
  recipe: { recipe_key: '', version: 1, name: '', definition: { schema_version: 1, stages: [] } },
  'config-definition': { key: '', domain: '', value_type: 'string', description: '', impact_modules: [], allowed_scopes: 'system,project,run', allowed_default_sources: '', restart_required: false, is_required: false, is_secret: false, is_active: true, needs_review: false, review_reason: '', value_schema: {}, initial_value: null },
}
const bytes = (value: number | null | undefined) => { const amount = Number(value); if (!Number.isFinite(amount)) return '—'; if (amount >= 1024 ** 3) return `${(amount / 1024 ** 3).toFixed(1)} GB`; if (amount >= 1024 ** 2) return `${(amount / 1024 ** 2).toFixed(1)} MB`; if (amount >= 1024) return `${(amount / 1024).toFixed(1)} KB`; return `${amount} B` }
const dateTime = (value: string) => value ? new Date(value).toLocaleString('zh-CN') : '—'
const message = (cause: unknown) => apiErrorSummary(cause, '请求失败，服务端没有返回可读错误')
const copyObservedHash = async (value: string) => {
  if (!value) return
  try { await navigator.clipboard.writeText(value); notice.value = '已复制完整 SHA-256。' }
  catch (cause) { error.value = `复制 SHA-256 失败：${message(cause)}` }
}
const pretty = (value: unknown) => JSON.stringify(value, null, 2) ?? ''; const sourceFor = (key: string) => resolved.value?.sources[key]
const capture = async <T>(key: string, promise: Promise<T>, commit: (value: T) => void) => {
  try { commit(await promise); const next = { ...sectionErrors.value }; delete next[key]; sectionErrors.value = next }
  catch (cause) { sectionErrors.value = { ...sectionErrors.value, [key]: message(cause) } }
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
const restoreDeploymentSmokeAttempts = async () => {
  clearDeploymentSmokePoll()
  try {
    const attempts = await platformApi.listDeploymentSmokeAttempts()
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
    sectionErrors.value = { ...sectionErrors.value, deploymentSmokeAttempts: message(cause) }
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
    if (completed) deployments.value = await platformApi.listDeployments()
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
const restoreRuntimeProbeAttempts = async () => {
  clearRuntimeProbePoll()
  try {
    const attempts = await platformApi.listRuntimeHostProbeAttempts()
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
    sectionErrors.value = { ...sectionErrors.value, runtimeProbeAttempts: message(cause) }
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
const loadLatestSnapshots = async () => {
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
  latestHostSnapshots.value = Object.fromEntries(entries.filter((item): item is readonly [string, HostCapabilitySnapshot] => Boolean(item)))
  latestHostSnapshotStates.value = states
  const next = { ...sectionErrors.value }
  if (failures.length) next.runtimeSnapshots = `读取主机快照失败：${failures.join('；')}`
  else delete next.runtimeSnapshots
  sectionErrors.value = next
}
const loadModels = async () => {
  await Promise.all([
    capture('providers', platformApi.listProviders(), (value) => { providers.value = value }),
    capture('modelProfiles', platformApi.listModelProfiles(), (value) => { modelProfiles.value = value }),
    capture('deployments', platformApi.listDeployments(), (value) => { deployments.value = value }),
    capture('modelArtifacts', platformApi.listModelArtifacts(), (value) => { modelArtifacts.value = value }),
    capture('runtimeHosts', platformApi.listRuntimeHosts(), (value) => { runtimeHosts.value = value }),
    ...(auth.user?.isSuperuser ? [capture('secretBindings', platformApi.listSecretBindings(), (value) => { secretBindings.value = value })] : []),
  ])
  await Promise.all([loadLatestSnapshots(), restoreDeploymentSmokeAttempts()])
}
const loadDefaults = async () => {
  await Promise.all([
    capture('definitions', platformApi.listConfigDefinitions(), (value) => { definitions.value = value }),
    capture('revisions', platformApi.listConfigRevisions(), (value) => { revisions.value = value }),
    capture('resolved', platformApi.resolveConfiguration(), (value) => { resolved.value = value }),
    ...(auth.user?.isSuperuser ? [capture('secretBindings', platformApi.listSecretBindings(), (value) => { secretBindings.value = value })] : []),
  ])
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
      sectionErrors.value = {
        ...sectionErrors.value,
        [`configResource:${item.key}`]: message(cause),
      }
      return [item.key, []] as const
    }
  }))
  configResourceOptions.value = Object.fromEntries(optionEntries)
  const drafts = Object.fromEntries(definitions.value.filter((item) => item.isActive && !item.isSecret).map((item) => [item.key, pretty(resolved.value?.values[item.key] ?? item.initialValue)]))
  configDraft.value = drafts; configOriginal.value = { ...drafts }; configDirty.value = new Set(); configUnsets.value = new Set(); secretSelections.value = {}
}
const loadSelectedHostSnapshot = async () => {
  const hostId = selectedHostId.value; if (!hostId) return
  selectedSnapshotState.value = 'idle'
  try {
    const latest = await platformApi.getLatestRuntimeSnapshot(hostId)
    if (selectedHostId.value !== hostId) return
    snapshot.value = latest; latestHostSnapshots.value = { ...latestHostSnapshots.value, [hostId]: latest }
    readiness.value = await platformApi.runtimeReadiness(hostId, latest.id)
    selectedSnapshotState.value = 'ready'
    const next = { ...sectionErrors.value }; delete next.runtimeSnapshot; sectionErrors.value = next
  } catch (cause) {
    const status = (cause as { response?: { status?: number } })?.response?.status
    selectedSnapshotState.value = status === 404 ? 'missing' : 'error'
    sectionErrors.value = { ...sectionErrors.value, runtimeSnapshot: status === 404 ? '所选主机尚无能力快照，请执行探测。' : message(cause) }
  }
}
const loadRuntime = async () => {
  await Promise.all([
    capture('runtimeHosts', platformApi.listRuntimeHosts(), (value) => { runtimeHosts.value = value }),
    capture('resolved', platformApi.resolveConfiguration(), (value) => { resolved.value = value }),
    restoreRuntimeProbeAttempts(),
    ...(auth.user?.isSuperuser ? [capture('productionReadiness', platformApi.getProductionReadiness(), (value) => { productionReadiness.value = value })] : []),
  ])
  if (!auth.user?.isSuperuser) productionReadiness.value = null
  if (!runtimeHosts.value.some((item) => item.id === selectedHostId.value)) selectedHostId.value = runtimeHosts.value[0]?.id || ''
  await Promise.resolve(); await loadSelectedHostSnapshot()
}
const load = async () => {
  loading.value = true; error.value = ''; notice.value = ''; sectionErrors.value = {}
  try {
    if (tab.value === 'models') await loadModels()
    else if (tab.value === 'workflows') await capture('workflows', platformApi.listWorkflowVersions(), (value) => { workflowVersions.value = value })
    else if (tab.value === 'prompts') { /* PromptProfileSettings owns its scoped server reads. */ }
    else if (tab.value === 'routing') await Promise.all([
      capture('recipes', platformApi.listRecipes(), (value) => { recipes.value = value }),
      capture('modelProfiles', platformApi.listModelProfiles(), (value) => { modelProfiles.value = value }),
      capture('deployments', platformApi.listDeployments(), (value) => { deployments.value = value }),
      capture('workflows', platformApi.listWorkflowVersions(), (value) => { workflowVersions.value = value }),
    ])
    else if (tab.value === 'defaults') await loadDefaults()
    else if (tab.value === 'runtime') await loadRuntime()
    else {
      await Promise.all([
        capture('providers', platformApi.listProviders(), (value) => { providers.value = value }),
        capture('deployments', platformApi.listDeployments(), (value) => { deployments.value = value }),
        capture('revisions', platformApi.listConfigRevisions(), (value) => { revisions.value = value }),
        capture('lifecycleAudit', platformApi.listPlatformLifecycleAuditEvents(), (value) => { lifecycleAudit.value = value }),
        ...(auth.user?.isSuperuser ? [
          capture('secretBindings', platformApi.listSecretBindings(), (value) => { secretBindings.value = value }),
          capture('secretAudit', platformApi.listSecretAuditEvents(), (value) => { secretAudit.value = value }),
        ] : []),
      ])
      if (!auth.user?.isSuperuser) { secretBindings.value = []; secretAudit.value = [] }
    }
  } finally { loading.value = false }
}
const selectTab = async (value: SettingsTab) => { tab.value = value; await router.replace({ query: { ...route.query, section: value } }) }
watch(tab, () => { if (tab.value !== 'models') clearDeploymentSmokePoll(); if (tab.value !== 'runtime') clearRuntimeProbePoll(); void load() })
onMounted(() => { void load(); runtimeEvidenceTimer = window.setInterval(() => { runtimeEvidenceNow.value = Date.now() }, 1000) })
onBeforeUnmount(() => { window.clearInterval(runtimeEvidenceTimer); clearDeploymentSmokePoll(); clearRuntimeProbePoll() })
const editorDependencies = computed(() => ({
  providers: providers.value,
  modelProfiles: modelProfiles.value,
  deployments: deployments.value,
  runtimeHosts: runtimeHosts.value,
  workflowVersions: workflowVersions.value,
}))
const editorResourceLabel = computed(() => ({
  provider: 'Provider', model: '模型版本', deployment: '部署', artifact: '模型文件',
  recipe: '生产配方', 'config-definition': '配置定义',
})[editorResource.value])
const openEditor = (resource: RegistryResource, existing?: ProviderProfile | ModelProfileVersion | ModelDeployment | ModelArtifact | ProductionRecipe | ConfigDefinition) => {
  editorResource.value = resource
  editorId.value = existing?.id || ''
  let payload: JsonObject = JSON.parse(JSON.stringify(editorTemplates[resource])) as JsonObject
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
    filename: existing.filename, storage_uri: existing.storageUri, sha256: existing.sha256,
    size_bytes: existing.sizeBytes, status: 'declared',
  }
  if (resource === 'config-definition' && existing && 'valueType' in existing) payload = {
    key: existing.key, domain: existing.domain, value_type: existing.valueType, description: existing.description,
    unit: existing.unit, impact_modules: existing.impactModules, restart_required: existing.restartRequired,
    allowed_scopes: existing.allowedScopes, allowed_default_sources: existing.allowedDefaultSources,
    is_required: existing.isRequired, is_secret: existing.isSecret, is_active: existing.isActive,
    needs_review: existing.needsReview, review_reason: existing.reviewReason,
    value_schema: existing.valueSchema, initial_value: existing.initialValue,
  }
  editorPayload.value = applyResourceDefaults(resource, payload, editorDependencies.value)
  editorValidity.value = { valid: false, errors: [], warnings: [] }
  editorError.value = ''
  editorOpen.value = true
}
const submitEditor = async () => {
  if (!editorValidity.value.valid) {
    editorError.value = `请先修复表单中的 ${editorValidity.value.errors.length} 个阻断项。`
    return
  }
  saving.value = true; editorError.value = ''
  try {
    const parsed = JSON.parse(JSON.stringify(editorPayload.value)) as JsonObject
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('提交内容必须是 JSON 对象')
    if (['provider', 'deployment'].includes(editorResource.value)) {
      delete parsed.credential_id
      if (auth.user?.isSuperuser && editorCredentialId.value === '__clear__') parsed.credential_id = null
      else if (auth.user?.isSuperuser && editorCredentialId.value && editorCredentialId.value !== '__keep__') parsed.credential_id = editorCredentialId.value
    }
    const reasonCode = editorId.value ? 'settings.update' : 'settings.create'
    if (editorResource.value === 'provider') editorId.value ? await platformApi.updateProvider(editorId.value, parsed, reasonCode) : await platformApi.createProvider(parsed, reasonCode)
    else if (editorResource.value === 'model') editorId.value ? await platformApi.updateModelProfile(editorId.value, parsed, reasonCode) : await platformApi.createModelProfile(parsed, reasonCode)
    else if (editorResource.value === 'deployment') editorId.value ? await platformApi.updateDeployment(editorId.value, parsed, reasonCode) : await platformApi.createDeployment(parsed, reasonCode)
    else if (editorResource.value === 'artifact') editorId.value ? await platformApi.updateModelArtifact(editorId.value, parsed, reasonCode) : await platformApi.createModelArtifact(parsed, reasonCode)
    else if (editorResource.value === 'recipe') editorId.value ? await platformApi.updateRecipe(editorId.value, parsed, reasonCode) : await platformApi.createRecipe(parsed, reasonCode)
    else editorId.value ? await platformApi.updateConfigDefinition(editorId.value, parsed, reasonCode) : await platformApi.createConfigDefinition(parsed, reasonCode)
    const action = editorId.value ? '更新' : '保存'
    editorOpen.value = false; await load(); notice.value = `服务端已${action}，列表已重新读取。`
  } catch (cause) { editorError.value = message(cause) } finally { saving.value = false }
}
const showDetail = (kind: string, title: string, entity: unknown) => { detailRequestKey.value = ''; detailKind.value = kind; detailTitle.value = title; detailEntity.value = entity; detailOpen.value = true }
const detailPayload = computed<unknown>(() => detailEntity.value ? JSON.parse(JSON.stringify(detailEntity.value)) : {})
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
  notice.value = '已下载 ComfyUI UI 画布 JSON；请在 ComfyUI 中拖入该文件或从 Workflow 菜单打开。'
}
const closeAction = (force = false) => { if (saving.value && !force) return; actionOpen.value = false; actionError.value = ''; actionOperation.value = ''; actionReferenceId.value = ''; actionReasonCode.value = '' }
const openAction = (kind: RegistryAction, id: string, title: string, description: string, options: { operation?: string; referenceId?: string; enable?: boolean } = {}) => {
  actionKind.value = kind; actionId.value = id; actionTitle.value = title; actionDescription.value = description
  actionOperation.value = options.operation || ''; actionReferenceId.value = options.referenceId || ''; actionEnable.value = options.enable === true
  actionReasonCode.value = ''; actionError.value = ''; actionOpen.value = true
}
const profileFor = (id: string) => modelProfiles.value.find((item) => item.id === id)
const artifactEditable = (item: ModelArtifact) => profileFor(item.profileVersion)?.status === 'draft'
const deploymentSmokeOperations = (item: ModelDeployment) => {
  const capabilities = profileFor(item.profileVersion)?.specification.capabilities
  const declared = Array.isArray(capabilities) ? capabilities.map(String) : []
  return declared.filter((value) => ['image.text_to_image', 'video.text_to_video', 'prompt.normalize', 'script.generate', 'script.structure'].includes(value))
}
const actionDeployment = computed(() => deployments.value.find((item) => item.id === actionId.value) || null)
const actionSmokeOperations = computed(() => actionDeployment.value ? deploymentSmokeOperations(actionDeployment.value) : [])
const smokeResolutionValidation = computed(() => {
  const submission = smokeResolutionDeployment.value?.latestSmoke?.unresolvedSubmission
  const costText = smokeResolutionActualCost.value.trim()
  const cost = costText === '' ? null : Number(costText)
  if (!submission) return '没有可消歧的 smoke submission。'
  if (smokeResolutionNote.value.trim().length < 8) return '消歧说明至少 8 个字符。'
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
  const rows: RegistryEntity[] = [...providers.value, ...modelProfiles.value, ...deployments.value, ...modelArtifacts.value, ...workflowVersions.value, ...recipes.value, ...definitions.value, ...revisions.value, ...runtimeHosts.value]
  return rows.find((item) => item.id === actionId.value) || null
})
const actionReviewPayload = computed(() => actionKind.value === 'save-defaults' ? pendingConfigReview.value : actionEntity.value)
const destructiveActions = new Set<RegistryAction>(['retire-model', 'delete-provider', 'delete-model', 'delete-deployment', 'delete-artifact', 'delete-workflow', 'retire-workflow', 'delete-recipe', 'retire-recipe', 'delete-definition'])
const auditReasonActions = new Set<RegistryAction>(['publish-model', 'retire-model', 'delete-provider', 'delete-model', 'delete-deployment', 'delete-artifact', 'delete-workflow', 'retire-workflow', 'publish-recipe', 'delete-recipe', 'retire-recipe', 'delete-definition', 'save-defaults', 'publish-revision', 'activate-revision', 'host-status'])
const actionReasonRequired = computed(() => auditReasonActions.has(actionKind.value))
const lifecycleObjectTypes = computed(() => [...new Set(lifecycleAudit.value.map((item) => item.objectType))].sort())
const lifecycleActions = computed(() => [...new Set(lifecycleAudit.value.map((item) => item.action))].sort())
const filteredLifecycleAudit = computed(() => lifecycleAudit.value.filter((item) => (
  (!lifecycleObjectFilter.value || item.objectType === lifecycleObjectFilter.value)
  && (!lifecycleActionFilter.value || item.action === lifecycleActionFilter.value)
)))
const actionConfirmLabel = computed(() => {
  if (actionKind.value.startsWith('delete-')) return '确认删除'
  if (actionKind.value.startsWith('retire-')) return '确认退役'
  if (actionKind.value === 'deployment-smoke') return '确认并执行真实烟测'
  if (actionKind.value === 'deployment-probe') return '确认探测'
  if (actionKind.value === 'verify-artifact') return '确认写入核验证据'
  if (actionKind.value === 'host-status') return actionEnable.value ? '确认启用' : '确认停用'
  return '确认提交'
})
const openArtifactVerification = (item: ModelArtifact) => {
  const candidate = Object.values(latestHostSnapshots.value)
    .filter((row) => row.modelAvailability.some((fact) => fact.artifact === item.id))
    .sort((left, right) => Date.parse(right.capturedAt) - Date.parse(left.capturedAt))[0]
  if (!candidate) { error.value = '没有包含该模型文件的主机快照；请先在“运行主机”执行探测'; return }
  const fact = candidate.modelAvailability.find((row) => row.artifact === item.id)
  openAction('verify-artifact', item.id, `核验模型文件 · ${item.logicalName || item.filename}`, `将使用主机快照 ${candidate.id} 的事实状态“${fact?.status || 'unknown'}”更新目录状态；快照与操作者会持久化。`, { referenceId: candidate.id })
}
const markConfigDirty = (key: string) => { const next = new Set(configDirty.value); next.add(key); configDirty.value = next; const unsets = new Set(configUnsets.value); unsets.delete(key); configUnsets.value = unsets }
const restoreConfigInheritance = (key: string) => { const next = new Set(configUnsets.value); next.add(key); configUnsets.value = next; const dirty = new Set(configDirty.value); dirty.delete(key); configDirty.value = dirty }
const undoConfigChange = (key: string) => { configDraft.value[key] = configOriginal.value[key] ?? ''; const dirty = new Set(configDirty.value); dirty.delete(key); configDirty.value = dirty; const unsets = new Set(configUnsets.value); unsets.delete(key); configUnsets.value = unsets }
const saveDefaults = () => {
  try {
    if (Object.keys(invalidConfigDrafts.value).length) throw new Error('存在未通过本地结构校验的配置项，请先修正。')
    const changes = buildSystemConfigChangeSet({ definitions: definitions.value, draftText: configDraft.value, dirtyKeys: configDirty.value, unsetKeys: configUnsets.value, secretSelections: secretSelections.value, isSuperuser: auth.user?.isSuperuser === true })
    if (!Object.keys(changes.values).length && !changes.unsetKeys.length) throw new Error('没有待发布的配置变更')
    openAction('save-defaults', '', '核对并发布系统配置', `仅提交 ${Object.keys(changes.values).length} 个修改项和 ${changes.unsetKeys.length} 个恢复继承项；未编辑的 model/workflow 默认值不会被提升为 system override。`)
  } catch (cause) { error.value = message(cause) }
}
const runAction = async () => {
  saving.value = true; actionError.value = ''; error.value = ''
  try {
    const reasonCode = actionReasonCode.value.trim()
    if (actionReasonRequired.value && !reasonCode) throw new Error('请选择本次高风险操作的审计原因代码。')
    if (actionKind.value === 'publish-model') await platformApi.publishModelProfile(actionId.value, reasonCode)
    else if (actionKind.value === 'retire-model') await platformApi.retireModelProfile(actionId.value, reasonCode)
    else if (actionKind.value === 'delete-provider') await platformApi.deleteProvider(actionId.value, reasonCode)
    else if (actionKind.value === 'delete-model') await platformApi.deleteModelProfile(actionId.value, reasonCode)
    else if (actionKind.value === 'delete-deployment') await platformApi.deleteDeployment(actionId.value, reasonCode)
    else if (actionKind.value === 'delete-artifact') await platformApi.deleteModelArtifact(actionId.value, reasonCode)
    else if (actionKind.value === 'verify-artifact') await platformApi.verifyModelArtifact(actionId.value, actionReferenceId.value)
    else if (actionKind.value === 'delete-workflow') await platformApi.deleteWorkflow(actionId.value, reasonCode)
    else if (actionKind.value === 'retire-workflow') await platformApi.retireWorkflow(actionId.value, reasonCode)
    else if (actionKind.value === 'publish-recipe') await platformApi.publishRecipe(actionId.value, reasonCode)
    else if (actionKind.value === 'delete-recipe') await platformApi.deleteRecipe(actionId.value, reasonCode)
    else if (actionKind.value === 'retire-recipe') await platformApi.retireRecipe(actionId.value, reasonCode)
    else if (actionKind.value === 'delete-definition') await platformApi.deleteConfigDefinition(actionId.value, reasonCode)
    else if (actionKind.value === 'publish-revision') await platformApi.publishConfigRevision(actionId.value, reasonCode)
    else if (actionKind.value === 'activate-revision') await platformApi.activateConfigRevision(actionId.value, reasonCode)
    else if (actionKind.value === 'host-status') await platformApi.setRuntimeHostStatus(actionId.value, actionEnable.value, reasonCode)
    else if (actionKind.value === 'deployment-probe') await platformApi.probeDeployment(actionId.value)
    else if (actionKind.value === 'deployment-smoke') {
      const attempt = await platformApi.smokeTestDeployment(actionId.value, actionOperation.value)
      deploymentSmokeAttempts.value = { ...deploymentSmokeAttempts.value, [attempt.deployment]: attempt }
      deploymentSmokeErrors.value = { ...deploymentSmokeErrors.value, [attempt.deployment]: '' }
      closeAction(true); notice.value = `真实烟测任务已创建并进入 ${attempt.status}；页面将持续读取持久状态。`
      scheduleDeploymentSmokePoll()
      return
    }
    else if (actionKind.value === 'save-defaults') {
      const changes = buildSystemConfigChangeSet({ definitions: definitions.value, draftText: configDraft.value, dirtyKeys: configDirty.value, unsetKeys: configUnsets.value, secretSelections: secretSelections.value, isSuperuser: auth.user?.isSuperuser === true })
      const draft = await platformApi.createConfigRevision({ scopeType: 'system', values: changes.values, unsetKeys: changes.unsetKeys, changeSummary: '通过系统设置中心核对并发布默认值与限制', reasonCode })
      await platformApi.publishConfigRevision(draft.id, reasonCode)
    }
    closeAction(true); await load(); notice.value = '操作已由服务端完成，当前分区已重新读取。'
  } catch (cause) { actionError.value = message(cause) } finally { saving.value = false }
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
const groupedDefinitions = computed(() => { const result = new Map<string, ConfigDefinition[]>(); for (const item of definitions.value) result.set(item.domain || 'other', [...(result.get(item.domain || 'other') || []), item]); return [...result.entries()] })
const productionReadinessState = computed(() => sectionErrors.value.productionReadiness ? 'error' : productionReadiness.value?.status || 'unconfigured')
const readinessReasonAction = (reasonCode: string): { label: string; section: SettingsTab } => {
  if (reasonCode.includes('recipe')) return { label: '检查已发布配方', section: 'routing' }
  if (reasonCode.includes('workflow')) return { label: '检查工作流版本', section: 'workflows' }
  if (reasonCode.includes('model') || reasonCode.includes('deployment') || reasonCode.includes('artifact')) return { label: '检查模型与部署', section: 'models' }
  if (reasonCode.includes('config') || reasonCode.includes('secret')) return { label: '检查默认值与凭据', section: 'defaults' }
  return { label: '检查运行主机', section: 'runtime' }
}
const credentialRows = computed(() => [...providers.value.map((item) => ({ owner: `Provider · ${item.name}`, configured: item.credentialConfigured, lifecycle: item.isActive ? 'active' : 'inactive', usable: item.credentialConfigured && item.isActive })), ...deployments.value.map((item) => ({ owner: `Deployment · ${item.name}`, configured: item.credentialConfigured, lifecycle: item.status, usable: item.credentialConfigured && item.status === 'active' }))])
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
    <header class="platform-page-header"><div><span class="eyebrow">PLATFORM CONTROL PLANE</span><h2>系统设置</h2><p>模型、ComfyUI 工作流、生产配方、默认参数和主机推荐均以服务端 revision 为准。</p></div><button class="button ghost small" :disabled="loading" @click="load"><RefreshCw :size="14" :class="{spin:loading}"/>重新读取</button></header>
    <nav class="settings-center-nav" aria-label="设置分类"><button v-for="item in tabs" :key="item.id" :class="{active:tab===item.id}" @click="selectTab(item.id)"><component :is="item.icon" :size="17"/><span><strong>{{ item.label }}</strong><small>{{ item.hint }}</small></span></button></nav>
    <div v-if="error" class="platform-alert danger"><AlertTriangle :size="17"/><div><strong>设置未能完整读取或保存</strong><p>{{ error }}</p></div><button class="button ghost small" @click="load">重试</button></div>
    <div v-if="notice" class="platform-alert success"><Check :size="17"/><p>{{ notice }}</p></div><div v-if="loading" class="platform-loading"><LoaderCircle class="spin" :size="22"/>正在读取服务端设置…</div>

    <template v-else-if="tab==='models'">
      <section class="platform-toolbar"><div><strong>模型注册表</strong><span>Provider → 草稿模型版本/文件声明 → 发布 → 主机启用并探测为 active → 部署 → 再探测主机 → 文件核验 → 部署 probe/smoke。已发布版本只能退役或新建下一版。</span></div><div><button class="button ghost small" @click="openEditor('provider')"><Plus :size="13"/>Provider</button><button class="button ghost small" @click="openEditor('model')"><Plus :size="13"/>模型版本</button><button class="button ghost small" @click="openEditor('artifact')"><Plus :size="13"/>模型文件</button><button class="button primary small" @click="openEditor('deployment')"><Plus :size="13"/>部署</button></div></section>
      <div class="platform-card-grid">
        <details class="platform-card" open><summary><span><Server :size="16"/>Provider</span><b>{{ providers.length }}</b><ChevronDown :size="14"/></summary><p v-if="sectionErrors.providers" class="platform-inline-error">{{ sectionErrors.providers }}</p><div class="platform-dense-list"><article v-for="item in providers" :key="item.id"><div><strong>{{ item.name || item.key }}</strong><small>{{ item.driverType }} · {{ item.modalities || '未声明 modality' }} · {{ item.operations || '未声明操作' }}</small></div><PlatformStatusPill :status="item.isActive?'active':'inactive'"/><span class="platform-row-actions"><button class="button ghost tiny" @click="showDetail('Provider',item.name||item.key,item)"><Eye :size="11"/>详情</button><button class="button ghost tiny" @click="openEditor('provider',item)">编辑</button><button v-if="!item.isSystem&&!item.isActive" class="button danger tiny" @click="openAction('delete-provider',item.id,`删除 Provider · ${item.name}`, '仅未被模型或工作流引用且已停用的 Provider 可删除。')"><Trash2 :size="11"/>删除</button></span></article><p v-if="!providers.length&&!sectionErrors.providers" class="platform-empty">尚无 Provider。</p></div></details>
        <details class="platform-card" open><summary><span><Boxes :size="16"/>模型版本</span><b>{{ modelProfiles.length }}</b><ChevronDown :size="14"/></summary><p v-if="sectionErrors.modelProfiles" class="platform-inline-error">{{ sectionErrors.modelProfiles }}</p><div class="platform-dense-list"><article v-for="item in modelProfiles" :key="item.id"><div><button class="registry-title-link" type="button" @click="showDetail('模型版本',`${item.profileKey} · v${item.version}`,item)"><strong>{{ item.profileKey }} · v{{ item.version }}</strong></button><small>{{ item.modelIdentifier }} · {{ item.modality }} · {{ bytes(item.specificationSizeBytes) }}</small></div><PlatformStatusPill :status="item.status"/><span class="platform-row-actions"><button class="button ghost tiny" @click="showDetail('模型版本',`${item.profileKey} · v${item.version}`,item)"><Eye :size="11"/>详情</button><button v-if="item.status==='draft'" class="button ghost tiny" @click="openEditor('model',item)">编辑</button><button v-if="item.status==='draft'" class="button primary tiny" @click="openAction('publish-model',item.id,`核对并发布 · ${item.profileKey} v${item.version}`, '发布后 specification 与文件关联不可原地修改；后续变更必须新建版本。')">发布</button><button v-if="item.status==='draft'" class="button danger tiny" @click="openAction('delete-model',item.id,`删除草稿 · ${item.profileKey} v${item.version}`, '仅无引用草稿可删除。')"><Trash2 :size="11"/></button><button v-if="item.status==='published'" class="button ghost tiny" @click="openAction('retire-model',item.id,`退役 · ${item.profileKey} v${item.version}`, '仍有 active deployment 或当前工作流引用时服务端会拒绝。')"><Archive :size="11"/>退役</button></span></article><p v-if="!modelProfiles.length&&!sectionErrors.modelProfiles" class="platform-empty">尚无模型版本。</p></div></details>
        <details class="platform-card" open>
          <summary><span><Gauge :size="16"/>部署</span><b>{{ deployments.length }}</b><ChevronDown :size="14"/></summary>
          <p v-if="sectionErrors.deployments" class="platform-inline-error">{{ sectionErrors.deployments }}</p>
          <p v-if="sectionErrors.deploymentSmokeAttempts" class="platform-inline-error">{{ sectionErrors.deploymentSmokeAttempts }}</p>
          <div class="platform-dense-list">
            <article v-for="item in deployments" :key="item.id">
              <div>
                <strong>{{ item.name || item.key }}</strong>
                <small>{{ item.providerDriver }} · priority {{ item.priority }}<template v-if="item.isDefault"> · 默认</template> · smoke {{ item.latestSmoke?.fresh?'fresh':item.latestSmoke?.status||'未执行' }}</small>
                <small v-if="deploymentSmokeFor(item.id)">持久任务 {{ deploymentSmokeFor(item.id)?.id.slice(0,8) }} · {{ deploymentSmokeFor(item.id)?.operation }} · {{ deploymentSmokeFor(item.id)?.status }} · 更新于 {{ dateTime(deploymentSmokeFor(item.id)?.updatedAt || '') }}</small>
                <small v-if="deploymentSmokeErrors[item.id]" class="danger">{{ deploymentSmokeErrors[item.id] }}</small>
                <small v-if="item.latestSmoke?.unresolvedSubmission&&!deploymentSmokeActiveStatuses.has(deploymentSmokeFor(item.id)?.status || '')" class="danger">{{ item.latestSmoke.unresolvedSubmission.status }} · 提交状态待人工消歧 · request {{ item.latestSmoke.unresolvedSubmission.requestSha256.slice(0,12) }}</small>
              </div>
              <PlatformStatusPill :status="deploymentSmokeFor(item.id)?.status || (item.latestSmoke?.unresolvedSubmission?'unresolved':item.status)"/>
              <span class="platform-row-actions">
                <button class="button ghost tiny" @click="showDeploymentDetail(item)"><Eye :size="11"/>详情与证据</button>
                <button v-if="deploymentSmokeFor(item.id)" class="button ghost tiny" @click="showDetail('部署烟测任务',`${item.name || item.key} · ${deploymentSmokeFor(item.id)?.status}`,deploymentSmokeFor(item.id))"><FileClock :size="11"/>烟测任务</button>
                <button class="button ghost tiny" @click="openEditor('deployment',item)">编辑</button>
                <button v-if="item.status==='active'&&item.providerDriver==='async_rest'" class="button ghost tiny" @click="openAction('deployment-probe',item.id,`探测 · ${item.name}`, '执行受控远端健康探测并追加不可变 capability snapshot。')"><Gauge :size="11"/>探测</button>
                <button v-if="item.latestSmoke?.unresolvedSubmission&&auth.user?.isStaff&&!deploymentSmokeActiveStatuses.has(deploymentSmokeFor(item.id)?.status || '')" class="button danger tiny" @click="openSmokeResolution(item)"><ShieldCheck :size="11"/>管理员消歧</button>
                <button v-if="deploymentSmokeFor(item.id)&&deploymentSmokeActiveStatuses.has(deploymentSmokeFor(item.id)?.status || '')" class="button danger tiny" :disabled="Boolean(deploymentSmokeBusyId)" @click="cancelDeploymentSmoke(deploymentSmokeFor(item.id)!)">{{ deploymentSmokeBusyId===deploymentSmokeFor(item.id)?.id?'取消中…':'取消任务' }}</button>
                <button v-if="item.status==='active'&&deploymentSmokeOperations(item).length&&!item.latestSmoke?.unresolvedSubmission&&!deploymentSmokeActiveStatuses.has(deploymentSmokeFor(item.id)?.status || '')" class="button ghost tiny" @click="openAction('deployment-smoke',item.id,`真实烟测 · ${item.name}`, '会提交真实低成本请求并可能产生费用；文本、图片和视频烟测都会先创建持久任务，再由页面轮询服务端终态。', {operation:deploymentSmokeOperations(item)[0]})"><Play :size="11"/>烟测</button>
                <button v-if="item.status==='disabled'" class="button danger tiny" @click="openAction('delete-deployment',item.id,`删除 Deployment · ${item.name}`, '存在烟测、主机可用性、工作流、推荐或运行证据时服务端会返回冲突并保留记录。')"><Trash2 :size="11"/></button>
              </span>
            </article>
            <p v-if="!deployments.length&&!sectionErrors.deployments" class="platform-empty">尚无部署。</p>
          </div>
        </details>
        <details class="platform-card" open><summary><span><FileJson2 :size="16"/>模型文件关联</span><b>{{ modelArtifacts.length }}</b><ChevronDown :size="14"/></summary><p v-if="sectionErrors.modelArtifacts||sectionErrors.runtimeSnapshots" class="platform-inline-error">{{ sectionErrors.modelArtifacts || sectionErrors.runtimeSnapshots }}</p><div class="platform-dense-list"><article v-for="item in modelArtifacts" :key="item.id"><div><strong>{{ item.logicalName || item.filename }}</strong><small>{{ item.role }} · {{ bytes(item.sizeBytes) }} · {{ item.verifiedAt?`核验 ${dateTime(item.verifiedAt)}`:'尚无核验证据' }}</small></div><PlatformStatusPill :status="item.status"/><span class="platform-row-actions"><button class="button ghost tiny" @click="showDetail('模型文件',item.logicalName||item.filename,item)"><Eye :size="11"/>详情</button><button v-if="artifactEditable(item)" class="button ghost tiny" @click="openEditor('artifact',item)">编辑</button><button v-if="artifactEditable(item)" class="button danger tiny" @click="openAction('delete-artifact',item.id,`删除模型文件关联 · ${item.logicalName}`, '仅草稿模型版本下、无工作流引用的关联可删除。')"><Trash2 :size="11"/></button><button v-if="profileFor(item.profileVersion)?.status==='published'" class="button primary tiny" @click="openArtifactVerification(item)"><Check :size="11"/>按主机快照核验</button></span></article><p v-if="!modelArtifacts.length&&!sectionErrors.modelArtifacts" class="platform-empty">尚无模型文件关联。远端文本模型可为 N/A，不应伪造文件。</p></div></details>
      </div>
    </template>

    <template v-else-if="tab==='workflows'">
      <section class="platform-toolbar"><div><strong>ComfyUI 工作流版本</strong><span>导入新版本 → Live validate → 真实 smoke → 核对发布；发布态不可覆盖，只能退役。</span></div><RouterLink class="button primary small" to="/settings/workflows/new"><Plus :size="13"/>导入工作流</RouterLink></section>
      <p v-if="sectionErrors.workflows" class="platform-inline-error">{{ sectionErrors.workflows }}</p>
      <div class="platform-table-wrap"><table class="platform-table"><thead><tr><th>模板 / 版本</th><th>产物</th><th>状态</th><th>校验</th><th>烟测</th><th>证据</th><th>操作</th></tr></thead><tbody>
        <tr v-for="item in workflowVersions" :key="item.id"><td><strong>{{ item.templateName || item.templateSlug }}</strong><small>v{{ item.version }} · {{ item.graphSha256.slice(0,10) }}</small></td><td>{{ item.outputKind }}</td><td><PlatformStatusPill :status="item.status"/></td><td><PlatformStatusPill :status="item.validationState"/></td><td><PlatformStatusPill :status="item.smokeTestState"/></td><td><small>{{ item.validatedDeployment?`deployment ${item.validatedDeployment.slice(0,8)}`:'尚无持久化验证目标' }}<br/>{{ item.executionContractSha256?item.executionContractSha256.slice(0,12):'no execution hash' }}</small></td><td><span class="platform-row-actions"><button class="button ghost tiny" @click="showWorkflowDetail(item)"><Eye :size="11"/>详情与证据</button><RouterLink class="button ghost tiny" :to="`/settings/workflows/${item.id}`">打开编辑器</RouterLink><button class="button ghost tiny" :disabled="Boolean(workflowExportBusyId)" @click="prepareWorkflowImport(item)"><Download :size="11"/>{{ workflowExportBusyId===item.id?'生成画布中…':'在 ComfyUI 中打开…' }}</button><button v-if="item.status==='draft'" class="button danger tiny" @click="openAction('delete-workflow',item.id,`删除工作流草稿 · ${item.templateName} v${item.version}`, '仅未被引用的 draft 可删除；工作流不支持原地 PATCH。')"><Trash2 :size="11"/></button><button v-if="item.status==='published'" class="button ghost tiny" @click="openAction('retire-workflow',item.id,`退役工作流 · ${item.templateName} v${item.version}`, '草稿或已发布配方仍引用时服务端会拒绝。')"><Archive :size="11"/>退役</button></span></td></tr>
      </tbody></table><p v-if="!workflowVersions.length&&!sectionErrors.workflows" class="platform-empty">尚无工作流版本。</p></div>
    </template>

    <PromptProfileSettings v-else-if="tab==='prompts'"/>

    <template v-else-if="tab==='routing'"><section class="platform-toolbar"><div><strong>Production Recipe</strong><span>详情会显示完整阶段、冻结 route IDs 与合同 hash；发布由服务端校验 content→delivery 完整闭环。</span></div><button class="button primary small" @click="openEditor('recipe')"><Plus :size="13"/>新建草稿配方</button></section><p v-if="sectionErrors.recipes" class="platform-inline-error">{{ sectionErrors.recipes }}</p><div class="platform-card-grid recipes"><details v-for="item in recipes" :key="item.id" class="platform-card" open><summary><span><GitBranch :size="16"/>{{ item.name || item.recipeKey }} · v{{ item.version }}</span><PlatformStatusPill :status="item.status"/><ChevronDown :size="14"/></summary><div class="recipe-detail"><code>{{ item.recipeKey }} · {{ item.definitionSha256.slice(0,12) }} · {{ bytes(item.definitionSizeBytes) }}</code><div class="platform-row-actions"><button class="button ghost small" @click="showDetail('生产配方',`${item.name} · v${item.version}`,item)"><Eye :size="12"/>审核详情</button><button v-if="item.status==='draft'" class="button ghost small" @click="openEditor('recipe',item)">编辑草稿</button><button v-if="item.status==='draft'" class="button primary small" @click="openAction('publish-recipe',item.id,`核对并发布 · ${item.name} v${item.version}`, '服务端将复验脚本模式、阶段顺序、route deployment/workflow 和 delivery closure；失败不会发布。')">核对并发布</button><button v-if="item.status==='draft'" class="button danger small" @click="openAction('delete-recipe',item.id,`删除配方草稿 · ${item.name}`, '仅无运行、推荐或其他审计引用的 draft 可删除。')"><Trash2 :size="12"/>删除</button><button v-if="item.status==='published'" class="button ghost small" @click="openAction('retire-recipe',item.id,`退役配方 · ${item.name}`, '退役后不再用于新 ProductionRun；历史运行继续引用不可变版本。')"><Archive :size="12"/>退役</button></div></div></details><p v-if="!recipes.length&&!sectionErrors.recipes" class="platform-empty">尚无配方。普通用户只能读取已发布配方，写入权限由服务端判定。</p></div></template>

    <template v-else-if="tab==='defaults'"><section class="platform-toolbar"><div><strong>Effective 配置</strong><span>只提交人工修改的 dirty keys；“恢复继承”通过 unset_keys 删除 system override，不会复制 model/workflow 默认值。</span></div><div><button class="button ghost small" @click="openEditor('config-definition')"><Plus :size="13"/>新增定义</button><button class="button primary small" :disabled="saving||(!configDirty.size&&!configUnsets.size)||Boolean(Object.keys(invalidConfigDrafts).length)" @click="saveDefaults"><Save :size="13"/>核对并发布变更</button></div></section><p v-if="sectionErrors.definitions||sectionErrors.resolved" class="platform-inline-error">{{ sectionErrors.definitions || sectionErrors.resolved }}</p><div class="resolved-fingerprint"><FileClock :size="15"/><span><strong>{{ resolved ? `已解析 ${Object.keys(resolved.values).length} 项 · 待改 ${configDirty.size} · 待恢复 ${configUnsets.size}` : '未能解析 effective 配置' }}</strong><small>{{ resolved?.sha256 || '必须修复服务端返回的 missing keys 后才能创建 ProductionRun' }}</small></span></div><details v-for="[domain,items] in groupedDefinitions" :key="domain" class="platform-card config-domain" open><summary><span><Settings2 :size="16"/>{{ domain }}</span><b>{{ items.length }}</b><ChevronDown :size="14"/></summary><div class="config-fields"><label v-for="item in items" :key="item.id" :class="{secret:item.isSecret,changed:configDirty.has(item.key),unset:configUnsets.has(item.key)}"><span><strong>{{ item.key }}</strong><em>{{ item.valueType }} · {{ item.allowedScopes }}<template v-if="item.restartRequired"> · 重启生效</template></em></span><ConfigValueEditor v-if="!item.isSecret" v-model="configDraft[item.key]" :definition="item" :resource-options="configResourceOptions[item.key]||[]" :disabled="configUnsets.has(item.key)" @change="markConfigDirty(item.key)"/><p v-if="sectionErrors[`configResource:${item.key}`]" class="platform-inline-error">依赖选项读取失败：{{ sectionErrors[`configResource:${item.key}`] }}</p><select v-else-if="auth.user?.isSuperuser" v-model="secretSelections[item.key]" :disabled="configUnsets.has(item.key)" @change="markConfigDirty(item.key)"><option value="">保持当前绑定</option><option v-for="secret in secretBindings.filter((row)=>row.isActive&&row.configured)" :key="secret.id" :value="secret.id">{{ secret.label || secret.key }} · v{{ secret.currentVersion }}</option></select><div v-else class="secret-value"><KeyRound :size="14"/>秘密值不回传浏览器；仅超级管理员可替换受管凭据。</div><div class="platform-row-actions"><button type="button" class="button ghost tiny" @click="showDetail('配置定义',item.key,item)"><Eye :size="11"/>详情</button><button type="button" class="button ghost tiny" @click="openEditor('config-definition',item)">编辑定义</button><button v-if="sourceFor(item.key)?.source==='system'&&!configUnsets.has(item.key)" type="button" class="button ghost tiny" @click="restoreConfigInheritance(item.key)">恢复继承</button><button v-if="configDirty.has(item.key)||configUnsets.has(item.key)" type="button" class="button ghost tiny" @click="undoConfigChange(item.key)">撤销</button><button v-if="!item.isRequired&&!item.isActive" type="button" class="button danger tiny" @click="openAction('delete-definition',item.id,`删除配置定义 · ${item.key}`, '已有 revision 使用时服务端会拒绝；应保留历史并停用。')"><Trash2 :size="11"/></button></div><small><template v-if="item.needsReview">需要人工确认：{{ item.reviewReason || '缺少产品初始值' }} · </template>{{ item.description || '服务端未提供说明' }} · 来源 {{ effectiveSourceLabel(sourceFor(item.key)) }}</small></label></div></details><details class="platform-card" open><summary><span><FileClock :size="16"/>配置 Revision</span><b>{{ revisions.length }}</b><ChevronDown :size="14"/></summary><p v-if="sectionErrors.revisions" class="platform-inline-error">{{ sectionErrors.revisions }}</p><div class="platform-dense-list"><article v-for="item in revisions" :key="item.id"><div><strong>{{ item.scopeType }} · revision {{ item.revision }}</strong><small>{{ item.changeSummary || '无变更说明' }} · {{ item.valuesSha256.slice(0,12) }}</small></div><PlatformStatusPill :status="item.activationState||item.status"/><span class="platform-row-actions"><button class="button ghost tiny" @click="showDetail('配置 Revision',`revision ${item.revision}`,item)"><Eye :size="11"/>详情</button><button v-if="item.status==='draft'" class="button primary tiny" @click="openAction('publish-revision',item.id,`发布配置 revision ${item.revision}`, '服务端将复验 stale base、必需值、秘密绑定权限与重启影响。')">发布</button><button v-if="(item.activationState==='pending_activation'||item.activationState==='failed')&&item.activationAvailable" class="button primary tiny" @click="openAction('activate-revision',item.id,`激活配置 revision ${item.revision}`, '仅新 boot 可激活 restart_required 配置；服务端会再次复验基线和秘密绑定权限。')">激活</button></span></article></div></details></template>

    <template v-else-if="tab==='runtime'">
      <section v-if="auth.user?.isSuperuser" class="platform-card production-readiness">
        <header><ShieldCheck :size="18"/><div><strong>生产就绪清单</strong><small>按全部已发布配方检查文本、图片、视频和组装闭环；只展示服务端原因代码，不展示底层地址或凭据。</small></div><PlatformStatusPill :status="productionReadinessState"/></header>
        <div v-if="sectionErrors.productionReadiness" class="platform-alert danger"><AlertTriangle :size="15"/><div><strong>就绪清单读取失败</strong><p>{{ sectionErrors.productionReadiness }}</p></div></div>
        <div v-else-if="productionReadinessState==='unconfigured'" class="platform-alert warning"><AlertTriangle :size="15"/><div><strong>尚未配置可发布生产配方</strong><p>原因代码：{{ productionReadiness?.reasonCodes.join('、') || 'published_recipe_missing' }}</p><button class="button ghost tiny" @click="selectTab('routing')">检查已发布配方</button></div></div>
        <div v-else class="platform-dense-list">
          <article v-for="recipe in productionReadiness?.recipes || []" :key="recipe.id"><div><strong>{{ recipe.recipeKey }} · v{{ recipe.version }}</strong><small>{{ recipe.reasonCodes.length ? recipe.reasonCodes.join('、') : '全部必需能力已闭环' }}</small><div class="platform-row-actions"><button v-for="reasonCode in recipe.reasonCodes" :key="reasonCode" class="button ghost tiny" @click="selectTab(readinessReasonAction(reasonCode).section)">{{ readinessReasonAction(reasonCode).label }}</button></div></div><PlatformStatusPill :status="recipe.state"/></article>
        </div>
      </section>
      <section class="platform-toolbar">
        <div><strong>主机自动发现与推荐</strong><span>先选择明确主机；探测、就绪、推荐和决策必须属于同一当前快照。</span></div>
        <div>
          <button class="button ghost small" :disabled="Boolean(runtimeBusy)||Boolean(runtimeProbeBusyId)||selectedProbeActive||!selectedHost||selectedHost.status==='disabled'" @click="probe"><Cpu :size="13"/>{{ selectedProbeActive?'探测进行中':runtimeProbeBusyId?'正在入队…':'探测所选主机' }}</button>
          <button v-if="selectedProbeActive&&selectedProbeAttempt" class="button danger small" :disabled="Boolean(runtimeProbeBusyId)" @click="cancelRuntimeProbe(selectedProbeAttempt)">{{ selectedProbeAttempt.status==='cancel_requested'?'等待安全取消':'取消探测' }}</button>
          <button class="button primary small" :disabled="!canRecommend" @click="recommend"><Gauge :size="13"/>{{ runtimeBusy==='recommend'?'计算中…':'生成当前快照推荐' }}</button>
        </div>
      </section>
      <p v-if="sectionErrors.runtimeHosts||sectionErrors.runtimeSnapshot||sectionErrors.runtimeProbeAttempts" class="platform-inline-error">{{ sectionErrors.runtimeHosts || sectionErrors.runtimeSnapshot || sectionErrors.runtimeProbeAttempts }}</p>
      <section v-if="selectedProbeAttempt" class="platform-card runtime-probe-attempt">
        <header><Cpu :size="18"/><div><strong>持久主机探测 · {{ selectedProbeAttempt.id }}</strong><small>{{ selectedProbeAttempt.trigger }} · {{ selectedProbeAttempt.progressMarker }} · 更新于 {{ dateTime(selectedProbeAttempt.updatedAt) }}</small></div><PlatformStatusPill :status="selectedProbeAttempt.status"/></header>
        <p v-if="selectedProbeActive">任务由 platform-maintenance worker 执行；刷新或离开页面不会丢失。取消请求会在文件分块检查点生效，不会由浏览器中断请求线程。</p>
        <p v-if="selectedProbeAttempt.heartbeatAt">worker 心跳：{{ dateTime(selectedProbeAttempt.heartbeatAt) }}</p>
        <p v-if="selectedProbeAttempt.finalSnapshot">终态快照：<code>{{ selectedProbeAttempt.finalSnapshot }}</code></p>
        <p v-if="runtimeProbeErrors[selectedHostId]" class="platform-inline-error">{{ runtimeProbeErrors[selectedHostId] }}</p>
      </section>
      <div class="platform-runtime-grid">
        <section class="platform-card runtime-summary">
          <header><Server :size="18"/><div><strong>事实 · 运行主机</strong><small>{{ runtimeHosts.length }} 个服务端登记主机</small></div></header>
          <div class="platform-dense-list runtime-host-list">
            <label v-for="host in runtimeHosts" :key="host.id" class="runtime-host-option" :class="{selected:selectedHostId===host.id}">
              <input v-model="selectedHostId" type="radio" name="runtime-host" :value="host.id"/>
              <span><strong>{{ host.name || host.key }}</strong><small>最近上报 {{ dateTime(host.lastSeenAt) }}<template v-if="runtimeProbeFor(host.id)"> · probe {{ runtimeProbeFor(host.id)?.status }}</template></small></span>
              <PlatformStatusPill :status="host.status"/><span class="platform-row-actions"><button class="button ghost tiny" type="button" @click.prevent="showDetail('运行主机',host.name||host.key,host)"><Eye :size="11"/>详情</button><button class="button ghost tiny" type="button" @click.prevent="openAction('host-status',host.id,`${host.status==='disabled'?'启用':'停用'}主机 · ${host.name}`,host.status==='disabled'?'启用后先进入 unavailable，必须重新 probe 才会成为 active。':'仍有 active deployment 时服务端会拒绝停用。',{enable:host.status==='disabled'})"><Power :size="11"/>{{ host.status==='disabled'?'启用':'停用' }}</button></span>
            </label>
            <p v-if="!runtimeHosts.length" class="platform-empty">尚无已登记主机。</p>
          </div>
        </section>
        <section class="platform-card runtime-summary">
          <header><Cpu :size="18"/><div><strong>事实 · 所选主机最近探测</strong><small>{{ snapshot ? dateTime(snapshot.capturedAt) : '本页尚未探测' }}</small></div><PlatformStatusPill v-if="snapshot" :status="snapshot.probeStatus"/></header>
           <dl v-if="snapshot">
            <div><dt>主机 / 快照</dt><dd><code>{{ selectedHost?.key }} / {{ snapshot.id }}</code></dd></div>
            <div><dt>CPU</dt><dd>{{ snapshot.cpuName || snapshot.cpuArchitecture }} · {{ snapshot.logicalCpuCount }} threads</dd></div>
            <div><dt>GPU</dt><dd>{{ snapshot.gpuCount }}</dd></div><div><dt>内存</dt><dd>{{ bytes(snapshot.memoryTotalBytes) }}</dd></div>
            <div><dt>磁盘可用</dt><dd>{{ bytes(snapshot.diskFreeBytes) }}</dd></div><div><dt>FFmpeg</dt><dd>{{ snapshot.ffmpegAvailable?'可用':'不可用' }}</dd></div>
            <div><dt>ComfyUI</dt><dd>{{ snapshot.comfyuiVersion || '未发现' }}</dd></div>
           </dl>
           <button v-if="snapshot" class="button ghost tiny" @click="showDetail('主机能力快照',`snapshot ${snapshot.revision}`,snapshot)"><Eye :size="11"/>查看完整快照事实</button>
          <details v-if="snapshot?.modelAvailability.length" class="runtime-model-facts" open>
            <summary>模型文件观测证据 · {{ snapshot.modelAvailability.length }}</summary>
            <article v-for="fact in snapshot.modelAvailability" :key="fact.id">
              <div><strong>{{ fact.status }} · artifact {{ fact.artifact || '未绑定' }}</strong><small>{{ fact.observationSource }} · {{ fact.reason }}</small></div>
              <dl>
                <div><dt>observed_size_bytes</dt><dd><code>{{ fact.observedSizeBytes ?? '未观测' }}</code><small v-if="fact.observedSizeBytes!==null">{{ bytes(fact.observedSizeBytes) }}</small></dd></div>
                <div><dt>observed_sha256</dt><dd><code>{{ fact.observedSha256 || '未观测' }}</code><button v-if="fact.observedSha256" class="button ghost tiny" type="button" @click="copyObservedHash(fact.observedSha256)"><Copy :size="11"/>复制完整 SHA</button></dd></div>
              </dl>
            </article>
          </details>
          <p v-else-if="selectedSnapshotState==='missing'" class="platform-empty">所选主机尚无快照（404）；请点击“探测所选主机”创建第一份服务端事实。</p>
          <p v-else-if="selectedSnapshotState==='error'" class="platform-inline-error">快照接口读取失败；这不是“尚未探测”，请修复接口错误后重试。</p>
          <p v-else class="platform-empty">选择主机后点击“探测所选主机”获取服务端事实。</p>
        </section>
      </div>
      <div class="resolved-fingerprint"><FileClock :size="15"/><span><strong>生效配置 · {{ resolved ? `${Object.keys(resolved.values).length} 项` : '未能解析' }}</strong><small>{{ resolved?.sha256 || '配置缺失' }}</small></span></div>
      <section v-if="readiness" class="readiness-grid"><article v-for="(item,key) in readiness.capabilities" :key="key"><span>{{ key }}</span><PlatformStatusPill :status="item.state"/><strong>{{ item.deploymentCount }} 个部署</strong><p>{{ item.reason }}</p></article></section>
      <details v-if="recommendation" class="platform-card recommendation" open>
        <summary><span><Gauge :size="16"/>推荐候选 · snapshot {{ recommendation.hostSnapshot }}</span><PlatformStatusPill :status="recommendation.status"/><ChevronDown :size="14"/></summary>
        <p v-if="recommendation.reason">{{ recommendation.reason }}</p>
        <div class="platform-dense-list"><article v-for="item in recommendation.candidates" :key="item.id"><div><strong>{{ item.capabilityKey }} · {{ item.deploymentKey }}</strong><small>{{ item.reasonCode }}<template v-if="item.workflowVersion"> · workflow {{ item.workflowVersion.slice(0,8) }}</template></small></div><PlatformStatusPill :status="item.status"/><span class="platform-row-actions"><button class="button ghost tiny" :disabled="!canDecide||item.status==='unavailable'" @click="openDecision(item,'accept')">接受</button><button class="button ghost tiny" :disabled="!canDecide||item.status==='unavailable'" @click="openDecision(item,'override')">覆盖</button><button class="button ghost tiny" :disabled="!canDecide" @click="openDecision(item,'reject')">拒绝</button></span></article><p v-if="!recommendation.candidates.length" class="platform-empty">当前快照没有候选项。</p></div>
        <small>接受/覆盖只创建草稿 revision 或草稿配方；切换主机或重新探测会立即使本推荐失效。</small>
      </details>
    </template>

    <template v-else>
      <section class="platform-toolbar"><div><strong>受管凭据与配置审计</strong><span>数据库只保存 AES-256-GCM 密文；浏览器与 API 永不回传秘密正文。</span></div><button v-if="auth.user?.isSuperuser" class="button primary small" @click="openSecret('create')"><Plus :size="13"/>新增受管凭据</button></section>
      <div class="platform-governance-grid">
        <section v-if="auth.user?.isSuperuser" class="platform-card"><header><KeyRound :size="18"/><div><strong>SecretBinding</strong><small>{{ secretBindings.length }} 个受管凭据；configured 不等于可调用</small></div></header><p v-if="sectionErrors.secretBindings" class="platform-inline-error">{{ sectionErrors.secretBindings }}</p><div class="platform-dense-list"><article v-for="item in secretBindings" :key="item.id"><div><strong>{{ item.label || item.key }}</strong><small>{{ item.key }} · {{ item.purpose }} · v{{ item.currentVersion || '—' }}</small></div><PlatformStatusPill :status="item.isActive&&item.configured?'configured':'blocked'"/><span class="platform-row-actions"><button class="button ghost tiny" @click="showDetail('受管凭据',item.label||item.key,item)"><Eye :size="11"/>元数据</button><button class="button ghost tiny" :disabled="!item.isActive" @click="openSecret('rotate',item)">轮换</button><button class="button ghost tiny" @click="openSecret('status',item)">{{ item.isActive?'停用':'启用' }}</button></span></article><p v-if="!secretBindings.length&&!sectionErrors.secretBindings" class="platform-empty">尚无受管凭据。秘密只能由超级管理员创建。</p></div></section>
        <section class="platform-card"><header><KeyRound :size="18"/><div><strong>绑定与生命周期</strong><small>{{ credentialRows.length }} 个 Provider / Deployment</small></div></header><div class="platform-dense-list"><article v-for="item in credentialRows" :key="item.owner"><div><strong>{{ item.owner }}</strong><small>{{ item.configured ? '已绑定受管凭据' : '尚未配置凭据' }} · owner {{ item.lifecycle }}<template v-if="item.configured&&!item.usable"> · 当前不可用</template></small></div><PlatformStatusPill :status="item.usable?'configured':'blocked'"/></article><p v-if="!credentialRows.length" class="platform-empty">尚无 Provider 或 Deployment。</p></div></section>
        <section class="platform-card lifecycle-audit-card">
          <header><ShieldCheck :size="18"/><div><strong>平台生命周期审计</strong><small>{{ filteredLifecycleAudit.length }} / {{ lifecycleAudit.length }} 条；staff 只读</small></div></header>
          <p v-if="sectionErrors.lifecycleAudit" class="platform-inline-error">{{ sectionErrors.lifecycleAudit }}</p>
          <div class="lifecycle-audit-filters">
            <label>对象类型<select v-model="lifecycleObjectFilter"><option value="">全部</option><option v-for="value in lifecycleObjectTypes" :key="value" :value="value">{{ value }}</option></select></label>
            <label>动作<select v-model="lifecycleActionFilter"><option value="">全部</option><option v-for="value in lifecycleActions" :key="value" :value="value">{{ value }}</option></select></label>
          </div>
          <div class="platform-dense-list">
            <article v-for="item in filteredLifecycleAudit" :key="item.id">
              <div><strong>{{ item.action }} · {{ item.objectType }}</strong><small>{{ item.actorUsername || 'system' }} · {{ item.reasonCode }} · {{ dateTime(item.createdAt) }}</small><small>object {{ item.objectId }} · {{ item.beforeSha256.slice(0,12) }} → {{ item.afterSha256.slice(0,12) }}</small></div>
              <button class="button ghost tiny" @click="showDetail('平台生命周期审计',`${item.action} · ${item.objectType}`,item)"><Eye :size="11"/>安全摘要</button>
            </article>
            <p v-if="!filteredLifecycleAudit.length&&!sectionErrors.lifecycleAudit" class="platform-empty">当前筛选条件下暂无生命周期审计记录。</p>
          </div>
        </section>
        <section class="platform-card"><header><FileClock :size="18"/><div><strong>配置 revisions</strong><small>{{ revisions.length }} 条，全部可查看</small></div></header><p v-if="sectionErrors.revisions" class="platform-inline-error">{{ sectionErrors.revisions }}</p><div class="platform-dense-list revisions"><article v-for="item in revisions" :key="item.id"><div><strong>{{ item.scopeType }} · revision {{ item.revision }}</strong><small>{{ item.changeSummary || '无变更说明' }} · {{ dateTime(item.createdAt) }}<template v-if="item.activationError"> · {{ item.activationError }}</template><template v-else-if="(item.activationState==='pending_activation'||item.activationState==='failed')&&!item.activationAvailable"> · 需由重启后的新进程激活</template></small></div><PlatformStatusPill :status="item.activationState||item.status"/><span class="platform-row-actions"><button class="button ghost tiny" @click="showDetail('配置 Revision',`revision ${item.revision}`,item)"><Eye :size="11"/>详情</button><button v-if="item.status==='draft'" class="button primary tiny" @click="openAction('publish-revision',item.id,`发布 revision ${item.revision}`, '服务端会复验 stale base 与秘密绑定权限。')">发布</button><button v-if="(item.activationState==='pending_activation'||item.activationState==='failed')&&item.activationAvailable" class="button primary tiny" @click="openAction('activate-revision',item.id,`激活 revision ${item.revision}`, '重启后的新进程将复验并激活该 revision。')">激活</button></span></article><p v-if="!revisions.length&&!sectionErrors.revisions" class="platform-empty">尚无配置 revision。</p></div></section>
        <section v-if="auth.user?.isSuperuser" class="platform-card"><header><ShieldCheck :size="18"/><div><strong>密钥审计</strong><small>{{ secretAudit.length }} 条不可变记录，全部可查看</small></div></header><p v-if="sectionErrors.secretAudit" class="platform-inline-error">{{ sectionErrors.secretAudit }}</p><div class="platform-dense-list"><article v-for="item in secretAudit" :key="item.id"><div><strong>{{ item.action }} · {{ item.bindingKey }}</strong><small>{{ item.actorUsername || 'system' }} · {{ item.reason || '无敏感正文' }} · {{ dateTime(item.createdAt) }}</small></div><button class="button ghost tiny" @click="showDetail('密钥审计',`${item.action} · ${item.bindingKey}`,item)"><Eye :size="11"/>详情</button></article><p v-if="!secretAudit.length&&!sectionErrors.secretAudit" class="platform-empty">暂无密钥审计记录。</p></div></section>
      </div>
    </template>

    <BaseModal :open="editorOpen" :dismissible="!saving" :title="`${editorId?'编辑':'新增'} ${editorResourceLabel}`" eyebrow="SCHEMA-DRIVEN · SERVER VALIDATED" width="980px" @close="editorOpen=false">
      <div class="registry-editor-modal"><p>常用参数已拆分为类型化字段；必填、默认值、影响和前置依赖会就近提示。折叠的高级 JSON 只承载驱动或合同扩展，服务端仍执行最终校验且不会本地伪成功。<template v-if="editorId&&['provider','deployment'].includes(editorResource)"> 受管凭据不会回显；默认保持现有绑定。</template></p><label v-if="auth.user?.isSuperuser&&['provider','deployment'].includes(editorResource)" class="editor-credential">受管凭据<select v-model="editorCredentialId"><option :value="editorId?'__keep__':''">{{ editorId?'保持现有绑定':'不绑定凭据' }}</option><option v-if="editorId" value="__clear__">解除现有绑定</option><option v-for="secret in secretBindings.filter((row)=>row.isActive&&row.configured)" :key="secret.id" :value="secret.id">{{ secret.label || secret.key }} · v{{ secret.currentVersion }}</option></select><small>凭据正文永不进入本表单；这里只保存 SecretBinding 引用。</small></label><RegistryResourceEditor v-model="editorPayload" :resource="editorResource" :dependencies="editorDependencies" :disabled="saving" @validity="editorValidity=$event"/><div v-if="editorError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ editorError }}</p></div></div>
      <template #footer><button class="button ghost" :disabled="saving" @click="editorOpen=false">取消</button><button class="button primary" :disabled="saving||!editorValidity.valid" @click="submitEditor">{{ saving?'服务端保存中…':editorId?'提交更新':'提交到服务端' }}</button></template>
    </BaseModal>
    <BaseModal :open="detailOpen" :dismissible="!saving" :title="detailTitle" :eyebrow="`${detailKind} · SERVER FACTS`" @close="detailOpen=false">
      <div class="registry-detail"><p>以下内容来自当前服务端记录。发布、校验、烟测与审计字段刷新后仍可复核；秘密正文、密文与 nonce 不会返回浏览器。</p><pre>{{ pretty(detailPayload) }}</pre></div>
      <template #footer><button class="button primary" @click="detailOpen=false">关闭</button></template>
    </BaseModal>

    <BaseModal :open="workflowExportOpen" :dismissible="!workflowExportBusyId" title="在 ComfyUI 中打开（下载导入）" eyebrow="READ-ONLY · NO DIRECT BROWSER INJECTION" width="720px" @close="closeWorkflowExport">
      <div class="workflow-export-review">
        <div v-if="workflowExportNeedsExplicitDeployment&&!workflowExport" class="workflow-export-deployment"><label>ComfyUI deployment *<select v-model="workflowExportDeploymentId" :disabled="Boolean(workflowExportBusyId)" @change="selectWorkflowExportDeployment"><option value="">请选择 active 且绑定兼容的部署</option><option v-for="item in workflowExportDeploymentOptions" :key="item.id" :value="item.id">{{ item.name || item.key }} · {{ item.runtimeHost ? `host ${item.runtimeHost.slice(0,8)}` : 'remote' }}</option></select></label><small v-if="workflowExportDeploymentOptions.length">未校验草稿没有持久化节点目录证据；必须显式选择，系统不会回退默认 ComfyUI。</small><div v-else class="platform-alert danger"><AlertTriangle :size="15"/><p>没有 active 且与该 workflow Provider/模型版本精确兼容的 ComfyUI deployment；请先完成部署配置。</p></div></div>
        <div v-else-if="workflowExportVersion?.validatedDeployment&&!workflowExport" class="platform-alert info"><ShieldCheck :size="15"/><p>该版本已绑定 validated deployment <code>{{ workflowExportVersion.validatedDeployment }}</code>；导出只能读取此部署，不能临时切换。</p></div>
        <div v-if="workflowExportBusyId" class="platform-loading"><LoaderCircle class="spin" :size="20"/>正在从绑定的 ComfyUI deployment 读取 object_info 并生成可导入 UI 画布…</div>
        <div v-else-if="workflowExportError" class="platform-alert danger"><AlertTriangle :size="15"/><div><strong>无法生成可导入画布</strong><p>{{ workflowExportError }}</p></div></div>
        <template v-else-if="workflowExport">
          <p>{{ workflowExport.openReason }}</p>
          <div class="platform-alert warning"><AlertTriangle :size="15"/><p>浏览器不能把本页数据直接注入另一个 ComfyUI 页面。本操作只下载服务端转换且校验的 UI 画布 JSON，不会提交生产任务。</p></div>
          <dl><div><dt>文件</dt><dd>{{ workflowExport.filename }}</dd></div><div><dt>节点目录部署</dt><dd>{{ workflowExport.deploymentName }} · <code>{{ workflowExport.deploymentId }}</code></dd></div><div><dt>部署来源</dt><dd>{{ workflowExport.deploymentSource==='validated'?'持久化验证证据':'草稿显式选择' }}</dd></div><div><dt>打开方式</dt><dd>download_import</dd></div><div><dt>直接打开</dt><dd>不支持（direct_open_supported=false）</dd></div><div><dt>原图 SHA-256</dt><dd><code>{{ workflowExport.graphSha256 }}</code></dd></div></dl>
          <ol><li>点击“下载 UI 画布 JSON”。</li><li>打开你已配置的 ComfyUI。</li><li>将 JSON 拖入画布，或从 Workflow 菜单打开该文件。</li></ol>
        </template>
      </div>
      <template #footer><button class="button ghost" :disabled="Boolean(workflowExportBusyId)" @click="closeWorkflowExport">关闭</button><button v-if="!workflowExport" class="button primary" :disabled="Boolean(workflowExportBusyId)||(workflowExportNeedsExplicitDeployment&&(!workflowExportDeploymentId||!workflowExportDeploymentOptions.length))" @click="loadWorkflowExport"><RefreshCw :size="13"/>{{ workflowExportBusyId?'读取中…':'读取绑定部署并生成' }}</button><button v-else class="button primary" :disabled="!workflowExport.importReady||Boolean(workflowExportBusyId)" @click="downloadWorkflowCanvas"><Download :size="13"/>下载 UI 画布 JSON</button></template>
    </BaseModal>

    <BaseModal :open="actionOpen" :dismissible="!saving" :title="actionTitle" eyebrow="REVIEW BEFORE COMMIT" @close="closeAction()">
      <div class="registry-action-review"><p>{{ actionDescription }}</p><dl><div><dt>对象 ID</dt><dd><code>{{ actionId || 'new system revision' }}</code></dd></div><div v-if="actionReferenceId"><dt>证据 / 引用</dt><dd><code>{{ actionReferenceId }}</code></dd></div></dl><pre v-if="actionReviewPayload" class="action-review-json">{{ pretty(actionReviewPayload) }}</pre><label v-if="actionReasonRequired">审计原因代码<select v-model="actionReasonCode"><option value="">请选择稳定原因代码</option><option value="planned_change">计划变更</option><option value="configuration_correction">配置纠正</option><option value="incident_response">故障处置</option><option value="lifecycle_cleanup">生命周期清理</option><option value="validation_completed">验证完成</option></select></label><label v-if="actionKind==='deployment-smoke'">烟测 operation<select v-model="actionOperation"><option v-for="operation in actionSmokeOperations" :key="operation" :value="operation">{{ operation }}</option></select></label><div v-if="actionError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ actionError }}</p></div></div>
      <template #footer><button class="button ghost" :disabled="saving" @click="closeAction()">取消</button><button class="button" :class="destructiveActions.has(actionKind)?'danger':'primary'" :disabled="saving||(actionKind==='deployment-smoke'&&!actionOperation)||(actionReasonRequired&&!actionReasonCode)" @click="runAction">{{ saving?'服务端处理中…':actionConfirmLabel }}</button></template>
    </BaseModal>

    <BaseModal :open="Boolean(smokeResolutionDeployment)" :dismissible="!smokeResolutionSaving" title="管理员消歧部署烟测" eyebrow="APPEND-ONLY SMOKE RESOLUTION" width="700px" @close="closeSmokeResolution">
      <div v-if="smokeResolutionDeployment?.latestSmoke?.unresolvedSubmission" class="registry-action-review">
        <p>请先核对 Provider 控制台和账单。提交只追加不可变消歧证据，不会把此结论计为 smoke passed；完成后必须重新执行新的烟测。</p>
        <dl><div><dt>Deployment</dt><dd>{{ smokeResolutionDeployment.name || smokeResolutionDeployment.key }}</dd></div><div><dt>Operation</dt><dd>{{ smokeResolutionDeployment.latestSmoke.unresolvedSubmission.operation }}</dd></div><div><dt>请求 SHA256</dt><dd><code>{{ smokeResolutionDeployment.latestSmoke.unresolvedSubmission.requestSha256 }}</code></dd></div><div><dt>目标 SHA256</dt><dd><code>{{ smokeResolutionDeployment.latestSmoke.unresolvedSubmission.targetSha256 }}</code></dd></div><div><dt>request ID 证据</dt><dd><code>{{ smokeResolutionDeployment.latestSmoke.unresolvedSubmission.providerRequestIdSha256 || '尚无 hash' }}</code></dd></div></dl>
        <label>核验结论<select v-model="smokeResolutionOutcome"><option value="confirmed_not_submitted">确认远端未提交</option><option value="confirmed_remote_submitted">确认远端已提交</option><option value="confirmed_remote_failed">确认远端失败</option></select></label>
        <label>核验说明（至少 8 个字符）<textarea v-model="smokeResolutionNote" rows="4" maxlength="4000" placeholder="说明查阅的控制台、账单时间范围和可复核结论"></textarea></label>
        <label v-if="smokeResolutionOutcome!=='confirmed_not_submitted'">Provider request ID（可选，仅提交、不回显）<input v-model="smokeResolutionProviderRequestId" type="password" maxlength="191" autocomplete="off"/></label>
        <div v-if="smokeResolutionOutcome!=='confirmed_not_submitted'" class="platform-form-grid"><label>实际费用（可选）<input v-model="smokeResolutionActualCost" inputmode="decimal" placeholder="0"/></label><label>币种（费用大于 0 必填）<input v-model="smokeResolutionCurrency" maxlength="12" placeholder="USD"/></label></div>
        <div v-if="smokeResolutionValidation" class="platform-alert warning"><AlertTriangle :size="15"/><p>{{ smokeResolutionValidation }}</p></div>
        <div v-if="smokeResolutionError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ smokeResolutionError }}</p></div>
      </div>
      <template #footer><button class="button ghost" :disabled="smokeResolutionSaving" @click="closeSmokeResolution">取消</button><button class="button primary" :disabled="smokeResolutionSaving||Boolean(smokeResolutionValidation)" @click="submitSmokeResolution">{{ smokeResolutionSaving?'提交中…':'追加消歧记录' }}</button></template>
    </BaseModal>

    <BaseModal :open="decisionOpen" :dismissible="!Boolean(runtimeBusy)" :title="decisionKind==='accept'?'接受推荐':decisionKind==='override'?'覆盖并接受推荐':'拒绝推荐'" eyebrow="RUNTIME RECOMMENDATION DECISION" @close="closeDecision()">
      <div class="registry-action-review"><p>候选：<strong>{{ decisionCandidate?.capabilityKey }} · {{ decisionCandidate?.deploymentKey }}</strong></p><p>提交前会再次核对当前主机、快照、推荐有效期与候选证据；接受或覆盖只创建草稿，不会自动发布或激活。</p><label v-if="decisionKind==='override'">覆盖 configuration JSON<textarea v-model="decisionOverrideText" rows="8" spellcheck="false"/></label><label>审计说明<textarea v-model="decisionReason" rows="3"/></label><div v-if="decisionError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ decisionError }}</p></div></div>
      <template #footer><button class="button ghost" :disabled="Boolean(runtimeBusy)" @click="closeDecision()">取消</button><button class="button primary" :disabled="Boolean(runtimeBusy)||!decisionReason.trim()" @click="submitDecision">{{ runtimeBusy==='decision'?'服务端处理中…':'确认提交决策' }}</button></template>
    </BaseModal>

    <BaseModal :open="secretOpen" :dismissible="!saving" :title="secretMode==='create'?'新增受管凭据':secretMode==='rotate'?'轮换受管凭据':'变更凭据状态'" eyebrow="SUPER ADMIN · ENCRYPTED" @close="closeSecret()">
      <div class="secret-form"><p v-if="secretTarget">目标：<strong>{{ secretTarget.label || secretTarget.key }}</strong></p><template v-if="secretMode==='create'"><label>唯一 key<input v-model="secretDraft.key" autocomplete="off" placeholder="provider.deepseek.primary"/></label><label>显示名称<input v-model="secretDraft.label" autocomplete="off"/></label><label>用途<input v-model="secretDraft.purpose" autocomplete="off"/></label></template><label v-if="secretMode!=='status'">秘密正文<input v-model="secretDraft.secretValue" type="password" autocomplete="new-password"/></label><p v-else>确认后将{{ secretDraft.isActive?'启用':'停用' }}该绑定；停用会让引用它的运行能力 fail closed。</p><label>审计说明<textarea v-model="secretDraft.reason" rows="3"/></label><div v-if="secretError" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ secretError }}</p></div></div>
      <template #footer><button class="button ghost" :disabled="saving" @click="closeSecret()">取消</button><button class="button primary" :disabled="saving||!secretDraft.reason.trim()||(secretMode!=='status'&&!secretDraft.secretValue)||(secretMode==='create'&&(!secretDraft.key||!secretDraft.label))" @click="submitSecret">确认</button></template>
    </BaseModal>
  </div>
</template>

<style scoped>
.registry-title-link{display:block;margin:0;padding:0;border:0;background:transparent;color:inherit;text-align:left;cursor:pointer}.registry-title-link:hover strong,.registry-title-link:focus-visible strong{color:#f4b04d;text-decoration:underline;text-underline-offset:3px}.registry-title-link:focus-visible{outline:2px solid #d99336;outline-offset:3px;border-radius:3px}
.runtime-probe-attempt{margin-bottom:14px}.runtime-probe-attempt p{margin:8px 0 0;color:#9ca8b7}.runtime-model-facts{margin-top:12px;border-top:1px solid #252b34;padding-top:10px}.runtime-model-facts summary{cursor:pointer;color:#cbd5e1;font-size:11px}.runtime-model-facts article{display:grid;gap:8px;padding:10px 0;border-bottom:1px solid #202630}.runtime-model-facts article>div{display:grid;gap:3px}.runtime-model-facts small{color:#7f8a98}.runtime-model-facts dl{display:grid;gap:7px;margin:0}.runtime-model-facts dl div{display:grid;grid-template-columns:145px minmax(0,1fr);gap:8px;align-items:start}.runtime-model-facts dt{color:#788391;font-size:10px}.runtime-model-facts dd{display:flex;flex-wrap:wrap;gap:7px;align-items:center;min-width:0;margin:0}.runtime-model-facts code{max-width:100%;overflow-wrap:anywhere;user-select:all;color:#dbe5ef;font-size:10px}
.registry-editor-modal{display:grid;gap:12px}.registry-editor-modal>p{margin:0;color:#99a4b1;line-height:1.55}.registry-editor-modal .editor-credential small{color:#74808d;font-size:10px}
.workflow-export-review{display:grid;gap:12px}.workflow-export-review>p{margin:0;color:#aeb7c3;line-height:1.6}.workflow-export-review dl{display:grid;gap:7px;margin:0}.workflow-export-review dl div{display:grid;grid-template-columns:120px minmax(0,1fr);gap:10px}.workflow-export-review dt{color:#788391}.workflow-export-review dd{min-width:0;margin:0;overflow-wrap:anywhere;color:#c8d1db}.workflow-export-review ol{margin:0;padding-left:22px;color:#aeb7c3;line-height:1.8}
.workflow-export-deployment{display:grid;gap:8px;padding:12px;border:1px solid var(--border);border-radius:8px;background:#0b0e12}.workflow-export-deployment label{display:grid;gap:6px;color:#aeb7c3;font-size:10px}.workflow-export-deployment select{width:100%;min-height:38px;padding:8px 10px;border:1px solid var(--border);border-radius:7px;background:#070a0e;color:#c8d1db}.workflow-export-deployment>small{color:#788391;line-height:1.55}
.lifecycle-audit-filters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding:10px 12px;border-bottom:1px solid #252b34}.lifecycle-audit-filters label{display:grid;gap:5px;color:#788391;font-size:9px}.lifecycle-audit-filters select{min-width:0;padding:7px 9px;border:1px solid #2a3039;border-radius:7px;background:#0b0e12;color:#cbd5e1}.lifecycle-audit-card .platform-dense-list{max-height:420px;overflow:auto}
.secret-form,.registry-action-review{display:grid;gap:15px}.secret-form label,.editor-credential,.registry-action-review label{display:grid;gap:7px;color:#aeb7c3}.secret-form input,.secret-form textarea,.editor-credential select,.registry-action-review textarea,.registry-action-review select{width:100%;border:1px solid #2a3039;border-radius:8px;background:#0b0e12;color:#f7f8fa;padding:10px 12px}.secret-form p,.registry-action-review p,.registry-detail p{color:#aeb7c3}.registry-detail pre,.action-review-json{max-height:min(65vh,680px);overflow:auto;margin:0;padding:14px;border:1px solid #252b34;border-radius:9px;background:#090c10;color:#cbd5e1;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;word-break:break-word}.action-review-json{max-height:320px}.registry-action-review dl{display:grid;gap:8px;margin:0}.registry-action-review dl div{display:grid;grid-template-columns:100px 1fr;gap:12px}.registry-action-review dt{color:#788391}.registry-action-review dd{margin:0;overflow-wrap:anywhere}
</style>
