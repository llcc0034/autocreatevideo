import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { repository } from '@/services/repository'
import { applyProjectEpisodeSummary, buildProjectRedoPayload, createSessionEpoch, SessionInvalidatedError } from '@/services/projectManagement'
import type { AssemblyResult, Asset, AssetGenerationJob, AssetGenerationMediaType, AssetGenerationResolutionOutcome, BibleDraftEntityType, Episode, ReleaseBundle, SeriesProject, Shot, StylePreset, Workflow } from '@/types'

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
  const stableAssetKey = (asset: TaskAssets[number]) => `${asset.itemKey}:${asset.sourceSha256}:${asset.checksum}`
  const preserveAssetPreviews = (nextAssets: TaskAssets, previousAssets: TaskAssets): TaskAssets => {
    if (!preserveSignedUrls || next.inputParameters?.request_sha256 !== previous.inputParameters?.request_sha256 || !nextAssets.length || !previousAssets.length) return nextAssets
    const previousByKey = new Map(previousAssets.map(asset => [stableAssetKey(asset), asset]))
    return nextAssets.map(asset => {
      const stable = previousByKey.get(stableAssetKey(asset))
      return !asset.previewUrl && stable?.previewUrl ? { ...asset, previewUrl: stable.previewUrl } : asset
    })
  }
  const referenceAssets = preserveAssetPreviews(next.referenceAssets, previous.referenceAssets)
  const contextMediaUses = preserveAssetPreviews(next.contextMediaUses, previous.contextMediaUses)
  return { ...next, outputMedia, referenceAssets, contextMediaUses }
}

const mergePolledWorkflows = (fresh: Workflow[], previous: Workflow[], preserveMediaUrls: boolean): Workflow[] => {
  const previousById = new Map(previous.map(item => [item.id, item]))
  // Only server-returned tasks exist here. Pending work belongs to StageRun,
  // not synthesized batch rows or stale local task caches.
  return fresh.map(item => preserveMediaUrls ? preserveWorkflowMedia(item, previousById.get(item.id)) : item)
}

