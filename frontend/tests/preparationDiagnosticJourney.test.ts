import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import { readFileSync } from 'node:fs'
import { basename } from 'node:path'
import { fileURLToPath } from 'node:url'
import http from 'node:http'
import https from 'node:https'
import net from 'node:net'
import { build, createServer, transformWithEsbuild } from 'vite'
import vue from '@vitejs/plugin-vue'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import { createRenderer, nextTick } from 'vue'

const root = fileURLToPath(new URL('..', import.meta.url))
const config = { root, configFile: false as const, envFile: false as const, logLevel: 'silent' as const,
  appType: 'custom' as const, resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false as const, ws: false as const, watch: null },
}
const settle = async () => { for (let i = 0; i < 12; i++) { await nextTick(); await new Promise(resolve => setImmediate(resolve)) } }
const diagnosticLabel = '查看这次调用的请求、返回与产物证据'

// Compile original client SFC templates, including RouterLink and child
// components. Capture setup state for assertions, never replace its rules.
function clientComponents() {
  return { name: 'preparation-diagnostic-client-sfc', async load(filename: string) {
    if (!filename.endsWith('.vue')) return
    const { descriptor, errors } = parse(readFileSync(filename, 'utf8'), { filename })
    assert.deepEqual(errors, [])
    const script = compileScript(descriptor, { id: filename, genDefaultAs: '__sfc__' })
    const template = compileTemplate({ source: descriptor.template!.content, filename, id: filename,
      compilerOptions: { bindingMetadata: script.bindings, hoistStatic: false } })
    assert.deepEqual(template.errors, [])
    const code = `${script.content}\n${template.code}\nimport { getCurrentInstance as __instance } from 'vue';
      const __setup = __sfc__.setup;
      __sfc__.setup = (props, ctx) => { const state = __setup(props, ctx);
        globalThis.__preparationDiagnosticCapture?.(${JSON.stringify(basename(filename))}, { state, props, instance: __instance() }); return state; };
      __sfc__.render = render; export default __sfc__;`
    return (await transformWithEsbuild(code, filename + '.ts', { loader: 'ts', target: 'esnext' })).code
  } }
}

class HostElement {
  tag: string
  children: HostElement[] = []; parent: HostElement | null = null; props: Record<string, any> = {}; text = ''; style = {}
  value = ''; checked = false; listeners: Record<string, Function> = {}; classList = { add() {}, remove() {} }
  constructor(tag: string) { this.tag = tag }
  get tagName() { return this.tag.toUpperCase() }
  addEventListener(name: string, handler: Function) { this.listeners[name] = handler }
  removeEventListener(name: string) { delete this.listeners[name] }
  getAttribute(name: string) { return this.props[name] }
  setAttribute(name: string, value: unknown) { this.props[name] = value }
  removeAttribute(name: string) { delete this.props[name] }
  closest(tag: string): HostElement | null { return this.tag === tag ? this : this.parent?.closest(tag) || null }
  querySelectorAll() { return [] }
  focus() {}
}
const text = (node: HostElement): string => node.text + node.children.map(text).join('')
const descendants = (node: HostElement): HostElement[] => [node, ...node.children.flatMap(descendants)]

function guardNodeNetwork(t: TestContext) {
  const calls: string[] = []
  const deny = (name: string): never => { calls.push(name); throw Error(`Unexpected network/listener: ${name}`) }
  t.mock.method(globalThis, 'fetch', () => deny('fetch'))
  for (const module of [http, https]) for (const key of ['get', 'request'] as const) t.mock.method(module, key, () => deny(key))
  t.mock.method(net.Socket.prototype, 'connect', () => deny('socket.connect'))
  t.mock.method(net.Server.prototype, 'listen', () => deny('server.listen'))
  t.after(() => assert.deepEqual(calls, [], 'no network, provider or server listener'))
}

