<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AlertTriangle, ArrowLeft, AudioLines, Box, Check, ChevronDown, ChevronLeft, ChevronRight, Clipboard, Clock3, Copy, Download, Film, GripVertical, Image, Layers3, Link2, Lock, Mic2, MonitorPlay, MoreHorizontal, Music2, Pause, Play, Plus, RotateCcw, Save, Sparkles, Trash2, Upload, UserRound, Video, Volume2, WandSparkles, Waves } from 'lucide-vue-next'
import ToggleSwitch from '@/components/ToggleSwitch.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import AssetImage from '@/components/AssetImage.vue'
import BaseModal from '@/components/BaseModal.vue'
import PromptImpactBadge from '@/components/PromptImpactBadge.vue'
import PromptWritingGuide from '@/components/PromptWritingGuide.vue'
import TranslationReviewPanel from '@/components/TranslationReviewPanel.vue'
import ShotDetailDraftPanel from '@/components/ShotDetailDraftPanel.vue'
import EpisodeDetailDraftModal from '@/components/EpisodeDetailDraftModal.vue'
import ResolutionProfilePicker from '@/components/ResolutionProfilePicker.vue'
import ProductionWorkflowPicker from '@/components/ProductionWorkflowPicker.vue'
import { DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, DEFAULT_PRODUCTION_RESOLUTION_PROFILE, isProductionResolutionProfile, normalizeImageProductionResolutionMultiple, normalizeProductionResolutionMultiple } from '@/constants/productionResolution'
import { productionErrorCode, repository, type ProductionCapability } from '@/services/repository'
import { promptArtifactNeedsRegeneration } from '@/services/promptArtifactFreshness'
import { bibleAssetIds } from '@/services/bibleAssets'
import { normalizeShotVersionScopeKey, wholeEpisodeRetryStorageKey } from '@/services/shotVersionScope'
import { applyPromotedFieldsToShot, applyShotTranslationPatch, bibleCanonicalTargetCollisionMessage, bibleCanonicalTargetCollisions, dialogueDeliveryTranslationFields } from '@/services/translationReview'
import { useStudioStore } from '@/stores/studio'
import type { Asset, AssetType, BibleItem, Episode, H3Mode, H3PromptPayload, H3ReferenceAudioPurpose, H3ReferenceBindings, H3ReferenceVideoPurpose, ProductionResolutionProfile, ProductionWorkflowSelection, ProductionWorkflowTemplate, PromptTranslationField, SeriesProject, Shot, ShotDetailDraftApplyResult, ShotDetailDraftBatchApplyResult, ShotDetailDraftLanguageMode, ShotTranslationPromotionResult, TaskOutputMedia, Workflow } from '@/types'

const route = useRoute(); const router = useRouter(); const studio = useStudioStore()
const project = ref<SeriesProject | null>(null); const episode = ref<Episode | null>(null); const selectedShotId = ref(''); const loading = ref(true); const generating = ref(false); const saving = ref(false); const regeneratingShot = ref(false); const generatingWholeEpisode = ref(false); const wholeEpisodeOpen = ref(false); const wholeEpisodeExtendedConfirmed = ref(false); const wholeEpisodeIdempotencyConflict = ref(''); const wholeEpisodeProfile = ref<ProductionResolutionProfile>(DEFAULT_PRODUCTION_RESOLUTION_PROFILE); const wholeEpisodeMultiple = ref(DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE); const wholeEpisodeImageProfile = ref<ProductionResolutionProfile>('0.98'); const wholeEpisodeImageMultiple = ref(DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE); const wholeEpisodeIdempotencyKey = ref(''); const uploadingReference = ref<'video'|'audio'|''>(''); const videoUploadInput = ref<HTMLInputElement|null>(null); const audioUploadInput = ref<HTMLInputElement|null>(null); const externalAudioPickerVideoId=ref(''); const uploadAudioForVideoId=ref(''); const videoShotResolutionProfile = ref<ProductionResolutionProfile>(DEFAULT_PRODUCTION_RESOLUTION_PROFILE); const imageShotResolutionProfile = ref<ProductionResolutionProfile>('0.98'); const videoShotResolutionMultiple = ref(DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE); const imageShotResolutionMultiple = ref(DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE); const previewVideo = ref<HTMLVideoElement|null>(null); const previewPlaying = ref(false); const previewCurrentTime = ref(0); const previewDuration = ref(0); const previewLoading = ref(false); const previewRecovering = ref(false); const previewError = ref(''); const generatedPrompt = ref<H3PromptPayload | null>(null); const generatedPromptsByShot = ref<Record<string, H3PromptPayload>>({}); const inspectorTab = ref<'visual'|'continuity'|'audio'>('visual'); const lastSubmittedTask = ref<{ shotId: string; taskId: string } | null>(null); const pendingShotRegenerationConfirmation = ref<{ shotId:string; shotOrder:number; shotTitle:string; mediaType:'video'|'image' }|null>(null)
const productionWorkflowTemplates = ref<ProductionWorkflowTemplate[]>([]); const productionWorkflowTemplatesLoading = ref(false); const productionWorkflowTemplatesError = ref(''); const shotRef2vaTemplateId = ref(''); const shotRef2vaParameters = ref<Record<string,number>>({}); const wholeRef2vaTemplateId = ref(''); const wholeRef2vaParameters = ref<Record<string,number>>({})
const productionCapability = ref<ProductionCapability | null>(null); const productionCapabilityError = ref('')
type ShotOutputPreview = { taskId:string; kind:'video'|'image'; url:string; filename:string; downloadUrl:string; completedAt:string }
const shotOutputPreviews = ref<Record<string, ShotOutputPreview>>({})
const translationPromotionBusy = ref(false)
const episodeDetailDraftOpen = ref(false)
const episodeDetailDraftMode = ref<'expand'|'apply'>('expand')
const shotWorkflowTasks = ref<Record<string, Workflow[]>>({})
const hydratedPreviewTasks = new Set<string>()
const previewRecoveryTasks = new Set<string>()
const previewRecoveryAttempts = new Map<string, number>()
let previewRequestEpoch = 0
let previewPollTimer: number | undefined
let previewPollInFlight = false
let previewDisposed = false
const modes: Array<{value:H3Mode;name:string;workflow:string;desc:string;requirement:string;fields:string[]}> = [
  { value:'T2VA', name:'文生视频', workflow:'video_minimax_h3_i2v · zero-frame', desc:'复用官方 i2v 图的零帧模式，只使用文字描述生成；实际工作流不会连接首帧或尾帧。', requirement:'无需参考图', fields:['integrated_multimodal_description','overall_soundscape','non_diegetic_music'] },
  { value:'FL2VA', name:'首尾帧生视频', workflow:'video_minimax_h3_i2v', desc:'同一工作流支持 0–2 张图片：无图文生、仅首帧 I2VA、仅尾帧 L2VA、双帧 FL2VA。', requirement:'首帧/尾帧均可选，已选时不可复用同一图', fields:['picture_alignment（按已选帧自动生成）','integrated_multimodal_description','overall_soundscape','non_diegetic_music'] },
  { value:'Ref2VA', name:'参考生视频', workflow:'video_minimax_h3_r2v', desc:'支持 0–9 张分类图片、0–3 个参考视频及 0–3 个独立参考音频；只连接实际选择的输入。', requirement:'图片/视频/音频均可选，标签和顺序自动生成', fields:['subject_definitions','summary','retention_analysis','detailed_description','overall_soundscape','non_diegetic_music'] }
]
const framings = ['特写','近景','中近景','中景','全景','大全景','俯拍','航拍']
const cameraExamples = [
  'Static medium shot at eye level, 50 mm lens, locked composition.',
  'Slow dolly in from a medium shot to a medium close-up, eye level, steady speed.',
  'Smooth lateral tracking shot, matching the subject\'s walking speed, stable final framing.'
]
const actionTemplate = '[Subject] performs [one visible action] while [one observable environment response]. End with [a clear final pose and spatial relationship].'
const videoPurposeOptions:Array<{value:H3ReferenceVideoPurpose;label:string;help:string}> = [
  {value:'motion_camera_reference',label:'参考动作与运镜',help:'只参考动作、节奏和镜头运动，不沿用视频中的人物或场景身份。'},
  {value:'video_editing',label:'视频编辑',help:'把参考视频作为需要按当前文字指令编辑、重演或改造的源片段。'},
  {value:'video_continuation',label:'视频续写',help:'从参考视频的结尾状态继续生成，提示词会强调时间与空间连续。'}
]
const audioPurposeOptions:Array<{value:H3ReferenceAudioPurpose;label:string;help:string}> = [
  {value:'audio_reference',label:'仅参考声音',help:'参考音色、节奏、环境声或演唱特征，不要求逐字复用原音轨。'},
  {value:'audio_reuse',label:'复用原音轨',help:'明确要求保留并复用该音频内容。'}
]
const videoPurposeHelp=(purpose?:H3ReferenceVideoPurpose)=>videoPurposeOptions.find((item)=>item.value===purpose)?.help||videoPurposeOptions[0].help
const audioPurposeHelp=(purpose?:H3ReferenceAudioPurpose)=>audioPurposeOptions.find((item)=>item.value===purpose)?.help||audioPurposeOptions[0].help
const videoPurposeLabel=(purpose?:H3ReferenceVideoPurpose)=>videoPurposeOptions.find((item)=>item.value===purpose)?.label||videoPurposeOptions[0].label
const audioPurposeLabel=(purpose?:H3ReferenceAudioPurpose)=>audioPurposeOptions.find((item)=>item.value===purpose)?.label||audioPurposeOptions[0].label
const videoPurposeOptionDisabled=(shot:Shot,currentAssetId:string,candidate:H3ReferenceVideoPurpose)=>{
  if(!['video_editing','video_continuation'].includes(candidate))return false
  const conflicting=candidate==='video_editing'?'video_continuation':'video_editing'
  return bindingsFor(shot).videos.some((item)=>item.assetId!==currentAssetId&&item.purpose===conflicting)
}
const selectedShotVersionKey = ref('')
const episodeShotVersions = computed(() => episode.value?.shotVersions || [])
const activeShotVersionLabel = computed(() => episodeShotVersions.value.find((item) => item.key === selectedShotVersionKey.value)?.label || '')
const selectedShotVersionScopeKey = computed(() => episode.value
  ? normalizeShotVersionScopeKey(selectedShotVersionKey.value, episode.value.id)
  : '')
const selectedShotVersionScopeLabel = computed(() => activeShotVersionLabel.value || '初始分镜')
const selectShotVersion = (key: string) => {
  const current = episode.value
  const version = current?.shotVersions?.find((item) => item.key === key)
  if (!current || !version) return
  current.shots = version.shots.map((shot) => ({ ...shot, metadata: { ...(shot.metadata || {}) } }))
  current.activeShotVersionKey = version.key
  selectedShotVersionKey.value = version.key
  selectedShotId.value = current.shots[0]?.id || ''
  generatedPrompt.value = null
  generatedPromptsByShot.value = {}
}
const selectedShot = computed(() => episode.value?.shots.find((item) => item.id === selectedShotId.value) || null)
const videoShotMinDurationSeconds = computed(() => productionCapability.value ? productionCapability.value.videoShotMinDurationMs / 1000 : undefined)
const videoShotMaxDurationSeconds = computed(() => productionCapability.value ? productionCapability.value.videoShotMaxDurationMs / 1000 : undefined)
const selectedShotDurationOutOfRange = computed(() => {
  const duration = selectedShot.value?.duration
  const minimum = videoShotMinDurationSeconds.value
  const maximum = videoShotMaxDurationSeconds.value
  return selectedShot.value?.mediaType === 'video'
    && duration !== undefined
    && minimum !== undefined
    && maximum !== undefined
    && (!Number.isFinite(duration) || duration < minimum || duration > maximum)
})
const adjustSelectedShotDuration = (delta: number) => {
  if (!selectedShot.value || videoShotMinDurationSeconds.value === undefined || videoShotMaxDurationSeconds.value === undefined) return
  selectedShot.value.duration = Math.min(videoShotMaxDurationSeconds.value, Math.max(videoShotMinDurationSeconds.value, selectedShot.value.duration + delta))
}
const shotResolutionProfile = computed<ProductionResolutionProfile>({
  get: () => selectedShot.value?.mediaType === 'image' ? imageShotResolutionProfile.value : videoShotResolutionProfile.value,
  set: (value) => { if(selectedShot.value?.mediaType === 'image')imageShotResolutionProfile.value=value;else videoShotResolutionProfile.value=value }
})
const shotResolutionMultiple = computed<number>({
  get: () => selectedShot.value?.mediaType === 'image' ? imageShotResolutionMultiple.value : videoShotResolutionMultiple.value,
  set: (value) => { if(selectedShot.value?.mediaType === 'image')imageShotResolutionMultiple.value=value;else videoShotResolutionMultiple.value=value }
})
const style = computed(() => studio.styles.find((item) => item.id === project.value?.styleId))
const workflowTimestamp = (workflow: Workflow, completed = false) => {
  const value = completed ? workflow.finishedAt || workflow.createdAt : workflow.createdAt || workflow.queuedAt
  return new Date(value || 0).getTime() || 0
}
const workflowOutputMedia = (workflow: Workflow | null | undefined, kind: 'video' | 'image'): TaskOutputMedia | undefined => workflow?.outputMedia?.find((item) => {
  if (item.kind === kind) return true
  return kind === 'image' ? /\.(?:png|jpe?g|webp|gif|avif)$/i.test(item.filename) : /\.(?:mp4|webm|mov|m4v|mkv)$/i.test(item.filename)
})
const selectedShotTasks = computed(() => shotWorkflowTasks.value[selectedShotId.value] || [])
const latestCompletedTask = computed(() => [...selectedShotTasks.value]
  .filter((workflow) => workflow.status === 'completed')
  .sort((left, right) => workflowTimestamp(right, true) - workflowTimestamp(left, true))[0] || null)
const latestSuccessfulTask = computed(() => [...selectedShotTasks.value]
  .filter((workflow) => workflow.status === 'completed' && Boolean(workflowOutputMedia(workflow, selectedShot.value?.mediaType || 'video')?.previewUrl))
  .sort((left, right) => workflowTimestamp(right, true) - workflowTimestamp(left, true))[0] || null)
const latestActiveTask = computed(() => [...selectedShotTasks.value]
  .filter((workflow) => ['queued', 'running', 'paused'].includes(workflow.status))
  .sort((left, right) => workflowTimestamp(right) - workflowTimestamp(left))[0] || null)
const latestFailedTask = computed(() => [...selectedShotTasks.value]
  .filter((workflow) => workflow.status === 'failed')
  .sort((left, right) => workflowTimestamp(right) - workflowTimestamp(left))[0] || null)
