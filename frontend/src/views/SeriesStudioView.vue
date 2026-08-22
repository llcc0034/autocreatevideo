<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AlertTriangle, ArrowLeft, ArrowRight, BookOpen, Box, Check, ChevronRight, Clapperboard, Clock3, Copy, Download, Eye, FileJson, Film, FolderInput, Image, ImagePlus, Languages, Layers3, Lock, MoreHorizontal, Plus, RefreshCw, Save, Search, Sparkles, Trash2, Unlock, Upload, UserRound, WandSparkles, Workflow, X } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import ToggleSwitch from '@/components/ToggleSwitch.vue'
import AssetImage from '@/components/AssetImage.vue'
import ProjectCoverImage from '@/components/ProjectCoverImage.vue'
import ResolutionProfilePicker from '@/components/ResolutionProfilePicker.vue'
import ProductionWorkflowPicker from '@/components/ProductionWorkflowPicker.vue'
import PromptImpactBadge from '@/components/PromptImpactBadge.vue'
import PromptWritingGuide from '@/components/PromptWritingGuide.vue'
import TranslationReviewPanel from '@/components/TranslationReviewPanel.vue'
import CreativeDraftWorkbench from '@/components/CreativeDraftWorkbench.vue'
import GlobalVisualRulesDraft from '@/components/GlobalVisualRulesDraft.vue'
import SeriesConceptDraftWorkbench from '@/components/SeriesConceptDraftWorkbench.vue'
import ScriptBibleCandidatesPanel from '@/components/ScriptBibleCandidatesPanel.vue'
import { productionErrorCode, repository, type ProductionCapability } from '@/services/repository'
import { PROJECT_COVER_ACCEPT, projectCoverFileError } from '@/services/projectManagement'
import { seriesConceptAcceptedFields } from '@/services/seriesConceptDraft'
import { masterScriptNeedsScriptBibleRefresh, mergeScriptBibleApplyIntoBible, scriptBiblePendingCountFromError } from '@/services/scriptBibleCandidates'
import { bibleImageCandidateErrorCode, bibleImageCandidateErrorMessage, bibleImageCandidateRetryStorageKey, createBibleImageCandidateRetryState, mergeBibleImagePromotion, parseBibleImageCandidateRetryState, type BibleImageCandidateRetryState } from '@/services/bibleImageCandidates'
import { bibleAssetIds } from '@/services/bibleAssets'
import { wholeEpisodeRetryStorageKey, wholeEpisodeRetryStoragePrefix } from '@/services/shotVersionScope'
import { bibleCanonicalTargetCollisionMessage, bibleCanonicalTargetCollisions, formatTranslationGlossary, missingBibleCanonicalNames as findMissingBibleCanonicalNames, parseTranslationGlossary } from '@/services/translationReview'
import { DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, DEFAULT_PRODUCTION_RESOLUTION_PROFILE, isProductionResolutionProfile, normalizeImageProductionResolutionMultiple, normalizeProductionResolutionMultiple } from '@/constants/productionResolution'
import { useStudioStore } from '@/stores/studio'
import { useAuthStore } from '@/stores/auth'
import type { AssemblyResult, Asset, BibleAssetDraftResult, BibleDraftLanguage, BibleImageCandidateProjection, BibleImageCandidatesResult, BibleImageEntityType, BibleImageLatestCandidate, BibleImagePromptInput, BibleImagePromptInputs, BibleItem, Episode, H3Mode, ProductionResolutionProfile, ProductionWorkflowSelection, ProductionWorkflowTemplate, PromptTranslationField, ReleaseBundle, ScriptBibleApplyResult, ScriptBreakdownOptions, SeriesConceptCandidate, SeriesProject, TaskOutputMedia, VisualRulesDraftApplyResult } from '@/types'

const studio = useStudioStore(); const auth = useAuthStore(); const route = useRoute(); const router = useRouter()
const project = ref<SeriesProject | null>(null); const loading = ref(true); const projectLoadError = ref(''); const saving = ref(false); const tab = ref<'overview'|'bible'|'episodes'|'release'>('overview'); const bibleSection = ref<'characters'|'scenes'|'props'>('characters')
const addBibleOpen = ref(false); const addEpisodeOpen = ref(false); const createLoading = ref(false); const breakdownLoading = ref(false)
const rebuildConfirmOpen = ref(false)
const scriptBiblePanel = ref<{ focus: () => Promise<void>; refresh: () => Promise<{ pendingCount: number; hasBlock: boolean } | null> } | null>(null)
const savedMasterScript = ref('')
const scriptBiblePendingCount = ref(0)
const scriptBibleHasBlock = ref(false)
const scriptBibleApplying = ref(false)
const scriptBibleGateOpen = ref(false)
const scriptBibleSkipConfirmOpen = ref(false)
const scriptBibleGateRequiresRebuild = ref(false)
const scriptBibleGateRebuildConfirmed = ref(false)
const rebuildContinueWithoutScriptBible = ref(false)
const bibleAssetPickerId = ref('')
const bibleImageCandidates = ref<BibleImageCandidatesResult | null>(null)
const bibleImageLoading = ref(false)
const bibleImageLoadError = ref('')
const bibleImageGeneratingKey = ref('')
const bibleImageBatchGenerating = ref(false)
const bibleImageBatchProgress = ref('')
const bibleImagePromotingKey = ref('')
const bibleImageActionErrors = ref<Record<string, string>>({})
const bibleImageDirections = ref<Record<string, string>>({})
const bibleImagePromptOverrides = ref<Record<string, Record<string, string>>>({})
const bibleImagePreview = ref<{ src: string; title: string; filename: string; downloadUrl: string } | null>(null)
const bibleImageQueueConfirmOpen = ref(false)
const bibleImagePendingConfirmation = ref<BibleImageCandidateRetryState | null>(null)
const episodeAssemblies = ref<Record<string, AssemblyResult>>({}); const completedEpisodeAssemblies = ref<Record<string, AssemblyResult>>({}); const selectedEpisodeId = ref(''); const assemblyLoadingId = ref(''); const assemblyError = ref(''); const assemblyPreviewRefreshingId = ref(''); const assemblyPreviewErrors = ref<Record<string,string>>({}); const episodeGenerationLoadingId = ref(''); const episodeIdempotencyKeys = ref<Record<string,string>>({}); const wholeEpisodeOpen = ref(false); const wholeEpisodeExtendedConfirmed = ref(false); const wholeEpisodeIdempotencyConflict = ref(''); const wholeEpisodeId = ref(''); const wholeEpisodeProfile = ref<ProductionResolutionProfile>(DEFAULT_PRODUCTION_RESOLUTION_PROFILE); const wholeEpisodeMultiple = ref(DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE); const wholeEpisodeImageProfile = ref<ProductionResolutionProfile>('0.98'); const wholeEpisodeImageMultiple = ref(DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE)
const productionWorkflowTemplates = ref<ProductionWorkflowTemplate[]>([]); const productionWorkflowTemplatesLoading = ref(false); const productionWorkflowTemplatesError = ref(''); const wholeRef2vaTemplateId = ref(''); const wholeRef2vaParameters = ref<Record<string,number>>({})
const productionCapability = ref<ProductionCapability | null>(null); const productionCapabilityError = ref('')
const releaseBundles = ref<ReleaseBundle[]>([]); const releaseLoading = ref(false); const releaseError = ref(''); const releaseTitle = ref(''); const releaseNotes = ref('')
const newProject = ref({ title: '', logline: '', masterScript: '', styleId: 'wuxia', aspectRatio: '9:16' as SeriesProject['aspectRatio'], resolution: '720p' as SeriesProject['resolution'], defaultMode: 'Ref2VA' as H3Mode })
const coverInput = ref<HTMLInputElement | null>(null)
const pendingCoverFile = ref<File | null>(null)
const pendingCoverPreview = ref('')
const coverSaving = ref(false)
const coverError = ref('')
const breakdownOptions = ref<ScriptBreakdownOptions>({ episodeCountHint: 1, defaultShotDuration: 0, multiShot: true, includeAudio: true, defaultFirstFrameContinuity: false, defaultLastFrameContinuity: false, replaceExisting: false, discardExistingProduction: false })
const videoShotMinDurationSeconds = computed(() => productionCapability.value ? productionCapability.value.videoShotMinDurationMs / 1000 : undefined)
const videoShotMaxDurationSeconds = computed(() => productionCapability.value ? productionCapability.value.videoShotMaxDurationMs / 1000 : undefined)
const translationGlossaryText = ref('')
const translationGlossaryValidation = computed(() => parseTranslationGlossary(translationGlossaryText.value))
const canonicalBackfillLoading = ref(false)
const bibleCreatePhase = ref<'idle'|'saving'|'canonicalizing'>('idle')
const canonicalGlossaryNotice = ref<{ kind: 'success'|'warning'; message: string } | null>(null)
const bibleVisualTranslationNotice = ref<{ kind: 'success'|'warning'; message: string } | null>(null)
const translationReviewRefreshKey = ref(0)
const bibleDraft = ref({ name: '', description: '', visualPrompt: '', referenceAssetIds: [] as string[] })
type BibleDraftField = 'name'|'description'|'visualPrompt'
const bibleDraftDirty = ref<Record<BibleDraftField,boolean>>({name:false,description:false,visualPrompt:false})
const bibleDraftProposal = ref<Partial<Record<BibleDraftField,string>>>({})
const bibleDraftLanguage = ref<BibleDraftLanguage>('zh')
const bibleDraftGenerating = ref(false); const bibleDraftGenerateError = ref(''); const bibleDraftGenerateResult = ref<BibleAssetDraftResult|null>(null); const bibleDraftStale = ref(false); const bibleDraftAttempt = ref(0)
const bibleCreateError = ref('')
const bibleAssetCatalog = ref<Asset[]>([]); const bibleAssetCatalogLoading = ref(false); const bibleAssetCatalogError = ref(''); const bibleAssetSearch = ref(''); const bibleAssetCategory = ref<'compatible'|'all'|'character'|'scene'|'prop'|'image'>('compatible'); const bibleAssetPreview = ref<Asset|null>(null)
const episodeDraft = ref({ title: '', synopsis: '', script: '' })
const isNew = computed(() => route.name === 'project-new')
const currentStyle = computed(() => studio.styles.find((item) => item.id === (project.value?.styleId || newProject.value.styleId)))
const scriptBibleSourceDirty = computed(() => Boolean(
  project.value && masterScriptNeedsScriptBibleRefresh(
    project.value.masterScript,
    savedMasterScript.value,
  )
))
const newProjectStyleHint = computed(() => currentStyle.value
  ? [currentStyle.value.name, currentStyle.value.description, currentStyle.value.tags.join('、')].filter(Boolean).join('；')
  : '')
const bibleItems = computed(() => project.value?.bible[bibleSection.value] || [])
const bibleScopeBySection = { characters: 'character', scenes: 'scene', props: 'prop' } as const
const bibleImageEntityKey = (entityType: BibleImageEntityType, entityId: string) => `${entityType}:${entityId}`
const bibleImageCandidateFor = (item: BibleItem): BibleImageCandidateProjection | undefined => {
  const entityType = bibleScopeBySection[bibleSection.value]
  return bibleImageCandidates.value?.candidates.find((candidate) => (
    candidate.entityType === entityType && candidate.entityId === item.id
  ))
}
const bibleImageProjectionFor = (entityType: BibleImageEntityType, entityId: string): BibleImageCandidateProjection | undefined => (
  bibleImageCandidates.value?.candidates.find((candidate) => (
    candidate.entityType === entityType && candidate.entityId === entityId
  ))
)
const bibleImageAllTargets = computed(() => {
  if (!project.value) return []
  return [
    ...project.value.bible.characters.map((item) => ({ entityType: 'character' as const, item })),
    ...project.value.bible.scenes.map((item) => ({ entityType: 'scene' as const, item })),
    ...project.value.bible.props.map((item) => ({ entityType: 'prop' as const, item })),
  ]
})
const bibleImageTargetConfirmed = (entityType: BibleImageEntityType, item: BibleItem): boolean => {
  const projection = bibleImageProjectionFor(entityType, item.id)
  return Boolean(
    item.referenceAssetIds.length
    || projection?.referenceAssets.length
    || projection?.latestCandidate?.persistedAsAsset
    || projection?.latestCandidate?.promotedAssets.length
    || projection?.latestCandidate?.outputMedia.some((media) => Boolean(media.promotedAssetId))
    || projection?.candidateHistory.some((candidate) => (
      candidate.persistedAsAsset
      || candidate.promotedAssets.length
      || candidate.outputMedia.some((media) => Boolean(media.promotedAssetId))
    ))
  )
}
const bibleImageUnconfirmedTargets = computed(() => (
  bibleImageAllTargets.value.filter(({ entityType, item }) => !bibleImageTargetConfirmed(entityType, item))
))
const bibleImageTaskStatusLabel = (status?: string) => ({
  queued: '排队中', running: '生成中', succeeded: '图片已生成', failed: '生成失败',
  cancel_requested: '正在取消', cancelled: '已取消', incomplete: '候选链不完整',
} as Record<string, string>)[status || ''] || '尚未生成'
const bibleImageOutputIsImage = (media: TaskOutputMedia) => (
  media.kind === 'image' || /\.(png|jpe?g|webp|gif|avif)$/i.test(media.filename)
)
const bibleImageOutputImages = (candidate: BibleImageLatestCandidate) => (
  candidate.outputMedia.filter(bibleImageOutputIsImage)
)
const bibleImageCandidateKey = (candidate: BibleImageLatestCandidate) => (
  candidate.generationTaskId || candidate.promptArtifactId
)
const bibleImagePromotionKey = (
  projection: BibleImageCandidateProjection,
  candidate: BibleImageLatestCandidate,
  media: TaskOutputMedia,
  index: number,
) => `${bibleImageEntityKey(projection.entityType, projection.entityId)}:${bibleImageCandidateKey(candidate)}:${media.index ?? index}`
const bibleImageOutputIsBound = (item: BibleItem, media: TaskOutputMedia) => Boolean(
  media.promotedAssetId && bibleAssetIds(item).includes(media.promotedAssetId),
)
const bibleImageOutputActionLabel = (
  item: BibleItem,
  projection: BibleImageCandidateProjection,
  candidate: BibleImageLatestCandidate,
  media: TaskOutputMedia,
  index: number,
) => {
  const key = bibleImagePromotionKey(projection, candidate, media, index)
  if (bibleImagePromotingKey.value === key) return media.promotedAssetId ? '正在重新绑定…' : '正在确认…'
  if (media.promotedAssetId) return bibleImageOutputIsBound(item, media) ? '已绑定参考图' : '重新绑定参考图'
  if (!candidate.sourceCurrent) return '仍加入参考图'
  return '确认并加入参考图'
}
const eventText = (event: Event) => (event.target instanceof HTMLTextAreaElement ? event.target.value : '')
const bibleImagePromptInputsFor = (item: BibleItem): BibleImagePromptInputs | null => (
  bibleImageCandidateFor(item)?.currentPromptInputs || null
)
const bibleImagePromptInputRows = (item: BibleItem, role: BibleImagePromptInput['role']): BibleImagePromptInput[] => (
  (role === 'positive'
    ? bibleImagePromptInputsFor(item)?.positive
    : bibleImagePromptInputsFor(item)?.negative) || []
)
const bibleImagePromptOverridesFor = (
  entityType: BibleImageEntityType,
  item: BibleItem,
): Record<string, string> => (
  bibleImagePromptOverrides.value[bibleImageEntityKey(entityType, item.id)] || {}
)
const bibleImagePromptDraftValue = (
  entityType: BibleImageEntityType,
  item: BibleItem,
  input: BibleImagePromptInput,
) => {
  const overrides = bibleImagePromptOverridesFor(entityType, item)
  return Object.prototype.hasOwnProperty.call(overrides, input.key) ? overrides[input.key] : input.value
}
const setBibleImagePromptOverride = (
  entityType: BibleImageEntityType,
  item: BibleItem,
  input: BibleImagePromptInput,
  nextValue: string,
) => {
  if (!input.editable || bibleImageHasRetry(item)) return
  const key = bibleImageEntityKey(entityType, item.id)
  const nextOverrides = { ...(bibleImagePromptOverrides.value[key] || {}) }
  if (nextValue === input.value) delete nextOverrides[input.key]
  else nextOverrides[input.key] = nextValue
  bibleImagePromptOverrides.value = {
    ...bibleImagePromptOverrides.value,
    [key]: nextOverrides,
  }
}
const bibleImagePromptOverrideCount = (entityType: BibleImageEntityType, item: BibleItem) => (
  Object.keys(bibleImagePromptOverridesFor(entityType, item)).length
)
const clearBibleImagePromptOverrides = (entityType: BibleImageEntityType, item: BibleItem) => {
  const key = bibleImageEntityKey(entityType, item.id)
  const next = { ...bibleImagePromptOverrides.value }
  delete next[key]
  bibleImagePromptOverrides.value = next
}
const bibleImagePromptTextPreview = (
  entityType: BibleImageEntityType,
  item: BibleItem,
  role: BibleImagePromptInput['role'],
) => (
  bibleImagePromptInputRows(item, role)
    .map((input) => bibleImagePromptDraftValue(entityType, item, input).trim())
    .filter(Boolean)
    .join(role === 'positive' ? ', ' : '; ')
)
const confirmStaleBibleImageCandidatePromotion = (item: BibleItem, candidate: BibleImageLatestCandidate) => (
  candidate.sourceCurrent || window.confirm(
    `“${item.name}”的这张候选图基于旧版 Bible、译文或视觉规则生成。\n\n` +
    '确认后会把这张历史候选作为参考图加入，不会覆盖当前 Bible 文本；后续生成可能同时受当前描述和旧图影响。是否继续？',
  )
)
const openBibleImagePreview = (
  item: BibleItem,
  candidate: BibleImageLatestCandidate,
  media: TaskOutputMedia,
  index: number,
) => {
  const src = media.previewUrl || media.downloadUrl
  if (!src) return
  bibleImagePreview.value = {
    src,
    title: `${item.name} · 参考图候选 ${index + 1}`,
    filename: media.filename || `${bibleImageCandidateKey(candidate)}.png`,
    downloadUrl: media.downloadUrl || media.previewUrl || '',
  }
}
const loadBibleImageCandidates = async (projectId = project.value?.id || ''): Promise<BibleImageCandidatesResult | null> => {
  if (!projectId) return null
  bibleImageLoading.value = true
  bibleImageLoadError.value = ''
  try {
    const result = await repository.getBibleImageCandidates(projectId)
    if (project.value?.id === projectId) bibleImageCandidates.value = result
    return result
  } catch (error) {
    if (project.value?.id === projectId) {
      bibleImageLoadError.value = bibleImageCandidateErrorMessage(error, '实体图片候选读取失败，请稍后刷新。')
    }
    return null
  } finally {
    if (project.value?.id === projectId) bibleImageLoading.value = false
  }
}
const storedBibleImageRetry = (projectId: string, entityType: BibleImageEntityType, entityId: string) => (
  parseBibleImageCandidateRetryState(sessionStorage.getItem(
    bibleImageCandidateRetryStorageKey(projectId, entityType, entityId),
  ))
)
const persistBibleImageRetry = (state: BibleImageCandidateRetryState) => {
  sessionStorage.setItem(
    bibleImageCandidateRetryStorageKey(state.projectId, state.entityType, state.entityId),
    JSON.stringify(state),
  )
}
const clearBibleImageRetry = (state: BibleImageCandidateRetryState) => {
  sessionStorage.removeItem(bibleImageCandidateRetryStorageKey(state.projectId, state.entityType, state.entityId))
}
const bibleImageHasRetry = (item: BibleItem) => Boolean(project.value && storedBibleImageRetry(
  project.value.id,
  bibleScopeBySection[bibleSection.value],
  item.id,
))
const containsCjk = (value: string) => /[\u3400-\u4dbf\u4e00-\u9fff]/.test(value)
const allBibleNames = computed(() => project.value
  ? [...project.value.bible.characters, ...project.value.bible.scenes, ...project.value.bible.props].map((item) => item.name)
  : [])
const effectiveTranslationGlossary = computed(() => translationGlossaryValidation.value.error
  ? project.value?.translationGlossary || {}
  : translationGlossaryValidation.value.value)
const missingCanonicalBibleNames = computed(() => findMissingBibleCanonicalNames(allBibleNames.value, effectiveTranslationGlossary.value))
const canonicalTargetCollisions = computed(() => translationGlossaryValidation.value.error
  ? []
  : bibleCanonicalTargetCollisions(allBibleNames.value, translationGlossaryValidation.value.value))
const canonicalTargetCollisionError = computed(() => bibleCanonicalTargetCollisionMessage(canonicalTargetCollisions.value))
const canonicalGlossaryBlockedReason = computed(() => translationGlossaryValidation.value.error || canonicalTargetCollisionError.value)
const canonicalForBibleItem = (item: BibleItem) => String(project.value?.translationGlossary?.[item.name] || '').trim()
const projectTranslationFields = computed<PromptTranslationField[]>(() => {
  if (!project.value) return []
  const fields: PromptTranslationField[] = []
  const add = (field: PromptTranslationField) => { if (field.sourceText.trim()) fields.push(field) }
  if (currentStyle.value?.promptPrefix) add({ fieldKey:'style.visual_prompt', label:'全剧风格视觉提示', sourceText:currentStyle.value.promptPrefix, scopeType:'project', scopeId:project.value.id, impact:'prompt' })
  add({ fieldKey:'project.production_settings.color_script', label:'全剧色彩脚本', sourceText:project.value.bible.colorScript, scopeType:'project', scopeId:project.value.id, impact:'continuity' })
  add({ fieldKey:'project.continuity_summary', label:'项目连续性摘要', sourceText:project.value.continuitySummary || '', scopeType:'project', scopeId:project.value.id, impact:'continuity', help:'与色彩脚本独立保存；用于跨集身份、服装、空间、道具和光线连续性，请单独审核' })
  return fields
})
const bibleTranslationFields = computed<PromptTranslationField[]>(() => {
  if (!project.value) return []
  const fields: PromptTranslationField[] = []
  const add = (field: PromptTranslationField) => { if (field.sourceText.trim()) fields.push(field) }
  const profile = bibleSection.value === 'characters'
    ? { scopeType:'character' as const, fieldKey:'character.visual_identity', prefix:'人物' }
    : bibleSection.value === 'scenes'
      ? { scopeType:'scene' as const, fieldKey:'scene.visual_identity', prefix:'场景' }
      : { scopeType:'prop' as const, fieldKey:'prop.visual_identity', prefix:'道具' }
  for (const item of bibleItems.value) {
    const base={scopeType:profile.scopeType,scopeId:item.id,impact:'continuity' as const}
    add({ ...base, fieldKey:profile.fieldKey, label:`${profile.prefix} · ${item.name} · 视觉锚点`, sourceText:item.visualPrompt })
    if(bibleSection.value==='characters'){add({...base,fieldKey:'character.age_appearance',label:`人物 · ${item.name} · 年龄外观`,sourceText:item.ageAppearance||''});add({...base,fieldKey:'character.face_identity',label:`人物 · ${item.name} · 面部身份`,sourceText:item.faceIdentity||''});add({...base,fieldKey:'character.wardrobe',label:`人物 · ${item.name} · 固定服装`,sourceText:item.wardrobe||''});add({...base,fieldKey:'character.voice_profile',label:`人物 · ${item.name} · 声音特征`,sourceText:item.voiceProfile||'',impact:'prompt'});if(!(item.voiceProfile||'').trim())add({...base,fieldKey:'character.personality',label:`人物 · ${item.name} · 说话者人物描述`,sourceText:item.personality||'',impact:'prompt',help:'声音特征为空，当前会作为 H3 对白说话者描述后备；不是管理用基础描述'})}
    else if(bibleSection.value==='scenes'){add({...base,fieldKey:'scene.location',label:`场景 · ${item.name} · 地点结构`,sourceText:item.location||''});add({...base,fieldKey:'scene.time_of_day',label:`场景 · ${item.name} · 时间`,sourceText:item.timeOfDay||''});add({...base,fieldKey:'scene.lighting',label:`场景 · ${item.name} · 光线`,sourceText:item.lighting||''});add({...base,fieldKey:'scene.color_palette',label:`场景 · ${item.name} · 色板`,sourceText:item.colorPalette||''});add({...base,fieldKey:'scene.ambient_sound',label:`场景 · ${item.name} · 环境声`,sourceText:item.ambientSound||'',impact:'prompt'})}
    else {add({...base,fieldKey:'prop.material',label:`道具 · ${item.name} · 材质`,sourceText:item.material||''});add({...base,fieldKey:'prop.distinctive_marks',label:`道具 · ${item.name} · 固定标记`,sourceText:item.distinctiveMarks||''});add({...base,fieldKey:'prop.continuity_state',label:`道具 · ${item.name} · 连续性状态`,sourceText:item.continuityState||''})}
  }
  return fields
})
const totalShots = computed(() => project.value?.episodes.reduce((sum, episode) => sum + episode.shots.length, 0) || 0)
const totalSeconds = computed(() => project.value?.episodes.reduce((sum, episode) => sum + episode.shots.reduce((s, shot) => s + (shot.mediaType === 'video' ? shot.duration : 0), 0), 0) || 0)
const wholeEpisode = computed(() => project.value?.episodes.find((episode) => episode.id === wholeEpisodeId.value) || null)
const wholeEpisodeHasRef2VA = computed(() => wholeEpisode.value?.shots.some((shot) => shot.mediaType === 'video' && shot.mode === 'Ref2VA') || false)
const wholeEpisodeWorkflowSelections = computed<ProductionWorkflowSelection[]>(() => wholeEpisodeHasRef2VA.value && wholeRef2vaTemplateId.value ? [{outputKind:'video',h3Mode:'Ref2VA',templateId:wholeRef2vaTemplateId.value,parameters:{...wholeRef2vaParameters.value}}] : [])
const currentRelease = computed(() => releaseBundles.value[0] || null)
const draftRelease = computed(() => releaseBundles.value.find((item) => item.status === 'draft') || null)
const releaseVideoEpisodeCount = computed(() => project.value?.episodes.filter((episode) => episode.shots.some((shot) => shot.mediaType === 'video')).length || 0)
const releaseSelectionComplete = computed(() => Boolean(
  currentRelease.value
  && currentRelease.value.selection.shots.length === totalShots.value
  && currentRelease.value.selection.episodes.length === releaseVideoEpisodeCount.value
  && (project.value?.status !== 'completed' || currentRelease.value.selection.seriesAssemblyId),
))
const bibleSections = [{ value: 'characters' as const, label: '角色圣经', singular: '角色', icon: UserRound }, { value: 'scenes' as const, label: '场景圣经', singular: '场景', icon: Image }, { value: 'props' as const, label: '道具圣经', singular: '道具', icon: Box }]
const h3Modes: Array<{ value: H3Mode; label: string; desc: string }> = [{ value: 'T2VA', label: '文生视频', desc: '无需参考输入' }, { value: 'FL2VA', label: '首尾帧生视频', desc: '0–2 张帧图自动切换 T2VA / I2VA / L2VA / FL2VA' }, { value: 'Ref2VA', label: '参考生视频', desc: '0–9 分类图、0–3 视频、配对音轨与 0–3 独立音频，支持纯音频' }]
const workflowLabel = (mode?:H3Mode) => h3Modes.find((item)=>item.value===mode)?.label || '首尾帧生视频'
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const invalidateProjectPromptsFromTranslation = () => {
  if (!project.value) return
  for (const episode of project.value.episodes) for (const shot of episode.shots) {
    shot.generatedPrompt = undefined; shot.prompt = ''; if (shot.status === 'ready') shot.status = 'draft'
  }
}

