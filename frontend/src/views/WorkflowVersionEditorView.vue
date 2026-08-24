<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { Handle, Position, VueFlow, type Connection } from '@vue-flow/core'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import { AlertTriangle, ArrowLeft, Check, Download, FileJson2, GitBranch, LoaderCircle, Play, Plus, RefreshCw, Save, Send, Upload, X } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import PlatformStatusPill from '@/components/PlatformStatusPill.vue'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { analyzeComfyWorkflow, buildApiGraph, parseComfyLiteralInput, type GraphProjectionEdge, type GraphProjectionNode, type WorkflowGraphAnalysis } from '@/services/comfyWorkflowGraph'
import { platformApi } from '@/services/platformApi'
import type {
  JsonObject,
  LegacyWorkflowTemplate,
  ModelArtifact,
  ModelDeployment,
  ModelProfileVersion,
  NodeMetadataItem,
  ProviderProfile,
  WorkflowModelRequirement,
  WorkflowSemanticBinding,
  WorkflowSmokeAttempt,
  WorkflowTemplateVersion,
} from '@/services/platformTypes'

interface CanvasNodeData { classType: string; label: string; inputs: JsonObject; outputCount: number; source: JsonObject }
interface SemanticBindingDraft extends Omit<WorkflowSemanticBinding, 'id'> { key: string }
interface ModelRequirementDraft extends Omit<WorkflowModelRequirement, 'id'> { key: string }
const route = useRoute(); const router = useRouter(); const workflowId = computed(() => String(route.params.workflowVersionId || 'new'))
const loading = ref(false); const busy = ref(''); const error = ref(''); const notice = ref('')
const templates = ref<LegacyWorkflowTemplate[]>([]); const deployments = ref<ModelDeployment[]>([]); const providers = ref<ProviderProfile[]>([]); const modelProfiles = ref<ModelProfileVersion[]>([]); const modelArtifacts = ref<ModelArtifact[]>([])
const templateId = ref(''); const deploymentId = ref(''); const providerId = ref(''); const modelProfileId = ref('')
const metadata = ref<NodeMetadataItem[]>([]); const metadataComplete = ref(false); const metadataCatalogSha256 = ref(''); const sourceText = ref('{}'); const sourceValue = ref<unknown>({}); const analysis = ref<WorkflowGraphAnalysis>(analyzeComfyWorkflow({})); const canvasNodes = ref<any[]>([]); const canvasEdges = ref<any[]>([])
const savedVersion = ref<WorkflowTemplateVersion | null>(null); const dirty = ref(false); const selectedNodeId = ref(''); const rawOpen = ref(false); const contractResult = ref<JsonObject | null>(null); const smokeResult = ref<JsonObject | null>(null)
const inputContractText = ref('{}'); const semanticBindings = ref<SemanticBindingDraft[]>([]); const modelRequirements = ref<ModelRequirementDraft[]>([])
const smokeAttempt = ref<WorkflowSmokeAttempt | null>(null); const smokeAttemptError = ref(''); const smokePollInFlight = ref(false); let smokePollTimer: number | undefined
const validationRequestFailed = ref(false); const smokeRequestFailed = ref(false); const evidenceRefreshError = ref('')
const publishOpen = ref(false)
const selectedNode = computed(() => canvasNodes.value.find((item) => item.id === selectedNodeId.value) as { id: string; data: CanvasNodeData } | undefined)
const compatibleDeployments = computed(() => deployments.value.filter((item) => item.status === 'active'
  && item.providerDriver === 'comfyui'
  && providers.value.some((provider) => provider.id === item.providerProfileId && provider.isActive && provider.driverType === 'comfyui')
  && modelProfiles.value.some((profile) => profile.id === item.profileVersion && profile.status === 'published')
  && (!providerId.value || item.providerProfileId === providerId.value)
  && (!modelProfileId.value || item.profileVersion === modelProfileId.value)))
