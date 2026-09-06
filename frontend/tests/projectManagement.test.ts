import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

import {
  buildProjectRedoPayload,
  createSessionEpoch,
  formatProjectUpdatedAt,
  isProjectDeleteConfirmed,
  PROJECT_COVER_ACCEPT,
  projectCoverFileError,
  projectEpisodeSummary,
  projectCompletion,
  safeProjectTimestamp,
  SessionInvalidatedError,
  sortProjectsForManagement,
} from '../src/services/projectManagement.ts'
import {
  clearRouteModuleRecovery,
  ROUTE_MODULE_RECOVERY_KEY,
  resolveRouteRecoveryStorage,
  routeModuleRecoveryAction,
} from '../src/services/routeModuleRecovery.ts'
import type { SeriesProject } from '../src/types/index.ts'

const makeProject = (patch: Partial<SeriesProject> = {}): SeriesProject => ({
  id: 'project-1',
  title: '听雨不归人',
  logline: '一名剑客在雨夜追查旧案。',
  masterScript: '第一集：雨夜试剑。',
  isFavorite: false,
  continuitySummary: '保留旧项目的连续性说明',
  translationGlossary: { 玄纹剑: 'Xuanwen Sword' },
  styleId: 'wuxia',
  status: 'writing',
  language: 'Chinese',
  aspectRatio: '9:16',
  resolution: '720p',
  width: 768,
  height: 1344,
  defaultMode: 'Ref2VA',
  bible: {
    characters: [{ id: 'character-1', name: '沈砚', description: '剑客', visualPrompt: 'gray robe', referenceAssetIds: [], locked: true }],
    scenes: [],
    props: [],
    negativePrompt: 'watermark',
    colorScript: 'cool cyan',
  },
  episodes: [],
  createdAt: '2026-08-01T00:00:00Z',
  updatedAt: '2026-08-10T00:00:00Z',
  ...patch,
})

const deferred = <T>() => {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve
    reject = onReject
  })
  return { promise, resolve, reject }
}

test('new project title does not impose a browser-only 60 character cap', () => {
  const studio = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  const titleInput = studio.match(/<input v-model="newProject\.title"[^>]*>/)?.[0] ?? ''

  assert.ok(titleInput, 'new project title input should remain available')
  assert.doesNotMatch(titleInput, /maxlength=/)
  assert.doesNotMatch(studio, /newProject\.title\.length\s*}}\/60/)
})

test('project sorting pins favorites, then uses newest valid update without mutating input', () => {
  const projects = [
    makeProject({ id: 'ordinary-new', updatedAt: '2026-08-12T00:00:00Z' }),
    makeProject({ id: 'favorite-old', isFavorite: true, updatedAt: '2026-08-02T00:00:00Z' }),
    makeProject({ id: 'favorite-new', isFavorite: true, updatedAt: '2026-08-11T00:00:00Z' }),
    makeProject({ id: 'ordinary-invalid', updatedAt: 'not-a-date' }),
  ]
  const originalOrder = projects.map((project) => project.id)

  assert.deepEqual(sortProjectsForManagement(projects).map((project) => project.id), [
    'favorite-new', 'favorite-old', 'ordinary-new', 'ordinary-invalid',
  ])
  assert.deepEqual(projects.map((project) => project.id), originalOrder)
})

test('redo payload is an independent draft with an explicit field allowlist', () => {
  const source = makeProject({
    title: `  ${'长'.repeat(220)}  `,
    coverAssetId: 'cover-asset-1',
    coverUrl: '/api/v1/media/cover-token/',
    episodes: [{
      id: 'episode-1', number: 1, title: '雨夜', synopsis: '开场', status: 'completed',
      audioEnabled: true, shots: [], createdAt: '2026-08-01T00:00:00Z',
    }],
  })
  const payload = buildProjectRedoPayload(source)

  assert.equal(Array.from(payload.title).length, 225)
  assert.ok(payload.title.endsWith(' · 重做'))
  assert.equal(payload.logline, source.logline)
  assert.equal(payload.masterScript, source.masterScript)
  assert.equal(payload.styleId, source.styleId)
  assert.equal(payload.language, source.language)
  assert.equal(payload.aspectRatio, source.aspectRatio)
  assert.equal(payload.resolution, source.resolution)
  assert.equal(payload.defaultMode, source.defaultMode)
  assert.equal(payload.status, 'draft')
  assert.equal(payload.isFavorite, false)
  assert.equal(payload.continuitySummary, '')
  assert.deepEqual(payload.translationGlossary, {})
  assert.deepEqual(payload.bible, { characters: [], scenes: [], props: [], negativePrompt: '', colorScript: '' })
  assert.deepEqual(Object.keys(payload).sort(), [
    'aspectRatio', 'bible', 'continuitySummary', 'defaultMode', 'height', 'isFavorite', 'language', 'logline', 'masterScript',
    'resolution', 'status', 'styleId', 'title', 'translationGlossary', 'width',
  ])
  assert.ok(!('id' in payload))
  assert.ok(!('episodes' in payload))
  assert.ok(!('createdAt' in payload))
  assert.ok(!('coverAssetId' in payload))
  assert.ok(!('coverUrl' in payload))
  assert.notEqual(payload.bible.characters, source.bible.characters)
})