const assemblyFor = (episodeId: string) => episodeAssemblies.value[episodeId]
const assemblyOutputFor = (episodeId: string) => completedEpisodeAssemblies.value[episodeId]
const assemblyActive = (episodeId: string) => ['planned', 'queued', 'running'].includes(assemblyFor(episodeId)?.status || '')
const assemblyStateLabel = (episodeId: string) => assemblyFor(episodeId)?.status === 'running' ? '后台组装中' : '组装排队中'
let assemblyPollTimer: ReturnType<typeof setInterval> | undefined
let assemblyPollRunning = false
let assemblyListPollTick = 0
const assemblyPreviewRecoveryAttempts = new Map<string, number>()
const loadEpisodeAssemblies = async (projectId: string) => {
  try {
    const rows = await studio.getEpisodeAssemblies(projectId)
    const latest: Record<string, AssemblyResult> = {}
    const completed: Record<string, AssemblyResult> = {}
    for (const item of rows) {
      if (!item.episodeId) continue
      if (!latest[item.episodeId]) latest[item.episodeId] = item
      if (item.status === 'succeeded' && !completed[item.episodeId]) completed[item.episodeId] = item
    }
    episodeAssemblies.value = latest; completedEpisodeAssemblies.value = completed; assemblyError.value = ''
  } catch (error) {
    episodeAssemblies.value = {}
    assemblyError.value = error instanceof Error ? error.message : '成片历史加载失败'
    studio.notify(`成片历史暂时无法加载：${assemblyError.value}`, 'error')
  }
}
const pollActiveAssemblies = async () => {
  if (assemblyPollRunning) return
  assemblyPollRunning = true
  try {
    assemblyListPollTick += 1
    // Periodically reconcile the authoritative list as well as known IDs. If
    // the enqueue HTTP response was lost after commit, this discovers the job
    // without requiring a page refresh or a duplicate submit.
    if (project.value && assemblyListPollTick % 15 === 0) {
      await loadEpisodeAssemblies(project.value.id)
    }
    const active = Object.values(episodeAssemblies.value).filter((item) => ['planned', 'queued', 'running'].includes(item.status))
    if (!active.length) return
    const refreshed = await Promise.all(active.map((item) => studio.getAssembly(item.id)))
    for (const item of refreshed) {
      if (!item.episodeId) continue
      const previous = episodeAssemblies.value[item.episodeId]
      episodeAssemblies.value = { ...episodeAssemblies.value, [item.episodeId]: item }
      if (item.status === 'succeeded') {
        completedEpisodeAssemblies.value = { ...completedEpisodeAssemblies.value, [item.episodeId]: item }
        const episode = project.value?.episodes.find((entry) => entry.id === item.episodeId)
        if (episode) episode.status = 'completed'
        if (previous?.status !== 'succeeded') studio.notify(`第 ${episode?.number || ''} 集成片已在后台组装完成`)
      } else if (item.status === 'failed' && previous?.status !== 'failed') {
        studio.notify(item.errorMessage || '本集成片后台组装失败', 'error')
      }
    }
  } catch {
    // A transient refresh error must not turn a durable background job into a
    // false failure. The next poll (or a manual refresh) will reconcile it.
  } finally { assemblyPollRunning = false }
}
const selectEpisode = (episode: Episode) => { selectedEpisodeId.value = episode.id; assemblyError.value = '' }
const handleAssemblyPreviewLoaded = (episodeId: string, assemblyId: string) => {
  assemblyPreviewRecoveryAttempts.delete(assemblyId)
  if (assemblyPreviewErrors.value[episodeId]) {
    assemblyPreviewErrors.value = { ...assemblyPreviewErrors.value, [episodeId]: '' }
  }
}
const recoverAssemblyPreview = async (episode: Episode, manual = false) => {
  const current = assemblyOutputFor(episode.id)
  if (!current || assemblyPreviewRefreshingId.value === current.id) return
  const attempts = assemblyPreviewRecoveryAttempts.get(current.id) || 0
  if (!manual && attempts >= 1) {
    assemblyPreviewErrors.value = { ...assemblyPreviewErrors.value, [episode.id]: '成片地址已刷新但仍无法播放，请手动重试；若持续失败，请检查组装产物。' }
    return
  }
  if (manual) assemblyPreviewRecoveryAttempts.delete(current.id)
  assemblyPreviewRecoveryAttempts.set(current.id, (assemblyPreviewRecoveryAttempts.get(current.id) || 0) + 1)
  assemblyPreviewRefreshingId.value = current.id
  assemblyPreviewErrors.value = { ...assemblyPreviewErrors.value, [episode.id]: '' }
  try {
    const refreshed = await studio.getAssembly(current.id)
    if (refreshed.status !== 'succeeded' || !refreshed.outputUrl) throw new Error('服务端未返回可播放的成片地址')
    completedEpisodeAssemblies.value = { ...completedEpisodeAssemblies.value, [episode.id]: refreshed }
  } catch (error) {
    assemblyPreviewErrors.value = { ...assemblyPreviewErrors.value, [episode.id]: error instanceof Error ? error.message : '成片预览地址刷新失败' }
  } finally {
    if (assemblyPreviewRefreshingId.value === current.id) assemblyPreviewRefreshingId.value = ''
  }
}
const downloadAssembly = async (episode: Episode) => {
  const current = assemblyOutputFor(episode.id)
  if (!current) return
  try {
    // Signed media URLs are intentionally short-lived. Refresh immediately
    // before download instead of relying on a link cached while the page was open.
    const refreshed = await studio.getAssembly(current.id)
    if (refreshed.status !== 'succeeded') throw new Error('本集成片当前不可下载')
    completedEpisodeAssemblies.value = { ...completedEpisodeAssemblies.value, [episode.id]: refreshed }
    const url = refreshed.downloadUrl || refreshed.outputUrl
    if (!url) throw new Error('服务端未返回成片下载地址')
    const link = document.createElement('a')
    link.href = url; link.download = ''; document.body.appendChild(link); link.click(); link.remove()
  } catch (error) {
    studio.notify(error instanceof Error ? error.message : '成片下载地址刷新失败', 'error')
  }
}
const assembleEpisode = async (episode: Episode) => {
  if (!project.value) return
  if (!episode.shots.length) { studio.notify('本集还没有分镜，无法组装成片', 'error'); return }
  selectedEpisodeId.value = episode.id; assemblyLoadingId.value = episode.id; assemblyError.value = ''
  try {
    const result = await studio.assembleEpisode(project.value.id, episode.id)
    episodeAssemblies.value = { ...episodeAssemblies.value, [episode.id]: result }
    if (result.status === 'failed') throw new Error(result.errorMessage || result.message || '本集成片组装失败')
    if (result.status === 'succeeded') {
      completedEpisodeAssemblies.value = { ...completedEpisodeAssemblies.value, [episode.id]: result }
      episode.status = 'completed'
      const storedEpisode = studio.projectById(project.value.id)?.episodes.find((item) => item.id === episode.id)
      if (storedEpisode) storedEpisode.status = 'completed'
      studio.notify(`第 ${episode.number} 集成片已组装完成`)
    } else {
      studio.notify(`第 ${episode.number} 集组装任务已进入后台队列；可以离开本页`)
    }
  } catch (error) {
    const originalMessage = error instanceof Error ? error.message : '本集成片组装失败'
    const transport = error as { code?: string; response?: unknown }
    const responseMayBeLost = !transport.response || transport.code === 'ECONNABORTED' || /timeout|network|连接|超时/i.test(originalMessage)
    if (responseMayBeLost && project.value) {
      await loadEpisodeAssemblies(project.value.id)
      const reconciled = assemblyFor(episode.id)
      if (reconciled && ['queued', 'running', 'succeeded'].includes(reconciled.status)) {
        studio.notify(`第 ${episode.number} 集组装任务已由服务端确认；页面将继续跟踪状态`, 'info')
        return
      }
    }
    assemblyError.value = originalMessage
    studio.notify(assemblyError.value, 'error')
  } finally { assemblyLoadingId.value = '' }
}
const openWholeEpisode = (episode: Episode) => {
  wholeEpisodeId.value = episode.id
  wholeEpisodeExtendedConfirmed.value = false
  wholeEpisodeIdempotencyConflict.value = ''
  const storageKey = wholeEpisodeRetryStorageKey(episode.id, episode.activeShotVersionKey)
  const pendingKey = sessionStorage.getItem(storageKey) || ''
  if (pendingKey) episodeIdempotencyKeys.value = { ...episodeIdempotencyKeys.value, [episode.id]: pendingKey }
  const pendingProfile = sessionStorage.getItem(`${storageKey}_profile`)
  const pendingImageProfile = sessionStorage.getItem(`${storageKey}_image_profile`)
  wholeEpisodeProfile.value = isProductionResolutionProfile(pendingProfile) ? pendingProfile : DEFAULT_PRODUCTION_RESOLUTION_PROFILE
  wholeEpisodeImageProfile.value = isProductionResolutionProfile(pendingImageProfile) ? pendingImageProfile : '0.98'
  wholeEpisodeMultiple.value = pendingKey
    ? normalizeProductionResolutionMultiple(sessionStorage.getItem(`${storageKey}_multiple`))
    : DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE
  wholeEpisodeImageMultiple.value = pendingKey
    ? normalizeImageProductionResolutionMultiple(sessionStorage.getItem(`${storageKey}_image_multiple`))
    : DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE
  wholeRef2vaTemplateId.value = ''; wholeRef2vaParameters.value = {}
  if (pendingKey) {
    try {
      const rows = JSON.parse(sessionStorage.getItem(`${storageKey}_workflow_selections`) || '[]') as Array<{templateId?:unknown;parameters?:unknown}>
      const row = rows[0]
      if (typeof row?.templateId === 'string') { wholeRef2vaTemplateId.value = row.templateId; wholeRef2vaParameters.value = row.parameters && typeof row.parameters === 'object' ? row.parameters as Record<string,number> : {} }
    } catch { /* Invalid local retry state safely falls back to the official default. */ }
  }
  wholeEpisodeOpen.value = true
}
const abandonWholeEpisodeRetryState = () => {
  const episode = wholeEpisode.value
  if (!episode) return
  const storageKey = wholeEpisodeRetryStorageKey(episode.id, episode.activeShotVersionKey)
  const nextKeys = { ...episodeIdempotencyKeys.value }; delete nextKeys[episode.id]; episodeIdempotencyKeys.value = nextKeys
  sessionStorage.removeItem(storageKey)
  sessionStorage.removeItem(`${storageKey}_profile`)
  sessionStorage.removeItem(`${storageKey}_multiple`)
  sessionStorage.removeItem(`${storageKey}_image_profile`)
  sessionStorage.removeItem(`${storageKey}_image_multiple`)
  sessionStorage.removeItem(`${storageKey}_workflow_selections`)
  wholeEpisodeIdempotencyConflict.value = ''
  wholeEpisodeExtendedConfirmed.value = false
  studio.notify('已放弃旧整集提交状态；再次生成时会创建新的幂等批次。', 'info')
}
const generateWholeEpisode = async () => {
  const episode = wholeEpisode.value
  if (!project.value) return
  if (!episode) return
  if (canonicalGlossaryBlockedReason.value) { studio.notify(canonicalGlossaryBlockedReason.value, 'error'); return }
  if (!episode.shots.length) { studio.notify('本集还没有分镜，无法生成整集', 'error'); return }
  episodeGenerationLoadingId.value = episode.id
  const storageKey = wholeEpisodeRetryStorageKey(episode.id, episode.activeShotVersionKey)
  const idempotencyKey = episodeIdempotencyKeys.value[episode.id] || sessionStorage.getItem(storageKey) || `whole-episode-${episode.id}-${typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : Date.now()}`
  episodeIdempotencyKeys.value = { ...episodeIdempotencyKeys.value, [episode.id]: idempotencyKey }
  sessionStorage.setItem(storageKey, idempotencyKey)
  sessionStorage.setItem(`${storageKey}_profile`, wholeEpisodeProfile.value)
  sessionStorage.setItem(`${storageKey}_multiple`, String(wholeEpisodeMultiple.value))
  sessionStorage.setItem(`${storageKey}_image_profile`, wholeEpisodeImageProfile.value)
  sessionStorage.setItem(`${storageKey}_image_multiple`, String(wholeEpisodeImageMultiple.value))
  sessionStorage.setItem(`${storageKey}_workflow_selections`, JSON.stringify(wholeEpisodeWorkflowSelections.value))
  try {
    const task = await studio.generateWholeEpisode(project.value, episode, idempotencyKey, wholeEpisodeProfile.value, wholeEpisodeMultiple.value, wholeEpisodeImageProfile.value, wholeEpisodeImageMultiple.value, wholeEpisodeWorkflowSelections.value, wholeEpisodeExtendedConfirmed.value)
    const nextKeys = { ...episodeIdempotencyKeys.value }; delete nextKeys[episode.id]; episodeIdempotencyKeys.value = nextKeys
    sessionStorage.removeItem(storageKey)
    sessionStorage.removeItem(`${storageKey}_profile`)
    sessionStorage.removeItem(`${storageKey}_multiple`)
    sessionStorage.removeItem(`${storageKey}_image_profile`)
    sessionStorage.removeItem(`${storageKey}_image_multiple`)
    sessionStorage.removeItem(`${storageKey}_workflow_selections`)
    wholeEpisodeExtendedConfirmed.value = false
    wholeEpisodeOpen.value = false
    router.push({ path: '/workflows', query: { task: task.id, batch: task.batchId || '' } })
  } catch (error) {
    if (productionErrorCode(error) === 'production_queue_confirmation_required') {
      wholeEpisodeExtendedConfirmed.value = true
      studio.notify(`本集 ${episode.shots.length} 个镜头超过当前队列建议值；镜数会完整保留，请核对提示后再次确认。`, 'info')
      return
    }
    const message = error instanceof Error ? error.message : '整集生成任务创建失败'
    if (productionErrorCode(error) === 'production_idempotency_conflict' || (/(idempotency_key|幂等)/i.test(message) && /(不完整|不同|新的|新 key|冲突)/i.test(message))) {
      wholeEpisodeIdempotencyConflict.value = '旧整集提交状态不完整或参数已发生冲突。幂等键已保留，系统不会自动换键重提。请先到生产工作流核对已有任务；只有确认放弃旧尝试后才能创建新批次。'
      studio.notify(wholeEpisodeIdempotencyConflict.value, 'error')
      return
    }
    studio.notify(message, 'error')
  } finally { episodeGenerationLoadingId.value = '' }
}

const loadReleaseBundles = async (projectId: string) => {
  try {
    releaseBundles.value = await studio.getReleaseBundles(projectId)
    const draft = releaseBundles.value.find((item) => item.status === 'draft')
    releaseTitle.value = draft?.title || ''
    releaseNotes.value = draft?.notes || ''
    releaseError.value = ''
  } catch (error) {
    releaseError.value = error instanceof Error ? error.message : '发行候选加载失败'
  }
}
const createReleaseCandidate = async () => {
  if (!project.value) return
  releaseLoading.value = true; releaseError.value = ''
  try {
    const created = await studio.createReleaseBundle(project.value.id, {
      title: releaseTitle.value, notes: releaseNotes.value,
    })
    releaseBundles.value = [created, ...releaseBundles.value]
    releaseTitle.value = created.title; releaseNotes.value = created.notes
    studio.notify(`发行候选 v${created.version} 已按当前成功产物建立`)
  } catch (error) {
    releaseError.value = error instanceof Error ? error.message : '发行候选创建失败'
    studio.notify(releaseError.value, 'error')
  } finally { releaseLoading.value = false }
}
const saveReleaseDraft = async (refreshSelection = false) => {
  if (!project.value || !draftRelease.value) return
  releaseLoading.value = true; releaseError.value = ''
  try {
    const selection = refreshSelection
      ? await studio.proposeReleaseSelection(project.value.id)
      : undefined
    const saved = await studio.updateReleaseBundle(draftRelease.value.id, {
      title: releaseTitle.value.trim() || draftRelease.value.title,
      notes: releaseNotes.value,
      ...(selection ? { selection } : {}),
    })
    releaseBundles.value = releaseBundles.value.map((item) => item.id === saved.id ? saved : item)
    releaseTitle.value = saved.title; releaseNotes.value = saved.notes
    studio.notify(refreshSelection ? '已重新采集当前分镜与成片' : '发行候选说明已保存')
  } catch (error) {
    releaseError.value = error instanceof Error ? error.message : '发行候选保存失败'
    studio.notify(releaseError.value, 'error')
  } finally { releaseLoading.value = false }
}
const sealReleaseCandidate = async () => {
  const draft = draftRelease.value
  if (!draft || !window.confirm('封存后不可再修改。确认将当前任务、输出索引与成片锁定为发行候选？')) return
  releaseLoading.value = true; releaseError.value = ''
  try {
    const sealed = await studio.sealReleaseBundle(draft.id)
    releaseBundles.value = releaseBundles.value.map((item) => item.id === sealed.id ? sealed : item)
    releaseTitle.value = ''; releaseNotes.value = ''
    studio.notify(`发行候选 v${sealed.version} 已封存并完成签名校验`)
  } catch (error) {
    releaseError.value = error instanceof Error ? error.message : '发行候选封存失败'
    studio.notify(releaseError.value, 'error')
  } finally { releaseLoading.value = false }
}
const markReleasePublished = async (bundle: ReleaseBundle) => {
  if (!auth.user?.isStaff || !window.confirm(`确认将 v${bundle.version} 标记为正式发行版本？`)) return
  releaseLoading.value = true; releaseError.value = ''
  try {
    const released = await studio.releaseReleaseBundle(bundle.id)
    await loadReleaseBundles(released.projectId || project.value?.id || '')
    studio.notify(`发行版本 v${released.version} 已由 staff 确认`)
  } catch (error) {
    releaseError.value = error instanceof Error ? error.message : '正式发行确认失败'
    studio.notify(releaseError.value, 'error')
  } finally { releaseLoading.value = false }
}
const verifyReleaseCandidate = async (bundle: ReleaseBundle) => {
  releaseLoading.value = true; releaseError.value = ''
  try {
    const result = await studio.verifyReleaseBundle(bundle.id)
    if (result.status !== 'verified' || !result.signatureVerified || !result.liveVerified) {
      throw new Error('发行清单签名或当前产物实时校验未通过')
    }
    studio.notify(`v${bundle.version} 签名与当前发行产物实时校验通过`)
  } catch (error) {
    releaseError.value = error instanceof Error ? error.message : '发行清单校验失败'
    studio.notify(releaseError.value, 'error')
  } finally { releaseLoading.value = false }
}

const loadProject = async () => {
  loading.value = true
  projectLoadError.value = ''
  translationGlossaryText.value = ''
  bibleImageCandidates.value = null
  bibleImageLoadError.value = ''
  bibleImagePendingConfirmation.value = null
  bibleImageQueueConfirmOpen.value = false
  try {
    await studio.initialize()
    if (!isNew.value) {
      const found = await studio.loadProject(String(route.params.projectId)); if (found) {
        project.value = clone(found)
        savedMasterScript.value = project.value.masterScript
        translationGlossaryText.value = formatTranslationGlossary(project.value.translationGlossary)
        if(!['T2VA','FL2VA','Ref2VA'].includes(project.value.defaultMode)){project.value.defaultMode='FL2VA';studio.notify('检测到不受支持的默认模式，已使用“首尾帧生视频”；两个帧槽位均可为空','info')}
        await Promise.all([
          studio.refreshAssets(), loadEpisodeAssemblies(found.id), loadReleaseBundles(found.id), loadBibleImageCandidates(found.id),
        ])
      } else router.replace('/projects')
    }
  } catch (error) {
    projectLoadError.value = error instanceof Error ? error.message : '项目加载失败'
    studio.notify(projectLoadError.value, 'error')
  } finally { loading.value = false }
}
const handleCreativeDraftApplied = async (episodeId: string) => {
  const found = await studio.loadProject(String(route.params.projectId), true)
  if (found) {
    project.value = clone(found)
    savedMasterScript.value = project.value.masterScript
    translationGlossaryText.value = formatTranslationGlossary(project.value.translationGlossary)
    selectedEpisodeId.value = episodeId
  }
  studio.notify('创作草稿已应用到正式分镜；尚未创建提示词或生产任务。')
}
const handleSeriesConceptAccepted = (candidate: SeriesConceptCandidate) => {
  newProject.value = { ...newProject.value, ...seriesConceptAcceptedFields(candidate) }
  studio.notify('AI 候选已一次性填入故事基础；项目尚未创建，请继续人工核对。', 'info')
}
const loadProductionWorkflowTemplates=async()=>{productionWorkflowTemplatesLoading.value=true;productionWorkflowTemplatesError.value='';try{productionWorkflowTemplates.value=await repository.getProductionWorkflowTemplates()}catch(error){productionWorkflowTemplatesError.value=error instanceof Error?error.message:'服务端未返回工作流模板'}finally{productionWorkflowTemplatesLoading.value=false}}
const loadProductionCapability=async()=>{productionCapabilityError.value='';try{productionCapability.value=await repository.getProductionCapability();const minimum=productionCapability.value.videoShotMinDurationMs/1000;const maximum=productionCapability.value.videoShotMaxDurationMs/1000;if(!Number.isFinite(breakdownOptions.value.defaultShotDuration)||breakdownOptions.value.defaultShotDuration<minimum||breakdownOptions.value.defaultShotDuration>maximum)breakdownOptions.value.defaultShotDuration=productionCapability.value.scriptBreakdownDefaultShotDurationMs/1000;breakdownOptions.value.defaultFirstFrameContinuity=productionCapability.value.scriptBreakdownDefaultFirstFrameContinuity;breakdownOptions.value.defaultLastFrameContinuity=productionCapability.value.scriptBreakdownDefaultLastFrameContinuity}catch(error){productionCapability.value=null;productionCapabilityError.value=error instanceof Error?error.message:'服务端未返回正式视频时长配置'}}
onMounted(async () => {
  await Promise.all([loadProject(),loadProductionWorkflowTemplates(),loadProductionCapability()])
  assemblyPollTimer = setInterval(pollActiveAssemblies, 2000)
  await pollActiveAssemblies()
})
const revokePendingCoverPreview = () => {
  if (pendingCoverPreview.value.startsWith('blob:')) URL.revokeObjectURL(pendingCoverPreview.value)
  pendingCoverPreview.value = ''
}
const chooseProjectCover = () => {
  coverError.value = ''
  coverInput.value?.click()
}
const selectProjectCover = async (event: Event) => {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] || null
  input.value = ''
  if (!file) return
  const validationError = projectCoverFileError(file)
  if (validationError) { coverError.value = validationError; studio.notify(validationError, 'error'); return }
  if (isNew.value) {
    revokePendingCoverPreview()
    pendingCoverFile.value = file
    pendingCoverPreview.value = URL.createObjectURL(file)
    coverError.value = ''
    return
  }
  if (!project.value || coverSaving.value) return
  coverSaving.value = true
  coverError.value = ''
  try { project.value = clone(await studio.setProjectCover(project.value, file)) }
  catch (error) { coverError.value = error instanceof Error ? error.message : '封面上传失败，请稍后重试'; studio.notify(coverError.value, 'error') }
  finally { coverSaving.value = false }
}
const removeProjectCover = async () => {
  if (isNew.value) {
    pendingCoverFile.value = null
    revokePendingCoverPreview()
    coverError.value = ''
    return
  }
  if (!project.value || coverSaving.value || !project.value.coverAssetId) return
  coverSaving.value = true
  coverError.value = ''
  try { project.value = clone(await studio.setProjectCover(project.value, null)) }
  catch (error) { coverError.value = error instanceof Error ? error.message : '封面移除失败，请稍后重试'; studio.notify(coverError.value, 'error') }
  finally { coverSaving.value = false }
}
const refreshCurrentProjectCover = async () => {
  if (!project.value) return undefined
  const refreshed = await studio.loadProject(project.value.id, true)
  project.value.coverAssetId = refreshed.coverAssetId
  project.value.coverUrl = refreshed.coverUrl
  return refreshed.coverUrl
}
onBeforeUnmount(() => { if (assemblyPollTimer) clearInterval(assemblyPollTimer); revokePendingCoverPreview() })
watch(() => route.params.projectId, async () => { await loadProject(); await pollActiveAssemblies() })