const filteredModelArtifacts = computed(() => modelArtifacts.value.filter((item) => !modelProfileId.value || item.profileVersion === modelProfileId.value))
const semanticValueTypes = ['string', 'integer', 'number', 'boolean', 'enum', 'asset_image', 'asset_video', 'asset_audio', 'model_artifact']
let draftSequence = 0
const draftKey = () => `draft-${Date.now()}-${++draftSequence}`
const inputContractState = computed<{ value: JsonObject | null; error: string }>(() => {
  try {
    const value = JSON.parse(inputContractText.value) as unknown
    if (!value || typeof value !== 'object' || Array.isArray(value)) return { value: null, error: 'input_contract 必须是 JSON 对象' }
    return { value: value as JsonObject, error: '' }
  } catch {
    return { value: null, error: 'input_contract 不是合法 JSON' }
  }
})
const inputContractField = (path: string): unknown => {
  let value: unknown = inputContractState.value.value
  for (const segment of path.split('.')) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
    value = (value as JsonObject)[segment]
  }
  return value
}
const setInputContractField = (path: string, value: unknown) => {
  if (!inputContractState.value.value) return
  const next = structuredClone(inputContractState.value.value)
  const segments = path.split('.'); let target = next
  segments.forEach((segment, index) => {
    if (index === segments.length - 1) {
      if (value === undefined || value === '') delete target[segment]
      else target[segment] = value
      return
    }
    if (!target[segment] || typeof target[segment] !== 'object' || Array.isArray(target[segment])) target[segment] = {}
    target = target[segment] as JsonObject
  })
  inputContractText.value = JSON.stringify(next, null, 2)
  markContractDirty()
}
const inputContractTextValue = (path: string) => String(inputContractField(path) ?? '')
const inputContractModes = computed(() => Array.isArray(inputContractField('h3_modes')) ? (inputContractField('h3_modes') as unknown[]).join(', ') : '')
const updateInputContractModes = (event: Event) => setInputContractField('h3_modes', (event.target as HTMLInputElement).value.split(',').map((item) => item.trim()).filter(Boolean))
const contractErrors = computed(() => {
  const failures: string[] = []
  if (inputContractState.value.error) failures.push(inputContractState.value.error)
  const bindingKeys = new Set<string>()
  semanticBindings.value.forEach((item, index) => {
    if (!item.semanticKey.trim() || !item.nodeId.trim() || !item.inputName.trim()) failures.push(`semantic binding ${index + 1} 缺少 key/node/input`)
    if (!semanticValueTypes.includes(item.valueType)) failures.push(`semantic binding ${index + 1} 的 value_type 无效`)
    const key = `${item.semanticKey}\u0000${item.nodeId}\u0000${item.inputName}`
    if (bindingKeys.has(key)) failures.push(`semantic binding ${index + 1} 重复`)
    bindingKeys.add(key)
  })
  const requirementKeys = new Set<string>()
  modelRequirements.value.forEach((item, index) => {
    if (!item.role.trim() || !item.alias.trim()) failures.push(`model requirement ${index + 1} 缺少 role/alias`)
    const key = `${item.role}\u0000${item.alias}`
    if (requirementKeys.has(key)) failures.push(`model requirement ${index + 1} 重复`)
    requirementKeys.add(key)
    const artifact = item.artifactId ? modelArtifacts.value.find((row) => row.id === item.artifactId) : null
    if (item.artifactId && !artifact) failures.push(`model requirement ${index + 1} 引用了不存在的 artifact`)
    if (artifact && modelProfileId.value && artifact.profileVersion !== modelProfileId.value) failures.push(`model requirement ${index + 1} 的 artifact 不属于所选模型版本`)
    if (item.expectedSha256 && !/^[0-9a-f]{64}$/i.test(item.expectedSha256)) failures.push(`model requirement ${index + 1} 的 SHA256 无效`)
  })
  return [...new Set(failures)]
})
const smokeTerminalStatuses = new Set(['succeeded', 'failed', 'cancelled', 'uncertain'])
const smokeActive = computed(() => Boolean(smokeAttempt.value && !smokeTerminalStatuses.has(smokeAttempt.value.status)))
const smokeCancellable = computed(() => Boolean(smokeAttempt.value && ['queued', 'submitting', 'running'].includes(smokeAttempt.value.status)))
const message = (cause: unknown) => apiErrorSummary(cause, '请求失败')
const asProjectionNodes = (): GraphProjectionNode[] => canvasNodes.value.map((item) => ({ id: String(item.id), classType: String(item.data.classType), label: String(item.data.label), position: { x: Number(item.position.x || 0), y: Number(item.position.y || 0) }, inputs: item.data.inputs as JsonObject, outputCount: Number(item.data.outputCount || 1), source: item.data.source as JsonObject }))
const asProjectionEdges = (): GraphProjectionEdge[] => canvasEdges.value.map((item) => ({ id: String(item.id), source: String(item.source), target: String(item.target), sourceOutput: Number(String(item.sourceHandle || 'output:0').split(':')[1] || 0), targetInput: String(item.targetHandle || '').replace(/^input:/, '') })).filter((item) => item.targetInput)
const currentGraph = computed(() => buildApiGraph(asProjectionNodes(), asProjectionEdges()))
const liveAnalysis = computed(() => analyzeComfyWorkflow(currentGraph.value, metadataComplete.value ? metadata.value : undefined))
const blockers = computed(() => [...new Set([...(analysis.value.format === 'ui' ? analysis.value.blockers : []), ...liveAnalysis.value.blockers, ...(!metadataComplete.value ? ['node-metadata 尚未完整读取，不能证明全部节点兼容'] : []), ...contractErrors.value])])
const warnings = computed(() => [...new Set([...analysis.value.warnings, ...liveAnalysis.value.warnings])])
const saveBlocked = computed(() => analysis.value.format === 'unknown' || (analysis.value.format === 'ui' && analysis.value.blockers.length > 0) || !templateId.value || !deploymentId.value || !metadataComplete.value || contractErrors.value.length > 0 || !Object.keys(currentGraph.value).length)
const publishBlocked = computed(() => dirty.value || blockers.value.length > 0 || !metadataComplete.value || validationRequestFailed.value || smokeRequestFailed.value || Boolean(evidenceRefreshError.value) || !savedVersion.value || savedVersion.value.validationState !== 'passed' || savedVersion.value.smokeTestState !== 'passed')
const validationDisplayState = computed(() => validationRequestFailed.value || evidenceRefreshError.value ? 'error' : savedVersion.value?.validationState || '')
const smokeDisplayState = computed(() => smokeRequestFailed.value || evidenceRefreshError.value ? 'error' : savedVersion.value?.smokeTestState || '')
const nodeMetadata = computed(() => new Map(metadata.value.map((item) => [item.classType, item])))
const selectedInputs = computed(() => selectedNode.value ? Object.entries(selectedNode.value.data.inputs) : [])
const isConnectionValue = (value: unknown) => Array.isArray(value) && value.length === 2 && canvasNodes.value.some((item) => String(item.id) === String(value[0])) && Number.isInteger(Number(value[1]))
const formatInput = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value)
const inputRows = (value: unknown) => Object.entries(value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}).map(([name, inputValue]) => ({ name, value: inputValue }))
const toCanvas = (value: WorkflowGraphAnalysis) => { canvasNodes.value = value.nodes.map((item) => ({ id: item.id, type: 'comfy', position: item.position, data: { classType: item.classType, label: item.label, inputs: { ...item.inputs }, outputCount: item.outputCount, source: { ...item.source } } as CanvasNodeData })); canvasEdges.value = value.edges.map((item) => ({ id: item.id, source: item.source, target: item.target, sourceHandle: `output:${item.sourceOutput}`, targetHandle: `input:${item.targetInput}`, type: 'smoothstep' })); selectedNodeId.value = canvasNodes.value[0]?.id || '' }
const applyValue = (value: unknown, markDirty = false) => { sourceValue.value = value; analysis.value = analyzeComfyWorkflow(value, metadataComplete.value ? metadata.value : undefined); toCanvas(analysis.value); dirty.value = markDirty }
const applySourceText = () => { error.value = ''; try { const parsed = JSON.parse(sourceText.value); applyValue(parsed, true); notice.value = `已投影 ${analysis.value.nodes.length} 个节点；尚未写入服务端。` } catch (cause) { error.value = `JSON 解析失败：${message(cause)}` } }
const chooseSourceFile = async (event: Event) => { const file = (event.target as HTMLInputElement).files?.[0]; if (!file) return; sourceText.value = await file.text(); applySourceText() }
const markContractDirty = () => { dirty.value = true }
const setContractDrafts = (version?: WorkflowTemplateVersion | null) => {
  inputContractText.value = JSON.stringify(version?.inputContract || {
    schema_version: 2,
    capability: '',
    compiler: { id: 'generic.comfy', version: 1 },
    parameters: {},
    outputs: {},
    smoke_inputs: {},
  }, null, 2)
  semanticBindings.value = (version?.semanticBindings || []).map(({ semanticKey, nodeId, inputName, valueType, isRequired }) => ({ key: draftKey(), semanticKey, nodeId, inputName, valueType, isRequired }))
  modelRequirements.value = (version?.modelRequirements || []).map(({ role, alias, artifactId, expectedFilename, expectedSha256, isRequired }) => ({ key: draftKey(), role, alias, artifactId, expectedFilename, expectedSha256, isRequired }))
}
const addSemanticBinding = () => { semanticBindings.value.push({ key: draftKey(), semanticKey: '', nodeId: selectedNodeId.value, inputName: '', valueType: 'string', isRequired: true }); markContractDirty() }
const removeSemanticBinding = (key: string) => { semanticBindings.value = semanticBindings.value.filter((item) => item.key !== key); markContractDirty() }
const addModelRequirement = () => { modelRequirements.value.push({ key: draftKey(), role: '', alias: '', artifactId: '', expectedFilename: '', expectedSha256: '', isRequired: true }); markContractDirty() }
const removeModelRequirement = (key: string) => { modelRequirements.value = modelRequirements.value.filter((item) => item.key !== key); markContractDirty() }
const selectRequirementArtifact = (item: ModelRequirementDraft) => {
  const artifact = modelArtifacts.value.find((row) => row.id === item.artifactId)
  if (artifact) {
    item.role = artifact.role
    item.expectedFilename = artifact.filename
    item.expectedSha256 = artifact.sha256
  }
  markContractDirty()
}
const loadMetadata = async () => {
  const requestedDeploymentId = deploymentId.value
  metadata.value = []; metadataComplete.value = false; metadataCatalogSha256.value = ''
  if (!requestedDeploymentId) return
  busy.value = 'metadata'; error.value = ''
  try {
    const result = await platformApi.getAllNodeMetadata(requestedDeploymentId)
    if (deploymentId.value !== requestedDeploymentId) return
    metadata.value = result.items; metadataComplete.value = true; metadataCatalogSha256.value = result.catalogSha256
    const updated = analyzeComfyWorkflow(currentGraph.value, metadata.value)
    analysis.value = { ...updated, format: analysis.value.format, blockers: analysis.value.format === 'ui' ? analysis.value.blockers : updated.blockers, warnings: [...analysis.value.warnings, ...updated.warnings] }
    canvasNodes.value = canvasNodes.value.map((node) => ({ ...node, data: { ...node.data, outputCount: nodeMetadata.value.get(node.data.classType)?.outputCount || node.data.outputCount, label: nodeMetadata.value.get(node.data.classType)?.displayName || node.data.label } }))
    notice.value = `已完整读取 ${result.totalCount} 个动态节点定义 · catalog ${result.catalogSha256.slice(0, 12)}。`
  } catch (cause) {
    if (deploymentId.value === requestedDeploymentId) {
      error.value = message(cause); metadata.value = []; metadataComplete.value = false; metadataCatalogSha256.value = ''
    }
  } finally { if (busy.value === 'metadata') busy.value = '' }
}
const clearSmokePoll = () => { if (smokePollTimer) window.clearTimeout(smokePollTimer); smokePollTimer = undefined }
const scheduleSmokePoll = () => {
  clearSmokePoll()
  if (smokeActive.value) smokePollTimer = window.setTimeout(() => void refreshSmokeAttempt(), 2500)
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
  } catch (cause) { smokeAttemptError.value = `烟测任务刷新失败：${message(cause)}` }
  finally { smokePollInFlight.value = false; if (smokeActive.value) scheduleSmokePoll() }
}
const restoreSmokeAttempt = async () => {
  clearSmokePoll(); smokeAttempt.value = null; smokeAttemptError.value = ''
  if (!savedVersion.value || !deploymentId.value) return
  try {
    const attempts = await platformApi.listWorkflowSmokeAttempts({ workflow_version: savedVersion.value.id, deployment: deploymentId.value })
    smokeAttempt.value = attempts[0] || null
    if (smokeActive.value) scheduleSmokePoll()
  } catch (cause) { smokeAttemptError.value = `烟测任务恢复失败：${message(cause)}` }
}
const changeDeployment = async () => {
  const evidenceMatches = Boolean(savedVersion.value && deploymentId.value === savedVersion.value.validatedDeployment)
  validationRequestFailed.value = !evidenceMatches
  smokeRequestFailed.value = !evidenceMatches
  contractResult.value = evidenceMatches ? savedVersion.value?.validationEvidence || null : null
  smokeResult.value = evidenceMatches ? savedVersion.value?.smokeTestEvidence || null : null
  await Promise.all([loadMetadata(), restoreSmokeAttempt()])
}
const load = async () => {
  loading.value = true; error.value = ''
  try {
    [templates.value, deployments.value, providers.value, modelProfiles.value, modelArtifacts.value] = await Promise.all([platformApi.listLegacyWorkflowTemplates(), platformApi.listDeployments(), platformApi.listProviders(), platformApi.listModelProfiles(), platformApi.listModelArtifacts()])
    if (workflowId.value !== 'new') {
      const version = await platformApi.getWorkflowVersion(workflowId.value)
      savedVersion.value = version; templateId.value = version.template; providerId.value = version.provider; modelProfileId.value = version.modelProfileVersion
      contractResult.value = version.validationEvidence || null; smokeResult.value = version.smokeTestEvidence || null
      setContractDrafts(version)
      sourceText.value = JSON.stringify(version.graph || {}, null, 2); applyValue(version.graph || {}); dirty.value = false
      deploymentId.value = compatibleDeployments.value.some((item) => item.id === version.validatedDeployment) ? version.validatedDeployment : compatibleDeployments.value[0]?.id || ''
    } else {
      if (templates.value.length) templateId.value = templates.value[0].id
      setContractDrafts(null)
      deploymentId.value = compatibleDeployments.value[0]?.id || ''
    }
    if (deploymentId.value) await loadMetadata()
    await restoreSmokeAttempt()
  } catch (cause) { error.value = message(cause) } finally { loading.value = false }
}
const refreshSavedEvidence = async () => {
  if (!savedVersion.value) return
  try {
    const refreshed = await platformApi.getWorkflowVersion(savedVersion.value.id)
    savedVersion.value = refreshed
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
onMounted(load)
onBeforeUnmount(clearSmokePoll)
watch([providerId, modelProfileId], () => {
  if (deploymentId.value && !compatibleDeployments.value.some((item) => item.id === deploymentId.value)) deploymentId.value = ''
  metadata.value = []; metadataComplete.value = false; metadataCatalogSha256.value = ''
  smokeAttempt.value = null; smokeAttemptError.value = ''; clearSmokePoll()
})
const updateInput = (nodeId: string, name: string, raw: string) => {
  canvasNodes.value = canvasNodes.value.map((node) => String(node.id) === nodeId
    ? { ...node, data: { ...node.data, inputs: { ...node.data.inputs, [name]: parseComfyLiteralInput(raw) } } }
    : node)
  dirty.value = true
}
const removeInput = (name: string) => { if (!selectedNode.value) return; const inputs = { ...selectedNode.value.data.inputs }; delete inputs[name]; selectedNode.value.data.inputs = inputs; canvasEdges.value = canvasEdges.value.filter((edge) => !(String(edge.target) === selectedNode.value?.id && String(edge.targetHandle) === `input:${name}`)); dirty.value = true }
const addLiteralInput = () => { if (!selectedNode.value) return; let index = 1; let name = `input_${index}`; while (name in selectedNode.value.data.inputs) name = `input_${++index}`; selectedNode.value.data.inputs = { ...selectedNode.value.data.inputs, [name]: '' }; dirty.value = true }
const addNode = (classType: string) => { const meta = nodeMetadata.value.get(classType); const numericIds = canvasNodes.value.map((item) => Number(item.id)).filter(Number.isFinite); const id = String((numericIds.length ? Math.max(...numericIds) : 0) + 1); const inputs = Object.fromEntries((meta?.requiredInputs || []).map((name) => [name, ''])); canvasNodes.value.push({ id, type: 'comfy', position: { x: 80 + (canvasNodes.value.length % 4) * 280, y: 80 + Math.floor(canvasNodes.value.length / 4) * 210 }, data: { classType, label: meta?.displayName || classType, inputs, outputCount: Math.max(1, meta?.outputCount || 1), source: {} } }); selectedNodeId.value = id; dirty.value = true }
const connect = (connection: Connection) => { if (!connection.source || !connection.target || !connection.targetHandle) return; canvasEdges.value = canvasEdges.value.filter((edge) => !(edge.target === connection.target && edge.targetHandle === connection.targetHandle)); canvasEdges.value.push({ id: `${connection.source}:${connection.sourceHandle}->${connection.target}:${connection.targetHandle}`, source: connection.source, target: connection.target, sourceHandle: connection.sourceHandle || 'output:0', targetHandle: connection.targetHandle, type: 'smoothstep' }); dirty.value = true }
const saveVersion = async () => {
  if (saveBlocked.value || !inputContractState.value.value) return
  busy.value = 'save'; error.value = ''; notice.value = ''
  try {
    const created = await platformApi.importWorkflowVersion({
      template: templateId.value,
      graph: currentGraph.value,
      inputContract: inputContractState.value.value,
      semanticBindings: semanticBindings.value.map(({ semanticKey, nodeId, inputName, valueType, isRequired }) => ({ semanticKey: semanticKey.trim(), nodeId: nodeId.trim(), inputName: inputName.trim(), valueType, isRequired })),
      modelRequirements: modelRequirements.value.map(({ role, alias, artifactId, expectedFilename, expectedSha256, isRequired }) => ({ role: role.trim(), alias: alias.trim(), artifactId: artifactId || '', expectedFilename: expectedFilename.trim(), expectedSha256: expectedSha256.trim().toLowerCase(), isRequired })),
      provider: providerId.value || null,
      modelProfileVersion: modelProfileId.value || null,
    })
    savedVersion.value = created; setContractDrafts(created); smokeAttempt.value = null; smokeAttemptError.value = ''; clearSmokePoll()
    sourceText.value = JSON.stringify(currentGraph.value, null, 2); sourceValue.value = currentGraph.value; analysis.value = analyzeComfyWorkflow(currentGraph.value, metadata.value); dirty.value = false
    await router.replace({ name: 'workflow-version-editor', params: { workflowVersionId: created.id } })
    notice.value = `服务端已创建不可变新版本 v${created.version}，下一步执行 live validate。`
  } catch (cause) { error.value = message(cause) } finally { busy.value = '' }
}
const validateVersion = async () => { if (!savedVersion.value || dirty.value || !deploymentId.value) return; busy.value = 'validate'; error.value = ''; validationRequestFailed.value = true; contractResult.value = null; try { const result = await platformApi.validateWorkflow(savedVersion.value.id, deploymentId.value); savedVersion.value = result.version; contractResult.value = result.version.validationEvidence || result.contractReport; smokeResult.value = result.version.smokeTestEvidence || null; validationRequestFailed.value = false; evidenceRefreshError.value = ''; notice.value = '服务端 live contract validate 已完成。' } catch (cause) { error.value = message(cause); await refreshSavedEvidence().catch(() => undefined) } finally { busy.value = '' } }
const smoke = async () => {
  if (!savedVersion.value || dirty.value || !deploymentId.value || smokeActive.value) return
  busy.value = 'smoke-start'; error.value = ''; smokeAttemptError.value = ''; notice.value = ''; smokeRequestFailed.value = true; smokeResult.value = null
  try {
    smokeAttempt.value = await platformApi.createWorkflowSmokeAttempt(savedVersion.value.id, deploymentId.value)
    smokeRequestFailed.value = false; evidenceRefreshError.value = ''
    notice.value = '真实低成本烟测已进入持久队列；本页将轮询任务，刷新或返回后仍可恢复。'
    scheduleSmokePoll()
  } catch (cause) { smokeAttemptError.value = message(cause); await refreshSavedEvidence().catch(() => undefined) }
  finally { busy.value = '' }
}
const cancelSmoke = async () => {
  if (!smokeAttempt.value || !smokeCancellable.value) return
  const attemptId = smokeAttempt.value.id
  busy.value = 'smoke-cancel'; smokeAttemptError.value = ''
  try {
    smokeAttempt.value = await platformApi.cancelWorkflowSmokeAttempt(attemptId)
    notice.value = smokeAttempt.value.status === 'cancelled' ? '烟测已在提交前取消。' : '取消请求已持久化，等待 worker 核对远端状态。'
    scheduleSmokePoll()
  } catch (cause) { smokeAttemptError.value = message(cause) } finally { busy.value = '' }
}
const downloadSavedComfyCanvas = async () => {
  const version = savedVersion.value
  if (!version || dirty.value) return
  const exportDeploymentId = version.validatedDeployment || deploymentId.value
  if (!exportDeploymentId) {
    error.value = '未校验草稿必须先在“Live 部署”中显式选择 active 且兼容的 ComfyUI deployment。'
    return
  }
  busy.value = 'canvas-export'; error.value = ''; notice.value = ''
  try {
    const payload = await platformApi.getWorkflowTemplateCanvasExport(version.id, exportDeploymentId)
    if (!payload.importReady || payload.directOpenSupported || payload.openMode !== 'download_import') {
      throw new Error(payload.openReason || '服务端未返回可安全导入的 ComfyUI UI 画布。')
    }
    const blob = new Blob([JSON.stringify(payload.workflow, null, 2)], { type: 'application/json;charset=utf-8' })
    const objectUrl = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = objectUrl; anchor.download = payload.filename; anchor.style.display = 'none'
    document.body.appendChild(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(objectUrl)
    notice.value = `已从 deployment“${payload.deploymentName}”生成并下载 ComfyUI UI 画布 JSON；请在 ComfyUI 中拖入该文件或从 Workflow 菜单打开。浏览器不会跨页面注入工作流，也不会下发生产任务。`
  } catch (cause) { error.value = message(cause) } finally { busy.value = '' }
}
const publish = async () => { if (publishBlocked.value || !savedVersion.value) return; busy.value = 'publish'; error.value = ''; try { savedVersion.value = await platformApi.publishWorkflow(savedVersion.value.id); publishOpen.value = false; notice.value = `v${savedVersion.value.version} 已发布。` } catch (cause) { error.value = message(cause) } finally { busy.value = '' } }
</script>

<template>
  <div class="platform-page workflow-version-editor">
    <header class="platform-page-header"><div><button class="platform-back" @click="router.push({path:'/settings',query:{section:'workflows'}})"><ArrowLeft :size="14"/>返回设置中心</button><span class="eyebrow">COMFYUI VERSIONED WORKFLOW</span><h2>{{ savedVersion ? `${savedVersion.templateName} · v${savedVersion.version}` : '导入工作流版本' }}</h2><p>首版只保存可安全映射的 ComfyUI API graph。UI JSON 包含按位置控件、子图或其他不支持数据时会阻止保存与发布；当前后端没有 UI 图双快照，原始 UI 文件请自行保管。</p></div><div class="workflow-version-state"><PlatformStatusPill v-if="savedVersion" :status="savedVersion.status"/><span v-if="dirty">画布有未保存更改</span></div></header>
    <div v-if="error" class="platform-alert danger"><AlertTriangle :size="17"/><div><strong>操作未完成</strong><p>{{ error }}</p></div></div><div v-if="notice" class="platform-alert success"><Check :size="17"/><p>{{ notice }}</p></div><div v-if="loading" class="platform-loading"><LoaderCircle class="spin" :size="22"/>读取工作流契约…</div>
    <template v-else>
      <section class="workflow-editor-toolbar"><label><span>模板</span><select v-model="templateId" :disabled="Boolean(savedVersion)"><option value="">请选择</option><option v-for="item in templates" :key="item.id" :value="item.id">{{ item.name }} · {{ item.outputKind }}</option></select></label><label><span>Provider</span><select v-model="providerId" @change="dirty=true"><option value="">不关联</option><option v-for="item in providers" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><label><span>模型版本</span><select v-model="modelProfileId" @change="dirty=true"><option value="">不关联</option><option v-for="item in modelProfiles.filter(row=>row.status==='published')" :key="item.id" :value="item.id">{{ item.profileKey }} v{{ item.version }}</option></select></label><label><span>Live 部署</span><select v-model="deploymentId" @change="changeDeployment"><option value="">请选择 active 且兼容的部署</option><option v-for="item in compatibleDeployments" :key="item.id" :value="item.id">{{ item.name || item.key }} · active</option></select></label><button class="button ghost small" :disabled="!deploymentId||Boolean(busy)" @click="loadMetadata"><RefreshCw :size="13"/>动态节点</button><label class="button ghost small file-button"><Upload :size="13"/>导入 JSON<input type="file" accept="application/json,.json" @change="chooseSourceFile"/></label><button v-if="savedVersion" class="button ghost small" :disabled="dirty||Boolean(busy)||(!savedVersion.validatedDeployment&&!deploymentId)" title="validated/published 版本固定读取持久化验证部署；未校验草稿读取上方显式选择的部署。不会回退默认 ComfyUI、跨页面注入或下发任务" @click="downloadSavedComfyCanvas"><Download :size="13"/>{{ busy==='canvas-export'?'转换中…':'在 ComfyUI 中打开（下载导入）' }}</button><button class="button ghost small" @click="rawOpen=!rawOpen"><FileJson2 :size="13"/>{{ rawOpen?'关闭':'查看' }}原始 JSON</button></section>
      <div v-if="blockers.length||warnings.length" class="workflow-contract-notices"><div v-if="blockers.length" class="platform-alert danger"><AlertTriangle :size="16"/><div><strong>发布阻断项 · {{ blockers.length }}</strong><ul><li v-for="item in blockers" :key="item">{{ item }}</li></ul></div></div><div v-if="warnings.length" class="platform-alert warning"><AlertTriangle :size="16"/><div><strong>契约提示</strong><ul><li v-for="item in warnings" :key="item">{{ item }}</li></ul></div></div></div>
      <section v-if="rawOpen" class="raw-workflow-editor"><textarea v-model="sourceText" rows="18" spellcheck="false"></textarea><button class="button primary small" @click="applySourceText">解析并投影到画布</button></section>
      <section class="workflow-contract-editor">
        <header><div><strong>执行合同</strong><small>与 graph 一起创建不可变新版本；服务端仍执行最终语义校验。</small></div><span>{{ semanticBindings.length }} bindings · {{ modelRequirements.length }} requirements</span></header>
        <div class="workflow-contract-basics">
          <label><span><strong>合同 Schema</strong><i>必填</i></span><input type="number" min="1" step="1" :value="inputContractTextValue('schema_version')" :disabled="Boolean(inputContractState.error)" @input="setInputContractField('schema_version',Number(($event.target as HTMLInputElement).value))"/><small>决定服务端使用哪一版执行合同解析；未知版本会阻断保存。</small></label>
          <label><span><strong>生产能力</strong><i>必填</i></span><input :value="inputContractTextValue('capability')" placeholder="image.text_to_image" :disabled="Boolean(inputContractState.error)" @input="setInputContractField('capability',($event.target as HTMLInputElement).value)"/><small>影响 Deployment、模型能力与输出类型的兼容检查。</small></label>
          <label><span><strong>编译器 ID</strong><i>必填</i></span><input :value="inputContractTextValue('compiler.id')" placeholder="generic.comfy" :disabled="Boolean(inputContractState.error)" @input="setInputContractField('compiler.id',($event.target as HTMLInputElement).value)"/><small>决定 ProductionRun 如何把标准语义装配进节点输入。</small></label>
          <label><span><strong>编译器版本</strong><i>必填</i></span><input type="number" min="1" step="1" :value="inputContractTextValue('compiler.version')" :disabled="Boolean(inputContractState.error)" @input="setInputContractField('compiler.version',Number(($event.target as HTMLInputElement).value))"/><small>发布后冻结；编译规则变化必须导入新工作流版本。</small></label>
          <label class="wide"><span><strong>H3 模式</strong><i>可选</i></span><input :value="inputContractModes" placeholder="T2VA, I2VA, Ref2VA" :disabled="Boolean(inputContractState.error)" @input="updateInputContractModes"/><small>逗号分隔；限制该工作流可用于哪些 H3 文生/参考生视频模式。</small></label>
        </div>
        <details><summary>input_contract · 高级完整 JSON（参数 Schema、输出与 smoke inputs）</summary><p>常用字段已在上方拆分；这里只维护 parameters、outputs、smoke_inputs 等扩展合同。服务端仍会拒绝秘密字段和不完整语义。</p><textarea v-model="inputContractText" rows="14" spellcheck="false" @input="markContractDirty"></textarea></details>
        <details open><summary><span>semantic_bindings</span><button class="button ghost tiny" type="button" @click.prevent="addSemanticBinding"><Plus :size="11"/>新增绑定</button></summary><div class="workflow-contract-rows"><article v-for="item in semanticBindings" :key="item.key"><label>Semantic key<input v-model="item.semanticKey" @input="markContractDirty"/></label><label>节点<select v-model="item.nodeId" @change="markContractDirty"><option value="">请选择</option><option v-for="node in canvasNodes" :key="node.id" :value="String(node.id)">{{ node.id }} · {{ node.data.classType }}</option></select></label><label>Input<input v-model="item.inputName" @input="markContractDirty"/></label><label>类型<select v-model="item.valueType" @change="markContractDirty"><option v-for="valueType in semanticValueTypes" :key="valueType" :value="valueType">{{ valueType }}</option></select></label><label class="workflow-contract-check"><input v-model="item.isRequired" type="checkbox" @change="markContractDirty"/>必需</label><button class="button danger tiny" type="button" @click="removeSemanticBinding(item.key)"><X :size="11"/>删除</button></article><p v-if="!semanticBindings.length" class="platform-empty">尚无语义绑定；需要注入的运行参数必须显式绑定。</p></div></details>
        <details open><summary><span>model_requirements</span><button class="button ghost tiny" type="button" @click.prevent="addModelRequirement"><Plus :size="11"/>新增要求</button></summary><div class="workflow-requirement-rows"><article v-for="item in modelRequirements" :key="item.key"><label>Artifact<select v-model="item.artifactId" @change="selectRequirementArtifact(item)"><option value="">不绑定目录 artifact</option><option v-for="artifact in filteredModelArtifacts" :key="artifact.id" :value="artifact.id">{{ artifact.logicalName || artifact.filename }} · {{ artifact.role }} · {{ artifact.status }}</option></select></label><label>Role<input v-model="item.role" @input="markContractDirty"/></label><label>Alias<input v-model="item.alias" @input="markContractDirty"/></label><label>Filename<input v-model="item.expectedFilename" @input="markContractDirty"/></label><label>SHA256<input v-model="item.expectedSha256" @input="markContractDirty"/></label><label class="workflow-contract-check"><input v-model="item.isRequired" type="checkbox" @change="markContractDirty"/>必需</label><button class="button danger tiny" type="button" @click="removeModelRequirement(item.key)"><X :size="11"/>删除</button></article><p v-if="!modelRequirements.length" class="platform-empty">远端文本模型可无文件要求；本地工作流所需文件应绑定模型目录 artifact。</p></div></details>
      </section>
      <section class="workflow-canvas-shell"><aside class="workflow-node-palette"><header><strong>动态节点</strong><small>{{ metadataComplete?`${metadata.length} 个 · ${metadataCatalogSha256.slice(0,8)}`:'目录未完整加载' }}</small></header><div><details v-for="category in [...new Set(metadata.map(item=>item.category||'Other'))]" :key="category"><summary>{{ category }}</summary><button v-for="item in metadata.filter(row=>(row.category||'Other')===category)" :key="item.classType" @click="addNode(item.classType)"><Plus :size="11"/><span><strong>{{ item.displayName }}</strong><small>{{ item.classType }}</small></span></button></details><p v-if="!metadata.length">选择 active deployment 后完整分页读取 object_info；目录漂移或重复页会阻断保存。</p></div></aside>
        <div class="workflow-canvas"><VueFlow v-model:nodes="canvasNodes" v-model:edges="canvasEdges" fit-view-on-init :min-zoom="0.15" :max-zoom="2" @connect="connect" @nodes-change="dirty=true" @edges-change="dirty=true"><Background :gap="20" pattern-color="#242b34"/><Controls/><template #node-comfy="{ data, id }"><article class="comfy-canvas-node" :class="{selected:selectedNodeId===String(id)}" @click.stop="selectedNodeId=String(id)"><header><span>{{ data.label }}</span><code>{{ id }}</code></header><strong>{{ data.classType }}</strong><div class="node-handles"><div v-for="(row,index) in inputRows(data.inputs)" :key="row.name"><Handle type="target" :position="Position.Left" :id="`input:${row.name}`" :style="{top:`${58+index*20}px`}"/><span>{{ row.name }}</span><small>{{ isConnectionValue(row.value)?'link':typeof row.value }}</small></div></div><Handle v-for="index in data.outputCount" :key="index" type="source" :position="Position.Right" :id="`output:${index-1}`" :style="{top:`${58+(index-1)*20}px`} "/></article></template></VueFlow></div>
        <aside class="workflow-node-inspector"><header><strong>节点属性</strong><small>{{ selectedNode?.id || '未选择' }}</small></header><template v-if="selectedNode"><label><span>class_type</span><input v-model="selectedNode.data.classType" @input="dirty=true"/></label><label><span>标题</span><input v-model="selectedNode.data.label" @input="dirty=true"/></label><div class="node-input-list"><article v-for="([name,value]) in selectedInputs" :key="name"><header><strong>{{ name }}</strong><button @click="removeInput(name)"><X :size="11"/></button></header><span v-if="isConnectionValue(value)" class="linked-input"><GitBranch :size="12"/>来自节点 {{ (value as unknown[])[0] }} 输出 {{ (value as unknown[])[1] }}</span><textarea v-else :value="formatInput(value)" rows="2" @input="updateInput(String(selectedNode.id),name,($event.target as HTMLTextAreaElement).value)"></textarea></article></div><button class="button ghost tiny" @click="addLiteralInput"><Plus :size="12"/>新增 literal input</button></template><p v-else>点击画布节点查看属性。metadata 不含 widget 类型，复杂值仅保留不编辑。</p></aside>
      </section>
      <section class="workflow-publish-bar"><div><span><strong>1. 保存为新版本</strong><small>不会 PATCH 或覆盖已有版本</small></span><button class="button ghost small" :disabled="saveBlocked||Boolean(busy)" @click="saveVersion"><Save :size="13"/>{{ busy==='save'?'导入中…':'保存新版本' }}</button></div><div><span><strong>2. Live validate</strong><small>{{ validationRequestFailed ? '本次校验失败；旧通过状态已在本页失效' : savedVersion?.validationMessage || '检查 object_info 节点/端口' }}</small></span><PlatformStatusPill v-if="savedVersion" :status="validationDisplayState"/><button class="button ghost small" :disabled="!savedVersion||dirty||!deploymentId||Boolean(busy)||!metadataComplete" @click="validateVersion"><Check :size="13"/>校验</button></div><div><span><strong>3. 真实 Smoke test</strong><small>{{ smokeRequestFailed ? '本次烟测创建失败；旧通过状态已在本页失效' : savedVersion?.smokeTestMessage || '创建持久任务，由 worker submit / poll / collect；可能消耗算力和费用' }}</small></span><PlatformStatusPill v-if="smokeAttempt" :status="smokeAttempt.status"/><PlatformStatusPill v-else-if="savedVersion" :status="smokeDisplayState"/><button class="button ghost small" :disabled="savedVersion?.validationState!=='passed'||validationRequestFailed||dirty||!deploymentId||Boolean(busy)||smokeActive" @click="smoke"><Play :size="13"/>{{ busy==='smoke-start'?'创建中…':smokeActive?'烟测执行中':'创建烟测任务' }}</button></div><div><span><strong>4. 发布</strong><small>{{ blockers.length?`${blockers.length} 项前端发布阻断`:'前置契约全部通过后可发布' }}</small></span><button class="button primary small" :disabled="publishBlocked||Boolean(busy)" @click="publishOpen=true"><Send :size="13"/>核对并发布</button></div></section>
      <section v-if="smokeAttempt" class="platform-card workflow-smoke-attempt"><header><div><strong>持久烟测任务</strong><small>{{ smokeAttempt.id }} · deployment {{ smokeAttempt.deployment }}</small></div><PlatformStatusPill :status="smokeAttempt.status"/></header><dl><div><dt>Execution contract</dt><dd><code>{{ smokeAttempt.executionContractSha256 }}</code></dd></div><div><dt>Target</dt><dd><code>{{ smokeAttempt.targetSha256 }}</code></dd></div><div><dt>Poll / remote</dt><dd>{{ smokeAttempt.pollCount }} · {{ smokeAttempt.remoteStatus || '尚未提交' }}</dd></div><div><dt>Hard / idle deadline</dt><dd>{{ smokeAttempt.hardDeadlineAt || '—' }} / {{ smokeAttempt.idleDeadlineAt || '—' }}</dd></div></dl><div class="platform-row-actions"><button class="button ghost tiny" :disabled="smokePollInFlight" @click="refreshSmokeAttempt"><RefreshCw :size="11"/>刷新任务</button><button v-if="smokeCancellable" class="button danger tiny" :disabled="Boolean(busy)" @click="cancelSmoke"><X :size="11"/>{{ busy==='smoke-cancel'?'取消中…':'请求取消' }}</button></div></section>
      <div v-if="smokeAttemptError" class="platform-alert danger"><AlertTriangle :size="15"/><div><strong>烟测任务未完成</strong><p>{{ smokeAttemptError }}</p></div></div>
      <div v-if="evidenceRefreshError" class="platform-alert danger"><AlertTriangle :size="15"/><div><strong>持久证据不可用</strong><p>{{ evidenceRefreshError }}</p></div></div>
      <details v-if="contractResult||smokeResult" class="platform-card workflow-results"><summary>服务端校验报告</summary><pre v-if="contractResult">{{ JSON.stringify(contractResult,null,2) }}</pre><pre v-if="smokeResult">{{ JSON.stringify(smokeResult,null,2) }}</pre></details>
    </template>
    <BaseModal :open="publishOpen" :dismissible="!Boolean(busy)" :title="`核对并发布 · ${savedVersion?.templateName || '工作流'} v${savedVersion?.version || ''}`" eyebrow="IMMUTABLE WORKFLOW RELEASE" @close="publishOpen=false">
      <div class="workflow-publish-review"><p>发布后 graph、bindings、requirements 与证据均不可原地覆盖；修改必须重新导入新版本。</p><dl><div><dt>Validated deployment</dt><dd><code>{{ savedVersion?.validatedDeployment || '—' }}</code></dd></div><div><dt>Graph hash</dt><dd><code>{{ savedVersion?.graphSha256 || '—' }}</code></dd></div><div><dt>Execution hash</dt><dd><code>{{ savedVersion?.executionContractSha256 || '—' }}</code></dd></div><div><dt>Validation evidence</dt><dd><code>{{ savedVersion?.validationEvidenceSha256 || '—' }}</code></dd></div><div><dt>Smoke evidence</dt><dd><code>{{ savedVersion?.smokeTestEvidenceSha256 || '—' }}</code></dd></div></dl><div v-if="error" class="platform-alert danger"><AlertTriangle :size="15"/><p>{{ error }}</p></div></div>
      <template #footer><button class="button ghost" :disabled="Boolean(busy)" @click="publishOpen=false">取消</button><button class="button primary" :disabled="publishBlocked||Boolean(busy)" @click="publish">{{ busy==='publish'?'发布中…':'确认发布不可变版本' }}</button></template>
    </BaseModal>
  </div>
</template>

<style scoped>
.workflow-contract-editor{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.workflow-contract-editor>header{grid-column:1/-1;padding:10px 12px;display:flex;align-items:center;justify-content:space-between;border:1px solid var(--border);border-radius:8px;background:#0c0f13}.workflow-contract-editor>header div{display:flex;flex-direction:column;gap:3px}.workflow-contract-editor>header strong{color:#b9c0c6;font-size:10px}.workflow-contract-editor>header small,.workflow-contract-editor>header>span{color:#69727a;font-size:8px}.workflow-contract-basics{grid-column:1/-1;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;padding:10px;border:1px solid var(--border);border-radius:8px;background:#0c0f13}.workflow-contract-basics label{display:grid;gap:5px;min-width:0}.workflow-contract-basics label.wide{grid-column:1/-1}.workflow-contract-basics label>span{display:flex;justify-content:space-between;gap:6px;color:#aeb6bf;font-size:9px}.workflow-contract-basics label i{color:#ce9958;font-style:normal;font-size:7px}.workflow-contract-basics input{min-width:0;width:100%;padding:8px 9px;border:1px solid var(--border);border-radius:6px;background:#070a0e;color:#c9d1db}.workflow-contract-basics small{color:#69737e;font-size:8px;line-height:1.45}.workflow-contract-editor>details{min-width:0;border:1px solid var(--border);border-radius:8px;background:#0c0f13;overflow:hidden}.workflow-contract-editor>details:first-of-type{grid-column:1/-1}.workflow-contract-editor summary{padding:9px 10px;display:flex;align-items:center;justify-content:space-between;color:#9ea6ae;font:9px Consolas,monospace;cursor:pointer}.workflow-contract-editor>details>p{margin:0;padding:0 10px 8px;color:#6f7984;font-size:8px}.workflow-contract-editor>details>textarea{width:calc(100% - 20px);min-height:160px;margin:0 10px 10px;padding:9px;resize:vertical;border:1px solid var(--border);border-radius:6px;background:#080b0e;color:#aeb5bc;font:9px/1.55 Consolas,monospace}.workflow-contract-rows,.workflow-requirement-rows{display:grid;gap:7px;padding:0 9px 9px}.workflow-contract-rows article,.workflow-requirement-rows article{padding:8px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;border:1px solid rgba(255,255,255,.055);border-radius:7px;background:#090c10}.workflow-contract-rows label,.workflow-requirement-rows label{min-width:0;display:flex;flex-direction:column;gap:4px;color:#68727b;font-size:8px}.workflow-contract-rows input,.workflow-contract-rows select,.workflow-requirement-rows input,.workflow-requirement-rows select{min-width:0;min-height:32px;padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:#070a0e;color:#aeb5bc;font-size:9px}.workflow-contract-check{align-items:center!important;flex-direction:row!important}.workflow-contract-check input{min-height:auto}.workflow-contract-rows article>.button,.workflow-requirement-rows article>.button{justify-self:end}.workflow-smoke-attempt{padding-bottom:10px}.workflow-smoke-attempt>header{padding:10px}.workflow-smoke-attempt>header>div{display:flex;min-width:0;flex-direction:column;gap:3px}.workflow-smoke-attempt>header small{overflow:hidden;color:#68727a;font:8px Consolas,monospace;text-overflow:ellipsis}.workflow-smoke-attempt dl{margin:0;padding:0 10px 9px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.workflow-smoke-attempt dl div{min-width:0}.workflow-smoke-attempt dt{color:#68727b;font-size:7px}.workflow-smoke-attempt dd{margin:3px 0 0;overflow:hidden;color:#9da5ad;font-size:8px;text-overflow:ellipsis;white-space:nowrap}.workflow-smoke-attempt>.platform-row-actions{padding:0 10px}
@media(max-width:900px){.workflow-contract-editor{grid-template-columns:1fr}.workflow-contract-editor>details:first-of-type{grid-column:auto}.workflow-contract-basics{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:620px){.workflow-contract-rows article,.workflow-requirement-rows article,.workflow-smoke-attempt dl,.workflow-contract-basics{grid-template-columns:1fr}.workflow-contract-basics label.wide{grid-column:auto}}
</style>
