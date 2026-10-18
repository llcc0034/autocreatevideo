import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createServer, build } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

const root = fileURLToPath(new URL('..', import.meta.url))
const config = { root, configFile: false as const, envFile: false as const, logLevel: 'silent' as const,
  plugins: [vue()], resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false as const, ws: false, watch: null },
}
const settle = async () => { for (let i = 0; i < 24; i++) { await Promise.resolve(); await nextTick() } }
function pending<T = any>() {
  let resolve!: (value: T) => void, reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

async function harness(context: any, options: { attempt?: string; readonly?: boolean; pendingInitial?: boolean; reply?: (fixture: any, id: string, query: any) => any } = {}) {
  const vite = await createServer(config)
  const storage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem: () => null } })
  let app: any, isolation: any
  context.after(async () => {
    app?.unmount(); isolation?.restore(); await vite.close()
    if (storage) Object.defineProperty(globalThis, 'localStorage', storage)
    else Reflect.deleteProperty(globalThis, 'localStorage')
  })
  const fixture = await vite.ssrLoadModule('/tests/fixtures/task-detail-evidence-identity.ts')
  const { default: Panel } = await vite.ssrLoadModule('/src/components/TaskCenterDetail.vue')
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  let respond = (id: string, q: any) => options.reply ? options.reply(fixture, id, q) : fixture.receipt(q.attempt_id || fixture.ids.b, id)
  // The real taskCenterApi + adapter execute; every undeclared transport is blocked.
  const firstReply = pending<void>()
  isolation = fixture.isolateHttp(async (id: string, q: any) => {
    try { return await respond(id, q) } finally { firstReply.resolve() }
  })
  const props = reactive({ kind: 'preparation', id: fixture.ids.preparation,
    attemptId: options.attempt ?? fixture.ids.a, readonly: options.readonly ?? true, expectedProjectId: fixture.ids.project })
  const events: any[] = []
  let state: any
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
    insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  app = renderer.createApp({ setup() { state = Panel.setup(props, { emit: (...args: any[]) => events.push(args), expose() {} }); return () => h('div') } })
  app.use(createPinia()); app.provide(ssrContextKey, { modules: new Set() }); app.mount({})
  if (!options.pendingInitial) await firstReply.promise
  await settle()
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] })
  await router.push('/fixture'); await router.isReady()
  async function html() {
    // Render the actual SFC template with its live setup state; no rewritten view.
    const snapshot = createSSRApp({ render: () => h({ ...Panel, setup: () => state }, props) })
    snapshot.use(router)
    return renderToString(snapshot)
  }
  return { fixture, props, state, events, isolation, api, html, app,
    respond: (reply: typeof respond) => { respond = reply } }
}

test('指定历史 A uses real API/adapter and renders only A input, output and submission identity', async context => {
  const { fixture: f, state, isolation, html } = await harness(context)
  assert.equal(state.data.value.source_id, f.ids.a); assert.equal(state.error.value, '')
  assert.deepEqual(isolation.requests.value[0].query, { call_page: 1, output_page: 1, attempt_id: f.ids.a })
  state.changeTab('calls')
  const rendered = await html()
  assert.match(rendered, /当前查看指定调用：synthetic-attempt-a/)
  assert.match(rendered, /合成输出 synthetic-attempt-a/)
  assert.doesNotMatch(rendered, /合成输出 synthetic-attempt-b/)
  assert.match(rendered, /generation_attempt · synthetic-attempt-a/)
  assert.equal(isolation.blocked.value, 0)
})

test('指定 A rejects B, wrong source kind, null and blank identity before displaying any evidence', async context => {
  for (const variant of ['other-attempt', 'wrong-kind', 'missing-id', 'empty-id']) {
    await context.test(variant, async sub => {
      const view = await harness(sub, { reply: (f, id) => {
        const value = f.receipt(variant === 'other-attempt' ? f.ids.b : f.ids.a, id)
        if (variant === 'wrong-kind') value.source_kind = 'asset_job'
        if (variant === 'missing-id') value.source_id = null
        if (variant === 'empty-id') value.source_id = ''
        return value
      } })
      assert.equal(view.state.data.value, null)
      assert.match(view.state.error.value, /指定调用.*不一致/)
      const rendered = await view.html()
      assert.match(rendered, /role="alert"/); assert.doesNotMatch(rendered, /class="task-summary"|合成输出|已读取本地登记/)
    })
  }
})