async function harness(t: TestContext, fixturePath = '/tests/fixtures/preparation-diagnostic-journey.ts') {
  guardNodeNetwork(t)
  const captures = new Map<string, any[]>(), warnings: string[] = []
  const globals = ['__preparationDiagnosticCapture', 'window', 'document', 'HTMLElement']
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
  let app: any, fixture: any
  const vite = await createServer({ ...config, plugins: [clientComponents()] })
  t.after(async () => {
    app?.unmount(); fixture?.restore(); await vite.close()
    for (const [key, descriptor] of globals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor); else Reflect.deleteProperty(globalThis, key)
    }
    assert.deepEqual(warnings, [], 'original component/router warnings')
  })
  Object.defineProperty(globalThis, '__preparationDiagnosticCapture', { configurable: true, value: (name: string, entry: any) => {
    captures.set(name, [...(captures.get(name) || []), entry])
  } })
  const module = await vite.ssrLoadModule(fixturePath)
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const transport = { fetch: globalThis.fetch,
    XMLHttpRequest: { prototype: { send() { assert.fail('XHR escaped fixture') } } },
    navigator: { sendBeacon() { assert.fail('beacon escaped fixture'); return false } }, confirm: () => true }
  fixture = module.createFixture(transport)
  await fixture.router.push('/fixture/preparation'); await fixture.router.isReady()
  Object.defineProperty(globalThis, 'window', { configurable: true, value: transport })
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { activeElement: null } })
  Object.defineProperty(globalThis, 'HTMLElement', { configurable: true, value: HostElement })
  const host = new HostElement('main'), body = new HostElement('body')
  const renderer = createRenderer<HostElement, HostElement>({
    createElement: tag => new HostElement(tag), createText: value => Object.assign(new HostElement('#text'), { text: value }),
    createComment: () => new HostElement('#comment'),
    insert(node, parent, anchor) {
      if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
      node.parent = parent
      const index = anchor ? parent.children.indexOf(anchor) : -1
      if (index < 0) parent.children.push(node); else parent.children.splice(index, 0, node)
    },
    remove(node) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = null },
    parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] || null,
    setText: (node, value) => { node.text = value }, setElementText: (node, value) => { node.text = value; node.children = [] },
    patchProp(node, key, _old, value) { node.props[key] = value; if (['value', 'checked'].includes(key)) (node as any)[key] = value },
    querySelector: selector => selector === 'body' ? body : null, setScopeId() {},
  })
  app = renderer.createApp(fixture.Root).use(fixture.pinia).use(fixture.router)
  app.config.warnHandler = (warning: string) => warnings.push(warning)
  app.mount(host); await settle()
  const nodes = (tag: string, label: string) => descendants(host).filter(node => node.tag === tag && text(node) === label)
  const one = (tag: string, label: string) => { const matches = nodes(tag, label); assert.equal(matches.length, 1, `${tag}: ${label}`); return matches[0]! }
  async function click(tag: string, label: string) {
    const node = one(tag, label); assert.ok(!node.props.disabled, `${label} enabled`)
    const event = { type: 'click', target: node, currentTarget: node, button: 0, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true }, stopPropagation() {} }
    await node.props.onClick(event); await settle()
  }
  const component = (name: string) => {
    const live = (captures.get(name + '.vue') || []).filter(entry => !entry.instance.isUnmounted)
    assert.equal(live.length, 1, `single original ${name}`); return live[0]
  }
  return { fixture, module, api, transport, host, nodes, one, click, component,
    workbench: () => component('ExecutionPreparationWorkbench').state,
    detail: () => component('TaskCenterDetail').state,
    async choose(mode: string) { await fixture.choose(mode); await settle() },
  }
}
type Harness = Awaited<ReturnType<typeof harness>>
function readonly(view: Harness) {
  assert.equal(view.fixture.isolation.blocked.value, 0)
  assert.ok(view.fixture.isolation.requests.value.every((row: any) => row.method === 'GET'))
  assert.ok(!view.fixture.events.value.includes('unexpected business changed event'))
}

