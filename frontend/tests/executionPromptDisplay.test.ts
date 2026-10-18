import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import http from 'node:http'
import https from 'node:https'
import net from 'node:net'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createSSRApp, createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'

const root = fileURLToPath(new URL('..', import.meta.url))
const escape = (text: string) => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;')
const otherId = '00000000-0000-4000-8000-000000000999'
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve() }
async function setup(t: TestContext) {
  const unexpected: string[] = [], calls: any[] = []
  const block = (entry: string): never => { unexpected.push(entry); throw Error('Unexpected network: ' + entry) }
  t.mock.method(globalThis, 'fetch', () => block('fetch'))
  for (const module of [http, https]) for (const method of ['request', 'get'] as const) t.mock.method(module, method, () => block(method))
  t.mock.method(net.Socket.prototype, 'connect', () => block('socket.connect'))
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  t.after(async () => { await vite.close(); assert.deepEqual(unexpected, [], 'No unexpected HTTP, write or network is permitted') })
  const fixture = await vite.ssrLoadModule('/tests/fixtures/prompt-length-diagnostics.ts')
  const { adaptExecutionPromptDisplay: adapt, executionPromptApi: client } = await vite.ssrLoadModule('/src/services/executionPromptDisplay.ts')
  const Panel = (await vite.ssrLoadModule('/src/components/ExecutionPromptDisplay.vue')).default
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  let reply: (config: any) => any = () => fixture.promptReceipt()
  const adapter = async (config: any) => {
    const method = config.method?.toUpperCase(), params = config.params
    if (method !== 'GET' || ![fixture.preparationId, otherId].some(id => config.url === '/execution-preparations/' + id + '/prompt-display/')
      || !params || Object.keys(params).length !== 1 || ![8, 9].includes(params.expected_preparation_revision) || config.data !== undefined) return block(method + ' ' + config.url)
    calls.push({ method, url: config.url, params: { ...params } })
    return { data: await reply(config), status: 200, statusText: 'OK', headers: {}, config }
  }
  api.interceptors.request.clear(); api.defaults.adapter = adapter
  api.interceptors.request.use((config: any) => { config.adapter = adapter; config.headers.delete('Authorization'); return config })
  const props = () => ({ preparationId: fixture.preparationId, revision: 8, frozen: false, disabled: false, fieldLabels: fixture.fieldLabels })
  const render = async (value: any) => {
    const Loaded = { ...Panel, setup(p: any, c: any) { const state = Panel.setup(p, c); state.result.value = value; state.opened.value = true; return state } }
    return renderToString(createSSRApp({ render: () => h(Loaded, { ...props(), frozen: value.mode === 'frozen' }) }))
  }
  const mount = () => {
    const input = reactive(props()); let state: any, mounted = true
    const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    const app = renderer.createApp({ setup() { state = Panel.setup(input, { emit() {}, expose() {} }); return () => h('div') } })
    app.provide(ssrContextKey, {}); app.mount({})
    const unmount = () => { if (mounted) { app.unmount(); mounted = false } }; t.after(unmount)
    const renderState = () => renderToString(createSSRApp({ render: () => h({ ...Panel, setup: () => state }, input) }))
    return { input, state, unmount, renderState }
  }
  return { fixture, adapt, client, api, render, mount, calls, setReply: (next: typeof reply) => { reply = next } }
}

test('strict reader rejects cross-preparation, stale revision and mislabelled final content; uses only declared GET', async t => {
  const { fixture: f, adapt, client, calls } = await setup(t), raw = f.promptReceipt()
  const result = adapt(raw, f.preparationId, 8)
  assert.equal(result.sources.dialogue[0].text, raw.sources.dialogue[0].text)
  for (const bad of [{ ...raw, preparation_id: otherId }, { ...raw, preparation_revision: 9 },
    { ...raw, mode: 'frozen' }, { ...raw, request_sha256: 'a'.repeat(64) },
    { ...raw, channels: [] }, { ...raw, mode: 'unavailable' },
    { ...raw, field_basis: [{ ...raw.field_basis[0], origin: 'adopted' }] }]) assert.throws(() => adapt(bad, f.preparationId, 8))
  await client.get(f.preparationId, 8)
  assert.deepEqual(calls, [{ method: 'GET', url: '/execution-preparations/' + f.preparationId + '/prompt-display/', params: { expected_preparation_revision: 8 } }])
})

