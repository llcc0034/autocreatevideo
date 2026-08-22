import { api, unwrap } from './api'
import {
  adaptSeriesConceptDraft,
  seriesConceptDraftPayload,
} from './seriesConceptDraft'
import {
  adaptPromptProviders,
  REMOTE_PROMPT_EGRESS_NOTICE,
} from './providerCapabilities'
import type {
  SeriesConceptDraftInput,
  SeriesConceptDraftResult,
  SeriesConceptGenerationCapability,
} from '@/types'

type Raw = Record<string, unknown>
const useMock = import.meta.env.VITE_USE_MOCK === 'true'
const objectValue = (value: unknown): Raw => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as Raw : {}
)
const positiveInteger = (value: unknown): number => {
  const number = Number(value)
  return Number.isSafeInteger(number) && number > 0 ? number : 0
}
const exactProvider = (value: unknown): 'local_qwen' | 'deepseek_api' | '' => {
  const provider = String(value || '').trim().toLowerCase()
  return provider === 'local_qwen' || provider === 'deepseek_api' ? provider : ''
}

const mockCapability = (): SeriesConceptGenerationCapability => {
  const providerSet = adaptPromptProviders({
    default_provider: 'local_qwen',
    providers: [
      {
        id: 'local_qwen', label: '本地 Qwen', enabled: false, configured: false, ready: false,
        model: '', transport: 'local', notice: 'Mock 模式未连接真实本地模型。',
        series_concept_draft_suitability: 'experimental',
        series_concept_draft_suitability_reason_code: 'series_concept_strict_structure_experimental',
        series_concept_draft_suitability_notice: 'Mock 模式未验证本地模型的完整结构能力。',
      },
      {
        id: 'deepseek_api', label: 'DeepSeek API', enabled: false, configured: false, ready: false,
        model: '', transport: 'remote', data_egress_notice: REMOTE_PROMPT_EGRESS_NOTICE,
        notice: 'Mock 模式未连接真实 DeepSeek API。',
        series_concept_draft_suitability: 'recommended',
        series_concept_draft_suitability_reason_code: 'series_concept_strict_structure_recommended',
        series_concept_draft_suitability_notice: 'Mock 模式不执行真实远端生成。',
      },
    ],
  })
  return {
    supported: false,
    reviewRequired: true,
    preferredProvider: 'deepseek_api',
    defaultEpisodeDurationSeconds: 60,
    minimumShotDurationMs: 5_000,
    maximumShotDurationMs: 15_000,
    notice: 'Mock 模式只验证页面交互，不伪造 AI 剧本候选。',
    ...providerSet,
  }
}

export const seriesConceptDraftRepository = {
  async capability(): Promise<SeriesConceptGenerationCapability> {
    if (useMock) return mockCapability()
    const raw = unwrap<Raw>((await api.get('/capabilities/creative-drafts/')).data)
    const providerSet = adaptPromptProviders(raw)
    const preferredProvider = exactProvider(raw.series_concept_draft_preferred_provider)
    const limits = objectValue(raw.series_concept_draft_limits)
    const defaultEpisodeDurationSeconds = positiveInteger(limits.default_episode_duration_seconds)
    const minimumShotDurationMs = positiveInteger(limits.minimum_shot_duration_ms)
    const maximumShotDurationMs = positiveInteger(limits.maximum_shot_duration_ms)
    if (
      !defaultEpisodeDurationSeconds
      || !minimumShotDurationMs
      || maximumShotDurationMs < minimumShotDurationMs
      || !preferredProvider
      || !providerSet.providers.some((provider) => provider.id === preferredProvider)
      || providerSet.providers.some((provider) => (
        !provider.seriesConceptDraftSuitability
        || !provider.seriesConceptDraftSuitabilityReasonCode
        || !provider.seriesConceptDraftSuitabilityNotice
      ))
      || raw.series_concept_draft_review_required !== true
      || raw.series_concept_draft_persisted !== false
    ) throw new Error('服务端未返回有效的连续剧候选能力配置。')
    return {
      supported: raw.series_concept_draft_supported === true,
      reviewRequired: raw.series_concept_draft_review_required === true,
      preferredProvider,
      defaultEpisodeDurationSeconds,
      minimumShotDurationMs,
      maximumShotDurationMs,
      notice: String(raw.notice || '').trim() || undefined,
      ...providerSet,
    }
  },

  async generate(input: SeriesConceptDraftInput): Promise<SeriesConceptDraftResult> {
    if (useMock) {
      throw new Error('Mock 模式不生成或伪造连续剧候选；请连接 Django 与真实模型通道后重试。')
    }
    const response = await api.post(
      '/series-concept-drafts/generate/',
      seriesConceptDraftPayload(input),
      { timeout: 0 },
    )
    const result = adaptSeriesConceptDraft(unwrap<Raw>(response.data))
    if (
      result.runtime.provider !== input.provider
      || result.format.episodeCount !== input.episodeCount
      || result.format.shotCount !== input.episodeCount * input.shotsPerEpisode
      || result.format.episodeDurationSeconds !== input.episodeDurationSeconds
      || result.format.shotDurationsMs.length !== input.episodeCount
      || result.format.shotDurationsMs.some((row) => row.length !== input.shotsPerEpisode)
    ) {
      throw new Error('生成服务返回的通道或剧本规模与本次请求不一致，请重试。')
    }
    return result
  },
}
