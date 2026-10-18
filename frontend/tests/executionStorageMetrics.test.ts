import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import http from 'node:http'
import https from 'node:https'
import net from 'node:net'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createSSRApp, createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { preparationId, storageReceipt } from './fixtures/execution-storage-metrics.ts'

const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve() }
async function setup(t: TestContext) {
  const unexpected: string[] = [], calls: any[] = []
  const block = (name: string): never => { unexpected.push(name); throw Error('Unexpected network ' + name) }
  t.mock.method(globalThis, 'fetch', () => block('fetch'))
  for (const module of [http, https]) for (const method of ['request', 'get'] as const) t.mock.method(module, method, () => block(method))
  t.mock.method(net.Socket.prototype, 'connect', () => block('socket'))
  const vite = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  t.after(async () => { await vite.close(); assert.deepEqual(unexpected, []) })
  const { adaptExecutionStorageMetrics: adapt, executionStorageApi: client } = await vite.ssrLoadModule('/src/services/executionStorageMetrics.ts')
  const Panel = (await vite.ssrLoadModule('/src/components/ExecutionStorageMetrics.vue')).default
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  let reply: (c: any) => any = () => storageReceipt()
  const adapter = async (config: any) => {
    if (config.method !== 'get' || config.url !== `/execution-preparations/${preparationId}/storage-metrics/`
      || config.data !== undefined || Object.keys(config.params).join() !== 'expected_preparation_revision') return block(config.url)
    calls.push(config.params.expected_preparation_revision)
    return { data: await reply(config), status: 200, statusText: 'OK', headers: {}, config }
  }
  api.interceptors.request.clear(); api.defaults.adapter = adapter
  api.interceptors.request.use((config: any) => { config.adapter = adapter; config.headers.delete('Authorization'); return config })
  const mount = () => {
    const input = reactive({ preparationId, revision: 8, disabled: false }); let state: any, mounted = true
    const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    const app = renderer.createApp({ setup() { state = Panel.setup(input, { emit() {}, expose() {} }); return () => h('div') } })
    app.provide(ssrContextKey, {}); app.mount({})
    const unmount = () => { if (mounted) { app.unmount(); mounted = false } }; t.after(unmount)
    const render = () => renderToString(createSSRApp({ render: () => h({ ...Panel, setup: () => state }, input) }))
    return { input, state, render, unmount }
  }
  return { adapt, client, mount, calls, setReply: (next: typeof reply) => { reply = next } }
}

test('only declared GET; exact byte and missing facts, no payload or physical-size claim', async t => {
  const { client, mount, calls } = await setup(t)
  const result = await client.get(preparationId, 8)
  assert.equal(result.items[3].sizeBytes, null); assert.equal(result.items[4].overCurrentLimitBytes, 1)
  const { state, render } = mount(); state.result.value = result; state.opened.value = true
  const html = await render()
  assert.match(html, /未生成 · 无字节记录/); assert.match(html, /不是数据库物理占用/)
  assert.match(html, /不代表本次已校验文件/); assert.match(html, /比当前上限多 1 字节/)
  assert.ok(html.includes(Number(72169).toLocaleString() + ' 字节'))
  assert.doesNotMatch(html, /<textarea|确认并采用|合计 \d|execution-requests\//)
  assert.deepEqual(calls, [8])
})

test('incomplete, stale, malformed or inconsistent metadata is rejected, never turned into zero', async t => {
  const { adapt } = await setup(t)
  const changes = [
    (r: any) => { r.preparation_id = 'other' }, (r: any) => { r.preparation_revision++ },
    (r: any) => { r.observed_at = 'bad-date' }, (r: any) => { r.items.pop() },
    (r: any) => { r.items[1] = r.items[0] }, (r: any) => { r.items[0].size_bytes = '13' },
    (r: any) => { r.items[0].size_bytes = 0 }, (r: any) => { r.items[0].size_bytes = Number.MAX_SAFE_INTEGER + 1 },
    (r: any) => { r.items[0].schema_version = 0 }, (r: any) => { r.items[0].sha256 = null },
    (r: any) => { r.items[3].size_bytes = 0 }, (r: any) => { r.items[3].over_current_limit_bytes = 0 },
    (r: any) => { r.items[0].state = 'not_generated' }, (r: any) => { r.items[4].schema_version = 2 },
    (r: any) => { r.items[4].over_current_limit_bytes = 0 }, (r: any) => { r.items[4].basis = 'physical' },
    (r: any) => { r.items[4].current_limit_bytes = 0 }, (r: any) => { r.items[4].label = '' },
  ]
  for (const change of changes) { const raw = storageReceipt(); change(raw); assert.throws(() => adapt(raw, preparationId, 8)) }
})

test('on-demand reader rejects stale races and failures, retries and invalidates on unmount without writes', async t => {
  const { mount, calls, setReply } = await setup(t), { state, input, render, unmount } = mount()
  const pending: any[] = []; setReply(() => new Promise((resolve, reject) => pending.push({ resolve, reject })))
  await nextTick(); assert.deepEqual(calls, [])
  input.disabled = true; await state.load(); assert.deepEqual(calls, [])
  input.disabled = false; const first = state.load(); await flush(); await state.load(); assert.deepEqual(calls, [8])
  input.revision = 9; await nextTick(); pending[0].resolve(storageReceipt()); await first
  assert.equal(state.result.value, null); assert.equal(state.opened.value, false)
  const failure = state.load(); await flush(); pending[1].reject(Error('权限不足，无法读取')); await failure
  assert.match(await render(), /权限不足，无法读取/); assert.equal(state.result.value, null)
  setReply(() => storageReceipt(9)); await state.load(); assert.equal(state.result.value.preparationRevision, 9)
  input.preparationId = 'changed'; await nextTick(); assert.equal(state.result.value, null)
  input.preparationId = preparationId; await nextTick(); setReply(() => new Promise(resolve => pending.push({ resolve })))
  const last = state.load(); await flush(); unmount(); pending[2].resolve(storageReceipt(9)); await last
  assert.equal(state.result.value, null)
})
