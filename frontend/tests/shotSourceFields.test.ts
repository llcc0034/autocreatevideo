import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { isSavedShotId, readShotSourceFields } from '../src/services/shotSourceFields.ts'

const id = (n=1) => `20000000-0000-0000-0000-${String(n).padStart(12,'0')}`
const row = () => ({structure_version_id:id(4),node_key:'custom',field_definition:{field_definition_id:id(5),field_definition_version_id:id(6),key:'custom',label:'自定义字段',version:2},source_path:'source.shots[0].custom',value_path:['shots',0,'custom'],present:true,source_present:true,default_paths:[],value:' 原文 '})
const payload = () => ({source:{shot_id:id(),structure_id:id(2),stage_id:id(3),episode_number:1,shot_number:9,document_sha256:'a'.repeat(64),materialization_sha256:'b'.repeat(64)},fields:[row()]})
test('original types, whitespace, exact identities and missing-vs-null survive transport validation', () => {
  const value:any=payload()
  value.fields=[null,'',0,false,[],{},' 原文\n'].map(v=>({...row(),value:v}))
  value.fields.push({...row(),present:false,source_present:false});delete value.fields.at(-1).value
  value.fields.push({...row(),source_present:false,default_paths:['source.shots[0].custom']})
  assert.equal(readShotSourceFields(value,id()),value)
  assert.ok(!Object.hasOwn(value.fields.at(-2),'value'))
  assert.equal(isSavedShotId('shot-unsaved'),false)
})
test('wrong shot, incomplete provenance and inconsistent presence never become an empty success', () => {
  for(const change of [(v:any)=>v.source.shot_id=id(99),(v:any)=>v.source.document_sha256='',(v:any)=>delete v.fields[0].value,(v:any)=>v.fields[0].present=false,(v:any)=>v.fields[0].field_definition.version=0,(v:any)=>v.fields[0].value_path=[-1],(v:any)=>v.fields=null]){
    const value=payload();change(value);assert.throws(()=>readShotSourceFields(value,id()))
  }
  assert.equal(readShotSourceFields({...payload(),fields:[]},id()).fields.length,0)
})
const settle=async()=>{for(let i=0;i<8;i++){await Promise.resolve();await nextTick()}}
async function harness(fn:any){
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  const {default:C}=await vite.ssrLoadModule('/src/components/ShotSourceFields.vue')
  const {shotSourceFieldsApi:api}=await vite.ssrLoadModule('/src/services/shotSourceFieldsApi.ts')
  const {api:http}=await vite.ssrLoadModule('/src/services/api.ts')
  const props=reactive({projectId:id(9),shotId:id()});let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=C.setup(props,{expose(){}});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({})
  try{await fn({state,api,http,props})}finally{app.unmount();await vite.close()}
}
test('transport uses only the exact shot GET and rejects invalid IDs or mismatched bodies',()=>harness(async({api,http})=>{
  const calls:string[]=[];http.get=async(path:string)=>{calls.push(path);return {data:payload()}}
  assert.equal((await api.read(id())).source.shot_id,id())
  assert.deepEqual(calls,[`/shots/${id()}/source-fields/`])
  await assert.rejects(()=>api.read('shot-unsaved'));assert.equal(calls.length,1)
  http.get=async()=>({data:{...payload(),source:{...payload().source,shot_id:id(99)}}})
  await assert.rejects(()=>api.read(id()))
}))
test('detail loads only on request; errors clear stale values; unsaved shots never query',()=>harness(async({state,api,props})=>{
  const calls:string[]=[];api.read=async(id:string)=>{calls.push(id);return payload()}
  assert.equal(calls.length,0);await state.load();assert.deepEqual(calls,[id()]);assert.equal(state.detail.value.fields[0].value,' 原文 ')
  api.read=async()=>{throw Error('原结构化来源损坏')};await state.load();assert.match(state.error.value,/损坏/);assert.equal(state.detail.value,null)
  props.shotId='shot-new';await settle();await state.load();assert.equal(state.open.value,false);assert.equal(calls.length,1)
}))
test('late responses after shot/project changes cannot display data from the previous scope',()=>harness(async({state,api,props})=>{
  for(const key of ['shotId','projectId']){
    let resolve!:(v:any)=>void;api.read=()=>new Promise(r=>resolve=r)
    const pending=state.load();props[key]=id(99);await settle();resolve(payload());await pending
    assert.equal(state.detail.value,null);assert.equal(state.error.value,'');assert.equal(state.open.value,false)
    props.shotId=id();await settle()
  }
}))
