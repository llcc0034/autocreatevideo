import { api, fetchAllPages, unwrap } from './api'
import { formatDialogueLines, parseDialogueText } from './dialogue'
import { adaptPromptArtifact } from './promptArtifactAdapter'
import { adaptShotDetailedDescription, shotDetailedDescriptionPayload } from './shotDetailSync'
import { visualRulesProjectPersistenceFields } from './visualRulesDraft'
import { adaptTaskProgressTelemetry } from './taskProgress'
import { legacyShotVersionKey, normalizeShotVersionScopeKey } from './shotVersionScope'
import { shotReferenceBindingsPayload } from './storyboardReferenceBindings'
import type { Asset, AssetType, BibleItem, Episode, EpisodeShotVersion, PromptArtifactPayload, ReferenceBindings, ReferenceEntityBinding, ReferencePurpose, SeriesProject, Shot, StylePreset, TaskOutputMedia, TaskReferenceAssetSummary, Workflow, WorkflowBatchSlot, WorkflowNode } from '@/types'

export { adaptPromptArtifact } from './promptArtifactAdapter'

type Raw = Record<string, any>

const styleIdBySlug = new Map<string, string>()
const styleSlugById = new Map<string, string>()
const styleBySlug = new Map<string, StylePreset>()
const projectTitleById = new Map<string, string>()
const episodeContextById = new Map<string, { projectId: string; number: number; title: string }>()
const shotContextById = new Map<string, { episodeId: string; number: number; title: string }>()

export const isBackendId = (value?: string) => Boolean(value && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value))

