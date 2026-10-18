import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'

const createVite = () => createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
const rawLinks = () => ({
  model:{id:'model-one',key:'qwen-model',version:1,status:'published',instance_count:2},
  validated_deployment:{id:'instance-one',name:'图片运行实例',status:'active',model_profile_version_id:'model-one'},
  execution_instances:{total:4,active_total:1,preview:[{id:'execution-one',name:'多图执行实例',version:2,status:'published',operation:'image.generate',mode:'',prompt_mode:'profile'}]},
  projector:{state:'linked',version_id:'projector-v1',name:'通用接线',key:'generic.fixed',version:1,status:'retired',is_current:false,is_active:true,definition_sha256:'a'.repeat(64)},
})

test('navigation carries exact identities and a return path, not a current Projector or guessed deployment', async (context) => {
  const vite = await createVite(); context.after(()=>vite.close())
  const links = await vite.ssrLoadModule('/src/services/workflowConfigurationLinks.ts')
  assert.deepEqual(links.workflowModelSettingsRoute('wf','model','instance').query,{section:'models',model_profile_version:'model',focus:'deployments',workflow_context:'wf',deployment:'instance'})
  assert.equal(links.workflowModelSettingsRoute('wf','model').query.deployment,undefined)
  assert.deepEqual(links.workflowExecutionSettingsRoute('wf','exact-version').query,{section:'executions',execution_version:'exact-version',workflow_context:'wf'})
  assert.equal(links.workflowExecutionsRoute('wf').query.relation,'executions')
  assert.deepEqual(links.workflowProjectorSettingsRoute('wf','frozen-v1').query,{section:'workflows',projector_version:'frozen-v1',workflow_context:'wf'})
})

test('adapter preserves association scope and rejects incomplete relation catalogs', async (context) => {
  const vite = await createVite(); context.after(()=>vite.close())
  const {adaptWorkflowConfigurationLinks:adapt} = await vite.ssrLoadModule('/src/services/workflowConfigurationLinks.ts')
  const {adaptWorkflowTemplateVersion} = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const result = adaptWorkflowTemplateVersion({configuration_links:rawLinks()})
  assert.equal(result.configurationLinks.projector.versionId,'projector-v1')
  assert.equal(result.configurationLinks.executionInstances.preview[0].id,'execution-one')
  assert.throws(()=>adapt({}),/关联配置响应不完整/)
  assert.throws(()=>adapt({...rawLinks(),execution_instances:{total:0}}))
  assert.throws(()=>adapt({...rawLinks(),projector:{...rawLinks().projector,version_id:null}}))
  const raw=rawLinks()
  for (const active_total of [-1, 5, undefined, 0]) assert.throws(()=>adapt({...raw,execution_instances:{...raw.execution_instances,active_total}}))
  assert.throws(()=>adapt({...raw,execution_instances:{...raw.execution_instances,preview:[]}}))
  assert.throws(()=>adapt({...raw,execution_instances:{...raw.execution_instances,preview:[{...raw.execution_instances.preview[0],status:'draft'}]}}))
})

