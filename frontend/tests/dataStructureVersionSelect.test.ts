import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createPinia } from 'pinia'

test('structure picker pages the shared catalog and retains exact selection without choosing latest', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { dataStructures: api } = await vite.ssrLoadModule('/src/services/dataStructures.ts')
  const { default: Selector } = await vite.ssrLoadModule('/src/components/DataStructureVersionSelect.vue')
  const version = { id: 'v1', structure_key: 'group1', version: 1, name: '已选结构', status: 'published' }
  const calls: any[] = [], events: unknown[][] = []
  api.list = async (...args: any[]) => { calls.push(args); return { count: 22, results: [{ ...version, id: 'v2', version: 2 }] } }
  api.get = async () => version
  const props = reactive({ modelValue: '', disabled: false })
  let state: any
  await renderToString(createSSRApp({ setup() { state = Selector.setup(props, { expose() {}, emit: (...values: unknown[]) => events.push(values) }); return () => h('div') } }))
  assert.equal(calls.length, 0, 'closed picker must not fetch the author catalog')
  await state.openOptions({ target: { open: true } })
  assert.deepEqual(calls[0], [1, '', 'published'])
  assert.equal(state.count.value, 22)
  assert.deepEqual(events, [], 'catalog load must never change selection')
  props.modelValue = 'v1'; await state.loadSelection()
  assert.equal(state.selected.value.id, 'v1')
  state.page.value = 2; state.search.value = '结构'; await state.load()
  assert.deepEqual(calls.at(-1), [2, '结构', 'published'])
  assert.equal(state.selected.value.id, 'v1', 'paging must not replace an off-page exact selection')
  let resolveOld: (value: any) => void = () => {}
  api.get = () => new Promise(resolve => { resolveOld = resolve })
  const pending = state.loadSelection()
  props.modelValue = ''; await state.loadSelection()
  resolveOld(version); await pending
  assert.equal(state.selected.value, null)
  assert.equal(state.selectionBusy.value, false)
  api.list = async () => { throw new Error('目录断开') }
  await state.load()
  assert.deepEqual(state.rows.value, [])
  assert.match(state.error.value, /目录断开/)
  assert.deepEqual(events, [])
})

test('source selection reads verified detail and cannot emit a broken summary as usable configuration', async context => {
  const storage=globalThis.localStorage
  globalThis.localStorage={getItem:()=>null} as any
  context.after(()=>{globalThis.localStorage=storage})
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { contentSources: api } = await vite.ssrLoadModule('/src/services/contentSources.ts')
  const { default: Manager } = await vite.ssrLoadModule('/src/components/ContentSourceManager.vue')
  const events: unknown[][] = []
  const detail = { id: 'source', source_key:'family',version:1,revision:1,reader:{protocol:'request_text/v1'}, data_structure_version: 'structure-v1', reader_selector: 'request.theme', name: '构想', description: '说明', status: 'published' }
  let state: any
  await renderToString(createSSRApp({ setup() { state = Manager.setup({ choose: true }, { expose() {}, emit: (...values: unknown[]) => events.push(values) }); return () => h('div') } }).use(createPinia()))
  api.get = async () => detail
  await state.openSource({ id: 'source' })
  assert.equal(state.form.data_structure_version, 'structure-v1')
  await state.openSource({ id: 'source' }, true)
  assert.deepEqual(events, [['select', detail]])
  api.get = async () => { throw new Error('来源未配置结构') }
  await state.openSource({ id: 'source' }, true)
  assert.equal(events.length, 1)
  assert.equal(state.selected.value.id, 'source', 'failed detail preserves the previous exact display')
  assert.match(state.error.value, /未配置结构/)
  let resolveOld: (value: any) => void = () => {}
  api.get = () => new Promise(resolve => { resolveOld = resolve })
  const pending = state.openSource({ id: 'source' }, true)
  api.get = async () => { throw new Error('新请求失败') }
  await state.readSource('different-source', true)
  resolveOld(detail); await pending
  assert.equal(events.length, 1, 'late detail may not emit an obsolete selection')
})