const createProject = async () => {
  if (createLoading.value) return
  if (!newProject.value.title.trim()) { studio.notify('请先填写项目名称', 'error'); return }
  createLoading.value = true
  try {
    let created = await studio.createProject(newProject.value)
    if (pendingCoverFile.value) {
      try { created = await studio.setProjectCover(created, pendingCoverFile.value) }
      catch (error) {
        studio.notify(`项目已创建，但封面上传失败，可进入项目稍后重试：${error instanceof Error ? error.message : '未知错误'}`, 'error')
      }
    }
    pendingCoverFile.value = null
    revokePendingCoverPreview()
    await router.replace(`/projects/${created.id}`)
  }
  catch (error) { studio.notify(error instanceof Error ? error.message : '创建项目失败', 'error') }
  finally { createLoading.value = false }
}
const save = async (refreshScriptBibleCandidates = false): Promise<SeriesProject | null> => {
  if (!project.value) return null
  if (scriptBibleApplying.value) {
    studio.notify('Bible 候选正在由服务端确认；请等待请求完成后再保存项目或母稿。', 'info')
    return null
  }
  const glossary = translationGlossaryValidation.value
  if (glossary.error) { studio.notify(glossary.error, 'error'); return null }
  if (canonicalTargetCollisionError.value) {
    canonicalGlossaryNotice.value = { kind: 'warning', message: canonicalTargetCollisionError.value }
    studio.notify(canonicalTargetCollisionError.value, 'error')
    return null
  }
  const previousGlossary = JSON.stringify(Object.entries(project.value.translationGlossary || {}).sort(([left], [right]) => left.localeCompare(right)))
  const nextGlossary = JSON.stringify(Object.entries(glossary.value).sort(([left], [right]) => left.localeCompare(right)))
  const glossaryChanged = previousGlossary !== nextGlossary
  project.value.translationGlossary = glossary.value
  saving.value = true
  try {
    project.value = clone(await studio.saveProject(project.value))
    savedMasterScript.value = project.value.masterScript
    translationGlossaryText.value = formatTranslationGlossary(project.value.translationGlossary)
    if (glossaryChanged) translationReviewRefreshKey.value += 1
    if (refreshScriptBibleCandidates) {
      const state = await scriptBiblePanel.value?.refresh()
      if (state) handleScriptBibleState(state)
      else studio.notify('项目已保存，但剧本 Bible 候选刷新失败；拆解前请先在候选面板重试。', 'info')
      await loadBibleImageCandidates(project.value.id)
    }
    return project.value
  } catch (error) { studio.notify(error instanceof Error ? error.message : '保存失败', 'error'); return null }
  finally { saving.value = false }
}
const bibleItemsForType = (source: SeriesProject, entityType: BibleImageEntityType): BibleItem[] => (
  entityType === 'character' ? source.bible.characters : entityType === 'scene' ? source.bible.scenes : source.bible.props
)
const bibleImagePersistedId = (id: string) => /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(id)
const bibleImagePromptFields = [
  'name', 'description', 'visualPrompt', 'ageAppearance', 'faceIdentity',
  'wardrobe', 'voiceProfile', 'personality', 'location', 'timeOfDay',
  'lighting', 'colorPalette', 'ambientSound', 'material',
  'distinctiveMarks', 'continuityState',
] as const
const bibleImageItemPromptSnapshot = (item: BibleItem) => Object.fromEntries(
  bibleImagePromptFields.map((field) => [field, String(item[field] || '').trim()]),
)
const bibleImagePromptFieldsMatch = (left: BibleItem, right: BibleItem) => (
  JSON.stringify(bibleImageItemPromptSnapshot(left)) === JSON.stringify(bibleImageItemPromptSnapshot(right))
)
const resolveSavedBibleImageItem = async (
  entityType: BibleImageEntityType,
  item: BibleItem,
): Promise<BibleItem | null> => {
  const currentProject = project.value
  if (!currentProject) return null
  if (!bibleImagePersistedId(item.id)) {
    const saved = await save(false)
    if (!saved) return null
    return bibleItemsForType(saved, entityType).find((candidate) => candidate.name === item.name) || null
  }
  let fresh: SeriesProject
  try {
    fresh = await studio.loadProject(currentProject.id, true)
  } catch (error) {
    setBibleImageActionError(
      bibleImageEntityKey(entityType, item.id),
      `读取已保存 Bible 失败，本次没有创建图片任务：${error instanceof Error ? error.message : '项目详情刷新失败'}`,
    )
    return null
  }
  const savedItem = bibleItemsForType(fresh, entityType).find((candidate) => candidate.id === item.id) || null
  if (!savedItem) {
    setBibleImageActionError(bibleImageEntityKey(entityType, item.id), '服务端已保存 Bible 中没有该实体；请刷新页面后再生成参考图。')
    return null
  }
  if (!bibleImagePromptFieldsMatch(item, savedItem)) {
    setBibleImageActionError(
      bibleImageEntityKey(entityType, item.id),
      '当前实体有未保存编辑或服务端版本已变化；请先保存并刷新 Bible 后再生成参考图。',
    )
    return null
  }
  return savedItem
}
const replaceBibleImageProjection = (candidate: BibleImageCandidateProjection) => {
  const current = bibleImageCandidates.value
  const promptConfig = current?.promptConfig || project.value?.bibleImagePromptConfig
  if (!promptConfig) return
  const candidates = [
    ...(current?.candidates || []).filter((item) => !(
      item.entityType === candidate.entityType && item.entityId === candidate.entityId
    )),
    candidate,
  ]
  const characterCount = project.value?.bible.characters.length || 0
  const sceneCount = project.value?.bible.scenes.length || 0
  const propCount = project.value?.bible.props.length || 0
  bibleImageCandidates.value = {
    projectId: project.value?.id || current?.projectId || '',
    promptConfig,
    candidates,
    counts: {
      characters: characterCount,
      scenes: sceneCount,
      props: propCount,
      total: characterCount + sceneCount + propCount,
      withCandidate: candidates.filter((item) => item.latestCandidate !== null).length,
    },
  }
}
const setBibleImageActionError = (key: string, message = '') => {
  bibleImageActionErrors.value = { ...bibleImageActionErrors.value, [key]: message }
}
const bibleImageRandomId = () => (
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`
)
const submitBibleImageCandidate = async (
  state: BibleImageCandidateRetryState,
  extendedGenerationConfirmed: boolean,
  options: { silent?: boolean } = {},
): Promise<boolean> => {
  const key = bibleImageEntityKey(state.entityType, state.entityId)
  bibleImageGeneratingKey.value = key
  setBibleImageActionError(key)
  try {
    const result = await repository.createBibleImageCandidate(state.projectId, {
      entityType: state.entityType,
      entityId: state.entityId,
      idempotencyKey: state.idempotencyKey,
      extraDirection: state.extraDirection,
      seed: state.seed,
      priority: state.priority,
      maxAttempts: state.maxAttempts,
      promptOverrides: state.promptOverrides,
      extendedGenerationConfirmed,
    })
    replaceBibleImageProjection(result.candidate)
    clearBibleImageRetry(state)
    bibleImagePendingConfirmation.value = null
    bibleImageQueueConfirmOpen.value = false
    try { await studio.refreshWorkflows(true) }
    catch { studio.notify('实体参考图任务已创建，但任务列表刷新失败；可从任务入口稍后查看。', 'info') }
    if (!options.silent) {
      studio.notify(result.idempotentReplay ? '已安全恢复此前创建的实体参考图任务。' : '实体参考图任务已创建；图片成功后仍需人工确认加入参考图。')
    }
    return true
  } catch (error) {
    if (bibleImageCandidateErrorCode(error) === 'production_queue_confirmation_required') {
      const pending = { ...state, queueConfirmationRequired: true }
      persistBibleImageRetry(pending)
      if (!options.silent) {
        bibleImagePendingConfirmation.value = pending
        bibleImageQueueConfirmOpen.value = true
      }
      setBibleImageActionError(key, bibleImageCandidateErrorMessage(error, '当前队列需要再次确认。'))
      return false
    }
    persistBibleImageRetry(state)
    setBibleImageActionError(key, bibleImageCandidateErrorMessage(error, '实体参考图任务创建失败；当前安全重试状态已保留。'))
    return false
  } finally {
    bibleImageGeneratingKey.value = ''
  }
}
const generateBibleImageCandidate = async (entityType: BibleImageEntityType, item: BibleItem) => {
  if (!project.value || bibleImageGeneratingKey.value || bibleImageBatchGenerating.value || saving.value) return
  const sourceId = item.id
  const sourceKey = bibleImageEntityKey(entityType, sourceId)
  const direction = bibleImageDirections.value[sourceKey] || ''
  const promptOverrides = { ...bibleImagePromptOverridesFor(entityType, item) }
  const savedItem = await resolveSavedBibleImageItem(entityType, item)
  if (!savedItem) {
    if (!bibleImageActionErrors.value[sourceKey]) {
      setBibleImageActionError(sourceKey, '无法定位已保存 Bible 实体，本次没有创建图片任务。')
    }
    return
  }
  const key = bibleImageEntityKey(entityType, savedItem.id)
  if (key !== sourceKey && direction) bibleImageDirections.value = { ...bibleImageDirections.value, [key]: direction }
  if (key !== sourceKey && Object.keys(promptOverrides).length) {
    bibleImagePromptOverrides.value = { ...bibleImagePromptOverrides.value, [key]: promptOverrides }
  }
  const retry = storedBibleImageRetry(project.value.id, entityType, savedItem.id)
  const state = retry || createBibleImageCandidateRetryState(
    project.value.id,
    entityType,
    savedItem.id,
    direction,
    promptOverrides,
    bibleImageRandomId(),
  )
  persistBibleImageRetry(state)
  if (state.queueConfirmationRequired) {
    bibleImagePendingConfirmation.value = state
    bibleImageQueueConfirmOpen.value = true
    return
  }
  await submitBibleImageCandidate(state, false)
}
const generateAllUnconfirmedBibleImageCandidates = async () => {
  if (!project.value || saving.value || bibleImageGeneratingKey.value || bibleImageBatchGenerating.value) return
  const currentProjectId = project.value.id
  bibleImageBatchGenerating.value = true
  bibleImageBatchProgress.value = ''
  try {
    const latestCandidates = await loadBibleImageCandidates(currentProjectId)
    if (!latestCandidates && bibleImageLoadError.value) {
      studio.notify('参考图候选状态读取失败，未开始批量生成。', 'error')
      return
    }
    const targets = bibleImageUnconfirmedTargets.value
    if (!targets.length) {
      studio.notify('当前没有需要生成参考图的未确认 Bible 实体。', 'info')
      return
    }
    const confirmed = typeof window === 'undefined' || window.confirm(
      `将为 ${targets.length} 个尚未确认参考图的 Bible 实体逐个创建图片任务。此操作不会自动入库；图片成功后仍需人工确认。是否继续？`,
    )
    if (!confirmed) return
    let created = 0
    let failed = 0
    for (const [index, target] of targets.entries()) {
      if (!project.value || project.value.id !== currentProjectId) break
      bibleImageBatchProgress.value = `${index + 1}/${targets.length} · ${target.item.name}`
      const key = bibleImageEntityKey(target.entityType, target.item.id)
      const savedItem = await resolveSavedBibleImageItem(target.entityType, target.item)
      if (!savedItem) {
        failed += 1
        continue
      }
      const savedKey = bibleImageEntityKey(target.entityType, savedItem.id)
      const direction = bibleImageDirections.value[key] || bibleImageDirections.value[savedKey] || ''
      const promptOverrides = {
        ...bibleImagePromptOverridesFor(target.entityType, target.item),
        ...bibleImagePromptOverridesFor(target.entityType, savedItem),
      }
      if (savedKey !== key && direction) bibleImageDirections.value = { ...bibleImageDirections.value, [savedKey]: direction }
      if (savedKey !== key && Object.keys(promptOverrides).length) {
        bibleImagePromptOverrides.value = { ...bibleImagePromptOverrides.value, [savedKey]: promptOverrides }
      }
      const state = createBibleImageCandidateRetryState(
        currentProjectId,
        target.entityType,
        savedItem.id,
        direction,
        promptOverrides,
        bibleImageRandomId(),
      )
      persistBibleImageRetry(state)
      const ok = await submitBibleImageCandidate(state, true, { silent: true })
      if (ok) created += 1
      else failed += 1
    }
    await loadBibleImageCandidates(currentProjectId)
    if (created) {
      studio.notify(
        failed
          ? `已创建 ${created} 个参考图任务，${failed} 个未创建；失败项已在对应卡片保留提示。`
          : `已创建 ${created} 个参考图任务；图片成功后仍需逐个确认加入参考图。`,
        failed ? 'info' : 'success',
      )
    } else if (failed) {
      studio.notify('没有创建新的参考图任务；请查看对应卡片错误。', 'error')
    }
  } finally {
    bibleImageBatchGenerating.value = false
    bibleImageBatchProgress.value = ''
  }
}
const confirmBibleImageQueue = async () => {
  const state = bibleImagePendingConfirmation.value
  if (!state) return
  await submitBibleImageCandidate(state, true)
}
const closeBibleImageQueueConfirmation = () => {
  if (bibleImageGeneratingKey.value) return
  bibleImageQueueConfirmOpen.value = false
  bibleImagePendingConfirmation.value = null
}
const abandonBibleImageRetry = (entityType: BibleImageEntityType, entityId: string) => {
  if (!project.value || bibleImageGeneratingKey.value) return
  const state = storedBibleImageRetry(project.value.id, entityType, entityId)
  if (state) clearBibleImageRetry(state)
  const key = bibleImageEntityKey(entityType, entityId)
  setBibleImageActionError(key)
  if (bibleImagePendingConfirmation.value?.entityId === entityId) closeBibleImageQueueConfirmation()
}
const promoteBibleImageOutput = async (
  candidate: BibleImageCandidateProjection | undefined,
  latestCandidate: BibleImageLatestCandidate | undefined,
  media: TaskOutputMedia,
  index: number,
) => {
  if (!project.value || !candidate || !latestCandidate?.generationTaskId || bibleImagePromotingKey.value) return
  const candidateProjectId = project.value.id
  const outputIndex = media.index ?? index
  const key = bibleImagePromotionKey(candidate, latestCandidate, media, index)
  if (media.promotedAssetId) {
    const localBibleItem = bibleItemsForType(project.value, candidate.entityType).find((item) => item.id === candidate.entityId)
    if (!localBibleItem) {
      setBibleImageActionError(bibleImageEntityKey(candidate.entityType, candidate.entityId), '该图片已入库，但当前页面无法定位对应 Bible；请刷新项目后再绑定。')
      return
    }
    if (bibleImageOutputIsBound(localBibleItem, media)) return
    if (!confirmStaleBibleImageCandidatePromotion(localBibleItem, latestCandidate)) return
    bibleImagePromotingKey.value = key
    setBibleImageActionError(bibleImageEntityKey(candidate.entityType, candidate.entityId))
    try {
      const previousAnchorAssetId = localBibleItem.anchorAssetId || null
      const previousReferenceAssetIds = [...localBibleItem.referenceAssetIds]
      localBibleItem.referenceAssetIds = [...new Set([...localBibleItem.referenceAssetIds, media.promotedAssetId])]
      const saved = await saveBibleAssets(localBibleItem, candidate.entityType)
      if (!saved) {
        localBibleItem.anchorAssetId = previousAnchorAssetId
        localBibleItem.referenceAssetIds = previousReferenceAssetIds
      }
    } finally {
      bibleImagePromotingKey.value = ''
    }
    return
  }
  const localBibleItem = bibleItemsForType(project.value, candidate.entityType).find((item) => item.id === candidate.entityId)
  if (!localBibleItem) {
    setBibleImageActionError(bibleImageEntityKey(candidate.entityType, candidate.entityId), '当前页面无法定位对应 Bible；请刷新项目后再确认。')
    return
  }
  if (!confirmStaleBibleImageCandidatePromotion(localBibleItem, latestCandidate)) return
  bibleImagePromotingKey.value = key
  setBibleImageActionError(bibleImageEntityKey(candidate.entityType, candidate.entityId))
  try {
    const asset = await repository.promoteTaskOutputToAsset(
      latestCandidate.generationTaskId,
      outputIndex,
      `${candidate.entityName}参考图`,
      undefined,
      { acceptStaleBibleImageCandidate: !latestCandidate.sourceCurrent },
    )
    if (!studio.assets.some((item) => item.id === asset.id)) studio.assets.unshift(asset)
    if (project.value?.id === candidateProjectId) {
      const bibleItem = bibleItemsForType(project.value, candidate.entityType).find((item) => item.id === candidate.entityId)
      if (!bibleItem) throw new Error('图片已保存，但当前页面无法定位对应 Bible；请刷新项目。')
      const current = bibleImageCandidates.value?.candidates.find((item) => (
        item.entityType === candidate.entityType && item.entityId === candidate.entityId
      )) || candidate
      replaceBibleImageProjection(mergeBibleImagePromotion(current, bibleItem, asset, outputIndex, latestCandidate))
    }
    let refreshWarning = ''
    try {
      // Refresh Pinia's authoritative hydrated project. Do not replace the
      // current page clone because it may contain unrelated unsaved edits.
      const refreshed = await studio.loadProject(candidateProjectId, true)
      if (project.value?.id === candidateProjectId) {
        project.value.updatedAt = refreshed.updatedAt || project.value.updatedAt
        const localBibleItem = bibleItemsForType(project.value, candidate.entityType).find((item) => item.id === candidate.entityId)
        const refreshedBibleItem = bibleItemsForType(refreshed, candidate.entityType).find((item) => item.id === candidate.entityId)
        if (localBibleItem && refreshedBibleItem) {
          localBibleItem.anchorAssetId = refreshedBibleItem.anchorAssetId
          localBibleItem.referenceAssetIds = [...refreshedBibleItem.referenceAssetIds]
        }
      }
    } catch (error) {
      refreshWarning = error instanceof Error ? error.message : '项目详情刷新失败'
    }
    studio.notify(
      refreshWarning
        ? `图片已确认入库，但项目缓存刷新失败：${refreshWarning}。请手动刷新项目后再离开当前页。`
        : `已人工确认“${asset.name}”并增量加入 ${candidate.entityName} 的参考图；原锚点未改变。`,
      refreshWarning ? 'info' : 'success',
    )
  } catch (error) {
    setBibleImageActionError(
      bibleImageEntityKey(candidate.entityType, candidate.entityId),
      bibleImageCandidateErrorMessage(error, '确认图片失败，当前候选与人工编辑仍保留。'),
    )
  } finally {
    bibleImagePromotingKey.value = ''
  }
}
const handleVisualRulesApplied = async (result: VisualRulesDraftApplyResult) => {
  if (!project.value || result.projectId !== project.value.id) return
  project.value.bible.negativePrompt = result.applied.negativePrompt
  project.value.bible.colorScript = result.applied.colorScript
  project.value.continuitySummary = result.applied.continuitySummary
  project.value.updatedAt = result.updatedAt || project.value.updatedAt
  invalidateProjectPromptsFromTranslation()
  try {
    const refreshed = await studio.loadProject(result.projectId, true)
    if (refreshed) {
      project.value = clone(refreshed)
      translationGlossaryText.value = formatTranslationGlossary(project.value.translationGlossary)
    }
    studio.notify('已采用并保存三项全剧视觉规则；色彩脚本与连续性摘要保持独立。')
  } catch {
    studio.notify('三项视觉规则已保存，但项目列表刷新失败；当前页面仍显示服务端已采用值。', 'error')
  }
}
const handleScriptBibleState = (state: { pendingCount: number; hasBlock: boolean }) => {
  scriptBiblePendingCount.value = state.pendingCount
  scriptBibleHasBlock.value = state.hasBlock
}
const scriptBibleCanonicalEntries = (result: ScriptBibleApplyResult) => {
  const rows = [
    ...result.created.characters.map((item) => ({ scopeType: 'character' as const, scopeId: item.id })),
    ...result.created.scenes.map((item) => ({ scopeType: 'scene' as const, scopeId: item.id })),
    ...result.created.props.map((item) => ({ scopeType: 'prop' as const, scopeId: item.id })),
    ...result.alreadyExists.characters.map((item) => ({ scopeType: 'character' as const, scopeId: item.id })),
    ...result.alreadyExists.scenes.map((item) => ({ scopeType: 'scene' as const, scopeId: item.id })),
    ...result.alreadyExists.props.map((item) => ({ scopeType: 'prop' as const, scopeId: item.id })),
  ]
  const seen = new Set<string>()
  return rows.filter((row) => {
    const key = `${row.scopeType}:${row.scopeId}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}