const selectedOutputPreview = computed(() => shotOutputPreviews.value[selectedShotId.value] || null)
const selectedVideoPreview = computed(() => selectedOutputPreview.value?.kind === 'video' ? selectedOutputPreview.value : null)
const selectedImagePreview = computed(() => selectedOutputPreview.value?.kind === 'image' ? selectedOutputPreview.value : null)
const previewHasNewerActiveTask = computed(() => Boolean(
  latestActiveTask.value
  && (!latestSuccessfulTask.value
    || workflowTimestamp(latestActiveTask.value) > workflowTimestamp(latestSuccessfulTask.value))
))
const previewStatusText = computed(() => {
  const image = selectedShot.value?.mediaType === 'image'
  const noun = image ? '图片' : '视频'
  if (previewLoading.value && !selectedOutputPreview.value) return `正在读取最新${noun}产物…`
  if (previewHasNewerActiveTask.value && selectedOutputPreview.value) return `新版本生成中 · 当前显示上一版${noun}`
  if (previewHasNewerActiveTask.value) return `${noun}生成中 · 完成后将自动载入`
  if (selectedOutputPreview.value) return image ? '最新成功图片 · 可预览与下载' : '最新成功视频 · 可播放预览'
  if (latestCompletedTask.value) return `任务已完成，但未找到可预览的${noun}产物`
  if (latestFailedTask.value) return `最近一次生成失败 · 暂无可预览${noun}`
  return `尚未生成${noun} · 请先创建生产任务`
})
const videoShots = computed(() => episode.value?.shots.filter((shot) => shot.mediaType === 'video') || [])
const selectedVideoShotIndex = computed(() => videoShots.value.findIndex((shot) => shot.id === selectedShotId.value))
const hasPreviousVideoShot = computed(() => selectedVideoShotIndex.value > 0)
const hasNextVideoShot = computed(() => selectedVideoShotIndex.value >= 0 && selectedVideoShotIndex.value < videoShots.value.length - 1)
const previewProgress = computed(() => previewDuration.value > 0 ? Math.min(100, Math.max(0, previewCurrentTime.value / previewDuration.value * 100)) : 0)
const formatPreviewTime = (seconds: number) => {
  const safe = Math.max(0, Number.isFinite(seconds) ? Math.floor(seconds) : 0)
  const hours = Math.floor(safe / 3600)
  const minutes = Math.floor((safe % 3600) / 60)
  const remaining = safe % 60
  return [hours, minutes, remaining].map((value) => String(value).padStart(2, '0')).join(':')
}
const totalDuration = computed(() => episode.value?.shots.reduce((sum, item) => sum + (item.mediaType === 'video' ? Number(item.duration || 0) : 0), 0) || 0)
const videoShotCount = computed(() => episode.value?.shots.filter((item) => item.mediaType === 'video').length || 0)
const imageShotCount = computed(() => episode.value?.shots.filter((item) => item.mediaType === 'image').length || 0)
const episodeHasRef2VA = computed(() => episode.value?.shots.some((item) => item.mediaType === 'video' && item.mode === 'Ref2VA') || false)
const makeRef2vaSelection = (templateId:string,parameters:Record<string,number>):ProductionWorkflowSelection[] => templateId ? [{outputKind:'video',h3Mode:'Ref2VA',templateId,parameters:{...parameters}}] : []
const shotWorkflowSelections = computed(() => selectedShot.value?.mediaType === 'video' && selectedShot.value.mode === 'Ref2VA' ? makeRef2vaSelection(shotRef2vaTemplateId.value,shotRef2vaParameters.value) : [])
const wholeEpisodeWorkflowSelections = computed(() => episodeHasRef2VA.value ? makeRef2vaSelection(wholeRef2vaTemplateId.value,wholeRef2vaParameters.value) : [])
const loadProductionWorkflowTemplates=async()=>{productionWorkflowTemplatesLoading.value=true;productionWorkflowTemplatesError.value='';try{productionWorkflowTemplates.value=await repository.getProductionWorkflowTemplates()}catch(error){productionWorkflowTemplatesError.value=error instanceof Error?error.message:'服务端未返回工作流模板'}finally{productionWorkflowTemplatesLoading.value=false}}
const loadProductionCapability=async()=>{productionCapabilityError.value='';try{productionCapability.value=await repository.getProductionCapability()}catch(error){productionCapability.value=null;productionCapabilityError.value=error instanceof Error?error.message:'服务端未返回正式视频时长配置'}}
const promptJson = computed(() => generatedPrompt.value ? JSON.stringify(generatedPrompt.value, null, 2) : '')
const actualPromptText = computed(() => generatedPrompt.value?.prompt_text || generatedPrompt.value?.integrated_multimodal_description || '')
const promptQuality = computed(() => generatedPrompt.value?.quality)
const promptWouldBlockProduction = (payload?: H3PromptPayload | null) => {
  if (!payload) return false
  const quality = payload.quality
  if (!quality) return false
  const gate = quality.production_language_gate
  if (gate) return gate.mode === 'block' && gate.would_block_enqueue
  return Boolean(quality.fields_requiring_english_rewrite?.length) && quality.optimizer?.status !== 'optimized'
}
const selectedPromptProductionBlocked = computed(() => promptWouldBlockProduction(generatedPrompt.value))
const selectedPromptRefreshReason = computed(() => selectedShot.value?.promptRefreshReason)
const selectedPromptRefreshMessage = computed(() => {
  if (selectedPromptRefreshReason.value === 'compiler_updated') return '提示词编译器已升级，旧 Prompt Artifact 仅保留审计记录，不再作为当前预览。请一键重新生成并复检。'
  if (selectedPromptRefreshReason.value === 'translations_changed') return '译文审核状态已变化，旧 Prompt Artifact 已失效。已确认项无需再修改中文源字段，请一键重新生成并复检。'
  if (selectedPromptRefreshReason.value === 'inputs_changed') return '分镜输入已变化，旧 Prompt Artifact 已失效。请一键重新生成并复检。'
  return ''
})
const blockedEpisodePromptShots = computed(() => (episode.value?.shots || []).filter((shot) => (
  promptWouldBlockProduction(generatedPromptsByShot.value[shot.id] || shot.generatedPrompt)
)))
const legacyNegativeRepairShots = computed(() => (episode.value?.shots || []).filter((shot) => shot.legacyNegativePromptRepairEligible))
const canonicalTargetCollisionError = computed(() => {
  if (!project.value) return ''
  const names = [...project.value.bible.characters, ...project.value.bible.scenes, ...project.value.bible.props].map((item) => item.name)
  return bibleCanonicalTargetCollisionMessage(bibleCanonicalTargetCollisions(names, project.value.translationGlossary))
})
const currentShotPersistenceBlockedReason = computed(() => {
  if (translationPromotionBusy.value) return '译文正在原子同步到正式分镜字段，请等待完成。'
  if (selectedShot.value?.legacyNegativePromptRepairEligible) return '当前镜头的来源匹配负面约束待服务端审计恢复。'
  return canonicalTargetCollisionError.value
})
const currentShotPersistenceBlocked = computed(() => Boolean(currentShotPersistenceBlockedReason.value))
const editorPersistenceBlockedReason = computed(() => {
  if (translationPromotionBusy.value) return '译文正在原子同步到正式分镜字段，请等待完成。'
  if (legacyNegativeRepairShots.value.length) return `本集有 ${legacyNegativeRepairShots.value.length} 个镜头的来源匹配负面约束待服务端审计恢复。`
  return canonicalTargetCollisionError.value
})
const editorPersistenceBlocked = computed(() => Boolean(editorPersistenceBlockedReason.value))
const continuityCount = computed(() => selectedShot.value ? selectedShot.value.characterIds.length + (selectedShot.value.sceneId ? 1 : 0) + selectedShot.value.propIds.length : 0)
const selectedModeProfile = computed(() => modes.find((item) => item.value === selectedShot.value?.mode) || modes[0])
const selectedTranslationFields = computed<PromptTranslationField[]>(() => {
  if (!project.value || !selectedShot.value) return []
  const shot = selectedShot.value
  const fields: PromptTranslationField[] = []
  const add = (field: PromptTranslationField) => { if (field.sourceText.trim()) fields.push(field) }
  if (style.value?.promptPrefix) add({ fieldKey:'style.visual_prompt', label:'风格视觉提示', sourceText:style.value.promptPrefix, scopeType:'project', scopeId:project.value.id, impact:'prompt', help:'进入全剧风格与画面质感定义' })
  add({ fieldKey:'project.production_settings.color_script', label:'全剧色彩脚本', sourceText:project.value.bible.colorScript, scopeType:'project', scopeId:project.value.id, impact:'continuity', help:'进入镜头色彩、光线与跨集连续性' })
  add({ fieldKey:'project.continuity_summary', label:'项目连续性摘要', sourceText:project.value.continuitySummary || '', scopeType:'project', scopeId:project.value.id, impact:'continuity', help:'与色彩脚本独立保存；作为图片提示词与跨集连续性的后备字段单独审核' })
  for (const id of shot.characterIds) { const item=project.value.bible.characters.find((row)=>row.id===id); if(item){ const base={scopeType:'character' as const,scopeId:item.id,impact:'continuity' as const}; add({...base,fieldKey:'character.visual_identity',label:`人物 · ${item.name} · 视觉锚点`,sourceText:item.visualPrompt});add({...base,fieldKey:'character.age_appearance',label:`人物 · ${item.name} · 年龄外观`,sourceText:item.ageAppearance||''});add({...base,fieldKey:'character.face_identity',label:`人物 · ${item.name} · 面部身份`,sourceText:item.faceIdentity||''});add({...base,fieldKey:'character.wardrobe',label:`人物 · ${item.name} · 固定服装`,sourceText:item.wardrobe||''});add({...base,fieldKey:'character.voice_profile',label:`人物 · ${item.name} · 声音特征`,sourceText:item.voiceProfile||'',impact:'prompt'});if(!(item.voiceProfile||'').trim())add({...base,fieldKey:'character.personality',label:`人物 · ${item.name} · 说话者人物描述`,sourceText:item.personality||'',impact:'prompt',help:'声音特征为空，当前会作为 H3 对白说话者描述后备；不是管理用基础描述'}) } }
  if (shot.sceneId) { const item=project.value.bible.scenes.find((row)=>row.id===shot.sceneId); if(item){ const base={scopeType:'scene' as const,scopeId:item.id,impact:'continuity' as const};add({...base,fieldKey:'scene.visual_identity',label:`场景 · ${item.name} · 视觉锚点`,sourceText:item.visualPrompt});add({...base,fieldKey:'scene.location',label:`场景 · ${item.name} · 地点结构`,sourceText:item.location||''});add({...base,fieldKey:'scene.time_of_day',label:`场景 · ${item.name} · 时间`,sourceText:item.timeOfDay||''});add({...base,fieldKey:'scene.lighting',label:`场景 · ${item.name} · 光线`,sourceText:item.lighting||''});add({...base,fieldKey:'scene.color_palette',label:`场景 · ${item.name} · 色板`,sourceText:item.colorPalette||''});add({...base,fieldKey:'scene.ambient_sound',label:`场景 · ${item.name} · 环境声`,sourceText:item.ambientSound||'',impact:'prompt'}) } }
  for (const id of shot.propIds) { const item=project.value.bible.props.find((row)=>row.id===id); if(item){ const base={scopeType:'prop' as const,scopeId:item.id,impact:'continuity' as const};add({...base,fieldKey:'prop.visual_identity',label:`道具 · ${item.name} · 视觉锚点`,sourceText:item.visualPrompt});add({...base,fieldKey:'prop.material',label:`道具 · ${item.name} · 材质`,sourceText:item.material||''});add({...base,fieldKey:'prop.distinctive_marks',label:`道具 · ${item.name} · 固定标记`,sourceText:item.distinctiveMarks||''});add({...base,fieldKey:'prop.continuity_state',label:`道具 · ${item.name} · 连续性状态`,sourceText:item.continuityState||''}) } }
  add({ fieldKey:'shot.camera', label:'镜头运动', sourceText:shot.camera, scopeType:'shot', scopeId:shot.id, impact:'prompt' })
  // The browser editor exposes the formal Shot.narrative field as `shot.action`.
  // Keep the translation identity aligned with the persisted backend field so
  // retranslation resolves the same authoritative source shown in the editor.
  add({ fieldKey:'shot.narrative', label:'动作与画面描述', sourceText:shot.action, scopeType:'shot', scopeId:shot.id, impact:'prompt' })
  // A Chinese-only high-detail apply deliberately keeps the Chinese source in
  // the formal Shot until this existing review panel has a human-confirmed
  // English candidate. Do not re-list an already-applied English detail.
  if (/[\u3400-\u4dbf\u4e00-\u9fff]/.test(shot.detailedDescription || '')) add({
    fieldKey:'shot.detailed_description', label:'逐镜高细节导演说明', sourceText:shot.detailedDescription || '',
    scopeType:'shot', scopeId:shot.id, impact:'prompt',
    help:'来自“逐镜高细节扩写”的中文正式字段；确认英文后会覆盖该字段，并需要重新生成提示词。',
  })
  add({ fieldKey:'shot.continuity_notes', label:'正向连续性指令', sourceText:shot.continuityDirection || '', scopeType:'shot', scopeId:shot.id, impact:'continuity' })
  if (shot.mediaType==='video'&&shot.audioEnabled) {
    add({ fieldKey:'shot.sound_effects', label:'整体声景', sourceText:shot.audio.soundscape, scopeType:'shot', scopeId:shot.id, impact:'prompt' })
    add({ fieldKey:'shot.music', label:'非叙事音乐', sourceText:shot.audio.music, scopeType:'shot', scopeId:shot.id, impact:'prompt' })
    dialogueDeliveryTranslationFields(shot.audio.dialogue, project.value.bible.characters, shot.id).forEach(add)
  }
  return fields
})
const effectiveFrameMode = (shot:Shot):H3Mode => {
  const bindings=bindingsFor(shot);const first=Boolean(bindings.firstFrameAssetId);const last=Boolean(bindings.lastFrameAssetId)
  return first&&last?'FL2VA':first?'I2VA':last?'L2VA':'T2VA'
}
const effectiveSelectedMode = computed(()=>selectedShot.value?.mode==='FL2VA'?effectiveFrameMode(selectedShot.value):selectedShot.value?.mode||'T2VA')
const assetsByType = computed<Record<AssetType, Asset[]>>(() => ({
  character: studio.assets.filter((item) => item.type === 'character'),
  scene: studio.assets.filter((item) => item.type === 'scene'),
  prop: studio.assets.filter((item) => item.type === 'prop'),
  image: studio.assets.filter((item) => item.type === 'image'),
  video: studio.assets.filter((item) => item.type === 'video'),
  audio: studio.assets.filter((item) => item.type === 'audio')
}))
const frameAssets = computed(()=>studio.assets.filter((item)=>['character','scene','prop','image'].includes(item.type)))
type BibleAssetType='character'|'scene'|'prop'
const clone = <T,>(value:T):T => JSON.parse(JSON.stringify(value))

const emptyBindings = ():H3ReferenceBindings => ({ characters: [], props: [], videos: [], audios: [] })
const bindingsFor = (shot:Shot):H3ReferenceBindings => {const bindings=(shot.referenceBindings ||= emptyBindings());bindings.characters ||= [];bindings.props ||= [];bindings.videos ||= [];bindings.audios ||= [];for(const item of bindings.videos){item.purpose ||= 'motion_camera_reference';if(item.useEmbeddedAudio||item.audioAssetId)item.audioPurpose ||= 'audio_reference';else item.audioPurpose=undefined}for(const item of bindings.audios)item.purpose ||= 'audio_reference';return bindings}
const bibleItems = (kind:BibleAssetType):BibleItem[] => {
  if (!project.value) return []
  return kind === 'character' ? project.value.bible.characters : kind === 'scene' ? project.value.bible.scenes : project.value.bible.props
}
const bibleItem = (kind:BibleAssetType,id:string) => bibleItems(kind).find((item)=>item.id===id)
const assetItem = (id?:string) => studio.assets.find((item)=>item.id===id)
const markBindingConfigured=(shot:Shot)=>{shot.metadata={...(shot.metadata||{}),referenceBindingConfigured:true}}
const invalidatePrompt = (shot:Shot, reason:NonNullable<Shot['promptRefreshReason']>='inputs_changed') => {
  const next={...generatedPromptsByShot.value};delete next[shot.id];generatedPromptsByShot.value=next
  if(selectedShotId.value===shot.id)generatedPrompt.value=null
  shot.generatedPrompt=undefined;shot.prompt='';shot.status='draft';shot.promptRefreshReason=reason
}
const applyShotTranslationPromotion = (result: ShotTranslationPromotionResult) => {
  if (!episode.value || !project.value) return
  const targets: Shot[] = []
  const localShot = episode.value.shots.find((shot) => shot.id === result.shotId)
  const storedShot = studio.projects.find((item) => item.id === project.value!.id)
    ?.episodes.find((item) => item.id === episode.value!.id)
    ?.shots.find((shot) => shot.id === result.shotId)
  if (localShot) targets.push(localShot)
  if (storedShot && storedShot !== localShot) targets.push(storedShot)
  let promotedCount = 0
  let recoveredNegative = false
  for (const target of targets) {
    const applied = applyPromotedFieldsToShot(target, result.promotedFields, project.value.bible.characters)
    const patched = applyShotTranslationPatch(target, result.shotPatch)
    if (target === localShot) {
      promotedCount = applied.length
      recoveredNegative = patched
    }
    if (applied.length || patched) invalidatePrompt(target, 'translations_changed')
  }
  if (promotedCount || recoveredNegative) {
    const parts = [promotedCount ? `已同步 ${promotedCount} 个正式英文导演字段` : '', recoveredNegative ? '已审计并恢复来源匹配负面约束' : ''].filter(Boolean)
    studio.notify(`${parts.join('；')}；镜头标题与对白正文保持中文，请一键重新生成提示词。`)
  }
}
/**
 * The high-detail endpoint has already persisted its own atomic Shot update.
 * Mirror only that authoritative patch into both in-memory project copies and
 * deliberately invalidate the old Prompt Artifact.  We never copy the draft
 * text into action/camera/narrative, so the compact editor remains compact.
 */
