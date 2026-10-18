import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h, nextTick, reactive } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createPinia } from 'pinia'

const defaults = {parent_key: null, field_definition_version: null, property_name: '', position: 0, required: false, allow_null: false, allow_blank: true, allow_empty: true, has_default: false, default_value: null, constraints: {}}
const options = {node_defaults: defaults, statuses: [{value: 'draft', label: '草稿'}, {value: 'published', label: '可用'}], constraint_schemas: {object: {type: 'object', properties: {}, additionalProperties: false}}, value_schemas: {object: {type: 'object'}}}
const first = {id: 's1', structure_key: 'stable', version: 1, name: '任意输入结构', description: '', status: 'published', nodes: [{...defaults, node_key: 'root', field_definition_version: 'f1'}]}
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))

test('structure center saves exact drafts, publishes visible edits and keeps failed input without implicit writes', async context => {
  const storage=globalThis.localStorage
  globalThis.localStorage={getItem:()=>null} as any
  context.after(()=>{globalThis.localStorage=storage})
  const vite = await createServer({server: {middlewareMode: true, hmr: false}, appType: 'custom', optimizeDeps: {noDiscovery: true}})
  context.after(() => vite.close())
  const {dataStructures: api} = await vite.ssrLoadModule('/src/services/dataStructures.ts')
  const {default: Component} = await vite.ssrLoadModule('/src/components/DataStructureManager.vue')
  const writes: any[] = [], reads: any[] = []
  let stored = clone(first)
  api.options = async () => options
  api.list = async (...args: any[]) => { reads.push(args); return {count: 21, results: [stored],hasNext:true,hasPrevious:false} }
  api.get = async () => clone(stored)
  api.create = async (content: any, source: string) => { writes.push(['create', clone(content), source]); stored = {...stored, ...clone(content), id: 's2', version: 2, status: 'draft'}; return clone(stored) }
  api.update = async (id: string, patch: any) => { writes.push(['update', id, clone(patch)]); stored = {...stored, ...clone(patch)}; return clone(stored) }
  api.transition = async (id: string, action: string) => { writes.push(['transition', id, action]); stored.status = action === 'publish' ? 'published' : 'retired'; return clone(stored) }
  api.schema = async () => ({valid: true, json_schema: {type: 'object'}, errors: {}})
  let state: any
  await renderToString(createSSRApp({setup() {state = Component.setup({}, {expose() {}, emit() {}}); return () => h('div')}}).use(createPinia()))
  await state.load(); assert.deepEqual(reads.at(-1), [1, '', ''])
  await state.open('s1'); assert.equal(state.readonly.value, true)
  await state.save(); assert.equal(writes.length, 0)
  state.begin(true); assert.equal(state.cloneSource.value, 's1')
  state.form.name = '第二种布局'; state.add()
  state.draftErrors.value = ['未完成数字']; state.removeIndex.value = 1; state.removeNode()
  assert.equal(state.form.nodes.length, 2, 'unfinished local input cannot disappear by removing another node')
  state.draftErrors.value = []
  state.updateNode({...state.node.value, node_key: 'motion', parent_key: 'root', property_name: 'action', field_definition_version: 'f2', has_default: true, default_value: false})
  await state.save()
  assert.equal(writes[0][2], 's1'); assert.deepEqual(Object.keys(writes[0][1]), ['name', 'description', 'nodes'])
  assert.equal(writes[0][1].nodes[1].default_value, false)
  await state.save(); assert.equal(writes.length, 1, 'unchanged draft causes no write')
  state.form.name = '启用前修改'; await state.transition('publish')
  assert.deepEqual(writes.slice(-2).map(row => row[0]), ['update', 'transition'])
  assert.deepEqual(writes.at(-2)[2], {name: '启用前修改'})
  assert.equal(state.readonly.value, true)
  state.begin(true); state.form.description = '失败后保留'
  api.create = async () => { throw {response:{status:400,data:{error:{code:'invalid',message:'隔离保存失败'}}}} }
  assert.equal(await state.save(), false)
  assert.equal(state.form.description, '失败后保留'); assert.equal(state.cloneSource.value, 's2')
  assert.match(state.error.value, /隔离保存失败/)
  state.nodeIndex.value = 0; state.updateNode({...state.node.value, node_key: 'renamed'})
  assert.equal(state.form.nodes[1].parent_key, 'renamed')
  state.removeIndex.value = 0; state.removeNode(); assert.equal(state.form.nodes.length, 2)
  assert.match(state.error.value, /子节点/)
  state.removeIndex.value = 1; state.removeNode(); assert.equal(state.form.nodes.length, 1)
  api.list = async () => { throw Error('列表失败') }; await state.load()
  assert.match(state.listError.value, /列表失败/); assert.equal(state.count.value, 21)
})

