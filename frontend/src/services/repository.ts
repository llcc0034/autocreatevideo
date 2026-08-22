import { api, fetchAllPages, unwrap } from './api'
import { mockAssets, mockProjects, mockSettings, mockWorkflows, stylePresets } from '@/data/mock'
import { DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, normalizeImageProductionResolutionMultiple, normalizeProductionResolutionMultiple } from '@/constants/productionResolution'
import type { AssemblyResult, Asset, BibleAssetDraftResult, BibleDraftEntityType, BibleDraftLanguage, BibleImageCandidateCreateOptions, BibleImageCandidateCreateResult, BibleImageCandidatesResult, CanonicalGlossaryEnsureResult, CanonicalGlossaryScope, ComfySettings, ComfyUIWorkflowSyncResult, DeferredGenerationCreateInput, DeferredGenerationRequest, DeferredGenerationStatus, Episode, GeneratedImageAssetTarget, GeneratedWorkflowExport, H3Mode, H3PromptOptimizerCapability, H3PromptPayload, ProductionResolutionProfile, ProductionWorkflowSelection, ProductionWorkflowTemplate, PromptTranslationEntry, PromptTranslationField, ReleaseBundle, ReleaseSelection, ScriptBibleApplyResult, ScriptBibleCandidatesResult, ScriptBibleReviewedSelection, ScriptBreakdownOptions, SeriesProject, Shot, ShotTranslationPromotionResult, StylePreset, TaskInputPrompt, TranslationCapability, VisualRulesColorDesign, VisualRulesDraftApplyResult, VisualRulesDraftResult, Workflow, WorkflowUserParameterDefinition } from '@/types'
import type { PromptProviderId } from '@/types'
import { adaptAssets, adaptCreatedEpisode, adaptProject, adaptProjectSummary, adaptPromptArtifact, adaptStyles, adaptTaskBatchSlots, adaptTasks, backendStyleId, defaultBibleImagePromptConfig, isBackendId, projectPayload, referenceAssetIdsForShot, referenceBindingsPayload, syncBible, syncEpisode, syncShot } from './backendAdapters'
import { buildGeneratedImageAssetPromotionPayload } from './outputAssetPromotion'
import { adaptCanonicalGlossaryEnsureResult, adaptShotTranslationPromotionResult, adaptTranslationEntry } from './translationReview'
import { adaptVisualRulesDraftApplyResult, adaptVisualRulesDraftResult, visualRulesDraftApplyPayload, visualRulesDraftGeneratePayload } from './visualRulesDraft'
import { adaptPromptProviders } from './providerCapabilities'
import { parseDialogueText } from './dialogue'
import { adaptScriptBibleApplyResult, adaptScriptBibleCandidates, scriptBibleApplyPayload } from './scriptBibleCandidates'
import { adaptBibleImageCandidateCreateResult, adaptBibleImageCandidates, bibleImageCandidateCreatePayload } from './bibleImageCandidates'
import { adaptDeferredGenerationRequest, deferredGenerationCreatePayload } from './deferredGeneration'
import { normalizeShotVersionScopeKey } from './shotVersionScope'

const sleep = (ms = 180) => new Promise((resolve) => setTimeout(resolve, ms))
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value))

const translationRows = (payload: unknown): PromptTranslationEntry[] => {
  const body = unwrap<unknown>(payload)
  const rows = Array.isArray(body)
    ? body
    : body && typeof body === 'object'
      ? ((body as Record<string, unknown>).entries || (body as Record<string, unknown>).translations || [])
      : []
  return Array.isArray(rows)
    ? rows.filter((row): row is Record<string, unknown> => Boolean(row && typeof row === 'object')).map(adaptTranslationEntry)
    : []
}

const loadLocal = <T>(key: string, fallback: T): T => {
  try { return JSON.parse(localStorage.getItem(key) || '') as T } catch { return clone(fallback) }
}
const saveLocal = <T>(key: string, value: T) => localStorage.setItem(key, JSON.stringify(value))

const fileDataUrl = (file: File): Promise<string> => new Promise((resolve, reject) => {
  const reader = new FileReader()
  reader.onload = () => resolve(String(reader.result || ''))
  reader.onerror = () => reject(new Error('无法读取封面文件，请重新选择'))
  reader.readAsDataURL(file)
})

const useMock = import.meta.env.VITE_USE_MOCK === 'true'
// Enqueue performs tenant/path validation and ffprobe snapshots before the
// durable job is returned. Give long episodes enough time without tying the
// browser to the hours-long FFmpeg execution itself.
const ASSEMBLY_ENQUEUE_TIMEOUT_MS = 120_000
// Server-side managed publication may spend up to 100s in FFmpeg. Keep this
// below Gunicorn's 120s ceiling, then reconcile by GET because a proxy/client
// timeout does not prove that the content-addressed publication failed.
const TASK_DISTRIBUTION_TIMEOUT_MS = 115_000
const TASK_DISTRIBUTION_RECONCILE_ATTEMPTS = 6

const strictMockBoolean = (name: string, value: unknown): boolean => {
  const normalized = String(value ?? '').trim().toLowerCase()
  if (normalized === 'true') return true
  if (normalized === 'false') return false
  throw new Error(`Mock 生产策略缺少有效布尔配置 ${name}`)
}

export interface ProductionCapability {
  recommendedMaxPending: number
  recommendedMaxActivePerUser: number
  maxConcurrent: number
  taskTimeoutSeconds: number
  videoShotMinDurationMs: number
  videoShotMaxDurationMs: number
  scriptBreakdownDefaultShotDurationMs: number
  scriptBreakdownDefaultFirstFrameContinuity: boolean
  scriptBreakdownDefaultLastFrameContinuity: boolean
  scriptBreakdownAutoSplitTriggerChars: number
  scriptBreakdownAutoSplitTargetChars: number
  activeTaskCount: number
  userActiveTaskCount: number
  confirmationRequired: boolean
  businessVolumeLimited: boolean
}

export interface DeferredGenerationRequestFilters {
  project?: string
  sourceTask?: string
  targetShot?: string
  status?: DeferredGenerationStatus
  frameSlot?: 'first_frame' | 'last_frame'
}

export interface ProductionBatchResumeSkippedTask {
  taskId: string
  batchIndex?: number
  status: string
  reason: string
}

export interface ProductionBatchResumeResult {
  batchId: string
  batchKind?: string
  scannedTaskCount: number
  retriedTaskIds: string[]
  skipped: ProductionBatchResumeSkippedTask[]
  tasks: Workflow[]
}

export interface ProductionBatchCancelResult {
  batchId: string
  batchKind?: string
  scannedTaskCount: number
  cancelledTaskIds: string[]
  cancelRequestedTaskIds: string[]
  skipped: ProductionBatchResumeSkippedTask[]
  downstreamCancellations: {
    deferredRequestsCancelled: number
    tasksCancelled: number
    tasksCancelRequested: number
  }
  tasks: Workflow[]
}

export interface ProductionBatchDeleteResult {
  batchId: string
  batchKind?: string
  scannedTaskCount: number
  deletedTaskIds: string[]
  deletedDeferredGenerationRequestIds: string[]
}

export interface ScriptBreakdownResult {
  project: SeriesProject
  method: string
  warnings: string[]
  counts: { episodes: number; shots: number }
  cleared: {
    episodes: number
    shots: number
    generationTasks: number
    assemblyJobs: number
    promptArtifacts: number
    generatedWorkflows: number
    creativeDrafts: number
    shotDetailDrafts: number
    shotDetailBatches: number
    shotDetailBatchItems: number
    promptFieldTranslations: number
    draftReleaseBundles: number
    physicalOutputsRetained: boolean
  }
}

export const productionErrorCode = (error: unknown): string => {
  const candidate = error as { response?: { data?: { code?: unknown; error?: { code?: unknown } } } }
  const code = String(candidate?.response?.data?.error?.code ?? candidate?.response?.data?.code ?? '').trim().toLowerCase()
  return /^[a-z][a-z0-9_]{0,79}$/.test(code) ? code : ''
}

const adaptProductionCapability = (raw: Record<string, unknown>): ProductionCapability => {
  const positiveInteger = (key: string): number => {
    const value = Number(raw[key])
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`生产策略缺少有效字段 ${key}`)
    return value
  }
  const positiveNumber = (key: string): number => {
    const value = Number(raw[key])
    if (!Number.isFinite(value) || value <= 0) throw new Error(`生产策略缺少有效字段 ${key}`)
    return value
  }
  const nonNegativeInteger = (key: string): number => {
    const value = Number(raw[key])
    if (!Number.isSafeInteger(value) || value < 0) throw new Error(`生产策略缺少有效字段 ${key}`)
    return value
  }
  const boolean = (key: string): boolean => {
    if (typeof raw[key] !== 'boolean') throw new Error(`生产策略缺少有效字段 ${key}`)
    return raw[key] as boolean
  }
  const videoShotMinDurationMs = positiveInteger('video_shot_min_duration_ms')
  const videoShotMaxDurationMs = positiveInteger('video_shot_max_duration_ms')
  if (videoShotMinDurationMs > videoShotMaxDurationMs) throw new Error('生产策略返回的视频镜头时长范围无效')
  const scriptBreakdownDefaultShotDurationMs = positiveInteger('script_breakdown_default_shot_duration_ms')
  if (
    scriptBreakdownDefaultShotDurationMs < videoShotMinDurationMs
    || scriptBreakdownDefaultShotDurationMs > videoShotMaxDurationMs
  ) throw new Error('生产策略返回的母稿默认镜头时长无效')
  const scriptBreakdownAutoSplitTriggerChars = positiveInteger('script_breakdown_auto_split_trigger_chars')
  const scriptBreakdownAutoSplitTargetChars = positiveInteger('script_breakdown_auto_split_target_chars')
  if (scriptBreakdownAutoSplitTargetChars > scriptBreakdownAutoSplitTriggerChars) {
    throw new Error('生产策略返回的母稿自动分段阈值无效')
  }
  return {
    recommendedMaxPending: positiveInteger('recommended_max_pending'),
    recommendedMaxActivePerUser: positiveInteger('recommended_max_active_per_user'),
    maxConcurrent: positiveInteger('max_concurrent'),
    taskTimeoutSeconds: positiveNumber('task_timeout_seconds'),
    videoShotMinDurationMs,
    videoShotMaxDurationMs,
    scriptBreakdownDefaultShotDurationMs,
    scriptBreakdownDefaultFirstFrameContinuity: boolean('script_breakdown_default_first_frame_continuity'),
    scriptBreakdownDefaultLastFrameContinuity: boolean('script_breakdown_default_last_frame_continuity'),
    scriptBreakdownAutoSplitTriggerChars,
    scriptBreakdownAutoSplitTargetChars,
    activeTaskCount: nonNegativeInteger('active_task_count'),
    userActiveTaskCount: nonNegativeInteger('user_active_task_count'),
    confirmationRequired: boolean('confirmation_required'),
    businessVolumeLimited: boolean('business_volume_limited'),
  }
}

const isAmbiguousPublicationFailure = (error: unknown): boolean => {
  if (!error || typeof error !== 'object') return false
  const candidate = error as { code?: string; response?: { status?: number }; message?: string }
  const status = Number(candidate.response?.status || 0)
  return !candidate.response
    || ['ECONNABORTED', 'ETIMEDOUT', 'ERR_NETWORK'].includes(String(candidate.code || ''))
    || [502, 503, 504].includes(status)
    || String(candidate.message || '').toLowerCase().includes('timeout')
}

const relationId = (value: unknown): string => {
  if (value && typeof value === 'object') return String((value as Record<string, unknown>).id || '')
  return value === null || value === undefined ? '' : String(value)
}

const adaptAssembly = (raw: Record<string, any>): AssemblyResult => {
  const status: AssemblyResult['status'] = ['planned', 'queued', 'running', 'succeeded', 'failed'].includes(raw.status)
    ? raw.status
    : 'planned'
  const scope: AssemblyResult['scope'] = raw.scope === 'series' ? 'series' : 'episode'
  const episodeId = relationId(raw.episode) || undefined
  return {
    id: String(raw.id),
    status,
    scope,
    projectId: relationId(raw.project),
    episodeId,
    outputUrl: String(raw.output_url || '') || undefined,
    downloadUrl: String(raw.download_url || '') || undefined,
    aiGenerated: raw.ai_generated !== false,
    provenanceUrl: String(raw.provenance_url || '') || undefined,
    sourceTaskIds: Array.isArray(raw.source_tasks) ? raw.source_tasks.map(relationId).filter(Boolean) : [],
    createdAt: String(raw.created_at || '') || undefined,
    queuedAt: String(raw.queued_at || '') || undefined,
    startedAt: String(raw.started_at || '') || undefined,
    finishedAt: String(raw.finished_at || '') || undefined,
    attempts: Number.isFinite(Number(raw.attempts)) ? Number(raw.attempts) : undefined,
    maxAttempts: Number.isFinite(Number(raw.max_attempts)) ? Number(raw.max_attempts) : undefined,
    errorMessage: String(raw.error_message || '') || undefined,
    message: status === 'succeeded'
      ? `${scope === 'episode' ? '本集' : '整剧'}成片已组装完成`
      : status === 'failed'
        ? (String(raw.error_message || '') || '成片组装失败')
        : status === 'running'
          ? '成片正在后台组装'
          : status === 'queued'
            ? '成片组装任务已进入队列'
            : '成片组装任务已创建'
  }
}

const adaptReleaseSelection = (raw: any): ReleaseSelection => ({
  shots: Array.isArray(raw?.shots) ? raw.shots.map((row: any) => ({
    shotId: String(row.shot_id || ''),
    taskId: String(row.task_id || ''),
    outputIndex: Number(row.output_index || 0),
  })).filter((row: { shotId: string; taskId: string }) => row.shotId && row.taskId) : [],
  episodes: Array.isArray(raw?.episodes) ? raw.episodes.map((row: any) => ({
    episodeId: String(row.episode_id || ''),
    assemblyId: String(row.assembly_id || ''),
  })).filter((row: { episodeId: string; assemblyId: string }) => row.episodeId && row.assemblyId) : [],
  seriesAssemblyId: String(raw?.series_assembly_id || '') || undefined,
})

const releaseSelectionPayload = (selection: ReleaseSelection) => ({
  shots: selection.shots.map((row) => ({
    shot_id: row.shotId, task_id: row.taskId, output_index: row.outputIndex,
  })),
  episodes: selection.episodes.map((row) => ({
    episode_id: row.episodeId, assembly_id: row.assemblyId,
  })),
  series_assembly_id: selection.seriesAssemblyId || null,
})

const adaptReleaseBundle = (raw: Record<string, any>): ReleaseBundle => ({
  id: String(raw.id),
  projectId: relationId(raw.project),
  version: Number(raw.version || 0),
  title: String(raw.title || ''),
  notes: String(raw.notes || ''),
  status: ['draft', 'sealed', 'released', 'superseded'].includes(raw.status)
    ? raw.status : 'draft',
  selection: adaptReleaseSelection(raw.selection),
  manifestSha256: String(raw.manifest_sha256 || '') || undefined,
  verificationStatus: ['draft', 'verified', 'invalid'].includes(raw.verification_status)
    ? raw.verification_status : 'invalid',
  sealedAt: String(raw.sealed_at || '') || undefined,
  releasedAt: String(raw.released_at || '') || undefined,
  createdAt: String(raw.created_at || '') || undefined,
  updatedAt: String(raw.updated_at || '') || undefined,
})

