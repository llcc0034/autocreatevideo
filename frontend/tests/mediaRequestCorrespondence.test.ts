import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import http from 'node:http'
import https from 'node:https'
import net from 'node:net'
import dns from 'node:dns'
import { fileURLToPath } from 'node:url'
import { createServer, transformWithEsbuild } from 'vite'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import { createRenderer, nextTick } from 'vue'

const root = fileURLToPath(new URL('..', import.meta.url))
const forbidden = ['src/components/AssetAppearanceTargetEditor.vue', 'src/components/TaskMediaInspector.vue',
  'src/content/productionManual.ts', 'src/content/systemSettingsGuide.json',
  'tests/productionTaskProvenance.test.ts', 'tests/fixtures/production-task-provenance.ts']
const flush = async () => { for (let i = 0; i < 12; i++) { await nextTick(); await new Promise(resolve => setImmediate(resolve)) } }

// Compile original client templates and handlers, not a rewritten helper or SSR-only mock.
function clientComponents(loaded: Set<string>) {
  return { name: 'm7-real-client-components', enforce: 'pre' as const, async load(filename: string) {
    const relative = path.relative(root, filename.split('?')[0]!).replaceAll('\\', '/')
    assert.ok(!forbidden.includes(relative), 'Requires coordinator baseline sync: ' + relative)
    if (!filename.endsWith('.vue')) return
    loaded.add(relative)
    const { descriptor } = parse(fs.readFileSync(filename, 'utf8'), { filename })
    const script = compileScript(descriptor, { id: filename, genDefaultAs: '__sfc__' })
    const template = compileTemplate({ source: descriptor.template!.content, filename, id: filename,
      compilerOptions: { bindingMetadata: script.bindings, hoistStatic: false } })
    assert.deepEqual(template.errors, [])
    return (await transformWithEsbuild(script.content + '\n' + template.code + '\n' +
      'import { getCurrentInstance as instance } from "vue";\n' +
      'const setup=__sfc__.setup; __sfc__.setup=(props,ctx)=>{const state=setup(props,ctx); globalThis.__m7Capture?.(' +
      JSON.stringify(path.basename(filename)) + ',{state,props,instance:instance()}); return state;};\n' +
      '__sfc__.render=render; export default __sfc__;', filename + '.ts', { loader: 'ts', target: 'esnext' })).code
  } }
}