test('cover file validation leaves configurable byte limits to the backend', () => {
  assert.equal(PROJECT_COVER_ACCEPT, 'image/jpeg,image/png,image/webp')
  assert.equal(projectCoverFileError({ name: 'cover.webp', type: 'image/webp', size: 10 * 1024 * 1024 }), null)
  assert.match(projectCoverFileError({ name: 'cover.gif', type: 'image/gif', size: 100 }) || '', /JPEG、PNG 或 WebP/)
  assert.match(projectCoverFileError({ name: 'empty.png', type: 'image/png', size: 0 }) || '', /文件为空/)
  assert.equal(projectCoverFileError({ name: 'large.jpg', type: 'image/jpeg', size: 100 * 1024 * 1024 }), null)
})

test('delete confirmation trims both values but otherwise requires an exact match', () => {
  assert.equal(isProjectDeleteConfirmed(' 听雨不归人 ', '  听雨不归人  '), true)
  assert.equal(isProjectDeleteConfirmed('听雨不归人', '听雨不归'), false)
  assert.equal(isProjectDeleteConfirmed('AutoPrompt', 'autoprompt'), false)
  assert.equal(isProjectDeleteConfirmed('   ', '   '), false)
})

test('route module recovery reloads once, then reports a repeated lazy import failure', () => {
  const values = new Map<string, string>()
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) },
  }
  const error = new TypeError('Failed to fetch dynamically imported module: http://127.0.0.1:5173/src/views/SeriesStudioView.vue')

  assert.equal(routeModuleRecoveryAction(error, '/projects/new', storage), 'reload')
  assert.equal(values.get(ROUTE_MODULE_RECOVERY_KEY), '/projects/new')
  assert.equal(routeModuleRecoveryAction(error, '/projects/new', storage), 'report')
  assert.equal(values.get(ROUTE_MODULE_RECOVERY_KEY), '/projects/new')

  clearRouteModuleRecovery(storage)
  assert.equal(values.has(ROUTE_MODULE_RECOVERY_KEY), false)
})

test('route module recovery ignores unrelated route errors and fails closed without storage', () => {
  const unavailableStorage = {
    getItem: () => { throw new Error('blocked') },
    setItem: () => { throw new Error('blocked') },
    removeItem: () => { throw new Error('blocked') },
  }

  assert.equal(routeModuleRecoveryAction(new Error('Navigation aborted'), '/projects/new', unavailableStorage), 'ignore')
  assert.equal(routeModuleRecoveryAction(new Error('Importing a module script failed.'), '/projects/new', unavailableStorage), 'report')
  assert.doesNotThrow(() => clearRouteModuleRecovery(unavailableStorage))
  const inaccessibleStorage = resolveRouteRecoveryStorage(() => { throw new Error('SecurityError') })
  assert.equal(inaccessibleStorage, undefined)
  assert.equal(routeModuleRecoveryAction(new Error('Importing a module script failed.'), '/projects/new', inaccessibleStorage), 'report')
  assert.doesNotThrow(() => clearRouteModuleRecovery(inaccessibleStorage))
})

test('session epoch invalidates asynchronous results captured before reset', async () => {
  const epoch = createSessionEpoch()
  const firstSession = epoch.capture()
  assert.equal(epoch.isCurrent(firstSession), true)

  let resolveRequest: ((value: string) => void) | undefined
  const delayedRequest = new Promise<string>((resolve) => { resolveRequest = resolve })
  const committed: string[] = []
  const pendingCommit = delayedRequest.then((value) => {
    if (epoch.isCurrent(firstSession)) committed.push(value)
  })
  epoch.invalidate()
  resolveRequest?.('tenant-a-project')
  await pendingCommit

  assert.equal(epoch.isCurrent(firstSession), false)
  assert.equal(epoch.isCurrent(epoch.capture()), true)
  assert.deepEqual(committed, [])
})

