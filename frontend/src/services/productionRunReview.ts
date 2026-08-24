import type { JsonObject, StageRun } from './platformTypes'

export interface ScriptReviewPayload {
  sourceStageKey: 'script_generate' | 'script_structure'
  candidateKind: 'generated_script_preview' | 'structured_script_preview'
  submissionSemantics: string
  scriptRevisionId: string
  artifactId: string
  attemptId: string | null
  contentSha256: string
  resultSha256: string
  snapshotSha256: string
  title: string
  logline: string
  warnings: string[]
}

export interface BibleCandidate {
  candidateId: string
  entityType: 'character' | 'scene' | 'prop'
  name: string
  description: string
  visualIdentityEn: string
  occurrence: number
  recommended: boolean
  reviewedContentSha256: string
}

export interface BibleReviewPayload {
  scriptRevisionId: string
  scriptContentSha256: string
  sourceFingerprint: string
  candidateSha256: string
  snapshotSha256: string
  collections: {
    characters: BibleCandidate[]
    scenes: BibleCandidate[]
    props: BibleCandidate[]
  }
  diagnostics: JsonObject[]
}

export interface StoryboardReviewEpisode {
  id: string
  number: number
  title: string
  shotCount: number
  imageCount: number
  videoCount: number
  audioShotCount: number
  durationMs: number
}

export interface StoryboardReviewPayload {
  snapshotSha256: string
  canonicalSha256: string
  episodeCount: number
  shotCount: number
  imageCount: number
  videoCount: number
  audioShotCount: number
  durationMs: number
  episodes: StoryboardReviewEpisode[]
}

export interface PromptNormalizationCandidate {
  requestId: string
  scopeType: string
  scopeId: string
  fieldKey: string
  label: string
  sourceText: string
  sourceHash: string
  contextHash: string
  translatedText: string
}

export interface PromptNormalizationReviewPayload {
  snapshotSha256: string
  attemptId: string | null
  resultSha256: string
  provider: string
  model: string
  submissionSemantics: string
  candidates: PromptNormalizationCandidate[]
}

export type ReviewParseResult<T> = { value: T; error: '' } | { value: null; error: string }

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const SHA256 = /^[0-9a-f]{64}$/
const record = (value: unknown): JsonObject | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : null
const failed = <T>(error: string): ReviewParseResult<T> => ({ value: null, error })
const text = (value: unknown): string | null => typeof value === 'string' ? value : null
const uuid = (value: unknown): string | null => typeof value === 'string' && UUID.test(value) ? value : null
const hash = (value: unknown): string | null => typeof value === 'string' && SHA256.test(value) ? value : null
const stringList = (value: unknown): string[] | null => Array.isArray(value) && value.every((item) => typeof item === 'string') ? value as string[] : null
const objectList = (value: unknown): JsonObject[] | null => Array.isArray(value) && value.every((item) => record(item) !== null) ? value as JsonObject[] : null
const sourceStage = (stages: StageRun[], key: string): StageRun | null => [...stages].sort((a, b) => b.ordinal - a.ordinal).find((stage) => stage.stageKey === key && stage.status === 'succeeded') || null

