import assert from 'node:assert/strict'
import test from 'node:test'
import {createPinia,setActivePinia} from 'pinia'
Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=>null,setItem(){},removeItem(){}}})
import {createServer} from 'vite'
import {createSSRApp,createRenderer,h,reactive,ref,nextTick,ssrContextKey} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {matchedRouteKey} from 'vue-router'

async function setup(context:any){setActivePinia(createPinia());const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});context.after(()=>vite.close());return vite}
const row={id:'out-id',output_key:'out-key',version:1,revision:2,name:'通用输出',description:'JSON',parser_key:'structured_json.v1',data_structure_version:'structure-id',status:'draft',contract_sha256:'a'.repeat(64),quality_advisories:{},parser_contract:{key:'structured_json.v1',format:'json'}}
const catalog=[{key:row.parser_key,label:'通用JSON',description:'配置字段结构',configuration:'structure'}]
const report={preview_only:true,output_version_id:row.id,output_revision:row.revision,output_contract_sha256:row.contract_sha256,structure_version_id:row.data_structure_version,parsed:true,valid:true,raw_value:null,value:null,json_schema:{type:'null'},fields:[{path:[],field_definition:{label:'内容',value_type:'text'}}],defaults:[],warnings:[],errors:{}}
async function componentState(component:any,props:any={},emit:any=()=>{}){
  let state:any
  const app=createSSRApp({setup(){state=component.setup(props,{expose:()=>{},emit});return()=>h('div')}})
  app.provide(matchedRouteKey,ref({leaveGuards:new Set(),updateGuards:new Set()}) as any)
  await renderToString(app);return state
}

test('generic output adapts its own contract and refuses missing/unknown protocol metadata',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/businessOutputs.ts')
  assert.equal(m.adaptBusinessOutput(row,true).id,row.id)
  for(const parser_contract of [{key:row.parser_key}, {...row.parser_contract,fields:[]},{key:'unknown',format:'json'}])assert.throws(()=>m.adaptBusinessOutput({...row,parser_contract},true))
  assert.deepEqual(m.adaptOutputParsers(catalog),catalog)
  for(const bad of [null,[],[...catalog,...catalog],[{...catalog[0],configuration:'unknown'}],[{...catalog[0],key:'unknown.v1'}],[{...catalog[0],configuration:'visual_rules'}],[{...catalog[0],key:'visual_rules.v2'}]])assert.throws(()=>m.adaptOutputParsers(bad))
  assert.throws(()=>m.outputVersionOptions({},row),'generic results must not become fixed visual fields')
})

test('generic preview submits exact saved identity, preserves false/null/invalid results and rejects mismatches',async context=>{
  const vite=await setup(context),{businessOutputs}=await vite.ssrLoadModule('/src/services/businessOutputs.ts'),{api}=await vite.ssrLoadModule('/src/services/api.ts')
  const calls:any[]=[];let response:any=report
  api.post=async(path:any,data:any)=>{calls.push({path,data});return {data:response}}
  for(const value of [null,false,0,'',[],{}]){response={...report,value,raw_value:value};assert.deepEqual((await businessOutputs.previewJson(row,'raw')).value,value)}
  assert.deepEqual(calls[0],{path:`/business-output-versions/${row.id}/preview-json/`,data:{response_text:'raw',expected_revision:2,expected_contract_sha256:row.contract_sha256}})
  response={...report,valid:false,parsed:true,value:{field:42},errors:{'response.field':['应为文本']}}
  assert.equal((await businessOutputs.previewJson(row,'raw')).valid,false)
  for(const changes of [{output_version_id:'other'},{output_revision:3},{structure_version_id:'other'},{output_contract_sha256:'b'.repeat(64)},{preview_only:false},{errors:{field:'bad'}},{errors:{field:['invalid']}},{defaults:[null]},{warnings:[null]},{defaults:[{path:'field',value:false,structure_version_id:'id'}]},{fields:[{}]},{parsed:false,valid:true},{value:undefined}]){
    response={...report,...changes};if('value' in changes)delete response.value
    await assert.rejects(businessOutputs.previewJson(row,'raw'))
  }
})