test('list and detail rendering name all three relationships and distinguish candidates, missing records, and invalid references', async (context) => {
  const vite = await createVite(); context.after(()=>vite.close())
  const {default: Component} = await vite.ssrLoadModule('/src/components/WorkflowConfigurationLinks.vue')
  const {adaptWorkflowConfigurationLinks:adapt} = await vite.ssrLoadModule('/src/services/workflowConfigurationLinks.ts')
  const render = async (links:unknown, compact=false) => {
    const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
    await router.push('/settings'); await router.isReady()
    const app=createSSRApp({render:()=>h(Component,{workflow:{id:'wf',configurationLinks:links},compact})})
    app.use(router)
    return renderToString(app)
  }
  const html=await render(adapt(rawLinks()))
  for (const text of ['图片运行实例','多图执行实例','通用接线','非当前版','已退役','查看全部 4 个执行版本','具体提示词策略','其他 3 个草稿、退役或停用版本']) assert.ok(html.includes(text),text)
  assert.match(html,/execution_version=execution-one/)
  assert.match(html,/projector_version=projector-v1/)
  assert.match(html,/deployment=instance-one/)
  const draft=rawLinks() as any
  draft.validated_deployment=null
  draft.projector={...draft.projector,state:'not_recorded',version_id:null,version:null}
  draft.execution_instances={total:0,active_total:0,preview:[]}
  const draftHtml=await render(adapt(draft),true)
  assert.match(draftHtml,/尚未绑定校验部署/)
  assert.match(draftHtml,/未记录 Projector/)
  assert.match(draftHtml,/尚无执行实例引用/)
  assert.doesNotMatch(draftHtml,/deployment=|projector_version=/)
  draft.projector.state='invalid'
  assert.match(await render(adapt(draft)),/冻结引用缺失或不一致/)
  assert.match(await render(undefined),/关联配置尚未读取/)
  const compact=await render(adapt(rawLinks()),true)
  assert.match(compact,/更多关联/)
  assert.match(compact,/其他版本 3/)
  assert.match(compact,/历史引用/)
  assert.doesNotMatch(compact,/通用接线|projector_version=|同模型的其他部署|查看全部/,'inactive/history details stay out of table cells')
  const inactive=rawLinks()
  inactive.validated_deployment.status='disabled'
  inactive.execution_instances={...inactive.execution_instances,active_total:0,preview:[]}
  const inactiveHtml=await render(adapt(inactive),true)
  assert.match(inactiveHtml,/部署未启用/)
  assert.match(inactiveHtml,/暂无启用的发布版本/)
  assert.doesNotMatch(inactiveHtml,/图片运行实例|多图执行实例|deployment=instance-one/)
  const active=rawLinks()
  active.projector.status='published';active.projector.is_current=true
  active.execution_instances.active_total=3
  const activeHtml=await render(adapt(active),true)
  assert.match(activeHtml,/projector_version=projector-v1/)
  assert.match(activeHtml,/3 个启用版本/)
  assert.equal((activeHtml.match(/class="relation-dot"/g)||[]).length,3,'every relationship has a status indicator')
  assert.equal((activeHtml.match(/class="relation-state"/g)||[]).length,3,'indicators also carry visible status text')
  assert.match(activeHtml,/source_kind=workflow[^]*relation=executions/,'multiple execution versions link to the exact relation list, not an implicit selection')
  assert.doesNotMatch(activeHtml,/aria-expanded|aria-haspopup/)
})

test('relation indicators preserve configuration semantics, missing data and exact-version navigation', async context => {
  const vite=await createVite();context.after(()=>vite.close())
  const {adaptWorkflowConfigurationLinks:adapt,workflowRelationItems:items}=await vite.ssrLoadModule('/src/services/workflowConfigurationLinks.ts')
  const raw=rawLinks()
  raw.projector.status='published'
  const healthy=items('wf',adapt(raw))
  assert.deepEqual(healthy.map((item:any)=>[item.key,item.status,item.tone]),[
    ['model','已启用','linked'],['execution','已启用','linked'],['projector','已引用','linked'],
  ])
  assert.equal(healthy[2].to.query.projector_version,'projector-v1','published non-current references retain the frozen version')
  assert.match(healthy[0].description,/不代表工作流或执行实例组合验收通过/)
  raw.validated_deployment.model_profile_version_id='other-model'
  assert.deepEqual(items('wf',adapt(raw))[0].tone,'danger')
  assert.equal(items('wf',adapt(raw))[0].to,undefined,'mismatched models require relation review')
  assert.equal(items('wf',adapt(raw))[1].to.query.execution_version,'execution-one')
  raw.projector.state='invalid'
  assert.equal(items('wf',adapt(raw))[2].tone,'danger')
  assert.equal(items('wf',adapt(raw))[2].to,'/settings/workflows/wf')
  const missing={...rawLinks(),model:null,validated_deployment:null,
    execution_instances:{total:0,active_total:0,preview:[]}}
  assert.equal(items('wf',adapt(missing))[1].status,'未配置')
  assert.equal(items('wf',adapt(missing))[1].to.query.relation,'executions')
})