test('rapid A → B ignores late A success and failure without overwriting B', async context => {
  for (const lateFailure of [false, true]) await context.test(String(lateFailure), async sub => {
    const late = pending()
    const view = await harness(sub, { pendingInitial: true, reply: (f, id, q) => q.attempt_id === f.ids.a ? late.promise : f.receipt(f.ids.b, id) })
    view.props.attemptId = view.fixture.ids.b; await settle()
    assert.equal(view.state.data.value.source_id, view.fixture.ids.b)
    if (lateFailure) late.reject(Error('迟到 A 失败'))
    else late.resolve(view.fixture.receipt(view.fixture.ids.a))
    await settle()
    assert.equal(view.state.data.value.source_id, view.fixture.ids.b)
    assert.equal(view.state.error.value, ''); assert.equal(view.state.loading.value, false)
  })
})

test('switching identity during full-body read releases its lock and discards late chunks', async context => {
  const view = await harness(context, { reply: async (f, id, q) => (await f.deferredReceipt(q.attempt_id, q.output_page, id)).value })
  const late = pending(), original = await view.fixture.deferredReceipt()
  view.respond((id, q) => q.content_field ? late.promise : view.fixture.receipt(q.attempt_id, id))
  const reading = view.state.readField('output.text'); await settle()
  assert.equal(view.state.reading.value, 'output.text')
  view.props.attemptId = view.fixture.ids.b; await settle()
  assert.equal(view.state.reading.value, '')
  assert.equal(view.state.data.value?.source_id, view.fixture.ids.b)
  late.resolve({ ...original.value, content: { field: 'output.text', offset: 0, next_offset: null,
    text: original.encoded, total_characters: Array.from(original.encoded).length, sha256: original.sha256 } })
  await reading; await settle()
  assert.equal(view.state.data.value.source_id, view.fixture.ids.b)
  assert.equal(view.state.data.value.output.text, `合成输出 ${view.fixture.ids.b}`)
  assert.equal(view.state.notice.value, ''); assert.equal(view.state.error.value, '')
})

test('call/output pages and all body chunks retain A; cached bodies are scoped to output page and selection', async context => {
  const view = await harness(context, { reply: async (f, id, q) => (await f.deferredReceipt(q.attempt_id, q.output_page, id)).value })
  view.respond(async (id, q) => {
    const part = await view.fixture.deferredReceipt(q.attempt_id, q.output_page, id)
    if (q.content_field) {
      const chars = Array.from(part.encoded), end = Math.min(q.content_offset + 13, chars.length)
      part.value.content = { field: q.content_field, offset: q.content_offset, next_offset: end < chars.length ? end : null,
        text: chars.slice(q.content_offset, end).join(''), total_characters: chars.length, sha256: part.sha256 }
    }
    return part.value
  })
  await view.state.readField('output.text')
  assert.equal(view.state.error.value, '')
  assert.match(view.state.data.value.output.text, /产物页 1 😀/)
  await view.state.pageCalls(1)
  assert.match(view.state.data.value.output.text, /产物页 1/)
  await view.state.pageOutput(1)
  assert.equal(view.state.data.value.output.text, '')
  await view.state.readField('output.text')
  assert.match(view.state.data.value.output.text, /产物页 2 😀/)
  assert.ok(view.isolation.requests.value.filter((r: any) => r.query.content_field).length > 2)
  for (const r of view.isolation.requests.value) assert.equal(r.query.attempt_id, view.fixture.ids.a)
  assert.equal(view.isolation.requests.value.at(-1).query.call_page, 2)
  assert.equal(view.isolation.requests.value.at(-1).query.output_page, 2)
  view.props.attemptId = view.fixture.ids.b; await settle()
  // Digest generation is async; wait for the actual request to settle.
  await view.state.load()
  assert.equal(view.state.data.value.output.text, '')
  assert.equal(view.state.outputPage.value, 1); assert.equal(view.state.callPage.value, 1)
})