// Inert host tree: Vue still runs real lifecycle, directives, template events and Teleport.
class Element {
  tag: string; children: Element[] = []; parent: Element | null = null; props: Record<string, any> = {}; text = ''; style = {}
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
const text = (node: Element): string => node.tag === 'style' ? '' : node.text + node.children.map(text).join('')
const all = (node: Element): Element[] => [node, ...node.children.flatMap(all)]

async function setup(t: TestContext, choice = 'manual-comfy') {
  const network: string[] = [], loaded = new Set<string>(), captures = new Map<string, any[]>(), failures: unknown[] = []
  const block = (label: string): never => { network.push(label); throw Error('Real network/listener prohibited: ' + label) }
  t.mock.method(globalThis, 'fetch', () => block('fetch'))
  for (const transport of [http, https]) for (const key of ['get', 'request'] as const) t.mock.method(transport, key, () => block(key))
  t.mock.method(net.Socket.prototype, 'connect', () => block('socket.connect'))
  t.mock.method(net.Server.prototype, 'listen', () => block('server.listen'))
  t.mock.method(dns, 'lookup', () => block('dns.lookup'))
  const saved = ['window', 'document', 'history', 'HTMLElement', 'requestAnimationFrame', '__m7Capture', 'localStorage'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
  const transport = { fetch: () => block('fixture.fetch'), XMLHttpRequest: { prototype: { send: () => block('fixture.XHR') } },
    navigator: { sendBeacon: () => block('beacon') }, location: { href: 'https://m7-fixture.invalid/' },
    addEventListener() {}, removeEventListener() {}, confirm: () => false,
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', transitionDelay: '0s', transitionDuration: '0s', animationDelay: '0s', animationDuration: '0s' }),
    setInterval: () => block('polling'), clearInterval() {} }
  const host = new Element('main'), body = new Element('body')
  Object.defineProperty(globalThis, 'window', { configurable: true, value: transport })
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { activeElement: null, body, querySelector: () => null } })
  Object.defineProperty(globalThis, 'history', { configurable: true, value: { state: {} } })
  Object.defineProperty(globalThis, 'HTMLElement', { configurable: true, value: Element })
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, get: () => block('credential storage') })
  Object.defineProperty(globalThis, 'requestAnimationFrame', { configurable: true, value: (callback: Function) => setImmediate(callback) })
  ;(globalThis as any).__m7Capture = (name: string, entry: any) => { const rows = captures.get(name) || []; rows.push(entry); captures.set(name, rows) }
  let app: any, fixture: any, vite: any
  t.after(async () => {
    app?.unmount(); fixture?.restore(); await vite?.close()
    for (const [key, descriptor] of saved) if (descriptor) Object.defineProperty(globalThis, key, descriptor); else Reflect.deleteProperty(globalThis, key)
    assert.deepEqual(network, [], 'No listener, DNS, network or credential access')
    assert.deepEqual(failures, [], 'No hidden component failures')
    assert.deepEqual(fixture?.blocked.value || [], [], 'No unexpected route or business write')
    assert.ok(forbidden.every(file => !loaded.has(file)))
  })
  vite = await createServer({ root, configFile: false, envFile: false, plugins: [clientComponents(loaded)],
    resolve: { alias: { '@': path.join(root, 'src') } }, logLevel: 'silent', appType: 'custom',
    server: { middlewareMode: true, hmr: false, ws: false, watch: null }, optimizeDeps: { noDiscovery: true } })
  assert.equal(vite.httpServer, null)
  const module = await vite.ssrLoadModule('/tests/fixtures/media-request-correspondence.ts')
  fixture = module.createFixture(transport)
  await fixture.choose(choice); await fixture.router.isReady()
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
  app = renderer.createApp(fixture.Root).use(fixture.pinia).use(fixture.router)
  app.config.warnHandler = (warning: string) => { failures.push(warning) }
  app.config.errorHandler = (error: unknown) => { failures.push(error) }
  app.mount(host); await flush()
  const nodes = () => [...all(host), ...all(body)]
  const component = (name: string) => {
    const rows = (captures.get(name + '.vue') || []).filter(row => !row.instance.isUnmounted)
    assert.equal(rows.length, 1, `${name} mounted once`); return rows[0]
  }
  const click = async (label: string) => {
    const matches = nodes().filter(node => node.tag === 'button' && text(node) === label)
    assert.equal(matches.length, 1, 'Unique real button: ' + label); assert.ok(!matches[0]!.props.disabled, 'Button enabled: ' + label)
    await matches[0]!.props.onClick({ preventDefault() {}, stopPropagation() {} }); await flush()
  }
  const openFacts = async () => {
    const node = nodes().find(node => node.tag === 'details' && node.props.class === 'preparation-facts')!
    assert.ok(node); await node.props.onToggle({ target: { open: true } }); await flush()
  }
  return { fixture, module, nodes, component, click, openFacts, loaded, failures, host, body,
    visible: () => text(host) + text(body), open: () => click('查看正文 / 审核字段') }
}

