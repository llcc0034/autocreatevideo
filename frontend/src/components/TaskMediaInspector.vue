<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { AlertTriangle, ArrowRight, AudioLines, Check, CheckCircle2, ChevronDown, Clapperboard, Clipboard, Clock3, Code2, Cpu, Download, ExternalLink, Eye, FileJson, FileText, Film, FolderInput, Image, Layers3, LoaderCircle, Lock, MoreHorizontal, Music2, Plus, RefreshCw, Save, Server, Sparkles, WandSparkles, Workflow as WorkflowIcon } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import ExecutionPreparationWorkbench from '@/components/ExecutionPreparationWorkbench.vue'
import TaskExecutionChecks from '@/components/TaskExecutionChecks.vue'
import AssetMediaPreview from '@/components/AssetMediaPreview.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import FrozenReferenceCorrespondence from '@/components/FrozenReferenceCorrespondence.vue'
import { useStudioStore } from '@/stores/studio'
import { useAuthStore } from '@/stores/auth'
import { repository, type ProductionCapability } from '@/services/repository'
import { workflowProgressPresentation } from '@/services/taskProgress'
import { assetPromptContinuationTarget } from '@/services/assetGenerationPayload'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { isWorkflowActive } from '@/services/taskProgress'
import type { AssetGenerationJob, AssetGenerationResolutionOutcome, TaskWorkflowExport, ReferenceEntityCategory, TaskInputPrompt, TaskOutputMedia, TaskReferenceAssetSummary, Workflow, WorkflowNode } from '@/types'

