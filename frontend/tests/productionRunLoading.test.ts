import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, ssrContextKey } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { canonical, assets as fulfilledAssets, id as fulfilledId, sha as fulfilledSha } from './fixtures/bible-fulfilled-review-data.ts'

const root = fileURLToPath(new URL('..', import.meta.url))
const settle = async () => { for (let i = 0; i < 30; i++) { await Promise.resolve(); await nextTick() } }
function deferred() {
  let resolve!: (v: any) => void, reject!: (e: Error) => void
  const promise = new Promise<any>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const run = (id = 'run', changes = {}) => ({ id, project: 'project', planSource: 'manual', status: 'awaiting_input',
  stages: [], artifacts: [], inputSnapshot: {}, ...changes })
const project = { id: 'project', title: '当前项目', episodes: [], bible: { characters: [], scenes: [], props: [] } }

test('workbench reads are scoped to the visible route without weakening review gates', async t => {
  const previousStorage = globalThis.localStorage, previousWindow = globalThis.window
  const timers = new Map<number, () => void>(); let timerId = 0
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} } as any
  globalThis.window = { setInterval: callback => { timers.set(++timerId, callback); return timerId }, clearInterval: id => timers.delete(id) } as any
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  const { platformApi: api } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const { api: http } = await vite.ssrLoadModule('/src/services/api.ts')
  const originalAdapter = http.defaults.adapter
  const unexpectedHttp: string[] = []
  http.defaults.adapter = async config => {
    unexpectedHttp.push(`${config.method} ${config.url}`)
    throw Error('Undeclared business HTTP is blocked in this isolated test')
  }
  const { repository } = await vite.ssrLoadModule('/src/services/repository.ts')
  const { useStudioStore } = await vite.ssrLoadModule('/src/stores/studio.ts')
  const { default: C } = await vite.ssrLoadModule('/src/views/ProductionRunView.vue')
  const originalApi = { ...api }, originalRepository = { ...repository }
  let calls: string[] = []
  const reset = () => {
    Object.assign(api, originalApi); Object.assign(repository, originalRepository); calls = []
    for (const method of ['listRecipes', 'listScriptSources', 'listProductionRuns', 'listGenerationAttempts']) api[method] = async () => { calls.push(method); return [] }
    api.getProductionRun = async id => { calls.push(`run:${id}`); return run(id) }
    repository.getAssets = async () => { calls.push('assets'); return [] }
    repository.getProductionCapability = async () => { calls.push('capability'); return { creationDefaults: { episodeCount: 1, shotsPerEpisode: 2, episodeDurationSeconds: 6 }, effectiveLimits: {} } }
  }
  const mount = async (path = '/projects/project/production/run', configure = (_: any, _loadProject: any) => {}) => {
    const pinia = createPinia(), studio = useStudioStore(pinia)
    const realLoadProject = studio.loadProject
    studio.initialize = async () => { throw Error('must not initialize unrelated tasks') }
    studio.loadStyles = async () => {}
    studio.projects = [project]
    studio.loadProject = async () => { calls.push('project'); return project }
    configure(studio, realLoadProject)
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/projects/:projectId/production', name: 'production-run', component: { render: () => null } },
      { path: '/projects/:projectId/production/:runId', name: 'production-run-detail', component: { render: () => null } },
    ] })
    await router.push(path); await router.isReady()
    const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    let state: any
    const app = renderer.createApp({ setup() { state = C.setup({}, { expose() {} }); return () => h('div') } })
    app.use(pinia); app.use(router); app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await settle()
    return { state, app, router, studio }
  }
  try {
    await t.test('schema3 recipe creation requires explicit unattended consent and rejects unknown modes', async () => {
      for (const mode of ['unattended', 'assisted', 'unknown']) {
        reset()
        api.listRecipes = async () => [{ id: 'recipe', definition: { schema_version: 3, interaction_mode: mode,
          stages: [{ key: 'script_generate', mode: 'ai', approval: 'auto' }, { key: 'script_review', mode: 'review', approval: 'auto' }] } }]
        const writes: any[] = []
        api.createProductionRun = async input => { writes.push(input); return run('created', { planSource: 'recipe' }) }
        const m = await mount('/projects/project/production?view=new')
        try {
          const s = m.state
          s.creationSource.value = 'recipe'; s.selectedRecipeId.value = 'recipe'; s.aiIdea.value = '测试故事'
          await settle()
          s.projectSourceReady.value = true // The source-confirmation child has its own isolated contract tests.
          assert.equal(s.isUnattendedRecipe.value, mode === 'unattended')
          if (mode === 'unattended') {
            assert.match(s.unattendedCreationError.value, /请明确确认/)
            assert.equal(s.canCreate.value, false); await s.createRun(); assert.equal(writes.length, 0)
          }
          if (mode === 'unknown') {
            s.unattendedConfirmed.value = true
            assert.match(s.recipeContractError.value, /交互模式/)
            assert.equal(s.canCreate.value, false); await s.createRun(); assert.equal(writes.length, 0)
            continue
          }
          s.unattendedConfirmed.value = true
          if (mode === 'unattended') {
            s.selectedRecipeId.value = ''; await settle()
            assert.equal(s.unattendedConfirmed.value, false, 'changing the exact recipe clears consent')
            s.selectedRecipeId.value = 'recipe'; await settle()
            s.unattendedConfirmed.value = true; s.projectSourceReady.value = true
          }
          assert.equal(s.canCreate.value, true)
          await s.createRun(); assert.equal(writes.length, 1)
          const { buildProductionRunRequestPayload } = await vite.ssrLoadModule('/src/services/productionRunPayload.ts')
          assert.equal(buildProductionRunRequestPayload(writes[0]).unattended_confirmed, mode === 'unattended' ? true : undefined)
        } finally { m.app.unmount() }
      }
    })
    await t.test('storyboard approval uses only observed hashes and known conflict requires a new read', async () => {
      reset()
      const raw = run('run', {planSource:'recipe',status:'awaiting_approval',currentStageKey:'storyboard_review',stages:[{id:'stage',stageKey:'storyboard_review',status:'awaiting_approval'}]})
      api.getProductionRun = async () => structuredClone(raw)
      repository.getProductionCapability = async () => {throw Error('unrelated Bible image limits must not be read')}
      let writes=0
      api.approveProductionRun=async(_id,input)=>{
        writes++;assert.equal(input.expectedSourceSnapshotSha256,fulfilledSha);assert.equal(input.expectedSelectionSha256,'b'.repeat(64))
        assert.equal('shotReferenceBindings' in input,false)
        throw {response:{status:409,data:{code:'storyboard_reference_selection_changed',message:'确切选择已改变，请重新核对'}}}
      }
      const m=await mount()
      try{
        assert.equal(m.state.productionCapability.value,null);assert.equal(m.state.productionCapabilityError.value,'')
        await m.state.decide();assert.equal(writes,0)
        m.state.storyboardReferenceReview.value={sourceSnapshotSha256:fulfilledSha,selectionSha256:'b'.repeat(64),shots:[]}
        m.state.storyboardReferenceError.value=''
        assert.equal(m.state.reviewApprovalError.value,'');m.state.decide();await settle();assert.equal(writes,1)
        assert.equal(m.state.storyboardReferenceReview.value,null);assert.equal(m.state.pendingRunReceipt.value,null)
        assert.match(m.state.reviewApprovalError.value,/确切选择已改变/);await m.state.decide();assert.equal(writes,1)
      }finally{m.app.unmount()}
    })
    await t.test('opening a single-shot selector invalidates approval and release requests a fresh hash once', async () => {
      reset();const m=await mount()
      try{
        m.state.storyboardReferenceReview.value={sourceSnapshotSha256:fulfilledSha,selectionSha256:'b'.repeat(64),shots:[]}
        const epoch=m.state.storyboardReviewRefreshEpoch.value
        m.state.protectRecipeShotSelection(true);assert.equal(m.state.storyboardReferenceReview.value,null)
        assert.equal(m.state.recipeShotSelectionProtected.value,true)
        m.state.protectRecipeShotSelection(false);assert.equal(m.state.storyboardReviewRefreshEpoch.value,epoch+1)
        m.state.protectRecipeShotSelection(false);assert.equal(m.state.storyboardReviewRefreshEpoch.value,epoch+1)
      }finally{m.app.unmount()}
    })
    await t.test('recipe image review hydrates original fulfillments, preserves same-snapshot drafts and submits exact Assets', async () => {
      reset()
      const raw = run('run', { planSource: 'recipe', status: 'awaiting_approval', currentStageKey: 'bible_image_review', stages: [
        { id: fulfilledId(3), stageKey: 'bible_image_generation', status: 'succeeded', outputSnapshotSha256: fulfilledSha,
          outputSnapshot: { stage_key: 'bible_image_generation', formal_object_type: 'bible_image_candidate_manifest', canonical: canonical() } },
        { id: fulfilledId(7), stageKey: 'bible_image_review', status: 'awaiting_approval' },
      ] })
      api.getProductionRun = async () => structuredClone(raw)
      repository.getAssets = async () => fulfilledAssets()
      repository.getBibleImageCandidates = async () => ({ candidates: [] })
      repository.getProductionCapability = async () => ({ creationDefaults: {}, effectiveLimits: { maxBibleReferenceAssetsPerEntity: 8 } })
      const posts: any[] = []
      api.approveProductionRun = async (_id, input) => { posts.push(input); return structuredClone(raw) }
      const m = await mount()
      try {
        const key = `character:${fulfilledId(4)}`
        assert.equal(m.state.reviewApprovalError.value, '')
        assert.equal(m.state.bibleImageSelections.value[key].anchorAssetId, fulfilledId(10))
        m.state.bibleImageSelections.value[key].selectedAssetIds.pop()
        m.state.currentRun.value = structuredClone(raw); await settle()
        assert.equal(m.state.bibleImageSelections.value[key].selectedAssetIds.length, 2)
        m.state.decide(); await settle(); assert.equal(posts.length, 0)
        m.state.bibleImageSelections.value[key].selectedAssetIds.push(fulfilledId(12)); await settle()
        m.state.decide(); await settle()
        assert.equal(posts.length, 1)
        assert.equal(posts[0].expectedSourceSnapshotSha256, fulfilledSha)
        assert.deepEqual(posts[0].bibleImageBindings[0].selectedLibraryAssets, [10, 11, 12].map(n => ({ assetId: fulfilledId(n), expectedSha256: fulfilledSha })))
        assert.equal(posts[0].bibleImageBindings[0].anchorLibraryAssetId, fulfilledId(10))
        assert.deepEqual(posts[0].bibleImageBindings[0].selectedArtifactIds, [])
        const pending = deferred(); repository.getBibleImageCandidates = () => pending.promise
        const read = m.state.loadBibleImageHistory()
        assert.match(m.state.reviewApprovalError.value, /正在读取/)
        pending.reject(Error('isolated read failure')); await read
        assert.match(m.state.reviewApprovalError.value, /读取失败/)
        m.state.decide(); await settle(); assert.equal(posts.length, 1)
      } finally { m.app.unmount() }
    })
    await t.test('project script management has no recipe, video contract or run-creation dependency', async () => {
      reset()
      api.listRecipes = async () => { throw Error('unrelated recipe must not be read') }
      repository.getProductionCapability = async () => { throw Error('unrelated video contract must not be read') }
      let writes = 0
      api.createProductionRun = async () => { writes++; throw Error('must not create a run') }
      const m = await mount('/projects/project/production?view=scripts')
      try {
        assert.equal(m.state.isScriptManagement.value, true)
        assert.equal(m.state.loading.value, false); assert.equal(m.state.pageReadFailed.value, false)
        assert.deepEqual([...calls].sort(), ['assets', 'listScriptSources', 'project'])
        assert.equal(m.state.currentRun.value, null); assert.equal(m.state.canCreate.value, false)
        await m.state.createRun(); assert.equal(writes, 0)
      } finally { m.app.unmount() }
    })
    await t.test('standalone import uses the original preview writer without implicit confirm, adoption or execution', async () => {
      reset()
      const source = { id: 'source', project: 'project', latestRevisionId: 'preview' }
      const revision = { id: 'preview', source: 'source', status: 'preview', revision: 1, content: '{"custom":false}', contentSha256: 'a'.repeat(64) }
      let saved = false; const writes: any[] = []
      api.createPastedScript = async (...input: any[]) => { writes.push(input); saved = true; return { source, revision } }
      api.listScriptSources = async () => saved ? [source] : []
      api.getScriptRevision = async () => revision
      api.confirmScriptRevision = async () => { throw Error('must not confirm') }
      api.performManualProductionAction = async () => { throw Error('must not adopt or generate') }
      const m = await mount('/projects/project/production?view=scripts')
      try {
        const s = m.state; s.scriptFormat.value = 'plain'; s.scriptText.value = revision.content
        await s.createScriptPreview()
        assert.deepEqual(writes, [['project', revision.content, 'plain']])
        assert.equal(s.previewRevision.value.id, 'preview'); assert.equal(s.selectedRevision.value.status, 'preview')
        assert.equal(s.canCreate.value, false); assert.equal(s.actionBusy.value, '')
        s.scriptText.value = '{"custom":0}'
        assert.equal(s.previewRevision.value, null); assert.equal(s.selectedRevisionId.value, '')
      } finally { m.app.unmount() }
    })
    await t.test('late script import cannot repopulate a different view or a foreign project', async () => {
      reset(); const pending = deferred()
      api.createPastedScript = async () => pending.promise
      const m = await mount('/projects/project/production?view=scripts')
      try {
        const s = m.state; s.scriptText.value = '原稿'; const request = s.createScriptPreview()
        await m.router.replace({ query: { view: 'new', source: 'manual' } }); await settle()
        pending.resolve({ source: { id: 'source', project: 'project' }, revision: { id: 'preview', source: 'source', status: 'preview', content: '原稿' } })
        await request; assert.equal(s.previewRevision.value, null)
        await m.router.replace({ query: { view: 'scripts' } }); await settle()
        api.createPastedScript = async () => ({ source: { id: 'source', project: 'foreign' }, revision: { id: 'preview', source: 'source', status: 'preview', content: '原稿' } })
        await s.createScriptPreview(); assert.match(s.error.value, /当前项目或来源不一致/)
        assert.equal(s.previewRevision.value, null)
      } finally { m.app.unmount() }
    })
    await t.test('standalone script entry reuses the one import and structure panel, with no create controls', async () => {
      const source = readFileSync(new URL('../src/views/ProductionRunView.vue', import.meta.url), 'utf8')
      const workbench = readFileSync(new URL('../src/components/ManualProductionWorkbench.vue', import.meta.url), 'utf8')
      assert.equal(source.match(/<ScriptRevisionStructurePanel /g)?.length, 1)
      assert.equal(source.match(/<strong>导入或选择剧本<\/strong>/g)?.length, 1)
      assert.ok(source.includes('v-else-if="isScriptManagement || isConfirmedScriptRecipe"'))
      assert.ok(source.includes('<section v-if="!isScriptManagement" class="platform-card production-step"><header><span>02</span>'))
      assert.match(workbench, /view: 'scripts'/); assert.match(workbench, /导入／编辑项目剧本（不调用模型）/)
    })
    await t.test('structured revision receipt selects only the matching source and keeps other action locks', async () => {
      reset(); const m = await mount('/projects/project/production?view=new&source=recipe')
      try {
        const s = m.state
        const original = { id: 'basis', source: 'source', content: '{}', status: 'confirmed' }
        const revised = { ...original, id: 'revised', status: 'preview' }
        s.selectedRevisionId.value = original.id; s.previewRevision.value = original
        s.revisions.value = [original]
        s.setStructuredEditBusy(true); assert.equal(s.actionBusy.value, 'script-fields')
        s.acceptStructuredRevision({ ...revised, source: 'other' }, 'basis')
        s.acceptStructuredRevision(revised, 'obsolete')
        assert.equal(s.previewRevision.value.id, 'basis')
        s.acceptStructuredRevision(revised, 'basis')
        assert.equal(s.selectedRevisionId.value, 'revised')
        assert.equal(s.previewRevision.value.status, 'preview')
        assert.deepEqual(s.revisions.value.map(v => v.id), ['revised', 'basis'])
        assert.equal(s.revisions.value[1].status, 'confirmed')
        assert.match(s.notice.value, /尚未确认或采用/)
        s.setStructuredEditBusy(false); assert.equal(s.actionBusy.value, '')
        s.actionBusy.value = 'confirm'
        s.setStructuredEditBusy(true); s.setStructuredEditBusy(false)
        assert.equal(s.actionBusy.value, 'confirm')
        assert.ok(!calls.some(v => /create|confirm/.test(v)))
      } finally { m.app.unmount() }
    })
    await t.test('exact run starts alongside project read and omits all creation dependencies', async () => {
      reset(); const slow = deferred()
      const m = await mount(undefined, studio => { studio.loadProject = () => slow.promise })
      try {
        assert.deepEqual(calls, ['run:run']); assert.equal(m.state.loading.value, true)
        slow.resolve(project); await settle()
        assert.equal(m.state.loading.value, false); assert.equal(m.state.currentRun.value.id, 'run')
        assert.equal(m.state.productionCapability.value, null)
      } finally { m.app.unmount() }
    })
    await t.test('history and manual creation skip recipes, script library and capability checks', async () => {
      for (const suffix of ['?view=list', '?view=new&source=manual']) {
        reset(); const m = await mount(`/projects/project/production${suffix}`)
        try {
          assert.equal(m.state.loading.value, false)
          assert.deepEqual([...calls].sort(), suffix.includes('list') ? ['listProductionRuns', 'project'] : ['project'])
        } finally { m.app.unmount() }
      }
    })
    await t.test('default entry restores the sole existing run without loading creation data twice', async () => {
      reset(); api.listProductionRuns = async () => [run()]
      const m = await mount('/projects/project/production')
      try { await settle(); assert.equal(m.router.currentRoute.value.params.runId, 'run'); assert.equal(m.state.currentRun.value.id, 'run'); assert.deepEqual([...calls].sort(), ['project', 'project', 'run:run']) }
      finally { m.app.unmount() }
    })
    await t.test('new recipe still obtains current limits and changing creation mode loads its own data', async () => {
      reset(); const m = await mount('/projects/project/production?view=new&source=manual')
      try {
        await m.router.replace({ query: { view: 'new', source: 'recipe' } }); await settle()
        assert.equal(m.state.creationSource.value, 'recipe'); assert.equal(m.state.loading.value, false)
        assert.equal(m.state.episodeDurationSeconds.value, 6)
        assert.ok(calls.includes('capability')); assert.ok(calls.includes('listRecipes')); assert.ok(calls.includes('listScriptSources'))
        assert.ok(!calls.includes('listProductionRuns'))
      } finally { m.app.unmount() }
    })
    await t.test('empty recipes, no selection and unavailable selection are not malformed recipes', async () => {
      reset(); const m = await mount('/projects/project/production?view=new&source=recipe')
      try {
        const s = m.state
        assert.match(s.recipeSelectionIssue.value, /暂无可见的已发布配方/)
        assert.equal(s.isInvalidRecipe.value, false); assert.equal(s.canCreate.value, false)
s.recipes.value = [{ id: 'recipe', definition: { schema_version: 3, interaction_mode: 'assisted', stages: [{ key: 'script_input', mode: 'manual', approval: 'manual' }] } }]
        assert.match(s.recipeSelectionIssue.value, /请先选择/)
        s.selectedRecipeId.value = 'unavailable'
        assert.match(s.recipeSelectionIssue.value, /已不可用/)
        assert.equal(s.isInvalidRecipe.value, false); assert.equal(s.canCreate.value, false)
        s.selectedRecipeId.value = 'recipe'
        assert.equal(s.recipeSelectionIssue.value, ''); assert.equal(s.isDownstreamRecipe.value, true)
        s.recipes.value = [{ id: 'recipe', definition: {} }]
        assert.equal(s.recipeSelectionIssue.value, ''); assert.equal(s.isInvalidRecipe.value, true)
        assert.equal(s.canCreate.value, false)
        const source = readFileSync(new URL('../src/views/ProductionRunView.vue', import.meta.url), 'utf8')
        assert.ok(source.indexOf('v-else-if="!selectedRecipe"') < source.indexOf('<strong>配方剧本模式无效</strong>'))
        assert.ok(source.includes('recipeSelectionIssue || (isInvalidRecipe'))
      } finally { m.app.unmount() }
    })
    await t.test('catalog retries retain an explicit empty or unavailable recipe and edited plan, never submit', async () => {
      reset()
      const recipe = { id: 'recipe', definition: { schema_version: 3, interaction_mode: 'assisted', stages: [{ key: 'script_input', mode: 'manual', approval: 'manual' }] } }
      api.listRecipes = async () => [recipe]
      let writes = 0
      api.createProductionRun = async () => { writes++; throw Error('must not submit') }
      const m = await mount('/projects/project/production?view=new&source=recipe')
      try {
        const s = m.state
        assert.equal(s.selectedRecipeId.value, 'recipe')
        s.selectedRecipeId.value = ''; s.episodeCountHint.value = 8
        s.shotsPerEpisode.value = null; s.episodeDurationSeconds.value = 90
        s.scriptText.value = 'keep original draft'; s.selectedEpisodeIds.value = []
        m.studio.projects = [{ ...project, episodes: [{ id: 'episode' }] }]
        m.studio.loadProject = async () => m.studio.projects[0]
        const key = s.idempotencyKey.value
        api.listRecipes = async () => { throw Error('catalog offline') }
        await s.load(); assert.equal(s.pageReadFailed.value, true); assert.equal(s.canCreate.value, false)
        api.listRecipes = async () => [recipe]
        repository.getProductionCapability = async () => ({ creationDefaults: { episodeCount: 3, shotsPerEpisode: 4, episodeDurationSeconds: 60 }, effectiveLimits: {} })
        await s.load()
        assert.equal(s.selectedRecipeId.value, ''); assert.match(s.recipeSelectionIssue.value, /请先选择/)
        assert.deepEqual([s.episodeCountHint.value, s.shotsPerEpisode.value, s.episodeDurationSeconds.value], [8, null, 90])
        assert.equal(s.scriptText.value, 'keep original draft'); assert.equal(s.idempotencyKey.value, key)
        assert.deepEqual(s.selectedEpisodeIds.value, [])
        s.selectedRecipeId.value = 'recipe'
        api.listRecipes = async () => [{ ...recipe, id: 'replacement' }]
        await s.load(); assert.equal(s.selectedRecipeId.value, 'recipe')
        assert.match(s.recipeSelectionIssue.value, /已不可用/); assert.equal(s.canCreate.value, false)
        await s.createRun(); assert.equal(writes, 0)
      } finally { m.app.unmount() }
    })
    await t.test('creation defaults initialize after a failed first read but not again after an intentional clear', async () => {
      reset(); repository.getProductionCapability = async () => { throw Error('limits offline') }
      const m = await mount('/projects/project/production?view=new&source=recipe')
      try {
        const s = m.state
        assert.equal(s.episodeCountHint.value, null); assert.equal(s.canCreate.value, false)
        repository.getProductionCapability = async () => ({ creationDefaults: { episodeCount: 2, shotsPerEpisode: 3, episodeDurationSeconds: 8 }, effectiveLimits: {} })
        await s.load(); assert.deepEqual([s.episodeCountHint.value, s.shotsPerEpisode.value, s.episodeDurationSeconds.value], [2, 3, 8])
        s.episodeCountHint.value = null; await s.load(); assert.equal(s.episodeCountHint.value, null)
      } finally { m.app.unmount() }
    })
    await t.test('a different project resets recipe creation drafts and initializes its own defaults', async () => {
      reset()
      const m = await mount('/projects/project/production?view=new&source=recipe')
      try {
        const s = m.state
        s.selectedRecipeId.value = 'old'; s.episodeCountHint.value = 9
        s.aiIdea.value = 'old idea'; s.aiStyleHint.value = 'old style'; s.configOverridesText.value = '{"old":true}'
        s.allowExternalCalls.value = true
        s.projectSourceConfirmations.value = [{ old: true }]; s.projectSourceReady.value = true
        m.studio.loadProject = async id => ({ ...project, id })
        repository.getProductionCapability = async () => ({ creationDefaults: { episodeCount: 4, shotsPerEpisode: 5, episodeDurationSeconds: 12 }, effectiveLimits: {} })
        api.listRecipes = async () => [{ id: 'new', definition: { schema_version: 3, interaction_mode: 'assisted', stages: [{ key: 'script_input', mode: 'manual', approval: 'manual' }] } }]
        await m.router.push('/projects/other/production?view=new&source=recipe'); await settle()
        assert.equal(s.selectedRecipeId.value, 'new')
        assert.deepEqual([s.episodeCountHint.value, s.shotsPerEpisode.value, s.episodeDurationSeconds.value], [4, 5, 12])
        assert.equal(s.aiIdea.value, ''); assert.equal(s.aiStyleHint.value, ''); assert.equal(s.configOverridesText.value, '{}')
        assert.equal(s.allowExternalCalls.value, false); assert.equal(s.projectSourceReady.value, false)
        assert.deepEqual(s.projectSourceConfirmations.value, [])
      } finally { m.app.unmount() }
    })
    await t.test('a failed or pending recipe read is not presented as an empty catalog', async () => {
      reset(); const slow = deferred(); api.listRecipes = () => slow.promise
      const m = await mount('/projects/project/production?view=new&source=recipe')
      try {
        assert.equal(m.state.loading.value, true); assert.equal(m.state.recipeSelectionIssue.value, '')
        slow.reject(Error('recipe read denied')); await settle()
        assert.equal(m.state.pageReadFailed.value, true); assert.equal(m.state.recipeSelectionIssue.value, '')
        assert.match(m.state.error.value, /recipe read denied/); assert.equal(m.state.canCreate.value, false)
        api.listRecipes = async () => []; await m.state.load()
        assert.equal(m.state.pageReadFailed.value, false)
        assert.match(m.state.recipeSelectionIssue.value, /暂无可见的已发布配方/)
      } finally { m.app.unmount() }
    })
    await t.test('detail errors and foreign-project results remain errors, never an empty creation screen', async () => {
      for (const response of [() => { throw Error('read failed') }, () => run('run', { project: 'foreign' }), () => run('different-run')]) {
        reset(); api.getProductionRun = async () => response()
        const m = await mount()
        try {
          assert.equal(m.state.pageReadFailed.value, true); assert.equal(m.state.currentRun.value, null)
          api.getProductionRun = async () => run(); await m.state.load()
          assert.equal(m.state.pageReadFailed.value, false); assert.equal(m.state.currentRun.value.id, 'run')
        } finally { m.app.unmount() }
      }
      const source = readFileSync(new URL('../src/views/ProductionRunView.vue', import.meta.url), 'utf8')
      assert.ok(source.indexOf('v-else-if="pageReadFailed') < source.indexOf('v-else-if="!currentRun"'))
    })
    await t.test('slow review limits do not block detail, fail explicitly and can be retried', async () => {
      reset(); const slow = deferred()
      api.getProductionRun = async () => run('run', { planSource: 'recipe', currentStageKey: 'bible_image_review', stages: [{ id: 'stage', stageKey: 'bible_image_review', status: 'awaiting_approval' }] })
      repository.getProductionCapability = () => slow.promise
      const m = await mount()
      try {
        assert.equal(m.state.loading.value, false); assert.equal(m.state.currentRun.value.id, 'run')
        assert.equal(m.state.productionCapabilityLoading.value, true)
        assert.equal(m.state.productionCapability.value, null)
        assert.ok(m.state.reviewApprovalError.value)
        slow.reject(Error('limits unavailable')); await settle()
        assert.equal(m.state.productionCapabilityLoading.value, false); assert.match(m.state.productionCapabilityError.value, /limits unavailable/)
        repository.getProductionCapability = async () => ({ effectiveLimits: {} })
        await m.state.loadReviewCapability(); assert.equal(m.state.productionCapabilityError.value, '')
      } finally { m.app.unmount() }
    })
    await t.test('manual actual-cost acknowledgement keeps results and never calls continue or generation', async () => {
      reset(); const requests:any[]=[]
      api.getProductionRun=async()=>run('run',{status:'awaiting_approval',blockedCode:'actual_budget_exceeded',
        currentStageKey:'script_generate',costCurrency:'USD',actualCostTotal:'2',stages:[{id:'stage',stageKey:'script_generate'}]})
      api.authorizeProductionCost=async(id,input)=>{requests.push({id,input});return run('run')}
      api.continueProductionRun=async()=>{throw Error('must not automatically continue')}
      const m=await mount()
      try{
        assert.equal(requests.length,0)
        await m.state.acknowledgeActualCost()
        assert.equal(requests.length,1);assert.equal(requests[0].input.stageKey,'script_generate')
        assert.equal(requests[0].input.currency,'USD')
        for(const key of ['budgetLimit','allowExternalCalls','approvedEstimatedCost'])assert.equal(key in requests[0].input,false)
        assert.equal(m.state.currentRun.value.status,'awaiting_input')
        assert.match(m.state.notice.value,/未重新生成或自动采用/)
        await m.state.acknowledgeActualCost();assert.equal(requests.length,1)
      }finally{m.app.unmount()}
    })
    await t.test('polling skips in-flight reads, busy actions and stops after unmount', async () => {
      reset(); const m = await mount(); const slow = deferred(); let reads = 0
      try {
        api.getProductionRun = () => { reads++; return slow.promise }
        const tick = [...timers.values()][0]; assert.ok(tick)
        tick(); tick(); tick(); assert.equal(reads, 1)
        slow.resolve(run()); await settle()
        m.state.actionBusy.value = 'preparation'; tick(); assert.equal(reads, 1)
        m.state.actionBusy.value = ''; tick(); await settle(); assert.equal(reads, 2)
      } finally { m.app.unmount(); assert.equal(timers.size, 0) }
    })
    await t.test('manual and recipe editors defer page reads and ignore pre-open poll receipts, then resume', async () => {
      for (const [lock, failure] of ['manualWorkbenchProtected','recipeShotSelectionProtected'].flatMap(lock => [true,false].map(failure => [lock,failure] as const))) {
        reset(); const m = await mount(); const slow = deferred(); let reads = 0
        try {
          api.getProductionRun = () => { reads++; return slow.promise }
          const tick = [...timers.values()][0]!
          tick(); assert.equal(reads, 1)
          m.state[lock].value = true
          failure ? slow.reject(Error('offline')) : slow.resolve(run('run', { status: 'failed' }))
          await settle()
          assert.equal(m.state.currentRun.value.status, 'awaiting_input')
          assert.equal(m.state.pageReadFailed.value, false); assert.equal(m.state.error.value, '')
          tick(); await m.state.load(); assert.equal(await m.state.refreshRun(), false)
          assert.equal(reads, 1, 'neither polling nor a full reload may discard the mounted draft')
          assert.equal(m.state.currentRun.value.id, 'run')
          m.state[lock].value = false
          api.getProductionRun = async () => { reads++; return run('run', { status: 'succeeded' }) }
          tick(); await settle()
          assert.equal(reads, 2); assert.equal(m.state.currentRun.value.status, 'succeeded')
        } finally { m.app.unmount() }
      }
      const source = readFileSync(new URL('../src/views/ProductionRunView.vue', import.meta.url), 'utf8')
      assert.ok(source.includes('@context-protected="manualWorkbenchProtected = $event"'))
      assert.ok(source.includes('@context-protected="protectRecipeShotSelection"'))
    })
    await t.test('late results cannot overwrite another run, a reset session or an unmounted view', async () => {
      for (const action of ['route', 'session', 'unmount']) {
        reset(); const slow = deferred(); api.getProductionRun = id => id === 'run' ? slow.promise : Promise.resolve(run(id))
        const m = await mount()
        try {
          if (action === 'route') { await m.router.push('/projects/project/production/other'); await settle() }
          else if (action === 'session') m.studio.resetSessionState()
          else m.app.unmount()
          slow.resolve(run()); await settle()
          assert.equal(m.state.currentRun.value?.id || null, action === 'route' ? 'other' : null)
        } finally { m.app.unmount() }
      }
    })
    await t.test('all shared writes reject late receipts and errors across run, project, session and unmount', async () => {
      for (const name of ['continue', 'retry', 'cancel', 'approve', 'route', 'cost', 'regenerate-bible-images', 'resolve']) {
        for (const change of ['run', 'project', 'session', 'unmount']) {
          for (const failure of [false, true]) {
            reset(); const m = await mount(); const slow = deferred()
            const pending = m.state.runAction(name, () => slow.promise)
            try {
              if (change === 'run') await m.router.push('/projects/project/production/other')
              if (change === 'project') {
                m.studio.loadProject = async id => ({ ...project, id })
                api.getProductionRun = async id => run(id, { project: 'other-project' })
                await m.router.push('/projects/other-project/production/other')
              }
              if (change === 'session') m.studio.resetSessionState()
              if (change === 'unmount') m.app.unmount()
              await settle()
              const expectedId = m.state.currentRun.value?.id
              m.state.notice.value = 'new notice'; m.state.error.value = 'new error'; m.state.actionBusy.value = 'new action'
              const beforeCalls = calls.length
              failure ? slow.reject(Error('old write failed')) : slow.resolve(run('run', { status: 'cancelled' }))
              assert.equal(await pending, false); await settle()
              assert.equal(m.state.currentRun.value?.id, expectedId, `${name}/${change}/${failure}`)
              assert.equal(m.state.notice.value, 'new notice'); assert.equal(m.state.error.value, 'new error')
              assert.equal(m.state.actionBusy.value, 'new action'); assert.equal(m.state.pendingRunReceipt.value, null)
              assert.equal(calls.length, beforeCalls, 'a stale receipt must not refresh a different project')
            } finally { m.app.unmount() }
          }
        }
      }
    })
    await t.test('project-refresh receipts are also context-scoped and release only their own busy state', async () => {
      for (const failure of [false, true]) {
        reset(); const m = await mount(); const slow = deferred(); const projects: string[] = []
        try {
          m.studio.loadProject = id => { projects.push(id); return slow.promise }
          const pending = m.state.runAction('cost', async () => run())
          await settle(); assert.equal(m.state.actionBusy.value, 'cost'); assert.deepEqual(projects, ['project'])
          m.studio.loadProject = async () => project
          await m.router.push('/projects/project/production/other'); await settle()
          m.state.actionBusy.value = 'new action'; m.state.notice.value = 'current notice'
          failure ? slow.reject(Error('old project failed')) : slow.resolve(project)
          assert.equal(await pending, false)
          assert.equal(m.state.currentRun.value.id, 'other'); assert.equal(m.state.actionBusy.value, 'new action')
          assert.equal(m.state.notice.value, 'current notice'); assert.equal(m.state.error.value, '')
        } finally { m.app.unmount() }
      }
    })
    await t.test('duplicate writes and reads during submission cannot replay or restore a pre-action snapshot', async () => {
      reset(); const m = await mount(); const reading = deferred(), writing = deferred(); let writes = 0
      try {
        api.getProductionRun = () => reading.promise
        const oldRead = m.state.refreshRun()
        const submit = () => { writes++; return writing.promise }
        const first = m.state.runAction('continue', submit)
        assert.equal(await m.state.runAction('continue', submit), false)
        assert.equal(await m.state.runAction('cancel', submit), false)
        await m.state.load(); assert.equal(m.state.currentRun.value.id, 'run')
        assert.equal(await m.state.refreshRun(), false); assert.equal(writes, 1)
        writing.resolve(run('run', { status: 'succeeded' })); assert.equal(await first, true)
        reading.resolve(run()); await oldRead
        assert.equal(m.state.currentRun.value.status, 'succeeded'); assert.equal(m.state.actionBusy.value, '')
        assert.equal(m.state.pendingRunReceipt.value, null)
      } finally { m.app.unmount() }
    })
    await t.test('unrecognized mutation identity never replaces the original run or refreshes another project', async () => {
      for (const result of [null, run('different-run'), run('run', { project: 'foreign' })]) {
        reset(); const m = await mount(); const before = calls.length
        try {
          assert.equal(await m.state.runAction('cancel', async () => result), false)
          assert.equal(m.state.currentRun.value.id, 'run'); assert.equal(m.state.currentRun.value.status, 'awaiting_input')
          assert.equal(m.state.pendingRunReceipt.value.runId, 'run'); assert.equal(m.state.runActionBlocked.value, true)
          assert.match(m.state.error.value, /回执与原项目或运行不一致/); assert.equal(calls.length, before)
          assert.equal(m.state.actionBusy.value, '')
        } finally { m.app.unmount() }
      }
    })
    await t.test('unknown retry receipt requires explicit original reads, preserves failure evidence and never replays', async () => {
      reset()
      const failed = run('run', { status: 'failed', blockedCode: '', currentStageKey: 'script_generate',
        stages: [{ id: 'stage', stageKey: 'script_generate' }],
        retryContext: { expectedStageId: 'stage', expectedAttemptIds: ['failed-a'] } })
      api.getProductionRun = async () => failed
      const requests: any[] = []; const reads: string[] = []
      api.retryProductionRun = async (id, input) => { requests.push({ id, input }); throw Error('receipt lost') }
      const m = await mount()
      try {
        m.state.requestRunConfirmation('retry'); assert.equal(requests.length, 0)
        m.state.retryPreviewReady.value = true // Child readiness is tested in productionRetryPreview.test.ts.
        await m.state.confirmRunAction(); assert.deepEqual(requests, [{ id: 'run', input: failed.retryContext }])
        assert.equal(m.state.currentRun.value.status, 'failed'); assert.equal(m.state.pendingRunReceipt.value.action, 'retry')
        assert.match(m.state.pendingRunConfirmationError.value, /不能据此判定未提交/)
        await m.state.confirmRunAction(); assert.equal(requests.length, 1)
        m.state.closeRunConfirmation(); m.state.requestRunConfirmation('retry')
        assert.equal(m.state.pendingRunConfirmation.value, null, 'closing a confirmation cannot remove the read-first gate')
        m.state.approvalComment.value = 'retain human draft'
        api.getProductionRun = async id => { reads.push(id); throw Error('offline') }
        await m.state.recoverRunReceipt()
        assert.deepEqual(reads, ['run']); assert.equal(m.state.currentRun.value.status, 'failed')
        assert.equal(m.state.pendingRunReceipt.value.runId, 'run'); assert.equal(m.state.receiptReadBusy.value, false)
        assert.equal(m.state.approvalComment.value, 'retain human draft')
        // Actual server state may already have moved on. A GET is not a retry or proof of success.
        api.getProductionRun = async id => { reads.push(id); return { ...failed, status: 'running', retryContext: null } }
        await m.state.recoverRunReceipt()
        assert.deepEqual(reads, ['run', 'run']); assert.equal(requests.length, 1)
        assert.equal(m.state.currentRun.value.status, 'running'); assert.equal(m.state.pendingRunReceipt.value, null)
        assert.equal(m.state.pendingRunConfirmation.value, null); assert.equal(m.state.actionBusy.value, '')
        assert.match(m.state.notice.value, /不代表上次操作成功/)
      } finally { m.app.unmount() }
    })
    await t.test('retry confirmation keeps exact failed-attempt scope and refuses drift before any POST', async () => {
      reset()
      api.getProductionRun = async () => run('run', { status: 'failed', currentStageKey: 'script_generate', blockedCode: '',
        stages: [{ id: 'stage', stageKey: 'script_generate' }], retryContext: { expectedStageId: 'stage', expectedAttemptIds: ['a'] } })
      let writes = 0; api.retryProductionRun = async () => { writes++; return run() }
      const m = await mount()
      try {
        m.state.requestRunConfirmation('retry')
        m.state.currentRun.value.retryContext.expectedAttemptIds.push('b')
        assert.deepEqual(m.state.pendingRunConfirmation.value.retryContext.expectedAttemptIds, ['a'])
        m.state.retryPreviewReady.value = true
        await m.state.confirmRunAction(); assert.equal(writes, 0)
        assert.match(m.state.pendingRunConfirmationError.value, /失败调用范围缺失或已变化/)
        m.state.closeRunConfirmation(); m.state.requestRunConfirmation('retry')
        m.state.retryPreviewReady.value = true
        await m.state.confirmRunAction(); assert.equal(writes, 1); assert.equal(m.state.pendingRunConfirmation.value, null)
      } finally { m.app.unmount() }
    })
    await t.test('known action with failed project refresh is not replayed or misreported as a failed server action', async () => {
      reset(); const m = await mount(); let writes = 0
      try {
        m.studio.loadProject = async () => { throw Error('project read failed') }
        api.authorizeProductionCost = async () => { writes++; return run('run', { allowExternalCalls: true }) }
        await m.state.saveManualBudget()
        assert.equal(writes, 1); assert.equal(m.state.currentRun.value.allowExternalCalls, true)
        assert.match(m.state.error.value, /服务端操作已返回，但项目事实刷新失败/)
        assert.equal(m.state.notice.value, ''); assert.equal(m.state.runActionBlocked.value, true)
        await m.state.saveManualBudget(); assert.equal(writes, 1)
        await m.state.recoverRunReceipt(); assert.ok(m.state.pendingRunReceipt.value)
        m.studio.loadProject = async () => project
        await m.state.recoverRunReceipt(); assert.equal(m.state.pendingRunReceipt.value, null); assert.equal(writes, 1)
      } finally { m.app.unmount() }
    })
    await t.test('explicit receipt reads are deduplicated, do not poll, and cannot clear a new route warning', async () => {
      reset(); const m = await mount(); const slow = deferred(); let reads = 0
      try {
        await m.state.runAction('continue', async () => { throw Error('lost') })
        api.getProductionRun = () => { reads++; return slow.promise }
        for (const tick of timers.values()) tick()
        assert.equal(reads, 0, 'polling cannot silently clear an unverified receipt')
        const pending = m.state.recoverRunReceipt(); await m.state.recoverRunReceipt()
        assert.equal(reads, 1); assert.equal(m.state.receiptReadBusy.value, true)
        api.getProductionRun = async id => run(id)
        await m.router.push('/projects/project/production/other'); await settle()
        await m.state.runAction('cancel', async () => { throw Error('new lost receipt') })
        slow.resolve(run()); await pending
        assert.equal(m.state.currentRun.value.id, 'other'); assert.equal(m.state.pendingRunReceipt.value.runId, 'other')
        assert.match(m.state.error.value, /new lost receipt/); assert.equal(m.state.receiptReadBusy.value, false)
      } finally { m.app.unmount() }
    })
    await t.test('cost wrapper notices cannot claim success after a blocked action or a route switch', async () => {
      for (const method of ['saveManualBudget', 'acknowledgeActualCost']) {
        reset(); const slow = deferred()
        api.getProductionRun = async id => run(id, { status: 'awaiting_approval', blockedCode: 'actual_budget_exceeded',
          currentStageKey: 'script_generate', stages: [{ id: 'stage', stageKey: 'script_generate' }] })
        api.authorizeProductionCost = () => slow.promise
        const m = await mount()
        try {
          m.state.actionBusy.value = 'another action'; await m.state[method](); assert.equal(m.state.notice.value, '')
          m.state.actionBusy.value = ''; const pending = m.state[method]()
          await m.router.push('/projects/project/production/other'); await settle(); m.state.notice.value = 'current message'
          slow.resolve(run()); await pending
          assert.equal(m.state.notice.value, 'current message'); assert.equal(m.state.currentRun.value.id, 'other')
        } finally { m.app.unmount() }
      }
    })
    await t.test('administrator resolution uses the shared receipt gate and retains audit input on failure', async () => {
      reset(); const m = await mount(); const slow = deferred(); const requests: any[] = []
      try {
        m.state.productionCapability.value = { effectiveLimits: { auditNoteMinCharacters: 1, auditNoteMaxCharacters: 500 } }
        m.state.openUncertainResolution({ id: 'attempt-a', errorCode: 'uncertain_remote_response', operation: 'script.generate', actualCost: null, costCurrency: 'USD' })
        m.state.uncertainResolutionNote.value = 'confirmed by operator, fixture only'
        api.resolveProductionRunUncertain = (id, input) => { requests.push({ id, input }); return slow.promise }
        const pending = m.state.submitUncertainResolution()
        await m.state.submitUncertainResolution(); assert.equal(requests.length, 1)
        assert.equal(m.state.actionBusy.value, 'resolve'); assert.equal(m.state.uncertainResolutionSaving.value, true)
        slow.reject(Error('resolution receipt lost')); await pending
        assert.equal(m.state.uncertainResolutionAttempt.value.id, 'attempt-a')
        assert.equal(m.state.uncertainResolutionNote.value, 'confirmed by operator, fixture only')
        assert.match(m.state.uncertainResolutionError.value, /提交未能确认/)
        assert.equal(m.state.uncertainResolutionSaving.value, false); assert.equal(m.state.notice.value, '')
        await m.state.submitUncertainResolution(); assert.equal(requests.length, 1)
        assert.equal(requests[0].id, 'run'); assert.equal(requests[0].input.attemptId, 'attempt-a')
        assert.equal(requests[0].input.actualCost, null, 'unknown cost must not become zero')
      } finally { m.app.unmount() }
    })

    await t.test('confirmed-script creation requires an explicitly read, visible revision, not a hidden catalog default', async () => {
      reset()
      const revision = { id: 'script', source: 'source', revision: 1, status: 'confirmed', content: '原剧本', contentSha256: 'a'.repeat(64), contentSizeBytes: 9 }
      api.listRecipes = async () => [{ id: 'recipe', definition: { schema_version: 3, interaction_mode: 'assisted', stages: [{ key: 'script_input', mode: 'confirmed_revision', approval: 'auto' }] } }]
      api.listScriptSources = async () => [{ id: 'source', latestRevisionId: 'script' }]
      api.getScriptRevision = async () => revision
      const m = await mount('/projects/project/production?view=new&source=recipe')
      try {
        const s = m.state
        s.projectSourceReady.value = true
        assert.equal(s.selectedRevisionId.value, '', 'reading the catalog must not choose a script')
        assert.equal(s.canCreate.value, false)
        s.scriptMode.value = 'existing'; await settle()
        s.selectedRevisionId.value = 'script'; await s.selectExistingRevision()
        assert.equal(s.previewRevision.value.id, 'script'); assert.equal(s.canCreate.value, true)
        s.scriptMode.value = 'paste'; s.scriptText.value = '新剧本'; await settle()
        assert.equal(s.previewRevision.value, null); assert.equal(s.canCreate.value, false)
        s.scriptMode.value = 'existing'; await settle()
        s.selectedRevisionId.value = 'script'; api.getScriptRevision = async () => { throw Error('detail unavailable') }
        await s.selectExistingRevision()
        assert.equal(s.previewRevision.value, null); assert.equal(s.canCreate.value, false)
        assert.match(s.error.value, /detail unavailable/)
      } finally { m.app.unmount() }
    })
    const scriptPreview = { id: 'script', source: 'source', revision: 1, status: 'preview', content: '当前剧本', contentSha256: 'a'.repeat(64), contentSizeBytes: 12 }
    const scriptResult = { source: { id: 'source', project: 'project', latestRevisionId: 'script' }, revision: scriptPreview }
    const setupScriptCreation = () => {
      reset()
      api.listRecipes = async () => [{ id: 'recipe', definition: { schema_version: 3, interaction_mode: 'assisted', stages: [{ key: 'script_input', mode: 'confirmed_revision', approval: 'auto' }] } }]
      api.listScriptSources = async () => [scriptResult.source]
      api.getScriptRevision = async () => scriptPreview
      api.createPastedScript = api.uploadScript = api.importScriptAsset = async () => scriptResult
      api.confirmScriptRevision = async () => ({ ...scriptPreview, status: 'confirmed' })
    }
    await t.test('paste, file and library previews stay bound to the input that was actually confirmed', async () => {
      for (const edit of ['text', 'format', 'file', 'library', 'mode']) {
        setupScriptCreation()
        const m = await mount('/projects/project/production?view=new&source=recipe')
        try {
          const s = m.state; s.projectSourceReady.value = true
          s.scriptMode.value = edit === 'file' ? 'file' : edit === 'library' ? 'library' : 'paste'
          s.scriptText.value = '当前剧本'; s.scriptFile.value = new File(['当前剧本'], 'script.txt'); s.selectedScriptAssetId.value = 'asset-a'
          if (edit === 'library') await s.importLibraryScript(); else await s.createScriptPreview()
          assert.equal(s.previewRevision.value.id, 'script'); assert.equal(s.canCreate.value, false)
          await s.confirmRevision(); assert.equal(s.canCreate.value, true)
          if (edit === 'text') s.scriptText.value = '修改后的剧本'
          if (edit === 'format') s.scriptFormat.value = 'fountain'
          if (edit === 'file') s.scriptFile.value = new File(['不同正文'], 'script.txt')
          if (edit === 'library') s.selectedScriptAssetId.value = 'asset-b'
          if (edit === 'mode') s.scriptMode.value = 'existing'
          assert.equal(s.previewRevision.value, null); assert.equal(s.selectedRevisionId.value, ''); assert.equal(s.canCreate.value, false)
          let creates = 0; api.createProductionRun = async () => { creates++; return run() }
          await s.createRun(); assert.equal(creates, 0)
        } finally { m.app.unmount() }
      }
    })
    await t.test('script preview and confirmation block duplicate actions and submit only the displayed confirmed revision', async () => {
      setupScriptCreation()
      const m = await mount('/projects/project/production?view=new&source=recipe')
      try {
        const s = m.state; s.projectSourceReady.value = true; s.scriptText.value = '当前剧本'
        const slow = deferred(); let previews = 0
        api.createPastedScript = () => { previews++; return slow.promise }
        const pending = s.createScriptPreview(); await s.createScriptPreview(); assert.equal(previews, 1)
        slow.resolve(scriptResult); await pending
        const confirm = deferred(); let confirms = 0
        api.confirmScriptRevision = () => { confirms++; return confirm.promise }
        const confirmation = s.confirmRevision(); await s.confirmRevision(); assert.equal(confirms, 1)
        confirm.resolve({ ...scriptPreview, status: 'confirmed' }); await confirmation
        assert.equal(s.canCreate.value, true)
        const requests: any[] = []
        api.createProductionRun = async input => { requests.push(input); throw Object.assign(Error('stop at isolated request'), { response: { status: 400 } }) }
        await s.createRun(); assert.equal(requests.length, 1); assert.equal(requests[0].scriptRevisionId, s.previewRevision.value.id)
      } finally { m.app.unmount() }
    })
    await t.test('late script preview, detail and confirmation cannot restore a different input or session', async () => {
      for (const action of ['preview', 'detail', 'confirm']) for (const boundary of ['input', 'mode', 'route', 'session', 'unmount']) {
        setupScriptCreation()
        const m = await mount('/projects/project/production?view=new&source=recipe')
        try {
          const s = m.state, slow = deferred(); s.projectSourceReady.value = true; s.scriptText.value = '当前剧本'
          let pending: Promise<void>
          if (action === 'preview') { api.createPastedScript = () => slow.promise; pending = s.createScriptPreview() }
          else if (action === 'detail') { s.scriptMode.value = 'existing'; s.selectedRevisionId.value = 'script'; api.getScriptRevision = () => slow.promise; pending = s.selectExistingRevision() }
          else { await s.createScriptPreview(); api.confirmScriptRevision = () => slow.promise; pending = s.confirmRevision() }
          if (boundary === 'input') { if (action === 'detail') s.selectedRevisionId.value = ''; else s.scriptText.value = '新正文' }
          if (boundary === 'mode') s.scriptMode.value = action === 'detail' ? 'paste' : 'existing'
          if (boundary === 'route') { await m.router.push('/projects/project/production/other'); await settle() }
          if (boundary === 'session') m.studio.resetSessionState()
          if (boundary === 'unmount') m.app.unmount()
          const originalPreview = s.previewRevision.value, originalSelection = s.selectedRevisionId.value
          s.error.value = 'current context'; s.notice.value = 'current notice'
          slow.resolve(action === 'preview' ? scriptResult : { ...scriptPreview, status: 'confirmed' }); await pending
          assert.equal(s.previewRevision.value, originalPreview); assert.equal(s.selectedRevisionId.value, originalSelection)
          assert.equal(s.error.value, 'current context'); assert.equal(s.notice.value, 'current notice')
        } finally { if (boundary !== 'unmount') m.app.unmount() }
      }
    })
    await t.test('mismatched script detail, preview or confirmation receipts never unlock creation', async () => {
      for (const failure of ['foreign-preview', 'missing-body', 'foreign-detail', 'changed-confirm']) {
        setupScriptCreation()
        const m = await mount('/projects/project/production?view=new&source=recipe')
        try {
          const s = m.state; s.projectSourceReady.value = true; s.scriptText.value = '当前剧本'
          if (failure === 'foreign-preview') api.createPastedScript = async () => ({ ...scriptResult, source: { ...scriptResult.source, project: 'foreign' } })
          if (failure === 'missing-body') api.createPastedScript = async () => ({ ...scriptResult, revision: { ...scriptPreview, content: '' } })
          if (failure === 'foreign-detail') {
            s.scriptMode.value = 'existing'; s.selectedRevisionId.value = 'script'; api.getScriptRevision = async () => ({ ...scriptPreview, source: 'foreign', status: 'confirmed' })
            await s.selectExistingRevision()
          } else {
            await s.createScriptPreview()
            if (failure === 'changed-confirm') { api.confirmScriptRevision = async () => ({ ...scriptPreview, content: '错误正文', status: 'confirmed' }); await s.confirmRevision() }
          }
          assert.equal(s.canCreate.value, false); assert.ok(s.error.value)
        } finally { m.app.unmount() }
      }
    })
    await t.test('script catalog loss never silently selects another confirmed revision', async () => {
      setupScriptCreation()
      const m = await mount('/projects/project/production?view=new&source=recipe')
      try {
        const s = m.state; s.scriptMode.value = 'existing'; s.selectedRevisionId.value = 'script'; await s.selectExistingRevision()
        api.listScriptSources = async () => [{ id: 'another-source', latestRevisionId: 'another' }]
        api.getScriptRevision = async () => ({ ...scriptPreview, id: 'another', status: 'confirmed' })
        await s.readScripts(); assert.equal(s.selectedRevisionId.value, ''); assert.equal(s.previewRevision.value, null)
      } finally { m.app.unmount() }
    })
    await t.test('recipe creation recovers the exact request and a known receipt only reopens detail', async () => {
      reset()
      api.listRecipes=async()=>[{id:'recipe',definition:{schema_version: 3, interaction_mode: 'assisted', stages:[{key:'script_input',mode:'manual',approval:'manual'}]}}]
      const m=await mount('/projects/project/production?view=new&source=recipe',studio=>{studio.projects=[{...project,episodes:[{id:'episode-a'}]}]})
      const pending=deferred(),requests:any[]=[]
      try{
        const s=m.state;s.selectedEpisodeIds.value=['episode-a'];s.configOverridesText.value='{"nested":{"value":1}}'
        api.createProductionRun=input=>{requests.push(JSON.parse(JSON.stringify(input)));return requests.length===1?pending.promise:Promise.resolve(run('created',{planSource:'recipe'}))}
        assert.equal(s.canCreate.value,false,'no creation before the child has checked project-source requirements')
        await s.createRun();assert.equal(requests.length,0)
        // This setup-only harness does not render children. The source component
        // tests cover its exact-contract checks and ready emission (including no sources).
        s.projectSourceReady.value=true
        assert.equal(s.canCreate.value,true)
        const request=s.createRun();await s.createRun();assert.equal(requests.length,1)
        s.selectedEpisodeIds.value.push('episode-b');s.configOverridesText.value='{"nested":{"value":2}}'
        pending.reject(Error('receipt lost'));await request
        assert.deepEqual(s.creationRequest.value.episodeIds,['episode-a'])
        assert.deepEqual(s.creationRequest.value.configOverrides,{nested:{value:1}})
        const replace=m.router.replace;m.router.replace=async()=>{throw Error('navigation failed')}
        await s.createRun();assert.equal(requests.length,2);assert.deepEqual(requests[0],requests[1])
        assert.equal(s.creationResult.value.id,'created');assert.match(s.error.value,/navigation failed/)
        m.router.replace=replace
        await s.createRun();await settle()
        assert.equal(requests.length,2);assert.equal(m.router.currentRoute.value.params.runId,'created')
      }finally{m.app.unmount()}
    })
    await t.test('recipe creation rejects stale receipts across routes, sessions and unmount',async()=>{
      for(const boundary of ['run','project','session','unmount'])for(const failure of [false,true]){
        reset();const m=await mount('/projects/project/production?view=new&source=recipe')
        const slow=deferred();api.createProductionRun=()=>slow.promise
        try{
          const s=m.state
          s.creationRequest.value={planSource:'recipe',projectId:'project',recipeVersionId:'recipe',idempotencyKey:'original',scriptMode:'downstream',episodeIds:['episode']}
          const pending=s.createRun()
          if(boundary==='run')await m.router.push('/projects/project/production/other')
          if(boundary==='project'){m.studio.loadProject=async()=>({...project,id:'other'});await m.router.push('/projects/other/production?view=new&source=recipe')}
          if(boundary==='session')m.studio.resetSessionState()
          if(boundary==='unmount')m.app.unmount()
          await settle();s.error.value='current message'
          if(failure)slow.reject(Error('old error'));else slow.resolve(run('old',{planSource:'recipe'}))
          await pending;assert.equal(s.error.value,'current message');assert.equal(s.creationResult.value,null)
          assert.notEqual(m.router.currentRoute.value.params.runId,'old')
        }finally{if(boundary!=='unmount')m.app.unmount()}
      }
    })
    await t.test('recipe rejection releases the form only for definitive errors; malformed receipts do not navigate',async()=>{
      for(const outcome of ['400','409','foreign','wrong-plan','no-id']){
        reset();const m=await mount('/projects/project/production?view=new&source=recipe')
        try{
          const s=m.state, key=s.idempotencyKey.value
          s.creationRequest.value={planSource:'recipe',projectId:'project',recipeVersionId:'recipe',idempotencyKey:key,scriptMode:'downstream',episodeIds:['episode']}
          api.createProductionRun=async()=>{
            if(outcome==='400'||outcome==='409')throw Object.assign(Error('rejected'),{response:{status:Number(outcome)}})
            return run(outcome==='no-id'?'':'created',{planSource:outcome==='wrong-plan'?'manual':'recipe',project:outcome==='foreign'?'foreign':'project'})
          }
          await s.createRun()
          assert.equal(s.creationResult.value,null);assert.equal(Boolean(s.creationRequest.value),outcome!=='400')
          assert.equal(s.idempotencyKey.value===key,outcome!=='400');assert.equal(m.router.currentRoute.value.params.runId,undefined)
        }finally{m.app.unmount()}
      }
    })
    await t.test('a read preserves an unresolved creation but explicitly entering another creation uses a new key',async()=>{
      reset();const m=await mount('/projects/project/production?view=new&source=recipe')
      try{
        const s=m.state,key=s.idempotencyKey.value
        s.creationRequest.value={planSource:'recipe',projectId:'project',recipeVersionId:'recipe',idempotencyKey:key,scriptMode:'downstream',episodeIds:['episode']}
        await s.load();assert.equal(s.creationRequest.value.idempotencyKey,key);assert.equal(s.idempotencyKey.value,key)
        await m.router.replace({query:{view:'new',source:'manual'}});await settle()
        assert.equal(s.creationRequest.value,null);assert.notEqual(s.idempotencyKey.value,key)
        const manualKey=s.idempotencyKey.value
        await m.router.replace({query:{view:'new',source:'recipe'}});await settle()
        assert.notEqual(s.idempotencyKey.value,manualKey);assert.equal(s.creationRequest.value,null)
      }finally{m.app.unmount()}
    })
    await t.test('committed adoption queues both reads while a new editor or child action protects the context', async () => {
      for (const lock of ['manualWorkbenchProtected', 'recipeShotSelectionProtected', 'actionBusy']) {
        reset(); const m = await mount()
        try {
          const s = m.state, originalRun = s.currentRun.value
          let runReads = 0, projectReads = 0
          api.getProductionRun = async () => { runReads++; return run('run', { status: 'succeeded' }) }
          m.studio.loadProject = async () => { projectReads++; return project }
          const pending = s.refreshAdoptedContent()
          s[lock].value = lock === 'actionBusy' ? 'preparation' : true
          await pending; await settle()
          assert.equal(s.error.value, '', `${lock}: a skipped read is not a failed adoption refresh`)
          assert.equal(runReads, 0); assert.equal(projectReads, 0)
          assert.equal(s.currentRun.value, originalRun); assert.equal(s.reviewRulesReady.value, false)
          assert.ok(s.pendingContentRefresh.value); assert.equal(s.runActionBlocked.value, true)
          s[lock].value = lock === 'actionBusy' ? '' : false
          await settle()
          assert.equal(runReads, 1); assert.equal(projectReads, 1)
          assert.equal(s.currentRun.value.status, 'succeeded'); assert.equal(s.reviewRulesReady.value, true)
          assert.equal(s.pendingContentRefresh.value, null); assert.equal(s.error.value, '')
        } finally { m.app.unmount() }
      }
    })
    await t.test('opening an editor during adoption reads retains the draft and rereads both facts after release', async () => {
      for (const lock of ['manualWorkbenchProtected', 'recipeShotSelectionProtected', 'actionBusy']) {
        reset(); const m = await mount(), slowRun = deferred(), slowProject = deferred()
        try {
          const s = m.state, originalRun = s.currentRun.value
          let runReads = 0, projectReads = 0
          api.getProductionRun = () => { runReads++; return runReads === 1 ? slowRun.promise : Promise.resolve(run('run', { status: 'succeeded' })) }
          m.studio.loadProject = () => { projectReads++; return projectReads === 1 ? slowProject.promise : Promise.resolve(project) }
          const pending = s.refreshAdoptedContent(); await settle()
          assert.equal(runReads, 1); assert.equal(projectReads, 1)
          s[lock].value = lock === 'actionBusy' ? 'preparation' : true
          slowRun.resolve(run('run', { status: 'running' })); slowProject.resolve(project)
          await pending; await settle()
          assert.equal(s.error.value, '', `${lock}: a successful but invalidated read must be deferred`)
          assert.equal(s.currentRun.value, originalRun); assert.equal(s.reviewRulesReady.value, false)
          assert.ok(s.pendingContentRefresh.value)
          s[lock].value = lock === 'actionBusy' ? '' : false
          await settle()
          assert.equal(runReads, 2); assert.equal(projectReads, 2)
          assert.equal(s.currentRun.value.status, 'succeeded'); assert.equal(s.reviewRulesReady.value, true)
          assert.equal(s.pendingContentRefresh.value, null)
        } finally { m.app.unmount() }
      }
    })
    await t.test('adoption refresh owns its read across a slow poll and other refresh callbacks', async () => {
      for (const pollFails of [false, true]) {
        reset(); const m = await mount(), poll = deferred(), adopted = deferred()
        try {
          const s = m.state; let reads = 0
          api.getProductionRun = () => { reads++; return reads === 1 ? poll.promise : adopted.promise }
          const tick = [...timers.values()][0]!
          tick(); assert.equal(reads, 1)
          const pending = s.artifactSelected({ runId: 'run', scopeId: 'group' }); await settle()
          assert.equal(reads, 2)
          tick(); const concurrent = s.refreshRun(); await settle(); assert.equal(reads, 2)
          adopted.resolve(run('run', { status: 'succeeded' })); await pending
          assert.equal(await concurrent, false)
          assert.equal(s.reviewRulesReady.value, true); assert.match(s.notice.value, /已重新读取/)
          pollFails ? poll.reject(Error('old poll unavailable')) : poll.resolve(run('run', { status: 'failed' }))
          await settle()
          assert.equal(s.currentRun.value.status, 'succeeded'); assert.equal(s.error.value, '')
          assert.equal(s.pageReadFailed.value, false)
        } finally { adopted.resolve(run()); poll.resolve(run()); m.app.unmount() }
      }
    })
    await t.test('a genuine adoption read failure remains explicit even when an editor opens during the request', async () => {
      for (const failed of ['run', 'project', 'both']) {
        reset(); const m = await mount(), slow = deferred()
        try {
          const s = m.state, originalRun = s.currentRun.value; let reads = 0
          api.getProductionRun = () => { reads++; return failed !== 'project' ? slow.promise : Promise.resolve(run()) }
          m.studio.loadProject = () => failed !== 'run' ? slow.promise : Promise.resolve(project)
          const pending = s.refreshAdoptedContent(); await settle()
          s.manualWorkbenchProtected.value = true
          slow.reject(Error(`${failed} unavailable after committed adoption`)); await pending
          assert.match(s.error.value, /回执已确认/); assert.match(s.error.value, new RegExp(`${failed} unavailable`))
          assert.equal(s.reviewRulesReady.value, false); assert.deepEqual(s.currentReviewRules.value, {})
          assert.equal(s.runActionBlocked.value, true)
          assert.equal(s.currentRun.value, originalRun); assert.equal(s.pageReadFailed.value, false, 'a read error must not unmount a protected editor')
          assert.equal(s.pendingContentRefresh.value, null); assert.equal(s.notice.value, '')
          s.manualWorkbenchProtected.value = false; await settle()
          assert.equal(reads, 1, 'a real failure requires explicit reread, not a silent retry')
          api.getProductionRun = async () => run(); m.studio.loadProject = async () => project
          await s.load(); assert.equal(s.error.value, ''); assert.equal(s.reviewRulesReady.value, true)
        } finally { m.app.unmount() }
      }
    })
    await t.test('artifact request protection blocks polls/reload/collapse and old release cannot unlock a new intent', async () => {
      reset(); const m=await mount()
      try {
        const s=m.state, original=s.currentRun.value;let reads=0
        api.getProductionRun=async()=>{reads++;throw Error('must not read while original request is pending')}
        s.expandedStageId.value='stage'
        s.protectArtifactSelection({runId:'run',scopeId:'stage',key:'first',active:true})
        await s.load();assert.equal(await s.refreshRun(),false);s.toggleStageDisclosure('stage')
        assert.equal(reads,0);assert.equal(s.currentRun.value,original);assert.equal(s.expandedStageId.value,'stage')
        assert.equal(s.runActionBlocked.value,true)
        s.protectArtifactSelection({runId:'run',scopeId:'stage',key:'first',active:false})
        s.protectArtifactSelection({runId:'run',scopeId:'other',key:'next',active:true})
        s.protectArtifactSelection({runId:'run',scopeId:'stage',key:'first',active:false})
        assert.equal(s.artifactSelectionProtection.value.key,'next')
        s.protectArtifactSelection({runId:'foreign',scopeId:'other',key:'next',active:false})
        assert.equal(s.artifactSelectionProtection.value.key,'next')
      } finally {m.app.unmount()}
    })
    await t.test('queued adoption refresh cannot read or announce success after changing runs', async () => {
      reset(); const m = await mount()
      try {
        const s = m.state
        s.manualWorkbenchProtected.value = true
        await s.refreshAdoptedContent()
        assert.equal(s.error.value, ''); assert.ok(s.pendingContentRefresh.value)
        await m.router.push('/projects/project/production/other'); await settle()
        const count = calls.length
        s.notice.value = 'new run notice'; s.manualWorkbenchProtected.value = false; await settle()
        assert.equal(calls.length, count); assert.equal(s.currentRun.value.id, 'other')
        assert.equal(s.pendingContentRefresh.value, null); assert.equal(s.notice.value, 'new run notice')
      } finally { m.app.unmount() }
    })
    await t.test('new adoption waits for the previous project read before fetching fresh facts from the real store', async () => {
      reset(); const slowProject = deferred(), latestRun = deferred(), latestProject = deferred()
      const m = await mount('/projects/project/production/run', (studio, loadProject) => {
        // Keep the real Pinia loadProject request de-duplication, only stub its repository boundary.
        repository.getProject = async () => project
        studio.loadProject = loadProject
      })
      try {
        const s = m.state; let runReads = 0, projectReads = 0
        api.getProductionRun = () => { runReads++; return runReads === 1 ? Promise.resolve(run()) : latestRun.promise }
        repository.getProject = () => { projectReads++; return projectReads === 1 ? slowProject.promise : latestProject.promise }
        const first = s.artifactSelected({ runId: 'run', scopeId: 'group' }); await settle()
        const second = s.refreshAdoptedContent(); await settle()
        assert.equal(runReads, 1); assert.equal(projectReads, 1); assert.equal(s.reviewRulesReady.value, false)
        slowProject.resolve(project); await first; await second; await settle()
        assert.equal(runReads, 2); assert.equal(projectReads, 2, 'the newer adoption must not reuse the older in-flight project promise')
        assert.equal(s.reviewRulesReady.value, false); assert.equal(s.notice.value, '')
        latestRun.resolve(run('run', { status: 'succeeded' })); latestProject.resolve(project); await settle()
        assert.equal(s.currentRun.value.status, 'succeeded'); assert.equal(s.reviewRulesReady.value, true)
        assert.equal(s.notice.value, '', 'the superseded selection callback must not announce its success')
      } finally { slowProject.resolve(project); latestRun.resolve(run()); latestProject.resolve(project); m.app.unmount() }
    })
    await t.test('queued and in-flight adoption refreshes respect session reset and unmount', async () => {
      for (const boundary of ['session', 'unmount']) for (const queued of [true, false]) {
        reset(); const m = await mount(), slow = deferred()
        try {
          const s = m.state; let reads = 0
          api.getProductionRun = () => { reads++; return slow.promise }
          if (queued) s.manualWorkbenchProtected.value = true
          const pending = s.refreshAdoptedContent(); await settle()
          assert.equal(reads, queued ? 0 : 1)
          if (boundary === 'session') m.studio.resetSessionState(); else m.app.unmount()
          s.error.value = 'new context error'; s.notice.value = 'new context notice'
          s.manualWorkbenchProtected.value = false; slow.resolve(run('run', { status: 'succeeded' }))
          await pending; await settle()
          assert.equal(reads, queued ? 0 : 1); assert.equal(s.pendingContentRefresh.value, null)
          assert.equal(s.reviewRulesReady.value, false)
          assert.equal(s.error.value, 'new context error'); assert.equal(s.notice.value, 'new context notice')
        } finally { slow.resolve(run()); if (boundary !== 'unmount') m.app.unmount() }
      }
    })
    await t.test('adoption refresh needs both original reads, waits for child busy release and can recover by reading',async()=>{
      for(const failed of ['run','project','foreign-project']){
        reset();const m=await mount()
        try{
          const s=m.state;s.actionBusy.value='text-review'
          if(failed==='run')api.getProductionRun=async()=>{throw Error('run unavailable')}
          if(failed==='project')m.studio.loadProject=async()=>{throw Error('project unavailable')}
          if(failed==='foreign-project')m.studio.loadProject=async()=>({...project,id:'foreign'})
          const pending=s.refreshAdoptedContent();s.actionBusy.value='';await pending
          assert.equal(s.reviewRulesReady.value,false);assert.equal(s.notice.value,'');assert.match(s.error.value,/刷新失败/)
          api.getProductionRun=async()=>run();m.studio.loadProject=async()=>project
          await s.load();assert.equal(s.reviewRulesReady.value,true);assert.equal(s.error.value,'')
        }finally{m.app.unmount()}
      }
      reset();const m=await mount()
      try{
        const s=m.state;s.actionBusy.value='text-review'
        const pending=s.refreshAdoptedContent();s.actionBusy.value='';await pending
        assert.equal(s.reviewRulesReady.value,true);assert.equal(s.error.value,'')
        await s.artifactSelected({runId:'run',scopeId:'group'});assert.match(s.notice.value,/已重新读取/)
        const count=calls.length;await s.artifactSelected({runId:'foreign',scopeId:'group'});assert.equal(calls.length,count)
      }finally{m.app.unmount()}
    })
    await t.test('late adoption project reads cannot enable review or announce success on another run',async()=>{
      for(const failure of [false,true]){
        reset();const m=await mount(),slow=deferred()
        try{
          m.studio.loadProject=()=>slow.promise
          const pending=m.state.artifactSelected({runId:'run',scopeId:'group'});await settle()
          m.studio.loadProject=async()=>project;await m.router.push('/projects/project/production/other');await settle()
          m.state.notice.value='new notice';m.state.error.value='new error';m.state.reviewRulesReady.value=false
          if(failure)slow.reject(Error('old error'));else slow.resolve(project)
          await pending;assert.equal(m.state.notice.value,'new notice');assert.equal(m.state.error.value,'new error');assert.equal(m.state.reviewRulesReady.value,false)
        }finally{m.app.unmount()}
      }
    })
    await t.test('stage retry confirmation requires a current preview and preserves the exact failed set', async () => {
      reset()
      const retryRun = () => run('run', { planSource: 'recipe', status: 'failed', currentStageKey: 'media_generation',
        blockedCode: 'execution_primary_failed', retryContext: { expectedStageId: 'stage', expectedAttemptIds: ['failed-a'] },
        stages: [{ id: 'stage', stageKey: 'media_generation', status: 'failed', reasonCode: 'execution_primary_failed' }] })
      api.getProductionRun = async () => retryRun()
      const submitted: unknown[] = [], pending = deferred()
      api.retryProductionRun = (...args: unknown[]) => { submitted.push(JSON.parse(JSON.stringify(args))); return pending.promise }
      const m = await mount()
      try {
        const s = m.state
        s.requestRunConfirmation('retry')
        assert.equal(s.retryPreviewReady.value, false)
        await s.confirmRunAction()
        assert.match(s.pendingRunConfirmationError.value, /先读取/); assert.equal(submitted.length, 0)
        // Preview rendering/ready emission is covered by productionRetryPreview.test.ts.
        s.retryPreviewReady.value = true
        s.currentRun.value.retryContext.expectedAttemptIds.push('later-failure')
        assert.deepEqual(s.pendingRunConfirmation.value.retryContext.expectedAttemptIds, ['failed-a'])
        await s.confirmRunAction()
        assert.match(s.pendingRunConfirmationError.value, /范围缺失或已变化/); assert.equal(submitted.length, 0)
        s.currentRun.value = retryRun(); s.requestRunConfirmation('retry')
        assert.equal(s.retryPreviewReady.value, false)
        s.retryPreviewReady.value = true
        const first = s.confirmRunAction()
        await s.confirmRunAction()
        assert.deepEqual(submitted, [['run', { expectedStageId: 'stage', expectedAttemptIds: ['failed-a'] }]])
        assert.equal(s.actionBusy.value, 'retry')
        pending.resolve(run('run', { planSource: 'recipe', status: 'running' })); await first
        assert.equal(s.currentRun.value.status, 'running'); assert.equal(s.pendingRunConfirmation.value, null)
        assert.equal(s.actionBusy.value, ''); assert.match(s.notice.value, /事实均已重新读取/)
      } finally { m.app.unmount() }
    })
    await t.test('stage retry rejects changed stage and unknown remote state without posting', async () => {
      for (const boundary of ['stage', 'unknown']) {
        reset()
        api.getProductionRun = async () => run('run', { planSource: 'recipe', status: 'failed', currentStageKey: 'media_generation',
          blockedCode: boundary === 'unknown' ? 'uncertain_remote_response' : 'execution_primary_failed',
          retryContext: { expectedStageId: 'stage', expectedAttemptIds: ['failed-a'] },
          stages: [{ id: 'stage', stageKey: 'media_generation', status: 'failed' }] })
        let writes = 0
        api.retryProductionRun = async () => { writes++; throw Error('must not submit') }
        const m = await mount()
        try {
          const s = m.state; s.requestRunConfirmation('retry'); s.retryPreviewReady.value = true
          if (boundary === 'stage') s.currentRun.value.stages[0].id = 'replacement'
          await s.confirmRunAction()
          assert.equal(writes, 0)
          assert.match(s.pendingRunConfirmationError.value, boundary === 'stage' ? /当前阶段已变化/ : /远端结果状态不确定/)
        } finally { m.app.unmount() }
      }
    })
    await t.test('shared sections filter recipe stages and preserve unrelated draft and unknown steps',async()=>{
      reset()
      const stages=['script_generate','visual_rules_review','bible_image_generation','storyboard_review','media_generation','assembly','custom_step'].map((stageKey,i)=>({id:'s'+i,stageKey,status:'pending'}))
      api.getProductionRun=async()=>run('run',{planSource:'recipe',status:'awaiting_input',currentStageKey:'media_generation',stages})
      const m=await mount('/projects/project/production/run?step=media')
      try{
        const s=m.state
        assert.equal(s.productionTab.value,'media');assert.equal(s.currentStageVisible.value,true)
        assert.deepEqual(s.visibleStages.value.map(row=>row.stageKey),['media_generation'])
        s.projectInputsProtected.value=true;s.productionTab.value='delivery'
        assert.equal(s.runActionBlocked.value,true);assert.equal(s.currentStageVisible.value,false)
        assert.deepEqual(s.visibleStages.value.map(row=>row.stageKey),['assembly'])
        s.productionTab.value='records'
        assert.equal(s.visibleStages.value.length,7)
        s.productionTab.value='bible'
        assert.deepEqual(s.visibleStages.value.map(row=>row.stageKey),['bible_image_generation'])
        assert.equal(s.projectInputsProtected.value,true)
      }finally{m.app.unmount()}
    })
    await t.test('manual creation callback is bound to the visible creation project and failed navigation stays local',async()=>{
      reset();const m=await mount('/projects/project/production?view=new&source=manual')
      try{
        await m.state.manualRunCreated(run('created',{project:'foreign'}))
        assert.match(m.state.error.value,/不一致/);assert.equal(m.state.currentRun.value,null)
        const slow=deferred(),replace=m.router.replace;m.router.replace=()=>slow.promise
        const pending=m.state.manualRunCreated(run('created'));await m.router.push('/projects/project/production/other');await settle()
        m.state.error.value='new error';slow.reject(Error('old navigation'));await pending
        assert.equal(m.state.error.value,'new error');assert.equal(m.state.currentRun.value.id,'other')
        m.router.replace=replace
      }finally{m.app.unmount()}
    })
  } finally {
    Object.assign(api, originalApi); Object.assign(repository, originalRepository)
    http.defaults.adapter = originalAdapter
    globalThis.localStorage = previousStorage; globalThis.window = previousWindow; await vite.close()
  }
  assert.deepEqual(unexpectedHttp, [], 'every business request must be explicitly stubbed; none may reach a real API')
})
