import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { createRenderer, reactive, nextTick, ssrContextKey } from 'vue'
const root = fileURLToPath(new URL('..', import.meta.url)), sha = 'a'.repeat(64)
const options = () => ({ preparation_id: 'prep', revision: 3, allowed: true, reason: '只换策略', replacement_id: null,
  versions: [{ id: 'next', version: 6, name: '主图', profile_name: '保留原文策略', profile_version: 4, definition_sha256: sha }] })
const preview = () => ({ preparation_id: 'prep', revision: 3, version_id: 'next', preview_sha256: sha,
  changes: [{ path: '/rules/0/language_policy', before: 'inherit', after: 'preserve_source' }] })
async function modules(fn) {
  const vite = await createServer({ root, envFile: false, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try { await fn(vite) } finally { await vite.close() }
}
function mount(Component, props, emit = (..._args: any[]) => {}) {
  let state: any
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  const app = renderer.createApp({ setup() { state = Component.setup(props, { expose() {}, emit }); return () => null } })
  app.provide(ssrContextKey, {}); app.mount({}); return { state, unmount: () => app.unmount() }
}
test('successor/preview/receipt reject mismatched identity and incomplete response', () => modules(async vite => {
  const api = await vite.ssrLoadModule('/src/services/configurationReplacement.ts')
  assert.equal(api.replacementOptions(options(), 'prep').versions.length, 1)
  for (const patch of [{ preparation_id: 'other' }, { revision: 0 }, { versions: [options().versions[0], options().versions[0]] }, { allowed: null }]) {
    assert.throws(() => api.replacementOptions({ ...options(), ...patch }, 'prep'))
  }
  assert.equal(api.replacementPreview(preview(), 'prep', 'next', 3).preview_sha256, sha)
  assert.throws(() => api.replacementPreview(preview(), 'prep', 'wrong', 3))
  assert.throws(() => api.replacementPreview(preview(), 'prep', 'next', 4))
  assert.throws(() => api.replacementResult({ preparation_id: 'prep', version_id: 'next', replacement_id: 'prep' }, 'prep', 'next'))
}))
test('actual component requires explicit version, preview and confirmation; stale preview resets', () => modules(async vite => {
  const { default: Component } = await vite.ssrLoadModule('/src/components/ExecutionConfigurationReplacement.vue')
  const { executionPreparationApi: api } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
  const before = { ...api }, calls: any[] = [], events: any[] = []
  api.configurationSuccessors = async () => options()
  api.previewConfigurationReplacement = async () => preview()
  api.replaceConfiguration = async (...args) => { calls.push(args); return { preparation_id: 'prep', version_id: 'next', replacement_id: 'replacement' } }
  const props = reactive({ preparationId: 'prep', revision: 3, disabled: false })
  const mounted = mount(Component, props, (...args) => events.push(args)), s = mounted.state
  try {
    await s.read(); assert.equal(s.versionId.value, '')
    await s.apply(); assert.equal(calls.length, 0)
    s.versionId.value = 'next'; await nextTick(); await s.inspect()
    await s.apply(); assert.equal(calls.length, 0)
    s.acknowledged.value = true; props.disabled = true; await s.apply(); assert.equal(calls.length, 0)
    props.disabled = false; await s.apply()
    assert.deepEqual(calls, [['prep', 'next', 3, sha]])
    assert.equal(s.receipt.value.replacement_id, 'replacement')
    assert.ok(events.some(row => row[0] === 'applied'))
    await nextTick(); await s.read(); s.versionId.value = 'next'; await nextTick(); await s.inspect()
    props.revision = 4; await nextTick(); assert.equal(s.preview.value, null); assert.equal(s.versionId.value, '')
  } finally { Object.assign(api, before); mounted.unmount() }
}))
test('late responses and changed preparations do not install stale previews; failures remain visible', () => modules(async vite => {
  const { default: Component } = await vite.ssrLoadModule('/src/components/ExecutionConfigurationReplacement.vue')
  const { executionPreparationApi: api } = await vite.ssrLoadModule('/src/services/executionPreparationApi.ts'), before = { ...api }
  const props = reactive({ preparationId: 'prep', revision: 3, disabled: false }), mounted = mount(Component, props), s = mounted.state
  try {
    let finish: any
    api.configurationSuccessors = () => new Promise(resolve => { finish = resolve })
    const pending = s.read(); props.preparationId = 'other'; await nextTick(); finish(options()); await pending
    assert.equal(s.options.value, null)
    api.configurationSuccessors = async () => { throw Error('原调用仍在途') }
    await s.read(); assert.match(s.error.value, /原调用仍在途/)
  } finally { Object.assign(api, before); mounted.unmount() }
}))
