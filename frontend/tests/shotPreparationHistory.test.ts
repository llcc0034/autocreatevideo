import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createRenderer, h, nextTick, reactive, ssrContextKey} from 'vue'

const row=(id='prep')=>({id,kind:'preparation',project_id:'project',production_run_id:'run'})
const page=(rows=[row()],next:string|null=null)=>({results:rows,next,previous:null})
const settle=async()=>{for(let i=0;i<8;i++){await Promise.resolve();await nextTick()}}
async function harness(fn:any){
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  const {default:C}=await vite.ssrLoadModule('/src/components/ShotPreparationHistory.vue')
  const {taskCenterApi:api}=await vite.ssrLoadModule('/src/services/taskCenterApi.ts')
  const props=reactive({projectId:'project',shotId:'shot'})
  let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=C.setup(props,{expose(){}});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({})
  try{await fn({state,api,props,app})}finally{app.unmount();await vite.close()}
}
test('history is lazy, exact-unit scoped and uses original cursor without following foreign URLs',()=>harness(async({state,api})=>{
  const reads:any[]=[];api.list=async(filters:any)=>{reads.push(filters);return page([row()], 'https://foreign.example/task/?cursor=opaque')}
  assert.equal(state.open.value,false);assert.equal(reads.length,0)
  await state.load();assert.deepEqual(reads[0],{category:'production',project_id:'project',unit_key:'shot:shot',page_size:10})
  assert.equal(state.selected.value,null);state.choose('preparation','prep','attempt');assert.deepEqual(state.selected.value,{id:'prep',attemptId:'attempt'})
  await state.load(state.nextCursor.value);assert.equal(reads[1].cursor,'opaque');assert.equal(state.selected.value,null)
  state.choose('preparation','foreign');assert.match(state.error.value,/不离开本镜/);assert.equal(state.selected.value,null)
}))
test('invalid scope, pagination and failed reads are explicit, not empty success or stale selectable rows',()=>harness(async({state,api})=>{
  for(const result of [page([{...row(),project_id:'other'}]),page([{...row(),kind:'assembly'}]),page([row()],'/missing-cursor')]){
    api.list=async()=>result;await state.load();assert.ok(state.error.value);assert.deepEqual(state.rows.value,[])
  }
  api.list=async()=>page();await state.load();assert.equal(state.rows.value.length,1)
  api.list=async()=>{throw Error('offline')};await state.load();assert.match(state.error.value,/offline/);assert.deepEqual(state.rows.value,[])
}))
test('late reads after shot change are ignored; reading a body locks record selection and refresh',()=>harness(async({state,api,props})=>{
  let resolve!:(v:any)=>void,calls=0;api.list=async()=>{calls++;return new Promise(r=>resolve=r)}
  const pending=state.load();props.shotId='other-shot';await settle();resolve(page());await pending
  assert.equal(state.open.value,false);assert.deepEqual(state.rows.value,[])
  state.busy.value=true;await state.load();state.choose('preparation','prep');assert.equal(calls,1);assert.equal(state.selected.value,null)
}))