const applyShotDetailDraft = (result: ShotDetailDraftApplyResult) => {
  if (!episode.value || !project.value || !result.appliedShot.id) return
  const targets: Shot[] = []
  const localShot = episode.value.shots.find((shot) => shot.id === result.appliedShot.id)
  const storedShot = studio.projects.find((item) => item.id === project.value!.id)
    ?.episodes.find((item) => item.id === episode.value!.id)
    ?.shots.find((shot) => shot.id === result.appliedShot.id)
  if (localShot) targets.push(localShot)
  if (storedShot && storedShot !== localShot) targets.push(storedShot)
  for (const target of targets) {
    target.detailedDescription = result.appliedShot.detailedDescription
    if (result.appliedShot.metadata) target.metadata = { ...target.metadata, ...result.appliedShot.metadata }
    invalidatePrompt(target, 'inputs_changed')
  }
  studio.notify(result.translationWritten
    ? '已应用已审英文高细节说明；当前提示词已失效，请重新生成并复检。'
    : '已应用中文高细节说明；请在上方翻译审核确认英文后，再重新生成并复检提示词。')
}
const openEpisodeDetailDraft = (mode:'expand'|'apply') => {
  episodeDetailDraftMode.value = mode
  episodeDetailDraftOpen.value = true
}
const applyEpisodeDetailDraft = (
  result: ShotDetailDraftBatchApplyResult,
  _languageMode: ShotDetailDraftLanguageMode,
) => {
  if (!episode.value || !project.value) return
  const versionKey = result.batch.shotVersionKey
  const storedEpisode = studio.projects.find((entry) => entry.id === project.value!.id)
    ?.episodes.find((entry) => entry.id === episode.value!.id)
  const targetCollections: Shot[][] = []
  const addCollection = (rows: Shot[] | undefined) => {
    if (rows && !targetCollections.includes(rows)) targetCollections.push(rows)
  }
  if (selectedShotVersionScopeKey.value === versionKey) addCollection(episode.value.shots)
  addCollection(episode.value.shotVersions?.find((entry) => entry.key === versionKey)?.shots)
  if (storedEpisode?.activeShotVersionKey === versionKey) addCollection(storedEpisode.shots)
  addCollection(storedEpisode?.shotVersions?.find((entry) => entry.key === versionKey)?.shots)
  let applied = 0
  for (const appliedShot of result.appliedShots) {
    const targets = targetCollections
      .map((rows) => rows.find((shot) => shot.id === appliedShot.id))
      .filter((shot, index, rows): shot is Shot => Boolean(shot) && rows.indexOf(shot) === index)
    for (const target of targets) {
      target.detailedDescription = appliedShot.detailedDescription
      if (appliedShot.metadata) target.metadata = { ...target.metadata, ...appliedShot.metadata }
      invalidatePrompt(target, 'inputs_changed')
    }
    if (targets.length) applied += 1
  }
  studio.notify(result.translationWritten
    ? `已原子应用 ${applied} 个视频镜头的已审英文扩写；相关提示词已失效。`
    : `已原子应用 ${applied} 个视频镜头的中文扩写；相关提示词已失效，请继续翻译审核。`)
}
const episodeAudioEnabled = computed<boolean>({
  get: () => episode.value?.audioEnabled ?? true,
  set: (enabled) => {
    if (!episode.value) return
    episode.value.audioEnabled = enabled
    for (const shot of episode.value.shots) {
      if (shot.mediaType !== 'video' || shot.audioEnabled === enabled) continue
      shot.audioEnabled = enabled
      invalidatePrompt(shot)
    }
  }
})
const selectedPromptInputs = computed(() => {
  const shot = selectedShot.value
  if (!shot) return null
  return {
    shotId: shot.id,
    fingerprint: JSON.stringify({
      mediaType: shot.mediaType,
      mode: shot.mode,
      duration: shot.duration,
      framing: shot.framing,
      camera: shot.camera,
      action: shot.action,
      detailedDescription: shot.detailedDescription || '',
      continuityDirection: shot.continuityDirection || '',
      requiresFirstFrameContinuity: shot.requiresFirstFrameContinuity,
      requiresLastFrameContinuity: shot.requiresLastFrameContinuity,
      negativePrompt: shot.negativePrompt,
      characterIds: shot.characterIds,
      sceneId: shot.sceneId || '',
      propIds: shot.propIds,
      referenceBindings: shot.referenceBindings || {},
      audioEnabled: shot.audioEnabled,
      audio: shot.audio
    })
  }
})
const orderedRefBindings = (shot:Shot) => {
  const bindings=bindingsFor(shot)
  const result:Array<{category:BibleAssetType;entityId:string;assetId:string;entityName:string}> = []
  for(const entityId of shot.characterIds){const binding=bindings.characters.find((item)=>item.entityId===entityId);if(binding)result.push({category:'character',entityId,assetId:binding.assetId,entityName:bibleItem('character',entityId)?.name||'未命名人物'})}
  if(shot.sceneId&&bindings.scene?.entityId===shot.sceneId)result.push({category:'scene',entityId:shot.sceneId,assetId:bindings.scene.assetId,entityName:bibleItem('scene',shot.sceneId)?.name||'未命名场景'})
  for(const entityId of shot.propIds){const binding=bindings.props.find((item)=>item.entityId===entityId);if(binding)result.push({category:'prop',entityId,assetId:binding.assetId,entityName:bibleItem('prop',entityId)?.name||'未命名道具'})}
  return result
}
const syncReferenceAssets = (shot:Shot) => {
  const bindings=bindingsFor(shot)
  const ordered=shot.mode==='FL2VA'?[bindings.firstFrameAssetId,bindings.lastFrameAssetId]:shot.mode==='Ref2VA'?[bindings.firstFrameReferenceAssetId,...orderedRefBindings(shot).map((item)=>item.assetId),...(bindings.videos||[]).flatMap((item)=>[item.assetId,item.audioAssetId]),...(bindings.audios||[]).map((item)=>item.assetId)]:[]
  shot.referenceAssetIds=[...new Set(ordered.filter(Boolean).map(String))]
}
const preferredUnusedAsset = (kind:BibleAssetType,entityId:string,used:Set<string>) => {
  const preferred=bibleAssetIds(bibleItem(kind,entityId))
  return preferred.map(assetItem).filter((item):item is Asset=>Boolean(item&&item.type===kind)).find((item)=>!used.has(item.id))
}
const referenceAssetsForEntity=(kind:BibleAssetType,entityId:string)=>{const allowed=new Set(bibleAssetIds(bibleItem(kind,entityId)));return assetsByType.value[kind].filter((item)=>allowed.has(item.id))}
const pruneReferenceBindings=(shot:Shot)=>{const bindings=bindingsFor(shot);const used=new Set<string>();if(shot.mode==='Ref2VA'&&bindings.firstFrameReferenceAssetId){if(assetItem(bindings.firstFrameReferenceAssetId))used.add(bindings.firstFrameReferenceAssetId);else bindings.firstFrameReferenceAssetId=undefined}else if(shot.mode!=='Ref2VA')bindings.firstFrameReferenceAssetId=undefined;const keep=(kind:BibleAssetType,entityId:string,assetId?:string)=>{const valid=Boolean(assetId&&referenceAssetsForEntity(kind,entityId).some((item)=>item.id===assetId)&&!used.has(assetId));if(valid)used.add(assetId!);return valid};bindings.characters=shot.characterIds.map((entityId)=>bindings.characters.find((item)=>item.entityId===entityId)).filter((item):item is {entityId:string;assetId:string}=>Boolean(item&&keep('character',item.entityId,item.assetId)));bindings.scene=shot.sceneId&&bindings.scene?.entityId===shot.sceneId&&keep('scene',shot.sceneId,bindings.scene.assetId)?bindings.scene:undefined;bindings.props=shot.propIds.map((entityId)=>bindings.props.find((item)=>item.entityId===entityId)).filter((item):item is {entityId:string;assetId:string}=>Boolean(item&&keep('prop',item.entityId,item.assetId)));for(const item of orderedRefBindings(shot))used.add(item.assetId);bindings.videos=(bindings.videos||[]).filter((item)=>assetItem(item.assetId)?.type==='video'&&!used.has(item.assetId)).slice(0,3).map((item)=>{used.add(item.assetId);const audioValid=item.audioAssetId&&assetItem(item.audioAssetId)?.type==='audio'&&!used.has(item.audioAssetId);if(audioValid)used.add(item.audioAssetId!);const hasPairedAudio=Boolean(item.useEmbeddedAudio||audioValid);return {...item,purpose:item.purpose||'motion_camera_reference',useEmbeddedAudio:Boolean(item.useEmbeddedAudio),audioAssetId:audioValid?item.audioAssetId:undefined,audioPurpose:hasPairedAudio?(item.audioPurpose||'audio_reference'):undefined}});const keptAudios:H3ReferenceBindings['audios']=[];for(const item of bindings.audios||[]){if(keptAudios.length>=3)break;if(assetItem(item.assetId)?.type==='audio'&&!used.has(item.assetId)){keptAudios.push({...item,purpose:item.purpose||'audio_reference'});used.add(item.assetId)}}bindings.audios=keptAudios;syncReferenceAssets(shot)}
const autoBindFromBible = (shot:Shot,showNotice=false,shouldInvalidate=true) => {
  const bindings=bindingsFor(shot);const used=new Set<string>()
  const bindList=(kind:'character'|'prop',ids:string[])=>ids.map((entityId)=>{
    const current=(kind==='character'?bindings.characters:bindings.props).find((item)=>item.entityId===entityId)
    const valid=current&&assetItem(current.assetId)?.type===kind&&!used.has(current.assetId)?assetItem(current.assetId):undefined
    const asset=valid||preferredUnusedAsset(kind,entityId,used);if(asset)used.add(asset.id)
    return asset?{entityId,assetId:asset.id}:null
  }).filter((item):item is {entityId:string;assetId:string}=>Boolean(item))
  bindings.characters=bindList('character',shot.characterIds)
  if(shot.sceneId){
    const current=bindings.scene?.entityId===shot.sceneId&&assetItem(bindings.scene.assetId)?.type==='scene'&&!used.has(bindings.scene.assetId)?assetItem(bindings.scene.assetId):undefined
    const asset=current||preferredUnusedAsset('scene',shot.sceneId,used);bindings.scene=asset?{entityId:shot.sceneId,assetId:asset.id}:undefined;if(asset)used.add(asset.id)
  }else bindings.scene=undefined
  bindings.props=bindList('prop',shot.propIds)
  syncReferenceAssets(shot);if(showNotice)markBindingConfigured(shot);if(shouldInvalidate)invalidatePrompt(shot)
  if(showNotice)studio.notify('已按一致性 Bible 自动绑定人物、场景和道具参考图','info')
}
const referenceErrorsFor = (shot:Shot):string[] => {
  if(shot.mediaType==='image'||shot.mode==='T2VA')return []
  const bindings=bindingsFor(shot)
  if(shot.mode==='FL2VA'){
    const errors:string[]=[]
    if(bindings.firstFrameAssetId&&bindings.firstFrameAssetId===bindings.lastFrameAssetId)errors.push('首帧和尾帧不能复用同一张图片')
    if(bindings.firstFrameAssetId&&!assetItem(bindings.firstFrameAssetId))errors.push('首帧素材已失效，请重新选择')
    if(bindings.lastFrameAssetId&&!assetItem(bindings.lastFrameAssetId))errors.push('尾帧素材已失效，请重新选择')
    return errors
  }
  const ordered=orderedRefBindings(shot);const errors:string[]=[]
  if(bindings.firstFrameReferenceAssetId&&!assetItem(bindings.firstFrameReferenceAssetId))errors.push('首帧连续参考素材已失效')
  for(const item of ordered){const asset=assetItem(item.assetId);if(!asset)errors.push(`${item.entityName} 的参考素材已失效`);else if(asset.type!==item.category)errors.push(`${item.entityName} 必须选择${item.category==='character'?'人物':item.category==='scene'?'场景':'道具'}分类素材`);if(!bibleAssetIds(bibleItem(item.category,item.entityId)).includes(item.assetId))errors.push(`${item.entityName} 的图片必须来自该实体的一致性 Bible 素材`)}
  const bindingsVideos=bindings.videos||[];const bindingsAudios=bindings.audios||[]
  const reservesFirstFrameReference=shot.requiresFirstFrameContinuity||Boolean(bindings.firstFrameReferenceAssetId)
  const pictureCount=ordered.length+(reservesFirstFrameReference?1:0)
  if(pictureCount>9)errors.push(`当前绑定 ${pictureCount} 张，MiniMax H3 最多支持 9 张参考图`)
  if(bindingsVideos.length>3)errors.push('参考视频最多允许 3 个')
  if(bindingsAudios.length>3)errors.push('独立参考音频最多允许 3 个')
  for(const [index,item] of bindingsVideos.entries()){const video=assetItem(item.assetId);if(video?.type!=='video')errors.push(`参考视频 ${index+1} 已失效或类型错误`);if(item.useEmbeddedAudio&&item.audioAssetId)errors.push(`参考视频 ${index+1} 的视频原音与外部配对音频不能同时启用`);if(item.useEmbeddedAudio&&video?.hasEmbeddedAudio!==true)errors.push(`参考视频 ${index+1} 未检测到可用内嵌音轨`);if(item.audioAssetId&&assetItem(item.audioAssetId)?.type!=='audio')errors.push(`参考视频 ${index+1} 的配对音频已失效或类型错误`)}
  for(const [index,item] of bindingsAudios.entries())if(assetItem(item.assetId)?.type!=='audio')errors.push(`独立参考音频 ${index+1} 已失效或类型错误`)
  const ids=[bindings.firstFrameReferenceAssetId,...ordered.map((item)=>item.assetId),...bindingsVideos.flatMap((item)=>[item.assetId,item.audioAssetId]).filter(Boolean).map(String),...bindingsAudios.map((item)=>item.assetId)].filter(Boolean).map(String);if(new Set(ids).size!==ids.length)errors.push('同一素材不能重复承担多个图片、视频或音频输入槽位')
  return [...new Set(errors)]
}
const referenceValidationErrors = computed(()=>selectedShot.value?referenceErrorsFor(selectedShot.value):[])
const finalReferenceInputs = computed(()=>{if(!selectedShot.value||selectedShot.value.mode!=='Ref2VA')return [] as Array<{label:string;kind:string;name:string;detail:string}>;const shot=selectedShot.value;const bindings=bindingsFor(shot);let pictureIndex=1;const result:Array<{label:string;kind:string;name:string;detail:string}>=[];if(shot.requiresFirstFrameContinuity||bindings.firstFrameReferenceAssetId)result.push({label:`Picture ${pictureIndex++}`,kind:'首帧连续参考',name:bindings.firstFrameReferenceAssetId?(assetItem(bindings.firstFrameReferenceAssetId)?.name||'素材已失效'):'等待上一镜尾帧自动生成',detail:'上一镜尾帧；作为本镜头开场状态参考'});for(const item of orderedRefBindings(shot))result.push({label:`Picture ${pictureIndex++}`,kind:item.category==='character'?'人物':item.category==='scene'?'场景':'道具',name:item.entityName,detail:assetItem(item.assetId)?.name||'素材已失效'});let audioIndex=1;for(const [index,item] of bindings.videos.entries()){if(item.useEmbeddedAudio||item.audioAssetId){result.push({label:`Audio ${audioIndex++}`,kind:'视频配对音频',name:item.useEmbeddedAudio?'使用视频原音':'外部配对音频',detail:`${audioPurposeLabel(item.audioPurpose)} · ${item.useEmbeddedAudio?(assetItem(item.assetId)?.name||'视频已失效'):(assetItem(item.audioAssetId)?.name||'音频已失效')}`})}result.push({label:`Video ${index+1}`,kind:'参考视频',name:assetItem(item.assetId)?.name||'视频已失效',detail:`${videoPurposeLabel(item.purpose)} · ${item.useEmbeddedAudio?'携带原音':item.audioAssetId?'已配外部音轨':'无配对音频'}`})}for(const item of bindings.audios)result.push({label:`Audio ${audioIndex++}`,kind:'独立参考音频',name:assetItem(item.assetId)?.name||'音频已失效',detail:audioPurposeLabel(item.purpose)});return result})
const setShotMode = (mode:H3Mode) => {if(!selectedShot.value)return;const shot=selectedShot.value;shot.mode=mode;markBindingConfigured(shot);const bindings=bindingsFor(shot);if(mode==='T2VA')shot.referenceBindings=emptyBindings();else if(mode==='FL2VA'){bindings.firstFrameReferenceAssetId=undefined;bindings.characters=[];bindings.scene=undefined;bindings.props=[];bindings.videos=[];bindings.audios=[]}else{bindings.firstFrameAssetId=undefined;bindings.lastFrameAssetId=undefined;pruneReferenceBindings(shot)}syncReferenceAssets(shot);invalidatePrompt(shot)}
const assetUsedElsewhere = (shot:Shot,assetId:string,slot:string) => {
  if(shot.mode==='FL2VA'){const bindings=bindingsFor(shot);return slot==='first'?bindings.lastFrameAssetId===assetId:bindings.firstFrameAssetId===assetId}
  const bindings=bindingsFor(shot)
  if(bindings.firstFrameReferenceAssetId&&bindings.firstFrameReferenceAssetId===assetId&&slot!=='first_frame_reference')return true
  return orderedRefBindings(shot).some((item)=>`${item.category}:${item.entityId}`!==slot&&item.assetId===assetId)
}
const selectFrameAsset=(slot:'first'|'last',assetId:string)=>{if(!selectedShot.value)return;if(assetUsedElsewhere(selectedShot.value,assetId,slot)){studio.notify('首帧和尾帧必须使用不同图片','error');return}const shot=selectedShot.value;markBindingConfigured(shot);const bindings=bindingsFor(shot);if(slot==='first')bindings.firstFrameAssetId=assetId;else bindings.lastFrameAssetId=assetId;syncReferenceAssets(shot);invalidatePrompt(shot)}
const selectEntityAsset=(kind:BibleAssetType,entityId:string,assetId:string)=>{if(!selectedShot.value)return;if(assetId&&assetUsedElsewhere(selectedShot.value,assetId,`${kind}:${entityId}`)){studio.notify('同一张素材不能重复绑定到不同实体','error');return}const shot=selectedShot.value;markBindingConfigured(shot);const bindings=bindingsFor(shot);if(kind==='character'){bindings.characters=bindings.characters.filter((item)=>item.entityId!==entityId);if(assetId)bindings.characters.push({entityId,assetId})}else if(kind==='scene')bindings.scene=assetId?{entityId,assetId}:undefined;else{bindings.props=bindings.props.filter((item)=>item.entityId!==entityId);if(assetId)bindings.props.push({entityId,assetId})}syncReferenceAssets(shot);invalidatePrompt(shot)}
const mediaAssetIds=(shot:Shot)=>{const bindings=bindingsFor(shot);return new Set([bindings.firstFrameReferenceAssetId,...orderedRefBindings(shot).map((item)=>item.assetId),...bindings.videos.flatMap((item)=>[item.assetId,item.audioAssetId]).filter(Boolean).map(String),...bindings.audios.map((item)=>item.assetId)].filter(Boolean).map(String))}
const toggleReferenceVideo=(assetId:string)=>{if(!selectedShot.value)return;const shot=selectedShot.value;const bindings=bindingsFor(shot);const index=bindings.videos.findIndex((item)=>item.assetId===assetId);if(index>=0)bindings.videos.splice(index,1);else{if(bindings.videos.length>=3){studio.notify('参考视频最多选择 3 个','error');return}if(mediaAssetIds(shot).has(assetId)){studio.notify('该素材已占用其他输入槽位','error');return}bindings.videos.push({assetId,purpose:'motion_camera_reference',useEmbeddedAudio:false})}markBindingConfigured(shot);syncReferenceAssets(shot);invalidatePrompt(shot)}
const setVideoPurpose=(videoAssetId:string,event:Event)=>{if(!selectedShot.value)return;const select=event.target as HTMLSelectElement;const purpose=select.value as H3ReferenceVideoPurpose;const item=bindingsFor(selectedShot.value).videos.find((binding)=>binding.assetId===videoAssetId);if(!item)return;if(videoPurposeOptionDisabled(selectedShot.value,videoAssetId,purpose)){select.value=item.purpose;studio.notify('同一 Ref2VA 分镜不能同时使用“视频编辑”和“视频续写”；请统一用途，或拆成不同分镜任务。','error');return}item.purpose=purpose;markBindingConfigured(selectedShot.value);invalidatePrompt(selectedShot.value)}
const setVideoAudioPurpose=(videoAssetId:string,event:Event)=>{if(!selectedShot.value)return;const item=bindingsFor(selectedShot.value).videos.find((binding)=>binding.assetId===videoAssetId);if(!item||(!item.useEmbeddedAudio&&!item.audioAssetId))return;item.audioPurpose=(event.target as HTMLSelectElement).value as H3ReferenceAudioPurpose;markBindingConfigured(selectedShot.value);invalidatePrompt(selectedShot.value)}
const setVideoAudioMode=(videoAssetId:string,mode:'none'|'embedded'|'external')=>{if(!selectedShot.value)return;const shot=selectedShot.value;const item=bindingsFor(shot).videos.find((binding)=>binding.assetId===videoAssetId);if(!item)return;if(mode==='embedded'&&assetItem(videoAssetId)?.hasEmbeddedAudio!==true){studio.notify('该视频未检测到内嵌音轨，请改用无配对音频或上传外部音频','error');return}item.useEmbeddedAudio=mode==='embedded';if(mode!=='external')item.audioAssetId=undefined;if(mode==='embedded')item.audioPurpose ||= 'audio_reference';else if(mode==='none')item.audioPurpose=undefined;externalAudioPickerVideoId.value=mode==='external'?videoAssetId:'';markBindingConfigured(shot);syncReferenceAssets(shot);invalidatePrompt(shot)}
const setPairedAudio=(videoAssetId:string,audioAssetId:string)=>{if(!selectedShot.value)return;const shot=selectedShot.value;const item=bindingsFor(shot).videos.find((binding)=>binding.assetId===videoAssetId);if(!item)return;if(audioAssetId&&mediaAssetIds(shot).has(audioAssetId)&&item.audioAssetId!==audioAssetId){studio.notify('该音频已占用其他输入槽位','error');return}item.useEmbeddedAudio=false;item.audioAssetId=audioAssetId||undefined;item.audioPurpose=audioAssetId?(item.audioPurpose||'audio_reference'):undefined;externalAudioPickerVideoId.value=audioAssetId?videoAssetId:'';markBindingConfigured(shot);syncReferenceAssets(shot);invalidatePrompt(shot)}
const toggleIndependentAudio=(assetId:string)=>{if(!selectedShot.value)return;const shot=selectedShot.value;const bindings=bindingsFor(shot);const index=bindings.audios.findIndex((item)=>item.assetId===assetId);if(index>=0)bindings.audios.splice(index,1);else{if(bindings.audios.length>=3){studio.notify('独立参考音频最多选择 3 个','error');return}if(mediaAssetIds(shot).has(assetId)){studio.notify('该音频已作为视频配对音轨或其他输入使用','error');return}bindings.audios.push({assetId,purpose:'audio_reference'})}markBindingConfigured(shot);syncReferenceAssets(shot);invalidatePrompt(shot)}
const setIndependentAudioPurpose=(assetId:string,event:Event)=>{if(!selectedShot.value)return;const item=bindingsFor(selectedShot.value).audios.find((binding)=>binding.assetId===assetId);if(!item)return;item.purpose=(event.target as HTMLSelectElement).value as H3ReferenceAudioPurpose;markBindingConfigured(selectedShot.value);invalidatePrompt(selectedShot.value)}
const startVideoUpload=()=>videoUploadInput.value?.click()
const startAudioUpload=(videoAssetId='')=>{uploadAudioForVideoId.value=videoAssetId;audioUploadInput.value?.click()}
const uploadReferenceFile=async(kind:'video'|'audio',event:Event)=>{const input=event.target as HTMLInputElement;const file=input.files?.[0];if(!file||!project.value||!selectedShot.value)return;uploadingReference.value=kind;try{const asset=await studio.uploadReferenceAsset(file,kind==='video'?'reference_video':'reference_audio',project.value.styleId);if(kind==='video')toggleReferenceVideo(asset.id);else if(uploadAudioForVideoId.value)setPairedAudio(uploadAudioForVideoId.value,asset.id);else toggleIndependentAudio(asset.id)}catch(error){studio.notify(error instanceof Error?error.message:'参考素材上传失败','error')}finally{uploadingReference.value='';uploadAudioForVideoId.value='';input.value=''}}
const toggleEntity=(kind:'character'|'prop',id:string)=>{if(!selectedShot.value)return;const shot=selectedShot.value;markBindingConfigured(shot);const list=kind==='character'?shot.characterIds:shot.propIds;const i=list.indexOf(id);if(i>=0)list.splice(i,1);else list.push(id);if(shot.mode==='Ref2VA')pruneReferenceBindings(shot);invalidatePrompt(shot)}
const selectScene=(id:string)=>{if(!selectedShot.value)return;const shot=selectedShot.value;markBindingConfigured(shot);shot.sceneId=id;if(shot.mode==='Ref2VA')pruneReferenceBindings(shot);invalidatePrompt(shot)}
const hydrateGeneratedPromptCache=()=>{
  generatedPromptsByShot.value=Object.fromEntries((episode.value?.shots||[]).filter((shot)=>Boolean(shot.generatedPrompt)&&shot.generatedPrompt?.compiler_current!==false).map((shot)=>[shot.id,clone(shot.generatedPrompt!)]))
  generatedPrompt.value=generatedPromptsByShot.value[selectedShotId.value]||null
}

