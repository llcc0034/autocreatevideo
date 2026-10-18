import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import http from 'node:http'
import https from 'node:https'
import net from 'node:net'
import { fileURLToPath } from 'node:url'
import { createServer, transformWithEsbuild } from 'vite'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import { createRenderer, nextTick } from 'vue'

const root = fileURLToPath(new URL('..', import.meta.url))
const tick = async () => { for (let i = 0; i < 8; i++) { await nextTick(); await new Promise(resolve => setImmediate(resolve)) } }
// Compile the actual SFC client templates in memory. No component is stubbed;
// only the host DOM is inert. Original child emits/v-model/watch/handlers run.
function components() {
  return { name: 'journey-client-sfc', async load(filename: string) {
    if (!filename.endsWith('.vue')) return
    const { descriptor } = parse(fs.readFileSync(filename, 'utf8'), { filename })
    const script = compileScript(descriptor, { id: filename, genDefaultAs: '__sfc__' })
    const template = compileTemplate({ source: descriptor.template!.content, filename, id: filename,
      compilerOptions: { bindingMetadata: script.bindings, hoistStatic: false } })
    assert.deepEqual(template.errors, [])
    const code = script.content + '\n' + template.code + '\n' +
      'import { getCurrentInstance as __journeyInstance } from "vue";\n' +
      'const originalSetup=__sfc__.setup; __sfc__.setup=(props,ctx)=>{const state=originalSetup(props,ctx); globalThis.__journeyCapture?.(' + JSON.stringify(path.basename(filename)) + ',{state,props,instance:__journeyInstance()}); return state;};\n' +
      '__sfc__.render=render; export default __sfc__;'
    return (await transformWithEsbuild(code, filename + '.ts', { loader: 'ts', target: 'esnext' })).code
  } }
}
class Element {
  children: Element[] = []; parent: Element | null = null; props: Record<string, any> = {}; text = ''; style = {}
  value: any = ''; checked = false; selected = false; selectedIndex = -1; multiple = false
  listeners: Record<string, Function> = {}; classList = { add() {}, remove() {} }
  tag: string
  constructor(tag: string) { this.tag = tag }
  get tagName() { return this.tag.toUpperCase() }
  get options() { return this.children.filter(node => node.tag === 'option') }
  addEventListener(name: string, handler: Function) { this.listeners[name] = handler }
  removeEventListener(name: string) { delete this.listeners[name] }
  setAttribute(key: string, value: unknown) { this.props[key] = value }
  removeAttribute(key: string) { delete this.props[key] }
  getAttribute(key: string) { return this.props[key] }
  querySelectorAll() { return [] }
  focus() {}
}
const text = (node: Element): string => node.text + node.children.map(text).join('')
const descendants = (node: Element): Element[] => [node, ...node.children.flatMap(descendants)]