test('original diagnostic RouterLink opens A through VueRouter and original detail adapter although latest is B', async t => {
  const v = await harness(t), { ids } = v.module
  assert.equal(v.workbench().preparation.value.presentation.task.id, ids.b)
  assert.equal(v.workbench().preparation.value.responseDiagnostic.taskId, ids.a)
  assert.deepEqual(v.fixture.isolation.requests.value.map((r: any) => r.url), [`/execution-preparations/${ids.preparation}/`])
  const link = v.one('a', diagnosticLabel)
  assert.deepEqual(v.fixture.router.resolve(link.props.href).query, { kind: 'preparation', id: ids.preparation, attempt_id: ids.a })
  await v.click('a', diagnosticLabel)
  assert.equal(v.fixture.router.currentRoute.value.name, 'workflows')
  assert.equal(v.component('TaskCenterDetail').props.attemptId, ids.a)
  assert.equal(v.detail().data.value.source_id, ids.a)
  assert.equal(v.detail().data.value.status, 'failed')
  assert.equal(v.detail().data.value.output_count, 0, 'retained text is not an artifact')
  assert.deepEqual(v.fixture.isolation.requests.value.at(-1), { method: 'GET', url: `/task-center/preparation/${ids.preparation}/`,
    query: { call_page: 1, output_page: 1, attempt_id: ids.a } })
  assert.ok(text(v.host).includes(`合成原提示词 ${ids.a}`)); assert.ok(!text(v.host).includes(`合成原提示词 ${ids.b}`))
  await v.click('button', '输出与校验')
  assert.ok(text(v.host).includes(`合成原返回 ${ids.a}`)); assert.ok(!text(v.host).includes(`合成原返回 ${ids.b}`))
  await v.click('button', '刷新此任务')
  assert.equal(v.fixture.isolation.requests.value.at(-1).query.attempt_id, ids.a)
  readonly(v)
})

test('original latest-return, memory back, production-return and reread only issue declared GETs', async t => {
  const v = await harness(t), { ids } = v.module
  await v.click('a', diagnosticLabel)
  await v.click('button', '返回最新调用')
  assert.equal(v.fixture.router.currentRoute.value.query.attempt_id, undefined)
  assert.equal(v.detail().data.value.source_id, ids.b)
  assert.equal(v.fixture.isolation.requests.value.at(-1).query.attempt_id, undefined)
  await v.click('button', '内存路由返回')
  assert.equal(v.fixture.router.currentRoute.value.query.attempt_id, ids.a)
  assert.equal(v.detail().data.value.source_id, ids.a)
  await v.click('a', '返回本轮生产工作台')
  assert.equal(v.workbench().preparation.value.responseDiagnostic.taskId, ids.a)
  await v.click('button', '刷新准备状态')
  assert.equal(v.workbench().error.value, '')
  await v.click('a', diagnosticLabel)
  assert.equal(v.detail().data.value.source_id, ids.a)
  readonly(v)
})

test('wrong task/project/call identities and missing adapter fields never display B or navigate to a replacement', async t => {
  const v = await harness(t), { ids } = v.module
  const variants: Record<string, unknown>[] = [
    { id: 'synthetic-other-preparation' }, { kind: 'assembly' }, { project_id: 'synthetic-other-project' },
    { source_kind: 'asset_job' }, { source_id: ids.b }, { source_id: null }, { source_id: '' }, { submission: undefined },
  ]
  for (const patch of variants) {
    v.fixture.responses.detail = (query: any) => ({ ...v.module.detailReceipt('production', query), ...patch })
    const before = v.fixture.isolation.requests.value.length
    await v.click('a', diagnosticLabel)
    assert.equal(v.detail().data.value, null, JSON.stringify(patch))
    assert.notEqual(v.detail().error.value, '')
    assert.equal(v.fixture.router.currentRoute.value.query.attempt_id, ids.a)
    assert.equal(v.fixture.isolation.requests.value.length, before + 1, 'no fallback request')
    assert.ok(!text(v.host).includes('合成原返回') && !text(v.host).includes('合成原提示词'))
    await v.click('a', '返回准备页（内存）')
  }
  readonly(v)
})