const asList = <T = Raw>(payload: unknown): T[] => unwrap<T[]>(payload) || []
const relationId = (value: unknown) => String(value && typeof value === 'object' ? (value as Raw).id || '' : value || '')
const normalizeNegativePrompt = (value: unknown): string => {
  const seen = new Set<string>()
  return String(value || '')
    .split(/[,，;；\n]+/)
    .map((item) => item.trim())
    .filter((item) => {
      if (!item) return false
      const key = item.toLocaleLowerCase()
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
    .join('; ')
}
const timelineValue = (key: string, value: unknown): string => {
  if (key.endsWith('_ms') && Number.isFinite(Number(value))) {
    const seconds = Number(value) / 1000
    return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} 秒`
  }
  if (typeof value === 'object' && value !== null) {
    try { return JSON.stringify(value) } catch { return String(value) }
  }
  return String(value)
}
const timelineDetails = (value: unknown): string | undefined => {
  if (typeof value === 'string') return value.trim() || undefined
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const labels: Record<string, string> = {
    attempt: '尝试次数', source_task_id: '来源任务', submission_state: '提交状态',
    prompt_id: 'Prompt ID', backend: '执行后端', queue_wait_ms: '排队耗时',
    duration_ms: '执行耗时', total_duration_ms: '总耗时', reason: '原因'
  }
  const details = Object.entries(value as Raw)
    .filter(([,item])=>item !== null && item !== undefined && item !== '')
    .map(([key,item])=>`${labels[key] || key}：${timelineValue(key, item)}`)
  return details.length ? details.join(' · ') : undefined
}

const hiddenTaskParameterKey = (key: string) => /(?:^|_)(?:paths?|directories|directory|dirs?|folders?|install_path|output_path|workflow_path|local_path|api_key|tokens?|secrets?|password)$|^staged_inputs$/i.test(key)
const absoluteLocalPath = (value: string) => /^[a-z]:[\\/]|^\\\\|^file:\/\/|^\/(?!api(?:\/|$)|media(?:\/|$)|static(?:\/|$)|assets(?:\/|$)).+/i.test(value.trim())
const safeTaskParameterValue = (value: unknown, key = '', depth = 0): unknown => {
  if (hiddenTaskParameterKey(key) || depth > 6) return undefined
  if (typeof value === 'string') return absoluteLocalPath(value) ? undefined : value
  if (value === null || typeof value === 'number' || typeof value === 'boolean') return value
  if (Array.isArray(value)) return value.map((item) => safeTaskParameterValue(item, key, depth + 1)).filter((item) => item !== undefined)
  if (!value || typeof value !== 'object') return undefined
  const entries = Object.entries(value as Raw)
    .map(([childKey, item]) => [childKey, safeTaskParameterValue(item, childKey, depth + 1)] as const)
    .filter(([, item]) => item !== undefined)
  return Object.fromEntries(entries)
}
const taskInputContractStatus = (raw: Raw): 'available' | 'unavailable' => {
  const status = String(raw.input_contract_status || '')
  if (!status) return 'available'
  if (status === 'available' || status === 'unavailable') return status
  throw new Error(`任务 ${String(raw.id || '未知')} 的 input_contract_status 无效`)
}
const taskInputContractCode = (
  raw: Raw,
  status: 'available' | 'unavailable',
): '' | 'legacy_schema_unsupported' | 'frozen_contract_invalid' => {
  const code = String(raw.input_contract_code || '')
  if (status === 'available') {
    if (code) throw new Error(`任务 ${String(raw.id || '未知')} 的可用输入合同包含异常原因码`)
    return ''
  }
  if (code === 'legacy_schema_unsupported' || code === 'frozen_contract_invalid') return code
  throw new Error(`任务 ${String(raw.id || '未知')} 缺少稳定的输入合同不可用原因码`)
}
const taskInputParameters = (
  raw: Raw,
  status: 'available' | 'unavailable',
): Record<string, unknown> | undefined => {
  if (status === 'unavailable') {
    if (raw.input_parameters !== null) {
      throw new Error(`任务 ${String(raw.id || '未知')} 的不可用输入合同必须返回 null`)
    }
    return undefined
  }
  if (!raw.input_parameters || typeof raw.input_parameters !== 'object' || Array.isArray(raw.input_parameters)) {
    throw new Error(`任务 ${String(raw.id || '未知')} 缺少冻结 input_parameters 合同`)
  }
  return (safeTaskParameterValue(raw.input_parameters, 'input_parameters') || {}) as Record<string, unknown>
}
const taskAssetSummaries = (
  parameters: Record<string, unknown>,
  field: 'reference_assets' | 'continuity_assets',
  fallbackLabel: string
): TaskReferenceAssetSummary[] => {
  if (!Array.isArray(parameters[field])) {
    throw new Error(`任务 input_parameters.${field} 必须是冻结数组`)
  }
  const rows = parameters[field]
  const optionalNumber = (value: unknown) => value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) ? undefined : Number(value)
  return rows.map((item, index) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      throw new Error(`任务 input_parameters.${field}[${index}] 必须是冻结对象`)
    }
    const row = item as Raw
    const category = String(row.category || '')
    const rawKind = String(row.media_kind || '').toLowerCase()
    if (!['image', 'video', 'audio'].includes(rawKind)) {
      throw new Error(`任务 input_parameters.${field}[${index}].media_kind 缺失或无效`)
    }
    const mediaKind = rawKind as TaskReferenceAssetSummary['mediaKind']
    return {
      assetId: relationId(row.asset_id) || undefined,
      displayName: String(row.display_name || row.name || row.entity_label || row.entity_name || `${fallbackLabel} ${index + 1}`),
      mediaKind,
      previewUrl: String(row.preview_url || ''),
      category: category || undefined,
      inputLabel: String(row.input_label || '') || undefined,
      entityLabel: String(row.entity_label || row.entity_name || '') || undefined,
      useEmbeddedAudio: row.use_embedded_audio === true,
      sourceFps: optionalNumber(row.source_fps),
      effectiveFps: optionalNumber(row.effective_fps),
      effectiveDurationMs: optionalNumber(row.effective_duration_ms),
      cfrGuaranteed: typeof row.cfr_guaranteed === 'boolean' ? row.cfr_guaranteed : undefined,
      pairedAudioDurationAligned: typeof row.paired_audio_duration_aligned === 'boolean' ? row.paired_audio_duration_aligned : undefined
    }
  })
}

export const adaptStyles = (payload: unknown): StylePreset[] => {
  styleIdBySlug.clear()
  styleSlugById.clear()
  styleBySlug.clear()
  return asList(payload).map((raw: Raw) => {
    const slug = String(raw.slug || '')
    const backendId = String(raw.id || '')
    const category = String(raw.category || '')
    const rawAssetCount = raw.asset_count === null || raw.asset_count === undefined
      ? Number.NaN
      : Number(raw.asset_count)
    const style: StylePreset = {
      id: slug,
      backendId,
      slug,
      name: String(raw.name || ''),
      englishName: slug.replace(/-/g, ' '),
      category,
      description: String(raw.description || ''),
      accent: '#d9a15d',
      gradient: 'linear-gradient(145deg,#463322,#111419 62%,#313b42)',
      tags: category ? [category] : [],
      assetCount: Number.isFinite(rawAssetCount) && rawAssetCount >= 0 ? rawAssetCount : null,
      promptPrefix: String(raw.visual_prompt || ''),
      negativePrompt: String(raw.negative_prompt || ''),
    }
    if (slug && backendId) {
      styleIdBySlug.set(slug, backendId)
      styleSlugById.set(backendId, slug)
    }
    if (slug) styleBySlug.set(slug, style)
    return style
  })
}

export const backendStyleId = (slug?: string) => slug ? styleIdBySlug.get(slug) || (isBackendId(slug) ? slug : undefined) : undefined
export const styleSlug = (id?: string) => id ? styleSlugById.get(id) || id : ''

const assetType = (raw: Raw):AssetType|undefined => {
  if (['character','scene','prop'].includes(raw.kind)) return raw.kind
  const mime=String(raw.mime_type||'').toLowerCase();const source=String(raw.file||raw.local_path||raw.reference_url||'').toLowerCase()
  if(raw.kind==='script'||mime.startsWith('text/')||/\.(txt|md|markdown|fountain)$/.test(source))return 'script'
  if(['audio','reference_audio'].includes(raw.kind)||mime.startsWith('audio/')||/\.(wav|mp3|aac|m4a|flac|ogg)$/.test(source))return 'audio'
  if(raw.kind==='output_video'||raw.kind==='reference_video'||mime.startsWith('video/')||/\.(mp4|mov|webm|mkv)$/.test(source))return 'video'
  if(raw.kind==='reference'&&['character','scene','prop'].includes(raw.metadata?.reference_category))return raw.metadata.reference_category
  if(['reference','output_image'].includes(raw.kind)||mime.startsWith('image/'))return 'image'
  return undefined
}
export const adaptAssets = (payload: unknown): Asset[] => asList(payload).map((raw: Raw) => ({raw,type:assetType(raw)})).filter((item):item is {raw:Raw;type:AssetType}=>Boolean(item.type)).map(({raw,type}) => ({
  id: String(raw.id), styleId: styleSlug(String(raw.style || '')), type, backendKind:String(raw.kind||''), name: raw.name, description: raw.description || '', promptHint:String(raw.prompt_hint||''), sourceInfo:String(raw.source_info||'')||undefined, checksum:String(raw.checksum||'')||undefined, ownerId:raw.owner===null?null:String(raw.owner||'')||undefined, updatedAt:String(raw.updated_at||'')||undefined, metadataSha256:String(raw.metadata_sha256||'')||undefined, isActive:raw.is_active!==false, thumbnail: ['character','scene','prop','image'].includes(type) ? raw.preview_url || raw.reference_url || '' : String(raw.metadata?.thumbnail_url||''), mediaUrl: raw.preview_url || raw.reference_url || '', downloadUrl:String(raw.download_url||''), mimeType:String(raw.mime_type||''), metadata:raw.metadata&&typeof raw.metadata==='object'?raw.metadata:{}, hasEmbeddedAudio:raw.metadata?.has_audio===true, tags: Array.isArray(raw.metadata?.tags) ? raw.metadata.tags : [raw.kind, '一致性参考'], seed: Number(raw.metadata?.seed || 0) || undefined
}))

export const adaptBibleItem = (raw: Raw): BibleItem => ({
  id: String(raw.id), slug: raw.slug, name: raw.name, description: raw.description || '', visualPrompt: raw.visual_identity || '', anchorAssetId: relationId(raw.anchor_asset) || null, referenceAssetIds: [...new Set<string>((Array.isArray(raw.reference_assets) ? raw.reference_assets : []).map((value: unknown) => relationId(value)).filter((value: string) => Boolean(value)))], locked: raw.is_locked !== false, seed: Number(raw.metadata?.seed || 0) || undefined, metadata:raw.metadata&&typeof raw.metadata==='object'?raw.metadata:{}, stableSpeakerId: raw.stable_speaker_id,
  ageAppearance: raw.age_appearance || '', faceIdentity: raw.face_identity || '', wardrobe: raw.wardrobe || '', voiceProfile: raw.voice_profile || '', personality: raw.personality || '', negativeConstraints: raw.negative_constraints || '',
  location: raw.location || '', timeOfDay: raw.time_of_day || '', lighting: raw.lighting || '', colorPalette: raw.color_palette || '', ambientSound: raw.ambient_sound || '',
  material: raw.material || '', distinctiveMarks: raw.distinctive_marks || '', continuityState: raw.continuity_state || ''
})

const adaptEntityBinding = (value: unknown): ReferenceEntityBinding | undefined => {
  if (!value || typeof value !== 'object') return undefined
  const raw = value as Raw
  const entityId = relationId(raw.entity_id ?? raw.entityId)
  const assetId = relationId(raw.asset_id ?? raw.assetId)
  return entityId && assetId ? { entityId, assetId } : undefined
}

const SAFE_REFERENCE_PURPOSE = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/
const referencePurpose = (value: unknown): ReferencePurpose => {
  const result = typeof value === 'string' ? value.trim() : ''
  return SAFE_REFERENCE_PURPOSE.test(result) ? result : ''
}

const adaptReferenceBindings = (raw: Raw): ReferenceBindings => {
  const source = (raw.reference_bindings || raw.metadata?.reference_bindings || {}) as Raw
  const characters = Array.isArray(source.characters) ? source.characters.map(adaptEntityBinding).filter(Boolean) as ReferenceEntityBinding[] : []
  const props = Array.isArray(source.props) ? source.props.map(adaptEntityBinding).filter(Boolean) as ReferenceEntityBinding[] : []
  const videos = Array.isArray(source.videos) ? source.videos.map((item:Raw)=>{
    const useEmbeddedAudio=Boolean(item.use_embedded_audio??item.useEmbeddedAudio)
    const audioAssetId=relationId(item.audio_asset_id??item.audioAssetId)||undefined
    return {
      assetId:relationId(item.asset_id??item.assetId),
      purpose:referencePurpose(item.purpose),
      useEmbeddedAudio,
      audioAssetId,
      ...((useEmbeddedAudio||audioAssetId)?{audioPurpose:referencePurpose(item.audio_purpose??item.audioPurpose)}:{})
    }
  }).filter((item)=>item.assetId) : []
  const audios = Array.isArray(source.audios) ? source.audios.map((item:Raw)=>({assetId:relationId(item.asset_id??item.assetId),purpose:referencePurpose(item.purpose)})).filter((item)=>item.assetId) : []
  return {
    firstFrameAssetId: relationId(source.first_frame_asset_id ?? source.firstFrameAssetId),
    lastFrameAssetId: relationId(source.last_frame_asset_id ?? source.lastFrameAssetId),
    firstFrameReferenceAssetId: relationId(source.first_frame_reference_asset_id ?? source.firstFrameReferenceAssetId) || undefined,
    characters,
    scene: adaptEntityBinding(source.scene),
    props,
    videos,
    audios
  }
}

export const referenceBindingsPayload = (bindings?: ReferenceBindings) => shotReferenceBindingsPayload(
  bindings ? {
    ...bindings,
    videos: (bindings.videos || []).map((item) => ({
      ...item,
      purpose: referencePurpose(item.purpose),
      ...((item.useEmbeddedAudio || item.audioAssetId)
        ? { audioPurpose: referencePurpose(item.audioPurpose) }
        : { audioPurpose: undefined }),
    })),
    audios: (bindings.audios || []).map((item) => ({ ...item, purpose: referencePurpose(item.purpose) })),
  } : undefined,
)

export const referenceAssetIdsForShot = (shot: Shot): string[] => {
  // Image-shot pixel conditioning is not declared by this Shot payload contract.
  // Bible images remain human continuity references and must not be inferred as
  // executable pixel inputs without an explicit selected-workflow capability.
  if (shot.mediaType === 'image') return []
  const bindings = shot.referenceBindings
  const ordered = [
    bindings?.firstFrameAssetId,
    bindings?.lastFrameAssetId,
    bindings?.firstFrameReferenceAssetId,
    ...(bindings?.characters || []).map((item) => item.assetId),
    bindings?.scene?.assetId,
    ...(bindings?.props || []).map((item) => item.assetId),
    ...(bindings?.videos || []).flatMap((item) => [item.assetId, item.audioAssetId]),
    ...(bindings?.audios || []).map((item) => item.assetId),
  ]
  return [...new Set(ordered.filter(Boolean).map(String))]
}

const adaptShot = (raw: Raw): Shot => {
  const mediaType = raw.media_type
  if (mediaType !== 'video' && mediaType !== 'image') {
    throw new Error('服务端 Shot 缺少有效 media_type')
  }
  const durationMs = Number(raw.duration_ms)
  if (!Number.isSafeInteger(durationMs) || durationMs < 1) {
    throw new Error('服务端 Shot 缺少有效 duration_ms')
  }
  if (typeof raw.include_audio !== 'boolean') {
    throw new Error('服务端 Shot 缺少有效 include_audio')
  }
  if (mediaType === 'image' && raw.include_audio) {
    throw new Error('图片 Shot 不能包含音频')
  }
  const metadata = raw.metadata && typeof raw.metadata === 'object' ? raw.metadata : {}
  const splitPromptFields = Number(metadata.prompt_field_version || 0) >= 2
  const storedNegativePrompt = normalizeNegativePrompt(metadata.negative_prompt || '')
  const versionNumber = Number(metadata.creative_version_shot_number || 0)
  const displayNumber = Number.isFinite(versionNumber) && versionNumber > 0 ? versionNumber : Number(raw.number || 1)
  return {
    id: String(raw.id), order: displayNumber, title: raw.title || `镜头 ${displayNumber || 1}`,
    mediaType, duration: durationMs / 1000,
    mode: String(raw.conditioning_mode || '').trim(), framing: metadata.framing || '中景', camera: raw.camera || '',
    action: raw.action || raw.narrative || '',
    ...adaptShotDetailedDescription(raw),
    continuityDirection: splitPromptFields ? raw.continuity_notes || '' : '',
    prompt: '', negativePrompt: splitPromptFields
      ? storedNegativePrompt
      : normalizeNegativePrompt(raw.continuity_notes || '人物身份漂移，多余肢体，文字水印，低清晰度'),
    characterIds: (raw.characters || []).map(String), sceneId: raw.scene ? String(raw.scene) : undefined,
    propIds: (raw.props || []).map(String), referenceAssetIds: (raw.reference_assets || []).map(String),
    referenceBindings: adaptReferenceBindings(raw),
    requiresFirstFrameContinuity: raw.requires_first_frame_continuity === true,
    requiresLastFrameContinuity: raw.requires_last_frame_continuity === true,
    metadata,
    audioEnabled: mediaType === 'video' && raw.include_audio,
    audio: { dialogue: formatDialogueLines(raw.dialogue), soundscape: raw.sound_effects || '', music: raw.music || '' },
    status: 'draft'
  }
}

const shotCreativeVersionKey = (shot: Shot): string => String(shot.metadata?.creative_version_key || '').trim()
const shotCreativeVersionActive = (shot: Shot): boolean => shot.metadata?.creative_version_active === true
const shotCreativeVersionSort = (shot: Shot): number => Number(shot.metadata?.creative_version_shot_number || shot.order || 0)

const groupEpisodeShotVersions = (episodeId: string, shots: Shot[]): { shots: Shot[]; shotVersions?: EpisodeShotVersion[]; activeShotVersionKey?: string } => {
  const initialVersionKey = legacyShotVersionKey(episodeId)
  const groups = new Map<string, EpisodeShotVersion>()
  for (const shot of shots) {
    const key = shotCreativeVersionKey(shot) || initialVersionKey
    const label = String(shot.metadata?.creative_version_label || (key === initialVersionKey ? '初始分镜' : '分镜版本'))
    const group = groups.get(key) || {
      key,
      label,
      active: false,
      mode: String(shot.metadata?.creative_version_mode || '') || undefined,
      shotCount: 0,
      createdAt: String(shot.metadata?.creative_version_applied_at || '') || undefined,
      shots: [],
    }
    group.active = group.active || shotCreativeVersionActive(shot)
    group.shots.push(shot)
    group.shotCount = group.shots.length
    if (!group.createdAt && shot.metadata?.creative_version_applied_at) {
      group.createdAt = String(shot.metadata.creative_version_applied_at)
    }
    groups.set(key, group)
  }
  const shotVersions = [...groups.values()].map((group) => ({
    ...group,
    shots: group.shots.slice().sort((left, right) => shotCreativeVersionSort(left) - shotCreativeVersionSort(right)),
  })).sort((left, right) => {
    if (left.active !== right.active) return left.active ? -1 : 1
    return String(right.createdAt || '').localeCompare(String(left.createdAt || ''))
  })
  const active = shotVersions.find((group) => group.active) || shotVersions[0]
  return {
    shots: active ? active.shots : [],
    shotVersions,
    activeShotVersionKey: active?.key,
  }
}

const adaptEpisode = (raw: Raw, shotRows: Raw[], promptRows: Raw[]): Episode => {
  const latestPrompt = new Map<string, Raw>()
  for (const prompt of [...promptRows].sort((left, right) => String(right.created_at || '').localeCompare(String(left.created_at || '')))) {
    const requestedShotIds = Array.isArray(prompt.request_options?.shot_ids) ? prompt.request_options.shot_ids.map(String) : []
    const shotIds = new Set([relationId(prompt.shot), ...requestedShotIds].filter(Boolean))
    for (const shotId of shotIds) {
      const key = `${shotId}:${prompt.prompt_type}`
      if (!latestPrompt.has(key)) latestPrompt.set(key, prompt)
    }
  }
  const allShots = [...shotRows].sort((left, right) => Number(left.number || 0) - Number(right.number || 0)).map(adaptShot).map((shot) => {
    const prompt = latestPrompt.get(`${shot.id}:${shot.mediaType}`)
    if (!prompt) return shot
    const generatedPrompt = adaptPromptArtifact(prompt)
    if (generatedPrompt.compiler_current === false) {
      return { ...shot, promptRefreshReason: 'compiler_updated' as const }
    }
    return { ...shot, prompt: generatedPrompt.integrated_multimodal_description, generatedPrompt, status: 'ready' as const }
  })
  const grouped = groupEpisodeShotVersions(String(raw.id), allShots)
  return { id: String(raw.id), number: Number(raw.number), title: raw.title, synopsis: raw.synopsis || '', script: raw.script || '', status: raw.status === 'generating' ? 'rendering' : raw.status, audioEnabled: episodeAudioEnabled(raw), shots: grouped.shots, shotVersions: grouped.shotVersions, activeShotVersionKey: grouped.activeShotVersionKey, createdAt: raw.created_at }
}

export const projectStatus = (status: string): SeriesProject['status'] => {
  if (status === 'active') return 'producing'
  if (status === 'paused' || status === 'archived' || status === 'completed' || status === 'draft') return status
  return 'draft'
}
export const backendStatus = (status: SeriesProject['status']) => {
  if (status === 'producing') return 'active'
  if (status === 'paused' || status === 'archived' || status === 'completed') return status
  return 'draft'
}
const inferResolution = (raw: Raw): SeriesProject['resolution'] => Math.max(Number(raw.width || 0), Number(raw.height || 0)) >= 3000 ? '4K' : Math.max(Number(raw.width || 0), Number(raw.height || 0)) >= 1800 ? '1080p' : '720p'

const emptyProjectBible = () => ({ characters: [], scenes: [], props: [], negativePrompt: '', colorScript: '' })

/** Adapt one list row without issuing any child-resource requests. */
export const adaptProjectSummary = (raw: Raw, detailLoaded = false): SeriesProject => {
  projectTitleById.set(String(raw.id), raw.title)
  const projectStyleId = styleSlug(String(raw.style || ''))
  const language = typeof raw.language === 'string' ? raw.language.trim() : ''
  const aspectRatio = typeof raw.aspect_ratio === 'string' ? raw.aspect_ratio.trim() : ''
  const width = Number(raw.width)
  const height = Number(raw.height)
  if (!language || !aspectRatio || !Number.isSafeInteger(width) || width < 1 || !Number.isSafeInteger(height) || height < 1) {
    throw new Error('服务端 Project 缺少有效 language、aspect_ratio 或尺寸事实')
  }
  return {
    id: String(raw.id), title: raw.title, logline: raw.logline || '', masterScript: raw.master_script || '', isFavorite: raw.is_favorite === true, coverAssetId: relationId(raw.cover_asset) || undefined, coverUrl: String(raw.cover_url || '') || undefined, continuitySummary: raw.continuity_summary || '', translationGlossary: raw.production_settings?.translation_glossary && typeof raw.production_settings.translation_glossary === 'object' ? Object.fromEntries(Object.entries(raw.production_settings.translation_glossary).filter(([source, target]) => source.trim() && String(target).trim()).map(([source, target]) => [source, String(target)])) : {}, styleId: projectStyleId, status: projectStatus(raw.status), language, aspectRatio, resolution: inferResolution(raw), width, height, defaultMode: String(raw.production_settings?.default_conditioning_mode || '').trim(),
    bible: emptyProjectBible(), episodes: [], episodeCount: Number(raw.episode_count || 0), completedEpisodeCount: Number(raw.completed_episode_count || 0), totalDurationSeconds: Number(raw.total_duration_ms || 0) / 1000, detailLoaded, createdAt: String(raw.created_at || ''), updatedAt: String(raw.updated_at || ''),
  }
}

export const adaptProject = async (raw: Raw): Promise<SeriesProject> => {
  const summary = adaptProjectSummary(raw)
  const params = { project: raw.id, ordering: 'number' }
  const [episodeRows, characterRows, sceneRows, propRows, shotRows, promptRows] = await Promise.all([
    fetchAllPages<Raw>('/episodes/', { ...params, page_size: 500 }),
    fetchAllPages<Raw>('/characters/', { project: raw.id, page_size: 500 }),
    fetchAllPages<Raw>('/scenes/', { project: raw.id, page_size: 500 }),
    fetchAllPages<Raw>('/props/', { project: raw.id, page_size: 500 }),
    fetchAllPages<Raw>('/shots/', { episode__project: raw.id, page_size: 500 }),
    fetchAllPages<Raw>('/prompts/', { project: raw.id, page_size: 500 })
  ])
  const shotsByEpisode = new Map<string, Raw[]>()
  const shotEpisode = new Map<string, string>()
  for (const shot of shotRows) {
    const episodeId = relationId(shot.episode)
    if (!episodeId) continue
    const rows = shotsByEpisode.get(episodeId) || []
    rows.push(shot); shotsByEpisode.set(episodeId, rows); shotEpisode.set(String(shot.id), episodeId)
  }
  const promptsByEpisode = new Map<string, Raw[]>()
  for (const prompt of promptRows) {
    const episodeId = relationId(prompt.episode) || shotEpisode.get(relationId(prompt.shot)) || ''
    if (!episodeId) continue
    const rows = promptsByEpisode.get(episodeId) || []
    rows.push(prompt); promptsByEpisode.set(episodeId, rows)
  }
  const episodes = [...episodeRows].sort((left, right) => Number(left.number || 0) - Number(right.number || 0)).map((episode) => {
    const episodeId = String(episode.id)
    return adaptEpisode(episode, shotsByEpisode.get(episodeId) || [], promptsByEpisode.get(episodeId) || [])
  })
  for (const episode of episodes) {
    episodeContextById.set(episode.id, { projectId: String(raw.id), number: episode.number, title: episode.title })
    for (const shot of episode.shots) shotContextById.set(shot.id, { episodeId: episode.id, number: shot.order, title: shot.title })
  }
  return {
    ...summary,
    bible: { characters: characterRows.map(adaptBibleItem), scenes: sceneRows.map(adaptBibleItem), props: propRows.map(adaptBibleItem), negativePrompt: raw.production_settings?.negative_prompt || '', colorScript: raw.production_settings?.color_script || '' },
    episodes, episodeCount: episodes.length, completedEpisodeCount: episodes.filter((episode) => episode.status === 'completed').length, totalDurationSeconds: episodes.reduce((total, episode) => total + episode.shots.reduce((sum, shot) => sum + shot.duration, 0), 0), detailLoaded: true,
  }
}

export const projectPayload = (project: Partial<SeriesProject>) => {
  const visualRules = visualRulesProjectPersistenceFields(project)
  const language = typeof project.language === 'string' ? project.language.trim() : ''
  const aspectRatio = typeof project.aspectRatio === 'string' ? project.aspectRatio.trim() : ''
  const width = Number(project.width)
  const height = Number(project.height)
  if (!language || !aspectRatio || !Number.isSafeInteger(width) || width < 1 || !Number.isSafeInteger(height) || height < 1) {
    throw new Error('项目创建或保存必须包含服务端声明的语言、画幅和宽高事实')
  }
  return {
    title: project.title, logline: project.logline || '', master_script: project.masterScript || '', status: backendStatus(project.status || 'draft'), style: backendStyleId(project.styleId), target_model: '', language, aspect_ratio: aspectRatio, width, height,
    production_settings: { default_conditioning_mode: String(project.defaultMode || '').trim(), negative_prompt: visualRules.negativePrompt, color_script: visualRules.colorScript, translation_glossary: project.translationGlossary || {} }, continuity_summary: visualRules.continuitySummary,
    ...(project.id && project.updatedAt ? { expected_updated_at: project.updatedAt } : {}),
  }
}

const slugValue = (item: BibleItem, index: number, prefix: string) => isBackendId(item.id) ? `${prefix}-${index + 1}` : item.id.toLowerCase().replace(/[^a-z0-9-]/g, '-').replace(/-+/g, '-').slice(0, 70) || `${prefix}-${Date.now()}-${index}`
export const persistBibleEntities = async (project: SeriesProject) => {
  const usedSpeakers = new Set(project.bible.characters.map((item) => item.stableSpeakerId).filter((value): value is string => Boolean(value)))
  for (const item of project.bible.characters) if (!item.stableSpeakerId) {
    let speakerNumber = 1
    while (usedSpeakers.has(`S${speakerNumber}`)) speakerNumber += 1
    item.stableSpeakerId = `S${speakerNumber}`; usedSpeakers.add(item.stableSpeakerId)
  }
  const serialize = (items: BibleItem[], prefix: string, characters = false) => items.map((item, index) => ({
    id: isBackendId(item.id) ? item.id : '', slug: item.slug || slugValue(item, index, prefix), name: item.name,
    description: item.description, visual_identity: item.visualPrompt,
    anchor_asset: isBackendId(item.anchorAssetId || '') ? item.anchorAssetId : null,
    reference_assets: item.referenceAssetIds.filter(isBackendId),
    is_locked: item.locked, metadata: { ...(item.metadata || {}), seed: item.seed }, ...(characters ? {
      stable_speaker_id: item.stableSpeakerId, age_appearance: item.ageAppearance || '', face_identity: item.faceIdentity || '',
      wardrobe: item.wardrobe || '', voice_profile: item.voiceProfile || '', personality: item.personality || '',
    } : prefix === 'scene' ? {
      location: item.location || '', time_of_day: item.timeOfDay || '', lighting: item.lighting || '',
      color_palette: item.colorPalette || '', ambient_sound: item.ambientSound || '',
    } : {
      material: item.material || '', distinctive_marks: item.distinctiveMarks || '', continuity_state: item.continuityState || '',
    })
  }))
  const persistCollection = async (
    endpoint: '/characters/' | '/scenes/' | '/props/',
    rows: Array<Raw & { id: string }>,
  ): Promise<BibleItem[]> => {
    const current = await fetchAllPages<Raw>(endpoint, { project: project.id, page_size: 500 })
    const retainedIds = new Set(rows.map((row) => row.id).filter(Boolean))
    for (const row of current) {
      const id = String(row.id || '')
      if (id && !retainedIds.has(id)) await api.delete(`${endpoint}${id}/`)
    }
    const saved: BibleItem[] = []
    for (const row of rows) {
      const { id, ...payload } = row
      const response = id
        ? await api.patch(`${endpoint}${id}/`, { project: project.id, ...payload })
        : await api.post(endpoint, { project: project.id, ...payload })
      saved.push(adaptBibleItem(unwrap<Raw>(response.data)))
    }
    return saved
  }
  project.bible.characters = await persistCollection(
    '/characters/', serialize(project.bible.characters, 'character', true),
  )
  project.bible.scenes = await persistCollection(
    '/scenes/', serialize(project.bible.scenes, 'scene'),
  )
  project.bible.props = await persistCollection(
    '/props/', serialize(project.bible.props, 'prop'),
  )
}

const episodeAudioEnabled = (raw: Raw): boolean => {
  const promptSettings = raw.prompt_settings
  if (!promptSettings || typeof promptSettings !== 'object' || Array.isArray(promptSettings) || typeof promptSettings.include_audio !== 'boolean') {
    throw new Error('服务端 Episode 缺少有效 prompt_settings.include_audio')
  }
  return promptSettings.include_audio
}

export const adaptCreatedEpisode = (raw: Raw): Episode => ({ id: String(raw.id), number: Number(raw.number), title: raw.title, synopsis: raw.synopsis || '', script: raw.script || '', status: raw.status, audioEnabled: episodeAudioEnabled(raw), shots: [], createdAt: raw.created_at })

export const dialoguePayload = (value: string, project: SeriesProject): Raw[] | undefined => (
  parseDialogueText(value, project.bible.characters)
)

export const shotPersistencePayload = (project: SeriesProject, shot: Shot): Raw => {
  const isVideo = shot.mediaType !== 'image'
  if (!isBackendId(shot.id) && !shot.action.trim()) {
    throw new Error(`新增分镜“${shot.title}”需要先填写动作与画面描述；镜头标题只用于标识，不会替代目标模型所需的导演描述。`)
  }
  const duration = Number(shot.duration)
  if (!Number.isFinite(duration) || duration <= 0) {
    throw new Error(`分镜“${shot.title}”的时长必须是正数；视频允许范围以生产能力配置为准。`)
  }
  const dialogue = isVideo ? dialoguePayload(shot.audio.dialogue, project) : []
  if (dialogue === undefined) {
    throw new Error(
      `分镜“${shot.title}”的对白格式无效。请逐行填写：[可选起止秒] 角色名或 S1（voiceover; in a low, restrained voice）：中文对白。`
    )
  }
  const characterIds = new Set(project.bible.characters.map((item) => item.id))
  const sceneIds = new Set(project.bible.scenes.map((item) => item.id))
  const propIds = new Set(project.bible.props.map((item) => item.id))
  const staleCharacterIds = shot.characterIds.filter((id) => !characterIds.has(id))
  const stalePropIds = shot.propIds.filter((id) => !propIds.has(id))
  if (staleCharacterIds.length || stalePropIds.length || (shot.sceneId && !sceneIds.has(shot.sceneId))) {
    throw new Error(
      `分镜“${shot.title}”仍引用已删除或已重建的 Bible 对象。请刷新项目后重新绑定，或在项目总览重新生成剧集与分镜。`
    )
  }
  const speakerCharacters = new Map(
    project.bible.characters.map((item) => [item.stableSpeakerId || '', item]),
  )
  for (const line of dialogue) {
    const character = speakerCharacters.get(String(line.speaker_id || ''))
    if (!character) {
      throw new Error(`分镜“${shot.title}”的对白说话人 ${line.speaker_id} 已不在当前角色 Bible；请重新拆解或修改对白。`)
    }
    if (!shot.characterIds.includes(character.id)) {
      throw new Error(`分镜“${shot.title}”的对白说话人 ${line.speaker_id} · ${character.name} 尚未绑定到本镜头；请重新拆解或在“一致性引用”中绑定。`)
    }
  }
  const shotBindings = shot.referenceBindings || { characters: [], props: [], videos: [], audios: [] }
  const staleBinding = isVideo && (
    shotBindings.characters.some((item) => !characterIds.has(item.entityId))
    || shotBindings.props.some((item) => !propIds.has(item.entityId))
    || Boolean(shotBindings.scene && !sceneIds.has(shotBindings.scene.entityId))
  )
  if (staleBinding) {
    throw new Error(
      `分镜“${shot.title}”的参考素材绑定仍指向已删除的 Bible 对象。请刷新项目后重新绑定，或重新生成剧集与分镜。`
    )
  }
  const referenceBindings = isVideo ? referenceBindingsPayload(shot.referenceBindings) : {}
  const referenceAssets = referenceAssetIdsForShot(shot)
  shot.referenceAssetIds = referenceAssets
  return {
    ...(isBackendId(shot.id) ? { id: shot.id } : {}), number: shot.order, title: shot.title, narrative: shot.action, camera: shot.camera, action: '',
    ...shotDetailedDescriptionPayload(shot),
    dialogue,
    sound_effects: isVideo ? shot.audio.soundscape : '', music: isVideo ? shot.audio.music : '', duration_ms: Math.round(duration * 1000),
    media_type: shot.mediaType, conditioning_mode: shot.mode, include_audio: isVideo && shot.audioEnabled,
    scene: isBackendId(shot.sceneId) ? shot.sceneId : null, characters: shot.characterIds.filter(isBackendId), props: shot.propIds.filter(isBackendId),
    reference_assets: referenceAssets.filter(isBackendId), reference_bindings: referenceBindings,
    requires_first_frame_continuity: shot.requiresFirstFrameContinuity,
    requires_last_frame_continuity: shot.requiresLastFrameContinuity,
    continuity_notes: shot.continuityDirection || '', metadata: { ...(shot.metadata || {}), framing: shot.framing, reference_bindings: referenceBindings, prompt_field_version: 2, negative_prompt: normalizeNegativePrompt(shot.negativePrompt) }
  }
}

const preserveShotPromptState = (saved: Shot, previous: Shot): Shot => ({
  ...saved,
  prompt: previous.prompt,
  generatedPrompt: previous.generatedPrompt,
  status: previous.status,
  promptRefreshReason: previous.promptRefreshReason,
})

export const syncShot = async (project: SeriesProject, episode: Episode, shot: Shot): Promise<Shot> => {
  const { id: _id, ...payload } = shotPersistencePayload(project, shot)
  const result = isBackendId(shot.id)
    ? unwrap<Raw>((await api.patch(`/shots/${shot.id}/`, { episode: episode.id, ...payload })).data)
    : unwrap<Raw>((await api.post('/shots/', { episode: episode.id, ...payload })).data)
  const saved = preserveShotPromptState(adaptShot(result), shot)
  shotContextById.set(saved.id, { episodeId: episode.id, number: saved.order, title: saved.title })
  return saved
}


const taskNode = (kind: string, status: Workflow['status'], progress: number): WorkflowNode[] => {
  const nodeStatus: WorkflowNode['status'] = status === 'completed' ? 'completed' : status === 'failed' ? 'failed' : status === 'running' ? 'running' : 'pending'
  return [{ id: `generation-${kind}`, type: kind === 'image' ? 'image' : 'video', label: `${kind === 'image' ? '图片' : '视频'}生成任务`, status: nodeStatus, progress: status === 'completed' ? 100 : progress }]
}

export const adaptTaskOutputMedia = (value: unknown, position = 0): TaskOutputMedia => {
  const media = value && typeof value === 'object' && !Array.isArray(value) ? value as Raw : {}
  return {
    index: Number.isFinite(Number(media.index)) ? Number(media.index) : position,
    kind: String(media.kind || 'file'),
    filename: String(media.filename || `output-${position + 1}`),
    previewUrl: String(media.preview_url || ''),
    // Production returns a managed distribution URL. Development may
    // explicitly return the signed ComfyUI master for rapid iteration.
    downloadUrl: String(media.download_url || ''),
    aiGenerated: media.ai_generated !== false,
    provenanceUrl: String(media.provenance_url || '') || undefined,
    artifactSha256: String(media.artifact_sha256 || '') || undefined,
    manifestSha256: String(media.manifest_sha256 || '') || undefined,
    distributionStatus: String(media.distribution_status || 'not_prepared'),
    distributionError: String(media.distribution_error || '') || undefined,
    publicProvenanceUrl: String(media.public_provenance_url || '') || undefined,
    publicArtifactSha256: String(media.public_artifact_sha256 || '') || undefined,
    publicManifestSha256: String(media.public_manifest_sha256 || '') || undefined,
    publicLabel: String(media.public_label || '') || undefined,
    publicVersion: String(media.public_version || '') || undefined,
    promotedAssetId: String(media.promoted_asset_id || '') || undefined,
  }
}

export const adaptTaskBatchSlots = (value: unknown): WorkflowBatchSlot[] => {
  const slots: WorkflowBatchSlot[] = []
  for (const raw of asList(value)) {
  const status = String(raw.slot_status || '')
  if (!['materialized', 'waiting_deferred', 'cancelled', 'cancelled_by_upstream', 'not_created'].includes(status)) {
    continue
  }
  const batchIndex = Number(raw.batch_index || 0)
  const batchSize = Number(raw.batch_size || 0)
  if (!Number.isFinite(batchIndex) || batchIndex <= 0 || !Number.isFinite(batchSize) || batchSize <= 0) {
    continue
  }
  slots.push({
    batchId: String(raw.batch_id || '') || undefined,
    batchKind: String(raw.batch_kind || '') || undefined,
    shotVersionKey: String(raw.shot_version_key || '') || undefined,
    shotVersionLabel: String(raw.shot_version_label || '') || undefined,
    batchIndex,
    batchSize,
    slotStatus: status as WorkflowBatchSlot['slotStatus'],
    message: String(raw.message || '') || undefined,
    workflowMode: String(raw.workflow_mode || '') || undefined,
    taskId: relationId(raw.task_id) || undefined,
    taskStatus: String(raw.task_status || '') || undefined,
    workflowId: relationId(raw.workflow_id) || undefined,
    deferredGenerationRequestId: relationId(raw.deferred_generation_request_id) || undefined,
    deferredGenerationStatus: String(raw.deferred_generation_status || '') || undefined,
    deferredUpstreamRequestId: relationId(raw.deferred_upstream_request_id) || undefined,
    episodeId: relationId(raw.episode_id) || undefined,
    episodeNumber: Number(raw.episode_number || 0) || undefined,
    episodeTitle: String(raw.episode_title || '') || undefined,
    shotId: relationId(raw.shot_id) || undefined,
    shotNumber: Number(raw.shot_number || 0) || undefined,
    shotTitle: String(raw.shot_title || '') || undefined,
    createdAt: String(raw.created_at || '') || undefined,
    updatedAt: String(raw.updated_at || '') || undefined,
  })
  }
  return slots
}

export const adaptTasks = (payload: unknown): Workflow[] => asList(payload).map((raw: Raw) => {
  const status: Workflow['status'] = raw.status === 'succeeded' ? 'completed' : ['failed', 'cancelled'].includes(raw.status) ? 'failed' : ['running', 'cancel_requested'].includes(raw.status) ? 'running' : 'queued'
  const kindLabel = raw.kind === 'image' ? '图片' : '视频'
  const progress = Number(raw.progress || (status === 'completed' ? 100 : 0))
  const progressTelemetry = adaptTaskProgressTelemetry(raw, status)
  const progressIndeterminate = progressTelemetry.progressIndeterminate
  const isStalled = raw.is_stalled === true || raw.stalled === true
  const stallReason = String(raw.stall_reason || raw.runtime_message || raw.remote_error || '') || undefined
  const currentStep = raw.status === 'cancelled' ? '任务已取消，可重新创建重试任务' : raw.status === 'cancel_requested' ? '正在取消 ComfyUI 任务' : status === 'completed' ? `${kindLabel}生成已完成` : isStalled ? stallReason || '任务长时间没有新的执行心跳' : progressTelemetry.progressSource === 'comfyui' && progressTelemetry.comfyProgress !== undefined ? `ComfyUI 当前节点采样 · ${progressTelemetry.comfyProgress}%` : progressIndeterminate ? '已提交，尚未收到 ComfyUI 量化进度' : status === 'running' ? `ComfyUI ${kindLabel}生成中` : status === 'failed' ? `${kindLabel}生成失败，请在任务详情中查看原因` : `等待 ComfyUI ${kindLabel}调度`
  const outputMedia = Array.isArray(raw.output_media) ? raw.output_media.map(adaptTaskOutputMedia) : []
  const projectId = relationId(raw.project_id ?? raw.project)
  const episodeId = relationId(raw.episode_id ?? raw.episode)
  const episodeContext = episodeContextById.get(episodeId)
  const rawShotIds = Array.isArray(raw.shot_ids) ? raw.shot_ids.map(relationId).filter(Boolean) : []
  const shotId = relationId(raw.shot_id) || rawShotIds[0] || ''
  const shotIds = [...new Set([shotId, ...rawShotIds].filter(Boolean))]
  const shotContext = shotContextById.get(shotId)
  const episodeNumber = Number(raw.episode_number || episodeContext?.number || 0) || undefined
  const episodeTitle = String(raw.episode_title || episodeContext?.title || '') || undefined
  const shotNumber = Number(raw.shot_number || shotContext?.number || 0) || undefined
  const shotTitle = String(raw.shot_title || shotContext?.title || '') || undefined
  const batchId = String(raw.batch_id || '') || undefined
  const batchIndex = Number(raw.batch_index || 0) || undefined
  const batchSize = Number(raw.batch_size || 0) || undefined
  const batchKind = String(raw.batch_kind || '') || undefined
  const rawProductionVariantNumber = Number(raw.production_variant_number || 0)
  const productionVariantNumber = Number.isInteger(rawProductionVariantNumber) && rawProductionVariantNumber > 0
    ? rawProductionVariantNumber
    : undefined
  const batchSlots = adaptTaskBatchSlots(raw.batch_slots)
  const projectName = projectTitleById.get(projectId) || String(raw.project_title || '') || `项目 ${projectId.slice(0, 8)}`
  const episodeLabel = episodeNumber ? `第${episodeNumber}集` : episodeTitle
  const shotLabel = shotNumber ? `镜头${String(shotNumber).padStart(2, '0')}${shotTitle ? ` ${shotTitle}` : ''}` : shotTitle
  const projectTitle = [projectName, episodeLabel, shotLabel || `${kindLabel}生成任务`].filter(Boolean).join(' · ')
  const rawTimeline = Array.isArray(raw.timeline) ? raw.timeline : Array.isArray(raw.history) ? raw.history : Array.isArray(raw.events) ? raw.events : []
  const timeline = rawTimeline.map((item: Raw) => ({
    event: String(item.event || item.type || 'status'),
    status: String(item.status || 'info'),
    occurredAt: String(item.occurred_at || item.timestamp || item.occurredAt || item.time || ''),
    label: String(item.label || item.title || item.event || item.type || '任务状态更新'),
    details: timelineDetails(item.details) || timelineDetails(item.message) || timelineDetails(item.description)
  })).filter((item) => item.occurredAt)
  const optionalMilliseconds = (value: unknown) => value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) ? undefined : Math.max(0, Number(value))
  const createdAt = String(raw.created_at || raw.queued_at || '')
  const queuedAt = String(raw.queued_at || raw.created_at || '') || undefined
  const finishedAt = String(raw.finished_at || raw.completed_at || '') || undefined
  const inputContractStatus = taskInputContractStatus(raw)
  const inputContractCode = taskInputContractCode(raw, inputContractStatus)
  const inputParameters = taskInputParameters(raw, inputContractStatus)
  const referenceAssets = inputParameters
    ? taskAssetSummaries(inputParameters, 'reference_assets', '参考输入')
    : []
  const continuityAssets = inputParameters
    ? taskAssetSummaries(inputParameters, 'continuity_assets', '一致性素材')
    : []
  return {
    id: String(raw.id), taskId: String(raw.id), kind: raw.kind === 'image' ? 'image' : 'video', backendStatus: String(raw.status || ''),
    generatedWorkflowId: relationId(raw.generated_workflow) || undefined,
    comfyPromptId: String(raw.comfy_prompt_id || raw.submission_token || '') || undefined,
    submissionToken: String(raw.submission_token || '') || undefined,
    submissionState: String(raw.submission_state || '') || undefined,
    backend: String(raw.backend || '') || undefined, modelProfile: String(raw.model_profile || '') || undefined,
    workflowVersion: Number(raw.workflow_version || 0) || undefined, seed: Number(raw.seed || 0),
    effectiveFrames: Number(raw.effective_frames || 0), effectiveDurationMs: Number(raw.effective_duration_ms || 0),
    inputParameters, inputContractStatus, inputContractCode, referenceAssets, continuityAssets,
    promptAvailable: inputContractStatus === 'unavailable'
      ? false
      : typeof inputParameters?.prompt_available === 'boolean' ? inputParameters.prompt_available : undefined,
    promptCharacterCount: Number(inputParameters?.prompt_character_count || 0),
    negativePromptCharacterCount: Number(inputParameters?.negative_prompt_character_count || 0),
    errorMessage: String(raw.error_message || '') || undefined, outputs: Array.isArray(raw.outputs) ? raw.outputs : [], outputMedia,
    queuedAt, startedAt: raw.started_at || undefined, finishedAt,
    submissionPreparedAt: raw.submission_prepared_at || undefined, submissionSubmittedAt: raw.submission_submitted_at || undefined,
    durationMs: optionalMilliseconds(raw.duration_ms), queueWaitMs: optionalMilliseconds(raw.queue_wait_ms), totalDurationMs: optionalMilliseconds(raw.total_duration_ms), timeline,
    lastHeartbeatAt: String(raw.last_heartbeat_at || raw.heartbeat_at || raw.updated_at || '') || undefined,
    timeoutAt: String(raw.timeout_at || raw.deadline_at || '') || undefined,
    isStalled, stallReason,
    projectId, projectTitle, status, progress, ...progressTelemetry,
    progressNote: isStalled ? stallReason || '任务长时间没有新的执行心跳，请检查 ComfyUI 状态并打开所属 ProductionRun 处理。' : progressTelemetry.progressSource === 'comfyui' && progressTelemetry.comfyProgress !== undefined ? '来自 ComfyUI 当前节点的量化采样进度，不等于整项任务完成度。' : progressIndeterminate ? '任务已提交或正在初始化，尚未收到 ComfyUI 量化进度。' : undefined,
    currentStep, nodes: taskNode(raw.kind, status, progress), createdAt,
    eta: status === 'running' ? '计算中' : undefined, attempts: Number(raw.attempts || 0), maxAttempts: Number(raw.max_attempts || 0),
    batchId, batchIndex, batchSize, batchKind,
    productionRunId: relationId(raw.production_run_id) || undefined,
    productionVariantNumber,
    shotVersionKey: String(raw.shot_version_key || '') || undefined,
    shotVersionLabel: String(raw.shot_version_label || '') || undefined,
    batchSlots, episodeId: episodeId || undefined, episodeNumber, episodeTitle,
    shotId: shotId || undefined, shotIds, shotNumber, shotTitle
  }
})
