import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'

const id=(n=1)=>`20000000-0000-0000-0000-${String(n).padStart(12,'0')}`
const payload=()=>({source:{shot_id:id(),structure_id:id(2),stage_id:id(3),episode_number:1,shot_number:9,
  document_sha256:'a'.repeat(64),materialization_sha256:'b'.repeat(64)},source_sha256:'c'.repeat(64),revision:null,fields:[{
  original:{structure_version_id:id(4),node_key:'custom',field_definition:{field_definition_id:id(5),field_definition_version_id:id(6),key:'custom',label:'原情绪',version:2},
    source_path:'source.shots[0].custom',value_path:['shots',0,'custom'],present:true,source_present:true,default_paths:[],value:' 原文 '},
  schema:{type:'string'},overridden:false,present:true,value:' 原文 '}]})
const revised=(request:any)=>({...payload(),revision:{id:id(10),revision:1,previous_id:null,source_sha256:'c'.repeat(64),content_sha256:'d'.repeat(64),created_by:1,created_at:'2026-10-03T12:00:00Z'},
  fields:payload().fields.map(row=>({...row,overridden:true,value:request.value}))})
const settle=async()=>{for(let i=0;i<8;i++){await Promise.resolve();await nextTick()}}
async function harness(fn:any){
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  const {default:C}=await vite.ssrLoadModule('/src/components/ShotFieldRevisions.vue')
  const {default:Editor}=await vite.ssrLoadModule('/src/components/StructuredJsonEditor.vue')
  const {shotFieldRevisionsApi:api}=await vite.ssrLoadModule('/src/services/shotFieldRevisionsApi.ts')
  const {readShotFieldRevision:parse}=await vite.ssrLoadModule('/src/services/shotFieldRevisions.ts')
  const {api:http}=await vite.ssrLoadModule('/src/services/api.ts')
  const props=reactive({projectId:id(9),shotId:id()});let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=C.setup(props,{expose(){}});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({})
  try{await fn({state,api,http,props,parse,Editor})}finally{app.unmount();await vite.close()}
}
test('typed current and original values stay separate, transport refuses corrupt metadata or missing value',()=>harness(async({parse})=>{
  for(const value of [null,'',0,false,[],{},' 原文\n']){
    const result=revised({value});assert.equal(parse(result,id()).fields[0].value,value)
    assert.equal(parse(result,id()).fields[0].original.value,' 原文 ')
  }
  for(const change of [(v:any)=>v.revision=null,(v:any)=>delete v.fields[0].value,(v:any)=>v.source.shot_id=id(99),(v:any)=>v.fields[0].schema=null]){
    const v=revised({value:'text'});change(v);assert.throws(()=>parse(v,id()))
  }
}))

test('revision values use the compiler nullable anyOf schema without replacing null or missing',()=>harness(async({parse,Editor})=>{
  const source=payload() as any
  source.fields[0].schema={anyOf:[{type:'string'},{type:'null'}],'x-node-key':'custom'}
  source.fields[0].value=null;source.fields[0].original.value=null
  const row=parse(source,id()).fields[0];let errors:any
  const html=await renderToString(createSSRApp(Editor,{modelValue:row.value,label:'独立字段值',schemas:[row.schema],onValidity:(v:any)=>errors=v}))
  assert.deepEqual(errors,[]);assert.match(html,/明确传空值/);assert.match(html,/value="string"/);assert.match(html,/value="null"/)
  assert.doesNotMatch(html,/role="alert"/);assert.equal(row.value,null)
  const missing=await renderToString(createSSRApp(Editor,{modelValue:undefined,label:'独立字段值',schemas:[{type:'string'}]}))
  assert.match(missing,/未提供（缺失）/);assert.match(missing,/填写此项/)
}))
test('lazy API uses exact shot and revision, invalid write receipt is an unknown result',()=>harness(async({api,http})=>{
  const paths:any[]=[];http.get=async(...args:any[])=>{paths.push(args);return{data:revised({value:'history'})}}
  await api.read(id(),id(10));assert.deepEqual(paths,[[`/shots/${id()}/field-revisions/`,{params:{revision_id:id(10)}}]])
  await assert.rejects(()=>api.read('unsaved'))
  await assert.rejects(()=>api.read(id(),id(99)))
  http.post=async()=>({data:{detail:payload(),replayed:false}})
  await assert.rejects(()=>api.write(id(),{source_sha256:'c'.repeat(64)}))
}))
test('unknown save freezes original request, repeat returns exact receipt and requires explicit current refresh',()=>harness(async({state,api})=>{
  let reads=0;const writes:any[]=[];api.read=async()=>{reads++;return payload()}
  assert.equal(reads,0);await state.load();state.selected.value='0';state.choose();state.draft.value=' 保留空格\n';state.dirty.value=true
  api.write=async(_id:string,request:any)=>{writes.push(structuredClone(JSON.parse(JSON.stringify(request))));if(writes.length===1)throw Error('回执丢失');return{detail:revised(request),replayed:true}}
  await state.save();assert.equal(state.locked.value,true);assert.ok(state.pending.value);await state.load();assert.equal(reads,1)
  state.draft.value='不可替换已发请求';await state.save()
  assert.deepEqual(writes[1],writes[0]);assert.equal(writes[1].value,' 保留空格\n')
  assert.equal(state.detail.value.fields[0].value,' 保留空格\n');assert.equal(state.pending.value,null);assert.equal(state.historical.value,true)
  await state.save();assert.equal(writes.length,2);await state.load();assert.equal(state.historical.value,false)
}))
test('definitive conflict keeps draft but does not retry; client validation prevents invalid writes',()=>harness(async({state,api})=>{
  api.read=async()=>payload();await state.load();state.selected.value='0';state.choose();state.draft.value='待核对草稿';state.dirty.value=true
  let writes=0;api.write=async()=>{writes++;throw{response:{status:409,data:{error:{message:'修订冲突'}}}}}
  state.errors.value=['类型无效'];await state.save();assert.equal(writes,0);state.errors.value=[]
  await state.save();assert.equal(writes,1);assert.equal(state.pending.value,null);assert.equal(state.stale.value,true);assert.equal(state.draft.value,'待核对草稿')
  await state.save();assert.equal(writes,1)
}))
test('history is read-only; scope changes discard late read/write responses; unsaved scope never queries',()=>harness(async({state,api,props})=>{
  api.read=async()=>revised({value:'history'});await state.load(id(10));state.selected.value='0';state.choose()
  let writes=0;api.write=async()=>{writes++;return{detail:revised({value:'write'}),replayed:false}}
  await state.save();assert.equal(writes,0)
  let resolve!:(v:any)=>void;api.read=()=>new Promise(r=>resolve=r)
  const loading=state.load();props.projectId=id(99);await settle();resolve(payload());await loading;assert.equal(state.detail.value,null)
  api.read=async()=>payload();await state.load();state.selected.value='0';state.choose();state.draft.value='draft'
  api.write=()=>new Promise(r=>resolve=r);const saving=state.save();props.shotId='shot-new';await settle();resolve({detail:revised({value:'late'}),replayed:false});await saving
  assert.equal(state.detail.value,null);assert.equal(state.notice.value,'');await state.load();assert.equal(state.detail.value,null)
}))
