import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import http from 'node:http'
import https from 'node:https'
import net from 'node:net'
import { fileURLToPath } from 'node:url'
import { build, createServer, transformWithEsbuild } from 'vite'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import { createRenderer, nextTick } from 'vue'

const root = fileURLToPath(new URL('..', import.meta.url))
const tick = async () => { for (let i = 0; i < 8; i++) { await nextTick(); await new Promise(resolve => setImmediate(resolve)) } }
// Real client SFC templates, handlers and child props; only the host DOM is inert.
function components() {
  return { name: 'provenance-client-sfc', async load(filename: string) {
    if (!filename.endsWith('.vue')) return
    const { descriptor } = parse(fs.readFileSync(filename, 'utf8'), { filename })
    const script = compileScript(descriptor, { id: filename, genDefaultAs: '__sfc__' })
    const template = compileTemplate({ source: descriptor.template!.content, filename, id: filename,
      compilerOptions: { bindingMetadata: script.bindings, hoistStatic: false } })
    assert.deepEqual(template.errors, [])
    return (await transformWithEsbuild(script.content + '\n' + template.code + '\n' +
      'import { getCurrentInstance as instance } from "vue";\n' +
      'const setup=__sfc__.setup; __sfc__.setup=(props,ctx)=>{const state=setup(props,ctx); globalThis.__provenanceCapture?.(' +
      JSON.stringify(path.basename(filename)) + ',{state,props,instance:instance()}); return state;};\n' +
      '__sfc__.render=render; export default __sfc__;', filename + '.ts', { loader: 'ts', target: 'esnext' })).code
  } }
}
class Element {
  tag: string
  children: Element[] = []; parent: Element | null = null; props: Record<string, any> = {}; text = ''; style = {}
  value: any = ''; checked = false; selected = false; multiple = false
  listeners: Record<string, Function> = {}; classList = { add() {}, remove() {} }
  constructor(tag: string) { this.tag = tag }
  get tagName() { return this.tag.toUpperCase() }
  get options() { return this.children.filter(node => node.tag === 'option') }
  addEventListener(key: string, fn: Function) { this.listeners[key] = fn }
  removeEventListener(key: string) { delete this.listeners[key] }
  setAttribute(key: string, value: unknown) { this.props[key] = value }
  removeAttribute(key: string) { delete this.props[key] }
  getAttribute(key: string) { return this.props[key] }
  querySelectorAll() { return [] }
  focus() {}
}
const text = (node: Element): string => node.text + node.children.map(text).join('')
const all = (node: Element): Element[] => [node, ...node.children.flatMap(all)]