const handleScriptBibleApplied = async (result: ScriptBibleApplyResult) => {
  const currentProject = project.value?.id === result.projectId ? project.value : null
  if (currentProject && result.updatedAt) currentProject.updatedAt = result.updatedAt
  const appended = currentProject ? mergeScriptBibleApplyIntoBible(currentProject.bible, result) : 0
  translationReviewRefreshKey.value += currentProject ? 1 : 0
  let refreshWarning = ''
  let canonicalMessage = ''
  let canonicalWarning = ''
  try {
    // Force-hydrate the authoritative store even when the panel discarded its
    // local response after a project/version change. Keep the page clone so
    // any unrelated unsaved edits remain intact.
    const refreshed = await studio.loadProject(result.projectId, true)
    if (currentProject && project.value?.id === result.projectId) {
      currentProject.updatedAt = refreshed.updatedAt || currentProject.updatedAt
      currentProject.translationGlossary = { ...refreshed.translationGlossary }
      translationGlossaryText.value = formatTranslationGlossary(currentProject.translationGlossary)
    }
  } catch (error) {
    refreshWarning = error instanceof Error ? error.message : '项目详情刷新失败'
  }
  const canonicalEntries = currentProject && project.value?.id === result.projectId
    ? scriptBibleCanonicalEntries(result)
    : []
  if (currentProject && canonicalEntries.length && project.value?.id === result.projectId) {
    try {
      const canonicalResult = await studio.ensureCanonicalGlossary(currentProject, { entries: canonicalEntries })
      if (project.value?.id === result.projectId) {
        applyCanonicalGlossaryResult(canonicalResult)
        const generated = canonicalResult.summary.generated
        const failed = canonicalResult.summary.failed + canonicalResult.summary.conflict
        canonicalMessage = generated ? `；已自动补全 ${generated} 个 canonical 术语` : ''
        canonicalWarning = failed ? `；${failed} 个 canonical 术语未自动补全，可稍后重试或人工填写` : ''
      }
    } catch (error) {
      canonicalWarning = `；canonical 术语自动补全暂未完成：${error instanceof Error ? error.message : '本地 Qwen 暂不可用'}`
      canonicalGlossaryNotice.value = { kind: 'warning', message: canonicalWarning.slice(1) }
    }
  }
  studio.notify(
    refreshWarning
      ? `Bible 建议已由服务端确认，但项目事实刷新失败：${refreshWarning}。请手动刷新后再继续。`
      : `Bible 建议已由服务端确认：新增 ${result.counts.created} 项，复用 ${result.counts.alreadyExists} 项${appended ? `；当前页面增量加入 ${appended} 项` : ''}${canonicalMessage}${canonicalWarning}。`,
    refreshWarning || canonicalWarning ? 'info' : 'success',
  )
}
const openScriptBibleGate = (requiresRebuild: boolean, rebuildConfirmed: boolean) => {
  scriptBibleGateRequiresRebuild.value = requiresRebuild
  scriptBibleGateRebuildConfirmed.value = rebuildConfirmed
  scriptBibleSkipConfirmOpen.value = false
  scriptBibleGateOpen.value = true
}
const closeScriptBibleGate = () => {
  scriptBibleGateOpen.value = false
  scriptBibleSkipConfirmOpen.value = false
  scriptBibleGateRequiresRebuild.value = false
  scriptBibleGateRebuildConfirmed.value = false
}
const reviewScriptBibleCandidates = async () => {
  closeScriptBibleGate()
  tab.value = 'bible'
  const refreshedState = await scriptBiblePanel.value?.refresh()
  if (refreshedState) handleScriptBibleState(refreshedState)
  await scriptBiblePanel.value?.focus()
}
const requestSkipScriptBible = () => {
  scriptBibleGateOpen.value = false
  scriptBibleSkipConfirmOpen.value = true
}
const closeRebuildConfirmation = () => {
  rebuildConfirmOpen.value = false
  rebuildContinueWithoutScriptBible.value = false
}
const resetBreakdownBypassState = () => {
  closeScriptBibleGate()
  closeRebuildConfirmation()
}
const clearReplacedEpisodeRetryState = (episodeIds: string[]) => {
  for (const episodeId of episodeIds) {
    const prefix = wholeEpisodeRetryStoragePrefix(episodeId)
    const matchingKeys: string[] = []
    for (let index = 0; index < sessionStorage.length; index += 1) {
      const key = sessionStorage.key(index)
      if (key === prefix || key?.startsWith(`${prefix}_`)) matchingKeys.push(key)
    }
    for (const key of matchingKeys) sessionStorage.removeItem(key)
  }
}
const runBreakdown = async (discardExistingProduction: boolean, continueWithoutScriptBible = false) => {
  if (!project.value?.masterScript.trim()) { studio.notify('请先粘贴完整剧本', 'error'); return }
  if (!productionCapability.value || videoShotMinDurationSeconds.value === undefined || videoShotMaxDurationSeconds.value === undefined) { studio.notify('尚未读取正式视频时长配置，请刷新后重试', 'error'); return }
  const replacedEpisodeIds = project.value.episodes.map((episode) => episode.id)
  breakdownOptions.value.discardExistingProduction = discardExistingProduction
  breakdownLoading.value = true
  try {
    project.value = clone(await studio.breakdownScript(project.value, breakdownOptions.value, continueWithoutScriptBible))
    clearReplacedEpisodeRetryState(replacedEpisodeIds)
    selectedEpisodeId.value = project.value.episodes[0]?.id || ''
    episodeAssemblies.value = {}; completedEpisodeAssemblies.value = {}; episodeIdempotencyKeys.value = {}
    wholeEpisodeOpen.value = false; wholeEpisodeId.value = ''; wholeEpisodeExtendedConfirmed.value = false; wholeEpisodeIdempotencyConflict.value = ''
    breakdownOptions.value.replaceExisting = false
    breakdownOptions.value.discardExistingProduction = false
    closeRebuildConfirmation()
    savedMasterScript.value = project.value.masterScript
    translationReviewRefreshKey.value += 1
    tab.value = 'episodes'
    try {
      await Promise.all([
        studio.refreshWorkflows(),
        loadEpisodeAssemblies(project.value.id),
        loadReleaseBundles(project.value.id),
      ])
    } catch {
      studio.notify('剧集已成功重建，但部分生产历史面板刷新失败；重新进入页面即可恢复。', 'info')
    }
  }
  catch (error) {
    if (productionErrorCode(error) === 'script_bible_confirmation_required') {
      scriptBiblePendingCount.value = Math.max(scriptBiblePendingCount.value, scriptBiblePendingCountFromError(error))
      scriptBibleHasBlock.value = true
      closeRebuildConfirmation()
      openScriptBibleGate(project.value.episodes.length > 0, discardExistingProduction)
      studio.notify('服务端检测到仍有未确认的 Bible 建议，尚未拆解或修改任何剧集。', 'info')
      return
    }
    studio.notify(error instanceof Error ? error.message : '剧本拆解失败', 'error')
  }
  finally { breakdownLoading.value = false; breakdownOptions.value.discardExistingProduction = false }
}
const breakdown = async () => {
  resetBreakdownBypassState()
  if (!project.value?.masterScript.trim()) { studio.notify('请先粘贴完整剧本', 'error'); return }
  if (!productionCapability.value || videoShotMinDurationSeconds.value === undefined || videoShotMaxDurationSeconds.value === undefined) { studio.notify('尚未读取正式视频时长配置，请刷新后重试', 'error'); return }
  if (masterScriptNeedsScriptBibleRefresh(project.value.masterScript, savedMasterScript.value)) {
    const saved = await save(false)
    if (!saved) return
    const refreshedState = await scriptBiblePanel.value?.refresh()
    if (!refreshedState) {
      studio.notify('剧本已保存，但无法读取对应的最新 Bible 候选；本次没有开始拆解。', 'error')
      return
    }
    handleScriptBibleState(refreshedState)
  }
  if (scriptBiblePendingCount.value > 0) {
    openScriptBibleGate(project.value.episodes.length > 0, false)
    return
  }
  if (project.value.episodes.length) {
    if (!breakdownOptions.value.replaceExisting) { studio.notify('项目已有剧集；如需重建，请显式勾选替换现有剧集', 'error'); return }
    rebuildConfirmOpen.value = true
    return
  }
  await runBreakdown(false)
}
const confirmProjectRebuild = async () => {
  const continueWithoutScriptBible = rebuildContinueWithoutScriptBible.value
  closeRebuildConfirmation()
  await runBreakdown(true, continueWithoutScriptBible)
}
const confirmSkipScriptBible = async () => {
  const requiresRebuild = scriptBibleGateRequiresRebuild.value
  const rebuildConfirmed = scriptBibleGateRebuildConfirmed.value
  closeScriptBibleGate()
  if (rebuildConfirmed) {
    await runBreakdown(true, true)
    return
  }
  if (requiresRebuild) {
    rebuildContinueWithoutScriptBible.value = true
    rebuildConfirmOpen.value = true
    return
  }
  await runBreakdown(false, true)
}
const applyCanonicalGlossaryResult = (result: Awaited<ReturnType<typeof studio.ensureCanonicalGlossary>>) => {
  if (!project.value || result.projectId !== project.value.id) return
  if (result.updatedAt) project.value.updatedAt = result.updatedAt
  project.value.translationGlossary = { ...result.translationGlossary }
  translationGlossaryText.value = formatTranslationGlossary(project.value.translationGlossary)
  if (result.summary.generated > 0) {
    invalidateProjectPromptsFromTranslation()
    translationReviewRefreshKey.value += 1
  }
  const details = [
    result.summary.generated ? `自动新增 ${result.summary.generated} 个` : '',
    result.summary.reused ? `复用 ${result.summary.reused} 个已有映射` : '',
    result.summary.conflict ? `${result.summary.conflict} 个冲突未覆盖` : '',
    result.summary.failed ? `${result.summary.failed} 个生成失败` : '',
  ].filter(Boolean).join('，')
  canonicalGlossaryNotice.value = {
    kind: result.summary.failed || result.summary.conflict ? 'warning' : 'success',
    message: details || '没有需要补写的 canonical 术语。',
  }
}
const applyBibleVisualTranslationResult = (
  result: Awaited<ReturnType<typeof studio.ensureCanonicalGlossary>>,
  scopeType: typeof bibleScopeBySection[keyof typeof bibleScopeBySection],
  scopeId: string,
  name: string,
  sourceText: string,
) => {
  const entry = result.visualTranslations.find((row) => row.scopeType === scopeType && row.scopeId === scopeId)
  const status = entry?.status
  if (status === 'generated') {
    bibleVisualTranslationNotice.value = { kind: 'success', message: `“${name}”的视觉锚点英文候选已生成，中文原文未改动。请在下方审核后点击“确认译文”；确认前不会进入正式提示词。` }
  } else if (status === 'reused') {
    const confirmed = entry?.translation?.status === 'confirmed'
    bibleVisualTranslationNotice.value = {
      kind: 'success',
      message: confirmed
        ? `“${name}”已复用当前中文视觉锚点的人工确认英文译文。`
        : `“${name}”已复用现有英文候选；请在下方继续审核并确认。`,
    }
  } else if (status === 'not_required') {
    bibleVisualTranslationNotice.value = { kind: 'success', message: `“${name}”的视觉锚点已是英文，无需创建翻译任务。` }
  } else if (status === 'failed' || status === 'conflict') {
    bibleVisualTranslationNotice.value = { kind: 'warning', message: `“${name}”已保存，但视觉锚点英文候选${status === 'conflict' ? '因源文变化未采用' : '生成失败'}。中文原文保持不变，请在下方点击“${entry?.translation ? '重新翻译' : '翻译此字段'}”。${entry?.message ? ` ${entry.message}` : ''}` }
  } else if (containsCjk(sourceText)) {
    bibleVisualTranslationNotice.value = { kind: 'warning', message: `“${name}”已保存，但服务端未返回视觉锚点候选状态。中文原文保持不变，请在下方点击“翻译此字段”。` }
  } else {
    bibleVisualTranslationNotice.value = { kind: 'success', message: `“${name}”的视觉锚点已是英文，无需翻译。` }
  }
  return entry
}
const backfillMissingCanonicalGlossary = async () => {
  if (!project.value || canonicalBackfillLoading.value) return
  const glossary = translationGlossaryValidation.value
  if (glossary.error) { studio.notify(glossary.error, 'error'); return }
  if (canonicalTargetCollisionError.value) {
    canonicalGlossaryNotice.value = { kind: 'warning', message: canonicalTargetCollisionError.value }
    studio.notify(canonicalTargetCollisionError.value, 'error')
    return
  }
  const previousGlossary = JSON.stringify(Object.entries(project.value.translationGlossary || {}).sort(([left], [right]) => left.localeCompare(right)))
  const nextGlossary = JSON.stringify(Object.entries(glossary.value).sort(([left], [right]) => left.localeCompare(right)))
  if (previousGlossary !== nextGlossary) {
    canonicalGlossaryNotice.value = { kind: 'warning', message: '检测到术语表有未保存的人工修改。请先保存项目，再自动补全剩余缺失术语。' }
    studio.notify('请先保存人工修改的 canonical 术语，再自动补全缺失项。', 'info')
    return
  }
  if (!missingCanonicalBibleNames.value.length) {
    canonicalGlossaryNotice.value = { kind: 'success', message: '人物、场景和道具名称均已有 canonical 英文，或名称本身无需翻译。' }
    return
  }
  canonicalBackfillLoading.value = true
  canonicalGlossaryNotice.value = null
  try {
    const result = await studio.ensureCanonicalGlossary(project.value, { includeAllMissing: true })
    applyCanonicalGlossaryResult(result)
    studio.notify(result.summary.failed ? '部分英文术语生成失败，可稍后重试或人工填写。' : '缺失的 Bible 英文术语已自动写入并保存', result.summary.failed ? 'info' : 'success')
  } catch (error) {
    canonicalGlossaryNotice.value = { kind: 'warning', message: `自动补全暂未完成：${error instanceof Error ? error.message : '本地 Qwen 暂不可用'}` }
    studio.notify('术语自动补全失败，可稍后重试或人工填写。', 'info')
  } finally { canonicalBackfillLoading.value = false }
}
const addBibleItem = async () => {
  if (!project.value || !bibleDraft.value.name.trim()) { studio.notify('请填写设定名称', 'error'); return }
  if (bibleCreatePhase.value !== 'idle') return
  bibleCreateError.value=''
  if (bibleDraftStale.value) { studio.notify('参考图或草稿语言已变化，请重新生成草稿，或明确放弃旧生成记录', 'error'); return }
  if (!bibleDraft.value.visualPrompt.trim()) {
    studio.notify('请填写视觉锚点；可使用中文或英文，只写跨镜头不可变化的视觉事实', 'error')
    return
  }
  const name = bibleDraft.value.name.trim()
  const normalizedName=name.normalize('NFKC').trim().toLocaleLowerCase('zh-CN')
  const existingName=[...project.value.bible.characters,...project.value.bible.scenes,...project.value.bible.props].find((entry)=>entry.name.normalize('NFKC').trim().toLocaleLowerCase('zh-CN')===normalizedName)
  if (existingName) { studio.notify(`项目 Bible 已存在“${existingName.name}”，名称在人物、场景和道具之间也必须唯一`, 'error'); return }
  const section = bibleSection.value
  const temporaryId = `${section.slice(0,-1)}-${Date.now()}`
  const selectedDraftAsset=bibleDraftAsset.value
  if(selectedDraftAsset&&!bibleDraftGenerateResult.value&&(!selectedDraftAsset.checksum||!selectedDraftAsset.updatedAt||!selectedDraftAsset.metadataSha256)){bibleCreateError.value='所选素材缺少最新校验快照，请关闭弹窗并重新打开素材目录后再创建。';studio.notify(bibleCreateError.value,'error');return}
  const sourceSnapshot=bibleDraftGenerateResult.value?.sourceAssets[0]
  if(bibleDraftGenerateResult.value&&(!sourceSnapshot?.id||!sourceSnapshot.checksum||!sourceSnapshot.updatedAt||!sourceSnapshot.metadataSha256||!bibleDraftGenerateResult.value.sourceMetadataSha256)){bibleCreateError.value='草稿缺少完整素材来源快照，请重新生成后再创建。';studio.notify(bibleCreateError.value,'error');return}
  const draftProvenance=bibleDraftGenerateResult.value&&!bibleDraftStale.value&&sourceSnapshot?{
    asset_draft_provenance:{evidence_basis:'asset_metadata',pixel_vision_used:false,reference_mode:'single_reference',entity_type:assetTypeForBible(),asset_id:sourceSnapshot.id,asset_checksum:sourceSnapshot.checksum,asset_updated_at:sourceSnapshot.updatedAt||'',asset_metadata_sha256:sourceSnapshot.metadataSha256||'',source_metadata_sha256:bibleDraftGenerateResult.value.sourceMetadataSha256||''}
  }:selectedDraftAsset?.metadataSha256?{asset_draft_provenance:{evidence_basis:'asset_metadata',pixel_vision_used:false,reference_mode:'single_reference',entity_type:assetTypeForBible(),asset_id:selectedDraftAsset.id,asset_checksum:selectedDraftAsset.checksum||'',asset_updated_at:selectedDraftAsset.updatedAt||'',asset_metadata_sha256:selectedDraftAsset.metadataSha256,source_metadata_sha256:selectedDraftAsset.metadataSha256}}:{}
  const [anchorAssetId, ...referenceAssetIds] = bibleDraft.value.referenceAssetIds
  const item: BibleItem = { id: temporaryId, name, description: bibleDraft.value.description, visualPrompt: bibleDraft.value.visualPrompt.trim(), anchorAssetId: anchorAssetId || null, referenceAssetIds, locked: true, seed: Math.floor(100000 + Math.random() * 899999), metadata:draftProvenance }
  project.value.bible[section].push(item)
  bibleCreatePhase.value = 'saving'
  canonicalGlossaryNotice.value = null
  bibleVisualTranslationNotice.value = null
  const saved = await save()
  if (!saved) {
    const unsavedIndex = project.value?.bible[section].findIndex((entry) => entry.id === temporaryId) ?? -1
    if (unsavedIndex >= 0) project.value?.bible[section].splice(unsavedIndex, 1)
    bibleCreateError.value='设定未创建。素材说明或校验快照可能已变化，也可能是保存服务暂不可用；临时字段仍保留，请刷新素材说明或直接重试。'
    bibleCreatePhase.value = 'idle'
    return
  }
  const storedItem = [...saved.bible[section]].reverse().find((entry) => entry.name.trim() === name)
  try {
    if (!storedItem) throw new Error('保存后未找到对应 Bible 实体，请刷新页面后重试翻译')
    bibleCreatePhase.value = 'canonicalizing'
    const scopeType = bibleScopeBySection[section]
    const result = await studio.ensureCanonicalGlossary(saved, {
      entries: [{ scopeType, scopeId: storedItem.id }],
      ensureVisualIdentityTranslation: true,
    })
    applyCanonicalGlossaryResult(result)
    const visualEntry = applyBibleVisualTranslationResult(result, scopeType, storedItem.id, name, storedItem.visualPrompt)
    translationReviewRefreshKey.value += 1
    if (visualEntry?.status === 'generated') studio.notify('设定已保存；视觉锚点英文候选已生成，需人工审核确认。')
    else if (visualEntry?.status === 'failed' || visualEntry?.status === 'conflict') studio.notify('设定已保存；视觉锚点英文候选暂未生成，可在审核面板重试。', 'info')
  } catch (error) {
    canonicalGlossaryNotice.value = { kind: 'warning', message: `“${name}”已保存；自动术语生成暂未完成：${error instanceof Error ? error.message : '本地 Qwen 暂不可用'}` }
    bibleVisualTranslationNotice.value = containsCjk(item.visualPrompt)
      ? { kind: 'warning', message: `“${name}”已保存；视觉锚点仍保留中文原文，英文候选暂未生成。请在下方审核面板点击“翻译此字段”。` }
      : { kind: 'success', message: `“${name}”的视觉锚点已是英文，无需翻译。` }
    translationReviewRefreshKey.value += 1
    studio.notify('设定已保存；自动英文处理未全部完成，可稍后在对应面板重试。', 'info')
  } finally {
    bibleDraft.value = { name: '', description: '', visualPrompt: '', referenceAssetIds: [] }
    resetBibleDraftAssistant()
    addBibleOpen.value = false
    bibleCreatePhase.value = 'idle'
  }
}
const removeBibleItem = (id: string) => { if (!project.value) return; project.value.bible[bibleSection.value] = project.value.bible[bibleSection.value].filter((item) => item.id !== id) }
const addEpisode = async () => {
  if (!project.value) return
  const next = project.value.episodes.length + 1; if (!episodeDraft.value.title) episodeDraft.value.title = `第 ${next} 集`
  await studio.addEpisode(project.value, episodeDraft.value); addEpisodeOpen.value = false; episodeDraft.value = { title: '', synopsis: '', script: '' }
}
const removeEpisode = async (id: string) => { if (project.value && window.confirm('删除这集及其全部分镜？此操作不会删除共享素材。')) await studio.deleteEpisode(project.value, id) }
const assetThumb = (item: BibleItem) => studio.assets.find((asset) => bibleAssetIds(item).includes(asset.id))?.thumbnail
const assetName = (assetId:string) => studio.assets.find((asset)=>asset.id===assetId)?.name || assetId.slice(0,8)
const toggleBibleAsset=(item:BibleItem,assetId:string)=>{
  if(item.anchorAssetId===assetId){studio.notify('该素材当前是身份锚点；请先在参考图列表中明确更换或解除锚点。','info');return}
  item.referenceAssetIds=item.referenceAssetIds.includes(assetId)?item.referenceAssetIds.filter((id)=>id!==assetId):[...item.referenceAssetIds,assetId]
}
const setBibleAnchor=(item:BibleItem,assetId:string|null)=>{
  const previous=item.anchorAssetId||null
  item.anchorAssetId=assetId
  item.referenceAssetIds=[...new Set([
    ...item.referenceAssetIds.filter((id)=>id!==assetId),
    ...(previous&&previous!==assetId?[previous]:[]),
  ])]
}
const isProjectExpectedUpdatedAtConflict = (error: unknown) => {
  const message = error instanceof Error ? error.message : String(error || '')
  const code = String((error as { response?: { data?: { error?: { code?: unknown } } } })?.response?.data?.error?.code || '')
  return code.includes('stale')
    || code.includes('updated_at')
    || message.includes('expected_updated_at')
    || message.includes('已被其他操作更新')
}
const saveBibleAssets = async (item: BibleItem, entityType = assetTypeForBible()): Promise<boolean> => {
  if (!project.value || saving.value) return false
  if (!bibleImagePersistedId(item.id)) {
    bibleAssetPickerId.value = ''
    await save(true)
    return true
  }
  const projectId = project.value.id
  const anchorAssetId = item.anchorAssetId || null
  const referenceAssetIds = [...new Set(item.referenceAssetIds.filter((id) => id && id !== anchorAssetId))]
  const locked = item.locked
  saving.value = true
  try {
    let saved: SeriesProject | null = null
    let lastError: unknown = null
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        // Bypass the store's in-flight detail cache here: this is a narrow
        // merge operation and must start from the latest server snapshot.
        const fresh = await repository.getProject(projectId)
        const target = bibleItemsForType(fresh, entityType).find((candidate) => candidate.id === item.id)
        if (!target) throw new Error('服务端已保存 Bible 中没有该实体；请刷新页面后再绑定参考图。')
        target.anchorAssetId = anchorAssetId
        target.referenceAssetIds = referenceAssetIds
        target.locked = locked
        saved = await studio.saveProject(fresh)
        break
      } catch (error) {
        lastError = error
        if (!isProjectExpectedUpdatedAtConflict(error) || attempt === 2) throw error
      }
    }
    if (!saved) throw lastError instanceof Error ? lastError : new Error('参考图绑定保存失败')
    const savedItem = bibleItemsForType(saved, entityType).find((candidate) => candidate.id === item.id)
    if (project.value?.id === projectId) {
      project.value.updatedAt = saved.updatedAt || project.value.updatedAt
      const localItem = bibleItemsForType(project.value, entityType).find((candidate) => candidate.id === item.id)
      if (localItem && savedItem) {
        localItem.anchorAssetId = savedItem.anchorAssetId || null
        localItem.referenceAssetIds = [...savedItem.referenceAssetIds]
        localItem.locked = savedItem.locked
      }
      await loadBibleImageCandidates(projectId)
    }
    bibleAssetPickerId.value = ''
    studio.notify('参考图绑定已保存；候选历史保持不变。')
    return true
  } catch (error) {
    studio.notify(error instanceof Error ? error.message : '参考图绑定保存失败', 'error')
    return false
  } finally {
    saving.value = false
  }
}
const episodeDuration = (index: number) => project.value?.episodes[index].shots.reduce((sum, shot) => sum + (shot.mediaType === 'video' ? shot.duration : 0), 0) || 0
const episodeVideoCount = (episode: Episode) => episode.shots.filter((shot) => shot.mediaType === 'video').length
const episodeImageCount = (episode: Episode) => episode.shots.filter((shot) => shot.mediaType === 'image').length
const episodeProductionLabel = (episode: Episode) => episodeVideoCount(episode) && episodeImageCount(episode) ? 'H3 视频 + SDXL 图片' : episodeImageCount(episode) ? 'SDXL 图片' : 'MiniMax H3 视频'
const assetTypeForBible = () => ({ characters: 'character', scenes: 'scene', props: 'prop' } as const)[bibleSection.value]
const bibleAssetTypeLabel = (asset:Asset) => ({character:'人物',scene:'场景',prop:'道具',image:'未分类图片'} as Record<string,string>)[asset.type] || '其他'
const bibleDraftAssetUnavailableReason=(asset:Asset)=>asset.isActive===false?'素材已停用，不能用于新草稿':asset.type!==assetTypeForBible()?`${bibleAssetTypeLabel(asset)}素材不能用于当前设定`:''
const isBibleDraftAssetCompatible = (asset:Asset) => !bibleDraftAssetUnavailableReason(asset)
const bibleDraftAsset = computed(() => bibleAssetCatalog.value.find((asset)=>asset.id===bibleDraft.value.referenceAssetIds[0]))
const filteredBibleAssetCatalog = computed(() => {
  const query=bibleAssetSearch.value.trim().toLocaleLowerCase('zh-CN')
  return bibleAssetCatalog.value.filter((asset)=>{
    if(!['character','scene','prop','image'].includes(asset.type))return false
    if(bibleAssetCategory.value==='compatible'&&!isBibleDraftAssetCompatible(asset))return false
    if(bibleAssetCategory.value!=='compatible'&&bibleAssetCategory.value!=='all'&&asset.type!==bibleAssetCategory.value)return false
    return !query||`${asset.name} ${asset.description} ${asset.promptHint||''} ${asset.tags.join(' ')}`.toLocaleLowerCase('zh-CN').includes(query)
  })
})
const resetBibleDraftAssistant = () => {
  bibleDraftDirty.value={name:false,description:false,visualPrompt:false};bibleDraftProposal.value={};bibleDraftLanguage.value='zh';bibleDraftGenerating.value=false;bibleDraftGenerateError.value='';bibleDraftGenerateResult.value=null;bibleDraftStale.value=false;bibleDraftAttempt.value=0;bibleCreateError.value='';bibleAssetSearch.value='';bibleAssetCategory.value='compatible';bibleAssetPreview.value=null
}
const openAddBibleModal = async () => {
  if(bibleCreatePhase.value!=='idle')return
  bibleDraft.value={name:'',description:'',visualPrompt:'',referenceAssetIds:[]};resetBibleDraftAssistant();addBibleOpen.value=true
  bibleAssetCatalogLoading.value=true;bibleAssetCatalogError.value=''
  try{bibleAssetCatalog.value=(await repository.getAssets({})).filter((asset)=>['character','scene','prop','image'].includes(asset.type))}
  catch(error){bibleAssetCatalogError.value=error instanceof Error?error.message:'素材库加载失败'}finally{bibleAssetCatalogLoading.value=false}
}
const closeAddBibleModal=()=>{if(bibleCreatePhase.value!=='idle'||bibleDraftGenerating.value)return;addBibleOpen.value=false;bibleAssetPreview.value=null}
const markBibleDraftDirty=(field:BibleDraftField)=>{bibleDraftDirty.value[field]=true}
const markBibleDraftStale=()=>{if(bibleDraftGenerateResult.value)bibleDraftStale.value=true;bibleDraftGenerateError.value=''}
const setBibleDraftLanguage=(language:BibleDraftLanguage)=>{if(language===bibleDraftLanguage.value)return;bibleDraftLanguage.value=language;markBibleDraftStale()}
const selectBibleDraftAsset=(asset:Asset)=>{
  if(!isBibleDraftAssetCompatible(asset)){studio.notify(`“${asset.name}”不可选择：${bibleDraftAssetUnavailableReason(asset)}`, 'info');return}
  if(bibleDraft.value.referenceAssetIds[0]===asset.id)return
  const hasManualEdits=(Object.keys(bibleDraftDirty.value) as BibleDraftField[]).some((field)=>bibleDraftDirty.value[field]&&Boolean(bibleDraft.value[field].trim()))
  if(hasManualEdits&&!window.confirm(`切换到“${asset.name}”会用该素材已保存的名称、基础描述和视觉锚点替换当前手工字段。是否继续？`))return
  bibleDraft.value={name:asset.name,description:asset.description,visualPrompt:asset.promptHint||'',referenceAssetIds:[asset.id]}
  bibleDraftDirty.value={name:false,description:false,visualPrompt:false};bibleDraftProposal.value={};bibleDraftGenerateResult.value=null;bibleDraftStale.value=false;bibleDraftGenerateError.value='';bibleDraftAttempt.value=0
  if(!asset.promptHint)studio.notify('已带入素材名称和基础描述；该素材缺少视觉锚点，请手工补充或先到素材库完善说明。','info')
}
const applyBibleDraftCandidateField=(rawField:BibleDraftField|string)=>{const field=rawField as BibleDraftField;const candidate=bibleDraftProposal.value[field];if(candidate===undefined)return;bibleDraft.value[field]=candidate;bibleDraftDirty.value[field]=false;const next={...bibleDraftProposal.value};delete next[field];bibleDraftProposal.value=next}
const applyAllBibleDraftCandidates=()=>{for(const field of Object.keys(bibleDraftProposal.value) as BibleDraftField[])applyBibleDraftCandidateField(field)}
const clearStaleBibleDraftProvenance=()=>{bibleDraftGenerateResult.value=null;bibleDraftProposal.value={};bibleDraftStale.value=false;bibleDraftGenerateError.value='';bibleDraftDirty.value={name:true,description:true,visualPrompt:true}}
const generateBibleDraftFromAsset=async()=>{
  if(!project.value||bibleDraftGenerating.value)return
  const asset=bibleDraftAsset.value
  if(!asset){bibleDraftGenerateError.value='请先选择一张与当前设定类别一致的参考图';return}
  if(!isBibleDraftAssetCompatible(asset)){bibleDraftGenerateError.value='参考图类别与当前 Bible 设定不一致，请重新选择';return}
  bibleDraftGenerating.value=true;bibleDraftGenerateError.value=''
  try{
    const result=await studio.generateBibleDraftFromAssets(project.value.id,{entityType:assetTypeForBible(),outputLanguage:bibleDraftLanguage.value,asset,attempt:++bibleDraftAttempt.value})
    const candidates:Record<BibleDraftField,string>={name:result.draft.name,description:result.draft.description,visualPrompt:result.draft.visualPrompt}
    const pending:Partial<Record<BibleDraftField,string>>={}
    for(const field of Object.keys(candidates) as BibleDraftField[]){const candidate=candidates[field].trim();if(!candidate)continue;if(bibleDraftDirty.value[field]&&bibleDraft.value[field]!==candidate)pending[field]=candidate;else{bibleDraft.value[field]=candidate;bibleDraftDirty.value[field]=false}}
    bibleDraftProposal.value=pending;bibleDraftGenerateResult.value=result;bibleDraftStale.value=false
    if(Object.keys(pending).length)studio.notify('草稿已生成；人工编辑字段未覆盖，请确认是否采用候选。','info')
    else studio.notify('已根据素材库说明补充设定草稿')
  }catch(error){bibleDraftGenerateError.value=error instanceof Error?error.message:'草稿生成失败，请重试'}finally{bibleDraftGenerating.value=false}
}
</script>

