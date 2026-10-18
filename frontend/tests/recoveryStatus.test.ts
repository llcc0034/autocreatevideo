import assert from 'node:assert/strict'
import test from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createSSRApp,h} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory} from 'vue-router'

test('original task media cancellation remains cancellation in badge, progress, node and timeline',async context=>{
  const storage=Object.getOwnPropertyDescriptor(globalThis,'localStorage')
  Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=>null}})
  const root=fileURLToPath(new URL('..',import.meta.url))
  const vite=await createServer({root,configFile:false,envFile:false,plugins:[vue()],logLevel:'silent',appType:'custom',
    resolve:{alias:{'@':fileURLToPath(new URL('../src',import.meta.url))}},optimizeDeps:{noDiscovery:true},
    server:{middlewareMode:true,hmr:false,ws:false,watch:null}})
  context.after(async()=>{await vite.close();if(storage)Object.defineProperty(globalThis,'localStorage',storage);else Reflect.deleteProperty(globalThis,'localStorage')})
  const {statusInspector,statusTask}=await vite.ssrLoadModule('/tests/fixtures/recovery-status.ts')
  const {api}=await vite.ssrLoadModule('/src/services/api.ts')
  api.defaults.adapter=async()=>assert.fail('No business HTTP')
  context.mock.method(globalThis,'fetch',async()=>assert.fail('No fetch'))
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]})
  await router.push('/');await router.isReady()
  for(const status of ['cancelled','cancel_requested'] as const){
    let state:any
    const app=createSSRApp({render:()=>h(statusInspector(status,(s:any)=>{state=s}))}).use(createPinia()).use(router)
    const html=await renderToString(app)
    assert.equal(statusTask(status).status,status)
    assert.match(html,new RegExp(`is-${status}`))
    assert.doesNotMatch(html,/任务执行失败|视频生成任务已完成|重新创建重试任务|75%/)
    assert.equal(state.nodeStatusLabel(state.selected.value.nodes[0]),status==='cancelled'?'已取消':'取消中')
    assert.equal(state.nodeProgressWidth(state.selected.value.nodes[0]),'0%')
    assert.equal(state.selectedProgress.value.percent,undefined)
    assert.equal(state.isActiveTask(state.selected.value),status==='cancel_requested')
    if(status==='cancelled')assert.ok(state.logs.value.some((row:any)=>row.text==='任务已取消'))
    else assert.ok(!state.logs.value.some((row:any)=>/失败|完成|已取消/.test(row.text)))
  }
})