async function setup(t: TestContext) {
  const network: string[] = [], captures = new Map<string, any[]>(), previousCapture = (globalThis as any).__provenanceCapture
  const block = (label: string): never => { network.push(label); throw Error('Real network prohibited: ' + label) }
  t.mock.method(globalThis, 'fetch', () => block('fetch'))
  for (const transport of [http, https]) for (const key of ['get', 'request'] as const) t.mock.method(transport, key, () => block(key))
  t.mock.method(net.Socket.prototype, 'connect', () => block('socket.connect'))
  ;(globalThis as any).__provenanceCapture = (name: string, entry: any) => {
    const rows = captures.get(name) || []; rows.push(entry); captures.set(name, rows)
  }
  const vite = await createServer({ root, configFile: false, plugins: [components()], resolve: { alias: { '@': path.join(root, 'src') } },
    logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true } })
  const module = await vite.ssrLoadModule('/tests/fixtures/production-task-provenance.ts')
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const transport = { fetch: async () => block('fixture fetch escaped'), XMLHttpRequest: { prototype: { send: () => block('fixture XHR escaped') } },
    navigator: { sendBeacon: () => block('fixture beacon escaped') }, addEventListener() {}, removeEventListener() {},
    setInterval: () => { throw Error('Failed Run must not poll') }, clearInterval() {} }
  const fixture = module.createFixture(transport)
  await fixture.choose('run-a'); await fixture.router.isReady()
  const saved = ['window', 'document', 'HTMLElement'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
  Object.defineProperty(globalThis, 'window', { configurable: true, value: transport })
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { activeElement: null } })
  Object.defineProperty(globalThis, 'HTMLElement', { configurable: true, value: Element })
  const host = new Element('main'), body = new Element('body')
  const renderer = createRenderer<Element, Element>({
    createElement: tag => new Element(tag), createText: value => Object.assign(new Element('#text'), { text: value }),
    createComment: () => new Element('#comment'),
    insert(node, parent, anchor) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = parent;
      const index = anchor ? parent.children.indexOf(anchor) : -1; if (index < 0) parent.children.push(node); else parent.children.splice(index, 0, node) },
    remove(node) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = null },
    parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] || null,
    setText: (node, value) => { node.text = value }, setElementText: (node, value) => { node.text = value; node.children = [] },
    patchProp(node, key, _old, value) { node.props[key] = value; if (['value', 'checked', 'selected', 'multiple', 'type'].includes(key)) (node as any)[key] = value },
    querySelector: selector => selector === 'body' ? body : null, setScopeId() {},
  })
  const app = renderer.createApp(fixture.Root).use(fixture.pinia).use(fixture.router)
  app.config.warnHandler = warning => assert.fail(warning)
  t.after(async () => {
    app.unmount(); fixture.restore(); await vite.close(); (globalThis as any).__provenanceCapture = previousCapture
    for (const [key, descriptor] of saved) if (descriptor) Object.defineProperty(globalThis, key, descriptor); else Reflect.deleteProperty(globalThis, key)
    assert.deepEqual(network, [])
  })
  app.mount(host); await tick()
  const component = (name: string) => {
    const rows = (captures.get(name + '.vue') || []).filter(entry => !entry.instance.isUnmounted)
    assert.equal(rows.length, 1, name + ' is the real mounted component'); return rows[0]
  }
  const links = () => all(host).filter(node => node.tag === 'a' && String(node.props.href).startsWith('/workflows?'))
  return { fixture, module, api, transport, host, component, links, captures }
}

test('real ProductionRunView passes exact run/stage to the original preparation component even with zero image artifacts', async t => {
  const v = await setup(t)
  for (const mode of ['run-a', 'run-b', 'run-a']) {
    await v.fixture.choose(mode); await tick()
    const { run, stage } = v.module.records(mode === 'run-b')
    const parent = v.component('ProductionRunView'), child = v.component('StageExecutionPreparations')
    assert.equal(parent.state.error.value, '')
    assert.deepEqual(JSON.parse(JSON.stringify(parent.state.currentRun.value.artifacts)), [])
    assert.equal(child.props.runId, run.id); assert.equal(child.props.stageId, stage.id)
    assert.equal(child.state.error.value, '')
    assert.equal(child.state.result.value.results[0].latest.outcome.artifactCount, 0)
    assert.match(text(v.host), /没有已归档产物/)
    assert.ok(text(v.host).includes(v.module.originalFailure), 'keep the original image failure description')
    assert.doesNotMatch(text(v.host), /未识别模式|配方不是当前 schema3 合同|执行路线不完整/)
    assert.doesNotMatch(text(v.host), /将原正文复制到编辑区|修正或补全后的完整响应|校验并保存候选|校验并保存人工修订|通用文本候选与结果/)
    assert.equal(all(v.host).filter(node => node.tag === 'textarea').length, 0)
    const links = v.links(); assert.equal(links.length, 1)
    assert.equal(text(links[0]!), '查看本阶段原调用')
    const expected = { category: 'production', production_run_id: run.id, stage_run_id: stage.id }
    assert.deepEqual(v.fixture.router.resolve(links[0]!.props.href).query, expected)
    const before = v.fixture.events.value.length
    // Invoke the actual RouterLink handler: navigation itself must not fetch or submit.
    await links[0]!.props.onClick({ button: 0, preventDefault() {} }); await tick()
    assert.deepEqual(v.fixture.router.currentRoute.value.query, expected)
    assert.match(text(v.host), /内存任务定位/)
    assert.equal(v.fixture.events.value.length, before)
    assert.equal(v.fixture.blocked.value, 0)
    assert.equal(v.captures.has('TaskMediaInspector.vue'), false)
  }
})