async function setup(t: TestContext) {
  const network: string[] = [], captures = new Map<string, any[]>(), previousCapture = (globalThis as any).__journeyCapture
  const block = (name: string): never => { network.push(name); throw Error('Unexpected network: ' + name) }
  t.mock.method(globalThis, 'fetch', () => block('fetch'))
  for (const module of [http, https]) for (const name of ['request', 'get'] as const) t.mock.method(module, name, () => block(name))
  t.mock.method(net.Socket.prototype, 'connect', () => block('socket.connect'))
  ;(globalThis as any).__journeyCapture = (name: string, entry: any) => { const rows = captures.get(name) || []; rows.push(entry); captures.set(name, rows) }
  const vite = await createServer({ root, configFile: false, plugins: [components()], resolve: { alias: { '@': path.join(root, 'src') } },
    logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true } })
  const module = await vite.ssrLoadModule('/tests/fixtures/asset-generation-relation-journey.ts')
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const body = new Element('body'), host = new Element('main')
  const transport = { fetch: () => block('fixture fetch escaped'), XMLHttpRequest: { prototype: { send: () => block('fixture XHR escaped') } },
    navigator: { sendBeacon: () => block('fixture beacon escaped') }, dispatchEvent: () => true,
    addEventListener() {}, removeEventListener() {}, confirm: () => true }
  const fixture = module.createFixture(transport)
  await fixture.router.push('/'); await fixture.router.isReady()
  const saved = ['window', 'document', 'HTMLElement'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
  Object.defineProperty(globalThis, 'window', { configurable: true, value: transport })
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { activeElement: null } })
  Object.defineProperty(globalThis, 'HTMLElement', { configurable: true, value: Element })
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
  const app = renderer.createApp(fixture.Root).use(fixture.router)
  app.config.warnHandler = warning => assert.fail(warning)
  t.after(async () => { app.unmount(); fixture.restore(); await vite.close(); (globalThis as any).__journeyCapture = previousCapture
    for (const [key, descriptor] of saved) if (descriptor) Object.defineProperty(globalThis, key, descriptor); else Reflect.deleteProperty(globalThis, key)
    assert.deepEqual(network, []) })
  app.mount(host); await tick()
  const component = (name: string, predicate: (entry: any) => boolean = () => true) => {
    const rows = (captures.get(name + '.vue') || []).filter(entry => !entry.instance.isUnmounted && predicate(entry))
    assert.equal(rows.length, 1, name + ' must be a single live original component'); return rows[0]
  }
  const button = (label: string) => {
    const rows = descendants(host).filter(node => node.tag === 'button' && text(node).includes(label))
    assert.equal(rows.length, 1, 'button ' + label); return rows[0]!
  }
  const click = async (label: string) => { const node = button(label); assert.ok(!node.props.disabled, label + ' is enabled'); await node.props.onClick?.({}); await tick() }
  const mainBrowser = () => component('AssetReferenceBrowser', entry => !entry.props.selectionKind).state
  const taskLink = () => {
    const rows = descendants(host).filter(node => node.tag === 'a' && text(node).includes('素材生成任务'))
    assert.equal(rows.length, 1); return rows[0]!
  }
  return { module, fixture, api, transport, host, component, button, click, mainBrowser, taskLink }
}
type Harness = Awaited<ReturnType<typeof setup>>
async function primary(view: Harness) {
  const { module: m, fixture, component, click, mainBrowser } = view
  const form = component('AssetExecutionCreateForm').state
  await form.choose(m.ids.primaryInstance); await tick()
  assert.equal(form.error.value, ''); assert.ok(form.contract.value)
  const editor = component('AssetAppearanceTargetEditor').state
  editor.changeRole('primary'); await tick()
  await editor.load(); assert.equal(editor.directory.value.count, 0)
  assert.equal(form.appearanceTarget.value.setting_revision_id, null)
  component('ExecutionRequestInputEditor').state.draft.value.channels.positive_prompt = m.primaryText
  await tick(); form.reviewed.value = true; await tick()
  await click('创建生成任务')
  assert.equal(fixture.created.value.id, m.ids.primaryJob); assert.equal(fixture.created.value.status, 'preparing')
  assert.equal(fixture.primaryReady.value, false); assert.equal(fixture.completion.value, null)
  assert.deepEqual(fixture.posts.value[0].payload.appearance_target, { role: 'primary', setting_revision_id: null, primary_version_id: null, primary_input: null })
  assert.equal(fixture.posts.value[0].payload.manual_channels.positive_prompt, m.primaryText)
  await click('载入模拟完成回执')
  assert.equal(fixture.completion.value.outputAsset.id, m.ids.primaryAsset)
  const reader = mainBrowser(); await reader.flow.select(reader.state.catalog.results[0]); await tick()
  assert.equal(reader.state.version.id, m.ids.primaryVersion); assert.equal(reader.state.asset.id, m.ids.primaryAsset)
  assert.match(reader.state.asset.mediaUrl, /^data:image\/svg\+xml/)
  assert.ok(text(view.host).includes('关联设定可留空') || text(view.host).includes('确切版本'))
  return reader
}
async function variant(view: Harness, wrong = false) {
  const { module: m, component, click } = view
  await click('填写变体原表单')
  const form = component('AssetExecutionCreateForm').state
  await form.choose(m.ids.variantInstance); await tick(); assert.equal(form.error.value, '')
  const inputs = component('ExecutionRequestInputEditor').state
  inputs.addMedia(form.contract.value.inputs[0]); await tick()
  const picker = component('ExecutionInputAssetPicker').state
  await picker.load(); picker.select(picker.result.value.results.find((row: any) => row.id === (wrong ? m.ids.wrongAsset : m.ids.primaryAsset)))
  const editor = component('AssetAppearanceTargetEditor').state
  editor.changeRole('variant'); await tick(); editor.browsing.value = true; await tick()
  const browser = component('AssetReferenceBrowser', entry => entry.props.selectionRole === 'primary').state
  await browser.flow.select(browser.state.catalog.results.find((row: any) => row.id === m.ids.primarySeries)); await tick()
  await browser.choose(); await tick()
  editor.chooseInput(0); await tick(); editor.chooseDifference(0)
  inputs.draft.value.channels.positive_prompt = m.differenceText
  await tick()
  assert.equal(form.primaryAssetId.value, m.ids.primaryAsset)
  assert.equal(form.appearanceTarget.value.primary_version_id, m.ids.primaryVersion)
  assert.equal(form.appearanceTarget.value.primary_input.item_key, inputs.draft.value.media.reference_1[0].itemKey)
  assert.ok(text(view.host).includes('实际主图输入：reference_1 / ' + inputs.draft.value.media.reference_1[0].itemKey))
  return { form, inputs, picker, editor }
}

