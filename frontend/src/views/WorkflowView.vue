<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { AlertTriangle, ArrowRight, AudioLines, Ban, Check, CheckCircle2, ChevronDown, Clapperboard, Clipboard, Clock3, Code2, Cpu, Download, ExternalLink, Eye, FileJson, FileText, Film, FolderInput, Image, Layers3, LoaderCircle, MoreHorizontal, Pause, Play, Plus, RefreshCw, Server, Sparkles, Trash2, Workflow as WorkflowIcon } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import ResolutionProfilePicker from '@/components/ResolutionProfilePicker.vue'
import ProductionWorkflowPicker from '@/components/ProductionWorkflowPicker.vue'
import { useStudioStore } from '@/stores/studio'
import { repository } from '@/services/repository'
import { workflowProgressPresentation } from '@/services/taskProgress'
import type { BibleItem, ComfyUIWorkflowSyncResult, GeneratedImageAssetTarget, GeneratedWorkflowExport, ProductionResolutionProfile, ProductionWorkflowSelection, ProductionWorkflowTemplate, Ref2VAReferenceCategory, SeriesProject, TaskInputPrompt, TaskOutputMedia, TaskReferenceAssetSummary, Workflow, WorkflowNode } from '@/types'

type TaskFilter = 'all' | 'active' | 'completed' | 'failed'
type HealthLevel = 'checking' | 'connected' | 'degraded' | 'unavailable'
type RuntimeAlert = { level: 'info' | 'warning' | 'danger'; title: string; message: string }
type ParameterItem = { key: string; label: string; value: string }
type ReferenceAssetGroup = { key: 'model-input' | 'continuity'; title: string; description: string; assets: TaskReferenceAssetSummary[]; continuity: boolean }
type OutputPromotionSelection = { taskId: string; workflowId: string; projectId: string; media: TaskOutputMedia; index: number }

const studio = useStudioStore(); const route = useRoute(); const createOpen = ref(false); const workflowInspectOpen = ref(false); const workflowInspectLoading = ref(false); const workflowInspectError = ref(''); const workflowExport = ref<GeneratedWorkflowExport|null>(null); const workflowExportFormat = ref<'ui'|'api'>('ui'); const workflowSyncing = ref(false); const workflowSyncResult = ref<ComfyUIWorkflowSyncResult|null>(null); const inspectedTaskId = ref(''); const selectedId = ref(''); const taskFilter = ref<TaskFilter>('all'); const failureExpanded = ref(false); const parametersExpanded = ref(false); const taskMenuOpen = ref(false); const taskMenuRoot = ref<HTMLElement|null>(null); const promptOpen = ref(false); const promptLoading = ref(false); const promptError = ref(''); const inputPrompt = ref<TaskInputPrompt|null>(null); const promptTaskId = ref(''); const referencePreviewOpen = ref(false); const referencePreviewAsset = ref<TaskReferenceAssetSummary|null>(null); const referenceImageFailures = ref<Record<string,boolean>>({}); const outputPromotionOpen = ref(false); const outputPromotionSelection = ref<OutputPromotionSelection|null>(null); const outputPromotionMode = ref<'frame'|'classified'>('frame'); const outputPromotionCategory = ref<Ref2VAReferenceCategory>('character'); const outputPromotionEntityId = ref(''); const outputPromotionName = ref(''); const selectedProjectId = ref(''); const episodeIds = ref<string[]>([]); const resolutionProfile = ref<ProductionResolutionProfile>('0.3'); const resolutionMultiple = ref(32); const imageResolutionProfile = ref<ProductionResolutionProfile>('0.98'); const imageResolutionMultiple = ref(32); const createLoading = ref(false); const retryLoading = ref(false); const cancelLoading = ref(false); const deleteConfirmOpen = ref(false); const deleteLoading = ref(false); const deleteTargetId = ref(''); const publishingOutputKey = ref(''); const promotingAssetKey = ref(''); const healthChecking = ref(false); const health = ref<{ok:boolean;message:string;latency?:number}|null>(null); const pollError = ref(''); const nowMs = ref(Date.now()); const outputMediaRecoveryAttempts = new Map<string,number>(); const referenceImageRecoveryAttempts = new Map<string,number>(); let ticker = 0; let clockTicker = 0; let healthTicker = 0; let promptRequestSequence = 0; let detailRequestSequence = 0; let workflowInspectRequestSequence = 0
const productionWorkflowTemplates = ref<ProductionWorkflowTemplate[]>([]); const productionWorkflowTemplatesLoading = ref(false); const productionWorkflowTemplatesError = ref(''); const ref2vaTemplateId = ref(''); const ref2vaWorkflowParameters = ref<Record<string,number>>({})
const mockMode = import.meta.env.VITE_USE_MOCK === 'true'
const healthLevel = computed<HealthLevel>(()=>{
  if (!health.value) return 'checking'
  if (health.value.ok) return 'connected'
  return /system_stats|500|internal server|健康检查|health check/i.test(health.value.message) ? 'degraded' : 'unavailable'
})
const healthBadge = computed(()=>healthLevel.value==='connected'?'CONNECTED':healthLevel.value==='degraded'?'DEGRADED':healthLevel.value==='unavailable'?'UNAVAILABLE':'CHECKING')
const isActiveTask = (workflow: Workflow) => ['queued', 'running', 'paused'].includes(workflow.status)
const taskKind = (workflow?: Workflow | null): 'video' | 'image' => workflow?.kind === 'image' || workflow?.nodes.some((node) => node.type === 'image' && node.id.startsWith('generation-')) ? 'image' : 'video'
const taskMediaLabel = (workflow?: Workflow | null) => taskKind(workflow) === 'image' ? '图片' : '视频'
const matchesTaskFilter = (workflow: Workflow, filter: TaskFilter) => filter === 'all' || (filter === 'active' ? isActiveTask(workflow) : workflow.status === filter)
const taskTime = (workflow: Workflow) => new Date(workflow.createdAt || workflow.queuedAt || 0).getTime() || 0
const filteredWorkflows = computed(() => studio.workflows
  .filter((workflow) => matchesTaskFilter(workflow, taskFilter.value))
  .slice()
  .sort((left, right) => {
    if (taskFilter.value === 'all') {
      const activeOrder = Number(!isActiveTask(left)) - Number(!isActiveTask(right))
      if (activeOrder) return activeOrder
    }
    return taskTime(right) - taskTime(left)
  }))
