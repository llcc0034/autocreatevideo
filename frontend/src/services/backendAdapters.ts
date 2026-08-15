import { api, fetchAllPages, unwrap } from './api'
import { stylePresets } from '@/data/mock'
import { formatDialogueLines, parseDialogueText } from './dialogue'
import { adaptPromptArtifact } from './promptArtifactAdapter'
import { adaptShotDetailedDescription, shotDetailedDescriptionPayload } from './shotDetailSync'
import { visualRulesProjectPersistenceFields } from './visualRulesDraft'
import { adaptTaskProgressTelemetry } from './taskProgress'
import type { Asset, AssetType, BibleItem, Episode, H3PromptPayload, H3ReferenceAudioPurpose, H3ReferenceBindings, H3ReferenceEntityBinding, H3ReferenceVideoPurpose, SeriesProject, Shot, StylePreset, TaskReferenceAssetSummary, Workflow, WorkflowNode } from '@/types'

export { adaptPromptArtifact } from './promptArtifactAdapter'

type Raw = Record<string, any>

const styleIdBySlug = new Map<string, string>()
const styleSlugById = new Map<string, string>()
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
const taskInputParameters = (raw: Raw): Record<string, unknown> => {
  const current = raw.input_parameters && typeof raw.input_parameters === 'object' && !Array.isArray(raw.input_parameters)
    ? raw.input_parameters
    : undefined
  const legacy = raw.capability_snapshot && typeof raw.capability_snapshot === 'object' && !Array.isArray(raw.capability_snapshot)
    ? raw.capability_snapshot
    : undefined
  return (safeTaskParameterValue(current && Object.keys(current).length ? current : legacy || {}, 'input_parameters') || {}) as Record<string, unknown>
}
const taskAssetSummaries = (
  parameters: Record<string, unknown>,
  field: 'reference_assets' | 'continuity_assets',
  fallbackLabel: string
): TaskReferenceAssetSummary[] | undefined => {
  if (!Object.prototype.hasOwnProperty.call(parameters, field)) return undefined
  const rows = Array.isArray(parameters[field]) ? parameters[field] : []
  const optionalNumber = (value: unknown) => value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) ? undefined : Number(value)
  return rows.filter((item): item is Raw => Boolean(item && typeof item === 'object' && !Array.isArray(item))).map((item, index) => {
    const category = String(item.category || '')
    const rawKind = String(item.media_kind || '').toLowerCase()
    const mediaKind: TaskReferenceAssetSummary['mediaKind'] = rawKind === 'video' || rawKind === 'audio'
      ? rawKind
      : /video/i.test(category) ? 'video' : /audio/i.test(category) ? 'audio' : 'image'
    return {
      assetId: relationId(item.asset_id) || undefined,
      displayName: String(item.display_name || item.name || item.entity_label || item.entity_name || `${fallbackLabel} ${index + 1}`),
      mediaKind,
      previewUrl: String(item.preview_url || ''),
      category: category || undefined,
      inputLabel: String(item.input_label || '') || undefined,
      entityLabel: String(item.entity_label || item.entity_name || '') || undefined,
      useEmbeddedAudio: item.use_embedded_audio === true,
      sourceFps: optionalNumber(item.source_fps),
      effectiveFps: optionalNumber(item.effective_fps),
      effectiveDurationMs: optionalNumber(item.effective_duration_ms),
      cfrGuaranteed: typeof item.cfr_guaranteed === 'boolean' ? item.cfr_guaranteed : undefined,
      pairedAudioDurationAligned: typeof item.paired_audio_duration_aligned === 'boolean' ? item.paired_audio_duration_aligned : undefined
    }
  })
}

