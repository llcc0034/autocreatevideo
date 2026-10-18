import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'

const root = fileURLToPath(new URL('..', import.meta.url))
const id = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
const profileId = id(1), definitionId = id(2), versionId = id(3)
const item = (unit = 'shot:first', key = 'custom_motion', version = versionId, definition = definitionId) => ({
  unit_key: unit, field_contract: { field_key: key, field_definition: {
    schema_version: 1, field_definition_id: definition, field_definition_version_id: version,
    key, label: '冻结的动态字段', version: 2,
  } },
})
function saved(items = [item(), item('shot:second')], profile = profileId): any {
  return { schema_version: 1, inputs: { logical_values: { motion: 'saved input' } }, parameters: { values: {} },
    profile_sources: { profile: { version_id: profile, contract_sha256: 'a'.repeat(64) } },
    field_requests: [{ schema_version: 1, size_bytes: 1024, sha256: 'b'.repeat(64), value: {
      schema_version: 1, operation: 'prompt.fields.generate', profile: { version_id: profile, contract_sha256: 'a'.repeat(64) }, items,
    } }],
  }
}
const relation = (kind: string, objectId: string, definition = definitionId) => ({
  id: objectId, kind, label: '当前目录名称不得替换冻结名称', status: 'retired', groups: [], notices: [], capabilities: [],
  config_url: kind === 'prompt' ? `/settings?section=prompts&prompt_profile_version=${objectId}`
    : `/settings?section=fields&field_definition=${definition}&field_version=${objectId}`,
  overview_url: '/settings/acceptance-smoke?view=relations', acceptance_url: '',
})
const page = (rows: any[], count = rows.length, next: string | null = null) => ({
  data: { count, next, previous: null, generated_at: 'fixture', context_label: '', results: rows },
})
const flush = async () => { await nextTick(); await new Promise(resolve => setImmediate(resolve)); await nextTick() }

async function harness(context: any, value = saved()) {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom',
    server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true } })
  let app: any
  context.after(async () => { app?.unmount(); await vite.close() })
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const { default: Facts } = await vite.ssrLoadModule('/src/components/ExecutionPreparationFacts.vue')
  const calls: { params: any; signal: AbortSignal }[] = []
  let writes = 0
  api.defaults.adapter = async () => assert.fail('No real network is permitted')
  for (const method of ['post', 'put', 'patch', 'delete']) api[method] = async () => { writes++; assert.fail('Viewing sources must never write') }
  let reply = async (params: any): Promise<any> => page(params.ids.split(',').map((objectId: string) => relation(params.kind, objectId)))
  api.get = async (url: string, options: any) => {
    assert.equal(url, '/registry-relations/')
    calls.push({ params: { ...options.params }, signal: options.signal })
    return reply(options.params)
  }
  const props = reactive({ resolvedInputs: value })
  let state: any
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
    insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  app = renderer.createApp({ setup() { state = Facts.setup(props, { expose() {} }); return () => h('div') } })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({})
  const render = async () => {
    const Snapshot = { ...Facts, setup: () => state }
    const view = createSSRApp({ render: () => h(Snapshot, props) })
    view.config.warnHandler = message => assert.fail(message)
    return renderToString(view)
  }
  return { props, state, calls, render, writes: () => writes, unmount: () => app.unmount(),
    reply: (fn: typeof reply) => { reply = fn },
    open: async () => { state.expanded.value = true; state.sourcesExpanded.value = true; await flush() },
  }
}

