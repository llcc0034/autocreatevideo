<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { AlertTriangle, ArrowLeft, ArrowRight, BookOpen, Box, Check, Clapperboard, Clock3, Download, Eye, Film, FolderInput, Image, ImagePlus, Layers3, Lock, Plus, RefreshCw, Save, Search, Sparkles, Trash2, Unlock, Upload, UserRound, Workflow } from 'lucide-vue-next'
import BaseModal from '@/components/BaseModal.vue'
import BibleVariantEditor from '@/components/BibleVariantEditor.vue'
import type { BibleVariantScope } from '@/services/bibleVariants'
import VisualStyleManager from '@/components/VisualStyleManager.vue'
import RecipeExecutionSummary from '@/components/RecipeExecutionSummary.vue'
import RecipeProjectSourceConfirmations from '@/components/RecipeProjectSourceConfirmations.vue'
import type { RecipeProjectSourceConfirmation } from '@/services/platformTypes'
import ProductionBudgetFields from '@/components/ProductionBudgetFields.vue'
import ProjectVersionWorkspace from '@/components/ProjectVersionWorkspace.vue'
import { projectRunSelection } from '@/services/productionRunPresentation'
import { onboardingManualInput, isDefinitiveCreationRejection } from '@/services/productionOnboarding'
import { recipeExecutionOverview } from '@/services/recipeExecutionOverview'
import StatusBadge from '@/components/StatusBadge.vue'
import ToggleSwitch from '@/components/ToggleSwitch.vue'
import AssetImage from '@/components/AssetImage.vue'
import ProjectCoverImage from '@/components/ProjectCoverImage.vue'
import PromptImpactBadge from '@/components/PromptImpactBadge.vue'
import PromptWritingGuide from '@/components/PromptWritingGuide.vue'
import TranslationReviewPanel from '@/components/TranslationReviewPanel.vue'
import ProjectFieldLineage from '@/components/ProjectFieldLineage.vue'
import ProjectOriginalNames from '@/components/ProjectOriginalNames.vue'
import { productionErrorCode, repository, type VideoConditioningCapability, type ProductionCreationDefaults } from '@/services/repository'
import { PROJECT_COVER_ACCEPT, projectCoverFileError } from '@/services/projectManagement'
import { bibleImageCandidateErrorMessage } from '@/services/bibleImageCandidates'
import { bibleAssetIds } from '@/services/bibleAssets'
import { useStudioStore } from '@/stores/studio'
import { platformApi } from '@/services/platformApi'
import { apiErrorSummary } from '@/services/apiErrorPresentation'
import { plannedAverageShotDurationMs } from '@/services/productionRunPayload'
import { productionRecipeInteractionMode, productionRecipeScriptMode } from '@/services/platformAdapters'
import { conditioningModeByKey } from '@/services/conditioningCapabilities'
import { episodeNarrativeDurationSeconds, formatNarrativeDuration } from '@/services/shotDuration'
import { useAuthStore } from '@/stores/auth'
import type { ProductionRecipe, ProductionRun, ProductionRunCreateInput, ScriptRevision } from '@/services/platformTypes'
import type { Asset, BibleImageCandidateProjection, BibleImageCandidatesResult, BibleImageEntityType, BibleImageLatestCandidate, BibleItem, Episode, PromptTranslationField, SeriesProject } from '@/types'

const studio = useStudioStore(); const auth = useAuthStore(); const route = useRoute(); const router = useRouter()
type StudioTab = 'overview'|'bible'|'episodes'
const props = defineProps<{ inputProject?: SeriesProject; inputSection?: StudioTab; inputRunId?: string; inputRun?: ProductionRun; inputActive?: boolean; inputDisabled?: boolean }>()
const emit = defineEmits<{ contextProtected: [value: boolean]; saved: [] }>()
const embedded = computed(() => Boolean(props.inputProject))
const studioTabFromRoute = (): StudioTab => {
  const requested = props.inputSection
  return ['overview', 'bible', 'episodes'].includes(String(requested || ''))
    ? requested as StudioTab
    : 'overview'
}
const project = ref<SeriesProject | null>(null); const loading = ref(true); const projectLoadError = ref(''); const saving = ref(false); const tab = ref<StudioTab>(studioTabFromRoute()); const bibleSection = ref<'characters'|'scenes'|'props'>('characters')
const editingInputs = computed(() => embedded.value && props.inputActive !== false)
function openProjectInputs(section?: StudioTab) {
  const run = typeof route.query.run === 'string' ? route.query.run : ''
  return router.push({ path: `/projects/${project.value?.id}/production${run ? '/' + run : ''}`, query: section ? { step: section === 'overview' ? 'visual' : section === 'episodes' ? 'storyboard' : section } : {} })
}
const addBibleOpen = ref(false); const addEpisodeOpen = ref(false); const createLoading = ref(false)
const bibleAssetPickerId = ref('')
const variantEntity = ref<{ scope: BibleVariantScope; name: string } | null>(null)
const variantBusy = ref(false), variantDirty = ref(false)
function openVariantSettings(item: BibleItem, kind: BibleImageEntityType = assetTypeForBible()) {
  if (!project.value) return
  variantBusy.value = false; variantDirty.value = false
  variantEntity.value = { name: item.name, scope: { project_id: project.value.id, entity_kind: kind, entity_id: item.id } }
}
function closeVariantSettings() {
  if (variantBusy.value || (variantDirty.value && !window.confirm('放弃尚未保存的变体设定编辑？已保存记录不受影响。'))) return
  variantEntity.value = null; variantDirty.value = false
}
function leaveVariantSettings() {
  if (variantBusy.value) return false
  if (variantDirty.value && !window.confirm('离开页面会放弃未保存的变体设定编辑，是否继续？')) return false
  variantEntity.value = null; variantDirty.value = false
  return true
}
onBeforeRouteLeave(leaveVariantSettings)
onBeforeRouteUpdate(leaveVariantSettings)
const bibleImageCandidates = ref<BibleImageCandidatesResult | null>(null)
const bibleImageLoading = ref(false)
const bibleImageLoadError = ref('')
const bibleImagePreview = ref<{ src: string; title: string; filename: string; downloadUrl: string } | null>(null)
const productionRuns = ref<ProductionRun[]>([])
const videoConditioningCapability = ref<VideoConditioningCapability | null>(null)
const videoConditioningCapabilityError = ref('')
const videoConditioningCapabilityLoading = ref(false), projectRunsLoading = ref(false)
const bibleAssetsLoading = ref(false), bibleAssetsError = ref(''), bibleAssetsLoaded = ref(false)
const detailReadsStarted = new Set<string>()
// One page visit owns its reads. A late response cannot update another project or login.
const currentProjectRead = (projectId: string) => {
  const epoch = creationPageEpoch, session = studio.captureSessionEpoch()
  return () => epoch === creationPageEpoch && session === studio.captureSessionEpoch() && project.value?.id === projectId && !isNew.value
}
const creationDefaults = ref<ProductionCreationDefaults | null>(null)
const creationPolicyLoading = ref(false)
const creationPolicyError = ref('')
const creationStylesLoading = ref(false)
const creationStylesError = ref('')
const creationRecipesError = ref('')
let creationDefaultsApplied = false
let creationPageEpoch = 0
let creationRecipesLoaded = false
const newProject = ref({ title: '', logline: '', masterScript: '', styleId: '', language: '', aspectRatio: '', width: undefined as number | undefined, height: undefined as number | undefined })
const showStyleManager = ref(false)
const selectableStyles = computed(() => studio.styles.filter(style => style.isActive))
const stylesChanged = () => {
  if (!selectableStyles.value.some(style => style.id === newProject.value.styleId)) newProject.value.styleId = ''
  creationStylesError.value = selectableStyles.value.length ? '' : '尚无可选视觉风格，请在视觉风格管理中新增或启用。'
}
const creationMode = ref<'manual'|'import'|'ai'>('manual')
const creationPlanSource = ref<'recipe'|'manual'>('manual')
const creationRunKey = ref(crypto.randomUUID())
const creationRunRequest = ref<ProductionRunCreateInput | null>(null)
const projectSourceConfirmations=ref<RecipeProjectSourceConfirmation[]>([]), projectSourceReady=ref(false)
const projectCreateUncertain = ref(false)
const importCreateUncertain = ref(false)
const projectRunsError = ref('')
const selectedProjectRunId = computed(() => props.inputRunId || (typeof route.query.run === 'string'
  ? projectRunSelection(productionRuns.value, route.query.run) : ''))
const projectProductionTarget = computed(() => selectedProjectRunId.value
  ? `/projects/${project.value?.id}/production/${selectedProjectRunId.value}`
  : `/projects/${project.value?.id}/production?view=list`)
const creationRecipes = ref<ProductionRecipe[]>([])
const creationRecipeId = ref('')
const creationRecipesLoading = ref(false)
const creationUnattendedConfirmed = ref(false)
const creationError = ref('')
const importMode = ref<'paste'|'file'>('paste')
const importText = ref('')
const importFormat = ref('plain')
const importFile = ref<File | null>(null)
const importPreview = ref<ScriptRevision | null>(null)
const createdProjectForOnboarding = ref<SeriesProject | null>(null)
const importReviewOpen = ref(false)
const aiIdea = ref('')
const aiEpisodeCount = ref<number | null>(null)
const aiShotsPerEpisode = ref<number | null>(null)
const aiEpisodeDurationSeconds = ref<number | null>(null)
const creationRunPlan = computed<{
  episodeCount: number
  shotsPerEpisode: number
  episodeDurationSeconds: number
} | null>(() => {
  const values = [
    aiEpisodeCount.value,
    aiShotsPerEpisode.value,
    aiEpisodeDurationSeconds.value,
  ]
  if (!values.every((value) => Number.isSafeInteger(value) && Number(value) > 0)) return null
  return {
    episodeCount: aiEpisodeCount.value as number,
    shotsPerEpisode: aiShotsPerEpisode.value as number,
    episodeDurationSeconds: aiEpisodeDurationSeconds.value as number,
  }
})
const aiPlannedAverageShotDurationMs = computed(() => {
  const plan = creationRunPlan.value
  if (!plan) return null
  try {
    return plannedAverageShotDurationMs(
      plan.shotsPerEpisode,
      plan.episodeDurationSeconds,
    )
  } catch {
    return null
  }
})
const aiAllowExternalCalls = ref(false)
const coverInput = ref<HTMLInputElement | null>(null)
const pendingCoverFile = ref<File | null>(null)
const pendingCoverPreview = ref('')
const coverSaving = ref(false)
const coverError = ref('')
const proseTranslationOpen = ref(false), proseTranslationLoaded = ref(false)
function toggleProseTranslation(event: Event) {
  proseTranslationOpen.value = (event.target as HTMLDetailsElement).open
  if (proseTranslationOpen.value) proseTranslationLoaded.value = true
}
const bibleCreatePhase = ref<'idle'|'saving'>('idle')
const bibleEntitySaveNotice = ref<{ kind: 'success'|'warning'; message: string } | null>(null)
const translationReviewRefreshKey = ref(0)
const bibleDraft = ref({ name: '', description: '', referenceAssetIds: [] as string[] })
const bibleCreateError = ref('')
const bibleAssetCatalog = ref<Asset[]>([]); const bibleAssetCatalogLoading = ref(false); const bibleAssetCatalogError = ref(''); const bibleAssetSearch = ref(''); const bibleAssetCategory = ref<'compatible'|'all'|'character'|'scene'|'prop'|'image'>('compatible'); const bibleAssetPreview = ref<Asset|null>(null)
const episodeDraft = ref({ title: '', synopsis: '', script: '' })
const inputBaseline = ref(''), inputSaveError = ref('')
const inputSnapshot = () => JSON.stringify(project.value)
const inputDirty = computed(() => embedded.value && Boolean(inputBaseline.value) && inputSnapshot() !== inputBaseline.value)
const inputProtected = computed(() => inputDirty.value || saving.value || variantBusy.value || variantDirty.value || bibleCreatePhase.value === 'saving' || Boolean(variantEntity.value) || addBibleOpen.value || addEpisodeOpen.value)
watch(inputProtected, value => emit('contextProtected', value), { flush: 'sync' })
function acceptInputProject(value: SeriesProject) {
  if (project.value?.id !== value.id) {
    proseTranslationOpen.value = false; proseTranslationLoaded.value = false
  }
  project.value = JSON.parse(JSON.stringify(value))
  inputBaseline.value = inputSnapshot()
}
function discardInputChanges() {
  if (saving.value || !props.inputProject || !window.confirm('放弃尚未保存的项目输入编辑？已保存的制作记录不受影响。')) return
  acceptInputProject(props.inputProject)
}
function protectInputUnload(event: BeforeUnloadEvent) {
  if (!embedded.value || !inputProtected.value) return
  event.preventDefault(); event.returnValue = ''
}
onMounted(() => { if (typeof window !== 'undefined') window.addEventListener?.('beforeunload', protectInputUnload) })
onBeforeUnmount(() => { if (typeof window !== 'undefined') window.removeEventListener?.('beforeunload', protectInputUnload) })
function leaveInputEditor() {
  if (!embedded.value) return true
  if (saving.value) return false
  return !inputDirty.value || window.confirm('项目输入尚未保存，离开会丢弃这些编辑。确定离开？')
}
onBeforeRouteLeave(leaveInputEditor)
onBeforeRouteUpdate((to, from) => to.params.projectId === from.params.projectId && to.params.runId === from.params.runId ? true : leaveInputEditor())
watch(() => props.inputProject, value => {
  if (value && !inputProtected.value) acceptInputProject(value)
})
watch(() => props.inputSection, value => { if (value) tab.value = value })
const isNew = computed(() => !embedded.value && route.name === 'project-new')
const currentStyle = computed(() => studio.styles.find((item) => item.id === (isNew.value ? newProject.value.styleId : project.value?.styleId)))
const newProjectAspectRatioOptions = computed(() => creationDefaults.value?.aspectRatioOptions || [])
const creationReady = computed(() => Boolean(creationDefaults.value && !creationPolicyLoading.value && !creationPolicyError.value && !creationStylesLoading.value && !creationStylesError.value && selectableStyles.value.some(style => style.id === newProject.value.styleId) && (creationPlanSource.value !== 'recipe' || (!creationRecipesLoading.value && !creationRecipesError.value && selectedCreationRecipe.value))))
const newProjectDimensions = computed(() => newProject.value.width && newProject.value.height ? `${newProject.value.width} × ${newProject.value.height} px` : '尺寸待加载')
const applyNewProjectAspectRatio = (value: string) => {
  const option = newProjectAspectRatioOptions.value.find((item) => item.value === value)
  if (!option) {
    newProject.value.aspectRatio = ''
    newProject.value.width = undefined
    newProject.value.height = undefined
    return
  }
  newProject.value.aspectRatio = option.value
  newProject.value.width = option.width
  newProject.value.height = option.height
}
const newProjectStyleHint = computed(() => currentStyle.value
  ? [currentStyle.value.name, currentStyle.value.description, currentStyle.value.tags.join('、')].filter(Boolean).join('；')
  : '')
const compatibleCreationRecipes = computed(() => creationRecipes.value.filter((recipe) => {
  const mode = productionRecipeScriptMode(recipe.definition)
  return creationMode.value === 'ai' ? mode === 'ai' : creationMode.value === 'import' ? mode === 'confirmed_revision' : false
}))
const selectedCreationRecipe = computed(() => creationPlanSource.value === 'recipe' ? compatibleCreationRecipes.value.find((recipe) => recipe.id === creationRecipeId.value) : undefined)
const creationIsUnattendedRecipe = computed(() => (
  productionRecipeInteractionMode(selectedCreationRecipe.value?.definition || {}) === 'unattended'
))
const selectedCreationRecipeSummary = computed(() => {
  const definition = selectedCreationRecipe.value?.definition
  const stages = definition && Array.isArray(definition.stages) ? definition.stages : []
  const reviewStages = stages.filter((value) => value && typeof value === 'object' && (value as Record<string, unknown>).approval === 'required')
  const hasStructure = stages.some((value) => {
    if (!value || typeof value !== 'object') return false
    const stage = value as Record<string, unknown>
    return stage.key === 'script_structure' && stage.mode === 'ai'
  })
  const hasMediaGeneration = stages.some((value) => value && typeof value === 'object' && (value as Record<string, unknown>).key === 'media_generation')
  const hasBibleImageGeneration = stages.some((value) => value && typeof value === 'object' && (value as Record<string, unknown>).key === 'bible_image_generation')
  return {
    stageCount: stages.length,
    reviewCount: reviewStages.length,
    hasStructure,
    hasMediaGeneration,
    hasBibleImageGeneration,
    label: stages.length
      ? `${stages.length} 个阶段 · ${reviewStages.length} 个人工门禁 · 其余阶段自动推进`
      : '配方未返回可识别的阶段定义',
  }
})
const conditioningModes = computed(() => videoConditioningCapability.value?.conditioningModes || [])
const creationRecipeContractError = computed(() => creationPlanSource.value === 'recipe' && selectedCreationRecipe.value
  ? recipeExecutionOverview(selectedCreationRecipe.value.definition).error : '')