test('actual form → exact P → original P input and difference → V generated_from P → original Job memory links', async t => {
  const view = await setup(t), { module: m, fixture, click, taskLink, mainBrowser } = view
  await primary(view)
  assert.deepEqual(fixture.router.resolve(taskLink().props.href).query, { category: 'asset', source_id: m.ids.primaryJob })
  const { form, inputs } = await variant(view)
  assert.deepEqual(form.appearanceErrors.value, [])
  const item = inputs.draft.value.media.reference_1[0]
  form.reviewed.value = true; await tick(); await click('创建生成任务')
  assert.equal(fixture.posts.value.length, 2)
  const payload = fixture.posts.value[1].payload
  assert.deepEqual(payload.inputs.reference_1.items, [{ kind: 'asset', asset_id: m.ids.primaryAsset, item_key: item.itemKey, purpose: null, scope_ref: { kind: 'request' } }])
  assert.equal(payload.manual_channels.positive_prompt, m.differenceText)
  assert.deepEqual(payload.appearance_target, { role: 'variant', setting_revision_id: null, primary_version_id: m.ids.primaryVersion,
    primary_input: { input_key: 'reference_1', item_key: item.itemKey }, difference_input: { kind: 'manual_channel', key: 'positive_prompt' } })
  assert.equal(fixture.created.value.id, m.ids.variantJob); assert.equal(fixture.variantReady.value, false)
  await click('载入模拟完成回执')
  const reader = mainBrowser(); await reader.flow.select(reader.state.catalog.results.find((row: any) => row.id === m.ids.variantSeries)); await tick()
  assert.equal(reader.state.version.id, m.ids.variantVersion); assert.equal(reader.state.version.generated_from_id, m.ids.primaryVersion)
  assert.equal(reader.state.asset.id, m.ids.variantAsset); assert.equal(reader.state.primary.id, m.ids.primarySeries)
  const link = taskLink(), route = fixture.router.resolve(link.props.href)
  assert.deepEqual(route.query, { category: 'asset', source_id: m.ids.variantJob })
  const reads = fixture.reads.value
  await fixture.interceptNavigation({ target: { closest: () => link }, preventDefault() {}, stopPropagation() {} }); await tick()
  assert.deepEqual(fixture.router.currentRoute.value.query, route.query)
  assert.match(text(view.host), /内存路由查询 · 非实际任务/); assert.equal(fixture.reads.value, reads)
  await click('返回组合夹具')
  const returned = mainBrowser(); await returned.flow.select(returned.state.catalog.results.find((row: any) => row.id === m.ids.variantSeries)); await tick()
  await click('查看生成依据主图版本')
  assert.equal(returned.state.version.id, m.ids.primaryVersion); assert.equal(returned.state.asset.id, m.ids.primaryAsset)
  assert.deepEqual(fixture.router.resolve(taskLink().props.href).query, { category: 'asset', source_id: m.ids.primaryJob })
  assert.equal(fixture.posts.value.length, 2); assert.equal(fixture.blocked.value, 0)
})

test('wrong original image blocks POST in the actual parent form; only explicit image correction and fresh review permit submission', async t => {
  const view = await setup(t), { fixture, module: m } = view
  await primary(view)
  const { form, inputs, picker } = await variant(view, true), item = inputs.draft.value.media.reference_1[0]
  assert.equal(item.assetId, m.ids.wrongAsset)
  assert.match(form.appearanceErrors.value.join('；'), /与确切主图版本相同的素材项/)
  assert.match(text(view.host), /不会自动替换图片/)
  form.reviewed.value = true; await tick()
  assert.equal(view.button('创建生成任务').props.disabled, true)
  const before = fixture.posts.value.length; await form.submit(); await tick()
  assert.equal(fixture.posts.value.length, before); assert.equal(form.pending.value, null)
  assert.equal(item.assetId, m.ids.wrongAsset, 'No automatic replacement with the chosen version asset')
  picker.select(picker.result.value.results.find((row: any) => row.id === m.ids.primaryAsset)); await tick()
  assert.deepEqual(form.appearanceErrors.value, []); assert.equal(form.reviewed.value, false)
  await form.submit(); assert.equal(fixture.posts.value.length, before)
  form.reviewed.value = true; await tick(); await view.click('创建生成任务')
  assert.equal(fixture.posts.value.length, before + 1)
  assert.equal(fixture.posts.value.at(-1).payload.inputs.reference_1.items[0].asset_id, m.ids.primaryAsset)
  assert.equal(fixture.blocked.value, 0, 'Mismatch was stopped before transport, not hidden by the mock adapter')
})

test('fixture refuses undeclared reads/writes/adapter escapes and raw network; never visits a business task page', async t => {
  const { api, fixture, transport } = await setup(t)
  for (const [method, url] of [['get', '/projects/'], ['post', '/production-runs/'], ['post', '/asset-reference-versions/'],
    ['delete', '/assets/'], ['post', '/asset-generation-jobs/']]) {
    await assert.rejects(api.request({ method, url, data: method === 'get' ? undefined : {}, adapter: () => assert.fail('No caller transport') }), /阻断/)
  }
  await assert.rejects(transport.fetch('/api/v1/assets/'), /阻断/)
  assert.throws(() => transport.XMLHttpRequest.prototype.send(), /阻断/)
  assert.throws(() => transport.navigator.sendBeacon('/api/v1/assets/'), /阻断/)
  assert.equal(fixture.blocked.value, 8); assert.equal(fixture.posts.value.length, 0)
  await fixture.interceptNavigation({ target: { closest: () => ({ getAttribute: () => '/projects' }) }, preventDefault() {}, stopPropagation() {} })
  assert.equal(fixture.router.currentRoute.value.name, 'journey')
})
