import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { adaptBulkFieldPreview, adaptBulkFieldResult, bulkFieldReceipts } from '../src/services/executionFieldBulkReview.ts'

const row = (id = 'prep') => ({preparation_id:id, expected_preparation_revision:8, preview_sha256:'a'.repeat(64), label:'第1集 · 镜头2', ready:true, reason:'', fields:[
  {unit_key:'shot:1',field_key:'direction',label:'导演描述',state:'selected',reason:'保持不变',value_id:'v0',revision:null,text:'',warnings:[]},
  {unit_key:'shot:1',field_key:'sound',label:'总体声景',state:'candidate',reason:'',value_id:'v1',revision:1,text:'<script>not executable</script> Sound.',warnings:['建议长度提醒']},
]})
test('preview checks exact scope, revision, fingerprint, fields and readiness',()=>{
  assert.equal(adaptBulkFieldPreview({items:[row()]},['prep'])[0].fields[1].text,row().fields[1].text)
  for(const bad of [{...row(),preparation_id:'other'},{...row(),expected_preparation_revision:0},
    {...row(),preview_sha256:'bad'},{...row(),fields:[]},{...row(),fields:[{...row().fields[1],state:'blocked'}]},
    {...row(),fields:[row().fields[1],row().fields[1]]}]) assert.throws(()=>adaptBulkFieldPreview({items:[bad]},['prep']))
  assert.throws(()=>adaptBulkFieldPreview({items:[row(),row()]},['prep','prep']))
})
test('confirmation sends explicit ready shot receipts only, not text or replace flags',()=>{
  const items=adaptBulkFieldPreview({items:[row(),{...row('blocked'),ready:false,reason:'需单独处理'}]},['prep','blocked'])
  assert.deepEqual(bulkFieldReceipts(items,['prep']),[{preparation_id:'prep',expected_preparation_revision:8,preview_sha256:'a'.repeat(64)}])
  for(const ids of [[],['blocked'],['prep','prep'],['missing']]) assert.throws(()=>bulkFieldReceipts(items,ids))
})
test('success requires authoritative receipts for every requested shot',()=>{
  const receipts=bulkFieldReceipts(adaptBulkFieldPreview({items:[row()]},['prep']),['prep'])
  assert.equal(adaptBulkFieldResult({confirmed_field_count:1,items:[{preparation_id:'prep',revision:10}]},receipts),1)
  for(const bad of [{confirmed_field_count:0,items:[]},{confirmed_field_count:1,items:[{preparation_id:'other',revision:10}]},
    {confirmed_field_count:1,items:[{preparation_id:'prep',revision:8}]}]) assert.throws(()=>adaptBulkFieldResult(bad,receipts))
})
const root=fileURLToPath(new URL('..',import.meta.url))
const setup=()=>createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
const renderer=()=>createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
test('panel is preview-only until explicit acknowledgement; stale submission cannot silently retry',async()=>{
  const vite=await setup();let app:any
  try{
    const {executionPreparationApi:api}=await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel=(await vite.ssrLoadModule('/src/components/ExecutionFieldBulkReview.vue')).default
    const calls:any[]=[],events:any[]=[];let fail=true
    api.bulkFieldPreview=async()=>adaptBulkFieldPreview({items:[row()]},['prep'])
    api.bulkFieldConfirm=async(items:any)=>{calls.push(items);if(fail)throw Error('409 changed');return 1}
    let state:any
    app=renderer().createApp({setup(){state=Panel.setup({preparationIds:['prep']},{emit:(...e:any[])=>events.push(e),expose(){}});return()=>h('div')}})
    app.provide(ssrContextKey,{});app.mount({});await nextTick();await nextTick()
    await state.confirm();assert.equal(calls.length,0)
    state.acknowledged.value=true;await state.confirm()
    assert.equal(calls.length,1);assert.equal(state.stale.value,true)
    assert.equal(events.some(e=>e[0]==='confirmed'),false)
    state.acknowledged.value=true;await state.confirm();assert.equal(calls.length,1)
    fail=false;await state.load();await nextTick();state.acknowledged.value=true;await state.confirm()
    assert.equal(calls.length,2);assert.equal(events.find(e=>e[0]==='confirmed')[1],1)
  }finally{app?.unmount();await vite.close()}
})
test('changing shot scope discards slow prior responses and resets acknowledgement',async()=>{
  const vite=await setup();let app:any
  try{
    const {executionPreparationApi:api}=await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    const Panel=(await vite.ssrLoadModule('/src/components/ExecutionFieldBulkReview.vue')).default
    const requests:any[]=[];api.bulkFieldPreview=(ids:any)=>new Promise(resolve=>requests.push({ids,resolve}))
    let state:any;const props=reactive({preparationIds:['prep']})
    app=renderer().createApp({setup(){state=Panel.setup(props,{emit(){},expose(){}});return()=>h('div')}})
    app.provide(ssrContextKey,{});app.mount({});await nextTick()
    props.preparationIds=['other'];await nextTick()
    requests[0].resolve(adaptBulkFieldPreview({items:[row()]},['prep']));await nextTick();await nextTick()
    assert.equal(state.items.value.length,0)
    requests[1].resolve(adaptBulkFieldPreview({items:[row('other')]},['other']));await nextTick();await nextTick()
    assert.equal(state.items.value[0].preparation_id,'other');assert.equal(state.acknowledged.value,false)
  }finally{app?.unmount();await vite.close()}
})
test('review panel shows versions, unchanged selections, warnings and escaped full text',async()=>{
  const vite=await setup()
  const oldDocument=globalThis.document,oldElement=globalThis.HTMLElement
  Object.assign(globalThis,{document:{activeElement:null},HTMLElement:class{}})
  try{
    const {executionPreparationApi:api}=await vite.ssrLoadModule('/src/services/executionPreparationApi.ts')
    api.bulkFieldPreview=()=>new Promise(()=>{})
    const Panel=(await vite.ssrLoadModule('/src/components/ExecutionFieldBulkReview.vue')).default
    const Loaded={...Panel,setup(p:any,c:any){const state=Panel.setup(p,c);state.items.value=adaptBulkFieldPreview({items:[row()]},['prep']);state.loading.value=false;state.selected.value=['prep'];return state}}
    const context:any={};const html=await renderToString(createSSRApp({render:()=>h(Loaded,{preparationIds:['prep']})}),context)
    const output=html+JSON.stringify(context.teleports)
    for(const text of ['第1集','已采用，保持不变','修订 1','建议长度提醒','查看本次正文','&lt;script&gt;','不直接提交模型调用','可能继续执行并产生费用'])assert.ok(output.includes(text),text)
    assert.ok(!output.includes('<script>not executable'))
  }finally{await nextTick();Object.assign(globalThis,{document:oldDocument,HTMLElement:oldElement});await vite.close()}
})
test('single-shot and multi-shot entries share review component and page-scoped selection',async()=>{
  const workbench=await readFile(new URL('../src/components/ExecutionPreparationWorkbench.vue',import.meta.url),'utf8')
  const stage=await readFile(new URL('../src/components/StageExecutionPreparations.vue',import.meta.url),'utf8')
  assert.match(workbench,/本镜字段一键确认并采用/);assert.match(stage,/批量确认并采用/)
  for(const source of [workbench,stage])assert.match(source,/<ExecutionFieldBulkReview/)
  assert.match(stage,/翻页清空选择/);assert.match(stage,/checkedIds.value = \[\]/)
})
