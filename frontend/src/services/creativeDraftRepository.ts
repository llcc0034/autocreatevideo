import { api, unwrap } from './api'
import { mockProjects } from '@/data/mock'
import {
  adaptCreativeDraft,
  adaptCreativeDraftApplyResult,
  creativeDraftDataPayload,
} from './creativeDraft'
import type {
  CreativeDraft,
  CreativeDraftApplyResult,
  CreativeDraftCreativity,
  CreativeDraftData,
  CreativeGenerationCapability,
  CreativeDraftLanguageMode,
  CreativeDraftPlacement,
  Episode,
  SeriesProject,
  Shot,
  PromptProviderId,
} from '@/types'
import { adaptPromptProviders } from './providerCapabilities'

type Raw = Record<string, any>
const useMock = import.meta.env.VITE_USE_MOCK === 'true'
// The child Qwen process may run for 900 seconds and its parent enforces a
// 960-second hard ceiling. Keep the browser at 17 minutes so the server has a
// full minute of network/serialization margin to return the terminal result.
const CREATIVE_GENERATION_TIMEOUT_MS = 1_020_000
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value))
const wait = () => new Promise((resolve) => setTimeout(resolve, 180))
const mockDrafts = new Map<string, CreativeDraft>()

const unwrapRows = (payload: unknown): Raw[] => {
  const body = unwrap<any>(payload)
  if (Array.isArray(body)) return body
  if (Array.isArray(body?.results)) return body.results
  if (Array.isArray(body?.drafts)) return body.drafts
  return []
}

const localProjects = (): SeriesProject[] => {
  try { return JSON.parse(localStorage.getItem('cineforge_projects') || '') } catch { return clone(mockProjects) }
}
const saveProjects = (projects: SeriesProject[]) => localStorage.setItem('cineforge_projects', JSON.stringify(projects))

const mockEnglish = (source: string, fallback: string) => source.trim() ? `[Mock draft] ${source.trim()}` : fallback
const mockData = (project: SeriesProject, episode: Episode, targetShotCount: number): CreativeDraftData => {
  const sourceShots = episode.shots.length
    ? episode.shots.slice(0, targetShotCount)
    : Array.from({ length: targetShotCount }, (_, index): Shot => ({
      id: `mock-source-${index + 1}`, order: index + 1, title: `镜头 ${index + 1}`,
      mediaType: 'video', duration: 5, mode: project.defaultMode, framing: '中景',
      camera: '稳定缓慢推进', action: episode.synopsis || project.logline || '人物进入场景，事件开始。',
      continuityDirection: '承接上一镜位置与视线，动作结束后保持可衔接姿态。', prompt: '',
      negativePrompt: project.bible.negativePrompt, characterIds: [], sceneId: project.bible.scenes[0]?.id,
      propIds: [], referenceAssetIds: [], audioEnabled: true,
      audio: { dialogue: '', soundscape: '连续环境声', music: '克制的叙事配乐' }, status: 'draft',
    }))
  return {
    schemaVersion: 1,
    episode: {
      titleZh: episode.title,
      titleEn: mockEnglish(episode.title, `Episode ${episode.number}`),
      synopsisZh: episode.synopsis,
      synopsisEn: mockEnglish(episode.synopsis, 'Episode synopsis'),
    },
    shots: sourceShots.map((shot, index) => ({
      clientKey: `draft-shot-${index + 1}`, order: index + 1,
      titleZh: shot.title || `镜头 ${index + 1}`, titleEn: `Shot ${index + 1}`,
      narrativeZh: shot.action || episode.synopsis, narrativeEn: mockEnglish(shot.action || episode.synopsis, 'Establish the scene and subject.'),
      cameraZh: shot.camera || '稳定缓慢推进', cameraEn: mockEnglish(shot.camera, 'one slow, motivated push-in'),
      actionZh: shot.action || '人物完成一个明确主动作', actionEn: mockEnglish(shot.action, 'the subject completes one clear primary action'),
      continuityZh: shot.continuityDirection || '承接上一镜状态，并以稳定姿态结束',
      continuityEn: mockEnglish(shot.continuityDirection || '', 'continue from the prior state and end in a stable matching pose'),
      soundscapeZh: shot.audio.soundscape || '连续环境声', soundscapeEn: mockEnglish(shot.audio.soundscape, 'continuous diegetic ambience'),
      musicZh: shot.audio.music || '克制的叙事配乐', musicEn: mockEnglish(shot.audio.music, 'restrained non-diegetic score'),
      durationMs: Math.min(15000, Math.max(5000, Math.round((shot.duration || 5) * 1000))),
      mediaType: 'video' as const, h3Mode: shot.mode, includeAudio: shot.audioEnabled,
      sceneId: shot.sceneId, characterIds: [...shot.characterIds], propIds: [...shot.propIds], dialogue: [],
    })),
  }
}

