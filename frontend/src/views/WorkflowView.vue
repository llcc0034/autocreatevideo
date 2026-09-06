<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AlertTriangle, ArrowRight, AudioLines, Check, CheckCircle2, ChevronDown, Clapperboard, Clipboard, Clock3, Code2, Cpu, Download, ExternalLink, Eye, FileJson, FileText, Film, FolderInput, Image, Layers3, LoaderCircle, Lock, MoreHorizontal, Music2, Plus, RefreshCw, Save, Server, Sparkles, WandSparkles, Workflow as WorkflowIcon } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import AssetMediaPreview from '@/components/AssetMediaPreview.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { useStudioStore } from '@/stores/studio'
import { useAuthStore } from '@/stores/auth'
import { repository, type ProductionCapability } from '@/services/repository'
import { workflowProgressPresentation } from '@/services/taskProgress'
import {
  buildTaskListEntries,
  isWorkflowActive,
  taskListEntryMatchesFilter,
  taskListFilterCounts,
  taskListGroupKey,
  type TaskListFilter,
  type TaskListEntry,
  type TaskListGroupEntry,
} from '@/services/taskGroups'
import { deferredGenerationFrameSlotLabel, deferredGenerationStatusLabel } from '@/services/deferredGeneration'
import type { AssetGenerationJob, AssetGenerationResolutionOutcome, DeferredGenerationRequest, GeneratedWorkflowExport, ReferenceEntityCategory, TaskInputPrompt, TaskOutputMedia, TaskReferenceAssetSummary, Workflow, WorkflowNode } from '@/types'

type HealthLevel = 'checking' | 'connected' | 'degraded' | 'unavailable'
type RuntimeAlert = { level: 'info' | 'warning' | 'danger'; title: string; message: string }
type ParameterItem = { key: string; label: string; value: string }
type ReferenceAssetGroup = { key: 'model-input' | 'continuity'; title: string; description: string; assets: TaskReferenceAssetSummary[]; continuity: boolean }
type TaskScope = 'all' | 'short_drama' | 'asset' | 'media'
type UnifiedTaskFilter = TaskListFilter | 'cancelled' | 'attention'

const studio = useStudioStore(); const auth = useAuthStore(); const route = useRoute(); const router = useRouter(); const workflowInspectOpen = ref(false); const workflowInspectLoading = ref(false); const workflowInspectError = ref(''); const workflowExport = ref<GeneratedWorkflowExport|null>(null); const workflowExportFormat = ref<'ui'|'api'>('ui'); const inspectedTaskId = ref(''); const selectedId = ref(''); const taskScope = ref<TaskScope>(['short_drama','asset','media'].includes(String(route.query.scope||''))?String(route.query.scope) as TaskScope:'all'); const taskFilter = ref<UnifiedTaskFilter>('all'); const failureExpanded = ref(false); const parametersExpanded = ref(false); const taskMenuOpen = ref(false); const taskMenuRoot = ref<HTMLElement|null>(null); const promptOpen = ref(false); const promptLoading = ref(false); const promptError = ref(''); const inputPrompt = ref<TaskInputPrompt|null>(null); const promptTaskId = ref(''); const referencePreviewOpen = ref(false); const referencePreviewAsset = ref<TaskReferenceAssetSummary|null>(null); const referenceImageFailures = ref<Record<string,boolean>>({}); const healthChecking = ref(false); const health = ref<{ok:boolean;message:string;latency?:number}|null>(null); const pollError = ref(''); const assetJobsError = ref(''); const nowMs = ref(Date.now()); const outputMediaRecoveryAttempts = new Map<string,number>(); const referenceImageRecoveryAttempts = new Map<string,number>(); let ticker = 0; let clockTicker = 0; let healthTicker = 0; let promptRequestSequence = 0; let detailRequestSequence = 0; let workflowInspectRequestSequence = 0
const productionCapability = ref<ProductionCapability|null>(null); const productionCapabilityError = ref('')
const queueGuidanceExceeded = computed(() => (productionCapability.value?.queueAdvisories.length || 0) > 0)
const resolutionTarget=ref<AssetGenerationJob|null>(null);const resolutionPending=ref(false);const resolutionError=ref('')
const resolutionOutcomes:Array<{value:AssetGenerationResolutionOutcome;label:string}>=[
  {value:'confirmed_not_submitted',label:'确认未提交'},
  {value:'confirmed_remote_failed',label:'确认远端失败'},
  {value:'confirmed_remote_cancelled',label:'确认远端取消'},
  {value:'confirmed_remote_succeeded',label:'确认远端成功'},
]
const blankResolutionForm=()=>({outcome:'confirmed_not_submitted' as AssetGenerationResolutionOutcome,note:'',providerRequestId:'',actualCost:'',costCurrency:'',idempotencyKey:crypto.randomUUID()})
const resolutionForm=ref(blankResolutionForm());let resolutionIntentJobId=''
const collapsedGroupKeys = ref<Set<string>>(new Set())
const observedGroupKeys = new Set<string>()
const deferredLoading = ref(false); const deferredError = ref(''); const deferredRequests = ref<DeferredGenerationRequest[]>([])
const healthLevel = computed<HealthLevel>(()=>{
  if (!health.value) return 'checking'
  if (health.value.ok) return 'connected'
  return /system_stats|500|internal server|健康检查|health check/i.test(health.value.message) ? 'degraded' : 'unavailable'
})
const healthBadge = computed(()=>healthLevel.value==='connected'?'已连接（CONNECTED）':healthLevel.value==='degraded'?'性能下降（DEGRADED）':healthLevel.value==='unavailable'?'不可用（UNAVAILABLE）':'检查中（CHECKING）')
const isActiveTask = isWorkflowActive
const taskKind = (workflow?: Workflow | null): 'video' | 'image' => workflow?.kind === 'image' || workflow?.nodes.some((node) => node.type === 'image' && node.id.startsWith('generation-')) ? 'image' : 'video'
const taskMediaLabel = (workflow?: Workflow | null) => taskKind(workflow) === 'image' ? '图片' : '视频'
const taskListEntries = computed(() => buildTaskListEntries(studio.workflows))
const shortDramaEntry=(entry:TaskListEntry)=>entry.kind==='group'?entry.groupKind==='production_run_episode':Boolean(entry.task.productionRunId)
const scopedTaskListEntries=computed(()=>taskListEntries.value.filter((entry)=>taskScope.value==='all'||taskScope.value==='asset'?taskScope.value==='all':taskScope.value==='short_drama'?shortDramaEntry(entry):!shortDramaEntry(entry)))
const filteredTaskListEntries = computed(() => scopedTaskListEntries.value.filter((entry) => taskFilter.value==='cancelled'||taskFilter.value==='attention'?false:taskListEntryMatchesFilter(entry,taskFilter.value)))
const assetJobActive=(job:AssetGenerationJob)=>['queued','submitting','submitted','running','cancel_requested'].includes(job.status)
const assetJobMatchesFilter=(job:AssetGenerationJob)=>taskFilter.value==='all'||taskFilter.value==='active'&&assetJobActive(job)||taskFilter.value==='completed'&&job.status==='succeeded'||taskFilter.value==='failed'&&job.status==='failed'||taskFilter.value==='cancelled'&&job.status==='cancelled'||taskFilter.value==='attention'&&job.status==='uncertain'
const filteredAssetJobs=computed(()=>taskScope.value==='short_drama'||taskScope.value==='media'?[]:studio.assetGenerationJobs.filter(assetJobMatchesFilter).slice().sort((left,right)=>String(right.updatedAt||right.createdAt).localeCompare(String(left.updatedAt||left.createdAt))))
const visibleWorkflowIds = computed(() => new Set(filteredTaskListEntries.value.flatMap((entry) => (
  entry.kind === 'task' ? [entry.task.id] : entry.tasks.map((task) => task.id)
))))
const scopedProductionCounts=computed(()=>taskListFilterCounts(scopedTaskListEntries.value))
const scopedAssetJobs=computed(()=>taskScope.value==='short_drama'||taskScope.value==='media'?[]:studio.assetGenerationJobs)
const assetFilterCount=(filter:UnifiedTaskFilter)=>scopedAssetJobs.value.filter((job)=>filter==='all'||filter==='active'&&assetJobActive(job)||filter==='completed'&&job.status==='succeeded'||filter==='failed'&&job.status==='failed'||filter==='cancelled'&&job.status==='cancelled'||filter==='attention'&&job.status==='uncertain').length
const taskFilters = computed<Array<{ key: UnifiedTaskFilter; label: string; count: number }>>(() => [
  { key: 'all', label: '全部', count: scopedProductionCounts.value.all+assetFilterCount('all') },
  { key: 'active', label: '运行中', count: scopedProductionCounts.value.active+assetFilterCount('active') },
  { key: 'completed', label: '已完成', count: scopedProductionCounts.value.completed+assetFilterCount('completed') },
  { key: 'failed', label: '失败', count: scopedProductionCounts.value.failed+assetFilterCount('failed') },
  { key: 'cancelled', label: '已取消', count: assetFilterCount('cancelled') },
  { key: 'attention', label: '待核对', count: assetFilterCount('attention') },
])
const taskScopes=computed<Array<{key:TaskScope;label:string;count:number}>>(()=>[
  {key:'all',label:'全部任务',count:taskListEntries.value.length+studio.assetGenerationJobs.length},
  {key:'short_drama',label:'短剧任务',count:taskListEntries.value.filter(shortDramaEntry).length},
  {key:'asset',label:'素材任务',count:studio.assetGenerationJobs.length},
  {key:'media',label:'独立媒体',count:taskListEntries.value.filter((entry)=>!shortDramaEntry(entry)).length},
])
const selected = computed(() => (
  visibleWorkflowIds.value.has(selectedId.value)
    ? studio.workflows.find((item) => item.id === selectedId.value)
    : undefined
))
const selectedAssetJob=computed(()=>filteredAssetJobs.value.find((job)=>job.id===selectedId.value))
const assetMediaLabel=(job:AssetGenerationJob)=>job.jobType==='prompt'?'提示词候选':({image:'图片',video:'视频',audio:'音频',script:'剧本'} as const)[job.mediaType]
const assetJobTitle=(job:AssetGenerationJob)=>job.jobType==='prompt'?`${({image:'图片',video:'视频',audio:'音频',script:'剧本'} as const)[job.mediaType]}提示词候选`:job.name||'未命名素材任务'
const assetJobIcon=(job:AssetGenerationJob)=>job.jobType==='prompt'?WandSparkles:({image:Image,video:Film,audio:Music2,script:FileText} as const)[job.mediaType]
const assetJobStatusLabel=(status:AssetGenerationJob['status'])=>({queued:'排队中',submitting:'正在提交',submitted:'已提交',running:'运行中',cancel_requested:'取消中',succeeded:'已成功',failed:'已失败',cancelled:'已取消',uncertain:'远端状态待核对'} as const)[status]
const assetJobBadgeStatus=(job:AssetGenerationJob)=>job.status==='succeeded'?'completed':assetJobActive(job)?'running':job.status==='failed'?'failed':job.status
const assetJobExecutionLabel=(job:AssetGenerationJob)=>job.executionKind==='comfy_workflow'?`ComfyUI · ${job.workflowName||'工作流'}${job.workflowVersion?` v${job.workflowVersion}`:''}`:`异步 API · ${job.modelDeploymentName||job.operation}`
const assetJobProgress=(job:AssetGenerationJob)=>({queued:8,submitting:20,submitted:36,running:68,cancel_requested:78,succeeded:100,failed:100,cancelled:100,uncertain:100} as const)[job.status]
const isGroupExpanded = (key: string) => !collapsedGroupKeys.value.has(key)
const expandGroup = (key: string) => {
  const next = new Set(collapsedGroupKeys.value)
  next.delete(key)
  collapsedGroupKeys.value = next
}
const toggleGroupExpansion = (key: string) => {
  const next = new Set(collapsedGroupKeys.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsedGroupKeys.value = next
}
const syncDefaultGroupExpansion = () => {
  const groups = taskListEntries.value.filter((entry): entry is TaskListGroupEntry => entry.kind === 'group')
  const liveKeys = new Set(groups.map((entry) => entry.key))
  const nextCollapsed = new Set([...collapsedGroupKeys.value].filter((key) => liveKeys.has(key)))
  for (const entry of groups) {
    if (!observedGroupKeys.has(entry.key)) {
      observedGroupKeys.add(entry.key)
      if (entry.status === 'completed') nextCollapsed.add(entry.key)
    }
  }
  for (const key of [...observedGroupKeys]) if (!liveKeys.has(key)) observedGroupKeys.delete(key)
  collapsedGroupKeys.value = nextCollapsed
}
const progressPresentation = (workflow: Workflow) => workflowProgressPresentation(workflow)
const selectedProgress = computed(() => selected.value ? progressPresentation(selected.value) : null)
const uiHiddenParameterKey = (key: string) => /(?:^|_)(?:paths?|directories|directory|dirs?|folders?|install_path|output_path|workflow_path|local_path|api_key|tokens?|secrets?|password)$|^staged_inputs$/i.test(key)
const uiAbsoluteLocalPath = (value: string) => /^[a-z]:[\\/]|^\\\\|^file:\/\/|^\/(?!api(?:\/|$)|media(?:\/|$)|static(?:\/|$)|assets(?:\/|$)).+/i.test(value.trim())
const safeUiParameterValue = (value: unknown, key = '', depth = 0): unknown => {
  if (uiHiddenParameterKey(key) || depth > 6) return undefined
  if (typeof value === 'string') return uiAbsoluteLocalPath(value) ? undefined : value
  if (value === null || typeof value === 'number' || typeof value === 'boolean') return value
  if (Array.isArray(value)) return value.map((item) => safeUiParameterValue(item, key, depth + 1)).filter((item) => item !== undefined)
  if (!value || typeof value !== 'object') return undefined
  return Object.fromEntries(Object.entries(value as Record<string, unknown>)
    .map(([childKey, item]) => [childKey, safeUiParameterValue(item, childKey, depth + 1)] as const)
    .filter(([, item]) => item !== undefined))
}
const taskInputParameters = (workflow: Workflow): Record<string, unknown> => {
  const source = workflow.inputParameters || {}
  return (safeUiParameterValue(source, 'input_parameters') || {}) as Record<string, unknown>
}
const selectedInputParameters = computed<Record<string, unknown>>(() => selected.value ? taskInputParameters(selected.value) : {})
const selectedInputContractUnavailable = computed(() => selected.value?.inputContractStatus === 'unavailable')
const selectedInputContractReason = computed(() => {
  if (!selectedInputContractUnavailable.value) return ''
  return selected.value?.inputContractCode === 'legacy_schema_unsupported'
    ? '历史工作流缺少当前 Schema / Legacy workflow schema is unsupported'
    : '冻结执行合同缺失或校验失败 / Frozen execution contract is missing or invalid'
})
const workflowTaskId = (workflow?: Workflow | null) => workflow?.isVirtualProductionUnit ? '' : workflow?.taskId || workflow?.id || ''
const selectedTaskId = computed(() => workflowTaskId(selected.value))
const deferredTargetLabel = (shotId: string) => shotId.slice(0, 12)
const loadDeferredRequests = async (taskId = selectedTaskId.value, quiet = true) => {
  if (!taskId) return
  deferredLoading.value = true
  if (!quiet) deferredError.value = ''
  try {
    const rows = await repository.getDeferredGenerationRequests({ sourceTask: taskId })
    if (taskId !== selectedTaskId.value) return
    deferredRequests.value = rows
    deferredError.value = ''
  } catch (error) {
    if (taskId !== selectedTaskId.value) return
    deferredError.value = error instanceof Error ? error.message : '延迟连续生成请求读取失败'
    if (!quiet) studio.notify(`延迟连续生成请求读取失败：${deferredError.value}`, 'error')
  } finally {
    if (taskId === selectedTaskId.value) deferredLoading.value = false
  }
}
const promptSummary = computed(() => {
  const workflow = selected.value
  if (!workflow) return '未记录'
  if (workflow.promptAvailable === false) return '未保存实际提交提示词'
  const promptCharacters = workflow.promptCharacterCount || 0
  const negativeCharacters = workflow.negativePromptCharacterCount || 0
  return promptCharacters || negativeCharacters ? `提示词 ${promptCharacters} 字 · 负面 ${negativeCharacters} 字` : '点击后按需读取实际提交内容'
})
const publicReferenceUrl = (asset?: TaskReferenceAssetSummary | null) => {
  const value = String(asset?.previewUrl || '').trim()
  if (value.startsWith('/') && !value.startsWith('//')) return value
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) ? url.toString() : ''
  } catch { return '' }
}
const referenceTimingLabel = (asset: TaskReferenceAssetSummary) => {
  if (asset.mediaKind !== 'video' || asset.sourceFps === undefined) return ''
  const source = Number.isInteger(asset.sourceFps) ? asset.sourceFps.toFixed(0) : asset.sourceFps.toFixed(3)
  const effective = asset.effectiveFps !== undefined ? `${asset.effectiveFps}fps` : '待暂存验证'
  const proof = asset.cfrGuaranteed === true ? ' · CFR已验证' : asset.cfrGuaranteed === false ? ' · CFR未通过' : ' · 执行时规范化'
  const audio = asset.pairedAudioDurationAligned === true ? ' · 配对音轨已同步' : ''
  return `源 ${source}fps → ${effective}${proof}${audio}`
}
const referenceAssetKey = (asset: TaskReferenceAssetSummary, index = 0) => asset.assetId || `${asset.mediaKind}:${asset.displayName}:${asset.inputLabel || asset.entityLabel || asset.category || index}`
const referenceMediaLabel = (kind: TaskReferenceAssetSummary['mediaKind']) => kind === 'video' ? '视频' : kind === 'audio' ? '音频' : '图片'
const selectedReferenceAssets = computed(() => selected.value?.referenceAssets || [])
const selectedContinuityAssets = computed(() => selected.value?.continuityAssets || [])
const referenceAssetGroups = computed<ReferenceAssetGroup[]>(() => [
  ...(selectedReferenceAssets.value.length ? [{
    key: 'model-input' as const,
    title: '模型参考输入',
    description: `${selectedReferenceAssets.value.length} 项素材 · 已接入当前任务工作流`,
    assets: selectedReferenceAssets.value,
    continuity: false
  }] : []),
  ...(selectedContinuityAssets.value.length ? [{
    key: 'continuity' as const,
    title: '关联一致性素材',
    description: `${selectedContinuityAssets.value.length} 项素材 · 仅用于连续性核对，未接入模型`,
    assets: selectedContinuityAssets.value,
    continuity: true
  }] : [])
])
const parameterNumber = (value: unknown): number | undefined => typeof value === 'number' && Number.isFinite(value) ? value : undefined
const parameterBoolean = (value: unknown): boolean | undefined => typeof value === 'boolean' ? value : undefined
const referenceCounts = (source: Record<string, unknown>) => {
  const images = parameterNumber(source.reference_image_count)
  const videos = parameterNumber(source.reference_video_count)
  const audios = parameterNumber(source.reference_audio_count)
  return {
    assetCount: parameterNumber(source.reference_asset_count),
    inputCount: parameterNumber(source.reference_input_count),
    images,
    videos,
    audios,
    detailed: images !== undefined && videos !== undefined && audios !== undefined,
  }
}
const formatParameterDuration = (requestedMs?: number, effectiveMs?: number) => {
  const requested = requestedMs !== undefined ? requestedMs / 1000 : undefined
  const effective = effectiveMs !== undefined ? effectiveMs / 1000 : undefined
  if (requested === undefined && effective === undefined) return '未记录'
  const value = (seconds: number) => `${seconds.toFixed(3)} 秒`
  if (requested !== undefined && effective !== undefined) return `${value(requested)} → ${value(effective)}`
  return value(requested ?? effective!)
}
const taskScopeLabel = (workflow: Workflow, source: Record<string, unknown>) => {
  const shotCount = parameterNumber(source.shot_count) ?? 0
  const batchId = typeof source.batch_id === 'string' ? source.batch_id : ''
  const batchIndex = parameterNumber(source.batch_index)
  const batchSize = parameterNumber(source.batch_size)
  if (batchId) return `整集镜头 ${batchIndex ?? '?'}/${batchSize ?? '?'} · 本任务 ${shotCount || 1} 镜`
  if (source.whole_episode === true) return `整集任务 · 本任务 ${shotCount || 1} 镜`
  if (shotCount > 1) return `单任务合并 ${shotCount} 镜`
  return `单镜${workflow.shotNumber ? ` · 镜头 ${String(workflow.shotNumber).padStart(2, '0')}` : ''}`
}
const coreInputParameters = computed<ParameterItem[]>(() => {
  const workflow = selected.value
  if (!workflow) return []
  const imageTask = taskKind(workflow) === 'image'
  const source = selectedInputParameters.value
  const requestedMode = source.conditioning_mode
  const effectiveMode = source.effective_conditioning_mode
  const profile = source.resolution_profile
  const width = parameterNumber(source.width)
  const height = parameterNumber(source.height)
  const multiple = parameterNumber(source.resolution_multiple)
  const requestedDurationMs = parameterNumber(source.requested_duration_ms)
  const effectiveDurationMs = parameterNumber(source.effective_duration_ms)
  const frames = parameterNumber(source.effective_frames)
  const fps = parameterNumber(source.fps)
  const seed = source.seed
  const audio = parameterBoolean(source.include_audio)
  const references = referenceCounts(source)
  const modelInputAssetCount = references.assetCount ?? selectedReferenceAssets.value.length
  const modelInputConnectionCount = references.inputCount
  const modelInputSummary = [
    `素材 ${modelInputAssetCount}`,
    references.detailed ? `图片 ${references.images ?? 0} / 视频 ${references.videos ?? 0} / 音频 ${references.audios ?? 0}` : '',
    modelInputConnectionCount !== undefined ? `工作流连接 ${modelInputConnectionCount}` : ''
  ].filter(Boolean).join(' · ') || '未记录'
  const continuityAssetCount = parameterNumber(source.continuity_asset_count)
  const resolutionProfile = profile === 'semantic_contract'
    ? '语义执行合同 (semantic contract)'
    : profile !== undefined ? String(profile) : '档位未记录'
  const resolution = `${resolutionProfile}${width && height ? ` · ${width} × ${height}` : ' · 实际尺寸未记录'}`
  return [
    imageTask
      ? { key: 'mode', label: '图片工作流', value: `${String(source.model_profile || '模型未声明')}${source.template_slug ? ` · ${String(source.template_slug)}` : ''}` }
      : { key: 'mode', label: '工作流模式', value: `选择工作流 ${String(requestedMode || '未记录')} · 实际条件 ${String(effectiveMode || '未记录')}` },
    { key: 'resolution', label: '分辨率档位 / 实际尺寸', value: resolution },
    { key: 'multiple', label: '尺寸倍数', value: multiple !== undefined ? String(multiple) : '未记录' },
    ...(!imageTask ? [
      { key: 'duration', label: '请求时长 → 实际时长', value: formatParameterDuration(requestedDurationMs, effectiveDurationMs) },
      { key: 'frames', label: '帧数 / FPS', value: frames !== undefined || fps !== undefined ? `${frames ?? '未记录'} 帧 · ${fps ?? '未记录'} FPS` : '未记录' }
    ] : []),
    { key: 'seed', label: 'Seed', value: seed !== undefined && seed !== '' ? String(seed) : '未记录' },
    ...(!imageTask ? [{ key: 'audio', label: '音频', value: audio === undefined ? '未记录' : audio ? '已启用' : '未启用' }] : []),
    { key: 'references', label: '模型参考输入', value: modelInputSummary },
    ...(continuityAssetCount !== undefined ? [{ key: 'continuity', label: '关联一致性素材', value: `${continuityAssetCount} 项 · 仅供连续性核对，未接入模型` }] : []),
    { key: 'scope', label: '生产范围', value: taskScopeLabel(workflow, source) }
  ]
})
const coreParameterKeys = new Set(['model','model_profile','workflow_mode','effective_conditioning_mode','conditioning_mode','resolution_profile','width','height','resolution_multiple','requested_duration_ms','effective_duration_ms','effective_frames','fps','seed','include_audio','reference_assets','reference_input_count','reference_asset_count','reference_image_count','reference_video_count','reference_audio_count','continuity_assets','continuity_asset_count','prompt_available','prompt_character_count','negative_prompt_character_count','shot_ids','shot_count','batch_id','batch_kind','batch_role','batch_index','batch_size','whole_episode','template_slug','source_workflow','prompt_template_key'])
const technicalValue = (value: unknown) => {
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (Array.isArray(value)) {
    if (!value.length) return '0 项'
    return value.every((item) => ['string', 'number', 'boolean'].includes(typeof item)) ? value.map(String).join(', ') : `${value.length} 项`
  }
  const text = String(value ?? '')
  return text.length > 220 ? `${text.slice(0, 217)}…` : text
}
const flattenParameters = (source: Record<string, unknown>, prefix = ''): ParameterItem[] => Object.entries(source).flatMap(([key, value]) => {
  const path = prefix ? `${prefix}.${key}` : key
  if (value && typeof value === 'object' && !Array.isArray(value)) return flattenParameters(value as Record<string, unknown>, path)
  return [{ key: path, label: path, value: technicalValue(value) }]
})
const technicalInputParameters = computed(() => flattenParameters(selectedInputParameters.value).filter((item) => {
  const key = item.key.split('.').slice(-1)[0] || item.key
  return !coreParameterKeys.has(key)
}))
const selectedTaskConfig = computed(() => {
  const workflow = selected.value
  if (!workflow) return {}
  return {
    schema_version: 1,
    task: {
      id: workflow.taskId || workflow.id,
      status: workflow.backendStatus || workflow.status,
      project_id: workflow.projectId,
      episode_id: workflow.episodeId,
      shot_id: workflow.shotId,
      batch_id: workflow.batchId,
      production_run_id: workflow.productionRunId,
      production_variant_number: workflow.productionVariantNumber,
      backend: workflow.backend,
      model_profile: workflow.modelProfile,
      workflow_version: workflow.workflowVersion,
      generated_workflow_id: workflow.generatedWorkflowId,
      comfy_prompt_id: workflow.comfyPromptId
    },
    input_parameters: selectedInputParameters.value
  }
})
const episodeContextLabel = (workflow: Workflow) => workflow.episodeNumber ? `第 ${workflow.episodeNumber} 集${workflow.episodeTitle ? ` · ${workflow.episodeTitle}` : ''}` : workflow.episodeTitle || ''
const shotContextLabel = (workflow: Workflow) => workflow.shotNumber ? `镜头 ${String(workflow.shotNumber).padStart(2, '0')}${workflow.shotTitle ? ` · ${workflow.shotTitle}` : ''}` : workflow.shotTitle || ''
const selectedTaskGroup = computed<TaskListGroupEntry | undefined>(() => {
  const workflow = selected.value
  const key = workflow ? taskListGroupKey(workflow) : ''
  if (!key) return undefined
  const entry = taskListEntries.value.find((candidate) => candidate.kind === 'group' && candidate.key === key)
  return entry?.kind === 'group' ? entry : undefined
})
const taskGroupTitle = (group: TaskListGroupEntry) => group.episodeNumber
  ? `第 ${group.episodeNumber} 集${group.episodeTitle ? ` · ${group.episodeTitle}` : ''}`
  : group.episodeTitle || group.tasks[0]?.projectTitle || '整集生产任务'