const creationUnattendedError = computed(() => creationIsUnattendedRecipe.value && creationPlanSource.value === 'recipe' && !creationUnattendedConfirmed.value
  ? '请明确确认由配方冻结的审核策略继续自动生产。' : '')
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
const bibleImageCandidateStatusLabel = (status?: string) => ({
  available: '待审核', selected: '已批准绑定', rejected: '已拒绝', invalid: '产物无效', legacy_missing: '历史文件缺失',
} as Record<string, string>)[status || ''] || (status ? `未识别候选状态：${status}` : '本项目暂无生成候选')
const bibleImageBindingRoleLabel = (role: BibleImageLatestCandidate['bindingRole']) => ({
  anchor: '当前锚点', reference: '已绑定参考', unbound: '未绑定',
})[role]
const bibleImageCandidateKey = (candidate: BibleImageLatestCandidate) => candidate.artifactId
const openBibleImagePreview = (
  item: BibleItem,
  candidate: BibleImageLatestCandidate,
) => {
  const src = candidate.previewUrl || candidate.downloadUrl
  if (!src) return
  const extension = candidate.mimeType.split('/')[1]?.replace('jpeg', 'jpg') || 'png'
  bibleImagePreview.value = {
    src,
    title: `${item.name} · 参考图候选 #${candidate.variantIndex}`,
    filename: `${candidate.artifactId}.${extension}`,
    downloadUrl: candidate.downloadUrl || candidate.previewUrl || '',
  }
}
const loadBibleImageCandidates = async (projectId = project.value?.id || ''): Promise<BibleImageCandidatesResult | null> => {
  if (!projectId || bibleImageLoading.value) return null
  const isCurrent = currentProjectRead(projectId)
  bibleImageLoading.value = true
  bibleImageLoadError.value = ''
  try {
    const result = await repository.getBibleImageCandidates(projectId)
    if (!isCurrent()) return null
    bibleImageCandidates.value = result
    return result
  } catch (error) {
    if (isCurrent()) {
      bibleImageLoadError.value = bibleImageCandidateErrorMessage(error, '实体图片候选读取失败，请稍后刷新。')
    }
    return null
  } finally {
    if (isCurrent()) bibleImageLoading.value = false
  }
}
const projectTranslationFields = computed<PromptTranslationField[]>(() => {
  if (!project.value) return []
  const fields: PromptTranslationField[] = []
  const add = (field: PromptTranslationField) => { if (field.sourceText.trim()) fields.push(field) }
  if (currentStyle.value?.promptPrefix) add({ fieldKey:'style.visual_prompt', label:'全剧风格视觉提示', sourceText:currentStyle.value.promptPrefix, scopeType:'project', scopeId:project.value.id, impact:'prompt' })
  if (currentStyle.value?.negativePrompt) add({ fieldKey:'style.negative_prompt', label:'风格负面约束', sourceText:currentStyle.value.negativePrompt, scopeType:'project', scopeId:project.value.id, impact:'prompt', help:'是否需要英文由本次策略与模型合同决定；此处仅维护可选译文' })
  add({ fieldKey:'project.production_settings.color_script', label:'全剧色彩脚本', sourceText:project.value.bible.colorScript, scopeType:'project', scopeId:project.value.id, impact:'continuity' })
  add({ fieldKey:'project.production_settings.negative_prompt', label:'全剧负面约束', sourceText:project.value.bible.negativePrompt, scopeType:'project', scopeId:project.value.id, impact:'prompt', help:'是否需要英文由本次策略与模型合同决定；此处仅维护可选译文' })
  add({ fieldKey:'project.continuity_summary', label:'项目连续性摘要', sourceText:project.value.continuitySummary || '', scopeType:'project', scopeId:project.value.id, impact:'continuity', help:'与色彩脚本独立保存；用于跨集身份、服装、空间、道具和光线连续性，请单独审核' })
  return fields
})
const bibleTranslationFields = computed<PromptTranslationField[]>(() => {
  if (!project.value) return []
  const fields: PromptTranslationField[] = []
  const add = (field: PromptTranslationField) => { if (field.sourceText.trim()) fields.push(field) }
  const profile = bibleSection.value === 'characters'
    ? { scopeType:'character' as const, prefix:'人物' }
    : bibleSection.value === 'scenes'
      ? { scopeType:'scene' as const, prefix:'场景' }
      : { scopeType:'prop' as const, prefix:'道具' }
  for (const item of bibleItems.value) {
    const base={scopeType:profile.scopeType,scopeId:item.id,impact:'continuity' as const}
    if(bibleSection.value==='characters'){add({...base,fieldKey:'character.age_appearance',label:`人物 · ${item.name} · 年龄外观`,sourceText:item.ageAppearance||''});add({...base,fieldKey:'character.face_identity',label:`人物 · ${item.name} · 面部身份`,sourceText:item.faceIdentity||''});add({...base,fieldKey:'character.wardrobe',label:`人物 · ${item.name} · 固定服装`,sourceText:item.wardrobe||''});add({...base,fieldKey:'character.voice_profile',label:`人物 · ${item.name} · 声音特征`,sourceText:item.voiceProfile||'',impact:'prompt'});if(!(item.voiceProfile||'').trim())add({...base,fieldKey:'character.personality',label:`人物 · ${item.name} · 说话者人物描述`,sourceText:item.personality||'',impact:'prompt',help:'声音特征为空，当前会作为 H3 对白说话者描述后备；不是管理用基础描述'});add({...base,fieldKey:'character.negative_constraints',label:`人物 · ${item.name} · 负面约束`,sourceText:item.negativeConstraints||'',impact:'prompt'})}
    else if(bibleSection.value==='scenes'){add({...base,fieldKey:'scene.location',label:`场景 · ${item.name} · 地点结构`,sourceText:item.location||''});add({...base,fieldKey:'scene.time_of_day',label:`场景 · ${item.name} · 时间`,sourceText:item.timeOfDay||''});add({...base,fieldKey:'scene.lighting',label:`场景 · ${item.name} · 光线`,sourceText:item.lighting||''});add({...base,fieldKey:'scene.color_palette',label:`场景 · ${item.name} · 色板`,sourceText:item.colorPalette||''});add({...base,fieldKey:'scene.ambient_sound',label:`场景 · ${item.name} · 环境声`,sourceText:item.ambientSound||'',impact:'prompt'});add({...base,fieldKey:'scene.negative_constraints',label:`场景 · ${item.name} · 负面约束`,sourceText:item.negativeConstraints||'',impact:'prompt'})}
    else {add({...base,fieldKey:'prop.material',label:`道具 · ${item.name} · 材质`,sourceText:item.material||''});add({...base,fieldKey:'prop.distinctive_marks',label:`道具 · ${item.name} · 固定标记`,sourceText:item.distinctiveMarks||''});add({...base,fieldKey:'prop.continuity_state',label:`道具 · ${item.name} · 连续性状态`,sourceText:item.continuityState||''});add({...base,fieldKey:'prop.negative_constraints',label:`道具 · ${item.name} · 负面约束`,sourceText:item.negativeConstraints||'',impact:'prompt'})}
  }
  return fields
})
const bibleContinuityEntities = computed(() => {
  if (!project.value) return []
  const statusEntry = (entityType: BibleImageEntityType, typeLabel: string, item: BibleItem) => {
    const persisted = bibleImageCandidates.value?.candidates.find((candidate) => (
      candidate.entityType === entityType && candidate.entityId === item.id
    ))
    return {
      entityType,
      typeLabel,
      item,
      anchorAssetId: persisted ? persisted.anchorAsset?.id || '' : item.anchorAssetId || '',
      referenceAssetCount: persisted ? persisted.referenceAssets.length : (item.referenceAssetIds || []).length,
    }
  }
  return [
    ...project.value.bible.characters.map((item) => statusEntry('character', '角色', item)),
    ...project.value.bible.scenes.map((item) => statusEntry('scene', '场景', item)),
    ...project.value.bible.props.map((item) => statusEntry('prop', '道具', item)),
  ]
})
const bibleMissingAnchorEntities = computed(() => bibleContinuityEntities.value.filter(({ anchorAssetId }) => !anchorAssetId))
const bibleEntitiesWithoutAnyImage = computed(() => bibleMissingAnchorEntities.value.filter(({ referenceAssetCount }) => referenceAssetCount === 0))
const entityReferenceShotCount = computed(() => !videoConditioningCapability.value ? null : project.value?.episodes.reduce((total, episode) => (
  total + episode.shots.filter((shot) => (
    shot.mediaType === 'video'
    && conditioningModeByKey(conditioningModes.value, shot.mode)?.requiresEntityReferenceBible
  )).length
), 0) || 0)
const bibleContinuityReady = computed(() => (
  bibleContinuityEntities.value.length > 0 && bibleMissingAnchorEntities.value.length === 0
))
const bibleMissingAnchorPreview = computed(() => {
  const preview = bibleMissingAnchorEntities.value.slice(0, 6).map(({ typeLabel, item }) => `${typeLabel}“${item.name}”`)
  const remaining = bibleMissingAnchorEntities.value.length - preview.length
  return `${preview.join('、')}${remaining > 0 ? `，另有 ${remaining} 个` : ''}`
})
const bibleSections = [{ value: 'characters' as const, label: '角色圣经', singular: '角色', icon: UserRound }, { value: 'scenes' as const, label: '场景圣经', singular: '场景', icon: Image }, { value: 'props' as const, label: '道具圣经', singular: '道具', icon: Box }]
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const markProjectInputsChangedFromTranslation = () => {
  if (!project.value) return
  for (const episode of project.value.episodes) for (const shot of episode.shots) {
    if (shot.status === 'ready') shot.status = 'draft'
  }
}

