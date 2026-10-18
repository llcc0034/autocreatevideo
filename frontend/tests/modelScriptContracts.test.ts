import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {scriptContractFixture as raw} from './helpers/scriptContractFixture.ts'

async function setup(context:any){const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});context.after(()=>vite.close());return vite}
function specification(options:any){return {capabilities:options.policies.map((row:any)=>row.operation),other:{keep:false},...Object.fromEntries(options.policies.map((row:any)=>[row.key,{
  timeout_seconds:100,chat_request_options:{temperature:0,thinking:{type:'disabled'}}
}]))}}

test('transport catalog has no duplicate business field dictionary and rejects old or incomplete catalogs',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelScriptContracts.ts'),options=m.adaptScriptContracts(raw)
  assert.equal(options.schemaVersion,3)
  assert.equal(options.referencePolicy,undefined)
  assert.deepEqual(m.scriptContractErrors(specification(options),options),[])
  for(const broken of [null,{}, {...raw,schema_version:2},{...raw,reference_policy:{}},{...raw,policies:[]},{...raw,policies:[...raw.policies,raw.policies[0]]},{...raw,policies:raw.policies.map(row=>({...row,fields:[]}))}])assert.throws(()=>m.adaptScriptContracts(broken))
  for(const row of options.policies)assert.deepEqual(row.numberFields.map((f:any)=>f.key),['timeout_seconds'])
})

test('transport validation rejects legacy business rules and missing settings without dropping explicit zero options',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelScriptContracts.ts'),options=m.adaptScriptContracts(raw),spec=specification(options)
  for(const key of ['series_concept','script_structure']){
    for(const value of [0,-1,601,Infinity,NaN,null,true,'30'])assert.ok(m.scriptContractErrors({...spec,[key]:{...spec[key],timeout_seconds:value}},options).length)
    for(const removed of ['field_contract','reference_policy','minimum_shot_duration_ms','maximum_shot_duration_ms','max_input_bytes','unknown'])assert.ok(m.scriptContractErrors({...spec,[key]:{...spec[key],[removed]:{}}},options).length)
    assert.ok(m.scriptContractErrors({...spec,[key]:{timeout_seconds:30}},options).length)
  }
  assert.deepEqual(m.scriptContractErrors(spec,options),[])
  assert.equal(spec.series_concept.chat_request_options.temperature,0)
  assert.ok(m.scriptContractErrors(spec,null).length)
})

test('explicit initialization invents no timeout or fields and JSON edits cannot duplicate the form',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelScriptContracts.ts'),options=m.adaptScriptContracts(raw),spec=specification(options)
  assert.deepEqual(m.initializeScriptPolicy(undefined),{chat_request_options:{}})
  const old={field_contract:{title:{required:false}},timeout_seconds:5,chat_request_options:{temperature:0}}
  assert.deepEqual(m.initializeScriptPolicy(old),old)
  assert.ok(m.scriptContractErrors({series_concept:old},options).length)
  m.assertScriptContractsUnchanged(spec,{...spec,other:{keep:true}},options)
  assert.throws(()=>m.assertScriptContractsUnchanged(spec,{...spec,series_concept:{}},options),/固定表单/)
  assert.throws(()=>m.assertScriptContractsUnchanged(spec,spec,null),/目录/)
})

test('editor only edits transport, retains unrelated and invalid values, and blocks readonly or missing-catalog writes',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelScriptContracts.ts'),{default:Editor}=await vite.ssrLoadModule('/src/components/ModelScriptContractsEditor.vue')
  const {createSSRApp,h,reactive,nextTick}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  const props:any=reactive({modelValue:specification(m.adaptScriptContracts(raw)),options:m.adaptScriptContracts(raw),chatOptions:null,disabled:false})
  let state:any,updates=0
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(event:string,value:any)=>{if(event==='update:modelValue'){props.modelValue=value;updates++}}});return()=>h('div')}}))
  assert.equal(updates,0)
  state.number('script_structure','timeout_seconds','22.5');assert.equal(props.modelValue.script_structure.timeout_seconds,22.5)
  state.number('series_concept','timeout_seconds','');await nextTick();assert.ok(state.errors.value.length)
  state.number('series_concept','timeout_seconds','30');await nextTick();assert.deepEqual(state.errors.value,[])
  assert.deepEqual(props.modelValue.other,{keep:false})
  assert.equal(props.modelValue.series_concept.chat_request_options.thinking.type,'disabled')
  props.modelValue.series_concept.field_contract={title:{required:false}}
  state.initialize(props.options.policies[0])
  assert.deepEqual(props.modelValue.series_concept.field_contract,{title:{required:false}})
  const html=await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  assert.match(html,/剧本模型传输设置/);assert.match(html,/已移除或未注册配置 field_contract/)
  for(const removed of ['固定单行协议','文本字段规则','最大实体数','最小镜头时长'])assert.ok(!html.includes(removed))
  const count=updates;props.disabled=true;state.number('series_concept','timeout_seconds','20');state.initialize(props.options.policies[0]);assert.equal(updates,count)
  props.disabled=false;props.options=null;state.number('series_concept','timeout_seconds','20');assert.equal(updates,count)
})

test('removal is an explicit draft operation, cancellation and readonly never alter saved-shaped values',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelScriptContracts.ts'),{default:Editor}=await vite.ssrLoadModule('/src/components/ModelScriptContractsEditor.vue')
  const {createSSRApp,h,reactive,nextTick}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  const props:any=reactive({modelValue:{series_concept:{timeout_seconds:30,chat_request_options:{},field_contract:{keep:false}}},options:m.adaptScriptContracts(raw),chatOptions:null,disabled:false})
  let state:any
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose(){},emit:(event:string,value:any)=>{if(event==='update:modelValue')props.modelValue=value}});return()=>h('div')}}))
  state.remove('series_concept','field_contract');assert.ok(props.modelValue.series_concept.field_contract)
  assert.equal(state.removalPath.value,'specification.series_concept.field_contract')
  state.pendingRemoval.value=null;state.confirmRemoval();assert.ok(props.modelValue.series_concept.field_contract)
  state.remove('series_concept','field_contract');state.confirmRemoval();assert.equal(props.modelValue.series_concept.field_contract,undefined)
  assert.deepEqual(props.modelValue.series_concept,{timeout_seconds:30,chat_request_options:{}})
  state.remove('series_concept');props.disabled=true;state.confirmRemoval();assert.ok(props.modelValue.series_concept)
  state.remove('series_concept');assert.equal(state.pendingRemoval.value,null)
  props.disabled=false;state.remove('series_concept');props.options=null;state.confirmRemoval();assert.ok(props.modelValue.series_concept)
  props.options=m.adaptScriptContracts(raw);state.remove('series_concept');props.modelValue.series_concept.timeout_seconds=90;await nextTick();state.confirmRemoval();assert.equal(props.modelValue.series_concept.timeout_seconds,90)
  state.remove('series_concept');state.confirmRemoval();assert.equal(props.modelValue.series_concept,undefined)
  props.modelValue={capabilities:['script.generate'],series_concept:{timeout_seconds:30,chat_request_options:{}}}
  state.remove('series_concept');assert.equal(state.pendingRemoval.value,null)
  state.remove('series_concept','timeout_seconds');assert.equal(state.pendingRemoval.value,null)
})