const setShotOutputPreview = (shotId: string, kind: 'video' | 'image', workflow: Workflow, forceUrlRefresh = false) => {
  const media = workflowOutputMedia(workflow, kind)
  if (!media?.previewUrl) return false
  const taskId = workflow.taskId || workflow.id
  const current = shotOutputPreviews.value[shotId]
  // Poll responses carry newly signed URLs. Keep the current URL for the same
  // task so an in-progress playback is not reloaded every five seconds.
  if (!forceUrlRefresh && current?.taskId === taskId && current.url) return true
  shotOutputPreviews.value = {
    ...shotOutputPreviews.value,
    [shotId]: {
      taskId,
      kind,
      url: media.previewUrl,
      filename: media.filename,
      downloadUrl: media.downloadUrl || media.previewUrl,
      completedAt: workflow.finishedAt || workflow.createdAt
    }
  }
  return true
}
const clearShotOutputPreview = (shotId: string) => {
  if (!shotOutputPreviews.value[shotId]) return
  const next = { ...shotOutputPreviews.value }
  delete next[shotId]
  shotOutputPreviews.value = next
}
const schedulePreviewPoll = () => {
  if (previewPollTimer !== undefined) window.clearTimeout(previewPollTimer)
  if (previewDisposed || !selectedShot.value) return
  const delay = latestActiveTask.value ? 5_000 : 15_000
  previewPollTimer = window.setTimeout(async () => {
    if (previewPollInFlight || previewDisposed) return schedulePreviewPoll()
    previewPollInFlight = true
    try { await loadShotOutputPreview(true) }
    finally { previewPollInFlight = false }
  }, delay)
}
const loadShotOutputPreview = async (silent = false) => {
  const shot = selectedShot.value
  if (!shot) return
  const shotId = shot.id
  const kind = shot.mediaType
  const epoch = ++previewRequestEpoch
  if (!silent) previewLoading.value = true
  previewError.value = ''
  try {
    const tasks = await repository.getShotWorkflows(shotId, kind)
    if (previewDisposed || epoch !== previewRequestEpoch || selectedShotId.value !== shotId) return
    shotWorkflowTasks.value = { ...shotWorkflowTasks.value, [shotId]: tasks }
    const successful = [...tasks]
      .filter((workflow) => workflow.status === 'completed' && Boolean(workflowOutputMedia(workflow, kind)?.previewUrl))
      .sort((left, right) => workflowTimestamp(right, true) - workflowTimestamp(left, true))[0]
    const active = tasks.some((workflow) => ['queued', 'running', 'paused'].includes(workflow.status))
    if (active) shot.status = 'rendering'
    else if (successful) shot.status = 'done'
    if (!successful) {
      clearShotOutputPreview(shotId)
      return
    }
    setShotOutputPreview(shotId, kind, successful)
    const taskId = successful.taskId || successful.id
    if (!hydratedPreviewTasks.has(taskId)) {
      try {
        const detail = await repository.getWorkflow(taskId)
        if (previewDisposed || epoch !== previewRequestEpoch || selectedShotId.value !== shotId) return
        hydratedPreviewTasks.add(taskId)
        setShotOutputPreview(shotId, kind, detail, true)
      } catch {
        // The list response already contains a short-lived signed URL. Detail
        // refresh failure must not hide a playable result.
      }
    }
  } catch (error) {
    if (previewDisposed || epoch !== previewRequestEpoch || selectedShotId.value !== shotId) return
    previewError.value = error instanceof Error ? error.message : `${kind === 'image' ? '图片' : '视频'}产物读取失败`
  } finally {
    if (epoch === previewRequestEpoch) previewLoading.value = false
    if (!previewDisposed && selectedShotId.value === shotId) schedulePreviewPoll()
  }
}
const resetPreviewPlayer = () => {
  const video = previewVideo.value
  if (video) { video.pause(); video.currentTime = 0 }
  previewPlaying.value = false
  previewCurrentTime.value = 0
}
const togglePreviewPlayback = async () => {
  if (!selectedVideoPreview.value?.url || previewLoading.value) return
  await nextTick()
  const video = previewVideo.value
  if (!video) return
  if (!video.paused) { video.pause(); return }
  try { await video.play() }
  catch (error) { previewError.value = error instanceof Error ? error.message : '浏览器无法播放该视频' }
}
const handlePreviewLoaded = () => {
  const video = previewVideo.value
  previewDuration.value = Number.isFinite(video?.duration) ? Number(video?.duration) : Number(selectedShot.value?.duration || 0)
  previewCurrentTime.value = Number(video?.currentTime || 0)
  const taskId = selectedVideoPreview.value?.taskId
  if (taskId) previewRecoveryAttempts.delete(taskId)
  previewError.value = ''
}
const handlePreviewTimeUpdate = () => { previewCurrentTime.value = Number(previewVideo.value?.currentTime || 0) }
const seekPreview = (event: MouseEvent) => {
  const video = previewVideo.value
  if (!video || !previewDuration.value) return
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / Math.max(1, rect.width)))
  video.currentTime = ratio * previewDuration.value
  previewCurrentTime.value = video.currentTime
}
const selectAdjacentVideoShot = (direction: -1 | 1) => {
  const target = videoShots.value[selectedVideoShotIndex.value + direction]
  if (target) selectedShotId.value = target.id
}
const recoverPreviewUrl = async (manual = false) => {
  const preview = selectedOutputPreview.value
  const shotId = selectedShotId.value
  if (!preview || previewRecoveryTasks.has(preview.taskId)) return
  if (!manual && (previewRecoveryAttempts.get(preview.taskId) || 0) >= 1) {
    previewError.value = `${preview.kind === 'image' ? '图片' : '视频'}地址已刷新但仍无法预览，请点击重试；若持续失败，请检查 ComfyUI 输出文件。`
    return
  }
  if (manual) previewRecoveryAttempts.delete(preview.taskId)
  previewRecoveryAttempts.set(preview.taskId, (previewRecoveryAttempts.get(preview.taskId) || 0) + 1)
  previewRecoveryTasks.add(preview.taskId); previewRecovering.value = true
  try {
    const detail = await repository.getWorkflow(preview.taskId)
    if (selectedShotId.value !== shotId || selectedOutputPreview.value?.taskId !== preview.taskId) return
    if (!setShotOutputPreview(shotId, preview.kind, detail, true)) throw new Error(`任务没有可预览的${preview.kind === 'image' ? '图片' : '视频'}产物`)
    if (preview.kind === 'video') { await nextTick(); previewVideo.value?.load() }
  } catch (error) {
    if (selectedShotId.value !== shotId) return
    previewError.value = error instanceof Error ? error.message : `${preview.kind === 'image' ? '图片' : '视频'}地址刷新失败`
  } finally {
    previewRecoveryTasks.delete(preview.taskId)
    previewRecovering.value = Boolean(selectedOutputPreview.value?.taskId && previewRecoveryTasks.has(selectedOutputPreview.value.taskId))
  }
}
const retryPreviewPlayback = () => { previewError.value=''; void recoverPreviewUrl(true) }
const handleImagePreviewLoaded = () => { const taskId=selectedImagePreview.value?.taskId;if(taskId)previewRecoveryAttempts.delete(taskId);previewError.value='' }
const downloadSelectedOutput = async () => {
  const preview=selectedOutputPreview.value
  const shotId=selectedShotId.value
  if(!preview||!shotId)return
  try{
    const detail=await repository.getWorkflow(preview.taskId)
    if(selectedShotId.value!==shotId)return
    if(!setShotOutputPreview(shotId,preview.kind,detail,true))throw new Error(`任务没有可下载的${preview.kind==='image'?'图片':'视频'}产物`)
    const fresh=selectedOutputPreview.value
    const url=String(fresh?.downloadUrl||fresh?.url||'').trim()
    const safeUrl=url.startsWith('/')&&!url.startsWith('//')?new URL(url,window.location.origin).toString():/^https?:\/\//i.test(url)?url:''
    if(!safeUrl)throw new Error('服务端未返回安全的下载地址')
    const anchor=document.createElement('a');anchor.href=safeUrl;anchor.download=fresh?.filename||preview.filename;anchor.rel='noopener';document.body.appendChild(anchor);anchor.click();anchor.remove()
  }catch(error){studio.notify(`下载${preview.kind==='image'?'图片':'视频'}失败：${error instanceof Error?error.message:'请稍后重试'}`,'error')}
}

const newShot = (order:number):Shot => ({ id:`shot-${Date.now()}-${order}`, order, title:`镜头 ${String(order).padStart(2,'0')}`, mediaType:'video', duration:(productionCapability.value?.scriptBreakdownDefaultShotDurationMs??0)/1000, mode:['T2VA','FL2VA','Ref2VA'].includes(project.value?.defaultMode||'')?project.value!.defaultMode:'FL2VA', framing:'中景', camera:'', action:'', continuityDirection:'', prompt:'', negativePrompt:'', characterIds:[], sceneId:project.value?.bible.scenes[0]?.id, propIds:[], referenceAssetIds:[], referenceBindings:emptyBindings(), requiresFirstFrameContinuity:productionCapability.value?.scriptBreakdownDefaultFirstFrameContinuity===true, requiresLastFrameContinuity:productionCapability.value?.scriptBreakdownDefaultLastFrameContinuity===true, audioEnabled:episode.value?.audioEnabled ?? true, audio:{ dialogue:'', soundscape:'', music:'' }, status:'draft' })
const load = async () => {
  loading.value=true; await studio.initialize(); const found=await studio.loadProject(String(route.params.projectId)); if(!found){router.replace('/projects');return}
  project.value=clone(found); const foundEpisode=project.value.episodes.find(item=>item.id===String(route.params.episodeId)); if(!foundEpisode){router.replace(`/projects/${project.value.id}`);return}
  episode.value=foundEpisode; if(!episode.value.shots.length) episode.value.shots.push(newShot(1));
  selectedShotVersionKey.value = episode.value.activeShotVersionKey || episode.value.shotVersions?.find((item)=>item.active)?.key || episode.value.shotVersions?.[0]?.key || ''
  const requestedShotId=String(Array.isArray(route.query.shot)?route.query.shot[0]||'':route.query.shot||'')
  selectedShotId.value=episode.value.shots.some((shot)=>shot.id===requestedShotId)?requestedShotId:episode.value.shots[0].id;await studio.refreshAssets()
  let mappedLegacyMode=false;let legacyReferenceNeedsReview=false
  for(const shot of episode.value.shots){
    if(!['T2VA','FL2VA','Ref2VA'].includes(shot.mode)){shot.mode='FL2VA';mappedLegacyMode=true}
    if(shot.mediaType==='image'){shot.referenceBindings=emptyBindings();shot.referenceAssetIds=[];shot.audioEnabled=false;continue}
    const bindings=bindingsFor(shot)
    if(shot.mode==='T2VA')shot.referenceBindings=emptyBindings()
    else if(shot.mode==='FL2VA'){bindings.characters=[];bindings.scene=undefined;bindings.props=[];bindings.videos=[];bindings.audios=[]}
    else{bindings.firstFrameAssetId=undefined;bindings.lastFrameAssetId=undefined}
    if(shot.mode==='Ref2VA'){pruneReferenceBindings(shot);if(!orderedRefBindings(shot).length&&shot.metadata?.referenceBindingConfigured!==true)legacyReferenceNeedsReview=true}else syncReferenceAssets(shot)
  }
  try {
    const translations=await repository.getProjectTranslations(project.value.id,{episodeId:episode.value.id})
    for(const shot of episode.value.shots){
      if(promptArtifactNeedsRegeneration(shot.generatedPrompt,project.value.id,shot,translations))invalidatePrompt(shot,shot.generatedPrompt?.compiler_current===false?'compiler_updated':'translations_changed')
    }
  }catch(error){
    studio.notify(error instanceof Error?`未能核对提示词与译文版本：${error.message}`:'未能核对提示词与译文版本','error')
  }
  hydrateGeneratedPromptCache();loading.value=false
  if(mappedLegacyMode)studio.notify('检测到 I2VA/L2VA 模式，已使用“首尾帧生视频”；请确认首帧/尾帧槽位（均可为空）','info')
  else if(legacyReferenceNeedsReview)studio.notify('当前参考生视频未包含显式分类绑定；可保持零参考，或点击“按一致性 Bible 自动绑定”后检查 Picture 顺序','info')
}
onMounted(async()=>{await Promise.all([loadProductionWorkflowTemplates(),loadProductionCapability()]);await load()})
watch(selectedShotId,(id)=>{
  shotRef2vaTemplateId.value='';shotRef2vaParameters.value={}
  previewRequestEpoch += 1
  if (previewPollTimer !== undefined) window.clearTimeout(previewPollTimer)
  resetPreviewPlayer(); previewDuration.value=0; previewError.value=''
  generatedPrompt.value=generatedPromptsByShot.value[id] || null
  if (id) void loadShotOutputPreview()
  const routedShot=String(Array.isArray(route.query.shot)?route.query.shot[0]||'':route.query.shot||'')
  if(id&&routedShot!==id)void router.replace({query:{...route.query,shot:id}})
})
watch(()=>route.query.shot,(value)=>{const id=String(Array.isArray(value)?value[0]||'':value||'');if(id&&id!==selectedShotId.value&&episode.value?.shots.some((shot)=>shot.id===id))selectedShotId.value=id})
watch(selectedPromptInputs,(current,previous)=>{
  if(!current||!previous||current.shotId!==previous.shotId||current.fingerprint===previous.fingerprint)return
  const shot=selectedShot.value
  if(shot?.id===current.shotId&&(shot.generatedPrompt||generatedPromptsByShot.value[shot.id]))invalidatePrompt(shot)
})
watch(() => selectedOutputPreview.value?.url, (url, previousUrl) => {
  if (url === previousUrl) return
  resetPreviewPlayer(); previewDuration.value=0
  if (url) void nextTick(() => previewVideo.value?.load())
})
watch(() => [selectedShotId.value, selectedShot.value?.mediaType] as const, ([id,mediaType],[previousId,previousMediaType]) => { if(id===previousId&&mediaType!==previousMediaType){const next={...generatedPromptsByShot.value};delete next[id];generatedPromptsByShot.value=next;generatedPrompt.value=null;clearShotOutputPreview(id);if(selectedShot.value){selectedShot.value.generatedPrompt=undefined;selectedShot.value.prompt='';selectedShot.value.status='draft';if(mediaType==='image'){selectedShot.value.referenceBindings=emptyBindings();selectedShot.value.referenceAssetIds=[]}}void loadShotOutputPreview()}if(mediaType==='image'){selectedShot.value!.audioEnabled=false;if(inspectorTab.value==='audio')inspectorTab.value='visual'} })
onBeforeUnmount(()=>{
  previewDisposed=true;previewRequestEpoch+=1
  if(previewPollTimer!==undefined)window.clearTimeout(previewPollTimer)
  previewVideo.value?.pause()
})
const addShot = () => { if(!episode.value)return; const item=newShot(episode.value.shots.length+1); episode.value.shots.push(item); selectedShotId.value=item.id }
const deleteShot = (id:string) => { if(!episode.value || episode.value.shots.length===1){studio.notify('至少保留一个镜头','error');return}; const index=episode.value.shots.findIndex(item=>item.id===id); episode.value.shots.splice(index,1);episode.value.shots.forEach((item,i)=>item.order=i+1);const next={...generatedPromptsByShot.value};delete next[id];generatedPromptsByShot.value=next;selectedShotId.value=episode.value.shots[Math.max(0,index-1)].id }
const moveShot=(from:number,to:number)=>{if(!episode.value||to<0||to>=episode.value.shots.length)return;const [item]=episode.value.shots.splice(from,1);episode.value.shots.splice(to,0,item);episode.value.shots.forEach((x,i)=>x.order=i+1)}
const persistEpisodeKeepingSelection = async (): Promise<Shot | null> => {
  if (!project.value || !episode.value) return null
  if (translationPromotionBusy.value) throw new Error('译文正在原子同步到正式分镜字段，请等待完成后再保存或生成。')
  if (legacyNegativeRepairShots.value.length) {
    throw new Error(`本集有 ${legacyNegativeRepairShots.value.length} 个镜头的来源匹配负面约束待服务端审计恢复；请进入对应镜头点击“审计并恢复负面约束”后再保存或生成。`)
  }
  if (canonicalTargetCollisionError.value) throw new Error(`${canonicalTargetCollisionError.value} 请回到“连续剧工作室 → 项目总览 → 全剧视觉规则”修正术语表后再保存或生成。`)
  const previous = selectedShot.value ? clone(selectedShot.value) : null
  const previousId = selectedShotId.value
  const previousOrder = previous?.order
  episode.value = clone(await studio.saveEpisode(project.value, episode.value))
  selectedShotVersionKey.value = episode.value.activeShotVersionKey || episode.value.shotVersions?.find((item)=>item.active)?.key || selectedShotVersionKey.value
  const restored = episode.value.shots.find((item) => item.id === previousId)
    || episode.value.shots.find((item) => item.order === previousOrder)
    || episode.value.shots[0]
    || null
  selectedShotId.value = restored?.id || ''
  if (restored && previous && !restored.generatedPrompt && previous.generatedPrompt) {
    restored.generatedPrompt = clone(previous.generatedPrompt)
    restored.prompt = previous.prompt
    restored.status = previous.status
  }
  hydrateGeneratedPromptCache()
  return restored
}
const persistCurrentShotKeepingSelection = async (): Promise<Shot | null> => {
  if (!project.value || !episode.value || !selectedShot.value) return null
  if (translationPromotionBusy.value) throw new Error('译文正在原子同步到正式分镜字段，请等待完成后再保存或生成。')
  if (selectedShot.value.legacyNegativePromptRepairEligible) {
    throw new Error('当前镜头的负面约束与创作草稿连续性字段来源匹配；请先点击“审计并恢复负面约束”，或人工改为真实的英文缺陷排除词后再保存或生成。')
  }
  if (canonicalTargetCollisionError.value) throw new Error(`${canonicalTargetCollisionError.value} 请回到“连续剧工作室 → 项目总览 → 全剧视觉规则”修正术语表后再保存或生成。`)
  const previousId = selectedShot.value.id
  const saved = clone(await studio.saveShot(project.value, episode.value, selectedShot.value))
  const selectionStillTargetsSavedShot = selectedShotId.value === previousId || selectedShotId.value === saved.id
  const index = episode.value.shots.findIndex((item) => item.id === saved.id || item.id === previousId)
  if (index >= 0) episode.value.shots[index] = saved; else episode.value.shots.push(saved)
  if (previousId !== saved.id) {
    const prompts = { ...generatedPromptsByShot.value }
    const previousPrompt = prompts[previousId]
    delete prompts[previousId]
    const promptToKeep = saved.generatedPrompt || previousPrompt
    if (promptToKeep && promptToKeep.compiler_current !== false) prompts[saved.id] = clone(promptToKeep)
    generatedPromptsByShot.value = prompts
  }
  if (selectionStillTargetsSavedShot) selectedShotId.value = saved.id
  hydrateGeneratedPromptCache()
  return saved
}
const save = async()=>{if(!project.value||!episode.value)return;saving.value=true;try{await persistEpisodeKeepingSelection()}catch(e){studio.notify(e instanceof Error?e.message:'保存分镜失败','error')}finally{saving.value=false}}
const generate=async()=>{if(!project.value||!episode.value||!selectedShot.value)return;generating.value=true;try{const initialErrors=referenceErrorsFor(selectedShot.value);if(initialErrors.length)throw new Error(initialErrors.join('；'));const shot=await persistCurrentShotKeepingSelection();if(!shot)throw new Error('保存后未找到当前分镜');const errors=referenceErrorsFor(shot);if(errors.length)throw new Error(errors.join('；'));const payload=await studio.generatePrompt(project.value,episode.value,shot);generatedPromptsByShot.value={...generatedPromptsByShot.value,[shot.id]:payload};if(selectedShotId.value===shot.id)generatedPrompt.value=payload;shot.prompt=payload.integrated_multimodal_description;shot.generatedPrompt=payload;shot.promptRefreshReason=undefined;shot.status='ready';studio.notify(shot.mediaType==='image'?'一致性图片提示词已生成':`${selectedModeProfile.value.name}提示词已生成`)}catch(e){studio.notify(e instanceof Error?e.message:'提示词生成失败','error')}finally{generating.value=false}}
const generateAll=async()=>{if(!project.value||!episode.value||!episode.value.shots.length)return;generating.value=true;try{for(const shot of episode.value.shots){const errors=referenceErrorsFor(shot);if(errors.length)throw new Error(`${shot.title}：${errors.join('；')}`)}await persistEpisodeKeepingSelection();const shots=[...(episode.value?.shots||[])];for(const item of shots){const payload=await studio.generatePrompt(project.value!,episode.value!,item);generatedPromptsByShot.value={...generatedPromptsByShot.value,[item.id]:payload};if(selectedShotId.value===item.id)generatedPrompt.value=payload;item.prompt=payload.integrated_multimodal_description;item.generatedPrompt=payload;item.promptRefreshReason=undefined;item.status='ready'}studio.notify(`已按各自工作流模板生成 ${shots.length} 个提示词`)}catch(e){studio.notify(e instanceof Error?e.message:'批量生成提示词失败','error')}finally{generating.value=false}}
const restoreWorkflowSelection=(serialized:string|null)=>{wholeRef2vaTemplateId.value='';wholeRef2vaParameters.value={};if(!serialized)return;try{const rows=JSON.parse(serialized) as Array<{templateId?:unknown;parameters?:unknown}>;const row=Array.isArray(rows)?rows[0]:undefined;if(typeof row?.templateId==='string'){wholeRef2vaTemplateId.value=row.templateId;wholeRef2vaParameters.value=row.parameters&&typeof row.parameters==='object'?row.parameters as Record<string,number>: {}}}catch{/* Invalid local retry state falls back to the official default. */}}
const clearWholeEpisodeRetryState=(storageKey:string)=>{wholeEpisodeIdempotencyKey.value='';wholeEpisodeIdempotencyConflict.value='';sessionStorage.removeItem(storageKey);sessionStorage.removeItem(`${storageKey}_profile`);sessionStorage.removeItem(`${storageKey}_multiple`);sessionStorage.removeItem(`${storageKey}_image_profile`);sessionStorage.removeItem(`${storageKey}_image_multiple`);sessionStorage.removeItem(`${storageKey}_workflow_selections`)}
const currentWholeEpisodeRetryStorageKey=()=>episode.value?wholeEpisodeRetryStorageKey(episode.value.id,selectedShotVersionScopeKey.value):''
const abandonWholeEpisodeRetryState=()=>{const storageKey=currentWholeEpisodeRetryStorageKey();if(!storageKey)return;clearWholeEpisodeRetryState(storageKey);wholeEpisodeExtendedConfirmed.value=false;studio.notify('已放弃当前分镜版本的旧整集提交状态；再次生成时会创建新的幂等批次。','info')}
const openWholeEpisodeDialog=()=>{if(!episode.value)return;wholeEpisodeExtendedConfirmed.value=false;wholeEpisodeIdempotencyConflict.value='';const storageKey=currentWholeEpisodeRetryStorageKey();wholeEpisodeIdempotencyKey.value=sessionStorage.getItem(storageKey)||'';const pendingProfile=sessionStorage.getItem(`${storageKey}_profile`);const pendingImageProfile=sessionStorage.getItem(`${storageKey}_image_profile`);wholeEpisodeProfile.value=isProductionResolutionProfile(pendingProfile)?pendingProfile:DEFAULT_PRODUCTION_RESOLUTION_PROFILE;wholeEpisodeImageProfile.value=isProductionResolutionProfile(pendingImageProfile)?pendingImageProfile:'0.98';wholeEpisodeMultiple.value=wholeEpisodeIdempotencyKey.value?normalizeProductionResolutionMultiple(sessionStorage.getItem(`${storageKey}_multiple`)):DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE;wholeEpisodeImageMultiple.value=wholeEpisodeIdempotencyKey.value?normalizeImageProductionResolutionMultiple(sessionStorage.getItem(`${storageKey}_image_multiple`)):DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE;restoreWorkflowSelection(wholeEpisodeIdempotencyKey.value?sessionStorage.getItem(`${storageKey}_workflow_selections`):null);wholeEpisodeOpen.value=true}
const submitWholeEpisode=async()=>{
  if(!project.value||!episode.value||!episode.value.shots.length)return
  generatingWholeEpisode.value=true
  try{
    for(const shot of episode.value.shots){const errors=referenceErrorsFor(shot);if(errors.length)throw new Error(`${shot.title}：${errors.join('；')}`)}
    await persistEpisodeKeepingSelection()
    if(!episode.value)throw new Error('保存后未找到当前剧集')
    const storageKey=currentWholeEpisodeRetryStorageKey()
    const idempotencyKey=wholeEpisodeIdempotencyKey.value||sessionStorage.getItem(storageKey)||`whole-episode-${episode.value.id}-${typeof crypto!=='undefined'&&crypto.randomUUID?crypto.randomUUID():Date.now()}`
    wholeEpisodeIdempotencyKey.value=idempotencyKey;sessionStorage.setItem(storageKey,idempotencyKey);sessionStorage.setItem(`${storageKey}_profile`,wholeEpisodeProfile.value);sessionStorage.setItem(`${storageKey}_multiple`,String(wholeEpisodeMultiple.value));sessionStorage.setItem(`${storageKey}_image_profile`,wholeEpisodeImageProfile.value);sessionStorage.setItem(`${storageKey}_image_multiple`,String(wholeEpisodeImageMultiple.value));sessionStorage.setItem(`${storageKey}_workflow_selections`,JSON.stringify(wholeEpisodeWorkflowSelections.value))
    const task=await studio.generateWholeEpisode(project.value,episode.value,idempotencyKey,wholeEpisodeProfile.value,wholeEpisodeMultiple.value,wholeEpisodeImageProfile.value,wholeEpisodeImageMultiple.value,wholeEpisodeWorkflowSelections.value,wholeEpisodeExtendedConfirmed.value)
    clearWholeEpisodeRetryState(storageKey);wholeEpisodeExtendedConfirmed.value=false;wholeEpisodeOpen.value=false
    router.push({path:'/workflows',query:{task:task.id,batch:task.batchId||''}})
  }catch(error){
    if(productionErrorCode(error)==='production_queue_confirmation_required'){
      wholeEpisodeExtendedConfirmed.value=true
      studio.notify(`本集 ${episode.value?.shots.length||0} 个镜头超过当前队列建议值；镜数会完整保留，请核对提示后再次确认。`,'info')
      return
    }
    const message=error instanceof Error?error.message:'整集生成任务创建失败'
    if(productionErrorCode(error)==='production_idempotency_conflict'||(/(idempotency_key|幂等)/i.test(message)&&/(不完整|不同|新的|新 key|冲突)/i.test(message))){
      wholeEpisodeIdempotencyConflict.value='旧整集提交状态不完整或参数已发生冲突。幂等键已保留，系统不会自动换键重提。请先到生产工作流核对已有任务；只有确认放弃旧尝试后才能创建新批次。'
      studio.notify(wholeEpisodeIdempotencyConflict.value,'error')
      return
    }
    studio.notify(message,'error')
  }
  finally{generatingWholeEpisode.value=false}
}
const saveAndRegenerateShot = async (extendedGenerationConfirmed = false) => {
  if (!project.value || !episode.value || !selectedShot.value) return
  const requestedKind = selectedShot.value.mediaType
  let requestedShotId = selectedShot.value.id
  regeneratingShot.value = true
  try {
    const persistedShot = await persistCurrentShotKeepingSelection()
    if (!persistedShot || persistedShot.mediaType !== requestedKind) throw new Error(`保存后未找到可生成的${requestedKind === 'image' ? '图片' : '视频'}镜头`)
    requestedShotId = persistedShot.id
    const errors = referenceErrorsFor(persistedShot)
    if (errors.length) throw new Error(errors.join('；'))
    const result = await studio.regenerateShot(project.value, episode.value, persistedShot, shotResolutionProfile.value, shotResolutionMultiple.value, shotWorkflowSelections.value, extendedGenerationConfirmed)
    persistedShot.status = 'rendering'
    pendingShotRegenerationConfirmation.value = null
    lastSubmittedTask.value = { shotId: persistedShot.id, taskId: result.taskId }
    studio.notify(`镜头 ${String(persistedShot.order).padStart(2, '0')} ${requestedKind === 'image' ? '图片' : '视频'}已按 ${shotResolutionProfile.value} 档 / ${shotResolutionMultiple.value} 倍数提交 · 任务 ${result.taskId.slice(0, 8)}`)
    await loadShotOutputPreview(true)
  } catch (e) {
    if (!extendedGenerationConfirmed && productionErrorCode(e) === 'production_queue_confirmation_required') {
      const shot = episode.value?.shots.find((item) => item.id === requestedShotId) || selectedShot.value
      if (shot) pendingShotRegenerationConfirmation.value = {
        shotId: shot.id, shotOrder: shot.order, shotTitle: shot.title, mediaType: shot.mediaType,
      }
      studio.notify('当前生产队列超过建议值；请核对提示后明确确认是否继续生成此镜头。', 'info')
      return
    }
    studio.notify(e instanceof Error ? e.message : `镜头${requestedKind === 'image' ? '图片' : '视频'}生成提交失败`, 'error')
  } finally {
    regeneratingShot.value = false
  }
}
const closeShotRegenerationConfirmation = () => {
  if (!regeneratingShot.value) pendingShotRegenerationConfirmation.value = null
}
const confirmShotRegeneration = async () => {
  const pending = pendingShotRegenerationConfirmation.value
  if (!pending) return
  if (selectedShot.value?.id !== pending.shotId) {
    pendingShotRegenerationConfirmation.value = null
    studio.notify('当前选中的镜头已变化，请重新发起生成。', 'error')
    return
  }
  await saveAndRegenerateShot(true)
}
const copyPrompt=async()=>{if(!actualPromptText.value)return;await navigator.clipboard.writeText(actualPromptText.value);studio.notify(generatedPrompt.value?.mediaType==='image'?'实际图片提示词已复制':'实际提交给 H3 的提示词已复制')}
const copyPromptJson=async()=>{if(!promptJson.value)return;await navigator.clipboard.writeText(promptJson.value);studio.notify('提示词结构 JSON 已复制')}
const thumbFor=(assetIds:string[])=>studio.assets.find(a=>assetIds.includes(a.id))?.thumbnail
</script>

