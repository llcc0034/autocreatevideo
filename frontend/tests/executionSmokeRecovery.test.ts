import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { unusedLanguageForm } from './fixtures/executionLanguage.ts'

const raw = (version = 'version') => ({ version_id: version, revision: 1, frozen: true, sha256: 'a'.repeat(64),
  context_fields: [], input_forms: {}, subject_categories: [{value:'character',label:'人物'},{value:'scene',label:'场景'},{value:'prop',label:'道具'}], language_form: unusedLanguageForm(),
  definition: { version_id: version, configuration: { deployment_id: 'deployment', operation: 'script.generate', mode: '',
    workflow_version_id: null, prompt_mode: 'manual', prompt_profile_version_id: null, inputs: [], parameters: [], auxiliaries: [] },
    interface: { output: { kind: 'text' }, input_purposes: {}, parameters: {}, prompt: { mode: 'manual', channels: { prompt: { rule: { type: 'string', required: true } } } } } } })
async function setup(t: any, resource = false) {
  const previousStorage = globalThis.localStorage
  let user = 'user'
  globalThis.localStorage = { getItem: key => key === 'cineforge_user' ? JSON.stringify({ id: user }) : null } as any
  const vite = await createServer({ logLevel: 'silent', server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  const { executionRegistryApi: registry } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const { resourceSmokeApi: resources } = await vite.ssrLoadModule('/src/services/executionResourceSmoke.ts')
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  registry.contract = async id => raw(id)
  resources.choices = async () => ({ target: { operations: [{ value: 'script.generate' }], modes: [], model_family: 'text' }, prompt_modes: [{ value: 'manual' }] })
  platformApi.listPromptProfileVersions = async () => []
  const { default: Form } = await vite.ssrLoadModule(`/src/components/${resource ? 'ResourceSmokeCreateForm' : 'ExecutionSmokeCreateForm'}.vue`)
  const props: any = reactive(resource ? { target: { kind: 'deployment', id: 'deployment' } } : { versionId: 'version' })
  const events: any[] = []; let state: any
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  const app = renderer.createApp({ setup() { state = Form.setup(props, { emit: (...args: any[]) => events.push(args), expose() {} }); return () => h('div') } })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await nextTick(); await nextTick()
  let mounted = true
  const unmount = () => { if (mounted) { mounted = false; app.unmount() } }
  t.after(async () => { unmount(); await vite.close(); globalThis.localStorage = previousStorage })
  return { state, props, registry, resources, events, unmount, setUser: value => { user = value } }
}
async function edit(state: any) {
  assert.equal(state.readState.value, 'ready', state.error.value)
  state.draft.value.channels.prompt = '原始测试输入'; state.allowExternal.value = true
  await nextTick(); state.reviewed.value = true
}

test('reload confirmation cancels without reads; failed read retains inputs and blocks creation, recovery resets only on success', async t => {
  const { state, registry, events } = await setup(t)
  const emptyDraft = JSON.stringify(state.draft.value)
  await edit(state)
  const original = JSON.stringify(state.draft.value), generation = state.formGeneration.value
  let reads = 0, creates = 0
  registry.contract = async () => { reads++; throw Error('合同读取失败') }
  registry.smoke = async () => { creates++ }
  await state.load(); assert.ok(state.reloadDiscard.value); assert.equal(reads, 0)
  assert.deepEqual(events.at(-1), ['locked', true])
  state.reloadDiscard.value = null
  assert.equal(JSON.stringify(state.draft.value), original); assert.equal(state.reviewed.value, true)
  await state.load(); await state.confirmReloadDiscard()
  assert.equal(reads, 1); assert.equal(JSON.stringify(state.draft.value), original)
  assert.equal(state.allowExternal.value, true); assert.equal(state.formGeneration.value, generation)
  assert.equal(state.readState.value, 'error'); assert.equal(state.reviewed.value, false)
  await state.submit(); assert.equal(creates, 0)
  registry.contract = async () => { reads++; return raw() }
  await state.load(); await state.confirmReloadDiscard()
  assert.equal(reads, 2); assert.equal(state.readState.value, 'ready')
  assert.equal(state.allowExternal.value, false); assert.equal(state.reviewed.value, false)
  assert.equal(JSON.stringify(state.draft.value), emptyDraft); assert.equal(state.formGeneration.value, generation + 1)
})

for (const invalidation of ['draft', 'identity', 'busy', 'unmount']) test(`stale reload confirmation cannot read after ${invalidation}`, async t => {
  const { state, registry, setUser, unmount } = await setup(t); await edit(state)
  let reads = 0; registry.contract = async () => { reads++; return raw() }
  await state.load(); assert.ok(state.reloadDiscard.value)
  if (invalidation === 'draft') state.draft.value.channels.prompt = '新草稿'
  if (invalidation === 'identity') setUser('other')
  if (invalidation === 'busy') state.uploading.value = true
  if (invalidation === 'unmount') unmount()
  await state.confirmReloadDiscard(); assert.equal(reads, 0)
})

test('initial required-field errors are not edits; typed but invalid input is protected', async t => {
  const { state } = await setup(t)
  state.inputErrors.value = ['必填数字尚未填写']; await nextTick(); assert.equal(state.dirty.value, false)
  state.inputTouched.value = true; await nextTick(); assert.equal(state.dirty.value, true)
  await state.load(); assert.ok(state.reloadDiscard.value)
})

test('pending smoke keeps the exact key and body through rejected retries, and forbids reread', async t => {
  const { state, registry, events } = await setup(t); await edit(state)
  let failure: any = Error('提交响应丢失'), reads = 0; const calls: any[] = []
  registry.contract = async () => { reads++; return raw() }
  registry.smoke = async (...args) => { calls.push(JSON.parse(JSON.stringify(args))); if (failure) throw failure; return { preparation: { id: 'prep', executionInstanceVersion: 'version' } } }
  await state.submit(); const pending = state.pending.value, snapshot = JSON.stringify(pending)
  assert.ok(pending); assert.deepEqual(events.find(e => e[0] === 'locked'), ['locked', true])
  await state.load(); assert.equal(reads, 0)
  for (const status of [400, 401, 403, 404, 408, 409, 422, 429, 500]) {
    failure = { response: { status } }; await state.submit()
    assert.equal(state.pending.value, pending); assert.equal(JSON.stringify(pending), snapshot)
  }
  failure = null; await state.submit()
  assert.equal(state.pending.value, null); assert.equal(events.filter(e => e[0] === 'created').length, 1)
  calls.forEach(call => assert.deepEqual(call, calls[0]))
})

for (const status of [400, 401, 403, 404, 422, 408, 409, 429, 500]) test(`first smoke response ${status} has explicit recovery semantics`, async t => {
  const { state, registry } = await setup(t); await edit(state)
  registry.smoke = async () => { throw { response: { status } } }
  await state.submit(); assert.equal(!!state.pending.value, [408, 409, 429, 500].includes(status))
})

test('pending smoke cannot be retried against another target or identity and resumes only its original target', async t => {
  const { state, registry, props, setUser } = await setup(t); await edit(state)
  let creates = 0; registry.smoke = async () => { creates++; throw Error('未知结果') }
  await state.submit(); const key = state.pending.value.key
  props.versionId = 'other'; await nextTick(); await state.submit(); assert.equal(creates, 1)
  props.versionId = 'version'; await nextTick(); await state.submit(); assert.equal(creates, 2)
  setUser('other'); await state.submit(); assert.equal(creates, 2)
  assert.equal(state.pending.value.key, key)
})

test('base-resource submission preserves its null-instance context and exact retry after an uncertain result', async t => {
  const { state, props, resources, registry } = await setup(t)
  const { frozen, revision, ...contract } = raw(null as any)
  resources.contract = async () => ({ ...contract, scope: 'resource_smoke' })
  props.versionId = undefined; props.resource = { target: { kind: 'deployment', id: 'deployment' }, selection: {
    operation: 'script.generate', mode: '', prompt_mode: 'manual', prompt_profile_version_id: null } }
  await nextTick(); await nextTick(); await edit(state)
  const calls: any[] = []
  registry.smoke = async () => assert.fail('base resource cannot submit an instance request')
  resources.create = async (...args) => { calls.push(JSON.parse(JSON.stringify(args))); throw Error('未知') }
  await state.submit(); assert.equal(state.pending.value.versionId, null)
  await state.submit(); assert.deepEqual(calls[1], calls[0]); assert.deepEqual(calls[0][0], props.resource)
})

for (const invalidation of ['identity', 'unmount']) for (const result of ['success', 'failure']) test(`late smoke ${result} ignored after ${invalidation}`, async t => {
  const { state, registry, setUser, unmount, events } = await setup(t); await edit(state)
  let finish: any, fail: any, creates = 0
  registry.smoke = () => { creates++; return new Promise((resolve, reject) => { finish = resolve; fail = reject }) }
  const running = state.submit(); await state.submit(); assert.equal(creates, 1)
  if (invalidation === 'identity') setUser('other'); else unmount()
  if (result === 'success') finish({ preparation: { id: 'prep', executionInstanceVersion: 'version' } }); else fail({ response: { status: 400 } })
  await running; assert.ok(state.pending.value); assert.equal(events.some(e => e[0] === 'created'), false)
})

for (const invalidation of ['identity', 'unmount', 'target']) test(`late contract read ignored after ${invalidation}`, async t => {
  const { state, registry, props, setUser, unmount } = await setup(t)
  const original = state.draft.value
  let finish: any
  registry.contract = () => new Promise(resolve => { finish = resolve })
  const running = state.load(); const first = finish
  if (invalidation === 'identity') setUser('other')
  if (invalidation === 'unmount') unmount()
  if (invalidation === 'target') { props.versionId = 'other'; await nextTick() }
  first(raw()); await running; assert.equal(state.draft.value, original)
  if (invalidation === 'target') { finish(raw('other')); await nextTick(); assert.equal(state.contract.value.versionId, 'other') }
})

async function chooseResource(state: any) {
  state.operation.value = 'script.generate'; state.promptMode.value = 'manual'; state.choose(); await nextTick()
}
test('resource selection discard is explicit, retains selector values and honors child lock', async t => {
  const { state, events } = await setup(t, true); await chooseResource(state)
  const original = state.context.value; state.childDirty.value = true
  state.changeSelection(); assert.ok(state.selectionDiscard.value); assert.equal(state.context.value, original)
  state.selectionDiscard.value = null; assert.equal(state.context.value, original)
  state.childLocked.value = true; state.changeSelection(); assert.equal(state.selectionDiscard.value, null)
  assert.deepEqual(events.at(-1), ['locked', true])
  state.childLocked.value = false; state.changeSelection(); state.confirmSelectionDiscard()
  assert.equal(state.context.value, null); assert.equal(state.operation.value, 'script.generate'); assert.equal(state.promptMode.value, 'manual')
})
for (const invalidation of ['target', 'identity', 'busy', 'lock', 'unmount']) test(`stale resource discard ignored after ${invalidation}`, async t => {
  const { state, props, setUser, unmount } = await setup(t, true); await chooseResource(state)
  const original = state.context.value; state.childDirty.value = true; state.changeSelection()
  if (invalidation === 'target') props.target.id = 'other'
  if (invalidation === 'identity') setUser('other')
  if (invalidation === 'busy') state.childBusy.value = true
  if (invalidation === 'lock') state.childLocked.value = true
  if (invalidation === 'unmount') unmount()
  state.confirmSelectionDiscard(); assert.equal(state.context.value, original)
})

test('both resource modal owners and the instance evidence panel propagate the unresolved-request lock', () => {
  for (const path of ['views/SettingsView.vue', 'views/WorkflowVersionEditorView.vue']) {
    const source = readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')
    assert.match(source, /@locked="resourceSmokeLocked=\$event"/)
    assert.match(source, /:dismissible="!resourceSmokeBusy&&!resourceSmokeLocked/)
    assert.match(source, /resourceSmokeBusy.value \|\| resourceSmokeLocked.value/)
  }
  const source = readFileSync(new URL('../src/components/ExecutionSmokeEvidence.vue', import.meta.url), 'utf8')
  assert.match(source, /@locked="formLocked=\$event"/)
  assert.match(source, /watch\(\(\)=>busy.value\|\|formLocked.value/)
})
