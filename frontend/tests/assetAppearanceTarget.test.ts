import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'

const root = fileURLToPath(new URL('..', import.meta.url))
const setting = { setting_revision_id: '10000000-0000-0000-0000-000000000001', appearance_id: '20000000-0000-0000-0000-000000000001',
  project_id: '30000000-0000-0000-0000-000000000001', project_title: '项目', entity_name: '人物', name: '主图', reference_name: '主图引用', revision: 1 }
const page = (rows = [setting]) => ({ count: rows.length, next: null, previous: null, results: rows })
async function setup(context: any) {
  const previousStorage = globalThis.localStorage
  globalThis.localStorage = { getItem: () => null } as any
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  let app: any
  context.after(async () => { app?.unmount(); await vite.close(); globalThis.localStorage = previousStorage })
  const module = await vite.ssrLoadModule('/src/services/assetAppearanceTarget.ts')
  const api = (await vite.ssrLoadModule('/src/services/api.ts')).api
  api.defaults.adapter = async () => { throw Error('No real API in isolated component test') }
  const { default: Editor } = await vite.ssrLoadModule('/src/components/AssetAppearanceTargetEditor.vue')
  const props = reactive({ modelValue: null as any, disabled: false, draft: { media: {}, literals: {}, contexts: {}, channels: {} } as any,
    options: { primary_inputs: [], difference_sources: [] } as any }), events: any[] = []
  let state: any
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  app = renderer.createApp({ setup() { state = Editor.setup(props, { emit: (key: string, value: any) => { if (key === 'update:modelValue') { events.push(value); props.modelValue = value } }, expose() {} }); return () => h('div') } })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await nextTick()
  return { module, api, props, state, events, unmount: () => app.unmount() }
}

test('appearance directory is strictly read-only paginated data and never accepts aliases or duplicate revisions', async context => {
  const { module, api } = await setup(context)
  const calls: any[] = []
  api.get = async (...args: any[]) => { calls.push(args); return { data: page() } }
  assert.deepEqual(await module.assetAppearanceApi.options(2, '人物', 'primary'), page())
  assert.deepEqual(calls, [['/asset-generation-jobs/appearance-options/', { params: { page: 2, search: '人物', role: 'primary' } }]])
  for (const invalid of [null, {}, { ...page(), count: -1 }, page([{ ...setting, revision: '1' } as any]),
    page([{ ...setting, setting_revision_id: 'latest' }]), page([setting, setting]), page([{ ...setting, name: [] } as any])]) {
    assert.throws(() => module.readAppearancePage(invalid), /目录不完整/)
  }
  assert.deepEqual(module.primaryTarget(null), { role: 'primary', setting_revision_id: null, primary_version_id: null, primary_input: null })
})

test('primary selection is opt-in, preserves exact revision across refresh and supports explicit removal', async context => {
  const { state, props, module } = await setup(context)
  let reads = 0
  module.assetAppearanceApi.options = async () => { reads++; return page() }
  await state.load(); assert.equal(reads, 0)
  state.changeRole('primary'); await nextTick(); assert.equal(reads, 0)
  assert.equal(props.modelValue.setting_revision_id, null)
  await state.load(); assert.equal(props.modelValue.setting_revision_id, null)
  state.choose(state.directory.value.results[0]); await nextTick()
  const original = props.modelValue
  module.assetAppearanceApi.options = async () => page([{ ...setting, revision: 2, setting_revision_id: '10000000-0000-0000-0000-000000000002' }])
  await state.load()
  assert.equal(props.modelValue, original); assert.equal(state.selected.value.revision, 1)
  state.choose(null); assert.equal(props.modelValue.setting_revision_id, null)
  state.changeRole(''); await nextTick()
  assert.equal(props.modelValue, null); assert.equal(state.directory.value, null)
})

test('failed refresh leaves a stale directory explicitly unavailable until retry', async context => {
  const { state, props, module } = await setup(context)
  state.changeRole('primary'); await nextTick()
  module.assetAppearanceApi.options = async () => page()
  await state.load(); const old = state.directory.value
  module.assetAppearanceApi.options = async () => { throw Error('读取失败') }
  await state.load(); assert.match(state.error.value, /读取失败/)
  assert.equal(state.directory.value, old)
  state.choose(old.results[0]); assert.equal(props.modelValue.setting_revision_id, null)
  module.assetAppearanceApi.options = async () => page()
  await state.load(); assert.equal(state.error.value, '')
  state.choose(state.directory.value.results[0]); assert.equal(props.modelValue.setting_revision_id, setting.setting_revision_id)
})

test('locked submissions block changes and out-of-order reads cannot replace the newest page', async context => {
  const { state, props, module, events } = await setup(context)
  state.changeRole('primary'); await nextTick()
  const completions: any[] = []
  module.assetAppearanceApi.options = () => new Promise(resolve => completions.push(resolve))
  const first = state.load(1), second = state.load(2)
  completions[1]({ ...page(), count: 2, previous: '?page=1' }); await second
  completions[0](page([])); await first
  assert.equal(state.page.value, 2); assert.equal(state.directory.value.count, 2)
  props.disabled = true
  const count = events.length
  state.choose(state.directory.value.results[0]); state.changeRole(''); await state.load()
  assert.equal(events.length, count); assert.equal(completions.length, 2)
})

test('turning off or unmounting invalidates late directory responses', async context => {
  const { state, props, module, unmount } = await setup(context)
  let finish: any
  module.assetAppearanceApi.options = () => new Promise(resolve => { finish = resolve })
  state.changeRole('primary'); await nextTick()
  const off = state.load(); state.changeRole(''); await nextTick()
  finish(page()); await off
  assert.equal(state.directory.value, null); assert.equal(props.modelValue, null)
  state.changeRole('primary'); await nextTick()
  const late = state.load(); unmount(); finish(page()); await late
  assert.equal(state.directory.value, null)
})