test('node constraints derive from options and preserve null, false and selected version', async context => {
  const vite = await createServer({server: {middlewareMode: true, hmr: false}, appType: 'custom', optimizeDeps: {noDiscovery: true}})
  context.after(() => vite.close())
  const {default: Component} = await vite.ssrLoadModule('/src/components/DataStructureNodeEditor.vue')
  const events: any[] = [], props = reactive({node: {...defaults, node_key: 'root'}, nodes: [{...defaults, node_key: 'root'}, {...defaults, node_key: 'child', parent_key: 'root'}], options, disabled: false})
  let state: any
  await renderToString(createSSRApp({setup() {state = Component.setup(props, {expose() {}, emit: (...args: any[]) => events.push(args)}); return () => h('div')}}))
  state.definition.value = {value_type: 'object'}
  assert.deepEqual(state.constraintSchema.value, options.constraint_schemas.object, 'reactive options are cloned safely')
  assert.equal(state.parents.value.length, 0, 'self and descendants cannot be parent options')
  state.toggleDefault(true); assert.equal(events.at(-1)[1].default_value, null)
  state.change({default_value: false}); assert.equal(events.at(-1)[1].default_value, false)
  const beforePosition = events.filter(row => row[0] === 'change').length
  for (const raw of ['', '-1', '0.5', 'NaN']) state.updatePosition(raw)
  assert.equal(events.filter(row => row[0] === 'change').length, beforePosition, 'invalid numeric draft does not silently become zero')
  assert.match(events.at(-1)[1][0], /非负整数/)
  state.updatePosition('0'); assert.equal(events.at(-1)[1].position, 0)
  assert.deepEqual(events.at(-2), ['draftValidity', []])
  props.disabled = true; await nextTick(); const count = events.length; state.change({node_key: 'no'}); assert.equal(events.length, count)
})

test('global field picker uses paged exact versions, retains off-page selection and ignores late replies', async context => {
  const vite = await createServer({server: {middlewareMode: true, hmr: false}, appType: 'custom', optimizeDeps: {noDiscovery: true}})
  context.after(() => vite.close())
  const {fieldDefinitionVersions: api} = await vite.ssrLoadModule('/src/services/fieldDefinitionVersions.ts')
  const {default: Component} = await vite.ssrLoadModule('/src/components/FieldDefinitionVersionSelect.vue')
  const row = {id: 'f1', field_definition: 'identity', label: '集合', version: 1, status: 'published'}
  const calls: any[] = [], events: any[] = []
  api.catalog = async (...args: any[]) => { calls.push(args); return {count: 25, results: []} }
  api.get = async () => row
  let state: any
  await renderToString(createSSRApp({setup() {state = Component.setup({global: true, modelValue: 'f1'}, {expose() {}, emit: (...args: any[]) => events.push(args)}); return () => h('div')}}))
  await state.load(); assert.equal(state.selectedVersion.value.id, 'f1')
  state.page.value = 2; state.search.value = '集合'; await state.load()
  assert.deepEqual(calls.at(-1), [2, '集合'])
  assert.equal(events.some(row => row[0] === 'update:modelValue'), false)
  let release!: (value: any) => void
  api.catalog = () => new Promise(resolve => {release = resolve})
  const pending = state.load()
  api.catalog = async () => {throw Error('最新目录失败')}; await state.load()
  release({count: 1, results: [row]}); await pending
  assert.match(state.error.value, /最新目录失败/); assert.equal(state.rows.value.length, 0)
})