export const adaptStyles = (payload: unknown): StylePreset[] => asList(payload).map((raw: Raw) => {
  styleIdBySlug.set(raw.slug, String(raw.id)); styleSlugById.set(String(raw.id), raw.slug)
  const preset = stylePresets.find((item) => item.slug === raw.slug)
  if (preset) return { ...preset, backendId: String(raw.id), name: raw.name || preset.name, description: raw.description || preset.description, promptPrefix: raw.visual_prompt || preset.promptPrefix }
  return { id: raw.slug, backendId: String(raw.id), slug: raw.slug, name: raw.name, englishName: raw.slug.replaceAll('-', ' '), category: raw.category || '其他', description: raw.description || '', accent: '#d9a15d', gradient: 'linear-gradient(145deg,#463322,#111419 62%,#313b42)', tags: [raw.category || '自定义'], assetCounts: { character: 0, scene: 0, prop: 0 }, promptPrefix: raw.visual_prompt || '' }
})

export const backendStyleId = (slug?: string) => slug ? styleIdBySlug.get(slug) : undefined
export const styleSlug = (id?: string) => (id && (styleSlugById.get(id) || stylePresets.find((item) => item.slug === id)?.slug)) || 'wuxia'

const assetType = (raw: Raw):AssetType|undefined => {
  if (['character','scene','prop'].includes(raw.kind)) return raw.kind
  const mime=String(raw.mime_type||'').toLowerCase();const source=String(raw.file||raw.local_path||raw.reference_url||'').toLowerCase()
  if(['audio','reference_audio'].includes(raw.kind)||mime.startsWith('audio/')||/\.(wav|mp3|aac|m4a|flac|ogg)$/.test(source))return 'audio'
  if(raw.kind==='output_video'||raw.kind==='reference_video'||mime.startsWith('video/')||/\.(mp4|mov|webm|mkv)$/.test(source))return 'video'
  if(raw.kind==='reference'&&['character','scene','prop'].includes(raw.metadata?.reference_category))return raw.metadata.reference_category
  if(['reference','output_image'].includes(raw.kind)||mime.startsWith('image/'))return 'image'
  return undefined
}
export const adaptAssets = (payload: unknown): Asset[] => asList(payload).map((raw: Raw) => ({raw,type:assetType(raw)})).filter((item):item is {raw:Raw;type:AssetType}=>Boolean(item.type)).map(({raw,type}) => ({
  id: String(raw.id), styleId: styleSlug(String(raw.style || '')), type, backendKind:String(raw.kind||''), name: raw.name, description: raw.description || '', promptHint:String(raw.prompt_hint||''), checksum:String(raw.checksum||'')||undefined, ownerId:raw.owner===null?null:String(raw.owner||'')||undefined, updatedAt:String(raw.updated_at||'')||undefined, metadataSha256:String(raw.metadata_sha256||'')||undefined, isActive:raw.is_active!==false, thumbnail: ['character','scene','prop','image'].includes(type) ? raw.preview_url || raw.reference_url || '' : String(raw.metadata?.thumbnail_url||''), mediaUrl: raw.preview_url || raw.reference_url || '', mimeType:String(raw.mime_type||''), metadata:raw.metadata&&typeof raw.metadata==='object'?raw.metadata:{}, hasEmbeddedAudio:raw.metadata?.has_audio===true, tags: Array.isArray(raw.metadata?.tags) ? raw.metadata.tags : [raw.kind, '一致性参考'], seed: Number(raw.metadata?.seed || 0) || undefined
}))

const adaptBibleItem = (raw: Raw): BibleItem => ({
  id: String(raw.id), slug: raw.slug, name: raw.name, description: raw.description || '', visualPrompt: raw.visual_identity || '', referenceAssetIds: [...new Set([raw.anchor_asset, ...(raw.reference_assets || [])].filter(Boolean).map(String))], locked: raw.is_locked !== false, seed: Number(raw.metadata?.seed || 0) || undefined, metadata:raw.metadata&&typeof raw.metadata==='object'?raw.metadata:{}, stableSpeakerId: raw.stable_speaker_id,
  ageAppearance: raw.age_appearance || '', faceIdentity: raw.face_identity || '', wardrobe: raw.wardrobe || '', voiceProfile: raw.voice_profile || '', personality: raw.personality || '',
  location: raw.location || '', timeOfDay: raw.time_of_day || '', lighting: raw.lighting || '', colorPalette: raw.color_palette || '', ambientSound: raw.ambient_sound || '',
  material: raw.material || '', distinctiveMarks: raw.distinctive_marks || '', continuityState: raw.continuity_state || ''
})

