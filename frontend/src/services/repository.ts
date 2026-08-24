import { api, fetchAllPages, unwrap } from './api'
import type { AssemblyResult, Asset, BibleDraftEntityType, BibleImageCandidatesResult, CanonicalGlossaryEnsureResult, CanonicalGlossaryScope, DeferredGenerationRequest, DeferredGenerationStatus, Episode, GeneratedWorkflowExport, PromptTranslationEntry, PromptTranslationField, ReleaseBundle, ReleaseSelection, SeriesProject, Shot, ShotTranslationPromotionResult, StylePreset, TaskInputPrompt, TranslationCapability, Workflow } from '@/types'
import { adaptAssets, adaptCreatedEpisode, adaptProject, adaptProjectSummary, adaptStyles, adaptTasks, backendStyleId, isBackendId, persistBibleEntities, projectPayload, syncShot } from './backendAdapters'
import { adaptCanonicalGlossaryEnsureResult, adaptShotTranslationPromotionResult, adaptTranslationEntry } from './translationReview'
import { adaptPromptNormalizationCapability } from './promptNormalization'
import { adaptBibleImageCandidates } from './bibleImageCandidates'
import { adaptDeferredGenerationRequest } from './deferredGeneration'
import { adaptAuthenticatedUser, type AuthenticatedUser } from './authTypes'

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

const fileDataUrl = (file: File): Promise<string> => new Promise((resolve, reject) => {
  const reader = new FileReader()
  reader.onload = () => resolve(String(reader.result || ''))
  reader.onerror = () => reject(new Error('无法读取封面文件，请重新选择'))
  reader.readAsDataURL(file)
})

// Enqueue performs tenant/path validation and ffprobe snapshots before the
// durable job is returned. Give long episodes enough time without tying the
// browser to the hours-long FFmpeg execution itself.
// Server-side managed publication may spend up to 100s in FFmpeg. Keep this
// below Gunicorn's 120s ceiling, then reconcile by GET because a proxy/client
// timeout does not prove that the content-addressed publication failed.

export interface ProductionCapability {
  recommendedMaxPending: number
  recommendedMaxActivePerUser: number
  maxConcurrent: number
  configuredConcurrency: number
  deploymentMaxConcurrent: number
  taskTimeoutSeconds: number
  configuredTaskTimeoutSeconds: number
  deploymentTaskTimeoutSeconds: number
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
  configurationPublicSha256: string
  configurationSources: Record<string, string>
}