test('same-task failed or mismatched refresh keeps explicit stale A; selection failure never retains A under B', async context => {
  const view = await harness(context)
  for (const failure of ['network', 'identity']) {
    view.state.notice.value = '完整正文已读取；内容来自本次保存记录。'
    view.respond(() => { if (failure === 'network') throw Error('合成网络读取失败'); return view.fixture.receipt(view.fixture.ids.b) })
    await view.state.refresh()
    assert.equal(view.state.data.value.source_id, view.fixture.ids.a)
    assert.match(await view.html(), /当前保留上次读取结果，尚未更新/)
    assert.equal(view.state.notice.value, '')
  }
  view.respond(() => { throw Error('B 不可读') })
  view.props.attemptId = view.fixture.ids.b; await settle()
  assert.equal(view.state.data.value, null); assert.match(view.state.error.value, /B 不可读/)
  assert.doesNotMatch(await view.html(), /合成输出|当前保留上次/)
  view.respond((id, q) => view.fixture.receipt(q.attempt_id, id)); await view.state.refresh()
  assert.equal(view.state.data.value.source_id, view.fixture.ids.b); assert.equal(view.state.error.value, '')
})

test('body response from another attempt, kind or project never attaches to the selected evidence', async context => {
  const view = await harness(context)
  const part = await view.fixture.deferredReceipt()
  for (const field of ['source_id', 'source_kind', 'project_id']) {
    view.respond(() => part.value); await view.state.refresh()
    view.respond(() => ({ ...part.value, [field]: 'foreign', content: { field: 'output.text', offset: 0, next_offset: null,
      text: part.encoded, total_characters: Array.from(part.encoded).length, sha256: part.sha256 } }))
    await view.state.readField('output.text')
    assert.notEqual(view.state.error.value, '', field)
    assert.equal(view.state.data.value.output.text, '', field)
    assert.equal(view.state.data.value.deferred_fields.length, 1)
  }
})

test('failed body pagination never caches partial text and retry can load the original complete body', async context => {
  const view = await harness(context), part = await view.fixture.deferredReceipt()
  view.respond(() => part.value); await view.state.refresh()
  view.respond((_id, q) => {
    if (q.content_offset) throw Error('合成第二片读取失败')
    return { ...part.value, content: { field: 'output.text', offset: 0, next_offset: 8,
      text: part.encoded.slice(0, 8), total_characters: Array.from(part.encoded).length, sha256: part.sha256 } }
  })
  await view.state.readField('output.text')
  assert.match(view.state.error.value, /第二片读取失败/); assert.equal(view.state.data.value.output.text, '')
  assert.equal(view.state.contentCache.size, 0); assert.equal(view.state.reading.value, '')
  view.respond(() => ({ ...part.value, content: { field: 'output.text', offset: 0, next_offset: null,
    text: part.encoded, total_characters: Array.from(part.encoded).length, sha256: part.sha256 } }))
  await view.state.readField('output.text')
  assert.equal(view.state.error.value, ''); assert.equal(view.state.data.value.output.text, part.body)
})

test('unsupported protocol and absent evidence remain unavailable even when the task status is succeeded', async context => {
  const view = await harness(context)
  for (const message of ['该协议未支持传输证据，无法核对。', '原调用缺少证据，不补造历史。']) {
    view.respond(() => {
      const value = view.fixture.receipt(); value.input.state = 'unavailable'; value.input.prompts = []
      value.input.notes = [message]; value.output = { state: 'unavailable', text: '', result: null, media: [], notes: [message] }
      return value
    })
    await view.state.refresh(); view.state.changeTab('calls')
    const rendered = await view.html()
    assert.ok(rendered.includes(message)); assert.match(rendered, /提交证据不可用/)
    assert.doesNotMatch(rendered, /已读取本地登记|冻结输入校验通过|合成输出/)
  }
  view.respond(() => { const value = view.fixture.receipt(); delete (value as any).submission; return value })
  await view.state.refresh()
  assert.match(view.state.error.value, /任务中心响应不完整/)
  assert.match(await view.html(), /当前保留上次读取结果，尚未更新/)
})