const mockFlags = (draft: CreativeDraft): CreativeDraft => ({
  ...draft,
  // Mirror the backend state machine: a structurally failed candidate remains
  // repairable/retryable while its authoritative context is still current.
  canEdit: ['draft', 'failed'].includes(draft.status) && draft.contextCurrent,
  canRegenerate: ['draft', 'confirmed', 'failed'].includes(draft.status) && draft.contextCurrent,
  canConfirm: draft.status === 'draft' && draft.contextCurrent && ['passed', 'ready', 'needs_review'].includes(draft.validation.status),
  canApply: draft.status === 'confirmed' && draft.contextCurrent && ['passed', 'ready', 'needs_review'].includes(draft.validation.status),
})

const mockProjectEpisode = (projectId: string, episodeId: string) => {
  const projects = localProjects()
  const project = projects.find((item) => item.id === projectId) || clone(mockProjects).find((item) => item.id === projectId)
  const episode = project?.episodes.find((item) => item.id === episodeId)
  if (!project || !episode) throw new Error('找不到用于创作草稿的项目或剧集。')
  return { projects, project, episode }
}

export const creativeDraftRepository = {
  async capability(): Promise<CreativeGenerationCapability> {
    if (!useMock) {
      const raw = unwrap<Raw>((await api.get('/capabilities/creative-drafts/')).data)
      const providerCapability = adaptPromptProviders(raw)
      return {
        supported: raw.creative_generation_supported === true,
        configured: raw.configured === true,
        provider: String(raw.provider || ''), model: String(raw.model || ''),
        modelDownloaded: raw.model_downloaded === true, modelVerified: raw.model_verified === true,
        device: String(raw.device || 'cpu'), minShots: Number(raw.min_shots || 1), maxShots: Number(raw.max_shots || 4),
        maxSourceChars: Number(raw.max_source_chars || 0),
        baselineMaxNewTokens: Number(raw.baseline_max_new_tokens || 0),
        maxNewTokens: Number(raw.max_new_tokens || raw.configured_max_new_tokens || 0),
        configuredMaxNewTokens: Number(raw.configured_max_new_tokens || raw.max_new_tokens || 0),
        contextSize: Number(raw.context_size || 0), loadedOnDemand: raw.loaded_on_demand === true,
        unloadedAfterRequest: raw.unloaded_after_request === true, reviewRequired: raw.review_required !== false,
        translationReviewReused: raw.translation_review_reused === true,
        notice: String(raw.notice || '') || undefined,
        ...providerCapability,
      }
    }
    await wait()
    return { supported: true, configured: true, provider: 'mock-qwen-local', model: 'Qwen/Qwen3-4B-GGUF',
      modelDownloaded: true, modelVerified: true, device: 'cpu', minShots: 1, maxShots: 4,
      maxSourceChars: 12000, baselineMaxNewTokens: 2304, maxNewTokens: 3072,
      configuredMaxNewTokens: 3072, contextSize: 32768, loadedOnDemand: true,
      unloadedAfterRequest: true, reviewRequired: true, translationReviewReused: true,
      notice: 'Mock 模式仅用于前端交互验证。', defaultProvider: 'local_qwen',
      providers: [{ id: 'local_qwen', label: '本地 Qwen', enabled: true, configured: true, ready: true,
        model: 'Qwen/Qwen3-4B-GGUF', transport: 'local', modelDownloaded: true, modelVerified: true, device: 'cpu' }] }
  },

  async list(projectId: string, episodeId: string): Promise<CreativeDraft[]> {
    if (!useMock) {
      const response = await api.get(`/projects/${projectId}/creative-drafts/`, { params: { episode: episodeId } })
      return unwrapRows(response.data).map(adaptCreativeDraft)
    }
    await wait()
    return [...mockDrafts.values()].filter((item) => item.projectId === projectId && item.episodeId === episodeId)
      .sort((left, right) => right.generationNo - left.generationNo).map(clone)
  },

  async generate(projectId: string, options: {
    episodeId: string
    targetShotCount: number
    creativity: CreativeDraftCreativity
    variationNote?: string
    provider?: PromptProviderId
  }): Promise<CreativeDraft> {
    if (!useMock) {
      const response = await api.post(`/projects/${projectId}/creative-drafts/generate/`, {
        episode_id: options.episodeId,
        target_shot_count: options.targetShotCount,
        creativity: options.creativity,
        variation_note: options.variationNote || '',
        provider: options.provider || 'local_qwen',
      }, { timeout: CREATIVE_GENERATION_TIMEOUT_MS })
      return adaptCreativeDraft(unwrap<Raw>(response.data))
    }
    await wait()
    const { project, episode } = mockProjectEpisode(projectId, options.episodeId)
    const generationNo = [...mockDrafts.values()].filter((item) => item.episodeId === episode.id).length + 1
    const now = new Date().toISOString()
    const draft = mockFlags({
      id: `creative-draft-${Date.now()}`, projectId, episodeId: episode.id, status: 'draft', revision: 1,
      generationNo, contextHash: `mock-${project.updatedAt}`, contextCurrent: true,
      creativity: options.creativity, variationNote: options.variationNote || '',
      draftData: mockData(project, episode, options.targetShotCount),
      validation: { status: 'ready', errors: [], warnings: [] },
      reviewIssues: [],
      reviewSummary: { issueCount: 0, riskCount: 0, requiresEditCount: 0, status: 'ready' },
      warnings: [], provider: 'mock-qwen-local', model: 'Qwen/Qwen3-4B-GGUF', createdAt: now, updatedAt: now,
      canEdit: true, canRegenerate: true, canConfirm: true, canApply: false,
    })
    mockDrafts.set(draft.id, draft)
    return clone(draft)
  },

  async update(id: string, expectedRevision: number, draftData: CreativeDraftData): Promise<CreativeDraft> {
    if (!useMock) {
      const response = await api.patch(`/creative-drafts/${id}/`, {
        expected_revision: expectedRevision,
        draft_data: creativeDraftDataPayload(draftData),
      })
      return adaptCreativeDraft(unwrap<Raw>(response.data))
    }
    await wait()
    const current = mockDrafts.get(id)
    if (!current || current.revision !== expectedRevision) throw new Error('草稿版本已变化，请刷新后重试。')
    const next = mockFlags({ ...current, revision: current.revision + 1, draftData: clone(draftData), updatedAt: new Date().toISOString() })
    mockDrafts.set(id, next); return clone(next)
  },

  async regenerate(id: string, options: {
    expectedRevision: number
    targetShotCount?: number
    creativity?: CreativeDraftCreativity
    variationNote?: string
    shotKeys?: string[]
    provider?: PromptProviderId
  }): Promise<CreativeDraft> {
    if (!useMock) {
      const response = await api.post(`/creative-drafts/${id}/regenerate/`, {
        expected_revision: options.expectedRevision,
        target_shot_count: options.targetShotCount,
        creativity: options.creativity,
        variation_note: options.variationNote || '',
        provider: options.provider || 'local_qwen',
        ...(options.shotKeys?.length ? { shot_keys: options.shotKeys } : {}),
        preserve_locked_content: true,
      }, { timeout: CREATIVE_GENERATION_TIMEOUT_MS })
      return adaptCreativeDraft(unwrap<Raw>(response.data))
    }
    await wait()
    const current = mockDrafts.get(id)
    if (!current || current.revision !== options.expectedRevision) throw new Error('草稿版本已变化，请刷新后重试。')
    mockDrafts.set(id, mockFlags({ ...current, status: 'superseded', canEdit: false, canApply: false }))
    const now = new Date().toISOString()
    const next = mockFlags({ ...clone(current), id: `creative-draft-${Date.now()}`, parentDraftId: current.id,
      status: 'draft', revision: 1, generationNo: current.generationNo + 1,
      creativity: options.creativity || current.creativity, variationNote: options.variationNote || '',
      createdAt: now, updatedAt: now, confirmedAt: undefined, appliedAt: undefined,
    })
    mockDrafts.set(next.id, next); return clone(next)
  },

  async confirm(id: string, expectedRevision: number, reviewAcknowledged = false): Promise<CreativeDraft> {
    if (!useMock) {
      const response = await api.post(`/creative-drafts/${id}/confirm/`, {
        expected_revision: expectedRevision,
        review_acknowledged: reviewAcknowledged,
      })
      return adaptCreativeDraft(unwrap<Raw>(response.data))
    }
    await wait()
    const current = mockDrafts.get(id)
    if (!current || current.revision !== expectedRevision) throw new Error('草稿版本已变化，请刷新后重试。')
    if (current.reviewIssues.length && !reviewAcknowledged) throw new Error('请先阅读并确认草稿风险。')
    const next = mockFlags({ ...current, status: 'confirmed', revision: current.revision + 1, confirmedAt: new Date().toISOString() })
    mockDrafts.set(id, next); return clone(next)
  },

  async apply(id: string, options: {
    expectedRevision: number
    languageMode: CreativeDraftLanguageMode
    placement: CreativeDraftPlacement
    shotKeys?: string[]
    englishReviewAcknowledged?: boolean
    reviewAcknowledged?: boolean
  }): Promise<CreativeDraftApplyResult> {
    if (!useMock) {
      const response = await api.post(`/creative-drafts/${id}/apply/`, {
        expected_revision: options.expectedRevision,
        language_mode: options.languageMode,
        placement: options.placement,
        review_acknowledged: options.reviewAcknowledged === true,
        ...(options.shotKeys?.length ? { shot_keys: options.shotKeys } : {}),
        ...(options.languageMode === 'bilingual_confirmed' ? {
          english_review_acknowledged: options.englishReviewAcknowledged === true,
        } : {}),
      })
      return adaptCreativeDraftApplyResult(unwrap<Raw>(response.data))
    }
    await wait()
    const current = mockDrafts.get(id)
    if (!current || current.revision !== options.expectedRevision) throw new Error('草稿版本已变化，请刷新后重试。')
    if (current.status !== 'confirmed') throw new Error('请先确认草稿，再应用到正式分镜。')
    if (current.reviewIssues.length && !options.reviewAcknowledged) throw new Error('请先阅读并确认草稿风险。')
    if (options.languageMode === 'bilingual_confirmed' && !options.englishReviewAcknowledged) throw new Error('请先确认已人工核对英文。')
    const { projects, project, episode } = mockProjectEpisode(current.projectId, current.episodeId)
    const selected = options.shotKeys?.length
      ? current.draftData.shots.filter((shot) => options.shotKeys?.includes(shot.clientKey))
      : current.draftData.shots
    const mapped: Shot[] = selected.map((shot, index) => ({
      id: `shot-${Date.now()}-${index}`, order: index + 1, title: shot.titleZh, mediaType: 'video',
      duration: shot.durationMs / 1000, mode: shot.h3Mode, framing: shot.narrativeZh,
      camera: shot.cameraZh, action: shot.actionZh || shot.narrativeZh, continuityDirection: shot.continuityZh,
      prompt: '', negativePrompt: project.bible.negativePrompt, characterIds: shot.characterIds,
      sceneId: shot.sceneId, propIds: shot.propIds, referenceAssetIds: [], audioEnabled: shot.includeAudio,
      audio: { dialogue: shot.dialogue.map((line) => `${line.speakerId}：${line.textZh}`).join('\n'), soundscape: shot.soundscapeZh, music: shot.musicZh }, status: 'draft',
    }))
    if (options.placement === 'replace' && !options.shotKeys?.length) episode.shots = mapped
    else episode.shots.push(...mapped.map((shot, index) => ({ ...shot, order: episode.shots.length + index + 1 })))
    const index = projects.findIndex((item) => item.id === project.id)
    if (index >= 0) { projects[index] = project; saveProjects(projects) }
    const next = mockFlags({ ...current, status: 'applied', revision: current.revision + 1, appliedAt: new Date().toISOString() })
    mockDrafts.set(id, next)
    return { draft: clone(next), episodeId: episode.id, appliedShotIds: mapped.map((shot) => shot.id), translationsWritten: options.languageMode === 'bilingual_confirmed' ? selected.length : 0 }
  },
}
