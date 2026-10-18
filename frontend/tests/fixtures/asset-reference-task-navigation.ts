// Browser-only acceptance fixture: synthetic records/media, no live business API.
import { createApp, h, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import AssetReferenceBrowser from '../../src/components/AssetReferenceBrowser.vue'
import { api } from '../../src/services/api'
import type { AssetReferenceSeries, AssetReferenceVersion } from '../../src/services/assetReferenceApi'
import baseStyles from '../../src/styles.css?inline'
import '../../src/platform.css'

// Handle both Vite's dev url(...) import and its minified quoted form; font
// URLs contain semicolons, so a declaration cannot be split at the first ';'.
export const fixtureBaseStyles = baseStyles.replace(/@import\s*(?:url\([^)]*\)|"[^"]*"|'[^']*')\s*;/g, '')
const id = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
export const fixtureChoices = [
  ['attempt-a', '同 Run · 原调用 A'], ['attempt-b', '同 Run · 原调用 B'],
  ['asset-job', '独立素材 job'], ['missing-attempt', '缺失 attempt（不得猜测）'],
] as const
export type FixtureMode = typeof fixtureChoices[number][0]
export const fixtureRunId = id(40)
export function fixtureRecords(mode: FixtureMode) {
  const index = fixtureChoices.findIndex(([key]) => key === mode)
  if (index < 0) throw Error('未声明的夹具场景')
  const label = fixtureChoices[index]![1]
  const series: AssetReferenceSeries = { id: id(1), name: `合成系列 · ${label}`, role: 'primary', owner_id: 1,
    primary_series_id: null, bible_variant_id: null, version_count: 1, variant_count: 0 }
  const version: AssetReferenceVersion = {
    id: id(11 + index), series_id: series.id, version: index + 1, source: 'generated',
    asset: { id: id(21 + index), name: `合成素材 · ${label}`, kind: 'reference', mime_type: 'image/svg+xml', is_active: true },
    generated_from_id: null, setting_revision_id: null,
    generated_artifact: mode === 'asset-job' ? null : { id: id(31 + index), status: mode === 'attempt-b' ? 'selected' : 'available',
      production_run_id: fixtureRunId, generation_attempt_id: mode === 'missing-attempt' ? null : id(51 + index) },
    generation_job: mode === 'asset-job' ? { id: id(70), status: 'succeeded' } : null,
  }
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="720" height="400"><rect width="720" height="400" fill="#152028"/><text x="32" y="150" fill="#e4a557" font-size="28" font-family="sans-serif">ISOLATED FIXTURE</text><text x="32" y="220" fill="white" font-size="24" font-family="sans-serif">${label}</text><text x="32" y="280" fill="#b6c1c8" font-size="18" font-family="sans-serif">data: media / no real generation</text></svg>`
  const media = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
  const asset = { ...version.asset, preview_url: media, reference_url: media, download_url: '',
    metadata: {}, source_info: '合成 data: 图片；不是模型调用或真实生产结果。' }
  return { series, version, asset }
}

type FixtureTransport = {
  fetch?: typeof fetch
  XMLHttpRequest?: { prototype: Pick<XMLHttpRequest, 'send'> }
  navigator?: Pick<Navigator, 'sendBeacon'>
}
// All other HTTP, including api(config), request adapter overrides, writes,
// unexpected GET/OPTIONS, fetch, XHR and beacon, fails before any transport.
export function installFixtureHttpIsolation(mode: { value: FixtureMode }, client = api, transport: FixtureTransport = globalThis) {
  const reads = ref(0), blockedRequests = ref(0), lastBlocked = ref('')
  function block(label: string): never {
    blockedRequests.value++; lastBlocked.value = label
    throw Error(`隔离夹具已阻断业务 HTTP：${label}`)
  }
  const oldGet = client.get, oldOptions = client.options, oldAdapter = client.defaults.adapter
  const oldFetch = transport.fetch, oldSend = transport.XMLHttpRequest?.prototype.send, oldBeacon = transport.navigator?.sendBeacon
  const interceptor = client.interceptors.request.use(config => block(`${config.method} ${config.url}`))
  client.defaults.adapter = async () => block('Axios transport')
  const response = (data: unknown) => { reads.value++; return { data: structuredClone(data) } }
  const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
  const page = <T>(rows: T[], number: number) => ({ count: rows.length, next: null, previous: null, results: number === 1 ? rows : [] })
  client.options = (async (url: string, config?: unknown) => {
    if (url !== '/asset-reference-series/' || config !== undefined) return block(`OPTIONS ${url}`)
    return response({ actions: { POST: { role: { choices: [
      { value: 'primary', display_name: '主图' }, { value: 'variant', display_name: '变体' },
    ] } } } })
  }) as typeof client.options
  client.get = (async (url: string, config?: unknown) => {
    const { series, version, asset } = fixtureRecords(mode.value)
    if (config === undefined) {
      if (url === `/assets/${asset.id}/`) return response(asset)
      if (url === `/asset-reference-series/${series.id}/`) return response(series)
      if (url === `/asset-reference-versions/${version.id}/`) return response(version)
    }
    if (!record(config) || Object.keys(config).some(key => key !== 'params') || !record(config.params)) return block(`GET ${url}`)
    const params = config.params
    if (!Number.isSafeInteger(params.page) || Number(params.page) < 1) return block(`GET ${url} page`)
    if (url === '/asset-reference-series/') {
      if (Object.keys(params).some(key => !['page', 'search', 'role', 'media_type', 'bible_linked', 'primary_series_id'].includes(key))
        || typeof params.search !== 'string'
        || ('role' in params && !['primary', 'variant'].includes(String(params.role)))
        || ('media_type' in params && !['image', 'video', 'audio'].includes(String(params.media_type)))
        || ('bible_linked' in params && typeof params.bible_linked !== 'boolean')
        || ('primary_series_id' in params && params.primary_series_id !== series.id)) return block(`GET ${url} filters`)
      const matches = !params.primary_series_id && params.role !== 'variant' && params.bible_linked !== true
        && (!params.media_type || params.media_type === 'image') && series.name.includes(params.search)
      return response(page(matches ? [series] : [], Number(params.page)))
    }
    if (url === '/asset-reference-versions/' && params.series_id === series.id
      && Object.keys(params).every(key => ['series_id', 'page'].includes(key))) return response(page([version], Number(params.page)))
    return block(`GET ${url}`)
  }) as typeof client.get
  if (oldFetch) transport.fetch = async () => block('fetch')
  if (oldSend) transport.XMLHttpRequest!.prototype.send = () => block('XMLHttpRequest.send')
  if (oldBeacon) transport.navigator!.sendBeacon = () => block('sendBeacon')
  return { reads, blockedRequests, lastBlocked, restore() {
    client.interceptors.request.eject(interceptor)
    client.get = oldGet; client.options = oldOptions; client.defaults.adapter = oldAdapter
    if (oldFetch) transport.fetch = oldFetch
    if (oldSend) transport.XMLHttpRequest!.prototype.send = oldSend
    if (oldBeacon) transport.navigator!.sendBeacon = oldBeacon
  } }
}

export function createFixtureApp(transport: FixtureTransport = globalThis) {
  const mode = ref<FixtureMode>('attempt-a'), revision = ref(0), navigationError = ref('')
  const isolation = installFixtureHttpIsolation(mode, api, transport)
  const empty = { render: () => null }
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', name: 'fixture', component: empty },
    { path: '/assets', name: 'assets', component: empty },
    { path: '/workflows', name: 'workflows', component: empty },
  ] })
  async function choose(value: FixtureMode) {
    if (!fixtureChoices.some(([key]) => key === value)) throw Error('未声明的夹具场景')
    mode.value = value; revision.value++; navigationError.value = ''; await router.push({ name: 'fixture' })
  }
  // Capture modified clicks too: a memory RouterLink href must never open a real
  // /workflows or /assets page in this window or another tab.
  function interceptNavigation(event: MouseEvent) {
    const link = (event.target as Element | null)?.closest('a')
    if (!link) return
    event.preventDefault(); event.stopPropagation()
    if (event.type === 'contextmenu') return
    const href = link.getAttribute('href') || ''
    if (!/^\/(?:assets|workflows)(?:\?|$)/.test(href)) { navigationError.value = '已阻断非夹具导航'; return }
    return router.push(href)
  }
  const Root = { setup() { return () => h('main', { class: 'fixture-shell',
    onClickCapture: interceptNavigation, onAuxclickCapture: interceptNavigation, onContextmenuCapture: interceptNavigation,
  }, [
    // Reuse production CSS without its remote Google Fonts import.
    h('style', fixtureBaseStyles),
    h('h1', '素材引用任务导航 · 隔离验证'),
    h('p', '全部记录和媒体均为合成夹具。先选场景，再点下方唯一系列，查看来源任务链接。不会读取现用数据、执行模型或保存业务修改。'),
    h('div', { class: 'fixture-choices', 'aria-label': '切换合成来源夹具' }, fixtureChoices.map(([value, label]) => h('button', {
      type: 'button', class: 'button ghost', 'aria-pressed': mode.value === value, onClick: () => choose(value),
    }, label))),
    h('p', { role: 'status' }, `当前夹具：${fixtureChoices.find(([value]) => value === mode.value)![1]}；模拟读取 ${isolation.reads.value} 次；已阻断 HTTP ${isolation.blockedRequests.value} 次。`),
    h('p', 'HTTP 网络出口关闭；仅允许静态页面/模块加载与本机 Vite HMR。任务链接只展示内存路由查询，不加载真实任务页。'),
    isolation.lastBlocked.value ? h('p', { role: 'alert' }, `已阻断：${isolation.lastBlocked.value}`) : null,
    navigationError.value ? h('p', { role: 'alert' }, navigationError.value) : null,
    router.currentRoute.value.name === 'fixture'
      ? h(AssetReferenceBrowser, { key: revision.value })
      : h('section', { class: 'panel', 'aria-label': '隔离导航查询' }, [
        h('h2', '内存路由结果（不是实际任务详情）'),
        h('pre', { class: 'fixture-query', 'data-testid': 'navigation-query' }, JSON.stringify({
          name: router.currentRoute.value.name, path: router.currentRoute.value.path, query: router.currentRoute.value.query,
        }, null, 2)),
        h('button', { type: 'button', class: 'button primary', onClick: () => router.push({ name: 'fixture' }) }, '返回素材引用夹具'),
      ]),
  ]) } }
  const app = createApp(Root); app.use(router)
  return { app, Root, router, mode, isolation, choose, interceptNavigation }
}

if (typeof document !== 'undefined') {
  const fixture = createFixtureApp()
  void fixture.router.push({ name: 'fixture' }).then(() => fixture.app.mount('#app'))
}
