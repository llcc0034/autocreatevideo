import assert from 'node:assert/strict'
import test from 'node:test'
import { readShotTiming, shotTimingPayload, timingMilliseconds } from '../src/services/shotTiming.ts'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'

test('45–50 seconds is an episode position with 5 seconds of generation', () => {
  const timing = readShotTiming({episode_start_ms:45000,episode_end_ms:50000},5)
  assert.deepEqual(shotTimingPayload({duration:5,...timing}),{episode_start_ms:45000,episode_end_ms:50000})
  assert.deepEqual(shotTimingPayload({duration:5}),{episode_start_ms:null,episode_end_ms:null})
})
test('time fields fail explicitly, without invented start or truncated precision', () => {
  for (const row of [{episode_start_ms:45000,episode_end_ms:null},{episode_start_ms:50000,episode_end_ms:45000},
    {episode_start_ms:45000,episode_end_ms:51000},{}, {episode_start_ms:true,episode_end_ms:5001}]) {
    assert.throws(() => readShotTiming(row,5))
  }
  assert.equal(timingMilliseconds(1.001),1001)
  assert.throws(() => timingMilliseconds(1.0001))
  assert.deepEqual(shotTimingPayload({duration:1.001,episodeStartMs:0,episodeEndMs:1001}),{episode_start_ms:0,episode_end_ms:1001})
})

test('editor keeps absolute episode times separate and emits only derived shot duration', async () => {
  const vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  const props=reactive({shot:{duration:5,episodeStartMs:45000 as number|null,episodeEndMs:50000 as number|null},minimumSeconds:1,maximumSeconds:10})
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  let state:any,app:any
  try {
    const {default:Component}=await vite.ssrLoadModule('/src/components/ShotTimingEditor.vue')
    app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(name,value)=>{assert.equal(name,'change');props.shot=value}});return()=>h('div')}})
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await nextTick()
    let html=await renderToString(createSSRApp({...Component,setup:()=>state},props))
    for (const label of ['本集开始时间（秒）','本集结束时间（秒）','不是源视频截取区间','由起止时间计算']) assert.ok(html.includes(label),label)
    state.update('episodeEndMs',{target:{value:'51.001'}});await nextTick()
    assert.equal(props.shot.duration,6.001)
    assert.deepEqual(shotTimingPayload(props.shot),{episode_start_ms:45000,episode_end_ms:51001})
    state.mode({target:{value:'duration'}});await nextTick()
    state.update('duration',{target:{value:'2.5'}});await nextTick()
    assert.deepEqual(props.shot,{duration:2.5,episodeStartMs:null,episodeEndMs:null})
    html=await renderToString(createSSRApp({...Component,setup:()=>state},props))
    assert.ok(!html.includes('本集开始时间（秒）'))
  } finally {app?.unmount();await vite.close()}
})
