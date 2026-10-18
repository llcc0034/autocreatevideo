import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { adaptExecutionParameterMappings } from '../src/services/executionParameterMappings.ts'

const root = fileURLToPath(new URL('..', import.meta.url))
const response = { version_id: 'version', revision: 2, definition_sha256: 'a'.repeat(64),
  status: 'verified', code: 'workflow_bindings_verified', message: '已核对编译前的声明节点，不代表调用成功。',
  basis_kind: 'workflow', basis_id: 'workflow', basis_sha256: 'b'.repeat(64),
  rows: [{ key: 'image.width', destinations: [{ node_id: '4', field: 'width' }, { node_id: '8', field: 'width' }] }] }
const props = { versionId: 'version', revision: 2, sha256: 'a'.repeat(64) }

test('mapping adapter retains exact one-to-many evidence and rejects incomplete or contradictory responses', () => {
  assert.deepEqual(adaptExecutionParameterMappings(response), response)
  for (const change of [{ revision: 0 }, { definition_sha256: 'bad' }, { rows: null }, { rows: [response.rows[0], response.rows[0]] },
    { status: 'unavailable' }, { basis_id: null }, { basis_kind: 'none', basis_id: null, basis_sha256: null },
    { rows: [{ key: 'x', destinations: [{ node_id: null, field: 'width' }] }] },
    { rows: [{ key: 'x', destinations: [response.rows[0].destinations[0], response.rows[0].destinations[0]] }] }]) {
    assert.throws(() => adaptExecutionParameterMappings({ ...response, ...change }), /映射响应不完整/)
  }
  assert.equal(adaptExecutionParameterMappings({ ...response, status: 'unavailable', rows: [] }).rows.length, 0)
  const api = adaptExecutionParameterMappings({ ...response, basis_kind: 'deployment', rows: [{ key: 'duration_seconds', destinations: [{ node_id: null, field: 'duration' }] }] })
  assert.equal(api.rows[0].destinations[0].field, 'duration')
})

test('mapping panel is lazy, read-only, preserves missing state and escapes destination names', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const Panel = (await vite.ssrLoadModule('/src/components/ExecutionParameterMappings.vue')).default
    const { executionRegistryApi: api } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
    let calls = 0
    api.parameterMappings = async () => { calls++; throw Error('should be lazy') }
    const render = (result: any, expanded = true) => renderToString(createSSRApp({ render: () => h({ ...Panel, setup(p: any, ctx: any) {
      const state = Panel.setup(p, ctx); state.expanded.value = expanded; state.result.value = result; return state
    } }, props) }))
    assert.doesNotMatch(await render(response, false), /image.width|>4</)
    assert.match(await render(null), /尚未查询/)
    const full = await render(response)
    for (const phrase of ['image.width', '>4<', '>8<', '编译前', '不是新配置入口']) assert.ok(full.includes(phrase), phrase)
    assert.doesNotMatch(full, /<input|<textarea|<select/)
    assert.match(await render({ ...response, rows: [{ key: 'unknown', destinations: [] }] }), /未记录此参数的声明目标/)
    const unavailable = await render({ ...response, status: 'unavailable', rows: [], message: '配置已变化，不能补造。' })
    assert.match(unavailable, /配置已变化/); assert.doesNotMatch(unavailable, /明确没有可调/)
    const escaped = await render({ ...response, rows: [{ key: '<script>', destinations: [{ node_id: '0', field: '<img src=x>' }] }] })
    assert.match(escaped, /&lt;script&gt;/); assert.match(escaped, /&lt;img/); assert.doesNotMatch(escaped, /<script>|<img /)
    assert.equal(calls, 0)
  } finally { await vite.close() }
})

test('mapping reads reject wrong identity, discard late results and allow explicit retry without writes', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let app: any
  try {
    const Panel = (await vite.ssrLoadModule('/src/components/ExecutionParameterMappings.vue')).default
    const { executionRegistryApi: api } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
    const { api: transport } = await vite.ssrLoadModule('/src/services/api.ts')
    transport.post = transport.patch = async () => { throw Error('no writes allowed') }
    let calls = 0, resolve: (value: any) => void = () => {}, state: any
    api.parameterMappings = async (id: string, hash: string) => { calls++; assert.equal(id, 'version'); assert.equal(hash, 'a'.repeat(64)); return new Promise(r => { resolve = r }) }
    const current = reactive({ ...props })
    const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    app = renderer.createApp({ setup() { state = Panel.setup(current, { expose() {} }); return () => h('div') } })
    app.provide(ssrContextKey, { modules: new Set() }); app.mount({})
    assert.equal(calls, 0)
    let pending = state.load(); await state.load(); assert.equal(calls, 1)
    resolve({ ...response, definition_sha256: 'c'.repeat(64) }); await pending
    assert.equal(state.result.value, null); assert.match(state.error.value, /合同已变化/)
    pending = state.load(); resolve(structuredClone(response)); await pending
    assert.equal(state.error.value, ''); assert.equal(state.result.value.rows.length, 1)
    pending = state.load(); current.revision = 3; await nextTick(); resolve(response); await pending
    assert.equal(state.result.value, null); assert.equal(state.loading.value, false)
    current.revision = 2; await nextTick()
    pending = state.load(); app.unmount(); app = null; resolve(response); await pending
    assert.equal(state.result.value, null)
  } finally { app?.unmount(); await vite.close() }
})