test('complete original text stays escaped and untruncated; preview/frozen keep identical saved lengths and exact provenance', async t => {
  const { fixture: f, adapt, render } = await setup(t)
  const preview = adapt(f.promptReceipt('preview'), f.preparationId, 8), frozen = adapt(f.promptReceipt('frozen'), f.preparationId, 8)
  assert.deepEqual(preview.channels, frozen.channels)
  assert.deepEqual(preview.lengthDiagnostics, frozen.lengthDiagnostics)
  assert.deepEqual(preview.profileBasis, frozen.profileBasis)
  assert.equal(preview.profileBasis.versionId, f.profileVersionId); assert.equal(preview.profileBasis.version, 7)
  for (const value of [preview, frozen]) {
    const html = await render(value)
    for (const channel of value.channels) if (channel.text) assert.ok(html.includes(escape(channel.text)), 'Full ' + channel.key + ' text must be readable')
    assert.match(html, /沈念/); assert.match(html, /1.093–2.37/)
    assert.match(html, /&lt;script&gt;untrusted&lt;\/script&gt;😀/)
    assert.match(html, /本次策略版本/); assert.ok(html.includes(f.profileVersionId)); assert.ok(html.includes(f.profileSha))
    assert.ok(html.includes(f.definitionVersionId)); assert.match(html, /projection.output/)
    assert.match(html, /系统投影 · 参考关系/); assert.match(html, /系统段 · 原对白约束/)
    assert.match(html, /上限来源/); assert.match(html, /frozen_target.input_schema.positive_prompt/)
    assert.doesNotMatch(html, /<script>|<textarea|确认并采用|冻结并提交|继续生成/)
  }
  const html = await render(preview), finalHtml = await render(frozen)
  assert.match(html, /尚未冻结，不代表已发送/); assert.match(html, /仅用于预览，尚未采用/)
  assert.match(finalHtml, /实际请求保存内容/); assert.match(finalHtml, /冻结不等于已发送/)
  assert.doesNotMatch(finalHtml, /仅用于预览，尚未采用|尚未冻结，不代表已发送/)
})

test('two 2000-codepoint fields each comply but accumulated text exceeds the effective Profile or target limit', async t => {
  const { fixture: f, adapt, render, mount, setReply, calls } = await setup(t)
  assert.equal(Array.from(f.directionText.slice(3)).length, 2000)
  assert.equal(Array.from(f.soundText.slice(3)).length, 2000)
  assert.equal(Array.from(f.positiveText).length, 4027); assert.equal(f.positiveText.length, 5027)
  for (const scenario of ['cumulative', 'target']) {
    const raw = f.promptReceipt(scenario), value = adapt(raw, f.preparationId, 8), length = value.lengthDiagnostics[0]
    assert.equal(length.characterCount, 4027); assert.equal(length.maxChars, 4000); assert.equal(length.overBy, 27)
    assert.ok(length.fragments.every((fragment: any) => fragment.textChars <= fragment.maxChars))
    assert.deepEqual(length.fragments.map((fragment: any) => [fragment.textChars, fragment.separatorChars]), [[2003, 0], [2003, 2], [17, 2]])
    assert.equal(length.limitSources[scenario === 'target' ? 1 : 0].maxChars, 4000)
    const html = await render(value)
    assert.match(html, /未通过校验，不可提交/); assert.match(html, /没有截断/)
    assert.ok(html.includes('通道合计 ' + Number(4027).toLocaleString() + ' 字符'))
    assert.ok(html.includes('生效上限 ' + Number(4000).toLocaleString() + ' 字符'))
    assert.match(html, /超出 27 字符/); assert.match(html, /分隔符 0 字符/); assert.match(html, /分隔符 2 字符/)
    assert.ok(html.includes('正文与标签 ' + Number(2003).toLocaleString() + ' 字符')); assert.ok(html.includes('字段上限 ' + Number(2100).toLocaleString() + ' 字符'))
    assert.ok(html.includes(escape(f.positiveText))); assert.doesNotMatch(html, /实际请求保存内容|<textarea|确认并采用/)
  }
  setReply(() => f.promptReceipt('target'))
  const { state } = mount(); await state.load()
  assert.equal(state.result.value.mode, 'diagnostic'); assert.equal(calls.length, 1); assert.equal(calls[0].method, 'GET')
})

