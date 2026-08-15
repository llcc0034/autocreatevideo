import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { repository } from '@/services/repository'
import { applyProjectEpisodeSummary, buildProjectRedoPayload, createSessionEpoch, SessionInvalidatedError } from '@/services/projectManagement'
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
  let initializeRequest: Promise<void> | null = null
  const projectDetailRequests = new Map<string, Promise<SeriesProject>>()
  const sessionEpoch = createSessionEpoch()
  let toastSequence = 0
  const captureSessionEpoch = () => sessionEpoch.capture()

  const assertCurrentSession = (epoch: number) => {
    if (!sessionEpoch.isCurrent(epoch)) throw new SessionInvalidatedError()
  }

  const notify = (message: string, type: 'success' | 'error' | 'info' = 'success') => {
    const requestEpoch = sessionEpoch.capture()
    const sequence = ++toastSequence
    toast.value = { message, type }
    window.setTimeout(() => {
      if (sessionEpoch.isCurrent(requestEpoch) && toastSequence === sequence) toast.value = null
    }, 3200)
  }
  const resetSessionState = () => {
    sessionEpoch.invalidate()
    toastSequence += 1
    styles.value = []; assets.value = []; projects.value = []; workflows.value = []; settings.value = null
    loading.value = false; error.value = ''; toast.value = null; ready.value = false
    initializeRequest = null
    projectDetailRequests.clear()
  }
  const initialize = async () => {
    if (ready.value) return
    if (initializeRequest) return initializeRequest
    const requestEpoch = sessionEpoch.capture()
    initializeRequest = (async () => {
      assertCurrentSession(requestEpoch)
      loading.value = true; error.value = ''
      try {
        // Project summaries are loaded once; full Bible/episode data is hydrated on demand.
        const nextStyles = await repository.getStyles()
        assertCurrentSession(requestEpoch)
        const nextProjects = await repository.getProjects()
        assertCurrentSession(requestEpoch)
        const [nextWorkflows, nextSettings] = await Promise.all([repository.getWorkflows(), repository.getSettings()])
        assertCurrentSession(requestEpoch)
        styles.value = nextStyles
        projects.value = nextProjects
        workflows.value = nextWorkflows
        settings.value = nextSettings
        ready.value = true
      } catch (e) {
        assertCurrentSession(requestEpoch)
        ready.value = false
        error.value = e instanceof Error ? e.message : '加载工作台失败'
      } finally {
        if (sessionEpoch.isCurrent(requestEpoch)) {
          loading.value = false
          initializeRequest = null
        }
      }
    })()
    return initializeRequest
  }
  const refreshAssets = async (filters?: { style?: string; type?: string; search?: string }) => {
    const requestEpoch = sessionEpoch.capture()
    const fresh = await repository.getAssets(filters)
    assertCurrentSession(requestEpoch)
    assets.value = fresh
    return assets.value
  }
  const createImageAsset = async (payload: { file: File; kind: BibleDraftEntityType|'reference'; name: string; description: string; promptHint: string; styleId?: string }) => { const requestEpoch=sessionEpoch.capture();const item=await repository.createImageAsset(payload);assertCurrentSession(requestEpoch);assets.value=[item,...assets.value.filter((asset)=>asset.id!==item.id)];notify('图片素材已入库');return item }
  const updateAssetMetadata = async (assetId:string,payload:{name:string;description:string;promptHint:string;kind:BibleDraftEntityType|'reference';styleId?:string}) => {const requestEpoch=sessionEpoch.capture();const item=await repository.updateAssetMetadata(assetId,payload);assertCurrentSession(requestEpoch);const index=assets.value.findIndex((asset)=>asset.id===assetId);if(index>=0)assets.value[index]=item;else assets.value.unshift(item);notify('素材说明已保存');return item}
  const deleteAsset = async (assetId:string) => {const requestEpoch=sessionEpoch.capture();await repository.deleteAsset(assetId);assertCurrentSession(requestEpoch);assets.value=assets.value.filter((asset)=>asset.id!==assetId);notify('素材已删除')}
  const generateBibleDraftFromAssets = (projectId:string,payload:{entityType:BibleDraftEntityType;outputLanguage:BibleDraftLanguage;asset:Asset;attempt?:number}):Promise<BibleAssetDraftResult> => repository.generateBibleDraftFromAssets(projectId,payload)
  const refreshProjects = async () => {
    const requestEpoch = sessionEpoch.capture()
    try {
      const summaries = await repository.getProjects()
      assertCurrentSession(requestEpoch)
      const hydrated = new Map(projects.value.filter((project) => project.detailLoaded).map((project) => [project.id, project]))
      projects.value = summaries.map((summary) => {
        const detail = hydrated.get(summary.id)
        return detail ? { ...detail, ...summary, bible: detail.bible, episodes: detail.episodes, detailLoaded: true } : summary
      })
      error.value = ''
      return projects.value
    } catch (e) {
      assertCurrentSession(requestEpoch)
      error.value = e instanceof Error ? e.message : '项目列表加载失败'
      throw e
    }
  }
  const loadProject = async (projectId: string, force = false) => {
    const requestEpoch = sessionEpoch.capture()
    const existing = projects.value.find((project) => project.id === projectId)
    if (existing?.detailLoaded && !force) return existing
    const pending = projectDetailRequests.get(projectId)
    if (pending) return pending
    const request = repository.getProject(projectId).then((detail) => {
      if (!sessionEpoch.isCurrent(requestEpoch)) throw new SessionInvalidatedError()
      const index = projects.value.findIndex((project) => project.id === projectId)
      if (index >= 0) projects.value[index] = detail
      else projects.value.unshift(detail)
      return detail
    }).finally(() => {
      if (projectDetailRequests.get(projectId) === request) projectDetailRequests.delete(projectId)
    })
    projectDetailRequests.set(projectId, request)
    return request
  }
  const uploadReferenceAsset = async (file:File,kind:'reference_video'|'reference_audio',styleId?:string) => {const requestEpoch=sessionEpoch.capture();const item=await repository.uploadReferenceAsset(file,kind,styleId);assertCurrentSession(requestEpoch);assets.value=[item,...assets.value.filter((asset)=>asset.id!==item.id)];notify(`${kind==='reference_video'?'参考视频':'参考音频'}已上传`);return item}
  const createProject = async (payload: Partial<SeriesProject>) => { const requestEpoch=sessionEpoch.capture();const item = await repository.createProject(payload);assertCurrentSession(requestEpoch);projects.value.unshift(item);notify('连续剧项目已创建');return item }
  const saveProject = async (project: SeriesProject) => { const requestEpoch=sessionEpoch.capture();const saved = await repository.updateProject(project);assertCurrentSession(requestEpoch);const index = projects.value.findIndex((item) => item.id === saved.id);if(index >= 0)projects.value[index] = saved;else projects.value.unshift(saved);notify('项目设定已保存');return saved }
  const setProjectCover = async (project: SeriesProject, file: File | null) => { const requestEpoch=sessionEpoch.capture();const saved=await repository.setProjectCover(project,file);assertCurrentSession(requestEpoch);const index=projects.value.findIndex((item)=>item.id===saved.id);if(index>=0)projects.value[index]=saved;else projects.value.unshift(saved);notify(file?'连续剧封面已更新':'连续剧封面已移除','info');return saved }
  const setProjectFavorite = async (project: SeriesProject, favorite: boolean) => { const requestEpoch=sessionEpoch.capture();const saved = await repository.setProjectFavorite(project, favorite);assertCurrentSession(requestEpoch);const index = projects.value.findIndex((item) => item.id === saved.id);if(index >= 0)projects.value[index] = saved;else projects.value.unshift(saved);notify(favorite ? '项目已收藏' : '已取消收藏', 'info');return saved }
  const duplicateProject = async (project: SeriesProject) => { const requestEpoch=sessionEpoch.capture();const item = await repository.createProject(buildProjectRedoPayload(project));assertCurrentSession(requestEpoch);projects.value.unshift(item);notify('已创建独立重做草稿，原项目保持不变');return item }
  const deleteProject = async (projectId: string) => { const requestEpoch=sessionEpoch.capture();await repository.deleteProject(projectId);assertCurrentSession(requestEpoch);projects.value = projects.value.filter((item) => item.id !== projectId);workflows.value = workflows.value.filter((item) => item.projectId !== projectId);notify('连续剧项目已删除', 'info') }
  const ensureCanonicalGlossary = async (
    project: SeriesProject,
    options: { entries?: Array<{ scopeType: CanonicalGlossaryScope; scopeId: string }>; includeAllMissing?: boolean; ensureVisualIdentityTranslation?: boolean } = {},
  ): Promise<CanonicalGlossaryEnsureResult> => {
    const requestEpoch = sessionEpoch.capture()
    const result = await repository.ensureCanonicalGlossary(project.id, options)
    assertCurrentSession(requestEpoch)
    const authoritative = acceptAuthoritativeCanonicalGlossary(result.translationGlossary)
    project.translationGlossary = authoritative
    const stored = projects.value.find((item) => item.id === project.id)
    if (stored && stored !== project) stored.translationGlossary = { ...authoritative }
    return { ...result, translationGlossary: authoritative }
  }
  const addEpisode = async (project: SeriesProject, payload: { title: string; synopsis: string; script?: string }) => { const requestEpoch=sessionEpoch.capture();const item = await repository.addEpisode(project.id, payload);assertCurrentSession(requestEpoch);project.episodes.push(item);applyProjectEpisodeSummary(project);await saveProject(project);assertCurrentSession(requestEpoch);notify(`第 ${item.number} 集已加入`);return item }
  const deleteEpisode = async (project: SeriesProject, episodeId: string) => { const requestEpoch=sessionEpoch.capture();await repository.deleteEpisode(project.id, episodeId);assertCurrentSession(requestEpoch);project.episodes = project.episodes.filter((item) => item.id !== episodeId);applyProjectEpisodeSummary(project);await saveProject(project);assertCurrentSession(requestEpoch);notify('剧集已删除', 'info') }
  const saveEpisode = async (project: SeriesProject, episode: Episode) => { const requestEpoch=sessionEpoch.capture();const saved = await repository.saveEpisode(project, episode);assertCurrentSession(requestEpoch);const index = project.episodes.findIndex((item) => item.id === saved.id);if(index >= 0)project.episodes[index] = saved;applyProjectEpisodeSummary(project);const storedProject=projects.value.find((item)=>item.id===project.id);if(storedProject&&storedProject!==project){const storedIndex=storedProject.episodes.findIndex((item)=>item.id===saved.id);if(storedIndex>=0)storedProject.episodes[storedIndex]=JSON.parse(JSON.stringify(saved));applyProjectEpisodeSummary(storedProject)}notify('分镜已保存');return saved }
  const saveShot = async (project: SeriesProject, episode: Episode, shot: Shot) => {
    const requestEpoch = sessionEpoch.capture()
    const previousId = shot.id
    const saved = await repository.saveShot(project, episode, shot)
    assertCurrentSession(requestEpoch)
    const updatedEpisodes = new Set<Episode>()
    const replace = (target: Episode | undefined, value: Shot) => {
      if (!target || updatedEpisodes.has(target)) return
      updatedEpisodes.add(target)
      const index = target.shots.findIndex((item) => item.id === previousId || item.id === saved.id)
      if (index >= 0) target.shots[index] = value; else target.shots.push(value)
    }
    replace(episode, saved)
    const projectEpisode = project.episodes.find((item) => item.id === episode.id)
    replace(projectEpisode, JSON.parse(JSON.stringify(saved)))
    applyProjectEpisodeSummary(project)
    const storedProject = projects.value.find((item) => item.id === project.id)
    const storedEpisode = storedProject?.episodes.find((item) => item.id === episode.id)
    replace(storedEpisode, JSON.parse(JSON.stringify(saved)))
    if (storedProject && storedProject !== project) applyProjectEpisodeSummary(storedProject)
    notify(`镜头 ${String(saved.order).padStart(2, '0')} 已保存`)
    return saved
  }
  const generatePrompt = async (project: SeriesProject, episode: Episode, shot: Shot): Promise<H3PromptPayload> => {const requestEpoch=sessionEpoch.capture();const payload=await repository.generatePrompt(project,episode,shot);assertCurrentSession(requestEpoch);const apply=(target:Shot)=>{target.generatedPrompt=payload;target.prompt=payload.integrated_multimodal_description;target.promptRefreshReason=undefined;target.status='ready'};apply(shot);const storedShot=projects.value.find((item)=>item.id===project.id)?.episodes.find((item)=>item.id===episode.id)?.shots.find((item)=>item.id===shot.id);if(storedShot&&storedShot!==shot)apply(storedShot);return payload}
  const breakdownScript = async (project: SeriesProject, options: ScriptBreakdownOptions) => {
    const requestEpoch = sessionEpoch.capture()
    const result = await repository.breakdownScript(project, options)
    assertCurrentSession(requestEpoch)
    const saved = result.project
    const index = projects.value.findIndex((item) => item.id === saved.id)
    if (index >= 0) projects.value[index] = saved
    const clearedProductionRows = result.cleared.generationTasks
      + result.cleared.assemblyJobs
      + result.cleared.promptArtifacts
      + result.cleared.generatedWorkflows
      + result.cleared.creativeDrafts
      + result.cleared.shotDetailDrafts
      + result.cleared.shotDetailBatches
      + result.cleared.shotDetailBatchItems
      + result.cleared.promptFieldTranslations
      + result.cleared.draftReleaseBundles
    notify(result.cleared.episodes
      ? `已清理旧 ${result.cleared.episodes} 集、${result.cleared.shots} 镜及 ${clearedProductionRows} 条派生记录，并重建为 ${result.counts.episodes} 集、${result.counts.shots} 镜`
      : `已拆解为 ${result.counts.episodes} 集、${result.counts.shots} 个分镜`)
    if (result.warnings.length) {
      const preview = result.warnings.slice(0, 3).join('；')
      notify(`拆解完成，${result.warnings.length} 项已按默认值处理：${preview}${result.warnings.length > 3 ? '……' : ''}`, 'info')
    }
    return saved
  }
  const refreshWorkflows = async (preserveMediaUrls = false) => {
    const requestEpoch = sessionEpoch.capture()
    const fresh = await repository.getWorkflows()
    assertCurrentSession(requestEpoch)
    if (!preserveMediaUrls) {
      workflows.value = fresh
      return workflows.value
    }
    const previousById = new Map(workflows.value.map((item) => [item.id, item]))
    workflows.value = fresh.map((item) => preserveWorkflowMedia(item, previousById.get(item.id)))
    return workflows.value
  }
  const mergeWorkflowDetail = (detail: Workflow, preserveMediaUrls = false, requestEpoch = sessionEpoch.capture()) => {
    assertCurrentSession(requestEpoch)
    const index = workflows.value.findIndex((item) => item.id === detail.id || Boolean(detail.taskId && item.taskId === detail.taskId))
    if (index < 0) { workflows.value.unshift(detail); return detail }
    const merged = preserveWorkflowMedia(detail, workflows.value[index], preserveMediaUrls)
    workflows.value[index] = merged
    return merged
  }
  const createWorkflow = async (project: SeriesProject, episodeIds: string[], resolutionProfile: ProductionResolutionProfile = 'project', resolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, imageResolutionProfile: ProductionResolutionProfile = '0.98', imageResolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, workflowSelections: ProductionWorkflowSelection[] = [], extendedGenerationConfirmed = false, idempotencyKey = '') => {
    const requestEpoch = sessionEpoch.capture()
    const item = await repository.createWorkflow(project, episodeIds, resolutionProfile, resolutionMultiple, imageResolutionProfile, imageResolutionMultiple, workflowSelections, extendedGenerationConfirmed, idempotencyKey)
    assertCurrentSession(requestEpoch)
    workflows.value.unshift(item); notify('整剧工作流已创建'); return item
  }
  const generateWholeEpisode = async (project: SeriesProject, episode: Episode, idempotencyKey: string, resolutionProfile: ProductionResolutionProfile = '0.3', resolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, imageResolutionProfile: ProductionResolutionProfile = '0.98', imageResolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, workflowSelections: ProductionWorkflowSelection[] = [], extendedGenerationConfirmed = false) => {
    const requestEpoch = sessionEpoch.capture()
    const item = await repository.generateWholeEpisode(project, episode, idempotencyKey, resolutionProfile, resolutionMultiple, imageResolutionProfile, imageResolutionMultiple, workflowSelections, extendedGenerationConfirmed)
    assertCurrentSession(requestEpoch)
    project.status = 'producing'; episode.status = 'rendering'
    for (const shot of episode.shots) shot.status = 'rendering'
    const storedProject = projects.value.find((value) => value.id === project.id)
    if (storedProject) storedProject.status = 'producing'
    const storedEpisode = storedProject?.episodes.find((value) => value.id === episode.id)
    if (storedEpisode) {
      storedEpisode.status = 'rendering'
      for (const shot of storedEpisode.shots) shot.status = 'rendering'
    }
    applyProjectEpisodeSummary(project)
    if (storedProject && storedProject !== project) applyProjectEpisodeSummary(storedProject)
    workflows.value.unshift(item)
    await refreshWorkflows().catch(() => undefined)
    assertCurrentSession(requestEpoch)
    notify(`第 ${episode.number} 集已提交 ${item.batchSize || item.batchTaskIds?.length || episode.shots.length} 个分镜任务`)
    return item
  }
  const regenerateShot = async (project: SeriesProject, episode: Episode, shot: Shot, resolutionProfile: ProductionResolutionProfile = '0.3', resolutionMultiple = DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE, workflowSelections: ProductionWorkflowSelection[] = [], extendedGenerationConfirmed = false) => {
    const requestEpoch = sessionEpoch.capture()
    const result = await repository.regenerateShot(project, episode, shot, resolutionProfile, resolutionMultiple, workflowSelections, extendedGenerationConfirmed)
    assertCurrentSession(requestEpoch)
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
    applyProjectEpisodeSummary(project)
    if (storedProject && storedProject !== project) applyProjectEpisodeSummary(storedProject)
    // The task has already been created at this point. A transient list-refresh
    // failure must not turn a successful submission into a false error message.
    await refreshWorkflows().catch(() => undefined)
    assertCurrentSession(requestEpoch)
    return result
  }
  const getEpisodeAssemblies = async (projectId: string): Promise<AssemblyResult[]> => repository.getEpisodeAssemblies(projectId)
  const getAssembly = async (assemblyId: string): Promise<AssemblyResult> => repository.getAssembly(assemblyId)
  const assembleEpisode = async (projectId: string, episodeId: string): Promise<AssemblyResult> => {
    const requestEpoch = sessionEpoch.capture()
    const result = await repository.assembleEpisode(projectId, episodeId)
    assertCurrentSession(requestEpoch)
    if (result.status === 'succeeded') {
      const storedProject = projects.value.find((item) => item.id === projectId)
      const storedEpisode = storedProject?.episodes.find((item) => item.id === episodeId)
      if (storedEpisode) {
        storedEpisode.status = 'completed'
        if (storedProject) applyProjectEpisodeSummary(storedProject)
      }
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
  const saveSettings = async (value: ComfySettings) => { const requestEpoch=sessionEpoch.capture();const saved=await repository.saveSettings(value);assertCurrentSession(requestEpoch);settings.value=saved;notify('ComfyUI 设置已保存') }
  const projectById = computed(() => (id: string) => projects.value.find((item) => item.id === id))
  return { styles, assets, projects, workflows, settings, loading, error, toast, ready, projectById, captureSessionEpoch, initialize, loadProject, resetSessionState, refreshAssets, refreshProjects, createImageAsset, updateAssetMetadata, deleteAsset, generateBibleDraftFromAssets, uploadReferenceAsset, createProject, saveProject, setProjectCover, setProjectFavorite, duplicateProject, deleteProject, ensureCanonicalGlossary, addEpisode, deleteEpisode, saveEpisode, saveShot, breakdownScript, generatePrompt, refreshWorkflows, mergeWorkflowDetail, createWorkflow, generateWholeEpisode, regenerateShot, getEpisodeAssemblies, getAssembly, assembleEpisode, getReleaseBundles, createReleaseBundle, proposeReleaseSelection, updateReleaseBundle, sealReleaseBundle, releaseReleaseBundle, verifyReleaseBundle, saveSettings, notify }
})