test('missing diagnostic identity or mismatched independent job leaves no replacement link', async t => {
  const v = await harness(t)
  for (const mode of ['missing-task', 'foreign-asset']) {
    await v.choose(mode)
    assert.equal(v.workbench().error.value, '')
    assert.equal(v.workbench().diagnosticTarget.value, null)
    assert.equal(v.nodes('a', diagnosticLabel).length, 0)
    assert.match(text(v.host), mode === 'foreign-asset' ? /诊断任务关联未核验，无法跳转/ : /缺少原任务身份/)
  }
  assert.ok(v.fixture.isolation.requests.value.every((r: any) => r.url.startsWith('/execution-preparations/')))
  readonly(v)
})

test('actual unsaved field form hides diagnostic navigation, cancel restores A without saving', async t => {
  const v = await harness(t)
  await v.choose('draft'); await v.click('button', '人工填写此字段')
  const textarea = descendants(v.host).find(node => node.tag === 'textarea')!
  assert.ok(textarea); const before = v.fixture.isolation.requests.value.length
  textarea.value = '合成未保存草稿'; textarea.listeners.input!({ target: textarea }); await settle()
  assert.equal(v.workbench().text.value, textarea.value)
  assert.equal(v.workbench().hasUnsavedDraft.value, true)
  assert.equal(v.nodes('a', diagnosticLabel).length, 0)
  assert.match(text(v.host), /请先完成当前编辑或重新读取准备/)
  assert.equal(v.fixture.isolation.requests.value.length, before)
  await v.click('button', '取消修改')
  assert.equal(v.workbench().hasUnsavedDraft.value, false)
  await v.click('a', diagnosticLabel)
  assert.equal(v.detail().data.value.source_id, v.module.ids.a)
  readonly(v)
})

test('all original draft flags and busy/loading guards suppress the rendered diagnostic link', async t => {
  const v = await harness(t), state = v.workbench()
  const before = v.fixture.isolation.requests.value.length
  for (const key of ['smokeDirty', 'primaryRetryDirty', 'recoveryText', 'busy', 'loading']) {
    state[key].value = key === 'recoveryText' ? '合成未保存修正' : true; await settle()
    assert.equal(v.nodes('a', diagnosticLabel).length, 0, key)
    state[key].value = key === 'recoveryText' ? '' : false; await settle()
    assert.equal(v.nodes('a', diagnosticLabel).length, 1, key)
  }
  assert.equal(v.fixture.isolation.requests.value.length, before)
  readonly(v)
})

test('failed preparation reread marks needsRefresh and hides stale diagnostic link until a successful GET', async t => {
  const v = await harness(t)
  await v.click('button', '令下一次准备读取失败'); await v.click('button', '刷新准备状态')
  assert.equal(v.workbench().needsRefresh.value, true)
  assert.equal(v.workbench().preparation.value.responseDiagnostic.taskId, v.module.ids.a)
  assert.match(text(v.host), /合成准备读取失败|下方为上次读取/)
  assert.equal(v.nodes('a', diagnosticLabel).length, 0)
  await v.click('button', '刷新准备状态')
  assert.equal(v.workbench().needsRefresh.value, false)
  await v.click('a', diagnosticLabel)
  assert.equal(v.detail().data.value.source_id, v.module.ids.a)
  readonly(v)
})

test('exact independent asset job follows original link and refresh without forcing an attempt_id', async t => {
  const v = await harness(t), { ids } = v.module
  await v.choose('asset')
  assert.equal(v.workbench().preparation.value.assetJob, ids.job)
  assert.deepEqual(v.fixture.router.resolve(v.one('a', diagnosticLabel).props.href).query, { kind: 'preparation', id: ids.preparation })
  await v.click('a', diagnosticLabel)
  assert.equal(v.detail().data.value.source_id, ids.job); assert.equal(v.detail().data.value.source_kind, 'asset_job')
  assert.equal(v.nodes('button', '返回最新调用').length, 0)
  await v.click('button', '刷新此任务')
  for (const row of v.fixture.isolation.requests.value.filter((r: any) => r.url.startsWith('/task-center/'))) {
    assert.deepEqual(row.query, { call_page: 1, output_page: 1 })
  }
  await v.click('a', '返回准备页（内存）')
  assert.equal(v.workbench().preparation.value.assetJob, ids.job)
  readonly(v)
})