for (const choice of ['manual-comfy', 'recipe-comfy', 'manual-rest', 'recipe-rest']) {
  test(`${choice}: original workbench renders adopted A, frozen identity/parameters and exact reference order, never latest B`, async t => {
    const v = await setup(t, choice), row = v.fixture.cases[choice], before = JSON.stringify(row)
    assert.equal(v.fixture.events.value.length, 1, 'Only current stage groups are read initially')
    await v.open(); await v.click('查看最终冻结的完整提示词'); await v.openFacts()
    const workbench = v.component('ExecutionPreparationWorkbench'), prompt = v.component('ExecutionPromptDisplay')
    assert.equal(workbench.props.preparationId, row.preparationId); assert.equal(workbench.props.versionId, row.versionId)
    assert.equal(workbench.props.manualRun?.planSource, choice.startsWith('manual') ? 'manual' : undefined)
    assert.equal(workbench.state.field.value.id, row.fieldA.id)
    assert.equal(workbench.state.preparation.value.fieldSlots[0].latestValueId, row.fieldB.id)
    assert.equal(prompt.state.result.value.fieldBasis[0].fieldValueId, row.fieldA.id)
    assert.equal(prompt.state.result.value.requestSha256, row.requestSha)
    assert.equal(prompt.state.result.value.channels[0].text, row.prompt.channels[0].text)
    for (const value of [row.selectedText, row.versionId, row.profileId, row.requestSha, '此修订已明确选入本次请求', '实际运行参数']) assert.ok(v.visible().includes(value), value)
    assert.ok(!v.visible().includes(row.pendingText)); assert.ok(!v.visible().includes(row.currentCatalog.versionId))
    const parameterSection = v.nodes().find(node => node.props['aria-label'] === '实际运行参数')!
    for (const [key, value] of Object.entries(row.parameters)) { assert.ok(text(parameterSection).includes(key)); assert.ok(text(parameterSection).includes(String(value))) }
    if (row.comfy) {
      await v.click('查看本镜执行记录')
      const evidence = v.component('ShotExecutionEvidence'), references = v.component('FrozenTaskReferences')
      assert.equal(evidence.state.error.value, '')
      assert.deepEqual(references.props.task.referenceAssets.map((use: any) => [use.assetId, use.referenceBinding.shot_reference.source.reference_version_id, use.referenceBinding.input_slot]),
        row.media.map((use: any) => [use.asset_id, use.reference_binding.shot_reference.source.reference_version_id, use.reference_binding.input_slot]))
      const rendered = v.nodes().filter(node => node.tag === 'dl' && node.props['aria-label'] === '原名、版本与输入顺序对应')
      assert.equal(rendered.length, 2)
      row.media.forEach((use: any, index: number) => {
        assert.ok(text(rendered[index]!).includes(use.reference_binding.shot_reference.source.reference_version_id))
        assert.ok(text(rendered[index]!).includes(`模型标记序号 ${index + 1}`))
        assert.ok(v.visible().includes(use.reference_binding.reference_name))
      })
      assert.ok(!v.nodes().some(node => ['img', 'video', 'audio', 'script'].includes(node.tag)))
      assert.ok(!v.loaded.has('src/components/TaskMediaInspector.vue'))
    } else {
      assert.ok(!v.loaded.has('src/components/FrozenTaskReferences.vue') || !v.nodes().some(node => node.props['aria-label'] === '原请求素材绑定（只读）'))
      assert.ok(!v.fixture.events.value.some((event: any) => event.url.startsWith('/tasks/')))
    }
    // Current catalog changes are intentionally never requested by the historical reader.
    row.currentCatalog.width += 1; row.currentCatalog.referenceName = 'CHANGED AGAIN'
    await v.click('刷新准备状态'); await v.click('收起完整提示词'); await v.click('查看最终冻结的完整提示词')
    assert.equal(prompt.state.result.value.requestSha256, row.requestSha)
    assert.equal(prompt.state.result.value.channels[0].text, row.prompt.channels[0].text)
    assert.deepEqual(workbench.state.preparation.value.resolvedInputs.parameters.values, row.parameters)
    const original = JSON.parse(before); original.currentCatalog = row.currentCatalog
    assert.deepEqual(JSON.parse(JSON.stringify(row)), original, 'Viewing never changes receipt data')
    assert.ok(v.fixture.events.value.every((event: any) => event.method === 'GET'))
    const reads = v.fixture.events.value.length; await flush(); assert.equal(v.fixture.events.value.length, reads, 'No automatic resend/poll')
  })
}

