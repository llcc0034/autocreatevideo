import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, createRenderer, h, nextTick, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createRouter, createMemoryHistory } from 'vue-router'
import { instance, version } from './fixtures/execution-authoring-data.ts'

async function setup(context: any) {
  const vite = await createServer({ logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { executionRegistryApi: client } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  for (const method of ['get', 'post', 'patch', 'put', 'delete', 'request']) api[method] = () => assert.fail(`unexpected network ${method}`)
  client.publish = client.smoke = client.retire = () => assert.fail('no execution or lifecycle mutation')
  const { default: Editor } = await vite.ssrLoadModule('/src/components/ExecutionInstanceEditor.vue')
  return { vite, client, Editor }
}
function mount(Component: any, props: any, emit = (_event: string, ..._args: any[]) => {}) {
  let state: any
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  const app = renderer.createApp({ setup() { state = Component.setup(props, { expose() {}, emit }); return () => null } })
  app.provide(ssrContextKey, {}); app.mount({})
  return { state, close: () => app.unmount() }
}
async function registry(vite: any, client: any) {
  const { default: Registry } = await vite.ssrLoadModule('/src/components/ExecutionInstanceRegistry.vue')
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: { render: () => null } }] })
  await router.push('/settings'); await router.isReady()
  let state: any
  const app = createSSRApp({ setup() { state = Registry.setup({ deployments: [] }, { expose() {} }); return () => h('div') } })
  app.use(router); await renderToString(app)
  client.instances = async () => ({ count: 1, next: null, previous: null, results: [instance] })
  client.instance = async () => structuredClone(instance)
  client.versions = async () => ({ count: 1, next: null, previous: null, results: [version] })
  state.selected.value = structuredClone(instance)
  state.detail.value = structuredClone(version)
  state.editorOpen.value = true
  return state
}
test('instance identity author uses shared hierarchy and only the original create writer once', async context => {
  const { Editor, client } = await setup(context)
  const events: any[] = []
  const { state, close } = mount(Editor, { instance: null }, (...args) => events.push(args)); context.after(close)
  state.draft.value = { key: 'custom.reference', name: '新能力', description: '原值 🙂\n  保留空白 ' }
  let finish: (value: any) => void = () => {}, calls = 0
  client.createInstance = (payload: any) => { calls++; assert.deepEqual(payload, state.draft.value); return new Promise(resolve => { finish = resolve }) }
  const pending = state.save(); await state.save(); assert.equal(calls, 1)
  assert.match(state.status.value, /正在保存/)
  finish({ ...instance, ...state.draft.value }); await pending
  assert.equal(events.filter(row => row[0] === 'saved').length, 1)
  assert.equal(events.find(row => row[0] === 'saved')[2], false)
  const html = await renderToString(createSSRApp(Editor, { instance: null }))
  assert.match(html, /data-authoring-section="identity"/); assert.match(html, /data-authoring-section="versions"/)
  assert.match(html, /创建实例并配置首个版本/); assert.match(html, /不会发布版本/)
})
test('metadata true-difference patch preserves original revision and failed values including empty description', async context => {
  const { Editor, client } = await setup(context)
  const original = { ...instance, description: '旧说明' }
  const { state, close } = mount(Editor, { instance: original }); context.after(close)
  assert.equal(state.canSave.value, false)
  state.draft.value.description = ''
  client.updateInstance = async (id: string, patch: any) => {
    assert.equal(id, instance.id); assert.deepEqual(patch, { expected_revision: instance.revision, description: '' })
    throw { response: { status: 400, data: { fields: { description: ['隔离拒绝'] } } } }
  }
  await state.save()
  assert.equal(state.unresolved.value, false); assert.equal(state.draft.value.description, '')
  assert.equal(original.description, '旧说明'); assert.equal(state.issues.value[0].section, 'identity')
})
test('unknown create cannot resend or auto-adopt fuzzy matches; recovery loads an explicit exact identity only', async context => {
  const { Editor, client } = await setup(context)
  const events: any[] = []
  const { state, close } = mount(Editor, { instance: null }, (...args) => events.push(args)); context.after(close)
  state.draft.value = { key: instance.key, name: '同名不代表身份', description: '' }
  let writes = 0, reads = 0
  client.createInstance = async () => { writes++; throw { response: { status: 504 } } }
  await state.save(); await state.save(); assert.equal(writes, 1); assert.equal(state.unresolved.value, true)
  const unrelated = { ...instance, id: 'other', key: 'other', name: state.draft.value.name }
  client.instances = async (page: number, query: string) => { assert.equal(query, instance.key); return { results: page === 1 ? [unrelated] : [instance], next: page === 1 ? 'page2' : null } }
  client.instance = async (id: string) => { reads++; assert.equal(id, instance.id); return structuredClone(instance) }
  await state.readRecords(); assert.equal(state.hasMore.value, true); await state.useRecord(unrelated); assert.equal(reads, 0)
  await state.readRecords(2); assert.equal(state.records.value.length, 2); assert.equal(events.some(row => row[0] === 'saved'), false)
  await state.useRecord(instance); assert.equal(reads, 1); assert.equal(writes, 1)
  assert.equal(events.find(row => row[0] === 'saved')[2], true)
})
test('conflicting metadata uses exact readback; mismatched response and late responses cannot be adopted', async context => {
  const { Editor, client } = await setup(context)
  const events: any[] = []
  const { state, close } = mount(Editor, { instance }, (...args) => events.push(args))
  state.draft.value.name = '本地未保存'
  client.updateInstance = async () => { throw { response: { status: 409 } } }
  await state.save(); assert.equal(state.unresolved.value, true)
  client.instance = async () => ({ ...instance, id: 'wrong' })
  await state.readRecords(); assert.match(state.recoveryError.value, /身份不一致/); assert.deepEqual(state.records.value, [])
  let finish: (value: any) => void = () => {}
  client.instance = () => new Promise(resolve => { finish = resolve })
  const pending = state.readRecords(); close(); finish(instance); await pending
  assert.equal(events.some(row => row[0] === 'saved'), false); assert.equal(state.draft.value.name, '本地未保存')
})
test('new instance opens an empty first-version author; editing or recovered identity never creates a version', async context => {
  const { vite, client } = await setup(context)
  client.createVersion = () => assert.fail('opening author must not write version')
  const state = await registry(vite, client)
  state.editorOpen.value = false; state.openFamily(false)
  await state.familySaved(instance, false)
  assert.equal(state.familyOpen.value, false); assert.equal(state.editorOpen.value, true); assert.equal(state.detail.value, null)
  assert.equal(state.versionView.value, 'editor'); assert.match(state.notice.value, /尚未创建版本/)
  state.editorOpen.value = false; state.openFamily(true); await state.familySaved(instance, false)
  assert.equal(state.editorOpen.value, false); assert.match(state.notice.value, /已有版本配置/)
  state.openFamily(false); await state.familySaved(instance, true)
  assert.equal(state.editorOpen.value, false); assert.match(state.notice.value, /未重新保存/)
})
test('published successor uses exact source revision and SHA, writes once and preserves source references', async context => {
  const { vite, client } = await setup(context)
  const state = await registry(vite, client)
  const source = { ...structuredClone(version), status: 'published' }
  state.detail.value = source
  state.contract.value = { version_id: source.id, revision: source.revision, frozen: true, sha256: 'exact-sha' }
  let calls = 0, finish: (value: any) => void = () => {}
  client.clone = (id: string, payload: any) => {
    calls++; assert.equal(id, source.id); assert.deepEqual(payload, { expected_revision: source.revision, expected_definition_sha256: 'exact-sha' })
    return new Promise(resolve => { finish = resolve })
  }
  const pending = state.clone(); await state.clone(); assert.equal(calls, 1)
  const successor = { ...structuredClone(version), id: 'successor-exact', version: 3, revision: 1 }
  finish(successor); await pending
  assert.equal(state.detail.value.id, successor.id); assert.equal(state.cloneSource.value.id, source.id)
  assert.deepEqual(source, { ...version, status: 'published' }); assert.match(state.notice.value, /原版本及使用方引用不变/)
  state.detail.value = source; state.contract.value = { version_id: source.id, revision: source.revision + 1, frozen: true, sha256: 'stale-sha' }
  await state.clone(); assert.equal(calls, 1); assert.ok(state.detailError.value)
})
test('unknown successor requires scoped paginated readback and never guesses the newest version or resends', async context => {
  const { vite, client } = await setup(context)
  const state = await registry(vite, client)
  const successor = { ...structuredClone(version), id: 'successor-exact', version: 3 }
  let writes = 0, reads = 0
  client.clone = async (_id: string, payload: any) => { assert.equal(payload.expected_definition_sha256, null); writes++; throw Error('unknown result') }
  await state.clone(); await state.clone(); state.createVersion(); await state.openVersion(successor)
  assert.equal(writes, 1); assert.equal(state.detail.value.id, version.id)
  client.versions = async (page: number, filter: any) => { assert.equal(filter.instance, instance.id); return { count: 2, results: page === 1 ? [version] : [successor], next: page === 1 ? 'page2' : null } }
  await state.readCloneRecords(); await state.readCloneRecords(2)
  assert.equal(state.cloneRecords.value.length, 2); assert.equal(state.detail.value.id, version.id)
  client.version = async (id: string) => { reads++; assert.equal(id, successor.id); return successor }
  await state.loadCloneRecord(version); assert.equal(reads, 0)
  await state.loadCloneRecord(successor); assert.equal(reads, 1); assert.equal(writes, 1)
  assert.equal(state.cloneUnknown.value, null); assert.equal(state.cloneSource.value, null)
  assert.match(state.notice.value, /未重新保存或认定变更来源/)
})
test('invalid clone responses cannot be presented as saved; explicit rejection preserves source for correction', async context => {
  const { vite, client } = await setup(context)
  const state = await registry(vite, client)
  client.clone = async () => { throw { response: { status: 400, data: { message: '需要修正' } } } }
  await state.clone(); assert.equal(state.cloneUnknown.value, null); assert.equal(state.detail.value.id, version.id)
  client.clone = async () => ({ ...version, id: 'other-version', instance_id: 'wrong-instance' })
  await state.clone(); assert.equal(state.cloneUnknown.value.id, version.id); assert.equal(state.detail.value.id, version.id)
  client.versions = async () => ({ results: [{ ...version, instance_id: 'wrong-instance' }], next: null })
  await state.readCloneRecords(); assert.match(state.cloneReadError.value, /其他实例/); assert.deepEqual(state.cloneRecords.value, [])
})
test('discarding an editor clears session flags before readonly overview and exact successor creation', async context => {
  const { vite, client } = await setup(context)
  const state = await registry(vite, client)
  state.editorDirty.value = true; state.editorUncertain.value = true
  state.closeEditor(); assert.equal(state.editorOpen.value, true); assert.equal(state.editorDirty.value, true)
  assert.equal(state.leaveTarget.value, 'version'); state.leaveTarget.value = null
  assert.equal(state.editorOpen.value, true)
  state.closeEditor(); state.discardAuthoring()
  assert.equal(state.editorOpen.value, false); assert.equal(state.editorDirty.value, false); assert.equal(state.editorUncertain.value, false)
  const source = { ...version, status: 'published' }
  client.version = async () => source
  client.contract = async () => ({ version_id: source.id, revision: source.revision, frozen: true, sha256: 'exact-sha' })
  await state.openVersion(source); assert.equal(state.versionView.value, 'overview')
  let clones = 0
  client.clone = async () => { clones++; return { ...version, id: 'successor' } }
  await state.clone(); assert.equal(clones, 1)
})
test('in-page family discard retains editing context until explicit choice and cannot close in flight', async context => {
  const { vite, client } = await setup(context)
  const state = await registry(vite, client)
  state.openFamily(false); state.familyDirty.value = true; state.closeFamily()
  assert.equal(state.leaveTarget.value, 'family'); assert.equal(state.familyOpen.value, true)
  state.leaveTarget.value = null; assert.equal(state.familyDirty.value, true)
  state.familyUncertain.value = true; state.closeFamily(); assert.match(state.leaveMessage.value, /不会撤销/)
  state.busy.value = true; state.discardAuthoring(); assert.equal(state.familyOpen.value, true)
  state.busy.value = false; state.discardAuthoring(); assert.equal(state.familyOpen.value, false); assert.equal(state.familyDirty.value, false)
  const { default: Notice } = await vite.ssrLoadModule('/src/components/ConfigurationDiscardNotice.vue')
  const html = await renderToString(createSSRApp(Notice, { message: '保存结果未知', busy: true }))
  assert.match(html, /role="alert"/); assert.equal((html.match(/disabled/g) || []).length, 2); assert.doesNotMatch(html, /type="submit"/)
})
test('parent lifecycle submission makes the entire source author inert without unmounting its values', async context => {
  const { vite } = await setup(context)
  const previousDocument = globalThis.document, previousElement = globalThis.HTMLElement
  globalThis.document = { activeElement: null } as any
  globalThis.HTMLElement = class {} as any
  context.after(() => {
    if (previousDocument === undefined) delete (globalThis as any).document; else globalThis.document = previousDocument
    if (previousElement === undefined) delete (globalThis as any).HTMLElement; else globalThis.HTMLElement = previousElement
  })
  const { default: Registry } = await vite.ssrLoadModule('/src/components/ExecutionInstanceRegistry.vue')
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: { render: () => null } }] })
  await router.push('/settings'); await router.isReady()
  const Wrapper = { ...Registry, setup(props: any, ctx: any) {
    const state = Registry.setup(props, ctx)
    state.selected.value = instance; state.detail.value = version; state.editorOpen.value = true; state.versionView.value = 'editor'; state.busy.value = true
    return state
  } }
  const app = createSSRApp(Wrapper, { deployments: [] }); app.use(router)
  const ssr: any = {}
  const pageHtml = await renderToString(app, ssr)
  const html = pageHtml + Object.values(ssr.teleports || {}).join('')
  await nextTick()
  assert.match(html, /<div[^>]*inert[^>]*aria-busy="true"[^>]*class="execution-editor-surface"/)
  assert.match(html, /正在提交配置操作/); assert.match(html, /草稿 v2/)
})