export const parseScriptReviewPayload = (stages: StageRun[]): ReviewParseResult<ScriptReviewPayload> => {
  const stage = [...stages].sort((a, b) => b.ordinal - a.ordinal).find((item) => (
    ['script_generate', 'script_structure'].includes(item.stageKey) && item.status === 'succeeded'
  )) || null
  if (!stage) return failed('缺少已成功的 script_generate 或 script_structure 阶段，不能核验剧本来源。')
  if (!hash(stage.outputSnapshotSha256)) return failed(`${stage.stageKey} 输出快照 hash 缺失或格式错误。`)
  const snapshot = record(stage.outputSnapshot)
  if (!snapshot || snapshot.stage_key !== stage.stageKey) return failed(`${stage.stageKey} 输出快照缺失或 stage_key 不匹配。`)
  const payload = record(snapshot.review_payload)
  const candidateKind = text(payload?.kind)
  const expectedKind = stage.stageKey === 'script_generate' ? 'generated_script_preview' : 'structured_script_preview'
  if (!payload || candidateKind !== expectedKind) return failed(`${stage.stageKey} review_payload kind 缺失或不受支持。`)
  const revisionId = uuid(payload.script_revision_id)
  const artifactId = uuid(payload.artifact_id)
  const attemptId = payload.attempt_id === null ? null : uuid(payload.attempt_id)
  const contentSha256 = hash(payload.content_sha256)
  const submissionSemantics = text(payload.submission_semantics) || ''
  const noSubmission = submissionSemantics === 'no_submission'
  const resultSha256 = noSubmission && payload.result_sha256 === '' ? '' : hash(payload.result_sha256)
  const title = text(payload.title)
  const logline = text(payload.logline)
  const warnings = payload.warnings === undefined ? [] : stringList(payload.warnings)
  if (!revisionId || !artifactId || (!noSubmission && !attemptId) || (noSubmission && attemptId !== null)) return failed('剧本审核快照中的 revision、artifact 或 attempt 标识无效。')
  if (!contentSha256 || resultSha256 === null) return failed('剧本审核快照中的内容 hash 或模型结果 hash 无效。')
  if (title === null || logline === null || warnings === null) return failed('剧本审核快照中的标题、梗概或 warnings 结构无效。')
  const canonical = record(snapshot.canonical)
  if (!canonical || canonical.id !== revisionId || canonical.content_sha256 !== contentSha256) return failed('剧本审核快照与正式 ScriptRevision 指纹不一致。')
  return {
    value: {
      sourceStageKey: stage.stageKey as ScriptReviewPayload['sourceStageKey'],
      candidateKind: candidateKind as ScriptReviewPayload['candidateKind'],
      submissionSemantics,
      scriptRevisionId: revisionId,
      artifactId,
      attemptId,
      contentSha256,
      resultSha256,
      snapshotSha256: stage.outputSnapshotSha256,
      title,
      logline,
      warnings,
    },
    error: '',
  }
}

const parseCandidate = (value: unknown, expectedType: BibleCandidate['entityType']): BibleCandidate | null => {
  const row = record(value)
  if (!row || row.entity_type !== expectedType) return null
  const candidateId = text(row.candidate_id)
  const name = text(row.name)
  const description = text(row.description)
  const visualIdentityEn = text(row.visual_identity_en)
  const reviewedContentSha256 = hash(row.reviewed_content_sha256)
  const occurrence = Number(row.occurrence)
  if (!candidateId?.trim() || !name?.trim() || description === null || visualIdentityEn === null || !reviewedContentSha256 || !Number.isInteger(occurrence) || occurrence < 0 || typeof row.recommended !== 'boolean') return null
  return { candidateId, entityType: expectedType, name, description, visualIdentityEn, occurrence, recommended: row.recommended, reviewedContentSha256 }
}