test('missing original output evidence remains explicit under A and never retrieves latest B', async t => {
  const v = await harness(t)
  await v.choose('missing-evidence'); await v.click('a', diagnosticLabel); await v.click('button', '输出与校验')
  assert.equal(v.detail().data.value.source_id, v.module.ids.a)
  assert.equal(v.detail().data.value.output.state, 'unavailable')
  assert.equal(v.detail().data.value.output_count, 0, 'missing evidence cannot invent an artifact')
  assert.match(text(v.host), /合成原调用缺少输出证据/)
  assert.ok(!text(v.host).includes(`合成原返回 ${v.module.ids.b}`))
  readonly(v)
})

test('undeclared reads, writes, adapter overrides, fetch/XHR/beacon and persistent storage fail closed', async t => {
  const v = await harness(t), { ids } = v.module, before = v.fixture.isolation.requests.value.length
  const bypass = () => assert.fail('caller adapter escaped isolation')
  const attacks = [
    () => v.api.get('/assets/'), () => v.api.get('https://example.invalid/'),
    () => v.api.get(`/execution-preparations/${ids.preparation}/`, { adapter: bypass }),
    () => v.api.get(`/task-center/preparation/${ids.preparation}/`, { params: { call_page: 1, output_page: 1, attempt_id: 'foreign' } }),
    () => v.api.get(`/task-center/preparation/${ids.preparation}/`, { params: { call_page: 1, output_page: 1 }, headers: { Authorization: 'synthetic-not-a-credential' } }),
    ...['post', 'put', 'patch', 'delete', 'postForm', 'putForm', 'patchForm', 'head', 'options'].map(method => () => v.api[method]('/execution-preparations/')),
    () => v.api.request({ method: 'get', url: '/assets/', adapter: bypass }),
    () => v.api({ method: 'get', url: '/assets/', adapter: bypass }),
    () => v.transport.fetch!('https://example.invalid/'),
  ]
  for (const attack of attacks) await assert.rejects(attack, /合成夹具禁止/)
  assert.throws(() => v.transport.XMLHttpRequest.prototype.send(), /合成夹具禁止/)
  assert.throws(() => v.transport.navigator.sendBeacon(), /合成夹具禁止/)
  assert.equal(v.fixture.isolation.blocked.value, attacks.length + 2)
  assert.equal(v.fixture.isolation.requests.value.length, before)
  assert.throws(() => localStorage.setItem('anything', 'forbidden'), /禁止浏览器持久存储/)
  assert.throws(() => localStorage.getItem('cineforge_refresh_token'), /禁止浏览器持久存储/)
  assert.throws(() => sessionStorage.getItem('anything'), /禁止浏览器持久存储/)
  assert.throws(() => sessionStorage.clear(), /禁止浏览器持久存储/)
})

