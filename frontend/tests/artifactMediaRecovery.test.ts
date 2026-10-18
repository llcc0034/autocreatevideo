import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'

const root = fileURLToPath(new URL('..', import.meta.url))
const settle = async () => { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }
const artifact = (id = 'video') => ({ id, status: 'available', kind: id === 'image' ? 'image' : 'video',
  mimeType: id === 'image' ? 'image/png' : 'video/mp4', objectKey: `${id}.media`, variantIndex: 1, sha256: 'a'.repeat(64) })
const preview = (suffix = 'fresh') => ({ previewUrl: `/isolated/${suffix}`, downloadUrl: `/isolated/${suffix}/download`,
  parameters: {}, backendStatus: 'available' })

async function component(fn: (ctx: any) => Promise<void>) {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let app: any
  try {
    const { platformApi: api } = await vite.ssrLoadModule('/src/services/platformApi.ts')
    const { default: Component } = await vite.ssrLoadModule('/src/components/GeneratedArtifactCompare.vue')
    const calls: any[] = []
    api.getArtifactPreview = async row => { calls.push(['preview', row.id]); return preview() }
    api.selectArtifact = async (run, body, key) => { calls.push(['select', run, body, key]); return {id: crypto.randomUUID(), action: 'select', result_kind: 'artifact_selection', target_id: crypto.randomUUID(), created_at: new Date().toISOString()} }
    const props = reactive({ runId: 'run', artifacts: [artifact(), artifact('image')], materialSelection: false })
    let state: any
    const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
      insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    app = renderer.createApp({ setup() { state = Component.setup(props, { expose() {}, emit: (...args) => calls.push(['emit', ...args]) }); return () => h('div') } })
    app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await settle()
    await fn({ api, props, state, calls, vite })
  } finally { app?.unmount(); await vite.close() }
}

test('media GET/decode error blocks only the failing candidate; explicit refresh recovers it', () => component(async ctx => {
  const { state, props, calls } = ctx
  assert.equal(state.canSelectCandidate(props.artifacts[0]), true)
  for (const row of props.artifacts) {
    state.mediaEvents(row.id, state.previews.value[row.id]).error()
    assert.match(state.mediaErrors.value[row.id], /加载失败/)
    assert.equal(state.canSelectCandidate(row), false)
    await state.select(row)
  }
  assert.equal(calls.filter(row => row[0] === 'select').length, 0)
  await state.loadPreview(props.artifacts[0]); await settle()
  assert.equal(state.canSelectCandidate(props.artifacts[0]), true)
  assert.equal(state.canSelectCandidate(props.artifacts[1]), false)
  assert.equal(calls.filter(row => row[0] === 'select').length, 0)
}))

test('overlapping signature refreshes ignore older success and old media events', () => component(async ctx => {
  const { state, props, api } = ctx
  const oldEvents = state.mediaEvents('video', state.previews.value.video)
  let resolveOld: any
  api.getArtifactPreview = () => new Promise(resolve => { resolveOld = resolve })
  const oldRead = state.loadPreview(props.artifacts[0])
  api.getArtifactPreview = async () => preview('newest')
  await state.loadPreview(props.artifacts[0])
  resolveOld(preview('stale')); await oldRead
  oldEvents.error()
  assert.equal(state.previews.value.video.previewUrl, '/isolated/newest')
  assert.equal(state.mediaErrors.value.video, '')
}))

test('failure status never renders selectable media even if URL is present; API failure is recoverable', () => component(async ctx => {
  const { state, props, api, calls } = ctx
  api.getArtifactPreview = async () => ({ ...preview(), backendStatus: 'invalid' })
  await state.loadPreview(props.artifacts[0])
  assert.equal(state.canSelectCandidate(props.artifacts[0]), false)
  assert.match(state.mediaErrors.value.video, /状态/)
  api.getArtifactPreview = async () => { throw new Error('isolated 403') }
  await state.loadPreview(props.artifacts[0])
  assert.equal(state.previews.value.video, null)
  assert.match(state.errors.value.video, /isolated 403/)
  props.artifacts[0].status = 'invalid'; await settle()
  assert.equal(state.canSelectCandidate(props.artifacts[0]), false)
  await state.select(props.artifacts[0])
  assert.equal(calls.filter(row => row[0] === 'select').length, 0)
}))

test('run change with identical artifact IDs reloads preview; late response cannot enter new run', () => component(async ctx => {
  const { state, props, api } = ctx
  let resolveOld: any
  api.getArtifactPreview = () => new Promise(resolve => { resolveOld = resolve })
  const oldRead = state.loadPreview(props.artifacts[0])
  api.getArtifactPreview = async () => preview('new-run')
  props.runId = 'new-run'; await settle()
  resolveOld(preview('old-run')); await oldRead
  assert.equal(state.previews.value.video.previewUrl, '/isolated/new-run')
}))

test('lost media selection retains original request across status and preview refresh; replay is not a current choice', () => component(async ctx => {
  const {state,props,api,calls}=ctx;const sent:any[]=[]
  api.selectArtifact=async(...args)=>{sent.push(structuredClone(args));if(sent.length===1)throw Error('lost');return{id:crypto.randomUUID(),action:'select',result_kind:'artifact_selection',target_id:crypto.randomUUID(),created_at:new Date().toISOString()}}
  await state.select(props.artifacts[0]);assert.ok(state.selectionState.pending)
  props.artifacts=props.artifacts.map(row=>({...row,status:row.id==='image'?'selected':'invalid'}));await settle()
  await state.loadAll();assert.ok(state.selectionState.pending)
  await state.command.submit();assert.deepEqual(sent[0],sent[1]);assert.equal(state.selectionState.pending,null)
  assert.equal(props.artifacts[1].status,'selected');assert.equal(props.artifacts[0].status,'invalid')
  assert.ok(calls.some(row=>row[0]==='emit'&&row[1]==='selected'&&row[2].runId==='run'))
}))

test('signature refresh always reads original detail API instead of reusing manifest cached URL', () => component(async ctx => {
  // Restore the actual API method in a separate Vite module instance.
  const apiModule = await ctx.vite.ssrLoadModule('/src/services/platformApi.ts?fresh-preview-test')
  let reads = 0
  apiModule.platformApi.getArtifact = async () => { reads++; return { ...artifact(), previewUrl: `/renewed/${reads}`, downloadUrl: '', comparisonParameters: {}, manifestMetadata: {} } }
  const row = { ...artifact(), previewUrl: '/expired', manifestMetadata: {} }
  assert.equal((await apiModule.platformApi.getArtifactPreview(row)).previewUrl, '/renewed/1')
  assert.equal((await apiModule.platformApi.getArtifactPreview(row)).previewUrl, '/renewed/2')
}))

test('changes outside the four visible candidates do not leave cleared previews empty', () => component(async ctx => {
  const { state, props } = ctx
  props.artifacts = ['a','b','c','d','hidden'].map(artifact); await settle()
  assert.deepEqual(state.selectedIds.value, ['a','b','c','d'])
  props.artifacts[4].status = 'invalid'; await settle()
  for (const id of state.selectedIds.value) assert.equal(state.previews.value[id]?.previewUrl, '/isolated/fresh')
}))
