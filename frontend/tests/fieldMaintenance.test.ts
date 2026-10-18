import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { readFileSync } from 'node:fs'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory, RouterView } from 'vue-router'

test('field page HTTP adapter preserves pagination for real DRF response envelopes', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const page = {count: 25, results: [{id: 'identity', key: 'motion', is_active: true, display_order: 1}], next: 'page2', previous: null}
  for (const payload of [page, {data: page}]) {
    api.get = async (url, options) => {
      assert.equal(url, '/field-definitions/')
      assert.deepEqual(options.params, {page: 1, page_size: 20, search: 'motion', is_active: true})
      return {data: payload}
    }
    const result = await platformApi.fieldDefinitionPage(1, 'motion', false)
    assert.equal(result.count, 25); assert.equal(result.results[0].key, 'motion'); assert.equal(result.hasNext,true); assert.equal(result.hasPrevious,false)
  }
  api.get = async () => ({data: page.results})
  await assert.rejects(platformApi.fieldDefinitionPage(1, '', true), /响应不完整/)
  api.get = async () => ({data: {...page, count:15, next:'page2'}})
  assert.equal((await platformApi.fieldDefinitionPage(1,'motion',true)).hasNext,true, 'server next is authoritative even when count is below requested page size')
  api.get = async () => ({data: {...page, next:undefined}})
  await assert.rejects(platformApi.fieldDefinitionPage(1,'motion',true), /响应不完整/)
})

test('raw identity response cannot convert missing or non-boolean state into inactive',async context=>{
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',logLevel:'silent'})
  context.after(()=>vite.close())
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const {api}=await vite.ssrLoadModule('/src/services/api.ts')
  for(const invalid of [undefined,null,0,'false']){
    const raw={id:'f1',key:'motion',is_active:invalid,display_order:1}
    api.get=api.post=api.patch=async()=>({data:raw})
    await assert.rejects(platformApi.getFieldDefinition('f1'),/有效启停状态/)
    await assert.rejects(platformApi.createFieldDefinition({key:'motion'}),/有效启停状态/)
    await assert.rejects(platformApi.updateFieldDefinition('f1',{is_active:false}),/有效启停状态/)
  }
  api.get=async()=>({data:{id:'f1',key:'motion',is_active:false,display_order:1}})
  assert.equal((await platformApi.getFieldDefinition('f1')).isActive,false)
})