export const useStudioStore = defineStore('studio', () => {
  const styles = ref<StylePreset[]>([])
  const assets = ref<Asset[]>([])
  const assetGenerationJobs = ref<AssetGenerationJob[]>([])
  let assetRefreshSequence = 0
  const projects = ref<SeriesProject[]>([])
  const workflows = ref<Workflow[]>([])
  const loading = ref(false)
  const error = ref('')
  const toast = ref<{ message: string; type: 'success' | 'error' | 'info' } | null>(null)
  const ready = ref(false)
  let initializeRequest: Promise<void> | null = null
  let stylesRequest: Promise<void> | null = null
  let navigationRequest: Promise<void> | null = null
  let stylesReady = false
  let navigationReady = false
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
    assetRefreshSequence += 1
    styles.value = []; assets.value = []; assetGenerationJobs.value = []; projects.value = []; workflows.value = []
    loading.value = false; error.value = ''; toast.value = null; ready.value = false
    initializeRequest = null
    stylesRequest = null; navigationRequest = null; stylesReady = false; navigationReady = false
    projectDetailRequests.clear()
  }
  const loadStyles = async (force = false) => {
    if (force) {
      if (stylesRequest) await stylesRequest
      stylesReady = false
    }
    if (stylesReady) return
    if (stylesRequest) return stylesRequest
    const epoch = sessionEpoch.capture()
    stylesRequest = (async () => {
      try {
        const rows = await repository.getStyles()
        assertCurrentSession(epoch)
        styles.value = rows; stylesReady = true
      } finally { if (sessionEpoch.isCurrent(epoch)) stylesRequest = null }
    })()
    return stylesRequest
  }
  const initializeNavigation = async () => {
    if (navigationReady) return
    if (navigationRequest) return navigationRequest
    const epoch = sessionEpoch.capture()
    navigationRequest = (async () => {
      try {
        await loadStyles()
        assertCurrentSession(epoch)
        const rows = await repository.getProjects()
        assertCurrentSession(epoch)
        projects.value = rows; navigationReady = true
      } finally { if (sessionEpoch.isCurrent(epoch)) navigationRequest = null }
    })()
    return navigationRequest
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
        await initializeNavigation()
        assertCurrentSession(requestEpoch)
        const nextWorkflows = await repository.getWorkflows()
        assertCurrentSession(requestEpoch)
        workflows.value = mergePolledWorkflows(nextWorkflows, [], false)
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
    const requestSequence = ++assetRefreshSequence
    const fresh = await repository.getAssets(filters)
    assertCurrentSession(requestEpoch)
    if (requestSequence !== assetRefreshSequence) return assets.value
    assets.value = fresh
    return assets.value
  }
  const refreshAsset = async (id:string, mergeIntoList=true) => {const requestEpoch=sessionEpoch.capture();const item=await repository.getAsset(id);assertCurrentSession(requestEpoch);if(mergeIntoList){const index=assets.value.findIndex((asset)=>asset.id===id);if(index>=0)assets.value[index]=item;else assets.value.unshift(item)}return item}
  const createAsset = async (payload: { file: File; mediaType: AssetGenerationMediaType; imageCategory?: BibleDraftEntityType|'reference'; name: string; description: string; promptHint: string; styleId?: string }) => { const requestEpoch=sessionEpoch.capture();const item=await repository.createAsset(payload);assertCurrentSession(requestEpoch);assets.value=[item,...assets.value.filter((asset)=>asset.id!==item.id)];notify('素材已入库');return item }
  const refreshAssetGenerationJobs = async () => {const requestEpoch=sessionEpoch.capture();const rows=await repository.getAssetGenerationJobs();assertCurrentSession(requestEpoch);assetGenerationJobs.value=rows;return rows}
  const cancelAssetGenerationJob = async(id:string)=>{const requestEpoch=sessionEpoch.capture();const job=await repository.cancelAssetGenerationJob(id);assertCurrentSession(requestEpoch);assetGenerationJobs.value=assetGenerationJobs.value.map((item)=>item.id===id?job:item);notify('已提交取消请求','info');return job}
  const resolveUncertainAssetGenerationJob = async(id:string,payload:{outcome:AssetGenerationResolutionOutcome;note:string;providerRequestId?:string;actualCost?:string|null;costCurrency?:string;idempotencyKey:string})=>{const requestEpoch=sessionEpoch.capture();await repository.resolveUncertainAssetGenerationJob(id,payload);assertCurrentSession(requestEpoch);const rows=await repository.getAssetGenerationJobs();assertCurrentSession(requestEpoch);assetGenerationJobs.value=rows;notify('素材生成任务消歧结论已记录');return rows.find((item)=>item.id===id)}
  const updateAssetMetadata = async (assetId:string,payload:{name:string;description:string;promptHint:string;kind?:BibleDraftEntityType|'reference';styleId?:string}) => {const requestEpoch=sessionEpoch.capture();const item=await repository.updateAssetMetadata(assetId,payload);assertCurrentSession(requestEpoch);const index=assets.value.findIndex((asset)=>asset.id===assetId);if(index>=0)assets.value[index]=item;else assets.value.unshift(item);notify('素材说明已保存');return item}
  const deleteAsset = async (assetId:string) => {const requestEpoch=sessionEpoch.capture();await repository.deleteAsset(assetId);assertCurrentSession(requestEpoch);assets.value=assets.value.filter((asset)=>asset.id!==assetId);notify('素材已删除')}
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
  const refreshWorkflows = async (preserveMediaUrls = false) => {
    const requestEpoch = sessionEpoch.capture()
    const fresh = await repository.getWorkflows()
    assertCurrentSession(requestEpoch)
    workflows.value = mergePolledWorkflows(fresh, workflows.value, preserveMediaUrls)
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
  const getEpisodeAssemblies = async (projectId: string): Promise<AssemblyResult[]> => repository.getEpisodeAssemblies(projectId)
  const getAssembly = async (assemblyId: string): Promise<AssemblyResult> => repository.getAssembly(assemblyId)
  const getReleaseBundles = (projectId: string): Promise<ReleaseBundle[]> => repository.getReleaseBundles(projectId)
  const verifyReleaseBundle = (bundleId: string) => repository.verifyReleaseBundle(bundleId)
  const projectById = computed(() => (id: string) => projects.value.find((item) => item.id === id))
  return { styles, assets, assetGenerationJobs, projects, workflows, loading, error, toast, ready, projectById, captureSessionEpoch, initialize, initializeNavigation, loadStyles, loadProject, resetSessionState, refreshAssets, refreshAsset, refreshAssetGenerationJobs, cancelAssetGenerationJob, resolveUncertainAssetGenerationJob, refreshProjects, createAsset, updateAssetMetadata, deleteAsset, uploadReferenceAsset, createProject, saveProject, setProjectCover, setProjectFavorite, duplicateProject, deleteProject, addEpisode, deleteEpisode, saveEpisode, saveShot, refreshWorkflows, mergeWorkflowDetail, getEpisodeAssemblies, getAssembly, getReleaseBundles, verifyReleaseBundle, notify }
})