/**
 * Mock data is an explicit development data source, never an error fallback.
 * In API mode every authentication, validation and server error must reach the UI.
 */
const fromConfiguredSource = async <T>(request: () => Promise<T>, mock: () => Promise<T> | T): Promise<T> => {
  if (!useMock) return request()
  await sleep()
  return mock()
}

const workflowParameterDefinition = (value: unknown): WorkflowUserParameterDefinition | null => {
  if (!value || typeof value !== 'object') return null
  const raw = value as Record<string, unknown>
  if (!['integer', 'number'].includes(String(raw.type)) || !Number.isFinite(Number(raw.default))) return null
  const definition: WorkflowUserParameterDefinition = {
    label: String(raw.label || ''),
    type: raw.type === 'integer' ? 'integer' : 'number',
    default: Number(raw.default),
  }
  if (Number.isFinite(Number(raw.min))) definition.min = Number(raw.min)
  if (Number.isFinite(Number(raw.max))) definition.max = Number(raw.max)
  if (Number.isFinite(Number(raw.step)) && Number(raw.step) > 0) definition.step = Number(raw.step)
  if (raw.help) definition.help = String(raw.help)
  return definition
}

const adaptProductionWorkflowTemplate = (raw: Record<string, unknown>): ProductionWorkflowTemplate => {
  const schema = raw.input_schema && typeof raw.input_schema === 'object' ? raw.input_schema as Record<string, unknown> : {}
  const rawParameters = schema.user_parameters && typeof schema.user_parameters === 'object'
    ? schema.user_parameters as Record<string, unknown>
    : {}
  const userParameters = Object.fromEntries(Object.entries(rawParameters)
    .filter(([key]) => /^[A-Z][A-Z0-9_]{0,63}$/.test(key))
    .map(([key, value]) => [key, workflowParameterDefinition(value)] as const)
    .filter((entry): entry is [string, WorkflowUserParameterDefinition] => entry[1] !== null))
  const h3Modes = Array.isArray(schema.h3_modes)
    ? schema.h3_modes.filter((mode): mode is H3Mode => ['T2VA', 'I2VA', 'FL2VA', 'L2VA', 'Ref2VA'].includes(String(mode)))
    : []
  return {
    id: String(raw.id || ''),
    name: String(raw.name || raw.slug || '未命名工作流'),
    slug: String(raw.slug || ''),
    version: Number(raw.version || 1),
    outputKind: raw.output_kind === 'image' ? 'image' : 'video',
    isSystem: raw.is_system === true,
    isActive: raw.is_active !== false,
    h3Modes,
    isDefault: schema.is_default === true,
    maturity: schema.maturity === 'experimental' ? 'experimental' : 'stable',
    userParameters,
  }
}

const workflowSelectionsPayload = (selections: ProductionWorkflowSelection[]) => selections.map((selection) => {
  if (selection.outputKind !== 'video' || !['T2VA', 'I2VA', 'FL2VA', 'L2VA', 'Ref2VA'].includes(selection.h3Mode)) {
    throw new Error('工作流选择包含不支持的媒体类型或 H3 模式')
  }
  if (!selection.templateId) throw new Error('请选择有效的工作流模板')
  const parameters = Object.fromEntries(Object.entries(selection.parameters).map(([key, value]) => {
    if (!/^[A-Z][A-Z0-9_]{0,63}$/.test(key) || !Number.isFinite(value)) throw new Error(`工作流参数 ${key} 无效`)
    return [key, value]
  }))
  return { output_kind: selection.outputKind, h3_mode: selection.h3Mode, template_id: selection.templateId, parameters }
})

const mockBreakdown = (project: SeriesProject, options: ScriptBreakdownOptions): ScriptBreakdownResult => {
  const script = project.masterScript.trim()
  const previousEpisodeCount = project.episodes.length
  const previousShotCount = project.episodes.reduce((sum, episode) => sum + episode.shots.length, 0)
  if (!script) throw new Error('请先粘贴完整剧本')
  if (project.episodes.length && !options.replaceExisting) throw new Error('项目已有剧集；请勾选“替换现有剧集”或手工追加')

  const heading = /^(?:第\s*\d+\s*集|EPISODE\s+\d+)\s*[：:.-]?\s*(.*)$/gim
  const matches = [...script.matchAll(heading)]
  const chunks = matches.length
    ? matches.map((match, index) => ({ title: match[1]?.trim(), body: script.slice((match.index || 0) + match[0].length, matches[index + 1]?.index ?? script.length) }))
    : [{ title: '', body: script }]
  const timestamp = new Date().toISOString()

  project.episodes = chunks.map((chunk, episodeIndex) => {
    const shotMatches = [...chunk.body.matchAll(/^(?:镜头|分镜|SHOT)\s*\d*\s*(.*)$/gim)]
    const blocks = shotMatches.length
      ? shotMatches.map((item, index) => `${item[1] || ''}${chunk.body.slice((item.index || 0) + item[0].length, shotMatches[index + 1]?.index ?? chunk.body.length)}`.trim())
      : chunk.body.split(/\n\s*\n|\n/).map((item) => item.trim()).filter(Boolean)
    const plans = (blocks.length ? blocks : ['根据本集剧本建立开场镜头']).map((block) => {
      const durationRaw = block.match(/[【[]\s*(?:时长|duration)\s*[=：:]\s*([^】\]]+)[】\]]/i)?.[1]?.trim()
      const normalizedDuration = durationRaw?.replace(/\s+/g, '') || ''
      const durationValue = normalizedDuration.includes(':')
        ? (() => { const [minutes, seconds] = normalizedDuration.split(':', 2); return Number(minutes) * 60 + Number(seconds.replace(/(?:秒|s)$/i, '')) })()
        : Number(normalizedDuration.replace(/(?:毫秒|ms|秒|s)$/i, ''))
      const duration = Number.isFinite(durationValue) && durationValue > 0
        ? (/(?:毫秒|ms)$/i.test(durationRaw || '') ? durationValue / 1000 : durationValue)
        : options.defaultShotDuration
      const booleanDirective = (name: string, fallback: boolean) => {
        const value = block.match(new RegExp(`[【[]\\s*${name}\\s*[=：:]\\s*([^】\\]]+)[】\\]]`, 'i'))?.[1]?.trim().toLowerCase()
        if (!value) return fallback
        if (['是', 'true', '1', 'yes', 'on', '需要'].includes(value)) return true
        if (['否', 'false', '0', 'no', 'off', '无', '不需要'].includes(value)) return false
        return fallback
      }
      const cleaned = block.replace(
        /[【[]\s*(?:时长|duration|首帧连续|尾帧连续|连续)\s*[=：:][^】\]]+[】\]]/gi,
        '',
      ).replace(/^\s*[：:.-]\s*/, '').trim()
      const dialogueLines: string[] = []
      const actionLines: string[] = []
      for (const line of cleaned.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)) {
        const parsedDialogue = parseDialogueText(line, project.bible.characters)
        if (parsedDialogue?.length) { dialogueLines.push(line); continue }
        const visible = line.replace(
          /^(\s*[【[][^】\]]+[】\]]\s*)?(?:动作|ACTION)\s*[：:]\s*/i,
          '$1',
        )
        actionLines.push(/^[【[]/.test(visible) ? visible : `[0s–${duration}s] ${visible}`)
      }
      return {
        action: actionLines.join('\n') || `[0s–${duration}s] 根据本集剧本建立镜头`,
        dialogue: dialogueLines.join('\n'),
        duration,
        firstFrame: booleanDirective('首帧连续', options.defaultFirstFrameContinuity),
        lastFrame: booleanDirective('尾帧连续', options.defaultLastFrameContinuity),
      }
    })
    const shots: Shot[] = plans.map((plan, shotIndex) => ({
      id: `shot-${Date.now()}-${episodeIndex}-${shotIndex}`, order: shotIndex + 1, title: `镜头 ${shotIndex + 1}`, mediaType: 'video',
      duration: plan.duration, mode: project.defaultMode, framing: '中景', camera: '稳定推轨，保持动作与视线连续', action: plan.action, prompt: '', negativePrompt: project.bible.negativePrompt,
      characterIds: project.bible.characters.filter((item) => `${plan.action}\n${plan.dialogue}`.includes(item.name)).map((item) => item.id),
      sceneId: project.bible.scenes.find((item) => plan.action.includes(item.name))?.id || project.bible.scenes[0]?.id,
      propIds: project.bible.props.filter((item) => plan.action.includes(item.name)).map((item) => item.id), referenceAssetIds: [], referenceBindings: { characters: [], props: [], videos: [], audios: [] },
      requiresFirstFrameContinuity: plan.firstFrame,
      requiresLastFrameContinuity: plan.lastFrame,
      audioEnabled: options.includeAudio, audio: { dialogue: plan.dialogue, soundscape: '依据场景生成连续环境声', music: '克制的叙事配乐' }, status: 'draft'
    }))
    return { id: `episode-${Date.now()}-${episodeIndex}`, number: episodeIndex + 1, title: chunk.title || `第 ${episodeIndex + 1} 集`, synopsis: plans[0]?.action || '', script: chunk.body.trim(), status: 'draft', multiShot: options.multiShot, audioEnabled: options.includeAudio, shots, createdAt: timestamp }
  })
  project.updatedAt = timestamp
  const projects = loadLocal('cineforge_projects', mockProjects)
  const index = projects.findIndex((item) => item.id === project.id)
  if (index >= 0) projects[index] = project
  saveLocal('cineforge_projects', projects)
  return {
    project: clone(project),
    method: 'deterministic-script-breakdown-v3-mock',
    warnings: [],
    counts: {
      episodes: project.episodes.length,
      shots: project.episodes.reduce((sum, episode) => sum + episode.shots.length, 0),
    },
    cleared: {
      episodes: options.replaceExisting ? previousEpisodeCount : 0,
      shots: options.replaceExisting ? previousShotCount : 0,
      generationTasks: 0,
      assemblyJobs: 0,
      promptArtifacts: 0,
      generatedWorkflows: 0,
      creativeDrafts: 0,
      shotDetailDrafts: 0,
      shotDetailBatches: 0,
      shotDetailBatchItems: 0,
      promptFieldTranslations: 0,
      draftReleaseBundles: 0,
      physicalOutputsRetained: true,
    },
  }
}