test('variant editor selects exact primary and original inputs, keeps body out and invalidates role directory', async context => {
  const { state, props, module } = await setup(context)
  state.changeRole('variant'); await nextTick()
  const roles: string[] = []
  module.assetAppearanceApi.options = async (_page: number, _search: string, role: string) => { roles.push(role); return page() }
  await state.load(); assert.deepEqual(roles, ['variant'])
  const asset = { id: '10000000-0000-0000-0000-000000000009' }
  const reference = { series: { id: 'series', role: 'primary', name: '主图' }, version: { id: '10000000-0000-0000-0000-000000000008', series_id: 'series', asset } }
  state.choosePrimary(asset, { ...reference, series: { ...reference.series, role: 'variant' } })
  assert.equal(props.modelValue.primary_version_id, '')
  state.choosePrimary(asset, reference); await nextTick()
  assert.equal(props.modelValue.primary_version_id, reference.version.id)
  props.options = { primary_inputs: [{ input_key: 'image', target_key: 'real_port' }], difference_sources: [{ kind: 'manual_channel', key: 'positive' }] }
  props.draft.media.image = [{ itemKey: 'one', assetId: asset.id }]; props.draft.channels.positive = '蓝色外套'
  state.chooseInput(0); state.chooseDifference(0); await nextTick()
  assert.deepEqual(module.appearanceTargetErrors(props.modelValue, props.draft, props.options, asset.id), [])
  assert.equal(JSON.stringify(props.modelValue).includes('蓝色外套'), false)
  props.draft.media.image[0].assetId = 'other'
  assert.match(module.appearanceTargetErrors(props.modelValue, props.draft, props.options, asset.id).join(), /同的素材项/)
  props.disabled = true; const fixed = props.modelValue
  state.chooseInput(-1); state.chooseDifference(-1); state.choosePrimary(asset, reference)
  assert.equal(props.modelValue, fixed)
  props.disabled = false; state.changeRole('primary'); await nextTick()
  assert.equal(state.directory.value, null); assert.equal(state.primary.value, null)
})

test('difference choices validate explicit manual text and exact original reference units without defaults', async context => {
  const { module, props } = await setup(context)
  const target = module.variantTarget(), draft = props.draft
  const options = { primary_inputs: [{input_key:'image',target_key:'slot'}], difference_sources: [
    {kind:'manual_channel',key:'positive'}, {kind:'reference_field',key:'reference',field_key:'detail'},
    {kind:'structured_field',key:'facts',field_key:'change'}, {kind:'logical_input',key:'text'}] }
  target.primary_version_id = '10000000-0000-0000-0000-000000000008'; target.primary_input = {input_key:'image',item_key:'one'}
  draft.media.image = [{itemKey:'one',assetId:'asset'}]
  target.difference_input = {kind:'manual_channel',key:'positive'}
  assert.match(module.appearanceTargetErrors(target,draft,options,'asset').join(), /差异正文/)
  target.difference_input = {kind:'reference_field',key:'reference',field_key:'detail',unit_key:'second'}
  draft.literals.reference = {enabled:true,value:{ordered_units:[{unit_key:'first'},{unit_key:'second'}]}}
  assert.deepEqual(module.appearanceTargetErrors(target,draft,options,'asset'), [])
  draft.literals.reference.value.ordered_units[0].unit_key = ' second '
  assert.deepEqual(module.referenceUnitKeys(draft,'reference'), [' second ', 'second'])
  target.difference_input.unit_key = ' second '
  assert.deepEqual(module.appearanceTargetErrors(target,draft,options,'asset'), [])
  target.difference_input.unit_key = ' second  '
  assert.match(module.appearanceTargetErrors(target,draft,options,'asset').join(), /确切单元/)
  target.difference_input.unit_key = 'second'
  draft.literals.reference.value.ordered_units.pop()
  assert.match(module.appearanceTargetErrors(target,draft,options,'asset').join(), /确切单元/)
  draft.contexts.reference = {selectorKey:'context'}
  assert.deepEqual(module.referenceUnitKeys(draft,'reference'), [])
  for (const kind of ['structured_field','logical_input']) {
    target.difference_input = kind === 'structured_field' ? {kind,key:'facts',field_key:'change'} : {kind,key:'text'}
    assert.match(module.appearanceTargetErrors(target,draft,options,'asset').join(), /原输入/)
  }
  target.difference_input = {kind:'manual_channel',key:'unknown'}
  assert.match(module.appearanceTargetErrors(target,draft,options,'asset').join(), /声明的原差异/)
})

test('reference unit selection preserves distinct source identities and refuses missing or locked changes', async context => {
  const { state, props } = await setup(context)
  props.options.difference_sources = [{ kind: 'reference_field', key: 'units', field_key: 'detail' }]
  props.draft.literals.units = { enabled: true, value: { ordered_units: [{ unit_key: 'second' }, { unit_key: ' second ' }] } }
  state.changeRole('variant'); await nextTick()
  state.chooseDifference(0); await nextTick()
  state.chooseUnit(' second '); await nextTick()
  assert.equal(props.modelValue.difference_input.unit_key, ' second ')
  const fixed = props.modelValue
  state.chooseUnit(' second  '); assert.equal(props.modelValue, fixed)
  props.disabled = true; state.chooseUnit('second'); assert.equal(props.modelValue, fixed)
  props.disabled = false; state.chooseUnit('second'); await nextTick()
  assert.equal(props.modelValue.difference_input.unit_key, 'second')
  state.chooseUnit(''); assert.equal(props.modelValue.difference_input.unit_key, '')
})