test('Projector navigation reads the exact version, keeps it read-only and ignores stale requests', async (context) => {
  const vite=await createVite(); context.after(()=>vite.close())
  const {createPinia}=await import('pinia')
  const {routeLocationKey,routerKey}=await import('vue-router')
  const storage=Object.getOwnPropertyDescriptor(globalThis,'localStorage')
  context.after(()=>storage?Object.defineProperty(globalThis,'localStorage',storage):Reflect.deleteProperty(globalThis,'localStorage'))
  Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}}})
  const {default:Settings}=await vite.ssrLoadModule('/src/views/SettingsView.vue')
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const route:any={query:{section:'workflows'},fullPath:'/settings?section=workflows'}
  let state:any
  const app=createSSRApp({setup(){state=Settings.setup({}, {expose:()=>{}});return ()=>h('div')}})
  app.use(createPinia());app.provide(routeLocationKey,route);app.provide(routerKey,{replace:async()=>{},beforeEach:()=>()=>{}} as any)
  await renderToString(app)
  const pending=new Map<string,(value:unknown)=>void>()
  const mock=context.mock.method(platformApi,'getWorkflowProjectorVersion',(id:string)=>new Promise(resolve=>pending.set(id,resolve)))
  const first='00000000-0000-4000-8000-000000000001', second='00000000-0000-4000-8000-000000000002'
  route.query.projector_version=first
  const one=state.followWorkflowProjectorRoute()
  route.query.projector_version=second
  const two=state.followWorkflowProjectorRoute()
  const version=(id:string)=>({id,version:1,status:'retired',projectorKey:'old',engineKey:'generic.fixed',engineVersion:1,providerDriverType:'comfyui',operation:'workflow.import',outputKind:'image',definition:{}})
  pending.get(second)!(version(second)); await two
  pending.get(first)!(version(first)); await one
  assert.equal(state.workflowProjectorEditorVersion.value.id,second)
  assert.equal(state.workflowProjectorReadOnly.value,true)
  assert.equal(mock.mock.callCount(),2)
  mock.mock.mockImplementation(async()=>{throw Error('forbidden')})
  await state.followWorkflowProjectorRoute()
  assert.match(state.projectorNavigationError.value,/forbidden/)
  assert.equal(state.workflowProjectorEditorOpen.value,false,'must not show an old version after failure')
})

test('more-relations opens the shared precise resource panel without another modal', async context => {
  const vite=await createVite();context.after(()=>vite.close())
  const {default:Component}=await vite.ssrLoadModule('/src/components/WorkflowConfigurationLinks.vue')
  const {adaptWorkflowConfigurationLinks:adapt}=await vite.ssrLoadModule('/src/services/workflowConfigurationLinks.ts')
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
  await router.push('/settings');await router.isReady()
  let state:any, navigated=0
  const wrapper={...Component,setup(props:any,ctx:any){state=Component.setup(props,ctx);return state}}
  const app=createSSRApp(wrapper,{workflow:{id:'wf',templateName:'测试工作流',configurationLinks:adapt(rawLinks())},compact:true,onNavigate:()=>navigated++})
  app.use(router)
  const renderContext:any={}
  const html=await renderToString(app,renderContext)
  assert.match(html,/href="\/settings\/acceptance-smoke\?view=relations&amp;kind=workflow&amp;ids=wf"/)
  assert.match(html,/测试工作流 · 更多关联/)
  assert.doesNotMatch(html,/aria-haspopup|aria-expanded|role="dialog"/)
  assert.equal(renderContext.teleports,undefined)
  state.navigate()
  assert.equal(navigated,1)
})