const loadProjectRuns = async (projectId: string) => {
  if (projectRunsLoading.value) return
  const isCurrent = currentProjectRead(projectId)
  projectRunsLoading.value = true
  projectRunsError.value = ''
  try {
    const runs = await platformApi.listProductionRuns(projectId)
    if (isCurrent()) productionRuns.value = runs
  } catch (cause) {
    if (isCurrent()) projectRunsError.value = apiErrorSummary(cause, '生产记录读取失败')
  } finally { if (isCurrent()) projectRunsLoading.value = false }
}
const loadBibleAssets = async (projectId: string) => {
  if (bibleAssetsLoading.value) return
  const isCurrent = currentProjectRead(projectId)
  bibleAssetsLoading.value = true; bibleAssetsError.value = ''; bibleAssetsLoaded.value = false
  try { await studio.refreshAssets(); if (isCurrent()) bibleAssetsLoaded.value = true }
  catch (cause) { if (isCurrent()) bibleAssetsError.value = apiErrorSummary(cause, '参考素材读取失败') }
  finally { if (isCurrent()) bibleAssetsLoading.value = false }
}
const loadVideoConditioningCapability = async (projectId: string) => {
  if (videoConditioningCapabilityLoading.value) return
  const isCurrent = currentProjectRead(projectId)
  videoConditioningCapabilityLoading.value = true; videoConditioningCapabilityError.value = ''; videoConditioningCapability.value = null
  try {
    const capability = await repository.getVideoConditioningCapability()
    if (isCurrent()) videoConditioningCapability.value = capability
  } catch (cause) { if (isCurrent()) videoConditioningCapabilityError.value = apiErrorSummary(cause, '参考输入要求读取失败') }
  finally { if (isCurrent()) videoConditioningCapabilityLoading.value = false }
}
const loadVisibleProjectTab = () => {
  if (isNew.value || loading.value || !project.value || !editingInputs.value) return
  const projectId = project.value.id
  const start = (key: string, read: () => Promise<unknown>) => {
    if (detailReadsStarted.has(key)) return
    detailReadsStarted.add(key); void read()
  }
  if (tab.value === 'bible') {
    start('assets', () => loadBibleAssets(projectId))
    start('candidates', () => loadBibleImageCandidates(projectId))
    start('capability', () => loadVideoConditioningCapability(projectId))
  }
}
const creationRequestIsCurrent = (epoch: number, session: number) => isNew.value && epoch === creationPageEpoch && session === studio.captureSessionEpoch()
const loadCreationStyles = async () => {
  if (creationStylesLoading.value) return
  const epoch = creationPageEpoch; const session = studio.captureSessionEpoch()
  const force = Boolean(creationStylesError.value)
  creationStylesLoading.value = true; creationStylesError.value = ''
  try {
    await studio.loadStyles(force)
    if (!creationRequestIsCurrent(epoch, session)) return
    if (!selectableStyles.value.length) creationStylesError.value = '尚无可选视觉风格，请在视觉风格管理中新增或启用。'
  } catch (cause) {
    if (creationRequestIsCurrent(epoch, session)) creationStylesError.value = apiErrorSummary(cause, '视觉风格加载失败')
  } finally { if (creationRequestIsCurrent(epoch, session)) creationStylesLoading.value = false }
}
const loadCreationPolicy = async () => {
  if (creationPolicyLoading.value) return
  const epoch = creationPageEpoch; const session = studio.captureSessionEpoch()
  creationPolicyLoading.value = true; creationPolicyError.value = ''
  try {
    const defaults = await repository.getProjectCreationCapability()
    if (!creationRequestIsCurrent(epoch, session)) return
    creationDefaults.value = defaults
    if (!creationDefaultsApplied) {
      aiEpisodeCount.value ??= defaults.episodeCount
      aiShotsPerEpisode.value ??= defaults.shotsPerEpisode
      aiEpisodeDurationSeconds.value ??= defaults.episodeDurationSeconds
      newProject.value.language ||= defaults.defaultLanguage
      if (!newProject.value.aspectRatio) applyNewProjectAspectRatio(defaults.defaultAspectRatio)
      creationDefaultsApplied = true
    }
  } catch (cause) {
    if (creationRequestIsCurrent(epoch, session)) { creationDefaults.value = null; creationPolicyError.value = apiErrorSummary(cause, '项目规格加载失败') }
  } finally { if (creationRequestIsCurrent(epoch, session)) creationPolicyLoading.value = false }
}
const loadCreationRecipes = async () => {
  if (creationRecipesLoading.value || creationRecipesLoaded) return
  const epoch = creationPageEpoch; const session = studio.captureSessionEpoch()
  creationRecipesLoading.value = true; creationRecipesError.value = ''
  try {
    const rows = await platformApi.listRecipes('published')
    if (!creationRequestIsCurrent(epoch, session)) return
    creationRecipes.value = rows; creationRecipesLoaded = true
  } catch (cause) {
    if (creationRequestIsCurrent(epoch, session)) creationRecipesError.value = apiErrorSummary(cause, '生产配方加载失败')
  } finally { if (creationRequestIsCurrent(epoch, session)) creationRecipesLoading.value = false }
}
const loadProject = async () => {
  if (props.inputProject) { acceptInputProject(props.inputProject); loading.value = false; loadVisibleProjectTab(); return }
  creationPageEpoch += 1
  const epoch = creationPageEpoch, session = studio.captureSessionEpoch(), projectId = String(route.params.projectId || '')
  const isCurrent = () => epoch === creationPageEpoch && session === studio.captureSessionEpoch()
  creationPolicyLoading.value = false; creationStylesLoading.value = false; creationRecipesLoading.value = false
  detailReadsStarted.clear()
  if (isNew.value) {
    project.value = null; loading.value = false
    await Promise.all([loadCreationStyles(), loadCreationPolicy(), ...(creationPlanSource.value === 'recipe' ? [loadCreationRecipes()] : [])])
    return
  }
  loading.value = true
  project.value = null
  projectLoadError.value = ''
  bibleImageCandidates.value = null; bibleImageLoading.value = false
  productionRuns.value = []; projectRunsError.value = ''; projectRunsLoading.value = false
  bibleImageLoadError.value = ''
  videoConditioningCapability.value = null; videoConditioningCapabilityError.value = ''; videoConditioningCapabilityLoading.value = false
  bibleAssetsLoading.value = false; bibleAssetsLoaded.value = false; bibleAssetsError.value = ''
  try {
    // The existing adapter resolves style IDs through this lightweight directory.
    // No task history or full production capability scan is required to read a project.
    await studio.loadStyles()
    if (!isCurrent()) return
    const found = await studio.loadProject(projectId, true)
    if (!isCurrent()) return
    if (!found || found.id !== projectId) throw new Error('项目数据与当前页面不一致，请重新读取。')
    project.value = clone(found)
    void loadProjectRuns(projectId)
  } catch (error) {
    if (!isCurrent()) return
    projectLoadError.value = error instanceof Error ? error.message : '项目加载失败'
  } finally { if (isCurrent()) loading.value = false }
  if (isCurrent()) loadVisibleProjectTab()
}
watch([creationMode, compatibleCreationRecipes], () => {
  if (!compatibleCreationRecipes.value.some((recipe) => recipe.id === creationRecipeId.value)) {
    creationRecipeId.value = compatibleCreationRecipes.value[0]?.id || ''
  }
}, { immediate: true })
watch([selectedCreationRecipe, creationPlanSource], () => { creationUnattendedConfirmed.value = false })
watch(() => selectedCreationRecipe.value?.id, () => {
  aiAllowExternalCalls.value = false
})
const refreshProjectPage = async () => { await loadProject() }
watch([tab, editingInputs], loadVisibleProjectTab)
onMounted(refreshProjectPage)
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
onBeforeUnmount(() => { creationPageEpoch += 1; revokePendingCoverPreview(); emit('contextProtected', false) })
watch(() => route.params.projectId, refreshProjectPage)


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
  if (createdProjectForOnboarding.value) return createdProjectForOnboarding.value
  if (projectCreateUncertain.value) throw new Error('项目创建结果尚未确认，请先到连续剧列表核对，不会自动重复创建。')
  const payload = {
    ...newProject.value,
    title: onboardingProjectTitle(),
    // Script text must enter through ScriptSource -> immutable preview ->
    // confirmed ScriptRevision.  Manual mode intentionally creates an empty
    // project instead of preserving a second, non-runnable master-script path.
    masterScript: '',
  }
  projectCreateUncertain.value = true
  let created: SeriesProject
  try { created = await studio.createProject(payload); projectCreateUncertain.value = false }
  catch (cause) { if (isDefinitiveCreationRejection(cause)) projectCreateUncertain.value = false; throw cause }
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
): Promise<ProductionRunCreateInput> => {
  if (!creationDefaults.value) throw new Error(creationPolicyError.value || '服务端项目创建策略缺失，不能创建生产运行')
  const plan = creationRunPlan.value
  if (!plan) throw new Error('集数、每集镜数和单集时长必须是正整数；请恢复服务端创建策略值或填写有效值。')
  if (!selectedCreationRecipe.value) throw new Error(`当前没有可用于${scriptMode === 'ai' ? 'AI 创建' : '导入剧本'}的已发布配方，请先到系统设置完成配方审核发布`)
  if (creationRecipeContractError.value) throw new Error(creationRecipeContractError.value)
  if (creationUnattendedError.value) throw new Error(creationUnattendedError.value)
  return {
    planSource: 'recipe',
    projectId: created.id,
    scriptMode,
    scriptRevisionId: scriptRevisionId || null,
    recipeVersionId: selectedCreationRecipe.value.id,
    projectSourceConfirmations: projectSourceConfirmations.value,
    idempotencyKey: creationRunKey.value,
    allowExternalCalls: aiAllowExternalCalls.value,
    configOverrides: {},
    idea: aiIdea.value.trim(),
    styleHint: newProjectStyleHint.value,
    episodeCount: plan.episodeCount,
    shotsPerEpisode: plan.shotsPerEpisode,
    episodeDurationSeconds: plan.episodeDurationSeconds,
    structuredImportPlan: scriptMode === 'confirmed_revision' && selectedCreationRecipeSummary.value.hasStructure,
    unattendedConfirmed: creationIsUnattendedRecipe.value ? creationUnattendedConfirmed.value : undefined,
  }
}
const startOnboardingRun = async (created: SeriesProject, revision: ScriptRevision | null = null) => {
  if (!creationRunRequest.value && creationPlanSource.value==='recipe' && !projectSourceReady.value) {
    creationError.value='项目已保存，尚未创建模型任务。请在“本轮项目来源确认”中预览确认，再继续完成创建。'
    return
  }
  if (!creationRunRequest.value) {
    creationRunRequest.value = creationPlanSource.value === 'manual'
      ? onboardingManualInput(await platformApi.getManualProductionOptions(created.id), {
        projectId: created.id, idempotencyKey: creationRunKey.value, revision,
        allowExternalCalls: false,
      })
      : await createRunFromOnboarding(created, creationMode.value === 'ai' ? 'ai' : 'confirmed_revision', revision?.id)
  }
  try {
    const run = await platformApi.createProductionRun(creationRunRequest.value)
    if (run.project !== created.id || run.planSource !== creationRunRequest.value.planSource) throw new Error('创建结果与当前项目或制作方式不一致。')
    await router.replace(`/projects/${created.id}/production/${run.id}`)
  } catch (cause) {
    if (isDefinitiveCreationRejection(cause)) { creationRunRequest.value = null; creationRunKey.value = crypto.randomUUID() }
    throw cause
  }
}
watch(creationPlanSource, value => {
  if (value === 'recipe' && isNew.value) void loadCreationRecipes()
  if (value === 'recipe' && creationMode.value === 'manual') creationMode.value = 'import'
  if (value === 'manual' && creationMode.value === 'ai') creationMode.value = 'manual'
})
const createProject = async () => {
  if (createLoading.value) return
  creationError.value = ''
  if (createdProjectForOnboarding.value && creationRunRequest.value) {
    createLoading.value = true
    try { await startOnboardingRun(createdProjectForOnboarding.value) }
    catch (cause) { creationError.value = apiErrorSummary(cause, '继续创建生产运行失败') }
    finally { createLoading.value = false }
    return
  }
  if (!creationReady.value) { creationError.value = creationPolicyError.value || creationStylesError.value || (creationPlanSource.value === 'recipe' ? creationRecipesError.value : '') || '请等待所需选项加载完成，选择风格；配方制作还需选择兼容的已发布配方。'; return }
  const selectedAspectRatio = creationDefaults.value?.aspectRatioOptions.find((option) => option.value === newProject.value.aspectRatio)
  if (!newProject.value.language.trim() || !selectedAspectRatio || newProject.value.width !== selectedAspectRatio.width || newProject.value.height !== selectedAspectRatio.height) {
    creationError.value = '服务端项目创建策略缺少有效语言、画幅或尺寸事实，不能创建项目。'
    return
  }
  if (creationMode.value === 'manual' && !newProject.value.title.trim()) { creationError.value = '手工创建项目时请先填写项目名称'; return }
  if (creationMode.value === 'import' && importMode.value === 'paste' && !importText.value.trim()) { creationError.value = '请粘贴要导入的完整剧本'; return }
  if (creationMode.value === 'import' && importMode.value === 'file' && !importFile.value) { creationError.value = '请选择要导入的剧本文件'; return }
  if (creationPlanSource.value === 'recipe' && creationMode.value === 'ai' && !aiIdea.value.trim()) { creationError.value = '请填写要由 AI 创作的故事需求'; return }
  if (creationMode.value !== 'manual' && creationPlanSource.value === 'recipe' && !selectedCreationRecipe.value) { creationError.value = '没有兼容的已发布配方，请先到系统设置完成配方配置与发布'; return }
  if (creationRecipeContractError.value) { creationError.value = creationRecipeContractError.value; return }
  if (creationUnattendedError.value) { creationError.value = creationUnattendedError.value; return }
  if (creationPlanSource.value === 'recipe' && !creationRunPlan.value) { creationError.value = '集数、每集镜数和单集时长必须是正整数；请恢复服务端创建策略值或填写有效值。'; return }
  createLoading.value = true
  try {
    const created = await createBaseProject()
    if (creationRunRequest.value) { await startOnboardingRun(created); return }
    if (creationMode.value !== 'import') { await startOnboardingRun(created); return }
    if (importPreview.value) { importPreview.value = await platformApi.getScriptRevision(importPreview.value.id); importReviewOpen.value = true; return }
    if (importCreateUncertain.value) throw new Error('剧本导入结果尚未确认，请进入已创建项目的剧本来源核对，避免重复导入。')
    importCreateUncertain.value = true
    try {
      const result = importMode.value === 'file' && importFile.value
        ? await platformApi.uploadScript(created.id, importFile.value)
        : await platformApi.createPastedScript(created.id, importText.value, importFormat.value)
      importPreview.value = result.revision
      importCreateUncertain.value = false
    } catch (cause) { if (isDefinitiveCreationRejection(cause)) importCreateUncertain.value = false; throw cause }
    importPreview.value = await platformApi.getScriptRevision(importPreview.value.id)
    importReviewOpen.value = true
  } catch (error) {
    creationError.value = apiErrorSummary(error, '创建流程失败')
    if (createdProjectForOnboarding.value) {
      creationError.value = `项目已创建，但后续步骤尚未完成：${creationError.value}`
    }
  } finally { createLoading.value = false }
}
const confirmImportedScriptAndContinue = async () => {
  if (!createdProjectForOnboarding.value || !importPreview.value || createLoading.value) return
  if (creationPlanSource.value === 'recipe' && !creationRunPlan.value) {
    creationError.value = '集数、每集镜数和单集时长必须是正整数；请恢复服务端创建策略值或填写有效值。'
    return
  }
  createLoading.value = true
  creationError.value = ''
  try {
    // Confirmation and ProductionRun creation are two distinct persisted
    // operations.  If confirmation succeeded but run creation was blocked
    // (for example by an expired smoke attestation), keep the confirmed
    // revision and make the retry resume at run creation instead of trying to
    // confirm the immutable revision a second time.
    const confirmed = importPreview.value.status === 'confirmed'
      ? importPreview.value
      : await platformApi.confirmScriptRevision(importPreview.value.id)
    importPreview.value = confirmed
    await startOnboardingRun(createdProjectForOnboarding.value, confirmed)
    importReviewOpen.value = false
  } catch (error) {
    creationError.value = `项目和剧本预览已保留，但确认或启动生产失败：${error instanceof Error ? error.message : '未知错误'}`
  } finally { createLoading.value = false }
}
const save = async (): Promise<SeriesProject | null> => {
  if (!project.value || saving.value || props.inputDisabled) return null
  inputSaveError.value = ''
  saving.value = true
  try {
    project.value = clone(await studio.saveProject(clone(project.value)))
    inputBaseline.value = inputSnapshot()
    emit('saved')
    return project.value
  } catch (error) { inputSaveError.value = apiErrorSummary(error, '保存失败'); studio.notify(inputSaveError.value, 'error'); return null }
  finally { saving.value = false }
}
const bibleItemsForType = (source: SeriesProject, entityType: BibleImageEntityType): BibleItem[] => (
  entityType === 'character' ? source.bible.characters : entityType === 'scene' ? source.bible.scenes : source.bible.props
)
const bibleImagePersistedId = (id: string) => /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(id)
const addBibleItem = async () => {
  if (!project.value || !bibleDraft.value.name.trim()) { studio.notify('请填写实体名称', 'error'); return }
  if (bibleCreatePhase.value !== 'idle') return
  bibleCreateError.value=''
  const name = bibleDraft.value.name.trim()
  const normalizedName=name.normalize('NFKC').trim().toLocaleLowerCase('zh-CN')
  const existingName=[...project.value.bible.characters,...project.value.bible.scenes,...project.value.bible.props].find((entry)=>entry.name.normalize('NFKC').trim().toLocaleLowerCase('zh-CN')===normalizedName)
  if (existingName) { studio.notify(`项目 Bible 已存在“${existingName.name}”，名称在人物、场景和道具之间也必须唯一`, 'error'); return }
  const section = bibleSection.value
  const temporaryId = `${section.slice(0,-1)}-${Date.now()}`
  const [anchorAssetId, ...referenceAssetIds] = bibleDraft.value.referenceAssetIds
  const item: BibleItem = { id: temporaryId, name, description: bibleDraft.value.description, anchorAssetId: anchorAssetId || null, referenceAssetIds, locked: true }
  project.value.bible[section].push(item)
  bibleCreatePhase.value = 'saving'
  bibleEntitySaveNotice.value = null
  const saved = await save()
  if (!saved) {
    const unsavedIndex = project.value?.bible[section].findIndex((entry) => entry.id === temporaryId) ?? -1
    if (unsavedIndex >= 0) project.value?.bible[section].splice(unsavedIndex, 1)
    bibleCreateError.value='实体未登记。保存服务暂不可用或项目事实已变化；手工字段仍保留，请刷新项目后重试。'
    bibleCreatePhase.value = 'idle'
    return
  }
  bibleEntitySaveNotice.value = {kind:'success',message:'实体已保存；共同身份与外观请在“主图 / 变体设定”中登记。保存实体不会创建图片或调用模型。'}
  translationReviewRefreshKey.value += 1
  bibleDraft.value = { name: '', description: '', referenceAssetIds: [] }
  resetBibleDraftForm()
  addBibleOpen.value = false
  bibleCreatePhase.value = 'idle'
}

const removeBibleItem = (id: string) => { if (!project.value) return; project.value.bible[bibleSection.value] = project.value.bible[bibleSection.value].filter((item) => item.id !== id) }
const addEpisode = async () => {
  if (!project.value) return
  const next = project.value.episodes.length + 1; if (!episodeDraft.value.title) episodeDraft.value.title = `第 ${next} 集`
  await studio.addEpisode(project.value, episodeDraft.value); addEpisodeOpen.value = false; episodeDraft.value = { title: '', synopsis: '', script: '' }
  inputBaseline.value = inputSnapshot(); emit('saved')
}
const removeEpisode = async (id: string) => { if (project.value && window.confirm('删除这集及其全部分镜？此操作不会删除共享素材。')) await studio.deleteEpisode(project.value, id) }
const assetThumb = (item: BibleItem) => bibleAssetsLoaded.value ? studio.assets.find((asset) => bibleAssetIds(item).includes(asset.id))?.thumbnail : undefined
const assetName = (assetId:string) => (bibleAssetsLoaded.value ? studio.assets.find((asset)=>asset.id===assetId)?.name : '') || assetId.slice(0,8)
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
  bibleDraft.value={name:'',description:'',referenceAssetIds:[]};resetBibleDraftForm();addBibleOpen.value=true
  bibleAssetCatalogLoading.value=true;bibleAssetCatalogError.value=''
  try{bibleAssetCatalog.value=(await repository.getAssets({})).filter((asset)=>['character','scene','prop','image'].includes(asset.type))}
  catch(error){bibleAssetCatalogError.value=error instanceof Error?error.message:'素材库加载失败'}finally{bibleAssetCatalogLoading.value=false}
}
const closeAddBibleModal=()=>{if(bibleCreatePhase.value!=='idle')return;addBibleOpen.value=false;bibleAssetPreview.value=null}
const selectBibleDraftAsset=(asset:Asset)=>{
  if(!isBibleDraftAssetCompatible(asset)){studio.notify(`“${asset.name}”不可选择：${bibleDraftAssetUnavailableReason(asset)}`, 'info');return}
  if(bibleDraft.value.referenceAssetIds[0]===asset.id)return
  const hasManualFields=[bibleDraft.value.name,bibleDraft.value.description].some((value)=>Boolean(value.trim()))
  if(hasManualFields&&!window.confirm(`切换到“${asset.name}”会用该素材已保存的名称和基础描述替换当前手工字段（不修改主图/变体设定）。是否继续？`))return
  bibleDraft.value={name:asset.name,description:asset.description,referenceAssetIds:[asset.id]}

}
</script>