export const parseBibleReviewPayload = (stages: StageRun[]): ReviewParseResult<BibleReviewPayload> => {
  const stage = sourceStage(stages, 'bible_materialize')
  if (!stage) return failed('缺少已成功的 bible_materialize 阶段，不能核验 Bible 候选。')
  if (!hash(stage.outputSnapshotSha256)) return failed('bible_materialize 输出快照 hash 缺失或格式错误。')
  const snapshot = record(stage.outputSnapshot)
  if (!snapshot || snapshot.stage_key !== 'bible_materialize') return failed('bible_materialize 输出快照缺失或 stage_key 不匹配。')
  const payload = record(snapshot.review_payload)
  if (!payload || payload.kind !== 'script_bible_candidates') return failed('bible_materialize review_payload kind 缺失或不受支持。')
  const scriptRevisionId = uuid(payload.script_revision_id)
  const scriptContentSha256 = hash(payload.script_content_sha256)
  const sourceFingerprint = hash(payload.source_fingerprint)
  const candidateSha256 = hash(payload.candidate_sha256)
  const diagnostics = objectList(payload.diagnostics)
  const collections = record(payload.collections)
  if (!scriptRevisionId || !scriptContentSha256 || !sourceFingerprint || !candidateSha256) return failed('Bible 审核快照的 revision 或内容指纹无效。')
  if (!diagnostics || !collections) return failed('Bible 审核快照的 collections 或 diagnostics 结构无效。')
  const parsed = {} as BibleReviewPayload['collections']
  for (const [key, entityType] of [['characters', 'character'], ['scenes', 'scene'], ['props', 'prop']] as const) {
    const rows = collections[key]
    if (!Array.isArray(rows) || rows.length === 0) return failed(`Bible 审核快照缺少 ${key} 候选。`)
    const candidates = rows.map((row) => parseCandidate(row, entityType))
    if (candidates.some((row) => row === null)) return failed(`Bible 审核快照的 ${key} 候选结构无效。`)
    parsed[key] = candidates as BibleCandidate[]
  }
  return { value: { scriptRevisionId, scriptContentSha256, sourceFingerprint, candidateSha256, snapshotSha256: stage.outputSnapshotSha256, collections: parsed, diagnostics }, error: '' }
}

export const parseStoryboardReviewPayload = (stages: StageRun[]): ReviewParseResult<StoryboardReviewPayload> => {
  const stage = sourceStage(stages, 'storyboard')
  if (!stage) return failed('缺少已成功的 storyboard 阶段，不能核验分镜来源。')
  if (!hash(stage.outputSnapshotSha256)) return failed('storyboard 输出快照 hash 缺失或格式错误。')
  const snapshot = record(stage.outputSnapshot)
  if (!snapshot || snapshot.stage_key !== 'storyboard' || snapshot.formal_object_type !== 'storyboard') return failed('storyboard 正式输出快照缺失或类型不匹配。')
  const canonicalSha256 = hash(snapshot.canonical_sha256)
  const canonical = record(snapshot.canonical)
  if (!canonicalSha256 || !canonical) return failed('storyboard canonical 或服务端 canonical hash 缺失。')
  if (!Array.isArray(canonical.episodes) || canonical.episodes.length === 0) return failed('storyboard canonical 中没有 Episode。')

  const episodes: StoryboardReviewEpisode[] = []
  for (const episodeValue of canonical.episodes) {
    const episode = record(episodeValue)
    const episodeId = uuid(episode?.id)
    const episodeNumber = Number(episode?.number)
    const episodeTitle = text(episode?.title)
    if (!episode || !episodeId || !Number.isInteger(episodeNumber) || episodeNumber < 1 || episodeTitle === null || !Array.isArray(episode.shots) || episode.shots.length === 0) {
      return failed('storyboard canonical 的 Episode 结构无效或没有 Shot。')
    }
    let durationMs = 0
    let imageCount = 0
    let videoCount = 0
    let audioShotCount = 0
    for (const shotValue of episode.shots) {
      const shot = record(shotValue)
      const shotId = uuid(shot?.id)
      const shotNumber = Number(shot?.number)
      const shotDurationMs = Number(shot?.duration_ms)
      const mediaType = text(shot?.media_type)
      if (!shot || !shotId || !Number.isInteger(shotNumber) || shotNumber < 1 || !Number.isInteger(shotDurationMs) || shotDurationMs <= 0 || !['image', 'video'].includes(mediaType || '') || typeof shot.include_audio !== 'boolean') {
        return failed('storyboard canonical 的 Shot 标识、媒体类型、音频或时长结构无效。')
      }
      durationMs += shotDurationMs
      if (mediaType === 'image') imageCount += 1
      else videoCount += 1
      if (shot.include_audio) audioShotCount += 1
    }
    episodes.push({ id: episodeId, number: episodeNumber, title: episodeTitle, shotCount: episode.shots.length, imageCount, videoCount, audioShotCount, durationMs })
  }
  return {
    value: {
      snapshotSha256: stage.outputSnapshotSha256,
      canonicalSha256,
      episodeCount: episodes.length,
      shotCount: episodes.reduce((total, item) => total + item.shotCount, 0),
      imageCount: episodes.reduce((total, item) => total + item.imageCount, 0),
      videoCount: episodes.reduce((total, item) => total + item.videoCount, 0),
      audioShotCount: episodes.reduce((total, item) => total + item.audioShotCount, 0),
      durationMs: episodes.reduce((total, item) => total + item.durationMs, 0),
      episodes,
    },
    error: '',
  }
}