type HealthLevel = 'checking' | 'connected' | 'degraded' | 'unavailable'
type RuntimeAlert = { level: 'info' | 'warning' | 'danger'; title: string; message: string }
type ParameterItem = { key: string; label: string; value: string }
type ReferenceAssetGroup = { key: 'model-input' | 'prompt-context'; title: string; description: string; assets: TaskReferenceAssetSummary[]; contextOnly: boolean }
const props = defineProps<{ taskId?: string | null; assetJobId?: string | null }>()
const emit = defineEmits<{ changed: []; select: [sourceId: string]; busy: [value: boolean]; dirty: [value: boolean] }>()
const studio = useStudioStore(); const auth = useAuthStore(); const router = useRouter()
const selectedId = computed(() => props.taskId || props.assetJobId || '')
const selected = ref<Workflow>(); const selectedAssetJob = ref<AssetGenerationJob>()
const workflowInspectOpen = ref(false), workflowInspectLoading = ref(false), workflowInspectError = ref(''), workflowExport = ref<TaskWorkflowExport|null>(null), workflowExportFormat = ref<'ui'|'api'>('api'), inspectedTaskId = ref('')
const failureExpanded = ref(false), parametersExpanded = ref(false), taskMenuOpen = ref(false), taskMenuRoot = ref<HTMLElement|null>(null)
const promptOpen = ref(false), promptLoading = ref(false), promptError = ref(''), inputPrompt = ref<TaskInputPrompt|null>(null), promptTaskId = ref('')
const referencePreviewOpen = ref(false), referencePreviewAsset = ref<TaskReferenceAssetSummary|null>(null), referenceImageFailures = ref<Record<string,boolean>>({})
const healthChecking = ref(false), health = ref<{ok:boolean;message:string;latency?:number}|null>(null), pollError = ref(''), nowMs = ref(Date.now()), loading = ref(false), actionPending = ref(false)
const outputMediaRecoveryAttempts = new Map<string,number>(), referenceImageRecoveryAttempts = new Map<string,number>()
let ticker = 0, clockTicker = 0, promptRequestSequence = 0, detailRequestSequence = 0, workflowInspectRequestSequence = 0
const productionCapability = ref<ProductionCapability|null>(null); const productionCapabilityError = ref('')
const resolutionTarget=ref<AssetGenerationJob|null>(null);const resolutionPending=ref(false);const resolutionError=ref('')
const resolutionOutcomes:Array<{value:AssetGenerationResolutionOutcome;label:string}>=[
  {value:'confirmed_not_submitted',label:'确认未提交'},
  {value:'confirmed_remote_failed',label:'确认远端失败'},
  {value:'confirmed_remote_cancelled',label:'确认远端取消'},
  {value:'confirmed_remote_succeeded',label:'确认远端成功'},
]
const blankResolutionForm=()=>({outcome:'confirmed_not_submitted' as AssetGenerationResolutionOutcome,note:'',providerRequestId:'',actualCost:'',costCurrency:'',idempotencyKey:crypto.randomUUID()})
const resolutionForm=ref(blankResolutionForm());let resolutionIntentJobId=''
const healthLevel = computed<HealthLevel>(()=>{
  if (!health.value) return 'checking'
  if (health.value.ok) return 'connected'
  return /system_stats|500|internal server|健康检查|health check/i.test(health.value.message) ? 'degraded' : 'unavailable'
})
const healthBadge = computed(()=>healthLevel.value==='connected'?'已连接（CONNECTED）':healthLevel.value==='degraded'?'性能下降（DEGRADED）':healthLevel.value==='unavailable'?'不可用（UNAVAILABLE）':'检查中（CHECKING）')
const isActiveTask = isWorkflowActive
const taskKind = (workflow?: Workflow | null) => workflow?.kind
const taskMediaLabel = (workflow?: Workflow | null) => workflow?.kind ? ({image:'图片', video:'视频', audio:'音频'} as const)[workflow.kind] : '媒体'
const assetJobActive=(job:AssetGenerationJob)=>['preparing','queued','submitting','submitted','running','cancel_requested'].includes(job.status)
const assetMediaLabel=(job:AssetGenerationJob)=>job.jobType==='field'?'字段候选':job.jobType==='prompt'?'提示词候选':({image:'图片',video:'视频',audio:'音频',script:'剧本',text:'字段文本'} as const)[job.mediaType]
const assetJobTitle=(job:AssetGenerationJob)=>job.jobType==='field'?'准备字段任务':job.jobType==='prompt'&&job.mediaType==='text'?job.name||'文本候选任务':job.jobType==='prompt'?`${({image:'图片',video:'视频',audio:'音频',script:'剧本',text:'文本'} as const)[job.mediaType]}提示词候选`:job.name||'未命名素材任务'
const assetJobIcon=(job:AssetGenerationJob)=>job.jobType==='prompt'?WandSparkles:({image:Image,video:Film,audio:Music2,script:FileText,text:FileText} as const)[job.mediaType]
const assetJobStatusLabel=(status:AssetGenerationJob['status'])=>({preparing:'准备输入 / 待准入',queued:'排队中',submitting:'正在提交',submitted:'已提交',running:'运行中',cancel_requested:'取消中',succeeded:'已成功',failed:'已失败',cancelled:'已取消',uncertain:'远端状态待核对'} as const)[status]
const assetJobBadgeStatus=(job:AssetGenerationJob)=>job.status==='succeeded'?'completed':assetJobActive(job)?'running':job.status==='failed'?'failed':job.status
const assetJobExecutionLabel=(job:AssetGenerationJob)=>`${job.executionInstanceName} · v${job.executionVersion} · ${job.operation}`
const assetJobProgress=(job:AssetGenerationJob)=>job.status==='succeeded'?100:null
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
const selectedInputContractReason = computed(() => selectedInputContractUnavailable.value ? '实际执行准备、冻结请求或素材回执缺失或校验失败' : '')
const workflowTaskId = (workflow?: Workflow | null) => workflow?.taskId || workflow?.id || ''
const selectedTaskId = computed(() => workflowTaskId(selected.value))
const promptSummary = computed(() => {
  const workflow = selected.value
  if (workflow && promptTaskId.value === (workflow.taskId || workflow.id) && inputPrompt.value) return `${inputPrompt.value.channels.length} 个冻结通道`
  return '按需读取本次冻结通道'
})
const publicReferenceUrl = (asset?: TaskReferenceAssetSummary | null) => {
  const value = String(asset?.previewUrl || '').trim()
  if (value.startsWith('/') && !value.startsWith('//')) return value
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) ? url.toString() : ''
  } catch { return '' }
}
const referenceUseLabel = (asset: TaskReferenceAssetSummary) => {
  const state = { staged: '已暂存', not_staged: '尚未暂存', not_applicable: '仅作上下文' }[asset.stagingStatus]
  const targets = asset.ports.map(row => row.targetKey).join('、')
  return [state, targets && `槽位：${targets}`, asset.purpose && `用途：${asset.purpose}`, asset.pairedTo && `配对：${asset.pairedTo}`].filter(Boolean).join(' · ')
}
const referenceAssetKey = (asset: TaskReferenceAssetSummary, _index = 0) => asset.itemKey
const referenceMediaLabel = (kind: TaskReferenceAssetSummary['mediaKind']) => kind === 'video' ? '视频' : kind === 'audio' ? '音频' : '图片'
const selectedReferenceAssets = computed(() => selected.value?.referenceAssets || [])
const selectedContextMediaUses = computed(() => selected.value?.contextMediaUses || [])
const referenceAssetGroups = computed<ReferenceAssetGroup[]>(() => [
  ...(selectedReferenceAssets.value.length ? [{
    key: 'model-input' as const,
    title: '模型参考输入',
    description: `${selectedReferenceAssets.value.length} 次素材使用 · 本次工作流槽位已冻结`,
    assets: selectedReferenceAssets.value,
    contextOnly: false
  }] : []),
  ...(selectedContextMediaUses.value.length ? [{
    key: 'prompt-context' as const,
    title: '提示词上下文素材',
    description: `${selectedContextMediaUses.value.length} 次素材使用 · 不占工作流槽位`,
    assets: selectedContextMediaUses.value,
    contextOnly: true
  }] : [])
])
const coreInputParameters = computed<ParameterItem[]>(() => {
  const source = selectedInputParameters.value
  const parameters = Array.isArray(source.parameters) ? source.parameters as Array<Record<string, unknown>> : []
  return [
    { key: 'operation', label: '本次操作', value: String(source.operation ?? '无法读取') },
    { key: 'workflow', label: '精确工作流版本', value: String(source.workflow_version_id ?? '无法读取') },
    ...parameters.map(row => ({ key: String(row.key), label: String(row.key),
      value: `${JSON.stringify(row.value)} · 来源：${String(row.value_source)}` })),
    { key: 'references', label: '工作流素材使用', value: `${selectedReferenceAssets.value.length} 次使用 · ${selectedReferenceAssets.value.reduce((sum, row) => sum + row.ports.length, 0)} 个连接` },
    { key: 'contexts', label: '提示词上下文素材', value: `${selectedContextMediaUses.value.length} 次使用 · 不占工作流槽位` }
  ]
})
const technicalInputParameters = computed(() => {
  const source = selectedInputParameters.value
  const parameters = Array.isArray(source.parameters) ? source.parameters as Array<Record<string, unknown>> : []
  return [
    ...['preparation_id', 'attempt_id', 'execution_instance_version_id', 'request_sha256', 'prompt_mode', 'prompt_profile_version_id']
      .map(key => ({ key, label: key, value: source[key] === null ? '不适用' : String(source[key] ?? '无法读取') })),
    ...parameters.map(row => ({ key: `rule.${String(row.key)}`, label: `${String(row.key)} 的冻结约束`, value: JSON.stringify(row.rule) }))
  ]
})
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
      execution: workflow.execution,
      comfy_prompt_id: workflow.comfyPromptId
    },
    input_parameters: selectedInputParameters.value
  }
})
const episodeContextLabel = (workflow: Workflow) => workflow.episodeNumber ? `第 ${workflow.episodeNumber} 集${workflow.episodeTitle ? ` · ${workflow.episodeTitle}` : ''}` : workflow.episodeTitle || ''
const unitContextLabel = (workflow: Workflow) => workflow.execution?.unitKey || ''
const selectedMedia = computed<TaskOutputMedia[]>(() => selected.value?.outputMedia || [])
const outputPromotionCategoryLabel = (category: ReferenceEntityCategory) => (
  category === 'character' ? '人物' : category === 'scene' ? '场景' : '道具'
)
const promotedAssetPurposeLabel = (_media: TaskOutputMedia) => '用途请在素材库核对'
const mediaKind = (media: TaskOutputMedia) => {
  const filename = media.filename.toLowerCase()
  if (/\.(mp4|webm|mov|m4v|mkv)$/.test(filename)) return 'video'
  if (/\.(png|jpe?g|webp|gif|avif)$/.test(filename)) return 'image'
  if (/\.(mp3|wav|ogg|m4a|aac|flac)$/.test(filename)) return 'audio'
  return ['video', 'image', 'audio'].includes(media.kind) ? media.kind : 'file'
}
const outputCount = (kind: 'video' | 'image' | 'audio') => selectedMedia.value.filter((media) => mediaKind(media) === kind).length
const emptyOutputMessage = computed(() => {
  if (selected.value?.status === 'cancelled') return '任务已取消，当前没有可预览产物；取消不代表生成成功或执行失败。'
  if (selected.value?.status === 'cancel_requested') return '取消请求处理中，尚未确认远端终态；请勿重复提交。'
  if (selected.value?.status === 'completed') return '任务已完成，但 ComfyUI 没有返回可访问的媒体文件。请先刷新任务；若仍为空，请检查 ComfyUI 输出目录。'
  if (selected.value?.status === 'failed') return '任务执行失败，没有可预览的媒体产物。展开失败原因后可从所属 ProductionRun 处理。'
  return `任务完成后，原始${taskMediaLabel(selected.value)}会自动出现在这里${taskKind(selected.value)==='video'?'，无需另行组装':''}。`
})
const inspectedTask = computed(()=>selected.value)
const selectedErrorMessage = computed(()=>selected.value?.status==='failed'?selected.value.errorMessage?.trim()||'':'')
const failureSummary = computed(()=>{
  const message=selectedErrorMessage.value
  if(!message)return '任务执行失败，服务端未返回详细错误。'
  const firstLine=message.split(/\r?\n/).map((line)=>line.trim()).find(Boolean)||message
  return firstLine.length>180?`${firstLine.slice(0,180)}…`:firstLine
})
const hasFailureDetail = computed(()=>Boolean(selectedErrorMessage.value))
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
const selectedDisplayNodes=computed<WorkflowNode[]>(()=>selected.value?.nodes || [])
const nodeStatusCount=(status:WorkflowNode['status'])=>selectedDisplayNodes.value.filter((node)=>node.status===status).length||0
const nodeStatusLabel=(node:WorkflowNode)=>node.status==='cancelled'?'已取消':node.status==='cancel_requested'?'取消中':node.status==='completed'?'已完成':node.status==='running'?'正在执行':node.status==='failed'?'执行失败':'等待执行'
const nodeProgressLabel=(node:WorkflowNode)=>['cancelled','cancel_requested'].includes(node.status)?nodeStatusLabel(node):node.status==='running'&&selectedProgress.value
  ? selectedProgress.value.compactLabel
  : `${node.progress}%`