test('browser fixture builds only in memory with env/watch/HMR off and restrictive transport/media CSP', async t => {
  guardNodeNetwork(t)
  const result: any = await build({ ...config, plugins: [vue()], build: { write: false, minify: false,
    rollupOptions: { input: fileURLToPath(new URL('./fixtures/preparation-diagnostic-journey.html', import.meta.url)) } } })
  assert.ok(result.output.some((entry: any) => entry.fileName.endsWith('.html')))
  const html = readFileSync(new URL('./fixtures/preparation-diagnostic-journey.html', import.meta.url), 'utf8')
  assert.match(html, /connect-src 'none'/); assert.match(html, /media-src 'none'/)
  assert.doesNotMatch(html, /https?:\/\/|wss?:\/\//)
  for (const css of result.output.filter((entry: any) => entry.fileName.endsWith('.css'))) assert.doesNotMatch(String(css.source), /@import|https?:\/\//)
  assert.equal(config.envFile, false); assert.equal(config.server.watch, null)
  assert.equal(config.server.hmr, false); assert.equal(config.server.ws, false)
})

const confirmationFixture = '/tests/fixtures/m6-confirmation-journey.ts'
const panelNames = ['ExecutionPromptDisplay', 'ExecutionStorageMetrics'] as const
async function openConfirmationPanels(v: Harness) {
  await v.click('button', '预览完整提示词与系统组装')
  await v.click('button', '查看准备字节明细')
}
function assertConfirmationPanels(v: Harness, receipt: any) {
  const prompt = v.component('ExecutionPromptDisplay').state
  const metrics = v.component('ExecutionStorageMetrics').state
  assert.equal(prompt.error.value, ''); assert.equal(metrics.error.value, '')
  assert.equal(prompt.result.value.preparationRevision, receipt.preparation.revision)
  assert.equal(metrics.result.value.preparationRevision, receipt.preparation.revision)
  assert.equal(prompt.result.value.mode, 'diagnostic')
  assert.deepEqual(prompt.result.value.channels.map((row: any) => row.text), receipt.prompt.channels.map((row: any) => row.text))
  assert.deepEqual(prompt.result.value.fieldBasis.map((row: any) => [row.fieldValueId, row.origin]),
    receipt.prompt.field_basis.map((row: any) => [row.field_value_id, row.origin]))
  assert.deepEqual(metrics.result.value.items.map((row: any) => [row.key, row.state, row.sizeBytes, row.sha256]),
    receipt.metrics.items.map((row: any) => [row.key, row.state, row.size_bytes, row.sha256]))
  for (const channel of receipt.prompt.channels) assert.ok(text(v.host).includes(channel.text), 'untruncated original channel')
}
function assertClearedConfirmationPanels(v: Harness, original: any[], revision: number) {
  for (const [index, name] of panelNames.entries()) {
    const panel = v.component(name)
    assert.equal(panel.instance, original[index].instance, 'existing child watcher, not remount')
    assert.equal(panel.props.revision, revision)
    assert.equal(panel.state.result.value, null)
    assert.equal(panel.state.error.value, '')
    assert.equal(panel.state.loading.value, false)
    assert.equal(panel.state.opened.value, false)
  }
}

test('M6-A04 original confirm buttons refresh each intermediate preparation and invalidate both original panels', async t => {
  const v = await harness(t, confirmationFixture), { receipts } = v.module, fixture = v.fixture
  const panels = panelNames.map(name => v.component(name))
  assert.equal(v.workbench().field.value.id, receipts.fields[0].id)
  assert.equal(v.workbench().currentSelection.value, undefined)
  await openConfirmationPanels(v)
  assertConfirmationPanels(v, receipts.candidate)
  for (let index = 0; index < receipts.fields.length; index++) {
    if (index > 0) {
      const tabs = descendants(v.host).find(node => node.tag === 'nav' && node.props['aria-label'] === '选择需要审核的输入字段')!
      const buttons = tabs.children.filter(node => node.tag === 'button')
      assert.equal(buttons.length, receipts.fields.length)
      assert.equal(v.workbench().preparation.value.fieldSlots[index].unitKey, receipts.fields[index].unit_key)
      assert.ok(!buttons[index]!.props.disabled)
      await buttons[index]!.props.onClick(); await settle()
    }
    assert.equal(v.workbench().field.value.id, receipts.fields[index].id)
    assert.equal(v.workbench().currentSelection.value, undefined)
    const before = fixture.stages[index], after = fixture.stages[index + 1]
    let releasePost!: () => void, releaseDetail!: () => void
    fixture.setReply((config: any) => {
      if (config.method === 'post') return new Promise(resolve => { releasePost = () => resolve(fixture.defaultReply(config)) })
      assert.equal(config.url, fixture.base)
      return new Promise(resolve => { releaseDetail = () => resolve(fixture.defaultReply(config)) })
    })
    const start = fixture.requests.length
    const pending = v.click('button', '确认并采用此修订')
    await settle()
    assert.equal(v.workbench().busy.value, true)
    assert.deepEqual(fixture.requests.slice(start).map((r: any) => r.method), ['post'])
    releasePost(); await settle()
    assert.equal(v.workbench().preparation.value.revision, before.preparation.revision)
    assert.equal(v.workbench().loading.value, true)
    assert.deepEqual(fixture.requests.slice(start).map((r: any) => [r.method, r.url]),
      [['post', `/execution-field-values/${receipts.fields[index].id}/confirm/`], ['get', fixture.base]])
    releaseDetail(); await pending
    assert.equal(v.workbench().preparation.value.revision, after.preparation.revision)
    assert.equal(v.workbench().busy.value, false)
    assert.equal(v.workbench().needsRefresh.value, false)
    assertClearedConfirmationPanels(v, panels, after.preparation.revision)
    assert.equal(fixture.requests.length, start + 2, 'revision change does not automatically read panels')
    fixture.setReply(fixture.defaultReply)
    await openConfirmationPanels(v)
    assertConfirmationPanels(v, after)
  }
  assert.equal(fixture.requests.filter((r: any) => r.method === 'post').length, 2, 'one exact POST per field')
})

test('M6-A04 actual confirmation discards pending old-revision panel responses after the new panels load', async t => {
  const v = await harness(t, confirmationFixture), fixture = v.fixture, { candidate, confirmations } = v.module.receipts
  const panels = panelNames.map(name => v.component(name)), releases: (() => void)[] = []
  fixture.setReply((config: any) => {
    if (config.url.endsWith('prompt-display/') || config.url.endsWith('storage-metrics/')) {
      return new Promise(resolve => { releases.push(() => resolve(structuredClone(
        config.url.endsWith('prompt-display/') ? candidate.prompt : candidate.metrics))) })
    }
    return fixture.defaultReply(config)
  })
  const oldPrompt = v.click('button', '预览完整提示词与系统组装')
  const oldMetrics = v.click('button', '查看准备字节明细')
  await settle()
  assert.equal(releases.length, 2)
  await v.click('button', '确认并采用此修订')
  assertClearedConfirmationPanels(v, panels, confirmations[0].after.preparation.revision)
  fixture.setReply(fixture.defaultReply)
  await openConfirmationPanels(v)
  assertConfirmationPanels(v, confirmations[0].after)
  for (const release of releases) release()
  await Promise.all([oldPrompt, oldMetrics])
  assertConfirmationPanels(v, confirmations[0].after)
  assert.equal(fixture.requests.filter((r: any) => r.method === 'post').length, 1)
})

test('M6-A04 confirmed POST plus failed detail refresh stays visibly stale until explicit read-only recovery', async t => {
  const v = await harness(t, confirmationFixture), fixture = v.fixture, { candidate, confirmations } = v.module.receipts
  const panels = panelNames.map(name => v.component(name))
  await openConfirmationPanels(v)
  fixture.setReply((config: any) => {
    if (config.method === 'get' && config.url === fixture.base) throw Error('Declared detail read failure after confirmed POST')
    return fixture.defaultReply(config)
  })
  await v.click('button', '确认并采用此修订')
  assert.equal(v.workbench().needsRefresh.value, true)
  assert.match(v.workbench().error.value, /Declared detail read failure/)
  assert.equal(v.workbench().preparation.value.revision, candidate.preparation.revision)
  for (const panel of panels) {
    assert.equal(panel.props.disabled, true)
    assert.equal(panel.state.result.value.preparationRevision, candidate.preparation.revision)
  }
  assert.ok(v.one('button', '确认并采用此修订').props.disabled)
  fixture.setReply(fixture.defaultReply)
  await v.click('button', '刷新准备状态')
  assert.equal(v.workbench().needsRefresh.value, false)
  assertClearedConfirmationPanels(v, panels, confirmations[0].after.preparation.revision)
  await openConfirmationPanels(v)
  assertConfirmationPanels(v, confirmations[0].after)
  assert.equal(fixture.requests.filter((r: any) => r.method === 'post').length, 1, 'read recovery never repeats confirmation')
})