test('source navigation is lazy, deduplicated and uses frozen multilingual identities rather than directory names', async context => {
  const input = saved([item(), item('shot:second'), item('shot:first', 'custom_sound', id(5), id(4))])
  input.field_requests[0].value.items[2].field_contract.field_definition.label = '冻结的动态字段'
  const before = JSON.stringify(input)
  const view = await harness(context, input)
  view.reply(async params => page(params.ids.split(',').map((objectId: string) => relation(params.kind, objectId, objectId === id(5) ? id(4) : definitionId))))
  await flush(); assert.equal(view.calls.length, 0)
  view.state.expanded.value = true; await flush(); assert.equal(view.calls.length, 0)
  assert.doesNotMatch(await view.render(), /冻结的动态字段|href=/)
  await view.open()
  assert.equal(view.calls.length, 2)
  assert.deepEqual(view.calls.find(call => call.params.kind === 'field_version')!.params.ids.split(','), [versionId, id(5)])
  const html = await view.render()
  assert.equal((html.match(/冻结的动态字段 · 定义 v2/g) || []).length, 3)
  assert.match(html, /shot:first/); assert.match(html, /shot:second/)
  assert.match(html, /custom_motion/); assert.match(html, /custom_sound/)
  assert.match(html, new RegExp(`prompt_profile_version=${profileId}`))
  assert.match(html, new RegExp(`field_definition=${definitionId}&amp;field_version=${versionId}`))
  assert.match(html, /relation_context=field_version%3A/)
  assert.match(html, /target="_blank"/); assert.match(html, /noopener noreferrer/)
  assert.doesNotMatch(html, /当前目录名称不得替换冻结名称/)
  assert.equal(JSON.stringify(input), before); assert.equal(view.writes(), 0)
})

test('missing, empty, duplicate and conflicting saved source identities never get guessed links', async context => {
  const view = await harness(context)
  const variants: { value: any; message: RegExp }[] = [
    { value: { inputs: {}, parameters: {} }, message: /未记录完整/ },
    { value: { ...saved(), field_requests: [] }, message: /没有辅助字段批次/ },
    { value: saved([item(), item()]), message: /重复单元字段/ },
  ]
  const broken = saved(); delete broken.field_requests[0].value.items[0].field_contract.field_definition.field_definition_version_id
  variants.push({ value: broken, message: /定义身份或版本缺失/ })
  const renamed = saved(); renamed.field_requests[0].value.items[1].field_contract.field_definition.label = '矛盾名称'
  variants.push({ value: renamed, message: /矛盾的冻结定义/ })
  const wrongProfile = saved(); wrongProfile.profile_sources.profile.version_id = id(9)
  variants.push({ value: wrongProfile, message: /冻结主策略不一致/ })
  const otherBatch = saved(); otherBatch.field_requests.push(saved([item('other')], id(8)).field_requests[0])
  variants.push({ value: otherBatch, message: /冻结策略身份或摘要矛盾/ })
  const otherVersion = saved([item(), item('shot:other', 'custom_motion', id(6))])
  variants.push({ value: otherVersion, message: /矛盾的冻结定义/ })
  for (const { value, message } of variants) {
    view.props.resolvedInputs = value; await view.open()
    const html = await view.render()
    assert.match(html, message); assert.doesNotMatch(html, /href=/)
  }
  assert.equal(view.calls.length, 0); assert.equal(view.writes(), 0)
})

test('partial directory failure and 403 preserve frozen facts and expose a read-only retry', async context => {
  const view = await harness(context)
  view.reply(async params => {
    if (params.kind === 'field_version') throw { response: { status: 403, data: { detail: '没有查看字段配置的权限' } } }
    return page([relation('prompt', profileId)])
  })
  await view.open()
  const failed = await view.render()
  assert.match(failed, /没有查看字段配置的权限/); assert.match(failed, /重新读取字段入口/)
  assert.match(failed, /冻结的动态字段/); assert.match(failed, /prompt_profile_version=/)
  assert.doesNotMatch(failed, /href="[^"]*field_version=/)
  view.reply(async params => page(params.ids.split(',').map((objectId: string) => relation(params.kind, objectId))))
  await view.state.fieldRelations.reload()
  assert.match(await view.render(), /href="[^"]*field_version=/)
  assert.equal(view.calls.filter(call => call.params.kind === 'prompt').length, 1)
  assert.equal(view.writes(), 0)
})

test('missing or foreign directory rows and navigation identities cannot masquerade as the saved version', async context => {
  const view = await harness(context)
  for (const mode of ['missing', 'foreign', 'wrong-link', 'wrong-definition', 'duplicate-param']) {
    view.reply(async params => {
      if (params.kind === 'prompt') return page([relation('prompt', profileId)])
      if (mode === 'missing') return page([])
      if (mode === 'foreign') return page([relation('field_version', id(99))])
      const row = relation('field_version', versionId, mode === 'wrong-definition' ? id(99) : definitionId)
      if (mode === 'wrong-link') row.config_url = row.config_url.replace(versionId, id(99))
      if (mode === 'duplicate-param') row.config_url += `&field_version=${versionId}`
      return page([row])
    })
    await view.open(); await view.state.fieldRelations.reload()
    const html = await view.render()
    assert.doesNotMatch(html, /href="[^"]*field_version=/, mode)
    assert.match(html, /不存在|错误对象|冻结身份不一致/, mode)
    assert.match(html, /冻结的动态字段/)
  }
})