test('one field center paginates, preserves identity, reports failures and links to the shared hierarchy', async context => {
  const storage=globalThis.localStorage;globalThis.localStorage={getItem:()=>null} as any
  context.after(()=>{globalThis.localStorage=storage})
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { platformApi: api } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const { api: http } = await vite.ssrLoadModule('/src/services/api.ts')
  const { default: Component } = await vite.ssrLoadModule('/src/components/FieldMaintenanceSettings.vue')
  const first = { id: 'stable-identity', key: 'custom_motion', isActive: true, displayOrder: 1 }
  const { fieldDefinitionVersions: definitions } = await vite.ssrLoadModule('/src/services/fieldDefinitionVersions.ts')
  const originalList = definitions.list
  definitions.list = async (id:string) => [{id:'definition-1',field_definition:id,version:1,status:'published',label:'动作描述'}]
  context.after(()=>{definitions.list=originalList})
  const writes: any[] = [], reads: any[] = []
  api.fieldDefinitionPage = async (...args: any[]) => { reads.push(args); return { count: 25, results: [first] } }
  api.getFieldDefinition = async () => first
  api.createFieldDefinition = async (payload: any) => { writes.push(['create', payload]); return { ...first, id: 'new-identity', ...payload } }
  api.updateFieldDefinition = async (id: string, payload: any) => { writes.push(['patch', id, payload]); return { ...first, isActive: payload.is_active } }
  const impactRow = {id:first.id, kind:'field', label:first.key, status:'active', config_url:'/settings?section=fields', overview_url:'/settings/acceptance-smoke?view=relations', acceptance_url:'', capabilities:[], notices:[], groups:[]}
  const impactPage = {count:1,results:[impactRow],next:null,previous:null,generated_at:'2026-09-29',context_label:''}
  http.get = async (url: string, config: any) => { reads.push([url, config.params]); return {data:impactPage} }
  api.executeRegistryDeletion = async () => { throw Error('Impact inspection must not delete') }
  let state: any
  const props = reactive({ fieldDefinitionId: first.id })
  const Host={setup(){state=Component.setup(props,{expose(){}});return()=>h('div')}}
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Host}]})
  await router.push('/');await router.isReady()
  await renderToString(createSSRApp({render:()=>h(RouterView)}).use(createPinia()).use(router))
  await state.openDeepLink()
  assert.equal(state.versions.value.id, first.id)
  await state.load()
  assert.equal(state.count.value, 25)
  state.page.value = 2; state.search.value = 'motion'; state.includeInactive.value = true
  await state.load()
  assert.deepEqual(reads.at(-1), [2, 'motion', true])
  for(let tick=0;tick<10;tick++)await Promise.resolve()
  assert.equal(state.displayNames.value[first.id].value.label, '动作描述')
  let resolveOld!:(v:any)=>void
  definitions.list=()=>new Promise(resolve=>{resolveOld=resolve})
  await state.load()
  definitions.list=async()=>[{id:'definition-2',field_definition:first.id,version:2,status:'published',label:'镜头动作'}]
  await state.load()
  resolveOld([{id:'definition-1',field_definition:first.id,version:1,status:'published',label:'旧名称'}])
  for(let tick=0;tick<10;tick++)await Promise.resolve()
  assert.equal(state.displayNames.value[first.id].value.label,'镜头动作','late label reads must not replace a newer page')
  definitions.list=async()=>{throw Error('name unavailable')}
  await state.load();for(let tick=0;tick<10;tick++)await Promise.resolve()
  assert.equal(state.displayNames.value[first.id].value,null);assert.match(state.displayNames.value[first.id].error,/name unavailable/)
  definitions.list=async()=>[{id:'definition-2',field_definition:first.id,version:2,status:'published',label:'镜头动作'}]
  await state.load();for(let tick=0;tick<10;tick++)await Promise.resolve()
  assert.equal(state.displayNames.value[first.id].value.label,'镜头动作')
  state.begin(first); state.form.isActive = false; await state.save()
  assert.deepEqual(writes[0], ['patch', first.id, { is_active: false }])
  state.begin(); state.form.key = 'new_key'; await state.save()
  assert.deepEqual(writes[1], ['create', { key: 'new_key' }])
  assert.equal(state.versions.value.id, 'new-identity')
  state.begin(first); await state.save()
  assert.equal(writes.length, 2)
  assert.match(state.editorError.value, /无需重复/)
  api.createFieldDefinition = async () => { throw Error('隔离保存失败') }
  state.begin(); state.form.key = 'preserved'; await state.save()
  assert.equal(state.form.key, 'preserved'); assert.equal(state.editorOpen.value, true)
  assert.match(state.editorError.value, /结果尚未确认/)
  assert.equal(state.unknownIdentity.value.key, 'preserved')
  const source=readFileSync(new URL('../src/components/FieldMaintenanceSettings.vue',import.meta.url),'utf8')
  assert.match(source, /:to="relationOverviewUrl\('field',row.id\)"/)
  assert.doesNotMatch(source, /showImpact|impactTarget|RegistryRelationSummary/)
  api.fieldDefinitionPage = async () => { throw Error('隔离列表失败') }
  await state.load()
  assert.match(state.error.value, /隔离列表失败/)
  assert.equal(state.rows.value[0].id, first.id, 'old list is retained with explicit failure')
  assert.equal(writes.length, 2)
})

test('Profile only selects field versions and links to the shared maintenance entry', () => {
  const profile = readFileSync(new URL('../src/components/PromptProfileSettings.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(profile, /createFieldDefinition|updateFieldDefinition|buildFieldPayload|openField|FieldDefinitionVersions\.vue/)
  assert.match(profile, /section=fields/)
  assert.match(profile, /FieldDefinitionVersionSelect/)
  const view = readFileSync(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
  assert.match(view, /FieldMaintenanceSettings v-else-if="tab==='fields'"/)
  assert.match(view, /requestedTab === 'fields'/)
})

test('source selection mode cannot create or modify source definitions', async context => {
  const storage=globalThis.localStorage
  globalThis.localStorage={getItem:()=>null} as any
  context.after(()=>{globalThis.localStorage=storage})
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { default: Component } = await vite.ssrLoadModule('/src/components/ContentSourceManager.vue')
  const { contentSources: api } = await vite.ssrLoadModule('/src/services/contentSources.ts')
  api.save = api.action = async () => { assert.fail('selection must not write') }
  let state: any
  const Host={setup(){state=Component.setup({choose:true},{expose(){},emit(){}});return()=>h('div')}}
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Host}]})
  await router.push('/');await router.isReady()
  await renderToString(createSSRApp({render:()=>h(RouterView)}).use(createPinia()).use(router))
  state.selected.value = { id: 'source', status: 'published' }; state.confirmed.value = true
  await state.save(); await state.act('successor'); await state.act('retire')
  assert.equal(state.busy.value, false)
})