<template>
  <div v-if="loading && !isNew" class="page-loading"><div class="loader-ring"></div><span>正在加载项目…</span></div>
  <div v-else-if="isNew" class="new-project-page page-stack">
    <input ref="coverInput" type="file" :accept="PROJECT_COVER_ACCEPT" hidden @change="selectProjectCover">
    <button class="back-link" @click="router.push('/projects')"><ArrowLeft :size="15"/>返回连续剧项目</button>
    <section class="new-project-heading"><span class="eyebrow">创建连续剧世界 / CREATE A SERIES UNIVERSE</span><h2>新建连续短剧</h2><p>先选择制作方式，再提供剧本来源。创建后直接进入本轮制作；不重复配置已有运行。</p></section>
    <section class="new-project-layout">
      <div class="panel new-project-form">

        <fieldset class="onboarding-fields" :disabled="createLoading || Boolean(creationRunRequest)">
          <div class="form-section"><div class="section-number">01</div><div class="section-content">
            <h3>选择本轮制作方式</h3>
            <div class="creation-mode-tabs" aria-label="制作方式">
              <button type="button" :disabled="Boolean(createdProjectForOnboarding)" :aria-pressed="creationPlanSource==='recipe'" :class="{active:creationPlanSource==='recipe'}" @click="creationPlanSource='recipe'"><Workflow :size="16"/><strong>按配方生产</strong><small>创建后自动推进；需要输入、审核或处理异常时暂停，确认后继续。</small></button>
              <button type="button" :disabled="Boolean(createdProjectForOnboarding)" :aria-pressed="creationPlanSource==='manual'" :class="{active:creationPlanSource==='manual'}" @click="creationPlanSource='manual'"><Clapperboard :size="16"/><strong>无配方人工制作</strong><small>逐步选择执行实例、生成、采用和审核；也可以在某一步使用 AI。</small></button>
            </div>
            <h3>剧本来源</h3>
            <div class="creation-mode-tabs" aria-label="剧本来源">
              <button v-if="creationPlanSource==='manual'" type="button" :disabled="Boolean(createdProjectForOnboarding)" :aria-pressed="creationMode==='manual'" :class="{active:creationMode==='manual'}" @click="creationMode='manual'"><BookOpen :size="15"/><strong>稍后填写 / 逐步生成</strong><small>先建立人工制作记录，不自动调用模型。</small></button>
              <button type="button" :disabled="Boolean(createdProjectForOnboarding)" :aria-pressed="creationMode==='import'" :class="{active:creationMode==='import'}" @click="creationMode='import'"><FolderInput :size="15"/><strong>导入剧本</strong><small>粘贴或上传，确认正文后进入所选制作方式。</small></button>
              <button v-if="creationPlanSource==='recipe'" type="button" :disabled="Boolean(createdProjectForOnboarding)" :aria-pressed="creationMode==='ai'" :class="{active:creationMode==='ai'}" @click="creationMode='ai'"><Sparkles :size="15"/><strong>配方生成剧本</strong><small>按照配方执行实例生成与审核。</small></button>
            </div>
            <label v-if="creationPlanSource==='recipe'" class="field"><span>兼容的已发布配方 *</span><select v-model="creationRecipeId" :disabled="creationRecipesLoading"><option value="">请选择配方</option><option v-for="recipe in compatibleCreationRecipes" :key="recipe.id" :value="recipe.id">{{ recipe.name }} · v{{ recipe.version }}</option></select><small v-if="selectedCreationRecipe">{{ selectedCreationRecipeSummary.label }}；实例、工作流、提示词策略由配方固定，不必重复选择。</small><small v-if="!creationRecipesLoading&&!creationRecipesError&&!compatibleCreationRecipes.length" class="creation-error">暂无兼容配方，请先发布支持该剧本来源的配方。</small></label>
            <p v-if="creationPlanSource==='recipe' && creationRecipesLoading" role="status">正在加载配方…</p>
            <p v-if="creationPlanSource==='recipe' && creationRecipesError" class="creation-error" role="alert">{{ creationRecipesError }} <button type="button" class="button ghost small" @click="loadCreationRecipes">重新加载配方</button></p>
            <fieldset class="onboarding-fields" :disabled="Boolean(createdProjectForOnboarding)">
              <label class="field"><span>项目名称 {{ creationMode==='manual'?'*':'（可稍后采用模型提议）' }}</span><input v-model="newProject.title" placeholder="例如：听雨不归人"></label>
              <label class="field"><span>一句话梗概</span><textarea v-model="newProject.logline" rows="3" placeholder="描述故事主题与核心冲突"></textarea></label>
              <template v-if="creationMode==='import'">
                <div class="creation-subtabs"><button type="button" :class="{active:importMode==='paste'}" @click="importMode='paste'">粘贴正文</button><button type="button" :class="{active:importMode==='file'}" @click="importMode='file'">上传文件</button></div>
                <label v-if="importMode==='paste'" class="field"><span>待导入剧本 *</span><textarea v-model="importText" rows="9" placeholder="粘贴完整剧本；服务端生成不可变预览，确认后采用。"></textarea><select v-model="importFormat"><option value="plain">纯文本</option><option value="markdown">Markdown</option><option value="fountain">Fountain</option></select></label>
                <label v-else class="field"><span>剧本文件 *</span><input type="file" accept=".txt,.md,.markdown,.fountain,.docx" @change="chooseImportFile"><small>{{ importFile?.name || '支持 TXT、Markdown、Fountain、DOCX' }}</small></label>
              </template>
            </fieldset>
            <template v-if="creationMode==='ai'">
              <label class="field"><span>AI 创作需求 *</span><textarea v-model="aiIdea" rows="5"></textarea></label>
              <div class="form-grid three"><label class="field"><span>集数</span><input v-model.number="aiEpisodeCount" type="number" min="1"></label><label class="field"><span>每集镜数</span><input v-model.number="aiShotsPerEpisode" type="number" min="1"></label><label class="field"><span>单集时长（秒）</span><input v-model.number="aiEpisodeDurationSeconds" type="number" min="1"></label></div>
            </template>
            <section v-if="creationPlanSource==='recipe'" class="onboarding-run-options">
              <strong>资源策略与调用授权</strong>
              <p>预算和调用次数由当前系统资源策略统一管理；创建时只确认是否允许外部调用。</p>
              <ProductionBudgetFields v-model:external="aiAllowExternalCalls" :disabled="createLoading"/>
              <small>点击创建并开始制作，将确认以上本轮设置；本地零费用以执行实例的计价配置为准。</small>
            </section>
            <p v-else class="onboarding-run-options">仅创建项目和人工制作记录，不会自动调用模型。后续在制作台选择执行实例、准备生成时，再核对系统资源提醒与外部调用授权。</p>
            <section v-if="creationPlanSource==='recipe'&&creationIsUnattendedRecipe" class="onboarding-unattended-panel"><label><input v-model="creationUnattendedConfirmed" type="checkbox">我确认由配方冻结的审核策略自动审核和继续；未通过检查时明确阻断。</label><p v-if="creationUnattendedError" class="creation-error">{{ creationUnattendedError }}</p></section>
            <p v-if="creationError" class="creation-error" role="alert">{{ creationError }}</p>
            <p v-if="createdProjectForOnboarding">项目已建立，重试将继续未完成的导入或运行创建，不会重新创建项目。<RouterLink :to="`/projects/${createdProjectForOnboarding.id}`">查看已创建项目</RouterLink></p>
            <p v-if="creationRunRequest">运行提交结果待确认；重试沿用同一份请求和标识，不重复启动。</p>
            <p v-if="projectCreateUncertain || importCreateUncertain" role="alert">创建结果待核对，请先检查已有项目或剧本来源，不能盲目重复创建。</p>
          </div></div>
        </fieldset>
        <fieldset class="onboarding-fields" :disabled="createLoading || Boolean(creationRunRequest)">
        <div
          v-if="creationMode==='import'&&creationPlanSource==='recipe'&&selectedCreationRecipeSummary.hasStructure"
          class="form-section onboarding-structure-plan-section"
        >
          <div class="section-number" title="剧本结构化计划（Script Structure Plan）">结构</div>
          <div class="section-content">
            <h3>冻结导入剧本的结构化时长计划</h3>
            <p>仅含剧本结构化（script_structure）阶段的配方使用这些参数；计划原样冻结，由配方字段映射传入所选实例，不改写原稿时间。</p>
            <div class="form-grid three">
              <label class="field"><span>计划集数</span><input v-model.number="aiEpisodeCount" type="number" min="1"></label>
              <label class="field"><span>计划每集镜数</span><input v-model.number="aiShotsPerEpisode" type="number" min="1"></label>
              <label class="field"><span>计划单集时长（秒）</span><input v-model.number="aiEpisodeDurationSeconds" type="number" min="1"></label>
            </div>
            <small v-if="aiPlannedAverageShotDurationMs">计划平均每镜 {{ aiPlannedAverageShotDurationMs }}ms；仅为本次规划参考；实际返回按所选输出结构及语义映射核验。</small>
          </div>
        </div>
        </fieldset>
        <RecipeExecutionSummary v-if="creationPlanSource==='recipe' && selectedCreationRecipe" :recipe="selectedCreationRecipe"/>
        <fieldset class="onboarding-fields" :disabled="createLoading || Boolean(createdProjectForOnboarding)">
        <div class="form-section"><div class="section-number">02</div><div class="section-content">
          <h3>选择视觉风格 <small>必选</small> <button type="button" class="button ghost small" @click="showStyleManager=true">管理视觉风格</button></h3>
          <VisualStyleManager v-if="showStyleManager" :open="showStyleManager" @close="showStyleManager=false" @changed="stylesChanged"/>
          <p>选择整部剧的视觉方向。自动提示词装配按策略引用风格；人工提交的完整提示词不会因此被静默改写。</p>
          <p v-if="creationStylesLoading" role="status">正在加载可选风格…其他内容可先填写。</p>
          <p v-else-if="creationStylesError" class="creation-error" role="alert">{{ creationStylesError }} <button type="button" class="button ghost small" @click="loadCreationStyles">重新加载风格</button></p>
          <template v-else>
            <div class="creation-style-grid" role="group" aria-label="视觉风格">
              <button v-for="style in selectableStyles" :key="style.id" type="button" :aria-pressed="newProject.styleId===style.id" :class="{active:newProject.styleId===style.id}" @click="newProject.styleId=style.id">
                <strong>{{ style.name }} <Check v-if="newProject.styleId===style.id" :size="16" aria-label="已选择"/></strong>
                <span>{{ style.description || '尚未填写风格说明' }}</span>
              </button>
            </div>
            <div v-if="currentStyle" class="creation-selection-detail"><strong>已选：{{ currentStyle.name }}</strong><details><summary>查看风格提示词与负面约束</summary><p>{{ currentStyle.promptPrefix || '此风格未配置视觉提示词' }}</p><p>负面约束：{{ currentStyle.negativePrompt || '未配置' }}</p></details></div>
            <p v-else class="creation-hint">请选择一种风格，平台不会替你默认选择。</p>
          </template>
        </div></div>
        <div class="form-section"><div class="section-number">03</div><div class="section-content">
          <h3>画幅与目标尺寸</h3><p>画幅用于确定本项目的目标宽高。以下选项来自平台配置，不代表每个模型都支持相同的输出尺寸。</p>
          <p v-if="creationPolicyLoading" role="status">正在加载项目规格…其他内容可先填写。</p>
          <p v-else-if="creationPolicyError" class="creation-error" role="alert">{{ creationPolicyError }} <button type="button" class="button ghost small" @click="loadCreationPolicy">重新加载规格</button></p>
          <template v-else-if="creationDefaults">
            <div class="creation-aspect-grid" role="group" aria-label="画幅比例">
              <button v-for="option in newProjectAspectRatioOptions" :key="option.value" type="button" :aria-pressed="newProject.aspectRatio===option.value" :class="{active:newProject.aspectRatio===option.value}" @click="applyNewProjectAspectRatio(option.value)">
                <span class="aspect-shape"><i :style="{aspectRatio:String(option.width/option.height), width:option.width>=option.height?'36px':'auto',height:option.width<option.height?'36px':'auto'}"></i></span>
                <span><strong>{{ option.label }}</strong><small>{{ option.width }} × {{ option.height }} px</small></span>
                <Check v-if="newProject.aspectRatio===option.value" :size="16"/>
              </button>
            </div>
            <div class="creation-selection-detail"><strong>项目目标尺寸：{{ newProjectDimensions }}</strong><span>项目语言：{{ newProject.language }}</span></div>
          </template>
          <p>生成时的实际尺寸、帧率和输入素材由执行实例合同决定；有配方时沿用配方实例，无配方时在生成步骤选择。规格不兼容时会明确提示，不会自动换模型或降档。</p>
        </div></div>
        <div class="form-section"><div class="section-number">04</div><div class="section-content"><h3>连续剧封面</h3><p>推荐 2:3 竖版图；支持 JPEG、PNG、WebP，最大 10 MB。封面只用于项目识别，不进入生成工作流。</p><div class="cover-picker"><button type="button" class="cover-picker-preview" :disabled="createLoading" :style="{background:currentStyle?.gradient}" :aria-label="pendingCoverPreview?'更换待上传的连续剧封面':'选择连续剧封面'" @click="chooseProjectCover"><img v-if="pendingCoverPreview" :src="pendingCoverPreview" alt="待上传的连续剧封面预览"><template v-else><ImagePlus :size="25"/><span>选择封面图片</span></template></button><div><strong>{{ pendingCoverFile?.name || '尚未选择封面' }}</strong><small>项目会先创建，再上传并绑定封面；封面失败不会造成重复项目。</small><div class="cover-picker-actions"><button type="button" class="button ghost small" :disabled="createLoading" @click="chooseProjectCover"><Upload :size="13"/>{{ pendingCoverPreview?'更换封面':'选择封面' }}</button><button v-if="pendingCoverPreview" type="button" class="button ghost small" :disabled="createLoading" @click="removeProjectCover"><Trash2 :size="13"/>移除</button></div></div></div><p v-if="coverError" class="cover-error" role="alert">{{ coverError }}</p></div></div>
        </fieldset>
        <RecipeProjectSourceConfirmations v-if="creationPlanSource==='recipe'&&selectedCreationRecipe" v-model="projectSourceConfirmations" :recipe="selectedCreationRecipe" :project-id="createdProjectForOnboarding?.id" :disabled="createLoading||Boolean(creationRunRequest)" @ready="projectSourceReady=$event"/>
        <div class="new-form-footer"><span><Lock :size="14"/>项目事实保存在平台数据库；外部调用须明确授权，超额等待人工确认</span><button class="button primary" :disabled="createLoading || (!creationRunRequest && !creationReady)" @click="createProject">{{ createLoading?'正在执行…':createdProjectForOnboarding?'继续完成创建':creationMode==='import'?'预览剧本并继续创建':creationPlanSource==='recipe'?'创建并启动配方生产':'创建并进入人工制作' }}<ArrowRight :size="17"/></button></div>
      </div>
      <aside class="new-project-preview panel" :style="{background:currentStyle?.gradient}"><img v-if="pendingCoverPreview" class="new-cover-preview-image" :src="pendingCoverPreview" alt="待上传的连续剧封面实时预览"><template v-else><div class="preview-pattern"></div><span class="eyebrow">项目实时预览 / LIVE PROJECT PREVIEW</span><div class="preview-center"><div class="preview-clap"><i></i><Clapperboard :size="36"/></div><small>{{ currentStyle?.name || '尚未选择风格' }}</small><h3>{{ onboardingProjectTitle() || '未命名连续剧' }}</h3><p>{{ newProject.logline || '你的故事将从这里开始。' }}</p></div><div class="preview-spec"><span>{{ newProject.aspectRatio }}</span><span>{{ newProjectDimensions }}</span></div></template></aside>
    </section>
    <BaseModal :open="importReviewOpen" title="核对服务端提取的剧本正文" eyebrow="剧本版本预览（Script Revision Preview）" width="1040px" :dismissible="!createLoading" @close="importReviewOpen=false">
      <div class="import-review"><div class="import-review-evidence"><Lock :size="15"/><span><strong>项目已创建，原始文件与正文摘要已记录</strong><small>只有确认这个不可变预览版本（preview revision）后，生产运行（ProductionRun）才能拆解分集与分镜；取消不会删除项目或伪装完成。</small></span></div><label class="field"><span>服务端提取正文（只读）</span><textarea :value="importPreview?.content || ''" rows="24" readonly></textarea><small>{{ importPreview?.contentSizeBytes?.toLocaleString() || 0 }} 字节（bytes）· SHA-256 {{ importPreview?.contentSha256?.slice(0,16) }}…</small></label><p v-if="creationError" class="creation-error" role="alert"><AlertTriangle :size="13"/>{{ creationError }}</p></div>
      <template #footer><button class="button ghost" :disabled="createLoading" @click="importReviewOpen=false">稍后处理</button><button class="button primary" :disabled="createLoading||!importPreview" @click="confirmImportedScriptAndContinue"><Check :size="15"/>{{ createLoading?'正在确认…':creationPlanSource==='recipe'?'确认剧本并启动配方生产':'确认剧本并进入人工制作' }}</button></template>
    </BaseModal>
  </div>


  <div v-else-if="project" class="series-page page-stack unified-project-studio" :class="{'embedded-project-inputs':embedded}">
    <input ref="coverInput" type="file" :accept="PROJECT_COVER_ACCEPT" hidden @change="selectProjectCover">
    <section v-if="!embedded && !(typeof route.query.run==='string' && route.query.run)" class="series-header" :style="{background:currentStyle?.gradient}"><ProjectCoverImage v-if="project.coverUrl" class="series-cover-background" :src="project.coverUrl" :alt="`《${project.title}》连续剧封面`" :refresh="refreshCurrentProjectCover" eager/><div class="series-pattern"></div><div class="series-header-main"><button class="back-link light" @click="router.push('/projects')"><ArrowLeft :size="14"/>全部项目</button><div class="series-title"><div class="series-poster"><ProjectCoverImage v-if="project.coverUrl" :src="project.coverUrl" alt="" :refresh="refreshCurrentProjectCover" eager/><template v-else><Clapperboard :size="30"/><span>{{ currentStyle?.name }}</span></template></div><div><div class="series-status"><StatusBadge :status="project.status" title="项目当前编辑状态，不代表所选制作版本的运行结果"/><span>{{ currentStyle?.englishName }}</span></div><h2>{{ project.title }}</h2><p>{{ project.logline }}</p><div class="series-facts"><span>{{ project.aspectRatio }}</span><i></i><span>{{ project.resolution }}</span><i></i><span>视频 · 图片</span><i></i><span>{{ project.episodes.length }} 集当前输入</span></div></div></div></div><div class="series-header-actions"><button type="button" class="button glass" :disabled="coverSaving" @click="chooseProjectCover"><Upload :size="16"/>{{ coverSaving?'封面处理中…':project.coverAssetId?'更换封面':'上传封面' }}</button><button v-if="project.coverAssetId" type="button" class="button glass" :disabled="coverSaving" aria-label="移除连续剧封面" @click="removeProjectCover"><Trash2 :size="16"/>移除封面</button><button type="button" class="button glass" @click="openProjectInputs()"><Layers3 :size="16"/>生产工作台</button><RouterLink :to="`/projects/${project.id}/production?view=new`" class="button glass"><Plus :size="16"/>新建制作版本</RouterLink><p v-if="coverError" class="cover-error header-cover-error" role="alert">{{ coverError }}</p></div></section>
    <ProjectVersionWorkspace v-if="!embedded" :project-id="project.id" :title="project.title" :runs="productionRuns" :loading="projectRunsLoading" :error="projectRunsError" :gradient="currentStyle?.gradient" :cover-url="project.coverUrl" @retry="loadProjectRuns(project.id)"/>
    <section v-if="embedded" v-show="editingInputs" class="project-input-editor" aria-label="项目当前创作输入">
      <fieldset class="project-input-fields" :disabled="inputDisabled||saving">
      <header class="panel project-editor-heading">
        <div><span class="eyebrow">手动创作 / PROJECT INPUTS</span><h3>编辑项目当前输入</h3><p>项目当前可编辑输入；保存不会自动采用、审核或调用模型，也不会改写本版本已保存的输入和成果。</p></div>
        <button v-if="inputDirty" type="button" class="button ghost" :disabled="saving" @click="discardInputChanges">放弃未保存编辑</button>
        <button type="button" class="button primary" :disabled="saving" @click="save()"><Save :size="16"/>{{ saving?'保存中…':'保存项目输入' }}</button>
      </header>
      <p v-if="inputSaveError" class="platform-alert danger" role="alert">{{inputSaveError}}；未保存的内容已保留，请核对后重试。</p>

    <template v-if="tab==='overview'">
      <section class="panel global-rules">
        <header><div><span class="eyebrow">全剧视觉规则 / GLOBAL VISUAL RULES</span><h3>全剧视觉规则</h3><p>在这里维护项目当前规则；下游是否读取以及读取哪个版本，由执行实例与输入来源配置决定。</p></div></header>
        <ProjectFieldLineage :run="inputRun" :project="project" :saved-project="inputProject"/>
        <div class="form-grid two">
          <label class="field"><span class="impact-label-row">全局负面提示词 <PromptImpactBadge kind="audit" title="由提示词策略决定是否输出负面通道，工作流合同决定是否接收；未接收时不作为独立模型输入。"/></span><textarea v-model="project.bible.negativePrompt" rows="5" placeholder="请用英文填写排除项，例如：identity drift; wardrobe color changes; extra limbs; watermark; flicker"></textarea><small>只写要排除的缺陷，无需排除时可留空。AI 候选可合并模型配置的建议缺陷清单，不自动带入项目原有排除项；人工审核可以调整，移除建议项只记录告警。</small></label>
          <label class="field"><span class="impact-label-row">色彩脚本 <small class="field-binding-note">按配置读取</small><PromptImpactBadge kind="continuity"/></span><textarea v-model="project.bible.colorScript" rows="5" placeholder="请用英文填写，例如：EP01–03 use a cool cyan-gray base…"></textarea><small>只写跨集色板、主辅光、环境阶段和转折；不要重复人物外观、空间规则或具体动作。</small></label>
          <label class="field"><span class="impact-label-row">连续性摘要 <small class="field-binding-note">按配置读取</small><PromptImpactBadge kind="continuity"/></span><textarea v-model="project.continuitySummary" rows="5" placeholder="请用英文填写，例如：Preserve stable character identity, wardrobe and spatial continuity…"></textarea><small>与色彩脚本独立保存。写人物身份、固定服装、空间结构、道具形态、天气和光线方向等不可变化规则。</small></label>
        </div>
        <div class="bible-image-prompt-config"><span><strong>实体参考图提示词由 PromptProfile 唯一装配</strong><small>ProductionRun 会冻结具体 Profile 版本、字段来源和哈希；项目页不再维护第二套硬编码提示词。</small></span><RouterLink class="button ghost small" to="/settings?section=prompts">管理 PromptProfile</RouterLink></div>
        <ProjectOriginalNames :project="project" @manage="openVariantSettings"/>
        <details class="optional-translations" :open="proseTranslationOpen" @toggle="toggleProseTranslation">
          <summary>正文翻译候选（按需使用）</summary>
          <template v-if="proseTranslationLoaded">
            <p>仅提供描述正文的翻译候选，不维护名称译表。确认译文不会改写原文，也不代表已被本轮生产采用。</p>
            <TranslationReviewPanel :project-id="project.id" :fields="projectTranslationFields" :refresh-key="translationReviewRefreshKey" compact title="正文翻译 · 候选与审核" @changed="markProjectInputsChangedFromTranslation"/>
          </template>
        </details>
      </section>
    </template>

    <template v-else-if="tab==='bible'">
      <section class="bible-layout">
        <aside class="panel bible-nav"><div><span class="eyebrow">一致性设定集 / CONSISTENCY BIBLE</span><h3>制作圣经</h3><p>被分镜选中的实体会提供文字锚点；参考图只有在对应分镜绑定后才进入工作流。卡片“管理锁定”和随机种子（Seed）目前仅为管理元数据，不改变真实任务的 Seed。</p></div><button v-for="section in bibleSections" :key="section.value" :class="{active:bibleSection===section.value}" @click="bibleSection=section.value"><component :is="section.icon" :size="17"/><span>{{ section.label }}</span><strong>{{ project.bible[section.value].length }}</strong></button><div class="bible-tip"><Sparkles :size="16"/><p>推荐至少建立 2 位主要角色、2 个核心场景和贯穿剧情的关键道具。</p></div></aside>
        <div class="bible-main">
          <header class="section-heading"><div><span class="eyebrow">{{ bibleSections.find(s=>s.value===bibleSection)?.label.toUpperCase() }}</span><h3>{{ bibleSections.find(s=>s.value===bibleSection)?.label }}</h3><p>先登记人物、场景或道具，再在“主图 / 变体设定”中维护共同身份和外观；不要求先有图片，保存不会调用模型。</p></div><button class="button primary" :disabled="bibleCreatePhase!=='idle'" @click="openAddBibleModal"><Plus :size="16"/>新增{{ bibleSections.find(s=>s.value===bibleSection)?.singular }}</button></header>
          <p v-if="videoConditioningCapabilityLoading" role="status">正在读取参考输入要求，暂不判断需要实体参考的镜头数量；不影响编辑 Bible。</p>
          <p v-else-if="videoConditioningCapabilityError" class="platform-alert danger" role="alert">{{ videoConditioningCapabilityError }} <button type="button" class="button ghost small" @click="loadVideoConditioningCapability(project.id)">重试读取参考要求</button></p>
          <p v-if="bibleAssetsLoading" role="status">正在读取参考素材名称与预览…</p>
          <p v-else-if="bibleAssetsError" class="platform-alert danger" role="alert">{{ bibleAssetsError }} <button type="button" class="button ghost small" @click="loadBibleAssets(project.id)">重试读取参考素材</button></p>
          <section class="bible-continuity-readiness" :class="{blocked:!bibleContinuityReady,ready:bibleContinuityReady}" aria-live="polite">
            <AlertTriangle v-if="!bibleContinuityReady" :size="18"/>
            <Check v-else :size="18"/>
            <div class="bible-continuity-readiness-copy">
              <header><strong>跨镜头一致性就绪状态</strong><b>{{ bibleContinuityReady?'实体参考图已就绪':'实体参考图未就绪 · 阻断' }}</b></header>
              <template v-if="bibleContinuityReady">
                <p>{{ bibleContinuityEntities.length }}/{{ bibleContinuityEntities.length }} 个角色、场景和道具均已确定唯一身份锚点。还需在生产运行（ProductionRun）的分镜审核中确认相关实体图已实际绑定到各镜头。</p>
              </template>
              <template v-else-if="bibleContinuityEntities.length">
                <p><strong>{{ bibleMissingAnchorEntities.length }}/{{ bibleContinuityEntities.length }} 个实体缺少身份锚点：</strong>{{ bibleMissingAnchorPreview }}。其中 {{ bibleEntitiesWithoutAnyImage.length }} 个连补充参考图也没有。</p>
                <p v-if="entityReferenceShotCount"><strong>当前已有 {{ entityReferenceShotCount }} 个由所选条件模式声明必须使用实体参考 Bible 的视频镜头：</strong>文字规则、相同随机种子（Seed）与其他动态输入都不能替代实体参考图；未补齐前不能视为跨镜头一致性就绪。</p>
                <p v-else>文字 Bible、相同随机种子（Seed）和视频提示词都不能替代人物、场景、道具的实体参考图。</p>
              </template>
              <template v-else>
                <p><strong>尚未建立角色、场景或道具 Bible。</strong><template v-if="entityReferenceShotCount"> 当前已有 {{ entityReferenceShotCount }} 个由所选条件模式声明必须使用实体参考 Bible 的视频镜头，但其他动态输入不能承担实体身份锚定。</template> 仅靠文字规则或相同随机种子（Seed）无法稳定保持多镜头一致性。</p>
              </template>
              <small v-if="!bibleContinuityReady">请进入生产运行（ProductionRun），使用本次 Bible 选择的模型生成或重新生成候选，也可从素材库选择，然后人工审核并确定每个实体的唯一身份锚点。</small>
            </div>
            <RouterLink class="button small" :class="bibleContinuityReady?'ghost':'primary'" :to="projectProductionTarget"><Workflow :size="14"/>{{ bibleContinuityReady?'核对分镜绑定':'前往 ProductionRun 补齐参考图' }}</RouterLink>
          </section>
          <div v-if="bibleEntitySaveNotice" class="canonical-notice bible-canonical-notice visual-translation-notice" :class="bibleEntitySaveNotice.kind"><strong>实体已登记</strong><span>{{ bibleEntitySaveNotice.message }}</span></div>
          <PromptWritingGuide scope="bible"/>
          <div v-if="bibleImageLoadError" class="bible-image-global-state error" role="alert"><AlertTriangle :size="14"/><span>{{ bibleImageLoadError }}</span><button type="button" :disabled="bibleImageLoading" @click="loadBibleImageCandidates(project.id)"><RefreshCw :size="13"/>重试</button></div>
          <div v-else class="bible-image-global-state"><ImagePlus :size="14"/><span>这里仅展示 ProductionRun 已生成的实体参考图候选历史；新生成与选择必须从对应 ProductionRun 操作。</span><button type="button" :disabled="bibleImageLoading" @click="loadBibleImageCandidates(project.id)"><RefreshCw :size="13" :class="{spinning:bibleImageLoading}"/>刷新候选状态</button></div>
          <TranslationReviewPanel :project-id="project.id" :fields="bibleTranslationFields" :refresh-key="translationReviewRefreshKey" compact :title="`${bibleSections.find(s=>s.value===bibleSection)?.label} · 英文候选人工审核`" @changed="markProjectInputsChangedFromTranslation"/>
          <div class="bible-card-list">
            <article v-for="item in bibleItems" :key="item.id" class="panel bible-card">
              <div class="bible-ref" :class="{placeholder:!assetThumb(item)}"><AssetImage v-if="assetThumb(item)" :src="assetThumb(item)" :label="item.name" :accent="currentStyle?.accent"/><component v-else :is="bibleSections.find(s=>s.value===bibleSection)?.icon" :size="30"/><span title="参考图（Reference）">参考</span></div>
              <div class="bible-fields"><label class="bible-impact-label">名称 <PromptImpactBadge kind="label" title="用于识别和任务展示。"/><PromptImpactBadge kind="indirect" title="保留原名称；身份由实体、设定版本和实际输入位置关联，不按译名猜测。"/></label><div class="bible-card-title"><input v-model="item.name" aria-label="名称"><span title="当前为工作室管理元数据；真实工作流使用项目连续性种子（continuity seed）。">管理种子（SEED） {{ item.seed }} · 不注入任务</span></div><p class="bible-original-name-note">直接使用原名称，无需补英文名。主图、变体与参考素材按明确关联使用。</p><label class="bible-impact-label">基础描述 <PromptImpactBadge kind="indirect" title="供创作和管理阅读；不作为主图身份或外观设定。"/></label><textarea v-model="item.description" rows="2" aria-label="基础描述" placeholder="供制片人员阅读；共同身份与外观请打开主图 / 变体设定"></textarea><p>共同身份和外观只在“主图 / 变体设定”中维护；此处保存不会覆盖设定修订。</p><details class="bible-advanced-editor"><summary>更多直接影响提示词的结构化字段 <PromptImpactBadge kind="prompt"/><PromptImpactBadge kind="continuity"/></summary><div v-if="bibleSection==='characters'" class="form-grid two"><label class="field"><span>年龄外观</span><textarea v-model="item.ageAppearance" rows="2" placeholder="请用英文填写年龄外观、体型和整体气质，例如：Young adult, lean build…"></textarea></label><label class="field"><span>面部身份</span><textarea v-model="item.faceIdentity" rows="2" placeholder="请用英文填写不可变化的面部结构和身份标记。"></textarea></label><label class="field"><span>固定服装</span><textarea v-model="item.wardrobe" rows="2" placeholder="请用英文填写固定服装颜色、版型和材质。"></textarea></label><label class="field"><span>声音特征</span><textarea v-model="item.voiceProfile" rows="2" placeholder="请用英文填写声音音色、语速和稳定表达特征。"></textarea></label><label class="field"><span>说话者人物描述（声音特征为空时后备）</span><textarea v-model="item.personality" rows="2" placeholder="请用英文填写说话者性格与稳定对白表现；不要复制管理说明。"></textarea></label></div><div v-else-if="bibleSection==='scenes'" class="form-grid two"><label class="field"><span>地点结构</span><textarea v-model="item.location" rows="2"></textarea></label><label class="field"><span>时间</span><textarea v-model="item.timeOfDay" rows="2"></textarea></label><label class="field"><span>光线</span><textarea v-model="item.lighting" rows="2"></textarea></label><label class="field"><span>色板</span><textarea v-model="item.colorPalette" rows="2"></textarea></label><label class="field"><span>环境声</span><textarea v-model="item.ambientSound" rows="2"></textarea></label></div><div v-else class="form-grid two"><label class="field"><span>材质</span><textarea v-model="item.material" rows="2"></textarea></label><label class="field"><span>固定标记</span><textarea v-model="item.distinctiveMarks" rows="2"></textarea></label><label class="field"><span>连续性状态</span><textarea v-model="item.continuityState" rows="2"></textarea></label></div><p>以上字段会直接进入导演提示词或连续性定义；中文内容需在上方“英文候选人工审核”确认后才会替换为英文。实体名称与基础描述不在此列。</p></details><label class="bible-impact-label">参考图 <PromptImpactBadge kind="continuity" title="身份锚点与补充参考图独立保存；仍需在具体分镜绑定后才会连接工作流。"/><PromptImpactBadge kind="production"/></label><div class="reference-chips"><span v-if="item.anchorAssetId" class="anchor-reference">身份锚点：{{ assetName(item.anchorAssetId) }} <button title="解除身份锚点并保留为普通参考图；不会删除素材或候选历史。" @click="setBibleAnchor(item,null)"><Trash2 :size="11"/></button></span><span v-for="assetId in item.referenceAssetIds" :key="assetId">{{ assetName(assetId) }} <button title="设为身份锚点；只影响当前一致性设定（Bible）绑定。" @click="setBibleAnchor(item,assetId)"><Lock :size="11"/></button><button title="移除普通参考图绑定；不会删除素材或候选历史。" @click="item.referenceAssetIds=item.referenceAssetIds.filter(id=>id!==assetId)"><Trash2 :size="11"/></button></span><button @click="bibleAssetPickerId=bibleAssetPickerId===item.id?'':item.id"><Plus :size="12"/>{{ bibleAssetPickerId===item.id?'收起素材库':'绑定参考图' }}</button></div><div v-if="bibleAssetPickerId===item.id" class="bible-inline-assets"><header><span>选择{{ bibleSections.find(s=>s.value===bibleSection)?.singular }}素材</span><small>只显示同分类授权素材；加入素材池不代表每个工作流都会自动连接；删除绑定不会删除候选历史。</small></header><div><button v-for="asset in (bibleAssetsLoaded ? studio.assets : []).filter(a=>a.type===assetTypeForBible())" :key="asset.id" :class="{active:bibleAssetIds(item).includes(asset.id)}" @click="toggleBibleAsset(item,asset.id)"><AssetImage :src="asset.thumbnail" :label="asset.name" :accent="currentStyle?.accent"/><span>{{ asset.name }}</span><i><Check :size="11"/></i></button></div><button class="button primary small" @click="saveBibleAssets(item,assetTypeForBible())"><Save :size="13"/>保存绑定</button></div></div>
              <section class="bible-image-candidate">
                <p v-if="bibleAssetIds(item).length">当前已绑定 {{ bibleAssetIds(item).length }} 张参考图；来源：{{ bibleImageLoadError || bibleImageLoading || !bibleImageCandidateFor(item) ? '待核对候选历史' : bibleImageCandidateFor(item)?.candidateHistory.some(row => row.bindingRole!=='unbound') ? '包含本项目生成候选' : '素材库已有素材' }}。绑定状态与下方生成历史分别记录。</p>
                <p v-else>当前尚未绑定参考图；是否必需由执行实例的素材合同决定。</p>
                <header><span><ImagePlus :size="15"/><strong>实体参考图候选</strong><small>只读历史；请从 ProductionRun 生成、审核和选择正式产物</small></span></header>
                <p v-if="bibleImageLoading" role="status">正在读取生成候选历史…</p>
                <p v-else-if="bibleImageLoadError" role="alert">候选历史读取失败，不能判断是否已有候选。请在上方重试。</p>
                <div v-else-if="bibleImageCandidateFor(item)" class="bible-image-status" :class="{stale:!bibleImageCandidateFor(item)?.latestCandidate?.sourceCurrent}">
                  <div><strong>冻结目标：{{ bibleSections.find(s=>s.value===bibleSection)?.singular }} · {{ bibleImageCandidateFor(item)?.entityName }}</strong><code>{{ bibleImageCandidateFor(item)?.entityId }}</code><small v-if="bibleImageCandidateFor(item)?.latestCandidate">目标基线 {{ bibleImageCandidateFor(item)?.latestCandidate?.targetSnapshotSha256.slice(0,12) }}…</small></div>
                  <span>{{ bibleImageCandidateStatusLabel(bibleImageCandidateFor(item)?.latestCandidate?.status) }}</span>
                  <p v-if="bibleImageCandidateFor(item)?.latestCandidate&&!bibleImageCandidateFor(item)?.latestCandidate?.sourceCurrent"><AlertTriangle :size="13"/>Bible 字段、译文或视觉规则已变化；此处仅保留旧来源的历史投影，不提供选用入口。</p>
                  <div v-if="bibleImageCandidateFor(item)?.candidateHistory.length" class="bible-image-history">
                    <article v-for="(candidate,candidateIndex) in bibleImageCandidateFor(item)?.candidateHistory || []" :key="bibleImageCandidateKey(candidate)" :class="{stale:!candidate.sourceCurrent||candidate.integrityStatus==='invalid'}">
                      <header><span><strong>{{ candidateIndex===0?'最新候选':'历史候选' }} · 生成候选序号 {{ candidate.variantIndex }}</strong><small>{{ bibleImageCandidateStatusLabel(candidate.status) }} · {{ bibleImageBindingRoleLabel(candidate.bindingRole) }} · {{ new Date(candidate.createdAt).toLocaleString('zh-CN') }}</small></span><RouterLink :to="`/projects/${project.id}/production/${candidate.productionRunId}`" class="button ghost small"><Workflow :size="13"/>打开 ProductionRun</RouterLink></header>
                      <p v-if="candidate.integrityStatus==='invalid'"><AlertTriangle :size="13"/>该候选的正式来源链校验失败（{{ candidate.integrityErrorCode }}），仅保留损坏历史，不可用于生产。</p>
                      <p v-else-if="!candidate.sourceCurrent"><AlertTriangle :size="13"/>该候选对应旧版 Bible、译文或视觉规则，仅作为历史记录展示。</p>
                      <div class="bible-image-outputs"><article><button v-if="candidate.previewUrl||candidate.downloadUrl" type="button" class="bible-image-preview-button" @click="openBibleImagePreview(item,candidate)"><img :src="candidate.previewUrl||candidate.downloadUrl" :alt="`${item.name} 参考图候选 ${candidate.variantIndex}`"><span><Eye :size="12"/>预览全图</span></button><div v-else><Image :size="24"/><span>预览地址暂不可用</span></div><span>{{ candidate.executionInstanceName }} · 准备 {{ candidate.preparationId.slice(0,8) }} · {{ candidate.modelIdentifier || '模型标识未记录' }} · {{ candidate.mimeType }} · {{ Math.ceil(candidate.sizeBytes/1024) }} KB</span></article></div>
                      <p v-if="candidate.materializedAsset" class="bible-image-bound-asset">已物化为所有者素材：{{ candidate.materializedAsset.name }}（{{ candidate.bindingRole==='unbound'?'当前未绑定':bibleImageBindingRoleLabel(candidate.bindingRole) }}）</p>
                    </article>
                  </div>
                  <div class="bible-image-task-actions"><button type="button" class="button ghost small" :disabled="bibleImageLoading" @click="loadBibleImageCandidates(project.id)"><RefreshCw :size="13"/>刷新状态</button></div>
                </div>
                <p v-else class="bible-image-empty">尚无由生产运行（ProductionRun）归档的历史候选。</p>
              </section>
              <div class="bible-card-actions"><button type="button" class="button ghost small" @click="openVariantSettings(item)"><Layers3 :size="15"/>管理主图与变体</button><button class="lock-button" :class="{locked:item.locked}" title="当前仅记录工作室管理状态，不改变提示词、参考连接或真实任务 Seed。" @click="item.locked=!item.locked"><component :is="item.locked?Lock:Unlock" :size="15"/>{{ item.locked?'管理标记：锁定':'管理标记：未锁定' }}</button><small class="management-only-note">仅管理元数据</small><button class="icon-btn danger" @click="removeBibleItem(item.id)"><Trash2 :size="15"/></button></div>
            </article>
            <button v-if="!bibleItems.length" class="panel empty-bible" @click="openAddBibleModal"><component :is="bibleSections.find(s=>s.value===bibleSection)?.icon" :size="30"/><strong>还没有{{ bibleSections.find(s=>s.value===bibleSection)?.singular }}设定</strong><span>可仅填写名称登记实体，也可选择素材库参考图；身份与外观在主图设定中另行维护。</span><i><Plus :size="14"/>立即新增</i></button>
          </div>
        </div>
      </section>
    </template>

    <template v-else-if="tab==='episodes'">
      <section class="episode-section">
        <header class="section-heading"><div><span class="eyebrow">剧集与分镜画布 / UNLIMITED EPISODE CANVAS</span><h3>剧集与分镜</h3><p>所有新剧集共享本项目的一致性设定集（Bible）；实际生成由每镜使用的执行实例确定。</p></div><button class="button primary" @click="addEpisodeOpen=true"><Plus :size="16"/>新增下一集</button></header>
        <div class="episode-grid">
          <article v-for="episode in project.episodes" :key="episode.id" class="panel episode-card">
            <div class="episode-number"><span>剧集（Episode）</span><strong>{{ String(episode.number).padStart(2,'0') }}</strong></div>
            <div class="episode-copy"><div><StatusBadge :status="episode.status" :label="episode.status==='completed' ? '已装配' : undefined" title="剧集装配状态；最终验收与封存请查看对应生产运行。"/><span>{{ episodeProductionLabel(episode) }}</span></div><h3>{{ episode.title }}</h3><p>{{ episode.synopsis || '尚未填写本集梗概，进入分镜编辑器开始创作。' }}</p><div class="episode-tags"><span><Film :size="13"/>{{ episodeVideoCount(episode) }} 视频</span><span><Image :size="13"/>{{ episodeImageCount(episode) }} 图片</span><span><Clock3 :size="13"/>{{ formatNarrativeDuration(episodeNarrativeDurationSeconds(episode)) }} 秒叙事时长</span><span v-if="episodeVideoCount(episode)">{{ episode.audioEnabled?'原生音频':'静音' }}</span></div></div>
            <div class="episode-card-footer manual-episode-actions"><RouterLink :to="{path:`/projects/${project.id}/episodes/${episode.id}/storyboard`,query:selectedProjectRunId?{run:selectedProjectRunId}:{}}" class="button ghost full">打开分镜编辑器 <ArrowRight :size="15"/></RouterLink><button class="icon-btn" :aria-label="`删除第 ${episode.number} 集`" @click="removeEpisode(episode.id)"><Trash2 :size="15"/></button></div>
          </article>
          <button class="episode-add-card" @click="addEpisodeOpen=true"><div><Plus :size="25"/></div><strong>新增第 {{ project.episodes.length+1 }} 集</strong><span>集数没有上限，可持续更新直至完结</span></button>
        </div>
      </section>
    </template>

      </fieldset>
    </section>

    <BaseModal :open="!!variantEntity" :title="`${variantEntity?.name || ''} · 主图与变体设定`" width="980px" :dismissible="!variantBusy" @close="closeVariantSettings">
      <BibleVariantEditor v-if="variantEntity" :key="`${variantEntity.scope.project_id}:${variantEntity.scope.entity_kind}:${variantEntity.scope.entity_id}`" :scope="variantEntity.scope" :entity-name="variantEntity.name" @busy="variantBusy=$event" @dirty="variantDirty=$event" @translations-changed="markProjectInputsChangedFromTranslation"/>
    </BaseModal>
    <BaseModal :open="!!bibleImagePreview" title="候选参考图完整预览" eyebrow="设定参考图候选（Bible Image Candidate）" width="920px" @close="bibleImagePreview=null">
      <div v-if="bibleImagePreview" class="bible-image-full-preview"><img :src="bibleImagePreview.src" :alt="bibleImagePreview.title"><footer><span><strong>{{ bibleImagePreview.title }}</strong><small>{{ bibleImagePreview.filename }}</small></span><a v-if="bibleImagePreview.downloadUrl" class="button ghost small" :href="bibleImagePreview.downloadUrl" target="_blank" rel="noopener"><Download :size="13"/>打开原图</a></footer></div>
    </BaseModal>
    <BaseModal :open="addBibleOpen" :title="`登记${bibleSections.find(s=>s.value===bibleSection)?.singular}实体`" eyebrow="实体登记（Entity Registration）" width="1040px" @close="closeAddBibleModal">
      <div class="bible-draft-editor">
        <div class="bible-draft-evidence"><Lock :size="17"/><span><strong>人工登记一致性实体（Bible）</strong><small>此处只登记实体名称和说明，可选绑定参考图；共同身份和外观请在保存后的“主图 / 变体设定”中维护。</small></span><b>人工（Manual）</b></div>

        <section class="bible-asset-browser">
          <header><div><span class="impact-label-row">选择 1 张参考图 <PromptImpactBadge kind="continuity"/><PromptImpactBadge kind="production" title="创建后绑定到 Bible 素材池；具体镜头仍需明确选择才会接入视频工作流。"/></span><small>显示素材库全部图片；只有与当前{{ bibleSections.find(s=>s.value===bibleSection)?.singular }}类别一致的素材可选，切换选择会替换上一张。</small></div><div class="bible-asset-tools"><label><Search :size="14"/><input v-model="bibleAssetSearch" placeholder="搜索名称、描述、视觉锚点"></label><select v-model="bibleAssetCategory"><option value="compatible">仅可用于当前设定</option><option value="all">全部图片</option><option value="character">人物</option><option value="scene">场景</option><option value="prop">道具</option><option value="image">未分类图片</option></select></div></header>
          <div v-if="bibleAssetCatalogLoading" class="bible-asset-loading"><div class="loader-ring"></div><span>正在独立加载素材库全部参考图…</span></div>
          <div v-else-if="bibleAssetCatalogError" class="bible-draft-error"><AlertTriangle :size="14"/><span>{{ bibleAssetCatalogError }}</span><button type="button" @click="openAddBibleModal"><RefreshCw :size="13"/>重试</button></div>
          <div v-else-if="filteredBibleAssetCatalog.length" class="bible-asset-catalog">
            <article v-for="asset in filteredBibleAssetCatalog" :key="asset.id" :class="{active:bibleDraft.referenceAssetIds[0]===asset.id,incompatible:!isBibleDraftAssetCompatible(asset)}">
              <button type="button" class="bible-asset-select" :disabled="!isBibleDraftAssetCompatible(asset)||bibleCreatePhase!=='idle'" :title="isBibleDraftAssetCompatible(asset)?`选择 ${asset.name}`:bibleDraftAssetUnavailableReason(asset)" @click="selectBibleDraftAsset(asset)"><AssetImage :src="asset.thumbnail" :label="asset.name" :accent="currentStyle?.accent"/><span><strong>{{ asset.name }}</strong><small>{{ bibleAssetTypeLabel(asset) }}<template v-if="asset.isActive===false"> · 已停用</template></small></span><i><Check :size="12"/></i></button>
              <button type="button" class="bible-asset-preview" :aria-label="`预览 ${asset.name}`" @click="bibleAssetPreview=asset"><Eye :size="13"/></button>
            </article>
          </div>
          <div v-else class="bible-asset-empty"><Image :size="22"/><span>没有匹配的参考图</span><button v-if="bibleAssetSearch||bibleAssetCategory!=='compatible'" type="button" @click="bibleAssetSearch='';bibleAssetCategory='compatible'">清除筛选</button></div>
          <footer><span>素材库 {{ bibleAssetCatalog.length }} 张图片 · 当前显示 {{ filteredBibleAssetCatalog.length }} 张</span><RouterLink to="/assets" target="_blank">维护素材名称与说明 <ArrowRight :size="12"/></RouterLink></footer>
        </section>

        <div v-if="bibleCreateError" class="bible-draft-error"><AlertTriangle :size="14"/><span>{{ bibleCreateError }}</span><button type="button" @click="bibleCreateError=''">关闭</button></div>

        <div class="form-stack bible-draft-fields">
          <label class="field"><span class="impact-label-row">名称 * <PromptImpactBadge kind="label"/><PromptImpactBadge kind="indirect" title="用于正式脚本与生产阶段的实体匹配。"/></span><input v-model="bibleDraft.name" :disabled="bibleCreatePhase!=='idle'" placeholder="可使用中文；项目内需唯一"><small>请填写稳定、唯一且可供脚本匹配的正式名称。</small></label>
          <label class="field"><span class="impact-label-row">基础描述 <PromptImpactBadge kind="indirect" title="供创作与管理阅读，是否进入模型请求取决于提示词策略。"/></span><textarea v-model="bibleDraft.description" :disabled="bibleCreatePhase!=='idle'" rows="3" placeholder="可用中文或英文记录创作意图；是否进入模型请求取决于提示词策略"></textarea></label>
          <p class="muted">先登记实体；共同身份、主图外观和变体差异在保存后的“主图 / 变体设定”中维护，无需先有图片。</p>
        </div>
        <div v-if="bibleCreatePhase!=='idle'" class="canonical-create-progress"><div class="loader-ring"></div><span><strong>正在保存实体…</strong><small>仅保存正式对象，不调用模型。英文候选需另行选择实例生成并审核。</small></span></div>
        <PromptWritingGuide scope="bible"/>
      </div>
      <template #footer><button class="button ghost" :disabled="bibleCreatePhase!=='idle'" @click="closeAddBibleModal">取消</button><button class="button primary" :disabled="bibleCreatePhase!=='idle'" @click="addBibleItem"><Lock :size="15"/>{{ bibleCreatePhase==='saving'?'正在保存实体…':'确认字段并登记实体' }}</button></template>
    </BaseModal>
    <BaseModal :open="!!bibleAssetPreview" title="参考图完整预览" eyebrow="素材预览（Asset Preview）" width="920px" @close="bibleAssetPreview=null"><div v-if="bibleAssetPreview" class="bible-asset-full-preview"><AssetImage :src="bibleAssetPreview.thumbnail||bibleAssetPreview.mediaUrl" :label="bibleAssetPreview.name"/><footer><span><strong>{{ bibleAssetPreview.name }}</strong><small>{{ bibleAssetTypeLabel(bibleAssetPreview) }} · {{ bibleAssetPreview.description || '未填写基础描述' }}</small></span><button v-if="isBibleDraftAssetCompatible(bibleAssetPreview)" type="button" class="button primary small" @click="selectBibleDraftAsset(bibleAssetPreview);bibleAssetPreview=null"><Check :size="13"/>选作参考图</button></footer></div></BaseModal>
    <BaseModal :open="addEpisodeOpen" title="新增连续剧集" eyebrow="下一集（Next Episode）" @close="addEpisodeOpen=false">
      <div class="next-episode-banner"><span>EP</span><strong>{{ String(project.episodes.length+1).padStart(2,'0') }}</strong><p>复用本项目的 {{ project.bible.characters.length }} 位角色、{{ project.bible.scenes.length }} 个场景与 {{ project.bible.props.length }} 件道具；具体设定按选定的修订引用。</p></div><div class="form-stack"><label class="field"><span class="impact-label-row">本集标题 <PromptImpactBadge kind="label" title="用于剧集、任务和成片识别；不会原样写入单镜提示词。"/></span><input v-model="episodeDraft.title" :placeholder="`第 ${project.episodes.length+1} 集`"></label><label class="field"><span class="impact-label-row">本集梗概 <PromptImpactBadge kind="indirect" title="用于结构预览与分镜拆解；不会原样复制进正式提示词。"/></span><textarea v-model="episodeDraft.synopsis" rows="4" placeholder="描述本集的起因、升级、转折与钩子。"></textarea></label><label class="field"><span class="impact-label-row">本集剧本原文 <PromptImpactBadge kind="indirect" title="分镜拆解读取；对白只能从此处或全剧母稿精确引用。"/></span><textarea v-model="episodeDraft.script" rows="8" placeholder="粘贴本集场景、动作和中文对白原文。建议保留角色名与场次；留空时使用全剧母稿。"></textarea><small>这是创作事实来源，不会整段塞入工作流；对白正文必须是这里或全剧母稿的精确子串。</small></label><div class="inherit-note"><Lock :size="15"/><span>本集不预设视频输入模式；在生产工作台按配方或手动选择执行实例，具体素材槽位与参数由该实例确定。</span></div></div>
      <template #footer><button class="button ghost" @click="addEpisodeOpen=false">取消</button><button class="button primary" @click="addEpisode"><Plus :size="15"/>新增并编辑分镜</button></template>
    </BaseModal>
  </div>
  <section v-else class="panel project-load-failure" role="alert">
    <AlertTriangle :size="30" />
    <h2>项目加载失败</h2>
    <p>{{ projectLoadError || '没有找到这个连续剧项目。' }}</p>
    <div><button type="button" class="button ghost" @click="router.push('/projects')"><ArrowLeft :size="15"/>返回项目列表</button><button type="button" class="button primary" @click="refreshProjectPage"><RefreshCw :size="15"/>重新加载</button></div>
  </section>
