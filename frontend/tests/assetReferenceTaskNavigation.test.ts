import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { build, createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { AssetReferenceSeries, AssetReferenceVersion } from '../src/services/assetReferenceApi.ts'

const root = fileURLToPath(new URL('..', import.meta.url))
const id = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
const series: AssetReferenceSeries = {
  id: id(1), name: '隔离来源测试系列', role: 'primary', owner_id: 1, primary_series_id: null,
  bible_variant_id: null, version_count: 2, variant_count: 0,
}
const version = (number = 1): AssetReferenceVersion => ({
  id: id(10 + number), series_id: series.id, version: number, source: 'generated',
  asset: { id: id(20 + number), name: `测试素材 v${number}`, kind: 'image', mime_type: 'image/png', is_active: true },
  generated_from_id: null, setting_revision_id: null,
  generated_artifact: { id: id(30 + number), status: number === 1 ? 'available' : 'selected', production_run_id: id(40), generation_attempt_id: id(50 + number) },
  generation_job: null,
})

async function harness(context: any) {
  // Middleware-only compilation and SSR: no listening server, browser, media or business API.
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom',
    server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const requests: string[] = []
  const block = (request: string): never => {
    requests.push(request)
    throw new Error(`Unexpected business HTTP in isolated navigation test: ${request}`)
  }
  // There are no declared GETs. Reject every Axios request before auth/transport,
  // including writes with per-request adapters; block raw fetch as well.
  api.interceptors.request.use((config: any) => block(`${config.method} ${config.url}`))
  api.defaults.adapter = async () => block('axios transport')
  context.mock.method(globalThis, 'fetch', async () => block('fetch'))
  const { default: Browser } = await vite.ssrLoadModule('/src/components/AssetReferenceBrowser.vue')
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/assets', name: 'assets', component: { render: () => null } },
    { path: '/workflows', name: 'workflows', component: { render: () => null } },
  ] })
  await router.push('/assets'); await router.isReady()
  const render = async (selected: AssetReferenceVersion, props: Record<string, unknown> = {}) => {
    const before = JSON.stringify(selected)
    const Snapshot = { ...Browser, setup(componentProps: any, setupContext: any) {
      const result = Browser.setup(componentProps, setupContext)
      // The directory contains a newer version; links must use the selected historical fact.
      Object.assign(result.state, { selected: series, version: selected,
        versions: { count: 2, next: null, previous: null, results: [version(2), version(1)] } })
      return result
    } }
    const app = createSSRApp({ render: () => h(Snapshot, props) })
    app.use(router)
    app.config.warnHandler = message => assert.fail(message)
    const html = await renderToString(app)
    assert.deepEqual(requests, [], 'Rendering navigation must never request business HTTP')
    assert.equal(JSON.stringify(selected), before, 'Navigation must not rewrite the saved source')
    const links = [...html.matchAll(/<a\b[^>]*href="([^"]+)"[^>]*>(.*?)<\/a>/gs)]
      .map(match => ({ route: router.resolve(match[1]!.replaceAll('&amp;', '&')), label: match[2]! }))
      .filter(link => link.route.name === 'workflows')
    return { html, links }
  }
  return { render }
}

test('generated artifacts link to the selected original attempt, separating calls within the same run', async context => {
  const view = await harness(context)
  for (const number of [1, 2, 1]) {
    const selected = version(number)
    const { links, html } = await view.render(selected)
    assert.equal(links.length, 1)
    assert.deepEqual(links[0]!.route.query, {
      category: 'production', source_id: id(50 + number), production_run_id: id(40),
    })
    assert.equal(links[0]!.label, '查看原生产调用')
    assert.match(html, new RegExp(`产物状态：${selected.generated_artifact!.status}`))
    assert.doesNotMatch(links[0]!.label, /available|selected|succeeded/)
    assert.match(html, new RegExp(`调用：${id(50 + number)}`))
    assert.doesNotMatch(html, /生产运行任务/)
  }
})

test('an exact generation attempt remains navigable when production run identity is absent', async context => {
  const view = await harness(context)
  const selected = version()
  selected.generated_artifact!.production_run_id = null
  const { links } = await view.render(selected)
  assert.equal(links.length, 1)
  assert.deepEqual(links[0]!.route.query, { category: 'production', source_id: id(51) })
})

test('missing attempt IDs never fall back to the run, artifact, asset, reference or latest attempt', async context => {
  const view = await harness(context)
  for (const missing of [null, '', undefined]) {
    const selected = version()
    if (missing === undefined) Reflect.deleteProperty(selected.generated_artifact!, 'generation_attempt_id')
    else selected.generated_artifact!.generation_attempt_id = missing
    const { links, html } = await view.render(selected)
    assert.deepEqual(links, [])
    assert.match(html, /生成调用 ID 未记录，无法定位原调用/)
    assert.match(html, /调用：未记录/)
  }
})