test('studio store rejects stale session results without disturbing new requests', async (context) => {
  const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
  const server = await createServer({
    root: frontendRoot,
    logLevel: 'silent',
    appType: 'custom',
    server: { middlewareMode: true, hmr: false },
  })
  context.after(async () => { await server.close() })

  const { repository } = await server.ssrLoadModule('/src/services/repository.ts')
  const { useStudioStore } = await server.ssrLoadModule('/src/stores/studio.ts')

  await context.test('initialize reset prevents the previous session from writing late results', async () => {
    setActivePinia(createPinia())
    const store = useStudioStore()
    const staleStyles = deferred<Array<{ id: string }>>()
    let styleRequests = 0
    let projectRequests = 0

    repository.getStyles = () => {
      styleRequests += 1
      return styleRequests === 1
        ? staleStyles.promise
        : Promise.resolve([{ id: 'fresh-style' }])
    }
    repository.getProjects = async () => {
      projectRequests += 1
      return [makeProject({ id: 'fresh-project', title: 'fresh initialize' })]
    }
    repository.getWorkflows = async () => []

    const staleInitialize = store.initialize()
    assert.equal(styleRequests, 1)
    store.resetSessionState()
    const freshInitialize = store.initialize()
    await freshInitialize

    assert.equal(store.ready, true)
    assert.equal(store.styles[0]?.id, 'fresh-style')
    assert.equal(store.projects[0]?.title, 'fresh initialize')
    assert.equal(projectRequests, 1)

    staleStyles.resolve([{ id: 'stale-style' }])
    await assert.rejects(
      staleInitialize,
      (error: unknown) => error instanceof Error && error.name === 'SessionInvalidatedError',
    )

    assert.equal(store.ready, true)
    assert.equal(store.styles[0]?.id, 'fresh-style')
    assert.equal(store.projects[0]?.title, 'fresh initialize')
    assert.equal(projectRequests, 1)
  })

  await context.test('workflow diagnostics failure does not erase successfully loaded projects', async () => {
    setActivePinia(createPinia())
    const store = useStudioStore()

    repository.getStyles = async () => [{ id: 'style-before-diagnostics-failure' }]
    repository.getProjects = async () => [makeProject({
      id: 'preserved-project',
      title: '已保存项目',
    })]
    repository.getWorkflows = async () => {
      throw new Error('任务诊断加载失败')
    }

    await store.initialize()

    assert.equal(store.ready, false)
    assert.equal(store.styles[0]?.id, 'style-before-diagnostics-failure')
    assert.equal(store.projects.length, 1)
    assert.equal(store.projects[0]?.title, '已保存项目')
    assert.match(store.error, /任务诊断加载失败/)
  })

  await context.test('stale same-project detail is cancelled and cannot delete the fresh pending request', async () => {
    setActivePinia(createPinia())
    const store = useStudioStore()
    const staleDetail = deferred<SeriesProject>()
    const freshDetail = deferred<SeriesProject>()
    let detailRequests = 0

    repository.getProject = () => {
      detailRequests += 1
      if (detailRequests === 1) return staleDetail.promise
      if (detailRequests === 2) return freshDetail.promise
      throw new Error('A duplicate detail request escaped request coalescing.')
    }

    const staleLoad = store.loadProject('same-project', true)
    store.resetSessionState()
    const freshLoad = store.loadProject('same-project', true)
    const staleCancelled = assert.rejects(
      staleLoad,
      (error: unknown) => error instanceof Error && error.name === 'SessionInvalidatedError',
    )

    staleDetail.resolve(makeProject({ id: 'same-project', title: 'stale detail' }))
    await staleCancelled
    assert.deepEqual(store.projects, [])

    const coalescedFreshLoad = store.loadProject('same-project', true)
    assert.equal(detailRequests, 2)
    freshDetail.resolve(makeProject({ id: 'same-project', title: 'fresh detail' }))
    const [fresh, coalesced] = await Promise.all([freshLoad, coalescedFreshLoad])

    assert.equal(fresh.title, 'fresh detail')
    assert.equal(coalesced.title, 'fresh detail')
    assert.equal(store.projects.length, 1)
    assert.equal(store.projects[0]?.title, 'fresh detail')
    assert.equal(detailRequests, 2)
  })

  await context.test('stale project creation cannot write into the next session', async () => {
    setActivePinia(createPinia())
    const store = useStudioStore()
    const staleCreate = deferred<SeriesProject>()
    repository.createProject = () => staleCreate.promise

    const pending = store.createProject({ title: 'tenant A project' })
    store.resetSessionState()
    store.projects.push(makeProject({ id: 'tenant-b', title: 'tenant B project' }))
    staleCreate.resolve(makeProject({ id: 'tenant-a', title: 'tenant A project' }))

    await assert.rejects(
      pending,
      (error: unknown) => error instanceof Error && error.name === 'SessionInvalidatedError',
    )
    assert.deepEqual(store.projects.map((project: SeriesProject) => project.id), ['tenant-b'])
  })
})

