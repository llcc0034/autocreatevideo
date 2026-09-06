import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createSSRApp, h, ref } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'

const server = () => createServer({ logLevel:'silent', appType:'custom', server:{middlewareMode:true,hmr:false}, optimizeDeps:{noDiscovery:true} })
const row = (id='model-one') => ({id,kind:'model',label:'Qwen · v1',status:'published',config_url:'/settings?section=models&model_profile_version='+id,
  overview_url:'/settings/acceptance-smoke?view=relations&kind=model&ids='+id,acceptance_url:'',capabilities:['image.generate'],notices:['关联数量不是运行验收结果'],
  groups:[{key:'bindings',label:'提示词绑定',kind:'binding',count:0,url:'/settings/acceptance-smoke?view=relations&kind=binding&source_kind=model&source_id='+id+'&relation=bindings'}]})
const page = (rows=[row()]) => ({count:rows.length,next:null,previous:null,generated_at:'2026-09-05T00:00:00Z',context_label:'',results:rows})

test('relation contract rejects incomplete, unsafe and fabricated zero summaries', async context => {
  const vite=await server(); context.after(()=>vite.close())
  const {adaptRelationPage,relationConfigUrl,relationOverviewUrl}=await vite.ssrLoadModule('/src/services/registryRelations.ts')
  assert.equal(adaptRelationPage(page()).results[0].groups[0].count,0)
  assert.equal(adaptRelationPage({data:page()}).count,1)
  for (const invalid of [{}, {...page(),results:[{...row(),groups:undefined}]}, {...page(),results:[{...row(),config_url:'https://external.example'}]}, {...page(),results:[{...row(),capabilities:null}]}]) assert.throws(()=>adaptRelationPage(invalid))
  assert.match(relationConfigUrl(row()),/relation_context=model%3Amodel-one/)
  assert.equal(relationOverviewUrl('projector','frozen-v1'),'/settings/acceptance-smoke?view=relations&kind=projector&ids=frozen-v1')
})

test('summary exposes precise group links, zero states, errors and retry without edit actions',async context=>{
  const vite=await server();context.after(()=>vite.close())
  const {default:Component}=await vite.ssrLoadModule('/src/components/RegistryRelationSummary.vue')
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
  await router.push('/settings');await router.isReady()
  const render=async(props:any)=>{const app=createSSRApp({render:()=>h(Component,props)});app.use(router);return renderToString(app)}
  const html=await render({row:row(),expanded:true})
  assert.match(html,/提示词绑定/); assert.match(html,/source_id=model-one/)
  assert.match(html,/configuration-relation relation-muted/)
  assert.match(html,/0 项/)
  assert.doesNotMatch(html,/relation-warning|relation-danger|relation-linked/,'zero configuration counts do not imply a fault or acceptance result')
  assert.match(html,/关联数量不是运行验收结果/);assert.doesNotMatch(html,/发布|保存|创建任务/)
  assert.match(await render({}),/尚未读取，不代表没有关联/)
  assert.match(await render({error:'读取失败'}),/重试/)
  assert.doesNotMatch(await render({error:'读取失败',row:row()}),/Qwen|source_id=model-one/)
  const compact=await render({row:row(),condensed:true})
  assert.match(compact,/更多关联/)
  assert.doesNotMatch(compact,/source_id=model-one|relation-chips|提示词绑定/)
  assert.equal((compact.match(/<a /g)||[]).length,1,'model rows have a single more-relations entry')
  const configured=await render({row:{...row(),status:'retired',groups:[{...row().groups[0],count:4}],notices:['关键配置提示','详细原因一','详细原因二']},expanded:true})
  assert.match(configured,/configuration-relation relation-info/)
  assert.match(configured,/4 项/)
  assert.match(configured,/notice-preview[^]*关键配置提示/)
  assert.match(configured,/<details[^>]*><summary[^>]*>更多配置说明（2）<\/summary>/)
  assert.match(configured,/详细原因一/)
  assert.doesNotMatch(configured,/<details open|relation-linked/)
  const current=await render({row:{...row(),notices:['尚未配置提示词绑定','Projector 引用异常，不能执行']},expanded:true})
  assert.match(current,/current-notices/)
  assert.match(current,/Projector 引用异常，不能执行/)
  assert.doesNotMatch(current,/<details/,'current configuration gaps must remain visible, regardless of notice ordering')
})

