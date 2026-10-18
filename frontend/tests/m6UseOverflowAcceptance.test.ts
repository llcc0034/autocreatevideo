import assert from 'node:assert/strict'
import test from 'node:test'
import {readFileSync} from 'node:fs'
import {fileURLToPath} from 'node:url'
import http from 'node:http'
import https from 'node:https'
import net from 'node:net'
import {createServer} from 'vite'
import {createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey} from 'vue'
import {renderToString} from 'vue/server-renderer'

const root = fileURLToPath(new URL('..', import.meta.url))
// Captured from the passing isolated backend acceptance tests. Keep only the
// actual two read APIs and preparation identity so this suite runs without
// machine-local .artifacts or a live backend. Browser media remains opt-in.
const captured = JSON.parse(readFileSync(new URL('./fixtures/m6-use-overflow-receipts.json', import.meta.url), 'utf8'))
const receipts = (name: string) => captured[name]
const escape = (text: string) => text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
const flush = async () => {for (let index = 0; index < 15; index++) await Promise.resolve()}

async function setup(t: any) {
  const unexpected: string[] = [], calls: any[] = []
  const block = (method: string): never => {unexpected.push(method); throw Error('No real network: ' + method)}
  t.mock.method(globalThis, 'fetch', () => block('fetch'))
  for (const module of [http, https]) for (const method of ['get', 'request'] as const) t.mock.method(module, method, () => block(method))
  t.mock.method(net.Socket.prototype, 'connect', () => block('socket.connect'))
  const vite = await createServer({root, logLevel: 'silent', appType: 'custom', server: {middlewareMode: true, hmr: false}})
  t.after(async () => {await vite.close(); assert.deepEqual(unexpected, [])})
  const Prompt = (await vite.ssrLoadModule('/src/components/ExecutionPromptDisplay.vue')).default
  const Metrics = (await vite.ssrLoadModule('/src/components/ExecutionStorageMetrics.vue')).default
  const {api} = await vite.ssrLoadModule('/src/services/api.ts')
  let reply: (config: any) => any = () => {throw Error('No declared reply')}
  const adapter = async (config: any) => {
    assert.equal(config.method, 'get'); assert.equal(config.data, undefined)
    assert.deepEqual(Object.keys(config.params), ['expected_preparation_revision'])
    calls.push({url: config.url, revision: config.params.expected_preparation_revision})
    return {config, data: await reply(config), status: 200, statusText: 'OK', headers: {}}
  }
  api.interceptors.request.clear(); api.defaults.adapter = adapter
  api.interceptors.request.use((config: any) => {config.adapter = adapter; config.headers.delete('Authorization'); return config})
  function mount(receipt: any) {
    const props = reactive({preparationId: receipt.preparation.id, revision: receipt.preparation.revision,
      frozen: false, disabled: false, fieldLabels: {unit_description: '逐镜描述', configured_action: '动作'}})
    let prompt: any, metrics: any
    const renderer = createRenderer<any, any>({createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
      insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}})
    const app = renderer.createApp({setup() {
      prompt = Prompt.setup(props, {emit() {}, expose() {}})
      metrics = Metrics.setup(props, {emit() {}, expose() {}})
      return () => h('div')
    }})
    app.provide(ssrContextKey, {}); app.mount({}); t.after(() => app.unmount())
    const render = () => renderToString(createSSRApp({render: () => h('div', [
      h({...Prompt, setup: () => prompt}, props), h({...Metrics, setup: () => metrics}, props)])}))
    return {props, prompt, metrics, render}
  }
  const setReceipt = (receipt: any) => {reply = config => {
    assert.equal(config.params.expected_preparation_revision, receipt.preparation.revision)
    const base = `/execution-preparations/${receipt.preparation.id}/`
    if (config.url === base + 'prompt-display/') return structuredClone(receipt.prompt)
    assert.equal(config.url, base + 'storage-metrics/'); return structuredClone(receipt.metrics)
  }}
  return {mount, calls, setReceipt, setReply: (value: typeof reply) => {reply = value}}
}

test('actual mixed-media API receipts show both uses of one audio alongside all five original correspondences', async t => {
  const env = await setup(t)
  for (const receipt of receipts('mixed')) {
    env.setReceipt(receipt)
    const view = env.mount(receipt)
    await view.prompt.load(); await view.metrics.load()
    assert.equal(view.prompt.error.value, ''); assert.equal(view.metrics.error.value, '')
    const references = view.prompt.result.value.sources.references
    assert.equal(references.length, 5)
    const audios = references.filter((row: any) => row.category === 'audio')
    assert.deepEqual(audios.map((row: any) => row.label), ['<Audio 1>', '<Audio 2>'])
    assert.notEqual(audios[0].name, audios[1].name)
    const html = await view.render()
    for (const row of references) {assert.ok(html.includes(escape(row.label))); assert.ok(html.includes(escape(row.name)))}
    for (const row of receipt.prompt.channels) assert.ok(html.includes(escape(row.text)))
    assert.match(html, /未生成 · 无字节记录/)
    assert.doesNotMatch(html, /确认并采用|冻结并提交/)
  }
})

test('actual candidate/adopted/blocked Unicode receipts invalidate both panels and discard late old-revision results', async t => {
  const env = await setup(t), [candidate, adopted, blocked] = receipts('overflow')
  const view = env.mount(candidate)
  await nextTick(); assert.equal(env.calls.length, 0)
  const pending: any[] = []
  env.setReply(config => new Promise(resolve => pending.push({config, resolve})))
  const oldPrompt = view.prompt.load(), oldMetrics = view.metrics.load()
  await flush(); assert.equal(pending.length, 2)
  view.props.revision = adopted.preparation.revision
  await nextTick()
  assert.equal(view.prompt.opened.value, false); assert.equal(view.metrics.opened.value, false)
  for (const row of pending) row.resolve(structuredClone(row.config.url.endsWith('prompt-display/') ? candidate.prompt : candidate.metrics))
  await Promise.all([oldPrompt, oldMetrics])
  assert.equal(view.prompt.result.value, null); assert.equal(view.metrics.result.value, null)
  assert.equal(env.calls.length, 2)
  for (const receipt of [adopted, blocked]) {
    view.props.revision = receipt.preparation.revision; await nextTick()
    env.setReceipt(receipt)
    await view.prompt.load(); await view.metrics.load()
    assert.equal(view.prompt.error.value, ''); assert.equal(view.metrics.error.value, '')
    assert.equal(view.prompt.result.value.mode, 'diagnostic')
    const html = await view.render()
    for (const channel of receipt.prompt.channels) {
      assert.equal(Array.from(channel.text).length, channel.character_count)
      if (channel.key === 'positive_prompt') assert.ok(Buffer.byteLength(channel.text, 'utf8') > channel.character_count)
      else assert.ok(Buffer.byteLength(channel.text, 'utf8') >= channel.character_count)
      assert.ok(html.includes(escape(channel.text)))
    }
    assert.match(html, /已采用/); assert.match(html, /未通过校验，不可提交/)
    assert.match(html, /未生成 · 无字节记录/); assert.doesNotMatch(html, /仅用于预览，尚未采用|实际请求保存内容/)
    assert.equal(view.metrics.result.value.preparationRevision, view.prompt.result.value.preparationRevision)
  }
  assert.equal(env.calls.length, 6)
})