const taskGroupIdentity = (group: TaskListGroupEntry) => group.groupKind === 'production_run_episode'
  ? `运行 ${group.productionRunId?.slice(0, 8) || '未知'}`
  : `批次 ${group.batchId?.slice(0, 12) || '未知'}`
const taskGroupTaskLabel = (workflow: Workflow, group: TaskListGroupEntry) => {
  if (group.groupKind === 'legacy_batch') return `整集镜头 ${workflow.batchIndex || '?'}/${workflow.batchSize || group.unitCount || '?'}`
  const shot = workflow.shotNumber
    ? `镜头 ${String(workflow.shotNumber).padStart(2, '0')}`
    : workflow.shotId ? `镜头 ${workflow.shotId.slice(0, 8)}` : '生产单元'
  const variant = workflow.productionVariantNumber && workflow.productionVariantNumber > 1
    ? ` · 变体 ${workflow.productionVariantNumber}`
    : ''
  return `${shot}${variant}`
}
const taskGroupTaskStatus = (workflow: Workflow) => {
  if (workflow.isVirtualProductionUnit) return workflow.currentStep.replace(/^整集镜头\s+\S+\s+·\s*/, '')
  return workflow.status === 'completed' ? '完成' : workflow.status === 'failed' ? '失败' : workflow.status === 'running' ? '运行中' : workflow.status === 'paused' ? '等待恢复' : '排队中'
}
const taskGroupSubmittedLabel = (group: TaskListGroupEntry) => formatTaskDateTime(group.submittedAt, true)
const taskGroupStatusSummary = (group: TaskListGroupEntry) => `已完成 ${group.completedCount}/${group.unitCount} · 运行/排队 ${group.activeCount} · 失败 ${group.failedCount} · 审计尝试 ${group.attemptCount}`
const taskListEntryFirstTask = (entry?: TaskListEntry) => entry?.kind === 'task'
  ? entry.task
  : entry?.currentTasks[0] || entry?.tasks[0]
const selectedMedia = computed<TaskOutputMedia[]>(() => selected.value?.outputMedia || [])
const outputPromotionCategoryLabel = (category: ReferenceEntityCategory) => (
  category === 'character' ? '人物' : category === 'scene' ? '场景' : '道具'
)
const promotedAssetFor = (media: TaskOutputMedia) => (
  media.promotedAssetId ? studio.assets.find((asset) => asset.id === media.promotedAssetId) : undefined
)
const promotedAssetPurposeLabel = (media: TaskOutputMedia) => {
  const type=promotedAssetFor(media)?.type
  if(type==='character'||type==='scene'||type==='prop')return `${outputPromotionCategoryLabel(type)}分类参考图`
  return type==='image'?'完整首尾帧锚点':'视觉锚点'
}
const mediaKind = (media: TaskOutputMedia) => {
  const filename = media.filename.toLowerCase()
  if (/\.(mp4|webm|mov|m4v|mkv)$/.test(filename)) return 'video'
  if (/\.(png|jpe?g|webp|gif|avif)$/.test(filename)) return 'image'
  if (/\.(mp3|wav|ogg|m4a|aac|flac)$/.test(filename)) return 'audio'
  return ['video', 'image', 'audio'].includes(media.kind) ? media.kind : 'file'
}
const outputCount = (kind: 'video' | 'image' | 'audio') => selectedMedia.value.filter((media) => mediaKind(media) === kind).length
const emptyOutputMessage = computed(() => {
  if (selected.value?.status === 'completed') return '任务已完成，但 ComfyUI 没有返回可访问的媒体文件。请先刷新任务；若仍为空，请检查 ComfyUI 输出目录。'
  if (selected.value?.status === 'failed') return '任务执行失败，没有可预览的媒体产物。展开失败原因后可从所属 ProductionRun 处理。'
  return `任务完成后，原始${taskMediaLabel(selected.value)}会自动出现在这里${taskKind(selected.value)==='video'?'，无需另行组装':''}。`
})
const inspectedTask = computed(()=>studio.workflows.find((item)=>item.id===inspectedTaskId.value)||selected.value)
const selectedErrorMessage = computed(()=>selected.value?.status==='failed'?selected.value.errorMessage?.trim()||'':'')
const failureSummary = computed(()=>{
  const message=selectedErrorMessage.value
  if(!message)return '任务执行失败，服务端未返回详细错误。'
  const firstLine=message.split(/\r?\n/).map((line)=>line.trim()).find(Boolean)||message
  return firstLine.length>180?`${firstLine.slice(0,180)}…`:firstLine
})
const hasFailureDetail = computed(()=>Boolean(selectedErrorMessage.value))
const selectedFilterLabel = computed(()=>taskFilters.value.find((filter)=>filter.key===taskFilter.value)?.label||'当前')
const exportedWorkflowJson = computed(()=>workflowExport.value?JSON.stringify(workflowExport.value.workflowJson,null,2):'')
const isCanvasWorkflow = computed(()=>workflowExport.value?.format==='comfyui_ui')
const workflowFormatLabel = computed(()=>isCanvasWorkflow.value?'UI 画布 JSON':'API Prompt JSON')
const safeComfyUrl = computed(()=>{
  if(!workflowExport.value?.safeToOpen)return ''
  const candidate=workflowExport.value.directOpenSupported?workflowExport.value.directOpenUrl:workflowExport.value.comfyuiUrl
  if(!candidate)return ''
  try{const url=new URL(candidate);return ['http:','https:'].includes(url.protocol)?url.toString():''}catch{return ''}
})
const nodeIcons:Record<WorkflowNode['type'],typeof FileText>={script:FileText,prompt:Sparkles,image:Image,video:Film,audio:AudioLines,compose:Layers3,export:Download}
const deferredExtractionNodeStatus=(request:DeferredGenerationRequest):WorkflowNode['status']=>{
  if(request.status==='materialized')return 'completed'
  if(request.status==='extracting'||request.status==='materializing')return 'running'
  if(request.status==='failed'||request.status==='stale'||request.status==='cancelled')return 'failed'
  return 'pending'
}
const deferredExtractionNodeProgress=(request:DeferredGenerationRequest)=>request.status==='materialized'?100:request.status==='materializing'?85:request.status==='extracting'?45:0
const deferredExtractionNodeLabel=(request:DeferredGenerationRequest)=>(
  request.frameSlot==='first_frame'
    ? '尾帧抽取任务'
    : '首帧抽取任务'
)
const selectedDisplayNodes=computed<WorkflowNode[]>(()=>{
  const workflow=selected.value
  if(!workflow)return []
  const base=workflow.nodes||[]
  const taskId=selectedTaskId.value
  if(!taskId||taskKind(workflow)!=='video'||!deferredRequests.value.length)return base
  const nodes=deferredRequests.value
    .filter((request)=>request.sourceTaskId===taskId)
    .map((request)=>({
      id:`deferred-frame-${request.id}`,
      type:'compose' as const,
      label:`${deferredExtractionNodeLabel(request)}${request.materializedTaskId?' · 下游任务已创建':''}`,
      status:deferredExtractionNodeStatus(request),
      progress:deferredExtractionNodeProgress(request),
    }))
  return nodes.length?[...base,...nodes]:base
})
const nodeStatusCount=(status:WorkflowNode['status'])=>selectedDisplayNodes.value.filter((node)=>node.status===status).length||0
const nodeStatusLabel=(node:WorkflowNode)=>node.status==='completed'?'已完成':node.status==='running'?'正在执行':node.status==='failed'?'执行失败':'等待执行'
const nodeProgressLabel=(node:WorkflowNode)=>node.status==='running'&&selectedProgress.value
  ? selectedProgress.value.compactLabel
  : `${node.progress}%`
