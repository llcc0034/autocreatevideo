import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp as vueSSRApp, h } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { renderToString } from 'vue/server-renderer'
const createSSRApp = (component:any) => {
  globalThis.localStorage = { getItem:()=>null } as any
  return vueSSRApp(component).use(createPinia()).use(createRouter({history:createMemoryHistory(),routes:[{path:'/settings',component:{render:()=>null}}]}))
}

test('source manager authors exact versions with bounded pages, explicit publish and truthful errors', async context => {
  const vite = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(() => vite.close())
  const {contentSources: api} = await vite.ssrLoadModule('/src/services/contentSources.ts')
  const {default: Manager} = await vite.ssrLoadModule('/src/components/ContentSourceManager.vue')
  const version = {id:'source-1',source_key:'family-1',version:1,name:'故事梗概',description:'输入说明',reader_selector:'project.logline',data_structure_version:'structure-v1',reader:{protocol:'project_snapshot/v1',selector:'project.logline',scope_kind:'project',value_kind:'text',snapshot_policy:'confirmed_preview'},status:'draft',revision:1,contract_sha256:'a'.repeat(64)}
  let state: any, calls: any[] = []
  api.list = async (...args:any[]) => {calls.push(['list',...args]);return {count:21,results:[version]}}
  api.readers = async () => [{value:'project.logline',label:'一句话梗概',record_location:'项目设定',execution_usage:'确认后冻结',reader:version.reader}]
  api.save = async (...args:any[]) => {calls.push(['save',JSON.parse(JSON.stringify(args))]);return version}
  api.action = async (...args:any[]) => {calls.push(['action',...args]);return {...version,status:'published',revision:2}}
  await renderToString(createSSRApp({setup(){state=Manager.setup({choose:false},{expose(){},emit(){}});return()=>h('div')}}))
  await state.load()
  assert.deepEqual(calls[0],['list',1,'',''])
  assert.equal(state.count.value,21)
  state.edit(null); Object.assign(state.form,{name:'故事梗概',description:'输入说明',reader_selector:'project.logline',data_structure_version:'structure-v1'})
  await state.save()
  assert.equal(state.selected.value.id,'source-1')
  assert.equal(calls.find(row=>row[0]==='save')[1][0].data_structure_version,'structure-v1')
  assert.equal(calls.filter(row=>row[0]==='action').length,0,'save must not publish or call a model')
  await state.act('publish')
  assert.equal(calls.filter(row=>row[0]==='action').length,0,'explicit review required')
  state.confirmed.value=true; await state.act('publish')
  assert.equal(state.selected.value.status,'published')
  state.readers.value = [{value:'project.logline',reader:{protocol:'future/v2'}}]
  assert.deepEqual(JSON.parse(JSON.stringify(state.savedReader.value)),version.reader,'saved detail must not use the current author catalog')
  assert.equal(state.choosingReader.value,false)
  state.form.reader_selector='style.visual_prompt'
  assert.equal(state.choosingReader.value,true)
  assert.equal(state.savedReader.value.selector,'project.logline','unsaved selection must not replace the version reader')
  api.list = async () => {throw new Error('source offline')}
  await state.load()
  assert.equal(state.rows.value.length,1,'failed refresh keeps previous rows explicitly marked stale')
  assert.equal(state.readers.value.length,1,'successful reader catalog remains independent from failed version list')
  assert.match(state.error.value,/source offline/)
})

test('selecting a source version maps once and preserves its identity in save and preview payloads', async context => {
  const vite = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(() => vite.close())
  const {default: Settings} = await vite.ssrLoadModule('/src/components/PromptProfileSettings.vue')
  let state:any
  await renderToString(createSSRApp({setup(){state=Settings.setup({}, {expose(){}});return()=>h('div')}}))
  state.ruleForm.channel='system_instruction'
  state.selectSourceVersion({id:'source-1',name:'梗概',version:2,reader_selector:'project.logline',contract_sha256:'a'.repeat(64)})
  assert.equal(state.ruleForm.sourceSelector,'project.logline')
  assert.equal(state.ruleForm.generationSourceSelector,'project.logline')
  assert.equal(state.ruleForm.generationMode,'passthrough')
  assert.equal(state.ruleForm.channel,'positive_prompt','project facts must never become system instructions')
  assert.equal(state.buildRulePayload().source_version,'source-1')
  const html = await renderToString(createSSRApp({render:()=>h(Settings,{})}))
  assert.doesNotMatch(html,/source-1/,'temporary editing state must not leak into another component')
})

test('request source authoring explains request scope and selects the same exact source version', async context => {
  const vite = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(() => vite.close())
  const {default: Manager} = await vite.ssrLoadModule('/src/components/ContentSourceManager.vue')
  const {default: Settings} = await vite.ssrLoadModule('/src/components/PromptProfileSettings.vue')
  // Exercise real content and bindings in SSR; focus management needs a browser.
  const {default: Modal} = await vite.ssrLoadModule('/src/components/BaseModal.vue')
  const modalSetup=Modal.setup,modalRender=Modal.ssrRender
  Modal.setup=(props:any,{slots}:any)=>()=>props.open?h('section',slots.default?.()):null
  delete Modal.ssrRender
  context.after(()=>{Modal.setup=modalSetup;Modal.ssrRender=modalRender})
  const reader = {protocol:'request_text/v1',selector:'request.theme',scope_kind:'request',value_kind:'text',snapshot_policy:'confirmed_request'}
  const source = {id:'request-source',name:'本次故事构想',version:1,reader_selector:'request.theme',reader,contract_sha256:'b'.repeat(64)}
  const html = await renderToString(createSSRApp({...Manager, setup(props,ctx) {
    const state=Manager.setup(props,ctx)
    state.readers.value=[{value:'request.theme',label:'请求主题',group_label:'本次请求输入',record_location:'创建执行准备 → 本次故事构想',description:'本次文字',execution_usage:'保存后冻结',reader}]
    state.edit(null);state.form.reader_selector='request.theme'
    return state
  }}))
  assert.match(html,/本次请求输入/);assert.match(html,/text \/ request/)
  assert.match(html,/本次填写文字/);assert.match(html,/不是自动查询项目/)
  assert.doesNotMatch(html,/文字 · 项目范围/)
  let state:any
  await renderToString(createSSRApp({setup(){state=Settings.setup({}, {expose(){}});return()=>h('div')}}))
  state.selectSourceVersion(source)
  assert.equal(state.ruleForm.sourceSelector,'request.theme')
  assert.equal(state.buildRulePayload().source_version,'request-source')
  assert.equal(state.ruleForm.generationMode,'passthrough')
})