test('switching preparations and closing the panel cancel old reads and ignore late responses', async context => {
  const view = await harness(context)
  const pending: { params: any; resolve: (value: any) => void }[] = []
  view.reply(params => new Promise(resolve => pending.push({ params, resolve })))
  await view.open(); assert.equal(pending.length, 2)
  const oldSignals = view.calls.map(call => call.signal)
  view.props.resolvedInputs = saved([item('shot:new', 'new_key', id(13), id(12))], id(11))
  await flush()
  assert.equal(view.state.sourcesExpanded.value, false)
  assert.ok(oldSignals.every(signal => signal.aborted))
  assert.doesNotMatch(await view.render(), /href=/)
  view.reply(async params => page(params.ids.split(',').map((objectId: string) => relation(params.kind, objectId, id(12)))))
  await view.open()
  for (const request of pending) request.resolve(page(request.params.ids.split(',').map((objectId: string) => relation(request.params.kind, objectId))))
  await flush()
  const current = await view.render()
  assert.match(current, /new_key/); assert.match(current, new RegExp(`prompt_profile_version=${id(11)}`))
  assert.doesNotMatch(current, new RegExp(`prompt_profile_version=${profileId}|field_version=${versionId}`))
  pending.length = 0
  view.state.sourcesExpanded.value = false; await flush()
  view.reply(params => new Promise(resolve => pending.push({ params, resolve })))
  await view.open()
  view.state.expanded.value = false; await flush()
  assert.equal(view.state.sourcesExpanded.value, false)
  for (const request of pending) request.resolve(page([]))
  await flush(); assert.doesNotMatch(await view.render(), /href=|不存在/)
  view.unmount(); assert.ok(view.calls.every(call => call.signal.aborted))
})

test('same-ID preparation replacement clears links and the shared reader respects bounded pagination', async context => {
  const items = Array.from({ length: 55 }, (_, index) => item('shot:many', `custom_${index}`, id(100 + index), id(200 + index)))
  const view = await harness(context, saved(items))
  view.reply(async params => {
    const ids: string[] = params.ids.split(',')
    const start = (params.page - 1) * 20
    const rows = ids.slice(start, start + 20).map(objectId => relation(params.kind, objectId,
      id(200 + (Number.parseInt(objectId.slice(-12), 16) - 100))))
    return page(rows, ids.length, start + 20 < ids.length ? '/next-page' : null)
  })
  await view.open()
  assert.equal(view.state.sourceRows.value.length, 55)
  assert.ok(view.state.sourceRows.value.every((row: any) => row.link.url && !row.link.error))
  assert.ok(view.calls.every(call => call.params.ids.split(',').length <= 50))
  assert.equal(view.calls.filter(call => call.params.kind === 'field_version').length, 4)
  view.props.resolvedInputs = saved(items)
  await flush(); assert.equal(view.state.sourcesExpanded.value, false)
  assert.doesNotMatch(await view.render(), /href=/)
  assert.equal(view.writes(), 0)
})

test('frozen labels and units are escaped and technical identities remain readable without changing input facts', async context => {
  const data = saved([item('<script>unit</script>', 'any_custom_field')])
  data.field_requests[0].value.items[0].field_contract.field_definition.label = '<script>label</script>动作'
  const before = JSON.stringify(data), view = await harness(context, data)
  await view.open()
  const html = await view.render()
  assert.match(html, /&lt;script&gt;label&lt;\/script&gt;动作/)
  assert.match(html, /&lt;script&gt;unit&lt;\/script&gt;/)
  assert.match(html, new RegExp('a'.repeat(64)))
  assert.doesNotMatch(html, /<script>|<input|<textarea/)
  assert.equal(JSON.stringify(data), before)
})