const nodeProgressWidth=(node:WorkflowNode)=>node.status==='running'&&selectedProgress.value?.mode==='comfyui'
  ? `${selectedProgress.value.percent || 0}%`
  : node.status==='running'&&selectedProgress.value?.indeterminate ? '8%' : `${node.progress}%`
const parsedTime = (value?: string) => {
  if (!value) return 0
  const time = new Date(value).getTime()
  return Number.isFinite(time) ? time : 0
}
const padTime = (value: number) => String(value).padStart(2, '0')
const formatTaskDateTime = (value?: string, includeSeconds=false) => {
  const time = parsedTime(value)
  if (!time) return '时间未知'
  const date = new Date(time)
  const datePart = `${padTime(date.getMonth()+1)}/${padTime(date.getDate())}`
  const timePart = `${padTime(date.getHours())}:${padTime(date.getMinutes())}${includeSeconds?`:${padTime(date.getSeconds())}`:''}`
  return `${datePart} ${timePart}`
}
const formatDuration = (milliseconds?: number) => {
  if (milliseconds === undefined || !Number.isFinite(milliseconds)) return '时长未知'
  if (milliseconds > 0 && milliseconds < 1000) return '<1秒'
  const seconds = Math.max(0, Math.round(milliseconds/1000))
  const hours = Math.floor(seconds/3600)
  const minutes = Math.floor((seconds%3600)/60)
  const remainder = seconds%60
  if (hours) return `${hours}时 ${padTime(minutes)}分 ${padTime(remainder)}秒`
  if (minutes) return `${minutes}分 ${padTime(remainder)}秒`
  return `${remainder}秒`
}
const taskStartAt = (workflow: Workflow) => workflow.startedAt || workflow.queuedAt || workflow.createdAt
const taskTimeKind = (workflow: Workflow) => workflow.startedAt ? '开始' : workflow.queuedAt ? '排队' : '创建'
const taskTimestampTitle = (workflow: Workflow) => {
  const time = parsedTime(taskStartAt(workflow))
  return time ? `${taskTimeKind(workflow)}时间：${new Date(time).toLocaleString('zh-CN')}` : '任务时间未知'
}
const taskDurationMs = (workflow: Workflow) => {
  if (workflow.status === 'queued') {
    if (workflow.queueWaitMs !== undefined) return workflow.queueWaitMs
    const queued = parsedTime(workflow.queuedAt || workflow.createdAt)
    return queued ? Math.max(0, nowMs.value-queued) : undefined
  }
  if (workflow.durationMs !== undefined) return workflow.durationMs
  const started = parsedTime(workflow.startedAt || workflow.submissionSubmittedAt || workflow.queuedAt || workflow.createdAt)
  const ended = parsedTime(workflow.finishedAt)
  if (started && ended) return Math.max(0, ended-started)
  if (started && isActiveTask(workflow)) return Math.max(0, nowMs.value-started)
  return workflow.totalDurationMs
}
const taskDurationLabel = (workflow: Workflow) => {
  const prefix = workflow.status === 'queued' ? '已等待' : isActiveTask(workflow) ? '已运行' : '耗时'
  const duration = taskDurationMs(workflow)
  return duration === undefined ? `${prefix}未知` : `${prefix} ${formatDuration(duration)}`
}
const taskGroupDurationLabel = (group: TaskListGroupEntry) => {
  const started = parsedTime(group.submittedAt)
  const ended = group.activeCount ? nowMs.value : parsedTime(group.lastActivityAt)
  if (!started || !ended) return group.activeCount ? '已运行未知' : '耗时未知'
  return `${group.activeCount ? '已运行' : '耗时'} ${formatDuration(Math.max(0, ended - started))}`
}
const selectedRuntimeAlert = computed<RuntimeAlert|null>(()=>{
  const workflow = selected.value
  if (!workflow || !isActiveTask(workflow)) return null
  const heartbeat = workflow.lastHeartbeatAt ? `最后心跳 ${formatTaskDateTime(workflow.lastHeartbeatAt, true)}。` : ''
  if (workflow.backendStatus === 'cancel_requested') return {
    level: 'warning', title: '取消请求正在处理',
    message: `${heartbeat}系统正在等待 ComfyUI 确认中断；请刷新只读诊断，或打开所属 ProductionRun 处理。`
  }
  if (workflow.isStalled) return {
    level: 'danger', title: '任务可能已经卡住',
    message: `${workflow.stallReason || '任务长时间没有新的执行心跳。'} ${heartbeat}可先检查 ComfyUI，再打开所属 ProductionRun 处理。`.trim()
  }
  if (healthLevel.value === 'degraded') return {
    level: 'warning', title: 'ComfyUI 健康检查异常',
    message: `${health.value?.message || 'ComfyUI API 可达，但健康接口返回异常。'} 当前任务可能仍在远端执行；页面会持续只读刷新，控制操作统一在所属 ProductionRun。`
  }
  if (healthLevel.value === 'unavailable') return {
    level: 'danger', title: '暂时无法连接 ComfyUI',
    message: `${health.value?.message || 'ComfyUI 当前不可达。'} 任务状态暂时无法得到远端确认，请勿重复创建相同任务；请打开所属 ProductionRun 处理。`
  }
  const progressState = progressPresentation(workflow)
  if (progressState.indeterminate) return {
    level: workflow.progressTelemetryState === 'unavailable' ? 'warning' : 'info',
    title: workflow.progressTelemetryState === 'unavailable' ? '量化进度暂不可用' : '任务正在执行',
    message: `${progressState.note || progressState.summary}${heartbeat}`
  }
  return null
})
const historyEventClass = (status: string, event: string, current=false) => {
  const value = `${status} ${event}`.toLowerCase()
  if (/fail|error|cancel/.test(value)) return 'error'
  if (/complete|success|succeed|finish/.test(value)) return 'success'
  if (current && /running|active|processing/.test(value)) return 'active'
  return 'info'
}
const logs = computed(()=>{
  const workflow = selected.value
  if (!workflow) return []
  // 旧任务没有 timeline 时，只从服务端保存的真实时间字段恢复记录，不生成固定或推测时间。
  const persisted = workflow.timeline?.length ? workflow.timeline : [
    workflow.queuedAt || workflow.createdAt ? { event:'queued', status:'completed', occurredAt:workflow.queuedAt || workflow.createdAt, label:'任务已进入生产队列', details:'记录来自任务排队时间' } : undefined,
    workflow.startedAt ? { event:'started', status:'completed', occurredAt:workflow.startedAt, label:'生产任务开始执行', details:'worker 已领取当前任务' } : undefined,
    workflow.submissionPreparedAt ? { event:'prepared', status:'completed', occurredAt:workflow.submissionPreparedAt, label:'ComfyUI 工作流已准备', details:'模型、提示词和输入参数已装配' } : undefined,
    workflow.submissionSubmittedAt ? { event:'submitted', status:workflow.finishedAt?'completed':'running', occurredAt:workflow.submissionSubmittedAt, label:'任务已提交至 ComfyUI', details:workflow.comfyPromptId?`Prompt ID：${workflow.comfyPromptId}`:'等待 ComfyUI 返回结果' } : undefined,
    workflow.finishedAt ? { event:workflow.status==='failed'?'failed':'completed', status:workflow.status==='failed'?'failed':'completed', occurredAt:workflow.finishedAt, label:workflow.status==='failed'?'任务执行失败':`${taskMediaLabel(workflow)}生成任务已完成`, details:workflow.status==='failed'?failureSummary.value:`实际执行耗时 ${formatDuration(taskDurationMs(workflow))}` } : undefined
  ].filter((event): event is NonNullable<typeof event> => Boolean(event))
  const ordered = persisted.slice().sort((left,right)=>parsedTime(left.occurredAt)-parsedTime(right.occurredAt))
  return ordered.map((event,index)=>({
    key:`${event.event}-${event.occurredAt}-${index}`,
    time:formatTaskDateTime(event.occurredAt,true),
    type:historyEventClass(event.status,event.event,isActiveTask(workflow)&&index===ordered.length-1),
    text:event.label,
    details:event.details
  }))
})
const taskStateSummary=(workflow:Workflow)=>workflow.inputContractStatus==='unavailable'?'历史执行合同不可展示 / LEGACY CONTRACT UNAVAILABLE':workflow.isVirtualProductionUnit&&workflow.status==='paused'?workflow.currentStep.replace(/^整集镜头\s+\S+\s+·\s*/,''):workflow.backendStatus==='cancel_requested'?'取消请求处理中':workflow.isStalled?workflow.stallReason||'任务可能已卡住':progressPresentation(workflow).summary
const taskProgressLabel=(workflow:Workflow)=>workflow.isVirtualProductionUnit&&workflow.status==='paused'?'等待恢复':workflow.backendStatus==='cancel_requested'?'取消中':workflow.isStalled?'需检查':progressPresentation(workflow).compactLabel
const hydrateSelectedTask=async(workflowId=selectedId.value,quiet=true,preserveMediaUrls=false)=>{
  const workflow=studio.workflows.find((item)=>item.id===workflowId)
  if(!workflow)return
  if(workflow.inputContractStatus==='unavailable')return workflow
  const taskId=workflowTaskId(workflow)
  if(!taskId)return workflow
  const requestSequence=++detailRequestSequence
  const requestEpoch=studio.captureSessionEpoch()
  try {
    const detail=await repository.getWorkflow(taskId)
    if(requestSequence!==detailRequestSequence||selectedId.value!==workflowId)return
    return studio.mergeWorkflowDetail(detail,preserveMediaUrls,requestEpoch)
  } catch(error) {
    if(requestSequence!==detailRequestSequence||selectedId.value!==workflowId)return
    if(!quiet)studio.notify(`读取任务详情失败：${error instanceof Error?error.message:'服务端未返回任务详情'}`,'error')
  }
}
const outputMediaIdentity=(media:TaskOutputMedia,index:number)=>`${media.index??index}:${media.filename}`
const outputMediaRecoveryKey=(media:TaskOutputMedia,index:number)=>`${selected.value?.taskId||selected.value?.id||''}:${outputMediaIdentity(media,index)}`
const safeOutputMediaUrl=(value:string|undefined)=>{
  const candidate=String(value||'').trim()
  if(!candidate)return ''
  if(candidate.startsWith('/')&&!candidate.startsWith('//'))return new URL(candidate,window.location.origin).toString()
  try{const url=new URL(candidate);return ['http:','https:'].includes(url.protocol)?url.toString():''}catch{return ''}
}
const refreshOutputMedia=async(media:TaskOutputMedia,index:number)=>{
  const workflow=selected.value
  if(!workflow)throw new Error('当前任务不存在')
  const workflowId=workflow.id
  const taskId=workflow.taskId||workflow.id
  const requestEpoch=studio.captureSessionEpoch()
  const detail=await repository.getWorkflow(taskId)
  if(selectedId.value!==workflowId)throw new Error('当前已切换到其他任务')
  const merged=studio.mergeWorkflowDetail(detail,false,requestEpoch)
  const identity=outputMediaIdentity(media,index)
  return merged.outputMedia?.find((item,itemIndex)=>outputMediaIdentity(item,itemIndex)===identity)
    || merged.outputMedia?.[index]
}
const handleOutputMediaError=async(media:TaskOutputMedia,index:number)=>{
  const failedUrl=media.previewUrl||media.downloadUrl||''
  const key=outputMediaRecoveryKey(media,index)
  if((outputMediaRecoveryAttempts.get(key)||0)>=1)return
  outputMediaRecoveryAttempts.set(key,1)
  try{
    const fresh=await refreshOutputMedia(media,index)
    if(!fresh?.previewUrl||fresh.previewUrl===failedUrl)throw new Error('服务端未返回新的预览地址')
  }catch(error){studio.notify(`产物地址刷新失败：${error instanceof Error?error.message:'请稍后重试'}`,'error')}
}
const handleOutputMediaLoaded=(media:TaskOutputMedia,index:number)=>{outputMediaRecoveryAttempts.delete(outputMediaRecoveryKey(media,index))}
const openOutputMedia=async(media:TaskOutputMedia,index:number)=>{
  const opened=window.open('about:blank','_blank')
  if(opened)opened.opener=null
  try{
    const fresh=await refreshOutputMedia(media,index)
    const url=safeOutputMediaUrl(fresh?.previewUrl||fresh?.downloadUrl)
    if(!url)throw new Error('任务没有可访问的预览地址')
    if(opened)opened.location.href=url
    else window.location.assign(url)
  }catch(error){opened?.close();studio.notify(`打开预览失败：${error instanceof Error?error.message:'请稍后重试'}`,'error')}
}
const downloadOutputMedia=async(media:TaskOutputMedia,index:number)=>{
  try{
    const fresh=await refreshOutputMedia(media,index)
    const url=safeOutputMediaUrl(fresh?.downloadUrl)
    if(!url)throw new Error('公开发行副本尚未生成或未通过完整性校验')
    const anchor=document.createElement('a');anchor.href=url;anchor.download=fresh?.filename||media.filename;anchor.rel='noopener';document.body.appendChild(anchor);anchor.click();anchor.remove()
  }catch(error){studio.notify(`下载失败：${error instanceof Error?error.message:'请稍后重试'}`,'error')}
}
const reconcileSelected=()=>{
  if(selectedAssetJob.value||visibleWorkflowIds.value.has(selectedId.value))return
  const nextId=filteredAssetJobs.value[0]?.id||taskListEntryFirstTask(filteredTaskListEntries.value[0])?.id||''
  if(selectedId.value!==nextId){selectedId.value=nextId;failureExpanded.value=false}
}
const setTaskFilter=(filter:UnifiedTaskFilter)=>{taskFilter.value=filter;taskMenuOpen.value=false;parametersExpanded.value=false;reconcileSelected();if(selected.value)void hydrateSelectedTask(selected.value.id)}
const setTaskScope=(scope:TaskScope)=>{taskScope.value=scope;taskMenuOpen.value=false;parametersExpanded.value=false;reconcileSelected();void router.replace({name:'workflows',query:{scope,...(selectedId.value?{task:selectedId.value}:{})}});if(selected.value)void hydrateSelectedTask(selected.value.id)}
const selectTask=(id:string)=>{const workflow=studio.workflows.find((item)=>item.id===id);const groupKey=workflow?taskListGroupKey(workflow):'';if(groupKey)expandGroup(groupKey);if(selectedId.value!==id){failureExpanded.value=false;parametersExpanded.value=false};taskMenuOpen.value=false;selectedId.value=id;void router.replace({name:'workflows',query:{scope:taskScope.value,task:id}});void hydrateSelectedTask(id,false)}
const selectAssetJob=(id:string)=>{if(selectedId.value!==id){failureExpanded.value=false;parametersExpanded.value=false};taskMenuOpen.value=false;selectedId.value=id;void router.replace({name:'workflows',query:{scope:taskScope.value==='all'?'all':'asset',task:id}})}
const closeTaskMenu=()=>{taskMenuOpen.value=false}
const onTaskMenuPointerDown=(event:PointerEvent)=>{if(taskMenuOpen.value&&event.target instanceof Node&&!taskMenuRoot.value?.contains(event.target))closeTaskMenu()}
const onTaskMenuKeydown=(event:KeyboardEvent)=>{if(event.key==='Escape'&&taskMenuOpen.value){event.preventDefault();closeTaskMenu()}}
const copyPlainText=async(text:string,label:string)=>{
  try {
    if(navigator.clipboard?.writeText) await navigator.clipboard.writeText(text)
    else {
      const field=document.createElement('textarea');field.value=text;field.style.position='fixed';field.style.opacity='0';document.body.appendChild(field);field.select();
      try { if(!document.execCommand('copy'))throw new Error('浏览器拒绝复制') } finally { field.remove() }
    }
    studio.notify(`${label}已复制`)
  } catch(error) { studio.notify(`${label}复制失败：${error instanceof Error?error.message:'请检查浏览器剪贴板权限'}`,'error') }
}
const openInputPrompt=async(force=false)=>{
  const workflow=selected.value;if(!workflow)return;closeTaskMenu();promptOpen.value=true
  if(workflow.inputContractStatus==='unavailable'){
    promptLoading.value=false;inputPrompt.value=null
    promptError.value='历史任务缺少当前可验证的冻结输入合同；系统不会回填或推断实际提交提示词。 / Legacy task input contract is unavailable; submitted prompts are not inferred.'
    return
  }
  const taskId=workflow.taskId||workflow.id
  if(!force&&promptTaskId.value===taskId&&inputPrompt.value)return
  const requestSequence=++promptRequestSequence
  promptTaskId.value=taskId;promptLoading.value=true;promptError.value='';inputPrompt.value=null
  try{
    const result=await repository.getTaskInputPrompt(taskId)
    if(requestSequence!==promptRequestSequence||promptTaskId.value!==taskId)return
    inputPrompt.value=result
  }
  catch(error){
    if(requestSequence!==promptRequestSequence||promptTaskId.value!==taskId)return
    promptError.value=error instanceof Error?error.message:'服务端未返回实际提交提示词'
  }
  finally{if(requestSequence===promptRequestSequence&&promptTaskId.value===taskId)promptLoading.value=false}
}
const copyPromptText=async(kind:'prompt'|'negative')=>{
  const value=kind==='prompt'?inputPrompt.value?.prompt:inputPrompt.value?.negativePrompt
  if(!value)return;await copyPlainText(value,kind==='prompt'?'实际提交提示词':'负面提示词')
}
const referenceImageCandidates=()=>[...selectedReferenceAssets.value,...selectedContinuityAssets.value]
const recoverReferenceImage=async(asset:TaskReferenceAssetSummary,index=0,manual=false)=>{
  const key=referenceAssetKey(asset,index)
  referenceImageFailures.value={...referenceImageFailures.value,[key]:true}
  if(!manual&&(referenceImageRecoveryAttempts.get(key)||0)>=1)return
  if(manual)referenceImageRecoveryAttempts.delete(key)
  referenceImageRecoveryAttempts.set(key,(referenceImageRecoveryAttempts.get(key)||0)+1)
  const workflowId=selectedId.value
  try{
    await hydrateSelectedTask(workflowId,false,false)
    if(selectedId.value!==workflowId)return
    const fresh=referenceImageCandidates().find((candidate,candidateIndex)=>referenceAssetKey(candidate,candidateIndex)===key)
    if(!fresh||!publicReferenceUrl(fresh))throw new Error('服务端未返回新的参考图预览地址')
    if(referencePreviewAsset.value&&referenceAssetKey(referencePreviewAsset.value)===key)referencePreviewAsset.value=fresh
    referenceImageFailures.value={...referenceImageFailures.value,[key]:false}
  }catch(error){if(manual)studio.notify(`参考图预览刷新失败：${error instanceof Error?error.message:'请稍后重试'}`,'error')}
}
const handleReferenceImageLoaded=(asset:TaskReferenceAssetSummary,index=0)=>{const key=referenceAssetKey(asset,index);referenceImageRecoveryAttempts.delete(key);if(referenceImageFailures.value[key])referenceImageFailures.value={...referenceImageFailures.value,[key]:false}}
const openReferenceImage=(asset:TaskReferenceAssetSummary,index=0)=>{
  if(asset.mediaKind!=='image'||!publicReferenceUrl(asset)||referenceImageFailures.value[referenceAssetKey(asset,index)])return
  referencePreviewAsset.value=asset;referencePreviewOpen.value=true
}
const copyTaskId=async()=>{const taskId=selected.value?.taskId||selected.value?.id;if(!taskId)return;closeTaskMenu();await copyPlainText(taskId,'任务 ID')}
const copyComfyPromptId=async()=>{const promptId=selected.value?.comfyPromptId;if(!promptId)return;closeTaskMenu();await copyPlainText(promptId,'Comfy Prompt ID')}
const copyInputParameters=async()=>{if(!selected.value||selectedInputContractUnavailable.value)return;closeTaskMenu();await copyPlainText(JSON.stringify(selectedInputParameters.value,null,2),'输入参数 JSON')}
const downloadTaskConfig=()=>{
  const workflow=selected.value;if(!workflow||selectedInputContractUnavailable.value)return;closeTaskMenu()
  const blob=new Blob([JSON.stringify(selectedTaskConfig.value,null,2)],{type:'application/json;charset=utf-8'})
  const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download=`task-${workflow.taskId||workflow.id}-config.json`;anchor.click();window.setTimeout(()=>URL.revokeObjectURL(url),0);studio.notify('任务配置 JSON 已下载')
}
const refreshTaskStatus=async()=>{
  const taskId=selected.value?.id;if(!taskId)return;closeTaskMenu()
  try{await studio.refreshWorkflows(true);pollError.value='';selectedId.value=studio.workflows.some((workflow)=>workflow.id===taskId)?taskId:selectedId.value;reconcileSelected();if(selectedId.value)await hydrateSelectedTask(selectedId.value);studio.notify('任务状态已刷新')}
  catch(error){const message=error instanceof Error?error.message:'服务端未返回最新任务状态';pollError.value=message;studio.notify(`刷新任务状态失败：${message}`,'error')}
}
const loadProductionCapability=async()=>{productionCapabilityError.value='';try{productionCapability.value=await repository.getProductionCapability()}catch(error){productionCapability.value=null;productionCapabilityError.value=error instanceof Error?error.message:'无法读取当前生产队列配置'}}
const refreshAssetJobs=async()=>{try{await studio.refreshAssetGenerationJobs();assetJobsError.value='';reconcileSelected()}catch(error){assetJobsError.value=error instanceof Error?error.message:'素材任务刷新失败'}}
const continueFromPrompt=(job:AssetGenerationJob)=>{if(job.jobType!=='prompt'||job.status!=='succeeded'||!job.resultText)return;void router.push({name:'assets',query:{type:job.mediaType,promptJob:job.id}})}
const openAssetJobOutput=(job:AssetGenerationJob)=>{if(!job.outputAsset)return;void router.push({name:'assets',query:{type:job.mediaType,asset:job.outputAsset.id}})}
const cancelAssetJob=async(job:AssetGenerationJob)=>{try{await studio.cancelAssetGenerationJob(job.id);selectedId.value=job.id}catch(error){studio.notify(error instanceof Error?error.message:'取消失败','error')}}
const retryAssetJob=async(job:AssetGenerationJob)=>{try{const retried=await studio.retryAssetGenerationJob(job.id);selectedId.value=retried.id;taskScope.value='asset';void router.replace({name:'workflows',query:{scope:'asset',task:retried.id}})}catch(error){studio.notify(error instanceof Error?error.message:'重试失败','error')}}
const downloadAssetJobOutput=async(job:AssetGenerationJob)=>{if(!job.outputAsset)return;try{const asset=await studio.refreshAsset(job.outputAsset.id,false);if(!asset.downloadUrl)throw new Error('该素材没有可下载的受管文件');const anchor=document.createElement('a');anchor.href=asset.downloadUrl;anchor.rel='noopener';anchor.download='';document.body.appendChild(anchor);anchor.click();anchor.remove()}catch(error){studio.notify(error instanceof Error?error.message:'下载失败','error')}}
const openUncertainResolution=(job:AssetGenerationJob)=>{if(!auth.user?.isStaff){studio.notify('远端状态不确定，请联系平台管理员核对请求、终态与账单后消歧。','info');return}if(resolutionIntentJobId!==job.id){resolutionIntentJobId=job.id;resolutionForm.value=blankResolutionForm()}resolutionTarget.value=job;resolutionError.value=''}
const closeUncertainResolution=()=>{if(resolutionPending.value)return;resolutionTarget.value=null;resolutionError.value=''}
const validateUncertainResolution=()=>{
  const job=resolutionTarget.value;const form=resolutionForm.value;if(!job)return '待消歧任务已失效，请刷新后重试'
  const note=form.note.trim();const requestId=form.providerRequestId.trim();const cost=form.actualCost.trim();const currency=form.costCurrency.trim().toUpperCase();const limits=productionCapability.value?.effectiveLimits
  if(!limits)return productionCapabilityError.value||'服务端审计说明限制不可用，不能提交消歧结论'
  if(note.length<limits.auditNoteMinCharacters||note.length>limits.auditNoteMaxCharacters)return `消歧说明必须为 ${limits.auditNoteMinCharacters} 到 ${limits.auditNoteMaxCharacters} 个字符`
  if(requestId.length>191)return 'Provider Request ID 不能超过 191 个字符'
  if(form.outcome==='confirmed_not_submitted'&&(requestId||job.providerRequestIdSha256))return '任务已有 Provider Request ID 摘要，不能确认未提交'
  if(form.outcome!=='confirmed_not_submitted'&&job.operation!=='script.generate'&&!requestId&&!job.providerRequestIdSha256)return '媒体远端结论必须填写 Provider Request ID，或由任务保留已核验摘要'
  if(cost&&!/^(?:0|[1-9]\d{0,7})(?:\.\d{1,6})?$/.test(cost))return '实际费用必须是最多 8 位整数、6 位小数的非负数'
  if(form.outcome==='confirmed_not_submitted'&&cost&&Number(cost)!==0)return '确认未提交时实际费用只能留空或填 0'
  if(currency&&!/^[A-Z][A-Z0-9]{2,11}$/.test(currency))return '费用币种必须是 3 到 12 位大写字母或数字，且以字母开头'
  if(cost&&!currency)return '填写实际费用时必须同时填写币种'
  return ''
}
const submitUncertainResolution=async()=>{if(resolutionPending.value||!resolutionTarget.value)return;if(!auth.user?.isStaff){resolutionError.value='只有平台管理员可以提交消歧结论';return}const validation=validateUncertainResolution();if(validation){resolutionError.value=validation;return}const jobId=resolutionTarget.value.id;const form=resolutionForm.value;resolutionPending.value=true;resolutionError.value='';try{await studio.resolveUncertainAssetGenerationJob(jobId,{outcome:form.outcome,note:form.note.trim(),providerRequestId:form.providerRequestId.trim(),actualCost:form.actualCost.trim()||null,costCurrency:form.costCurrency.trim().toUpperCase(),idempotencyKey:form.idempotencyKey});resolutionTarget.value=null;resolutionIntentJobId='';resolutionForm.value=blankResolutionForm();assetJobsError.value='';selectedId.value=jobId}catch(error){resolutionError.value=error instanceof Error?error.message:'素材生成任务消歧失败'}finally{resolutionPending.value=false}}
const goToProduction = () => {
  const projectId = selected.value?.projectId || studio.projects[0]?.id
  void router.push(projectId ? `/projects/${projectId}/production` : '/projects')
}
const refreshProductionTasks=async(preserveMediaUrls=false)=>{try{await studio.refreshWorkflows(preserveMediaUrls);pollError.value='';reconcileSelected();if(selected.value)await hydrateSelectedTask(selected.value.id,true,preserveMediaUrls)}catch(error){pollError.value=error instanceof Error?error.message:'短剧与独立媒体任务刷新失败'}}
const refresh=async(preserveMediaUrls=false)=>{await Promise.all([refreshProductionTasks(preserveMediaUrls),refreshAssetJobs()])}
watch(()=>taskListEntries.value.map((entry)=>entry.kind==='group'?`${entry.key}:${entry.status}`:entry.key).join('|'),syncDefaultGroupExpansion,{immediate:true})
watch(selectedTaskId,(taskId)=>{
  deferredRequests.value=[];deferredError.value=''
  if(taskId)void loadDeferredRequests(taskId,true)
})
const loadWorkflowExport=async(task:Workflow,format:'ui'|'api')=>{const requestSequence=++workflowInspectRequestSequence;workflowInspectLoading.value=true;workflowInspectError.value='';workflowExport.value=null;try{const result=await repository.getGeneratedWorkflowExport(task,format);if(requestSequence===workflowInspectRequestSequence)workflowExport.value=result}catch(error){if(requestSequence===workflowInspectRequestSequence)workflowInspectError.value=error instanceof Error?error.message:'读取工作流失败'}finally{if(requestSequence===workflowInspectRequestSequence)workflowInspectLoading.value=false}}
const inspectWorkflow=async(task:Workflow)=>{if(task.inputContractStatus==='unavailable')return;inspectedTaskId.value=task.id;workflowInspectOpen.value=true;workflowExportFormat.value='ui';await loadWorkflowExport(task,'ui')}
const switchWorkflowFormat=async(format:'ui'|'api')=>{if(format===workflowExportFormat.value&&workflowExport.value)return;workflowExportFormat.value=format;const task=inspectedTask.value;if(task)await loadWorkflowExport(task,format)}
const copyWorkflow=async()=>{if(!exportedWorkflowJson.value)return;try{await navigator.clipboard.writeText(exportedWorkflowJson.value);studio.notify('实际 ComfyUI 工作流 JSON 已复制')}catch{studio.notify('复制失败，请使用下载 JSON','error')}}
const downloadWorkflow=()=>{if(!workflowExport.value||!exportedWorkflowJson.value)return;const blob=new Blob([exportedWorkflowJson.value],{type:'application/json;charset=utf-8'});const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download=workflowExport.value.filename;anchor.click();window.setTimeout(()=>URL.revokeObjectURL(url),0);studio.notify(`${workflowFormatLabel.value} 已下载`)}
const openInComfy=()=>{if(!safeComfyUrl.value){studio.notify(workflowExport.value?.openReason||'当前没有可安全打开的 ComfyUI 地址','error');return}const opened=window.open(safeComfyUrl.value,'_blank','noopener,noreferrer');if(opened)opened.opener=null}
const checkHealth=async()=>{if(healthChecking.value)return;healthChecking.value=true;try{health.value=await repository.testComfy('')}catch(error){health.value={ok:false,message:error instanceof Error?error.message:'ComfyUI 不可达'}}finally{healthChecking.value=false}}
onMounted(async()=>{
  document.addEventListener('pointerdown',onTaskMenuPointerDown);document.addEventListener('keydown',onTaskMenuKeydown)
  await Promise.all([studio.initialize(),loadProductionCapability(),refreshAssetJobs()])
  const requestedTask=String(Array.isArray(route.query.task)?route.query.task[0]||'':route.query.task||'');const requestedBatch=String(Array.isArray(route.query.batch)?route.query.batch[0]||'':route.query.batch||'')
  const assetTarget=studio.assetGenerationJobs.find((item)=>item.id===requestedTask)
  const target=studio.workflows.find((item)=>item.id===requestedTask||item.taskId===requestedTask)||studio.workflows.filter((item)=>requestedBatch&&item.batchId===requestedBatch).sort((left,right)=>(left.batchIndex||0)-(right.batchIndex||0))[0]
  if(assetTarget){taskScope.value='asset';taskFilter.value='all';selectedId.value=assetTarget.id}
  else if(target){taskFilter.value='all';selectedId.value=target.id;const groupKey=taskListGroupKey(target);if(groupKey)expandGroup(groupKey)}
  else reconcileSelected()
  if(selected.value)await hydrateSelectedTask(selected.value.id)
  await checkHealth();ticker=window.setInterval(()=>refresh(true),5000);healthTicker=window.setInterval(()=>{void checkHealth()},15000);clockTicker=window.setInterval(()=>{nowMs.value=Date.now()},1000)
})
onBeforeUnmount(()=>{promptRequestSequence+=1;detailRequestSequence+=1;workflowInspectRequestSequence+=1;document.removeEventListener('pointerdown',onTaskMenuPointerDown);document.removeEventListener('keydown',onTaskMenuKeydown);window.clearInterval(ticker);window.clearInterval(healthTicker);window.clearInterval(clockTicker)})
</script>

