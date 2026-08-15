import { api, unwrap } from './api'
import {
  adaptShotDetailDraft,
  adaptShotDetailDraftApplyResult,
  adaptShotDetailDraftBatch,
  adaptShotDetailDraftBatchApplyResult,
  adaptShotDetailDraftBatchDetail,
  shotDetailDraftDataPayload,
} from './shotDetailDraft'
import type {
  ShotDetailDraft,
  ShotDetailDraftApplyResult,
  ShotDetailDraftData,
  ShotDetailDraftLanguageMode,
  CreativeGenerationCapability,
  PromptProviderId,
  ShotDetailDraftBatch,
  ShotDetailDraftBatchDetail,
  ShotDetailDraftBatchApplyResult,
  ShotHighDetailPolicy,
} from '@/types'
import {
  adaptPromptProviders,
  mockCreativePromptProviderCapability,
  promptProvider,
} from './providerCapabilities'

type Raw = Record<string, any>
const useMock = import.meta.env.VITE_USE_MOCK === 'true'
// Zero lets Axios wait for the server-owned provider timeout. Deployments can
// set a finite browser ceiling without changing application code.
const configuredShotDetailTimeout = Number(import.meta.env.VITE_SHOT_DETAIL_DRAFT_TIMEOUT_MS ?? 0)
if (!Number.isFinite(configuredShotDetailTimeout) || configuredShotDetailTimeout < 0) {
  throw new Error('VITE_SHOT_DETAIL_DRAFT_TIMEOUT_MS must be zero or a positive number')
}
const SHOT_DETAIL_DRAFT_TIMEOUT_MS = configuredShotDetailTimeout
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const wait = () => new Promise((resolve) => setTimeout(resolve, 180))

const mockDrafts = new Map<string, ShotDetailDraft>()

const unwrapRows = (payload: unknown): Raw[] => {
  const body = unwrap<any>(payload)
  if (Array.isArray(body)) return body
  if (Array.isArray(body?.results)) return body.results
  if (Array.isArray(body?.drafts)) return body.drafts
  return []
}

const positive = (value: unknown): number | undefined => {
  const result = Number(value)
  return Number.isFinite(result) && result > 0 ? result : undefined
}

const adaptHighDetailPolicy = (raw: Raw): ShotHighDetailPolicy => ({
  provider: raw.provider === 'deepseek_api' ? 'deepseek_api' : 'local_qwen',
  recommendedZhChars: positive(raw.recommended_zh_chars),
  recommendedEnChars: positive(raw.recommended_en_chars),
  transportZhMaxChars: positive(raw.transport_zh_max_chars),
  transportEnMaxChars: positive(raw.transport_en_max_chars),
  recommendedMaxShots: positive(raw.recommended_max_shots),
  maxAttempts: positive(raw.max_attempts),
})

const mockFlags = (draft: ShotDetailDraft): ShotDetailDraft => ({
  ...draft,
  canEdit: draft.status === 'draft',
  canRegenerate: ['draft', 'confirmed', 'failed'].includes(draft.status),
  canConfirm: draft.status === 'draft' && ['passed', 'ready', 'needs_review'].includes(draft.validation.status) && draft.contextCurrent,
  canApply: draft.status === 'confirmed' && ['passed', 'ready', 'needs_review'].includes(draft.validation.status)
    && !draft.reviewSummary.requiresEditCount && draft.contextCurrent,
})

const mockDetail = (note: string) => ({
  detailZh: `镜头以已绑定的人物、场景和道具为唯一视觉依据。${note ? `本次重点：${note}。` : ''}从当前构图与上一镜结束状态自然承接，先建立空间、人物视线和光线方向，再完成一个明确主动作；镜头只执行一个有动机的缓慢运动，保留可供下一镜衔接的姿态、视线和道具位置。环境声与画面动作同步出现，结尾停在稳定的叙事状态。`,
  detailEn: `Use only the bound characters, environment, props, and approved continuity references. ${note ? `Creative emphasis: ${note}. ` : ''}Continue naturally from the current shot boundary. Establish the space, eyeline, and lighting direction, then complete one readable primary action. Use one motivated, restrained camera move and preserve a stable final pose, eyeline, and prop position for the next shot. Keep environmental sound synchronized with visible action and end on a clear, matchable narrative state.`,
})