export const parsePromptNormalizationReviewPayload = (
  stages: StageRun[],
): ReviewParseResult<PromptNormalizationReviewPayload> => {
  const stage = sourceStage(stages, 'script_normalize')
  if (!stage) return failed('缺少已成功的 script_normalize 阶段，不能核验英文候选。')
  if (!hash(stage.outputSnapshotSha256)) return failed('script_normalize 输出快照 hash 缺失或格式错误。')
  const snapshot = record(stage.outputSnapshot)
  if (!snapshot || snapshot.stage_key !== 'script_normalize') return failed('script_normalize 输出快照缺失或 stage_key 不匹配。')
  const payload = record(snapshot.review_payload)
  if (!payload || payload.kind !== 'prompt_normalization_candidates') return failed('script_normalize review_payload kind 缺失或不受支持。')
  const attemptId = uuid(payload.attempt_id)
  const resultSha256 = hash(payload.result_sha256)
  const provider = text(payload.provider)
  const model = text(payload.model)
  const submissionSemantics = text(payload.submission_semantics)
  if (!resultSha256 || provider === null || model === null || submissionSemantics === null) return failed('提示词标准化审核快照的 attempt、hash 或模型信息无效。')
  if (!Array.isArray(payload.candidates) || payload.candidates.length > 100) return failed('提示词标准化审核快照候选数量无效。')
  const noSubmission = submissionSemantics === 'no_submission'
  if (noSubmission && (attemptId !== null || payload.candidates.length !== 0)) return failed('no_submission 审核快照不能包含 attempt 或候选。')
  if (!noSubmission && (!attemptId || payload.candidates.length === 0 || !provider.trim() || !model.trim() || !submissionSemantics.trim())) return failed('已提交的提示词规范化审核快照缺少 attempt、候选或模型信息。')
  const candidates: PromptNormalizationCandidate[] = []
  const seen = new Set<string>()
  for (const value of payload.candidates) {
    const row = record(value)
    const requestId = text(row?.request_id)
    const scopeType = text(row?.scope_type)
    const scopeId = uuid(row?.scope_id)
    const fieldKey = text(row?.field_key)
    const label = text(row?.label)
    const sourceText = text(row?.source_text)
    const sourceHash = hash(row?.source_hash)
    const contextHash = hash(row?.context_hash)
    const translatedText = text(row?.translated_text)
    if (!row || !requestId?.trim() || requestId.length > 191 || seen.has(requestId) || !scopeType?.trim() || !scopeId || !fieldKey?.trim() || label === null || sourceText === null || !sourceHash || !contextHash || translatedText === null) return failed('提示词标准化候选的标识、字段或 hash 无效。')
    seen.add(requestId)
    candidates.push({ requestId, scopeType, scopeId, fieldKey, label, sourceText, sourceHash, contextHash, translatedText })
  }
  return {
    value: {
      snapshotSha256: stage.outputSnapshotSha256,
      attemptId,
      resultSha256,
      provider,
      model,
      submissionSemantics,
      candidates,
    },
    error: '',
  }
}