<template>
  <div v-if="loading" class="page-loading"><div class="loader-ring"></div><span>正在装载制作圣经…</span></div>
  <div v-else-if="isNew" class="new-project-page page-stack">
    <input ref="coverInput" type="file" :accept="PROJECT_COVER_ACCEPT" hidden @change="selectProjectCover">
    <button class="back-link" @click="router.push('/projects')"><ArrowLeft :size="15"/>返回连续剧项目</button>
    <section class="new-project-heading"><span class="eyebrow">CREATE A SERIES UNIVERSE</span><h2>新建连续短剧</h2><p>先确定故事和视觉世界。角色、场景与道具可在下一步持续补充，并自动贯穿无限集数。</p></section>
    <section class="new-project-layout">
      <div class="panel new-project-form">
        <div class="form-section"><div class="section-number">01</div><div class="section-content"><h3>故事基础</h3><p>为连续剧建立可识别的核心概念；下方徽标说明每个字段在当前真实生产链路中的作用。</p><SeriesConceptDraftWorkbench :style-hint="newProjectStyleHint" @accept="handleSeriesConceptAccepted"/><label class="field"><span class="impact-label-row">项目名称 * <PromptImpactBadge kind="label" title="只用于项目、任务和页面识别，不进入 H3 提示词。"/></span><input v-model="newProject.title" placeholder="例如：听雨不归人" maxlength="60"><small>{{ newProject.title.length }}/60</small></label><label class="field"><span class="impact-label-row">一句话梗概 <PromptImpactBadge kind="indirect" title="用于项目说明；当前真实单镜提示词不会原样引用该字段。"/></span><textarea v-model="newProject.logline" rows="3" placeholder="主角是谁、想要什么、面临怎样的阻力？"></textarea></label><label class="field"><span class="impact-label-row">完整剧本（可稍后补充） <PromptImpactBadge kind="indirect" title="只作为剧集与分镜拆解源；拆解完成后不会自动复制到每个提示词。"/></span><textarea v-model="newProject.masterScript" rows="8" placeholder="粘贴含“第N集”“镜头N”和角色对白的完整剧本，创建后可自动拆成无限剧集与分镜。"></textarea><small>剧本只提交到你的 Django 服务；拆解输入预算由后端配置决定，前端不会静默截断。</small></label><PromptWritingGuide scope="project"/></div></div>
        <div class="form-section"><div class="section-number">02</div><div class="section-content"><div class="impact-heading"><h3>选择视觉风格</h3><PromptImpactBadge kind="prompt" title="风格的 visual prompt 会进入每个镜头，风格负面规则也会合并到负面提示词。"/><PromptImpactBadge kind="continuity"/></div><p>风格决定默认镜头语言、色彩与提示词前缀。</p><div class="new-style-grid"><button v-for="style in studio.styles" :key="style.id" :class="{active:newProject.styleId===style.id}" :style="{background:style.gradient,'--accent':style.accent}" @click="newProject.styleId=style.id"><i class="check"><Check :size="12"/></i><span>{{ style.englishName }}</span><strong>{{ style.name }}</strong><small>{{ style.tags.join(' · ') }}</small></button></div></div></div>
        <div class="form-section"><div class="section-number">03</div><div class="section-content"><h3>生产规格</h3><p>本地 H3 工作流按 24 fps 和单任务安全策略提交；每个分镜仍可单独切换工作流。</p><div class="form-grid three"><label class="field"><span class="impact-label-row">画幅比例 <PromptImpactBadge kind="production" title="决定官方分辨率档位的宽高方向，并记录到连续性生产格式。"/><PromptImpactBadge kind="continuity"/></span><select v-model="newProject.aspectRatio"><option value="9:16">9:16 · 竖屏短剧</option><option value="16:9">16:9 · 横屏影视</option><option value="1:1">1:1 · 方形内容</option></select></label><label class="field"><span class="impact-label-row">项目默认分辨率 <PromptImpactBadge kind="production" title="仅在生产档位选择“项目默认”时生效；0.1–2.0 MP 档位会覆盖该尺寸。"/></span><select v-model="newProject.resolution"><option value="720p">768×1344 · 项目默认尺寸</option><option value="1080p" disabled>1080p · 仅作后期超分预留，不直接提交</option></select></label><label class="field"><span class="impact-label-row">新分镜默认 H3 工作流 <PromptImpactBadge kind="indirect" title="只初始化之后创建或拆解出的分镜，不会改写已有分镜。"/><PromptImpactBadge kind="production"/></span><select v-model="newProject.defaultMode"><option v-for="mode in h3Modes" :key="mode.value" :value="mode.value">{{ mode.label }} · {{ mode.desc }}</option></select></label></div></div></div>
        <div class="form-section"><div class="section-number">04</div><div class="section-content"><h3>连续剧封面</h3><p>推荐 2:3 竖版图；支持 JPEG、PNG、WebP，最大 10 MB。封面只用于项目识别，不进入生成工作流。</p><div class="cover-picker"><button type="button" class="cover-picker-preview" :disabled="createLoading" :style="{background:currentStyle?.gradient}" :aria-label="pendingCoverPreview?'更换待上传的连续剧封面':'选择连续剧封面'" @click="chooseProjectCover"><img v-if="pendingCoverPreview" :src="pendingCoverPreview" alt="待上传的连续剧封面预览"><template v-else><ImagePlus :size="25"/><span>选择封面图片</span></template></button><div><strong>{{ pendingCoverFile?.name || '尚未选择封面' }}</strong><small>项目会先创建，再上传并绑定封面；封面失败不会造成重复项目。</small><div class="cover-picker-actions"><button type="button" class="button ghost small" :disabled="createLoading" @click="chooseProjectCover"><Upload :size="13"/>{{ pendingCoverPreview?'更换封面':'选择封面' }}</button><button v-if="pendingCoverPreview" type="button" class="button ghost small" :disabled="createLoading" @click="removeProjectCover"><Trash2 :size="13"/>移除</button></div></div></div><p v-if="coverError" class="cover-error" role="alert">{{ coverError }}</p></div></div>
        <div class="new-form-footer"><span><Lock :size="14"/>所有数据默认保存在你的本地制作环境</span><button class="button primary" :disabled="createLoading" @click="createProject">{{ createLoading?'正在创建…':'创建并进入工作室' }}<ArrowRight :size="17"/></button></div>
      </div>
      <aside class="new-project-preview panel" :style="{background:currentStyle?.gradient}"><img v-if="pendingCoverPreview" class="new-cover-preview-image" :src="pendingCoverPreview" alt="待上传的连续剧封面实时预览"><template v-else><div class="preview-pattern"></div><span class="eyebrow">LIVE PROJECT PREVIEW</span><div class="preview-center"><div class="preview-clap"><i></i><Clapperboard :size="36"/></div><small>{{ currentStyle?.englishName }}</small><h3>{{ newProject.title || '未命名连续剧' }}</h3><p>{{ newProject.logline || '你的故事将从这里开始。' }}</p></div><div class="preview-spec"><span>{{ newProject.aspectRatio }}</span><span>{{ newProject.resolution }}</span><span>{{ workflowLabel(newProject.defaultMode) }}</span></div></template></aside>
    </section>
  </div>

  <div v-else-if="project" class="series-page page-stack">
    <input ref="coverInput" type="file" :accept="PROJECT_COVER_ACCEPT" hidden @change="selectProjectCover">
    <section class="series-header" :style="{background:currentStyle?.gradient}"><ProjectCoverImage v-if="project.coverUrl" class="series-cover-background" :src="project.coverUrl" :alt="`《${project.title}》连续剧封面`" :refresh="refreshCurrentProjectCover" eager/><div class="series-pattern"></div><div class="series-header-main"><button class="back-link light" @click="router.push('/projects')"><ArrowLeft :size="14"/>全部项目</button><div class="series-title"><div class="series-poster"><ProjectCoverImage v-if="project.coverUrl" :src="project.coverUrl" alt="" :refresh="refreshCurrentProjectCover" eager/><template v-else><Clapperboard :size="30"/><span>{{ currentStyle?.name }}</span></template></div><div><div class="series-status"><StatusBadge :status="project.status"/><span>{{ currentStyle?.englishName }}</span></div><h2>{{ project.title }}</h2><p>{{ project.logline }}</p><div class="series-facts"><span>{{ project.aspectRatio }}</span><i></i><span>{{ project.resolution }}</span><i></i><span>H3 视频 · SDXL 图片</span><i></i><span>{{ project.episodes.length }} 集</span></div></div></div></div><div class="series-header-actions"><button type="button" class="button glass" :disabled="coverSaving" @click="chooseProjectCover"><Upload :size="16"/>{{ coverSaving?'封面处理中…':project.coverAssetId?'更换封面':'上传封面' }}</button><button v-if="project.coverAssetId" type="button" class="button glass" :disabled="coverSaving" aria-label="移除连续剧封面" @click="removeProjectCover"><Trash2 :size="16"/>移除封面</button><button class="button glass" :disabled="saving||Boolean(canonicalGlossaryBlockedReason)" :title="canonicalGlossaryBlockedReason||''" @click="save(true)"><Save :size="16"/>{{ saving?'保存中…':'保存设定' }}</button><RouterLink to="/workflows" class="button primary"><Workflow :size="16"/>生成整剧工作流</RouterLink><p v-if="coverError" class="cover-error header-cover-error" role="alert">{{ coverError }}</p></div></section>
    <nav class="studio-tabs"><button :class="{active:tab==='overview'}" @click="tab='overview'"><Layers3 :size="16"/>项目总览</button><button :class="{active:tab==='bible'}" @click="tab='bible'"><BookOpen :size="16"/>一致性 Bible <span>{{ project.bible.characters.length+project.bible.scenes.length+project.bible.props.length }}</span><span v-if="scriptBiblePendingCount" class="pending-count">{{ scriptBiblePendingCount }} 待确认</span></button><button :class="{active:tab==='episodes'}" @click="tab='episodes'"><Film :size="16"/>剧集与分镜 <span>{{ project.episodes.length }}</span></button><button :class="{active:tab==='release'}" @click="tab='release'"><Lock :size="16"/>发行候选 <span>{{ releaseBundles.length }}</span></button></nav>
    <ScriptBibleCandidatesPanel ref="scriptBiblePanel" v-show="tab==='bible'" :project-id="project.id" :source-dirty="scriptBibleSourceDirty" @state="handleScriptBibleState" @applied="handleScriptBibleApplied" @apply-busy="scriptBibleApplying=$event"/>

    <template v-if="tab==='overview'">
      <section class="metric-grid compact"><article class="metric-card"><div class="metric-icon amber"><Film :size="19"/></div><div><span>剧集</span><strong>{{ project.episodes.length }}</strong><small>可无限新增</small></div></article><article class="metric-card"><div class="metric-icon cyan"><Clapperboard :size="19"/></div><div><span>分镜</span><strong>{{ totalShots }}</strong><small>已规划镜头</small></div></article><article class="metric-card"><div class="metric-icon violet"><Clock3 :size="19"/></div><div><span>预计时长</span><strong>{{ totalSeconds }}<em> 秒</em></strong><small>当前脚本总计</small></div></article><article class="metric-card"><div class="metric-icon green"><Lock :size="19"/></div><div><span>设定锚点</span><strong>{{ project.bible.characters.length+project.bible.scenes.length+project.bible.props.length }}</strong><small>人物 / 场景 / 道具</small></div></article>
      </section>
      <section class="panel script-workbench">
        <header class="panel-header"><div><span class="eyebrow">MASTER SCRIPT BREAKDOWN</span><h3>完整剧本与自动拆解</h3><p>识别“第 N 集 / EPISODE N”“镜头 / SHOT”和角色对白，并绑定现有角色、场景与道具 Bible。</p></div><button class="button ghost small" :disabled="saving||Boolean(canonicalGlossaryBlockedReason)" :title="canonicalGlossaryBlockedReason||''" @click="save(true)"><Save :size="14"/>保存剧本</button></header>
        <div class="script-workbench-body">
          <div class="script-editor-stack"><label class="field script-editor"><span class="impact-label-row">全剧母稿 <PromptImpactBadge kind="indirect" title="作为确定性剧本拆解源；拆解后，生成只读取已保存的剧集、分镜和 Bible。"/><PromptImpactBadge kind="audit" title="原文会保留，便于核对拆解结果。"/></span><textarea v-model="project.masterScript" rows="14" placeholder="第1集：标题&#10;镜头1【时长=8秒】【首帧连续=否】【尾帧连续=是】：场景&#10;[0-3秒] 动作：人物推门进入&#10;[3-6秒] 角色名（in a low voice）：对白&#10;&#10;第2集：……"></textarea><small>{{ project.masterScript.length.toLocaleString() }} 字符 · 拆解输入预算由后端配置决定，前端不会静默截断</small></label><PromptWritingGuide scope="project"/></div>
          <aside class="script-breakdown-options">
            <div><span class="eyebrow">BREAKDOWN PROFILE</span><h4>拆解参数</h4></div>
            <label class="field"><span class="impact-label-row">预计集数提示 <PromptImpactBadge kind="indirect" title="仅在母稿没有明确“第 N 集 / EPISODE N”标题时参与均匀切分；它是业务目标，不是模型单批次限制。"/></span><input v-model.number="breakdownOptions.episodeCountHint" type="number" min="1"></label>
            <label class="field"><span class="impact-label-row">默认单镜时长 <PromptImpactBadge kind="production" title="只初始化本次新拆出的分镜时长；有效范围来自生产能力接口，之后可逐镜修改。"/></span><input v-model.number="breakdownOptions.defaultShotDuration" type="number" :min="videoShotMinDurationSeconds" :max="videoShotMaxDurationSeconds" step="0.5" :disabled="!productionCapability"><small v-if="productionCapability">当前正式视频范围 {{ videoShotMinDurationSeconds }}–{{ videoShotMaxDurationSeconds }} 秒</small><small v-else class="field-error">{{ productionCapabilityError || '正在读取正式视频时长配置…' }}</small></label>
            <div class="setting-toggle"><div><span class="impact-label-row">默认承接上一镜尾帧 <PromptImpactBadge kind="continuity" title="母稿未写“首帧连续”时使用；只记录连续性要求，不会伪造首帧素材绑定。"/></span><small>明确写在镜头标题中的值优先</small></div><ToggleSwitch v-model="breakdownOptions.defaultFirstFrameContinuity"/></div>
            <div class="setting-toggle"><div><span class="impact-label-row">默认保留下一镜衔接帧 <PromptImpactBadge kind="continuity" title="母稿未写“尾帧连续”时使用；会进入导演提示词，但不等同于已生成尾帧素材。"/></span><small>明确写在镜头标题中的值优先</small></div><ToggleSwitch v-model="breakdownOptions.defaultLastFrameContinuity"/></div>
            <div class="setting-toggle"><div><span class="impact-label-row">多分镜提示词 <PromptImpactBadge kind="indirect" title="保存为剧集拆解偏好；当前整集生产仍为逐镜独立任务。"/></span><small>记录拆解偏好；当前生产为逐镜单任务，便于失败重试</small></div><ToggleSwitch v-model="breakdownOptions.multiShot"/></div>
            <div class="setting-toggle"><div><span class="impact-label-row">MiniMax H3 原生音频 <PromptImpactBadge kind="production" title="初始化新分镜的音频开关；最终以每个分镜自己的开关为准。"/></span><small>为新分镜保留对白、环境声与配乐字段</small></div><ToggleSwitch v-model="breakdownOptions.includeAudio"/></div>
            <label v-if="project.episodes.length" class="script-replace"><input v-model="breakdownOptions.replaceExisting" type="checkbox"><span><strong class="impact-label-row">重建并替换现有 {{ project.episodes.length }} 集 <PromptImpactBadge kind="audit" title="破坏性重建；点击生成后还需在弹框中确认清理范围。"/></strong><small>确认后清理旧分镜及其提示词、草稿、终态任务和组装记录；当前 Bible、素材与全剧规则保留。</small></span></label>
            <button class="button primary full" :disabled="breakdownLoading||!project.masterScript.trim()||!productionCapability" @click="breakdown"><WandSparkles :size="16"/>{{ breakdownLoading ? '正在拆解剧本…' : '自动生成剧集与分镜' }}</button>
          </aside>
        </div>
      </section>
      <section class="panel global-rules">
        <header><div><span class="eyebrow">GLOBAL VISUAL RULES</span><h3>全剧视觉规则</h3><p>可直接人工填写，也可在下方根据已保存故事信息生成隔离的英文候选。AI 候选不会自动覆盖；色彩脚本与连续性摘要是两个独立字段。</p></div><button class="button ghost small" :disabled="saving||Boolean(canonicalGlossaryBlockedReason)" :title="canonicalGlossaryBlockedReason||''" @click="save(true)"><Save :size="14"/>{{ saving?'保存中…':'保存所有更改' }}</button></header>
        <div class="form-grid two">
          <label class="field"><span class="impact-label-row">全局负面提示词 <PromptImpactBadge kind="audit" title="会被合并并保存在提示词产物中用于审计；当前本地 H3 原生节点没有独立 negative 输入，因此不会单独控制成片。"/></span><textarea v-model="project.bible.negativePrompt" rows="5" placeholder="identity drift; wardrobe color changes; duplicated people; extra limbs; text; watermark; flicker"></textarea><small>只写要排除的缺陷。AI 候选会把平台安全基线和现有英文排除项确定性合并，不让模型自由改写。</small></label>
          <label class="field"><span class="impact-label-row">色彩脚本 <PromptImpactBadge kind="prompt" title="会进入每份视频提示词第一镜的色彩与光线设计。"/><PromptImpactBadge kind="continuity"/></span><textarea v-model="project.bible.colorScript" rows="5" placeholder="EP01–03 use a cool cyan-gray base with restrained dark-red clue accents; shift to warm gold at the climax while preserving natural skin tones."></textarea><small>只写跨集色板、主辅光、环境阶段和转折；不要重复人物外观、空间规则或具体动作。</small></label>
          <label class="field"><span class="impact-label-row">连续性摘要 <PromptImpactBadge kind="prompt" title="作为图像提示词和跨集连续性的独立项目级后备字段。"/><PromptImpactBadge kind="continuity"/></span><textarea v-model="project.continuitySummary" rows="5" placeholder="Preserve stable character identity, wardrobe, architecture, prop geometry, weather logic, light direction, and spatial continuity across every episode."></textarea><small>与色彩脚本独立保存。写人物身份、固定服装、空间结构、道具形态、天气和光线方向等不可变化规则。</small></label>
          <label class="field glossary-field"><span class="impact-label-row">翻译 canonical 术语表 <PromptImpactBadge kind="audit" title="不会独立生成画面；本地 Qwen 翻译每个高影响字段时会强制复用这里的英文专名。"/><PromptImpactBadge kind="continuity" title="统一人物、场景和道具在所有镜头中的英文称呼。"/></span><textarea v-model="translationGlossaryText" rows="5" placeholder="玄纹剑 = Xuanwen Sword&#10;听雨客栈 = Listening Rain Inn&#10;沈砚 = Shen Yan"></textarea><small>视觉规则草稿只读取术语表作为专名约束，不会生成或覆盖术语。每个 Bible 实体必须使用唯一英文 canonical；发现碰撞时不会自动猜译名。</small><div class="glossary-status-row"><em v-if="translationGlossaryValidation.error" class="glossary-error">{{ translationGlossaryValidation.error }}</em><em v-else-if="canonicalTargetCollisionError" class="glossary-error">{{ canonicalTargetCollisionError }}</em><em v-else class="glossary-ok">{{ Object.keys(translationGlossaryValidation.value).length }} 个 canonical 术语<span v-if="missingCanonicalBibleNames.length"> · {{ missingCanonicalBibleNames.length }} 个 Bible 名称待补全</span></em><button class="button ghost small" type="button" :disabled="canonicalBackfillLoading||saving||Boolean(canonicalGlossaryBlockedReason)||!missingCanonicalBibleNames.length" :title="canonicalGlossaryBlockedReason||''" @click="backfillMissingCanonicalGlossary"><RefreshCw :size="13" :class="{spinning:canonicalBackfillLoading}"/>{{ canonicalBackfillLoading?'正在调用本地 Qwen…':missingCanonicalBibleNames.length?`补全 ${missingCanonicalBibleNames.length} 个缺失术语`:'术语已完整' }}</button></div><div v-if="canonicalGlossaryNotice" class="canonical-notice" :class="canonicalGlossaryNotice.kind">{{ canonicalGlossaryNotice.message }}</div></label>
        </div>
        <details class="bible-image-prompt-config" open>
          <summary><span><strong>实体参考图提示词默认配置</strong><small>创建项目时随风格写入默认值；保存后会作为角色、场景、道具参考图的项目级输入。</small></span></summary>
          <div class="form-grid two">
            <label class="field"><span class="impact-label-row">项目风格提示词 <PromptImpactBadge kind="prompt"/></span><textarea v-model="project.bibleImagePromptConfig.stylePrompt" rows="3" placeholder="cinematic neo-wuxia, realistic faces, restrained jade and vermilion palette"></textarea><small>会追加到每次实体参考图正向提示词；可与当前风格预设不同，保存后以这里为准。</small></label>
            <label class="field"><span class="impact-label-row">项目风格负面规则 <PromptImpactBadge kind="prompt"/></span><textarea v-model="project.bibleImagePromptConfig.styleNegativePrompt" rows="3" placeholder="overexposed skin, low quality, watermark"></textarea><small>会进入 SDXL 负向提示词；仅用于参考图，不覆盖全局视频负面提示词。</small></label>
            <label class="field"><span class="impact-label-row">角色构图规则 <PromptImpactBadge kind="prompt"/></span><textarea v-model="project.bibleImagePromptConfig.compositionRules.character" rows="3"></textarea><small>例如单主体、全身或 3/4、无遮挡脸、无其他人物。</small></label>
            <label class="field"><span class="impact-label-row">场景构图规则 <PromptImpactBadge kind="prompt"/></span><textarea v-model="project.bibleImagePromptConfig.compositionRules.scene" rows="3"></textarea><small>例如空镜建立图、清晰空间布局、无人干扰。</small></label>
            <label class="field"><span class="impact-label-row">道具构图规则 <PromptImpactBadge kind="prompt"/></span><textarea v-model="project.bibleImagePromptConfig.compositionRules.prop" rows="3"></textarea><small>例如单物体、产品设定图、完整可见、无手和人物。</small></label>
            <label class="field"><span class="impact-label-row">参考图质量后缀 <PromptImpactBadge kind="prompt"/></span><textarea v-model="project.bibleImagePromptConfig.qualitySuffix" rows="3"></textarea><small>作为正向 prompt 尾部的质量与生产参考说明。</small></label>
          </div>
          <details class="bible-image-negative-config">
            <summary>各类型固定负向规则</summary>
            <div class="form-grid three">
              <label class="field"><span>角色负向规则</span><textarea v-model="project.bibleImagePromptConfig.negativeRules.character" rows="3"></textarea></label>
              <label class="field"><span>场景负向规则</span><textarea v-model="project.bibleImagePromptConfig.negativeRules.scene" rows="3"></textarea></label>
              <label class="field"><span>道具负向规则</span><textarea v-model="project.bibleImagePromptConfig.negativeRules.prop" rows="3"></textarea></label>
            </div>
          </details>
        </details>
        <GlobalVisualRulesDraft :key="project.id" :project="project" :style="currentStyle" :blocked-reason="canonicalGlossaryBlockedReason" @applied="handleVisualRulesApplied"/>
        <TranslationReviewPanel :project-id="project.id" :fields="projectTranslationFields" :refresh-key="translationReviewRefreshKey" :blocked-reason="canonicalGlossaryBlockedReason" compact title="项目级手工中文字段 · 本地 Qwen 译文人工审核" @changed="invalidateProjectPromptsFromTranslation"/>
      </section>
      <section class="content-grid equal">
        <div class="panel continuity-summary"><header class="panel-header"><div><span class="eyebrow">WORLD BIBLE</span><h3>连续性完整度</h3></div><button class="text-link" @click="tab='bible'">管理设定 <ChevronRight :size="14"/></button></header><div class="continuity-score"><div class="score-ring" :style="{'--score':Math.min(100,(project.bible.characters.length?35:0)+(project.bible.scenes.length?30:0)+(project.bible.props.length?20:0)+(project.bible.colorScript?15:0))+'%'}"><strong>{{ Math.min(100,(project.bible.characters.length?35:0)+(project.bible.scenes.length?30:0)+(project.bible.props.length?20:0)+(project.bible.colorScript?15:0)) }}</strong><span>%</span></div><div><strong>世界设定可生产</strong><p>已建立核心视觉锚点，继续丰富道具与色彩脚本可提高稳定性。</p></div></div><div class="bible-bars"><div><span><UserRound :size="14"/>角色设定</span><strong>{{ project.bible.characters.length }}</strong><i><b :style="{width:Math.min(100,project.bible.characters.length*28)+'%'}"></b></i></div><div><span><Image :size="14"/>场景设定</span><strong>{{ project.bible.scenes.length }}</strong><i><b :style="{width:Math.min(100,project.bible.scenes.length*34)+'%'}"></b></i></div><div><span><Box :size="14"/>道具设定</span><strong>{{ project.bible.props.length }}</strong><i><b :style="{width:Math.min(100,project.bible.props.length*34)+'%'}"></b></i></div></div></div>
        <div class="panel recent-episodes"><header class="panel-header"><div><span class="eyebrow">EPISODE BOARD</span><h3>最近剧集</h3></div><button class="text-link" @click="tab='episodes'">全部剧集 <ChevronRight :size="14"/></button></header><div v-for="(episode,index) in project.episodes.slice(-4)" :key="episode.id" class="episode-mini-row"><span>{{ String(episode.number).padStart(2,'0') }}</span><div><strong>{{ episode.title }}</strong><p>{{ episode.synopsis || '尚未填写本集梗概' }}</p></div><div><StatusBadge :status="episode.status"/><small>{{ episodeDuration(index) }} 秒 · {{ episode.shots.length }} 镜</small></div><RouterLink :to="`/projects/${project.id}/episodes/${episode.id}/storyboard`" class="icon-btn"><ArrowRight :size="15"/></RouterLink></div><button v-if="!project.episodes.length" class="empty-inline" @click="tab='episodes';addEpisodeOpen=true"><Plus :size="22"/>添加第一集</button></div>
      </section>
    </template>

    <template v-else-if="tab==='bible'">
      <section class="bible-layout">
        <aside class="panel bible-nav"><div><span class="eyebrow">CONSISTENCY BIBLE</span><h3>制作圣经</h3><p>被分镜选中的实体会提供文字锚点；参考图只有在对应分镜绑定后才进入工作流。卡片“管理锁定”和 Seed 目前仅为管理元数据，不改变真实任务 Seed。</p></div><button v-for="section in bibleSections" :key="section.value" :class="{active:bibleSection===section.value}" @click="bibleSection=section.value"><component :is="section.icon" :size="17"/><span>{{ section.label }}</span><strong>{{ project.bible[section.value].length }}</strong></button><div class="bible-tip"><Sparkles :size="16"/><p>推荐至少建立 2 位主要角色、2 个核心场景和贯穿剧情的关键道具。</p></div></aside>
        <div class="bible-main">
          <header class="section-heading"><div><span class="eyebrow">{{ bibleSections.find(s=>s.value===bibleSection)?.label.toUpperCase() }}</span><h3>{{ bibleSections.find(s=>s.value===bibleSection)?.label }}</h3><p>视觉锚点可直接写英文，也可写中文后由本地 Qwen 生成英文候选；新增时还可选择素材库参考图，依据已保存的素材说明快速生成可编辑草稿。</p></div><button class="button primary" :disabled="bibleCreatePhase!=='idle'" @click="openAddBibleModal"><Plus :size="16"/>新增{{ bibleSections.find(s=>s.value===bibleSection)?.singular }}</button></header>
          <div v-if="canonicalGlossaryNotice" class="canonical-notice bible-canonical-notice" :class="canonicalGlossaryNotice.kind"><strong>名称 canonical</strong><span>{{ canonicalGlossaryNotice.message }}</span></div>
          <div v-if="bibleVisualTranslationNotice" class="canonical-notice bible-canonical-notice visual-translation-notice" :class="bibleVisualTranslationNotice.kind"><strong>视觉锚点英文候选</strong><span>{{ bibleVisualTranslationNotice.message }}</span></div>
          <PromptWritingGuide scope="bible"/>
          <div v-if="bibleImageLoadError" class="bible-image-global-state error" role="alert"><AlertTriangle :size="14"/><span>{{ bibleImageLoadError }}</span><button type="button" :disabled="bibleImageLoading" @click="loadBibleImageCandidates(project.id)"><RefreshCw :size="13"/>重试</button></div>
          <div v-else class="bible-image-global-state"><ImagePlus :size="14"/><span>实体参考图使用系统固定模型与工作流；质量提示仅供人工审核，不会自动阻断生成。{{ bibleImageBatchProgress ? `批量创建中：${bibleImageBatchProgress}` : '' }}</span><button type="button" :disabled="bibleImageLoading||bibleImageBatchGenerating" @click="loadBibleImageCandidates(project.id)"><RefreshCw :size="13" :class="{spinning:bibleImageLoading}"/>刷新候选状态</button><button type="button" class="button ghost small" :disabled="saving||bibleImageLoading||bibleImageBatchGenerating||Boolean(bibleImageGeneratingKey)||!bibleImageUnconfirmedTargets.length" @click="generateAllUnconfirmedBibleImageCandidates"><Sparkles :size="13"/>{{ bibleImageBatchGenerating?'正在批量创建…':`一键生成未确认参考图 (${bibleImageUnconfirmedTargets.length})` }}</button></div>
          <TranslationReviewPanel :project-id="project.id" :fields="bibleTranslationFields" :refresh-key="translationReviewRefreshKey" :blocked-reason="canonicalGlossaryBlockedReason" compact :title="`${bibleSections.find(s=>s.value===bibleSection)?.label} · 本地 Qwen 译文人工审核`" @changed="invalidateProjectPromptsFromTranslation"/>
          <div class="bible-card-list">
            <article v-for="item in bibleItems" :key="item.id" class="panel bible-card">
              <div class="bible-ref" :class="{placeholder:!assetThumb(item)}"><AssetImage v-if="assetThumb(item)" :src="assetThumb(item)" :label="item.name" :accent="currentStyle?.accent"/><component v-else :is="bibleSections.find(s=>s.value===bibleSection)?.icon" :size="30"/><span>REF</span></div>
              <div class="bible-fields"><label class="bible-impact-label">名称 <PromptImpactBadge kind="label" title="用于识别和任务展示。"/><PromptImpactBadge kind="indirect" title="母稿拆解会用名称精确匹配实体；名称本身不会作为视觉描述写入 H3。"/></label><div class="bible-card-title"><input v-model="item.name" aria-label="名称"><span title="当前为工作室管理元数据；真实工作流使用项目 continuity seed。">管理 SEED {{ item.seed }} · 不注入任务</span></div><div class="bible-canonical-row" :class="{missing:!canonicalForBibleItem(item)}"><span>CANONICAL</span><strong v-if="canonicalForBibleItem(item)">{{ item.name }} = {{ canonicalForBibleItem(item) }}</strong><strong v-else>尚未补全 · 可在项目总览自动补全或人工填写</strong></div><label class="bible-impact-label">基础描述 <PromptImpactBadge kind="indirect" title="供创作和管理阅读；不会自动同步到下方视觉锚点。"/></label><textarea v-model="item.description" rows="2" aria-label="基础描述" placeholder="供制片人员阅读；需要影响画面时请同步更新下方视觉锚点"></textarea><label class="bible-impact-label">视觉锚点（中文或英文） <PromptImpactBadge kind="prompt"/><PromptImpactBadge kind="continuity"/></label><textarea v-model="item.visualPrompt" rows="3" aria-label="视觉锚点" placeholder="中文或英文均可；中文保存后请在上方审核面板生成并确认英文候选，只写不可变化的外观、结构、材质、颜色和固定标记。"></textarea><details class="bible-advanced-editor"><summary>更多直接影响提示词的结构化字段 <PromptImpactBadge kind="prompt"/><PromptImpactBadge kind="continuity"/></summary><div v-if="bibleSection==='characters'" class="form-grid two"><label class="field"><span>年龄外观</span><textarea v-model="item.ageAppearance" rows="2" placeholder="Age appearance, build and physical presence."></textarea></label><label class="field"><span>面部身份</span><textarea v-model="item.faceIdentity" rows="2" placeholder="Immutable facial geometry and identity marks."></textarea></label><label class="field"><span>固定服装</span><textarea v-model="item.wardrobe" rows="2" placeholder="Permanent wardrobe colors, cut and materials."></textarea></label><label class="field"><span>声音特征</span><textarea v-model="item.voiceProfile" rows="2" placeholder="Voice timbre, pace and stable delivery traits."></textarea></label><label class="field"><span>说话者人物描述（声音特征为空时后备）</span><textarea v-model="item.personality" rows="2" placeholder="Speaker temperament and stable dialogue presence; do not copy the management description."></textarea></label></div><div v-else-if="bibleSection==='scenes'" class="form-grid two"><label class="field"><span>地点结构</span><textarea v-model="item.location" rows="2"></textarea></label><label class="field"><span>时间</span><textarea v-model="item.timeOfDay" rows="2"></textarea></label><label class="field"><span>光线</span><textarea v-model="item.lighting" rows="2"></textarea></label><label class="field"><span>色板</span><textarea v-model="item.colorPalette" rows="2"></textarea></label><label class="field"><span>环境声</span><textarea v-model="item.ambientSound" rows="2"></textarea></label></div><div v-else class="form-grid two"><label class="field"><span>材质</span><textarea v-model="item.material" rows="2"></textarea></label><label class="field"><span>固定标记</span><textarea v-model="item.distinctiveMarks" rows="2"></textarea></label><label class="field"><span>连续性状态</span><textarea v-model="item.continuityState" rows="2"></textarea></label></div><p>以上字段会直接进入导演提示词或连续性定义；中文内容需在上方“本地 Qwen 译文人工审核”确认后才会替换为英文。实体名称与基础描述不在此列。</p></details><label class="bible-impact-label">参考图 <PromptImpactBadge kind="continuity" title="身份锚点与补充参考图独立保存；仍需在具体分镜绑定后才会连接工作流。"/><PromptImpactBadge kind="production"/></label><div class="reference-chips"><span v-if="item.anchorAssetId" class="anchor-reference">身份锚点：{{ assetName(item.anchorAssetId) }} <button title="解除身份锚点并保留为普通参考图；不会删除素材或候选历史。" @click="setBibleAnchor(item,null)"><Trash2 :size="11"/></button></span><span v-for="assetId in item.referenceAssetIds" :key="assetId">{{ assetName(assetId) }} <button title="设为身份锚点；只影响当前 Bible 绑定。" @click="setBibleAnchor(item,assetId)"><Lock :size="11"/></button><button title="移除普通参考图绑定；不会删除素材或候选历史。" @click="item.referenceAssetIds=item.referenceAssetIds.filter(id=>id!==assetId)"><Trash2 :size="11"/></button></span><button @click="bibleAssetPickerId=bibleAssetPickerId===item.id?'':item.id"><Plus :size="12"/>{{ bibleAssetPickerId===item.id?'收起素材库':'绑定参考图' }}</button></div><div v-if="bibleAssetPickerId===item.id" class="bible-inline-assets"><header><span>选择{{ bibleSections.find(s=>s.value===bibleSection)?.singular }}素材</span><small>只显示同分类授权素材；加入素材池不代表每个工作流都会自动连接；删除绑定不会删除候选历史。</small></header><div><button v-for="asset in studio.assets.filter(a=>a.type===assetTypeForBible())" :key="asset.id" :class="{active:bibleAssetIds(item).includes(asset.id)}" @click="toggleBibleAsset(item,asset.id)"><AssetImage :src="asset.thumbnail" :label="asset.name" :accent="currentStyle?.accent"/><span>{{ asset.name }}</span><i><Check :size="11"/></i></button></div><button class="button primary small" @click="saveBibleAssets(item,assetTypeForBible())"><Save :size="13"/>保存绑定</button></div></div>
              <section class="bible-image-candidate">
                <header><span><ImagePlus :size="15"/><strong>实体参考图候选</strong><small>系统固定模型 / 固定工作流 · 每次生成都会保留为独立候选，人工确认后才入库</small></span><button type="button" class="button ghost small" :disabled="saving||Boolean(bibleImageGeneratingKey)||bibleImageBatchGenerating||Boolean(bibleImagePromotingKey)" @click="generateBibleImageCandidate(assetTypeForBible(),item)"><Sparkles :size="13"/>{{ bibleImageGeneratingKey===bibleImageEntityKey(assetTypeForBible(),item.id)?'正在创建任务…':bibleImageHasRetry(item)?'按原请求安全重试':bibleImageCandidateFor(item)?.candidateHistory.length?'按调整重新生成':'生成参考图' }}</button></header>
                <label>手工调整提示词 / 补充创作要求（可选，重新生成时生效）<textarea v-model="bibleImageDirections[bibleImageEntityKey(assetTypeForBible(),item.id)]" rows="2" maxlength="2000" :disabled="bibleImageHasRetry(item)" placeholder="例如：冷雨逆光、半身构图、避开夸张表情；不能选择模型、模板或工作流。"></textarea></label>
                <details v-if="bibleImagePromptInputsFor(item)" class="bible-image-prompt-inputs">
                  <summary><span>本次参考图提示词来源</span><small v-if="bibleImagePromptOverrideCount(assetTypeForBible(),item)">{{ bibleImagePromptOverrideCount(assetTypeForBible(),item) }} 项人工修改</small></summary>
                  <p>下方字段由服务端从已保存项目、Bible、译文和参考图配置中拆出；人工修改只影响下一次参考图生成，不会直接改 Bible 正式字段。</p>
                  <div class="bible-image-prompt-source-grid">
                    <label v-for="input in bibleImagePromptInputRows(item,'positive')" :key="input.key" class="field"><span>{{ input.label }}<small>{{ input.source }}</small></span><textarea :value="bibleImagePromptDraftValue(assetTypeForBible(),item,input)" rows="2" :disabled="!input.editable||bibleImageHasRetry(item)" @input="setBibleImagePromptOverride(assetTypeForBible(),item,input,eventText($event))"></textarea></label>
                  </div>
                  <details class="bible-image-negative-config">
                    <summary>负向提示词来源</summary>
                    <div class="bible-image-prompt-source-grid">
                      <label v-for="input in bibleImagePromptInputRows(item,'negative')" :key="input.key" class="field"><span>{{ input.label }}<small>{{ input.source }}</small></span><textarea :value="bibleImagePromptDraftValue(assetTypeForBible(),item,input)" rows="2" :disabled="!input.editable||bibleImageHasRetry(item)" @input="setBibleImagePromptOverride(assetTypeForBible(),item,input,eventText($event))"></textarea></label>
                    </div>
                  </details>
                  <div class="bible-image-prompt-preview">
                    <div><strong>正向预览</strong><p>{{ bibleImagePromptTextPreview(assetTypeForBible(),item,'positive') || bibleImagePromptInputsFor(item)?.finalPromptText }}</p></div>
                    <div><strong>负向预览</strong><p>{{ bibleImagePromptTextPreview(assetTypeForBible(),item,'negative') || bibleImagePromptInputsFor(item)?.negativePrompt || '无' }}</p></div>
                    <button v-if="bibleImagePromptOverrideCount(assetTypeForBible(),item)" type="button" class="button ghost small" :disabled="bibleImageHasRetry(item)" @click="clearBibleImagePromptOverrides(assetTypeForBible(),item)">清除本次人工修改</button>
                  </div>
                </details>
                <div v-if="bibleImageHasRetry(item)" class="bible-image-retry"><RefreshCw :size="13"/><span>上次响应尚未确认；重试会保持原字段与幂等键，避免重复创建任务。</span><button type="button" @click="abandonBibleImageRetry(assetTypeForBible(),item.id)">核对任务后放弃</button></div>
                <div v-if="bibleImageCandidateFor(item)" class="bible-image-status" :class="{stale:!bibleImageCandidateFor(item)?.latestCandidate?.sourceCurrent}">
                  <div><strong>冻结目标：{{ bibleSections.find(s=>s.value===bibleSection)?.singular }} · {{ bibleImageCandidateFor(item)?.entityName }}</strong><code>{{ bibleImageCandidateFor(item)?.entityId }}</code><small>来源指纹 {{ bibleImageCandidateFor(item)?.latestCandidate?.sourceFingerprint.slice(0,12) || bibleImageCandidateFor(item)?.sourceFingerprint.slice(0,12) }}…</small></div>
                  <span>{{ bibleImageTaskStatusLabel(bibleImageCandidateFor(item)?.latestCandidate?.taskStatus) }}</span>
                  <p v-if="bibleImageCandidateFor(item)?.latestCandidate&&!bibleImageCandidateFor(item)?.latestCandidate?.sourceCurrent"><AlertTriangle :size="13"/>Bible 字段、译文或视觉规则已变化；该候选仍可人工加入参考图，但会按旧来源记录并可能与当前描述冲突。</p>
                  <div v-if="bibleImageCandidateFor(item)?.candidateHistory.length" class="bible-image-history">
                    <article v-for="(candidate,candidateIndex) in bibleImageCandidateFor(item)?.candidateHistory || []" :key="bibleImageCandidateKey(candidate)" :class="{stale:!candidate.sourceCurrent}">
                      <header><span><strong>{{ candidateIndex===0?'最新候选':'历史候选' }} #{{ (bibleImageCandidateFor(item)?.candidateHistory.length || 0) - candidateIndex }}</strong><small>{{ bibleImageTaskStatusLabel(candidate.taskStatus) }} · {{ new Date(candidate.createdAt).toLocaleString('zh-CN') }}</small></span><RouterLink v-if="candidate.generationTaskId" :to="{path:'/workflows',query:{task:candidate.generationTaskId}}" class="button ghost small"><Workflow :size="13"/>打开任务</RouterLink></header>
                      <p v-if="!candidate.sourceCurrent"><AlertTriangle :size="13"/>该候选对应旧版 Bible、译文或视觉规则；可以继续作为历史参考图加入，系统会保留当时的生成来源。</p>
                      <ul v-if="candidate.quality.warnings.length"><li v-for="warning in candidate.quality.warnings" :key="`${bibleImageCandidateKey(candidate)}:${warning}`"><AlertTriangle :size="12"/>{{ warning }}</li></ul>
                      <div v-if="bibleImageOutputImages(candidate).length" class="bible-image-outputs"><article v-for="(media,index) in bibleImageOutputImages(candidate)" :key="`${bibleImageCandidateKey(candidate)}:${media.index??index}:${media.filename}`"><button v-if="media.previewUrl||media.downloadUrl" type="button" class="bible-image-preview-button" @click="openBibleImagePreview(item,candidate,media,index)"><img :src="media.previewUrl||media.downloadUrl" :alt="`${item.name} 参考图候选 ${index+1}`"><span><Eye :size="12"/>预览全图</span></button><div v-else><Image :size="24"/><span>预览地址暂不可用</span></div><span>{{ media.filename }}</span><button type="button" class="button primary small" :disabled="(Boolean(media.promotedAssetId)&&bibleImageOutputIsBound(item,media))||bibleImagePromotingKey===bibleImagePromotionKey(bibleImageCandidateFor(item)!,candidate,media,index)" @click="promoteBibleImageOutput(bibleImageCandidateFor(item),candidate,media,index)"><FolderInput :size="13"/>{{ bibleImageOutputActionLabel(item,bibleImageCandidateFor(item)!,candidate,media,index) }}</button></article></div>
                      <p v-else class="bible-image-empty">该候选还没有可预览图片；任务完成后点击刷新状态。</p>
                    </article>
                  </div>
                  <div class="bible-image-task-actions"><button type="button" class="button ghost small" :disabled="bibleImageLoading" @click="loadBibleImageCandidates(project.id)"><RefreshCw :size="13"/>刷新状态</button></div>
                </div>
                <p v-else class="bible-image-empty">尚无候选。生成时会读取并校验服务端已保存 Bible；当前卡片有未保存编辑时不会使用旧数据创建任务。</p>
                <p v-if="bibleImageActionErrors[bibleImageEntityKey(assetTypeForBible(),item.id)]" class="bible-image-error" role="alert"><AlertTriangle :size="13"/>{{ bibleImageActionErrors[bibleImageEntityKey(assetTypeForBible(),item.id)] }}</p>
              </section>
              <div class="bible-card-actions"><button class="lock-button" :class="{locked:item.locked}" title="当前仅记录工作室管理状态，不改变提示词、参考连接或真实任务 Seed。" @click="item.locked=!item.locked"><component :is="item.locked?Lock:Unlock" :size="15"/>{{ item.locked?'管理标记：锁定':'管理标记：未锁定' }}</button><small class="management-only-note">仅管理元数据</small><button class="icon-btn danger" @click="removeBibleItem(item.id)"><Trash2 :size="15"/></button></div>
            </article>
            <button v-if="!bibleItems.length" class="panel empty-bible" @click="openAddBibleModal"><component :is="bibleSections.find(s=>s.value===bibleSection)?.icon" :size="30"/><strong>还没有{{ bibleSections.find(s=>s.value===bibleSection)?.singular }}设定</strong><span>可手工填写，或从素材库说明快速生成第一个视觉锚点。</span><i><Plus :size="14"/>立即新增</i></button>
          </div>
        </div>
      </section>
    </template>

    <template v-else-if="tab==='episodes'">
      <section class="episode-section">
        <header class="section-heading"><div><span class="eyebrow">UNLIMITED EPISODE CANVAS</span><h3>剧集与分镜</h3><p>所有新剧集默认继承本项目的一致性 Bible，也可以逐镜覆盖生成模式。</p></div><button class="button primary" @click="addEpisodeOpen=true"><Plus :size="16"/>新增下一集</button></header>
        <CreativeDraftWorkbench :project="project" :initial-episode-id="selectedEpisodeId" @applied="handleCreativeDraftApplied"/>
        <div class="episode-grid">
          <article v-for="episode in project.episodes" :key="episode.id" class="panel episode-card" :class="{expanded:selectedEpisodeId===episode.id}" @click="selectEpisode(episode)">
            <div class="episode-number"><span>EPISODE</span><strong>{{ String(episode.number).padStart(2,'0') }}</strong></div>
            <div class="episode-copy"><div><StatusBadge :status="episode.status"/><span>{{ episodeProductionLabel(episode) }}</span><span v-if="assemblyFor(episode.id)?.status==='failed'&&assemblyOutputFor(episode.id)" class="episode-assembly-ready stale">重组失败 · 旧成片可用</span><span v-else-if="assemblyOutputFor(episode.id)" class="episode-assembly-ready" :class="{stale:episode.status==='rendering'||assemblyActive(episode.id)}">{{ episode.status==='rendering'||assemblyActive(episode.id)?'旧成片 · 待重组':'成片就绪' }}</span><span v-else-if="assemblyActive(episode.id)" class="episode-assembly-ready stale">{{ assemblyStateLabel(episode.id) }}</span></div><h3>{{ episode.title }}</h3><p>{{ episode.synopsis || '尚未填写本集梗概，进入分镜编辑器开始创作。' }}</p><div class="episode-tags"><span><Film :size="13"/>{{ episodeVideoCount(episode) }} 视频</span><span><Image :size="13"/>{{ episodeImageCount(episode) }} 图片</span><span><Clock3 :size="13"/>{{ episode.shots.reduce((s,x)=>s+(x.mediaType==='video'?x.duration:0),0) }} 秒视频</span><span v-if="episodeVideoCount(episode)">{{ episode.audioEnabled?'原生音频':'静音' }}</span></div></div>
            <div class="episode-card-footer"><RouterLink :to="`/projects/${project.id}/episodes/${episode.id}/storyboard`" class="button ghost full" @click.stop>打开分镜编辑器 <ArrowRight :size="15"/></RouterLink><button class="button primary episode-generate-button" :disabled="episodeGenerationLoadingId===episode.id||!episode.shots.length||Boolean(canonicalGlossaryBlockedReason)" :title="canonicalGlossaryBlockedReason||'选择官方分辨率档位后逐镜创建整集批次'" @click.stop="openWholeEpisode(episode)"><Sparkles :size="14"/>{{ episodeGenerationLoadingId===episode.id?'正在提交整集…':'生成整集' }}</button><button class="button ghost episode-output-button" :disabled="assemblyLoadingId===episode.id" @click.stop="assemblyFor(episode.id)||assemblyOutputFor(episode.id)?selectEpisode(episode):assembleEpisode(episode)"><component :is="assemblyFor(episode.id)||assemblyOutputFor(episode.id)?Eye:Film" :size="14"/>{{ assemblyLoadingId===episode.id?'正在提交…':assemblyActive(episode.id)?assemblyStateLabel(episode.id):assemblyFor(episode.id)?.status==='failed'?'查看失败':assemblyOutputFor(episode.id)?'查看成片':'组装本集' }}</button><button class="icon-btn" @click.stop="removeEpisode(episode.id)"><Trash2 :size="15"/></button></div>
            <section v-if="selectedEpisodeId===episode.id" class="episode-assembly-panel" @click.stop>
              <header><div><span class="eyebrow">EPISODE MASTER</span><h4>第 {{ episode.number }} 集组装成片</h4><p>只使用本集各分镜最新一次成功的视频任务；单镜重新生成后，点击“重新组装本集”即可替换。</p></div><button class="icon-btn" title="刷新成片链接" @click="loadEpisodeAssemblies(project.id)"><RefreshCw :size="14"/></button></header>
              <template v-if="assemblyOutputFor(episode.id)?.outputUrl">
                <div v-if="assemblyActive(episode.id)" class="episode-assembly-stale"><div class="loader-ring"></div><span>{{ assemblyStateLabel(episode.id) }}；下方旧成片仍可预览，完成后会自动替换。可以安全离开本页。</span></div>
                <div v-else-if="assemblyFor(episode.id)?.status==='failed'" class="episode-assembly-inline-error"><span>重新组装失败：{{ assemblyFor(episode.id).errorMessage || '请查看后台任务日志' }}</span><button class="button ghost small" @click="assembleEpisode(episode)"><RefreshCw :size="13"/>重试</button></div>
                <div v-else-if="assemblyError" class="episode-assembly-inline-error"><span>重新组装失败：{{ assemblyError }}</span><button class="button ghost small" @click="assembleEpisode(episode)"><RefreshCw :size="13"/>重试</button></div>
                <div v-else-if="episode.status==='rendering'" class="episode-assembly-stale"><RefreshCw :size="14"/><span>本集有分镜正在重新生成；下方仍可预览旧成片。新任务完成后请重新组装本集。</span></div>
                <video :key="`${assemblyOutputFor(episode.id).id}:${assemblyOutputFor(episode.id).outputUrl}`" class="episode-master-video" :src="assemblyOutputFor(episode.id).outputUrl" controls preload="metadata" @loadedmetadata="handleAssemblyPreviewLoaded(episode.id,assemblyOutputFor(episode.id).id)" @error="recoverAssemblyPreview(episode)">当前浏览器不支持视频预览。</video>
                <div v-if="assemblyPreviewErrors[episode.id]" class="episode-assembly-inline-error"><span>成片预览失败：{{ assemblyPreviewErrors[episode.id] }}</span><button class="button ghost small" :disabled="Boolean(assemblyPreviewRefreshingId)" @click="recoverAssemblyPreview(episode,true)"><RefreshCw :size="13"/>{{ assemblyPreviewRefreshingId===assemblyOutputFor(episode.id).id?'刷新中…':'重新获取地址' }}</button></div>
                <div class="episode-master-actions"><div><strong>{{ episode.status==='rendering'||assemblyActive(episode.id)?'当前展示旧成片':'本集成片已就绪' }}</strong><span>{{ assemblyOutputFor(episode.id).sourceTaskIds?.length || episode.shots.filter(item=>item.mediaType==='video').length }} 个视频产物 · {{ assemblyOutputFor(episode.id).finishedAt ? new Date(assemblyOutputFor(episode.id).finishedAt!).toLocaleString('zh-CN') : '刚刚完成' }}</span></div><a v-if="assemblyOutputFor(episode.id).provenanceUrl" class="button ghost" :href="assemblyOutputFor(episode.id).provenanceUrl" target="_blank" rel="noopener"><FileJson :size="14"/>来源清单</a><button class="button ghost" :disabled="assemblyLoadingId===episode.id||assemblyActive(episode.id)" @click="assembleEpisode(episode)"><RefreshCw :size="14"/>{{ assemblyLoadingId===episode.id?'正在提交…':assemblyActive(episode.id)?assemblyStateLabel(episode.id):'重新组装本集' }}</button><button class="button primary" @click="downloadAssembly(episode)"><Download :size="14"/>下载本集视频</button></div>
              </template>
              <div v-else-if="assemblyLoadingId===episode.id||assemblyActive(episode.id)" class="episode-assembly-empty"><div class="loader-ring"></div><strong>{{ assemblyLoadingId===episode.id?'正在提交组装任务':assemblyStateLabel(episode.id) }}</strong><p>任务由独立后台进程持续执行；关闭或刷新页面不会中断，回来后会自动恢复显示。</p></div>
              <div v-else-if="assemblyFor(episode.id)?.status==='failed'||assemblyError" class="episode-assembly-empty error"><strong>本集组装失败</strong><p>{{ assemblyFor(episode.id)?.errorMessage || assemblyError }}</p><button class="button ghost small" @click="assembleEpisode(episode)"><RefreshCw :size="14"/>重试组装</button></div>
              <div v-else class="episode-assembly-empty"><Film :size="28"/><strong>本集还没有组装成片</strong><p>请先确保本集每个视频分镜都已有成功的生产任务；图片分镜不会被加入视频成片。</p><button class="button primary" :disabled="!episode.shots.some(item=>item.mediaType==='video')" @click="assembleEpisode(episode)"><Film :size="14"/>组装本集视频</button></div>
            </section>
          </article>
          <button class="episode-add-card" @click="addEpisodeOpen=true"><div><Plus :size="25"/></div><strong>新增第 {{ project.episodes.length+1 }} 集</strong><span>集数没有上限，可持续更新直至完结</span></button>
        </div>
      </section>
    </template>

    <template v-else>
      <section class="release-workspace">
        <header class="section-heading release-heading"><div><span class="eyebrow">EXPLICIT RELEASE CANDIDATE</span><h3>发行候选与版本清单</h3><p>制作历史不会自动成为发行版本。这里明确锁定每个分镜的任务与输出索引、每集成片及整剧成片，封存后再生成的新内容不会悄悄替换它。</p></div><button class="button ghost" :disabled="releaseLoading" @click="loadReleaseBundles(project.id)"><RefreshCw :size="15"/>刷新版本</button></header>
        <div class="release-principles panel"><div><Lock :size="20"/><span><strong>封存即不可变</strong><small>封存时会重新验证媒体哈希、来源清单和成片所引用的精确分镜版本。</small></span></div><div><Film :size="20"/><span><strong>重做后必须重组</strong><small>单镜重新生成后，请先重新组装对应剧集，再创建新的发行候选；旧候选不会自动漂移。</small></span></div><div><FileJson :size="20"/><span><strong>制作与发行分离</strong><small>草稿用于检查选择；封存候选用于版本审计，正式发行不会改变已封存内容。</small></span></div></div>
        <div v-if="releaseError" class="release-error"><span>{{ releaseError }}</span><button @click="releaseError=''">关闭</button></div>

        <section v-if="draftRelease" class="panel release-draft">
          <header><div><span class="release-status draft">草稿</span><h4>发行候选 v{{ draftRelease.version }}</h4><p>可重新采集当前成功产物并编辑说明；封存前不会影响现有发行版本。</p></div><span class="release-updated">{{ draftRelease.updatedAt ? new Date(draftRelease.updatedAt).toLocaleString('zh-CN') : '' }}</span></header>
          <div class="release-selection-metrics"><div><span>分镜产物</span><strong>{{ draftRelease.selection.shots.length }} / {{ totalShots }}</strong></div><div><span>单集成片</span><strong>{{ draftRelease.selection.episodes.length }} / {{ releaseVideoEpisodeCount }}</strong></div><div><span>整剧成片</span><strong>{{ draftRelease.selection.seriesAssemblyId ? '已选择' : project.status==='completed' ? '缺少' : '非必需' }}</strong></div><div><span>完整性</span><strong :class="{ok:releaseSelectionComplete}">{{ releaseSelectionComplete ? '选择完整' : '仍需补齐' }}</strong></div></div>
          <div class="release-form"><label class="field"><span>版本标题</span><input v-model="releaseTitle" maxlength="200" placeholder="例如：第一季正式版"></label><label class="field"><span>版本说明</span><textarea v-model="releaseNotes" rows="4" maxlength="10000" placeholder="记录本版修订范围、目标渠道和需人工复核的事项。"></textarea></label></div>
          <div class="release-actions"><RouterLink to="/workflows" class="button ghost"><Workflow :size="14"/>检查任务与公开副本</RouterLink><button class="button ghost" :disabled="releaseLoading" @click="saveReleaseDraft(false)"><Save :size="14"/>保存说明</button><button class="button ghost" :disabled="releaseLoading" @click="saveReleaseDraft(true)"><RefreshCw :size="14"/>重新采集当前产物</button><button class="button primary" :disabled="releaseLoading||!releaseSelectionComplete" :title="!releaseSelectionComplete?'请先补齐所有分镜产物与本集/整剧成片':''" @click="sealReleaseCandidate"><Lock :size="14"/>封存并签名</button></div>
        </section>
        <section v-else class="panel release-create"><div><Plus :size="28"/><span><strong>建立新的发行候选</strong><p>系统会选择当前每个分镜的明确成功输出，以及与这些分镜完全一致的每集/整剧成片。缺项会保留为草稿并在封存时明确指出。</p></span></div><div class="release-form"><label class="field"><span>版本标题（可选）</span><input v-model="releaseTitle" maxlength="200" placeholder="留空将自动使用项目名与版本号"></label><label class="field"><span>版本说明（可选）</span><textarea v-model="releaseNotes" rows="3" maxlength="10000" placeholder="例如：修正第2集镜头2后重新组装"></textarea></label></div><button class="button primary" :disabled="releaseLoading||!totalShots" @click="createReleaseCandidate"><Plus :size="15"/>按当前产物创建候选</button></section>

        <section class="release-history"><header><div><span class="eyebrow">RELEASE HISTORY</span><h4>版本历史</h4></div><small>{{ releaseBundles.length }} 个版本</small></header><div v-if="releaseBundles.length" class="release-list"><article v-for="bundle in releaseBundles" :key="bundle.id" class="panel release-row"><div class="release-version"><span>V</span><strong>{{ bundle.version }}</strong></div><div class="release-copy"><div><span class="release-status" :class="bundle.status">{{ bundle.status==='released'?'已发行':bundle.status==='sealed'?'已封存':bundle.status==='superseded'?'已替代':'草稿' }}</span><span :class="['verification-chip',bundle.verificationStatus]">{{ bundle.verificationStatus==='verified'?'签名已验证':bundle.verificationStatus==='invalid'?'签名异常':'待封存' }}</span></div><h4>{{ bundle.title }}</h4><p>{{ bundle.notes || '没有版本说明' }}</p><small>{{ bundle.selection.shots.length }} 个分镜 · {{ bundle.selection.episodes.length }} 集成片<span v-if="bundle.selection.seriesAssemblyId"> · 含整剧成片</span><span v-if="bundle.manifestSha256"> · SHA {{ bundle.manifestSha256.slice(0,12) }}…</span></small></div><div class="release-row-actions"><button v-if="bundle.status!=='draft'" class="button ghost small" :disabled="releaseLoading" @click="verifyReleaseCandidate(bundle)"><Check :size="13"/>实时校验产物</button><button v-if="bundle.status==='sealed'&&auth.user?.isStaff" class="button primary small" :disabled="releaseLoading" @click="markReleasePublished(bundle)"><Lock :size="13"/>确认发行</button></div></article></div><div v-else class="panel release-empty"><FileJson :size="28"/><strong>还没有发行版本</strong><p>生产任务与组装成片仍保留在制作历史中，但不会被误认为正式发行内容。</p></div></section>
      </section>
    </template>

    <BaseModal :open="bibleImageQueueConfirmOpen" title="确认实体参考图排队" eyebrow="RESOURCE ADVISORY" width="600px" @close="closeBibleImageQueueConfirmation">
      <div v-if="bibleImagePendingConfirmation" class="script-rebuild-confirm">
        <div><AlertTriangle :size="22"/><span><strong>当前生产队列超过建议值</strong><small>冻结目标：{{ bibleImagePendingConfirmation.entityType==='character'?'人物':bibleImagePendingConfirmation.entityType==='scene'?'场景':'道具' }} · {{ bibleImagePendingConfirmation.entityId }}</small></span></div>
        <p>继续只会使用同一幂等键完整排队，不会提高 worker 并发，也不会改变已保存的 Bible、模型、模板或工作流。取消不会创建新的请求；稍后重试仍会保留原请求。</p>
      </div>
      <template #footer><button class="button ghost" :disabled="Boolean(bibleImageGeneratingKey)" @click="closeBibleImageQueueConfirmation">取消，不提交</button><button class="button primary" :disabled="Boolean(bibleImageGeneratingKey)||!bibleImagePendingConfirmation" @click="confirmBibleImageQueue"><AlertTriangle :size="15"/>{{ bibleImageGeneratingKey?'正在提交…':'确认并按原请求排队' }}</button></template>
    </BaseModal>

    <BaseModal :open="scriptBibleGateOpen" title="拆解前仍有 Bible 建议" eyebrow="SCRIPT BIBLE REVIEW REQUIRED" width="600px" @close="!breakdownLoading&&closeScriptBibleGate()">
      <div class="script-rebuild-confirm">
        <div><AlertTriangle :size="22"/><span><strong>建议先确认剧本中的人物、场景和道具</strong><small>当前有 {{ scriptBiblePendingCount || '若干' }} 项尚未确认。确认候选只会增量加入 Bible，不会删除现有设定；当前尚未创建、替换或删除任何剧集与分镜。</small></span></div>
        <p>你也可以明确跳过这一次检查并继续拆解。跳过只对下一次拆解请求生效，不会保存为项目设置，也不会把候选标记为已确认。</p>
      </div>
      <template #footer><button class="button ghost" :disabled="breakdownLoading" @click="requestSkipScriptBible">跳过并继续</button><button class="button primary" :disabled="breakdownLoading" @click="reviewScriptBibleCandidates"><BookOpen :size="15"/>先确认建议</button></template>
    </BaseModal>
    <BaseModal :open="scriptBibleSkipConfirmOpen" title="再次确认跳过 Bible 建议" eyebrow="ONE-TIME BYPASS" width="600px" @close="!breakdownLoading&&(scriptBibleSkipConfirmOpen=false,scriptBibleGateOpen=true)">
      <div class="script-rebuild-confirm">
        <div><AlertTriangle :size="22"/><span><strong>本次拆解可能无法绑定尚未建立的实体</strong><small>未确认候选不会写入 Bible；新分镜可能缺少对应人物、场景或道具的稳定 UUID 与视觉锚点，需要之后人工补充。</small></span></div>
        <p>这是一次性绕过。关闭弹窗或请求结束后，系统仍会在后续拆解前重新检查候选。</p>
      </div>
      <template #footer><button class="button ghost" :disabled="breakdownLoading" @click="scriptBibleSkipConfirmOpen=false;scriptBibleGateOpen=true">返回审核选择</button><button class="button primary" :disabled="breakdownLoading" @click="confirmSkipScriptBible"><AlertTriangle :size="15"/>确认跳过并继续</button></template>
    </BaseModal>
    <BaseModal :open="rebuildConfirmOpen" title="确认重建剧集与分镜" eyebrow="DESTRUCTIVE SCRIPT REBUILD" width="620px" @close="!breakdownLoading&&closeRebuildConfirmation()">
      <div v-if="project" class="script-rebuild-confirm">
        <div><AlertTriangle :size="22"/><span><strong>将以当前母稿和当前 Bible 重建</strong><small>现有 {{ project.episodes.length }} 集、{{ totalShots }} 个分镜会被替换。新分镜会重新绑定当前的林澈等 Bible 对象，不会继续引用已删除的 UUID。</small></span></div>
        <ul>
          <li>会清理：旧分镜、提示词、创作草稿、逐镜扩写、终态生成任务、组装记录、草稿发行候选和失效翻译。</li>
          <li>会保留：项目、当前 Bible、素材库、封面、风格、全剧视觉规则、术语表及物理输出文件。</li>
          <li>若仍有运行中/远端未终结任务，或已有封存/发行版本，服务端会停止重建且不修改任何数据。</li>
        </ul>
        <p>数据库中的旧制作记录删除后不可恢复。物理输出文件不会在本次请求中直接删除，可由后续存储清理流程回收。</p>
      </div>
      <template #footer><button class="button ghost" :disabled="breakdownLoading" @click="closeRebuildConfirmation">取消</button><button class="button primary" :disabled="breakdownLoading" @click="confirmProjectRebuild"><Trash2 :size="15"/>{{ breakdownLoading?'正在清理并重建…':'确认清理并重建' }}</button></template>
    </BaseModal>
    <BaseModal :open="wholeEpisodeOpen" title="生成整集" eyebrow="WHOLE EPISODE BATCH" width="560px" @close="!episodeGenerationLoadingId&&(wholeEpisodeOpen=false)">
      <ProductionWorkflowPicker v-if="wholeEpisodeHasRef2VA" v-model:template-id="wholeRef2vaTemplateId" v-model:parameters="wholeRef2vaParameters" :templates="productionWorkflowTemplates" :loading="productionWorkflowTemplatesLoading" :error="productionWorkflowTemplatesError" :disabled="Boolean(wholeEpisode&&episodeIdempotencyKeys[wholeEpisode.id])" h3-mode="Ref2VA"/>
      <div v-if="wholeEpisode" class="whole-episode-confirm"><div><Sparkles :size="22"/><span><strong>第 {{ wholeEpisode.number }} 集 · {{ wholeEpisode.title }}</strong><small>将逐镜创建 {{ wholeEpisode.shots.length }} 个独立任务：{{ episodeVideoCount(wholeEpisode) }} 个 H3 视频、{{ episodeImageCount(wholeEpisode) }} 个 SDXL 图片；失败分镜可单独重试。</small></span></div><div v-if="wholeEpisodeExtendedConfirmed"><AlertTriangle :size="18"/><span><strong>请再次确认长任务</strong><small>本集 {{ wholeEpisode.shots.length }} 镜会全部保留，并按分镜独立入队；超过队列建议值只会增加等待时间、模型执行时间与 API 用量，不会截断镜数。</small></span></div><div v-if="wholeEpisodeIdempotencyConflict" class="whole-episode-idempotency-conflict"><AlertTriangle :size="16"/><span><strong>已停止自动重提</strong><small>{{ wholeEpisodeIdempotencyConflict }}</small></span></div><ResolutionProfilePicker v-if="episodeVideoCount(wholeEpisode)" v-model="wholeEpisodeProfile" v-model:multiple="wholeEpisodeMultiple" label="H3 视频分辨率档位" :disabled="Boolean(episodeIdempotencyKeys[wholeEpisode.id])"/><details v-if="episodeImageCount(wholeEpisode)" class="whole-episode-image-resolution" open><summary>图片输出参数 · 推荐 0.98 MP</summary><ResolutionProfilePicker v-model="wholeEpisodeImageProfile" v-model:multiple="wholeEpisodeImageMultiple" :multiple-step="8" label="SDXL 图片分辨率档位" :disabled="Boolean(episodeIdempotencyKeys[wholeEpisode.id])"/><p>SDXL 推荐至少 0.8 MP，默认 0.98 MP；图片宽高按 8 的倍数取整；0.1–0.3 MP 仅用于连通性测试，不建议作为交付图片。</p></details><small v-if="episodeIdempotencyKeys[wholeEpisode.id]">上次响应未确认，重试将保持视频/图片各自档位、尺寸倍数与幂等键。</small><button v-if="episodeIdempotencyKeys[wholeEpisode.id]" type="button" class="button ghost small" @click="abandonWholeEpisodeRetryState">核对任务后放弃旧尝试</button><p>网络错误后重试会复用本集当前幂等键，成功返回后才会清除，避免重复创建任务。图片作为独立定帧产物，不参与本集视频组装。</p></div>
      <template #footer><button class="button ghost" :disabled="Boolean(episodeGenerationLoadingId)" @click="wholeEpisodeOpen=false">取消</button><button class="button primary" :disabled="Boolean(episodeGenerationLoadingId)||Boolean(wholeEpisodeIdempotencyConflict)||!wholeEpisode||Boolean(canonicalGlossaryBlockedReason)" :title="wholeEpisodeIdempotencyConflict||canonicalGlossaryBlockedReason||''" @click="generateWholeEpisode"><Sparkles :size="15"/>{{ episodeGenerationLoadingId?'正在创建批次…':wholeEpisodeExtendedConfirmed?`确认并生成 ${wholeEpisode?.shots.length||0} 个分镜任务`:`生成 ${wholeEpisode?.shots.length||0} 个分镜任务` }}</button></template>
    </BaseModal>
    <BaseModal :open="!!bibleImagePreview" title="候选参考图完整预览" eyebrow="BIBLE IMAGE CANDIDATE" width="920px" @close="bibleImagePreview=null">
      <div v-if="bibleImagePreview" class="bible-image-full-preview"><img :src="bibleImagePreview.src" :alt="bibleImagePreview.title"><footer><span><strong>{{ bibleImagePreview.title }}</strong><small>{{ bibleImagePreview.filename }}</small></span><a v-if="bibleImagePreview.downloadUrl" class="button ghost small" :href="bibleImagePreview.downloadUrl" target="_blank" rel="noopener"><Download :size="13"/>打开原图</a></footer></div>
    </BaseModal>
    <BaseModal :open="addBibleOpen" :title="`新增${bibleSections.find(s=>s.value===bibleSection)?.singular}设定`" eyebrow="NEW CONTINUITY ANCHOR" width="1040px" @close="closeAddBibleModal">
      <div class="bible-draft-editor">
        <div class="bible-draft-evidence"><Sparkles :size="17"/><span><strong>可从素材库说明快速生成，也可完全手工填写</strong><small>自动草稿只读取所选素材已保存的名称、基础描述和视觉锚点元数据，不读取或识别图片像素。生成只填充本弹窗，创建前所有字段都可修改。</small></span><b>METADATA ONLY</b></div>

        <section class="bible-asset-browser">
          <header><div><span class="impact-label-row">选择 1 张参考图 <PromptImpactBadge kind="continuity"/><PromptImpactBadge kind="production" title="创建后绑定到 Bible 素材池；具体镜头仍需明确选择才会接入视频工作流。"/></span><small>显示素材库全部图片；只有与当前{{ bibleSections.find(s=>s.value===bibleSection)?.singular }}类别一致的素材可选，切换选择会替换上一张。</small></div><div class="bible-asset-tools"><label><Search :size="14"/><input v-model="bibleAssetSearch" placeholder="搜索名称、描述、视觉锚点"></label><select v-model="bibleAssetCategory"><option value="compatible">仅可用于当前设定</option><option value="all">全部图片</option><option value="character">人物</option><option value="scene">场景</option><option value="prop">道具</option><option value="image">未分类图片</option></select></div></header>
          <div v-if="bibleAssetCatalogLoading" class="bible-asset-loading"><div class="loader-ring"></div><span>正在独立加载素材库全部参考图…</span></div>
          <div v-else-if="bibleAssetCatalogError" class="bible-draft-error"><AlertTriangle :size="14"/><span>{{ bibleAssetCatalogError }}</span><button type="button" @click="openAddBibleModal"><RefreshCw :size="13"/>重试</button></div>
          <div v-else-if="filteredBibleAssetCatalog.length" class="bible-asset-catalog">
            <article v-for="asset in filteredBibleAssetCatalog" :key="asset.id" :class="{active:bibleDraft.referenceAssetIds[0]===asset.id,incompatible:!isBibleDraftAssetCompatible(asset)}">
              <button type="button" class="bible-asset-select" :disabled="!isBibleDraftAssetCompatible(asset)||bibleCreatePhase!=='idle'||bibleDraftGenerating" :title="isBibleDraftAssetCompatible(asset)?`选择 ${asset.name}`:bibleDraftAssetUnavailableReason(asset)" @click="selectBibleDraftAsset(asset)"><AssetImage :src="asset.thumbnail" :label="asset.name" :accent="currentStyle?.accent"/><span><strong>{{ asset.name }}</strong><small>{{ bibleAssetTypeLabel(asset) }}<template v-if="asset.isActive===false"> · 已停用</template><template v-else-if="!asset.promptHint"> · 缺视觉锚点</template></small></span><i><Check :size="12"/></i></button>
              <button type="button" class="bible-asset-preview" :aria-label="`预览 ${asset.name}`" @click="bibleAssetPreview=asset"><Eye :size="13"/></button>
            </article>
          </div>
          <div v-else class="bible-asset-empty"><Image :size="22"/><span>没有匹配的参考图</span><button v-if="bibleAssetSearch||bibleAssetCategory!=='compatible'" type="button" @click="bibleAssetSearch='';bibleAssetCategory='compatible'">清除筛选</button></div>
          <footer><span>素材库 {{ bibleAssetCatalog.length }} 张图片 · 当前显示 {{ filteredBibleAssetCatalog.length }} 张</span><RouterLink to="/assets" target="_blank">维护素材名称与说明 <ArrowRight :size="12"/></RouterLink></footer>
        </section>

        <section class="bible-draft-assistant" :class="{ready:!!bibleDraftAsset}">
          <div class="bible-draft-assistant-copy"><Languages :size="18"/><span><strong>根据所选素材自动补充</strong><small v-if="bibleDraftAsset">依据“{{ bibleDraftAsset.name }}”当前已保存的素材说明；不满意可修改说明后重试。</small><small v-else>先从上方选择一张同类别参考图。</small></span></div>
          <div class="bible-draft-language"><span>草稿语言</span><button type="button" :class="{active:bibleDraftLanguage==='zh'}" :disabled="bibleDraftGenerating" @click="setBibleDraftLanguage('zh')">中文</button><button type="button" :class="{active:bibleDraftLanguage==='en'}" :disabled="bibleDraftGenerating" @click="setBibleDraftLanguage('en')">English</button></div>
          <button type="button" class="button primary" :disabled="!bibleDraftAsset||bibleDraftGenerating||bibleCreatePhase!=='idle'" @click="generateBibleDraftFromAsset"><RefreshCw v-if="bibleDraftGenerating" :size="14" class="spinning"/><WandSparkles v-else :size="14"/>{{ bibleDraftGenerating?'正在生成草稿…':bibleDraftGenerateResult?'重新生成草稿':'自动补充三个字段' }}</button>
        </section>
        <div v-if="bibleDraftGenerateError" class="bible-draft-error"><AlertTriangle :size="14"/><span>{{ bibleDraftGenerateError }}</span><button type="button" :disabled="bibleDraftGenerating" @click="generateBibleDraftFromAsset"><RefreshCw :size="13"/>重试</button></div>
        <div v-if="bibleDraftStale" class="bible-draft-stale"><AlertTriangle :size="14"/><span><strong>草稿依据已变化</strong><small>你已切换参考图或草稿语言。请按当前选择重新生成，或明确放弃旧生成记录后继续手工填写。</small></span><button type="button" @click="clearStaleBibleDraftProvenance">保留字段并放弃旧记录</button></div>
        <div v-if="bibleDraftGenerateResult&&!bibleDraftStale" class="bible-draft-provenance"><Check :size="14"/><span><strong>草稿已生成 · 未创建 Bible</strong><small>依据：素材元数据 · 未使用像素识图<template v-if="bibleDraftGenerateResult.sourceMetadataSha256"> · 元数据 SHA {{ bibleDraftGenerateResult.sourceMetadataSha256.slice(0,12) }}…</template></small></span><code>{{ bibleDraftGenerateResult.sourceAssets[0]?.checksum?.slice(0,12) }}…</code></div>
        <div v-if="bibleDraftGenerateResult?.warnings.length" class="bible-draft-warnings"><AlertTriangle :size="14"/><span><strong>生成提示</strong><small v-for="warning in bibleDraftGenerateResult.warnings" :key="warning">{{ warning }}</small></span></div>
        <div v-if="Object.keys(bibleDraftProposal).length" class="bible-draft-conflicts"><header><span><strong>保留了你的人工编辑</strong><small>以下候选没有自动覆盖对应字段。可逐项查看并明确采用。</small></span><button type="button" @click="applyAllBibleDraftCandidates">采用全部候选</button></header><article v-for="(candidate,field) in bibleDraftProposal" :key="field"><span><b>{{ field==='name'?'名称':field==='description'?'基础描述':'视觉锚点' }}</b><small>{{ candidate }}</small></span><button type="button" @click="applyBibleDraftCandidateField(field)">采用此候选</button></article></div>
        <div v-if="bibleCreateError" class="bible-draft-error"><AlertTriangle :size="14"/><span>{{ bibleCreateError }}</span><button type="button" @click="bibleCreateError=''">关闭</button></div>

        <div class="form-stack bible-draft-fields">
          <label class="field"><span class="impact-label-row">名称 * <PromptImpactBadge kind="label"/><PromptImpactBadge kind="indirect" title="用于母稿拆解的精确匹配；自动草稿默认复制素材名称，仍可修改。"/></span><input v-model="bibleDraft.name" :disabled="bibleCreatePhase!=='idle'" placeholder="可使用中文；项目内需唯一" @input="markBibleDraftDirty('name')"><small>自动草稿直接采用素材库名称，不让模型猜名；创建前可人工修改。</small></label>
          <label class="field"><span class="impact-label-row">基础描述 <PromptImpactBadge kind="indirect" title="供创作与管理阅读，不会直接进入 H3。"/></span><textarea v-model="bibleDraft.description" :disabled="bibleCreatePhase!=='idle'" rows="3" placeholder="可用中文或英文记录创作意图；不会直接进入 H3" @input="markBibleDraftDirty('description')"></textarea></label>
          <label class="field"><span class="impact-label-row">视觉锚点（中文或英文）* <PromptImpactBadge kind="prompt"/><PromptImpactBadge kind="continuity"/></span><textarea v-model="bibleDraft.visualPrompt" :disabled="bibleCreatePhase!=='idle'" rows="4" placeholder="只写不可变化的外观、结构、材质、颜色和固定标记，不写动作或运镜。" @input="markBibleDraftDirty('visualPrompt')"></textarea><small>写中文：创建后生成待审核英文候选；写英文：直接保存。自动补充内容也必须由你确认后再创建。</small></label>
        </div>
        <div v-if="bibleCreatePhase!=='idle'" class="canonical-create-progress"><div class="loader-ring"></div><span><strong>{{ bibleCreatePhase==='saving'?'正在保存 Bible…':'Bible 已保存，正在处理名称术语与视觉锚点…' }}</strong><small>{{ bibleCreatePhase==='canonicalizing'?'中文视觉锚点只会产生待审核英文候选，不会覆盖原文或自动确认。':'保存成功后才会按需调用本地 Qwen。' }}</small></span></div>
        <PromptWritingGuide scope="bible"/>
      </div>
      <template #footer><button class="button ghost" :disabled="bibleCreatePhase!=='idle'||bibleDraftGenerating" @click="closeAddBibleModal">取消</button><button class="button primary" :disabled="bibleCreatePhase!=='idle'||bibleDraftGenerating||bibleDraftStale" :title="bibleDraftStale?'请先重新生成草稿或放弃旧生成记录':''" @click="addBibleItem"><Lock :size="15"/>{{ bibleCreatePhase==='saving'?'正在保存设定…':bibleCreatePhase==='canonicalizing'?'正在生成英文候选…':'确认字段并创建设定' }}</button></template>
    </BaseModal>
    <BaseModal :open="!!bibleAssetPreview" title="参考图完整预览" eyebrow="ASSET PREVIEW" width="920px" @close="bibleAssetPreview=null"><div v-if="bibleAssetPreview" class="bible-asset-full-preview"><AssetImage :src="bibleAssetPreview.thumbnail||bibleAssetPreview.mediaUrl" :label="bibleAssetPreview.name"/><footer><span><strong>{{ bibleAssetPreview.name }}</strong><small>{{ bibleAssetTypeLabel(bibleAssetPreview) }} · {{ bibleAssetPreview.description || '未填写基础描述' }}</small></span><button v-if="isBibleDraftAssetCompatible(bibleAssetPreview)" type="button" class="button primary small" @click="selectBibleDraftAsset(bibleAssetPreview);bibleAssetPreview=null"><Check :size="13"/>选作参考图</button></footer></div></BaseModal>
    <BaseModal :open="addEpisodeOpen" title="新增连续剧集" eyebrow="NEXT EPISODE" @close="addEpisodeOpen=false">
      <div class="next-episode-banner"><span>EP</span><strong>{{ String(project.episodes.length+1).padStart(2,'0') }}</strong><p>自动继承 {{ project.bible.characters.length }} 位角色、{{ project.bible.scenes.length }} 个场景与 {{ project.bible.props.length }} 件道具的视觉锚点。</p></div><div class="form-stack"><label class="field"><span class="impact-label-row">本集标题 <PromptImpactBadge kind="label" title="用于剧集、任务和成片识别；不会原样写入单镜提示词。"/></span><input v-model="episodeDraft.title" :placeholder="`第 ${project.episodes.length+1} 集`"></label><label class="field"><span class="impact-label-row">本集梗概 <PromptImpactBadge kind="indirect" title="用于本地 Qwen 理解本集起因、转折与钩子；不会原样复制进正式提示词。"/></span><textarea v-model="episodeDraft.synopsis" rows="4" placeholder="描述本集的起因、升级、转折与钩子。"></textarea></label><label class="field"><span class="impact-label-row">本集剧本原文 <PromptImpactBadge kind="indirect" title="本地 Qwen 创作分镜时读取；对白只能从此处或全剧母稿精确引用，未应用草稿不会改变正式生产。"/></span><textarea v-model="episodeDraft.script" rows="8" placeholder="粘贴本集场景、动作和中文对白原文。建议保留角色名与场次；留空时使用全剧母稿。"></textarea><small>这是创作事实来源，不会整段塞入 H3；自动草稿中的对白正文必须是这里或全剧母稿的精确子串。</small></label><div class="inherit-note"><Lock :size="15"/><span>本集将使用项目默认的 <strong>{{ workflowLabel(project.defaultMode) }}</strong>，并开启多分镜与原生音频；可在分镜编辑器逐镜调整。</span></div></div>
      <template #footer><button class="button ghost" @click="addEpisodeOpen=false">取消</button><button class="button primary" @click="addEpisode"><Plus :size="15"/>新增并编辑分镜</button></template>
    </BaseModal>
  </div>
  <section v-else class="panel project-load-failure" role="alert">
    <AlertTriangle :size="30" />
    <h2>项目加载失败</h2>
    <p>{{ projectLoadError || '没有找到这个连续剧项目。' }}</p>
    <div><button type="button" class="button ghost" @click="router.push('/projects')"><ArrowLeft :size="15"/>返回项目列表</button><button type="button" class="button primary" @click="loadProject"><RefreshCw :size="15"/>重新加载</button></div>
  </section>
