import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createRenderer,h,nextTick,reactive,ssrContextKey} from 'vue'

test('probe deep link reads exact identity and rejects missing, mismatched and late responses without writes',async t=>{
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
 const storage=globalThis.localStorage;globalThis.localStorage={getItem:()=>null} as any
 const {createPinia}=await import('pinia')
 const {default:Panel}=await vite.ssrLoadModule('/src/components/RuntimeProbeDetail.vue')
 const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
 const first='00000000-0000-4000-8000-000000000001',second='00000000-0000-4000-8000-000000000002'
 const props=reactive({id:first}),reads:string[]=[];let state:any,resolve!:(v:any)=>void,mode='ok'
 t.mock.method(platformApi,'getRuntimeHostProbeAttempt',async(id:string)=>{reads.push(id);if(mode==='missing')throw Error('404 missing');if(mode==='late')return new Promise(r=>{resolve=r});return {id:mode==='mismatch'?second:id,status:'running'}})
 t.mock.method(platformApi,'listRuntimeHostProbeAttempts',async()=>{throw Error('must not substitute latest')})
 for(const name of ['createRuntimeHostProbe','cancelRuntimeHostProbeAttempt'])t.mock.method(platformApi,name,async()=>{throw Error('read must not write')})
 const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
 const app=renderer.createApp({setup(){state=Panel.setup(props,{expose(){}});return()=>h('div')}})
 t.after(async()=>{app.unmount();globalThis.localStorage=storage;await vite.close()})
 app.use(createPinia());app.provide(ssrContextKey,{modules:new Set()});app.mount({})
 const settle=async()=>{for(let i=0;i<10;i++){await Promise.resolve();await nextTick()}}
 await settle();assert.equal(state.attempt.value.id,first)
 mode='missing';await state.refresh();assert.equal(state.attempt.value,null);assert.match(state.error.value,/404 missing/)
 mode='mismatch';await state.refresh();assert.equal(state.attempt.value,null);assert.match(state.error.value,/不一致/)
 mode='late';const late=state.refresh();mode='ok';props.id=second;await settle();resolve({id:first});await late
 assert.equal(state.attempt.value.id,second);assert.equal(reads.at(-1),second)
 props.id='invalid';await settle();assert.equal(state.attempt.value,null);assert.match(state.error.value,/标识无效/)
})