const taskFilters = computed<Array<{ key: TaskFilter; label: string; count: number }>>(() => [
  { key: 'all', label: '全部', count: studio.workflows.length },
  { key: 'active', label: '运行中', count: studio.workflows.filter(isActiveTask).length },
  { key: 'completed', label: '已完成', count: studio.workflows.filter((workflow) => workflow.status === 'completed').length },
  { key: 'failed', label: '失败', count: studio.workflows.filter((workflow) => workflow.status === 'failed').length }
])
const selected = computed(() => filteredWorkflows.value.find((item)=>item.id===selectedId.value))
const progressPresentation = (workflow: Workflow) => workflowProgressPresentation(workflow)
const selectedProgress = computed(() => selected.value ? progressPresentation(selected.value) : null)
const selectedCanCancel = computed(()=>{
  const workflow = selected.value
  if (!workflow) return false
  const backendStatus = workflow.backendStatus || workflow.status
  return ['queued','running','cancel_requested'].includes(backendStatus) || workflow.cancelAllowed === true
})
const isDeletableTask = (workflow: Workflow) => {
  const backendStatus = String(workflow.backendStatus || '').toLowerCase()
  if (['succeeded', 'failed', 'cancelled'].includes(backendStatus)) return true
  return mockMode && !backendStatus && ['completed', 'failed'].includes(workflow.status)
}
const selectedCanDelete = computed(() => Boolean(selected.value && isDeletableTask(selected.value)))
const deleteTarget = computed(() => studio.workflows.find((workflow) => workflow.id === deleteTargetId.value))
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
  const legacy = workflow as Workflow & { capability_snapshot?: Record<string, unknown>; capabilitySnapshot?: Record<string, unknown> }
  const source = workflow.inputParameters || legacy.capability_snapshot || legacy.capabilitySnapshot || {}
  const safe = (safeUiParameterValue(source, 'input_parameters') || {}) as Record<string, unknown>
  return {
    ...safe,
    ...(safe.seed === undefined && workflow.seed !== undefined ? { seed: workflow.seed } : {}),
    ...(safe.effective_frames === undefined && workflow.effectiveFrames ? { effective_frames: workflow.effectiveFrames } : {}),
    ...(safe.effective_duration_ms === undefined && workflow.effectiveDurationMs ? { effective_duration_ms: workflow.effectiveDurationMs } : {}),
    ...(safe.model_profile === undefined && workflow.modelProfile ? { model_profile: workflow.modelProfile } : {})
  }
}
const selectedInputParameters = computed<Record<string, unknown>>(() => selected.value ? taskInputParameters(selected.value) : {})
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
const findParameter = (source: Record<string, unknown>, aliases: string[], depth = 0): unknown => {
  for (const alias of aliases) if (Object.prototype.hasOwnProperty.call(source, alias)) return source[alias]
  if (depth >= 4) return undefined
  for (const value of Object.values(source)) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) continue
    const nested = findParameter(value as Record<string, unknown>, aliases, depth + 1)
    if (nested !== undefined) return nested
  }
  return undefined
}
const isTextOnlyTask = (source: Record<string, unknown>) => {
  const workflowMode = String(findParameter(source, ['workflow_mode']) || '').trim().toLowerCase()
  const effectiveMode = String(findParameter(source, ['effective_conditioning_mode']) || findParameter(source, ['h3_mode', 'mode']) || '').trim().toUpperCase()
  return workflowMode === 'text_to_video' || effectiveMode === 'T2VA'
}
const selectedIsTextOnly = computed(() => taskKind(selected.value) === 'video' && isTextOnlyTask(selectedInputParameters.value))
const selectedReferenceAssets = computed(() => selectedIsTextOnly.value || taskKind(selected.value) === 'image' ? [] : selected.value?.referenceAssets || [])
const selectedContinuityAssets = computed(() => {
  const modelInputKeys = new Set(selectedReferenceAssets.value.map((asset, index) => referenceAssetKey(asset, index)))
  // During a rolling deployment an older backend can still return historical
  // T2VA assets through reference_assets. Treat them as continuity-only until
  // the new field arrives; an explicit continuity_assets: [] still wins.
  const explicitAssets = selected.value?.continuityAssets
  const imageTask = taskKind(selected.value) === 'image'
  const sourceAssets = imageTask
    ? [...(selected.value?.referenceAssets || []), ...(explicitAssets || [])]
    : explicitAssets === undefined && selectedIsTextOnly.value
      ? selected.value?.referenceAssets || []
      : explicitAssets || []
  const seen = new Set<string>()
  return sourceAssets.filter((asset, index) => {
    const key = referenceAssetKey(asset, index)
    if (modelInputKeys.has(key) || seen.has(key)) return false
    seen.add(key)
    return true
  })
})
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
const parameterNumber = (value: unknown): number | undefined => value !== null && value !== '' && Number.isFinite(Number(value)) ? Number(value) : undefined
const parameterBoolean = (value: unknown): boolean | undefined => {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value !== 'string') return undefined
  if (/^(true|1|yes|on|enabled)$/i.test(value)) return true
  if (/^(false|0|no|off|disabled)$/i.test(value)) return false
  return undefined
}
const referenceCounts = (source: Record<string, unknown>) => {
  const nested = findParameter(source, ['reference_counts', 'reference_input_counts'])
  const nestedRecord = nested && typeof nested === 'object' && !Array.isArray(nested) ? nested as Record<string, unknown> : {}
  const countValue = (aliases: string[], nestedAliases: string[]) => {
    const directValue = findParameter(source, aliases)
    if (Array.isArray(directValue)) return directValue.length
    const direct = parameterNumber(directValue)
    if (direct !== undefined) return direct
    for (const key of nestedAliases) {
      const value = nestedRecord[key]
      if (Array.isArray(value)) return value.length
      const count = parameterNumber(value)
      if (count !== undefined) return count
    }
    return undefined
  }
  const images = countValue(['reference_image_count', 'image_reference_count', 'reference_images'], ['image', 'images'])
  const videos = countValue(['reference_video_count', 'video_reference_count', 'reference_videos'], ['video', 'videos'])
  const audios = countValue(['reference_audio_count', 'audio_reference_count', 'reference_audios'], ['audio', 'audios'])
  const inputCount = parameterNumber(findParameter(source, ['reference_input_count', 'input_connection_count']))
  let assetCount = parameterNumber(findParameter(source, ['reference_asset_count', 'reference_count']))
  const split = [images, videos, audios].filter((value): value is number => value !== undefined)
  if (assetCount === undefined) {
    const references = findParameter(source, ['reference_assets', 'reference_inputs', 'references', 'reference_bindings'])
    if (Array.isArray(references)) assetCount = references.length
    else if (references && typeof references === 'object') {
      const counts = Object.values(references as Record<string, unknown>).map((value) => Array.isArray(value) ? value.length : parameterNumber(value)).filter((value): value is number => value !== undefined)
      if (counts.length) assetCount = counts.reduce((sum, value) => sum + value, 0)
    }
  }
  if (assetCount === undefined && split.length) assetCount = split.reduce((sum, value) => sum + value, 0)
  return { assetCount, inputCount, images, videos, audios, detailed: split.length > 0 }
}
const formatParameterDuration = (requestedMs?: number, effectiveMs?: number, requestedSeconds?: number) => {
  const requested = requestedMs !== undefined ? requestedMs / 1000 : requestedSeconds
  const effective = effectiveMs !== undefined ? effectiveMs / 1000 : undefined
  if (requested === undefined && effective === undefined) return '未记录'
  const value = (seconds: number) => `${seconds.toFixed(3)} 秒`
  if (requested !== undefined && effective !== undefined) return `${value(requested)} → ${value(effective)}`
  return value(requested ?? effective!)
}
const taskScopeLabel = (workflow: Workflow, source: Record<string, unknown>) => {
  const rawShotIds = findParameter(source, ['shot_ids'])
  const shotCount = parameterNumber(findParameter(source, ['shot_count'])) ?? (Array.isArray(rawShotIds) ? rawShotIds.length : 0)
  const batchId = workflow.batchId || String(findParameter(source, ['batch_id']) || '')
  const batchIndex = workflow.batchIndex ?? parameterNumber(findParameter(source, ['batch_index']))
  const batchSize = workflow.batchSize ?? parameterNumber(findParameter(source, ['batch_size']))
  if (batchId) return `整集批次子任务 ${batchIndex ?? '?'}/${batchSize ?? '?'} · 本任务 ${shotCount || 1} 镜`
  if (shotCount > 1) return `单任务合并 ${shotCount} 镜`
  return `单镜${workflow.shotNumber ? ` · 镜头 ${String(workflow.shotNumber).padStart(2, '0')}` : ''}`
}
const coreInputParameters = computed<ParameterItem[]>(() => {
  const workflow = selected.value
  if (!workflow) return []
  const imageTask = taskKind(workflow) === 'image'
  const source = selectedInputParameters.value
  const requestedMode = findParameter(source, ['h3_mode', 'mode'])
  const effectiveMode = findParameter(source, ['effective_conditioning_mode'])
  const profile = findParameter(source, imageTask ? ['image_resolution_profile', 'resolution_profile', 'megapixels', 'resolution_tier'] : ['resolution_profile', 'megapixels', 'resolution_tier'])
  const width = parameterNumber(findParameter(source, ['width', 'output_width']))
  const height = parameterNumber(findParameter(source, ['height', 'output_height']))
  const multiple = parameterNumber(findParameter(source, imageTask ? ['image_resolution_multiple', 'resolution_multiple', 'multiple'] : ['resolution_multiple', 'multiple']))
  const requestedDurationMs = parameterNumber(findParameter(source, ['requested_duration_ms', 'duration_ms']))
  const effectiveDurationMs = parameterNumber(findParameter(source, ['effective_duration_ms']))
  const durationSeconds = parameterNumber(findParameter(source, ['duration_seconds', 'duration']))
  const frames = parameterNumber(findParameter(source, ['effective_frames', 'frames', 'frame_count', 'length']))
  const fps = parameterNumber(findParameter(source, ['fps', 'frame_rate']))
  const seed = findParameter(source, ['seed', 'noise_seed'])
  const audio = parameterBoolean(findParameter(source, ['include_audio', 'audio_enabled', 'enable_audio']))
  const references = referenceCounts(source)
  const textOnly = selectedIsTextOnly.value
  const noModelReferences = textOnly || imageTask
  const modelInputAssetCount = noModelReferences ? 0 : references.assetCount ?? selectedReferenceAssets.value.length
  const modelInputConnectionCount = noModelReferences ? 0 : references.inputCount
  const modelInputSummary = noModelReferences
    ? '未接入模型 · 工作流连接 0'
    : [
        `素材 ${modelInputAssetCount}`,
        references.detailed ? `图片 ${references.images ?? 0} / 视频 ${references.videos ?? 0} / 音频 ${references.audios ?? 0}` : '',
        modelInputConnectionCount !== undefined ? `工作流连接 ${modelInputConnectionCount}` : ''
      ].filter(Boolean).join(' · ') || '未记录'
  const continuityAssetCount = parameterNumber(findParameter(source, ['continuity_asset_count'])) ?? selectedContinuityAssets.value.length
  const resolution = `${profile !== undefined ? profile === 'project' ? '项目默认' : `${profile} MP` : '档位未记录'}${width && height ? ` · ${width} × ${height}` : ' · 实际尺寸未记录'}`
  return [
    imageTask
      ? { key: 'mode', label: '图片工作流', value: `${workflow.modelProfile || 'SDXL 图片生成'}${findParameter(source, ['template_slug']) ? ` · ${findParameter(source, ['template_slug'])}` : ''}` }
      : { key: 'mode', label: '工作流模式', value: `选择工作流 ${requestedMode || '未记录'} · 实际条件 ${effectiveMode || requestedMode || '未记录'}` },
    { key: 'resolution', label: '分辨率档位 / 实际尺寸', value: resolution },
    { key: 'multiple', label: '尺寸倍数', value: multiple !== undefined ? String(multiple) : '未记录' },
    ...(!imageTask ? [
      { key: 'duration', label: '请求时长 → 实际时长', value: formatParameterDuration(requestedDurationMs, effectiveDurationMs, durationSeconds) },
      { key: 'frames', label: '帧数 / FPS', value: frames !== undefined || fps !== undefined ? `${frames ?? '未记录'} 帧 · ${fps ?? '未记录'} FPS` : '未记录' }
    ] : []),
    { key: 'seed', label: 'Seed', value: seed !== undefined && seed !== '' ? String(seed) : '未记录' },
    ...(!imageTask ? [{ key: 'audio', label: '音频', value: audio === undefined ? '未记录' : audio ? '已启用' : '未启用' }] : []),
    { key: 'references', label: '模型参考输入', value: modelInputSummary },
    ...(continuityAssetCount ? [{ key: 'continuity', label: '关联一致性素材', value: `${continuityAssetCount} 项 · 仅供连续性核对，未接入模型` }] : []),
    { key: 'scope', label: '生产范围', value: taskScopeLabel(workflow, source) }
  ]
})
const coreParameterKeys = new Set(['workflow_mode','effective_conditioning_mode','h3_mode','mode','resolution_profile','image_resolution_profile','megapixels','resolution_tier','width','output_width','height','output_height','resolution_multiple','image_resolution_multiple','multiple','requested_duration_ms','effective_duration_ms','duration_ms','duration_seconds','duration','effective_frames','frames','frame_count','length','fps','frame_rate','seed','noise_seed','include_audio','audio_enabled','enable_audio','reference_assets','reference_input_count','input_connection_count','reference_count','reference_asset_count','reference_counts','reference_input_counts','reference_image_count','image_reference_count','reference_images','reference_video_count','video_reference_count','reference_videos','reference_audio_count','audio_reference_count','reference_audios','continuity_assets','continuity_asset_count','prompt_available','prompt_character_count','negative_prompt_character_count','scope','generation_scope','batch_kind','shot_count','batch_id','batch_index','batch_size','whole_episode'])
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
const technicalInputParameters = computed(() => flattenParameters(selectedInputParameters.value).filter((item) => !coreParameterKeys.has(item.key.split('.').slice(-1)[0] || item.key)))
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
      backend: workflow.backend,
      model_profile: workflow.modelProfile,
      workflow_version: workflow.workflowVersion,
      generated_workflow_id: workflow.generatedWorkflowId,
      comfy_prompt_id: workflow.comfyPromptId
    },
    input_parameters: selectedInputParameters.value
  }
})
const selectedBatchAllTasks = computed(() => {
  if (!selected.value?.batchId) return []
  return studio.workflows.filter((item) => item.batchId === selected.value!.batchId).slice().sort((left, right) => (left.batchIndex || 0) - (right.batchIndex || 0))
})
const selectedBatchTasks = computed(() => {
  const latestByIndex = new Map<string, Workflow>()
  for (const task of selectedBatchAllTasks.value) {
    const key = task.batchIndex ? String(task.batchIndex) : task.id
    const previous = latestByIndex.get(key)
    if (!previous || taskTime(task) >= taskTime(previous)) latestByIndex.set(key, task)
  }
  return [...latestByIndex.values()].sort((left, right) => (left.batchIndex || 0) - (right.batchIndex || 0))
})
const selectedBatchSize = computed(() => Math.max(selected.value?.batchSize || 0, selectedBatchTasks.value.length))
const selectedBatchCompleted = computed(() => selectedBatchTasks.value.filter((item) => item.status === 'completed').length)
const selectedBatchFailed = computed(() => selectedBatchTasks.value.filter((item) => item.status === 'failed').length)
const selectedBatchActive = computed(() => selectedBatchTasks.value.filter(isActiveTask).length)
const selectedBatchCompletionPercent = computed(() => selectedBatchSize.value
  ? Math.round(selectedBatchCompleted.value / selectedBatchSize.value * 100)
  : 0)