<template>
  <div v-if="loading" class="page-loading"><div class="loader-ring"></div><span>正在装载分镜台…</span></div>
  <div v-else-if="project&&episode" class="storyboard-page">
    <header class="storyboard-header">
      <div class="storyboard-title"><button class="icon-btn" @click="router.push(`/projects/${project.id}`)"><ArrowLeft :size="17"/></button><div><span>{{ project.title }} / EP {{ String(episode.number).padStart(2,'0') }}</span><h2>{{ episode.title }}</h2></div><StatusBadge :status="episode.status"/></div>
      <div class="storyboard-global"><label><span title="普通生产可按连续镜头合并提示词；“生成整集”始终逐镜创建独立、可重试任务">普通工作流多分镜</span><ToggleSwitch v-model="episode.multiShot"/></label><label><span title="同步开启或关闭本集现有全部视频镜头的 MiniMax H3 原生音频">原生音频（全部视频镜头）</span><ToggleSwitch v-model="episodeAudioEnabled"/></label><i></i><span>{{ videoShotCount }} 视频 · {{ imageShotCount }} 图片 · {{ totalDuration }} 秒视频</span></div>
      <div class="storyboard-actions">
        <button class="button ghost small" :disabled="saving||editorPersistenceBlocked" :title="editorPersistenceBlockedReason" @click="save"><Save :size="15"/>{{ saving?'保存中':'保存' }}</button>
        <div class="episode-tool-group" aria-label="整集工具">
          <button class="button ghost small" :disabled="!episode.shots.length" @click="openEpisodeDetailDraft('expand')"><WandSparkles :size="15"/>整集扩写</button>
          <button class="button ghost small" :disabled="!episode.shots.length" @click="openEpisodeDetailDraft('apply')"><Check :size="15"/>整集应用</button>
          <button class="button ghost small" :disabled="generating||editorPersistenceBlocked" :title="editorPersistenceBlockedReason" @click="generateAll"><WandSparkles :size="15"/>{{ generating?'生成中…':'生成全部提示词' }}</button>
          <button class="button primary small" :disabled="generatingWholeEpisode||!episode.shots.length||blockedEpisodePromptShots.length>0||editorPersistenceBlocked" :title="editorPersistenceBlocked?editorPersistenceBlockedReason:blockedEpisodePromptShots.length?`${blockedEpisodePromptShots.length} 个已复检的图片/视频提示词仍需英文改写，生产门禁会阻断提交`:'保存分镜后逐镜创建整集批次'" @click="openWholeEpisodeDialog"><Sparkles :size="15"/>生成整集</button>
        </div>
      </div>
    </header>

    <div v-if="canonicalTargetCollisionError" class="canonical-collision-banner"><AlertTriangle :size="15"/><span><strong>canonical 术语碰撞 · 保存、翻译同步和生成已暂停</strong><small>{{ canonicalTargetCollisionError }} 请回到“连续剧工作室 → 项目总览 → 全剧视觉规则”人工修正并保存。</small></span></div>
    <div v-if="episodeShotVersions.length>1" class="shot-version-bar">
      <div><Layers3 :size="16"/><span><strong>分镜版本</strong><small>当前显示 {{ activeShotVersionLabel || '已选版本' }}；保存所选版本会把它设为当前版本，旧版本仍保留用于对比。</small></span></div>
      <select :value="selectedShotVersionKey" :disabled="saving||generating||generatingWholeEpisode" @change="selectShotVersion(($event.target as HTMLSelectElement).value)">
        <option v-for="version in episodeShotVersions" :key="version.key" :value="version.key">{{ version.label }} · {{ version.shotCount }} 镜{{ version.active ? ' · 当前' : '' }}</option>
      </select>
    </div>

    <div class="storyboard-workspace">
      <aside class="shot-rail">
        <header><div><span>SHOT LIST</span><strong>{{ episode.shots.length }} 个镜头</strong></div><button class="icon-btn" @click="addShot"><Plus :size="16"/></button></header>
        <div class="shot-list">
          <article v-for="(shot,index) in episode.shots" :key="shot.id" :class="{active:selectedShotId===shot.id}" @click="selectedShotId=shot.id">
            <button class="drag-handle"><GripVertical :size="14"/></button>
            <div class="shot-thumb" :style="{background:style?.gradient}"><span>{{ String(shot.order).padStart(2,'0') }}</span><Play v-if="selectedShotId===shot.id" :size="14" fill="currentColor"/></div>
            <div class="shot-row-copy"><div><strong>{{ shot.title }}</strong><small>{{ shot.mediaType==='image' ? '图片 · SDXL 一致性定帧' : `${shot.duration}s · ${shot.mode}` }}</small></div><p>{{ shot.action }}</p><div><i :class="shot.status"></i><span>{{ shot.status==='rendering'?`${shot.mediaType==='image'?'图片':'视频'}生成中`:shot.status==='done'?`${shot.mediaType==='image'?'图片':'视频'}就绪`:shot.status==='ready'?'提示词就绪':'待编辑' }}</span><AudioLines v-if="shot.mediaType==='video'&&shot.audioEnabled" :size="12"/></div></div>
            <div class="shot-row-actions"><button @click.stop="moveShot(index,index-1)" :disabled="index===0"><ChevronLeft :size="12"/></button><button @click.stop="moveShot(index,index+1)" :disabled="index===episode.shots.length-1"><ChevronRight :size="12"/></button></div>
          </article>
          <button class="add-shot-inline" @click="addShot"><Plus :size="17"/><span>新增镜头</span></button>
        </div>
      </aside>

      <main v-if="selectedShot" class="shot-editor">
        <section class="preview-stage">
          <div class="preview-toolbar"><span>STAGE PREVIEW</span><div><button v-if="selectedOutputPreview" type="button" class="preview-toolbar-download" :title="`刷新签名并下载最新${selectedShot.mediaType==='image'?'图片':'视频'}`" @click="downloadSelectedOutput"><Download :size="14"/></button><button :disabled="!selectedVideoPreview" title="暂停并回到视频开头" @click="resetPreviewPlayer"><RotateCcw :size="14"/></button><span>FIT</span><button disabled title="暂无更多预览操作"><MoreHorizontal :size="15"/></button></div></div>
          <div class="preview-canvas" :class="[`ratio-${project.aspectRatio.replace(':','-')}`,{playable:Boolean(selectedOutputPreview)}]" :style="{background:style?.gradient}">
            <video v-if="selectedShot.mediaType==='video'&&selectedVideoPreview" :key="`${selectedVideoPreview.taskId}:${selectedVideoPreview.url}`" ref="previewVideo" :src="selectedVideoPreview.url" preload="metadata" playsinline @click="togglePreviewPlayback" @loadedmetadata="handlePreviewLoaded" @timeupdate="handlePreviewTimeUpdate" @play="previewPlaying=true" @pause="previewPlaying=false" @ended="previewPlaying=false" @error="recoverPreviewUrl()">当前浏览器不支持视频预览。</video>
            <img v-else-if="selectedShot.mediaType==='image'&&selectedImagePreview" :key="`${selectedImagePreview.taskId}:${selectedImagePreview.url}`" :src="selectedImagePreview.url" :alt="selectedImagePreview.filename" @load="handleImagePreviewLoaded" @error="recoverPreviewUrl()"/>
            <template v-else><div class="preview-grid-lines"></div><div class="preview-vignette"></div><div class="preview-scene-label"><span>{{ project.bible.scenes.find(s=>s.id===selectedShot?.sceneId)?.name||'未指定场景' }}</span><small>{{ selectedShot.framing }} · {{ selectedShot.camera }}</small></div></template>
            <div v-if="!selectedOutputPreview" class="preview-empty-state" :class="{loading:previewLoading}"><div v-if="previewLoading" class="loader-ring"></div><component :is="selectedShot.mediaType==='image'?Image:Film" v-else :size="24"/><strong>{{ previewStatusText }}</strong><small v-if="previewError">{{ previewError }}</small><small v-else>任务完成后会自动载入真实{{ selectedShot.mediaType==='image'?'图片':'视频' }}产物</small></div>
            <div class="preview-product-state" :class="{stale:previewHasNewerActiveTask,ready:Boolean(selectedOutputPreview)&&!previewHasNewerActiveTask}"><i></i><span>{{ previewStatusText }}</span></div>
            <div v-if="selectedOutputPreview&&previewError" class="preview-error-state"><AlertTriangle :size="18"/><div><strong>{{ selectedShot.mediaType==='image'?'图片暂时无法预览':'视频暂时无法播放' }}</strong><small>{{ previewError }}</small></div><button :disabled="previewRecovering" @click="retryPreviewPlayback">{{ previewRecovering?'重试中…':'重新加载' }}</button></div>
            <button v-if="selectedShot.mediaType==='video'&&selectedVideoPreview" class="preview-play" :aria-label="previewPlaying?'暂停视频':'播放视频'" @click="togglePreviewPlayback"><component :is="previewPlaying?Pause:Play" :size="22" fill="currentColor"/></button>
            <div class="preview-hud top"><span>EP{{ String(episode.number).padStart(2,'0') }} / SHOT {{ String(selectedShot.order).padStart(2,'0') }}</span><span>{{ selectedShot.mediaType==='image' ? 'REFERENCE IMAGE' : `H3 ${selectedShot.mode}` }}</span></div>
            <div class="preview-hud bottom"><span>{{ selectedShot.mediaType==='image'?(selectedImagePreview?'OUTPUT STILL':'STILL'):selectedVideoPreview?'PLAYBACK':'REC' }} <i></i></span><span v-if="selectedShot.mediaType==='video'">{{ Number(previewDuration||selectedShot.duration).toFixed(1) }} SEC</span><span>{{ project.aspectRatio }} · {{ project.resolution }}</span></div>
          </div>
          <div v-if="selectedShot.mediaType==='video'" class="transport"><button :disabled="!hasPreviousVideoShot" title="上一个视频镜头" @click="selectAdjacentVideoShot(-1)"><ChevronLeft :size="15"/></button><button class="transport-play" :disabled="!selectedVideoPreview||previewLoading" :title="selectedVideoPreview?(previewPlaying?'暂停视频':'播放视频'):'暂无可播放视频'" @click="togglePreviewPlayback"><component :is="previewPlaying?Pause:Play" :size="16" fill="currentColor"/></button><button :disabled="!hasNextVideoShot" title="下一个视频镜头" @click="selectAdjacentVideoShot(1)"><ChevronRight :size="15"/></button><span>{{ formatPreviewTime(previewCurrentTime) }}</span><div class="transport-line" :class="{disabled:!selectedVideoPreview}" role="slider" aria-label="视频播放进度" :aria-valuemin="0" :aria-valuemax="Math.round(previewDuration||0)" :aria-valuenow="Math.round(previewCurrentTime)" @click="seekPreview"><i :style="{width:`${previewProgress}%`}"></i></div><span>{{ formatPreviewTime(previewDuration||selectedShot.duration) }}</span></div>
        </section>

        <section class="editor-panel panel">
          <nav class="inspector-tabs"><button :class="{active:inspectorTab==='visual'}" @click="inspectorTab='visual'"><MonitorPlay :size="15"/>画面与构图</button><button :class="{active:inspectorTab==='continuity'}" @click="inspectorTab='continuity'"><Lock :size="15"/>一致性引用 <span>{{ continuityCount }}</span></button><button :disabled="selectedShot.mediaType==='image'" :class="{active:inspectorTab==='audio'}" @click="inspectorTab='audio'"><AudioLines :size="15"/>原生音频</button></nav>
          <div v-if="inspectorTab==='visual'" class="inspector-content">
            <div v-if="selectedShot.mediaType==='video'" class="workflow-mode-section">
              <header><div><Film :size="15"/><span><strong>MiniMax H3 生产工作流 <PromptImpactBadge kind="production" title="决定提示词模板、参考输入连接和 ComfyUI 工作流。"/></strong><small>不同工作流使用独立的素材校验和官方提示词结构</small></span></div><code>{{ selectedModeProfile.workflow }}</code></header>
              <div class="workflow-mode-grid"><button v-for="mode in modes" :key="mode.value" :class="{active:selectedShot.mode===mode.value}" @click="setShotMode(mode.value)"><span>{{ mode.value }}</span><strong>{{ mode.name }}</strong><small>{{ mode.requirement }}</small><i><Check :size="12"/></i></button></div>
            </div>
            <PromptWritingGuide scope="shot"/>
            <TranslationReviewPanel :project-id="project.id" :episode-id="episode.id" :shot-id="selectedShot.id" :fields="selectedTranslationFields" :repair-legacy-negative-prompt="selectedShot.legacyNegativePromptRepairEligible===true" :blocked-reason="canonicalTargetCollisionError" compact title="当前分镜 · 本地 Qwen 译文人工审核" @promoted="applyShotTranslationPromotion" @promotion-busy="translationPromotionBusy=$event" @changed="invalidatePrompt(selectedShot,'translations_changed')"/>
            <ShotDetailDraftPanel v-if="selectedShot.mediaType==='video'" :project-id="project.id" :shot="selectedShot" :blocked-reason="currentShotPersistenceBlockedReason" @applied="applyShotDetailDraft"/>
            <div class="form-grid four">
              <label class="field"><span class="field-label">镜头标题 <PromptImpactBadge kind="label" title="只用于镜头列表、任务识别和检索，不写入 H3 导演描述。"/></span><input v-model="selectedShot.title" placeholder="例如：雨夜入栈"></label>
              <label class="field"><span class="field-label">生成类型 <PromptImpactBadge kind="production" title="决定生成图片或视频，并切换对应工作流字段。"/></span><select v-model="selectedShot.mediaType"><option value="video">视频 · MiniMax H3</option><option value="image">图片 · 一致性参考帧</option></select></label>
              <label class="field"><span class="field-label">景别 / 构图 <PromptImpactBadge kind="prompt" title="作为 shot size 与 framing 写入导演描述。"/></span><select v-model="selectedShot.framing"><option v-for="item in framings" :key="item">{{ item }}</option></select></label>
              <label v-if="selectedShot.mediaType==='video'" class="field"><span class="field-label">镜头时长 <PromptImpactBadge kind="production" title="控制 H3 duration 与动作密度；有效范围来自生产能力配置。"/></span><div class="number-control"><button :disabled="!productionCapability" @click="adjustSelectedShotDuration(-1)">−</button><input v-model.number="selectedShot.duration" type="number" :min="videoShotMinDurationSeconds" :max="videoShotMaxDurationSeconds" step="0.5" :disabled="!productionCapability"><em>秒</em><button :disabled="!productionCapability" @click="adjustSelectedShotDuration(1)">+</button></div><small v-if="productionCapability" :class="{'field-error':selectedShotDurationOutOfRange}">当前正式视频范围 {{ videoShotMinDurationSeconds }}–{{ videoShotMaxDurationSeconds }} 秒<span v-if="selectedShotDurationOutOfRange">；请先调整到范围内</span></small><small v-else class="field-error">{{ productionCapabilityError || '正在读取正式视频时长配置…' }}</small></label>
            </div>
            <div class="form-grid two">
              <div class="field"><span class="field-label">镜头运动 <PromptImpactBadge kind="prompt" title="进入 Camera 段；建议使用简洁英文并只保留一个主要运镜。"/></span><textarea v-model="selectedShot.camera" rows="3" placeholder="Describe shot size, angle, lens behavior, one camera move, speed, and final framing in English."></textarea><div class="prompt-example-list"><button v-for="(example,index) in cameraExamples" :key="example" type="button" :title="example" @click="selectedShot.camera=example">英文示例 {{ index+1 }}</button></div></div>
              <div class="field"><span class="field-label">动作与画面描述 <PromptImpactBadge kind="prompt" title="作为可观察动作链进入当前镜头；可在每行动作前保留母稿解析出的相对时间范围。"/></span><textarea v-model="selectedShot.action" rows="4" placeholder="[0s–2.5s] The subject opens the door.&#10;[2.5s–5s] The subject stops in a stable final pose."></textarea><div class="prompt-example-list"><button type="button" title="插入后请替换方括号内容" @click="selectedShot.action=actionTemplate">插入英文动作模板</button><small>时间范围相对本镜头起点；未写时母稿拆解会使用 0 至整镜时长。</small></div></div>
            </div>
            <div class="form-grid two">
              <label class="field"><span class="field-label">正向连续性指令 <PromptImpactBadge kind="continuity" title="写入正向 Continuity；仅描述必须保持的身份、服装、空间和状态。"/></span><textarea v-model="selectedShot.continuityDirection" rows="2" placeholder="Preserve the same face, hairstyle, wardrobe, prop hand, screen direction, and end-state continuity."></textarea><small>只写要保持的正向事实，不要在这里填写“不要、避免、漂移”等缺陷词。</small></label>
              <label class="field"><span class="field-label">负面约束 / 质量审计 <PromptImpactBadge kind="audit" title="与正向导演描述分离并保存在提示词产物中；当前本地 H3 原生节点没有独立 negative 输入。"/></span><textarea v-model="selectedShot.negativePrompt" rows="2" :readonly="selectedShot.legacyNegativePromptRepairEligible" placeholder="identity drift, wardrobe change, extra limbs, text, watermark, low resolution"></textarea><small v-if="selectedShot.legacyNegativePromptRepairEligible" class="legacy-negative-warning">该值已由服务端确认与创作草稿连续性字段来源匹配，不能继续保存为负面词；请在上方点击“审计并恢复负面约束”。</small><small v-else>当前用于审计，不会错误塞入正向提示词；不要复制正向连续性描述。</small></label>
            </div>
            <div class="mode-guide"><Sparkles :size="16"/><div><strong>{{ selectedShot.mediaType==='image' ? 'SDXL 一致性定帧提示词' : `${selectedModeProfile.name} · 自动提示词模板` }}</strong><p v-if="selectedShot.mediaType==='video'">{{ selectedModeProfile.desc }}</p><p v-else>当前为 SDXL 标准文生图：使用风格及已选人物、场景、道具的 Bible 英文视觉锚点生成静态构图；素材库参考图仅用于人工连续性核对，不会作为像素输入连接。</p><div v-if="selectedShot.mediaType==='video'" class="template-fields"><code v-for="field in selectedModeProfile.fields" :key="field">{{ field }}</code></div></div></div>
          </div>
          <div v-else-if="inspectorTab==='continuity'" class="inspector-content continuity-selector">
            <div class="selector-group"><header><div><Link2 :size="16"/><span>首尾帧连续要求</span><PromptImpactBadge kind="continuity" title="这是导演与生产意图，不代表素材槽位已经绑定。"/></div><small>母稿默认值可在项目总览的拆解参数中调整</small></header><div class="form-grid two"><div class="setting-toggle"><div><strong>首帧承接上一镜</strong><small>开始状态需匹配上一镜结束状态</small></div><ToggleSwitch v-model="selectedShot.requiresFirstFrameContinuity"/></div><div class="setting-toggle"><div><strong>尾帧衔接下一镜</strong><small>结束时保留稳定、可匹配的状态与构图</small></div><ToggleSwitch v-model="selectedShot.requiresLastFrameContinuity"/></div></div></div>
            <div class="selector-group"><header><div><UserRound :size="16"/><span>本镜头人物</span><PromptImpactBadge kind="continuity" title="人物 Bible 定义参与身份与服装一致性；Ref2VA 可进一步绑定人物图片。"/></div><small>可多选 · 人物素材只绑定人物</small></header><div class="continuity-options"><button v-for="item in project.bible.characters" :key="item.id" :class="{active:selectedShot.characterIds.includes(item.id)}" @click="toggleEntity('character',item.id)"><div><AssetImage v-if="thumbFor(bibleAssetIds(item))" :src="thumbFor(bibleAssetIds(item))" :label="item.name" :accent="style?.accent"/><UserRound v-else :size="20"/></div><span><strong>{{ item.name }}</strong><small>一致性视觉锚点</small></span><i><Check :size="12"/></i></button></div></div>
            <div class="selector-group"><header><div><Image :size="16"/><span>场景锚点</span><PromptImpactBadge kind="continuity" title="场景 Bible 定义参与固定空间结构、光线与色彩连续性。"/></div><small>单选 · 场景素材只绑定场景</small></header><div class="continuity-options"><button v-for="item in project.bible.scenes" :key="item.id" :class="{active:selectedShot.sceneId===item.id}" @click="selectScene(item.id)"><div><AssetImage v-if="thumbFor(bibleAssetIds(item))" :src="thumbFor(bibleAssetIds(item))" :label="item.name" :accent="style?.accent"/><Image v-else :size="20"/></div><span><strong>{{ item.name }}</strong><small>已锁定空间结构</small></span><i><Check :size="12"/></i></button></div></div>
            <div class="selector-group"><header><div><Box :size="16"/><span>关键道具</span><PromptImpactBadge kind="continuity" title="道具 Bible 定义参与形状、材质、比例与持久标记的一致性。"/></div><small>可多选 · 道具素材只绑定道具</small></header><div class="continuity-options"><button v-for="item in project.bible.props" :key="item.id" :class="{active:selectedShot.propIds.includes(item.id)}" @click="toggleEntity('prop',item.id)"><div><AssetImage v-if="thumbFor(bibleAssetIds(item))" :src="thumbFor(bibleAssetIds(item))" :label="item.name" :accent="style?.accent"/><Box v-else :size="20"/></div><span><strong>{{ item.name }}</strong><small>一致性视觉锚点</small></span><i><Check :size="12"/></i></button></div></div>

            <section v-if="selectedShot.mediaType==='image'" class="reference-mode-empty"><Image :size="19"/><div><strong>SDXL 标准文生图输入边界</strong><p>当前图片工作流只读取上方已选实体的 Bible 英文视觉锚点。卡片缩略图帮助人工核对一致性，但不会作为参考图像素连接到模型。</p></div></section>

            <section v-if="selectedShot.mediaType==='video'&&selectedShot.mode==='T2VA'" class="reference-mode-empty"><Film :size="19"/><div><strong>文生视频不读取参考图片</strong><p>该工作流只提交文字、声音和镜头参数；人物服装一致性要求高时，请切换到“首尾帧”或“参考生视频”。</p></div></section>

            <section v-if="selectedShot.mediaType==='video'&&selectedShot.mode==='FL2VA'" class="reference-binding-panel">
              <header><div><Image :size="16"/><span><strong>首尾帧素材绑定 <PromptImpactBadge kind="production" title="实际选中的首帧/尾帧会连接 H3 图像输入，并自动决定 T2VA、I2VA、L2VA 或 FL2VA。"/></strong><small>两个槽位均可不选；从平台素材库选择，不接受本地路径，已选图片不得重复。</small></span></div><code>实际条件模式 · {{ effectiveSelectedMode }}</code></header>
              <div class="frame-binding-grid">
                <article><header><span>首帧（可选） <PromptImpactBadge kind="continuity" title="锁定镜头开始时的主体、服装、场景与构图。"/></span><strong>0.00s</strong></header><button v-if="bindingsFor(selectedShot).firstFrameAssetId" class="clear-binding" @click="selectFrameAsset('first','')">清除首帧</button><div class="binding-asset-options"><button v-for="asset in frameAssets" :key="`first-${asset.id}`" :class="{active:bindingsFor(selectedShot).firstFrameAssetId===asset.id,used:bindingsFor(selectedShot).lastFrameAssetId===asset.id}" @click="selectFrameAsset('first',asset.id)"><AssetImage :src="asset.thumbnail" :label="asset.name" :accent="style?.accent"/><span><strong>{{ asset.name }}</strong><small>{{ asset.type==='character'?'人物':asset.type==='scene'?'场景':asset.type==='prop'?'道具':'参考图' }}</small></span><i><Check :size="11"/></i></button></div></article>
                <article><header><span>尾帧（可选） <PromptImpactBadge kind="continuity" title="锁定镜头结束状态和最终构图；可不传或单独传尾帧。"/></span><strong>{{ Number(selectedShot.duration).toFixed(2) }}s</strong></header><button v-if="bindingsFor(selectedShot).lastFrameAssetId" class="clear-binding" @click="selectFrameAsset('last','')">清除尾帧</button><div class="binding-asset-options"><button v-for="asset in frameAssets" :key="`last-${asset.id}`" :class="{active:bindingsFor(selectedShot).lastFrameAssetId===asset.id,used:bindingsFor(selectedShot).firstFrameAssetId===asset.id}" @click="selectFrameAsset('last',asset.id)"><AssetImage :src="asset.thumbnail" :label="asset.name" :accent="style?.accent"/><span><strong>{{ asset.name }}</strong><small>{{ asset.type==='character'?'人物':asset.type==='scene'?'场景':asset.type==='prop'?'道具':'参考图' }}</small></span><i><Check :size="11"/></i></button></div></article>
              </div>
            </section>

            <section v-if="selectedShot.mediaType==='video'&&selectedShot.mode==='Ref2VA'" class="reference-binding-panel">
              <header><div><Link2 :size="16"/><span><strong>分类参考输入绑定 <PromptImpactBadge kind="production" title="实际选中的图片、视频和音频会按标签与顺序连接 Ref2VA 节点；所有输入均可为空。"/></strong><small>按需要选择 0–9 个实体引用；未绑定实体仍保留文字描述，不占 Picture 槽位。</small></span></div><button class="button ghost small" @click="autoBindFromBible(selectedShot,true)"><RotateCcw :size="12"/>按一致性 Bible 自动绑定</button></header>
              <div class="entity-binding-list">
                <article v-for="entityId in selectedShot.characterIds" :key="`character-${entityId}`"><header><span class="binding-category character">人物</span><strong>{{ bibleItem('character',entityId)?.name }}</strong><button v-if="bindingsFor(selectedShot).characters.some(item=>item.entityId===entityId)" class="clear-entity-binding" @click="selectEntityAsset('character',entityId,'')">清除引用</button></header><div v-if="referenceAssetsForEntity('character',entityId).length" class="binding-asset-options"><button v-for="asset in referenceAssetsForEntity('character',entityId)" :key="asset.id" :class="{active:bindingsFor(selectedShot).characters.find(item=>item.entityId===entityId)?.assetId===asset.id,used:orderedRefBindings(selectedShot).some(item=>item.entityId!==entityId&&item.assetId===asset.id)}" @click="selectEntityAsset('character',entityId,asset.id)"><AssetImage :src="asset.thumbnail" :label="asset.name" :accent="style?.accent"/><span><strong>{{ asset.name }}</strong><small>人物素材</small></span><i><Check :size="11"/></i></button></div><p v-else class="binding-empty">该人物未关联可用图片，将仅使用一致性 Bible 的文字描述。</p></article>
                <article v-if="selectedShot.sceneId"><header><span class="binding-category scene">场景</span><strong>{{ bibleItem('scene',selectedShot.sceneId)?.name }}</strong><button v-if="bindingsFor(selectedShot).scene?.entityId===selectedShot.sceneId" class="clear-entity-binding" @click="selectEntityAsset('scene',selectedShot.sceneId,'')">清除引用</button></header><div v-if="referenceAssetsForEntity('scene',selectedShot.sceneId).length" class="binding-asset-options"><button v-for="asset in referenceAssetsForEntity('scene',selectedShot.sceneId)" :key="asset.id" :class="{active:bindingsFor(selectedShot).scene?.entityId===selectedShot.sceneId&&bindingsFor(selectedShot).scene?.assetId===asset.id,used:orderedRefBindings(selectedShot).some(item=>item.category!=='scene'&&item.assetId===asset.id)}" @click="selectEntityAsset('scene',selectedShot.sceneId,asset.id)"><AssetImage :src="asset.thumbnail" :label="asset.name" :accent="style?.accent"/><span><strong>{{ asset.name }}</strong><small>场景素材</small></span><i><Check :size="11"/></i></button></div><p v-else class="binding-empty">该场景未关联可用图片，将仅使用一致性 Bible 的文字描述。</p></article>
                <article v-for="entityId in selectedShot.propIds" :key="`prop-${entityId}`"><header><span class="binding-category prop">道具</span><strong>{{ bibleItem('prop',entityId)?.name }}</strong><button v-if="bindingsFor(selectedShot).props.some(item=>item.entityId===entityId)" class="clear-entity-binding" @click="selectEntityAsset('prop',entityId,'')">清除引用</button></header><div v-if="referenceAssetsForEntity('prop',entityId).length" class="binding-asset-options"><button v-for="asset in referenceAssetsForEntity('prop',entityId)" :key="asset.id" :class="{active:bindingsFor(selectedShot).props.find(item=>item.entityId===entityId)?.assetId===asset.id,used:orderedRefBindings(selectedShot).some(item=>item.entityId!==entityId&&item.assetId===asset.id)}" @click="selectEntityAsset('prop',entityId,asset.id)"><AssetImage :src="asset.thumbnail" :label="asset.name" :accent="style?.accent"/><span><strong>{{ asset.name }}</strong><small>道具素材</small></span><i><Check :size="11"/></i></button></div><p v-else class="binding-empty">该道具未关联可用图片，将仅使用一致性 Bible 的文字描述。</p></article>
              </div>
              <section class="reference-av-section">
                <header><div><Video :size="15"/><span><strong>参考视频 <PromptImpactBadge kind="continuity" title="为动作、节奏或镜头行为提供视频参考；只有已选择输入才会连接节点。"/></strong><small>{{ bindingsFor(selectedShot).videos.length }} / 3 · 默认不使用视频原音</small></span></div><div><button class="button ghost small" :disabled="uploadingReference==='video'" @click="startVideoUpload"><Upload :size="12"/>{{ uploadingReference==='video'?'上传中…':'上传参考视频' }}</button><input ref="videoUploadInput" type="file" accept="video/mp4,video/webm,video/quicktime" hidden @change="uploadReferenceFile('video',$event)"></div></header>
                <div class="av-library-options"><button v-for="asset in assetsByType.video" :key="asset.id" :class="{active:bindingsFor(selectedShot).videos.some(item=>item.assetId===asset.id)}" @click="toggleReferenceVideo(asset.id)"><Film :size="18"/><span><strong>{{ asset.name }}</strong><small>{{ asset.mimeType||'参考视频' }}</small></span><i><Check :size="11"/></i></button><p v-if="!assetsByType.video.length">素材库暂无参考视频，可点击上方按钮安全上传。</p></div>
                <div class="selected-video-bindings">
                  <article v-for="(item,index) in bindingsFor(selectedShot).videos" :key="item.assetId">
                    <header><span><b>Video {{ index+1 }}</b><strong>{{ assetItem(item.assetId)?.name }}</strong></span><button @click="toggleReferenceVideo(item.assetId)"><Trash2 :size="12"/>清除视频</button></header>
                    <label class="reference-purpose-field"><span>视频用途 <PromptImpactBadge kind="prompt" title="用途会直接改变 Ref2VA 提示词如何解释该 Video 输入，请按真实意图选择。"/></span><select :value="item.purpose" @change="setVideoPurpose(item.assetId,$event)"><option v-for="option in videoPurposeOptions" :key="option.value" :value="option.value" :disabled="videoPurposeOptionDisabled(selectedShot,item.assetId,option.value)">{{ option.label }}</option></select><small>{{ videoPurposeHelp(item.purpose) }} “视频编辑”和“视频续写”不能在同一分镜混用。</small></label>
                    <div class="audio-mode-tabs"><button :class="{active:!item.useEmbeddedAudio&&!item.audioAssetId&&externalAudioPickerVideoId!==item.assetId}" @click="setVideoAudioMode(item.assetId,'none')">无配对音频</button><button :class="{active:item.useEmbeddedAudio}" :disabled="assetItem(item.assetId)?.hasEmbeddedAudio!==true" :title="assetItem(item.assetId)?.hasEmbeddedAudio===true?'已检测到视频内嵌音轨':'未检测到音轨，不能生成 Audio 输入'" @click="setVideoAudioMode(item.assetId,'embedded')">{{ assetItem(item.assetId)?.hasEmbeddedAudio===true?'使用视频原音':'未检测到音轨' }}</button><button :class="{active:Boolean(item.audioAssetId)||externalAudioPickerVideoId===item.assetId}" @click="setVideoAudioMode(item.assetId,'external')">选择上传音频</button></div>
                    <div v-if="externalAudioPickerVideoId===item.assetId||item.audioAssetId" class="paired-audio-options"><button class="upload-paired-audio" :disabled="uploadingReference==='audio'" @click="startAudioUpload(item.assetId)"><Upload :size="13"/>{{ uploadingReference==='audio'&&uploadAudioForVideoId===item.assetId?'上传中…':'上传并配对新音频' }}</button><button v-for="audio in assetsByType.audio" :key="audio.id" :class="{active:item.audioAssetId===audio.id,used:mediaAssetIds(selectedShot).has(audio.id)&&item.audioAssetId!==audio.id}" @click="setPairedAudio(item.assetId,audio.id)"><Volume2 :size="14"/><span>{{ audio.name }}</span><i><Check :size="10"/></i></button><p v-if="!assetsByType.audio.length">尚无可用音频，可直接上传并配对。</p></div>
                    <label v-if="item.useEmbeddedAudio||item.audioAssetId" class="reference-purpose-field audio-purpose"><span>配对音频用途 <PromptImpactBadge kind="prompt" title="选择“仅参考声音”或“复用原音轨”会直接改变 Audio 输入的提示词职责。"/></span><select :value="item.audioPurpose" @change="setVideoAudioPurpose(item.assetId,$event)"><option v-for="option in audioPurposeOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select><small>{{ audioPurposeHelp(item.audioPurpose) }}</small></label>
                  </article>
                </div>
              </section>
              <section class="reference-av-section">
                <header><div><Volume2 :size="15"/><span><strong>独立参考音频 <PromptImpactBadge kind="production" title="已选择音频会连接 Ref2VA 音频输入，影响节奏、说话或声音条件。"/></strong><small>{{ bindingsFor(selectedShot).audios.length }} / 3 · 在所有视频输入之后连接</small></span></div><div><button class="button ghost small" :disabled="uploadingReference==='audio'" @click="startAudioUpload()"><Upload :size="12"/>{{ uploadingReference==='audio'&&!uploadAudioForVideoId?'上传中…':'上传参考音频' }}</button><input ref="audioUploadInput" type="file" accept="audio/wav,audio/mpeg,audio/mp4,audio/flac,audio/ogg" hidden @change="uploadReferenceFile('audio',$event)"></div></header>
                <div class="av-library-options audio"><button v-for="asset in assetsByType.audio" :key="asset.id" :class="{active:bindingsFor(selectedShot).audios.some(item=>item.assetId===asset.id),used:mediaAssetIds(selectedShot).has(asset.id)&&!bindingsFor(selectedShot).audios.some(item=>item.assetId===asset.id)}" @click="toggleIndependentAudio(asset.id)"><Volume2 :size="18"/><span><strong>{{ asset.name }}</strong><small>{{ asset.mimeType||'参考音频' }}</small></span><i><Check :size="11"/></i></button><p v-if="!assetsByType.audio.length">素材库暂无参考音频，可点击上方按钮安全上传。</p></div>
                <div class="selected-audio-bindings"><article v-for="(item,index) in bindingsFor(selectedShot).audios" :key="item.assetId"><header><span><b>Audio {{ bindingsFor(selectedShot).videos.filter(video=>video.useEmbeddedAudio||video.audioAssetId).length+index+1 }}</b><strong>{{ assetItem(item.assetId)?.name }}</strong></span><button @click="toggleIndependentAudio(item.assetId)"><Trash2 :size="12"/>清除音频</button></header><label class="reference-purpose-field audio-purpose"><span>独立音频用途 <PromptImpactBadge kind="prompt" title="用途会直接改变 Ref2VA 提示词如何使用该 Audio 输入。"/></span><select :value="item.purpose" @change="setIndependentAudioPurpose(item.assetId,$event)"><option v-for="option in audioPurposeOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select><small>{{ audioPurposeHelp(item.purpose) }}</small></label></article></div>
              </section>
              <div class="picture-order-preview"><header><strong>最终节点输入顺序</strong><span>{{ orderedRefBindings(selectedShot).length }} Picture · {{ bindingsFor(selectedShot).videos.length }} Video · {{ finalReferenceInputs.filter(item=>item.label.startsWith('Audio')).length }} Audio</span></header><div v-if="finalReferenceInputs.length"><article v-for="input in finalReferenceInputs" :key="`${input.label}-${input.name}`"><b>{{ input.label }}</b><span>{{ input.kind }} · {{ input.name }}</span><small>{{ input.detail }}</small></article></div><p v-else>R2V 零参考 · 仅使用人物、场景、道具的一致性 Bible 文字定义；图片、视频与音频输入都不会创建空连接。</p></div>
              <div class="fixed-loader-note"><Lock :size="13"/><span>模型、CLIP、VAE 与采样器由预置工作流模板固定装配；这里只选择实际参考输入，不会暴露或修改模型路径。</span></div>
            </section>

            <div v-if="referenceValidationErrors.length" class="reference-validation"><AlertTriangle :size="15"/><div><strong>提交前需要修正</strong><p v-for="error in referenceValidationErrors" :key="error">{{ error }}</p></div></div>
          </div>
          <div v-else-if="selectedShot.mediaType==='video'" class="inspector-content audio-editor">
            <div class="audio-master"><div><AudioLines :size="20"/><span><strong>MiniMax H3 原生音频</strong><small>画面与声音在同一次生成中保持时间同步</small></span></div><ToggleSwitch v-model="selectedShot.audioEnabled"/></div>
            <div :class="{disabled:!selectedShot.audioEnabled}" class="audio-fields">
              <label class="field"><span><Mic2 :size="14"/>对白 / 画内语音 / 旁白 <PromptImpactBadge kind="prompt" title="对白原文逐字进入 &lt;d&gt;；可选时间范围控制该句在镜内的发生区间。"/></span><textarea v-model="selectedShot.audio.dialogue" rows="4" placeholder="[1–3.5秒] 沈砚（in a low, restrained voice）：谁在等我？&#10;[3.5–5秒] S1（voiceover; softly）：我又回到了这里。"></textarea><small>每行格式：<b>[可选起止秒] 角色名或 S1（英文演绎）：中文对白</b>。时间相对本镜头起点；未写时间的旧对白按整镜范围处理。需要旁白时加入独立 token <code>voiceover</code>。不要填写 scenetrans / cutoff。</small><code>timing + director delivery + &lt;d&gt;[Chinese]对白原文&lt;/d&gt;</code></label>
              <label class="field"><span><Waves :size="14"/>整体声景 <PromptImpactBadge kind="prompt" title="进入 overall_soundscape，描述环境声、动作拟音、空间与时间同步。"/></span><textarea v-model="selectedShot.audio.soundscape" rows="3" placeholder="Rain strikes the tiled roof; wet footsteps approach from screen right; the sword guard gives one short metallic click."></textarea><small>优先英文描述可听事实与出现时机，不要写配乐。</small><code>overall_soundscape</code></label>
              <label class="field"><span><Music2 :size="14"/>非叙事音乐 <PromptImpactBadge kind="prompt" title="进入 non_diegetic_music；仅描述画外配乐，不与环境声混写。"/></span><textarea v-model="selectedShot.audio.music" rows="3" placeholder="Sparse guqin harmonics at 72 BPM; a slow two-note pulse enters after 2 seconds, swells once, then fades before the cut."></textarea><small>写配器、速度、节奏、动态与进入/退出时机；无配乐请留空，系统会输出 N/A。</small><code>non_diegetic_music</code></label>
            </div>
          </div>
        </section>

        <section class="timeline-panel panel">
          <header><div><span class="eyebrow">EPISODE TIMELINE</span><strong>{{ videoShotCount }} VIDEO · {{ imageShotCount }} IMAGE · {{ totalDuration }} SEC</strong></div><div class="timeline-scale"><span>0s</span><span>{{ Math.round(totalDuration/2) }}s</span><span>{{ totalDuration }}s</span></div></header>
          <div class="timeline-track"><button v-for="shot in episode.shots" :key="shot.id" :class="{active:selectedShotId===shot.id}" :style="{flexGrow:shot.mediaType==='image'?1:shot.duration}" @click="selectedShotId=shot.id"><i :style="{background:style?.accent}"></i><strong>{{ String(shot.order).padStart(2,'0') }}</strong><span>{{ shot.title }}</span><small>{{ shot.mediaType==='image'?'IMG':`${shot.duration}s` }}</small></button></div>
          <div v-if="episode.audioEnabled&&episode.shots.some(shot=>shot.mediaType==='video')" class="audio-track"><AudioLines :size="13"/><div><i v-for="shot in episode.shots.filter(item=>item.mediaType==='video')" :key="shot.id" :style="{flexGrow:shot.duration}"><b v-for="n in 16" :key="n" :style="{height:`${20+((n*shot.order*13)%70)}%`}"></b></i></div></div>
        </section>
      </main>

      <aside v-if="selectedShot" class="prompt-panel">
        <header><div><span class="eyebrow">{{ selectedShot.mediaType==='image'?'CONSISTENCY IMAGE PROMPT':'MINIMAX H3 VIDEO PROMPT' }}</span><h3>提示词预览</h3></div><button v-if="generatedPrompt" class="icon-btn" @click="copyPrompt"><Copy :size="15"/></button></header>
        <div class="h3-mode-card"><div><component :is="selectedShot.mediaType==='image'?Image:Sparkles" :size="17"/><span><strong>{{ selectedShot.mediaType==='image'?'SDXL 一致性定帧':selectedModeProfile.name }}</strong><small>{{ selectedShot.mediaType==='image'?'Standard text-to-image workflow':selectedModeProfile.workflow }}</small></span></div><span>{{ selectedShot.mediaType==='image'?'IMAGE':effectiveSelectedMode }}</span></div>
        <div v-if="!generatedPrompt" class="prompt-empty"><div><WandSparkles :size="27"/></div><h4>{{ selectedPromptRefreshReason?'重新生成并复检':'生成' }}{{ selectedShot.mediaType==='image'?'图片':selectedModeProfile.name }}专用提示词</h4><p>{{ selectedShot.mediaType==='image'?'整合风格、静态构图，以及已选人物、场景和道具的 Bible 英文视觉锚点；当前 SDXL 标准文生图不会连接参考图像素。':`${selectedModeProfile.desc} 模板字段：${selectedModeProfile.fields.join('、')}。` }}</p><div v-if="selectedPromptRefreshMessage" class="prompt-blocked-note"><RotateCcw :size="13"/>{{ selectedPromptRefreshMessage }}</div><div v-if="referenceValidationErrors.length" class="prompt-blocked-note"><AlertTriangle :size="13"/>请先在“一致性引用”中修正 {{ referenceValidationErrors.length }} 项素材绑定</div><button class="button primary full" :disabled="generating||referenceValidationErrors.length>0||currentShotPersistenceBlocked" :title="currentShotPersistenceBlockedReason" @click="generate">{{ generating?'正在编排…':selectedPromptRefreshReason?`一键重新生成当前${selectedShot.mediaType==='image'?'图片':selectedModeProfile.name}提示词`:`生成当前${selectedShot.mediaType==='image'?'图片':selectedModeProfile.name}提示词` }}</button></div>
        <template v-else>
          <div class="prompt-summary"><span>TYPE <strong>{{ generatedPrompt.mediaType.toUpperCase() }}</strong></span><span>MODEL <strong>{{ generatedPrompt.model }}</strong></span><span v-if="generatedPrompt.mode">WORKFLOW <strong>{{ generatedPrompt.mode }}</strong></span><span v-if="generatedPrompt.effective_conditioning_mode">CONDITION <strong>{{ generatedPrompt.effective_conditioning_mode }}</strong></span><span v-if="generatedPrompt.prompt_template_key">TEMPLATE <strong>{{ generatedPrompt.prompt_template_key }}</strong></span><span v-if="generatedPrompt.duration">DURATION <strong>{{ generatedPrompt.duration }}s</strong></span></div>
          <section v-if="promptQuality" class="prompt-quality-panel" :class="{attention:promptQuality.status==='needs_attention'}">
            <header><div><AlertTriangle v-if="promptQuality.status==='needs_attention'" :size="15"/><Check v-else :size="15"/><span><small>PROMPT QUALITY</small><strong>{{ selectedPromptProductionBlocked?'生产已阻断：请先完成英文改写':promptQuality.status==='needs_attention'?'质量警告：建议先修正':'提示词质量检查通过' }}</strong></span></div><PromptImpactBadge kind="audit" :title="selectedPromptProductionBlocked?`后端生产语言门禁为 block；该${selectedShot.mediaType==='image'?'图片':'视频'}不会进入 ComfyUI 队列，直至列出的导演字段完成英文改写并重新复检。`:'来自后端提示词结构、语言、长度和重复风险审计；图片和视频导演字段均需英文，只有中文对白、歌词与真实屏显文字可保留原语言。'"/></header>
            <p v-if="selectedPromptProductionBlocked" class="quality-audit-note production-blocked-note">预览仍可查看，但生产提交会被后端拒绝。请将下方高影响导演字段改为英文，再点击“重新生成并复检”；中文对白、歌词和真实屏显文字保持原语言，不要翻译。</p>
            <p v-else-if="promptQuality.status==='needs_attention'" class="quality-audit-note">实际 prompt_text 仍有质量警告；当前语言门禁未阻断生产。系统不会在未配置文本优化模型时伪造英文翻译，请按下方建议修正后重新生成，对白原文保持中文。</p>
            <p v-else class="quality-audit-note">已核对实际 prompt_text：导演描述为英文，中文只保留在对白、歌词或真实屏显文字中。</p>
            <div class="prompt-quality-metrics"><span>字符数 <strong>{{ promptQuality.prompt_character_count }}<template v-if="promptQuality.prompt_character_limit!==undefined"> / {{ promptQuality.prompt_character_limit }}</template></strong></span><span v-if="promptQuality.prompt_word_count!==undefined">英文词数 <strong>{{ promptQuality.prompt_word_count_is_approximate?'约 ':'' }}{{ promptQuality.prompt_word_count }}</strong></span><span>语言策略 <strong>{{ promptQuality.language_policy }}</strong></span><span v-if="promptQuality.dialogue_language">对白语言 <strong>{{ promptQuality.dialogue_language }}</strong></span><span v-if="promptQuality.production_language_gate">生产门禁 <strong>{{ promptQuality.production_language_gate.mode.toUpperCase() }} · {{ promptQuality.production_language_gate.would_block_enqueue?'会阻断':'可提交' }}</strong></span><span v-if="promptQuality.detailed_description_word_count!==undefined">导演描述 <strong>{{ promptQuality.detailed_description_word_count }} words</strong></span></div>
            <div v-if="promptQuality.optimizer" class="quality-detail optimizer"><strong>英文语义优化器</strong><div><code>{{ promptQuality.optimizer.status }}</code><code>{{ promptQuality.optimizer.transport==='remote'?'显式授权的远程服务':'本地 / 未联网' }}</code><code v-if="promptQuality.optimizer.model">{{ promptQuality.optimizer.model }}</code></div><p>{{ promptQuality.optimizer.message }}</p><ul v-if="promptQuality.optimizer.validation_errors?.length"><li v-for="error in promptQuality.optimizer.validation_errors" :key="error">{{ error }}</li></ul></div>
            <div v-if="promptQuality.source_fields_optimized?.length" class="quality-detail"><strong>本次已安全改写为英文</strong><div><code v-for="field in promptQuality.source_fields_optimized" :key="field">{{ field }}</code></div></div>
            <div v-if="promptQuality.fields_requiring_english_rewrite?.length" class="quality-detail"><strong>需要英文改写的字段</strong><div><code v-for="field in promptQuality.fields_requiring_english_rewrite" :key="field">{{ field }}</code></div></div>
            <div v-if="promptQuality.warnings?.length" class="quality-detail warnings"><strong>质量警告</strong><ul><li v-for="warning in promptQuality.warnings" :key="warning">{{ warning }}</li></ul></div>
          </section>
          <div class="json-preview actual-prompt"><strong>实际提交给 {{ generatedPrompt.mediaType==='image'?'图片工作流':'MiniMax H3' }} 的 prompt_text</strong><pre>{{ actualPromptText }}</pre></div>
          <details class="json-preview structured-preview"><summary>查看结构化字段 JSON</summary><pre>{{ promptJson }}</pre></details>
          <div class="prompt-actions"><button class="button ghost full" @click="copyPrompt"><Clipboard :size="15"/>复制实际提示词</button><button class="button ghost full" @click="copyPromptJson"><Clipboard :size="15"/>复制结构 JSON</button><button class="button primary full" :disabled="generating||currentShotPersistenceBlocked" :title="currentShotPersistenceBlockedReason" @click="generate"><RotateCcw :size="15"/>重新生成并复检</button></div>
        </template>
        <div class="shot-render-action">
          <div><strong>生成此镜头的新{{ selectedShot.mediaType==='image'?'图片':'视频' }}</strong><p>{{ selectedShot.mediaType==='video'?'先保存当前编辑，再只为这个分镜创建新的 ComfyUI 视频任务；成功后可回到连续剧工作室重新组装本集。':'先保存当前编辑，再创建独立 SDXL 图片任务；完成后会在上方舞台和任务详情中预览、下载。当前只使用 Bible 英文视觉锚点，不连接参考图像素。' }}</p></div>
          <div class="resolution-impact-header"><span>输出尺寸与倍数</span><PromptImpactBadge kind="production" title="分辨率档位与尺寸倍数直接控制 ComfyUI 宽高和显存负载，不写入生成提示词。"/></div>
          <ResolutionProfilePicker v-model="shotResolutionProfile" v-model:multiple="shotResolutionMultiple" :multiple-step="selectedShot.mediaType==='image'?8:4" :label="`${selectedShot.mediaType==='image'?'图片':'视频'}生成分辨率档位`" compact/>
          <ProductionWorkflowPicker v-if="selectedShot.mediaType==='video'&&selectedShot.mode==='Ref2VA'" v-model:template-id="shotRef2vaTemplateId" v-model:parameters="shotRef2vaParameters" :templates="productionWorkflowTemplates" :loading="productionWorkflowTemplatesLoading" :error="productionWorkflowTemplatesError" h3-mode="Ref2VA"/>
          <div v-if="selectedShot.mediaType==='image'" class="image-resolution-guidance"><Image :size="13"/><span><strong>SDXL 推荐约 1 MP</strong>默认使用 0.98 MP；0.1 / 0.2 MP 仅适合验证工作流连通性，成片细节和人物质量通常不足。</span></div>
          <div v-if="selectedPromptProductionBlocked" class="prompt-blocked-note"><AlertTriangle :size="13"/>该提示词的导演字段仍含待改写中文；生产门禁会拒绝排队。完成英文改写后请先“重新生成并复检”。</div>
          <button class="button primary full" :disabled="regeneratingShot||saving||generating||currentShotPersistenceBlocked||referenceValidationErrors.length>0||selectedPromptProductionBlocked" :title="currentShotPersistenceBlockedReason" @click="saveAndRegenerateShot(false)"><component :is="selectedShot.mediaType==='image'?Image:Film" :size="15"/>{{ regeneratingShot?'保存并提交中…':`保存并重新生成${selectedShot.mediaType==='image'?'图片':'视频'}` }}</button>
          <div v-if="lastSubmittedTask?.shotId===selectedShot.id" class="shot-task-link"><span>任务 {{ lastSubmittedTask.taskId.slice(0,8) }} 已进入生产队列</span><RouterLink to="/workflows">查看进度 <ChevronRight :size="12"/></RouterLink></div>
        </div>
        <div class="continuity-health"><header><span><Lock :size="14"/>连续性检查</span><strong :class="{invalid:referenceValidationErrors.length}">{{ referenceValidationErrors.length ? '素材待修正' : '通过' }}</strong></header><div><span>工作流素材绑定</span><i :class="{ok:!referenceValidationErrors.length}"></i></div><div><span>场景锚点</span><i :class="{ok:selectedShot.sceneId}"></i></div><div><span>全局色彩脚本</span><i :class="{ok:project.bible.colorScript}"></i></div><div><span>负面提示词</span><i :class="{ok:selectedShot.negativePrompt}"></i></div></div>
        <button class="delete-shot" @click="deleteShot(selectedShot.id)"><Trash2 :size="14"/>删除当前镜头</button>
      </aside>
    </div>
    <EpisodeDetailDraftModal
      :open="episodeDetailDraftOpen"
      :project-id="project.id"
      :episode="episode"
      :shots="episode.shots"
      :shot-version-key="selectedShotVersionScopeKey"
      :shot-version-label="selectedShotVersionScopeLabel"
      :initial-mode="episodeDetailDraftMode"
      :blocked-reason="editorPersistenceBlocked ? editorPersistenceBlockedReason : ''"
      :persist-episode="persistEpisodeKeepingSelection"
      @close="episodeDetailDraftOpen=false"
      @applied="applyEpisodeDetailDraft"
    />
    <BaseModal :open="Boolean(pendingShotRegenerationConfirmation)" title="确认生成此镜头" eyebrow="QUEUE GUIDANCE" width="540px" @close="closeShotRegenerationConfirmation">
      <div v-if="pendingShotRegenerationConfirmation" class="shot-regeneration-confirm">
        <AlertTriangle :size="22"/><span><strong>镜头 {{ String(pendingShotRegenerationConfirmation.shotOrder).padStart(2,'0') }} · {{ pendingShotRegenerationConfirmation.shotTitle }}</strong><small>当前生产队列已超过服务端配置的建议值。继续会按原工作流完整创建 1 个{{ pendingShotRegenerationConfirmation.mediaType==='image'?'图片':'视频' }}任务，只会增加等待时间和资源占用，不会改变镜头内容。</small><p>当前编辑已保存；选择取消不会创建新的生产任务。稍后仍可重新点击生成。</p></span>
      </div>
      <template #footer><button class="button ghost" :disabled="regeneratingShot" @click="closeShotRegenerationConfirmation">取消，不提交</button><button class="button primary" :disabled="regeneratingShot" @click="confirmShotRegeneration"><component :is="pendingShotRegenerationConfirmation?.mediaType==='image'?Image:Film" :size="15"/>{{ regeneratingShot?'正在确认并提交…':'确认并生成此镜头' }}</button></template>
    </BaseModal>
    <BaseModal :open="wholeEpisodeOpen" title="生成整集" eyebrow="WHOLE EPISODE BATCH" width="560px" @close="!generatingWholeEpisode&&(wholeEpisodeOpen=false)">
      <ProductionWorkflowPicker v-if="episodeHasRef2VA" v-model:template-id="wholeRef2vaTemplateId" v-model:parameters="wholeRef2vaParameters" :templates="productionWorkflowTemplates" :loading="productionWorkflowTemplatesLoading" :error="productionWorkflowTemplatesError" :disabled="Boolean(wholeEpisodeIdempotencyKey)" h3-mode="Ref2VA"/>
      <div class="whole-episode-confirm"><div><Sparkles :size="22"/><span><strong>第 {{ episode.number }} 集 · {{ episode.title }}</strong><small>将逐镜创建 {{ episode.shots.length }} 个独立任务：{{ videoShotCount }} 个 H3 视频、{{ imageShotCount }} 个 SDXL 图片；失败分镜可单独重试。</small></span></div><div v-if="wholeEpisodeExtendedConfirmed"><AlertTriangle :size="18"/><span><strong>请再次确认长任务</strong><small>本集 {{ episode.shots.length }} 镜会全部保留，并按分镜独立入队；超过队列建议值只会增加等待时间、模型执行时间与 API 用量，不会截断镜数。</small></span></div><div v-if="wholeEpisodeIdempotencyConflict" class="whole-episode-idempotency-conflict"><AlertTriangle :size="16"/><span><strong>已停止自动重提</strong><small>{{ wholeEpisodeIdempotencyConflict }}</small></span></div><div v-if="blockedEpisodePromptShots.length" class="prompt-blocked-note"><AlertTriangle :size="13"/>{{ blockedEpisodePromptShots.length }} 个已复检的图片/视频提示词仍需英文改写，后端生产门禁会拒绝整集排队。请返回对应镜头修正并重新复检；中文对白、歌词和真实屏显文字可保留原语言。</div><div v-if="videoShotCount" class="resolution-impact-header"><span>视频输出尺寸与倍数</span><PromptImpactBadge kind="production" title="该档位和尺寸倍数只应用到本集视频分镜。"/></div><ResolutionProfilePicker v-if="videoShotCount" v-model="wholeEpisodeProfile" v-model:multiple="wholeEpisodeMultiple" :disabled="Boolean(wholeEpisodeIdempotencyKey)"/><details v-if="imageShotCount" class="whole-episode-image-resolution" open><summary>图片输出参数 · 推荐 0.98 MP</summary><ResolutionProfilePicker v-model="wholeEpisodeImageProfile" v-model:multiple="wholeEpisodeImageMultiple" :multiple-step="8" label="SDXL 图片分辨率档位" :disabled="Boolean(wholeEpisodeIdempotencyKey)"/><p>SDXL 推荐至少 0.8 MP，默认 0.98 MP；图片宽高按 8 的倍数取整；0.1–0.3 MP 仅用于连通性测试，不建议作为交付图片。</p></details><small v-if="wholeEpisodeIdempotencyKey">上次响应未确认，重试将保持视频/图片各自档位、尺寸倍数与幂等键。</small><button v-if="wholeEpisodeIdempotencyKey" type="button" class="button ghost small" @click="abandonWholeEpisodeRetryState">核对任务后放弃旧尝试</button><p>网络错误后再次点击会复用同一个幂等键，避免重复创建整集任务；成功返回后才会生成新的提交键。图片产物不会进入本集视频组装。</p></div>
      <template #footer><button class="button ghost" :disabled="generatingWholeEpisode" @click="wholeEpisodeOpen=false">取消</button><button class="button primary" :disabled="generatingWholeEpisode||Boolean(wholeEpisodeIdempotencyConflict)||editorPersistenceBlocked||blockedEpisodePromptShots.length>0" :title="wholeEpisodeIdempotencyConflict||editorPersistenceBlockedReason" @click="submitWholeEpisode"><Sparkles :size="15"/>{{ generatingWholeEpisode?'正在创建批次…':wholeEpisodeExtendedConfirmed?`确认并生成 ${episode.shots.length} 个分镜任务`:`生成 ${episode.shots.length} 个分镜任务` }}</button></template>
    </BaseModal>
  </div>