const nodeProgressWidth=(node:WorkflowNode)=>['cancelled','cancel_requested'].includes(node.status)?'0%':node.status==='running'&&selectedProgress.value?.mode==='comfyui'
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
    workflow.finishedAt && ['completed','failed','cancelled'].includes(workflow.status) ? { event:workflow.status, status:workflow.status, occurredAt:workflow.finishedAt, label:workflow.status==='cancelled'?'任务已取消':workflow.status==='failed'?'任务执行失败':`${taskMediaLabel(workflow)}生成任务已完成`, details:workflow.status==='failed'?failureSummary.value:`实际执行耗时 ${formatDuration(taskDurationMs(workflow))}` } : undefined
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
const taskStateSummary=(workflow:Workflow)=>workflow.inputContractStatus==='unavailable'?'本次执行输入不可展示':workflow.backendStatus==='cancel_requested'?'取消请求处理中':workflow.isStalled?workflow.stallReason||'任务可能已卡住':progressPresentation(workflow).summary
const taskProgressLabel=(workflow:Workflow)=>workflow.backendStatus==='cancel_requested'?'取消中':workflow.isStalled?'需检查':progressPresentation(workflow).compactLabel
const hydrateSelectedTask=async(workflowId=selectedId.value,_quiet=true,_preserveMediaUrls=false)=>{
  const sequence=++detailRequestSequence
  const taskId=props.taskId
  if(!taskId)return
  const epoch=studio.captureSessionEpoch()
  try{const detail=await repository.getWorkflow(taskId);if(sequence===detailRequestSequence&&selectedId.value===workflowId&&epoch===studio.captureSessionEpoch()){
    if(_preserveMediaUrls&&selected.value)detail.outputMedia=detail.outputMedia?.map((media,index)=>{
      const previous=selected.value?.outputMedia?.find((old,oldIndex)=>outputMediaIdentity(old,oldIndex)===outputMediaIdentity(media,index))
      return media.previewUrl&&previous?.previewUrl?{...media,previewUrl:previous.previewUrl}:media
    })
    selected.value=detail;pollError.value='';return detail}}
  catch(error){if(sequence===detailRequestSequence)pollError.value=error instanceof Error?error.message:'读取任务详情失败'}
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
  if(selectedId.value!==workflowId||requestEpoch!==studio.captureSessionEpoch())throw new Error('当前已切换到其他任务或登录身份')
  selected.value=detail
  const merged=detail
  const identity=outputMediaIdentity(media,index)
  return merged.outputMedia?.find((item,itemIndex)=>outputMediaIdentity(item,itemIndex)===identity)
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
const copyPromptText=async(key:string)=>{
  const channel=inputPrompt.value?.channels.find(item=>item.key===key)
  if(!channel?.text)return;await copyPlainText(channel.text,`提示词通道 ${key}`)
}
const referenceImageCandidates=()=>[...selectedReferenceAssets.value,...selectedContextMediaUses.value]
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
const refreshTaskStatus=async()=>{closeTaskMenu();await refresh();emit('changed')}
const loadProductionCapability=async()=>{productionCapabilityError.value='';try{productionCapability.value=await repository.getProductionCapability()}catch(error){productionCapability.value=null;productionCapabilityError.value=error instanceof Error?error.message:'无法读取当前生产队列配置'}}
const preparationTarget=ref<AssetGenerationJob|null>(null),preparationBusy=ref(false),preparationDirty=ref(false)
const closeAssetPreparation=()=>{
  if(preparationBusy.value)return
  if(preparationDirty.value&&!window.confirm('关闭会丢弃尚未保存的字段输入，是否继续？'))return
  preparationTarget.value=null;preparationDirty.value=false
}
const openAssetPreparation=(job:AssetGenerationJob)=>{preparationTarget.value=job;preparationDirty.value=false}
const refreshPreparedJobs=async()=>{await refresh();emit('changed')}
const continueFromPrompt=(job:AssetGenerationJob)=>{const target=assetPromptContinuationTarget(job);if(target)void router.push(target)}
const openAssetJobOutput=(job:AssetGenerationJob)=>{if(!job.outputAsset)return;void router.push({name:'assets',query:{type:job.mediaType,asset:job.outputAsset.id}})}
const retryAssetJob=async(job:AssetGenerationJob)=>{if(actionPending.value||job.retries.length||!['failed','cancelled'].includes(job.status)||!window.confirm('使用相同输入准备一次重试？可能产生新的模型费用；原失败记录会保留。'))return;actionPending.value=true;try{const retried=await repository.retryAssetGenerationJob(job.id);emit('select',retried.id);emit('changed')}catch(error){pollError.value=apiErrorSummary(error,'重试失败；请重新读取原任务确认是否已有后继，不要更换幂等键')}finally{actionPending.value=false}}
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
const submitUncertainResolution=async()=>{if(resolutionPending.value||!resolutionTarget.value)return;if(!auth.user?.isStaff){resolutionError.value='只有平台管理员可以提交消歧结论';return}const validation=validateUncertainResolution();if(validation){resolutionError.value=validation;return}const jobId=resolutionTarget.value.id;const form=resolutionForm.value;resolutionPending.value=true;resolutionError.value='';try{await repository.resolveUncertainAssetGenerationJob(jobId,{outcome:form.outcome,note:form.note.trim(),providerRequestId:form.providerRequestId.trim(),actualCost:form.actualCost.trim()||null,costCurrency:form.costCurrency.trim().toUpperCase(),idempotencyKey:form.idempotencyKey});resolutionTarget.value=null;resolutionIntentJobId='';resolutionForm.value=blankResolutionForm();await refresh();emit('changed')}catch(error){resolutionError.value=error instanceof Error?error.message:'素材生成任务消歧失败'}finally{resolutionPending.value=false}}
const refreshPending=ref(false),assetPreviewError=ref('')
let recoveredAssetId=''
const inspectorLocked=computed(()=>loading.value||preparationBusy.value||preparationDirty.value||actionPending.value||resolutionPending.value)
const refresh=async(_preserveMediaUrls=false)=>{
  if(inspectorLocked.value){if(!_preserveMediaUrls)refreshPending.value=true;return}
  const identity=selectedId.value,epoch=studio.captureSessionEpoch();loading.value=true
  try{if(props.taskId)await hydrateSelectedTask(identity,true,_preserveMediaUrls);else if(props.assetJobId){const job=await repository.getAssetGenerationJob(props.assetJobId);if(identity===selectedId.value&&epoch===studio.captureSessionEpoch()){
    if(_preserveMediaUrls&&job.outputAsset?.mediaUrl&&selectedAssetJob.value?.outputAsset?.id===job.outputAsset.id&&selectedAssetJob.value.outputAsset.mediaUrl)job.outputAsset.mediaUrl=selectedAssetJob.value.outputAsset.mediaUrl
    selectedAssetJob.value=job;pollError.value=''}}}
  catch(error){if(identity===selectedId.value)pollError.value=error instanceof Error?error.message:'任务详情读取失败'}
  finally{loading.value=false}
}
watch(inspectorLocked,locked=>{if(!locked&&refreshPending.value){refreshPending.value=false;void refresh()}})
const recoverAssetPreview=async(payload:{assetId:string;source:string})=>{
  if(payload.assetId!==selectedAssetJob.value?.outputAsset?.id)return
  if(recoveredAssetId===payload.assetId){assetPreviewError.value='预览仍不可读取，请手动刷新此任务后重试。';return}
  recoveredAssetId=payload.assetId;assetPreviewError.value='正在重新获取预览地址…'
  await refresh()
  if(payload.assetId===selectedAssetJob.value?.outputAsset?.id&&selectedAssetJob.value.outputAsset.mediaUrl===payload.source)assetPreviewError.value='未获得新的预览地址，请稍后手动刷新。'
}
const loadWorkflowExport=async(task:Workflow,format:'ui'|'api')=>{const requestSequence=++workflowInspectRequestSequence;workflowInspectLoading.value=true;workflowInspectError.value='';workflowExport.value=null;try{const result=await repository.getTaskWorkflowExport(task,format);if(requestSequence===workflowInspectRequestSequence)workflowExport.value=result}catch(error){if(requestSequence===workflowInspectRequestSequence)workflowInspectError.value=error instanceof Error?error.message:'读取工作流失败'}finally{if(requestSequence===workflowInspectRequestSequence)workflowInspectLoading.value=false}}
const inspectWorkflow=async(task:Workflow)=>{inspectedTaskId.value=task.id;workflowInspectOpen.value=true;workflowExportFormat.value='api';await loadWorkflowExport(task,'api')}
const switchWorkflowFormat=async(format:'ui'|'api')=>{if(format===workflowExportFormat.value&&workflowExport.value)return;workflowExportFormat.value=format;const task=inspectedTask.value;if(task)await loadWorkflowExport(task,format)}
const copyWorkflow=async()=>{if(!exportedWorkflowJson.value)return;try{await navigator.clipboard.writeText(exportedWorkflowJson.value);studio.notify('实际 ComfyUI 工作流 JSON 已复制')}catch{studio.notify('复制失败，请使用下载 JSON','error')}}
const downloadWorkflow=()=>{if(!workflowExport.value||!exportedWorkflowJson.value)return;const blob=new Blob([exportedWorkflowJson.value],{type:'application/json;charset=utf-8'});const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download=workflowExport.value.filename;anchor.click();window.setTimeout(()=>URL.revokeObjectURL(url),0);studio.notify(`${workflowFormatLabel.value} 已下载`)}
const openInComfy=()=>{if(!safeComfyUrl.value){studio.notify(workflowExport.value?.openReason||'当前没有可安全打开的 ComfyUI 地址','error');return}const opened=window.open(safeComfyUrl.value,'_blank','noopener,noreferrer');if(opened)opened.opener=null}
const checkHealth=async()=>{if(healthChecking.value)return;healthChecking.value=true;try{health.value=await repository.testComfy('')}catch(error){health.value={ok:false,message:error instanceof Error?error.message:'ComfyUI 不可达'}}finally{healthChecking.value=false}}
watch(()=>[props.taskId,props.assetJobId],()=>{
  ++detailRequestSequence;selected.value=undefined;selectedAssetJob.value=undefined;pollError.value='';loading.value=false;recoveredAssetId='';assetPreviewError.value='';refreshPending.value=false
  void refresh();if(props.assetJobId)void loadProductionCapability()
},{immediate:true})
watch([preparationBusy,resolutionPending,actionPending],values=>emit('busy',values.some(Boolean)),{flush:'sync'})
watch(preparationDirty,value=>emit('dirty',value),{flush:'sync'})
onMounted(()=>{document.addEventListener('pointerdown',onTaskMenuPointerDown);document.addEventListener('keydown',onTaskMenuKeydown);ticker=window.setInterval(()=>{if(!document.hidden)void refresh(true)},5000);clockTicker=window.setInterval(()=>{nowMs.value=Date.now()},1000)})
onBeforeUnmount(()=>{promptRequestSequence++;detailRequestSequence++;workflowInspectRequestSequence++;document.removeEventListener('pointerdown',onTaskMenuPointerDown);document.removeEventListener('keydown',onTaskMenuKeydown);window.clearInterval(ticker);window.clearInterval(clockTicker)})
</script>

<template>
  <div class="workflow-page page-stack">
    <p v-if="pollError" class="form-error" role="alert">{{ pollError }}；已显示的内容不是新读取结果。<button class="button ghost small" @click="refresh()">重新读取</button></p>
    <p v-if="loading&&!selected&&!selectedAssetJob" role="status">正在读取此任务的媒体诊断…</p>
    <section class="media-inspector-content">
      <main v-if="selectedAssetJob" class="workflow-detail asset-workflow-detail">
        <section class="panel workflow-overview asset-workflow-overview">
          <header><div><span class="eyebrow">素材工作流（ASSET WORKFLOW）{{ selectedAssetJob.id.toUpperCase() }}</span><h2>{{ assetJobTitle(selectedAssetJob) }}</h2><div><StatusBadge :status="assetJobBadgeStatus(selectedAssetJob)" :label="assetJobStatusLabel(selectedAssetJob.status)"/><span class="workflow-kind-pill asset">素材任务 · {{ assetMediaLabel(selectedAssetJob) }}</span><span><Clock3 :size="13"/>创建于 {{ formatTaskDateTime(selectedAssetJob.createdAt,true) }}</span><span v-if="selectedAssetJob.finishedAt">完成于 {{ formatTaskDateTime(selectedAssetJob.finishedAt,true) }}</span></div></div>
<div class="workflow-main-actions asset-workflow-actions"><button v-if="assetPromptContinuationTarget(selectedAssetJob)" class="button primary" @click="continueFromPrompt(selectedAssetJob)"><WandSparkles :size="15"/>查看候选并继续</button><button v-if="selectedAssetJob.outputAsset" class="button primary" @click="openAssetJobOutput(selectedAssetJob)"><Eye :size="15"/>查看素材</button><button v-if="selectedAssetJob.outputAsset" class="button ghost" @click="downloadAssetJobOutput(selectedAssetJob)"><Download :size="15"/>下载</button><button v-if="selectedAssetJob.status==='uncertain'&&auth.user?.isStaff" class="button primary" @click="openUncertainResolution(selectedAssetJob)">人工消歧</button><button v-if="!selectedAssetJob.retries.length&&['failed','cancelled'].includes(selectedAssetJob.status)" class="button ghost" :disabled="actionPending" @click="retryAssetJob(selectedAssetJob)"><RefreshCw :size="15"/>准备重试</button><button v-for="retry in selectedAssetJob.retries" :key="retry.id" class="button ghost" :disabled="actionPending" @click="emit('select',retry.id)">查看已有重试 · {{assetJobStatusLabel(retry.status)}}</button><button v-if="selectedAssetJob.retryOf" class="button ghost" :disabled="actionPending" @click="emit('select',selectedAssetJob.retryOf)">查看原任务</button></div>
          </header>
          <div v-if="selectedAssetJob.status==='uncertain'" class="task-runtime-alert warning"><AlertTriangle :size="16"/><div><strong>远端状态待核对</strong><p>{{ auth.user?.isStaff?'请在服务提供方控制台核对请求、终态与账单后提交人工消歧。':'请联系平台管理员核对远端请求、终态与账单；不要直接重试。' }}</p></div></div>
          <div v-else-if="selectedAssetJob.errorMessage" class="task-runtime-alert danger"><AlertTriangle :size="16"/><div><strong>{{ selectedAssetJob.errorCode || '素材工作流执行失败' }}</strong><p>{{ selectedAssetJob.errorMessage }}</p></div></div>
          <div class="big-progress" :class="{indeterminate:assetJobActive(selectedAssetJob)&&!['cancel_requested'].includes(selectedAssetJob.status)}"><div><span>{{ assetJobExecutionLabel(selectedAssetJob) }}</span><strong>{{ selectedAssetJob.status==='succeeded'?'已完成':'按实际阶段显示' }}</strong></div><i><b :style="{width:`${assetJobProgress(selectedAssetJob) ?? (assetJobActive(selectedAssetJob)?8:0)}%`}"></b></i><p>{{ assetJobStatusLabel(selectedAssetJob.status) }}<template v-if="selectedAssetJob.remoteStatus"> · 远端状态 {{ selectedAssetJob.remoteStatus }}</template></p></div>
        </section>

        <section class="asset-workflow-grid">
          <article class="panel asset-workflow-facts"><header><span class="eyebrow">冻结执行合同 / FROZEN ROUTE</span><h3>执行实例与底层资源</h3></header><dl><div><dt>执行实例</dt><dd>{{ selectedAssetJob.executionInstanceName }} · v{{ selectedAssetJob.executionVersion }}</dd></div><div><dt>执行入口</dt><dd>{{ selectedAssetJob.executionKind==='comfy_workflow'?'ComfyUI 工作流':'API 运行资源' }}</dd></div><div><dt>模型部署</dt><dd>{{ selectedAssetJob.modelDeploymentName || '—' }}</dd></div><div><dt>工作流版本</dt><dd>{{ selectedAssetJob.workflowName ? `${selectedAssetJob.workflowName} v${selectedAssetJob.workflowVersion || '—'}` : '不适用' }}</dd></div><div><dt>操作能力</dt><dd><code>{{ selectedAssetJob.operation }}</code></dd></div><div><dt>外部调用</dt><dd>{{ selectedAssetJob.isExternal ? (selectedAssetJob.allowExternalCalls?'已授权':'未授权') : '本地/受管' }}</dd></div><div><dt>估算 / 实际费用</dt><dd>{{ selectedAssetJob.estimatedCost ?? '未知' }} / {{ selectedAssetJob.actualCost ?? '未知' }} {{ selectedAssetJob.costCurrency }}</dd></div><div><dt>轮询次数</dt><dd>{{ selectedAssetJob.pollCount }}</dd></div><div><dt>任务 ID</dt><dd><code>{{ selectedAssetJob.id }}</code></dd></div></dl></article>
          <article class="panel asset-workflow-prompt"><header><span class="eyebrow">执行准备 / PREPARATION</span><h3>输入、参数与提示词</h3></header><p>统一读取本任务的准备记录。未冻结请求不等于已提交，候选字段不等于已经采用。</p><p>{{ selectedAssetJob.preparation.status }} · 修订 {{ selectedAssetJob.preparation.revision }}</p><p v-if="selectedAssetJob.preparation.reason">{{ selectedAssetJob.preparation.reason }}</p><button type="button" class="button ghost" @click="openAssetPreparation(selectedAssetJob)">查看准备 / 复核字段</button><template v-if="selectedAssetJob.resultText"><label>生成结果</label><pre class="result">{{ selectedAssetJob.resultText }}</pre></template></article>
        </section>
        <section v-if="selectedAssetJob.outputAsset" class="panel asset-workflow-output"><header><div><span class="eyebrow">素材产物 / LIBRARY OUTPUT</span><h3>{{ selectedAssetJob.outputAsset.name }}</h3><p>产物已进入风格素材库，可下载并在短剧制作中正式引用。</p></div><div><button class="button ghost small" @click="downloadAssetJobOutput(selectedAssetJob)"><Download :size="14"/>下载</button><button class="button primary small" @click="openAssetJobOutput(selectedAssetJob)"><ArrowRight :size="14"/>在素材库查看</button></div></header><div class="asset-workflow-preview"><AssetMediaPreview :asset="selectedAssetJob.outputAsset" controls @source-error="recoverAssetPreview" @source-loaded="assetPreviewError=''"/><p v-if="assetPreviewError" role="status">{{assetPreviewError}}</p></div></section>
      </main>

      <main v-else-if="selected" class="workflow-detail">
        <section class="panel workflow-overview">
          <header>
            <div><span class="eyebrow">工作流（Workflow）{{ selected.id.toUpperCase() }}</span><h2>{{ selected.projectTitle }}</h2><div><StatusBadge :status="selected.status"/><span class="workflow-kind-pill story">{{ selected.execution?.productionRunId?'生产任务':'执行关联不可用' }}</span><span>{{ selected.currentStep }}</span><span><Clock3 :size="13"/>{{ taskTimeKind(selected) }}于 {{ formatTaskDateTime(taskStartAt(selected)) }}</span><span>{{ taskDurationLabel(selected) }}</span><span v-if="selected.finishedAt">完成于 {{ formatTaskDateTime(selected.finishedAt) }}</span><span v-if="selected.execution">实际调用 #{{ selected.execution.attemptNumber }}</span><span>队列领取 {{ selected.attempts ?? 0 }} 次</span></div></div>
            <div class="workflow-main-actions">
              <button class="button ghost" title="查看本次执行准备的冻结图，不上传素材或提交任务" @click="inspectWorkflow(selected)"><Eye :size="15"/>查看本次工作流</button>
              <RouterLink v-if="selected.execution" class="button ghost" :to="`/projects/${selected.projectId}/production/${selected.execution.productionRunId}`"><Clapperboard :size="15"/>打开生产工作台</RouterLink>
              <RouterLink v-if="selected.projectId" class="button primary" :to="`/projects/${selected.projectId}/production`"><WorkflowIcon :size="15"/>在 ProductionRun 中处理</RouterLink>
              <div ref="taskMenuRoot" class="task-more-menu">
                <button type="button" class="icon-btn" aria-label="打开任务更多操作" aria-haspopup="menu" aria-controls="task-more-actions" :aria-expanded="taskMenuOpen" title="任务更多操作" @click.stop="taskMenuOpen=!taskMenuOpen"><MoreHorizontal :size="17"/></button>
                <div v-if="taskMenuOpen" id="task-more-actions" class="task-more-menu-popover" role="menu" aria-label="任务更多操作">
                  <button type="button" role="menuitem" @click="copyTaskId"><Clipboard :size="14"/><span><strong>复制任务 ID</strong><small>{{ selected.taskId || selected.id }}</small></span></button>
                  <button v-if="selected.comfyPromptId" type="button" role="menuitem" @click="copyComfyPromptId"><Clipboard :size="14"/><span><strong>复制 Comfy Prompt ID</strong><small>{{ selected.comfyPromptId }}</small></span></button>
                  <button type="button" role="menuitem" @click="openInputPrompt()"><FileText :size="14"/><span><strong>查看本次请求提示词</strong><small>冻结通道、策略来源与参考标签</small></span></button>
                  <button v-if="!selectedInputContractUnavailable" type="button" role="menuitem" @click="copyInputParameters"><Code2 :size="14"/><span><strong>复制输入参数 JSON</strong><small>已自动排除绝对本地路径</small></span></button>
                  <button v-if="!selectedInputContractUnavailable" type="button" role="menuitem" @click="downloadTaskConfig"><Download :size="14"/><span><strong>下载任务配置 JSON</strong><small>任务标识、上下文与安全输入参数</small></span></button>
                  <button type="button" role="menuitem" @click="refreshTaskStatus"><RefreshCw :size="14"/><span><strong>刷新任务状态</strong><small>立即重新读取服务端任务与产物</small></span></button>
                </div>
              </div>
            </div>
          </header>
          <div v-if="selected.episodeId||selected.execution" class="task-context-summary"><span v-if="selected.episodeId"><small>剧集</small><strong>{{ episodeContextLabel(selected) }}</strong></span><span v-if="selected.execution"><small>生产单元</small><strong>{{ selected.execution.unitKey }}</strong></span><span v-if="selected.execution"><small>实际执行准备</small><code>{{ selected.execution.preparationId }}</code></span></div>
          <div v-if="selectedRuntimeAlert" class="task-runtime-alert" :class="selectedRuntimeAlert.level" role="status"><AlertTriangle :size="16"/><div><strong>{{ selectedRuntimeAlert.title }}</strong><p>{{ selectedRuntimeAlert.message }}</p></div><button type="button" :disabled="healthChecking" @click="checkHealth()"><RefreshCw :size="12"/>{{ healthChecking?'检测中':'重新检测' }}</button></div>
          <div v-if="selectedProgress" class="big-progress" :class="{indeterminate:selectedProgress.indeterminate}"><div><span>{{ selectedProgress.heading }}</span><strong>{{ selectedProgress.valueLabel }}</strong></div><i><b :style="{width:selectedProgress.indeterminate?'8%':`${selectedProgress.percent || 0}%`}"></b></i><p v-if="selectedProgress.note">{{ selectedProgress.note }}</p></div>
        </section>

        <TaskExecutionChecks :task-id="selected.taskId || selected.id"/>

        <section class="panel task-input-parameters">
          <header><div><span class="eyebrow">输入参数 / INPUT PARAMETERS</span><h3>输入参数</h3><p>展示本次准备冻结的参数与逐次素材使用，不重抽种子、不重新组装。已冻结不代表远端已接受；暂存状态单独标明。</p></div><button v-if="technicalInputParameters.length" type="button" :aria-expanded="parametersExpanded" aria-controls="task-technical-parameters" @click="parametersExpanded=!parametersExpanded"><span>{{ parametersExpanded?'收起技术参数':'查看更多参数' }}</span><b>{{ technicalInputParameters.length }}</b><ChevronDown :size="13" :class="{expanded:parametersExpanded}"/></button></header>
          <div v-if="selectedInputContractUnavailable" class="task-runtime-alert warning" role="status"><AlertTriangle :size="16"/><div><strong>本次执行输入不可展示</strong><p>{{ selectedInputContractReason }}。任务状态、时间和归属仍按数据库事实展示；系统不会回填或推断输入参数。原因码 / Code：<code>{{ selected.inputContractCode }}</code></p></div></div>
          <template v-else>
          <div class="task-input-core"><article v-for="item in coreInputParameters" :key="item.key"><small>{{ item.label }}</small><strong :title="item.value">{{ item.value }}</strong></article><article class="task-prompt-parameter"><small>本次请求提示词</small><button type="button" title="按需读取执行准备；不依据旧任务摘要推断内容" @click="openInputPrompt()"><FileText :size="13"/><span>{{ promptSummary }}</span><ArrowRight :size="12"/></button></article></div>
          <div v-for="group in referenceAssetGroups" :key="group.key" class="task-reference-inputs" :class="{continuity:group.contextOnly}"><header><div><Layers3 :size="15"/><span><strong>{{ group.title }}</strong><small>{{ group.description }}</small></span></div><b v-if="group.contextOnly">未接入模型</b></header><div class="task-reference-gallery"><article v-for="(asset,index) in group.assets" :key="referenceAssetKey(asset,index)">
            <button v-if="asset.mediaKind==='image'&&publicReferenceUrl(asset)&&!referenceImageFailures[referenceAssetKey(asset,index)]" type="button" class="reference-image-thumb" :aria-label="`打开大图预览：${asset.displayName}`" @click="openReferenceImage(asset,index)"><img :src="publicReferenceUrl(asset)" :alt="asset.displayName" loading="lazy" @load="handleReferenceImageLoaded(asset,index)" @error="recoverReferenceImage(asset,index)"/><span><Eye :size="13"/>查看大图</span></button>
            <div v-else-if="asset.mediaKind==='video'&&publicReferenceUrl(asset)" class="reference-media-control"><video :src="publicReferenceUrl(asset)" controls preload="metadata">当前浏览器无法播放该参考视频。</video></div>
            <div v-else-if="asset.mediaKind==='audio'&&publicReferenceUrl(asset)" class="reference-media-control audio"><AudioLines :size="22"/><audio :src="publicReferenceUrl(asset)" controls preload="metadata">当前浏览器无法播放该参考音频。</audio></div>
            <div v-else class="reference-media-fallback"><Image v-if="asset.mediaKind==='image'" :size="23"/><Film v-else-if="asset.mediaKind==='video'" :size="23"/><AudioLines v-else :size="23"/><span>{{ publicReferenceUrl(asset)?'预览加载失败':'未提供预览地址' }}</span></div>
            <footer><div><strong :title="asset.displayName">{{ asset.displayName }}</strong><span>{{ referenceMediaLabel(asset.mediaKind) }}<template v-if="!group.contextOnly&&asset.inputLabel"> · {{ asset.inputLabel }}</template><template v-else-if="group.contextOnly&&(asset.purpose)"> · {{ asset.purpose }}</template></span><span v-if="referenceUseLabel(asset)" class="reference-timing" :title="referenceUseLabel(asset)">{{ referenceUseLabel(asset) }}</span></div><a v-if="asset.mediaKind!=='image'&&publicReferenceUrl(asset)" :href="publicReferenceUrl(asset)" target="_blank" rel="noopener noreferrer">打开<ExternalLink :size="11"/></a></footer>
            <FrozenReferenceCorrespondence :binding="asset.referenceBinding"/>
          </article></div></div>
          <div v-if="parametersExpanded" id="task-technical-parameters" class="task-input-technical"><div v-for="item in technicalInputParameters" :key="item.key"><code>{{ item.label }}</code><span :title="item.value">{{ item.value }}</span></div></div>
          </template>
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
              <span v-if="nodeStatusCount('cancel_requested')"><i></i><b>{{ nodeStatusCount('cancel_requested') }}</b>取消中</span>
              <span v-if="nodeStatusCount('cancelled')"><i></i><b>{{ nodeStatusCount('cancelled') }}</b>已取消</span>
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
                    <span v-else-if="media.distributionStatus==='failed'" class="distribution-failed" :title="media.distributionError">公开副本失败：{{ media.distributionError }}</span>
                    <span v-else-if="mediaKind(media)==='video'||mediaKind(media)==='image'" class="distribution-pending">母版仅供预览，公开下载前需生成带标识副本</span>
                    <span v-if="media.promotedAssetId" class="distribution-ready">已保存到素材库 · {{ promotedAssetPurposeLabel(media) }}</span>
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
            <div v-else class="task-output-empty" role="status"><component :is="taskKind(selected)==='image'?Image:taskKind(selected)==='audio'?AudioLines:Film" :size="23"/><div><strong>暂无可预览产物</strong><p>{{ emptyOutputMessage }}</p></div><button v-if="selected.status==='completed'" class="button ghost small" @click="refresh(false)"><RefreshCw :size="13"/>刷新产物</button></div>
            <p class="task-output-note">此处展示 ComfyUI 返回的单任务原始文件。开发模式可直接下载母版用于调试；生产模式只提供带标识且验证通过的发行副本。来源清单只公开哈希和模型/任务标识，不泄露完整提示词或本地路径。</p>
          </div>
        </section>
      </main>
      <div v-else-if="!loading&&!pollError" class="empty-state">此任务没有可读取的媒体详情。</div>
    </section>

    <BaseModal :open="workflowInspectOpen" title="ComfyUI 工作流详情" eyebrow="只读工作流查看器 / WORKFLOW INSPECTOR" width="900px" @close="workflowInspectOpen=false">
      <div class="workflow-inspector">
        <div class="workflow-format-picker" role="tablist" aria-label="工作流导出格式">
          <div><strong>选择导出格式</strong><p>API 图读取本次冻结请求与暂存回执；UI 画布还需读取该任务目标的节点目录。均不会提交生成任务。</p></div>
          <div class="workflow-format-tabs">
            <button type="button" role="tab" :aria-selected="workflowExportFormat==='ui'" :class="{active:workflowExportFormat==='ui'}" :disabled="workflowInspectLoading" @click="switchWorkflowFormat('ui')">UI 画布（推荐）</button>
            <button type="button" role="tab" :aria-selected="workflowExportFormat==='api'" :class="{active:workflowExportFormat==='api'}" :disabled="workflowInspectLoading" @click="switchWorkflowFormat('api')">API Prompt 图</button>
          </div>
        </div>
        <div v-if="inspectedTask" class="task-identity-grid">
          <div><span>当前任务 ID</span><code>{{ inspectedTask.taskId || inspectedTask.id }}</code></div>
          <div v-if="inspectedTask.episodeId"><span>所属剧集</span><strong>{{ episodeContextLabel(inspectedTask) }}</strong></div>
          <div v-if="inspectedTask.execution"><span>生产单元 / 实际调用</span><strong>{{ inspectedTask.execution.unitKey }} · #{{ inspectedTask.execution.attemptNumber }}</strong></div>
          <div v-if="inspectedTask.execution"><span>执行准备</span><code>{{ inspectedTask.execution.preparationId }}</code></div>
          <div><span>Comfy prompt ID</span><code>{{ inspectedTask.comfyPromptId || '尚未提交至 ComfyUI' }}</code></div>
          <div><span>状态 / 进度</span><strong>{{ inspectedTask.backendStatus || inspectedTask.status }} · {{ progressPresentation(inspectedTask).compactLabel }}</strong></div>
          <div><span>提交状态</span><strong>{{ inspectedTask.submissionState || 'unprepared' }}</strong></div>
          <div><span>队列领取次数（非生成次数）</span><strong>{{ inspectedTask.attempts ?? '未记录' }}</strong></div>
        </div>
        <div v-if="workflowInspectLoading" class="workflow-inspector-loading"><LoaderCircle :size="24"/><span>正在读取本次冻结图与素材暂存回执…</span></div>
        <div v-else-if="workflowInspectError" class="workflow-inspector-error"><AlertTriangle :size="18"/><div><strong>无法读取工作流</strong><p>{{ workflowInspectError }}</p></div></div>
        <template v-else-if="workflowExport">
          <div class="workflow-import-notice" :class="{safe:isCanvasWorkflow&&workflowExport.importReady}"><FileJson :size="19"/><div><strong>{{ isCanvasWorkflow?'可导入的 ComfyUI UI 画布':'API Prompt 图（不能直接拖入画布）' }}</strong><p>{{ workflowExport.openReason }}</p></div><span>{{ workflowFormatLabel }}</span></div>
          <ol v-if="isCanvasWorkflow&&workflowExport.importReady" class="workflow-import-steps"><li>下载 UI 画布 JSON 后可在 ComfyUI 中手动导入查看。</li><li>核对工作流版本、参考素材和输出契约；本诊断页不会写入 ComfyUI 工作流库或提交任务。</li></ol>
          <div v-if="workflowExport.importReady===false" class="workflow-placeholder-warning"><AlertTriangle :size="15"/>后端未能确认 {{ isCanvasWorkflow?'UI 画布':'API Prompt 图' }} 已完整就绪；建议仅审计 JSON，不要直接执行。</div>
          <div v-if="workflowExport.containsStagedPlaceholders" class="workflow-placeholder-warning"><AlertTriangle :size="15"/>尚缺 {{ workflowExport.missingReceiptCount }} 份素材使用回执；当前展示的是带占位符的冻结准备图，不是完整暂存图。此页面不会代替任务上传素材。</div>
          <div class="workflow-export-meta"><span>执行准备<code>{{ workflowExport.preparationId }}</code></span><span>实际调用<code>{{ workflowExport.attemptId }}</code></span><span>执行实例版本<code>{{ workflowExport.executionInstanceVersionId }}</code></span><span>工作流版本<code>{{ workflowExport.workflowVersionId }}</code></span><span>冻结请求摘要<code>{{ workflowExport.requestSha256 }}</code></span><span>当前导出文件摘要<code>{{ workflowExport.contentHash }}</code></span><span>文件<code>{{ workflowExport.filename }}</code></span></div>
          <div class="workflow-json-preview"><pre>{{ exportedWorkflowJson }}</pre></div>
        </template>
      </div>
      <template #footer><button class="button ghost" @click="workflowInspectOpen=false">关闭</button><button class="button ghost" :disabled="!workflowExport" @click="copyWorkflow"><Clipboard :size="15"/>复制当前 JSON</button><button class="button ghost" :disabled="!workflowExport" @click="downloadWorkflow"><Download :size="15"/>下载{{ workflowFormatLabel }}</button><button class="button primary" :disabled="!safeComfyUrl" :title="workflowExport?.openReason" @click="openInComfy"><ExternalLink :size="15"/>打开 ComfyUI</button></template>
    </BaseModal>

    <BaseModal :open="promptOpen" title="本次请求提示词" eyebrow="冻结提示词通道 / FROZEN REQUEST CHANNELS" width="900px" @close="promptOpen=false">
      <div class="task-prompt-inspector">
        <div v-if="promptLoading" class="task-prompt-loading"><LoaderCircle :size="24"/><span>正在读取本次执行准备中的提示词…</span></div>
        <div v-else-if="promptError" class="task-prompt-error"><AlertTriangle :size="19"/><div><strong>无法读取请求提示词</strong><p>{{ promptError }}</p></div><button class="button ghost small" @click="openInputPrompt(true)"><RefreshCw :size="13"/>重试</button></div>
        <template v-else-if="inputPrompt">
          <p>这里展示本次执行准备中已冻结的内容，不重新组装、不改写。请求已冻结不代表远端已经接受或执行成功。</p>
          <div class="task-prompt-meta">
            <div><small>来源方式</small><strong>{{ {manual:'人工提交',profile:'策略组装',none:'无需提示词'}[inputPrompt.promptMode] }}</strong></div>
            <div><small>执行准备</small><code>{{ inputPrompt.preparationId }}</code></div>
            <div><small>实际调用</small><code>{{ inputPrompt.attemptId }}</code></div>
            <div><small>请求摘要</small><code>{{ inputPrompt.requestSha256 }}</code></div>
            <div v-if="inputPrompt.promptProfileVersionId"><small>提示词策略版本</small><code>{{ inputPrompt.promptProfileVersionId }}</code></div>
          </div>
          <p v-if="inputPrompt.declaredReferenceLabels.length">本次参考标签：{{ inputPrompt.declaredReferenceLabels.join(' · ') }}</p>
          <p v-if="!inputPrompt.channels.length">{{ inputPrompt.promptMode==='none'?'此实例明确声明无需提示词。':'本次请求未提供提示词通道。' }}</p>
          <div class="task-prompt-texts">
            <article v-for="channel in inputPrompt.channels" :key="channel.key"><header><div><span>{{ channel.key }}</span><small>{{ channel.characterCount }} 字符 · 对应工作流输入 {{ channel.semanticKey }}</small></div><button type="button" :disabled="!channel.text" @click="copyPromptText(channel.key)"><Clipboard :size="13"/>复制此通道</button></header><pre>{{ channel.text === '' ? '空字符串（此通道明确为空）' : channel.text }}</pre></article>
          </div>
        </template>
      </div>
      <template #footer><button class="button ghost" @click="promptOpen=false">关闭</button></template>
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
    <BaseModal :open="!!preparationTarget" :dismissible="!preparationBusy" title="素材任务准备工作台" width="1120px" @close="closeAssetPreparation"><ExecutionPreparationWorkbench v-if="preparationTarget" :key="preparationTarget.preparation.id" :preparation-id="preparationTarget.preparation.id" :version-id="preparationTarget.executionInstanceVersionId" purpose="production" @busy="preparationBusy=$event" @dirty="preparationDirty=$event" @changed="refreshPreparedJobs"/></BaseModal>
</template>

<style scoped>
.media-inspector-content{display:block;min-width:0}.workflow-page{min-width:0}

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
</style>
