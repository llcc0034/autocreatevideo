import assert from 'node:assert/strict'
import test from 'node:test'
import {createPinia,setActivePinia} from 'pinia'
Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=>null,setItem(){},removeItem(){}}})
import {createServer} from 'vite'
import {createSSRApp,createRenderer,h,reactive,ref,nextTick,ssrContextKey} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {matchedRouteKey} from 'vue-router'

async function setup(context:any){setActivePinia(createPinia());const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});context.after(()=>vite.close());return vite}
const mapping={schema_version:1,node:{kind:'path',scope:'root',path:[]}}
const row={id:'output-script',output_key:'script',version:1,revision:1,name:'剧本映射',description:'',parser_key:'script_semantics.v1',status:'draft',data_structure_version:'source',contract_sha256:'a'.repeat(64),quality_advisories:{},parser_contract:{key:'script_semantics.v1',format:'json',mapping}}
const options={key:'script_semantics.v1',json_schema:{type:'object',properties:{entities:{type:'array',items:{type:'object'}}}},mapping:{schema_version:1,nodes:[{value:'path',label:'路径'}],scopes:[{value:'root',label:'根'}]}}
const report={preview_only:true,validation_scope:'structure_and_mapping',output_version_id:row.id,output_revision:1,output_contract_sha256:row.contract_sha256,configuration_sha256:'b'.repeat(64),source_structure_version_id:row.data_structure_version,parsed:true,valid:true,raw_value:{extra:0},source_value:{extra:0},value:{entities:[]},source_fields:[],defaults:[],provenance:[],warnings:[],errors:{}}

test('script protocol metadata, options and exact preview envelope are validated',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/scriptSemantics.ts'),b=await vite.ssrLoadModule('/src/services/businessOutputs.ts')
  assert.equal(b.adaptBusinessOutput(row,true).id,row.id)
  assert.deepEqual(b.adaptOutputParsers([{key:row.parser_key,label:'剧本',description:'配置',configuration:'script_semantics'}])[0].key,row.parser_key)
  assert.deepEqual(m.adaptScriptSemanticOptions(options),options)
  for(const bad of [null,{...options,key:'other'},{...options,mapping:{}},{...options,mapping:{...options.mapping,nodes:[{}]}}])assert.throws(()=>m.adaptScriptSemanticOptions(bad))
  for(const parser of [{...row.parser_contract,mapping:null},{...row.parser_contract,fields:[]}])assert.throws(()=>b.adaptBusinessOutput({...row,parser_contract:parser},true))
  for(const changes of [{output_version_id:'other'},{output_revision:2},{output_contract_sha256:'e'.repeat(64)},{source_structure_version_id:'other'},{validation_scope:'materialized'},{configuration_sha256:''},{parsed:false},{errors:{field:['bad']}},{source_value:undefined},{provenance:[null]},{warnings:null}]){
    const value:any={...report,...changes};if('source_value' in changes)delete value.source_value
    assert.throws(()=>m.adaptScriptSemanticPreview(value,row))
  }
  assert.equal(m.adaptScriptSemanticPreview({...report,valid:false,errors:{field:['bad']},value:false},row).value,false)
})

test('script preview uses original registry endpoint and saved revision without writes',async context=>{
  const vite=await setup(context),{scriptSemantics}=await vite.ssrLoadModule('/src/services/scriptSemantics.ts'),{api}=await vite.ssrLoadModule('/src/services/api.ts')
  const calls:any[]=[];api.post=async(path:any,data:any)=>{calls.push({path,data});return {data:report}}
  const result=await scriptSemantics.preview(row,'{"extra":0}')
  assert.deepEqual(result.source_value,{extra:0})
  assert.deepEqual(calls,[{path:`/business-output-versions/${row.id}/preview-script-semantics/`,data:{response_text:'{"extra":0}',expected_revision:1,expected_contract_sha256:row.contract_sha256}}])
})

test('manager keeps semantic mapping separate from visual fields and persists exact payload',async context=>{
  const vite=await setup(context),{businessOutputs}=await vite.ssrLoadModule('/src/services/businessOutputs.ts'),{default:Manager}=await vite.ssrLoadModule('/src/components/BusinessOutputManager.vue')
  let state:any;const app=createSSRApp({setup(){state=Manager.setup({},{expose(){},emit(){}});return()=>h('div')}})
  app.provide(matchedRouteKey,ref({leaveGuards:new Set(),updateGuards:new Set()}) as any);await renderToString(app)
  state.catalogReady.value=true;state.parsers.value=[{key:row.parser_key}];state.assign(row)
  assert.equal(state.dirty.value,false);assert.equal(state.visual.value,false)
  assert.deepEqual(state.payload(),{name:row.name,description:'',parser_key:row.parser_key,data_structure_version:row.data_structure_version,quality_advisories:{},semantic_mapping:mapping})
  state.form.semantic_mapping={schema_version:1,node:{kind:'object',fields:{}}};assert.equal(state.dirty.value,true)
  let saves=0;businessOutputs.save=async(data:any)=>{saves++;assert.ok(data.semantic_mapping);assert.equal(data.field_sources,undefined);throw new Error('isolated failure')}
  state.semanticErrors.value=['读取失败'];await state.save();assert.equal(saves,0)
  state.semanticErrors.value=[];await state.save();assert.equal(saves,1);assert.ok(state.error.value);assert.equal(state.form.semantic_mapping.node.kind,'object')
  state.assign({...row,status:'published'});assert.equal(state.readonly.value,true)
})