export const repository = {
  async getProductionWorkflowTemplates(): Promise<ProductionWorkflowTemplate[]> {
    return fromConfiguredSource(async () => {
      const body = unwrap<unknown>((await api.get('/workflow-templates/production-options/')).data)
      if (!Array.isArray(body)) throw new Error('工作流模板接口未返回数组')
      const rows = body.filter((row): row is Record<string, unknown> => Boolean(row && typeof row === 'object'))
      return rows.map(adaptProductionWorkflowTemplate)
        .filter((template) => template.id && template.outputKind === 'video' && template.isSystem && template.isActive)
        .sort((left, right) => Number(right.isDefault) - Number(left.isDefault) || left.name.localeCompare(right.name, 'zh-CN'))
    }, () => [])
  },
  async getCurrentUser() {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.get('/auth/me/')).data)
      return {
        id: String(raw.id),
        name: raw.first_name || raw.username || raw.email || '用户',
        email: raw.email || raw.username || '',
        isStaff: Boolean(raw.is_staff),
      }
    }, () => ({ id: 'demo-user', name: '创作者', email: 'demo@example.com', isStaff: false }))
  },
  async getH3PromptOptimizerCapability(): Promise<H3PromptOptimizerCapability> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.get('/capabilities/h3-prompt-optimizer/')).data)
      const transport: H3PromptOptimizerCapability['transport'] = ['local', 'remote'].includes(raw.transport)
        ? raw.transport
        : 'unconfigured'
      return {
        enabled: Boolean(raw.enabled),
        configured: Boolean(raw.configured),
        transport,
        model: String(raw.model || ''),
        failureMode: raw.failure_mode === 'block' ? 'block' : 'warn',
        languageGate: raw.language_gate === 'warn' ? 'warn' : 'block',
      }
    }, () => ({
      enabled: false,
      configured: false,
      transport: 'unconfigured',
      model: '',
      failureMode: 'warn',
      languageGate: 'block',
    }))
  },
  async getProductionCapability(): Promise<ProductionCapability> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, unknown>>(
        (await api.get('/capabilities/production/')).data,
      )
      return adaptProductionCapability(raw)
    }, () => adaptProductionCapability({
      recommended_max_pending: import.meta.env.VITE_MOCK_PRODUCTION_RECOMMENDED_MAX_PENDING,
      recommended_max_active_per_user: import.meta.env.VITE_MOCK_PRODUCTION_RECOMMENDED_MAX_ACTIVE_PER_USER,
      max_concurrent: import.meta.env.VITE_MOCK_PRODUCTION_MAX_CONCURRENT,
      task_timeout_seconds: import.meta.env.VITE_MOCK_PRODUCTION_TASK_TIMEOUT_SECONDS,
      video_shot_min_duration_ms: import.meta.env.VITE_MOCK_VIDEO_SHOT_MIN_DURATION_MS,
      video_shot_max_duration_ms: import.meta.env.VITE_MOCK_VIDEO_SHOT_MAX_DURATION_MS,
      script_breakdown_default_shot_duration_ms: import.meta.env.VITE_MOCK_SCRIPT_BREAKDOWN_DEFAULT_SHOT_DURATION_MS,
      script_breakdown_default_first_frame_continuity: strictMockBoolean('VITE_MOCK_SCRIPT_BREAKDOWN_DEFAULT_FIRST_FRAME_CONTINUITY', import.meta.env.VITE_MOCK_SCRIPT_BREAKDOWN_DEFAULT_FIRST_FRAME_CONTINUITY),
      script_breakdown_default_last_frame_continuity: strictMockBoolean('VITE_MOCK_SCRIPT_BREAKDOWN_DEFAULT_LAST_FRAME_CONTINUITY', import.meta.env.VITE_MOCK_SCRIPT_BREAKDOWN_DEFAULT_LAST_FRAME_CONTINUITY),
      script_breakdown_auto_split_trigger_chars: import.meta.env.VITE_MOCK_SCRIPT_BREAKDOWN_AUTO_SPLIT_TRIGGER_CHARS,
      script_breakdown_auto_split_target_chars: import.meta.env.VITE_MOCK_SCRIPT_BREAKDOWN_AUTO_SPLIT_TARGET_CHARS,
      active_task_count: 0,
      user_active_task_count: 0,
      confirmation_required: true,
      business_volume_limited: false,
    }))
  },
  async getTranslationCapability(): Promise<TranslationCapability> {
    return fromConfiguredSource<TranslationCapability>(async () => {
      const raw = unwrap<Record<string, unknown>>((await api.get('/capabilities/prompt-translation/')).data)
      const providerCapability = adaptPromptProviders(raw)
      return {
        enabled: raw.enabled !== false,
        configured: raw.configured === true,
        provider: String(raw.provider || 'qwen-local'),
        model: String(raw.model || ''),
        sourceLanguage: String(raw.source_language || raw.source_lang || 'Chinese'),
        targetLanguage: String(raw.target_language || raw.target_lang || 'English'),
        message: String(raw.message || raw.notice || '') || undefined,
        modelDownloaded: raw.model_downloaded === true,
        modelVerified: raw.model_verified === true,
        modelSizeBytes: Number(raw.model_size_bytes || 0) || undefined,
        modelVerifiedAt: String(raw.verified_at || '') || undefined,
        device: String(raw.device || 'cpu'),
        loadedOnDemand: raw.loaded_on_demand === true,
        unloadedAfterRequest: raw.unloaded_after_request === true,
        reviewRequired: raw.review_required !== false,
        officialQwenMtCheckpoint: raw.official_qwen_mt_checkpoint === true,
        installCommand: String(raw.install_command || '') || undefined,
        estimatedDownloadGb: Number(raw.estimated_download_gb || 2.5),
        ...providerCapability,
      }
    }, () => ({
      enabled: true, configured: true, provider: 'mock-qwen-local', model: 'Qwen/Qwen3-4B-GGUF',
      sourceLanguage: 'Chinese', targetLanguage: 'English', message: 'Mock translation review capability.',
      modelDownloaded: true, modelVerified: true, modelSizeBytes: 2_497_280_256, modelVerifiedAt: new Date().toISOString(), device: 'cpu', loadedOnDemand: true, unloadedAfterRequest: true,
      reviewRequired: true, officialQwenMtCheckpoint: false, estimatedDownloadGb: 2.5,
      defaultProvider: 'local_qwen', providers: [{ id: 'local_qwen', label: '本地 Qwen', enabled: true,
        configured: true, ready: true, model: 'Qwen/Qwen3-4B-GGUF', transport: 'local',
        modelDownloaded: true, modelVerified: true, device: 'cpu' }],
    }))
  },
  async ensureCanonicalGlossary(
    projectId: string,
    options: {
      entries?: Array<{ scopeType: CanonicalGlossaryScope; scopeId: string }>
      includeAllMissing?: boolean
      ensureVisualIdentityTranslation?: boolean
    } = {},
  ): Promise<CanonicalGlossaryEnsureResult> {
    return fromConfiguredSource(async () => {
      const response = await api.post(`/projects/${projectId}/ensure-canonical-glossary/`, {
        entries: options.entries?.map((entry) => ({ scope_type: entry.scopeType, scope_id: entry.scopeId })),
        include_all_missing: options.includeAllMissing === true,
        ensure_visual_identity_translation: options.ensureVisualIdentityTranslation === true,
      }, { timeout: 360_000 })
      return adaptCanonicalGlossaryEnsureResult(response.data)
    }, () => {
      const project = loadLocal('cineforge_projects', mockProjects).find((item) => item.id === projectId)
      const requested = options.entries || []
      const byScope = {
        character: project?.bible.characters || [], scene: project?.bible.scenes || [], prop: project?.bible.props || [],
      }
      const selected = options.includeAllMissing
        ? (Object.entries(byScope) as Array<[CanonicalGlossaryScope, typeof byScope.character]>).flatMap(([scopeType, items]) => items.map((item) => ({ scopeType, scopeId: item.id, item })))
        : requested.map((entry) => ({ ...entry, item: byScope[entry.scopeType].find((item) => item.id === entry.scopeId) })).filter((entry) => entry.item)
      const entries = selected.map(({ scopeType, scopeId, item }) => {
        const canonicalEnglish = String(project?.translationGlossary?.[item!.name] || '')
        return {
          scopeType, scopeId, name: item!.name, canonicalEnglish,
          status: canonicalEnglish ? 'reused' as const : 'failed' as const,
          origin: canonicalEnglish ? 'existing' as const : 'local_qwen' as const,
          retryable: !canonicalEnglish,
          ...(!canonicalEnglish ? { errorCode: 'mock_translation_unavailable', message: 'Mock 数据源不会猜测英文专名，请手工填写 canonical 术语。' } : {}),
        }
      })
      const visualTranslations = options.ensureVisualIdentityTranslation ? selected.map(({ scopeType, scopeId, item }) => {
        const sourceText = String(item!.visualPrompt || '')
        const needsTranslation = /[\u3400-\u4dbf\u4e00-\u9fff]/.test(sourceText)
        return {
          scopeType, scopeId, fieldKey: `${scopeType}.visual_identity`, sourceText, translatedText: '',
          status: needsTranslation ? 'failed' as const : 'not_required' as const,
          origin: needsTranslation ? 'local_qwen' as const : 'source_english' as const,
          contextCurrent: false, retryable: needsTranslation,
          ...(needsTranslation ? { errorCode: 'mock_translation_unavailable', message: 'Mock 数据源不会伪造视觉锚点英文候选。' } : {}),
        }
      }) : []
      return {
        projectId, updatedAt: project?.updatedAt, translationGlossary: { ...(project?.translationGlossary || {}) }, entries,
        summary: { requested: entries.length, generated: 0, reused: entries.filter((entry) => entry.status === 'reused').length, conflict: 0, failed: entries.filter((entry) => entry.status === 'failed').length },
        visualSummary: {
          requested: visualTranslations.length, generated: 0, reused: 0,
          notRequired: visualTranslations.filter((entry) => entry.status === 'not_required').length,
          conflict: 0, failed: visualTranslations.filter((entry) => entry.status === 'failed').length,
        },
        visualTranslations,
      }
    })
  },
  async getProjectTranslations(
    projectId: string,
    filters: { episodeId?: string; shotId?: string; status?: string } = {},
  ): Promise<PromptTranslationEntry[]> {
    return fromConfiguredSource(async () => {
      const response = await api.get(`/projects/${projectId}/translations/`, { params: {
        episode: filters.episodeId || undefined,
        shot: filters.shotId || undefined,
        status: filters.status || undefined,
      } })
      return translationRows(response.data)
    }, () => [])
  },
  async translateProjectFields(
    projectId: string,
    fields: PromptTranslationField[],
    provider: PromptProviderId = 'local_qwen',
  ): Promise<PromptTranslationEntry[]> {
    if (!fields.length) return []
    return fromConfiguredSource(async () => {
      const response = await api.post(`/projects/${projectId}/translations/translate/`, {
        source_lang: 'Chinese', target_lang: 'English',
        provider,
        entries: fields.map((field) => ({
          field_key: field.fieldKey,
          source_text: field.sourceText,
          scope_type: field.scopeType,
          scope_id: field.scopeId,
        })),
      }, { timeout: 360_000 })
      return translationRows(response.data)
    }, () => fields.map((field, index) => ({
      id: `translation-${Date.now()}-${index}`, fieldKey: field.fieldKey,
      scopeType: field.scopeType, scopeId: field.scopeId, sourceText: field.sourceText,
      translatedText: `[Mock English translation] ${field.sourceText}`, status: 'draft_translation' as const,
      revision: 1, provider: 'mock-qwen-local', model: 'Qwen/Qwen3-4B-GGUF', updatedAt: new Date().toISOString(),
    })))
  },
  async retranslateProjectField(entryId: string, sourceText?: string, provider: PromptProviderId = 'local_qwen'): Promise<PromptTranslationEntry> {
    return fromConfiguredSource(async () => adaptTranslationEntry(unwrap<Record<string, unknown>>((await api.post(
      `/translations/${entryId}/retranslate/`, { ...(sourceText === undefined ? {} : { source_text: sourceText }), provider }, { timeout: 360_000 },
    )).data)), () => ({
      id: entryId, fieldKey: '', sourceText: sourceText || '', translatedText: `[Mock English translation] ${sourceText || ''}`,
      status: 'draft_translation', revision: 2, provider: 'mock-qwen-local', model: 'Qwen/Qwen3-4B-GGUF', updatedAt: new Date().toISOString(),
    }))
  },
  async confirmProjectTranslation(entryId: string, translatedText: string, expectedRevision: number): Promise<PromptTranslationEntry> {
    return fromConfiguredSource(async () => adaptTranslationEntry(unwrap<Record<string, unknown>>((await api.post(
      `/translations/${entryId}/confirm/`, { expected_revision: expectedRevision, translation: translatedText },
    )).data)), () => ({
      id: entryId, fieldKey: '', sourceText: '', translatedText, status: 'confirmed', revision: 1,
      provider: 'mock-qwen-local', model: 'Qwen/Qwen3-4B-GGUF', confirmedAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
    }))
  },
  async promoteShotTranslations(
    shotId: string,
    entries: Array<{ entryId: string; expectedRevision: number }>,
    options: { repairLegacyNegativePrompt?: boolean } = {},
  ): Promise<ShotTranslationPromotionResult> {
    return fromConfiguredSource(async () => adaptShotTranslationPromotionResult(unwrap<Record<string, unknown>>((await api.post(
      '/translations/promote-shot/', {
        shot_id: shotId,
        entries: entries.map((entry) => ({ entry_id: entry.entryId, expected_revision: entry.expectedRevision })),
        repair_legacy_negative_prompt: options.repairLegacyNegativePrompt === true,
      },
    )).data)), () => ({ shotId, promotedFields: [], entries: [], shotPatch: {} }))
  },
  async revokeProjectTranslation(entryId: string): Promise<PromptTranslationEntry> {
    return fromConfiguredSource(async () => adaptTranslationEntry(unwrap<Record<string, unknown>>((await api.post(
      `/translations/${entryId}/revoke/`, {},
    )).data)), () => ({
      id: entryId, fieldKey: '', sourceText: '', translatedText: '', status: 'draft_translation', revision: 1,
      provider: 'mock-qwen-local', model: 'Qwen/Qwen3-4B-GGUF', updatedAt: new Date().toISOString(),
    }))
  },
  async login(email: string, password: string) {
    return fromConfiguredSource(
      async () => {
        const tokens = unwrap<{ access: string; refresh: string }>((await api.post('/auth/token/', { username: email, password })).data)
        localStorage.setItem('cineforge_token', tokens.access)
        const raw = unwrap<Record<string, any>>((await api.get('/auth/me/')).data)
        return { access: tokens.access, refresh: tokens.refresh, user: { id: String(raw.id), name: raw.first_name || raw.username || email, email: raw.email || email, isStaff: Boolean(raw.is_staff) } }
      },
      () => ({ access: 'demo-access-token', refresh: 'demo-refresh-token', user: { id: 'demo-user', name: email.split('@')[0] || '创作者', email, isStaff: false } })
    )
  },
  async register(name: string, email: string, password: string) {
    return fromConfiguredSource(
      async () => {
        await api.post('/auth/register/', { username: email, email, password })
        const tokens = unwrap<{ access: string; refresh: string }>((await api.post('/auth/token/', { username: email, password })).data)
        localStorage.setItem('cineforge_token', tokens.access)
        const raw = unwrap<Record<string, any>>((await api.get('/auth/me/')).data)
        return { access: tokens.access, refresh: tokens.refresh, user: { id: String(raw.id), name: raw.first_name || name || raw.username || email, email: raw.email || email, isStaff: Boolean(raw.is_staff) } }
      },
      () => ({ access: 'demo-access-token', refresh: 'demo-refresh-token', user: { id: 'demo-user', name, email, isStaff: false } })
    )
  },
  async getRegistrationCapability(): Promise<{ publicRegistrationEnabled: boolean }> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, unknown>>((await api.get('/auth/register/')).data)
      return { publicRegistrationEnabled: raw.public_registration_enabled === true }
    }, () => ({ publicRegistrationEnabled: true }))
  },
  async getStyles(): Promise<StylePreset[]> {
    return fromConfiguredSource(async () => adaptStyles(await fetchAllPages('/styles/', { is_active: true, page_size: 500 })), () => clone(stylePresets))
  },
  async getAssets(params?: { style?: string; type?: string; search?: string }): Promise<Asset[]> {
    return fromConfiguredSource(async () => {
      // UI categories are derived from both native kinds and generic
      // reference.metadata.reference_category. Filtering by backend `kind`
      // would silently hide classified generic references (and `image` is not
      // a backend kind), so adapt first and filter the complete result here.
      const rows=adaptAssets(await fetchAllPages('/assets/', { style: backendStyleId(params?.style), search: params?.search, page_size: 500 }))
        .filter((asset)=>asset.metadata?.usage !== 'project_cover')
      return params?.type?rows.filter((asset)=>asset.type===params.type):rows
    }, () => {
      const query = params?.search?.toLowerCase()
      return clone(mockAssets.filter((asset) => (!params?.style || asset.styleId === params.style) && (!params?.type || asset.type === params.type) && (!query || `${asset.name}${asset.description}${asset.tags.join('')}`.toLowerCase().includes(query))))
    })
  },
  async createImageAsset(payload: {
    file: File
    kind: BibleDraftEntityType | 'reference'
    name: string
    description: string
    promptHint: string
    styleId?: string
  }): Promise<Asset> {
    return fromConfiguredSource(async () => {
      const form = new FormData()
      form.append('file', payload.file, payload.file.name)
      form.append('kind', payload.kind)
      form.append('name', payload.name.trim())
      form.append('description', payload.description.trim())
      form.append('prompt_hint', payload.promptHint.trim())
      const style = backendStyleId(payload.styleId)
      if (style) form.append('style', style)
      const raw = unwrap<Record<string, unknown>>((await api.post('/assets/', form, { headers: { 'Content-Type': 'multipart/form-data' }, timeout: 120_000 })).data)
      const asset = adaptAssets([raw])[0]
      if (!asset) throw new Error('上传成功，但服务端未返回可用的图片素材')
      return asset
    }, () => ({
      id: `asset-${Date.now()}`, styleId: payload.styleId || '', type: payload.kind === 'reference' ? 'image' : payload.kind,
      backendKind: payload.kind, name: payload.name.trim(), description: payload.description.trim(), promptHint: payload.promptHint.trim(),
      checksum: `mock-${Date.now()}`, ownerId: 'demo-user', updatedAt:new Date().toISOString(), metadataSha256:`mock-meta-${Date.now()}`, thumbnail: URL.createObjectURL(payload.file), mediaUrl: URL.createObjectURL(payload.file),
      mimeType: payload.file.type, metadata: {}, tags: [payload.kind, '用户素材'],
    }))
  },
  async setProjectCover(project: SeriesProject, file: File | null): Promise<SeriesProject> {
    return fromConfiguredSource(async () => {
      if (!file) {
        const raw = unwrap<Record<string, unknown>>((await api.patch(`/projects/${project.id}/`, { cover_asset: null })).data)
        return {
          ...project,
          coverAssetId: relationId(raw.cover_asset) || undefined,
          coverUrl: String(raw.cover_url || '') || undefined,
          updatedAt: String(raw.updated_at || project.updatedAt),
        }
      }

      const form = new FormData()
      const assetName = Array.from(`${project.title.trim() || '连续剧'} · 封面`).slice(0, 160).join('')
      form.append('file', file, file.name)
      form.append('kind', 'reference')
      form.append('name', assetName)
      form.append('metadata', JSON.stringify({ usage: 'project_cover', project_id: project.id }))
      const rawAsset = unwrap<Record<string, unknown>>((await api.post('/assets/', form, {
        headers: { 'Content-Type': 'multipart/form-data' }, timeout: 120_000,
      })).data)
      const uploadedAssetId = relationId(rawAsset.id)
      if (!uploadedAssetId) throw new Error('封面上传成功，但服务端未返回素材 ID')

      try {
        const raw = unwrap<Record<string, unknown>>((await api.patch(`/projects/${project.id}/`, {
          cover_asset: uploadedAssetId,
        })).data)
        return {
          ...project,
          coverAssetId: relationId(raw.cover_asset) || uploadedAssetId,
          coverUrl: String(raw.cover_url || '') || undefined,
          updatedAt: String(raw.updated_at || project.updatedAt),
        }
      } catch (bindingError) {
        try {
          await api.delete(`/assets/${uploadedAssetId}/`)
        } catch {
          throw new Error('封面绑定失败，且已上传的临时封面未能自动清理，请稍后在素材管理中检查。')
        }
        throw bindingError
      }
    }, async () => {
      const projects = loadLocal('cineforge_projects', mockProjects)
      const index = projects.findIndex((item) => item.id === project.id)
      if (index < 0) throw new Error('项目不存在')
      const next: SeriesProject = {
        ...projects[index],
        coverAssetId: file ? `cover-${Date.now()}` : undefined,
        coverUrl: file ? await fileDataUrl(file) : undefined,
        updatedAt: new Date().toISOString(),
      }
      projects[index] = next
      saveLocal('cineforge_projects', projects)
      return clone(next)
    })
  },
  async updateAssetMetadata(assetId: string, payload: { name: string; description: string; promptHint: string; kind: BibleDraftEntityType | 'reference'; styleId?: string }): Promise<Asset> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, unknown>>((await api.patch(`/assets/${assetId}/`, {
        name: payload.name.trim(), description: payload.description.trim(), prompt_hint: payload.promptHint.trim(),
        kind: payload.kind, style: backendStyleId(payload.styleId) || null,
      })).data)
      const asset = adaptAssets([raw])[0]
      if (!asset) throw new Error('保存成功，但服务端未返回可用的素材信息')
      return asset
    }, () => {
      const existing = mockAssets.find((item) => item.id === assetId)
      if (!existing) throw new Error('素材不存在')
      return { ...clone(existing), name: payload.name.trim(), description: payload.description.trim(), promptHint: payload.promptHint.trim(), type: payload.kind === 'reference' ? 'image' : payload.kind, backendKind: payload.kind, styleId: payload.styleId || '' }
    })
  },
  async deleteAsset(assetId: string): Promise<void> {
    return fromConfiguredSource(async () => {
      await api.delete(`/assets/${assetId}/`)
    }, () => { if (!assetId.trim()) throw new Error('素材不存在') })
  },
  async generateBibleDraftFromAssets(
    projectId: string,
    payload: { entityType: BibleDraftEntityType; outputLanguage: BibleDraftLanguage; asset: Asset; attempt?: number },
  ): Promise<BibleAssetDraftResult> {
    if (!payload.asset.checksum) throw new Error(`素材“${payload.asset.name}”缺少内容校验值，请刷新素材库后重试`)
    return fromConfiguredSource<BibleAssetDraftResult>(async () => {
      const response = await api.post(`/projects/${projectId}/bible-drafts/from-assets/`, {
        entity_type: payload.entityType,
        output_language: payload.outputLanguage,
        asset_id: payload.asset.id,
        asset_checksum: payload.asset.checksum,
        attempt: payload.attempt,
      }, { timeout: 360_000 })
      const raw = unwrap<Record<string, any>>(response.data)
      const draft = (raw.draft || {}) as Record<string, unknown>
      return {
        evidenceBasis: 'asset_metadata', pixelVisionUsed: false, referenceMode: 'single_reference', sourceMetadataSha256: String(raw.source_metadata_sha256 || '') || undefined,
        outputLanguage: raw.output_language === 'en' ? 'en' : 'zh',
        entityType: ['character', 'scene', 'prop'].includes(raw.entity_type) ? raw.entity_type : payload.entityType,
        draft: { name: String(draft.name || ''), description: String(draft.description || ''), visualPrompt: String(draft.visual_identity || '') },
        sourceAssets: Array.isArray(raw.source_assets) ? raw.source_assets.map((item: Record<string, unknown>) => ({ id: String(item.id || ''), name: String(item.name || ''), kind: String(item.kind || ''), checksum: String(item.checksum || ''), description: String(item.description || ''), promptHint: String(item.prompt_hint || ''), updatedAt:String(item.updated_at||'')||undefined, metadataSha256:String(item.metadata_sha256||'')||undefined, metadataFieldsUsed:Array.isArray(item.metadata_fields_used)?item.metadata_fields_used.map(String):undefined })) : [],
        warnings: Array.isArray(raw.warnings) ? raw.warnings.map(String) : [],
        runtime: raw.runtime && typeof raw.runtime === 'object' ? raw.runtime : undefined,
      }
    }, () => {
      const names = [payload.asset.name].filter(Boolean)
      const descriptions = [payload.asset.description].filter(Boolean)
      const anchors = [payload.asset.promptHint].filter(Boolean)
      const prefix = payload.outputLanguage === 'en' ? '[Mock English draft] ' : ''
      return {
        evidenceBasis: 'asset_metadata' as const, pixelVisionUsed: false as const, referenceMode: 'single_reference' as const, sourceMetadataSha256: `mock-${payload.asset.checksum}`, outputLanguage: payload.outputLanguage, entityType: payload.entityType,
        draft: { name: names.join(' / '), description: `${prefix}${descriptions.join('；')}`, visualPrompt: `${prefix}${anchors.join('；')}` },
        sourceAssets: [payload.asset].map((asset) => ({ id: asset.id, name: asset.name, kind: asset.backendKind || asset.type, checksum: asset.checksum || '', description: asset.description, promptHint: asset.promptHint || '' })),
        warnings: anchors.length ? [] : ['所选素材没有视觉锚点元数据，草稿质量可能较低。'], runtime: { provider: 'mock' },
      }
    })
  },
  async uploadReferenceAsset(file: File, kind: 'reference_video'|'reference_audio', styleId?: string):Promise<Asset> {
    return fromConfiguredSource(async()=>{
      const form=new FormData();form.append('file',file,file.name);form.append('name',file.name.replace(/\.[^.]+$/,''));form.append('kind',kind)
      const style=backendStyleId(styleId);if(style)form.append('style',style)
      const raw=unwrap<Record<string,unknown>>((await api.post('/assets/',form,{headers:{'Content-Type':'multipart/form-data'},timeout:120_000})).data)
      const asset=adaptAssets([raw])[0];if(!asset)throw new Error('上传成功，但服务端未返回可用的参考素材');return asset
    },()=>({id:`upload-${Date.now()}`,styleId:styleId||'',type:kind==='reference_video'?'video':'audio',backendKind:kind,name:file.name,description:'本地开发上传素材',thumbnail:'',mediaUrl:URL.createObjectURL(file),mimeType:file.type,metadata:{has_audio:false},hasEmbeddedAudio:false,tags:['用户上传']}))
  },
  async getProjects(): Promise<SeriesProject[]> {
    return fromConfiguredSource(async () => {
      const raw = await fetchAllPages<Record<string, unknown>>('/projects/', { page_size: 200 })
      return raw.map((project) => adaptProjectSummary(project))
    }, () => loadLocal('cineforge_projects', mockProjects).map((project) => ({
      ...project,
      episodeCount: project.episodes.length,
      completedEpisodeCount: project.episodes.filter((episode) => episode.status === 'completed').length,
      totalDurationSeconds: project.episodes.reduce((total, episode) => total + episode.shots.reduce((sum, shot) => sum + shot.duration, 0), 0),
      detailLoaded: true,
    })))
  },
  async getProject(id: string): Promise<SeriesProject> {
    return fromConfiguredSource(async () => adaptProject(unwrap<Record<string, unknown>>((await api.get(`/projects/${id}/`)).data)), () => {
      const project = loadLocal('cineforge_projects', mockProjects).find((item) => item.id === id)
      if (!project) throw new Error('项目不存在')
      return { ...project, episodeCount: project.episodes.length, completedEpisodeCount: project.episodes.filter((episode) => episode.status === 'completed').length, totalDurationSeconds: project.episodes.reduce((total, episode) => total + episode.shots.reduce((sum, shot) => sum + shot.duration, 0), 0), detailLoaded: true }
    })
  },
  async getScriptBibleCandidates(projectId: string): Promise<ScriptBibleCandidatesResult> {
    return fromConfiguredSource(async () => {
      const response = await api.get(`/projects/${projectId}/script-bible-candidates/`)
      const result = adaptScriptBibleCandidates(unwrap<unknown>(response.data))
      if (result.projectId !== projectId) throw new Error('服务端返回的剧本 Bible 候选项目不匹配。')
      return result
    }, () => {
      throw new Error('Mock 模式不提供剧本 Bible 候选，未执行任何写入。')
    })
  },
  async applyScriptBibleCandidates(
    projectId: string,
    sourceFingerprint: string,
    selections: ScriptBibleReviewedSelection[],
  ): Promise<ScriptBibleApplyResult> {
    return fromConfiguredSource(async () => {
      const response = await api.post(
        `/projects/${projectId}/script-bible-candidates/apply/`,
        scriptBibleApplyPayload(sourceFingerprint, selections),
      )
      const result = adaptScriptBibleApplyResult(unwrap<unknown>(response.data))
      if (
        result.projectId !== projectId
        || result.sourceFingerprint !== sourceFingerprint
        || result.counts.requested !== selections.length
      ) throw new Error('服务端返回的剧本 Bible 确认结果与请求不匹配。')
      return result
    }, () => {
      throw new Error('Mock 模式不提供剧本 Bible 候选确认，未执行任何写入。')
    })
  },
  async getBibleImageCandidates(projectId: string): Promise<BibleImageCandidatesResult> {
    return fromConfiguredSource(async () => {
      const response = await api.get(`/projects/${projectId}/bible-image-candidates/`)
      const result = adaptBibleImageCandidates(unwrap<unknown>(response.data))
      if (result.projectId !== projectId) throw new Error('服务端返回的实体图片候选项目不匹配。')
      return result
    }, () => {
      throw new Error('Mock 模式不提供实体图片候选，未执行任何写入。')
    })
  },
  async createBibleImageCandidate(
    projectId: string,
    options: BibleImageCandidateCreateOptions,
  ): Promise<BibleImageCandidateCreateResult> {
    return fromConfiguredSource(async () => {
      const response = await api.post(
        `/projects/${projectId}/bible-image-candidates/`,
        bibleImageCandidateCreatePayload(options),
      )
      const result = adaptBibleImageCandidateCreateResult(unwrap<unknown>(response.data))
      if (
        result.projectId !== projectId
        || result.candidate.entityType !== options.entityType
        || result.candidate.entityId !== options.entityId
        || result.candidate.latestCandidate?.generationTaskId !== result.generationTaskId
      ) throw new Error('服务端返回的实体图片候选与请求目标不匹配。')
      return result
    }, () => {
      throw new Error('Mock 模式不提供实体图片候选生成，未创建任何任务。')
    })
  },
  async generateVisualRulesDraft(projectId: string, payload: {
    attempt: number
    variationNote?: string
    expectedContextFingerprint?: string
  }): Promise<VisualRulesDraftResult> {
    return fromConfiguredSource(async () => {
      const response = await api.post(
        `/projects/${projectId}/visual-rules-drafts/generate/`,
        visualRulesDraftGeneratePayload(payload),
        { timeout: 360_000 },
      )
      return adaptVisualRulesDraftResult(unwrap<Record<string, unknown>>(response.data))
    }, () => {
      const project = loadLocal('cineforge_projects', mockProjects).find((item) => item.id === projectId)
      if (!project) throw new Error('项目不存在')
      const fingerprint = `${String(Date.now()).padStart(16, '0')}${'0'.repeat(48)}`.slice(0, 64)
      return {
        projectId,
        contextFingerprint: fingerprint,
        contextCurrent: true,
        candidateLanguage: 'en', reviewRequired: true, persisted: false,
        draft: {
          negativePrompt: 'identity drift, face drift, age drift, hairstyle change, costume change, prop mutation, extra limbs, readable text, subtitle, watermark, logo, flicker, jitter, low detail',
          colorScript: 'Use a restrained cool cyan-gray base with warm practical highlights; reserve muted dark red for clues, then transition to controlled warm gold at the climax while preserving natural skin tones.',
          continuitySummary: 'Preserve stable character identity, wardrobe, architecture, prop geometry, weather logic, light direction, and spatial continuity across every episode.',
        },
        colorDesign: { baseColors:['teal','charcoal'], accentColor:'amber', paletteRelation:'cinematic', lighting:'low_key', contrast:'restrained', saturation:'muted', exposure:'dark', progression:'cool_to_warm', preserveNaturalSkinExposure:true },
        fieldModes: { negativePrompt: 'deterministic_guard', colorScript: 'structured_design', continuitySummary: 'deterministic_guard' },
        sourceSummary: { projectFields: ['title','logline','master_script'], productionSettingFields: ['negative_prompt','color_script','translation_glossary'], styleIncluded: true, characterCount: project.bible.characters.length, sceneCount: project.bible.scenes.length, propCount: project.bible.props.length, episodeSummaryCount: project.episodes.length, episodeScriptsIncluded: false, shotsIncluded: false },
        generation: { attempt: payload.attempt, directionNoteProvided: false, variationNoteProvided: Boolean(payload.variationNote?.trim()), generationFingerprint: 'f'.repeat(64) },
        warnings: [],
        runtime: { provider: 'mock-local-qwen', model: 'mock-qwen', loadedOnDemand: true, unloadedAfterRequest: true, reviewRequired: true, persisted: false, elapsedMs: 180, repairAttempted: false, repairSucceeded: false },
      }
    })
  },
  async applyVisualRulesDraft(projectId: string, payload: {
    contextFingerprint: string
    generationFingerprint: string
    attempt: number
    directionNote?: string
    variationNote?: string
    colorDesign: VisualRulesColorDesign
  }): Promise<VisualRulesDraftApplyResult> {
    return fromConfiguredSource(async () => {
      const response = await api.post(
        `/projects/${projectId}/visual-rules-drafts/apply/`,
        visualRulesDraftApplyPayload(payload),
      )
      return adaptVisualRulesDraftApplyResult(unwrap<Record<string, unknown>>(response.data))
    }, () => {
      const projects = loadLocal('cineforge_projects', mockProjects)
      const project = projects.find((item) => item.id === projectId)
      if (!project) throw new Error('项目不存在')
      const applied = {
        negativePrompt: 'identity drift, face drift, costume change, prop mutation, readable text, watermark, flicker, jitter, low detail',
        colorScript: 'The mock service applied the reviewed structured color design.',
        continuitySummary: 'Maintain stable identity, wardrobe, architecture, spatial logic, lighting logic, and prop continuity across episodes.',
      }
      project.bible.negativePrompt = applied.negativePrompt
      project.bible.colorScript = applied.colorScript
      project.continuitySummary = applied.continuitySummary
      project.updatedAt = new Date().toISOString()
      saveLocal('cineforge_projects', projects)
      return {
        projectId,
        appliedContextFingerprint: payload.contextFingerprint,
        currentContextFingerprint: payload.contextFingerprint,
        updatedAt: project.updatedAt,
        applied,
      }
    })
  },
  async createProject(payload: Partial<SeriesProject>): Promise<SeriesProject> {
    return fromConfiguredSource(async () => adaptProjectSummary(unwrap<Record<string, unknown>>((await api.post('/projects/', projectPayload(payload))).data), true), () => {
      const projects = loadLocal('cineforge_projects', mockProjects)
      const timestamp = new Date().toISOString()
      const styleId = payload.styleId || 'wuxia'
      const project: SeriesProject = {
        id: `project-${Date.now()}`, title: payload.title || '未命名连续剧', logline: payload.logline || '', masterScript: payload.masterScript || '', isFavorite: payload.isFavorite === true, continuitySummary: payload.continuitySummary || '', translationGlossary: payload.translationGlossary || {}, bibleImagePromptConfig: payload.bibleImagePromptConfig || defaultBibleImagePromptConfig(stylePresets.find((item) => item.id === styleId)), styleId, status: 'draft', aspectRatio: payload.aspectRatio || '9:16', resolution: payload.resolution || '720p', defaultMode: payload.defaultMode || 'Ref2VA',
        bible: payload.bible || { characters: [], scenes: [], props: [], negativePrompt: '身份漂移，面部变化，服装变化，低清晰度，文字水印', colorScript: '' }, episodes: [], episodeCount: 0, completedEpisodeCount: 0, totalDurationSeconds: 0, detailLoaded: true, createdAt: timestamp, updatedAt: timestamp
      }
      projects.unshift(project); saveLocal('cineforge_projects', projects); return clone(project)
    })
  },
  async updateProject(project: SeriesProject): Promise<SeriesProject> {
    return fromConfiguredSource(async () => {
      const patched = unwrap<Record<string, unknown>>((await api.patch(`/projects/${project.id}/`, projectPayload(project))).data)
      const bibleSyncProject = { ...project, updatedAt: String(patched.updated_at || project.updatedAt) }
      await syncBible(bibleSyncProject)
      return adaptProject(unwrap<Record<string, unknown>>(
        (await api.get(`/projects/${project.id}/`)).data,
      ))
    }, () => {
      const projects = loadLocal('cineforge_projects', mockProjects)
      const next = { ...project, updatedAt: new Date().toISOString() }
      const index = projects.findIndex((item) => item.id === project.id)
      if (index >= 0) projects[index] = next; else projects.unshift(next)
      saveLocal('cineforge_projects', projects); return clone(next)
    })
  },
  async setProjectFavorite(project: SeriesProject, favorite: boolean): Promise<SeriesProject> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, unknown>>((await api.patch(`/projects/${project.id}/`, { is_favorite: favorite })).data)
      return { ...project, isFavorite: raw.is_favorite === true, updatedAt: String(raw.updated_at || project.updatedAt) }
    }, () => {
      const projects = loadLocal('cineforge_projects', mockProjects)
      const index = projects.findIndex((item) => item.id === project.id)
      if (index < 0) throw new Error('项目不存在')
      projects[index] = { ...projects[index], isFavorite: favorite, updatedAt: new Date().toISOString() }
      saveLocal('cineforge_projects', projects)
      return clone(projects[index])
    })
  },
  async deleteProject(projectId: string): Promise<void> {
    return fromConfiguredSource(async () => { await api.delete(`/projects/${projectId}/`) }, () => {
      const workflows = loadLocal('cineforge_workflows', mockWorkflows)
      const activeWorkflow = workflows.some((workflow) => (
        workflow.projectId === projectId && ['queued', 'running', 'paused'].includes(workflow.status)
      ))
      if (activeWorkflow) throw new Error('项目仍有排队、运行或暂停中的任务，请等待任务结束后再删除')
      const projects = loadLocal('cineforge_projects', mockProjects)
      const next = projects.filter((item) => item.id !== projectId)
      if (next.length === projects.length) throw new Error('项目不存在')
      saveLocal('cineforge_projects', next)
      saveLocal('cineforge_workflows', workflows.filter((workflow) => workflow.projectId !== projectId))
    })
  },
  async addEpisode(projectId: string, payload: Pick<Episode, 'title' | 'synopsis'> & { script?: string }): Promise<Episode> {
    return fromConfiguredSource(async () => adaptCreatedEpisode(unwrap<Record<string, unknown>>((await api.post(`/projects/${projectId}/next-episode/`, payload)).data)), () => {
      const projects = loadLocal('cineforge_projects', mockProjects)
      const project = projects.find((item) => item.id === projectId)
      if (!project) throw new Error('项目不存在')
      const number = (project.episodes[project.episodes.length - 1]?.number || 0) + 1
      const episode: Episode = { id: `episode-${Date.now()}`, number, title: payload.title || `第 ${number} 集`, synopsis: payload.synopsis || '', script: payload.script || '', status: 'draft', multiShot: true, audioEnabled: true, shots: [], createdAt: new Date().toISOString() }
      project.episodes.push(episode); project.updatedAt = new Date().toISOString(); saveLocal('cineforge_projects', projects); return clone(episode)
    })
  },
  async deleteEpisode(projectId: string, episodeId: string): Promise<void> {
    return fromConfiguredSource(async () => { await api.delete(`/episodes/${episodeId}/`) }, () => {
      const projects = loadLocal('cineforge_projects', mockProjects); const project = projects.find((item) => item.id === projectId)
      if (project) { project.episodes = project.episodes.filter((item) => item.id !== episodeId); saveLocal('cineforge_projects', projects) }
    })
  },
  async saveEpisode(project: SeriesProject, episode: Episode): Promise<Episode> {
    return fromConfiguredSource(async () => syncEpisode(project, episode), () => {
      const projects = loadLocal('cineforge_projects', mockProjects); const storedProject = projects.find((item) => item.id === project.id)
      if (!storedProject) throw new Error('项目不存在')
      const index = storedProject.episodes.findIndex((item) => item.id === episode.id); if (index >= 0) storedProject.episodes[index] = episode; else storedProject.episodes.push(episode)
      storedProject.updatedAt = new Date().toISOString(); saveLocal('cineforge_projects', projects); return clone(episode)
    })
  },
  async saveShot(project: SeriesProject, episode: Episode, shot: Shot): Promise<Shot> {
    return fromConfiguredSource(async () => syncShot(project, episode, shot), () => {
      const projects = loadLocal('cineforge_projects', mockProjects)
      const storedProject = projects.find((item) => item.id === project.id)
      const storedEpisode = storedProject?.episodes.find((item) => item.id === episode.id)
      if (!storedProject || !storedEpisode) throw new Error('项目或剧集不存在')
      const index = storedEpisode.shots.findIndex((item) => item.id === shot.id)
      if (index >= 0) storedEpisode.shots[index] = clone(shot); else storedEpisode.shots.push(clone(shot))
      storedProject.updatedAt = new Date().toISOString()
      saveLocal('cineforge_projects', projects)
      return clone(shot)
    })
  },
  async breakdownScript(
    project: SeriesProject,
    options: ScriptBreakdownOptions,
    continueWithoutScriptBible = false,
  ): Promise<ScriptBreakdownResult> {
    return fromConfiguredSource(async () => {
      if (!isBackendId(project.id)) throw new Error('当前项目不是后端项目，无法执行真实剧本拆解')
      const response = unwrap<Record<string, unknown>>((await api.post(`/projects/${project.id}/breakdown-script/`, {
        script: project.masterScript,
        episode_count_hint: options.episodeCountHint,
        default_shot_duration_ms: Math.round(options.defaultShotDuration * 1000),
        multi_shot: options.multiShot,
        include_audio: options.includeAudio,
        default_first_frame_continuity: options.defaultFirstFrameContinuity,
        default_last_frame_continuity: options.defaultLastFrameContinuity,
        replace_existing: options.replaceExisting,
        discard_existing_production: options.discardExistingProduction,
        continue_without_script_bible: continueWithoutScriptBible,
      })).data)
      const counts = response.counts && typeof response.counts === 'object'
        ? response.counts as Record<string, unknown>
        : {}
      const cleared = response.cleared && typeof response.cleared === 'object'
        ? response.cleared as Record<string, unknown>
        : {}
      return {
        project: await adaptProject(unwrap<Record<string, unknown>>((await api.get(`/projects/${project.id}/`)).data)),
        method: String(response.method || 'deterministic-script-breakdown-v3'),
        warnings: Array.isArray(response.warnings) ? response.warnings.map(String) : [],
        counts: {
          episodes: Number(counts.episodes || 0),
          shots: Number(counts.shots || 0),
        },
        cleared: {
          episodes: Number(cleared.episodes || 0),
          shots: Number(cleared.shots || 0),
          generationTasks: Number(cleared.generation_tasks || 0),
          assemblyJobs: Number(cleared.assembly_jobs || 0),
          promptArtifacts: Number(cleared.prompt_artifacts || 0),
          generatedWorkflows: Number(cleared.generated_workflows || 0),
          creativeDrafts: Number(cleared.creative_drafts || 0),
          shotDetailDrafts: Number(cleared.shot_detail_drafts || 0),
          shotDetailBatches: Number(cleared.shot_detail_batches || 0),
          shotDetailBatchItems: Number(cleared.shot_detail_batch_items || 0),
          promptFieldTranslations: Number(cleared.prompt_field_translations || 0),
          draftReleaseBundles: Number(cleared.draft_release_bundles || 0),
          physicalOutputsRetained: cleared.physical_outputs_retained !== false,
        },
      }
    }, () => mockBreakdown(clone(project), options))
  },
  async generatePrompt(project: SeriesProject, episode: Episode, shot: Shot): Promise<H3PromptPayload> {
    return fromConfiguredSource(async () => {
      const isVideo = shot.mediaType !== 'image'
      const safeDuration = Number(shot.duration)
      if (isVideo && (!Number.isFinite(safeDuration) || safeDuration <= 0)) throw new Error('镜头时长必须是正数；允许范围以生产能力配置为准')
      const artifacts = unwrap<Array<Record<string, any>>>((await api.post('/prompts/generate/', { project_id: project.id, episode_id: episode.id, shot_ids: [shot.id], prompt_type: isVideo ? 'video' : 'image', h3_mode: shot.mode, reference_bindings: isVideo ? referenceBindingsPayload(shot.referenceBindings) : {}, reference_asset_ids: referenceAssetIdsForShot(shot), multi_shot: false, include_audio: isVideo && shot.audioEnabled, shot_durations: isVideo ? [{ shot_id: shot.id, duration_ms: Math.round(safeDuration * 1000) }] : [], overall_soundscape: isVideo ? shot.audio.soundscape : '', non_diegetic_music: isVideo ? shot.audio.music : '', extra_direction: '' })).data)
      const artifact = artifacts[0]
      if (!artifact) throw new Error('服务端未返回提示词产物')
      const payload = adaptPromptArtifact(artifact)
      return {
        ...payload,
        ...(isVideo && shot.audioEnabled ? {
          overall_soundscape: payload.overall_soundscape || shot.audio.soundscape,
          non_diegetic_music: payload.non_diegetic_music || shot.audio.music
        } : {})
      }
    }, () => {
      const isVideo = shot.mediaType !== 'image'
      const style = stylePresets.find((item) => item.id === project.styleId)
      const characters = project.bible.characters.filter((item) => shot.characterIds.includes(item.id))
      const scene = project.bible.scenes.find((item) => item.id === shot.sceneId)
      const props = project.bible.props.filter((item) => shot.propIds.includes(item.id))
      const bindings = referenceBindingsPayload(shot.referenceBindings)
      const effectiveMode:H3Mode = shot.mode === 'FL2VA'
        ? bindings.first_frame_asset_id && bindings.last_frame_asset_id ? 'FL2VA' : bindings.first_frame_asset_id ? 'I2VA' : bindings.last_frame_asset_id ? 'L2VA' : 'T2VA'
        : shot.mode
      const assetName=(assetId?:string)=>mockAssets.find((item)=>item.id===assetId)?.name||assetId||'未命名素材'
      const referenceManifest:Array<Record<string,unknown>>=[]
      const pictureByEntity=new Map<string,string>()
      if(shot.mode==='FL2VA'){
        const frames=[
          bindings.first_frame_asset_id?{category:'first_frame',assetId:String(bindings.first_frame_asset_id)}:null,
          bindings.last_frame_asset_id?{category:'last_frame',assetId:String(bindings.last_frame_asset_id)}:null
        ].filter(Boolean) as Array<{category:string;assetId:string}>
        frames.forEach((item,index)=>referenceManifest.push({category:item.category,asset_id:item.assetId,input_slot:index+1,input_label:`<Picture ${index+1}>`,name:assetName(item.assetId)}))
      }else if(shot.mode==='Ref2VA'){
        const imageRefs:Array<{category:'character'|'scene'|'prop';entityId:string;assetId:string;name:string}>=[]
        for(const item of characters){const binding=shot.referenceBindings?.characters?.find((value)=>value.entityId===item.id);if(binding)imageRefs.push({category:'character',entityId:item.id,assetId:binding.assetId,name:item.name})}
        if(scene&&shot.referenceBindings?.scene?.entityId===scene.id)imageRefs.push({category:'scene',entityId:scene.id,assetId:shot.referenceBindings.scene.assetId,name:scene.name})
        for(const item of props){const binding=shot.referenceBindings?.props?.find((value)=>value.entityId===item.id);if(binding)imageRefs.push({category:'prop',entityId:item.id,assetId:binding.assetId,name:item.name})}
        imageRefs.forEach((item,index)=>{const label=`<Picture ${index+1}>`;pictureByEntity.set(`${item.category}:${item.entityId}`,label);referenceManifest.push({category:item.category,entity_id:item.entityId,entity_name:item.name,asset_id:item.assetId,input_slot:index+1,input_label:label,name:assetName(item.assetId)})})
        let audioOrdinal=1
        for(const [index,item] of (shot.referenceBindings?.videos||[]).entries()){
          const audioLabel=item.useEmbeddedAudio||item.audioAssetId?`<Audio ${audioOrdinal++}>`:undefined
          referenceManifest.push({category:'video',asset_id:item.assetId,input_slot:index+1,input_label:`<Video ${index+1}>`,name:assetName(item.assetId),use_embedded_audio:item.useEmbeddedAudio,audio_asset_id:item.audioAssetId,audio_input_label:audioLabel})
        }
        for(const [index,item] of (shot.referenceBindings?.audios||[]).entries())referenceManifest.push({category:'audio',asset_id:item.assetId,input_slot:index+1,input_label:`<Audio ${audioOrdinal++}>`,name:assetName(item.assetId)})
      }
      const subjectDefinitions:string[]=[];const retentionAnalysis:string[]=[]
      for(const item of characters){const label=pictureByEntity.get(`character:${item.id}`);subjectDefinitions.push(label?`${label} 仅定义人物 ${item.name}：${item.visualPrompt}`:`[Character ${item.stableSpeakerId||item.id}] 仅使用连续性 Bible 文字定义，无 Picture 绑定：${item.visualPrompt}`);retentionAnalysis.push(`${label||`[Character ${item.stableSpeakerId||item.id}]`}：严格保留 ${item.name} 的脸型、发型、体态、服装颜色和材质。`)}
      if(scene){const label=pictureByEntity.get(`scene:${scene.id}`);subjectDefinitions.push(label?`${label} 仅定义场景 ${scene.name}：${scene.visualPrompt}`:`[Environment ${scene.slug||scene.id}] 仅使用连续性 Bible 文字定义，无 Picture 绑定：${scene.visualPrompt}`);retentionAnalysis.push(`${label||`[Environment ${scene.slug||scene.id}]`}：保持场景布局、光线、时段和色彩。`)}
      for(const item of props){const label=pictureByEntity.get(`prop:${item.id}`);subjectDefinitions.push(label?`${label} 仅定义道具 ${item.name}：${item.visualPrompt}`:`[Prop ${item.slug||item.id}] 仅使用连续性 Bible 文字定义，无 Picture 绑定：${item.visualPrompt}`);retentionAnalysis.push(`${label||`[Prop ${item.slug||item.id}]`}：保持轮廓、材质、比例、颜色和连续状态。`)}
      for(const item of referenceManifest.filter((value)=>value.category==='video')){if(item.audio_input_label)subjectDefinitions.push(`${item.audio_input_label} 是 ${item.input_label} 的配对音轨。`);subjectDefinitions.push(`${item.input_label} 仅提供参考视频的动作、运镜和节奏：${item.name}。`);retentionAnalysis.push(`${item.input_label}：只保留声明的动作与镜头语言，不覆盖人物、场景或道具身份。`)}
      for(const item of referenceManifest.filter((value)=>value.category==='audio')){subjectDefinitions.push(`${item.input_label} 是独立参考音频：${item.name}。`);retentionAnalysis.push(`${item.input_label}：只保留声明的声学特征，不转移视觉属性。`)}
      const formalDetail = shot.detailedDescription?.trim()
      const detailedDescription = [formalDetail || `[Shot 1] ${shot.framing}，${shot.camera}。${shot.action}`]
      const baseDescription = `[${style?.promptPrefix || style?.name}] ${formalDetail || `${shot.framing}，${shot.camera}。${shot.action}`}。角色锁定：${characters.map((item) => `${item.name}（${item.visualPrompt}，seed ${item.seed}）`).join('；') || '无指定角色'}。场景锁定：${scene ? `${scene.name}（${scene.visualPrompt}）` : '依照全剧场景圣经'}。关键道具：${props.map((item) => item.visualPrompt).join('；') || '无'}。画幅 ${project.aspectRatio}，${project.resolution}，${isVideo ? '电影级运镜与时间连续性，动作前后状态严格衔接' : '高质量连续性参考定帧，静态构图、人物身份与材质严格锁定'}。`
      const framePromptKey=effectiveMode==='T2VA'?'h3_i2v_zero_frame_v1':effectiveMode==='I2VA'?'h3_i2v_first_frame_v1':effectiveMode==='L2VA'?'h3_i2v_last_frame_v1':'h3_i2v_first_last_v1'
      return {
        model: isVideo ? 'MiniMax H3' : 'SDXL', mediaType: isVideo ? 'video' : 'image', ...(isVideo ? { mode: shot.mode, effective_conditioning_mode: effectiveMode, prompt_template_key: shot.mode==='Ref2VA'?(referenceManifest.length?'h3_r2v_classified_v1':'h3_r2v_zero_reference_v1'):shot.mode==='T2VA'?'h3_t2v_text_v1':framePromptKey, reference_bindings: referenceManifest, duration: shot.duration } : {}),
        prompt_text: baseDescription,
        integrated_multimodal_description: baseDescription,
        ...(isVideo && shot.mode === 'Ref2VA' ? {
          subject_definitions: subjectDefinitions,
          summary: episode.synopsis || project.logline,
          retention_analysis: retentionAnalysis,
          detailed_description: detailedDescription
        } : {}),
        ...(isVideo && shot.audioEnabled ? { overall_soundscape: `${shot.audio.soundscape}${shot.audio.dialogue ? `；对白：${shot.audio.dialogue}` : ''}`, non_diegetic_music: shot.audio.music } : {}),
        negative_prompt: `${project.bible.negativePrompt}；${shot.negativePrompt}`,
        continuity: { series_id: project.id, episode: episode.number, shot: shot.order, character_seeds: Object.fromEntries(characters.map((item) => [item.id, item.seed])), reference_assets: referenceManifest.flatMap((item)=>[item.asset_id,item.audio_asset_id]).filter(Boolean), color_script: project.bible.colorScript }
      }
    })
  },
  async getWorkflows(): Promise<Workflow[]> {
    return fromConfiguredSource(async () => adaptTasks(await fetchAllPages('/tasks/', { page_size: 500 })), () => loadLocal('cineforge_workflows', mockWorkflows))
  },
  async getShotWorkflows(shotId: string, kind: 'video' | 'image' = 'video'): Promise<Workflow[]> {
    return fromConfiguredSource(async () => {
      if (!isBackendId(shotId)) return []
      return adaptTasks(await fetchAllPages('/tasks/', {
        shot_id: shotId,
        kind,
        ordering: '-queued_at',
        page_size: 100
      }))
    }, () => loadLocal('cineforge_workflows', mockWorkflows)
      .filter((workflow) => (workflow.shotId === shotId || workflow.shotIds?.includes(shotId)) && (workflow.kind || (workflow.nodes[0]?.type === 'image' ? 'image' : 'video')) === kind))
  },
  async getWorkflow(taskId: string): Promise<Workflow> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.get(`/tasks/${taskId}/`)).data)
      const workflow = adaptTasks([raw])[0]
      if (!workflow) throw new Error('服务端未返回任务详情')
      return workflow
    }, () => {
      const workflow = loadLocal('cineforge_workflows', mockWorkflows).find((item) => (item.taskId || item.id) === taskId)
      if (!workflow) throw new Error('没有找到任务详情')
      return clone(workflow)
    })
  },
  async getDeferredGenerationRequests(filters: DeferredGenerationRequestFilters = {}): Promise<DeferredGenerationRequest[]> {
    return fromConfiguredSource(async () => {
      const params: Record<string, unknown> = { page_size: 100 }
      if (filters.project) params.project = filters.project
      if (filters.sourceTask) params.source_task = filters.sourceTask
      if (filters.targetShot) params.target_shot = filters.targetShot
      if (filters.status) params.status = filters.status
      if (filters.frameSlot) params.frame_slot = filters.frameSlot
      return (await fetchAllPages<unknown>('/deferred-generation-requests/', params))
        .map(adaptDeferredGenerationRequest)
    }, () => [])
  },
  async createDeferredGenerationRequest(input: DeferredGenerationCreateInput): Promise<DeferredGenerationRequest> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<unknown>((await api.post(
        '/deferred-generation-requests/',
        deferredGenerationCreatePayload(input),
      )).data)
      return adaptDeferredGenerationRequest(raw)
    }, () => { throw new Error('Mock 模式不创建延迟连续生成请求') })
  },
  async cancelDeferredGenerationRequest(requestId: string): Promise<DeferredGenerationRequest> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<unknown>((await api.post(
        `/deferred-generation-requests/${requestId}/cancel/`,
        {},
      )).data)
      return adaptDeferredGenerationRequest(raw)
    }, () => { throw new Error('Mock 模式没有可取消的延迟连续生成请求') })
  },
  async publishTaskOutput(taskId: string, outputIndex: number): Promise<Workflow> {
    return fromConfiguredSource(async () => {
      try {
        const raw = unwrap<Record<string, any>>((await api.post(
          `/tasks/${taskId}/publish-output/`,
          { output_index: outputIndex },
          { timeout: TASK_DISTRIBUTION_TIMEOUT_MS },
        )).data)
        const workflow = adaptTasks([raw])[0]
        if (!workflow) throw new Error('服务端未返回更新后的任务详情')
        return workflow
      } catch (error) {
        if (!isAmbiguousPublicationFailure(error)) throw error
        let latest: Workflow | undefined
        for (let attempt = 0; attempt < TASK_DISTRIBUTION_RECONCILE_ATTEMPTS; attempt += 1) {
          if (attempt) await sleep(2_000)
          try {
            const raw = unwrap<Record<string, any>>((await api.get(`/tasks/${taskId}/`)).data)
            latest = adaptTasks([raw])[0]
            const output = latest?.outputMedia?.find((item) => item.index === outputIndex)
            if (output?.distributionStatus === 'ready' && output.downloadUrl) return latest
            if (output?.distributionStatus === 'failed') {
              throw new Error(output.distributionError || '公开发行副本生成失败')
            }
          } catch (reconcileError) {
            if (!isAmbiguousPublicationFailure(reconcileError)) throw reconcileError
          }
        }
        throw new Error(
          '公开副本请求超时，服务端可能仍在完成原子发布；请刷新任务详情确认，已验证副本出现后即可公开下载。',
        )
      }
    }, () => {
      throw new Error('Mock 模式不生成可公开发行的媒体副本')
    })
  },
  async promoteTaskOutputToAsset(
    taskId: string,
    outputIndex: number,
    name = '',
    target?: GeneratedImageAssetTarget,
    options: { acceptStaleBibleImageCandidate?: boolean } = {},
  ): Promise<Asset> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.post(
        `/tasks/${taskId}/promote-output-to-asset/`,
        buildGeneratedImageAssetPromotionPayload(outputIndex, name, target, options),
      )).data)
      const asset = adaptAssets([raw])[0]
      if (!asset) throw new Error('服务端未返回可用的图片素材')
      return asset
    }, () => { throw new Error('Mock 模式不保存真实生成产物') })
  },
  async getTaskInputPrompt(taskId: string): Promise<TaskInputPrompt> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.get(`/tasks/${taskId}/input-prompt/`)).data)
      const prompt = String(raw.prompt || '')
      const negativePrompt = String(raw.negative_prompt || '')
      return {
        taskId: String(raw.task_id || taskId),
        promptArtifactId: String(raw.prompt_artifact_id || '') || undefined,
        promptType: String(raw.prompt_type || '') || undefined,
        model: String(raw.model || '') || undefined,
        h3Mode: String(raw.h3_mode || '') || undefined,
        effectiveConditioningMode: String(raw.effective_conditioning_mode || '') || undefined,
        promptTemplateKey: String(raw.prompt_template_key || '') || undefined,
        contentHash: String(raw.content_hash || '') || undefined,
        prompt,
        negativePrompt,
        promptCharacterCount: Number(raw.prompt_character_count ?? prompt.length),
        negativePromptCharacterCount: Number(raw.negative_prompt_character_count ?? negativePrompt.length),
        promptSource: String(raw.prompt_source || '') || undefined,
        negativePromptSource: String(raw.negative_prompt_source || '') || undefined
      }
    }, () => {
      const workflow = loadLocal('cineforge_workflows', mockWorkflows).find((item) => (item.taskId || item.id) === taskId)
      const parameters = workflow?.inputParameters || {}
      const prompt = String(parameters.prompt || parameters.prompt_text || '')
      const negativePrompt = String(parameters.negative_prompt || '')
      if (!prompt && !negativePrompt) throw new Error('Mock 任务没有服务端实际提交提示词')
      return { taskId, prompt, negativePrompt, promptCharacterCount: prompt.length, negativePromptCharacterCount: negativePrompt.length }
    })
  },
  async getGeneratedWorkflowExport(task: Workflow, workflowFormat: 'api' | 'ui' = 'ui'): Promise<GeneratedWorkflowExport> {
    return fromConfiguredSource(async () => {
      if (!task.generatedWorkflowId) throw new Error('该任务未关联可查看的 generated_workflow')
      let raw: Record<string, any>
      try {
        raw = unwrap<Record<string, any>>((await api.get(
          `/tasks/${task.taskId || task.id}/comfyui-workflow/`,
          { params: { workflow_format: workflowFormat } }
        )).data)
      } catch (error: any) {
        if (error?.response?.status !== 404) throw error
        const workflow = unwrap<Record<string, any>>((await api.get(`/workflows/${task.generatedWorkflowId}/`)).data)
        raw = { task_id: task.taskId || task.id, generated_workflow_id: workflow.id, workflow: workflow.workflow_json, injected_values: workflow.injected_values, content_hash: workflow.content_hash, filename: `comfyui-workflow-${workflow.id}.json`, format: 'comfyui_api', safe_to_open: false, direct_open_supported: false, open_mode: 'download_import' }
      }
      const workflowJson = raw.workflow || raw.workflow_json
      if (!workflowJson || typeof workflowJson !== 'object' || Array.isArray(workflowJson)) throw new Error('服务端未返回有效的 ComfyUI 工作流 JSON')
      const safeToOpen = raw.safe_to_open === true
      return {
        taskId: String(raw.task_id || task.taskId || task.id),
        generatedWorkflowId: String(raw.generated_workflow_id || raw.id || task.generatedWorkflowId),
        workflowJson,
        injectedValues: raw.injected_values && typeof raw.injected_values === 'object' ? raw.injected_values : {},
        contentHash: String(raw.content_hash || ''),
        filename: String(raw.filename || `comfyui-workflow-${task.generatedWorkflowId}.json`),
        format: String(raw.format || 'comfyui_api'),
        templateSlug: String(raw.template_slug || '') || undefined,
        templateVersion: Number(raw.template_version || 0) || undefined,
        importReady: raw.import_ready !== false,
        comfyPromptId: task.comfyPromptId,
        comfyuiUrl: String(raw.comfyui_url || '') || undefined,
        directOpenUrl: String(raw.direct_open_url || '') || undefined,
        safeToOpen,
        directOpenSupported: raw.direct_open_supported === true,
        openMode: String(raw.open_mode || 'download_import'),
        downloadUrl: String(raw.download_url || '') || undefined,
        availableFormats: Array.isArray(raw.available_formats)
          ? raw.available_formats.filter((value: unknown) => value === 'comfyui_api' || value === 'comfyui_ui')
          : undefined,
        apiDownloadUrl: String(raw.api_download_url || '') || undefined,
        uiDownloadUrl: String(raw.ui_download_url || '') || undefined,
        containsStagedPlaceholders: raw.contains_staged_placeholders === true,
        stagedPlaceholderCount: Number(raw.staged_placeholder_count || 0),
        openReason: String(raw.open_reason || '') || (safeToOpen
          ? (raw.direct_open_supported === true ? '后端已确认可通过安全地址打开该工作流。' : '请先下载 UI 画布 JSON，再打开 ComfyUI 并拖入或打开该文件；网页不会自动载入画布。')
          : '当前没有可安全打开的 ComfyUI 地址，请先下载工作流 JSON。')
      }
    }, () => ({
      taskId: task.taskId || task.id,
      generatedWorkflowId: task.generatedWorkflowId || `mock-workflow-${task.id}`,
      workflowJson: { mock: true, task_id: task.id, nodes: task.nodes },
      injectedValues: {}, contentHash: '', filename: `comfyui-workflow-${task.id}.json`,
      format: 'comfyui_api', templateSlug: 'mock', templateVersion: 1, importReady: true,
      comfyPromptId: task.comfyPromptId, comfyuiUrl: undefined, directOpenUrl: undefined,
      safeToOpen: false, directOpenSupported: false, openMode: 'download_import', downloadUrl: undefined,
      availableFormats: ['comfyui_api', 'comfyui_ui'], apiDownloadUrl: undefined, uiDownloadUrl: undefined,
      containsStagedPlaceholders: false, stagedPlaceholderCount: 0, openReason: 'Mock 模式不连接真实 ComfyUI。'
    }))
  },
  async syncGeneratedWorkflowToComfyUI(taskId: string): Promise<ComfyUIWorkflowSyncResult> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.post(
        `/tasks/${taskId}/sync-comfyui-workflow/`,
        {},
      )).data)
      return {
        taskId: String(raw.task_id || taskId),
        generatedWorkflowId: String(raw.generated_workflow_id || ''),
        synced: raw.synced === true,
        workflowPath: String(raw.workflow_path || ''),
        filename: String(raw.filename || ''),
        comfyuiUrl: String(raw.comfyui_url || ''),
        openReason: String(raw.open_reason || ''),
      }
    }, () => {
      throw new Error('Mock 模式不会写入本地 ComfyUI 工作流库')
    })
  },
  async createWorkflow(project: SeriesProject, episodeIds: string[], resolutionProfile: ProductionResolutionProfile = 'project', resolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, imageResolutionProfile: ProductionResolutionProfile = '0.98', imageResolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, workflowSelections: ProductionWorkflowSelection[] = [], extendedGenerationConfirmed = false, idempotencyKey = ''): Promise<Workflow> {
    const makeWorkflow = (taskIds: string[] = [], persist = false) => {
      const workflows = loadLocal('cineforge_workflows', mockWorkflows)
      const mockNodes: Workflow['nodes'] = [
        { id: 'script', label: '剧本拆解', type: 'script', status: 'pending', progress: 0 }, { id: 'prompt', label: 'H3 提示词', type: 'prompt', status: 'pending', progress: 0 }, { id: 'reference', label: '一致性参考图', type: 'image', status: 'pending', progress: 0 }, { id: 'video', label: '视频生成', type: 'video', status: 'pending', progress: 0 }, { id: 'audio', label: '原生音频', type: 'audio', status: 'pending', progress: 0 }, { id: 'compose', label: '剪辑合成', type: 'compose', status: 'pending', progress: 0 }, { id: 'export', label: '成片导出', type: 'export', status: 'pending', progress: 0 }
      ]
      const apiNodes: Workflow['nodes'] = [{ id: taskIds[0] || 'queue', label: `${taskIds.length} 个独立生成任务`, type: 'video', status: 'pending', progress: 0 }]
      const flow: Workflow = { id: taskIds[0] || `wf-${Date.now()}`, taskId: taskIds[0], backendStatus: 'queued', projectId: project.id, projectTitle: `${project.title} · ${episodeIds.length ? `${episodeIds.length} 集` : '全剧'}`, status: 'queued', progress: 0, currentStep: taskIds.length ? `已提交 ${taskIds.length} 个 ComfyUI 生成任务` : '等待 ComfyUI 调度', createdAt: new Date().toISOString(), eta: '等待估算', nodes: persist ? mockNodes : apiNodes }
      if (persist) { workflows.unshift(flow); saveLocal('cineforge_workflows', workflows) }
      return clone(flow)
    }
    return fromConfiguredSource(async () => {
      if (!isBackendId(project.id)) throw new Error('当前项目不是后端项目，无法提交真实生产任务')
      const payload = {
        ...(episodeIds.length ? { episode_ids: episodeIds } : {}),
        multi_shot: false,
        enqueue: true,
        idempotency_key: idempotencyKey,
        extended_generation_confirmed: extendedGenerationConfirmed,
        priority: 0,
        max_attempts: 3,
        resolution_profile: resolutionProfile,
        resolution_multiple: normalizeProductionResolutionMultiple(resolutionMultiple),
        image_resolution_profile: imageResolutionProfile,
        image_resolution_multiple: normalizeImageProductionResolutionMultiple(imageResolutionMultiple),
        workflow_overrides: {},
        ...(workflowSelections.length ? { workflow_selections: workflowSelectionsPayload(workflowSelections) } : {})
      }
      const result = unwrap<Record<string, any>>((await api.post(`/projects/${project.id}/build-production/`, payload, { timeout: 0 })).data)
      const taskIds = Array.isArray(result.task_ids)
        ? result.task_ids.map(String)
        : (result.production_units || []).map((unit: Record<string, any>) => unit.task_id).filter(Boolean).map(String)
      const flow = makeWorkflow(taskIds)
      const batchId = String(result.batch_id || '')
      const batchSize = Number(result.batch_size || taskIds.length || 0)
      if (batchId) {
        flow.batchId = batchId
        flow.batchKind = String(result.batch_kind || 'series')
        flow.batchIndex = 1
        flow.batchSize = batchSize
        flow.batchTaskIds = taskIds
        flow.currentStep = `整剧生产批次 1/${batchSize} · 等待调度`
      }
      if (!taskIds.length) flow.currentStep = '工作流已生成，暂无可执行镜头'
      return flow
    }, () => makeWorkflow([], true))
  },
  async generateWholeEpisode(project: SeriesProject, episode: Episode, idempotencyKey: string, resolutionProfile: ProductionResolutionProfile = '0.3', resolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, imageResolutionProfile: ProductionResolutionProfile = '0.98', imageResolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, workflowSelections: ProductionWorkflowSelection[] = [], extendedGenerationConfirmed = false): Promise<Workflow> {
    const shotVersionKey = normalizeShotVersionScopeKey(episode.activeShotVersionKey, episode.id)
    const workflowFromUnit = (taskId: string, unit: Record<string, any>, index: number, taskIds: string[], batchId: string, batchSlots: Workflow['batchSlots'] = []): Workflow => {
      const shotIds = Array.isArray(unit.shot_ids) ? unit.shot_ids.map(String) : []
      const shot = episode.shots.find((item) => shotIds.includes(item.id))
      const shotNumber = Number(unit.shot_number || shot?.order || index + 1)
      const shotTitle = String(unit.shot_title || shot?.title || `镜头 ${String(shotNumber).padStart(2, '0')}`)
      const kind = String(unit.media_type || shot?.mediaType || 'video')
      const batchIndex = Number(unit.batch_index || index + 1)
      const batchSize = Number(unit.batch_size || episode.shots.length || taskIds.length)
      const unitTaskId = String(taskId || unit.task_id || '')
      const workflowMode = String(unit.workflow_mode || '')
      const deferredStatus = String(unit.deferred_generation_status || '')
      const isVirtual = !unitTaskId
      const shotId = String(unit.shot_id || shotIds[0] || shot?.id || '') || undefined
      const virtualId = `episode-batch-${batchId || idempotencyKey}-${batchIndex}-${shotId || index + 1}`
      const virtualStep = workflowMode === 'waiting_for_deferred_first_frame_chain'
        ? '等待上游镜头完成尾帧抽取链路'
        : workflowMode === 'waiting_for_deferred_first_frame_source'
          ? '等待上一镜尾帧抽取任务物化'
          : '等待上一镜视频完成后抽取尾帧并创建本镜任务'
      return {
        id: unitTaskId || virtualId,
        taskId: unitTaskId || undefined,
        kind: kind === 'image' ? 'image' : 'video',
        backendStatus: isVirtual ? (deferredStatus || workflowMode || 'waiting_for_deferred_first_frame') : 'queued',
        projectId: project.id,
        projectTitle: `${project.title} · 第${episode.number}集 · 镜头${String(shotNumber).padStart(2, '0')} ${shotTitle}`,
        status: 'queued', progress: 0,
        currentStep: isVirtual ? `整集镜头 ${batchIndex}/${batchSize} · ${virtualStep}` : `整集镜头 ${batchIndex}/${batchSize} · 等待 ComfyUI 调度`,
        createdAt: new Date().toISOString(), eta: '等待估算',
        nodes: [{ id: unitTaskId || virtualId, label: isVirtual ? '尾帧抽取与下游任务占位' : `${kind === 'image' ? '图片' : '视频'}生成任务`, type: isVirtual ? 'compose' : (kind === 'image' ? 'image' : 'video'), status: 'pending', progress: 0 }],
        batchId, batchIndex, batchSize, batchSlots,
        batchKind: String(unit.batch_kind || 'whole_episode'), batchTaskIds: taskIds,
        shotVersionKey: String(unit.shot_version_key || shotVersionKey),
        shotVersionLabel: String(unit.shot_version_label || '') || undefined,
        isVirtualProductionUnit: isVirtual || undefined,
        virtualProductionUnitKind: isVirtual ? (
          workflowMode === 'waiting_for_deferred_first_frame_source'
            ? 'waiting_for_deferred_first_frame_source'
            : workflowMode === 'waiting_for_deferred_first_frame_chain'
              ? 'waiting_for_deferred_first_frame_chain'
              : 'deferred_first_frame_continuity'
        ) : undefined,
        deferredGenerationRequestId: String(unit.deferred_generation_request_id || '') || undefined,
        deferredGenerationStatus: deferredStatus || undefined,
        deferredSourceTaskId: String(unit.deferred_source_task_id || '') || undefined,
        deferredUpstreamRequestId: String(unit.deferred_upstream_request_id || '') || undefined,
        deferredFrameSlot: unit.deferred_frame_slot === 'last_frame' ? 'last_frame' : unit.deferred_frame_slot === 'first_frame' ? 'first_frame' : undefined,
        episodeId: episode.id, episodeNumber: episode.number, episodeTitle: episode.title,
        shotId,
        shotIds: shotIds.length ? shotIds : shot ? [shot.id] : [], shotNumber, shotTitle
      }
    }
    return fromConfiguredSource(async () => {
      if (!isBackendId(project.id) || !isBackendId(episode.id)) throw new Error('请先保存项目和剧集，再生成整集')
      const payload = {
        whole_episode: true,
        episode_ids: [episode.id],
        shot_version_key: shotVersionKey,
        idempotency_key: idempotencyKey,
        multi_shot: false,
        enqueue: true,
        extended_generation_confirmed: extendedGenerationConfirmed,
        priority: 0,
        max_attempts: 3,
        resolution_profile: resolutionProfile,
        resolution_multiple: normalizeProductionResolutionMultiple(resolutionMultiple),
        image_resolution_profile: imageResolutionProfile,
        image_resolution_multiple: normalizeImageProductionResolutionMultiple(imageResolutionMultiple),
        workflow_overrides: {},
        ...(workflowSelections.length ? { workflow_selections: workflowSelectionsPayload(workflowSelections) } : {})
      }
      const result = unwrap<Record<string, any>>((await api.post(`/projects/${project.id}/build-production/`, payload, { timeout: 0 })).data)
      const units = Array.isArray(result.production_units) ? result.production_units as Record<string, any>[] : []
      const batchSlots = adaptTaskBatchSlots(result.batch_slots)
      const taskIds = Array.isArray(result.task_ids)
        ? result.task_ids.map(String)
        : units.map((unit) => unit.task_id).filter(Boolean).map(String)
      if (!taskIds.length && !units.length) throw new Error('服务端未创建整集分镜任务')
      const batchId = String(result.batch_id || units[0]?.batch_id || idempotencyKey)
      const sourceUnits = units.length ? units : taskIds.map((taskId, index) => ({ task_id: taskId, batch_index: index + 1, batch_size: taskIds.length }))
      const responseVersionKey = String(result.shot_version_key || '')
      if (responseVersionKey && responseVersionKey !== shotVersionKey) {
        throw new Error('服务端返回的整集批次不属于当前分镜版本，请刷新后重试')
      }
      const flows = sourceUnits.map((unit, index) => workflowFromUnit(String(unit.task_id || ''), {
        shot_version_key: responseVersionKey || shotVersionKey,
        shot_version_label: result.shot_version_label,
        ...unit,
      }, index, taskIds, batchId, batchSlots))
      const primary = flows.find((item) => item.taskId) || flows[0]
      return clone({ ...primary, batchWorkflows: flows })
    }, () => {
      if (!episode.shots.length) throw new Error('本集还没有分镜，无法生成整集')
      const batchId = `mock-episode-batch-${Date.now()}`
      const taskIds = episode.shots.map((_, index) => `${batchId}-${index + 1}`)
      const flows = episode.shots.map((shot, index) => workflowFromUnit(taskIds[index], { shot_ids: [shot.id], media_type: shot.mediaType, batch_index: index + 1, batch_size: taskIds.length }, index, taskIds, batchId))
      const workflows = loadLocal('cineforge_workflows', mockWorkflows)
      saveLocal('cineforge_workflows', [...flows, ...workflows])
      return clone({ ...flows[0], batchWorkflows: flows })
    })
  },
  async regenerateShot(project: SeriesProject, episode: Episode, shot: Shot, resolutionProfile: ProductionResolutionProfile = '0.3', resolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, workflowSelections: ProductionWorkflowSelection[] = [], extendedGenerationConfirmed = false): Promise<{ taskId: string; taskIds: string[] }> {
    return fromConfiguredSource(async () => {
      if (!isBackendId(project.id) || !isBackendId(episode.id) || !isBackendId(shot.id)) {
        throw new Error('请先保存项目和分镜，再提交真实视频生成任务')
      }
      const payload = {
        episode_ids: [episode.id],
        shot_ids: [shot.id],
        multi_shot: false,
        include_audio: shot.mediaType === 'video' && shot.audioEnabled,
        h3_mode: shot.mode,
        enqueue: true,
        extended_generation_confirmed: extendedGenerationConfirmed,
        priority: 0,
        max_attempts: 3,
        resolution_profile: resolutionProfile,
        resolution_multiple: normalizeProductionResolutionMultiple(resolutionMultiple),
        ...(shot.mediaType === 'image' ? {
          image_resolution_profile: resolutionProfile,
          image_resolution_multiple: normalizeImageProductionResolutionMultiple(resolutionMultiple)
        } : {}),
        workflow_overrides: {},
        ...(workflowSelections.length ? { workflow_selections: workflowSelectionsPayload(workflowSelections) } : {})
      }
      const result = unwrap<Record<string, any>>((await api.post(`/projects/${project.id}/build-production/`, payload, { timeout: 0 })).data)
      const taskIds = Array.isArray(result.task_ids)
        ? result.task_ids.map(String)
        : (result.production_units || []).map((unit: Record<string, any>) => unit.task_id).filter(Boolean).map(String)
      if (!taskIds.length) throw new Error(`服务端未创建镜头${shot.mediaType === 'image' ? '图片' : '视频'}生成任务`)
      return { taskId: taskIds[0], taskIds }
    }, () => {
      const taskId = `mock-shot-task-${Date.now()}`
      return { taskId, taskIds: [taskId] }
    })
  },
  async cancelGenerationTask(taskId: string): Promise<{ id: string; status: string; warning?: string; downstreamCancellations?: { deferredRequestsCancelled: number; tasksCancelled: number; tasksCancelRequested: number } }> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.post(`/tasks/${taskId}/cancel/`, {})).data)
      const downstream = raw.downstream_cancellations && typeof raw.downstream_cancellations === 'object'
        ? raw.downstream_cancellations as Record<string, unknown>
        : {}
      return {
        id: String(raw.id || taskId),
        status: String(raw.status || 'cancel_requested'),
        warning: String(raw.warning || '') || undefined,
        downstreamCancellations: {
          deferredRequestsCancelled: Math.max(0, Number(downstream.deferred_requests_cancelled || 0) || 0),
          tasksCancelled: Math.max(0, Number(downstream.tasks_cancelled || 0) || 0),
          tasksCancelRequested: Math.max(0, Number(downstream.tasks_cancel_requested || 0) || 0),
        },
      }
    }, () => {
      const workflows = loadLocal('cineforge_workflows', mockWorkflows)
      const original = workflows.find((item) => item.id === taskId)
      if (!original || !['queued', 'running', 'paused'].includes(original.status)) throw new Error('当前任务不能取消')
      original.backendStatus = 'cancelled'
      original.status = 'failed'
      original.progress = 0
      original.currentStep = '任务已取消，可重新创建重试任务'
      original.finishedAt = new Date().toISOString()
      original.errorMessage = '用户已取消任务'
      saveLocal('cineforge_workflows', workflows)
      return { id: taskId, status: 'cancelled' }
    })
  },
  async cancelGenerationBatch(taskId: string): Promise<ProductionBatchCancelResult> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.post(`/tasks/${taskId}/cancel-batch/`, {})).data)
      const skipped = Array.isArray(raw.skipped)
        ? raw.skipped
          .filter((item: unknown): item is Record<string, any> => Boolean(item && typeof item === 'object'))
          .map((item) => ({
            taskId: String(item.task_id || ''),
            batchIndex: item.batch_index !== null && item.batch_index !== undefined && Number.isSafeInteger(Number(item.batch_index))
              ? Number(item.batch_index)
              : undefined,
            status: String(item.status || ''),
            reason: String(item.reason || ''),
          }))
          .filter((item) => item.taskId)
        : []
      const downstream = raw.downstream_cancellations && typeof raw.downstream_cancellations === 'object'
        ? raw.downstream_cancellations as Record<string, unknown>
        : {}
      return {
        batchId: String(raw.batch_id || ''),
        batchKind: String(raw.batch_kind || '') || undefined,
        scannedTaskCount: Number(raw.scanned_task_count || 0),
        cancelledTaskIds: Array.isArray(raw.cancelled_task_ids)
          ? raw.cancelled_task_ids.map(String).filter(Boolean)
          : [],
        cancelRequestedTaskIds: Array.isArray(raw.cancel_requested_task_ids)
          ? raw.cancel_requested_task_ids.map(String).filter(Boolean)
          : [],
        skipped,
        downstreamCancellations: {
          deferredRequestsCancelled: Math.max(0, Number(downstream.deferred_requests_cancelled || 0) || 0),
          tasksCancelled: Math.max(0, Number(downstream.tasks_cancelled || 0) || 0),
          tasksCancelRequested: Math.max(0, Number(downstream.tasks_cancel_requested || 0) || 0),
        },
        tasks: Array.isArray(raw.tasks) ? adaptTasks(raw.tasks) : [],
      }
    }, () => { throw new Error('Mock 模式不支持取消真实生产批次') })
  },
  async deleteGenerationTask(taskId: string): Promise<{ id: string }> {
    return fromConfiguredSource(async () => {
      await api.delete(`/tasks/${taskId}/`)
      return { id: taskId }
    }, () => {
      const workflows = loadLocal('cineforge_workflows', mockWorkflows)
      const index = workflows.findIndex((item) => (item.taskId || item.id) === taskId)
      if (index < 0) throw new Error('没有找到需要删除的历史任务')
      const target = workflows[index]
      const terminal = ['succeeded', 'failed', 'cancelled'].includes(target.backendStatus || '')
        || (!target.backendStatus && ['completed', 'failed'].includes(target.status))
      if (!terminal) throw new Error('仅已完成、失败或已取消的历史任务可以删除')
      workflows.splice(index, 1)
      saveLocal('cineforge_workflows', workflows)
      return { id: taskId }
    })
  },
  async deleteGenerationBatch(taskId: string): Promise<ProductionBatchDeleteResult> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.delete(`/tasks/${taskId}/delete-batch/`)).data)
      return {
        batchId: String(raw.batch_id || ''),
        batchKind: String(raw.batch_kind || '') || undefined,
        scannedTaskCount: Number(raw.scanned_task_count || 0),
        deletedTaskIds: Array.isArray(raw.deleted_task_ids)
          ? raw.deleted_task_ids.map(String).filter(Boolean)
          : [],
        deletedDeferredGenerationRequestIds: Array.isArray(raw.deleted_deferred_generation_request_ids)
          ? raw.deleted_deferred_generation_request_ids.map(String).filter(Boolean)
          : [],
      }
    }, () => {
      const workflows = loadLocal('cineforge_workflows', mockWorkflows)
      const source = workflows.find((item) => (item.taskId || item.id) === taskId)
      if (!source?.batchId) throw new Error('没有找到需要删除的整集批次')
      const batchItems = workflows.filter((item) => item.batchId === source.batchId)
      if (!batchItems.length) throw new Error('没有找到需要删除的整集批次')
      const nonTerminal = batchItems.find((item) => {
        const terminal = ['succeeded', 'failed', 'cancelled'].includes(item.backendStatus || '')
          || (!item.backendStatus && ['completed', 'failed'].includes(item.status))
        return !terminal
      })
      if (nonTerminal) throw new Error('仅能删除全部进入终态的整集批次')
      const deletedTaskIds = batchItems.map((item) => item.taskId || item.id)
      saveLocal('cineforge_workflows', workflows.filter((item) => item.batchId !== source.batchId))
      return {
        batchId: source.batchId,
        batchKind: source.batchKind,
        scannedTaskCount: batchItems.length,
        deletedTaskIds,
        deletedDeferredGenerationRequestIds: [],
      }
    })
  },
  async retryGenerationTask(taskId: string, extendedGenerationConfirmed = false): Promise<Workflow> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.post(`/tasks/${taskId}/retry/`, {
        extended_generation_confirmed: extendedGenerationConfirmed,
      })).data)
      const workflow = adaptTasks([raw])[0]
      if (!workflow) throw new Error('服务端未返回重试后的任务详情')
      return workflow
    }, () => {
      const workflows = loadLocal('cineforge_workflows', mockWorkflows)
      const original = workflows.find((item) => item.id === taskId)
      if (!original || original.status !== 'failed') throw new Error('仅失败任务可以重试')
      const id = `wf-retry-${Date.now()}`
      const retried: Workflow = {
        ...clone(original), id, taskId: id, backendStatus: 'queued', status: 'queued', progress: 0,
        currentStep: '重试任务已进入队列', createdAt: new Date().toISOString(),
        nodes: original.nodes.map((node) => ({ ...node, status: 'pending', progress: 0 }))
      }
      workflows.unshift(retried); saveLocal('cineforge_workflows', workflows)
      return retried
    })
  },
  async resumeGenerationBatch(taskId: string, extendedGenerationConfirmed = false): Promise<ProductionBatchResumeResult> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.post(`/tasks/${taskId}/resume-batch/`, {
        extended_generation_confirmed: extendedGenerationConfirmed,
      })).data)
      const skipped = Array.isArray(raw.skipped)
        ? raw.skipped
          .filter((item: unknown): item is Record<string, any> => Boolean(item && typeof item === 'object'))
          .map((item) => ({
            taskId: String(item.task_id || ''),
            batchIndex: item.batch_index !== null && item.batch_index !== undefined && Number.isSafeInteger(Number(item.batch_index))
              ? Number(item.batch_index)
              : undefined,
            status: String(item.status || ''),
            reason: String(item.reason || ''),
          }))
          .filter((item) => item.taskId)
        : []
      return {
        batchId: String(raw.batch_id || ''),
        batchKind: String(raw.batch_kind || '') || undefined,
        scannedTaskCount: Number(raw.scanned_task_count || 0),
        retriedTaskIds: Array.isArray(raw.retried_task_ids)
          ? raw.retried_task_ids.map(String).filter(Boolean)
          : [],
        skipped,
        tasks: Array.isArray(raw.tasks) ? adaptTasks(raw.tasks) : [],
      }
    }, () => { throw new Error('Mock 模式不支持恢复真实生产批次') })
  },
  async getEpisodeAssemblies(projectId: string): Promise<AssemblyResult[]> {
    return fromConfiguredSource(async () => {
      if (!isBackendId(projectId)) return []
      const rows = await fetchAllPages<Record<string, any>>('/assemblies/', {
        project: projectId,
        scope: 'episode',
        dry_run: false,
        ordering: '-created_at',
        page_size: 500
      })
      return rows.map(adaptAssembly)
    }, () => [])
  },
  async getAssembly(assemblyId: string): Promise<AssemblyResult> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.get(`/assemblies/${assemblyId}/`)).data)
      return adaptAssembly(raw)
    }, () => ({
      id: assemblyId,
      status: 'succeeded',
      scope: 'episode',
      projectId: '',
      message: 'Mock 模式：本集成片已组装完成'
    }))
  },
  async assembleEpisode(projectId: string, episodeId: string): Promise<AssemblyResult> {
    return fromConfiguredSource(async () => {
      if (!isBackendId(projectId) || !isBackendId(episodeId)) throw new Error('请先保存本集，再执行真实成片组装')
      const raw = unwrap<Record<string, any>>((await api.post(
        '/assemblies/episode/',
        { episode_id: episodeId, dry_run: false, output_name: '' },
        { timeout: ASSEMBLY_ENQUEUE_TIMEOUT_MS }
      )).data)
      return adaptAssembly(raw)
    }, () => ({
      id: `assembly-${Date.now()}`,
      status: 'succeeded',
      scope: 'episode',
      projectId,
      episodeId,
      outputUrl: `/mock-output/${projectId}/${episodeId}.mp4`,
      downloadUrl: `/mock-output/${projectId}/${episodeId}.mp4?download=1`,
      sourceTaskIds: [],
      createdAt: new Date().toISOString(),
      finishedAt: new Date().toISOString(),
      message: 'Mock 模式：本集成片已组装完成'
    }))
  },
  async getReleaseBundles(projectId: string): Promise<ReleaseBundle[]> {
    return fromConfiguredSource(async () => {
      if (!isBackendId(projectId)) return []
      const rows = await fetchAllPages<Record<string, any>>('/release-bundles/', {
        project: projectId,
        ordering: '-version',
        page_size: 100,
      })
      return rows.map(adaptReleaseBundle)
    }, () => [])
  },
  async createReleaseBundle(
    projectId: string,
    payload: { title?: string; notes?: string } = {},
  ): Promise<ReleaseBundle> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.post('/release-bundles/', {
        project_id: projectId,
        title: payload.title || '',
        notes: payload.notes || '',
      })).data)
      return adaptReleaseBundle(raw)
    }, () => ({
      id: `release-${Date.now()}`, projectId, version: 1,
      title: payload.title || 'Mock 发行候选 v1', notes: payload.notes || '',
      status: 'draft', selection: { shots: [], episodes: [] },
      verificationStatus: 'draft', createdAt: new Date().toISOString(),
    }))
  },
  async proposeReleaseSelection(projectId: string): Promise<ReleaseSelection> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.post('/release-bundles/propose/', {
        project_id: projectId,
      })).data)
      return adaptReleaseSelection(raw.selection)
    }, () => ({ shots: [], episodes: [] }))
  },
  async updateReleaseBundle(
    bundleId: string,
    payload: { title?: string; notes?: string; selection?: ReleaseSelection },
  ): Promise<ReleaseBundle> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.patch(`/release-bundles/${bundleId}/`, {
        ...(payload.title !== undefined ? { title: payload.title } : {}),
        ...(payload.notes !== undefined ? { notes: payload.notes } : {}),
        ...(payload.selection ? { selection: releaseSelectionPayload(payload.selection) } : {}),
      })).data)
      return adaptReleaseBundle(raw)
    }, () => ({
      id: bundleId, projectId: '', version: 1, title: payload.title || 'Mock 发行候选',
      notes: payload.notes || '', status: 'draft',
      selection: payload.selection || { shots: [], episodes: [] }, verificationStatus: 'draft',
    }))
  },
  async sealReleaseBundle(bundleId: string): Promise<ReleaseBundle> {
    return fromConfiguredSource(async () => adaptReleaseBundle(
      unwrap<Record<string, any>>((await api.post(`/release-bundles/${bundleId}/seal/`, {})).data),
    ), () => ({
      id: bundleId, projectId: '', version: 1, title: 'Mock 发行候选', notes: '',
      status: 'sealed', selection: { shots: [], episodes: [] }, verificationStatus: 'verified',
    }))
  },
  async releaseReleaseBundle(bundleId: string): Promise<ReleaseBundle> {
    return fromConfiguredSource(async () => adaptReleaseBundle(
      unwrap<Record<string, any>>((await api.post(`/release-bundles/${bundleId}/release/`, {})).data),
    ), () => ({
      id: bundleId, projectId: '', version: 1, title: 'Mock 发行版本', notes: '',
      status: 'released', selection: { shots: [], episodes: [] }, verificationStatus: 'verified',
    }))
  },
  async verifyReleaseBundle(bundleId: string): Promise<{ status: string; signatureVerified: boolean; liveVerified: boolean }> {
    return fromConfiguredSource(async () => {
      const raw = unwrap<Record<string, any>>((await api.get(`/release-bundles/${bundleId}/verify/`)).data)
      return {
        status: String(raw.verification?.status || 'invalid'),
        signatureVerified: raw.verification?.signature_verified === true,
        liveVerified: raw.verification?.live_verified === true,
      }
    }, () => ({ status: 'verified', signatureVerified: true, liveVerified: true }))
  },
  async getSettings(): Promise<ComfySettings> {
    if (useMock) { await sleep(80); return { ...loadLocal('cineforge_comfy_settings', mockSettings), concurrency: 1 } }
    try { await api.get('/comfyui/health/') } catch (error: any) {
      // A 503 means the configured ComfyUI is offline, but settings remain readable.
      if (error?.response?.status !== 503) throw error
    }
    return { host: '由 backend/.env 的 COMFYUI_BASE_URL 管理', installPath: '服务端环境配置（前端只读）', outputPath: '由 COMFYUI_OUTPUT_DIR 管理', workflowPath: '由后端 WorkflowTemplate 管理', concurrency: 1, autoStart: false, apiKey: '' }
  },
  async saveSettings(settings: ComfySettings): Promise<ComfySettings> {
    if (!useMock) return { ...settings, concurrency: 1, autoStart: false, apiKey: '' }
    const safeSettings = { ...settings, concurrency: 1 }
    saveLocal('cineforge_comfy_settings', safeSettings); await sleep(100); return clone(safeSettings)
  },
  async testComfy(host: string): Promise<{ ok: boolean; message: string; latency?: number }> {
    const started = performance.now()
    if (useMock) { await sleep(80); return { ok: true, message: `Mock 模式：连接配置有效（${host}）`, latency: 12 } }
    try {
      const result = unwrap<Record<string, any>>((await api.get('/comfyui/health/')).data)
      const reachable = Boolean(result.reachable)
      const degraded = Boolean(result.degraded)
      const message = degraded
        ? `ComfyUI API 可达，但健康检查异常：${String(result.error || 'system_stats 返回异常')}`
        : reachable
          ? 'Django 服务端已连通其受控 ComfyUI 目标'
          : String(result.error || 'ComfyUI 当前不可达')
      return { ok: reachable && !degraded, message, latency: Math.round(performance.now() - started) }
    } catch (error: any) {
      throw new Error(error?.message || 'ComfyUI 当前不可达')
    }
  }
}
