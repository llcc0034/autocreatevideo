import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { repository } from '@/services/repository'
import { acceptAuthoritativeCanonicalGlossary } from '@/services/translationReview'
import { DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE } from '@/constants/productionResolution'
import type { AssemblyResult, Asset, BibleAssetDraftResult, BibleDraftEntityType, BibleDraftLanguage, CanonicalGlossaryEnsureResult, CanonicalGlossaryScope, ComfySettings, Episode, H3PromptPayload, ProductionResolutionProfile, ProductionWorkflowSelection, ReleaseBundle, ReleaseSelection, ScriptBreakdownOptions, SeriesProject, Shot, StylePreset, Workflow } from '@/types'

const preserveWorkflowMedia = (next: Workflow, previous?: Workflow, preserveSignedUrls = true): Workflow => {
  if (!previous) return next
  let outputMedia = next.outputMedia
  if (previous.outputMedia?.length) {
    if (!next.outputMedia?.length) outputMedia = previous.outputMedia
    else {
      const previousMedia = new Map(previous.outputMedia.map((media, index) => [`${media.index ?? index}:${media.filename}`, media]))
      outputMedia = next.outputMedia.map((media, index) => {
        const stable = previousMedia.get(`${media.index ?? index}:${media.filename}`)
        // Background polling keeps the current URL so an active video is not
        // reloaded every few seconds. Explicit detail refreshes pass false and
        // must accept the newly signed URLs; otherwise a page left open longer
        // than MEDIA_SIGNED_URL_TTL_SECONDS can never recover.
        return stable && preserveSignedUrls
          ? {
              ...media,
              previewUrl: stable.previewUrl || media.previewUrl,
              downloadUrl: (
                media.distributionStatus === 'ready'
                && stable.publicArtifactSha256 === media.publicArtifactSha256
              ) ? (stable.downloadUrl || media.downloadUrl) : media.downloadUrl,
              publicProvenanceUrl: (
                media.distributionStatus === 'ready'
                && stable.publicManifestSha256 === media.publicManifestSha256
              ) ? (stable.publicProvenanceUrl || media.publicProvenanceUrl) : media.publicProvenanceUrl,
            }
          : media
      })
    }
  }
  type TaskAssets = Workflow['referenceAssets']
  const stableAssetKey = (asset: NonNullable<TaskAssets>[number], index: number) => asset.assetId || `${asset.mediaKind}:${asset.displayName}:${asset.inputLabel || index}`
  const preserveAssetPreviews = (nextAssets: TaskAssets, previousAssets: TaskAssets): TaskAssets => {
    // An omitted field means the lightweight polling response did not include
    // detail data. An explicit [] is authoritative and must clear stale assets.
    if (nextAssets === undefined) return previousAssets
    if (!nextAssets.length || !previousAssets?.length) return nextAssets
    const previousByKey = new Map(previousAssets.map((asset, index) => [stableAssetKey(asset, index), asset]))
    return nextAssets.map((asset, index) => {
      const stable = previousByKey.get(stableAssetKey(asset, index))
      return !asset.previewUrl && stable?.previewUrl ? { ...asset, previewUrl: stable.previewUrl } : asset
    })
  }
  const referenceAssets = preserveAssetPreviews(next.referenceAssets, previous.referenceAssets)
  const continuityAssets = preserveAssetPreviews(next.continuityAssets, previous.continuityAssets)
  return { ...next, outputMedia, referenceAssets, continuityAssets }
}