test('an empty preparation list still exposes stage provenance, while missing run identity never guesses a locator', async t => {
  const v = await setup(t)
  await v.fixture.choose('empty'); await tick()
  assert.match(text(v.host), /本阶段尚未创建主准备/)
  assert.equal(v.links().length, 1)
  assert.deepEqual(v.fixture.router.resolve(v.links()[0]!.props.href).query, {
    category: 'production', production_run_id: v.module.id(2), stage_run_id: v.module.id(3),
  })
  await v.fixture.choose('missing-run'); await tick()
  assert.equal(v.links().length, 0)
  assert.ok(text(v.host).includes(v.module.originalFailure))
  assert.equal(v.fixture.blocked.value, 0)
})

test('fixture blocks all writes, undeclared GETs, adapter overrides and raw transports; modified clicks stay in memory', async t => {
  const v = await setup(t)
  let overrides = 0, expectedBlocks = 0
  for (const method of ['post', 'put', 'patch', 'delete']) {
    await assert.rejects(() => v.api[method]('/production-runs/unknown/retry/', {}), /MOCK 已阻断/); expectedBlocks++
  }
  await assert.rejects(() => v.api.get('/undeclared/'), /MOCK 已阻断/); expectedBlocks++
  await assert.rejects(() => v.api.get('/execution-preparations/groups/', { params: { stage_run_id: v.module.id(999), page: 1, kind: 'primary' } }), /MOCK 已阻断/); expectedBlocks++
  await assert.rejects(() => v.api.get('/execution-preparations/groups/', { adapter: async () => { overrides++; return {} } }), /MOCK 已阻断/); expectedBlocks++
  await assert.rejects(() => v.transport.fetch(), /MOCK 已阻断/); expectedBlocks++
  assert.throws(() => v.transport.XMLHttpRequest.prototype.send(), /MOCK 已阻断/); expectedBlocks++
  assert.throws(() => v.transport.navigator.sendBeacon(), /MOCK 已阻断/); expectedBlocks++
  assert.equal(overrides, 0); assert.equal(v.fixture.blocked.value, expectedBlocks)
  const href = v.links()[0]!.props.href, before = v.fixture.events.value.length
  for (const type of ['click', 'auxclick']) {
    let prevented = false, stopped = false
    await v.fixture.navigate({ type, ctrlKey: true, target: { closest: () => ({ getAttribute: () => href }) },
      preventDefault: () => { prevented = true }, stopPropagation: () => { stopped = true } }); await tick()
    assert.equal(prevented, true); assert.equal(stopped, true)
    assert.equal(v.fixture.router.currentRoute.value.name, 'workflows')
  }
  assert.equal(v.fixture.events.value.length, before)
})

test('isolated fixture and original page compile in memory without a listener or emitted build', async () => {
  const result = await build({ root, logLevel: 'silent', build: { write: false, emptyOutDir: false,
    rollupOptions: { input: fileURLToPath(new URL('./fixtures/production-task-provenance.html', import.meta.url)) } } })
  assert.ok(!Array.isArray(result) && 'output' in result)
  const html = result.output.find(item => item.type === 'asset' && item.fileName.endsWith('.html'))
  assert.ok(html && html.type === 'asset')
  assert.match(String(html.source), /Content-Security-Policy/)
  assert.match(String(html.source), /form-action 'none'/)
})