test('unconfigured caps and empty channels differ from unavailable and manual frozen without Profile diagnostics', async t => {
  const { fixture: f, adapt, render } = await setup(t)
  const unlimited = adapt(f.promptReceipt('unlimited'), f.preparationId, 8), html = await render(unlimited)
  assert.equal(unlimited.lengthDiagnostics[0].maxChars, null); assert.equal(unlimited.lengthDiagnostics[0].overBy, 0)
  assert.match(html, /通道上限未配置/); assert.match(html, /通道合计 0 字符/)
  assert.match(html, /此通道组装为空文本/); assert.doesNotMatch(html, /此通道保存为空文本/)
  const manual = adapt(f.promptReceipt('manual'), f.preparationId, 8), manualHtml = await render(manual)
  assert.equal(manual.profileBasis, null); assert.deepEqual(manual.lengthDiagnostics, [])
  assert.match(manualHtml, /未使用策略组装/); assert.match(manualHtml, /此通道保存为空文本/)
  assert.doesNotMatch(manualHtml, /字段定义版本 ID|策略合同 SHA256|生效上限/)
  const missing = adapt(f.promptReceipt('unavailable'), f.preparationId, 8), missingHtml = await render(missing)
  assert.equal(missing.profileBasis, null); assert.deepEqual(missing.lengthDiagnostics, [])
  assert.match(missingHtml, /暂不能生成完整预览/); assert.match(missingHtml, /不能改取当前策略/)
  assert.doesNotMatch(missingHtml, /prompt-body|策略合同 SHA256/)
})

test('malformed counts, missing versions/sources and inconsistent effective limits are rejected, never repaired', async t => {
  const { fixture: f, adapt, mount, setReply, calls } = await setup(t)
  const mutations: [string, (raw: any) => void][] = [
    ['UTF16 instead of codepoints', r => { r.channels[0].character_count = 5027 }],
    ['negative', r => { r.length_diagnostics[0].fragments[0].text_chars = -1 }],
    ['fraction', r => { r.length_diagnostics[0].fragments[0].separator_chars = 0.5 }],
    ['unsafe integer', r => { r.length_diagnostics[0].character_count = Number.MAX_SAFE_INTEGER + 1 }],
    ['string count', r => { r.channels[0].character_count = '4027' }],
    ['missing channel diagnostic', r => { r.length_diagnostics.pop() }],
    ['duplicate channel diagnostic', r => { r.length_diagnostics[1] = r.length_diagnostics[0] }],
    ['sum mismatch', r => { r.length_diagnostics[0].fragments[0].text_chars-- }],
    ['over_by mismatch', r => { r.length_diagnostics[0].over_by = 1 }],
    ['missing Profile', r => { r.profile_basis = null }],
    ['Profile version', r => { r.profile_basis.version = 0 }],
    ['Profile id', r => { r.profile_basis.version_id = 'current' }],
    ['malformed 36-char UUID', r => { r.profile_basis.version_id = '-'.repeat(36) }],
    ['Profile fingerprint', r => { r.profile_basis.contract_sha256 = 'current' }],
    ['missing definition', r => { delete r.length_diagnostics[0].fragments[0].field_definition_version_id }],
    ['definition UUID', r => { r.length_diagnostics[0].fragments[0].field_definition_version_id = 'v1' }],
    ['malformed definition UUID', r => { r.length_diagnostics[0].fragments[0].field_definition_version_id = '-'.repeat(36) }],
    ['missing selector', r => { delete r.length_diagnostics[0].fragments[0].source_selector }],
    ['empty selector', r => { r.length_diagnostics[0].fragments[0].source_selector = '' }],
    ['non-positive field cap', r => { r.length_diagnostics[0].fragments[0].max_chars = 0 }],
    ['fractional field cap', r => { r.length_diagnostics[0].fragments[0].max_chars = 2100.5 }],
    ['field over cap', r => { r.length_diagnostics[0].fragments[0].max_chars = 2000 }],
    ['null field cap', r => { r.length_diagnostics[0].fragments[0].max_chars = null }],
    ['effective minimum mismatch', r => { r.length_diagnostics[0].limit_sources[1].max_chars = 4000 }],
    ['empty limit source', r => { r.length_diagnostics[0].limit_sources[0].source = '' }],
    ['duplicate limit source', r => { r.length_diagnostics[0].limit_sources.push(r.length_diagnostics[0].limit_sources[0]) }],
    ['invalid source cap', r => { r.length_diagnostics[0].limit_sources[0].max_chars = 0 }],
  ]
  for (const [label, mutate] of mutations) { const raw = f.promptReceipt(); mutate(raw); assert.throws(() => adapt(raw, f.preparationId, 8), undefined, label) }
  for (const scenario of ['preview', 'frozen']) {
    const raw = f.promptReceipt(scenario); raw.length_diagnostics = []; assert.throws(() => adapt(raw, f.preparationId, 8))
  }
  for (const change of [{ mode: 'preview' }, { issues: [] }, { length_diagnostics: [] }]) assert.throws(() => adapt({ ...f.promptReceipt('target'), ...change }, f.preparationId, 8))
  const badUnavailable = f.promptReceipt('unavailable'); badUnavailable.profile_basis = f.promptReceipt().profile_basis
  assert.throws(() => adapt(badUnavailable, f.preparationId, 8))
  const badManual = f.promptReceipt('manual'); badManual.length_diagnostics = f.promptReceipt().length_diagnostics
  assert.throws(() => adapt(badManual, f.preparationId, 8))
  const { state, renderState } = mount(); setReply(() => f.promptReceipt('invalid')); await state.load()
  assert.equal(state.result.value, null); assert.match(state.error.value, /响应不完整/)
  assert.doesNotMatch(await renderState(), /prompt-body|策略合同 SHA256/); assert.equal(calls.length, 1)
})