export const shotDetailDraftRepository = {
  async capability(): Promise<CreativeGenerationCapability> {
    if (!useMock) {
      const raw = unwrap<Raw>((await api.get('/capabilities/creative-drafts/')).data)
      const providerCapability = adaptPromptProviders(raw)
      const defaultCapability = promptProvider(providerCapability.providers, providerCapability.defaultProvider)
      return {
        supported: raw.creative_generation_supported === true, configured: defaultCapability?.configured === true,
        provider: defaultCapability?.id || '', model: defaultCapability?.model || '',
        modelDownloaded: defaultCapability?.modelDownloaded === true, modelVerified: defaultCapability?.modelVerified === true,
        device: defaultCapability?.device || '', minShots: defaultCapability?.minShots,
        batchShots: defaultCapability?.batchShots, maxSourceChars: defaultCapability?.maxSourceChars,
        minShotDurationMs: defaultCapability?.minShotDurationMs, maxShotDurationMs: defaultCapability?.maxShotDurationMs,
        baselineMaxNewTokens: defaultCapability?.baselineMaxNewTokens,
        maxNewTokens: defaultCapability?.maxNewTokens,
        configuredMaxNewTokens: defaultCapability?.configuredMaxNewTokens,
        contextSize: defaultCapability?.contextSize, loadedOnDemand: defaultCapability?.loadedOnDemand === true,
        unloadedAfterRequest: defaultCapability?.unloadedAfterRequest === true,
        reviewRequired: defaultCapability?.reviewRequired !== false,
        translationReviewReused: defaultCapability?.translationReviewReused === true,
        notice: defaultCapability?.notice,
        shotHighDetailPolicies: Object.fromEntries(
          Object.entries((raw.shot_high_detail_policies || {}) as Raw)
            .map(([id, policy]) => [id, adaptHighDetailPolicy((policy || {}) as Raw)]),
        ),
        shotHighDetailSharedResourceBusy: raw.shot_high_detail_shared_resource_busy === true,
        ...providerCapability,
      }
    }
    await wait()
    const mockProvider = mockCreativePromptProviderCapability()
    return {
      supported: true, configured: true, provider: mockProvider.id, model: mockProvider.model,
      modelDownloaded: mockProvider.modelDownloaded === true, modelVerified: mockProvider.modelVerified === true,
      device: mockProvider.device || '', minShots: mockProvider.minShots,
      batchShots: mockProvider.batchShots, maxSourceChars: mockProvider.maxSourceChars,
      minShotDurationMs: mockProvider.minShotDurationMs, maxShotDurationMs: mockProvider.maxShotDurationMs,
      baselineMaxNewTokens: mockProvider.baselineMaxNewTokens,
      maxNewTokens: mockProvider.maxNewTokens ?? mockProvider.configuredMaxNewTokens,
      configuredMaxNewTokens: mockProvider.configuredMaxNewTokens ?? mockProvider.maxNewTokens,
      contextSize: mockProvider.contextSize, loadedOnDemand: mockProvider.loadedOnDemand === true,
      unloadedAfterRequest: mockProvider.unloadedAfterRequest === true,
      reviewRequired: mockProvider.reviewRequired !== false,
      translationReviewReused: mockProvider.translationReviewReused === true,
      defaultProvider: 'local_qwen', providers: [mockProvider],
      shotHighDetailPolicies: {
        local_qwen: {
          provider: 'local_qwen',
          recommendedZhChars: positive(import.meta.env.VITE_MOCK_SHOT_DETAIL_RECOMMENDED_ZH_CHARS),
          recommendedEnChars: positive(import.meta.env.VITE_MOCK_SHOT_DETAIL_RECOMMENDED_EN_CHARS),
          transportZhMaxChars: positive(import.meta.env.VITE_MOCK_SHOT_DETAIL_TRANSPORT_ZH_MAX_CHARS),
          transportEnMaxChars: positive(import.meta.env.VITE_MOCK_SHOT_DETAIL_TRANSPORT_EN_MAX_CHARS),
        },
      },
    }
  },

  async listBatches(projectId: string, episodeId: string): Promise<ShotDetailDraftBatch[]> {
    if (useMock) return []
    const response = await api.get(`/projects/${projectId}/shot-detail-draft-batches/`, {
      params: { episode: episodeId, page_size: 50 },
    })
    return unwrapRows(response.data).map(adaptShotDetailDraftBatch)
  },

  async createBatch(projectId: string, options: {
    episodeId: string
    requestId: string
    provider: PromptProviderId
    variationNote?: string
    confirmationAcknowledged?: boolean
  }): Promise<ShotDetailDraftBatch> {
    if (useMock) throw new Error('Mock 模式暂不支持整集高细节扩写。')
    const response = await api.post(`/projects/${projectId}/shot-detail-draft-batches/`, {
      episode_id: options.episodeId,
      request_id: options.requestId,
      provider: options.provider,
      variation_note: options.variationNote || '',
      confirmation_acknowledged: options.confirmationAcknowledged === true,
    })
    return adaptShotDetailDraftBatch(unwrap<Raw>(response.data))
  },

  async batchDetail(batchId: string, page = 1, pageSize = 200): Promise<ShotDetailDraftBatchDetail> {
    if (useMock) throw new Error('Mock 模式暂不支持整集高细节扩写。')
    const response = await api.get(`/shot-detail-draft-batches/${batchId}/`, {
      params: { page, page_size: pageSize },
    })
    return adaptShotDetailDraftBatchDetail(unwrap<Raw>(response.data))
  },

  async batchDetailAll(batchId: string): Promise<ShotDetailDraftBatchDetail> {
    const first = await this.batchDetail(batchId, 1, 200)
    const pages = Math.ceil(first.pagination.count / 200)
    if (pages <= 1) return first
    const rest = await Promise.all(
      Array.from({ length: pages - 1 }, (_, index) => this.batchDetail(batchId, index + 2, 200)),
    )
    return { ...first, items: [first, ...rest].flatMap((result) => result.items) }
  },

  async applyBatch(batchId: string, options: {
    expectedBatchRevision: number
    items: Array<{ itemId: string; draftId: string; expectedRevision: number }>
    languageMode: ShotDetailDraftLanguageMode
    englishReviewAcknowledged?: boolean
    reviewAcknowledged?: boolean
  }): Promise<ShotDetailDraftBatchApplyResult> {
    if (useMock) throw new Error('Mock 模式暂不支持整集高细节扩写。')
    const response = await api.post(`/shot-detail-draft-batches/${batchId}/apply/`, {
      expected_batch_revision: options.expectedBatchRevision,
      items: options.items.map((item) => ({
        item_id: item.itemId, draft_id: item.draftId, expected_revision: item.expectedRevision,
      })),
      language_mode: options.languageMode,
      english_review_acknowledged: options.englishReviewAcknowledged === true,
      review_acknowledged: options.reviewAcknowledged === true,
    })
    return adaptShotDetailDraftBatchApplyResult(unwrap<Raw>(response.data))
  },

  async list(projectId: string, shotId: string): Promise<ShotDetailDraft[]> {
    if (!useMock) {
      const response = await api.get(`/projects/${projectId}/shot-detail-drafts/`, { params: { shot: shotId } })
      return unwrapRows(response.data).map(adaptShotDetailDraft)
    }
    await wait()
    return [...mockDrafts.values()]
      .filter((item) => item.projectId === projectId && item.shotId === shotId)
      .sort((left, right) => right.generationNo - left.generationNo)
      .map(clone)
  },

  async generate(projectId: string, options: { shotId: string; variationNote?: string; provider?: PromptProviderId }): Promise<ShotDetailDraft> {
    if (!useMock) {
      const response = await api.post(`/projects/${projectId}/shot-detail-drafts/generate/`, {
        shot_id: options.shotId,
        variation_note: options.variationNote || '',
        provider: options.provider || 'local_qwen',
      }, { timeout: SHOT_DETAIL_DRAFT_TIMEOUT_MS })
      return adaptShotDetailDraft(unwrap<Raw>(response.data))
    }
    await wait()
    const now = new Date().toISOString()
    const generationNo = [...mockDrafts.values()].filter((item) => item.projectId === projectId && item.shotId === options.shotId).length + 1
    const draft = mockFlags({
      id: `shot-detail-${Date.now()}`, projectId, shotId: options.shotId, status: 'draft', revision: 1,
      generationNo, contextHash: `mock-${Date.now()}`, contextCurrent: true,
      variationNote: options.variationNote || '', draftData: { schemaVersion: 1, ...mockDetail(options.variationNote || '') },
      validation: { status: 'passed', errors: [], warnings: ['Mock 模式仅用于前端交互验证。'] },
      reviewIssues: [], reviewSummary: { issueCount: 0, riskCount: 0, requiresEditCount: 0, status: 'ready' },
      warnings: [], provider: 'mock-qwen-local', model: 'Qwen/Qwen3-4B-GGUF',
      createdAt: now, updatedAt: now, canEdit: true, canRegenerate: true, canConfirm: true, canApply: false,
    })
    mockDrafts.set(draft.id, draft)
    return clone(draft)
  },

  async update(id: string, expectedRevision: number, draftData: ShotDetailDraftData): Promise<ShotDetailDraft> {
    if (!useMock) {
      const response = await api.patch(`/shot-detail-drafts/${id}/`, {
        expected_revision: expectedRevision,
        draft_data: shotDetailDraftDataPayload(draftData),
      })
      return adaptShotDetailDraft(unwrap<Raw>(response.data))
    }
    await wait()
    const current = mockDrafts.get(id)
    if (!current || current.revision !== expectedRevision) throw new Error('高细节草稿版本已变化，请刷新后重试。')
    const next = mockFlags({ ...current, revision: current.revision + 1, draftData: clone(draftData), updatedAt: new Date().toISOString() })
    mockDrafts.set(id, next)
    return clone(next)
  },

  async regenerate(id: string, options: { expectedRevision: number; variationNote?: string; provider?: PromptProviderId }): Promise<ShotDetailDraft> {
    if (!useMock) {
      const response = await api.post(`/shot-detail-drafts/${id}/regenerate/`, {
        expected_revision: options.expectedRevision,
        variation_note: options.variationNote || '',
        provider: options.provider || 'local_qwen',
      }, { timeout: SHOT_DETAIL_DRAFT_TIMEOUT_MS })
      return adaptShotDetailDraft(unwrap<Raw>(response.data))
    }
    await wait()
    const current = mockDrafts.get(id)
    if (!current || current.revision !== options.expectedRevision) throw new Error('高细节草稿版本已变化，请刷新后重试。')
    mockDrafts.set(id, mockFlags({ ...current, status: 'superseded', canEdit: false, canApply: false }))
    const now = new Date().toISOString()
    const next = mockFlags({
      ...clone(current), id: `shot-detail-${Date.now()}`, parentDraftId: current.id,
      status: 'draft', revision: 1, generationNo: current.generationNo + 1,
      variationNote: options.variationNote || current.variationNote,
      draftData: { schemaVersion: 1, ...mockDetail(options.variationNote || current.variationNote) },
      createdAt: now, updatedAt: now, confirmedAt: undefined, appliedAt: undefined,
    })
    mockDrafts.set(next.id, next)
    return clone(next)
  },

  async confirm(id: string, expectedRevision: number, reviewAcknowledged = false): Promise<ShotDetailDraft> {
    if (!useMock) {
      const response = await api.post(`/shot-detail-drafts/${id}/confirm/`, {
        expected_revision: expectedRevision,
        review_acknowledged: reviewAcknowledged,
      })
      return adaptShotDetailDraft(unwrap<Raw>(response.data))
    }
    await wait()
    const current = mockDrafts.get(id)
    if (!current || current.revision !== expectedRevision) throw new Error('高细节草稿版本已变化，请刷新后重试。')
    if (current.reviewIssues.length && !reviewAcknowledged) throw new Error('请先阅读并确认高细节候选风险。')
    const next = mockFlags({ ...current, status: 'confirmed', revision: current.revision + 1, confirmedAt: new Date().toISOString() })
    mockDrafts.set(id, next)
    return clone(next)
  },

  async apply(id: string, options: {
    expectedRevision: number
    languageMode: ShotDetailDraftLanguageMode
    englishReviewAcknowledged?: boolean
    reviewAcknowledged?: boolean
  }): Promise<ShotDetailDraftApplyResult> {
    if (!useMock) {
      const response = await api.post(`/shot-detail-drafts/${id}/apply/`, {
        expected_revision: options.expectedRevision,
        language_mode: options.languageMode,
        review_acknowledged: options.reviewAcknowledged === true,
        ...(options.languageMode === 'bilingual_confirmed' ? {
          english_review_acknowledged: options.englishReviewAcknowledged === true,
        } : {}),
      })
      return adaptShotDetailDraftApplyResult(unwrap<Raw>(response.data))
    }
    await wait()
    const current = mockDrafts.get(id)
    if (!current || current.revision !== options.expectedRevision) throw new Error('高细节草稿版本已变化，请刷新后重试。')
    if (current.status !== 'confirmed') throw new Error('请先确认高细节草稿，再应用到正式镜头。')
    if (current.reviewIssues.length && !options.reviewAcknowledged) throw new Error('请先阅读并确认高细节候选风险。')
    if (current.reviewSummary.requiresEditCount) throw new Error('当前候选含有需要修正的关联问题，正式应用前请先处理。')
    if (options.languageMode === 'bilingual_confirmed' && !options.englishReviewAcknowledged) throw new Error('请先确认已逐项核对英文。')
    const next = mockFlags({ ...current, status: 'applied', revision: current.revision + 1, appliedAt: new Date().toISOString() })
    mockDrafts.set(id, next)
    return {
      draft: clone(next),
      appliedShot: {
        id: current.shotId,
        detailedDescription: options.languageMode === 'bilingual_confirmed' ? current.draftData.detailEn : current.draftData.detailZh,
        metadata: { shot_detail_draft_id: current.id, applied_language_mode: options.languageMode },
      },
      translationWritten: options.languageMode === 'bilingual_confirmed',
    }
  },
}