test('preview and frozen display keep A while separately viewing the newer pending B never adopts it', async t => {
  const v = await setup(t), row = v.fixture.cases['manual-comfy']
  row.preparation.request_frozen_at = null; row.preparation.request_sha256 = null; row.preparation.prepared_request = null
  row.prompt.mode = 'preview'; row.prompt.request_sha256 = null; row.prompt.frozen_at = null
  await v.open(); await v.click('预览完整提示词与系统组装')
  const prompt = v.component('ExecutionPromptDisplay'), body = prompt.state.result.value.channels[0].text
  await v.click('查看全部字段修订'); await v.click('冻结镜头动作 · 修订 2')
  assert.equal(v.component('ExecutionPreparationWorkbench').state.field.value.id, row.fieldB.id)
  assert.ok(v.visible().includes('尚未采用这份正文')); assert.ok(v.visible().includes(row.pendingText))
  assert.equal(prompt.state.result.value.fieldBasis[0].fieldValueId, row.fieldA.id)
  assert.equal(prompt.state.result.value.channels[0].text, body)
  // A later GET receipt models backend freezing, not a frontend write/assembly implementation.
  const frozen = v.module.records('manual-comfy')
  row.preparation = frozen.preparation; row.preparation.revision = 9
  row.prompt = frozen.prompt; row.prompt.preparation_revision = 9
  await v.click('刷新准备状态')
  assert.equal(prompt.state.result.value, null, 'Revision/frozen-state change invalidates prior preview')
  await v.click('查看最终冻结的完整提示词')
  assert.equal(prompt.state.result.value.channels[0].text, body)
  assert.equal(prompt.state.result.value.fieldBasis[0].fieldValueId, row.fieldA.id)
  assert.ok(!v.nodes().some(node => node.tag === 'button' && text(node) === '确认并采用此修订'))
})

test('switching run/preparation/shot while old reads are pending discards late responses and old reference facts', async t => {
  const v = await setup(t), old = v.fixture.cases['manual-comfy'], current = v.fixture.cases['recipe-comfy']
  await v.open(); await v.click('查看最终冻结的完整提示词'); await v.click('查看本镜执行记录')
  await v.click('收起完整提示词')
  let release: () => void = () => {}; const held = new Promise<void>(resolve => { release = resolve })
  v.fixture.setResponse(async (config: any, body: any) => { if (config.url === `/execution-preparations/${old.preparationId}/prompt-display/`) await held; return body })
  const loading = v.click('查看最终冻结的完整提示词'); await flush()
  await v.fixture.choose('recipe-comfy'); await flush()
  assert.ok(!v.visible().includes(old.selectedText)); assert.ok(!v.visible().includes(old.media[0].reference_binding.shot_reference.source.reference_version_id))
  release(); await loading; await flush()
  await v.open(); await v.click('查看最终冻结的完整提示词'); await v.click('查看本镜执行记录')
  assert.equal(v.component('ExecutionPromptDisplay').state.result.value.preparationId, current.preparationId)
  assert.equal(v.component('ShotExecutionEvidence').state.task.value.id, current.taskId)
  assert.ok(v.visible().includes(current.selectedText)); assert.ok(!v.visible().includes(old.selectedText))
})

