import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createRenderer,createSSRApp,h,nextTick,reactive,ssrContextKey} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {readFile} from 'node:fs/promises'
import {scriptFixture} from './fixtures/script-semantics-data.ts'

async function setup(t:any){const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});t.after(()=>vite.close());return vite}
test('semantic guide follows renamed and whole-object mappings, not response key guesses',async t=>{
  const vite=await setup(t),{scriptSemanticGuideGroups}=await vite.ssrLoadModule('/src/services/scriptSemanticGuide.ts')
  const rows=(mapping:any)=>scriptSemanticGuideGroups(scriptFixture.options,mapping).flatMap((group:any)=>group.rows)
  const original=structuredClone(scriptFixture.layouts.b.mapping),nested=rows(original)
  assert.equal(nested.find((r:any)=>r.path==='entities[*].name').source,'（响应.world.cast）[*].label')
  assert.equal(nested.find((r:any)=>r.path==='entities[*].primary.reference_name').source,'（响应.world.cast）[*].looks.base.reference_name')
  assert.equal(nested.find((r:any)=>r.path==='entities[*].variants[*].reference_name').source,'（响应.world.cast）[*].looks.alternatives[*].reference_name')
  assert.match(nested.find((r:any)=>r.path==='episodes[*].shots[*].dialogue.text').source,/chapters.*frames.*speech\.text/)
  assert.deepEqual(original,scriptFixture.layouts.b.mapping)
  const direct=rows({node:{kind:'path',scope:'root',path:[]}})
  assert.equal(direct.find((r:any)=>r.path==='entities[*].name').source,'响应.entities[*].name')
  assert.match(rows({node:{kind:'object',fields:{}}})[0].source,/未映射/)
  const escaped=rows({node:{kind:'path',scope:'root',path:['*','含.点',0]}})
  assert.equal(escaped[0].source,'响应["*"]["含.点"][0].entities[*].key')
  for(const kind of ['literal','lookup','array','unknown'])assert.doesNotMatch(rows({node:{kind,value:{}}})[0].source,/^响应/)
})

test('read-only guide loads only catalog, keeps mapping, retries and ignores stale loads',async t=>{
  const vite=await setup(t),{scriptSemantics}=await vite.ssrLoadModule('/src/services/scriptSemantics.ts'),{default:Guide}=await vite.ssrLoadModule('/src/components/ScriptSemanticGuide.vue')
  const pending:any[]=[];scriptSemantics.options=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}))
  const original=structuredClone(scriptFixture.layouts.a.mapping),props=reactive({mapping:structuredClone(original),options:undefined as any})
  let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Guide.setup(props,{expose(){}});return()=>h('div')}})
  app.provide(ssrContextKey,{});app.mount({});t.after(()=>app.unmount())
  assert.equal(state.loading.value,true)
  const retry=state.load();pending[0].resolve(scriptFixture.options);await nextTick();assert.equal(state.catalog.value,null)
  pending[1].reject(new Error('隔离目录读取失败'));await retry;assert.ok(state.error.value);assert.equal(state.catalog.value,null)
  const recover=state.load();pending[2].resolve(scriptFixture.options);await recover;assert.ok(state.catalog.value);assert.equal(state.error.value,'')
  props.options=scriptFixture.options;await nextTick();await nextTick();assert.equal(pending.length,3)
  assert.deepEqual(props.mapping,original)
  props.mapping=structuredClone(scriptFixture.layouts.b.mapping);await nextTick()
  assert.equal(state.groups.value[0].rows.find((row:any)=>row.path==='entities[*].name').source,'（响应.world.cast）[*].label')
})

test('shared guide explains configurable vs fixed roles, not actual production or translation',async t=>{
  const vite=await setup(t),{default:Guide}=await vite.ssrLoadModule('/src/components/ScriptSemanticGuide.vue')
  const html=await renderToString(createSSRApp(Guide,{mapping:scriptFixture.layouts.b.mapping,options:scriptFixture.options,draft:true}))
  for(const text of ['哪些可以配置','Canonical','不是另一次翻译','配置来源','响应.world.cast','本次生成结果','当前草稿映射','不会调用模型']){
    if(text==='本次生成结果')assert.match(html,/不是某次生成结果/)
    else assert.ok(html.includes(text),text)
  }
  assert.doesNotMatch(html,/<textarea|<input|<select/)
  const definition=await readFile('src/components/BusinessOutputDefinitionView.vue','utf8'),editor=await readFile('src/components/ScriptSemanticOutputEditor.vue','utf8'),manager=await readFile('src/components/BusinessOutputManager.vue','utf8')
  assert.match(definition,/ScriptSemanticGuide v-if="definition.parser_key==='script_semantics.v1'"/)
  assert.match(editor,/ScriptSemanticGuide v-if="options&&!hideGuide"/)
  assert.match(manager,/disabled hide-guide/)
})