test('none-mode auxiliary displays saved messages and structured protocol without channels or film dialogue', async t => {
  const { fixture: f, adapt, render, mount, setReply, calls } = await setup(t)
  const raw = f.promptReceipt('auxiliary'), value = adapt(raw, f.preparationId, 8)
  assert.equal(value.promptMode, 'none'); assert.equal(value.contentKind, 'messages')
  assert.deepEqual(value.channels, []); assert.deepEqual(value.fieldBasis, [])
  assert.deepEqual(value.messages.map((row: any) => ({ role: row.role, content: row.content })),
    raw.messages.map((row: any) => ({ role: row.role, content: row.content })))
  const html = await render(value)
  for (const message of value.messages) assert.ok(html.includes(escape(message.content)), 'Preserve whitespace, Unicode and full protocol text')
  assert.ok(html.indexOf('系统消息') < html.indexOf('用户消息'))
  assert.match(html, /冻结文本请求|结构化协议/); assert.match(html, /冻结不等于已发送/)
  assert.doesNotMatch(html, /正向提示词|冻结的原对白|画外音|普通台词|<script>|<textarea|你明确采用/)
  const { input, state } = mount(); input.frozen = true; await nextTick()
  setReply(() => raw); await state.load()
  assert.equal(state.error.value, ''); assert.equal(state.result.value.contentKind, 'messages')
  assert.equal(calls.length, 1); assert.equal(calls[0].method, 'GET')
})

test('none non-chat explicitly reports not applicable; invalid and manual/profile empty channels still fail', async t => {
  const { fixture: f, adapt, render } = await setup(t)
  const value = adapt(f.promptReceipt('none'), f.preparationId, 8), html = await render(value)
  assert.equal(value.contentKind, 'not_applicable'); assert.deepEqual(value.messages, [])
  assert.match(html, /提示词不适用/); assert.doesNotMatch(html, /暂不能生成完整预览|prompt-body|冻结的原对白/)
  const mutations: [(raw: any) => void, string?][] = [
    [r => { delete r.prompt_mode }], [r => { delete r.content_kind }], [r => { delete r.messages }],
    [r => { r.messages = [] }], [r => { r.messages[0].role = 'dialogue' }],
    [r => { r.messages[1].content = {}; r.messages[1].character_count = 0 }],
    [r => { r.messages[1].character_count = r.messages[1].content.length }],
    [r => { r.messages[1].content = ' '; r.messages[1].character_count = 1 }],
    [r => { r.channels = f.promptReceipt('manual').channels }],
    [r => { r.prompt_mode = 'manual' }], [r => { r.prompt_mode = 'profile' }],
    [r => { r.content_kind = 'not_applicable' }],
    [r => { r.mode = 'preview'; r.request_sha256 = null; r.frozen_at = null }],
    [r => { r.field_basis = f.promptReceipt('frozen').field_basis }],
    [r => { r.profile_basis = f.promptReceipt().profile_basis }],
    [r => { r.channels = [] }, 'manual'], [r => { r.channels = [] }, 'frozen'],
    [r => { r.messages = f.promptReceipt('auxiliary').messages }, 'manual'],
    [r => { r.messages = f.promptReceipt('auxiliary').messages }, 'frozen'],
    [r => { r.content_kind = 'messages' }, 'none'],
  ]
  for (const [mutate, scenario] of mutations) {
    const raw = f.promptReceipt(scenario || 'auxiliary'); mutate(raw)
    assert.throws(() => adapt(raw, f.preparationId, 8))
  }
})