test('historical attempt is read-only even without embedding readonly prop; no media mutations or polling', async context => {
  const view = await harness(context, { readonly: false, reply: f => {
    const value = f.receipt(); value.links.generation_task_id = 'synthetic-generation-task'; return value
  } })
  view.state.changeTab('media')
  assert.equal(view.state.tab.value, 'input')
  assert.doesNotMatch(await view.html(), /素材操作与媒体诊断/)
  let timers = 0
  context.mock.method(globalThis, 'setTimeout', (() => { timers++; return 0 }) as any)
  view.state.data.value.status = 'running'; view.state.schedulePoll()
  assert.equal(timers, 0)
  view.state.data.value.source_kind = 'workflow_smoke'; view.state.data.value.actions.cancel_smoke = true
  view.state.openCancelTask(); assert.equal(view.state.cancelTarget.value, null)
  await view.state.cancelTask(); assert.equal(view.isolation.blocked.value, 0)
})

test('project/task mismatches, account changes and unmount preserve isolation', async context => {
  const view = await harness(context)
  for (const field of ['id', 'kind', 'project_id']) {
    view.respond(() => ({ ...view.fixture.receipt(), [field]: field === 'kind' ? 'assembly' : 'foreign' }))
    await view.state.refresh(); assert.notEqual(view.state.error.value, '')
    assert.equal(view.state.data.value.source_id, view.fixture.ids.a)
  }
  view.respond((id, q) => view.fixture.receipt(q.attempt_id, id))
  const late = pending(); view.respond(() => late.promise)
  view.state.auth.user = { id: 'synthetic-new-user' }; await settle()
  assert.equal(view.state.data.value, null)
  view.app.unmount(); late.resolve(view.fixture.receipt()); await settle()
  assert.equal(view.state.data.value, null)
})

test('fixture blocks undeclared GET, all writes, adapter overrides and raw transports before credentials/network', async context => {
  const view = await harness(context)
  const attacks = [() => view.api.get('/assets/'), () => view.api.get('https://example.invalid/'),
    () => view.api.get(`/task-center/preparation/${view.fixture.ids.preparation}/`, { params: { call_page: 1, output_page: 1 }, adapter: () => assert.fail('adapter bypass') }),
    () => view.api.post('/task-center/'), () => view.api.delete('/task-center/'),
    () => view.api.request({ method: 'get', url: '/assets/', adapter: () => assert.fail('adapter bypass') }),
    () => fetch('https://example.invalid/')]
  for (const attack of attacks) await assert.rejects(attack, /合成夹具禁止/)
  const transport = { XMLHttpRequest: { prototype: { send() { assert.fail('XHR escaped') } } }, navigator: { sendBeacon() { assert.fail('beacon escaped'); return false } } }
  const extra = view.fixture.isolateHttp(async () => assert.fail('unexpected read'), transport)
  try { assert.throws(() => transport.XMLHttpRequest.prototype.send(), /合成夹具禁止/); assert.throws(() => transport.navigator.sendBeacon(), /合成夹具禁止/) }
  finally { extra.restore() }
  assert.equal(view.isolation.blocked.value, attacks.length)
})

test('browser fixture builds in memory with no external font, connection, output files, watch or WebSocket', async () => {
  const result: any = await build({ ...config, build: { write: false, minify: false,
    rollupOptions: { input: fileURLToPath(new URL('./fixtures/task-detail-evidence-identity.html', import.meta.url)) } } })
  assert.ok(result.output.some((file: any) => file.fileName.endsWith('.html')))
  const html = readFileSync(new URL('./fixtures/task-detail-evidence-identity.html', import.meta.url), 'utf8')
  assert.match(html, /connect-src 'none'/); assert.doesNotMatch(html, /https?:\/\/|wss?:\/\//)
  assert.equal(config.envFile, false); assert.equal(config.server.ws, false); assert.equal(config.server.watch, null)
})