</template>

<style scoped>
.storyboard-actions{align-items:center}.episode-tool-group{display:flex;align-items:center;gap:7px;padding-left:7px;border-left:1px solid var(--border)}.episode-tool-group .button{white-space:nowrap}@media(max-width:720px){.storyboard-actions{width:100%;overflow-x:auto;padding-bottom:3px;justify-self:stretch;flex-wrap:nowrap}.storyboard-actions>.button{flex:none}.episode-tool-group{min-width:max-content;flex-wrap:nowrap}}
.canonical-collision-banner{margin:10px 14px 0;padding:10px 12px;display:flex;align-items:flex-start;gap:8px;border:1px solid rgba(229,113,117,.3);border-radius:7px;background:rgba(229,113,117,.065);color:#e18b91}.canonical-collision-banner>span{display:flex;min-width:0;flex-direction:column;gap:3px}.canonical-collision-banner strong{font-size:11px}.canonical-collision-banner small{font-size:10px;line-height:1.55}
.shot-version-bar{margin:10px 14px 0;padding:10px 12px;display:flex;align-items:center;justify-content:space-between;gap:12px;border:1px solid rgba(140,111,230,.24);border-radius:8px;background:rgba(140,111,230,.045)}.shot-version-bar>div{min-width:0;display:flex;align-items:flex-start;gap:8px;color:#bdaeff}.shot-version-bar span{min-width:0;display:flex;flex-direction:column;gap:3px}.shot-version-bar strong{font-size:11px;color:#d8d0ff}.shot-version-bar small{font-size:10px;line-height:1.55;color:#85809f}.shot-version-bar select{min-width:220px;height:34px;padding:0 30px 0 10px;border:1px solid rgba(140,111,230,.28);border-radius:6px;background:#0b0e14;color:#d7d1ff;font-size:11px}@media(max-width:720px){.shot-version-bar{align-items:stretch;flex-direction:column}.shot-version-bar select{width:100%;min-width:0}}
.field-label{display:flex;align-items:center;justify-content:space-between;gap:7px;flex-wrap:wrap}.field>small{font-size:11px;line-height:1.55;color:#6f757d}.field>small.legacy-negative-warning{color:#d9a45f}.prompt-example-list{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.prompt-example-list button{min-height:30px;padding:5px 9px;border:1px solid rgba(230,167,90,.18);border-radius:4px;background:rgba(230,167,90,.035);color:#aa8358;font-size:10px;cursor:pointer}.prompt-example-list button:hover{border-color:rgba(230,167,90,.4);color:var(--accent)}.prompt-example-list small{font-size:10px;color:#676d75}.workflow-mode-section strong,.reference-binding-panel>header strong,.frame-binding-grid article>header>span{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.selector-group>header>div{flex-wrap:wrap}.audio-fields .field>span{flex-wrap:wrap}.resolution-impact-header{display:flex;align-items:center;justify-content:space-between;gap:8px}.resolution-impact-header>span{font-size:11px;font-weight:600;color:#a9adb3}.prompt-quality-panel{padding:12px;display:flex;flex-direction:column;gap:10px;border:1px solid rgba(104,197,167,.2);border-radius:7px;background:rgba(104,197,167,.035)}.prompt-quality-panel.attention{border-color:rgba(229,113,117,.28);background:rgba(229,113,117,.035)}.prompt-quality-panel>header{display:flex;align-items:center;justify-content:space-between;gap:8px}.prompt-quality-panel>header>div{min-width:0;display:flex;align-items:center;gap:8px;color:var(--green)}.prompt-quality-panel.attention>header>div{color:#e57175}.prompt-quality-panel>header span{min-width:0;display:flex;flex-direction:column;gap:3px}.prompt-quality-panel>header small{font:10px Consolas,monospace;letter-spacing:.1em;color:#767e7d}.prompt-quality-panel>header strong{font-size:12px;color:#c8cdce}.quality-audit-note{margin:0;color:#777f86;font-size:10px;line-height:1.55}.prompt-quality-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.prompt-quality-metrics>span{min-width:0;padding:8px;border:1px solid rgba(255,255,255,.055);border-radius:4px;background:#0a0d11;color:#7b8289;font-size:10px;line-height:1.5}.prompt-quality-metrics strong{display:block;margin-top:3px;color:#bbc0c5;font-size:11px;word-break:break-word}.quality-detail{display:flex;flex-direction:column;gap:7px}.quality-detail>strong{font-size:11px;color:#aeb4ba}.quality-detail>div{display:flex;gap:5px;flex-wrap:wrap}.quality-detail code{padding:5px 7px;border:1px solid rgba(230,167,90,.18);border-radius:4px;background:#0a0d11;color:#cf995b;font-size:10px;word-break:break-word}.quality-detail ul{max-height:160px;margin:0;padding:9px 9px 9px 24px;overflow:auto;border-radius:4px;background:#0a0d11}.quality-detail li{margin:0 0 6px;color:#bf888b;font-size:11px;line-height:1.55}.quality-detail li:last-child{margin-bottom:0}
.image-resolution-guidance{padding:8px;display:flex;align-items:flex-start;gap:7px;border:1px solid rgba(104,197,167,.18);border-radius:6px;background:rgba(104,197,167,.035);color:var(--green)}.image-resolution-guidance span{font-size:8px;line-height:1.55;color:#788c85}.image-resolution-guidance strong{display:block;color:#9bc5b7;font-size:9px}
.shot-regeneration-confirm{display:flex;align-items:flex-start;gap:11px;padding:13px;border:1px solid rgba(218,158,73,.25);border-radius:8px;background:rgba(218,158,73,.055);color:#d2a15c}.shot-regeneration-confirm>span{display:grid;gap:5px}.shot-regeneration-confirm strong{font-size:11px;color:#d8b276}.shot-regeneration-confirm small,.shot-regeneration-confirm p{margin:0;color:#858d94;font-size:9px;line-height:1.6}
.whole-episode-idempotency-conflict{display:flex;align-items:flex-start;gap:8px;padding:10px;border:1px solid rgba(223,101,90,.28);border-radius:7px;background:rgba(223,101,90,.055);color:#df8177}.whole-episode-idempotency-conflict>span{display:grid;gap:5px}.whole-episode-idempotency-conflict small{color:#a68585;font-size:9px;line-height:1.55}.whole-episode-idempotency-conflict button{justify-self:start}
.whole-episode-image-resolution{padding:9px;border:1px solid rgba(104,197,167,.18);border-radius:7px;background:rgba(104,197,167,.025)}.whole-episode-image-resolution summary{color:#9bc5b7;font-size:10px;font-weight:650;cursor:pointer}.whole-episode-image-resolution>.resolution-picker{margin-top:9px}.whole-episode-image-resolution>p{margin:7px 0 0!important;color:#74857f!important;font-size:8px!important;line-height:1.55!important}
.reference-purpose-field{margin-top:8px;padding:8px;display:grid;grid-template-columns:minmax(120px,.7fr) minmax(170px,1fr);align-items:center;gap:6px 10px;border:1px solid rgba(230,167,90,.14);border-radius:5px;background:rgba(230,167,90,.025)}.reference-purpose-field>span{display:flex;align-items:center;gap:5px;color:#b8bdc3;font-size:10px;font-weight:650}.reference-purpose-field select{width:100%;height:32px;padding:0 28px 0 9px;border:1px solid rgba(230,167,90,.23);border-radius:4px;background:#0a0d11;color:#d2b083;font-size:10px}.reference-purpose-field small{grid-column:1/-1;color:#777f87;font-size:9px;line-height:1.55}.reference-purpose-field.audio-purpose{border-color:rgba(104,197,167,.14);background:rgba(104,197,167,.02)}.reference-purpose-field.audio-purpose select{border-color:rgba(104,197,167,.22);color:#88bca9}.selected-audio-bindings{display:flex;flex-direction:column;gap:7px}.selected-audio-bindings>article{padding:9px;border:1px solid rgba(104,197,167,.14);border-radius:6px;background:rgba(104,197,167,.02)}.selected-audio-bindings>article>header{display:flex;align-items:center;justify-content:space-between;gap:8px}.selected-audio-bindings>article>header span{min-width:0;display:flex;align-items:center;gap:7px}.selected-audio-bindings>article>header b{font:8px Consolas,monospace;color:var(--green)}.selected-audio-bindings>article>header strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px;color:#aeb3b9}.selected-audio-bindings>article>header button{display:flex;align-items:center;gap:4px;background:transparent;color:#766568;font-size:9px;cursor:pointer}@media(max-width:720px){.reference-purpose-field{grid-template-columns:1fr}.reference-purpose-field small{grid-column:1}}
</style>