async function mountEditor(vite:any,context:any){
  const {scriptSemantics}=await vite.ssrLoadModule('/src/services/scriptSemantics.ts'),{dataStructures}=await vite.ssrLoadModule('/src/services/dataStructures.ts')
  scriptSemantics.options=async()=>options;dataStructures.schema=async()=>({valid:true,json_schema:{type:'object'},errors:{}})
  const {default:Editor}=await vite.ssrLoadModule('/src/components/ScriptSemanticOutputEditor.vue')
  const props=reactive({modelValue:structuredClone(mapping),structureId:'source',version:{...row},disabled:false,previewDisabled:false})
  let state:any;const events:any[]=[]
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Editor.setup(props,{expose(){},emit:(...args:any[])=>events.push(args)});return()=>h('div')}})
  app.provide(ssrContextKey,{});app.mount({});context.after(()=>app.unmount());await state.load();await nextTick()
  return {state,props,events,scriptSemantics,dataStructures}
}

test('editor preserves samples, ignores late preview after edit/version change and recovers',async context=>{
  const vite=await setup(context),{state,props,scriptSemantics}=await mountEditor(vite,context)
  const pending:any[]=[];scriptSemantics.preview=()=>new Promise(resolve=>pending.push(resolve))
  state.text.value='first';const first=state.preview();state.text.value='second';pending.shift()(report);await first;assert.equal(state.result.value,null)
  const next=state.preview();props.version.revision++;await nextTick();pending.shift()(report);await next;assert.equal(state.result.value,null)
  scriptSemantics.preview=async()=>{throw new Error('isolated network failure')};await state.preview();assert.ok(state.previewError.value);assert.equal(state.text.value,'second')
  scriptSemantics.preview=async()=>report;await state.preview();assert.equal(state.result.value.valid,true)
  props.previewDisabled=true;await nextTick();assert.equal(state.result.value,null);await state.preview();assert.equal(state.result.value,null)
})

test('schema reads discard stale responses and failures do not silently clear saved mapping',async context=>{
  const vite=await setup(context),{state,props,events,dataStructures}=await mountEditor(vite,context)
  const before=JSON.stringify(props.modelValue),pending:any[]=[];dataStructures.schema=()=>new Promise(resolve=>pending.push(resolve))
  props.structureId='second';await nextTick();props.structureId='third';await nextTick()
  pending[1]({valid:true,json_schema:{type:'array'},errors:{}});await Promise.resolve();await Promise.resolve();await nextTick()
  pending[0]({valid:true,json_schema:{type:'string'},errors:{}});await Promise.resolve();await Promise.resolve();await nextTick()
  assert.equal(state.schema.value.type,'array');assert.equal(JSON.stringify(props.modelValue),before)
  dataStructures.schema=async()=>{throw new Error('schema unavailable')};await state.load();assert.ok(state.error.value);assert.equal(state.schema.value,null);assert.ok(state.readiness.value.length)
  props.disabled=true;state.update({kind:'literal',value:1});assert.equal(events.filter(e=>e[0]==='update:modelValue').length,0)
})

test('editor renders source preservation and preview-only scope explicitly',async context=>{
  const vite=await setup(context),{default:Editor}=await vite.ssrLoadModule('/src/components/ScriptSemanticOutputEditor.vue')
  const html=await renderToString(createSSRApp(Editor,{modelValue:mapping,structureId:'',version:null}))
  assert.match(html,/额外创作字段保留/);assert.match(html,/尚不验证实体引用关系/);assert.match(html,/请先选择模型响应结构版本/);assert.match(html,/<textarea/)
})

test('unfinished local mapping invalidates successful and in-flight previews without rewriting mapping',async context=>{
 const vite=await setup(context),{state,props,scriptSemantics}=await mountEditor(vite,context)
 const original=JSON.stringify(props.modelValue);state.text.value='{"extra":0}';scriptSemantics.preview=async()=>report;await state.preview();assert.ok(state.result.value)
 state.draftErrors.value=['需要有效数字'];assert.equal(state.result.value,null);await state.preview();assert.equal(state.result.value,null)
 state.draftErrors.value=[];let resolve:any;scriptSemantics.preview=()=>new Promise(r=>resolve=r);const pending=state.preview();state.draftErrors.value=['需要有效数字'];resolve(report);await pending
 assert.equal(state.result.value,null);assert.equal(state.text.value,'{"extra":0}');assert.equal(JSON.stringify(props.modelValue),original)
})