test('independent asset jobs retain their exact source ID without a production locator', async context => {
  const view = await harness(context)
  const selected = version()
  selected.generated_artifact = null
  selected.generation_job = { id: id(70), status: 'succeeded' }
  const { links, html } = await view.render(selected)
  assert.equal(links.length, 1)
  assert.deepEqual(links[0]!.route.query, { category: 'asset', source_id: id(70) })
  assert.match(links[0]!.label, /素材生成任务 · succeeded/)
  assert.doesNotMatch(html, /查看原生产调用|产物状态|无法定位/)
})

test('a missing asset job ID cannot produce a category-only task link', async context => {
  const view = await harness(context)
  for (const missing of ['', null, undefined]) {
    const selected = version()
    selected.generated_artifact = null
    // Explicitly malformed historical response, not a new API contract.
    selected.generation_job = { id: missing, status: 'succeeded' } as any
    const { links, html } = await view.render(selected)
    assert.deepEqual(links, [])
    assert.match(html, /素材生成任务 ID 未记录，无法定位原任务/)
  }
})

test('source-less versions and input-selection mode do not offer inferred task navigation', async context => {
  const view = await harness(context)
  for (const source of ['upload', 'existing', 'generated'] as const) {
    const selected = version()
    selected.source = source; selected.generated_artifact = null
    const { links } = await view.render(selected)
    assert.deepEqual(links, [])
  }
  const { links } = await view.render(version(), { selectionKind: 'image' })
  assert.deepEqual(links, [])
})

async function browserFixture(context: any) {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom',
    server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true } })
  let fixture: any
  context.after(async () => { fixture?.isolation.restore(); await vite.close() })
  const module = await vite.ssrLoadModule('/tests/fixtures/asset-reference-task-navigation.ts')
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  let forwarded = 0
  const transport = {
    fetch: async (..._args: any[]) => { forwarded++; throw Error('Unexpected fetch transport') },
    XMLHttpRequest: { prototype: { send() { forwarded++; throw Error('Unexpected XHR transport') } } },
    navigator: { sendBeacon() { forwarded++; return true } },
  }
  const originalFetch = transport.fetch
  fixture = module.createFixtureApp(transport)
  return { vite, module, api, fixture, transport, originalFetch, forwarded: () => forwarded }
}