test('failed preparation refresh explicitly marks retained old facts and blocks stale execution; retry is only a read', async t => {
  const v = await setup(t), row = v.fixture.cases['manual-comfy']
  row.preparation.request_frozen_at = null; row.preparation.request_sha256 = null; row.preparation.prepared_request = null
  row.prompt.mode = 'preview'; row.prompt.request_sha256 = null; row.prompt.frozen_at = null
  await v.open(); await v.click('预览完整提示词与系统组装')
  v.fixture.scenario.value = 'read-failure'; await v.click('刷新准备状态')
  assert.ok(v.visible().includes('隔离：准备读取失败')); assert.ok(v.visible().includes('下方为上次读取的内容'))
  const state = v.component('ExecutionPreparationWorkbench').state
  assert.equal(state.needsRefresh.value, true); assert.equal(state.canAdvance.value, false); assert.equal(state.editable.value, false)
  for (const label of ['检查并继续此任务', '修改这份正文（另存修订）', '确认并采用此修订']) {
    const button = v.nodes().find(node => node.tag === 'button' && text(node) === label)
    assert.ok(button?.props.disabled, label + ' cannot execute stale facts')
  }
  const reads = v.fixture.events.value.length; await flush(); assert.equal(v.fixture.events.value.length, reads)
  v.fixture.scenario.value = 'normal'; await v.click('刷新准备状态')
  assert.equal(state.needsRefresh.value, false); assert.ok(!v.visible().includes('隔离：准备读取失败'))
})

test('prompt read failure and foreign revision are explicit, clear previous prompt, and never reassemble or resend', async t => {
  const v = await setup(t), row = v.fixture.cases['manual-comfy']
  await v.open(); await v.click('查看最终冻结的完整提示词'); await v.click('收起完整提示词')
  v.fixture.scenario.value = 'prompt-failure'; await v.click('查看最终冻结的完整提示词')
  const state = v.component('ExecutionPromptDisplay').state
  assert.equal(state.result.value, null); assert.ok(v.visible().includes('隔离：提示词读取失败'))
  v.fixture.scenario.value = 'normal'
  v.fixture.setResponse(async (config: any, body: any) => config.url.endsWith('/prompt-display/') ? { ...body, preparation_revision: 99 } : body)
  await v.click('重新读取完整提示词'); assert.equal(state.result.value, null)
  assert.ok(v.visible().includes('完整提示词响应不完整或与当前准备不一致'))
  v.fixture.setResponse(async (_config: any, body: any) => body)
  await v.click('重新读取完整提示词'); assert.equal(state.result.value.requestSha256, row.requestSha)
})

test('task read failure and contradictory frozen identities clear original reference facts; recovery never substitutes current assets', async t => {
  const v = await setup(t), row = v.fixture.cases['manual-comfy']
  await v.click('查看本镜执行记录')
  const state = v.component('ShotExecutionEvidence').state
  assert.equal(state.task.value.id, row.taskId)
  for (const scenario of ['task-failure', 'identity-mismatch']) {
    v.fixture.scenario.value = scenario; await v.click('重新读取执行记录')
    assert.equal(state.task.value, null); assert.equal(state.prompt.value, null); assert.ok(state.error.value)
    assert.ok(!v.visible().includes(row.media[0].reference_binding.shot_reference.source.reference_version_id))
    assert.ok(!v.nodes().some(node => node.props['aria-label'] === '原请求素材绑定（只读）'))
    const reads = v.fixture.events.value.length; await flush(); assert.equal(v.fixture.events.value.length, reads)
  }
  v.fixture.scenario.value = 'normal'
  for (const field of ['request_sha256', 'execution_instance_version_id', 'attempt_id']) {
    v.fixture.setResponse(async (config: any, body: any) => config.url.endsWith('/input-prompt/')
      ? { ...body, [field]: field === 'request_sha256' ? 'f'.repeat(64) : v.module.id(99999) } : body)
    await v.click('重新读取执行记录')
    assert.equal(state.task.value, null, field); assert.equal(state.prompt.value, null, field)
    assert.match(state.error.value, /原请求证据不一致/, field)
    assert.ok(!v.nodes().some(node => node.props['aria-label'] === '原请求素材绑定（只读）'))
  }
  v.fixture.setResponse(async (_config: any, body: any) => body)
  await v.click('重新读取执行记录')
  assert.equal(state.task.value.id, row.taskId); assert.equal(state.prompt.value.requestSha256, row.requestSha)
  assert.ok(!v.visible().includes(row.currentCatalog.referenceName))
})