test('date and completion helpers safely expose honest list metadata', () => {
  assert.equal(safeProjectTimestamp('not-a-date'), null)
  assert.equal(safeProjectTimestamp(''), null)
  assert.equal(formatProjectUpdatedAt('not-a-date'), '时间未知')
  assert.notEqual(formatProjectUpdatedAt('2026-08-13T12:30:00Z'), '时间未知')

  const empty = makeProject()
  assert.deepEqual(projectCompletion(empty), { completed: 0, total: 0, percent: 0 })
  const mixed = makeProject({ episodes: [
    { id: '1', number: 1, title: '一', synopsis: '', status: 'completed', audioEnabled: true, shots: [], createdAt: '' },
    { id: '2', number: 2, title: '二', synopsis: '', status: 'draft', audioEnabled: true, shots: [], createdAt: '' },
    { id: '3', number: 3, title: '三', synopsis: '', status: 'completed', audioEnabled: true, shots: [], createdAt: '' },
  ] })
  assert.deepEqual(projectCompletion(mixed), { completed: 2, total: 3, percent: 67 })
  assert.deepEqual(projectCompletion(makeProject({
    episodes: [], episodeCount: 12, completedEpisodeCount: 5, detailLoaded: false,
  })), { completed: 5, total: 12, percent: 42 })
  assert.deepEqual(projectCompletion(makeProject({
    episodes: [], episodeCount: 12, completedEpisodeCount: 5, detailLoaded: true,
  })), { completed: 0, total: 0, percent: 0 })
  assert.deepEqual(projectEpisodeSummary(mixed), {
    episodeCount: 3,
    completedEpisodeCount: 2,
    totalDurationSeconds: 0,
  })
  assert.deepEqual(projectCompletion(makeProject({
    episodes: mixed.episodes,
    episodeCount: 99,
    completedEpisodeCount: 98,
    detailLoaded: true,
  })), { completed: 2, total: 3, percent: 67 })
  assert.equal(new SessionInvalidatedError().name, 'SessionInvalidatedError')
})