<template>
  <div class="workflow-page page-stack">
    <section class="page-heading split-heading"><div><span class="eyebrow">统一任务中心 / PRODUCTION WORKFLOWS</span><h2>生产工作流</h2><p>统一追踪素材生成、短剧生产和独立媒体任务；短剧任务保持只读诊断，素材任务在此完成取消、重试与审计处理。</p></div><div class="workflow-heading-actions"><RouterLink class="button ghost" :to="{name:'assets'}"><Plus :size="16"/>新增素材</RouterLink><button class="button primary" @click="goToProduction"><Plus :size="16"/>进入一键生产</button></div></section>
    <section class="pipeline-health panel"><div class="health-item"><span class="health-icon" :class="{online:healthLevel==='connected',degraded:healthLevel==='degraded'}"><Server :size="18"/></span><div><span>ComfyUI 服务 · 仅适用于相关媒体任务</span><strong>{{ health?.message || '正在探测服务端连接…' }}</strong></div><i :class="{neutral:healthLevel==='checking'||healthLevel==='unavailable',warning:healthLevel==='degraded'}">{{ healthBadge }}</i></div><div class="health-divider"></div><div class="health-item"><span class="health-icon"><Cpu :size="18"/></span><div><span>短剧媒体队列</span><strong>{{ productionCapability?`有效并发 ${productionCapability.maxConcurrent} · 队列 ${productionCapability.activeTaskCount}/${productionCapability.recommendedMaxPending} · 我的活跃任务 ${productionCapability.userActiveTaskCount}/${productionCapability.recommendedMaxActivePerUser}`:productionCapabilityError?'生产策略读取失败':'正在读取生产策略…' }}</strong></div><i :class="{neutral:!queueGuidanceExceeded,warning:queueGuidanceExceeded}">{{ productionCapability?(queueGuidanceExceeded?'超过建议值（不阻断）':`并发数（CONCURRENCY）${productionCapability.maxConcurrent}`):productionCapabilityError?'不可用（UNAVAILABLE）':'加载中（LOADING）' }}</i></div><div class="health-divider"></div><div class="health-item"><span class="health-icon"><Sparkles :size="18"/></span><div><span>任务路由冻结</span><strong>短剧由 ProductionRun 冻结；素材由 AssetGenerationJob 冻结</strong></div><i class="neutral">按任务类型</i></div><RouterLink to="/settings" class="button ghost small">连接状态 <ArrowRight :size="14"/></RouterLink></section>

    <section class="workflow-layout">
      <aside class="panel workflow-list">
        <header><div><span class="eyebrow">任务队列 / TASK QUEUE</span><h3>全部工作流</h3></div><button class="icon-btn" :disabled="studio.loading" title="刷新任务列表" aria-label="刷新任务列表" @click="refresh(false)"><RefreshCw :size="15"/></button></header>
        <p v-if="pollError" class="form-error">短剧/媒体队列刷新失败：{{ pollError }}</p><p v-if="assetJobsError" class="form-error">素材队列刷新失败：{{ assetJobsError }}</p>
        <div class="workflow-scope-filters" role="group" aria-label="按任务来源筛选"><button v-for="scope in taskScopes" :key="scope.key" type="button" :class="{active:taskScope===scope.key}" :aria-pressed="taskScope===scope.key" @click="setTaskScope(scope.key)"><span>{{ scope.label }}</span><b>{{ scope.count }}</b></button></div>
        <div class="workflow-filters" role="group" aria-label="按任务状态筛选">
          <button v-for="filter in taskFilters" :key="filter.key" type="button" :data-status-filter="filter.key" :class="{active:taskFilter===filter.key}" :aria-pressed="taskFilter===filter.key" :aria-label="`${filter.label}，${filter.count} 个任务项`" @click="setTaskFilter(filter.key)"><span>{{ filter.label }}</span><b>{{ filter.count }}</b></button>
        </div>
        <div class="workflow-list-scroll">
          <div v-if="filteredAssetJobs.length" class="workflow-kind-divider asset"><WandSparkles :size="12"/><span>素材任务</span><b>{{ filteredAssetJobs.length }}</b></div>
          <button v-for="job in filteredAssetJobs" :key="`asset:${job.id}`" type="button" class="asset-workflow-list-item" :data-asset-job-id="job.id" :class="[job.status,{active:selectedAssetJob?.id===job.id}]" :aria-pressed="selectedAssetJob?.id===job.id" @click="selectAssetJob(job.id)">
            <div class="workflow-item-top"><StatusBadge :status="assetJobBadgeStatus(job)" :label="assetJobStatusLabel(job.status)"/><b>素材任务 · {{ assetMediaLabel(job) }}</b><time :datetime="job.createdAt">{{ formatTaskDateTime(job.createdAt) }}</time></div>
            <strong :title="assetJobTitle(job)">{{ assetJobTitle(job) }}</strong>
            <div class="workflow-item-summary"><p>{{ assetJobExecutionLabel(job) }}</p><span><component :is="assetJobIcon(job)" :size="10"/>{{ job.operation }}</span></div>
            <div class="workflow-item-progress" :class="job.status"><i><b :style="{width:`${assetJobProgress(job)}%`}"></b></i><span>{{ assetJobStatusLabel(job.status) }}</span></div>
          </button>
          <div v-if="filteredTaskListEntries.length" class="workflow-kind-divider story"><Clapperboard :size="12"/><span>{{ taskScope==='media'?'独立媒体任务':'短剧与媒体任务' }}</span><b>{{ filteredTaskListEntries.length }}</b></div>
          <template v-for="entry in filteredTaskListEntries" :key="entry.key">
            <article v-if="entry.kind==='group'" class="workflow-task-group" :class="[entry.status,{collapsed:!isGroupExpanded(entry.key)}]" :data-task-group-key="entry.key">
              <div class="workflow-task-group-heading">
                <button type="button" class="workflow-task-group-toggle" :aria-expanded="isGroupExpanded(entry.key)" :aria-label="`${isGroupExpanded(entry.key)?'折叠':'展开'}整集任务：${taskGroupTitle(entry)}`" @click="toggleGroupExpansion(entry.key)">
                  <ChevronDown :size="14"/>
                  <span><small>{{ entry.groupKind==='production_run_episode'?'短剧任务 · 整集':'独立媒体 · 历史批次' }}</small><strong>{{ taskGroupTitle(entry) }}</strong><em>下发 {{ taskGroupSubmittedLabel(entry) }} · {{ entry.unitCount }} 个生产单元 · {{ taskGroupIdentity(entry) }}</em></span>
                </button>
                <div class="workflow-task-group-actions" aria-label="整集任务状态与入口">
                  <StatusBadge :status="entry.status"/>
                  <RouterLink v-if="entry.groupKind==='production_run_episode'&&entry.productionRunId" :to="`/projects/${entry.projectId}/production/${entry.productionRunId}`" title="打开所属 ProductionRun"><ExternalLink :size="11"/>打开运行</RouterLink>
                  <span class="platform-status muted">只读诊断</span>
                </div>
              </div>
              <div class="workflow-task-group-summary">
                <p><span>{{ taskGroupStatusSummary(entry) }}</span><b><Clock3 :size="10"/>{{ taskGroupDurationLabel(entry) }}</b></p>
                <div class="workflow-task-group-progress" :class="{indeterminate:entry.indeterminate}"><i><b :style="{width:entry.indeterminate?'8%':`${entry.completionPercent}%`}"></b></i><strong>{{ entry.completionPercent }}%</strong></div>
              </div>
            </article>
            <template v-if="entry.kind==='group'&&isGroupExpanded(entry.key)">
              <button v-for="flow in entry.tasks" :key="flow.id" type="button" class="workflow-group-child" :data-task-id="flow.id" :class="[flow.status,{active:selected?.id===flow.id}]" :aria-pressed="selected?.id===flow.id" @click="selectTask(flow.id)">
                <div class="workflow-item-top"><StatusBadge :status="flow.status"/><b>{{ taskGroupTaskLabel(flow,entry) }}</b><time :datetime="taskStartAt(flow)" :title="taskTimestampTitle(flow)">{{ formatTaskDateTime(taskStartAt(flow)) }}</time></div>
                <strong :title="flow.projectTitle">{{ shotContextLabel(flow) || flow.projectTitle }}</strong>
                <div class="workflow-item-summary"><p>{{ taskStateSummary(flow) }}</p><span><Clock3 :size="10"/>{{ taskDurationLabel(flow) }}</span></div>
                <div class="workflow-item-progress" :class="[flow.status,{indeterminate:progressPresentation(flow).indeterminate}]"><i><b :style="{width:progressPresentation(flow).indeterminate?'8%':`${progressPresentation(flow).percent || 0}%`}"></b></i><span>{{ taskProgressLabel(flow) }}</span></div>
              </button>
            </template>
            <button v-else-if="entry.kind==='task'" type="button" :data-task-id="entry.task.id" :class="[entry.task.status,{active:selected?.id===entry.task.id}]" :aria-pressed="selected?.id===entry.task.id" @click="selectTask(entry.task.id)">
              <div class="workflow-item-top"><StatusBadge :status="entry.task.status"/><b>{{ entry.task.productionRunId?'短剧任务':'独立媒体' }}</b><time :datetime="taskStartAt(entry.task)" :title="taskTimestampTitle(entry.task)">{{ formatTaskDateTime(taskStartAt(entry.task)) }}</time></div>
              <strong :title="entry.task.projectTitle">{{ entry.task.projectTitle }}</strong>
              <div class="workflow-item-summary"><p>{{ taskStateSummary(entry.task) }}</p><span><Clock3 :size="10"/>{{ taskDurationLabel(entry.task) }}</span></div>
              <div class="workflow-item-progress" :class="[entry.task.status,{indeterminate:progressPresentation(entry.task).indeterminate}]"><i><b :style="{width:progressPresentation(entry.task).indeterminate?'8%':`${progressPresentation(entry.task).percent || 0}%`}"></b></i><span>{{ taskProgressLabel(entry.task) }}</span></div>
            </button>
          </template>
          <div v-if="!filteredTaskListEntries.length&&!filteredAssetJobs.length" class="workflow-filter-empty" role="status"><WorkflowIcon :size="24"/><strong>{{ studio.workflows.length||studio.assetGenerationJobs.length?`${selectedFilterLabel}暂无任务`:'暂无生产任务' }}</strong><p>{{ studio.workflows.length||studio.assetGenerationJobs.length?'切换其他来源或状态即可查看对应任务。':'从素材库创建素材任务，或从项目生产运行创建短剧任务。' }}</p></div>
        </div>
      </aside>

      <main v-if="selectedAssetJob" class="workflow-detail asset-workflow-detail">
        <section class="panel workflow-overview asset-workflow-overview">
          <header><div><span class="eyebrow">素材工作流（ASSET WORKFLOW）{{ selectedAssetJob.id.toUpperCase() }}</span><h2>{{ assetJobTitle(selectedAssetJob) }}</h2><div><StatusBadge :status="assetJobBadgeStatus(selectedAssetJob)" :label="assetJobStatusLabel(selectedAssetJob.status)"/><span class="workflow-kind-pill asset">素材任务 · {{ assetMediaLabel(selectedAssetJob) }}</span><span><Clock3 :size="13"/>创建于 {{ formatTaskDateTime(selectedAssetJob.createdAt,true) }}</span><span v-if="selectedAssetJob.finishedAt">完成于 {{ formatTaskDateTime(selectedAssetJob.finishedAt,true) }}</span></div></div>
            <div class="workflow-main-actions asset-workflow-actions"><button v-if="selectedAssetJob.jobType==='prompt'&&selectedAssetJob.status==='succeeded'&&selectedAssetJob.resultText" class="button primary" @click="continueFromPrompt(selectedAssetJob)"><WandSparkles :size="15"/>采用并继续生成</button><button v-if="selectedAssetJob.outputAsset" class="button primary" @click="openAssetJobOutput(selectedAssetJob)"><Eye :size="15"/>查看素材</button><button v-if="selectedAssetJob.outputAsset" class="button ghost" @click="downloadAssetJobOutput(selectedAssetJob)"><Download :size="15"/>下载</button><button v-if="assetJobActive(selectedAssetJob)&&selectedAssetJob.status!=='cancel_requested'" class="button ghost" @click="cancelAssetJob(selectedAssetJob)">取消任务</button><button v-if="selectedAssetJob.status==='uncertain'&&auth.user?.isStaff" class="button primary" @click="openUncertainResolution(selectedAssetJob)">人工消歧</button><button v-if="['failed','cancelled'].includes(selectedAssetJob.status)" class="button ghost" @click="retryAssetJob(selectedAssetJob)"><RefreshCw :size="15"/>重试</button></div>
          </header>
          <div v-if="selectedAssetJob.status==='uncertain'" class="task-runtime-alert warning"><AlertTriangle :size="16"/><div><strong>远端状态待核对</strong><p>{{ auth.user?.isStaff?'请在服务提供方控制台核对请求、终态与账单后提交人工消歧。':'请联系平台管理员核对远端请求、终态与账单；不要直接重试。' }}</p></div></div>
          <div v-else-if="selectedAssetJob.errorMessage" class="task-runtime-alert danger"><AlertTriangle :size="16"/><div><strong>{{ selectedAssetJob.errorCode || '素材工作流执行失败' }}</strong><p>{{ selectedAssetJob.errorMessage }}</p></div></div>
          <div class="big-progress" :class="{indeterminate:assetJobActive(selectedAssetJob)&&!['cancel_requested'].includes(selectedAssetJob.status)}"><div><span>{{ assetJobExecutionLabel(selectedAssetJob) }}</span><strong>{{ assetJobProgress(selectedAssetJob) }}%</strong></div><i><b :style="{width:`${assetJobProgress(selectedAssetJob)}%`}"></b></i><p>{{ assetJobStatusLabel(selectedAssetJob.status) }}<template v-if="selectedAssetJob.remoteStatus"> · 远端状态 {{ selectedAssetJob.remoteStatus }}</template></p></div>
        </section>

        <section class="asset-workflow-grid">
          <article class="panel asset-workflow-facts"><header><span class="eyebrow">冻结执行合同 / FROZEN ROUTE</span><h3>模型与工作流</h3></header><dl><div><dt>执行入口</dt><dd>{{ selectedAssetJob.executionKind==='comfy_workflow'?'ComfyUI 工作流':'异步 API Deployment' }}</dd></div><div><dt>运行部署</dt><dd>{{ selectedAssetJob.modelDeploymentName || '—' }}</dd></div><div><dt>工作流版本</dt><dd>{{ selectedAssetJob.workflowName ? `${selectedAssetJob.workflowName} v${selectedAssetJob.workflowVersion || '—'}` : '不适用' }}</dd></div><div><dt>操作能力</dt><dd><code>{{ selectedAssetJob.operation }}</code></dd></div><div><dt>外部调用</dt><dd>{{ selectedAssetJob.isExternal ? (selectedAssetJob.allowExternalCalls?'已授权':'未授权') : '本地/受管' }}</dd></div><div><dt>预算 / 实际费用</dt><dd>{{ selectedAssetJob.budgetLimit || '—' }} / {{ selectedAssetJob.actualCost || '—' }} {{ selectedAssetJob.costCurrency }}</dd></div><div><dt>轮询次数</dt><dd>{{ selectedAssetJob.pollCount }}</dd></div><div><dt>任务 ID</dt><dd><code>{{ selectedAssetJob.id }}</code></dd></div></dl></article>
          <article class="panel asset-workflow-prompt"><header><span class="eyebrow">冻结输入 / IMMUTABLE INPUT</span><h3>{{ selectedAssetJob.jobType==='prompt'?'创作需求与候选':'最终提示词' }}</h3></header><label>正向输入</label><pre>{{ selectedAssetJob.prompt || '未记录' }}</pre><template v-if="selectedAssetJob.negativePrompt"><label>负面提示词</label><pre class="negative">{{ selectedAssetJob.negativePrompt }}</pre></template><template v-if="selectedAssetJob.resultText"><label>生成结果</label><pre class="result">{{ selectedAssetJob.resultText }}</pre></template></article>
        </section>
        <section v-if="selectedAssetJob.outputAsset" class="panel asset-workflow-output"><header><div><span class="eyebrow">素材产物 / LIBRARY OUTPUT</span><h3>{{ selectedAssetJob.outputAsset.name }}</h3><p>产物已进入风格素材库，可下载并在短剧制作中正式引用。</p></div><div><button class="button ghost small" @click="downloadAssetJobOutput(selectedAssetJob)"><Download :size="14"/>下载</button><button class="button primary small" @click="openAssetJobOutput(selectedAssetJob)"><ArrowRight :size="14"/>在素材库查看</button></div></header><div class="asset-workflow-preview"><AssetMediaPreview :asset="selectedAssetJob.outputAsset" controls/></div></section>
      </main>

      <main v-else-if="selected" class="workflow-detail">
        <section class="panel workflow-overview">
          <header>
            <div><span class="eyebrow">工作流（Workflow）{{ selected.id.toUpperCase() }}</span><h2>{{ selected.projectTitle }}</h2><div><StatusBadge :status="selected.status"/><span class="workflow-kind-pill story">{{ selected.productionRunId?'短剧任务':'独立媒体任务' }}</span><span>{{ selected.currentStep }}</span><span><Clock3 :size="13"/>{{ taskTimeKind(selected) }}于 {{ formatTaskDateTime(taskStartAt(selected)) }}</span><span>{{ taskDurationLabel(selected) }}</span><span v-if="selected.finishedAt">完成于 {{ formatTaskDateTime(selected.finishedAt) }}</span><span v-if="selected.maxAttempts">尝试 {{ selected.attempts }}/{{ selected.maxAttempts }}</span></div></div>
            <div class="workflow-main-actions">
              <button class="button ghost" :disabled="!selected.generatedWorkflowId||selectedInputContractUnavailable" :title="selectedInputContractUnavailable?'历史执行合同不可用，不能安全导出工作流 / Legacy execution contract unavailable':'只读查看，不会提交或中断任务'" @click="inspectWorkflow(selected)"><Eye :size="15"/>在 ComfyUI 查看工作流</button>
              <RouterLink v-if="selected.projectId&&selected.episodeId&&selected.shotId" class="button ghost" :to="{path:`/projects/${selected.projectId}/episodes/${selected.episodeId}/storyboard`,query:{shot:selected.shotId}}"><Clapperboard :size="15"/>修改此镜头</RouterLink>
              <RouterLink v-if="selected.projectId" class="button primary" :to="`/projects/${selected.projectId}/production`"><WorkflowIcon :size="15"/>在 ProductionRun 中处理</RouterLink>
              <div ref="taskMenuRoot" class="task-more-menu">
                <button type="button" class="icon-btn" aria-label="打开任务更多操作" aria-haspopup="menu" aria-controls="task-more-actions" :aria-expanded="taskMenuOpen" title="任务更多操作" @click.stop="taskMenuOpen=!taskMenuOpen"><MoreHorizontal :size="17"/></button>
                <div v-if="taskMenuOpen" id="task-more-actions" class="task-more-menu-popover" role="menu" aria-label="任务更多操作">
                  <button type="button" role="menuitem" @click="copyTaskId"><Clipboard :size="14"/><span><strong>复制任务 ID</strong><small>{{ selected.taskId || selected.id }}</small></span></button>
                  <button v-if="selected.comfyPromptId" type="button" role="menuitem" @click="copyComfyPromptId"><Clipboard :size="14"/><span><strong>复制 Comfy Prompt ID</strong><small>{{ selected.comfyPromptId }}</small></span></button>
                  <button v-if="selected.promptAvailable!==false" type="button" role="menuitem" @click="openInputPrompt()"><FileText :size="14"/><span><strong>查看实际提交提示词</strong><small>{{ promptSummary }}</small></span></button>
                  <button v-if="!selectedInputContractUnavailable" type="button" role="menuitem" @click="copyInputParameters"><Code2 :size="14"/><span><strong>复制输入参数 JSON</strong><small>已自动排除绝对本地路径</small></span></button>
                  <button v-if="!selectedInputContractUnavailable" type="button" role="menuitem" @click="downloadTaskConfig"><Download :size="14"/><span><strong>下载任务配置 JSON</strong><small>任务标识、上下文与安全输入参数</small></span></button>
                  <button type="button" role="menuitem" @click="refreshTaskStatus"><RefreshCw :size="14"/><span><strong>刷新任务状态</strong><small>立即重新读取服务端任务与产物</small></span></button>
                </div>
              </div>
            </div>
          </header>
          <div v-if="selected.episodeId||selected.shotId||selectedTaskGroup" class="task-context-summary"><span v-if="selected.episodeId"><small>剧集</small><strong>{{ episodeContextLabel(selected) }}</strong></span><span v-if="selected.shotId"><small>分镜</small><strong>{{ shotContextLabel(selected) }}</strong></span><span v-if="selectedTaskGroup"><small>整集生产单元</small><strong>{{ taskGroupTaskLabel(selected,selectedTaskGroup) }}</strong></span></div>
          <div v-if="selectedRuntimeAlert" class="task-runtime-alert" :class="selectedRuntimeAlert.level" role="status"><AlertTriangle :size="16"/><div><strong>{{ selectedRuntimeAlert.title }}</strong><p>{{ selectedRuntimeAlert.message }}</p></div><button type="button" :disabled="healthChecking" @click="checkHealth()"><RefreshCw :size="12"/>{{ healthChecking?'检测中':'重新检测' }}</button></div>
          <div v-if="selectedProgress" class="big-progress" :class="{indeterminate:selectedProgress.indeterminate}"><div><span>{{ selectedProgress.heading }}</span><strong>{{ selectedProgress.valueLabel }}</strong></div><i><b :style="{width:selectedProgress.indeterminate?'8%':`${selectedProgress.percent || 0}%`}"></b></i><p v-if="selectedProgress.note">{{ selectedProgress.note }}</p></div>
        </section>

        <section v-if="taskKind(selected)==='video'" class="panel deferred-generation-panel">
          <header><div><span class="eyebrow">延迟连续生成 / DEFERRED CONTINUITY</span><h3>自动连续生成请求</h3><p>历史请求只读审计；新生产必须从所属生产运行（ProductionRun）发起。</p></div><button type="button" class="button ghost small" :disabled="deferredLoading" @click="loadDeferredRequests(selectedTaskId,false)"><RefreshCw :size="13" :class="{spinning:deferredLoading}"/>刷新</button></header>
          <p v-if="deferredError" class="form-error">延迟连续生成请求读取失败：{{ deferredError }}</p>
          <div v-if="deferredLoading&&!deferredRequests.length" class="deferred-empty"><LoaderCircle :size="18"/>正在读取请求状态…</div>
          <div v-else-if="deferredRequests.length" class="deferred-request-list">
            <article v-for="request in deferredRequests" :key="request.id" :class="['deferred-request-card',request.status]">
              <div><strong>{{ deferredGenerationStatusLabel(request.status) }}</strong><span>{{ deferredGenerationFrameSlotLabel(request.frameSlot) }} → {{ deferredTargetLabel(request.targetShotId) }}</span><small>请求 {{ request.id.slice(0,8) }} · 尝试 {{ request.attempts }}/{{ request.maxAttempts }} · {{ request.updatedAt ? formatTaskDateTime(request.updatedAt,true) : '时间未知' }}</small></div>
              <p v-if="request.errorCode||request.errorMessage"><AlertTriangle :size="13"/>{{ request.errorCode }}{{ request.errorCode&&request.errorMessage?'：':'' }}{{ request.errorMessage }}</p>
              <div class="deferred-request-actions">
                <RouterLink v-if="request.materializedTaskId" class="button ghost small" :to="{path:'/workflows',query:{task:request.materializedTaskId}}"><WorkflowIcon :size="13"/>打开下游任务</RouterLink>
              </div>
            </article>
          </div>
          <div v-else class="deferred-empty"><ArrowRight :size="18"/><div><strong>暂无连续生成请求</strong><p>该任务没有历史连续生成审计记录。</p></div></div>
        </section>

        <section class="panel task-input-parameters">
          <header><div><span class="eyebrow">输入参数 / INPUT PARAMETERS</span><h3>输入参数</h3><p>展示任务提交时固化的参数快照，刷新页面后仍可核对；本地绝对路径不会显示或导出。</p></div><button v-if="technicalInputParameters.length" type="button" :aria-expanded="parametersExpanded" aria-controls="task-technical-parameters" @click="parametersExpanded=!parametersExpanded"><span>{{ parametersExpanded?'收起技术参数':'查看更多参数' }}</span><b>{{ technicalInputParameters.length }}</b><ChevronDown :size="13" :class="{expanded:parametersExpanded}"/></button></header>
          <div v-if="selectedInputContractUnavailable" class="task-runtime-alert warning" role="status"><AlertTriangle :size="16"/><div><strong>历史执行合同不可展示 / LEGACY EXECUTION CONTRACT UNAVAILABLE</strong><p>{{ selectedInputContractReason }}。任务状态、时间和归属仍按数据库事实展示；系统不会回填或推断输入参数。原因码 / Code：<code>{{ selected.inputContractCode }}</code></p></div></div>
          <template v-else>
          <div class="task-input-core"><article v-for="item in coreInputParameters" :key="item.key"><small>{{ item.label }}</small><strong :title="item.value">{{ item.value }}</strong></article><article class="task-prompt-parameter"><small>实际提交提示词</small><button type="button" :disabled="selected.promptAvailable===false" :title="selected.promptAvailable===false?'该历史任务未保存可审计的实际提交提示词':'点击后才从服务端读取完整提示词'" @click="openInputPrompt()"><FileText :size="13"/><span>{{ promptSummary }}</span><ArrowRight :size="12"/></button></article></div>
          <div v-for="group in referenceAssetGroups" :key="group.key" class="task-reference-inputs" :class="{continuity:group.continuity}"><header><div><Layers3 :size="15"/><span><strong>{{ group.title }}</strong><small>{{ group.description }}</small></span></div><b v-if="group.continuity">未接入模型</b></header><div class="task-reference-gallery"><article v-for="(asset,index) in group.assets" :key="referenceAssetKey(asset,index)">
            <button v-if="asset.mediaKind==='image'&&publicReferenceUrl(asset)&&!referenceImageFailures[referenceAssetKey(asset,index)]" type="button" class="reference-image-thumb" :aria-label="`打开大图预览：${asset.displayName}`" @click="openReferenceImage(asset,index)"><img :src="publicReferenceUrl(asset)" :alt="asset.displayName" loading="lazy" @load="handleReferenceImageLoaded(asset,index)" @error="recoverReferenceImage(asset,index)"/><span><Eye :size="13"/>查看大图</span></button>
            <div v-else-if="asset.mediaKind==='video'&&publicReferenceUrl(asset)" class="reference-media-control"><video :src="publicReferenceUrl(asset)" controls preload="metadata">当前浏览器无法播放该参考视频。</video></div>
            <div v-else-if="asset.mediaKind==='audio'&&publicReferenceUrl(asset)" class="reference-media-control audio"><AudioLines :size="22"/><audio :src="publicReferenceUrl(asset)" controls preload="metadata">当前浏览器无法播放该参考音频。</audio></div>
            <div v-else class="reference-media-fallback"><Image v-if="asset.mediaKind==='image'" :size="23"/><Film v-else-if="asset.mediaKind==='video'" :size="23"/><AudioLines v-else :size="23"/><span>{{ publicReferenceUrl(asset)?'预览加载失败':'未提供预览地址' }}</span></div>
            <footer><div><strong :title="asset.displayName">{{ asset.displayName }}</strong><span>{{ referenceMediaLabel(asset.mediaKind) }}<template v-if="!group.continuity&&asset.inputLabel"> · {{ asset.inputLabel }}</template><template v-else-if="group.continuity&&(asset.entityLabel||asset.category)"> · {{ asset.entityLabel || asset.category }}</template></span><span v-if="!group.continuity&&referenceTimingLabel(asset)" class="reference-timing" :title="referenceTimingLabel(asset)">{{ referenceTimingLabel(asset) }}</span></div><a v-if="asset.mediaKind!=='image'&&publicReferenceUrl(asset)" :href="publicReferenceUrl(asset)" target="_blank" rel="noopener noreferrer">打开<ExternalLink :size="11"/></a></footer>
          </article></div></div>
          <div v-if="parametersExpanded" id="task-technical-parameters" class="task-input-technical"><div v-for="item in technicalInputParameters" :key="item.key"><code>{{ item.label }}</code><span :title="item.value">{{ item.value }}</span></div></div>
          </template>
        </section>

        <section v-if="selectedTaskGroup" class="panel episode-batch-overview">
          <header><div><span class="eyebrow">整集任务 / WHOLE EPISODE TASK</span><h3>{{ taskGroupTitle(selectedTaskGroup) }}</h3><p>{{ taskGroupIdentity(selectedTaskGroup) }} · {{ selectedTaskGroup.unitCount }} 个生产单元 · {{ selectedTaskGroup.tasks.length }} 条审计记录</p></div><div class="episode-batch-actions"><span class="platform-status muted">只读诊断</span><strong>完成 {{ selectedTaskGroup.completedCount }}/{{ selectedTaskGroup.unitCount }}</strong></div></header>
          <div class="episode-batch-progress" :class="{indeterminate:selectedTaskGroup.indeterminate}"><i><b :style="{width:selectedTaskGroup.indeterminate?'8%':`${selectedTaskGroup.completionPercent}%`}"></b></i><span>{{ taskGroupStatusSummary(selectedTaskGroup) }}</span></div>
          <div class="episode-batch-tasks"><button v-for="task in selectedTaskGroup.tasks" :key="task.id" :class="[task.status,{active:task.id===selected.id}]" @click="selectTask(task.id)"><span>{{ taskGroupTaskLabel(task,selectedTaskGroup) }}</span><strong>{{ shotContextLabel(task) || task.projectTitle }}</strong><small>{{ taskGroupTaskStatus(task) }}</small></button></div>
        </section>

        <section v-if="selected.status==='failed'" class="panel task-error-detail">
          <header>
            <div class="task-error-title"><span><AlertTriangle :size="16"/></span><div><span class="eyebrow">失败详情 / FAILURE DETAILS</span><h3>失败原因</h3></div></div>
            <button v-if="hasFailureDetail" type="button" class="task-error-toggle" :aria-expanded="failureExpanded" aria-controls="selected-task-error-detail" @click="failureExpanded=!failureExpanded">{{ failureExpanded?'收起':'查看更多' }}<ChevronDown :size="13" :class="{expanded:failureExpanded}"/></button>
          </header>
          <div class="task-error-meta"><span>任务 ID <code>{{ selected.taskId || selected.id }}</code></span><span v-if="selected.finishedAt">结束于 {{ new Date(selected.finishedAt).toLocaleString('zh-CN') }}</span></div>
          <p v-if="!failureExpanded" class="task-error-summary">{{ failureSummary }}</p>
          <pre v-else id="selected-task-error-detail" class="task-error-trace">{{ selectedErrorMessage || failureSummary }}</pre>
        </section>

        <section class="panel node-canvas">
          <header>
            <div><span class="eyebrow">ComfyUI 执行编排 / ORCHESTRATION</span><h3>生产节点</h3></div>
            <div class="node-legend" aria-label="节点状态统计">
              <span><i class="done"></i><b>{{ nodeStatusCount('completed') }}</b>完成</span>
              <span><i class="active"></i><b>{{ nodeStatusCount('running') }}</b>运行</span>
              <span v-if="nodeStatusCount('failed')"><i class="failed"></i><b>{{ nodeStatusCount('failed') }}</b>失败</span>
              <span><i></i><b>{{ nodeStatusCount('pending') }}</b>等待</span>
            </div>
          </header>
          <div class="node-flow" role="list" aria-label="ComfyUI 生产节点执行进度">
            <template v-for="(node,index) in selectedDisplayNodes" :key="node.id">
              <article :class="[`node-${node.status}`]" role="listitem" :aria-current="node.status==='running'?'step':undefined">
                <span class="node-order">{{ String(index+1).padStart(2,'0') }}</span>
                <div class="node-icon">
                  <component :is="nodeIcons[node.type]" :size="18"/>
                  <LoaderCircle v-if="node.status==='running'" class="node-spinner" :size="28"/>
                  <CheckCircle2 v-else-if="node.status==='completed'" class="node-state-icon" :size="13"/>
                  <AlertTriangle v-else-if="node.status==='failed'" class="node-state-icon" :size="13"/>
                </div>
                <div class="node-copy"><strong>{{ node.label }}</strong><small>{{ nodeStatusLabel(node) }}</small></div>
                <div class="node-progress" :class="{indeterminate:selectedProgress?.indeterminate&&node.status==='running'}">
                  <div><span>{{ node.status==='running'&&selectedProgress?.mode==='comfyui'?'当前节点采样':'节点进度' }}</span><small>{{ nodeProgressLabel(node) }}</small></div>
                  <i><b :style="{width:nodeProgressWidth(node)}"></b></i>
                </div>
              </article>
              <div v-if="index<selectedDisplayNodes.length-1" class="node-connector" :class="{done:node.status==='completed'}" aria-hidden="true"><i></i><ArrowRight :size="12"/></div>
            </template>
          </div>
        </section>

        <section class="workflow-bottom-grid">
          <div class="panel task-log"><header><div><span class="eyebrow">任务历史 / TASK HISTORY</span><h3>执行记录</h3></div><div class="task-log-actions"><span>{{ logs.length }} 条记录</span><button @click="refresh(false)">刷新</button></div></header><div class="log-scroll"><div v-for="log in logs" :key="log.key" :class="log.type"><span>{{ log.time }}</span><i></i><p><strong>{{ log.text }}</strong><small v-if="log.details">{{ log.details }}</small></p></div><div v-if="!logs.length" class="empty-compact"><Code2 :size="22"/><p>任务创建后将在这里显示真实执行记录</p></div></div></div>
          <div class="panel output-summary" :class="{'is-empty':!selectedMedia.length}">
            <header><div><span class="eyebrow">任务产物 / TASK OUTPUTS</span><h3>本任务原始产物</h3></div><span class="output-total">{{ selectedMedia.length }} 个文件</span></header>
            <div class="output-stats"><div><Film :size="17"/><span><strong>{{ outputCount('video') }}</strong>视频</span></div><div><Image :size="17"/><span><strong>{{ outputCount('image') }}</strong>图片</span></div><div><AudioLines :size="17"/><span><strong>{{ outputCount('audio') }}</strong>音频</span></div></div>
            <div v-if="selectedMedia.length" class="task-output-list">
              <article v-for="(media,index) in selectedMedia" :key="`${media.index ?? index}-${media.filename}`" class="task-output-card">
                <video v-if="mediaKind(media)==='video' && media.previewUrl" :src="media.previewUrl" controls playsinline preload="metadata" @loadedmetadata="handleOutputMediaLoaded(media,index)" @error="handleOutputMediaError(media,index)">当前浏览器不支持视频预览。</video>
                <img v-else-if="mediaKind(media)==='image' && media.previewUrl" :src="media.previewUrl" :alt="media.filename" loading="lazy" @load="handleOutputMediaLoaded(media,index)" @error="handleOutputMediaError(media,index)"/>
                <audio v-else-if="mediaKind(media)==='audio' && media.previewUrl" :src="media.previewUrl" controls preload="metadata" @loadedmetadata="handleOutputMediaLoaded(media,index)" @error="handleOutputMediaError(media,index)">当前浏览器不支持音频预览。</audio>
                <div v-else class="task-output-file"><FileText :size="25"/><span>{{ mediaKind(media)==='video'?'视频暂不可预览':'文件产物' }}</span></div>
                <footer>
                  <div>
                    <strong :title="media.filename">{{ media.filename }}</strong>
                    <span>{{ mediaKind(media).toUpperCase() }} · 母版 {{ index + 1 }} · {{ media.aiGenerated?'AI 生成':'来源未标识' }}</span>
                    <span v-if="media.distributionStatus==='ready'" class="distribution-ready">公开副本已验证 · {{ media.publicLabel }} · {{ media.publicVersion }}</span>
                    <span v-else-if="media.distributionStatus==='development_master'" class="distribution-ready">开发模式 · 可直接下载 ComfyUI 母版（不是发行成片）</span>
                    <span v-if="media.promotedAssetId" class="distribution-ready">已保存到素材库 · {{ promotedAssetPurposeLabel(media) }}</span>
                    <span v-else-if="media.distributionStatus==='failed'" class="distribution-failed" :title="media.distributionError">公开副本失败：{{ media.distributionError }}</span>
                    <span v-else-if="mediaKind(media)==='video'||mediaKind(media)==='image'" class="distribution-pending">母版仅供预览，公开下载前需生成带标识副本</span>
                  </div>
                  <div class="task-output-actions">
                    <a v-if="media.provenanceUrl" :href="media.provenanceUrl" target="_blank" rel="noopener" class="button ghost small"><FileJson :size="13"/>母版来源</a>
                    <a v-if="media.publicProvenanceUrl" :href="media.publicProvenanceUrl" target="_blank" rel="noopener" class="button ghost small"><FileJson :size="13"/>发行清单</a>
                    <button v-if="media.previewUrl" type="button" class="button ghost small" @click="openOutputMedia(media,index)"><Eye :size="13"/>预览母版</button>
                    <button v-if="media.downloadUrl" type="button" class="button primary small" @click="downloadOutputMedia(media,index)"><Download :size="13"/>{{ media.distributionStatus==='development_master'?'下载母版':'公开下载' }}</button>
                    <span v-else-if="mediaKind(media)==='video'||mediaKind(media)==='image'" class="platform-status warning">尚未归档公开副本</span>
                  </div>
                </footer>
              </article>
            </div>
            <div v-else class="task-output-empty" role="status"><component :is="taskKind(selected)==='image'?Image:Film" :size="23"/><div><strong>暂无可预览产物</strong><p>{{ emptyOutputMessage }}</p></div><button v-if="selected.status==='completed'" class="button ghost small" @click="refresh(false)"><RefreshCw :size="13"/>刷新产物</button></div>
            <p class="task-output-note">此处展示 ComfyUI 返回的单任务原始文件。开发模式可直接下载母版用于调试；生产模式只提供带标识且验证通过的发行副本。来源清单只公开哈希和模型/任务标识，不泄露完整提示词或本地路径。</p>
          </div>
        </section>
      </main>
      <main v-else class="panel empty-state"><WorkflowIcon :size="30"/><h3>{{ studio.workflows.length||studio.assetGenerationJobs.length?'当前筛选没有任务':'尚无历史任务' }}</h3><p>{{ studio.workflows.length||studio.assetGenerationJobs.length?'当前来源或状态下没有可展示的任务，请切换筛选条件。':'可从素材库创建素材任务，或从项目生产运行创建短剧任务。' }}</p><button v-if="studio.workflows.length||studio.assetGenerationJobs.length" class="button ghost" @click="setTaskScope('all');setTaskFilter('all')">查看全部任务</button><button v-else class="button primary" @click="goToProduction"><Plus :size="15"/>进入一键生产</button></main>
    </section>

    <BaseModal :open="workflowInspectOpen" title="ComfyUI 工作流详情" eyebrow="只读工作流查看器 / WORKFLOW INSPECTOR" width="900px" @close="workflowInspectOpen=false">
      <div class="workflow-inspector">
        <div class="workflow-format-picker" role="tablist" aria-label="工作流导出格式">
          <div><strong>选择导出格式</strong><p>UI 画布用于在 ComfyUI 中查看和编辑；API Prompt 用于审计实际提交图。</p></div>
          <div class="workflow-format-tabs">
            <button type="button" role="tab" :aria-selected="workflowExportFormat==='ui'" :class="{active:workflowExportFormat==='ui'}" :disabled="workflowInspectLoading" @click="switchWorkflowFormat('ui')">UI 画布（推荐）</button>
            <button type="button" role="tab" :aria-selected="workflowExportFormat==='api'" :class="{active:workflowExportFormat==='api'}" :disabled="workflowInspectLoading" @click="switchWorkflowFormat('api')">API Prompt 图</button>
          </div>
        </div>
        <div v-if="inspectedTask" class="task-identity-grid">
          <div><span>当前任务 ID</span><code>{{ inspectedTask.taskId || inspectedTask.id }}</code></div>
          <div v-if="inspectedTask.episodeId"><span>所属剧集</span><strong>{{ episodeContextLabel(inspectedTask) }}</strong></div>
          <div v-if="inspectedTask.shotId"><span>所属分镜</span><strong>{{ shotContextLabel(inspectedTask) }}</strong></div>
          <div v-if="inspectedTask.batchId"><span>整集镜头</span><strong>{{ inspectedTask.batchIndex || '?' }}/{{ inspectedTask.batchSize || '?' }} · {{ inspectedTask.batchId }}</strong></div>
          <div><span>Generated workflow</span><code>{{ inspectedTask.generatedWorkflowId || '尚未关联' }}</code></div>
          <div><span>Comfy prompt ID</span><code>{{ inspectedTask.comfyPromptId || '尚未提交至 ComfyUI' }}</code></div>
          <div><span>状态 / 进度</span><strong>{{ inspectedTask.backendStatus || inspectedTask.status }} · {{ progressPresentation(inspectedTask).compactLabel }}</strong></div>
          <div><span>提交状态</span><strong>{{ inspectedTask.submissionState || 'unprepared' }}</strong></div>
          <div><span>尝试次数</span><strong>{{ inspectedTask.attempts || 0 }} / {{ inspectedTask.maxAttempts || 0 }}</strong></div>
          <div><span>执行后端</span><strong>{{ inspectedTask.backend || '—' }}</strong></div>
          <div><span>模型档案</span><strong>{{ inspectedTask.modelProfile || '—' }}</strong></div>
          <div><span>有效帧 / 时长</span><strong>{{ inspectedTask.effectiveFrames || 0 }} 帧 · {{ inspectedTask.effectiveDurationMs ? `${(inspectedTask.effectiveDurationMs/1000).toFixed(3)}s` : '—' }}</strong></div>
        </div>
        <div v-if="workflowInspectLoading" class="workflow-inspector-loading"><LoaderCircle :size="24"/><span>正在只读加载实际 generated_workflow…</span></div>
        <div v-else-if="workflowInspectError" class="workflow-inspector-error"><AlertTriangle :size="18"/><div><strong>无法读取工作流</strong><p>{{ workflowInspectError }}</p></div></div>
        <template v-else-if="workflowExport">
          <div class="workflow-import-notice" :class="{safe:isCanvasWorkflow&&workflowExport.importReady}"><FileJson :size="19"/><div><strong>{{ isCanvasWorkflow?'可导入的 ComfyUI UI 画布':'API Prompt 图（不能直接拖入画布）' }}</strong><p>{{ workflowExport.openReason }}</p></div><span>{{ workflowFormatLabel }}</span></div>
          <ol v-if="isCanvasWorkflow&&workflowExport.importReady" class="workflow-import-steps"><li>下载 UI 画布 JSON 后可在 ComfyUI 中手动导入查看。</li><li>核对工作流版本、参考素材和输出契约；本诊断页不会写入 ComfyUI 工作流库或提交任务。</li></ol>
          <div v-if="workflowExport.importReady===false" class="workflow-placeholder-warning"><AlertTriangle :size="15"/>后端未能确认 {{ isCanvasWorkflow?'UI 画布':'API Prompt 图' }} 已完整就绪；建议仅审计 JSON，不要直接执行。</div>
          <div v-if="workflowExport.containsStagedPlaceholders" class="workflow-placeholder-warning"><AlertTriangle :size="15"/>工作流包含 {{ workflowExport.stagedPlaceholderCount || 0 }} 个服务端暂存输入占位；请保持素材文件可访问，不要手动改变节点输入路径。</div>
          <div class="workflow-export-meta"><span>模板（TEMPLATE）<code>{{ workflowExport.templateSlug || '—' }} v{{ workflowExport.templateVersion || '—' }}</code></span><span>内容摘要（CONTENT HASH）<code>{{ workflowExport.contentHash || '—' }}</code></span><span>文件（FILE）<code>{{ workflowExport.filename }}</code></span></div>
          <div class="workflow-json-preview"><pre>{{ exportedWorkflowJson }}</pre></div>
        </template>
      </div>
      <template #footer><button class="button ghost" @click="workflowInspectOpen=false">关闭</button><button class="button ghost" :disabled="!workflowExport" @click="copyWorkflow"><Clipboard :size="15"/>复制当前 JSON</button><button class="button ghost" :disabled="!workflowExport" @click="downloadWorkflow"><Download :size="15"/>下载{{ workflowFormatLabel }}</button><button class="button primary" :disabled="!safeComfyUrl" :title="workflowExport?.openReason" @click="openInComfy"><ExternalLink :size="15"/>打开 ComfyUI</button></template>
    </BaseModal>

    <BaseModal :open="promptOpen" title="实际提交提示词" eyebrow="不可变输入提示词 / IMMUTABLE INPUT PROMPT" width="900px" @close="promptOpen=false">
      <div class="task-prompt-inspector">
        <div v-if="promptLoading" class="task-prompt-loading"><LoaderCircle :size="24"/><span>正在读取任务提交时固化的提示词…</span></div>
        <div v-else-if="promptError" class="task-prompt-error"><AlertTriangle :size="19"/><div><strong>无法读取实际提交提示词</strong><p>{{ promptError }}</p></div><button class="button ghost small" @click="openInputPrompt(true)"><RefreshCw :size="13"/>重试</button></div>
        <template v-else-if="inputPrompt">
          <div class="task-prompt-meta"><div><small>模型 / 实际条件模式</small><strong>{{ inputPrompt.model || '未记录' }} · {{ inputPrompt.effectiveConditioningMode || inputPrompt.conditioningMode || '未记录' }}</strong></div><div><small>模板</small><code>{{ inputPrompt.promptTemplateKey || '未记录' }}</code></div><div><small>Prompt Artifact</small><code>{{ inputPrompt.promptArtifactId || '未记录' }}</code></div><div><small>内容哈希</small><code>{{ inputPrompt.contentHash || '未记录' }}</code></div></div>
          <div class="task-prompt-texts">
            <article><header><div><span>实际提交提示词</span><small>{{ inputPrompt.promptCharacterCount }} 字符 · {{ inputPrompt.promptSource || '来源未记录' }}</small></div><button type="button" :disabled="!inputPrompt.prompt" @click="copyPromptText('prompt')"><Clipboard :size="13"/>复制</button></header><pre>{{ inputPrompt.prompt || '该任务没有保存正向提示词文本。' }}</pre></article>
            <article class="negative"><header><div><span>{{ inputPrompt.negativePromptSubmitted?'实际提交的负面提示词':'归档负面约束（未提交）' }}</span><small>{{ inputPrompt.negativePromptCharacterCount }} 字符 · {{ inputPrompt.negativePromptSubmitted?'已进入工作流':'当前工作流无对应输入' }} · {{ inputPrompt.negativePromptSource || '来源未记录' }}</small></div><button type="button" :disabled="!inputPrompt.negativePrompt" @click="copyPromptText('negative')"><Clipboard :size="13"/>复制</button></header><pre>{{ inputPrompt.negativePrompt || (inputPrompt.negativePromptSubmitted?'工作流提交了空负面提示词。':'该任务没有归档负面约束，也没有向模型提交负面提示词。') }}</pre></article>
          </div>
        </template>
      </div>
      <template #footer><button class="button ghost" @click="promptOpen=false">关闭</button><button class="button primary" :disabled="!inputPrompt?.prompt" @click="copyPromptText('prompt')"><Clipboard :size="15"/>复制实际提示词</button></template>
    </BaseModal>

    <BaseModal :open="referencePreviewOpen" :title="referencePreviewAsset?.displayName || '素材图片预览'" eyebrow="素材图片预览（Asset Preview）" width="1000px" @close="referencePreviewOpen=false">
      <div v-if="referencePreviewAsset" class="reference-image-preview">
        <img v-if="publicReferenceUrl(referencePreviewAsset)&&!referenceImageFailures[referenceAssetKey(referencePreviewAsset)]" :src="publicReferenceUrl(referencePreviewAsset)" :alt="referencePreviewAsset.displayName" @load="handleReferenceImageLoaded(referencePreviewAsset)" @error="recoverReferenceImage(referencePreviewAsset)"/>
        <div v-else class="reference-preview-fallback"><Image :size="34"/><strong>大图预览加载失败</strong><span>预览签名可能已经过期，可重新向服务端获取。</span><button class="button ghost small" @click="recoverReferenceImage(referencePreviewAsset,0,true)"><RefreshCw :size="13"/>重新获取预览</button></div>
        <footer><span>{{ referenceMediaLabel(referencePreviewAsset.mediaKind) }}<template v-if="referencePreviewAsset.inputLabel"> · {{ referencePreviewAsset.inputLabel }}</template></span><code v-if="referencePreviewAsset.assetId">{{ referencePreviewAsset.assetId }}</code></footer>
      </div>
      <template #footer><button class="button ghost" @click="referencePreviewOpen=false">关闭</button><a v-if="publicReferenceUrl(referencePreviewAsset)" class="button primary" :href="publicReferenceUrl(referencePreviewAsset)" target="_blank" rel="noopener noreferrer"><ExternalLink :size="15"/>在新窗口打开</a></template>
    </BaseModal>

    <BaseModal :open="!!resolutionTarget" title="素材生成任务人工消歧" eyebrow="管理员证据核验（Staff Evidence）" width="680px" @close="closeUncertainResolution">
      <div v-if="resolutionTarget" class="asset-resolution-editor">
        <div class="asset-resolution-evidence"><AlertTriangle :size="17"/><span><strong>{{ resolutionTarget.name || '提示词生成任务' }} · {{ resolutionTarget.id }}</strong><small>请先在服务提供方控制台核对请求、远端终态和账单。结论会写入不可变审计证据，不会重放原始提交。</small><small v-if="resolutionTarget.ignoreLateResult">该任务已记录取消意图；确认远端成功也只恢复费用对账，不会将产物入库。</small><small v-if="resolutionTarget.providerRequestIdSha256">已冻结请求标识摘要：{{ resolutionTarget.providerRequestIdSha256.slice(0,20) }}…</small></span></div>
        <label class="field"><span>消歧结论 *</span><select v-model="resolutionForm.outcome" :disabled="resolutionPending"><option v-for="item in resolutionOutcomes" :key="item.value" :value="item.value">{{ item.label }}</option></select></label>
        <label class="field"><span>核验证据说明 *（{{ productionCapability?.effectiveLimits.auditNoteMinCharacters || '—' }}–{{ productionCapability?.effectiveLimits.auditNoteMaxCharacters || '—' }} 个字符）</span><textarea v-model="resolutionForm.note" :disabled="resolutionPending||!productionCapability" rows="4" :minlength="productionCapability?.effectiveLimits.auditNoteMinCharacters" :maxlength="productionCapability?.effectiveLimits.auditNoteMaxCharacters" placeholder="记录核验位置、远端终态与账单依据"></textarea></label>
        <label class="field"><span>服务提供方请求标识（Provider Request ID）</span><input v-model="resolutionForm.providerRequestId" :disabled="resolutionPending" maxlength="191" autocomplete="off" :placeholder="resolutionTarget.providerRequestIdSha256?'服务端已有摘要；仅在需要复核原值时填写':'确认媒体远端终态时必填'"></label>
        <div class="form-grid two"><label class="field"><span>实际费用（可填 0）</span><input v-model="resolutionForm.actualCost" :disabled="resolutionPending" inputmode="decimal" placeholder="0.000000"></label><label class="field"><span>费用币种</span><input v-model="resolutionForm.costCurrency" :disabled="resolutionPending" maxlength="12" class="asset-currency-input" placeholder="USD"></label></div>
        <div v-if="resolutionError" class="asset-resolution-error" role="alert"><AlertTriangle :size="14"/><span>{{ resolutionError }}</span></div>
      </div>
      <template #footer><button class="button ghost" :disabled="resolutionPending" @click="closeUncertainResolution">取消</button><button class="button primary" :disabled="resolutionPending" @click="submitUncertainResolution"><Save :size="15"/>{{ resolutionPending?'提交中…':'记录消歧结论' }}</button></template>
    </BaseModal>

  </div>
