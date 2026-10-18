import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, createRenderer, h, nextTick, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import { settingsNavigationGroups, settingsPrimaryNavigation, settingsSectionForQuery } from '../src/content/settingsSectionCatalog.ts'

const viteServer=()=>createServer({logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false},optimizeDeps:{noDiscovery:true}})
const routerFactory=()=>createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
const overview=(kind='execution',id='exact')=>`/settings/acceptance-smoke?view=relations&kind=${kind}&ids=${id}`
const row=(kind='execution',id='exact')=>({kind,id,label:`${kind} exact version`,status:'published',config_url:`/settings?section=models&execution_version=${id}`,overview_url:overview(kind,id),acceptance_url:'',capabilities:['declared.operation'],notices:[],groups:[]})
const page=(rows=[row()])=>({count:rows.length,next:null,previous:null,generated_at:'2026-10-09T01:00:00Z',context_label:'',results:rows})
const fixture=(mode='manual')=>{
  const version:any={id:'exact',revision:4,status:'published',operation:'custom.video',mode:'custom',instance_name:'精确样例'}
  const contract:any={version_id:'exact',revision:4,frozen:true,sha256:'a'.repeat(64),definition:{
    configuration:{inputs:[{key:'custom.frames',kind:'image',required:false,min_items:0,max_items:7,targets:[{target_kind:'workflow_slot',target_key:'reference_pixels',is_primary:true}]}],auxiliaries:[],workflow_version_id:'frozen-workflow',output_version_id:'exact-output'},
    resources:{model_profile_version_id:'frozen-model',deployment_id:'exact-deployment',workflow:{id:'frozen-workflow',projector:{version_id:'old-projector'}},prompt_profile:mode==='profile'?{id:'frozen-profile'}:null},
    interface:{prompt:{mode},parameters:{},output:{protocol:'media.custom',schema_version:2}},
  }}
  return {version,contract}
}

test('all settings have exactly one navigation group and exact execution links select the independent section',()=>{
  const items=settingsNavigationGroups.flatMap(group=>group.items)
  assert.equal(new Set(items).size,items.length)
  assert.deepEqual([...items].sort(),settingsPrimaryNavigation.map(item=>item.id).sort())
  assert.equal(settingsSectionForQuery({section:'models',execution_version:'old-exact'}),'executions')
  assert.equal(settingsSectionForQuery({section:'models',deployment:'d'}),'models')
  assert.equal(settingsSectionForQuery({}),'executions')
})