const selectedBatchProgressLabel = computed(() => `完成 ${selectedBatchCompleted.value}/${selectedBatchSize.value || 0}`)
const episodeContextLabel = (workflow: Workflow) => workflow.episodeNumber ? `第 ${workflow.episodeNumber} 集${workflow.episodeTitle ? ` · ${workflow.episodeTitle}` : ''}` : workflow.episodeTitle || ''
const shotContextLabel = (workflow: Workflow) => workflow.shotNumber ? `镜头 ${String(workflow.shotNumber).padStart(2, '0')}${workflow.shotTitle ? ` · ${workflow.shotTitle}` : ''}` : workflow.shotTitle || ''
const batchTaskStatus = (workflow: Workflow) => workflow.status === 'completed' ? '完成' : workflow.status === 'failed' ? '失败' : workflow.status === 'running' ? '运行中' : '排队中'
const selectedMedia = computed<TaskOutputMedia[]>(() => selected.value?.outputMedia || [])
const outputPromotionProject = computed(() => {
  const projectId = outputPromotionSelection.value?.projectId || ''
  return projectId ? studio.projectById(projectId) : undefined
})
const outputPromotionEntities = computed<BibleItem[]>(() => {
  const bible = outputPromotionProject.value?.bible
  if (!bible) return []
  if (outputPromotionCategory.value === 'character') return bible.characters
  if (outputPromotionCategory.value === 'scene') return bible.scenes
  return bible.props
})
const outputPromotionHasEntities = computed(() => Boolean(
  outputPromotionProject.value && (
    outputPromotionProject.value.bible.characters.length
    || outputPromotionProject.value.bible.scenes.length
    || outputPromotionProject.value.bible.props.length
  )
))
const outputPromotionCategoryOptions = computed(() => [
  { value:'character' as const, label:'人物', count:outputPromotionProject.value?.bible.characters.length||0 },
  { value:'scene' as const, label:'场景', count:outputPromotionProject.value?.bible.scenes.length||0 },
  { value:'prop' as const, label:'道具', count:outputPromotionProject.value?.bible.props.length||0 },
])
const outputPromotionCategoryLabel = (category: Ref2VAReferenceCategory) => (
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
  if (selected.value?.status === 'failed') return '任务执行失败，没有可预览的媒体产物。展开失败原因后可直接重试。'
  return `任务完成后，原始${taskMediaLabel(selected.value)}会自动出现在这里${taskKind(selected.value)==='video'?'，无需另行组装':''}。`
})
const inspectedTask = computed(()=>studio.workflows.find((item)=>item.id===inspectedTaskId.value)||selected.value)
const selectedProject = computed(()=>studio.projects.find((item)=>item.id===selectedProjectId.value))
const selectedProductionEpisodes = computed(() => {
  const project = selectedProject.value
  if (!project) return []
  return episodeIds.value.length ? project.episodes.filter((episode) => episodeIds.value.includes(episode.id)) : project.episodes
})
const selectedProductionHasVideo = computed(() => selectedProductionEpisodes.value.some((episode) => episode.shots.some((shot) => shot.mediaType === 'video')))
const selectedProductionHasImage = computed(() => selectedProductionEpisodes.value.some((episode) => episode.shots.some((shot) => shot.mediaType === 'image')))
const selectedProductionHasRef2VA = computed(() => selectedProductionEpisodes.value.some((episode) => episode.shots.some((shot) => shot.mediaType === 'video' && shot.mode === 'Ref2VA')))
const productionWorkflowSelections = computed<ProductionWorkflowSelection[]>(() => selectedProductionHasRef2VA.value && ref2vaTemplateId.value ? [{ outputKind: 'video', h3Mode: 'Ref2VA', templateId: ref2vaTemplateId.value, parameters: { ...ref2vaWorkflowParameters.value } }] : [])
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
const resolutionProfiles: Array<{value:ProductionResolutionProfile;label:string;hint:string;badge:string}> = [
  { value: '0.1', label: '官方 0.1 MP', hint: '416×256（16:9），最省显存的验证档位', badge: '最省显存' },
  { value: '0.2', label: '官方 0.2 MP', hint: '最低显存占用，首次烟测最稳', badge: '最稳' },
  { value: '0.3', label: '官方 0.3 MP', hint: 'GTX 1070 8GB 推荐，已有历史成功产物', badge: '8GB 推荐' },
  { value: 'project', label: '项目默认', hint: '沿用项目目标尺寸；当前机器曾发生显存不足', badge: '高显存' }
]
const advancedResolutionProfiles: Array<{value:ProductionResolutionProfile;label:string}> = [
  { value: '0.4', label: '0.4 MP · 显存占用增加' }, { value: '0.5', label: '0.5 MP · 显存占用增加' },
  { value: '0.6', label: '0.6 MP · 显存占用增加' }, { value: '0.7', label: '0.7 MP · 显存占用增加' },
  { value: '0.8', label: '0.8 MP · 高显存' }, { value: '0.9', label: '0.9 MP · 高显存' },
  { value: '0.98', label: '0.98 MP · 高显存' }, { value: '1.0', label: '1.0 MP · 极高显存，易 OOM' },
  { value: '1.2', label: '1.2 MP · 极高显存，易 OOM' }, { value: '1.5', label: '1.5 MP · 极高显存，易 OOM' },
  { value: '1.8', label: '1.8 MP · 极高显存，易 OOM' }, { value: '2.0', label: '2.0 MP · 极高显存，易 OOM' }
]
const projectDimensions = (project: SeriesProject) => {
  if (project.width && project.height) return [project.width, project.height]
  const portrait = project.resolution === '4K' ? [2176, 3840] : project.resolution === '1080p' ? [1088, 1920] : [768, 1344]
  if (project.aspectRatio === '16:9') return [portrait[1], portrait[0]]
  if (project.aspectRatio === '1:1') return [portrait[0], portrait[0]]
  return portrait
}
const profileDimensions = (profile: ProductionResolutionProfile) => {
  const project = selectedProject.value
  if (!project) return '选择项目后显示实际尺寸'
  const multiple = Number.isFinite(resolutionMultiple.value) ? Math.min(128,Math.max(8,Math.round(resolutionMultiple.value/4)*4)) : 32
  const rounded = (value:number) => Math.max(multiple,Math.round(value/multiple)*multiple)
  if (profile === 'project') return `${projectDimensions(project).map(rounded).join('×')} · ${project.aspectRatio}`
  const officialLandscape: Record<Exclude<ProductionResolutionProfile, 'project'>, [number, number]> = {
    '0.1': [416, 256], '0.2': [608, 352], '0.3': [736, 416], '0.4': [864, 480], '0.5': [960, 544],
    '0.6': [1056, 608], '0.7': [1152, 640], '0.8': [1216, 672], '0.9': [1280, 736], '0.98': [1344, 768],
    '1.0': [1376, 768], '1.2': [1504, 832], '1.5': [1664, 928], '1.8': [1824, 1024], '2.0': [1920, 1088]
  }
  let dimensions: number[]
  if (multiple === 32) {
    const landscape = officialLandscape[profile]
    dimensions = project.aspectRatio === '1:1'
      ? [Math.round(Math.sqrt(Number(profile)*1024*1024)/multiple)*multiple, Math.round(Math.sqrt(Number(profile)*1024*1024)/multiple)*multiple]
      : project.aspectRatio === '16:9' ? landscape : [...landscape].reverse()
  } else {
    const [widthRatio,heightRatio] = project.aspectRatio === '1:1' ? [1,1] : project.aspectRatio === '16:9' ? [16,9] : [9,16]
    const scale = Math.sqrt(Number(profile)*1024*1024/(widthRatio*heightRatio))
    dimensions = [Math.round(widthRatio*scale/multiple)*multiple,Math.round(heightRatio*scale/multiple)*multiple]
  }
  return `${dimensions.join('×')} · ${project.aspectRatio}`
}
const normalizeResolutionMultiple=()=>{const value=Number(resolutionMultiple.value);resolutionMultiple.value=Number.isFinite(value)?Math.min(128,Math.max(8,Math.round(value/4)*4)):32;const imageValue=Number(imageResolutionMultiple.value);imageResolutionMultiple.value=Number.isFinite(imageValue)?Math.min(128,Math.max(8,Math.round(imageValue/4)*4)):32}
const advancedProfileSelected = computed(()=>advancedResolutionProfiles.some((item)=>item.value===resolutionProfile.value))
const selectAdvancedProfile=(event:Event)=>{const value=(event.target as HTMLSelectElement).value as ProductionResolutionProfile;if(value)resolutionProfile.value=value}
const isHighMemoryProfile=(profile:ProductionResolutionProfile)=>profile==='project'||(Number.isFinite(Number(profile))&&Number(profile)>=0.98)
const nodeIcons:Record<WorkflowNode['type'],typeof FileText>={script:FileText,prompt:Sparkles,image:Image,video:Film,audio:AudioLines,compose:Layers3,export:Download}
const nodeStatusCount=(status:WorkflowNode['status'])=>selected.value?.nodes.filter((node)=>node.status===status).length||0
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
const selectedRuntimeAlert = computed<RuntimeAlert|null>(()=>{
  const workflow = selected.value
  if (!workflow || !isActiveTask(workflow)) return null
  const heartbeat = workflow.lastHeartbeatAt ? `最后心跳 ${formatTaskDateTime(workflow.lastHeartbeatAt, true)}。` : ''
  if (workflow.backendStatus === 'cancel_requested') return {
    level: 'warning', title: '取消请求正在处理',
    message: `${heartbeat}系统正在等待 ComfyUI 确认中断；可点击“重新确认取消”立即核对远端队列与历史并刷新终态。`
  }
  if (workflow.isStalled) return {
    level: 'danger', title: '任务可能已经卡住',
    message: `${workflow.stallReason || '任务长时间没有新的执行心跳。'} ${heartbeat}可先检查 ComfyUI，再取消当前任务。`.trim()
  }
  if (healthLevel.value === 'degraded') return {
    level: 'warning', title: 'ComfyUI 健康检查异常',
    message: `${health.value?.message || 'ComfyUI API 可达，但健康接口返回异常。'} 当前任务可能仍在远端执行；页面会持续刷新，也可取消任务后再处理。`
  }
  if (healthLevel.value === 'unavailable') return {
    level: 'danger', title: '暂时无法连接 ComfyUI',
    message: `${health.value?.message || 'ComfyUI 当前不可达。'} 任务状态暂时无法得到远端确认，请勿重复创建相同任务；可取消当前任务。`
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
const taskStateSummary=(workflow:Workflow)=>workflow.backendStatus==='cancel_requested'?'取消请求处理中':workflow.isStalled?workflow.stallReason||'任务可能已卡住':progressPresentation(workflow).summary
const taskProgressLabel=(workflow:Workflow)=>workflow.backendStatus==='cancel_requested'?'取消中':workflow.isStalled?'需检查':progressPresentation(workflow).compactLabel
const hydrateSelectedTask=async(workflowId=selectedId.value,quiet=true,preserveMediaUrls=false)=>{
  const workflow=studio.workflows.find((item)=>item.id===workflowId)
  if(!workflow)return
  const taskId=workflow.taskId||workflow.id
  const requestSequence=++detailRequestSequence
  try {
    const detail=await repository.getWorkflow(taskId)
    if(requestSequence!==detailRequestSequence||selectedId.value!==workflowId)return
    return studio.mergeWorkflowDetail(detail,preserveMediaUrls)
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
  const detail=await repository.getWorkflow(taskId)
  if(selectedId.value!==workflowId)throw new Error('当前已切换到其他任务')
  const merged=studio.mergeWorkflowDetail(detail,false)
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
const publishOutputMedia=async(media:TaskOutputMedia,index:number)=>{
  const workflow=selected.value
  if(!workflow)return
  const key=outputMediaRecoveryKey(media,index)
  publishingOutputKey.value=key
  try{
    const detail=await repository.publishTaskOutput(workflow.taskId||workflow.id,media.index??index)
    studio.mergeWorkflowDetail(detail,false)
    studio.notify('公开发行副本已生成：母版未修改，下载文件含可见 AI 标识、元数据和签名来源清单','success')
  }catch(error){studio.notify(error instanceof Error?error.message:'公开发行副本生成失败','error')}
  finally{publishingOutputKey.value=''}
}
const setOutputPromotionCategory=(category:Ref2VAReferenceCategory)=>{
  outputPromotionCategory.value=category
  outputPromotionEntityId.value=(
    category==='character'?outputPromotionProject.value?.bible.characters[0]?.id:
    category==='scene'?outputPromotionProject.value?.bible.scenes[0]?.id:
    outputPromotionProject.value?.bible.props[0]?.id
  )||''
}
const openOutputPromotion=(media:TaskOutputMedia,index:number)=>{
  const workflow=selected.value
  if(!workflow||mediaKind(media)!=='image')return
  outputPromotionSelection.value={
    taskId:workflow.taskId||workflow.id,
    workflowId:workflow.id,
    projectId:workflow.projectId,
    media,
    index,
  }
  outputPromotionMode.value='frame'
  outputPromotionName.value=''
  const project=studio.projectById(workflow.projectId)
  const firstCategory:Ref2VAReferenceCategory=project?.bible.characters.length?'character':project?.bible.scenes.length?'scene':'prop'
  outputPromotionCategory.value=firstCategory
  outputPromotionEntityId.value=(firstCategory==='character'?project?.bible.characters[0]?.id:firstCategory==='scene'?project?.bible.scenes[0]?.id:project?.bible.props[0]?.id)||''
  outputPromotionOpen.value=true
}
const closeOutputPromotion=()=>{
  if(promotingAssetKey.value)return
  outputPromotionOpen.value=false
  outputPromotionSelection.value=null
  outputPromotionName.value=''
}
const promoteOutputToAsset=async()=>{
  const selection=outputPromotionSelection.value
  if(!selection)return
  let target:GeneratedImageAssetTarget|undefined
  if(outputPromotionMode.value==='classified'){
    if(!outputPromotionEntityId.value){studio.notify('请先选择要绑定的人物、场景或道具','error');return}
    target={referenceCategory:outputPromotionCategory.value,entityId:outputPromotionEntityId.value}
  }
  const key=outputMediaRecoveryKey(selection.media,selection.index)
  promotingAssetKey.value=key
  try{
    const asset=await repository.promoteTaskOutputToAsset(
      selection.taskId,
      selection.media.index??selection.index,
      outputPromotionName.value,
      target,
    )
    let refreshWarning=''
    try{await Promise.all([studio.refreshAssets(),studio.refreshProjects()])}
    catch(error){refreshWarning=error instanceof Error?error.message:'素材或 Bible 刷新失败'}
    try{
      const detail=await repository.getWorkflow(selection.taskId)
      studio.mergeWorkflowDetail(detail,false)
    }catch(error){refreshWarning ||= error instanceof Error?error.message:'任务状态刷新失败'}
    outputPromotionOpen.value=false
    outputPromotionSelection.value=null
    const purpose=target
      ? `已绑定到${outputPromotionCategoryLabel(target.referenceCategory)} Bible，可在 Ref2VA 中作为分类参考图选择`
      : '已保存为完整画面锚点，可在首帧/尾帧工作流中选择'
    studio.notify(
      refreshWarning?`图片已保存，但页面刷新失败：${refreshWarning}`:`已保存到素材库：${asset.name}。${purpose}。`,
      refreshWarning?'info':'success',
    )
  }catch(error){studio.notify(error instanceof Error?error.message:'保存到素材库失败','error')}
  finally{promotingAssetKey.value=''}
}
const reconcileSelected=()=>{
  if(filteredWorkflows.value.some((workflow)=>workflow.id===selectedId.value))return
  const nextId=filteredWorkflows.value[0]?.id||''
  if(selectedId.value!==nextId){selectedId.value=nextId;failureExpanded.value=false}
}
const setTaskFilter=(filter:TaskFilter)=>{taskFilter.value=filter;taskMenuOpen.value=false;parametersExpanded.value=false;reconcileSelected();if(selectedId.value)void hydrateSelectedTask(selectedId.value)}
const selectTask=(id:string)=>{if(selectedId.value!==id){failureExpanded.value=false;parametersExpanded.value=false};taskMenuOpen.value=false;selectedId.value=id;void hydrateSelectedTask(id,false)}
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
const copyInputParameters=async()=>{if(!selected.value)return;closeTaskMenu();await copyPlainText(JSON.stringify(selectedInputParameters.value,null,2),'输入参数 JSON')}
const downloadTaskConfig=()=>{
  const workflow=selected.value;if(!workflow)return;closeTaskMenu()
  const blob=new Blob([JSON.stringify(selectedTaskConfig.value,null,2)],{type:'application/json;charset=utf-8'})
  const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download=`task-${workflow.taskId||workflow.id}-config.json`;anchor.click();window.setTimeout(()=>URL.revokeObjectURL(url),0);studio.notify('任务配置 JSON 已下载')
}
const refreshTaskStatus=async()=>{
  const taskId=selected.value?.id;if(!taskId)return;closeTaskMenu()
  try{await studio.refreshWorkflows(true);pollError.value='';selectedId.value=studio.workflows.some((workflow)=>workflow.id===taskId)?taskId:selectedId.value;reconcileSelected();if(selectedId.value)await hydrateSelectedTask(selectedId.value);studio.notify('任务状态已刷新')}
  catch(error){const message=error instanceof Error?error.message:'服务端未返回最新任务状态';pollError.value=message;studio.notify(`刷新任务状态失败：${message}`,'error')}
}
const selectProject=(id:string)=>{selectedProjectId.value=id;episodeIds.value=[]}
const toggleEpisode=(id:string)=>{const i=episodeIds.value.indexOf(id);if(i>=0)episodeIds.value.splice(i,1);else episodeIds.value.push(id)}
const selectAll=()=>{episodeIds.value=episodeIds.value.length===selectedProject.value?.episodes.length?[]:selectedProject.value?.episodes.map(e=>e.id)||[]}
const create=async()=>{if(!selectedProject.value){studio.notify('请选择一个连续剧项目','error');return}if(!selectedProductionHasVideo.value&&!selectedProductionHasImage.value){studio.notify('所选范围没有可生产的分镜','error');return}normalizeResolutionMultiple();createLoading.value=true;try{const item=await studio.createWorkflow(selectedProject.value,episodeIds.value,resolutionProfile.value,resolutionMultiple.value,imageResolutionProfile.value,imageResolutionMultiple.value,productionWorkflowSelections.value);taskFilter.value='active';selectedId.value=item.id;failureExpanded.value=false;createOpen.value=false;if(mockMode){item.status='running';item.currentStep='剧本拆解';item.nodes[0].status='running';item.nodes[0].progress=8}else await refresh();reconcileSelected()}catch(e){studio.notify(e instanceof Error?e.message:'创建失败','error')}finally{createLoading.value=false}}
const loadProductionWorkflowTemplates=async()=>{productionWorkflowTemplatesLoading.value=true;productionWorkflowTemplatesError.value='';try{productionWorkflowTemplates.value=await repository.getProductionWorkflowTemplates()}catch(error){productionWorkflowTemplatesError.value=error instanceof Error?error.message:'服务端未返回工作流模板'}finally{productionWorkflowTemplatesLoading.value=false}}
const openCreateWizard=()=>{ref2vaTemplateId.value='';ref2vaWorkflowParameters.value={};createOpen.value=true}
const toggleRun=(flow:Workflow)=>{if(!mockMode){studio.notify('真实任务由服务端队列管理，请使用取消或重试 API','info');return}if(flow.status==='running'){flow.status='paused';flow.currentStep='已手动暂停'}else if(flow.status==='paused'||flow.status==='queued'){flow.status='running';flow.currentStep=flow.nodes.find(n=>n.status!=='completed')?.label||'恢复运行'} }
const simulate=()=>{if(!mockMode)return;studio.workflows.filter(f=>f.status==='running').forEach(flow=>{const node=flow.nodes.find(n=>n.status==='running')||flow.nodes.find(n=>n.status==='pending');if(!node)return;if(node.status==='pending')node.status='running';node.progress=Math.min(100,node.progress+1);flow.progress=Math.round(flow.nodes.reduce((s,n)=>s+n.progress,0)/flow.nodes.length);flow.progressSource='comfyui';flow.comfyProgress=node.progress;flow.progressTelemetryState='receiving';flow.remoteState='running';flow.currentStep=`${node.label} · 当前节点采样 ${node.progress}%`;if(node.progress>=100){node.status='completed';const next=flow.nodes.find(n=>n.status==='pending');if(next){next.status='running';flow.comfyProgress=next.progress;flow.currentStep=next.label}else{flow.status='completed';flow.progress=100;flow.progressSource='terminal';flow.comfyProgress=undefined;flow.remoteState='succeeded';flow.currentStep='已完成'}}});reconcileSelected()}
const refresh=async(preserveMediaUrls=false)=>{try{await studio.refreshWorkflows(preserveMediaUrls);pollError.value='';reconcileSelected();if(selectedId.value)await hydrateSelectedTask(selectedId.value,true,preserveMediaUrls)}catch(error){pollError.value=error instanceof Error?error.message:'队列刷新失败'}}
const cancelTask=async()=>{if(!selected.value||!selectedCanCancel.value)return;const taskId=selected.value.taskId||selected.value.id;cancelLoading.value=true;try{const result=await repository.cancelGenerationTask(taskId);studio.notify(result.warning?`取消请求已记录：${result.warning}`:result.status==='cancel_requested'?'取消请求已记录，正在等待 ComfyUI 确认':'任务已取消',result.warning?'info':'success');await refresh();selectedId.value=result.id||taskId;reconcileSelected()}catch(error){studio.notify(error instanceof Error?error.message:'取消任务失败','error')}finally{cancelLoading.value=false}}
const openDeleteConfirm=()=>{if(!selected.value||!selectedCanDelete.value)return;deleteTargetId.value=selected.value.id;deleteConfirmOpen.value=true}
const closeDeleteConfirm=()=>{if(deleteLoading.value)return;deleteConfirmOpen.value=false;deleteTargetId.value=''}
const deleteTask=async()=>{
  const target = deleteTarget.value
  if (!target || !isDeletableTask(target)) { studio.notify('当前任务不是可删除的历史终态任务','error'); closeDeleteConfirm(); return }
  const visibleIds = filteredWorkflows.value.map((workflow) => workflow.id)
  const currentIndex = visibleIds.indexOf(target.id)
  const preferredNextId = visibleIds[currentIndex + 1] || visibleIds[currentIndex - 1] || ''
  deleteLoading.value = true
  try {
    await repository.deleteGenerationTask(target.taskId || target.id)
    const localIndex = studio.workflows.findIndex((workflow) => workflow.id === target.id)
    if (localIndex >= 0) studio.workflows.splice(localIndex, 1)
    deleteConfirmOpen.value = false
    deleteTargetId.value = ''
    let refreshWarning = ''
    try { await studio.refreshWorkflows(false); pollError.value = '' }
    catch (error) { refreshWarning = error instanceof Error ? error.message : '任务列表刷新失败'; pollError.value = refreshWarning }
    selectedId.value = preferredNextId && filteredWorkflows.value.some((workflow) => workflow.id === preferredNextId)
      ? preferredNextId
      : filteredWorkflows.value[0]?.id || ''
    failureExpanded.value = false
    parametersExpanded.value = false
    taskMenuOpen.value = false
    studio.notify(refreshWarning ? `历史任务已删除，但列表刷新失败：${refreshWarning}` : '历史任务已删除', refreshWarning ? 'info' : 'success')
  } catch (error) {
    studio.notify(`删除历史任务失败：${error instanceof Error ? error.message : '服务端拒绝了删除请求'}`, 'error')
  } finally { deleteLoading.value = false }
}
const retry=async()=>{if(!selected.value||selected.value.status!=='failed')return;retryLoading.value=true;try{const result=await repository.retryGenerationTask(selected.value.taskId||selected.value.id);taskFilter.value='active';await refresh();selectedId.value=String(result.id);failureExpanded.value=false;reconcileSelected();studio.notify('重试任务已创建并重新进入队列')}catch(error){studio.notify(error instanceof Error?error.message:'任务重试失败','error')}finally{retryLoading.value=false}}
const loadWorkflowExport=async(task:Workflow,format:'ui'|'api')=>{const requestSequence=++workflowInspectRequestSequence;workflowInspectLoading.value=true;workflowInspectError.value='';workflowExport.value=null;try{const result=await repository.getGeneratedWorkflowExport(task,format);if(requestSequence===workflowInspectRequestSequence)workflowExport.value=result}catch(error){if(requestSequence===workflowInspectRequestSequence)workflowInspectError.value=error instanceof Error?error.message:'读取工作流失败'}finally{if(requestSequence===workflowInspectRequestSequence)workflowInspectLoading.value=false}}
const inspectWorkflow=async(task:Workflow)=>{inspectedTaskId.value=task.id;workflowInspectOpen.value=true;workflowExportFormat.value='ui';workflowSyncResult.value=null;await loadWorkflowExport(task,'ui')}
const switchWorkflowFormat=async(format:'ui'|'api')=>{if(format===workflowExportFormat.value&&workflowExport.value)return;workflowExportFormat.value=format;workflowSyncResult.value=null;const task=inspectedTask.value;if(task)await loadWorkflowExport(task,format)}
const copyWorkflow=async()=>{if(!exportedWorkflowJson.value)return;try{await navigator.clipboard.writeText(exportedWorkflowJson.value);studio.notify('实际 ComfyUI 工作流 JSON 已复制')}catch{studio.notify('复制失败，请使用下载 JSON','error')}}
const downloadWorkflow=()=>{if(!workflowExport.value||!exportedWorkflowJson.value)return;const blob=new Blob([exportedWorkflowJson.value],{type:'application/json;charset=utf-8'});const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download=workflowExport.value.filename;anchor.click();window.setTimeout(()=>URL.revokeObjectURL(url),0);studio.notify(`${workflowFormatLabel.value} 已下载`)}
const syncWorkflowToComfy=async()=>{const task=inspectedTask.value;if(!task||!isCanvasWorkflow.value||workflowExport.value?.importReady!==true)return;workflowSyncing.value=true;try{const result=await repository.syncGeneratedWorkflowToComfyUI(task.taskId||task.id);workflowSyncResult.value=result;studio.notify(`工作流已同步到 ComfyUI：${result.workflowPath}`)}catch(error){studio.notify(error instanceof Error?error.message:'同步到 ComfyUI 失败','error')}finally{workflowSyncing.value=false}}
const openInComfy=()=>{if(!safeComfyUrl.value){studio.notify(workflowExport.value?.openReason||'当前没有可安全打开的 ComfyUI 地址','error');return}const opened=window.open(safeComfyUrl.value,'_blank','noopener,noreferrer');if(opened)opened.opener=null}
const checkHealth=async()=>{if(healthChecking.value)return;healthChecking.value=true;try{health.value=await repository.testComfy('')}catch(error){health.value={ok:false,message:error instanceof Error?error.message:'ComfyUI 不可达'}}finally{healthChecking.value=false}}
onMounted(async()=>{document.addEventListener('pointerdown',onTaskMenuPointerDown);document.addEventListener('keydown',onTaskMenuKeydown);await Promise.all([studio.initialize(),loadProductionWorkflowTemplates()]);const requestedTask=String(Array.isArray(route.query.task)?route.query.task[0]||'':route.query.task||'');const requestedBatch=String(Array.isArray(route.query.batch)?route.query.batch[0]||'':route.query.batch||'');const target=studio.workflows.find((item)=>item.id===requestedTask||item.taskId===requestedTask)||studio.workflows.filter((item)=>requestedBatch&&item.batchId===requestedBatch).sort((left,right)=>(left.batchIndex||0)-(right.batchIndex||0))[0];if(target){taskFilter.value='all';selectedId.value=target.id}else reconcileSelected();selectedProjectId.value=studio.projects[0]?.id||'';if(selectedId.value)await hydrateSelectedTask(selectedId.value);await checkHealth();ticker=window.setInterval(mockMode?simulate:()=>refresh(true),mockMode?1600:5000);healthTicker=window.setInterval(()=>{void checkHealth()},15000);clockTicker=window.setInterval(()=>{nowMs.value=Date.now()},1000)})
onBeforeUnmount(()=>{promptRequestSequence+=1;detailRequestSequence+=1;workflowInspectRequestSequence+=1;document.removeEventListener('pointerdown',onTaskMenuPointerDown);document.removeEventListener('keydown',onTaskMenuKeydown);window.clearInterval(ticker);window.clearInterval(healthTicker);window.clearInterval(clockTicker)})
</script>

<template>
  <div class="workflow-page page-stack">
    <section class="page-heading split-heading"><div><span class="eyebrow">AUTONOMOUS PRODUCTION PIPELINE</span><h2>媒体生产工作流</h2><p>将视频分镜交给 MiniMax H3、图片定帧交给 SDXL，并统一进入本地 ComfyUI 队列；只有视频产物参与按集组装。</p></div><button class="button primary" @click="openCreateWizard"><Plus :size="16"/>创建生产任务</button></section>
    <section class="pipeline-health panel"><div class="health-item"><span class="health-icon" :class="{online:healthLevel==='connected',degraded:healthLevel==='degraded'}"><Server :size="18"/></span><div><span>ComfyUI 服务 · 每 15 秒检测</span><strong>{{ health?.message || '正在探测服务端连接…' }}</strong></div><i :class="{neutral:healthLevel==='checking'||healthLevel==='unavailable',warning:healthLevel==='degraded'}">{{ healthBadge }}</i></div><div class="health-divider"></div><div class="health-item"><span class="health-icon"><Cpu :size="18"/></span><div><span>执行策略</span><strong>本地 CUDA · 单任务安全模式</strong></div><i class="neutral">LIMIT 1</i></div><div class="health-divider"></div><div class="health-item"><span class="health-icon"><Sparkles :size="18"/></span><div><span>生成模型</span><strong>MiniMax H3 · SDXL</strong></div><i class="neutral">VIDEO + IMAGE</i></div><RouterLink to="/settings" class="button ghost small">连接状态 <ArrowRight :size="14"/></RouterLink></section>

    <section class="workflow-layout">
      <aside class="panel workflow-list">
        <header><div><span class="eyebrow">TASK QUEUE</span><h3>生产任务</h3></div><button class="icon-btn" :disabled="studio.loading" title="刷新任务列表" aria-label="刷新任务列表" @click="refresh(false)"><RefreshCw :size="15"/></button></header>
        <p v-if="pollError" class="form-error">队列刷新失败：{{ pollError }}</p>
        <div class="workflow-filters" role="group" aria-label="按任务状态筛选">
          <button v-for="filter in taskFilters" :key="filter.key" type="button" :data-status-filter="filter.key" :class="{active:taskFilter===filter.key}" :aria-pressed="taskFilter===filter.key" :aria-label="`${filter.label}，${filter.count} 个任务`" @click="setTaskFilter(filter.key)"><span>{{ filter.label }}</span><b>{{ filter.count }}</b></button>
        </div>
        <div class="workflow-list-scroll">
          <button v-for="flow in filteredWorkflows" :key="flow.id" type="button" :data-task-id="flow.id" :class="[flow.status,{active:selected?.id===flow.id}]" :aria-pressed="selected?.id===flow.id" @click="selectTask(flow.id)">
            <div class="workflow-item-top"><StatusBadge :status="flow.status"/><b v-if="flow.batchId">整集批次 {{ flow.batchIndex || '?' }}/{{ flow.batchSize || '?' }}</b><time :datetime="taskStartAt(flow)" :title="taskTimestampTitle(flow)">{{ formatTaskDateTime(taskStartAt(flow)) }}</time></div>
            <strong :title="flow.projectTitle">{{ flow.projectTitle }}</strong>
            <div class="workflow-item-summary"><p>{{ taskStateSummary(flow) }}</p><span><Clock3 :size="10"/>{{ taskDurationLabel(flow) }}</span></div>
            <div class="workflow-item-progress" :class="[flow.status,{indeterminate:progressPresentation(flow).indeterminate}]"><i><b :style="{width:progressPresentation(flow).indeterminate?'8%':`${progressPresentation(flow).percent || 0}%`}"></b></i><span>{{ taskProgressLabel(flow) }}</span></div>
          </button>
          <div v-if="!filteredWorkflows.length" class="workflow-filter-empty" role="status"><WorkflowIcon :size="24"/><strong>{{ studio.workflows.length?`${selectedFilterLabel}暂无任务`:'暂无生产任务' }}</strong><p>{{ studio.workflows.length?'切换其他状态即可查看对应任务。':'创建任务后会在这里显示基本信息与状态。' }}</p></div>
        </div>
      </aside>

      <main v-if="selected" class="workflow-detail">
        <section class="panel workflow-overview">
          <header>
            <div><span class="eyebrow">WORKFLOW {{ selected.id.toUpperCase() }}</span><h2>{{ selected.projectTitle }}</h2><div><StatusBadge :status="selected.status"/><span>{{ selected.currentStep }}</span><span><Clock3 :size="13"/>{{ taskTimeKind(selected) }}于 {{ formatTaskDateTime(taskStartAt(selected)) }}</span><span>{{ taskDurationLabel(selected) }}</span><span v-if="selected.finishedAt">完成于 {{ formatTaskDateTime(selected.finishedAt) }}</span><span v-if="selected.maxAttempts">尝试 {{ selected.attempts }}/{{ selected.maxAttempts }}</span></div></div>
            <div class="workflow-main-actions">
              <button class="button ghost" :disabled="!selected.generatedWorkflowId" title="只读查看，不会提交或中断任务" @click="inspectWorkflow(selected)"><Eye :size="15"/>在 ComfyUI 查看工作流</button>
              <RouterLink v-if="selected.projectId&&selected.episodeId&&selected.shotId" class="button ghost" :to="{path:`/projects/${selected.projectId}/episodes/${selected.episodeId}/storyboard`,query:{shot:selected.shotId}}"><Clapperboard :size="15"/>修改并重新生成此镜头</RouterLink>
              <button v-if="selectedCanCancel" class="button ghost task-cancel-button" :disabled="cancelLoading" @click="cancelTask"><Ban :size="15"/>{{ cancelLoading?'正在核对…':selected.backendStatus==='cancel_requested'?'重新确认取消':'取消任务' }}</button>
              <button v-if="selected.status==='failed'" class="button primary" :disabled="retryLoading||(selected.retryAllowed===false&&!selected.retryRequiresReconciliation)" :title="selected.retryRequiresReconciliation?'将先确认旧 Prompt 已不在 ComfyUI 队列或历史中，再安全创建重试任务':selected.retryAllowed===false?selected.retryBlockReason||'服务端尚未确认远端任务已终止，暂不能安全重试':''" @click="retry"><RefreshCw :size="15"/>{{ retryLoading?'正在验证并创建重试…':selected.retryRequiresReconciliation?'验证远端并重试':'重试失败任务' }}</button>
              <button v-if="selectedCanDelete" class="button ghost task-delete-button" @click="openDeleteConfirm"><Trash2 :size="15"/>删除历史任务</button>
              <button v-if="mockMode&&selected.status!=='completed'" class="button ghost" @click="toggleRun(selected)"><component :is="selected.status==='running'?Pause:Play" :size="15"/>{{ selected.status==='running'?'暂停 Mock 任务':'继续 Mock 任务' }}</button>
              <div ref="taskMenuRoot" class="task-more-menu">
                <button type="button" class="icon-btn" aria-label="打开任务更多操作" aria-haspopup="menu" aria-controls="task-more-actions" :aria-expanded="taskMenuOpen" title="任务更多操作" @click.stop="taskMenuOpen=!taskMenuOpen"><MoreHorizontal :size="17"/></button>
                <div v-if="taskMenuOpen" id="task-more-actions" class="task-more-menu-popover" role="menu" aria-label="任务更多操作">
                  <button type="button" role="menuitem" @click="copyTaskId"><Clipboard :size="14"/><span><strong>复制任务 ID</strong><small>{{ selected.taskId || selected.id }}</small></span></button>
                  <button v-if="selected.comfyPromptId" type="button" role="menuitem" @click="copyComfyPromptId"><Clipboard :size="14"/><span><strong>复制 Comfy Prompt ID</strong><small>{{ selected.comfyPromptId }}</small></span></button>
                  <button v-if="selected.promptAvailable!==false" type="button" role="menuitem" @click="openInputPrompt()"><FileText :size="14"/><span><strong>查看实际提交提示词</strong><small>{{ promptSummary }}</small></span></button>
                  <button type="button" role="menuitem" @click="copyInputParameters"><Code2 :size="14"/><span><strong>复制输入参数 JSON</strong><small>已自动排除绝对本地路径</small></span></button>
                  <button type="button" role="menuitem" @click="downloadTaskConfig"><Download :size="14"/><span><strong>下载任务配置 JSON</strong><small>任务标识、上下文与安全输入参数</small></span></button>
                  <button type="button" role="menuitem" @click="refreshTaskStatus"><RefreshCw :size="14"/><span><strong>刷新任务状态</strong><small>立即重新读取服务端任务与产物</small></span></button>
                </div>
              </div>
            </div>
          </header>
          <div v-if="selected.episodeId||selected.shotId||selected.batchId" class="task-context-summary"><span v-if="selected.episodeId"><small>剧集</small><strong>{{ episodeContextLabel(selected) }}</strong></span><span v-if="selected.shotId"><small>分镜</small><strong>{{ shotContextLabel(selected) }}</strong></span><span v-if="selected.batchId"><small>批次位置</small><strong>整集批次 {{ selected.batchIndex || '?' }}/{{ selected.batchSize || '?' }}</strong></span></div>
          <div v-if="selectedRuntimeAlert" class="task-runtime-alert" :class="selectedRuntimeAlert.level" role="status"><AlertTriangle :size="16"/><div><strong>{{ selectedRuntimeAlert.title }}</strong><p>{{ selectedRuntimeAlert.message }}</p></div><button type="button" :disabled="selected.backendStatus==='cancel_requested'?cancelLoading:healthChecking" @click="selected.backendStatus==='cancel_requested'?cancelTask():checkHealth()"><RefreshCw :size="12"/>{{ selected.backendStatus==='cancel_requested'?(cancelLoading?'核对中':'重新确认取消'):(healthChecking?'检测中':'重新检测') }}</button></div>
          <div v-if="selectedProgress" class="big-progress" :class="{indeterminate:selectedProgress.indeterminate}"><div><span>{{ selectedProgress.heading }}</span><strong>{{ selectedProgress.valueLabel }}</strong></div><i><b :style="{width:selectedProgress.indeterminate?'8%':`${selectedProgress.percent || 0}%`}"></b></i><p v-if="selectedProgress.note">{{ selectedProgress.note }}</p></div>
        </section>

        <section class="panel task-input-parameters">
          <header><div><span class="eyebrow">INPUT PARAMETERS</span><h3>输入参数</h3><p>展示任务提交时固化的参数快照，刷新页面后仍可核对；本地绝对路径不会显示或导出。</p></div><button v-if="technicalInputParameters.length" type="button" :aria-expanded="parametersExpanded" aria-controls="task-technical-parameters" @click="parametersExpanded=!parametersExpanded"><span>{{ parametersExpanded?'收起技术参数':'查看更多参数' }}</span><b>{{ technicalInputParameters.length }}</b><ChevronDown :size="13" :class="{expanded:parametersExpanded}"/></button></header>
          <div class="task-input-core"><article v-for="item in coreInputParameters" :key="item.key"><small>{{ item.label }}</small><strong :title="item.value">{{ item.value }}</strong></article><article class="task-prompt-parameter"><small>实际提交提示词</small><button type="button" :disabled="selected.promptAvailable===false" :title="selected.promptAvailable===false?'该历史任务未保存可审计的实际提交提示词':'点击后才从服务端读取完整提示词'" @click="openInputPrompt()"><FileText :size="13"/><span>{{ promptSummary }}</span><ArrowRight :size="12"/></button></article></div>
          <div v-for="group in referenceAssetGroups" :key="group.key" class="task-reference-inputs" :class="{continuity:group.continuity}"><header><div><Layers3 :size="15"/><span><strong>{{ group.title }}</strong><small>{{ group.description }}</small></span></div><b v-if="group.continuity">未接入模型</b></header><div class="task-reference-gallery"><article v-for="(asset,index) in group.assets" :key="referenceAssetKey(asset,index)">
            <button v-if="asset.mediaKind==='image'&&publicReferenceUrl(asset)&&!referenceImageFailures[referenceAssetKey(asset,index)]" type="button" class="reference-image-thumb" :aria-label="`打开大图预览：${asset.displayName}`" @click="openReferenceImage(asset,index)"><img :src="publicReferenceUrl(asset)" :alt="asset.displayName" loading="lazy" @load="handleReferenceImageLoaded(asset,index)" @error="recoverReferenceImage(asset,index)"/><span><Eye :size="13"/>查看大图</span></button>
            <div v-else-if="asset.mediaKind==='video'&&publicReferenceUrl(asset)" class="reference-media-control"><video :src="publicReferenceUrl(asset)" controls preload="metadata">当前浏览器无法播放该参考视频。</video></div>
            <div v-else-if="asset.mediaKind==='audio'&&publicReferenceUrl(asset)" class="reference-media-control audio"><AudioLines :size="22"/><audio :src="publicReferenceUrl(asset)" controls preload="metadata">当前浏览器无法播放该参考音频。</audio></div>
            <div v-else class="reference-media-fallback"><Image v-if="asset.mediaKind==='image'" :size="23"/><Film v-else-if="asset.mediaKind==='video'" :size="23"/><AudioLines v-else :size="23"/><span>{{ publicReferenceUrl(asset)?'预览加载失败':'未提供预览地址' }}</span></div>
            <footer><div><strong :title="asset.displayName">{{ asset.displayName }}</strong><span>{{ referenceMediaLabel(asset.mediaKind) }}<template v-if="!group.continuity&&asset.inputLabel"> · {{ asset.inputLabel }}</template><template v-else-if="group.continuity&&(asset.entityLabel||asset.category)"> · {{ asset.entityLabel || asset.category }}</template></span><span v-if="!group.continuity&&referenceTimingLabel(asset)" class="reference-timing" :title="referenceTimingLabel(asset)">{{ referenceTimingLabel(asset) }}</span></div><a v-if="asset.mediaKind!=='image'&&publicReferenceUrl(asset)" :href="publicReferenceUrl(asset)" target="_blank" rel="noopener noreferrer">打开<ExternalLink :size="11"/></a></footer>
          </article></div></div>
          <div v-if="parametersExpanded" id="task-technical-parameters" class="task-input-technical"><div v-for="item in technicalInputParameters" :key="item.key"><code>{{ item.label }}</code><span :title="item.value">{{ item.value }}</span></div></div>
        </section>

        <section v-if="selected.batchId" class="panel episode-batch-overview">
          <header><div><span class="eyebrow">WHOLE EPISODE BATCH</span><h3>{{ episodeContextLabel(selected) || '整集生产批次' }}</h3><p>批次 {{ selected.batchId.slice(0,12) }} · 已加载 {{ selectedBatchTasks.length }}/{{ selectedBatchSize }} 个分镜任务</p></div><strong>{{ selectedBatchProgressLabel }}</strong></header>
          <div class="episode-batch-progress"><i><b :style="{width:`${selectedBatchCompletionPercent}%`}"></b></i><span>已完成 {{ selectedBatchCompleted }}/{{ selectedBatchSize }} · 运行/排队 {{ selectedBatchActive }} · 失败 {{ selectedBatchFailed }}</span></div>
          <div class="episode-batch-tasks"><button v-for="task in selectedBatchTasks" :key="task.id" :class="[task.status,{active:task.id===selected.id}]" @click="selectTask(task.id)"><span>{{ task.batchIndex || '?' }}/{{ task.batchSize || selectedBatchSize }}</span><strong>{{ shotContextLabel(task) || task.projectTitle }}</strong><small>{{ batchTaskStatus(task) }}</small></button></div>
        </section>

        <section v-if="selected.status==='failed'" class="panel task-error-detail">
          <header>
            <div class="task-error-title"><span><AlertTriangle :size="16"/></span><div><span class="eyebrow">FAILURE DETAILS</span><h3>失败原因</h3></div></div>
            <button v-if="hasFailureDetail" type="button" class="task-error-toggle" :aria-expanded="failureExpanded" aria-controls="selected-task-error-detail" @click="failureExpanded=!failureExpanded">{{ failureExpanded?'收起':'查看更多' }}<ChevronDown :size="13" :class="{expanded:failureExpanded}"/></button>
          </header>
          <div class="task-error-meta"><span>任务 ID <code>{{ selected.taskId || selected.id }}</code></span><span v-if="selected.finishedAt">结束于 {{ new Date(selected.finishedAt).toLocaleString('zh-CN') }}</span></div>
          <p v-if="!failureExpanded" class="task-error-summary">{{ failureSummary }}</p>
          <pre v-else id="selected-task-error-detail" class="task-error-trace">{{ selectedErrorMessage || failureSummary }}</pre>
        </section>

        <section class="panel node-canvas">
          <header>
            <div><span class="eyebrow">COMFYUI ORCHESTRATION</span><h3>生产节点</h3></div>
            <div class="node-legend" aria-label="节点状态统计">
              <span><i class="done"></i><b>{{ nodeStatusCount('completed') }}</b>完成</span>
              <span><i class="active"></i><b>{{ nodeStatusCount('running') }}</b>运行</span>
              <span v-if="nodeStatusCount('failed')"><i class="failed"></i><b>{{ nodeStatusCount('failed') }}</b>失败</span>
              <span><i></i><b>{{ nodeStatusCount('pending') }}</b>等待</span>
            </div>
          </header>
          <div class="node-flow" role="list" aria-label="ComfyUI 生产节点执行进度">
            <template v-for="(node,index) in selected.nodes" :key="node.id">
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
              <div v-if="index<selected.nodes.length-1" class="node-connector" :class="{done:node.status==='completed'}" aria-hidden="true"><i></i><ArrowRight :size="12"/></div>
            </template>
          </div>
        </section>

        <section class="workflow-bottom-grid">
          <div class="panel task-log"><header><div><span class="eyebrow">TASK HISTORY</span><h3>执行记录</h3></div><div class="task-log-actions"><span>{{ logs.length }} 条记录</span><button @click="refresh(false)">刷新</button></div></header><div class="log-scroll"><div v-for="log in logs" :key="log.key" :class="log.type"><span>{{ log.time }}</span><i></i><p><strong>{{ log.text }}</strong><small v-if="log.details">{{ log.details }}</small></p></div><div v-if="!logs.length" class="empty-compact"><Code2 :size="22"/><p>任务创建后将在这里显示真实执行记录</p></div></div></div>
          <div class="panel output-summary" :class="{'is-empty':!selectedMedia.length}">
            <header><div><span class="eyebrow">TASK OUTPUTS</span><h3>本任务原始产物</h3></div><span class="output-total">{{ selectedMedia.length }} 个文件</span></header>
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
                    <button v-if="mediaKind(media)==='image'" type="button" class="button ghost small" :disabled="Boolean(media.promotedAssetId)||promotingAssetKey===outputMediaRecoveryKey(media,index)" @click="openOutputPromotion(media,index)"><FolderInput :size="13"/>{{ media.promotedAssetId?'已存素材库':promotingAssetKey===outputMediaRecoveryKey(media,index)?'正在保存…':'保存并选择用途' }}</button>
                    <a v-if="media.provenanceUrl" :href="media.provenanceUrl" target="_blank" rel="noopener" class="button ghost small"><FileJson :size="13"/>母版来源</a>
                    <a v-if="media.publicProvenanceUrl" :href="media.publicProvenanceUrl" target="_blank" rel="noopener" class="button ghost small"><FileJson :size="13"/>发行清单</a>
                    <button v-if="media.previewUrl" type="button" class="button ghost small" @click="openOutputMedia(media,index)"><Eye :size="13"/>预览母版</button>
                    <button v-if="media.downloadUrl" type="button" class="button primary small" @click="downloadOutputMedia(media,index)"><Download :size="13"/>{{ media.distributionStatus==='development_master'?'下载母版':'公开下载' }}</button>
                    <button v-else-if="mediaKind(media)==='video'||mediaKind(media)==='image'" type="button" class="button primary small" :disabled="publishingOutputKey===outputMediaRecoveryKey(media,index)" @click="publishOutputMedia(media,index)"><RefreshCw :size="13"/>{{ publishingOutputKey===outputMediaRecoveryKey(media,index)?'正在生成…':'生成公开副本' }}</button>
                  </div>
                </footer>
              </article>
            </div>
            <div v-else class="task-output-empty" role="status"><component :is="taskKind(selected)==='image'?Image:Film" :size="23"/><div><strong>暂无可预览产物</strong><p>{{ emptyOutputMessage }}</p></div><button v-if="selected.status==='completed'" class="button ghost small" @click="refresh(false)"><RefreshCw :size="13"/>刷新产物</button></div>
            <p class="task-output-note">此处展示 ComfyUI 返回的单任务原始文件。开发模式可直接下载母版用于调试；生产模式只提供带标识且验证通过的发行副本。来源清单只公开哈希和模型/任务标识，不泄露完整提示词或本地路径。</p>
          </div>
        </section>
      </main>
      <main v-else class="panel empty-state"><WorkflowIcon :size="30"/><h3>{{ studio.workflows.length?'当前筛选没有任务':'创建第一个工作流' }}</h3><p>{{ studio.workflows.length?'当前状态下没有可展示的任务，请切换筛选条件。':'选择连续剧和需要制作的集数，系统会自动编排全部节点。' }}</p><button v-if="studio.workflows.length" class="button ghost" @click="setTaskFilter('all')">查看全部任务</button><button v-else class="button primary" @click="createOpen=true"><Plus :size="15"/>创建任务</button></main>
    </section>

    <BaseModal :open="outputPromotionOpen" title="保存生成图片到素材库" eyebrow="REUSABLE IMAGE ASSET" width="680px" @close="closeOutputPromotion">
      <div v-if="outputPromotionSelection" class="output-promotion-form">
        <div class="output-promotion-source"><Image :size="21"/><span><small>当前图片产物</small><strong>{{ outputPromotionSelection.media.filename }}</strong></span></div>
        <label class="field"><span>素材名称（可选）</span><input v-model="outputPromotionName" maxlength="160" placeholder="留空将使用镜头标题自动命名"></label>
        <div class="output-promotion-mode-grid">
          <button type="button" :class="{active:outputPromotionMode==='frame'}" @click="outputPromotionMode='frame'">
            <Image :size="20"/><span><strong>仅作为完整画面锚点</strong><small>保存为整帧图，适合首帧 / 尾帧；不会宣称画面只代表某个人物、场景或道具。</small></span><Check :size="14"/>
          </button>
          <button type="button" :class="{active:outputPromotionMode==='classified'}" :disabled="!outputPromotionHasEntities" @click="outputPromotionMode='classified'">
            <Layers3 :size="20"/><span><strong>绑定 Ref2VA 分类目标</strong><small>选择一个 Bible 实体，保存后可在参考生视频中作为严格分类图片输入。</small></span><Check :size="14"/>
          </button>
        </div>
        <section v-if="outputPromotionMode==='classified'" class="output-promotion-target">
          <header><strong>选择图片代表的唯一实体</strong><small>分类会固化，保存后不能把同一产物悄悄改绑到其他实体。</small></header>
          <div class="output-promotion-categories">
            <button v-for="option in outputPromotionCategoryOptions" :key="option.value" type="button" :class="{active:outputPromotionCategory===option.value}" :disabled="!option.count" @click="setOutputPromotionCategory(option.value)">{{ option.label }} · {{ option.count }}</button>
          </div>
          <label class="field"><span>{{ outputPromotionCategoryLabel(outputPromotionCategory) }} Bible 实体</span><select v-model="outputPromotionEntityId"><option value="" disabled>请选择实体</option><option v-for="entity in outputPromotionEntities" :key="entity.id" :value="entity.id">{{ entity.name }}</option></select></label>
          <p><AlertTriangle :size="14"/>分类参考图用于定义单一人物身份、固定场景或关键道具；若图片是包含多主体的完整构图，请选择“仅作为完整画面锚点”。</p>
        </section>
        <p v-else class="output-promotion-frame-note"><Image :size="14"/>完整画面锚点可直接用于首帧 / 尾帧，但不会自动出现在 Ref2VA 的人物、场景、道具分类列表中。</p>
      </div>
      <template #footer><button class="button ghost" :disabled="Boolean(promotingAssetKey)" @click="closeOutputPromotion">取消</button><button class="button primary" :disabled="Boolean(promotingAssetKey)||(outputPromotionMode==='classified'&&!outputPromotionEntityId)" @click="promoteOutputToAsset"><FolderInput :size="15"/>{{ promotingAssetKey?'正在复制并绑定…':'确认保存' }}</button></template>
    </BaseModal>

    <BaseModal :open="workflowInspectOpen" title="ComfyUI 工作流详情" eyebrow="READ-ONLY WORKFLOW INSPECTOR" width="900px" @close="workflowInspectOpen=false">
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
          <div v-if="inspectedTask.batchId"><span>整集批次</span><strong>{{ inspectedTask.batchIndex || '?' }}/{{ inspectedTask.batchSize || '?' }} · {{ inspectedTask.batchId }}</strong></div>
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
          <ol v-if="isCanvasWorkflow&&workflowExport.importReady&&!workflowSyncResult" class="workflow-import-steps"><li>推荐点击“同步到 ComfyUI 工作流库”，平台会保存到固定的 workflows/autoprompt 目录。</li><li>也可以下载 UI 画布 JSON 后手动拖入 ComfyUI。</li><li>打开或刷新 ComfyUI，在工作流库中选择该任务画布。</li><li>核对参考素材和输出路径后再手动运行；同步操作本身不会提交任务。</li></ol>
          <div v-if="workflowSyncResult" class="workflow-import-notice safe"><CheckCircle2 :size="19"/><div><strong>已同步到 ComfyUI 工作流库</strong><p>{{ workflowSyncResult.openReason }}</p></div><span>{{ workflowSyncResult.workflowPath }}</span></div>
          <div v-if="workflowExport.importReady===false" class="workflow-placeholder-warning"><AlertTriangle :size="15"/>后端未能确认 {{ isCanvasWorkflow?'UI 画布':'API Prompt 图' }} 已完整就绪；建议仅审计 JSON，不要直接执行。</div>
          <div v-if="workflowExport.containsStagedPlaceholders" class="workflow-placeholder-warning"><AlertTriangle :size="15"/>工作流包含 {{ workflowExport.stagedPlaceholderCount || 0 }} 个服务端暂存输入占位；请保持素材文件可访问，不要手动改变节点输入路径。</div>
          <div class="workflow-export-meta"><span>TEMPLATE <code>{{ workflowExport.templateSlug || '—' }} v{{ workflowExport.templateVersion || '—' }}</code></span><span>CONTENT HASH <code>{{ workflowExport.contentHash || '—' }}</code></span><span>FILE <code>{{ workflowExport.filename }}</code></span></div>
          <div class="workflow-json-preview"><pre>{{ exportedWorkflowJson }}</pre></div>
        </template>
      </div>
      <template #footer><button class="button ghost" @click="workflowInspectOpen=false">关闭</button><button class="button ghost" :disabled="!workflowExport" @click="copyWorkflow"><Clipboard :size="15"/>复制当前 JSON</button><button class="button ghost" :disabled="!workflowExport" @click="downloadWorkflow"><Download :size="15"/>下载{{ workflowFormatLabel }}</button><button v-if="isCanvasWorkflow" class="button ghost" :disabled="workflowSyncing||workflowExport?.importReady!==true" :title="workflowExport?.openReason" @click="syncWorkflowToComfy"><FolderInput :size="15"/>{{ workflowSyncing?'正在同步…':workflowSyncResult?'重新同步到 ComfyUI':'同步到 ComfyUI 工作流库' }}</button><button class="button primary" :disabled="!safeComfyUrl" :title="workflowSyncResult?.openReason||workflowExport?.openReason" @click="openInComfy"><ExternalLink :size="15"/>{{ workflowSyncResult?'打开 ComfyUI 查看已同步工作流':'打开 ComfyUI' }}</button></template>
    </BaseModal>

    <BaseModal :open="promptOpen" title="实际提交提示词" eyebrow="IMMUTABLE INPUT PROMPT" width="900px" @close="promptOpen=false">
      <div class="task-prompt-inspector">
        <div v-if="promptLoading" class="task-prompt-loading"><LoaderCircle :size="24"/><span>正在读取任务提交时固化的提示词…</span></div>
        <div v-else-if="promptError" class="task-prompt-error"><AlertTriangle :size="19"/><div><strong>无法读取实际提交提示词</strong><p>{{ promptError }}</p></div><button class="button ghost small" @click="openInputPrompt(true)"><RefreshCw :size="13"/>重试</button></div>
        <template v-else-if="inputPrompt">
          <div class="task-prompt-meta"><div><small>模型 / 实际条件模式</small><strong>{{ inputPrompt.model || '未记录' }} · {{ inputPrompt.effectiveConditioningMode || inputPrompt.h3Mode || '未记录' }}</strong></div><div><small>模板</small><code>{{ inputPrompt.promptTemplateKey || '未记录' }}</code></div><div><small>Prompt Artifact</small><code>{{ inputPrompt.promptArtifactId || '未记录' }}</code></div><div><small>内容哈希</small><code>{{ inputPrompt.contentHash || '未记录' }}</code></div></div>
          <div class="task-prompt-texts">
            <article><header><div><span>实际提交提示词</span><small>{{ inputPrompt.promptCharacterCount }} 字符 · {{ inputPrompt.promptSource || '来源未记录' }}</small></div><button type="button" :disabled="!inputPrompt.prompt" @click="copyPromptText('prompt')"><Clipboard :size="13"/>复制</button></header><pre>{{ inputPrompt.prompt || '该任务没有保存正向提示词文本。' }}</pre></article>
            <article class="negative"><header><div><span>负面提示词</span><small>{{ inputPrompt.negativePromptCharacterCount }} 字符 · {{ inputPrompt.negativePromptSource || '来源未记录' }}</small></div><button type="button" :disabled="!inputPrompt.negativePrompt" @click="copyPromptText('negative')"><Clipboard :size="13"/>复制</button></header><pre>{{ inputPrompt.negativePrompt || '该任务没有提交负面提示词。' }}</pre></article>
          </div>
        </template>
      </div>
      <template #footer><button class="button ghost" @click="promptOpen=false">关闭</button><button class="button primary" :disabled="!inputPrompt?.prompt" @click="copyPromptText('prompt')"><Clipboard :size="15"/>复制实际提示词</button></template>
    </BaseModal>

    <BaseModal :open="referencePreviewOpen" :title="referencePreviewAsset?.displayName || '素材图片预览'" eyebrow="ASSET IMAGE PREVIEW" width="1000px" @close="referencePreviewOpen=false">
      <div v-if="referencePreviewAsset" class="reference-image-preview">
        <img v-if="publicReferenceUrl(referencePreviewAsset)&&!referenceImageFailures[referenceAssetKey(referencePreviewAsset)]" :src="publicReferenceUrl(referencePreviewAsset)" :alt="referencePreviewAsset.displayName" @load="handleReferenceImageLoaded(referencePreviewAsset)" @error="recoverReferenceImage(referencePreviewAsset)"/>
        <div v-else class="reference-preview-fallback"><Image :size="34"/><strong>大图预览加载失败</strong><span>预览签名可能已经过期，可重新向服务端获取。</span><button class="button ghost small" @click="recoverReferenceImage(referencePreviewAsset,0,true)"><RefreshCw :size="13"/>重新获取预览</button></div>
        <footer><span>{{ referenceMediaLabel(referencePreviewAsset.mediaKind) }}<template v-if="referencePreviewAsset.inputLabel"> · {{ referencePreviewAsset.inputLabel }}</template></span><code v-if="referencePreviewAsset.assetId">{{ referencePreviewAsset.assetId }}</code></footer>
      </div>
      <template #footer><button class="button ghost" @click="referencePreviewOpen=false">关闭</button><a v-if="publicReferenceUrl(referencePreviewAsset)" class="button primary" :href="publicReferenceUrl(referencePreviewAsset)" target="_blank" rel="noopener noreferrer"><ExternalLink :size="15"/>在新窗口打开</a></template>
    </BaseModal>

    <BaseModal :open="deleteConfirmOpen" title="删除历史任务" eyebrow="DELETE TASK HISTORY" width="520px" @close="closeDeleteConfirm">
      <div class="delete-task-confirm">
        <span><AlertTriangle :size="21"/></span>
        <div><strong>确认永久删除这条任务记录？</strong><p>只删除平台任务记录，不会删除 ComfyUI 或本地磁盘中的产物文件，也不会删除提示词与工作流。删除已完成任务后，其产物将不再通过这条任务参与平台预览或组装。此操作无法撤销。</p></div>
      </div>
      <div v-if="deleteTarget" class="delete-task-target"><small>任务</small><strong>{{ deleteTarget.projectTitle }}</strong><code>{{ deleteTarget.taskId || deleteTarget.id }}</code></div>
      <template #footer><button class="button ghost" :disabled="deleteLoading" @click="closeDeleteConfirm">保留记录</button><button class="button task-delete-confirm-button" :disabled="deleteLoading" @click="deleteTask"><Trash2 :size="15"/>{{ deleteLoading?'正在删除…':'确认删除' }}</button></template>
    </BaseModal>

    <BaseModal :open="createOpen" title="创建媒体生产工作流" eyebrow="NEW PRODUCTION PIPELINE" width="720px" @close="createOpen=false">
      <div class="workflow-wizard">
        <div class="wizard-step"><span>01</span><div><h4>选择连续剧项目</h4><p>工作流会读取项目的一致性 Bible 和默认生产规格。</p><div class="project-select-grid"><button v-for="project in studio.projects" :key="project.id" :class="{active:selectedProjectId===project.id}" @click="selectProject(project.id)"><div :style="{background:studio.styles.find(s=>s.id===project.styleId)?.gradient}"><Clapperboard :size="20"/></div><span><strong>{{ project.title }}</strong><small>{{ project.episodes.length }} 集 · {{ studio.styles.find(s=>s.id===project.styleId)?.name }}</small></span><i><Check :size="12"/></i></button></div></div></div>
        <div class="wizard-step"><span>02</span><div><h4>选择生产范围</h4><p>不勾选时默认处理全剧；之后新增集数可再次追加任务。</p><button v-if="selectedProject" class="select-all" @click="selectAll"><span>{{ episodeIds.length===selectedProject.episodes.length?'取消全选':'全选所有剧集' }}</span><strong>{{ episodeIds.length }}/{{ selectedProject.episodes.length }}</strong></button><div class="episode-check-list"><button v-for="episode in selectedProject?.episodes" :key="episode.id" :class="{active:episodeIds.includes(episode.id)}" @click="toggleEpisode(episode.id)"><i><Check :size="11"/></i><span>EP {{ String(episode.number).padStart(2,'0') }}</span><strong>{{ episode.title }}</strong><small>{{ episode.shots.length }} 镜</small></button></div></div></div>
        <div v-if="selectedProductionHasRef2VA" class="wizard-step"><span>03</span><div><h4>Ref2VA 生成工作流</h4><p>可为所选范围内的 Ref2VA 视频指定工作流和对应参数；其他模式仍自动使用官方默认。</p><ProductionWorkflowPicker v-model:template-id="ref2vaTemplateId" v-model:parameters="ref2vaWorkflowParameters" :templates="productionWorkflowTemplates" :loading="productionWorkflowTemplatesLoading" :error="productionWorkflowTemplatesError" h3-mode="Ref2VA"/></div></div>
        <div v-if="selectedProductionHasVideo" class="wizard-step"><span>{{ selectedProductionHasRef2VA?'04':'03' }}</span><div><h4>H3 视频分辨率 / 显存档位</h4><p>此档位只应用到视频任务；视频 FPS 固定为 24。图片使用下方独立的 SDXL 档位。</p><div class="resolution-profile-grid"><button v-for="profile in resolutionProfiles" :key="profile.value" :class="{active:resolutionProfile===profile.value,danger:profile.value==='project'}" @click="resolutionProfile=profile.value"><span><strong>{{ profile.label }}</strong><i>{{ profile.badge }}</i></span><b>{{ profileDimensions(profile.value) }}</b><small>{{ profile.hint }}</small><Check :size="13"/></button></div><label class="resolution-profile-select" :class="{active:advancedProfileSelected}"><span><strong>更多官方档位</strong><small>0.4–2.0 MP · 高档位显存与耗时显著增加</small></span><select :value="advancedProfileSelected?resolutionProfile:''" @change="selectAdvancedProfile"><option value="" disabled>选择更高分辨率档位</option><option v-for="profile in advancedResolutionProfiles" :key="profile.value" :value="profile.value">{{ profile.label }} · {{ profileDimensions(profile.value) }}</option></select></label><label class="resolution-multiple-field"><span><strong>尺寸取整倍数</strong><small>官方默认 32；允许 8–128，按 4 递增</small></span><input v-model.number="resolutionMultiple" type="number" min="8" max="128" step="4" @blur="normalizeResolutionMultiple"/><b>宽高将取 {{ resolutionMultiple }} 的整数倍</b></label><div class="resolution-profile-note" :class="{danger:isHighMemoryProfile(resolutionProfile)}"><AlertTriangle :size="14"/><span>本次视频将提交 {{ resolutionProfile==='project'?'项目默认':`${resolutionProfile} MP` }}（{{ profileDimensions(resolutionProfile) }}），不修改项目设定。{{ resolutionProfile==='0.1'?'这是最省显存的低分辨率验证档位。':resolutionProfile==='0.3'?'这是本机 8GB 显存的推荐起点。':resolutionProfile==='0.2'?'这是低显存、稳定的烟测档位。':isHighMemoryProfile(resolutionProfile)?'该档位需要高显存，1.0 MP 及以上在 8GB 机器上极易 OOM，请谨慎使用。':'该档位比 0.3 MP 占用更多显存。' }}</span></div></div></div>
        <div v-if="selectedProductionHasImage" class="wizard-step"><span>{{ selectedProductionHasVideo?(selectedProductionHasRef2VA?'05':'04'):'03' }}</span><div><h4>SDXL 图片分辨率 / 显存档位</h4><p>图片独立使用本档位，不继承视频低显存档。推荐至少 0.8 MP，默认 0.98 MP；0.1–0.3 MP 仅用于连通性测试。</p><ResolutionProfilePicker v-model="imageResolutionProfile" v-model:multiple="imageResolutionMultiple" :multiple-step="8" label="SDXL 图片分辨率档位" compact/></div></div>
        <div class="workflow-plan"><Sparkles :size="17"/><p>将按镜头类型创建任务：视频使用 MiniMax H3 与原生音频，图片使用 SDXL 标准文生图。选择的工作流只应用到匹配的 Ref2VA 镜头；其他模式使用官方默认。任务详情可预览和下载两类原始产物；只有视频按集组装。</p></div>
      </div>
      <template #footer><button class="button ghost" @click="createOpen=false">取消</button><button class="button primary" :disabled="createLoading||(!selectedProductionHasVideo&&!selectedProductionHasImage)" @click="create"><WorkflowIcon :size="15"/>{{ createLoading?'正在编排…':'开始生产' }}</button></template>
    </BaseModal>
  </div>
</template>