export interface DeferredGenerationRequestFilters {
  project?: string
  sourceTask?: string
  targetShot?: string
  status?: DeferredGenerationStatus
  frameSlot?: 'first_frame' | 'last_frame'
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
  const configuredConcurrency = positiveInteger('configured_concurrency')
  const deploymentMaxConcurrent = positiveInteger('deployment_max_concurrent')
  const maxConcurrent = positiveInteger('max_concurrent')
  if (maxConcurrent !== Math.min(configuredConcurrency, deploymentMaxConcurrent)) {
    throw new Error('生产策略返回的有效并发与配置/部署上限不一致')
  }
  const configuredTaskTimeoutSeconds = positiveNumber('configured_task_timeout_seconds')
  const deploymentTaskTimeoutSeconds = positiveNumber('deployment_task_timeout_seconds')
  const taskTimeoutSeconds = positiveNumber('task_timeout_seconds')
  if (taskTimeoutSeconds !== Math.min(configuredTaskTimeoutSeconds, deploymentTaskTimeoutSeconds)) {
    throw new Error('生产策略返回的有效任务超时与配置/部署上限不一致')
  }
  const configurationPublicSha256 = String(raw.configuration_public_sha256 || '')
  if (!/^[0-9a-f]{64}$/.test(configurationPublicSha256)) throw new Error('生产策略缺少公开配置指纹')
  const rawSources = raw.configuration_sources
  if (!rawSources || typeof rawSources !== 'object' || Array.isArray(rawSources)) {
    throw new Error('生产策略缺少配置来源')
  }
  const configurationSources = Object.fromEntries(
    Object.entries(rawSources as Record<string, unknown>).map(([key, value]) => [key, String(value || '')]),
  )
  return {
    recommendedMaxPending: positiveInteger('recommended_max_pending'),
    recommendedMaxActivePerUser: positiveInteger('recommended_max_active_per_user'),
    maxConcurrent,
    configuredConcurrency,
    deploymentMaxConcurrent,
    taskTimeoutSeconds,
    configuredTaskTimeoutSeconds,
    deploymentTaskTimeoutSeconds,
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
    configurationPublicSha256,
    configurationSources,
  }
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

const currentUserForNewSession = async (
  tokens: { access: string; refresh: string },
  fallbackName: string,
): Promise<AuthenticatedUser> => {
  localStorage.setItem('cineforge_token', tokens.access)
  localStorage.setItem('cineforge_refresh_token', tokens.refresh)
  try {
    const raw = unwrap<Record<string, any>>((await api.get('/auth/me/')).data)
    return adaptAuthenticatedUser(raw, fallbackName)
  } catch (error) {
    if (localStorage.getItem('cineforge_token') === tokens.access) {
      localStorage.removeItem('cineforge_token')
      localStorage.removeItem('cineforge_refresh_token')
      localStorage.removeItem('cineforge_user')
    }
    throw error
  }
}

export const repository = {
  async getCurrentUser(): Promise<AuthenticatedUser> {
    return (async () => {
      const raw = unwrap<Record<string, any>>((await api.get('/auth/me/')).data)
      return adaptAuthenticatedUser(raw)
    })()
  },
  async getProductionCapability(projectId?: string): Promise<ProductionCapability> {
    return (async () => {
      const raw = unwrap<Record<string, unknown>>(
        (await api.get('/capabilities/production/', {
          params: projectId ? { project_id: projectId } : undefined,
        })).data,
      )
      return adaptProductionCapability(raw)
    })()
  },
  async getTranslationCapability(projectId: string): Promise<TranslationCapability> {
    return (async () => {
      return adaptPromptNormalizationCapability(unwrap<unknown>((await api.get(
        `/projects/${projectId}/capabilities/prompt-normalization/`,
      )).data))
    })()
  },
  async ensureCanonicalGlossary(
    projectId: string,
    options: {
      entries?: Array<{ scopeType: CanonicalGlossaryScope; scopeId: string }>
      includeAllMissing?: boolean
      ensureVisualIdentityTranslation?: boolean
    } = {},
  ): Promise<CanonicalGlossaryEnsureResult> {
    return (async () => {
      const response = await api.post(`/projects/${projectId}/ensure-canonical-glossary/`, {
        entries: options.entries?.map((entry) => ({ scope_type: entry.scopeType, scope_id: entry.scopeId })),
        include_all_missing: options.includeAllMissing === true,
        ensure_visual_identity_translation: options.ensureVisualIdentityTranslation === true,
      }, { timeout: 360_000 })
      return adaptCanonicalGlossaryEnsureResult(response.data)
    })()
  },
  async getProjectTranslations(
    projectId: string,
    filters: { episodeId?: string; shotId?: string; status?: string } = {},
  ): Promise<PromptTranslationEntry[]> {
    return (async () => {
      const response = await api.get(`/projects/${projectId}/translations/`, { params: {
        episode: filters.episodeId || undefined,
        shot: filters.shotId || undefined,
        status: filters.status || undefined,
      } })
      return translationRows(response.data)
    })()
  },
  async translateProjectFields(
    projectId: string,
    fields: PromptTranslationField[],
    options: {
      modelDeploymentId: string
    },
  ): Promise<PromptTranslationEntry[]> {
    if (!fields.length) return []
    return (async () => {
      const response = await api.post(`/projects/${projectId}/translations/translate/`, {
        source_lang: 'Chinese', target_lang: 'English',
        model_deployment_id: options.modelDeploymentId,
        entries: fields.map((field) => ({
          field_key: field.fieldKey,
          source_text: field.sourceText,
          scope_type: field.scopeType,
          scope_id: field.scopeId,
        })),
      }, { timeout: 360_000 })
      return translationRows(response.data)
    })()
  },
  async createManualProjectTranslation(
    projectId: string,
    field: PromptTranslationField,
  ): Promise<PromptTranslationEntry> {
    const response = await api.post(`/projects/${projectId}/translations/manual/`, {
      entries: [{
        field_key: field.fieldKey,
        source_text: field.sourceText,
        scope_type: field.scopeType,
        scope_id: field.scopeId,
      }],
    })
    const rows = translationRows(response.data)
    if (rows.length !== 1) throw new Error('手工英文候选创建结果无效')
    return rows[0]
  },
  async retranslateProjectField(
    entryId: string,
    sourceText: string | undefined,
    options: {
      modelDeploymentId: string
    },
  ): Promise<PromptTranslationEntry> {
    return (async () => adaptTranslationEntry(unwrap<Record<string, unknown>>((await api.post(
      `/translations/${entryId}/retranslate/`, {
        ...(sourceText === undefined ? {} : { source_text: sourceText }),
        model_deployment_id: options.modelDeploymentId,
      }, { timeout: 360_000 },
    )).data)))()
  },
  async confirmProjectTranslation(entryId: string, translatedText: string, expectedRevision: number): Promise<PromptTranslationEntry> {
    return (async () => adaptTranslationEntry(unwrap<Record<string, unknown>>((await api.post(
      `/translations/${entryId}/confirm/`, { expected_revision: expectedRevision, translation: translatedText },
    )).data)))()
  },
  async promoteShotTranslations(
    shotId: string,
    entries: Array<{ entryId: string; expectedRevision: number }>,
    options: { repairLegacyNegativePrompt?: boolean } = {},
  ): Promise<ShotTranslationPromotionResult> {
    return (async () => adaptShotTranslationPromotionResult(unwrap<Record<string, unknown>>((await api.post(
      '/translations/promote-shot/', {
        shot_id: shotId,
        entries: entries.map((entry) => ({ entry_id: entry.entryId, expected_revision: entry.expectedRevision })),
        repair_legacy_negative_prompt: options.repairLegacyNegativePrompt === true,
      },
    )).data)))()
  },
  async revokeProjectTranslation(entryId: string): Promise<PromptTranslationEntry> {
    return (async () => adaptTranslationEntry(unwrap<Record<string, unknown>>((await api.post(
      `/translations/${entryId}/revoke/`, {},
    )).data)))()
  },
  async login(account: string, password: string) {
    return (async () => {
        const tokens = unwrap<{ access: string; refresh: string }>((await api.post('/auth/token/', { username: account, password })).data)
        const user = await currentUserForNewSession(tokens, account)
        return { access: tokens.access, refresh: tokens.refresh, user }
      })()
  },
  async register(account: string, email: string, password: string) {
    return (async () => {
        await api.post('/auth/register/', { username: account, email, password })
        const tokens = unwrap<{ access: string; refresh: string }>((await api.post('/auth/token/', { username: account, password })).data)
        const user = await currentUserForNewSession(tokens, account)
        return { access: tokens.access, refresh: tokens.refresh, user }
      })()
  },
  async getRegistrationCapability(): Promise<{ publicRegistrationEnabled: boolean }> {
    return (async () => {
      const raw = unwrap<Record<string, unknown>>((await api.get('/auth/register/')).data)
      return { publicRegistrationEnabled: raw.public_registration_enabled === true }
    })()
  },
  async getStyles(): Promise<StylePreset[]> {
    return (async () => adaptStyles(await fetchAllPages('/styles/', { is_active: true, page_size: 500 })))()
  },
  async getAssets(params?: { style?: string; type?: string; search?: string }): Promise<Asset[]> {
    return (async () => {
      // UI categories are derived from both native kinds and generic
      // reference.metadata.reference_category. Filtering by backend `kind`
      // would silently hide classified generic references (and `image` is not
      // a backend kind), so adapt first and filter the complete result here.
      const rows=adaptAssets(await fetchAllPages('/assets/', { style: backendStyleId(params?.style), search: params?.search, page_size: 500 }))
        .filter((asset)=>asset.metadata?.usage !== 'project_cover')
      return params?.type?rows.filter((asset)=>asset.type===params.type):rows
    })()
  },
  async createImageAsset(payload: {
    file: File
    kind: BibleDraftEntityType | 'reference'
    name: string
    description: string
    promptHint: string
    styleId?: string
  }): Promise<Asset> {
    return (async () => {
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
    })()
  },
  async setProjectCover(project: SeriesProject, file: File | null): Promise<SeriesProject> {
    return (async () => {
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
    })()
  },
  async updateAssetMetadata(assetId: string, payload: { name: string; description: string; promptHint: string; kind: BibleDraftEntityType | 'reference'; styleId?: string }): Promise<Asset> {
    return (async () => {
      const raw = unwrap<Record<string, unknown>>((await api.patch(`/assets/${assetId}/`, {
        name: payload.name.trim(), description: payload.description.trim(), prompt_hint: payload.promptHint.trim(),
        kind: payload.kind, style: backendStyleId(payload.styleId) || null,
      })).data)
      const asset = adaptAssets([raw])[0]
      if (!asset) throw new Error('保存成功，但服务端未返回可用的素材信息')
      return asset
    })()
  },
  async deleteAsset(assetId: string): Promise<void> {
    return (async () => {
      await api.delete(`/assets/${assetId}/`)
    })()
  },
  async uploadReferenceAsset(file: File, kind: 'reference_video'|'reference_audio', styleId?: string):Promise<Asset> {
    return (async()=>{
      const form=new FormData();form.append('file',file,file.name);form.append('name',file.name.replace(/\.[^.]+$/,''));form.append('kind',kind)
      const style=backendStyleId(styleId);if(style)form.append('style',style)
      const raw=unwrap<Record<string,unknown>>((await api.post('/assets/',form,{headers:{'Content-Type':'multipart/form-data'},timeout:120_000})).data)
      const asset=adaptAssets([raw])[0];if(!asset)throw new Error('上传成功，但服务端未返回可用的参考素材');return asset
    })()
  },
  async getProjects(): Promise<SeriesProject[]> {
    return (async () => {
      const raw = await fetchAllPages<Record<string, unknown>>('/projects/', { page_size: 200 })
      return raw.map((project) => adaptProjectSummary(project))
    })()
  },
  async getProject(id: string): Promise<SeriesProject> {
    return (async () => adaptProject(unwrap<Record<string, unknown>>((await api.get(`/projects/${id}/`)).data)))()
  },
  async getBibleImageCandidates(projectId: string): Promise<BibleImageCandidatesResult> {
    return (async () => {
      const response = await api.get(`/projects/${projectId}/bible-image-candidates/`)
      const result = adaptBibleImageCandidates(unwrap<unknown>(response.data))
      if (result.projectId !== projectId) throw new Error('服务端返回的实体图片候选项目不匹配。')
      return result
    })()
  },
  async createProject(payload: Partial<SeriesProject>): Promise<SeriesProject> {
    return (async () => adaptProjectSummary(unwrap<Record<string, unknown>>((await api.post('/projects/', projectPayload(payload))).data), true))()
  },
  async updateProject(project: SeriesProject): Promise<SeriesProject> {
    return (async () => {
      const patched = unwrap<Record<string, unknown>>((await api.patch(`/projects/${project.id}/`, projectPayload(project))).data)
      const bibleProject = { ...project, updatedAt: String(patched.updated_at || project.updatedAt) }
      await persistBibleEntities(bibleProject)
      return adaptProject(unwrap<Record<string, unknown>>(
        (await api.get(`/projects/${project.id}/`)).data,
      ))
    })()
  },
  async setProjectFavorite(project: SeriesProject, favorite: boolean): Promise<SeriesProject> {
    return (async () => {
      const raw = unwrap<Record<string, unknown>>((await api.patch(`/projects/${project.id}/`, { is_favorite: favorite })).data)
      return { ...project, isFavorite: raw.is_favorite === true, updatedAt: String(raw.updated_at || project.updatedAt) }
    })()
  },
  async deleteProject(projectId: string): Promise<void> {
    return (async () => { await api.delete(`/projects/${projectId}/`) })()
  },
  async addEpisode(projectId: string, payload: Pick<Episode, 'title' | 'synopsis'> & { script?: string }): Promise<Episode> {
    return (async () => adaptCreatedEpisode(unwrap<Record<string, unknown>>((await api.post(`/projects/${projectId}/next-episode/`, payload)).data)))()
  },
  async deleteEpisode(projectId: string, episodeId: string): Promise<void> {
    return (async () => { await api.delete(`/episodes/${episodeId}/`) })()
  },
  async saveEpisode(project: SeriesProject, episode: Episode): Promise<Episode> {
    return (async () => {
      const patched = unwrap<Record<string, any>>((await api.patch(`/episodes/${episode.id}/`, {
        project: project.id,
        number: episode.number,
        title: episode.title,
        synopsis: episode.synopsis,
        script: episode.script || '',
        status: episode.status === 'rendering' ? 'generating' : episode.status,
        prompt_settings: { include_audio: episode.audioEnabled },
      })).data)
      const shots = []
      for (const shot of episode.shots) shots.push(await syncShot(project, episode, shot))
      return {
        ...episode,
        ...adaptCreatedEpisode(patched),
        shots,
        shotVersions: episode.shotVersions,
        activeShotVersionKey: episode.activeShotVersionKey,
      }
    })()
  },
  async saveShot(project: SeriesProject, episode: Episode, shot: Shot): Promise<Shot> {
    return (async () => syncShot(project, episode, shot))()
  },
  async getWorkflows(): Promise<Workflow[]> {
    return (async () => adaptTasks(await fetchAllPages('/tasks/', { page_size: 500 })))()
  },
  async getShotWorkflows(shotId: string, kind: 'video' | 'image' = 'video'): Promise<Workflow[]> {
    return (async () => {
      if (!isBackendId(shotId)) return []
      return adaptTasks(await fetchAllPages('/tasks/', {
        shot_id: shotId,
        kind,
        ordering: '-queued_at',
        page_size: 100
      }))
    })()
  },
  async getWorkflow(taskId: string): Promise<Workflow> {
    return (async () => {
      const raw = unwrap<Record<string, any>>((await api.get(`/tasks/${taskId}/`)).data)
      const workflow = adaptTasks([raw])[0]
      if (!workflow) throw new Error('服务端未返回任务详情')
      return workflow
    })()
  },
  async getDeferredGenerationRequests(filters: DeferredGenerationRequestFilters = {}): Promise<DeferredGenerationRequest[]> {
    return (async () => {
      const params: Record<string, unknown> = { page_size: 100 }
      if (filters.project) params.project = filters.project
      if (filters.sourceTask) params.source_task = filters.sourceTask
      if (filters.targetShot) params.target_shot = filters.targetShot
      if (filters.status) params.status = filters.status
      if (filters.frameSlot) params.frame_slot = filters.frameSlot
      return (await fetchAllPages<unknown>('/deferred-generation-requests/', params))
        .map(adaptDeferredGenerationRequest)
    })()
  },
  async getTaskInputPrompt(taskId: string): Promise<TaskInputPrompt> {
    return (async () => {
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
    })()
  },
  async getGeneratedWorkflowExport(task: Workflow, workflowFormat: 'api' | 'ui' = 'ui'): Promise<GeneratedWorkflowExport> {
    return (async () => {
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
    })()
  },
  async getEpisodeAssemblies(projectId: string): Promise<AssemblyResult[]> {
    return (async () => {
      if (!isBackendId(projectId)) return []
      const rows = await fetchAllPages<Record<string, any>>('/assemblies/', {
        project: projectId,
        scope: 'episode',
        dry_run: false,
        ordering: '-created_at',
        page_size: 500
      })
      return rows.map(adaptAssembly)
    })()
  },
  async getAssembly(assemblyId: string): Promise<AssemblyResult> {
    return (async () => {
      const raw = unwrap<Record<string, any>>((await api.get(`/assemblies/${assemblyId}/`)).data)
      return adaptAssembly(raw)
    })()
  },
  async getReleaseBundles(projectId: string): Promise<ReleaseBundle[]> {
    return (async () => {
      if (!isBackendId(projectId)) return []
      const rows = await fetchAllPages<Record<string, any>>('/release-bundles/', {
        project: projectId,
        ordering: '-version',
        page_size: 100,
      })
      return rows.map(adaptReleaseBundle)
    })()
  },
  async verifyReleaseBundle(bundleId: string): Promise<{ status: string; signatureVerified: boolean; liveVerified: boolean }> {
    return (async () => {
      const raw = unwrap<Record<string, any>>((await api.get(`/release-bundles/${bundleId}/verify/`)).data)
      return {
        status: String(raw.verification?.status || 'invalid'),
        signatureVerified: raw.verification?.signature_verified === true,
        liveVerified: raw.verification?.live_verified === true,
      }
    })()
  },
  async testComfy(host: string): Promise<{ ok: boolean; message: string; latency?: number }> {
    const started = performance.now()
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