const adaptEntityBinding = (value: unknown): H3ReferenceEntityBinding | undefined => {
  if (!value || typeof value !== 'object') return undefined
  const raw = value as Raw
  const entityId = relationId(raw.entity_id ?? raw.entityId)
  const assetId = relationId(raw.asset_id ?? raw.assetId)
  return entityId && assetId ? { entityId, assetId } : undefined
}

const videoPurpose = (value: unknown): H3ReferenceVideoPurpose => (
  ['motion_camera_reference', 'video_editing', 'video_continuation'].includes(String(value))
    ? String(value) as H3ReferenceVideoPurpose
    : 'motion_camera_reference'
)

const audioPurpose = (value: unknown): H3ReferenceAudioPurpose => (
  ['audio_reference', 'audio_reuse'].includes(String(value))
    ? String(value) as H3ReferenceAudioPurpose
    : 'audio_reference'
)

const adaptReferenceBindings = (raw: Raw): H3ReferenceBindings => {
  const source = (raw.reference_bindings || raw.metadata?.reference_bindings || {}) as Raw
  const legacyAssets = (raw.reference_assets || []).map(String)
  const characters = Array.isArray(source.characters) ? source.characters.map(adaptEntityBinding).filter(Boolean) as H3ReferenceEntityBinding[] : []
  const props = Array.isArray(source.props) ? source.props.map(adaptEntityBinding).filter(Boolean) as H3ReferenceEntityBinding[] : []
  const videos = Array.isArray(source.videos) ? source.videos.map((item:Raw)=>{
    const useEmbeddedAudio=Boolean(item.use_embedded_audio??item.useEmbeddedAudio)
    const audioAssetId=relationId(item.audio_asset_id??item.audioAssetId)||undefined
    return {
      assetId:relationId(item.asset_id??item.assetId),
      purpose:videoPurpose(item.purpose),
      useEmbeddedAudio,
      audioAssetId,
      ...((useEmbeddedAudio||audioAssetId)?{audioPurpose:audioPurpose(item.audio_purpose??item.audioPurpose)}:{})
    }
  }).filter((item)=>item.assetId) : []
  const audios = Array.isArray(source.audios) ? source.audios.map((item:Raw)=>({assetId:relationId(item.asset_id??item.assetId),purpose:audioPurpose(item.purpose)})).filter((item)=>item.assetId) : []
  return {
    firstFrameAssetId: relationId(source.first_frame_asset_id ?? source.firstFrameAssetId) || (['I2VA', 'FL2VA'].includes(raw.h3_mode) ? legacyAssets[0] : undefined),
    lastFrameAssetId: relationId(source.last_frame_asset_id ?? source.lastFrameAssetId) || (raw.h3_mode === 'L2VA' ? legacyAssets[0] : raw.h3_mode === 'FL2VA' ? legacyAssets[1] : undefined),
    characters,
    scene: adaptEntityBinding(source.scene),
    props,
    videos,
    audios
  }
}

export const referenceBindingsPayload = (bindings?: H3ReferenceBindings) => ({
  ...(bindings?.firstFrameAssetId ? { first_frame_asset_id: bindings.firstFrameAssetId } : {}),
  ...(bindings?.lastFrameAssetId ? { last_frame_asset_id: bindings.lastFrameAssetId } : {}),
  ...((bindings?.characters || []).length ? { characters: bindings!.characters.map((item) => ({ entity_id: item.entityId, asset_id: item.assetId })) } : {}),
  ...(bindings?.scene ? { scene: { entity_id: bindings.scene.entityId, asset_id: bindings.scene.assetId } } : {}),
  ...((bindings?.props || []).length ? { props: bindings!.props.map((item) => ({ entity_id: item.entityId, asset_id: item.assetId })) } : {}),
  ...((bindings?.videos || []).length ? { videos: bindings!.videos.map((item)=>({
    asset_id:item.assetId,
    purpose:videoPurpose(item.purpose),
    use_embedded_audio:item.useEmbeddedAudio,
    ...(item.audioAssetId?{audio_asset_id:item.audioAssetId}:{}),
    ...((item.useEmbeddedAudio||item.audioAssetId)?{audio_purpose:audioPurpose(item.audioPurpose)}:{})
  })) } : {}),
  ...((bindings?.audios || []).length ? { audios: bindings!.audios.map((item)=>({asset_id:item.assetId,purpose:audioPurpose(item.purpose)})) } : {})
})