test('on-demand reader invalidates old revision, ignores late responses, explicitly fails and recovers without writes', async t => {
  const { fixture: f, mount, setReply, calls } = await setup(t)
  const pending: any[] = []; setReply(() => new Promise((resolve, reject) => pending.push({ resolve, reject })))
  const { input, state, renderState } = mount(); await nextTick(); assert.equal(calls.length, 0)
  const first = state.load(); await flush(); await state.load(); assert.equal(calls.length, 1)
  input.revision = 9; await nextTick(); assert.equal(state.opened.value, false)
  const current = state.load(); await flush()
  pending[1].resolve({ ...f.promptReceipt(), preparation_revision: 9 }); await current
  pending[0].resolve(f.promptReceipt('cumulative')); await first
  assert.equal(state.result.value.preparationRevision, 9); assert.equal(state.result.value.mode, 'preview')
  const failed = state.load(); await flush(); pending[2].reject(Error('source unavailable 原始失败')); await failed
  assert.match(state.error.value, /source unavailable 原始失败/); assert.equal(state.result.value, null)
  const failedHtml = await renderState(); assert.match(failedHtml, /重新读取完整提示词/); assert.doesNotMatch(failedHtml, /prompt-body/)
  input.disabled = true; await state.load(); assert.equal(calls.length, 3)
  input.disabled = false; const retry = state.load(); await flush()
  pending[3].resolve({ ...f.promptReceipt(), preparation_revision: 9 }); await retry
  assert.equal(state.error.value, ''); assert.equal(state.result.value.preparationRevision, 9)
})

test('preparation/frozen/unmount changes discard obsolete result or error and reject frozen-state mismatch', async t => {
  const { fixture: f, mount, setReply } = await setup(t)
  const pending: any[] = []; setReply(() => new Promise((resolve, reject) => pending.push({ resolve, reject })))
  const { input, state, unmount } = mount()
  const first = state.load(); await flush(); input.preparationId = otherId; await nextTick()
  pending[0].reject(Error('old preparation failure')); await first
  assert.equal(state.error.value, ''); assert.equal(state.result.value, null)
  const second = state.load(); await flush(); input.frozen = true; await nextTick()
  pending[1].resolve({ ...f.promptReceipt(), preparation_id: otherId }); await second
  assert.equal(state.result.value, null); assert.equal(state.opened.value, false)
  const mismatch = state.load(); await flush(); pending[2].resolve({ ...f.promptReceipt(), preparation_id: otherId }); await mismatch
  assert.match(state.error.value, /冻结状态已变化/); assert.equal(state.result.value, null)
  const final = state.load(); await flush(); unmount()
  pending[3].resolve({ ...f.promptReceipt('frozen'), preparation_id: otherId }); await final
  assert.equal(state.result.value, null)
})

test('browser fixture refuses writes, undeclared reads, per-request adapters, fetch, XHR and beacon', async t => {
  const { fixture: f, api, client } = await setup(t), events: any[] = []
  class FakeXHR { open(..._args: any[]) {} send() { assert.fail('Native send must not execute') } }
  const saved = ['window', 'XMLHttpRequest', 'navigator'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { dispatchEvent: (event: any) => events.push(event.detail) } })
  Object.defineProperty(globalThis, 'XMLHttpRequest', { configurable: true, value: FakeXHR })
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { sendBeacon() { assert.fail('Native beacon must not execute') } } })
  try {
    const isolation = f.installIsolation(() => 'target')
    assert.equal((await client.get(f.preparationId, 8)).mode, 'diagnostic'); assert.equal(isolation.reads.value, 1)
    await assert.rejects(api.post('/execution-preparations/' + f.preparationId + '/finalize/', {}), /阻断/)
    await assert.rejects(api.get('/current-profile/'), /阻断/)
    await assert.rejects(api.get('/execution-preparations/' + f.preparationId + '/prompt-display/', { params: { expected_preparation_revision: 99 } }), /阻断/)
    await assert.rejects(api.post('/advance/', {}, { adapter: () => assert.fail('Caller adapter must not execute') }), /阻断/)
    await assert.rejects((globalThis as any).window.fetch('/business/'), /阻断/)
    const xhr = new FakeXHR(); xhr.open('POST', '/business/'); assert.throws(() => xhr.send(), /阻断/)
    assert.throws(() => (globalThis as any).navigator.sendBeacon('/business/'), /阻断/)
    assert.equal(isolation.blocked.value, 7); assert.equal(events.filter(event => event.blocked).length, 7)
  } finally {
    for (const [key, descriptor] of saved) if (descriptor) Object.defineProperty(globalThis, key, descriptor); else Reflect.deleteProperty(globalThis, key)
  }
})
