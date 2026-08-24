<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AlertTriangle, ArrowLeft, ArrowRight, BookOpen, Box, Check, ChevronRight, Clapperboard, Clock3, Copy, Download, Eye, FileJson, Film, FolderInput, Image, ImagePlus, Languages, Layers3, Lock, MoreHorizontal, Plus, RefreshCw, Save, Search, Sparkles, Trash2, Unlock, Upload, UserRound, Workflow, X } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import ToggleSwitch from '@/components/ToggleSwitch.vue'
import AssetImage from '@/components/AssetImage.vue'
import ProjectCoverImage from '@/components/ProjectCoverImage.vue'
import PromptImpactBadge from '@/components/PromptImpactBadge.vue'
import PromptWritingGuide from '@/components/PromptWritingGuide.vue'
import TranslationReviewPanel from '@/components/TranslationReviewPanel.vue'
import { productionErrorCode, repository } from '@/services/repository'
import { PROJECT_COVER_ACCEPT, projectCoverFileError } from '@/services/projectManagement'
import { bibleImageCandidateErrorMessage } from '@/services/bibleImageCandidates'
import { bibleAssetIds } from '@/services/bibleAssets'
import { bibleCanonicalTargetCollisionMessage, bibleCanonicalTargetCollisions, formatTranslationGlossary, missingBibleCanonicalNames as findMissingBibleCanonicalNames, parseTranslationGlossary } from '@/services/translationReview'
import { useStudioStore } from '@/stores/studio'
import { platformApi } from '@/services/platformApi'
import { productionRecipeScriptMode } from '@/services/platformAdapters'
import { DEFAULT_PROJECT_H3_MODE, projectDefaultH3Mode } from '@/services/projectProductionDefaults'
import { episodeNarrativeDurationSeconds, formatNarrativeDuration, seriesNarrativeDurationSeconds } from '@/services/shotDuration'
import type { ProductionRecipe, ProductionRecipeMediaRouteOption, ProductionRun, ScriptRevision } from '@/services/platformTypes'
import type { AssemblyResult, Asset, BibleImageCandidateProjection, BibleImageCandidatesResult, BibleImageEntityType, BibleImageLatestCandidate, BibleItem, Episode, H3Mode, PromptTranslationField, ReleaseBundle, SeriesProject, TaskOutputMedia } from '@/types'

const studio = useStudioStore(); const route = useRoute(); const router = useRouter()
const project = ref<SeriesProject | null>(null); const loading = ref(true); const projectLoadError = ref(''); const saving = ref(false); const tab = ref<'overview'|'bible'|'episodes'|'release'>('overview'); const bibleSection = ref<'characters'|'scenes'|'props'>('characters')
const addBibleOpen = ref(false); const addEpisodeOpen = ref(false); const createLoading = ref(false)
const bibleAssetPickerId = ref('')
const bibleImageCandidates = ref<BibleImageCandidatesResult | null>(null)
const bibleImageLoading = ref(false)
const bibleImageLoadError = ref('')
const bibleImagePreview = ref<{ src: string; title: string; filename: string; downloadUrl: string } | null>(null)
const episodeAssemblies = ref<Record<string, AssemblyResult>>({}); const completedEpisodeAssemblies = ref<Record<string, AssemblyResult>>({}); const selectedEpisodeId = ref(''); const assemblyError = ref(''); const assemblyPreviewRefreshingId = ref(''); const assemblyPreviewErrors = ref<Record<string,string>>({})
const releaseBundles = ref<ReleaseBundle[]>([]); const productionRuns = ref<ProductionRun[]>([]); const releaseLoading = ref(false); const releaseError = ref('')
const newProject = ref({ title: '', logline: '', masterScript: '', styleId: 'wuxia', aspectRatio: '9:16' as SeriesProject['aspectRatio'], resolution: '720p' as SeriesProject['resolution'], defaultMode: DEFAULT_PROJECT_H3_MODE })
const creationMode = ref<'manual'|'import'|'ai'>('manual')
const creationAction = ref<'project_only'|'start_production'>('start_production')
const creationRecipes = ref<ProductionRecipe[]>([])
const creationRecipeId = ref('')
const creationRecipesLoading = ref(false)
const creationMediaRoutes = ref<ProductionRecipeMediaRouteOption[]>([])
const creationMediaRoutesLoading = ref(false)
const creationMediaRoutesError = ref('')
const creationMediaRouteIndex = ref<number | null>(null)
let creationMediaRouteLoadSequence = 0
const creationError = ref('')
const importMode = ref<'paste'|'file'>('paste')
const importText = ref('')
const importFormat = ref('plain')
const importFile = ref<File | null>(null)
const importPreview = ref<ScriptRevision | null>(null)
const createdProjectForOnboarding = ref<SeriesProject | null>(null)
const importReviewOpen = ref(false)
const aiIdea = ref('')
const aiEpisodeCount = ref(1)
const aiShotsPerEpisode = ref(2)
const aiEpisodeDurationSeconds = ref(10)
const aiAllowExternalCalls = ref(true)
const aiBudgetLimit = ref('1.00')
const aiMaxGenerationTasks = ref(20)
const coverInput = ref<HTMLInputElement | null>(null)
const pendingCoverFile = ref<File | null>(null)
const pendingCoverPreview = ref('')
const coverSaving = ref(false)
const coverError = ref('')
const translationGlossaryText = ref('')
const translationGlossaryValidation = computed(() => parseTranslationGlossary(translationGlossaryText.value))
const canonicalBackfillLoading = ref(false)
const bibleCreatePhase = ref<'idle'|'saving'|'canonicalizing'>('idle')
const canonicalGlossaryNotice = ref<{ kind: 'success'|'warning'; message: string } | null>(null)
const bibleVisualTranslationNotice = ref<{ kind: 'success'|'warning'; message: string } | null>(null)
const translationReviewRefreshKey = ref(0)
const bibleDraft = ref({ name: '', description: '', visualPrompt: '', referenceAssetIds: [] as string[] })
const bibleCreateError = ref('')
const bibleAssetCatalog = ref<Asset[]>([]); const bibleAssetCatalogLoading = ref(false); const bibleAssetCatalogError = ref(''); const bibleAssetSearch = ref(''); const bibleAssetCategory = ref<'compatible'|'all'|'character'|'scene'|'prop'|'image'>('compatible'); const bibleAssetPreview = ref<Asset|null>(null)
const episodeDraft = ref({ title: '', synopsis: '', script: '' })
const isNew = computed(() => route.name === 'project-new')
const currentStyle = computed(() => studio.styles.find((item) => item.id === (project.value?.styleId || newProject.value.styleId)))
const newProjectStyleHint = computed(() => currentStyle.value
  ? [currentStyle.value.name, currentStyle.value.description, currentStyle.value.tags.join('、')].filter(Boolean).join('；')
  : '')