</template>

<style scoped>
.bible-image-global-state{display:flex;align-items:center;gap:8px;padding:9px 11px;border:1px solid #283039;border-radius:7px;background:#0b0f13;color:#838d95;font-size:10px}.bible-image-global-state>span{flex:1}.bible-image-global-state button{display:flex;align-items:center;gap:5px;border:0;background:none;color:#d6a054;cursor:pointer}.bible-image-global-state.error{border-color:#593039;color:#e78c94}.bible-image-prompt-config{margin-top:12px;padding:12px;border:1px solid #2b3540;border-radius:8px;background:#0a0f13}.bible-image-prompt-config>summary,.bible-image-negative-config>summary,.bible-image-prompt-inputs>summary{display:flex;align-items:center;justify-content:space-between;gap:12px;cursor:pointer;color:#cdd5db}.bible-image-prompt-config>summary span{display:flex;flex-direction:column;gap:3px}.bible-image-prompt-config small,.bible-image-prompt-inputs small{color:#74808a;font-size:9px}.bible-image-prompt-config>.form-grid,.bible-image-negative-config>.form-grid{margin-top:12px}.bible-image-negative-config{grid-column:1/-1;margin-top:10px;padding-top:10px;border-top:1px solid #202a33}.bible-image-candidate{grid-column:1/-1;display:flex;flex-direction:column;gap:9px;padding:12px;border:1px solid #252d34;border-radius:8px;background:#090d11}.bible-image-candidate>header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.bible-image-candidate>header>span{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.bible-image-candidate>header small{width:100%;padding-left:21px;color:#707982;font-size:9px}.bible-image-candidate>label{display:flex;flex-direction:column;gap:5px;color:#858e96;font-size:9px}.bible-image-candidate textarea{box-sizing:border-box;width:100%;padding:8px;border:1px solid #28313a;border-radius:6px;background:#080c0f;color:#dce0e3;resize:vertical}.bible-image-prompt-inputs{padding:10px;border:1px solid #263442;border-radius:7px;background:#0b1015}.bible-image-prompt-inputs>p{margin:8px 0;color:#77838d;font-size:9px;line-height:1.5}.bible-image-prompt-source-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px}.bible-image-prompt-source-grid .field{gap:5px}.bible-image-prompt-source-grid .field span{display:flex;align-items:center;justify-content:space-between;gap:8px}.bible-image-prompt-source-grid .field small{font-weight:400}.bible-image-prompt-preview{display:grid;gap:8px;margin-top:10px;padding:9px;border:1px solid #1f2a33;border-radius:6px;background:#080c10}.bible-image-prompt-preview strong{font-size:9px;color:#93a0aa}.bible-image-prompt-preview p{margin:4px 0 0;color:#d6dde2;font-size:9px;line-height:1.55}.bible-image-retry,.bible-image-error{display:flex;align-items:flex-start;gap:7px;margin:0;padding:8px 10px;border-radius:6px;font-size:9px;line-height:1.5}.bible-image-retry{border:1px solid #55421f;background:#1b160c;color:#cda45e}.bible-image-retry span{flex:1}.bible-image-retry button{border:0;background:none;color:#e3b45e;cursor:pointer}.bible-image-error{border:1px solid #5b2932;background:#1e1014;color:#ec8d96}.bible-image-empty{margin:0;color:#6f7880;font-size:9px}.bible-image-status{display:flex;flex-direction:column;gap:8px;padding-top:9px;border-top:1px solid #222a31}.bible-image-status>div:first-child{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.bible-image-status code{color:#70808e;font-size:8px}.bible-image-status>span{align-self:flex-start;padding:3px 7px;border:1px solid #31553e;border-radius:10px;color:#6fc68f;font-size:8px}.bible-image-status.stale>span{border-color:#6a4927;color:#df9e5c}.bible-image-status>p,.bible-image-status li{display:flex;align-items:flex-start;gap:6px;margin:0;color:#dda063;font-size:9px;line-height:1.5}.bible-image-status ul{display:grid;gap:5px;margin:0;padding:9px 10px;list-style:none;border:1px solid #4d3b22;border-radius:6px;background:#17130c}.bible-image-task-actions{display:flex!important;justify-content:flex-end}.bible-image-outputs{display:grid!important;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:9px!important}.bible-image-outputs article{display:flex;flex-direction:column;gap:6px;padding:7px;border:1px solid #293139;border-radius:7px;background:#0c1115}.bible-image-outputs img,.bible-image-outputs article>div{width:100%;aspect-ratio:1/1;border-radius:5px;object-fit:cover;background:#11171c}.bible-image-outputs article>div{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:5px;color:#68727b;font-size:8px}.bible-image-outputs article>span{overflow:hidden;color:#79838b;font-size:8px;text-overflow:ellipsis;white-space:nowrap}.bible-image-history{display:flex;flex-direction:column;gap:10px}.bible-image-history>article{display:flex;flex-direction:column;gap:8px;padding:9px;border:1px solid #252e36;border-radius:7px;background:#0a0f13}.bible-image-history>article.stale{border-color:#5b3c23;background:#15100a}.bible-image-history header{display:flex;align-items:center;justify-content:space-between;gap:9px}.bible-image-history header>span{display:flex;min-width:0;flex-direction:column;gap:3px}.bible-image-history header strong{color:#cfd5da;font-size:10px}.bible-image-history header small{color:#74808a;font-size:8px}.bible-image-history p{display:flex;align-items:flex-start;gap:6px;margin:0;color:#dda063;font-size:9px;line-height:1.5}.bible-image-preview-button{position:relative;width:100%;aspect-ratio:1/1;padding:0;overflow:hidden;border:0;border-radius:5px;background:#11171c;cursor:zoom-in}.bible-image-preview-button img{width:100%;height:100%;object-fit:cover}.bible-image-preview-button>span{position:absolute;right:5px;bottom:5px;display:flex;align-items:center;gap:4px;padding:3px 6px;border-radius:999px;background:rgba(5,8,11,.78);color:#d8dde1;font-size:8px}.bible-image-full-preview{display:flex;flex-direction:column;gap:10px}.bible-image-full-preview>img{width:100%;height:min(72vh,720px);object-fit:contain;border-radius:8px;background:#080b0e}.bible-image-full-preview>footer{display:flex;align-items:center;justify-content:space-between;gap:14px}.bible-image-full-preview>footer>span{display:flex;min-width:0;flex-direction:column;gap:4px}.bible-image-full-preview strong{font-size:12px}.bible-image-full-preview small{overflow:hidden;color:#737c84;font-size:9px;text-overflow:ellipsis;white-space:nowrap}
.cover-picker{display:grid;grid-template-columns:112px minmax(0,1fr);align-items:center;gap:15px}.cover-picker-preview{width:112px;aspect-ratio:2/3;padding:0;overflow:hidden;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:7px;border:1px dashed rgba(230,167,90,.38);border-radius:7px;color:#d7a45e;cursor:pointer}.cover-picker-preview img{width:100%;height:100%;object-fit:cover}.cover-picker-preview span{font-size:9px}.cover-picker>div{display:flex;min-width:0;flex-direction:column;gap:7px}.cover-picker strong{overflow:hidden;color:#c1c5ca;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.cover-picker small{color:#697078;font-size:9px;line-height:1.55}.cover-picker-actions{display:flex;gap:7px}.cover-error{margin-top:9px;color:#e4868d;font-size:10px;line-height:1.5}.new-cover-preview-image{position:absolute!important;inset:0;z-index:0!important;width:100%;height:100%;object-fit:cover}.new-project-preview:has(.new-cover-preview-image):after{content:'';position:absolute;inset:0;z-index:1;background:linear-gradient(180deg,rgba(5,7,9,.15),rgba(5,7,9,.78))}.series-cover-background{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:.42}.series-poster>img{width:100%;height:100%;border-radius:inherit;object-fit:cover}.series-header-actions{flex-wrap:wrap;justify-content:flex-end}.header-cover-error{width:100%;margin:0;text-align:right}
.impact-label-row{display:flex!important;align-items:center;flex-wrap:wrap;gap:6px}.impact-heading{display:flex;align-items:center;flex-wrap:wrap;gap:7px}.impact-heading h3{margin-right:2px}.script-editor-stack{min-width:0;display:flex;flex-direction:column;gap:10px}.bible-impact-label{display:flex;align-items:center;flex-wrap:wrap;gap:5px;min-height:20px;font-size:10px!important;letter-spacing:.04em!important;color:#777d85!important}.bible-card-title span{font-size:9px!important;color:#8e7655!important}.management-only-note{max-width:118px;color:#6d737a;font-size:10px;line-height:1.35;text-align:right}.bible-card-actions .lock-button{height:auto;min-height:32px;padding:6px 9px;text-align:left}.global-rules header>div>p{max-width:760px;margin-top:5px;font-size:10px;line-height:1.55;color:#6f757d}.global-rules>.translation-review{margin-top:14px}.global-rules .field>small{position:static;font-size:10px;line-height:1.5}.global-rules .glossary-field{grid-column:1/-1}.global-rules .glossary-field textarea{font-family:Consolas,monospace;line-height:1.6}.global-rules .glossary-field>em{font-size:9px;font-style:normal}.global-rules .glossary-field>.glossary-ok{color:#70c9ae}.global-rules .glossary-field>.glossary-error{color:#e9867b}.form-stack>.prompt-writing-guide{margin-top:2px}
.project-load-failure{max-width:620px;margin:42px auto;padding:42px;display:flex;align-items:center;flex-direction:column;gap:12px;text-align:center;color:#d99651}.project-load-failure h2{margin:0;color:#e4e8eb}.project-load-failure p{max-width:520px;margin:0;color:#7d878f;line-height:1.6}.project-load-failure>div{display:flex;gap:9px;margin-top:8px}
.script-rebuild-confirm{display:flex;flex-direction:column;gap:14px}.script-rebuild-confirm>div{display:flex;align-items:flex-start;gap:10px;padding:12px;border:1px solid rgba(229,107,99,.3);border-radius:8px;background:rgba(229,107,99,.06);color:#df8178}.script-rebuild-confirm>div>span{display:flex;flex-direction:column;gap:4px}.script-rebuild-confirm strong{font-size:12px;color:#e6a099}.script-rebuild-confirm small,.script-rebuild-confirm li,.script-rebuild-confirm p{font-size:9px;line-height:1.6;color:#8f8585}.script-rebuild-confirm ul{margin:0;padding:0 0 0 20px;display:grid;gap:6px}.script-rebuild-confirm p{margin:0;padding:10px;border-radius:7px;background:#0b0f13;color:#777f86}
.glossary-status-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:7px}.glossary-status-row em{font-size:10px;font-style:normal}.glossary-status-row .glossary-ok{color:#70c9ae}.glossary-status-row .glossary-error{color:#e9867b}.canonical-notice{padding:9px 11px;border:1px solid rgba(104,197,167,.25);border-radius:7px;background:rgba(104,197,167,.06);color:#8bd8be;font-size:10px;line-height:1.5}.canonical-notice.warning{border-color:rgba(218,154,70,.3);background:rgba(218,154,70,.07);color:#d7a65f}.bible-canonical-notice{display:flex;align-items:flex-start;gap:8px;margin:-2px 0 4px}.bible-canonical-notice>strong{flex:0 0 auto;color:inherit;font-size:9px;letter-spacing:.04em}.bible-canonical-notice>span{min-width:0}.visual-translation-notice{border-color:rgba(94,164,218,.25);background:rgba(94,164,218,.06);color:#8bc5e7}.visual-translation-notice.warning{border-color:rgba(218,154,70,.3);background:rgba(218,154,70,.07);color:#d7a65f}.bible-canonical-row{display:flex;align-items:center;gap:8px;min-height:25px;padding:5px 8px;border:1px solid rgba(104,197,167,.18);border-radius:6px;background:rgba(104,197,167,.035)}.bible-canonical-row>span{color:#59b99a;font-size:7px;letter-spacing:.14em}.bible-canonical-row>strong{overflow:hidden;color:#9acdbd;font-family:Consolas,monospace;font-size:9px;font-weight:500;text-overflow:ellipsis;white-space:nowrap}.bible-canonical-row.missing{border-color:rgba(218,154,70,.2);background:rgba(218,154,70,.035)}.bible-canonical-row.missing>span,.bible-canonical-row.missing>strong{color:#a98452}.canonical-create-hint,.canonical-create-progress{display:flex;gap:10px;align-items:flex-start;padding:11px 12px;border:1px solid rgba(104,197,167,.22);border-radius:7px;background:rgba(104,197,167,.05);color:#67c5a6}.canonical-create-hint>span,.canonical-create-progress>span{display:flex;min-width:0;flex-direction:column;gap:3px}.canonical-create-hint strong,.canonical-create-progress strong{font-size:11px;color:#a8dccb}.canonical-create-hint small,.canonical-create-progress small{font-size:9px;line-height:1.5;color:#72978a}.canonical-create-progress .loader-ring{width:16px;height:16px;flex:0 0 auto}.spinning{animation:spin 1s linear infinite}
.bible-draft-editor{display:flex;flex-direction:column;gap:13px}.bible-draft-evidence{display:flex;align-items:flex-start;gap:10px;padding:12px;border:1px solid rgba(104,197,167,.25);border-radius:8px;background:rgba(104,197,167,.05);color:#64c5a5}.bible-draft-evidence>span{display:flex;min-width:0;flex:1;flex-direction:column;gap:4px}.bible-draft-evidence strong{font-size:11px;color:#a7daca}.bible-draft-evidence small{font-size:9px;line-height:1.55;color:#71958a}.bible-draft-evidence>b{padding:4px 7px;border:1px solid rgba(104,197,167,.25);border-radius:999px;font-size:7px;letter-spacing:.1em}.bible-asset-browser{overflow:hidden;border:1px solid #293038;border-radius:9px;background:#0d1115}.bible-asset-browser>header{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;padding:12px;border-bottom:1px solid #242a31}.bible-asset-browser>header>div:first-child{display:flex;flex-direction:column;gap:5px}.bible-asset-browser>header span{font-size:10px;color:#aeb5bb}.bible-asset-browser>header small{font-size:8px;line-height:1.45;color:#677078}.bible-asset-tools{display:flex;align-items:center;gap:7px}.bible-asset-tools label{width:240px;height:32px;padding:0 9px;display:flex;align-items:center;gap:7px;border:1px solid #2d343c;border-radius:6px;background:#090c10;color:#69727b}.bible-asset-tools input{min-width:0;flex:1;border:0;outline:0;background:transparent;color:#c4c9ce;font-size:9px}.bible-asset-tools select{height:32px;border:1px solid #2d343c;border-radius:6px;background:#090c10;color:#9fa6ac;font-size:9px}.bible-asset-catalog{max-height:330px;padding:10px;display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;overflow-y:auto}.bible-asset-catalog article{min-width:0;height:150px;position:relative;overflow:hidden;border:1px solid #2a3037;border-radius:7px;background:#11161b}.bible-asset-catalog article.active{border-color:#d99b48;box-shadow:0 0 0 1px rgba(217,155,72,.2)}.bible-asset-catalog article.incompatible{opacity:.48}.bible-asset-select{width:100%;height:100%;padding:0;position:relative;overflow:hidden;border:0;background:transparent;text-align:left;cursor:pointer}.bible-asset-select:disabled{cursor:not-allowed}.bible-asset-select img{width:100%;height:105px;object-fit:cover}.bible-asset-select>span{height:43px;padding:5px 7px;display:flex;min-width:0;flex-direction:column;gap:2px}.bible-asset-select strong{overflow:hidden;color:#c2c7cc;font-size:9px;text-overflow:ellipsis;white-space:nowrap}.bible-asset-select small{overflow:hidden;color:#687179;font-size:7px;text-overflow:ellipsis;white-space:nowrap}.bible-asset-select>i{width:18px;height:18px;position:absolute;right:6px;top:6px;display:none;align-items:center;justify-content:center;border-radius:50%;background:#d99b48;color:#16100a}.bible-asset-catalog article.active .bible-asset-select>i{display:flex}.bible-asset-preview{width:24px;height:24px;position:absolute;right:5px;top:76px;display:flex;align-items:center;justify-content:center;border:1px solid rgba(255,255,255,.18);border-radius:5px;background:rgba(7,9,12,.8);color:#ccd1d5;cursor:pointer}.bible-asset-browser>footer{display:flex;justify-content:space-between;gap:12px;padding:8px 11px;border-top:1px solid #242a31;color:#68717a;font-size:8px}.bible-asset-browser>footer a{display:flex;align-items:center;gap:5px;color:#c7914b}.bible-asset-loading,.bible-asset-empty{min-height:150px;display:flex;align-items:center;justify-content:center;gap:9px;color:#737d85;font-size:10px}.bible-asset-empty{flex-direction:column}.bible-asset-empty button{border:0;background:transparent;color:#c48d48;cursor:pointer}.bible-draft-assistant{padding:11px 12px;display:grid;grid-template-columns:minmax(0,1fr) auto auto;align-items:center;gap:13px;border:1px solid #2b3239;border-radius:8px;background:#0c1014}.bible-draft-assistant.ready{border-color:rgba(217,155,72,.28);background:rgba(217,155,72,.035)}.bible-draft-assistant-copy{display:flex;align-items:flex-start;gap:9px;color:#d39a50}.bible-draft-assistant-copy>span{display:flex;flex-direction:column;gap:3px}.bible-draft-assistant-copy strong{font-size:10px}.bible-draft-assistant-copy small{font-size:8px;color:#737b82}.bible-draft-language{height:32px;padding:3px;display:flex;align-items:center;gap:3px;border:1px solid #2c333a;border-radius:7px;background:#090c10}.bible-draft-language>span{padding:0 6px;color:#6e777f;font-size:8px}.bible-draft-language button{height:24px;padding:0 9px;border:0;border-radius:5px;background:transparent;color:#757e86;font-size:8px;cursor:pointer}.bible-draft-language button.active{background:rgba(217,155,72,.14);color:#e2a657}.bible-draft-error,.bible-draft-stale,.bible-draft-provenance,.bible-draft-warnings{display:flex;align-items:flex-start;gap:8px;padding:10px 11px;border:1px solid rgba(239,115,123,.27);border-radius:7px;background:rgba(239,115,123,.055);color:#df7f88}.bible-draft-error span{min-width:0;flex:1;font-size:9px;line-height:1.5}.bible-draft-error button,.bible-draft-stale button{display:flex;align-items:center;gap:4px;border:0;background:transparent;color:inherit;font-size:8px;cursor:pointer}.bible-draft-stale{border-color:rgba(217,155,72,.28);background:rgba(217,155,72,.055);color:#d49b50}.bible-draft-stale>span,.bible-draft-provenance>span,.bible-draft-warnings>span{display:flex;min-width:0;flex:1;flex-direction:column;gap:3px}.bible-draft-stale strong,.bible-draft-provenance strong,.bible-draft-warnings strong{font-size:9px}.bible-draft-stale small,.bible-draft-provenance small,.bible-draft-warnings small{font-size:8px;line-height:1.45}.bible-draft-provenance{border-color:rgba(104,197,167,.24);background:rgba(104,197,167,.05);color:#65c3a4}.bible-draft-provenance code{color:#7b9e93;font-size:8px}.bible-draft-warnings{border-color:rgba(217,155,72,.25);background:rgba(217,155,72,.045);color:#c7924c}.bible-draft-conflicts{overflow:hidden;border:1px solid rgba(91,164,218,.25);border-radius:8px;background:rgba(91,164,218,.035)}.bible-draft-conflicts>header{padding:9px 11px;display:flex;align-items:center;justify-content:space-between;gap:10px;border-bottom:1px solid rgba(91,164,218,.18)}.bible-draft-conflicts>header span{display:flex;flex-direction:column;gap:2px}.bible-draft-conflicts strong{color:#8ec1df;font-size:9px}.bible-draft-conflicts small{font-size:8px;line-height:1.45;color:#718996}.bible-draft-conflicts button{border:0;background:transparent;color:#84bad9;font-size:8px;cursor:pointer}.bible-draft-conflicts article{padding:8px 11px;display:flex;align-items:center;justify-content:space-between;gap:12px;border-top:1px solid rgba(91,164,218,.1)}.bible-draft-conflicts article span{display:flex;min-width:0;flex:1;flex-direction:column;gap:3px}.bible-draft-conflicts article b{font-size:8px;color:#8b9ba5}.bible-draft-conflicts article small{max-height:44px;overflow:auto;color:#9aa9b2}.bible-draft-fields{padding-top:2px}.bible-asset-full-preview{display:flex;flex-direction:column;gap:10px}.bible-asset-full-preview>img{width:100%;height:min(68vh,680px);object-fit:contain;border-radius:8px;background:#080b0e}.bible-asset-full-preview>footer{display:flex;align-items:center;justify-content:space-between;gap:14px}.bible-asset-full-preview>footer>span{display:flex;min-width:0;flex-direction:column;gap:4px}.bible-asset-full-preview strong{font-size:12px}.bible-asset-full-preview small{font-size:9px;color:#737c84}
.whole-episode-image-resolution{padding:9px;border:1px solid rgba(104,197,167,.18);border-radius:7px;background:rgba(104,197,167,.025)}.whole-episode-image-resolution summary{color:#9bc5b7;font-size:10px;font-weight:650;cursor:pointer}.whole-episode-image-resolution>p{margin:7px 0 0!important;color:#74857f!important;font-size:8px!important;line-height:1.55!important}
.whole-episode-idempotency-conflict{display:flex;align-items:flex-start;gap:8px;padding:10px;border:1px solid rgba(223,101,90,.28);border-radius:7px;background:rgba(223,101,90,.055);color:#df8177}.whole-episode-idempotency-conflict>span{display:grid;gap:5px}.whole-episode-idempotency-conflict small{color:#a68585;font-size:9px;line-height:1.55}.whole-episode-idempotency-conflict button{justify-self:start}
.release-workspace{display:flex;flex-direction:column;gap:14px;padding:28px 30px 42px}.release-heading{margin-bottom:0}.release-principles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;padding:0;overflow:hidden;background:rgba(255,255,255,.045)}.release-principles>div{display:flex;gap:12px;align-items:flex-start;padding:17px;background:#101419}.release-principles svg{flex:0 0 auto;color:#d69a47}.release-principles span{display:flex;flex-direction:column;gap:5px}.release-principles strong{font-size:12px;color:#e8ecef}.release-principles small{font-size:10px;line-height:1.55;color:#747d86}.release-error{display:flex;justify-content:space-between;gap:12px;padding:11px 14px;border:1px solid rgba(238,93,106,.32);border-radius:8px;background:rgba(238,93,106,.07);color:#e58a94;font-size:11px}.release-error button{border:0;background:transparent;color:inherit;cursor:pointer}.release-draft,.release-create{padding:20px}.release-draft>header{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}.release-draft h4,.release-history h4{margin:6px 0 4px;font-size:17px}.release-draft header p,.release-create p{margin:0;color:#757e86;font-size:11px;line-height:1.55}.release-updated{font-size:9px;color:#626b73}.release-status,.verification-chip{display:inline-flex;align-items:center;height:22px;padding:0 8px;border:1px solid #39414a;border-radius:999px;color:#8e98a2;font-size:9px}.release-status.draft{border-color:rgba(215,154,70,.35);color:#d99d4a;background:rgba(215,154,70,.07)}.release-status.sealed{border-color:rgba(84,184,221,.35);color:#6fc4e5}.release-status.released{border-color:rgba(82,201,159,.4);color:#62d0aa;background:rgba(82,201,159,.06)}.release-status.superseded{color:#777f87}.verification-chip.verified{border-color:rgba(82,201,159,.28);color:#62cda7}.verification-chip.invalid{border-color:rgba(238,93,106,.35);color:#e97e89}.release-selection-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:18px 0}.release-selection-metrics>div{display:flex;flex-direction:column;gap:7px;padding:12px;border:1px solid #242a30;border-radius:7px;background:#0c1014}.release-selection-metrics span{font-size:9px;color:#69727b}.release-selection-metrics strong{font-size:13px;color:#d6dade}.release-selection-metrics strong.ok{color:#64cfa9}.release-form{display:grid;grid-template-columns:minmax(220px,.7fr) minmax(320px,1.3fr);gap:10px}.release-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}.release-create>div:first-child{display:flex;align-items:flex-start;gap:14px;margin-bottom:15px}.release-create>div:first-child svg{color:#d89b48}.release-create>div:first-child span{display:flex;flex-direction:column;gap:4px}.release-create>button{margin-top:13px}.release-history{display:flex;flex-direction:column;gap:10px}.release-history>header{display:flex;justify-content:space-between;align-items:end}.release-history>header h4{margin:4px 0 0}.release-history>header small{color:#68717a;font-size:9px}.release-list{display:flex;flex-direction:column;gap:8px}.release-row{display:grid;grid-template-columns:52px minmax(0,1fr) auto;gap:14px;align-items:center;padding:15px}.release-version{display:flex;flex-direction:column;align-items:center;justify-content:center;height:52px;border:1px solid #2a3036;border-radius:7px;background:#0b0f13}.release-version span{font-size:8px;letter-spacing:.18em;color:#6d747b}.release-version strong{font-size:20px;color:#d89b48}.release-copy>div{display:flex;gap:6px}.release-copy h4{margin:6px 0 3px;font-size:13px}.release-copy p{margin:0 0 7px;color:#717a82;font-size:10px}.release-copy>small{color:#626b73;font-size:9px}.release-row-actions{display:flex;gap:7px}.release-empty{display:flex;min-height:160px;align-items:center;justify-content:center;flex-direction:column;gap:7px;color:#69717a}.release-empty strong{color:#bdc3c8}.release-empty p{margin:0;font-size:10px}
@media (max-width:900px){.release-principles,.release-selection-metrics{grid-template-columns:1fr 1fr}.release-form{grid-template-columns:1fr}.release-row{grid-template-columns:44px 1fr}.release-row-actions{grid-column:2}.release-workspace{padding:20px 14px}.bible-asset-browser>header{align-items:stretch;flex-direction:column}.bible-asset-tools{align-items:stretch}.bible-asset-tools label{width:auto;flex:1}.bible-asset-catalog{grid-template-columns:repeat(3,minmax(0,1fr))}.bible-draft-assistant{grid-template-columns:1fr}.bible-draft-language{width:max-content}}
</style>