</template>

<style scoped>
.workflow-heading-actions{display:flex;align-items:center;gap:7px}.workflow-scope-filters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px;padding:8px;border-bottom:1px solid var(--border)}.workflow-scope-filters button{min-width:0;height:30px;padding:0 7px;display:flex;align-items:center;justify-content:space-between;gap:5px;border:1px solid transparent;border-radius:6px;background:#0d1014;color:#687078;font-size:9px;cursor:pointer}.workflow-scope-filters button b{padding:2px 5px;border-radius:999px;background:#171b20;color:#747b83;font-size:7px}.workflow-scope-filters button.active{border-color:rgba(230,167,90,.24);background:rgba(230,167,90,.06);color:#d3a262}.workflow-page .workflow-filters{grid-template-columns:repeat(3,minmax(0,1fr))}
.workflow-kind-divider{height:28px;margin:4px 3px 6px;padding:0 7px;display:flex;align-items:center;gap:6px;border-bottom:1px solid var(--border);color:#8f969e;font-size:8px;font-weight:700;letter-spacing:.06em}.workflow-kind-divider b{margin-left:auto;padding:2px 5px;border-radius:999px;background:#181c21;font-size:7px}.workflow-kind-divider.asset{color:#68c5a7}.workflow-kind-divider.story{margin-top:10px;color:#b3a1ed}.workflow-list-scroll>button.asset-workflow-list-item.active{border-color:rgba(104,197,167,.3);background:rgba(104,197,167,.055)}.workflow-list-scroll>button.asset-workflow-list-item.uncertain{border-color:rgba(218,154,70,.3);background:rgba(218,154,70,.045)}.workflow-list-scroll>button.asset-workflow-list-item.cancelled{opacity:.78}.asset-workflow-list-item .workflow-item-top>b{color:#68c5a7;border-color:rgba(104,197,167,.2);background:rgba(104,197,167,.055)}.asset-workflow-list-item .workflow-item-progress b{background:#68c5a7}.asset-workflow-list-item .workflow-item-progress.failed b{background:var(--red)}.asset-workflow-list-item .workflow-item-progress.uncertain b{background:#d39a52}.asset-workflow-list-item .workflow-item-progress.cancelled b{background:#777d84}
.workflow-kind-pill{padding:3px 6px;border:1px solid var(--border);border-radius:999px;font-size:8px;font-weight:700;white-space:nowrap}.workflow-kind-pill.asset{border-color:rgba(104,197,167,.26);background:rgba(104,197,167,.06);color:#70c9ad}.workflow-kind-pill.story{border-color:rgba(159,140,255,.25);background:rgba(159,140,255,.055);color:#b6a4ee}.asset-workflow-actions{flex-wrap:wrap;justify-content:flex-end}.asset-workflow-grid{display:grid;grid-template-columns:minmax(250px,.75fr) minmax(0,1.25fr);gap:12px}.asset-workflow-facts,.asset-workflow-prompt,.asset-workflow-output{padding:17px}.asset-workflow-facts h3,.asset-workflow-prompt h3,.asset-workflow-output h3{margin-top:3px;font-size:14px}.asset-workflow-facts dl{margin:14px 0 0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.asset-workflow-facts dl>div{min-width:0;padding:8px;border:1px solid var(--border);border-radius:6px;background:#0b0e12}.asset-workflow-facts dt{font-size:7px;color:#5f666d}.asset-workflow-facts dd{margin:4px 0 0;overflow-wrap:anywhere;font-size:9px;color:#a7adb2}.asset-workflow-facts code{font:8px var(--font-mono);color:#7fb8a6}.asset-workflow-prompt{display:flex;flex-direction:column;gap:8px}.asset-workflow-prompt label{margin-top:4px;font-size:8px;color:#656c73}.asset-workflow-prompt pre{max-height:230px;margin:0;padding:11px;overflow:auto;border:1px solid var(--border);border-radius:6px;background:#090c10;color:#a8b1af;white-space:pre-wrap;overflow-wrap:anywhere;font:9px/1.65 var(--font-mono)}.asset-workflow-prompt pre.negative{color:#a77f82}.asset-workflow-prompt pre.result{border-color:rgba(104,197,167,.2);background:rgba(104,197,167,.035);color:#9bcab9}.asset-workflow-output>header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.asset-workflow-output>header p{margin-top:4px;font-size:9px;color:#697078}.asset-workflow-output>header>div:last-child{display:flex;gap:6px}.asset-workflow-preview{height:min(58vh,520px);margin-top:14px;overflow:hidden;border-radius:8px;background:#080b0e}
.asset-resolution-editor{display:flex;flex-direction:column;gap:12px}.asset-resolution-evidence{display:flex;gap:10px;padding:12px;border:1px solid rgba(218,154,70,.25);border-radius:8px;background:rgba(218,154,70,.05);color:#d39a52}.asset-resolution-evidence span{display:flex;flex-direction:column;gap:4px}.asset-resolution-evidence strong{font-size:11px;color:#d8b27c}.asset-resolution-evidence small{font-size:9px;line-height:1.55;color:#9b805b}.asset-resolution-error{display:flex;align-items:flex-start;gap:8px;padding:9px 10px;border:1px solid rgba(239,115,123,.28);border-radius:7px;background:rgba(239,115,123,.06);color:#e4868d}.asset-resolution-error span{font-size:10px;line-height:1.5}.asset-currency-input{text-transform:uppercase}
@media(max-width:980px){.asset-workflow-grid{grid-template-columns:1fr}.asset-workflow-actions{justify-content:flex-start}.workflow-heading-actions{align-self:stretch}.workflow-heading-actions .button{flex:1}}
@media(max-width:720px){.workflow-heading-actions{width:100%}.workflow-scope-filters{grid-template-columns:1fr 1fr}.asset-workflow-facts dl{grid-template-columns:1fr}.asset-workflow-output>header{flex-direction:column}.asset-workflow-output>header>div:last-child{width:100%}.asset-workflow-output>header .button{flex:1}.asset-workflow-preview{height:360px}}
.workflow-plan.danger {
  border-color: rgba(230, 167, 90, .28);
  background: rgba(230, 167, 90, .055);
  color: var(--accent);
}
.workflow-plan.danger p { color: #b4946e; }
.workflow-task-group { width:calc(100% - 8px);margin:12px 4px 6px;padding:10px;border:1px solid rgba(151,132,255,.22);border-radius:10px;background:rgba(151,132,255,.06); }
.workflow-task-group:hover { border-color:rgba(151,132,255,.38);background:rgba(151,132,255,.08); }
.workflow-task-group.failed { border-color:rgba(239,115,123,.28);background:rgba(239,115,123,.045); }
.workflow-task-group-heading { display:grid;gap:8px;min-width:0; }
.workflow-task-group-toggle { display:grid;grid-template-columns:auto minmax(0,1fr);gap:8px;align-items:center;min-width:0;padding:0;border:0;background:transparent;text-align:left;cursor:pointer; }
.workflow-task-group-toggle>svg { color:#bda6ff;transition:transform .16s ease; }
.workflow-task-group.collapsed .workflow-task-group-toggle>svg { transform:rotate(-90deg); }
.workflow-task-group-toggle>span { min-width:0;display:flex;flex-direction:column;gap:3px; }
.workflow-task-group-toggle small { color:#bda6ff;font-family:var(--font-mono);font-size:8px;letter-spacing:.16em;text-transform:uppercase; }
.workflow-task-group-toggle strong { color:#f4efe7;font-size:12px;line-height:1.35;overflow:hidden;text-overflow:ellipsis;white-space:nowrap; }
.workflow-task-group-toggle em { color:#8f98a8;font-size:9px;font-style:normal;line-height:1.45;overflow:hidden;text-overflow:ellipsis;white-space:nowrap; }
.workflow-task-group-actions { display:flex;gap:6px;align-items:center;justify-content:space-between;flex-wrap:wrap; }
.workflow-task-group-actions>a { display:inline-flex;align-items:center;gap:3px;color:#a99ae2;font-size:9px; }
.workflow-task-group-summary { margin-top:9px;padding-top:8px;border-top:1px solid rgba(151,132,255,.12); }
.workflow-task-group-summary>p { margin:0 0 7px;display:flex;align-items:center;justify-content:space-between;gap:7px;color:#858d9a;font-size:9px; }
.workflow-task-group-summary>p>span { min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap; }
.workflow-task-group-summary>p>b { flex:0 0 auto;display:flex;align-items:center;gap:3px;color:#9a8fc4;font-size:9px;font-weight:500;white-space:nowrap; }
.workflow-task-group-progress { display:flex;align-items:center;gap:7px; }
.workflow-task-group-progress>i { height:3px;flex:1;overflow:hidden;border-radius:3px;background:#272735; }
.workflow-task-group-progress>i b { display:block;height:100%;border-radius:3px;background:linear-gradient(90deg,#7664b5,var(--violet)); }
.workflow-task-group-progress>strong { color:#9f91d6;font:9px var(--font-mono); }
.workflow-task-group-progress.indeterminate>i b { animation:workflow-indeterminate 1.25s linear infinite;transform:translateX(-120%);background:linear-gradient(90deg,transparent,var(--violet),transparent); }
.workflow-list-scroll>button.workflow-group-child { width:calc(100% - 12px);margin-left:12px;border-left-color:rgba(151,132,255,.22); }
.episode-batch-actions { display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap; }
.episode-batch-actions>strong { font-family:var(--font-display);font-size:22px;color:var(--violet); }
.episode-batch-tasks button { grid-template-columns:minmax(86px,auto) minmax(0,1fr) auto; }
.deferred-generation-panel header { display:flex;align-items:flex-start;justify-content:space-between;gap:14px; }
.deferred-request-list { display:grid;gap:10px; }
.deferred-request-card { display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:11px;border:1px solid rgba(151,132,255,.18);border-radius:12px;background:rgba(151,132,255,.045); }
.deferred-request-card.materialized { border-color:rgba(86,198,147,.28);background:rgba(86,198,147,.055); }
.deferred-request-card.failed,.deferred-request-card.stale { border-color:rgba(231,76,97,.32);background:rgba(231,76,97,.055); }
.deferred-request-card strong { display:block;color:#f4efe7;font-size:12px; }
.deferred-request-card span,.deferred-request-card small { display:block;color:#9fa7b5;font-size:11px;line-height:1.6; }
.deferred-request-card p { display:flex;align-items:center;gap:6px;margin:6px 0 0;color:#e9919d;font-size:11px; }
.deferred-request-actions { display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end; }
.deferred-empty { display:flex;align-items:center;gap:10px;padding:12px;border:1px dashed rgba(151,132,255,.22);border-radius:12px;color:#9fa7b5;font-size:12px; }
.deferred-empty strong { display:block;color:#f4efe7; }
.deferred-empty p { margin:2px 0 0; }
</style>