const compatibleCreationRecipes = computed(() => creationRecipes.value.filter((recipe) => {
  const mode = productionRecipeScriptMode(recipe.definition)
  return creationMode.value === 'ai' ? mode === 'ai' : creationMode.value === 'import' ? mode === 'confirmed_revision' : false
}))
const selectedCreationRecipe = computed(() => compatibleCreationRecipes.value.find((recipe) => recipe.id === creationRecipeId.value))
const selectedCreationRecipeSummary = computed(() => {
  const definition = selectedCreationRecipe.value?.definition
  const stages = definition && Array.isArray(definition.stages) ? definition.stages : []
  const reviewStages = stages.filter((value) => value && typeof value === 'object' && (value as Record<string, unknown>).approval === 'required')
  const hasStructure = stages.some((value) => value && typeof value === 'object' && (value as Record<string, unknown>).key === 'script_structure')
  const hasMediaGeneration = stages.some((value) => value && typeof value === 'object' && (value as Record<string, unknown>).key === 'media_generation')
  return {
    stageCount: stages.length,
    reviewCount: reviewStages.length,
    hasStructure,
    hasMediaGeneration,
    label: stages.length
      ? `${stages.length} 个阶段 · ${reviewStages.length} 个人工门禁 · 其余阶段自动推进`
      : '配方未返回可识别的阶段定义',
  }
})
const selectedCreationMediaRoute = computed(() => creationMediaRoutes.value.find((routeOption) => (
  routeOption.routeIndex === creationMediaRouteIndex.value
)))
const creationMediaRouteLabel = (routeOption: ProductionRecipeMediaRouteOption) => (
  `route ${routeOption.routeIndex} · ${routeOption.workflowLabel || '受限 REST 媒体执行'} · ${routeOption.deploymentLabel || routeOption.modelIdentifier}`
  + ` · 输出 ${routeOption.outputKinds.join('/') || '未声明'}`
  + (routeOption.outputKinds.includes('video') ? ` · 模式 ${routeOption.h3Modes.join('/') || '通用'}` : '')
)
const creationMediaRouteSupportsInitialShots = (routeOption: ProductionRecipeMediaRouteOption) => (
  routeOption.outputKinds.includes('video')
  && (!routeOption.h3Modes.length || routeOption.h3Modes.includes(newProject.value.defaultMode))
)
const creationWorkflowSelectionError = computed(() => {
  if (creationAction.value !== 'start_production' || !selectedCreationRecipe.value?.id || !selectedCreationRecipeSummary.value.hasMediaGeneration) return ''
  if (creationMediaRoutesLoading.value) return '正在核验媒体工作流依赖，请稍候'
  if (creationMediaRoutesError.value) return creationMediaRoutesError.value
  if (!creationMediaRoutes.value.length) return '所选配方没有可选择的媒体 route，请先在系统设置补齐并发布配方路由'
  if (creationMediaRouteIndex.value === null || !selectedCreationMediaRoute.value) return '请选择本次创建使用的整集统一工作流'
  if (!selectedCreationMediaRoute.value.isSelectable) return '所选媒体 route 的发布、smoke 或部署依赖尚未满足'
  if (!creationMediaRouteSupportsInitialShots(selectedCreationMediaRoute.value)) {
    return `所选 route 不支持新分镜默认的视频输出 / ${newProject.value.defaultMode} 模式，请更换工作流或项目默认模式`
  }
  return ''
})
const creationNeedsTextModel = computed(() => (
  creationMode.value === 'ai'
  || (creationMode.value === 'import' && selectedCreationRecipeSummary.value.hasStructure)
))
const bibleItems = computed(() => project.value?.bible[bibleSection.value] || [])
const bibleScopeBySection = { characters: 'character', scenes: 'scene', props: 'prop' } as const
const bibleImageCandidateFor = (item: BibleItem): BibleImageCandidateProjection | undefined => {
  const entityType = bibleScopeBySection[bibleSection.value]
  return bibleImageCandidates.value?.candidates.find((candidate) => (
    candidate.entityType === entityType && candidate.entityId === item.id
  ))
}
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
  if (currentStyle.value?.negativePrompt) add({ fieldKey:'style.negative_prompt', label:'风格负面约束', sourceText:currentStyle.value.negativePrompt, scopeType:'project', scopeId:project.value.id, impact:'prompt', help:'英文排除短语；中文会阻断媒体任务' })
  add({ fieldKey:'project.production_settings.color_script', label:'全剧色彩脚本', sourceText:project.value.bible.colorScript, scopeType:'project', scopeId:project.value.id, impact:'continuity' })
  add({ fieldKey:'project.production_settings.negative_prompt', label:'全剧负面约束', sourceText:project.value.bible.negativePrompt, scopeType:'project', scopeId:project.value.id, impact:'prompt', help:'英文排除短语；中文会阻断媒体任务' })
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
    if(bibleSection.value==='characters'){add({...base,fieldKey:'character.age_appearance',label:`人物 · ${item.name} · 年龄外观`,sourceText:item.ageAppearance||''});add({...base,fieldKey:'character.face_identity',label:`人物 · ${item.name} · 面部身份`,sourceText:item.faceIdentity||''});add({...base,fieldKey:'character.wardrobe',label:`人物 · ${item.name} · 固定服装`,sourceText:item.wardrobe||''});add({...base,fieldKey:'character.voice_profile',label:`人物 · ${item.name} · 声音特征`,sourceText:item.voiceProfile||'',impact:'prompt'});if(!(item.voiceProfile||'').trim())add({...base,fieldKey:'character.personality',label:`人物 · ${item.name} · 说话者人物描述`,sourceText:item.personality||'',impact:'prompt',help:'声音特征为空，当前会作为 H3 对白说话者描述后备；不是管理用基础描述'});add({...base,fieldKey:'character.negative_constraints',label:`人物 · ${item.name} · 负面约束`,sourceText:item.negativeConstraints||'',impact:'prompt'})}
    else if(bibleSection.value==='scenes'){add({...base,fieldKey:'scene.location',label:`场景 · ${item.name} · 地点结构`,sourceText:item.location||''});add({...base,fieldKey:'scene.time_of_day',label:`场景 · ${item.name} · 时间`,sourceText:item.timeOfDay||''});add({...base,fieldKey:'scene.lighting',label:`场景 · ${item.name} · 光线`,sourceText:item.lighting||''});add({...base,fieldKey:'scene.color_palette',label:`场景 · ${item.name} · 色板`,sourceText:item.colorPalette||''});add({...base,fieldKey:'scene.ambient_sound',label:`场景 · ${item.name} · 环境声`,sourceText:item.ambientSound||'',impact:'prompt'});add({...base,fieldKey:'scene.negative_constraints',label:`场景 · ${item.name} · 负面约束`,sourceText:item.negativeConstraints||'',impact:'prompt'})}
    else {add({...base,fieldKey:'prop.material',label:`道具 · ${item.name} · 材质`,sourceText:item.material||''});add({...base,fieldKey:'prop.distinctive_marks',label:`道具 · ${item.name} · 固定标记`,sourceText:item.distinctiveMarks||''});add({...base,fieldKey:'prop.continuity_state',label:`道具 · ${item.name} · 连续性状态`,sourceText:item.continuityState||''});add({...base,fieldKey:'prop.negative_constraints',label:`道具 · ${item.name} · 负面约束`,sourceText:item.negativeConstraints||'',impact:'prompt'})}
  }
  return fields
})
const totalShots = computed(() => project.value?.episodes.reduce((sum, episode) => sum + episode.shots.length, 0) || 0)
const totalSeconds = computed(() => project.value ? seriesNarrativeDurationSeconds(project.value.episodes) : 0)
const productionRunForRelease = (bundleId: string) => productionRuns.value.find((run) => run.releaseBundleId === bundleId)
const productionRunForAssembly = (assemblyId: string) => {
  const bundle = releaseBundles.value.find((item) => item.selection.episodes.some((entry) => entry.assemblyId === assemblyId) || item.selection.seriesAssemblyId === assemblyId)
  return bundle ? productionRunForRelease(bundle.id) : undefined
}
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
const loadReleaseBundles = async (projectId: string) => {
  try {
    const [bundles, runs] = await Promise.all([
      studio.getReleaseBundles(projectId),
      platformApi.listProductionRuns(projectId),
    ])
    releaseBundles.value = bundles
    productionRuns.value = runs
    releaseError.value = ''
  } catch (error) {
    releaseError.value = error instanceof Error ? error.message : '发行候选加载失败'
  }
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
  try {
    await studio.initialize()
    if (isNew.value) {
      creationRecipesLoading.value = true
      try { creationRecipes.value = await platformApi.listRecipes('published') }
      catch (error) { creationError.value = error instanceof Error ? error.message : '生产配方加载失败' }
      finally { creationRecipesLoading.value = false }
    } else {
      const found = await studio.loadProject(String(route.params.projectId)); if (found) {
        project.value = clone(found)
        translationGlossaryText.value = formatTranslationGlossary(project.value.translationGlossary)
        const normalizedDefaultMode=projectDefaultH3Mode(project.value.defaultMode)
        if(normalizedDefaultMode!==project.value.defaultMode){project.value.defaultMode=normalizedDefaultMode;studio.notify('检测到不受支持的默认模式，已使用当前可生产的“文生视频”模式','info')}
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
watch([creationMode, compatibleCreationRecipes], () => {
  if (creationMode.value === 'ai') creationAction.value = 'start_production'
  if (!compatibleCreationRecipes.value.some((recipe) => recipe.id === creationRecipeId.value)) {
    creationRecipeId.value = compatibleCreationRecipes.value[0]?.id || ''
  }
}, { immediate: true })
watch([selectedCreationRecipe, creationAction, () => newProject.value.defaultMode], async ([recipe, action]) => {
  const sequence = ++creationMediaRouteLoadSequence
  creationMediaRoutes.value = []
  creationMediaRoutesError.value = ''
  creationMediaRouteIndex.value = null
  if (!recipe || action !== 'start_production' || !selectedCreationRecipeSummary.value.hasMediaGeneration) {
    creationMediaRoutesLoading.value = false
    return
  }
  creationMediaRoutesLoading.value = true
  try {
    const catalog = await platformApi.getRecipeMediaRouteOptions(recipe.id)
    if (sequence !== creationMediaRouteLoadSequence) return
    if (catalog.recipeVersionId !== recipe.id || catalog.recipeDefinitionSha256 !== recipe.definitionSha256) {
      throw new Error('媒体 route 清单与所选配方版本不一致，请重新选择配方')
    }
    creationMediaRoutes.value = catalog.routes
    creationMediaRouteIndex.value = catalog.routes.find((routeOption) => (
      routeOption.isSelectable && creationMediaRouteSupportsInitialShots(routeOption)
    ))?.routeIndex ?? null
  } catch (error) {
    if (sequence === creationMediaRouteLoadSequence) {
      creationMediaRoutesError.value = error instanceof Error ? error.message : '媒体工作流依赖核验失败'
    }
  } finally {
    if (sequence === creationMediaRouteLoadSequence) creationMediaRoutesLoading.value = false
  }
}, { immediate: true })
onMounted(async () => {
  await loadProject()
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

const chooseImportFile = (event: Event) => {
  importFile.value = (event.target as HTMLInputElement).files?.[0] || null
}
const onboardingProjectTitle = (): string => {
  const entered = newProject.value.title.trim()
  if (entered) return entered
  if (creationMode.value === 'import') {
    const fileStem = importFile.value?.name.replace(/\.[^.]+$/, '').trim() || ''
    const firstLine = importText.value.split(/\r?\n/).map((line) => line.trim()).find(Boolean) || ''
    const candidate = (fileStem || firstLine).replace(/^#{1,6}\s*/, '').slice(0, 60).trim()
    if (candidate) return candidate
    return '待审核导入剧本'
  }
  if (creationMode.value === 'ai') return '待审核 AI 连续剧'
  return ''
}
const createBaseProject = async (): Promise<SeriesProject> => {
  const payload = {
    ...newProject.value,
    title: onboardingProjectTitle(),
    // Script text must enter through ScriptSource -> immutable preview ->
    // confirmed ScriptRevision.  Manual mode intentionally creates an empty
    // project instead of preserving a second, non-runnable master-script path.
    masterScript: '',
  }
  let created = await studio.createProject(payload)
  createdProjectForOnboarding.value = created
  if (pendingCoverFile.value) {
    try { created = await studio.setProjectCover(created, pendingCoverFile.value) }
    catch (error) {
      studio.notify(`项目已创建，但封面上传失败，可稍后重试：${error instanceof Error ? error.message : '未知错误'}`, 'error')
    }
  }
  pendingCoverFile.value = null
  revokePendingCoverPreview()
  createdProjectForOnboarding.value = created
  return created
}
const createRunFromOnboarding = async (
  created: SeriesProject,
  scriptMode: 'ai'|'confirmed_revision',
  scriptRevisionId?: string,
): Promise<ProductionRun> => {
  if (!selectedCreationRecipe.value) throw new Error(`当前没有可用于${scriptMode === 'ai' ? 'AI 创建' : '导入剧本'}的已发布配方，请先到系统设置完成配方审核发布`)
  if (creationWorkflowSelectionError.value) throw new Error(creationWorkflowSelectionError.value)
  if (creationNeedsTextModel.value && aiAllowExternalCalls.value && !aiBudgetLimit.value.trim()) throw new Error('允许云端文本 API 时必须填写本次预算上限')
  return platformApi.createProductionRun({
    projectId: created.id,
    scriptMode,
    scriptRevisionId: scriptRevisionId || null,
    recipeVersionId: selectedCreationRecipe.value.id,
    idempotencyKey: crypto.randomUUID(),
    allowExternalCalls: creationNeedsTextModel.value ? aiAllowExternalCalls.value : false,
    budgetLimit: creationNeedsTextModel.value ? aiBudgetLimit.value.trim() || null : null,
    maxGenerationTasks: aiMaxGenerationTasks.value,
    configOverrides: {},
    idea: aiIdea.value.trim(),
    styleHint: newProjectStyleHint.value,
    episodeCount: aiEpisodeCount.value,
    shotsPerEpisode: aiShotsPerEpisode.value,
    episodeDurationSeconds: aiEpisodeDurationSeconds.value,
    workflowSelection: selectedCreationRecipeSummary.value.hasMediaGeneration
      ? { schemaVersion: 1, mode: 'uniform', routeIndex: Number(creationMediaRouteIndex.value) }
      : null,
  })
}
const createProject = async () => {
  if (createLoading.value) return
  creationError.value = ''
  if (creationMode.value === 'manual' && !newProject.value.title.trim()) { creationError.value = '手工创建项目时请先填写项目名称'; return }
  if (creationMode.value === 'import' && importMode.value === 'paste' && !importText.value.trim()) { creationError.value = '请粘贴要导入的完整剧本'; return }
  if (creationMode.value === 'import' && importMode.value === 'file' && !importFile.value) { creationError.value = '请选择要导入的剧本文件'; return }
  if (creationMode.value === 'ai' && creationAction.value === 'start_production' && !aiIdea.value.trim()) { creationError.value = '请填写要由 AI 创作的故事需求'; return }
  if (creationMode.value !== 'manual' && creationAction.value === 'start_production' && !selectedCreationRecipe.value) { creationError.value = '没有兼容的已发布配方，请先到系统设置完成配方配置与发布'; return }
  if (creationWorkflowSelectionError.value) { creationError.value = creationWorkflowSelectionError.value; return }
  if (creationAction.value === 'start_production' && creationNeedsTextModel.value && aiAllowExternalCalls.value && !aiBudgetLimit.value.trim()) { creationError.value = '允许云端文本 API 时必须填写本次预算上限'; return }
  createLoading.value = true
  try {
    const created = await createBaseProject()
    if (creationMode.value === 'manual') {
      await router.replace(`/projects/${created.id}`)
      return
    }
    if (creationMode.value === 'ai') {
      const run = await createRunFromOnboarding(created, 'ai')
      await router.replace(`/projects/${created.id}/production/${run.id}`)
      return
    }
    const result = importMode.value === 'file' && importFile.value
      ? await platformApi.uploadScript(created.id, importFile.value)
      : await platformApi.createPastedScript(created.id, importText.value, importFormat.value)
    importPreview.value = await platformApi.getScriptRevision(result.revision.id)
    importReviewOpen.value = true
  } catch (error) {
    creationError.value = error instanceof Error ? error.message : '创建流程失败'
    if (createdProjectForOnboarding.value) {
      creationError.value = `项目已创建，但后续步骤尚未完成：${creationError.value}`
    }
  } finally { createLoading.value = false }
}
const confirmImportedScriptAndContinue = async () => {
  if (!createdProjectForOnboarding.value || !importPreview.value || createLoading.value) return
  createLoading.value = true
  creationError.value = ''
  try {
    const confirmed = await platformApi.confirmScriptRevision(importPreview.value.id)
    importPreview.value = confirmed
    if (creationAction.value === 'project_only') {
      importReviewOpen.value = false
      await router.replace(`/projects/${createdProjectForOnboarding.value.id}`)
      return
    }
    const run = await createRunFromOnboarding(createdProjectForOnboarding.value, 'confirmed_revision', confirmed.id)
    importReviewOpen.value = false
    await router.replace(`/projects/${createdProjectForOnboarding.value.id}/production/${run.id}`)
  } catch (error) {
    creationError.value = `项目和剧本预览已保留，但确认或启动生产失败：${error instanceof Error ? error.message : '未知错误'}`
  } finally { createLoading.value = false }
}
const save = async (): Promise<SeriesProject | null> => {
  if (!project.value) return null
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
    translationGlossaryText.value = formatTranslationGlossary(project.value.translationGlossary)
    if (glossaryChanged) translationReviewRefreshKey.value += 1
    return project.value
  } catch (error) { studio.notify(error instanceof Error ? error.message : '保存失败', 'error'); return null }
  finally { saving.value = false }
}
const bibleItemsForType = (source: SeriesProject, entityType: BibleImageEntityType): BibleItem[] => (
  entityType === 'character' ? source.bible.characters : entityType === 'scene' ? source.bible.scenes : source.bible.props
)
const bibleImagePersistedId = (id: string) => /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(id)
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
    canonicalGlossaryNotice.value = { kind: 'warning', message: `自动补全暂未完成：${error instanceof Error ? error.message : '提示词标准化部署暂不可用'}` }
    studio.notify('术语自动补全失败，可稍后重试或人工填写。', 'info')
  } finally { canonicalBackfillLoading.value = false }
}
const addBibleItem = async () => {
  if (!project.value || !bibleDraft.value.name.trim()) { studio.notify('请填写设定名称', 'error'); return }
  if (bibleCreatePhase.value !== 'idle') return
  bibleCreateError.value=''
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
  const [anchorAssetId, ...referenceAssetIds] = bibleDraft.value.referenceAssetIds
  const item: BibleItem = { id: temporaryId, name, description: bibleDraft.value.description, visualPrompt: bibleDraft.value.visualPrompt.trim(), anchorAssetId: anchorAssetId || null, referenceAssetIds, locked: true }
  project.value.bible[section].push(item)
  bibleCreatePhase.value = 'saving'
  canonicalGlossaryNotice.value = null
  bibleVisualTranslationNotice.value = null
  const saved = await save()
  if (!saved) {
    const unsavedIndex = project.value?.bible[section].findIndex((entry) => entry.id === temporaryId) ?? -1
    if (unsavedIndex >= 0) project.value?.bible[section].splice(unsavedIndex, 1)
    bibleCreateError.value='设定未创建。保存服务暂不可用或项目事实已变化；手工字段仍保留，请刷新项目后重试。'
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
    canonicalGlossaryNotice.value = { kind: 'warning', message: `“${name}”已保存；自动术语生成暂未完成：${error instanceof Error ? error.message : '提示词标准化部署暂不可用'}` }
    bibleVisualTranslationNotice.value = containsCjk(item.visualPrompt)
      ? { kind: 'warning', message: `“${name}”已保存；视觉锚点仍保留中文原文，英文候选暂未生成。请在下方审核面板点击“翻译此字段”。` }
      : { kind: 'success', message: `“${name}”的视觉锚点已是英文，无需翻译。` }
    translationReviewRefreshKey.value += 1
    studio.notify('设定已保存；自动英文处理未全部完成，可稍后在对应面板重试。', 'info')
  } finally {
    bibleDraft.value = { name: '', description: '', visualPrompt: '', referenceAssetIds: [] }
    resetBibleDraftForm()
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
    await save()
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
const episodeProductionLabel = (episode: Episode) => episodeVideoCount(episode) && episodeImageCount(episode) ? '视频 + 图片' : episodeImageCount(episode) ? '图片' : '视频'
const assetTypeForBible = () => ({ characters: 'character', scenes: 'scene', props: 'prop' } as const)[bibleSection.value]
const bibleAssetTypeLabel = (asset:Asset) => ({character:'人物',scene:'场景',prop:'道具',image:'未分类图片'} as Record<string,string>)[asset.type] || '其他'
const bibleDraftAssetUnavailableReason=(asset:Asset)=>asset.isActive===false?'素材已停用，不能用于新草稿':asset.type!==assetTypeForBible()?`${bibleAssetTypeLabel(asset)}素材不能用于当前设定`:''
const isBibleDraftAssetCompatible = (asset:Asset) => !bibleDraftAssetUnavailableReason(asset)
const filteredBibleAssetCatalog = computed(() => {
  const query=bibleAssetSearch.value.trim().toLocaleLowerCase('zh-CN')
  return bibleAssetCatalog.value.filter((asset)=>{
    if(!['character','scene','prop','image'].includes(asset.type))return false
    if(bibleAssetCategory.value==='compatible'&&!isBibleDraftAssetCompatible(asset))return false
    if(bibleAssetCategory.value!=='compatible'&&bibleAssetCategory.value!=='all'&&asset.type!==bibleAssetCategory.value)return false
    return !query||`${asset.name} ${asset.description} ${asset.promptHint||''} ${asset.tags.join(' ')}`.toLocaleLowerCase('zh-CN').includes(query)
  })
})
const resetBibleDraftForm = () => {
  bibleCreateError.value='';bibleAssetSearch.value='';bibleAssetCategory.value='compatible';bibleAssetPreview.value=null
}
const openAddBibleModal = async () => {
  if(bibleCreatePhase.value!=='idle')return
  bibleDraft.value={name:'',description:'',visualPrompt:'',referenceAssetIds:[]};resetBibleDraftForm();addBibleOpen.value=true
  bibleAssetCatalogLoading.value=true;bibleAssetCatalogError.value=''
  try{bibleAssetCatalog.value=(await repository.getAssets({})).filter((asset)=>['character','scene','prop','image'].includes(asset.type))}
  catch(error){bibleAssetCatalogError.value=error instanceof Error?error.message:'素材库加载失败'}finally{bibleAssetCatalogLoading.value=false}
}
const closeAddBibleModal=()=>{if(bibleCreatePhase.value!=='idle')return;addBibleOpen.value=false;bibleAssetPreview.value=null}
const selectBibleDraftAsset=(asset:Asset)=>{
  if(!isBibleDraftAssetCompatible(asset)){studio.notify(`“${asset.name}”不可选择：${bibleDraftAssetUnavailableReason(asset)}`, 'info');return}
  if(bibleDraft.value.referenceAssetIds[0]===asset.id)return
  const hasManualFields=[bibleDraft.value.name,bibleDraft.value.description,bibleDraft.value.visualPrompt].some((value)=>Boolean(value.trim()))
  if(hasManualFields&&!window.confirm(`切换到“${asset.name}”会用该素材已保存的名称、基础描述和视觉锚点替换当前手工字段。是否继续？`))return
  bibleDraft.value={name:asset.name,description:asset.description,visualPrompt:asset.promptHint||'',referenceAssetIds:[asset.id]}
  if(!asset.promptHint)studio.notify('已带入素材名称和基础描述；该素材缺少视觉锚点，请手工补充或先到素材库完善说明。','info')
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
        <div class="form-section"><div class="section-number">01</div><div class="section-content"><h3>选择创建方式</h3><p>导入和 AI 创建进入同一条 ProductionRun 审计链；手工模式只建立空项目，不保存无法拆解的旁路母稿。</p><div class="creation-mode-tabs"><button type="button" :class="{active:creationMode==='manual'}" @click="creationMode='manual'"><BookOpen :size="15"/><strong>空项目 / 手工设定</strong><small>只建立项目；有剧本请使用“导入剧本”</small></button><button type="button" :class="{active:creationMode==='import'}" @click="creationMode='import'"><FolderInput :size="15"/><strong>导入剧本</strong><small>服务端提取、预览并确认后拆解</small></button><button type="button" :class="{active:creationMode==='ai'}" @click="creationMode='ai'"><Sparkles :size="15"/><strong>AI 描述创建</strong><small>按需求生成剧本并进入人工审核</small></button></div><label class="field"><span class="impact-label-row">项目名称 {{ creationMode==='manual'?'*':'（可稍后采用模型提议）' }} <PromptImpactBadge kind="label" title="只用于项目、任务和页面识别；导入或 AI 模式留空时先使用可审计的临时名称，审核时再决定是否采用模型提议。"/></span><input v-model="newProject.title" :placeholder="creationMode==='manual'?'例如：听雨不归人':'可留空；导入时优先使用文件名，AI 模式使用待审核名称'" maxlength="60"><small>{{ newProject.title.length }}/60</small></label><label class="field"><span class="impact-label-row">一句话梗概 <PromptImpactBadge kind="indirect" title="用于项目说明；不会替代剧本或模型生成结果。"/></span><textarea v-model="newProject.logline" rows="3" placeholder="主角是谁、想要什么、面临怎样的阻力？"></textarea></label><template v-if="creationMode==='manual'"><div class="import-review-evidence"><BookOpen :size="15"/><span><strong>手工模式创建空项目</strong><small>若已有完整剧本，请切换“导入剧本”；正文会经过服务端提取、不可变预览与确认后才能进入正式拆解。</small></span></div></template><template v-else-if="creationMode==='import'"><div class="creation-subtabs"><button type="button" :class="{active:importMode==='paste'}" @click="importMode='paste'">粘贴正文</button><button type="button" :class="{active:importMode==='file'}" @click="importMode='file'">上传文件</button></div><label v-if="importMode==='paste'" class="field"><span>待导入剧本 *</span><textarea v-model="importText" rows="11" placeholder="粘贴完整剧本；将由服务端生成不可变预览 revision，确认前不会拆解。"></textarea><select v-model="importFormat"><option value="plain">纯文本</option><option value="markdown">Markdown</option><option value="fountain">Fountain</option></select></label><label v-else class="field"><span>剧本文件 *</span><input type="file" accept=".txt,.md,.markdown,.fountain,.docx,text/plain,text/markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document" @change="chooseImportFile"><small>{{ importFile?.name || '支持 TXT、Markdown、Fountain、DOCX；PDF 会被明确拒绝。' }}</small></label></template><template v-else><label class="field"><span class="impact-label-row">AI 创作需求 * <PromptImpactBadge kind="production" title="会通过已发布配方调用文本模型；响应先进入剧本人工审核。"/></span><textarea v-model="aiIdea" rows="8" placeholder="描述世界观、人物、冲突、叙事节奏、结局和禁忌。"></textarea></label><div class="form-grid three"><label class="field"><span>集数</span><input v-model.number="aiEpisodeCount" type="number" min="1"></label><label class="field"><span>每集镜数</span><input v-model.number="aiShotsPerEpisode" type="number" min="1"></label><label class="field"><span>单集时长（秒）</span><input v-model.number="aiEpisodeDurationSeconds" type="number" min="1"></label></div><div class="form-grid three"><label class="field"><span>允许云端文本 API</span><select v-model="aiAllowExternalCalls"><option :value="true">允许（记录费用与审计）</option><option :value="false">禁止</option></select></label><label class="field"><span>预算上限</span><input v-model="aiBudgetLimit" inputmode="decimal" placeholder="1.00"></label><label class="field"><span>生成任务上限</span><input v-model.number="aiMaxGenerationTasks" type="number" min="1"></label></div></template><template v-if="creationMode!=='manual'"><div class="form-grid two onboarding-run-options"><label v-if="creationMode==='import'" class="field"><span>创建后动作</span><select v-model="creationAction"><option value="start_production">确认剧本并启动正式 ProductionRun</option><option value="project_only">仅保存项目与确认剧本</option></select><small>选择配方后会按其阶段自动推进；遇到配方声明的人工门禁时明确暂停。</small></label><div v-else class="field"><span>创建后动作</span><strong>创建并启动正式 AI ProductionRun</strong><small>AI 创建必须通过持久 ProductionRun 调用模型，不能以“仅保存项目”伪装成已生成。</small></div><label v-if="creationAction==='start_production'" class="field"><span>兼容的已发布配方 *</span><select v-model="creationRecipeId" :disabled="creationRecipesLoading||!compatibleCreationRecipes.length"><option v-for="recipe in compatibleCreationRecipes" :key="recipe.id" :value="recipe.id">{{ recipe.name }} · v{{ recipe.version }}</option></select><small v-if="selectedCreationRecipe">{{ selectedCreationRecipeSummary.label }}<template v-if="creationMode==='import'&&!selectedCreationRecipeSummary.hasStructure"> · 该配方直接使用已确认格式，未包含 AI 结构化阶段</template></small><small v-if="!creationRecipesLoading&&!compatibleCreationRecipes.length" class="creation-error">尚无兼容配方，请先到系统设置完成模型、工作流与配方审核发布。</small></label></div><div v-if="creationMode==='import'&&creationAction==='start_production'&&creationNeedsTextModel" class="form-grid three onboarding-run-options"><label class="field"><span>允许云端文本 API</span><select v-model="aiAllowExternalCalls"><option :value="true">允许（记录费用与审计）</option><option :value="false">禁止</option></select></label><label class="field"><span>结构化预算上限</span><input v-model="aiBudgetLimit" inputmode="decimal" placeholder="1.00"><small>只有配方包含 script_structure 且实际路由为外部模型时才会产生费用。</small></label><label class="field"><span>生成任务上限</span><input v-model.number="aiMaxGenerationTasks" type="number" min="1"></label></div></template><p v-if="creationError" class="creation-error" role="alert"><AlertTriangle :size="13"/>{{ creationError }}<RouterLink v-if="createdProjectForOnboarding" :to="`/projects/${createdProjectForOnboarding.id}`">进入已创建项目</RouterLink></p><PromptWritingGuide scope="project"/></div></div>
        <div v-if="creationAction==='start_production'&&selectedCreationRecipeSummary.hasMediaGeneration" class="form-section onboarding-workflow-section"><div class="section-number">WF</div><div class="section-content"><h3>冻结生产工作流</h3><p>创建前尚未物化正式 Shot，因此先按“视频输出 + 项目默认模式”核验整集统一 route；已有 Shot 的运行页仍可逐镜头选择。</p><div class="workflow-selection-panel"><label class="field"><span>整集统一工作流 *</span><select v-model.number="creationMediaRouteIndex" :disabled="creationMediaRoutesLoading||!creationMediaRoutes.some((routeOption)=>routeOption.isSelectable&&creationMediaRouteSupportsInitialShots(routeOption))"><option :value="null" disabled>{{ creationMediaRoutesLoading ? '正在核验工作流依赖…' : '请选择支持项目默认视频模式的媒体 route' }}</option><option v-for="routeOption in creationMediaRoutes" :key="routeOption.routeIndex" :value="routeOption.routeIndex" :disabled="!routeOption.isSelectable||!creationMediaRouteSupportsInitialShots(routeOption)">{{ creationMediaRouteLabel(routeOption) }}{{ routeOption.isSelectable&&creationMediaRouteSupportsInitialShots(routeOption) ? '' : '（依赖或默认模式不兼容）' }}</option></select><small v-if="creationWorkflowSelectionError" class="creation-error">{{ creationWorkflowSelectionError }}</small></label><details v-if="creationMediaRoutes.some((routeOption)=>!routeOption.isSelectable||!creationMediaRouteSupportsInitialShots(routeOption))" class="workflow-route-gaps"><summary>查看 route 依赖或模式缺口</summary><ul><li v-for="routeOption in creationMediaRoutes.filter((item)=>!item.isSelectable||!creationMediaRouteSupportsInitialShots(item))" :key="`creation-route-gap-${routeOption.routeIndex}`"><strong>{{ creationMediaRouteLabel(routeOption) }}</strong>：{{ !creationMediaRouteSupportsInitialShots(routeOption)?`不支持 video / ${newProject.defaultMode}`:(routeOption.reasons.join('；') || routeOption.reasonCodes.join('、')) }}</li></ul></details></div></div></div>
        <div class="form-section"><div class="section-number">02</div><div class="section-content"><div class="impact-heading"><h3>选择视觉风格</h3><PromptImpactBadge kind="prompt" title="风格的 visual prompt 会进入每个镜头，风格负面规则也会合并到负面提示词。"/><PromptImpactBadge kind="continuity"/></div><p>风格决定默认镜头语言、色彩与提示词前缀。</p><div class="new-style-grid"><button v-for="style in studio.styles" :key="style.id" :class="{active:newProject.styleId===style.id}" :style="{background:style.gradient,'--accent':style.accent}" @click="newProject.styleId=style.id"><i class="check"><Check :size="12"/></i><span>{{ style.englishName }}</span><strong>{{ style.name }}</strong><small>{{ style.tags.join(' · ') }}</small></button></div></div></div>
        <div class="form-section"><div class="section-number">03</div><div class="section-content"><h3>生产规格</h3><p>这里只保存项目画幅、分辨率偏好和新分镜初始输入模式；实际模型、工作流、帧率与并发由 ProductionRun 的冻结路由和配置快照决定。</p><div class="form-grid three"><label class="field"><span class="impact-label-row">画幅比例 <PromptImpactBadge kind="production" title="决定分辨率的宽高方向，并记录到连续性生产格式。"/><PromptImpactBadge kind="continuity"/></span><select v-model="newProject.aspectRatio"><option value="9:16">9:16 · 竖屏短剧</option><option value="16:9">16:9 · 横屏影视</option><option value="1:1">1:1 · 方形内容</option></select></label><label class="field"><span class="impact-label-row">项目分辨率档位 <PromptImpactBadge kind="production" title="仅作为项目偏好；正式宽高由运行快照、主机建议与工作流兼容性决定。"/></span><select v-model="newProject.resolution"><option value="720p">标准档 · 由运行配置解析</option><option value="1080p" disabled>高分档 · 待兼容工作流启用</option></select></label><label class="field"><span class="impact-label-row">新分镜默认视频输入模式 <PromptImpactBadge kind="indirect" title="只初始化之后创建或拆解出的分镜，不会改写已有分镜，也不替代 ProductionRun 路由选择。"/><PromptImpactBadge kind="production"/></span><select v-model="newProject.defaultMode"><option v-for="mode in h3Modes" :key="mode.value" :value="mode.value">{{ mode.label }} · {{ mode.desc }}</option></select></label></div></div></div>
        <div class="form-section"><div class="section-number">04</div><div class="section-content"><h3>连续剧封面</h3><p>推荐 2:3 竖版图；支持 JPEG、PNG、WebP，最大 10 MB。封面只用于项目识别，不进入生成工作流。</p><div class="cover-picker"><button type="button" class="cover-picker-preview" :disabled="createLoading" :style="{background:currentStyle?.gradient}" :aria-label="pendingCoverPreview?'更换待上传的连续剧封面':'选择连续剧封面'" @click="chooseProjectCover"><img v-if="pendingCoverPreview" :src="pendingCoverPreview" alt="待上传的连续剧封面预览"><template v-else><ImagePlus :size="25"/><span>选择封面图片</span></template></button><div><strong>{{ pendingCoverFile?.name || '尚未选择封面' }}</strong><small>项目会先创建，再上传并绑定封面；封面失败不会造成重复项目。</small><div class="cover-picker-actions"><button type="button" class="button ghost small" :disabled="createLoading" @click="chooseProjectCover"><Upload :size="13"/>{{ pendingCoverPreview?'更换封面':'选择封面' }}</button><button v-if="pendingCoverPreview" type="button" class="button ghost small" :disabled="createLoading" @click="removeProjectCover"><Trash2 :size="13"/>移除</button></div></div></div><p v-if="coverError" class="cover-error" role="alert">{{ coverError }}</p></div></div>
        <div class="new-form-footer"><span><Lock :size="14"/>项目事实保存在 Django；云端调用仅在显式授权和预算内执行</span><button class="button primary" :disabled="createLoading" @click="createProject">{{ createLoading?'正在执行…':creationMode==='ai'&&creationAction==='start_production'?'创建并启动 AI 生产':creationMode==='import'?'创建并预览剧本':'创建并进入工作室' }}<ArrowRight :size="17"/></button></div>
      </div>
      <aside class="new-project-preview panel" :style="{background:currentStyle?.gradient}"><img v-if="pendingCoverPreview" class="new-cover-preview-image" :src="pendingCoverPreview" alt="待上传的连续剧封面实时预览"><template v-else><div class="preview-pattern"></div><span class="eyebrow">LIVE PROJECT PREVIEW</span><div class="preview-center"><div class="preview-clap"><i></i><Clapperboard :size="36"/></div><small>{{ currentStyle?.englishName }}</small><h3>{{ onboardingProjectTitle() || '未命名连续剧' }}</h3><p>{{ newProject.logline || '你的故事将从这里开始。' }}</p></div><div class="preview-spec"><span>{{ newProject.aspectRatio }}</span><span>{{ newProject.resolution }}</span><span>{{ workflowLabel(newProject.defaultMode) }}</span></div></template></aside>
    </section>
    <BaseModal :open="importReviewOpen" title="核对服务端提取的剧本正文" eyebrow="SCRIPT REVISION PREVIEW" width="1040px" :dismissible="!createLoading" @close="importReviewOpen=false">
      <div class="import-review"><div class="import-review-evidence"><Lock :size="15"/><span><strong>项目已创建，原始文件与正文摘要已记录</strong><small>只有确认这个不可变 preview revision 后，ProductionRun 才能拆解分集与分镜；取消不会删除项目或伪装完成。</small></span></div><label class="field"><span>服务端提取正文（只读）</span><textarea :value="importPreview?.content || ''" rows="24" readonly></textarea><small>{{ importPreview?.contentSizeBytes?.toLocaleString() || 0 }} bytes · SHA-256 {{ importPreview?.contentSha256?.slice(0,16) }}…</small></label><p v-if="creationError" class="creation-error" role="alert"><AlertTriangle :size="13"/>{{ creationError }}</p></div>
      <template #footer><button class="button ghost" :disabled="createLoading" @click="importReviewOpen=false">稍后处理</button><button class="button primary" :disabled="createLoading||!importPreview" @click="confirmImportedScriptAndContinue"><Check :size="15"/>{{ createLoading?'正在确认…':creationAction==='start_production'?'确认剧本并启动 ProductionRun':'确认剧本并进入项目' }}</button></template>
    </BaseModal>
  </div>

  <div v-else-if="project" class="series-page page-stack">
    <input ref="coverInput" type="file" :accept="PROJECT_COVER_ACCEPT" hidden @change="selectProjectCover">
    <section class="series-header" :style="{background:currentStyle?.gradient}"><ProjectCoverImage v-if="project.coverUrl" class="series-cover-background" :src="project.coverUrl" :alt="`《${project.title}》连续剧封面`" :refresh="refreshCurrentProjectCover" eager/><div class="series-pattern"></div><div class="series-header-main"><button class="back-link light" @click="router.push('/projects')"><ArrowLeft :size="14"/>全部项目</button><div class="series-title"><div class="series-poster"><ProjectCoverImage v-if="project.coverUrl" :src="project.coverUrl" alt="" :refresh="refreshCurrentProjectCover" eager/><template v-else><Clapperboard :size="30"/><span>{{ currentStyle?.name }}</span></template></div><div><div class="series-status"><StatusBadge :status="project.status"/><span>{{ currentStyle?.englishName }}</span></div><h2>{{ project.title }}</h2><p>{{ project.logline }}</p><div class="series-facts"><span>{{ project.aspectRatio }}</span><i></i><span>{{ project.resolution }}</span><i></i><span>视频 · 图片</span><i></i><span>{{ project.episodes.length }} 集</span></div></div></div></div><div class="series-header-actions"><button type="button" class="button glass" :disabled="coverSaving" @click="chooseProjectCover"><Upload :size="16"/>{{ coverSaving?'封面处理中…':project.coverAssetId?'更换封面':'上传封面' }}</button><button v-if="project.coverAssetId" type="button" class="button glass" :disabled="coverSaving" aria-label="移除连续剧封面" @click="removeProjectCover"><Trash2 :size="16"/>移除封面</button><button class="button glass" :disabled="saving||Boolean(canonicalGlossaryBlockedReason)" :title="canonicalGlossaryBlockedReason||''" @click="save()"><Save :size="16"/>{{ saving?'保存中…':'保存设定' }}</button><RouterLink :to="`/projects/${project.id}/production`" class="button primary"><Workflow :size="16"/>一键生产连续剧</RouterLink><p v-if="coverError" class="cover-error header-cover-error" role="alert">{{ coverError }}</p></div></section>
    <nav class="studio-tabs"><button :class="{active:tab==='overview'}" @click="tab='overview'"><Layers3 :size="16"/>项目总览</button><button :class="{active:tab==='bible'}" @click="tab='bible'"><BookOpen :size="16"/>一致性 Bible <span>{{ project.bible.characters.length+project.bible.scenes.length+project.bible.props.length }}</span></button><button :class="{active:tab==='episodes'}" @click="tab='episodes'"><Film :size="16"/>剧集与分镜 <span>{{ project.episodes.length }}</span></button><button :class="{active:tab==='release'}" @click="tab='release'"><Lock :size="16"/>发行候选 <span>{{ releaseBundles.length }}</span></button></nav>

    <template v-if="tab==='overview'">
      <section class="metric-grid compact"><article class="metric-card"><div class="metric-icon amber"><Film :size="19"/></div><div><span>剧集</span><strong>{{ project.episodes.length }}</strong><small>可无限新增</small></div></article><article class="metric-card"><div class="metric-icon cyan"><Clapperboard :size="19"/></div><div><span>分镜</span><strong>{{ totalShots }}</strong><small>已规划镜头</small></div></article><article class="metric-card"><div class="metric-icon violet"><Clock3 :size="19"/></div><div><span>叙事总时长</span><strong>{{ formatNarrativeDuration(totalSeconds) }}<em> 秒</em></strong><small>图片展示与视频时长合计</small></div></article><article class="metric-card"><div class="metric-icon green"><Lock :size="19"/></div><div><span>设定锚点</span><strong>{{ project.bible.characters.length+project.bible.scenes.length+project.bible.props.length }}</strong><small>人物 / 场景 / 道具</small></div></article>
      </section>
      <section class="panel script-workbench">
        <header class="panel-header"><div><span class="eyebrow">MASTER SCRIPT SOURCE</span><h3>完整剧本</h3><p>工作室只保存正式母稿；结构化拆解、Bible 对齐与生成统一由 ProductionRun 配方执行。</p></div><div class="release-heading-actions"><button class="button ghost small" :disabled="saving||Boolean(canonicalGlossaryBlockedReason)" :title="canonicalGlossaryBlockedReason||''" @click="save()"><Save :size="14"/>保存剧本</button><RouterLink class="button primary small" :to="`/projects/${project.id}/production`"><Workflow :size="14"/>进入 ProductionRun</RouterLink></div></header>
        <div class="script-editor-stack"><label class="field script-editor"><span class="impact-label-row">全剧母稿 <PromptImpactBadge kind="indirect" title="作为 ProductionRun 的权威脚本源。"/><PromptImpactBadge kind="audit" title="原文会保留，便于核对运行产物。"/></span><textarea v-model="project.masterScript" rows="14" placeholder="粘贴完整剧本；保存后进入 ProductionRun 执行正式配方。"></textarea><small>{{ project.masterScript.length.toLocaleString() }} 字符 · 当前页面不会旁路拆解或改写剧集、分镜</small></label><PromptWritingGuide scope="project"/></div>
      </section>
      <section class="panel global-rules">
        <header><div><span class="eyebrow">GLOBAL VISUAL RULES</span><h3>全剧视觉规则</h3><p>在这里直接维护正式规则；色彩脚本与连续性摘要是两个独立字段。</p></div><button class="button ghost small" :disabled="saving||Boolean(canonicalGlossaryBlockedReason)" :title="canonicalGlossaryBlockedReason||''" @click="save()"><Save :size="14"/>{{ saving?'保存中…':'保存所有更改' }}</button></header>
        <div class="form-grid two">
          <label class="field"><span class="impact-label-row">全局负面提示词 <PromptImpactBadge kind="audit" title="会被合并并保存在提示词产物中用于审计；当前本地 H3 原生节点没有独立 negative 输入，因此不会单独控制成片。"/></span><textarea v-model="project.bible.negativePrompt" rows="5" placeholder="identity drift; wardrobe color changes; duplicated people; extra limbs; text; watermark; flicker"></textarea><small>只写要排除的缺陷。AI 候选会把平台安全基线和现有英文排除项确定性合并，不让模型自由改写。</small></label>
          <label class="field"><span class="impact-label-row">色彩脚本 <PromptImpactBadge kind="prompt" title="会进入每份视频提示词第一镜的色彩与光线设计。"/><PromptImpactBadge kind="continuity"/></span><textarea v-model="project.bible.colorScript" rows="5" placeholder="EP01–03 use a cool cyan-gray base with restrained dark-red clue accents; shift to warm gold at the climax while preserving natural skin tones."></textarea><small>只写跨集色板、主辅光、环境阶段和转折；不要重复人物外观、空间规则或具体动作。</small></label>
          <label class="field"><span class="impact-label-row">连续性摘要 <PromptImpactBadge kind="prompt" title="作为图像提示词和跨集连续性的独立项目级后备字段。"/><PromptImpactBadge kind="continuity"/></span><textarea v-model="project.continuitySummary" rows="5" placeholder="Preserve stable character identity, wardrobe, architecture, prop geometry, weather logic, light direction, and spatial continuity across every episode."></textarea><small>与色彩脚本独立保存。写人物身份、固定服装、空间结构、道具形态、天气和光线方向等不可变化规则。</small></label>
          <label class="field glossary-field"><span class="impact-label-row">翻译 canonical 术语表 <PromptImpactBadge kind="audit" title="不会独立生成画面；提示词标准化模型处理高影响字段时会强制复用这里的英文专名。"/><PromptImpactBadge kind="continuity" title="统一人物、场景和道具在所有镜头中的英文称呼。"/></span><textarea v-model="translationGlossaryText" rows="5" placeholder="玄纹剑 = Xuanwen Sword&#10;听雨客栈 = Listening Rain Inn&#10;沈砚 = Shen Yan"></textarea><small>视觉规则只读取术语表作为专名约束，不会生成或覆盖术语。每个 Bible 实体必须使用唯一英文 canonical；发现碰撞时不会自动猜译名。</small><div class="glossary-status-row"><em v-if="translationGlossaryValidation.error" class="glossary-error">{{ translationGlossaryValidation.error }}</em><em v-else-if="canonicalTargetCollisionError" class="glossary-error">{{ canonicalTargetCollisionError }}</em><em v-else class="glossary-ok">{{ Object.keys(translationGlossaryValidation.value).length }} 个 canonical 术语<span v-if="missingCanonicalBibleNames.length"> · {{ missingCanonicalBibleNames.length }} 个 Bible 名称待补全</span></em><button class="button ghost small" type="button" :disabled="canonicalBackfillLoading||saving||Boolean(canonicalGlossaryBlockedReason)||!missingCanonicalBibleNames.length" :title="canonicalGlossaryBlockedReason||''" @click="backfillMissingCanonicalGlossary"><RefreshCw :size="13" :class="{spinning:canonicalBackfillLoading}"/>{{ canonicalBackfillLoading?'正在调用标准化模型…':missingCanonicalBibleNames.length?`补全 ${missingCanonicalBibleNames.length} 个缺失术语`:'术语已完整' }}</button></div><div v-if="canonicalGlossaryNotice" class="canonical-notice" :class="canonicalGlossaryNotice.kind">{{ canonicalGlossaryNotice.message }}</div></label>
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
        <TranslationReviewPanel :project-id="project.id" :fields="projectTranslationFields" :refresh-key="translationReviewRefreshKey" :blocked-reason="canonicalGlossaryBlockedReason" compact title="项目级手工中文字段 · 英文候选人工审核" @changed="invalidateProjectPromptsFromTranslation"/>
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
          <header class="section-heading"><div><span class="eyebrow">{{ bibleSections.find(s=>s.value===bibleSection)?.label.toUpperCase() }}</span><h3>{{ bibleSections.find(s=>s.value===bibleSection)?.label }}</h3><p>视觉锚点可直接写英文，也可写中文后由已配置的提示词标准化模型生成英文候选；新增时还可选择素材库参考图。</p></div><button class="button primary" :disabled="bibleCreatePhase!=='idle'" @click="openAddBibleModal"><Plus :size="16"/>新增{{ bibleSections.find(s=>s.value===bibleSection)?.singular }}</button></header>
          <div v-if="canonicalGlossaryNotice" class="canonical-notice bible-canonical-notice" :class="canonicalGlossaryNotice.kind"><strong>名称 canonical</strong><span>{{ canonicalGlossaryNotice.message }}</span></div>
          <div v-if="bibleVisualTranslationNotice" class="canonical-notice bible-canonical-notice visual-translation-notice" :class="bibleVisualTranslationNotice.kind"><strong>视觉锚点英文候选</strong><span>{{ bibleVisualTranslationNotice.message }}</span></div>
          <PromptWritingGuide scope="bible"/>
          <div v-if="bibleImageLoadError" class="bible-image-global-state error" role="alert"><AlertTriangle :size="14"/><span>{{ bibleImageLoadError }}</span><button type="button" :disabled="bibleImageLoading" @click="loadBibleImageCandidates(project.id)"><RefreshCw :size="13"/>重试</button></div>
          <div v-else class="bible-image-global-state"><ImagePlus :size="14"/><span>这里仅展示 ProductionRun 已生成的实体参考图候选历史；新生成与选择必须从对应 ProductionRun 操作。</span><button type="button" :disabled="bibleImageLoading" @click="loadBibleImageCandidates(project.id)"><RefreshCw :size="13" :class="{spinning:bibleImageLoading}"/>刷新候选状态</button></div>
          <TranslationReviewPanel :project-id="project.id" :fields="bibleTranslationFields" :refresh-key="translationReviewRefreshKey" :blocked-reason="canonicalGlossaryBlockedReason" compact :title="`${bibleSections.find(s=>s.value===bibleSection)?.label} · 英文候选人工审核`" @changed="invalidateProjectPromptsFromTranslation"/>
          <div class="bible-card-list">
            <article v-for="item in bibleItems" :key="item.id" class="panel bible-card">
              <div class="bible-ref" :class="{placeholder:!assetThumb(item)}"><AssetImage v-if="assetThumb(item)" :src="assetThumb(item)" :label="item.name" :accent="currentStyle?.accent"/><component v-else :is="bibleSections.find(s=>s.value===bibleSection)?.icon" :size="30"/><span>REF</span></div>
              <div class="bible-fields"><label class="bible-impact-label">名称 <PromptImpactBadge kind="label" title="用于识别和任务展示。"/><PromptImpactBadge kind="indirect" title="母稿拆解会用名称精确匹配实体；名称本身不会作为视觉描述写入 H3。"/></label><div class="bible-card-title"><input v-model="item.name" aria-label="名称"><span title="当前为工作室管理元数据；真实工作流使用项目 continuity seed。">管理 SEED {{ item.seed }} · 不注入任务</span></div><div class="bible-canonical-row" :class="{missing:!canonicalForBibleItem(item)}"><span>CANONICAL</span><strong v-if="canonicalForBibleItem(item)">{{ item.name }} = {{ canonicalForBibleItem(item) }}</strong><strong v-else>尚未补全 · 可在项目总览自动补全或人工填写</strong></div><label class="bible-impact-label">基础描述 <PromptImpactBadge kind="indirect" title="供创作和管理阅读；不会自动同步到下方视觉锚点。"/></label><textarea v-model="item.description" rows="2" aria-label="基础描述" placeholder="供制片人员阅读；需要影响画面时请同步更新下方视觉锚点"></textarea><label class="bible-impact-label">视觉锚点（中文或英文） <PromptImpactBadge kind="prompt"/><PromptImpactBadge kind="continuity"/></label><textarea v-model="item.visualPrompt" rows="3" aria-label="视觉锚点" placeholder="中文或英文均可；中文保存后请在上方审核面板生成并确认英文候选，只写不可变化的外观、结构、材质、颜色和固定标记。"></textarea><details class="bible-advanced-editor"><summary>更多直接影响提示词的结构化字段 <PromptImpactBadge kind="prompt"/><PromptImpactBadge kind="continuity"/></summary><div v-if="bibleSection==='characters'" class="form-grid two"><label class="field"><span>年龄外观</span><textarea v-model="item.ageAppearance" rows="2" placeholder="Age appearance, build and physical presence."></textarea></label><label class="field"><span>面部身份</span><textarea v-model="item.faceIdentity" rows="2" placeholder="Immutable facial geometry and identity marks."></textarea></label><label class="field"><span>固定服装</span><textarea v-model="item.wardrobe" rows="2" placeholder="Permanent wardrobe colors, cut and materials."></textarea></label><label class="field"><span>声音特征</span><textarea v-model="item.voiceProfile" rows="2" placeholder="Voice timbre, pace and stable delivery traits."></textarea></label><label class="field"><span>说话者人物描述（声音特征为空时后备）</span><textarea v-model="item.personality" rows="2" placeholder="Speaker temperament and stable dialogue presence; do not copy the management description."></textarea></label></div><div v-else-if="bibleSection==='scenes'" class="form-grid two"><label class="field"><span>地点结构</span><textarea v-model="item.location" rows="2"></textarea></label><label class="field"><span>时间</span><textarea v-model="item.timeOfDay" rows="2"></textarea></label><label class="field"><span>光线</span><textarea v-model="item.lighting" rows="2"></textarea></label><label class="field"><span>色板</span><textarea v-model="item.colorPalette" rows="2"></textarea></label><label class="field"><span>环境声</span><textarea v-model="item.ambientSound" rows="2"></textarea></label></div><div v-else class="form-grid two"><label class="field"><span>材质</span><textarea v-model="item.material" rows="2"></textarea></label><label class="field"><span>固定标记</span><textarea v-model="item.distinctiveMarks" rows="2"></textarea></label><label class="field"><span>连续性状态</span><textarea v-model="item.continuityState" rows="2"></textarea></label></div><p>以上字段会直接进入导演提示词或连续性定义；中文内容需在上方“英文候选人工审核”确认后才会替换为英文。实体名称与基础描述不在此列。</p></details><label class="bible-impact-label">参考图 <PromptImpactBadge kind="continuity" title="身份锚点与补充参考图独立保存；仍需在具体分镜绑定后才会连接工作流。"/><PromptImpactBadge kind="production"/></label><div class="reference-chips"><span v-if="item.anchorAssetId" class="anchor-reference">身份锚点：{{ assetName(item.anchorAssetId) }} <button title="解除身份锚点并保留为普通参考图；不会删除素材或候选历史。" @click="setBibleAnchor(item,null)"><Trash2 :size="11"/></button></span><span v-for="assetId in item.referenceAssetIds" :key="assetId">{{ assetName(assetId) }} <button title="设为身份锚点；只影响当前 Bible 绑定。" @click="setBibleAnchor(item,assetId)"><Lock :size="11"/></button><button title="移除普通参考图绑定；不会删除素材或候选历史。" @click="item.referenceAssetIds=item.referenceAssetIds.filter(id=>id!==assetId)"><Trash2 :size="11"/></button></span><button @click="bibleAssetPickerId=bibleAssetPickerId===item.id?'':item.id"><Plus :size="12"/>{{ bibleAssetPickerId===item.id?'收起素材库':'绑定参考图' }}</button></div><div v-if="bibleAssetPickerId===item.id" class="bible-inline-assets"><header><span>选择{{ bibleSections.find(s=>s.value===bibleSection)?.singular }}素材</span><small>只显示同分类授权素材；加入素材池不代表每个工作流都会自动连接；删除绑定不会删除候选历史。</small></header><div><button v-for="asset in studio.assets.filter(a=>a.type===assetTypeForBible())" :key="asset.id" :class="{active:bibleAssetIds(item).includes(asset.id)}" @click="toggleBibleAsset(item,asset.id)"><AssetImage :src="asset.thumbnail" :label="asset.name" :accent="currentStyle?.accent"/><span>{{ asset.name }}</span><i><Check :size="11"/></i></button></div><button class="button primary small" @click="saveBibleAssets(item,assetTypeForBible())"><Save :size="13"/>保存绑定</button></div></div>
              <section class="bible-image-candidate">
                <header><span><ImagePlus :size="15"/><strong>实体参考图候选</strong><small>只读历史；请从 ProductionRun 生成、审核和选择正式产物</small></span></header>
                <div v-if="bibleImageCandidateFor(item)" class="bible-image-status" :class="{stale:!bibleImageCandidateFor(item)?.latestCandidate?.sourceCurrent}">
                  <div><strong>冻结目标：{{ bibleSections.find(s=>s.value===bibleSection)?.singular }} · {{ bibleImageCandidateFor(item)?.entityName }}</strong><code>{{ bibleImageCandidateFor(item)?.entityId }}</code><small>来源指纹 {{ bibleImageCandidateFor(item)?.latestCandidate?.sourceFingerprint.slice(0,12) || bibleImageCandidateFor(item)?.sourceFingerprint.slice(0,12) }}…</small></div>
                  <span>{{ bibleImageTaskStatusLabel(bibleImageCandidateFor(item)?.latestCandidate?.taskStatus) }}</span>
                  <p v-if="bibleImageCandidateFor(item)?.latestCandidate&&!bibleImageCandidateFor(item)?.latestCandidate?.sourceCurrent"><AlertTriangle :size="13"/>Bible 字段、译文或视觉规则已变化；此处仅保留旧来源的历史投影，不提供选用入口。</p>
                  <div v-if="bibleImageCandidateFor(item)?.candidateHistory.length" class="bible-image-history">
                    <article v-for="(candidate,candidateIndex) in bibleImageCandidateFor(item)?.candidateHistory || []" :key="bibleImageCandidateKey(candidate)" :class="{stale:!candidate.sourceCurrent}">
                      <header><span><strong>{{ candidateIndex===0?'最新候选':'历史候选' }} #{{ (bibleImageCandidateFor(item)?.candidateHistory.length || 0) - candidateIndex }}</strong><small>{{ bibleImageTaskStatusLabel(candidate.taskStatus) }} · {{ new Date(candidate.createdAt).toLocaleString('zh-CN') }}</small></span><RouterLink v-if="candidate.generationTaskId" :to="{path:'/workflows',query:{task:candidate.generationTaskId}}" class="button ghost small"><Workflow :size="13"/>打开任务</RouterLink></header>
                      <p v-if="!candidate.sourceCurrent"><AlertTriangle :size="13"/>该候选对应旧版 Bible、译文或视觉规则，仅作为历史记录展示。</p>
                      <ul v-if="candidate.quality.warnings.length"><li v-for="warning in candidate.quality.warnings" :key="`${bibleImageCandidateKey(candidate)}:${warning}`"><AlertTriangle :size="12"/>{{ warning }}</li></ul>
                      <div v-if="bibleImageOutputImages(candidate).length" class="bible-image-outputs"><article v-for="(media,index) in bibleImageOutputImages(candidate)" :key="`${bibleImageCandidateKey(candidate)}:${media.index??index}:${media.filename}`"><button v-if="media.previewUrl||media.downloadUrl" type="button" class="bible-image-preview-button" @click="openBibleImagePreview(item,candidate,media,index)"><img :src="media.previewUrl||media.downloadUrl" :alt="`${item.name} 参考图候选 ${index+1}`"><span><Eye :size="12"/>预览全图</span></button><div v-else><Image :size="24"/><span>预览地址暂不可用</span></div><span>{{ media.filename }}</span></article></div>
                      <p v-else class="bible-image-empty">该候选还没有可预览图片；任务完成后点击刷新状态。</p>
                    </article>
                  </div>
                  <div class="bible-image-task-actions"><button type="button" class="button ghost small" :disabled="bibleImageLoading" @click="loadBibleImageCandidates(project.id)"><RefreshCw :size="13"/>刷新状态</button></div>
                </div>
                <p v-else class="bible-image-empty">尚无由 ProductionRun 归档的历史候选。</p>
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
        <div class="episode-grid">
          <article v-for="episode in project.episodes" :key="episode.id" class="panel episode-card" :class="{expanded:selectedEpisodeId===episode.id}" @click="selectEpisode(episode)">
            <div class="episode-number"><span>EPISODE</span><strong>{{ String(episode.number).padStart(2,'0') }}</strong></div>
            <div class="episode-copy"><div><StatusBadge :status="episode.status"/><span>{{ episodeProductionLabel(episode) }}</span><span v-if="assemblyFor(episode.id)?.status==='failed'&&assemblyOutputFor(episode.id)" class="episode-assembly-ready stale">重组失败 · 旧成片可用</span><span v-else-if="assemblyOutputFor(episode.id)" class="episode-assembly-ready" :class="{stale:episode.status==='rendering'||assemblyActive(episode.id)}">{{ episode.status==='rendering'||assemblyActive(episode.id)?'旧成片 · 待重组':'成片就绪' }}</span><span v-else-if="assemblyActive(episode.id)" class="episode-assembly-ready stale">{{ assemblyStateLabel(episode.id) }}</span></div><h3>{{ episode.title }}</h3><p>{{ episode.synopsis || '尚未填写本集梗概，进入分镜编辑器开始创作。' }}</p><div class="episode-tags"><span><Film :size="13"/>{{ episodeVideoCount(episode) }} 视频</span><span><Image :size="13"/>{{ episodeImageCount(episode) }} 图片</span><span><Clock3 :size="13"/>{{ formatNarrativeDuration(episodeNarrativeDurationSeconds(episode)) }} 秒叙事时长</span><span v-if="episodeVideoCount(episode)">{{ episode.audioEnabled?'原生音频':'静音' }}</span></div></div>
            <div class="episode-card-footer"><RouterLink :to="`/projects/${project.id}/episodes/${episode.id}/storyboard`" class="button ghost full" @click.stop>打开分镜编辑器 <ArrowRight :size="15"/></RouterLink><RouterLink class="button primary episode-generate-button" :to="`/projects/${project.id}/production`" @click.stop><Sparkles :size="14"/>一键生产</RouterLink><button v-if="assemblyFor(episode.id)||assemblyOutputFor(episode.id)" class="button ghost episode-output-button" @click.stop="selectEpisode(episode)"><Eye :size="14"/>{{ assemblyActive(episode.id)?assemblyStateLabel(episode.id):assemblyFor(episode.id)?.status==='failed'?'查看失败':assemblyOutputFor(episode.id)?'查看成片':'查看母版状态' }}</button><RouterLink v-else class="button ghost episode-output-button" :to="`/projects/${project.id}/production`" @click.stop><Film :size="14"/>在一键生产中生成母版</RouterLink><button class="icon-btn" @click.stop="removeEpisode(episode.id)"><Trash2 :size="15"/></button></div>
            <section v-if="selectedEpisodeId===episode.id" class="episode-assembly-panel" @click.stop>
              <header><div><span class="eyebrow">EPISODE MASTER</span><h4>第 {{ episode.number }} 集组装成片</h4><p>此处只读展示 ProductionRun 归档的分集母版；创建、重试与重新组装统一从一键生产运行发起。</p></div><button class="icon-btn" title="刷新成片链接" @click="loadEpisodeAssemblies(project.id)"><RefreshCw :size="14"/></button></header>
              <template v-if="assemblyOutputFor(episode.id)?.outputUrl">
                <div v-if="assemblyActive(episode.id)" class="episode-assembly-stale"><div class="loader-ring"></div><span>{{ assemblyStateLabel(episode.id) }}；下方旧成片仍可预览，完成后会自动替换。可以安全离开本页。</span></div>
                <div v-else-if="assemblyFor(episode.id)?.status==='failed'" class="episode-assembly-inline-error"><span>母版生成失败：{{ assemblyFor(episode.id).errorMessage || '请打开对应 ProductionRun 查看阻断原因' }}</span><RouterLink class="button ghost small" :to="`/projects/${project.id}/production`"><Workflow :size="13"/>打开运行</RouterLink></div>
                <div v-else-if="assemblyError" class="episode-assembly-inline-error"><span>母版历史读取失败：{{ assemblyError }}</span><button class="button ghost small" @click="loadEpisodeAssemblies(project.id)"><RefreshCw :size="13"/>重新读取</button></div>
                <div v-else-if="episode.status==='rendering'" class="episode-assembly-stale"><RefreshCw :size="14"/><span>本集有分镜正在重新生成；下方仍可预览旧成片。新任务完成后请重新组装本集。</span></div>
                <video :key="`${assemblyOutputFor(episode.id).id}:${assemblyOutputFor(episode.id).outputUrl}`" class="episode-master-video" :src="assemblyOutputFor(episode.id).outputUrl" controls preload="metadata" @loadedmetadata="handleAssemblyPreviewLoaded(episode.id,assemblyOutputFor(episode.id).id)" @error="recoverAssemblyPreview(episode)">当前浏览器不支持视频预览。</video>
                <div v-if="assemblyPreviewErrors[episode.id]" class="episode-assembly-inline-error"><span>成片预览失败：{{ assemblyPreviewErrors[episode.id] }}</span><button class="button ghost small" :disabled="Boolean(assemblyPreviewRefreshingId)" @click="recoverAssemblyPreview(episode,true)"><RefreshCw :size="13"/>{{ assemblyPreviewRefreshingId===assemblyOutputFor(episode.id).id?'刷新中…':'重新获取地址' }}</button></div>
                <div class="episode-master-actions"><div><strong>{{ episode.status==='rendering'||assemblyActive(episode.id)?'当前展示旧成片':'本集成片已就绪' }}</strong><span>{{ assemblyOutputFor(episode.id).sourceTaskIds?.length || episode.shots.filter(item=>item.mediaType==='video').length }} 个视频产物 · {{ assemblyOutputFor(episode.id).finishedAt ? new Date(assemblyOutputFor(episode.id).finishedAt!).toLocaleString('zh-CN') : '刚刚完成' }}</span></div><a v-if="assemblyOutputFor(episode.id).provenanceUrl" class="button ghost" :href="assemblyOutputFor(episode.id).provenanceUrl" target="_blank" rel="noopener"><FileJson :size="14"/>来源清单</a><RouterLink v-if="productionRunForAssembly(assemblyOutputFor(episode.id).id)" class="button ghost" :to="`/projects/${project.id}/production/${productionRunForAssembly(assemblyOutputFor(episode.id).id)!.id}`"><Workflow :size="14"/>打开 ProductionRun</RouterLink><RouterLink v-else class="button ghost" :to="`/projects/${project.id}/production`"><Workflow :size="14"/>查看生产运行</RouterLink><button class="button primary" @click="downloadAssembly(episode)"><Download :size="14"/>下载本集视频</button></div>
              </template>
              <div v-else-if="assemblyActive(episode.id)" class="episode-assembly-empty"><div class="loader-ring"></div><strong>{{ assemblyStateLabel(episode.id) }}</strong><p>任务由 ProductionRun 持续执行；关闭或刷新页面不会中断，回来后会自动恢复显示。</p><RouterLink class="button ghost small" :to="`/projects/${project.id}/production`"><Workflow :size="14"/>查看生产运行</RouterLink></div>
              <div v-else-if="assemblyFor(episode.id)?.status==='failed'||assemblyError" class="episode-assembly-empty error"><strong>本集母版不可用</strong><p>{{ assemblyFor(episode.id)?.errorMessage || assemblyError }}</p><RouterLink class="button ghost small" :to="`/projects/${project.id}/production`"><Workflow :size="14"/>打开运行并处理</RouterLink></div>
              <div v-else class="episode-assembly-empty"><Film :size="28"/><strong>本集还没有组装成片</strong><p>分集母版由 ProductionRun 的组装与交付阶段统一创建，本页不再提供低层旁路。</p><RouterLink class="button primary" :to="`/projects/${project.id}/production`"><Workflow :size="14"/>进入一键生产</RouterLink></div>
            </section>
          </article>
          <button class="episode-add-card" @click="addEpisodeOpen=true"><div><Plus :size="25"/></div><strong>新增第 {{ project.episodes.length+1 }} 集</strong><span>集数没有上限，可持续更新直至完结</span></button>
        </div>
      </section>
    </template>

    <template v-else>
      <section class="release-workspace">
        <header class="section-heading release-heading"><div><span class="eyebrow">PRODUCTION DELIVERY · READ ONLY</span><h3>发行包与版本清单</h3><p>此页只读展示 ProductionRun 生成并封存的 ReleaseBundle。创建、选片、组装和封存只有一条服务端生产主链，不再提供旧低层写入口。</p></div><div class="release-heading-actions"><RouterLink class="button primary" :to="`/projects/${project.id}/production`"><Workflow :size="15"/>进入一键生产</RouterLink><button class="button ghost" :disabled="releaseLoading" @click="loadReleaseBundles(project.id)"><RefreshCw :size="15"/>刷新版本</button></div></header>
        <div class="release-principles panel"><div><Lock :size="20"/><span><strong>ProductionRun 封存</strong><small>交付阶段验证媒体哈希、来源清单和逐集母版后，生成不可变发行包。</small></span></div><div><Film :size="20"/><span><strong>逐集母版</strong><small>每集母版独立交付；默认不拼接整季长视频。</small></span></div><div><FileJson :size="20"/><span><strong>只读诊断</strong><small>可查看版本与实时校验结果；需要处理时跳转对应 ProductionRun。</small></span></div></div>
        <div v-if="releaseError" class="release-error"><span>{{ releaseError }}</span><button @click="releaseError=''">关闭</button></div>

        <section class="release-history"><header><div><span class="eyebrow">RELEASE HISTORY</span><h4>版本历史</h4></div><small>{{ releaseBundles.length }} 个版本</small></header><div v-if="releaseBundles.length" class="release-list"><article v-for="bundle in releaseBundles" :key="bundle.id" class="panel release-row"><div class="release-version"><span>V</span><strong>{{ bundle.version }}</strong></div><div class="release-copy"><div><span class="release-status" :class="bundle.status">{{ bundle.status==='released'?'已发行':bundle.status==='sealed'?'已封存':bundle.status==='superseded'?'已替代':'草稿' }}</span><span :class="['verification-chip',bundle.verificationStatus]">{{ bundle.verificationStatus==='verified'?'签名已验证':bundle.verificationStatus==='invalid'?'签名异常':'待封存' }}</span></div><h4>{{ bundle.title }}</h4><p>{{ bundle.notes || '没有版本说明' }}</p><small>{{ bundle.selection.shots.length }} 个分镜 · {{ bundle.selection.episodes.length }} 集母版<span v-if="bundle.selection.seriesAssemblyId"> · 含整剧成片</span><span v-if="bundle.manifestSha256"> · SHA {{ bundle.manifestSha256.slice(0,12) }}…</span></small><small v-if="productionRunForRelease(bundle.id)">ProductionRun {{ productionRunForRelease(bundle.id)!.id }}</small><small v-else>未从运行列表解析到来源 ProductionRun</small></div><div class="release-row-actions"><RouterLink v-if="productionRunForRelease(bundle.id)" class="button primary small" :to="`/projects/${project.id}/production/${productionRunForRelease(bundle.id)!.id}`"><Workflow :size="13"/>打开运行</RouterLink><button v-if="bundle.status!=='draft'" class="button ghost small" :disabled="releaseLoading" @click="verifyReleaseCandidate(bundle)"><Check :size="13"/>实时校验产物</button></div></article></div><div v-else class="panel release-empty"><FileJson :size="28"/><strong>还没有发行版本</strong><p>ReleaseBundle 只会由 ProductionRun 的交付阶段创建和封存，本页不会建立旁路草稿。</p><RouterLink class="button primary" :to="`/projects/${project.id}/production`"><Workflow :size="14"/>进入一键生产</RouterLink></div></section>
      </section>
    </template>

    <BaseModal :open="!!bibleImagePreview" title="候选参考图完整预览" eyebrow="BIBLE IMAGE CANDIDATE" width="920px" @close="bibleImagePreview=null">
      <div v-if="bibleImagePreview" class="bible-image-full-preview"><img :src="bibleImagePreview.src" :alt="bibleImagePreview.title"><footer><span><strong>{{ bibleImagePreview.title }}</strong><small>{{ bibleImagePreview.filename }}</small></span><a v-if="bibleImagePreview.downloadUrl" class="button ghost small" :href="bibleImagePreview.downloadUrl" target="_blank" rel="noopener"><Download :size="13"/>打开原图</a></footer></div>
    </BaseModal>
    <BaseModal :open="addBibleOpen" :title="`新增${bibleSections.find(s=>s.value===bibleSection)?.singular}设定`" eyebrow="NEW CONTINUITY ANCHOR" width="1040px" @close="closeAddBibleModal">
      <div class="bible-draft-editor">
        <div class="bible-draft-evidence"><Lock :size="17"/><span><strong>人工创建正式 Bible 对象</strong><small>参考图只建立素材绑定；名称、描述和视觉锚点均由你直接填写，保存后回到 ProductionRun 门禁。</small></span><b>MANUAL</b></div>

        <section class="bible-asset-browser">
          <header><div><span class="impact-label-row">选择 1 张参考图 <PromptImpactBadge kind="continuity"/><PromptImpactBadge kind="production" title="创建后绑定到 Bible 素材池；具体镜头仍需明确选择才会接入视频工作流。"/></span><small>显示素材库全部图片；只有与当前{{ bibleSections.find(s=>s.value===bibleSection)?.singular }}类别一致的素材可选，切换选择会替换上一张。</small></div><div class="bible-asset-tools"><label><Search :size="14"/><input v-model="bibleAssetSearch" placeholder="搜索名称、描述、视觉锚点"></label><select v-model="bibleAssetCategory"><option value="compatible">仅可用于当前设定</option><option value="all">全部图片</option><option value="character">人物</option><option value="scene">场景</option><option value="prop">道具</option><option value="image">未分类图片</option></select></div></header>
          <div v-if="bibleAssetCatalogLoading" class="bible-asset-loading"><div class="loader-ring"></div><span>正在独立加载素材库全部参考图…</span></div>
          <div v-else-if="bibleAssetCatalogError" class="bible-draft-error"><AlertTriangle :size="14"/><span>{{ bibleAssetCatalogError }}</span><button type="button" @click="openAddBibleModal"><RefreshCw :size="13"/>重试</button></div>
          <div v-else-if="filteredBibleAssetCatalog.length" class="bible-asset-catalog">
            <article v-for="asset in filteredBibleAssetCatalog" :key="asset.id" :class="{active:bibleDraft.referenceAssetIds[0]===asset.id,incompatible:!isBibleDraftAssetCompatible(asset)}">
              <button type="button" class="bible-asset-select" :disabled="!isBibleDraftAssetCompatible(asset)||bibleCreatePhase!=='idle'" :title="isBibleDraftAssetCompatible(asset)?`选择 ${asset.name}`:bibleDraftAssetUnavailableReason(asset)" @click="selectBibleDraftAsset(asset)"><AssetImage :src="asset.thumbnail" :label="asset.name" :accent="currentStyle?.accent"/><span><strong>{{ asset.name }}</strong><small>{{ bibleAssetTypeLabel(asset) }}<template v-if="asset.isActive===false"> · 已停用</template><template v-else-if="!asset.promptHint"> · 缺视觉锚点</template></small></span><i><Check :size="12"/></i></button>
              <button type="button" class="bible-asset-preview" :aria-label="`预览 ${asset.name}`" @click="bibleAssetPreview=asset"><Eye :size="13"/></button>
            </article>
          </div>
          <div v-else class="bible-asset-empty"><Image :size="22"/><span>没有匹配的参考图</span><button v-if="bibleAssetSearch||bibleAssetCategory!=='compatible'" type="button" @click="bibleAssetSearch='';bibleAssetCategory='compatible'">清除筛选</button></div>
          <footer><span>素材库 {{ bibleAssetCatalog.length }} 张图片 · 当前显示 {{ filteredBibleAssetCatalog.length }} 张</span><RouterLink to="/assets" target="_blank">维护素材名称与说明 <ArrowRight :size="12"/></RouterLink></footer>
        </section>

        <div v-if="bibleCreateError" class="bible-draft-error"><AlertTriangle :size="14"/><span>{{ bibleCreateError }}</span><button type="button" @click="bibleCreateError=''">关闭</button></div>

        <div class="form-stack bible-draft-fields">
          <label class="field"><span class="impact-label-row">名称 * <PromptImpactBadge kind="label"/><PromptImpactBadge kind="indirect" title="用于正式脚本与生产阶段的实体匹配。"/></span><input v-model="bibleDraft.name" :disabled="bibleCreatePhase!=='idle'" placeholder="可使用中文；项目内需唯一"><small>请填写稳定、唯一且可供脚本匹配的正式名称。</small></label>
          <label class="field"><span class="impact-label-row">基础描述 <PromptImpactBadge kind="indirect" title="供创作与管理阅读，不会直接进入 H3。"/></span><textarea v-model="bibleDraft.description" :disabled="bibleCreatePhase!=='idle'" rows="3" placeholder="可用中文或英文记录创作意图；不会直接进入 H3"></textarea></label>
          <label class="field"><span class="impact-label-row">视觉锚点（中文或英文）* <PromptImpactBadge kind="prompt"/><PromptImpactBadge kind="continuity"/></span><textarea v-model="bibleDraft.visualPrompt" :disabled="bibleCreatePhase!=='idle'" rows="4" placeholder="只写不可变化的外观、结构、材质、颜色和固定标记，不写动作或运镜。"></textarea><small>保存正式对象后，通过 ProductionRun 的审核门禁处理后续编译。</small></label>
        </div>
        <div v-if="bibleCreatePhase!=='idle'" class="canonical-create-progress"><div class="loader-ring"></div><span><strong>{{ bibleCreatePhase==='saving'?'正在保存 Bible…':'Bible 已保存，正在处理名称术语与视觉锚点…' }}</strong><small>{{ bibleCreatePhase==='canonicalizing'?'中文视觉锚点只会产生待审核英文候选，不会覆盖原文或自动确认。':'保存成功后才会按项目配置调用标准化模型。' }}</small></span></div>
        <PromptWritingGuide scope="bible"/>
      </div>
      <template #footer><button class="button ghost" :disabled="bibleCreatePhase!=='idle'" @click="closeAddBibleModal">取消</button><button class="button primary" :disabled="bibleCreatePhase!=='idle'" @click="addBibleItem"><Lock :size="15"/>{{ bibleCreatePhase==='saving'?'正在保存设定…':bibleCreatePhase==='canonicalizing'?'正在处理正式字段…':'确认字段并创建设定' }}</button></template>
    </BaseModal>
    <BaseModal :open="!!bibleAssetPreview" title="参考图完整预览" eyebrow="ASSET PREVIEW" width="920px" @close="bibleAssetPreview=null"><div v-if="bibleAssetPreview" class="bible-asset-full-preview"><AssetImage :src="bibleAssetPreview.thumbnail||bibleAssetPreview.mediaUrl" :label="bibleAssetPreview.name"/><footer><span><strong>{{ bibleAssetPreview.name }}</strong><small>{{ bibleAssetTypeLabel(bibleAssetPreview) }} · {{ bibleAssetPreview.description || '未填写基础描述' }}</small></span><button v-if="isBibleDraftAssetCompatible(bibleAssetPreview)" type="button" class="button primary small" @click="selectBibleDraftAsset(bibleAssetPreview);bibleAssetPreview=null"><Check :size="13"/>选作参考图</button></footer></div></BaseModal>
    <BaseModal :open="addEpisodeOpen" title="新增连续剧集" eyebrow="NEXT EPISODE" @close="addEpisodeOpen=false">
      <div class="next-episode-banner"><span>EP</span><strong>{{ String(project.episodes.length+1).padStart(2,'0') }}</strong><p>自动继承 {{ project.bible.characters.length }} 位角色、{{ project.bible.scenes.length }} 个场景与 {{ project.bible.props.length }} 件道具的视觉锚点。</p></div><div class="form-stack"><label class="field"><span class="impact-label-row">本集标题 <PromptImpactBadge kind="label" title="用于剧集、任务和成片识别；不会原样写入单镜提示词。"/></span><input v-model="episodeDraft.title" :placeholder="`第 ${project.episodes.length+1} 集`"></label><label class="field"><span class="impact-label-row">本集梗概 <PromptImpactBadge kind="indirect" title="用于结构预览与分镜拆解；不会原样复制进正式提示词。"/></span><textarea v-model="episodeDraft.synopsis" rows="4" placeholder="描述本集的起因、升级、转折与钩子。"></textarea></label><label class="field"><span class="impact-label-row">本集剧本原文 <PromptImpactBadge kind="indirect" title="分镜拆解读取；对白只能从此处或全剧母稿精确引用。"/></span><textarea v-model="episodeDraft.script" rows="8" placeholder="粘贴本集场景、动作和中文对白原文。建议保留角色名与场次；留空时使用全剧母稿。"></textarea><small>这是创作事实来源，不会整段塞入工作流；对白正文必须是这里或全剧母稿的精确子串。</small></label><div class="inherit-note"><Lock :size="15"/><span>本集将使用项目默认的 <strong>{{ workflowLabel(project.defaultMode) }}</strong>；具体模型与参数由 ProductionRun 冻结。</span></div></div>
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
.creation-mode-tabs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:10px 0 14px}.creation-mode-tabs button{display:grid;grid-template-columns:auto 1fr;gap:3px 8px;align-items:center;padding:11px;border:1px solid #2a323a;border-radius:8px;background:#0b1014;color:#8c969e;text-align:left;cursor:pointer}.creation-mode-tabs button svg{grid-row:1/3;color:#c98e43}.creation-mode-tabs button strong{font-size:10px;color:#cbd1d5}.creation-mode-tabs button small{font-size:8px;line-height:1.4}.creation-mode-tabs button.active{border-color:#b67b35;background:rgba(195,132,55,.08);box-shadow:0 0 0 1px rgba(195,132,55,.12)}.creation-subtabs{display:flex;gap:6px;margin:4px 0}.creation-subtabs button{padding:7px 11px;border:1px solid #293039;border-radius:6px;background:#0b1014;color:#77818a;font-size:9px;cursor:pointer}.creation-subtabs button.active{border-color:#a9783d;color:#dda357}.onboarding-run-options{padding:11px;border:1px solid #28313a;border-radius:8px;background:#0b1014}.creation-error{display:flex;align-items:flex-start;gap:7px;margin:7px 0;color:#e4868d;font-size:9px;line-height:1.5}.creation-error a{margin-left:auto;color:#d69a4a}.import-review{display:flex;flex-direction:column;gap:12px}.import-review-evidence{display:flex;align-items:flex-start;gap:9px;padding:11px;border:1px solid rgba(104,197,167,.25);border-radius:7px;background:rgba(104,197,167,.05);color:#67c6a7}.import-review-evidence span{display:flex;flex-direction:column;gap:3px}.import-review-evidence strong{font-size:10px}.import-review-evidence small{font-size:8px;line-height:1.5;color:#739a8d}.import-review textarea{font-family:Consolas,monospace;line-height:1.6}
.bible-image-global-state{display:flex;align-items:center;gap:8px;padding:9px 11px;border:1px solid #283039;border-radius:7px;background:#0b0f13;color:#838d95;font-size:10px}.bible-image-global-state>span{flex:1}.bible-image-global-state button{display:flex;align-items:center;gap:5px;border:0;background:none;color:#d6a054;cursor:pointer}.bible-image-global-state.error{border-color:#593039;color:#e78c94}.bible-image-prompt-config{margin-top:12px;padding:12px;border:1px solid #2b3540;border-radius:8px;background:#0a0f13}.bible-image-prompt-config>summary,.bible-image-negative-config>summary{display:flex;align-items:center;justify-content:space-between;gap:12px;cursor:pointer;color:#cdd5db}.bible-image-prompt-config>summary span{display:flex;flex-direction:column;gap:3px}.bible-image-prompt-config small{color:#74808a;font-size:9px}.bible-image-prompt-config>.form-grid,.bible-image-negative-config>.form-grid{margin-top:12px}.bible-image-negative-config{grid-column:1/-1;margin-top:10px;padding-top:10px;border-top:1px solid #202a33}.bible-image-candidate{grid-column:1/-1;display:flex;flex-direction:column;gap:9px;padding:12px;border:1px solid #252d34;border-radius:8px;background:#090d11}.bible-image-candidate>header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.bible-image-candidate>header>span{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.bible-image-candidate>header small{width:100%;padding-left:21px;color:#707982;font-size:9px}.bible-image-empty{margin:0;color:#6f7880;font-size:9px}.bible-image-status{display:flex;flex-direction:column;gap:8px;padding-top:9px;border-top:1px solid #222a31}.bible-image-status>div:first-child{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.bible-image-status code{color:#70808e;font-size:8px}.bible-image-status>span{align-self:flex-start;padding:3px 7px;border:1px solid #31553e;border-radius:10px;color:#6fc68f;font-size:8px}.bible-image-status.stale>span{border-color:#6a4927;color:#df9e5c}.bible-image-status>p,.bible-image-status li{display:flex;align-items:flex-start;gap:6px;margin:0;color:#dda063;font-size:9px;line-height:1.5}.bible-image-status ul{display:grid;gap:5px;margin:0;padding:9px 10px;list-style:none;border:1px solid #4d3b22;border-radius:6px;background:#17130c}.bible-image-task-actions{display:flex!important;justify-content:flex-end}.bible-image-outputs{display:grid!important;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:9px!important}.bible-image-outputs article{display:flex;flex-direction:column;gap:6px;padding:7px;border:1px solid #293139;border-radius:7px;background:#0c1115}.bible-image-outputs img,.bible-image-outputs article>div{width:100%;aspect-ratio:1/1;border-radius:5px;object-fit:cover;background:#11171c}.bible-image-outputs article>div{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:5px;color:#68727b;font-size:8px}.bible-image-outputs article>span{overflow:hidden;color:#79838b;font-size:8px;text-overflow:ellipsis;white-space:nowrap}.bible-image-history{display:flex;flex-direction:column;gap:10px}.bible-image-history>article{display:flex;flex-direction:column;gap:8px;padding:9px;border:1px solid #252e36;border-radius:7px;background:#0a0f13}.bible-image-history>article.stale{border-color:#5b3c23;background:#15100a}.bible-image-history header{display:flex;align-items:center;justify-content:space-between;gap:9px}.bible-image-history header>span{display:flex;min-width:0;flex-direction:column;gap:3px}.bible-image-history header strong{color:#cfd5da;font-size:10px}.bible-image-history header small{color:#74808a;font-size:8px}.bible-image-history p{display:flex;align-items:flex-start;gap:6px;margin:0;color:#dda063;font-size:9px;line-height:1.5}.bible-image-preview-button{position:relative;width:100%;aspect-ratio:1/1;padding:0;overflow:hidden;border:0;border-radius:5px;background:#11171c;cursor:zoom-in}.bible-image-preview-button img{width:100%;height:100%;object-fit:cover}.bible-image-preview-button>span{position:absolute;right:5px;bottom:5px;display:flex;align-items:center;gap:4px;padding:3px 6px;border-radius:999px;background:rgba(5,8,11,.78);color:#d8dde1;font-size:8px}.bible-image-full-preview{display:flex;flex-direction:column;gap:10px}.bible-image-full-preview>img{width:100%;height:min(72vh,720px);object-fit:contain;border-radius:8px;background:#080b0e}.bible-image-full-preview>footer{display:flex;align-items:center;justify-content:space-between;gap:14px}.bible-image-full-preview>footer>span{display:flex;min-width:0;flex-direction:column;gap:4px}.bible-image-full-preview strong{font-size:12px}.bible-image-full-preview small{overflow:hidden;color:#737c84;font-size:9px;text-overflow:ellipsis;white-space:nowrap}
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
