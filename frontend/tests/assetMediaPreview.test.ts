import test from 'node:test'
import assert from 'node:assert/strict'
import {createServer} from 'vite'
import {createRenderer,createSSRApp,h,nextTick,reactive,ssrContextKey} from 'vue'
import {renderToString} from 'vue/server-renderer'
const settle=async()=>{for(let i=0;i<6;i++){await Promise.resolve();await nextTick()}}
const base={id:'script-a',type:'script',name:'原剧本 🟢',description:'原描述',styleId:'',tags:[],mediaUrl:'/script-a.md',thumbnail:''}
async function setup(t:any){
 const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});t.after(()=>vite.close())
 const {default:Component}=await vite.ssrLoadModule('/src/components/AssetMediaPreview.vue')
 const pending:any[]=[],events:any[]=[],originalFetch=globalThis.fetch
 globalThis.fetch=async(url,options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}));t.after(()=>{globalThis.fetch=originalFetch})
 const props=reactive({asset:{...base},compact:false,controls:true});let state:any
 const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
 const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(...args:any[])=>events.push(args)});return()=>h('div')}});app.provide(ssrContextKey,{modules:new Set()});app.mount({});t.after(()=>app.unmount());await settle()
 return{state,props,pending,events,Component}
}
test('script switches suppress late body text and report only current asset/source identity',async t=>{
 const {state,props,pending,events}=await setup(t);let body:any
 pending[0].resolve({ok:true,text:()=>new Promise(r=>body=r)});await settle()
 props.asset={...base,id:'script-b',mediaUrl:'/script-b.md'};await settle();assert.equal(pending[0].options.signal.aborted,true)
 pending[1].resolve({ok:true,text:async()=>'当前中文 🟠'});await settle();body('迟到旧稿');await settle()
 assert.equal(state.scriptText.value,'当前中文 🟠');assert.deepEqual(events,[['sourceLoaded',{assetId:'script-b',source:'/script-b.md'}]])
})
test('script errors remain visible, do not promise automatic recovery, and do not reuse old text',async t=>{
 const {state,props,pending,events}=await setup(t)
 pending[0].resolve({ok:true,text:async()=>'原稿'});await settle();props.asset={...base,mediaUrl:'/broken.md'};await settle()
 assert.equal(state.scriptText.value,'');pending[1].reject(Error('offline'));await settle()
 assert.match(state.scriptError.value,/不可读取/);assert.doesNotMatch(state.scriptError.value,/正在刷新/);assert.equal(state.loadingScript.value,false)
 assert.deepEqual(events.at(-1),['sourceError',{assetId:base.id,source:'/broken.md'}])
})
test('compact cards abort text requests and missing URL is explicitly distinct from empty content',async t=>{
 const {state,props,pending,events}=await setup(t);props.compact=true;await settle();assert.equal(pending.length,1)
 pending[0].reject(Error('late failure'));await settle();assert.equal(state.scriptError.value,'');assert.deepEqual(events,[])
 props.asset={...base,mediaUrl:''};props.compact=false;await settle();assert.equal(pending.length,1);assert.equal(state.scriptUnavailable.value,'未提供剧本预览地址')
})
test('audio/video render actual media URL over thumbnail; missing script shows a readable state',async t=>{
 const {Component}=await setup(t)
 for(const type of ['video','audio']){
  const html=await renderToString(createSSRApp(Component,{asset:{...base,type,mediaUrl:'/actual-media',thumbnail:'/poster.png'},controls:true}))
  assert.match(html,new RegExp(`<${type}[^>]*src="/actual-media"`));assert.match(html,/controls/);assert.doesNotMatch(html,/poster.png/)
 }
 const html=await renderToString(createSSRApp(Component,{asset:{...base,mediaUrl:''}}));assert.match(html,/未提供剧本预览地址/)
})