</template>

<style scoped>
.embedded-project-inputs{gap:0}.project-input-fields{border:0;padding:0;margin:0;min-width:0;display:grid;gap:22px}
/* Keep the established studio shell; only its content source changes by selected run. */
.unified-project-studio{min-width:0}
.unified-project-studio .series-header{gap:24px;flex-wrap:wrap}
.unified-project-studio .series-header-main{flex:1 1 440px;min-width:0}
.unified-project-studio .series-title>div:last-child{min-width:0}
.unified-project-studio .series-title h2{overflow-wrap:anywhere}
.unified-project-studio .series-title p{font-size:14px}
.unified-project-studio .series-poster{flex-shrink:0}
.unified-project-studio .series-header-actions{flex-wrap:wrap}
.unified-project-studio .series-facts{font-size:12px;flex-wrap:wrap}
.project-input-editor{display:grid;gap:22px;min-width:0}
.project-editor-heading{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:20px}
.project-editor-heading>div{flex:1 1 360px;min-width:0}
.project-editor-heading h3{margin:6px 0;font-size:20px}
.project-editor-heading p{font-size:14px;line-height:1.7;color:var(--text-secondary)}
.manual-episode-actions{grid-template-columns:minmax(0,1fr) auto 34px}
.project-input-editor .episode-card{cursor:default}
.project-input-editor .episode-copy>div:first-child{flex-wrap:wrap}
.project-input-editor .episode-copy p{height:auto;-webkit-line-clamp:3;font-size:13px;color:var(--text-secondary)}
.project-input-editor .episode-tags span{font-size:12px}
.project-input-editor .global-rules header{gap:16px;flex-wrap:wrap}
@media(max-width:600px){
 .unified-project-studio .series-header{margin:-16px -16px 0;padding:20px 16px;min-height:0}
 .unified-project-studio .series-header-main{flex-basis:100%}
 .unified-project-studio .series-title{gap:12px;align-items:flex-start}
 .unified-project-studio .series-title h2{font-size:22px}
 .unified-project-studio .series-poster{width:58px;height:76px}
 .unified-project-studio .series-header-actions{width:100%}
 .unified-project-studio .series-header-actions>.button{flex:1 1 auto;white-space:normal}
 .project-editor-heading{padding:16px}
 .project-editor-heading>.button{flex:1 1 auto}
 .project-input-editor .bible-layout,.project-input-editor .episode-grid{grid-template-columns:1fr}
 .project-input-editor .bible-nav{position:static}
 .project-input-editor .bible-card{grid-template-columns:70px minmax(0,1fr)}
 .project-input-editor .bible-ref{height:100px}
 .project-input-editor .bible-card-actions{grid-column:1/-1;flex-direction:row;flex-wrap:wrap}
 .project-input-editor .section-heading{align-items:flex-start;flex-wrap:wrap}
 .project-input-editor .manual-episode-actions{grid-template-columns:minmax(0,1fr) 34px}
 .manual-episode-actions>.button:first-child{grid-column:1/-1}
 .project-input-editor .form-grid.two{grid-template-columns:1fr}
}

