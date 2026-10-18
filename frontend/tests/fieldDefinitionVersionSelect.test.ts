import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h, reactive } from 'vue'
import { readFile } from 'node:fs/promises'
import { renderToString } from 'vue/server-renderer'

test('field version picker never chooses latest, reads exact details and ignores stale responses', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { fieldDefinitionVersions: api } = await vite.ssrLoadModule('/src/services/fieldDefinitionVersions.ts')
  const { default: Selector } = await vite.ssrLoadModule('/src/components/FieldDefinitionVersionSelect.vue')
  const version = { id: 'v1', field_definition: 'f1', version: 1, label: '原名称', status: 'published', value_type: 'text' }
  const newer = { ...version, id: 'v2', version: 2, label: '新版', status: 'draft' }
  const calls: string[] = [], events: unknown[][] = []
  api.list = async (id: string) => { calls.push(id); return [newer, version] }
  api.get = async (id: string) => { calls.push(id); return version }
  const props = reactive({ fieldDefinition: 'f1', modelValue: '' })
  let state: any
  await renderToString(createSSRApp({ setup() { state = Selector.setup(props, { expose() {}, emit: (...values: unknown[]) => events.push(values) }); return () => h('div') } }))
  await state.load()
  assert.deepEqual(state.rows.value.map((row: any) => row.id), ['v1'])
  assert.equal(events.some(event => event[0] === 'update:modelValue'), false)
  assert.equal(calls.includes('v1'), false)
  props.modelValue = 'v1'
  await state.load()
  assert.equal(events.at(-1)?.[1], version)
  let resolveOld: (value: any) => void = () => {}
  api.get = () => new Promise(resolve => { resolveOld = resolve })
  const pending = state.load()
  props.fieldDefinition = ''
  await state.load()
  resolveOld(version)
  await pending
  assert.deepEqual(state.rows.value, [])
  assert.equal(state.busy.value, false)
  assert.equal(events.at(-1)?.[1], null)
  props.fieldDefinition = 'other'
  api.get = async () => version
  await state.load()
  assert.match(state.error.value, /不属于/)
})

test('manual directory refresh exposes a published successor without changing the unsaved exact reference', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { fieldDefinitionVersions: api } = await vite.ssrLoadModule('/src/services/fieldDefinitionVersions.ts')
  const { default: Selector } = await vite.ssrLoadModule('/src/components/FieldDefinitionVersionSelect.vue')
  const original = { id:'old', field_definition:'field', version:1, label:'原字段', status:'published', value_type:'text' }
  const successor = { ...original, id:'new', version:2, label:'新字段' }
  let catalog = [original], fail = false, state:any
  const events:any[] = [], props = reactive({fieldDefinition:'field',modelValue:'old'})
  api.list = async()=>{if(fail)throw Error('目录不可读');return catalog}
  api.get = async()=>original
  await renderToString(createSSRApp({setup(){state=Selector.setup(props,{expose(){},emit:(...args:any[])=>events.push(args)});return()=>h('div')}}))
  await state.load();catalog=[original,successor];await state.load()
  assert.deepEqual(state.rows.value.map((row:any)=>row.id),['old','new'])
  assert.equal(state.selectedVersion.value.id,'old');assert.equal(props.modelValue,'old')
  fail=true;await state.load();assert.match(state.error.value,/目录不可读/);assert.equal(props.modelValue,'old')
  fail=false;await state.load();assert.equal(state.error.value,'');assert.equal(state.selectedVersion.value.id,'old')
  assert.equal(events.some(event=>event[0]==='update:modelValue'),false)
})

test('local refresh remains available after successful loading and respects author busy/disabled state',async()=>{
  const source=await readFile(new URL('../src/components/FieldDefinitionVersionSelect.vue',import.meta.url),'utf8')
  assert.match(source,/<button v-if="!global"[^>]+:disabled="disabled \|\| busy \|\| !fieldDefinition"[^>]+@click="load">[\s\S]*?刷新字段版本目录/)
  assert.doesNotMatch(source,/<button v-if="error"[^>]+刷新字段版本目录/)
})