export const useStudioStore = defineStore('studio', () => {
  const styles = ref<StylePreset[]>([])
  const assets = ref<Asset[]>([])
  const projects = ref<SeriesProject[]>([])
  const workflows = ref<Workflow[]>([])
  const settings = ref<ComfySettings | null>(null)
  const loading = ref(false)
  const error = ref('')
  const toast = ref<{ message: string; type: 'success' | 'error' | 'info' } | null>(null)
  const ready = ref(false)

  const notify = (message: string, type: 'success' | 'error' | 'info' = 'success') => {
    toast.value = { message, type }; window.setTimeout(() => { toast.value = null }, 3200)
  }
  const resetSessionState = () => {
    styles.value = []; assets.value = []; projects.value = []; workflows.value = []; settings.value = null
    loading.value = false; error.value = ''; toast.value = null; ready.value = false
  }
  const initialize = async () => {
    if (ready.value) return
    loading.value = true; error.value = ''
    try {
      // Project/asset adapters depend on the style map, while task labels depend on project titles.
      styles.value = await repository.getStyles()
      projects.value = await repository.getProjects()
      ;[workflows.value, settings.value] = await Promise.all([repository.getWorkflows(), repository.getSettings()])
      ready.value = true
    }
    catch (e) { error.value = e instanceof Error ? e.message : '加载工作台失败' }
    finally { loading.value = false }
  }
  const refreshAssets = async (filters?: { style?: string; type?: string; search?: string }) => { assets.value = await repository.getAssets(filters) }
  const createImageAsset = async (payload: { file: File; kind: BibleDraftEntityType|'reference'; name: string; description: string; promptHint: string; styleId?: string }) => { const item=await repository.createImageAsset(payload);assets.value=[item,...assets.value.filter((asset)=>asset.id!==item.id)];notify('图片素材已入库');return item }
  const updateAssetMetadata = async (assetId:string,payload:{name:string;description:string;promptHint:string;kind:BibleDraftEntityType|'reference';styleId?:string}) => {const item=await repository.updateAssetMetadata(assetId,payload);const index=assets.value.findIndex((asset)=>asset.id===assetId);if(index>=0)assets.value[index]=item;else assets.value.unshift(item);notify('素材说明已保存');return item}
  const generateBibleDraftFromAssets = (projectId:string,payload:{entityType:BibleDraftEntityType;outputLanguage:BibleDraftLanguage;asset:Asset;attempt?:number}):Promise<BibleAssetDraftResult> => repository.generateBibleDraftFromAssets(projectId,payload)
  const refreshProjects = async () => { projects.value = await repository.getProjects(); return projects.value }
  const uploadReferenceAsset = async (file:File,kind:'reference_video'|'reference_audio',styleId?:string) => {const item=await repository.uploadReferenceAsset(file,kind,styleId);assets.value=[item,...assets.value.filter((asset)=>asset.id!==item.id)];notify(`${kind==='reference_video'?'参考视频':'参考音频'}已上传`);return item}
  const createProject = async (payload: Partial<SeriesProject>) => { const item = await repository.createProject(payload); projects.value.unshift(item); notify('连续剧项目已创建'); return item }
  const saveProject = async (project: SeriesProject) => { const saved = await repository.updateProject(project); const index = projects.value.findIndex((item) => item.id === saved.id); if (index >= 0) projects.value[index] = saved; else projects.value.unshift(saved); notify('项目设定已保存'); return saved }
  const ensureCanonicalGlossary = async (
    project: SeriesProject,
    options: { entries?: Array<{ scopeType: CanonicalGlossaryScope; scopeId: string }>; includeAllMissing?: boolean; ensureVisualIdentityTranslation?: boolean } = {},
  ): Promise<CanonicalGlossaryEnsureResult> => {
    const result = await repository.ensureCanonicalGlossary(project.id, options)
    const authoritative = acceptAuthoritativeCanonicalGlossary(result.translationGlossary)
    project.translationGlossary = authoritative
    const stored = projects.value.find((item) => item.id === project.id)
    if (stored && stored !== project) stored.translationGlossary = { ...authoritative }
    return { ...result, translationGlossary: authoritative }
  }
  const addEpisode = async (project: SeriesProject, payload: { title: string; synopsis: string; script?: string }) => { const item = await repository.addEpisode(project.id, payload); project.episodes.push(item); await saveProject(project); notify(`第 ${item.number} 集已加入`); return item }
  const deleteEpisode = async (project: SeriesProject, episodeId: string) => { await repository.deleteEpisode(project.id, episodeId); project.episodes = project.episodes.filter((item) => item.id !== episodeId); await saveProject(project); notify('剧集已删除', 'info') }
  const saveEpisode = async (project: SeriesProject, episode: Episode) => { const saved = await repository.saveEpisode(project, episode); const index = project.episodes.findIndex((item) => item.id === saved.id); if (index >= 0) project.episodes[index] = saved; const storedProject=projects.value.find((item)=>item.id===project.id);if(storedProject&&storedProject!==project){const storedIndex=storedProject.episodes.findIndex((item)=>item.id===saved.id);if(storedIndex>=0)storedProject.episodes[storedIndex]=JSON.parse(JSON.stringify(saved))}notify('分镜已保存'); return saved }
  const generatePrompt = async (project: SeriesProject, episode: Episode, shot: Shot): Promise<H3PromptPayload> => {const payload=await repository.generatePrompt(project,episode,shot);const apply=(target:Shot)=>{target.generatedPrompt=payload;target.prompt=payload.integrated_multimodal_description;target.promptRefreshReason=undefined;target.status='ready'};apply(shot);const storedShot=projects.value.find((item)=>item.id===project.id)?.episodes.find((item)=>item.id===episode.id)?.shots.find((item)=>item.id===shot.id);if(storedShot&&storedShot!==shot)apply(storedShot);return payload}
  const breakdownScript = async (project: SeriesProject, options: ScriptBreakdownOptions) => {
    const saved = await repository.breakdownScript(project, options)
    const index = projects.value.findIndex((item) => item.id === saved.id)
    if (index >= 0) projects.value[index] = saved
    notify(`已拆解为 ${saved.episodes.length} 集、${saved.episodes.reduce((sum, item) => sum + item.shots.length, 0)} 个分镜`)
    return saved
  }
  const refreshWorkflows = async (preserveMediaUrls = false) => {
    const fresh = await repository.getWorkflows()
    if (!preserveMediaUrls) {
      workflows.value = fresh
      return workflows.value
    }
    const previousById = new Map(workflows.value.map((item) => [item.id, item]))
    workflows.value = fresh.map((item) => preserveWorkflowMedia(item, previousById.get(item.id)))
    return workflows.value
  }
  const mergeWorkflowDetail = (detail: Workflow, preserveMediaUrls = false) => {
    const index = workflows.value.findIndex((item) => item.id === detail.id || Boolean(detail.taskId && item.taskId === detail.taskId))
    if (index < 0) { workflows.value.unshift(detail); return detail }
    const merged = preserveWorkflowMedia(detail, workflows.value[index], preserveMediaUrls)
    workflows.value[index] = merged
    return merged
  }
  const createWorkflow = async (project: SeriesProject, episodeIds: string[], resolutionProfile: ProductionResolutionProfile = 'project', resolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, imageResolutionProfile: ProductionResolutionProfile = '0.98', imageResolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, workflowSelections: ProductionWorkflowSelection[] = []) => {
    const item = await repository.createWorkflow(project, episodeIds, resolutionProfile, resolutionMultiple, imageResolutionProfile, imageResolutionMultiple, workflowSelections); workflows.value.unshift(item); notify('整剧工作流已创建'); return item
  }
  const generateWholeEpisode = async (project: SeriesProject, episode: Episode, idempotencyKey: string, resolutionProfile: ProductionResolutionProfile = '0.3', resolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, imageResolutionProfile: ProductionResolutionProfile = '0.98', imageResolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, workflowSelections: ProductionWorkflowSelection[] = []) => {
    const item = await repository.generateWholeEpisode(project, episode, idempotencyKey, resolutionProfile, resolutionMultiple, imageResolutionProfile, imageResolutionMultiple, workflowSelections)
    project.status = 'producing'; episode.status = 'rendering'
    for (const shot of episode.shots) shot.status = 'rendering'
    const storedProject = projects.value.find((value) => value.id === project.id)
    if (storedProject) storedProject.status = 'producing'
    const storedEpisode = storedProject?.episodes.find((value) => value.id === episode.id)
    if (storedEpisode) {
      storedEpisode.status = 'rendering'
      for (const shot of storedEpisode.shots) shot.status = 'rendering'
    }
    workflows.value.unshift(item)
    await refreshWorkflows().catch(() => undefined)
    notify(`第 ${episode.number} 集已提交 ${item.batchSize || item.batchTaskIds?.length || episode.shots.length} 个分镜任务`)
    return item
  }
  const regenerateShot = async (project: SeriesProject, episode: Episode, shot: Shot, resolutionProfile: ProductionResolutionProfile = '0.3', resolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, workflowSelections: ProductionWorkflowSelection[] = []) => {
    const result = await repository.regenerateShot(project, episode, shot, resolutionProfile, resolutionMultiple, workflowSelections)
    project.status = 'producing'
    shot.status = 'rendering'
    if (shot.mediaType === 'video') episode.status = 'rendering'
    const storedProject = projects.value.find((item) => item.id === project.id)
    if (storedProject) storedProject.status = 'producing'
    const storedEpisode = storedProject?.episodes.find((item) => item.id === episode.id)
    if (storedEpisode) {
      if (shot.mediaType === 'video') storedEpisode.status = 'rendering'
      const storedShot = storedEpisode.shots.find((item) => item.id === shot.id)
      if (storedShot) storedShot.status = 'rendering'
    }
    // The task has already been created at this point. A transient list-refresh
    // failure must not turn a successful submission into a false error message.
    await refreshWorkflows().catch(() => undefined)
    return result
  }
  const getEpisodeAssemblies = async (projectId: string): Promise<AssemblyResult[]> => repository.getEpisodeAssemblies(projectId)
  const getAssembly = async (assemblyId: string): Promise<AssemblyResult> => repository.getAssembly(assemblyId)
  const assembleEpisode = async (projectId: string, episodeId: string): Promise<AssemblyResult> => {
    const result = await repository.assembleEpisode(projectId, episodeId)
    if (result.status === 'succeeded') {
      const storedEpisode = projects.value.find((item) => item.id === projectId)?.episodes.find((item) => item.id === episodeId)
      if (storedEpisode) storedEpisode.status = 'completed'
    }
    return result
  }
  const getReleaseBundles = (projectId: string): Promise<ReleaseBundle[]> => repository.getReleaseBundles(projectId)
  const createReleaseBundle = (projectId: string, payload: { title?: string; notes?: string } = {}) => repository.createReleaseBundle(projectId, payload)
  const proposeReleaseSelection = (projectId: string): Promise<ReleaseSelection> => repository.proposeReleaseSelection(projectId)
  const updateReleaseBundle = (bundleId: string, payload: { title?: string; notes?: string; selection?: ReleaseSelection }) => repository.updateReleaseBundle(bundleId, payload)
  const sealReleaseBundle = (bundleId: string) => repository.sealReleaseBundle(bundleId)
  const releaseReleaseBundle = (bundleId: string) => repository.releaseReleaseBundle(bundleId)
  const verifyReleaseBundle = (bundleId: string) => repository.verifyReleaseBundle(bundleId)
  const saveSettings = async (value: ComfySettings) => { settings.value = await repository.saveSettings(value); notify('ComfyUI 设置已保存') }
  const projectById = computed(() => (id: string) => projects.value.find((item) => item.id === id))
  return { styles, assets, projects, workflows, settings, loading, error, toast, ready, projectById, initialize, resetSessionState, refreshAssets, refreshProjects, createImageAsset, updateAssetMetadata, generateBibleDraftFromAssets, uploadReferenceAsset, createProject, saveProject, ensureCanonicalGlossary, addEpisode, deleteEpisode, saveEpisode, breakdownScript, generatePrompt, refreshWorkflows, mergeWorkflowDetail, createWorkflow, generateWholeEpisode, regenerateShot, getEpisodeAssemblies, getAssembly, assembleEpisode, getReleaseBundles, createReleaseBundle, proposeReleaseSelection, updateReleaseBundle, sealReleaseBundle, releaseReleaseBundle, verifyReleaseBundle, saveSettings, notify }
})