@media (max-width:480px){.unified-project-studio .series-header{margin:-18px -12px 0;padding:20px 12px}}

.creation-style-grid,.creation-aspect-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:10px}
.creation-style-grid button,.creation-aspect-grid button{border:1px solid var(--border);border-radius:10px;background:var(--bg-soft);padding:14px;text-align:left;cursor:pointer;color:var(--text-secondary);min-width:0}
.creation-style-grid button.active,.creation-aspect-grid button.active{border-color:var(--accent);background:rgba(217,161,93,.08)}
.creation-style-grid button:focus-visible,.creation-aspect-grid button:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.creation-style-grid strong{display:flex;justify-content:space-between;gap:8px;color:var(--text)}
.creation-style-grid button>span{display:block;margin-top:7px;font-size:12px;line-height:1.6;overflow-wrap:anywhere}
.creation-aspect-grid button{display:flex;gap:12px;align-items:center}
.creation-aspect-grid strong,.creation-aspect-grid small{display:block;line-height:1.6}
.aspect-shape{width:40px;height:40px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.aspect-shape i{display:block;border:1px solid currentColor;border-radius:3px}
.creation-selection-detail{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;margin:14px 0;padding:12px;border:1px solid var(--border);border-radius:8px;font-size:12px;overflow-wrap:anywhere}
.creation-selection-detail details{flex-basis:100%;min-width:0}.creation-selection-detail summary{cursor:pointer}.creation-selection-detail p{white-space:pre-wrap}
.creation-hint{color:var(--accent)}
.new-project-page .section-content>p{font-size:12px;line-height:1.7;color:var(--text-secondary)}
.new-project-page .section-content>p.creation-error{color:var(--red)}
.new-project-page .section-content>p.creation-hint{color:var(--accent)}
.new-project-page .section-content h3>small{font-size:11px;color:var(--text-secondary);font-weight:400;margin-left:6px}
.new-project-page .section-content .field>small{position:static;line-height:1.5}
.onboarding-fields{border:0;margin:0;padding:0;min-width:0;display:grid;gap:12px}.creation-mode-tabs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:10px 0 14px}.creation-mode-tabs button{display:grid;grid-template-columns:auto 1fr;gap:3px 8px;align-items:center;padding:11px;border:1px solid #2a323a;border-radius:8px;background:#0b1014;color:#8c969e;text-align:left;cursor:pointer}.creation-mode-tabs button svg{grid-row:1/3;color:#c98e43}.creation-mode-tabs button strong{font-size:10px;color:#cbd1d5}.creation-mode-tabs button small{font-size:8px;line-height:1.4}.creation-mode-tabs button.active{border-color:#b67b35;background:rgba(195,132,55,.08);box-shadow:0 0 0 1px rgba(195,132,55,.12)}.creation-subtabs{display:flex;gap:6px;margin:4px 0}.creation-subtabs button{padding:7px 11px;border:1px solid #293039;border-radius:6px;background:#0b1014;color:#77818a;font-size:9px;cursor:pointer}.creation-subtabs button.active{border-color:#a9783d;color:#dda357}.onboarding-run-options{padding:11px;border:1px solid #28313a;border-radius:8px;background:#0b1014}.onboarding-unattended-panel{display:grid;gap:11px;margin-top:11px;padding:12px;border:1px solid rgba(215,154,70,.34);border-radius:8px;background:rgba(215,154,70,.045)}.onboarding-unattended-panel header strong,.onboarding-unattended-panel header small{display:block}.onboarding-unattended-panel header strong{color:#e2b06a;font-size:11px}.onboarding-unattended-panel header small{margin-top:4px;color:#8b8174;font-size:9px;line-height:1.5}.onboarding-unattended-panel dl{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin:0}.onboarding-unattended-panel dl>div{padding:8px;border:1px solid #2a3036;border-radius:6px;background:#0b1014}.onboarding-unattended-panel dt{color:#747d85;font-size:8px}.onboarding-unattended-panel dd{margin:4px 0 0;color:#b7bec4;font-size:9px;line-height:1.4}.unattended-onboarding-confirm{display:flex;align-items:flex-start;gap:8px;color:#aeb5bb;font-size:9px;line-height:1.55}.unattended-onboarding-confirm input{flex:0 0 auto;margin-top:2px}.creation-unattended-ready{display:flex;align-items:flex-start;gap:7px;margin:0;color:#68c6a5;font-size:9px;line-height:1.5}.creation-error{display:flex;align-items:flex-start;gap:7px;margin:7px 0;color:#e4868d;font-size:9px;line-height:1.5}.creation-error a{margin-left:auto;color:#d69a4a}.import-review{display:flex;flex-direction:column;gap:12px}.import-review-evidence{display:flex;align-items:flex-start;gap:9px;padding:11px;border:1px solid rgba(104,197,167,.25);border-radius:7px;background:rgba(104,197,167,.05);color:#67c6a7}.import-review-evidence span{display:flex;flex-direction:column;gap:3px}.import-review-evidence strong{font-size:10px}.import-review-evidence small{font-size:8px;line-height:1.5;color:#739a8d}.import-review textarea{font-family:Consolas,monospace;line-height:1.6}
.bible-continuity-readiness{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:flex-start;gap:10px;padding:12px;border:1px solid rgba(104,197,167,.28);border-radius:8px;background:rgba(104,197,167,.055);color:#72caae}.bible-continuity-readiness.blocked{border-color:rgba(235,103,98,.48);background:rgba(235,103,98,.085);color:#eb827d}.bible-continuity-readiness>svg{margin-top:2px;flex:0 0 auto}.bible-continuity-readiness-copy{min-width:0;display:flex;flex-direction:column;gap:5px}.bible-continuity-readiness-copy>header{display:flex;align-items:center;flex-wrap:wrap;gap:7px}.bible-continuity-readiness-copy>header strong{color:#dce2e5;font-size:11px}.bible-continuity-readiness-copy>header b{padding:3px 7px;border:1px solid currentColor;border-radius:999px;font-size:8px}.bible-continuity-readiness-copy p{margin:0;color:#97aaa4;font-size:9px;line-height:1.55}.bible-continuity-readiness.blocked p{color:#c99b99}.bible-continuity-readiness-copy p strong{color:inherit}.bible-continuity-readiness-copy small{color:#dcaaa5;font-size:9px;line-height:1.55}.bible-continuity-readiness>.button{align-self:center;white-space:nowrap}.bible-image-global-state{display:flex;align-items:center;gap:8px;padding:9px 11px;border:1px solid #283039;border-radius:7px;background:#0b0f13;color:#838d95;font-size:10px}.bible-image-global-state>span{flex:1}.bible-image-global-state button{display:flex;align-items:center;gap:5px;border:0;background:none;color:#d6a054;cursor:pointer}.bible-image-global-state.error{border-color:#593039;color:#e78c94}.bible-image-prompt-config{margin-top:12px;padding:12px;border:1px solid #2b3540;border-radius:8px;background:#0a0f13}.bible-image-prompt-config>summary,.bible-image-negative-config>summary{display:flex;align-items:center;justify-content:space-between;gap:12px;cursor:pointer;color:#cdd5db}.bible-image-prompt-config>summary span{display:flex;flex-direction:column;gap:3px}.bible-image-prompt-config small{color:#74808a;font-size:9px}.bible-image-prompt-config>.form-grid,.bible-image-negative-config>.form-grid{margin-top:12px}.bible-image-negative-config{grid-column:1/-1;margin-top:10px;padding-top:10px;border-top:1px solid #202a33}.bible-image-candidate{grid-column:1/-1;display:flex;flex-direction:column;gap:9px;padding:12px;border:1px solid #252d34;border-radius:8px;background:#090d11}.bible-image-candidate>header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.bible-image-candidate>header>span{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.bible-image-candidate>header small{width:100%;padding-left:21px;color:#707982;font-size:9px}.bible-image-empty{margin:0;color:#6f7880;font-size:9px}.bible-image-status{display:flex;flex-direction:column;gap:8px;padding-top:9px;border-top:1px solid #222a31}.bible-image-status>div:first-child{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.bible-image-status code{color:#70808e;font-size:8px}.bible-image-status>span{align-self:flex-start;padding:3px 7px;border:1px solid #31553e;border-radius:10px;color:#6fc68f;font-size:8px}.bible-image-status.stale>span{border-color:#6a4927;color:#df9e5c}.bible-image-status>p,.bible-image-status li{display:flex;align-items:flex-start;gap:6px;margin:0;color:#dda063;font-size:9px;line-height:1.5}.bible-image-status ul{display:grid;gap:5px;margin:0;padding:9px 10px;list-style:none;border:1px solid #4d3b22;border-radius:6px;background:#17130c}.bible-image-task-actions{display:flex!important;justify-content:flex-end}.bible-image-outputs{display:grid!important;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:9px!important}.bible-image-outputs article{display:flex;flex-direction:column;gap:6px;padding:7px;border:1px solid #293139;border-radius:7px;background:#0c1115}.bible-image-outputs img,.bible-image-outputs article>div{width:100%;aspect-ratio:1/1;border-radius:5px;object-fit:cover;background:#11171c}.bible-image-outputs article>div{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:5px;color:#68727b;font-size:8px}.bible-image-outputs article>span{overflow:hidden;color:#79838b;font-size:8px;text-overflow:ellipsis;white-space:nowrap}.bible-image-history{display:flex;flex-direction:column;gap:10px}.bible-image-history>article{display:flex;flex-direction:column;gap:8px;padding:9px;border:1px solid #252e36;border-radius:7px;background:#0a0f13}.bible-image-history>article.stale{border-color:#5b3c23;background:#15100a}.bible-image-history header{display:flex;align-items:center;justify-content:space-between;gap:9px}.bible-image-history header>span{display:flex;min-width:0;flex-direction:column;gap:3px}.bible-image-history header strong{color:#cfd5da;font-size:10px}.bible-image-history header small{color:#74808a;font-size:8px}.bible-image-history p{display:flex;align-items:flex-start;gap:6px;margin:0;color:#dda063;font-size:9px;line-height:1.5}.bible-image-preview-button{position:relative;width:100%;aspect-ratio:1/1;padding:0;overflow:hidden;border:0;border-radius:5px;background:#11171c;cursor:zoom-in}.bible-image-preview-button img{width:100%;height:100%;object-fit:cover}.bible-image-preview-button>span{position:absolute;right:5px;bottom:5px;display:flex;align-items:center;gap:4px;padding:3px 6px;border-radius:999px;background:rgba(5,8,11,.78);color:#d8dde1;font-size:8px}.bible-image-full-preview{display:flex;flex-direction:column;gap:10px}.bible-image-full-preview>img{width:100%;height:min(72vh,720px);object-fit:contain;border-radius:8px;background:#080b0e}.bible-image-full-preview>footer{display:flex;align-items:center;justify-content:space-between;gap:14px}.bible-image-full-preview>footer>span{display:flex;min-width:0;flex-direction:column;gap:4px}.bible-image-full-preview strong{font-size:12px}.bible-image-full-preview small{overflow:hidden;color:#737c84;font-size:9px;text-overflow:ellipsis;white-space:nowrap}
.cover-picker{display:grid;grid-template-columns:112px minmax(0,1fr);align-items:center;gap:15px}.cover-picker-preview{width:112px;aspect-ratio:2/3;padding:0;overflow:hidden;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:7px;border:1px dashed rgba(230,167,90,.38);border-radius:7px;color:#d7a45e;cursor:pointer}.cover-picker-preview img{width:100%;height:100%;object-fit:cover}.cover-picker-preview span{font-size:9px}.cover-picker>div{display:flex;min-width:0;flex-direction:column;gap:7px}.cover-picker strong{overflow:hidden;color:#c1c5ca;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.cover-picker small{color:#697078;font-size:9px;line-height:1.55}.cover-picker-actions{display:flex;gap:7px}.cover-error{margin-top:9px;color:#e4868d;font-size:10px;line-height:1.5}.new-cover-preview-image{position:absolute!important;inset:0;z-index:0!important;width:100%;height:100%;object-fit:cover}.new-project-preview:has(.new-cover-preview-image):after{content:'';position:absolute;inset:0;z-index:1;background:linear-gradient(180deg,rgba(5,7,9,.15),rgba(5,7,9,.78))}.series-cover-background{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:.42}.series-poster>img{width:100%;height:100%;border-radius:inherit;object-fit:cover}.series-header-actions{flex-wrap:wrap;justify-content:flex-end}.header-cover-error{width:100%;margin:0;text-align:right}
.impact-label-row{display:flex!important;align-items:center;flex-wrap:wrap;gap:6px}.impact-heading{display:flex;align-items:center;flex-wrap:wrap;gap:7px}.impact-heading h3{margin-right:2px}.script-editor-stack{min-width:0;display:flex;flex-direction:column;gap:10px}.bible-impact-label{display:flex;align-items:center;flex-wrap:wrap;gap:5px;min-height:20px;font-size:10px!important;letter-spacing:.04em!important;color:#777d85!important}.bible-card-title span{font-size:9px!important;color:#8e7655!important}.management-only-note{max-width:118px;color:#6d737a;font-size:10px;line-height:1.35;text-align:right}.bible-card-actions .lock-button{height:auto;min-height:32px;padding:6px 9px;text-align:left}.global-rules header>div>p{max-width:760px;margin-top:5px;font-size:10px;line-height:1.55;color:#6f757d}.global-rules>.translation-review{margin-top:14px}.global-rules .field>small{position:static;font-size:10px;line-height:1.5}.form-stack>.prompt-writing-guide{margin-top:2px}
.project-load-failure{max-width:620px;margin:42px auto;padding:42px;display:flex;align-items:center;flex-direction:column;gap:12px;text-align:center;color:#d99651}.project-load-failure h2{margin:0;color:#e4e8eb}.project-load-failure p{max-width:520px;margin:0;color:#7d878f;line-height:1.6}.project-load-failure>div{display:flex;gap:9px;margin-top:8px}
.script-rebuild-confirm{display:flex;flex-direction:column;gap:14px}.script-rebuild-confirm>div{display:flex;align-items:flex-start;gap:10px;padding:12px;border:1px solid rgba(229,107,99,.3);border-radius:8px;background:rgba(229,107,99,.06);color:#df8178}.script-rebuild-confirm>div>span{display:flex;flex-direction:column;gap:4px}.script-rebuild-confirm strong{font-size:12px;color:#e6a099}.script-rebuild-confirm small,.script-rebuild-confirm li,.script-rebuild-confirm p{font-size:9px;line-height:1.6;color:#8f8585}.script-rebuild-confirm ul{margin:0;padding:0 0 0 20px;display:grid;gap:6px}.script-rebuild-confirm p{margin:0;padding:10px;border-radius:7px;background:#0b0f13;color:#777f86}
.canonical-notice{padding:9px 11px;border:1px solid rgba(104,197,167,.25);border-radius:7px;background:rgba(104,197,167,.06);color:#8bd8be;font-size:10px;line-height:1.5}.canonical-notice.warning{border-color:rgba(218,154,70,.3);background:rgba(218,154,70,.07);color:#d7a65f}.bible-canonical-notice{display:flex;align-items:flex-start;gap:8px;margin:-2px 0 4px}.bible-canonical-notice>strong{flex:0 0 auto;color:inherit;font-size:9px;letter-spacing:.04em}.bible-canonical-notice>span{min-width:0}.visual-translation-notice{border-color:rgba(94,164,218,.25);background:rgba(94,164,218,.06);color:#8bc5e7}.visual-translation-notice.warning{border-color:rgba(218,154,70,.3);background:rgba(218,154,70,.07);color:#d7a65f}.canonical-create-hint,.canonical-create-progress{display:flex;gap:10px;align-items:flex-start;padding:11px 12px;border:1px solid rgba(104,197,167,.22);border-radius:7px;background:rgba(104,197,167,.05);color:#67c5a6}.canonical-create-hint>span,.canonical-create-progress>span{display:flex;min-width:0;flex-direction:column;gap:3px}.canonical-create-hint strong,.canonical-create-progress strong{font-size:11px;color:#a8dccb}.canonical-create-hint small,.canonical-create-progress small{font-size:9px;line-height:1.5;color:#72978a}.canonical-create-progress .loader-ring{width:16px;height:16px;flex:0 0 auto}.spinning{animation:spin 1s linear infinite}
.optional-translations{margin:18px 0;padding:16px;border:1px solid var(--border);border-radius:12px;min-width:0}.optional-translations>summary{cursor:pointer;font-size:14px;color:var(--text-muted)}.optional-translations>p{font-size:14px;line-height:1.7;color:var(--text-muted)}
.bible-draft-editor{display:flex;flex-direction:column;gap:13px}.bible-draft-evidence{display:flex;align-items:flex-start;gap:10px;padding:12px;border:1px solid rgba(104,197,167,.25);border-radius:8px;background:rgba(104,197,167,.05);color:#64c5a5}.bible-draft-evidence>span{display:flex;min-width:0;flex:1;flex-direction:column;gap:4px}.bible-draft-evidence strong{font-size:11px;color:#a7daca}.bible-draft-evidence small{font-size:9px;line-height:1.55;color:#71958a}.bible-draft-evidence>b{padding:4px 7px;border:1px solid rgba(104,197,167,.25);border-radius:999px;font-size:7px;letter-spacing:.1em}.bible-asset-browser{overflow:hidden;border:1px solid #293038;border-radius:9px;background:#0d1115}.bible-asset-browser>header{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;padding:12px;border-bottom:1px solid #242a31}.bible-asset-browser>header>div:first-child{display:flex;flex-direction:column;gap:5px}.bible-asset-browser>header span{font-size:10px;color:#aeb5bb}.bible-asset-browser>header small{font-size:8px;line-height:1.45;color:#677078}.bible-asset-tools{display:flex;align-items:center;gap:7px}.bible-asset-tools label{width:240px;height:32px;padding:0 9px;display:flex;align-items:center;gap:7px;border:1px solid #2d343c;border-radius:6px;background:#090c10;color:#69727b}.bible-asset-tools input{min-width:0;flex:1;border:0;outline:0;background:transparent;color:#c4c9ce;font-size:9px}.bible-asset-tools select{height:32px;border:1px solid #2d343c;border-radius:6px;background:#090c10;color:#9fa6ac;font-size:9px}.bible-asset-catalog{max-height:330px;padding:10px;display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;overflow-y:auto}.bible-asset-catalog article{min-width:0;height:150px;position:relative;overflow:hidden;border:1px solid #2a3037;border-radius:7px;background:#11161b}.bible-asset-catalog article.active{border-color:#d99b48;box-shadow:0 0 0 1px rgba(217,155,72,.2)}.bible-asset-catalog article.incompatible{opacity:.48}.bible-asset-select{width:100%;height:100%;padding:0;position:relative;overflow:hidden;border:0;background:transparent;text-align:left;cursor:pointer}.bible-asset-select:disabled{cursor:not-allowed}.bible-asset-select img{width:100%;height:105px;object-fit:cover}.bible-asset-select>span{height:43px;padding:5px 7px;display:flex;min-width:0;flex-direction:column;gap:2px}.bible-asset-select strong{overflow:hidden;color:#c2c7cc;font-size:9px;text-overflow:ellipsis;white-space:nowrap}.bible-asset-select small{overflow:hidden;color:#687179;font-size:7px;text-overflow:ellipsis;white-space:nowrap}.bible-asset-select>i{width:18px;height:18px;position:absolute;right:6px;top:6px;display:none;align-items:center;justify-content:center;border-radius:50%;background:#d99b48;color:#16100a}.bible-asset-catalog article.active .bible-asset-select>i{display:flex}.bible-asset-preview{width:24px;height:24px;position:absolute;right:5px;top:76px;display:flex;align-items:center;justify-content:center;border:1px solid rgba(255,255,255,.18);border-radius:5px;background:rgba(7,9,12,.8);color:#ccd1d5;cursor:pointer}.bible-asset-browser>footer{display:flex;justify-content:space-between;gap:12px;padding:8px 11px;border-top:1px solid #242a31;color:#68717a;font-size:8px}.bible-asset-browser>footer a{display:flex;align-items:center;gap:5px;color:#c7914b}.bible-asset-loading,.bible-asset-empty{min-height:150px;display:flex;align-items:center;justify-content:center;gap:9px;color:#737d85;font-size:10px}.bible-asset-empty{flex-direction:column}.bible-asset-empty button{border:0;background:transparent;color:#c48d48;cursor:pointer}.bible-draft-assistant{padding:11px 12px;display:grid;grid-template-columns:minmax(0,1fr) auto auto;align-items:center;gap:13px;border:1px solid #2b3239;border-radius:8px;background:#0c1014}.bible-draft-assistant.ready{border-color:rgba(217,155,72,.28);background:rgba(217,155,72,.035)}.bible-draft-assistant-copy{display:flex;align-items:flex-start;gap:9px;color:#d39a50}.bible-draft-assistant-copy>span{display:flex;flex-direction:column;gap:3px}.bible-draft-assistant-copy strong{font-size:10px}.bible-draft-assistant-copy small{font-size:8px;color:#737b82}.bible-draft-language{height:32px;padding:3px;display:flex;align-items:center;gap:3px;border:1px solid #2c333a;border-radius:7px;background:#090c10}.bible-draft-language>span{padding:0 6px;color:#6e777f;font-size:8px}.bible-draft-language button{height:24px;padding:0 9px;border:0;border-radius:5px;background:transparent;color:#757e86;font-size:8px;cursor:pointer}.bible-draft-language button.active{background:rgba(217,155,72,.14);color:#e2a657}.bible-draft-error,.bible-draft-stale,.bible-draft-provenance,.bible-draft-warnings{display:flex;align-items:flex-start;gap:8px;padding:10px 11px;border:1px solid rgba(239,115,123,.27);border-radius:7px;background:rgba(239,115,123,.055);color:#df7f88}.bible-draft-error span{min-width:0;flex:1;font-size:9px;line-height:1.5}.bible-draft-error button,.bible-draft-stale button{display:flex;align-items:center;gap:4px;border:0;background:transparent;color:inherit;font-size:8px;cursor:pointer}.bible-draft-stale{border-color:rgba(217,155,72,.28);background:rgba(217,155,72,.055);color:#d49b50}.bible-draft-stale>span,.bible-draft-provenance>span,.bible-draft-warnings>span{display:flex;min-width:0;flex:1;flex-direction:column;gap:3px}.bible-draft-stale strong,.bible-draft-provenance strong,.bible-draft-warnings strong{font-size:9px}.bible-draft-stale small,.bible-draft-provenance small,.bible-draft-warnings small{font-size:8px;line-height:1.45}.bible-draft-provenance{border-color:rgba(104,197,167,.24);background:rgba(104,197,167,.05);color:#65c3a4}.bible-draft-provenance code{color:#7b9e93;font-size:8px}.bible-draft-warnings{border-color:rgba(217,155,72,.25);background:rgba(217,155,72,.045);color:#c7924c}.bible-draft-conflicts{overflow:hidden;border:1px solid rgba(91,164,218,.25);border-radius:8px;background:rgba(91,164,218,.035)}.bible-draft-conflicts>header{padding:9px 11px;display:flex;align-items:center;justify-content:space-between;gap:10px;border-bottom:1px solid rgba(91,164,218,.18)}.bible-draft-conflicts>header span{display:flex;flex-direction:column;gap:2px}.bible-draft-conflicts strong{color:#8ec1df;font-size:9px}.bible-draft-conflicts small{font-size:8px;line-height:1.45;color:#718996}.bible-draft-conflicts button{border:0;background:transparent;color:#84bad9;font-size:8px;cursor:pointer}.bible-draft-conflicts article{padding:8px 11px;display:flex;align-items:center;justify-content:space-between;gap:12px;border-top:1px solid rgba(91,164,218,.1)}.bible-draft-conflicts article span{display:flex;min-width:0;flex:1;flex-direction:column;gap:3px}.bible-draft-conflicts article b{font-size:8px;color:#8b9ba5}.bible-draft-conflicts article small{max-height:44px;overflow:auto;color:#9aa9b2}.bible-draft-fields{padding-top:2px}.bible-asset-full-preview{display:flex;flex-direction:column;gap:10px}.bible-asset-full-preview>img{width:100%;height:min(68vh,680px);object-fit:contain;border-radius:8px;background:#080b0e}.bible-asset-full-preview>footer{display:flex;align-items:center;justify-content:space-between;gap:14px}.bible-asset-full-preview>footer>span{display:flex;min-width:0;flex-direction:column;gap:4px}.bible-asset-full-preview strong{font-size:12px}.bible-asset-full-preview small{font-size:9px;color:#737c84}
.whole-episode-image-resolution{padding:9px;border:1px solid rgba(104,197,167,.18);border-radius:7px;background:rgba(104,197,167,.025)}.whole-episode-image-resolution summary{color:#9bc5b7;font-size:10px;font-weight:650;cursor:pointer}.whole-episode-image-resolution>p{margin:7px 0 0!important;color:#74857f!important;font-size:8px!important;line-height:1.55!important}
.whole-episode-idempotency-conflict{display:flex;align-items:flex-start;gap:8px;padding:10px;border:1px solid rgba(223,101,90,.28);border-radius:7px;background:rgba(223,101,90,.055);color:#df8177}.whole-episode-idempotency-conflict>span{display:grid;gap:5px}.whole-episode-idempotency-conflict small{color:#a68585;font-size:9px;line-height:1.55}.whole-episode-idempotency-conflict button{justify-self:start}
@media (max-width:900px){.onboarding-unattended-panel dl{grid-template-columns:1fr}.bible-continuity-readiness{grid-template-columns:auto minmax(0,1fr)}.bible-continuity-readiness>.button{grid-column:2;justify-self:start}.bible-asset-browser>header{align-items:stretch;flex-direction:column}.bible-asset-tools{align-items:stretch}.bible-asset-tools label{width:auto;flex:1}.bible-asset-catalog{grid-template-columns:repeat(3,minmax(0,1fr))}.bible-draft-assistant{grid-template-columns:1fr}.bible-draft-language{width:max-content}}
</style>