test('execution overview projects exact saved facts, preserves zero and never invents a missing branch',async context=>{
  const vite=await viteServer();context.after(()=>vite.close())
  const {executionPath,canonicalSettingsResourceUrl}=await vite.ssrLoadModule('/src/services/executionPath.ts')
  const {version,contract}=fixture()
  const path=executionPath(version,contract)
  assert.equal(path.inputs[0].min_items,0);assert.equal(path.inputs[0].required,false)
  assert.equal(path.inputs[0].targets[0].target_key,'reference_pixels')
  assert.equal(path.projectorId,'old-projector');assert.equal(path.profileId,null)
  assert.deepEqual(path.auxiliaries,[]);assert.equal(path.outputId,'exact-output')
  contract.definition.interface.output={adapter:{id:'generic.media'},contract:{primary:{kind:'video',node_id:'declared-node',required:true}}}
  assert.equal(executionPath(version,contract).outputKind,'video')
  assert.equal(executionPath(version,contract).primaryOutput.node_id,'declared-node')
  contract.definition.interface.output={adapter:'text.custom/v1',kind:'text'}
  assert.equal(executionPath(version,contract).outputAdapter,'text.custom/v1')
  assert.equal(executionPath(version,contract).primaryOutput,null)
  for (const change of [{version_id:'other'},{revision:5},{frozen:false}]) assert.throws(()=>executionPath(version,{...contract,...change}),/精确版本/)
  delete contract.definition.configuration.auxiliaries
  assert.equal(executionPath(version,contract).auxiliaries,null)
  contract.definition.configuration.inputs=[{key:'bad',targets:null}]
  assert.throws(()=>executionPath(version,contract),/输入声明不完整/)
  delete contract.definition.resources
  assert.throws(()=>executionPath(version,contract),/合同不完整/)
  const url=canonicalSettingsResourceUrl('/settings?section=models&execution_version=v1&workflow_context=w1#part')
  assert.match(url,/section=executions/);assert.match(url,/execution_version=v1/);assert.match(url,/workflow_context=w1#part/)
  assert.throws(()=>canonicalSettingsResourceUrl('https://outside.example/settings'))
})

test('three-stage overview distinguishes manual, strategy and absent prompt handling without any model call',async context=>{
  const vite=await viteServer();context.after(()=>vite.close())
  const {default:Component}=await vite.ssrLoadModule('/src/components/ExecutionPathOverview.vue')
  const {api}=await vite.ssrLoadModule('/src/services/api.ts')
  for(const method of ['post','patch','put','delete']) api[method]=()=>assert.fail('overview must never write')
  const router=routerFactory();await router.push('/settings?section=executions');await router.isReady()
  const render=async(mode:string,selected='inputs')=>{
    const props=fixture(mode)
    const wrapper={...Component,setup(p:any,ctx:any){const state=Component.setup(p,ctx);state.selected.value=selected;return state}}
    const app=createSSRApp(wrapper,props);app.use(router);return renderToString(app)
  }
  const manual=await render('manual')
  for(const value of ['输入准备','模型能力调用','输出','custom.frames','reference_pixels','不会自动使用辅助 AI','数量 0 – 7']) assert.ok(manual.includes(value),value)
  assert.doesNotMatch(manual,/frozen-profile|type="submit"/)
  assert.match(await render('profile'),/查看精确提示词策略与字段依赖/)
  assert.match(await render('none'),/明确不需要提示词处理/)
  const invocation=await render('manual','invocation')
  assert.match(invocation,/模型能力版本/)
  assert.match(invocation,/查看引用的 Projector/)
  assert.match(invocation,/下钻此工作流的节点与输入输出/)
  assert.match(await render('manual','output'),/查看确切业务输出版本/)
  assert.doesNotMatch(invocation,/<a[^>]+href=/)
  const {readFile}=await import('node:fs/promises')
  const source=await readFile(new URL('../src/components/ExecutionPathOverview.vue',import.meta.url),'utf8')
  assert.match(source,/\.execution-path-overview\{[^}]*font-size:var\(--config-body-size\)/)
  assert.match(source,/\.path-detail h3,\.path-detail h4\{font-size:var\(--config-section-size\);font-weight:600/)
  assert.match(source,/@media\(max-width:640px\)\{\.execution-path-overview\{--config-control-height:44px\}/)
  assert.doesNotMatch(source,/font-size:\s*13px/)
  for(const [kind,key] of [['prompt','profileId'],['model','modelId'],['projector','projectorId'],['workflow','workflowId'],['output','outputId']]) assert.ok(source.includes(`inspect('${kind}',path.${key},$event)`))
})

test('resource hierarchy is context-specific and unknown relationships remain visible',async context=>{
  const vite=await viteServer();context.after(()=>vite.close())
  const {registryHierarchy,registryRoles}=await vite.ssrLoadModule('/src/services/registryHierarchy.ts')
  const {relationKinds}=await vite.ssrLoadModule('/src/services/registryRelations.ts')
  assert.deepEqual(Object.keys(registryRoles).sort(),Object.keys(relationKinds).sort())
  const group=(key:string)=>({key,label:key,kind:'workflow',count:0,url:overview('workflow')})
  assert.equal(registryHierarchy({...row('model'),groups:[group('workflows')]})[0].key,'related')
  assert.equal(registryHierarchy({...row('execution'),groups:[group('workflow')]})[0].key,'processing')
  assert.equal(registryHierarchy({...row('recipe'),groups:[group('executions')]})[0].key,'inputs')
  assert.equal(registryHierarchy({...row('prompt'),groups:[group('future')]})[0].key,'related')
})

test('resource breadcrumbs retain exact identities, handle cycles and reject unrelated navigation state',async context=>{
  const vite=await viteServer();context.after(()=>vite.close())
  const {readRegistryTrail,descendRegistry}=await vite.ssrLoadModule('/src/services/registryHierarchy.ts')
  const a={label:'实例',url:overview()},b={label:'策略',url:overview('prompt','p1')}
  assert.deepEqual(descendRegistry([],a,b.url).trail,[a])
  assert.deepEqual(descendRegistry([a],b,a.url).trail,[])
  assert.deepEqual(descendRegistry([a],b,b.url).trail,[a])
  assert.deepEqual(readRegistryTrail([{label:'bad',url:'https://outside.example/'}]),[])
  assert.deepEqual(readRegistryTrail([{label:'bad',url:overview()+'&execute=true'}]),[])
  assert.deepEqual(readRegistryTrail([a,b]),[a,b])
  const {relationConfigUrl}=await vite.ssrLoadModule('/src/services/registryRelations.ts')
  const target=new URL(relationConfigUrl({...row(),config_url:'/settings?section=models&execution_version=exact#part'}),'https://test.invalid')
  assert.equal(target.searchParams.get('section'),'executions')
  assert.equal(target.searchParams.get('relation_context'),'execution:exact')
  assert.equal(target.hash,'#part')
})

test('unique return control retains the drill-down trail from both settings and workflow detail',async context=>{
  const vite=await viteServer();context.after(()=>vite.close())
  const {default:Component}=await vite.ssrLoadModule('/src/components/RegistryContextReturn.vue')
  const router=routerFactory()
  const id='00000000-0000-4000-8000-000000000001'
  const trail=[{label:'原实例',url:overview('execution','parent')}]
  for(const path of ['/settings','/settings/workflows/'+id]) {
    await router.push({path,query:{relation_context:'workflow:'+id},state:{registryTrail:trail}})
    let state:any
    const app=createSSRApp({setup(){state=Component.setup({}, {expose(){}});return()=>h('div')}});app.use(router);await renderToString(app)
    assert.equal(state.target.value.query.ids,id)
    assert.deepEqual(state.target.value.state.registryTrail,trail)
  }
})

test('single-object panel cancels stale reads, detects wrong objects, retries reads and preserves trail across pages',async context=>{
  const vite=await viteServer();context.after(()=>vite.close())
  const {default:Component}=await vite.ssrLoadModule('/src/components/RegistryRelationsPanel.vue')
  const {api}=await vite.ssrLoadModule('/src/services/api.ts')
  const pending:any[]=[]
  api.get=(_url:any,options:any)=>new Promise((resolve,reject)=>pending.push({resolve,reject,options}))
  for(const method of ['post','patch','put','delete']) api[method]=()=>assert.fail('resource navigation must not write')
  const router=routerFactory();await router.push(overview());await router.isReady()
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  let state:any
  const app=renderer.createApp({setup(){state=Component.setup({}, {expose(){}});return()=>h('div')}})
  app.use(router);app.provide(ssrContextKey,{});app.mount({});context.after(()=>app.unmount())
  await router.push(overview('prompt','p1'));await nextTick()
  assert.equal(pending[0].options.signal.aborted,true)
  pending[1].resolve({data:page([row('prompt','p1')])});await new Promise(setImmediate)
  pending[0].resolve({data:page()});await new Promise(setImmediate)
  assert.equal(state.data.value.results[0].id,'p1')
  const refresh=state.load();pending.at(-1).resolve({data:page([row('model','wrong')])});await refresh
  assert.match(state.error.value,/其他对象/);assert.equal(state.data.value,null)
  const retry=state.load();pending.at(-1).resolve({data:page([row('prompt','p1')])});await retry
  assert.equal(state.error.value,'')
  await state.descend(overview('structure','s1'));await nextTick()
  assert.equal(state.trail.value[0].url,overview('prompt','p1'))
  assert.deepEqual(Object.keys(pending.at(-1).options.params).sort(),['ids','kind','page','page_size'])
  await state.changePage(2);await nextTick()
  assert.equal(state.trail.value.length,1)
  state.ancestor(0);await new Promise(setImmediate);await nextTick()
  assert.equal(router.currentRoute.value.query.ids,'p1');assert.equal(state.trail.value.length,0)
})

test('server pagination smaller than requested stays accessible and missing exact objects never look dependency-free',async context=>{
  const vite=await viteServer();context.after(()=>vite.close())
  const {default:Component}=await vite.ssrLoadModule('/src/components/RegistryRelationsPanel.vue')
  const {api}=await vite.ssrLoadModule('/src/services/api.ts');api.get=async()=>({data:page([])})
  const render=async(url:string,data:any)=>{
    const router=routerFactory();await router.push(url);await router.isReady()
    const wrapper={...Component,setup(p:any,ctx:any){const state=Component.setup(p,ctx);state.data.value=data;state.loading.value=false;return state}}
    const app=createSSRApp(wrapper);app.use(router);return renderToString(app)
  }
  const small={...page([row()]),count:20,next:'server-page-2'}
  const html=await render('/settings/acceptance-smoke?view=relations&kind=execution',small)
  assert.match(html,/关联目录分页/);assert.match(html,/下一页/);assert.match(html,/匹配共 20 项/)
  assert.match(await render(overview(),page([])),/指定对象不存在或已删除/)
  assert.match(await render(overview()+'&search=no-match',page([])),/清除搜索/)
})

test('standalone directory uses the shared readonly explorer and exact maintenance links without leaving search context', async context => {
  const vite=await viteServer();context.after(()=>vite.close())
  const {default:Component}=await vite.ssrLoadModule('/src/components/RegistryRelationsPanel.vue')
  const {api}=await vite.ssrLoadModule('/src/services/api.ts')
  api.get=()=>new Promise(()=>{})
  for (const method of ['post','patch','put','delete']) api[method]=()=>assert.fail('directory must not write')
  const router=routerFactory();await router.push(overview('recipe','old')+'&search=原值&page=2');await router.isReady()
  const wrapper={...Component,setup(p:any,ctx:any){const state=Component.setup(p,ctx);state.data.value=page([{...row('recipe','old'),label:'同名配方 v2',config_url:'/settings?section=routing&recipe_version=old'}]);state.loading.value=false;return state}}
  const app=createSSRApp(wrapper);app.use(router);const html=await renderToString(app)
  assert.match(html,/class="registry-resource-explorer"/)
  assert.match(html,/class="resource-owner"/)
  assert.match(html,/同名配方 v2 · 查看定义与使用方/)
  assert.match(html,/recipe_version=old&amp;relation_context=recipe%3Aold" target="_blank" rel="noopener"/)
  assert.match(html,/value="原值"/);assert.match(router.currentRoute.value.fullPath,/page=2/)
  assert.doesNotMatch(html,/公共资源只读详情|configurationTarget/,'details remain lazy')
})
