import assert from 'node:assert/strict'
import test from 'node:test'
import {readFileSync} from 'node:fs'
import {createServer} from 'vite'
import {createSSRApp,createRenderer,h,nextTick,reactive,ssrContextKey} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {createRouter,createMemoryHistory} from 'vue-router'
import {adaptRetryPreview,sameRetryContext} from '../src/services/productionRetryPreview.ts'
import {retryPreviewFixture,assemblyRetryPreviewFixture} from './fixtures/retry-preview.ts'

test('retry preview adapter preserves exact identities and rejects incomplete or contradictory metadata',()=>{
  const raw=retryPreviewFixture(), value=adaptRetryPreview(raw)
  assert.deepEqual(value.retryContext,{expectedStageId:'stage',expectedAttemptIds:['failed']})
  assert.equal(value.results[2]!.attempt_id,null)
  assert.ok(sameRetryContext(value.retryContext,{expectedStageId:'stage',expectedAttemptIds:['failed']}))
  assert.ok(!sameRetryContext(value.retryContext,{expectedStageId:'other',expectedAttemptIds:['failed']}))
  for(const mutate of [r=>delete r.can_submit,r=>delete r.reason,r=>r.count=5,r=>r.retry_context.expected_attempt_ids=['failed','failed'],
    r=>r.results[0].disposition='unknown',r=>r.results[0].attempt_id=null,r=>r.can_submit=false,r=>r.preparation_count=-1]){
    const bad:any=structuredClone(raw);mutate(bad);assert.throws(()=>adaptRetryPreview(bad))
  }
})

test('assembly preview checks mode, original consumption, counts and complete confirmation identity',()=>{
  const raw=assemblyRetryPreviewFixture(), value=adaptRetryPreview(raw)
  assert.equal(value.assembly_count,3)
  assert.equal(value.results[0]!.preparation_id,null)
  assert.equal(value.results[2]!.disposition,'attention')
  assert.ok(!sameRetryContext(value.retryContext,{...value.retryContext,expectedAssemblyJobIds:['replacement']}))
  assert.ok(!sameRetryContext(value.retryContext,{...value.retryContext,expectedInputSha256:'b'.repeat(64)}))
  for (const mutate of [r=>delete r.assembly_count,r=>delete r.retry_context.expected_input_sha256,
    r=>r.retry_context.expected_attempt_ids=['model'],r=>r.mode='stage_flow',r=>r.results[0].preparation_id='prep',
    r=>r.results[0].episode_number=0,r=>delete r.results[0].consumer_input_sha256,r=>r.call_count=1]) {
    const bad:any=structuredClone(raw);mutate(bad);assert.throws(()=>adaptRetryPreview(bad))
  }
})

test('assembly component renders episode job original digest and existing task link, escaping failure text',async()=>{
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  try {
    const {default:Panel}=await vite.ssrLoadModule('/src/components/ProductionRetryPreview.vue')
    const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
    const raw=assemblyRetryPreviewFixture();raw.results[0]!.error_message='<script>isolated failure</script>'
    const value=adaptRetryPreview(raw)
    platformApi.getProductionRetryPreview=async()=>value
    const Loaded={...Panel,async setup(props:any,ctx:any){const state=Panel.setup(props,ctx);await Promise.resolve();await nextTick();return state}}
    const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',name:'workflows',component:{render:()=>null}}]})
    const html=await renderToString(createSSRApp({render:()=>h(Loaded,{runId:'run',context:value.retryContext})}).use(router))
    for (const text of ['第 1 集','failed-job','原消费输入 SHA','查看原装配任务','结果尚未确认','&lt;script&gt;']) assert.ok(html.includes(text),text)
    assert.ok(!html.includes('查看准备与调用'));assert.ok(!html.includes('<script>isolated failure'))
  } finally {await vite.close()}
})