test('management view keeps card navigation separate from destructive controls', () => {
  const source = readFileSync(new URL('../src/views/SeriesProjectsView.vue', import.meta.url), 'utf8')
  const routerSource = readFileSync(new URL('../src/router/index.ts', import.meta.url), 'utf8')
  const shellSource = readFileSync(new URL('../src/layouts/AppShell.vue', import.meta.url), 'utf8')
  const adapterSource = readFileSync(new URL('../src/services/backendAdapters.ts', import.meta.url), 'utf8')
  const repositorySource = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  const storeSource = readFileSync(new URL('../src/stores/studio.ts', import.meta.url), 'utf8')
  const studioSource = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  const dashboardSource = readFileSync(new URL('../src/views/DashboardView.vue', import.meta.url), 'utf8')
  const coverSource = readFileSync(new URL('../src/components/ProjectCoverImage.vue', import.meta.url), 'utf8')
  assert.ok(source.includes('class="project-card-main"'))
  assert.ok(source.includes(':to="projectHref(project)"'))
  assert.ok(source.includes('@click.stop @keydown.stop'))
  assert.ok(source.includes('studio.setProjectFavorite(project, !project.isFavorite)'))
  assert.ok(source.includes('studio.duplicateProject(redoTarget.value)'))
  assert.ok(source.includes('await router.push(projectHref(created))'))
  assert.ok(source.includes('studio.deleteProject(project.id)'))
  assert.ok(source.includes('v-model="deleteConfirmation"'))
  assert.ok(source.includes(':disabled="!canConfirmDelete"'))
  assert.equal(source.match(/to="\/projects\/new"/gu)?.length, 1)
  assert.ok(source.includes('role="progressbar"'))
  assert.ok(source.includes('aria-label="正在加载连续剧项目"'))
  assert.ok(source.includes('ProjectCoverImage'))
  assert.ok(routerSource.includes("path: 'projects', name: 'projects'"))
  assert.ok(shellSource.includes("label: '连续剧项目', to: '/projects'"))
  assert.ok(shellSource.includes("['projects','dashboard','project-new']"))
  assert.ok(adapterSource.includes('isFavorite: raw.is_favorite === true'))
  assert.ok(adapterSource.includes('coverAssetId: relationId(raw.cover_asset) || undefined'))
  assert.ok(adapterSource.includes("coverUrl: String(raw.cover_url || '') || undefined"))
  assert.ok(adapterSource.includes('export const adaptProjectSummary'))
  assert.ok(adapterSource.includes("status === 'paused' || status === 'archived'"))
  assert.ok(adapterSource.includes('episodeCount: Number(raw.episode_count || 0)'))
  assert.ok(adapterSource.includes('detailLoaded, createdAt:'))
  assert.ok(!adapterSource.match(/projectPayload[\s\S]*is_favorite:/u))
  assert.ok(repositorySource.includes('return raw.map((project) => adaptProjectSummary(project))'))
  assert.ok(repositorySource.includes("fetchAllPages<Record<string, unknown>>('/projects/', { page_size: 200 })"))
  assert.ok(!repositorySource.includes('Promise.all(raw.map(adaptProject))'))
  assert.ok(repositorySource.includes('adaptProjectSummary(unwrap<Record<string, unknown>>((await api.post'))
  assert.ok(repositorySource.includes('await api.delete(`/projects/${projectId}/`)'))
  assert.equal(repositorySource.includes('saveLocal('), false)
  assert.ok(repositorySource.includes("metadata', JSON.stringify({ usage: 'project_cover', project_id: project.id })"))
  assert.ok(repositorySource.includes("await api.delete(`/assets/${uploadedAssetId}/`)"))
  assert.ok(repositorySource.includes("cover_asset: uploadedAssetId"))
  assert.ok(repositorySource.includes("cover_asset: null"))
  assert.ok(repositorySource.includes("asset.metadata?.usage !== 'project_cover'"))
  assert.ok(storeSource.includes('if (initializeRequest) return initializeRequest'))
  assert.ok(storeSource.includes('const requestEpoch = sessionEpoch.capture()'))
  assert.ok(storeSource.includes('if (!sessionEpoch.isCurrent(requestEpoch)) throw new SessionInvalidatedError()'))
  assert.ok(storeSource.includes('sessionEpoch.invalidate()'))
  assert.ok(storeSource.includes('applyProjectEpisodeSummary(project)'))
  assert.ok(storeSource.includes('const loadProject = async'))
  assert.ok(storeSource.includes('workflows.value = workflows.value.filter((item) => item.projectId !== projectId)'))
  assert.ok(storeSource.includes('repository.setProjectCover(project,file)'))
  assert.ok(studioSource.includes(':accept="PROJECT_COVER_ACCEPT"'))
  assert.ok(studioSource.includes('projectCoverFileError(file)'))
  assert.ok(studioSource.includes('项目已创建，但封面上传失败'))
  assert.ok(studioSource.includes("creationError.value = apiErrorSummary(error, '创建流程失败')"))
  assert.ok(studioSource.includes('URL.revokeObjectURL(pendingCoverPreview.value)'))
  assert.ok(studioSource.includes('pendingCoverFile.value = null'))
  assert.ok(studioSource.includes('studio.loadProject(project.value.id, true)'))
  assert.ok(studioSource.includes('project.value.coverAssetId = refreshed.coverAssetId'))
  assert.ok(!studioSource.includes('project.value = clone(refreshed)\n  return refreshed.coverUrl'))
  assert.ok(studioSource.includes('<template v-else><div class="preview-pattern"></div>'))
  assert.ok(dashboardSource.includes('ProjectCoverImage'))
  assert.ok(source.includes('<template v-if="!hasVisibleCover(project)">'))
  assert.ok(dashboardSource.includes('<template v-if="!hasVisibleCover(project)">'))
  assert.ok(source.includes('@unavailable="markCoverUnavailable(project.id)"'))
  assert.ok(dashboardSource.includes('@unavailable="markCoverUnavailable(project.id)"'))
  assert.ok(!source.includes('.project-cover-image~span'))
  assert.ok(!dashboardSource.includes('.project-cover>img~span'))
  assert.ok(coverSource.includes('refreshedOnce'))
  assert.ok(coverSource.includes('@load="handleLoad"'))
})