async function isolatedFixture(context: any) {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom',
    server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true } })
  let isolation: any
  context.after(async () => { isolation?.restore(); await vite.close() })
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const fixture = await vite.ssrLoadModule('/tests/fixtures/execution-field-configuration-sources.ts')
  let forwarded = 0
  const native = {
    fetch: async (_input?: unknown, _options?: unknown) => { forwarded++; throw Error('Unexpected real transport') },
    XMLHttpRequest: { prototype: { send() { forwarded++; throw Error('Unexpected XHR transport') } } },
    navigator: { sendBeacon() { forwarded++; return true } },
  }
  const originalFetch = native.fetch
  isolation = fixture.installFixtureHttpIsolation(api, native)
  return { api, native, isolation, originalFetch, forwarded: () => forwarded }
}

test('browser fixture blocks HTTP writes, generic Axios calls, adapter overrides and raw browser transports', async context => {
  const fixture = await isolatedFixture(context)
  const { api, native, isolation } = fixture
  let overridden = 0
  const adapter = async () => { overridden++; throw Error('A request-specific adapter must never execute') }
  const blocked = /隔离夹具已拦截未声明的业务 HTTP 请求/
  // This also proves the guard runs before authentication/localStorage access;
  // the Node environment has no real browser login state.
  for (const method of ['post', 'put', 'patch', 'delete', 'head', 'options', 'postForm', 'putForm', 'patchForm']) {
    await assert.rejects(() => api[method]('/registry-relations/', {}), blocked, method)
  }
  await assert.rejects(() => api.request({ method: 'post', url: '/write/', adapter }), blocked)
  await assert.rejects(() => api({ method: 'put', url: '/write/', adapter }), blocked)
  await assert.rejects(() => api.request({ method: 'get', url: '/undeclared/', adapter }), blocked)
  await assert.rejects(() => api.request({ method: 'get', url: '/registry-relations/', adapter }), blocked)
  await assert.rejects(() => native.fetch('/write/', { method: 'POST' } as any), blocked)
  assert.throws(() => native.XMLHttpRequest.prototype.send(), blocked)
  assert.throws(() => native.navigator.sendBeacon(), blocked)
  assert.equal(isolation.reads.value, 0)
  assert.equal(isolation.blockedRequests.value, 16)
  assert.equal(overridden, 0); assert.equal(fixture.forwarded(), 0)
  isolation.restore(); assert.equal(native.fetch, fixture.originalFetch)
})

test('browser fixture declares only exact mock directory GETs and rejects unknown URLs, IDs and query shapes', async context => {
  const { api, isolation, forwarded } = await isolatedFixture(context)
  const params = { kind: 'prompt', ids: profileId, page: 1, page_size: 50 }
  const blocked = /隔离夹具已拦截未声明的业务 HTTP 请求/
  for (const [url, options] of [
    ['/undeclared/', { params }],
    ['https://api.example.invalid/registry-relations/', { params }],
    ['/registry-relations/?unexpected=1', { params }],
    ['/registry-relations/', {}],
    ['/registry-relations/', { params: { ...params, kind: 'execution' } }],
    ['/registry-relations/', { params: { ...params, ids: id(999) } }],
    ['/registry-relations/', { params: { ...params, ids: `${profileId},${profileId}` } }],
    ['/registry-relations/', { params: { ...params, search: 'anything' } }],
    ['/registry-relations/', { params: { ...params, page_size: 51 } }],
    ['/registry-relations/', { params: { ...params, page: 0 } }],
    ['/registry-relations/', { params, method: 'post' }],
    ['/registry-relations/', { params, adapter: async () => assert.fail('No adapter may run') }],
  ] as const) await assert.rejects(() => api.get(url, options), blocked)
  assert.equal(isolation.reads.value, 0)
  assert.equal(isolation.blockedRequests.value, 12)
  const result = await api.get('/registry-relations/', { params })
  assert.equal(result.data.results[0].id, profileId)
  const fields = await api.get('/registry-relations/', { params: { ...params, kind: 'field_version', ids: versionId } })
  assert.equal(fields.data.results[0].id, versionId)
  assert.equal(isolation.reads.value, 2); assert.equal(forwarded(), 0)
})