export const referenceAssetIdsForShot = (shot: Shot): string[] => {
  // The currently verified SDXL image template is text-to-image. Bible images
  // remain human continuity references and must not be presented or submitted
  // as pixel-conditioned model inputs.
  if (shot.mediaType === 'image') return []
  const bindings = shot.referenceBindings
  const ordered = shot.mode === 'FL2VA'
    ? [bindings?.firstFrameAssetId, bindings?.lastFrameAssetId]
    : shot.mode === 'Ref2VA'
      ? [...(bindings?.characters || []).map((item) => item.assetId), bindings?.scene?.assetId, ...(bindings?.props || []).map((item) => item.assetId), ...(bindings?.videos || []).flatMap((item)=>[item.assetId,item.audioAssetId]), ...(bindings?.audios || []).map((item)=>item.assetId)]
      : []
  return [...new Set(ordered.filter(Boolean).map(String))]
}

const adaptShot = (raw: Raw): Shot => {
  const metadata = raw.metadata && typeof raw.metadata === 'object' ? raw.metadata : {}
  const splitPromptFields = Number(metadata.prompt_field_version || 0) >= 2
  const creativeDraftProvenance = Boolean(metadata.creative_draft_id && metadata.client_key)
  const storedNegativePrompt = normalizeNegativePrompt(metadata.negative_prompt || '')
  const legacyNegativePromptRepairEligible = raw.legacy_negative_prompt_repair_eligible === true
  return {
    id: String(raw.id), order: Number(raw.number || 1), title: raw.title || `镜头 ${raw.number || 1}`,
    mediaType: raw.media_type === 'image' ? 'image' : 'video', duration: Math.max(.1, Number(raw.duration_ms || 3500) / 1000),
    mode: raw.h3_mode || 'T2VA', framing: metadata.framing || '中景', camera: raw.camera || '',
    action: raw.action || raw.narrative || '',
    ...adaptShotDetailedDescription(raw),
    continuityDirection: splitPromptFields || creativeDraftProvenance ? raw.continuity_notes || '' : '',
    prompt: '', negativePrompt: splitPromptFields
      ? storedNegativePrompt
      : creativeDraftProvenance
        ? storedNegativePrompt
        : normalizeNegativePrompt(raw.continuity_notes || '人物身份漂移，多余肢体，文字水印，低清晰度'),
    ...(legacyNegativePromptRepairEligible ? { legacyNegativePromptRepairEligible: true } : {}),
    characterIds: (raw.characters || []).map(String), sceneId: raw.scene ? String(raw.scene) : undefined,
    propIds: (raw.props || []).map(String), referenceAssetIds: (raw.reference_assets || []).map(String),
    referenceBindings: adaptReferenceBindings(raw),
    requiresFirstFrameContinuity: raw.requires_first_frame_continuity === true,
    requiresLastFrameContinuity: raw.requires_last_frame_continuity === true,
    metadata,
    audioEnabled: raw.media_type !== 'image' && raw.include_audio !== false,
    audio: { dialogue: formatDialogueLines(raw.dialogue), soundscape: raw.sound_effects || '', music: raw.music || '' },
    status: 'draft'
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
  const shots = [...shotRows].sort((left, right) => Number(left.number || 0) - Number(right.number || 0)).map(adaptShot).map((shot) => {
    const prompt = latestPrompt.get(`${shot.id}:${shot.mediaType}`)
    if (!prompt) return shot
    const generatedPrompt = adaptPromptArtifact(prompt)
    if (generatedPrompt.compiler_current === false) {
      return { ...shot, promptRefreshReason: 'compiler_updated' as const }
    }
    return { ...shot, prompt: generatedPrompt.integrated_multimodal_description, generatedPrompt, status: 'ready' as const }
  })
  return { id: String(raw.id), number: Number(raw.number), title: raw.title, synopsis: raw.synopsis || '', script: raw.script || '', status: raw.status === 'generating' ? 'rendering' : raw.status, multiShot: raw.prompt_settings?.multi_shot !== false, audioEnabled: raw.prompt_settings?.include_audio !== false, shots, createdAt: raw.created_at }
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
  return {
    id: String(raw.id), title: raw.title, logline: raw.logline || '', masterScript: raw.master_script || '', isFavorite: raw.is_favorite === true, coverAssetId: relationId(raw.cover_asset) || undefined, coverUrl: String(raw.cover_url || '') || undefined, continuitySummary: raw.continuity_summary || '', translationGlossary: raw.production_settings?.translation_glossary && typeof raw.production_settings.translation_glossary === 'object' ? Object.fromEntries(Object.entries(raw.production_settings.translation_glossary).filter(([source, target]) => source.trim() && String(target).trim()).map(([source, target]) => [source, String(target)])) : {}, styleId: styleSlug(String(raw.style || '')), status: projectStatus(raw.status), aspectRatio: raw.aspect_ratio || '9:16', resolution: inferResolution(raw), width: Number(raw.width || 0) || undefined, height: Number(raw.height || 0) || undefined, defaultMode: raw.production_settings?.default_h3_mode || 'Ref2VA',
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

const dimensions = (ratio: SeriesProject['aspectRatio'], resolution: SeriesProject['resolution']) => {
  const portrait = resolution === '4K' ? [2176, 3840] : resolution === '1080p' ? [1088, 1920] : [768, 1344]
  if (ratio === '16:9') return { width: portrait[1], height: portrait[0] }
  if (ratio === '1:1') return { width: portrait[0], height: portrait[0] }
  return { width: portrait[0], height: portrait[1] }
}

export const projectPayload = (project: Partial<SeriesProject>) => {
  const visualRules = visualRulesProjectPersistenceFields(project)
  return {
    title: project.title, logline: project.logline || '', master_script: project.masterScript || '', status: backendStatus(project.status || 'draft'), style: backendStyleId(project.styleId), target_model: 'minimax-h3', language: 'Chinese', aspect_ratio: project.aspectRatio || '9:16', ...(project.width && project.height ? { width: project.width, height: project.height } : dimensions(project.aspectRatio || '9:16', project.resolution || '720p')), fps: 24,
    production_settings: { default_h3_mode: project.defaultMode || 'Ref2VA', negative_prompt: visualRules.negativePrompt, color_script: visualRules.colorScript, translation_glossary: project.translationGlossary || {} }, continuity_summary: visualRules.continuitySummary,
  }
}

const slugValue = (item: BibleItem, index: number, prefix: string) => isBackendId(item.id) ? `${prefix}-${index + 1}` : item.id.toLowerCase().replace(/[^a-z0-9-]/g, '-').replace(/-+/g, '-').slice(0, 70) || `${prefix}-${Date.now()}-${index}`
export const syncBible = async (project: SeriesProject) => {
  const usedSpeakers = new Set(project.bible.characters.map((item) => item.stableSpeakerId).filter((value): value is string => Boolean(value)))
  for (const item of project.bible.characters) if (!item.stableSpeakerId) {
    let speakerNumber = 1
    while (usedSpeakers.has(`S${speakerNumber}`)) speakerNumber += 1
    item.stableSpeakerId = `S${speakerNumber}`; usedSpeakers.add(item.stableSpeakerId)
  }
  const serialize = (items: BibleItem[], prefix: string, characters = false) => items.map((item, index) => ({
    ...(isBackendId(item.id) ? { id: item.id } : {}), slug: item.slug || slugValue(item, index, prefix), name: item.name,
    description: item.description, visual_identity: item.visualPrompt, reference_assets: item.referenceAssetIds.filter(isBackendId),
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
  const result = unwrap<Raw>((await api.post(`/projects/${project.id}/sync-bibles/`, {
    characters: serialize(project.bible.characters, 'character', true), scenes: serialize(project.bible.scenes, 'scene'), props: serialize(project.bible.props, 'prop')
  })).data)
  project.bible.characters = (result.characters || []).map(adaptBibleItem)
  project.bible.scenes = (result.scenes || []).map(adaptBibleItem)
  project.bible.props = (result.props || []).map(adaptBibleItem)
}

export const adaptCreatedEpisode = (raw: Raw): Episode => ({ id: String(raw.id), number: Number(raw.number), title: raw.title, synopsis: raw.synopsis || '', script: raw.script || '', status: raw.status, multiShot: raw.prompt_settings?.multi_shot !== false, audioEnabled: raw.prompt_settings?.include_audio !== false, shots: [], createdAt: raw.created_at })

export const dialoguePayload = (value: string, project: SeriesProject): Raw[] | undefined => (
  parseDialogueText(value, project.bible.characters)
)

export const shotPersistencePayload = (project: SeriesProject, shot: Shot): Raw => {
  if (shot.legacyNegativePromptRepairEligible) {
    throw new Error(`分镜“${shot.title}”的负面约束与创作草稿连续性字段来源匹配；请先点击“审计并恢复负面约束”，或人工改为真实的英文缺陷排除词后再保存。`)
  }
  const isVideo = shot.mediaType !== 'image'
  if (!isBackendId(shot.id) && !shot.action.trim()) {
    throw new Error(`新增分镜“${shot.title}”需要先填写动作与画面描述；镜头标题只用于标识，不会替代 H3 导演描述。`)
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
    media_type: shot.mediaType, h3_mode: shot.mode, include_audio: isVideo && shot.audioEnabled,
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

export const syncEpisode = async (project: SeriesProject, episode: Episode): Promise<Episode> => {
  const shotPayload = episode.shots.map((shot) => shotPersistencePayload(project, shot))
  const result = unwrap<Raw>((await api.post(`/episodes/${episode.id}/sync-shots/`, { shots: shotPayload })).data)
  await api.patch(`/episodes/${episode.id}/`, { project: project.id, number: episode.number, title: episode.title, synopsis: episode.synopsis, script: episode.script || '', status: episode.status === 'rendering' ? 'generating' : episode.status, prompt_settings: { multi_shot: episode.multiShot, include_audio: episode.audioEnabled } })
  const previousById = new Map(episode.shots.map((shot) => [shot.id, shot]))
  episode.shots = (result.shots || []).map(adaptShot).map((shot: Shot) => {
    const previous = previousById.get(shot.id)
    return previous ? {
      ...shot,
      prompt: previous.prompt,
      generatedPrompt: previous.generatedPrompt,
      status: previous.status,
      promptRefreshReason: previous.promptRefreshReason,
      legacyNegativePromptRepairEligible: previous.legacyNegativePromptRepairEligible,
    } : shot
  })
  return episode
}

const taskNode = (kind: string, status: Workflow['status'], progress: number): WorkflowNode[] => {
  const nodeStatus: WorkflowNode['status'] = status === 'completed' ? 'completed' : status === 'failed' ? 'failed' : status === 'running' ? 'running' : 'pending'
  return [{ id: `generation-${kind}`, type: kind === 'image' ? 'image' : 'video', label: `${kind === 'image' ? '图片' : '视频'}生成任务`, status: nodeStatus, progress: status === 'completed' ? 100 : progress }]
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
  const outputMedia = Array.isArray(raw.output_media) ? raw.output_media.map((item, position) => {
    const media = item as Raw
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
      publishUrl: String(media.publish_url || '') || undefined,
      promotedAssetId: String(media.promoted_asset_id || '') || undefined,
      promoteToAssetUrl: String(media.promote_to_asset_url || '') || undefined
    }
  }) : []
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
  const inputParameters = taskInputParameters(raw)
  const referenceAssets = taskAssetSummaries(inputParameters, 'reference_assets', '参考输入')
  const continuityAssets = taskAssetSummaries(inputParameters, 'continuity_assets', '一致性素材')
  return {
    id: String(raw.id), taskId: String(raw.id), kind: raw.kind === 'image' ? 'image' : 'video', backendStatus: String(raw.status || ''),
    generatedWorkflowId: relationId(raw.generated_workflow) || undefined,
    comfyPromptId: String(raw.comfy_prompt_id || raw.submission_token || '') || undefined,
    submissionToken: String(raw.submission_token || '') || undefined,
    submissionState: String(raw.submission_state || '') || undefined,
    backend: String(raw.backend || '') || undefined, modelProfile: String(raw.model_profile || '') || undefined,
    workflowVersion: Number(raw.workflow_version || 0) || undefined, seed: Number(raw.seed || 0),
    effectiveFrames: Number(raw.effective_frames || 0), effectiveDurationMs: Number(raw.effective_duration_ms || 0),
    inputParameters, referenceAssets, continuityAssets,
    promptAvailable: typeof inputParameters.prompt_available === 'boolean' ? inputParameters.prompt_available : undefined,
    promptCharacterCount: Number(inputParameters.prompt_character_count || 0),
    negativePromptCharacterCount: Number(inputParameters.negative_prompt_character_count || 0),
    errorMessage: String(raw.error_message || '') || undefined, outputs: Array.isArray(raw.outputs) ? raw.outputs : [], outputMedia,
    queuedAt, startedAt: raw.started_at || undefined, finishedAt,
    submissionPreparedAt: raw.submission_prepared_at || undefined, submissionSubmittedAt: raw.submission_submitted_at || undefined,
    durationMs: optionalMilliseconds(raw.duration_ms), queueWaitMs: optionalMilliseconds(raw.queue_wait_ms), totalDurationMs: optionalMilliseconds(raw.total_duration_ms), timeline,
    lastHeartbeatAt: String(raw.last_heartbeat_at || raw.heartbeat_at || raw.updated_at || '') || undefined,
    timeoutAt: String(raw.timeout_at || raw.deadline_at || '') || undefined,
    isStalled, stallReason,
    retryAllowed: typeof raw.retry_allowed === 'boolean'
      ? raw.retry_allowed
      : typeof raw.can_retry === 'boolean'
        ? raw.can_retry
        : undefined,
    retryRequiresReconciliation: raw.retry_requires_reconciliation === true,
    retryBlockReason: String(raw.retry_block_reason || (raw.can_retry === false ? raw.remote_error || '' : '') || '') || undefined,
    cancelAllowed: typeof raw.cancel_allowed === 'boolean'
      ? raw.cancel_allowed
      : typeof raw.can_cancel === 'boolean'
        ? raw.can_cancel
        : undefined,
    projectId, projectTitle, status, progress, ...progressTelemetry,
    progressNote: isStalled ? stallReason || '任务长时间没有新的执行心跳，请检查 ComfyUI 状态或取消任务。' : progressTelemetry.progressSource === 'comfyui' && progressTelemetry.comfyProgress !== undefined ? '来自 ComfyUI 当前节点的量化采样进度，不等于整项任务完成度。' : progressIndeterminate ? '任务已提交或正在初始化，尚未收到 ComfyUI 量化进度。' : undefined,
    currentStep, nodes: taskNode(raw.kind, status, progress), createdAt,
    eta: status === 'running' ? '计算中' : undefined, attempts: Number(raw.attempts || 0), maxAttempts: Number(raw.max_attempts || 0),
    batchId, batchIndex, batchSize, batchKind, episodeId: episodeId || undefined, episodeNumber, episodeTitle,
    shotId: shotId || undefined, shotIds, shotNumber, shotTitle
  }
})