test('retry component renders backend distinctions and paginated scope without claiming successful output regeneration',async()=>{
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  try{
    const {default:Panel}=await vite.ssrLoadModule('/src/components/ProductionRetryPreview.vue')
    const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
    let raw=retryPreviewFixture(),reads=0
    platformApi.getProductionRetryPreview=async()=>{reads++;return adaptRetryPreview(raw)}
    const Loaded={...Panel,async setup(props:any,ctx:any){const state=Panel.setup(props,ctx);await Promise.resolve();await nextTick();return state}}
    const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',name:'workflows',component:{render:()=>null}}]})
    const render=()=>renderToString(createSSRApp({render:()=>h(Loaded,{runId:'run',context:{expectedStageId:'stage',expectedAttemptIds:['failed']}})}).use(router))
    const html=await render()
    for(const text of ['可重试','跳过','需处理','共 3 项','尚未创建主调用','已有产物及选择保持不变','查看准备与调用'])assert.ok(html.includes(text),text)
    raw={...raw,can_submit:false,reason:'远端结果未知，不能重发。'}
    raw.results[0]!.message='<script>not executed</script>'
    const blocked=await render()
    assert.ok(blocked.includes('远端结果未知，不能重发。'));assert.ok(blocked.includes('&lt;script&gt;'));assert.ok(!blocked.includes('<script>not executed'))
    assert.equal(reads,2)
  }finally{await vite.close()}
})

test('mounted preview handles refresh, pagination, stale responses and scope changes using reads only',async()=>{
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  let app:any
  try{
    const {default:Panel}=await vite.ssrLoadModule('/src/components/ProductionRetryPreview.vue')
    const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
    const pending:{run:string,page:number,resolve:(v:unknown)=>void,reject:(v:unknown)=>void}[]=[]
    let writes=0
    platformApi.getProductionRetryPreview=(run:string,_stage:string,page:number)=>new Promise((resolve,reject)=>pending.push({run,page,resolve,reject}))
    platformApi.retryProductionRun=()=>{writes++;throw Error('Preview must not write')}
    const props=reactive({runId:'first',context:{expectedStageId:'stage',expectedAttemptIds:['failed']}}),ready:boolean[]=[]
    let state:any
    const Mounted={...Panel,setup(p:any,c:any){state=Panel.setup(p,c);return state},render:()=>null}
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({render:()=>h(Mounted,{...props,onReady:(value:boolean)=>ready.push(value)})})
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await nextTick()
    props.runId='second';await nextTick()
    pending[1]!.resolve(adaptRetryPreview(retryPreviewFixture()));await nextTick();await nextTick()
    assert.equal(ready.at(-1),true)
    pending[0]!.reject(Error('old read'));await nextTick();await nextTick()
    assert.equal(state.error.value,'');assert.equal(ready.at(-1),true)
    const next=state.load(2);assert.equal(ready.at(-1),false)
    const paged={...retryPreviewFixture(),results:[retryPreviewFixture().results[2]],previous:'/?page=1'}
    pending[2]!.resolve(adaptRetryPreview(paged));await next
    assert.equal(state.page.value,2);assert.equal(pending[2]!.page,2)
    const refresh=state.load();pending[3]!.resolve(adaptRetryPreview({...retryPreviewFixture(),retry_context:{expected_stage_id:'stage',expected_attempt_ids:['changed']}}));await refresh
    assert.equal(ready.at(-1),false);assert.match(state.error.value,/范围已变化/)
    const missing=state.load();pending[4]!.reject(Error('offline'));await missing
    assert.equal(state.data.value,null);assert.equal(ready.at(-1),false)
    const after=state.load();app.unmount();app=null;pending[5]!.resolve(adaptRetryPreview(retryPreviewFixture()));await after
    assert.equal(ready.at(-1),false);assert.equal(writes,0)
    const page=readFileSync(new URL('../src/views/ProductionRunView.vue',import.meta.url),'utf8')
    assert.ok(page.includes('<ProductionRetryPreview'));assert.ok(page.includes('!retryPreviewReady.value'))
    assert.ok(page.includes('platformApi.retryProductionRun(pending.runId, context)'))
  }finally{app?.unmount();await vite.close()}
})
