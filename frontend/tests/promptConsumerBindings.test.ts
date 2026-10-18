import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'

const flush = async () => { await nextTick(); await new Promise(resolve => setImmediate(resolve)); await nextTick() }
const deferred = () => { let resolve!: (value: any) => void; let reject!: (value: any) => void; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const owner = (id = 'owner', profile = 'profile', auxiliaries: any[] = []) => ({ id, instance_name: '同名实例', version: id === 'owner' ? 2 : 1, status: 'retired', prompt_mode: 'profile', prompt_profile_version_id: profile, configuration: { prompt_mode: 'profile', prompt_profile_version_id: profile, auxiliaries } })
const relationPage = (ids: string[], next: string | null = null) => ({ count: 3, next, previous: null, generated_at: '', context_label: 'exact', results: ids.map(id => ({ id, kind: 'execution', label: '同名实例 · ' + id, status: 'draft', config_url: '/settings', overview_url: '/settings', acceptance_url: '', groups: [], notices: [], capabilities: [] })) })

test('strategy consumer binding reads are exact, lazy and resilient', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } }); context.after(() => vite.close())
  const { executionRegistryApi } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const { readPromptConsumerBindings: read } = await vite.ssrLoadModule('/src/services/promptConsumerBindings.ts')
  const { default: Panel } = await vite.ssrLoadModule('/src/components/PromptConsumerBindings.vue')
  const calls: string[] = []
  const binding = (purpose: string, target_version_id = 'aux') => ({ purpose, target_version_id })
  await context.test('same target is read once but every saved purpose remains', async () => {
    calls.length = 0
    executionRegistryApi.version = async (id: string) => { calls.push(id); return id === 'owner' ? owner('owner', 'profile', [binding('generate'), binding('translate')]) : owner(id) }
    const result = await read('profile', 'owner')
    assert.deepEqual(calls, ['owner', 'aux']); assert.equal(result.bindings.length, 2)
    assert.equal(result.bindings[1].purpose, 'translate'); assert.equal(result.bindings[0].target.status, 'retired')
  })
  await context.test('owner identity and both strategy references are checked', async () => {
    for (const invalid of [owner('wrong'), owner('owner','other'), { ...owner(), configuration: { ...owner().configuration, prompt_profile_version_id: 'other' } }, { ...owner(), prompt_mode: 'manual' }]) {
      executionRegistryApi.version = async () => invalid
      await assert.rejects(read('profile', 'owner'), /引用已变化/)
    }
    executionRegistryApi.version = async () => owner('owner', 'profile', [binding('same'), binding('same')])
    await assert.rejects(read('profile', 'owner'), /用途为空或重复/)
  })
  await context.test('partial failure keeps precise target and is not an empty-success result', async () => {
    executionRegistryApi.version = async (id: string) => id === 'owner' ? owner('owner', 'profile', [binding('a', 'wrong-id'), binding('b', 'failed'), binding('c', 'ok')]) : id === 'failed' ? Promise.reject(Error('read denied')) : owner(id === 'wrong-id' ? 'another' : id)
    const result = await read('profile', 'owner')
    assert.match(result.bindings[0].error, /身份不匹配/); assert.equal(result.bindings[0].target_version_id, 'wrong-id')
    assert.ok(result.bindings[1].error); assert.equal(result.bindings[2].target.id, 'ok')
    executionRegistryApi.version = async () => owner()
    assert.deepEqual((await read('profile','owner')).bindings, [])
  })
  const renderer = createRenderer<any,any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, insert() {}, remove() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  const mount = () => {
    const props = reactive({ profileId: 'profile' }); let state: any
    const app = renderer.createApp({ setup() { state = Panel.setup(props, { expose() {}, emit() {} }); return () => h('div') } }); app.provide(ssrContextKey, {}); app.mount({})
    return { props, state, app }
  }
  await context.test('lazy explicit selection with server next pagination and error retry', async () => {
    const requests: any[] = []; calls.length = 0
    api.get = async (_url: string, config: any) => { requests.push(config.params); return { data: relationPage(['owner'], 'next') } }
    executionRegistryApi.version = async (id: string) => { calls.push(id); return owner(id) }
    const { state, app } = mount()
    assert.equal(requests.length, 0); state.toggle({ target: { open: true } }); await flush()
    assert.equal(calls.length, 0); assert.equal(state.selectedId.value, '')
    assert.deepEqual(requests[0], { kind:'execution',source_kind:'prompt',source_id:'profile',relation:'executions',page:1,page_size:25 })
    assert.equal(state.data.value.next, 'next') // Do not infer end from the single-row page.
    await state.select('owner'); assert.equal(state.detail.value.owner.id, 'owner')
    api.get = async () => { throw Error('page two failed') }
    await state.load(2); assert.equal(state.data.value, null); assert.equal(state.detail.value, null); assert.match(state.error.value, /page two failed/)
    app.unmount()
  })
  await context.test('late owner success/failure and old profile page never replace current context', async () => {
    api.get = async () => ({ data: relationPage(['owner', 'other']) })
    const { state, props, app } = mount(); state.toggle({ target: { open: true } }); await flush()
    const slow = deferred(); executionRegistryApi.version = (id: string) => id === 'owner' ? slow.promise : Promise.resolve(owner(id))
    const first = state.select('owner'); await state.select('other'); slow.resolve(owner()); await first
    assert.equal(state.detail.value.owner.id, 'other')
    const late = deferred(); executionRegistryApi.version = () => late.promise
    const second = state.select('owner'); props.profileId = 'next-profile'; await flush(); late.reject(Error('old failure')); await second
    assert.equal(state.detail.value, null); assert.equal(state.bindingError.value, '')
    const oldPage = deferred(); api.get = () => oldPage.promise
    const pending = state.load(); state.toggle({ target: { open: false } }); oldPage.resolve({ data: relationPage(['old']) }); await pending
    assert.equal(state.data.value, null)
    const gone = state.load(); app.unmount(); await gone; assert.equal(state.data.value, null)
  })
})