test('manager generic payload has only shared structure, tracks dirty state and rejects unsupported selection',async context=>{
  const vite=await setup(context),{businessOutputs}=await vite.ssrLoadModule('/src/services/businessOutputs.ts'),{default:Manager}=await vite.ssrLoadModule('/src/components/BusinessOutputManager.vue')
  const selected:any[]=[],state=await componentState(Manager,{choose:true,supportedParserKeys:['visual_rules.v2']},(...args:any[])=>selected.push(args))
  state.catalogReady.value=true;state.parsers.value=catalog;state.assign(null);Object.assign(state.form,{name:row.name,parser_key:row.parser_key,data_structure_version:row.data_structure_version,quality_advisories:{obsolete:true},allowed_values:{old:[]},field_sources:{old:'old'},response_keys:{old:'old'}})
  assert.deepEqual(state.payload(),{name:row.name,description:'',parser_key:row.parser_key,data_structure_version:row.data_structure_version,quality_advisories:{}})
  let saves=0;businessOutputs.save=async(data:any)=>{saves++;return {...row,...data,id:'created-output',revision:1}};businessOutputs.list=async()=>({count:1,results:[row],hasNext:false,hasPrevious:false});businessOutputs.options=async()=>({parsers:catalog})
  state.form.parser_key='unknown';await state.save();assert.equal(saves,0)
  state.form.parser_key=row.parser_key;await state.save();assert.equal(saves,1);assert.equal(state.dirty.value,false)
  state.form.data_structure_version='other';assert.equal(state.dirty.value,true)
  state.assign(row);businessOutputs.detail=async()=>({...row,status:'published'});await state.selectVersion(row);assert.equal(selected.length,0);assert.match(state.error.value,/不支持/)
  state.assign({...row,status:'retired'});assert.equal(state.readonly.value,true)
})

test('preview keeps sample on errors, invalidates edits/version changes and discards late responses',async context=>{
  const vite=await setup(context),{businessOutputs}=await vite.ssrLoadModule('/src/services/businessOutputs.ts'),{default:Preview}=await vite.ssrLoadModule('/src/components/StructuredOutputPreview.vue')
  const props=reactive({version:{...row},disabled:false});let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Preview.setup(props,{expose:()=>{}});return()=>h('div')}})
  app.provide(ssrContextKey,{});app.mount({});context.after(()=>app.unmount())
  const pending:any[]=[];businessOutputs.previewJson=()=>new Promise(resolve=>pending.push(resolve))
  state.edit('{broken');const first=state.preview();state.edit('false');pending.shift()(report);await first
  assert.equal(state.result.value,null);assert.equal(state.text.value,'false')
  const second=state.preview();props.version.revision++;await nextTick();pending.shift()(report);await second;assert.equal(state.result.value,null)
  businessOutputs.previewJson=async()=>{throw new Error('isolated failure')};await state.preview();assert.ok(state.error.value);assert.equal(state.text.value,'false');assert.equal(state.busy.value,false)
  businessOutputs.previewJson=async()=>report;await state.preview();assert.equal(state.result.value.valid,true)
  props.disabled=true;await nextTick();assert.equal(state.result.value,null);await state.preview();assert.equal(state.result.value,null)
})

test('preview renders explicit sample-only boundaries and editable original JSON',async context=>{
  const vite=await setup(context),{default:Preview}=await vite.ssrLoadModule('/src/components/StructuredOutputPreview.vue')
  const html=await renderToString(createSSRApp(Preview,{version:row}))
  assert.match(html,/样例JSON原文/);assert.match(html,/不会生成内容、创建任务或采用结果/);assert.match(html,/<textarea/)
  const disabled=await renderToString(createSSRApp(Preview,{version:row,disabled:true}));assert.match(disabled,/请先保存修改/);assert.match(disabled,/<button[^>]*disabled/)
})