test('browser fixture supplies four explicit sources through the real read-only browser flow and data media', async context => {
  const { vite, module, api, fixture, forwarded } = await browserFixture(context)
  assert.doesNotMatch(module.fixtureBaseStyles, /@import|fonts\.googleapis\.com/)
  assert.match(module.fixtureBaseStyles, /^\s*:root\s*\{/)
  const { createReferenceBrowser } = await vite.ssrLoadModule('/src/services/assetReferenceBrowser.ts')
  const { assetReferenceApi } = await vite.ssrLoadModule('/src/services/assetReferenceApi.ts')
  const { repository } = await vite.ssrLoadModule('/src/services/repository.ts')
  assert.equal((await assetReferenceApi.roles()).length, 2)
  const flow = createReferenceBrowser({ ...assetReferenceApi, getAsset: repository.getAsset })
  context.after(flow.dispose)
  const attempts: string[] = []
  for (const [mode] of module.fixtureChoices) {
    await fixture.choose(mode)
    await flow.search({ page: 1, search: '' })
    assert.equal(flow.state.catalog.results.length, 1)
    await flow.select(flow.state.catalog.results[0])
    const expected = module.fixtureRecords(mode)
    assert.deepEqual(JSON.parse(JSON.stringify(flow.state.version)), expected.version)
    assert.equal(flow.state.asset.id, expected.asset.id)
    assert.match(flow.state.asset.mediaUrl, /^data:image\/svg\+xml/)
    assert.match(decodeURIComponent(flow.state.asset.mediaUrl), /ISOLATED FIXTURE/)
    assert.equal(flow.state.variants.count, 0)
    assert.equal(flow.state.previewError, '')
    if (mode.startsWith('attempt-')) {
      assert.equal(flow.state.version.generated_artifact.production_run_id, module.fixtureRunId)
      attempts.push(flow.state.version.generated_artifact.generation_attempt_id)
    } else if (mode === 'asset-job') {
      assert.equal(flow.state.version.generation_job.id, id(70))
      assert.equal(flow.state.version.generated_artifact, null)
    } else assert.equal(flow.state.version.generated_artifact.generation_attempt_id, null)
  }
  assert.deepEqual(attempts, [id(51), id(52)])
  const empty = await api.get('/asset-reference-series/', { params: { page: 1, search: '不存在的系列' } })
  assert.equal(empty.data.count, 0)
  assert.equal(fixture.isolation.blockedRequests.value, 0)
  assert.equal(forwarded(), 0)
})

test('browser fixture rejects all writes, unlisted reads, adapter escapes and raw HTTP transports', async context => {
  const { api, fixture, transport, originalFetch, forwarded } = await browserFixture(context)
  let overridden = 0, blockedCount = 0
  const adapter = async () => { overridden++; throw Error('Unexpected per-request adapter') }
  const blocked = /隔离夹具已阻断业务 HTTP/
  for (const method of ['post', 'put', 'patch', 'delete', 'head', 'postForm', 'putForm', 'patchForm']) {
    await assert.rejects(() => api[method]('/asset-reference-series/', {}), blocked); blockedCount++
  }
  for (const method of ['get', 'post', 'put', 'delete']) {
    await assert.rejects(() => api.request({ method, url: '/asset-reference-series/', adapter }), blocked); blockedCount++
    await assert.rejects(() => api({ method, url: '/assets/', adapter }), blocked); blockedCount++
  }
  const params = { page: 1, search: '' }
  for (const [url, config] of [
    ['/undeclared/', undefined], ['https://example.invalid/assets/', undefined],
    [`/assets/${id(999)}/`, undefined], ['/asset-reference-series/?page=1', undefined],
    ['/asset-reference-series/', undefined], ['/asset-reference-series/', { params, method: 'post' }],
    ['/asset-reference-series/', { params, adapter }],
    ['/asset-reference-series/', { params: { ...params, page: 0 } }],
    ['/asset-reference-series/', { params: { ...params, unexpected: true } }],
    ['/asset-reference-series/', { params: { ...params, primary_series_id: id(999) } }],
    ['/asset-reference-series/', { params: { ...params, role: 'guessed' } }],
    ['/asset-reference-versions/', { params: { series_id: id(999), page: 1 } }],
  ]) { await assert.rejects(() => api.get(url, config), blocked); blockedCount++ }
  await assert.rejects(() => api.options('/assets/'), blocked); blockedCount++
  await assert.rejects(() => api.options('/asset-reference-series/', { adapter }), blocked); blockedCount++
  await assert.rejects(() => transport.fetch('/api/v1/write/', { method: 'POST' }), blocked); blockedCount++
  assert.throws(() => transport.XMLHttpRequest.prototype.send(), blocked); blockedCount++
  assert.throws(() => transport.navigator.sendBeacon(), blocked); blockedCount++
  assert.equal(fixture.isolation.blockedRequests.value, blockedCount)
  assert.equal(fixture.isolation.reads.value, 0)
  assert.equal(overridden, 0); assert.equal(forwarded(), 0)
  fixture.isolation.restore(); assert.equal(transport.fetch, originalFetch)
})

test('fixture task clicks, modified clicks and return stay in memory and display exact query values', async context => {
  const { fixture, forwarded } = await browserFixture(context)
  await fixture.choose('attempt-a')
  const snapshot = async () => {
    const app = createSSRApp(fixture.Root); app.use(fixture.router)
    app.config.warnHandler = message => assert.fail(message)
    return renderToString(app)
  }
  assert.match(await snapshot(), /主图、变体与版本/)
  for (const type of ['click', 'auxclick']) {
    let prevented = false, stopped = false
    const href = `/workflows?category=production&source_id=${id(51)}&production_run_id=${id(40)}`
    await fixture.interceptNavigation({ type, ctrlKey: true, target: { closest: () => ({ getAttribute: () => href }) },
      preventDefault: () => { prevented = true }, stopPropagation: () => { stopped = true } })
    assert.equal(prevented, true); assert.equal(stopped, true)
    assert.deepEqual(fixture.router.currentRoute.value.query, { category: 'production', source_id: id(51), production_run_id: id(40) })
    const html = await snapshot()
    assert.match(html, /内存路由结果（不是实际任务详情）/)
    assert.match(html, /source_id/); assert.match(html, new RegExp(id(51)))
    assert.match(html, /返回素材引用夹具/); assert.doesNotMatch(html, /主图、变体与版本/)
    await fixture.router.push({ name: 'fixture' })
    assert.match(await snapshot(), /主图、变体与版本/)
  }
  for (const href of ['https://example.invalid/workflows', '/api/v1/assets/']) {
    fixture.interceptNavigation({ type: 'click', target: { closest: () => ({ getAttribute: () => href }) }, preventDefault() {}, stopPropagation() {} })
    assert.equal(fixture.router.currentRoute.value.name, 'fixture')
    assert.match(await snapshot(), /已阻断非夹具导航/)
  }
  assert.equal(fixture.isolation.reads.value, 0)
  assert.equal(fixture.isolation.blockedRequests.value, 0)
  assert.equal(forwarded(), 0)
})

test('browser fixture HTML and real component compile without emitting files or starting a listener', async () => {
  const result = await build({ root, logLevel: 'silent', build: { write: false, emptyOutDir: false,
    rollupOptions: { input: fileURLToPath(new URL('./fixtures/asset-reference-task-navigation.html', import.meta.url)) } } })
  assert.ok(!Array.isArray(result) && 'output' in result)
  const html = result.output.find(item => item.type === 'asset' && item.fileName.endsWith('.html'))
  assert.ok(html && html.type === 'asset')
  assert.match(String(html.source), /Content-Security-Policy/)
  assert.match(String(html.source), /img-src data:/)
  assert.match(String(html.source), /form-action 'none'/)
  assert.ok(result.output.some(item => item.type === 'chunk' && item.isEntry))
})