test('shared relationship control preserves full names, explicit statuses, and semantic navigation', async context=>{
  const vite=await server();context.after(()=>vite.close())
  const {default:Component}=await vite.ssrLoadModule('/src/components/ConfigurationRelationButton.vue')
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
  await router.push('/settings');await router.isReady()
  const render=async(props:any)=>{const app=createSSRApp({render:()=>h(Component,props)});app.use(router);return renderToString(app)}
  const name='很长的多图参考工作流配置名称'.repeat(10)
  const html=await render({label:'Projector',name,status:'已引用',tone:'linked',description:'查看冻结配置',to:'/settings?projector_version=frozen'})
  assert.ok(html.includes(`title="Projector · ${name} · 已引用。查看冻结配置"`))
  assert.match(html,/aria-label="Projector：/)
  assert.match(html,/<a[^>]+href="\/settings\?projector_version=frozen"/)
  assert.match(html,/relation-state[^]*已引用/)
  assert.doesNotMatch(html,/aria-haspopup|aria-expanded/)
  const button=await render({label:'提示词',status:'3 项',tone:'info',description:'查看全部关联',expanded:false})
  assert.match(button,/<button type="button"/)
  assert.match(button,/aria-haspopup="dialog" aria-expanded="false"/)
})

test('batch loader bounds requests, clears stale context and discards late responses',async context=>{
  const vite=await server();context.after(()=>vite.close())
  const {useRegistryRelations}=await vite.ssrLoadModule('/src/services/registryRelations.ts')
  const {api}=await vite.ssrLoadModule('/src/services/api.ts')
  const previous=api.get;context.after(()=>{api.get=previous})
  const items=ref<any[]>([]);let state:any
  await renderToString(createSSRApp({setup(){state=useRegistryRelations('model',()=>items.value);return()=>h('div')}}))
  const calls:any[]=[]
  api.get=async(url:string,options:any)=>{calls.push({url,...options});return {data:page(options.params.ids.split(',').map(row))}}
  items.value=Array.from({length:105},(_,i)=>({id:'model-'+i}))
  await state.reload()
  assert.deepEqual(calls.map(call=>call.params.ids.split(',').length),[50,50,5])
  assert.equal(Object.keys(state.rows.value).length,105)
  items.value=[{id:'small-1'},{id:'small-2'}]
  api.get=async(_url:string,{params}:any)=>({data:{...page([row('small-'+params.page)]),count:2,next:params.page===1?'next-page':null}})
  await state.reload()
  assert.deepEqual(Object.keys(state.rows.value),['small-1','small-2'])
  const pending:any[]=[]
  api.get=(_url:string,options:any)=>new Promise(resolve=>pending.push({resolve,options}))
  items.value=[{id:'old'}];const first=state.reload()
  assert.deepEqual(state.rows.value,{})
  items.value=[{id:'new'}];const second=state.reload()
  assert.equal(pending[0].options.signal.aborted,true)
  pending[1].resolve({data:page([row('new')])});await second
  pending[0].resolve({data:page([row('old')])});await first
  assert.deepEqual(Object.keys(state.rows.value),['new'])
  api.get=async()=>({data:page([])});await state.reload()
  assert.match(state.error.value,/已删除或不存在/);assert.deepEqual(state.rows.value,{})
})

test('views share summary and keep exact-object configuration in original pages',async()=>{
  const settings=await readFile(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
  const prompts=await readFile(new URL('../src/components/PromptProfileSettings.vue',import.meta.url),'utf8')
  const center=await readFile(new URL('../src/views/AcceptanceSmokeCenterView.vue',import.meta.url),'utf8')
  const panel=await readFile(new URL('../src/components/RegistryRelationsPanel.vue',import.meta.url),'utf8')
  for(const state of ['modelRelations','deploymentRelations','projectorRelations','recipeRelations']) assert.ok(settings.includes(`:row="${state}.rows.value`))
  for(const state of ['profileRelations','bindingRelations']) assert.ok(prompts.includes(`:row="${state}.rows.value`))
  assert.match(settings,/workflowProjectorCatalogOpen.value \? workflowProjectorVersions/)
  assert.match(settings,/recipeNavigationError/);assert.match(settings,/返回关联总览/)
  assert.match(settings,/:row="deploymentRelations.rows.value\[item.id\]"[^>]*condensed/)
  assert.match(settings,/:row="modelRelations.rows.value\[group.profile.id\]"[^>]*condensed/)
  assert.doesNotMatch(settings,/提示词策略关联<\/button>|关联全貌与配方引用<\/RouterLink>/)
  assert.match(prompts,/div v-for="item in filteredProfiles"/)
  assert.match(center,/if \(showRelations.value\).*pageData.value = null; return/)
  assert.match(center,/workflowId: String\(route.query.workflow_id/)
  assert.match(panel,/page_size: 25/);assert.match(panel,/controller\?\.abort/)
  assert.match(panel,/class="relation-actions"/)
  assert.match(panel,/:aria-label="`\$\{row.label\} · 查看验收与下一步`"/)
  assert.doesNotMatch(panel,/<footer>/,'object actions stay alongside the title instead of creating a mostly empty footer')
  assert.doesNotMatch(panel,/platformApi\.(validate|publish|create)/)
})
