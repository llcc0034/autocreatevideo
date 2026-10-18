import assert from 'node:assert/strict'
import { unusedLanguageForm } from './fixtures/executionLanguage.ts'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from 'vue/server-renderer'

const root=fileURLToPath(new URL('..',import.meta.url))
const selection={operation:'script.generate',mode:'',prompt_mode:'manual',prompt_profile_version_id:null}
const context={target:{kind:'deployment',id:'deployment'},selection}
const request={expectedDefinitionSha256:'a'.repeat(64),fixture:{inputs:{},parameters:{seed:0},manual_channels:{positive_prompt:'  Explicit.\n'},subject_facts:[],source_language:'',target_language:''},fieldValues:[],allowExternalCalls:false,budgetLimit:null}
const preparation={id:'prep',stage_run:null,asset_job:null,deployment_smoke:'attempt',workflow_smoke:null,execution_instance_version:null,
  purpose:'resource_smoke',unit_key:'smoke',request_key:'main',revision:1,status:'preparing',intent_sha256:'intent',request_sha256:null,
  request_frozen_at:null,next_check_at:null,reason_code:'',reason:'',evaluation_revision:null,evaluated_at:null,created_by:1,created_at:'created',updated_at:'updated'}
const created={attempt_kind:'deployment_smoke',attempt_id:'attempt',status:'preparing',preparation}
async function setup(t) {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true}})
  t.after(()=>vite.close())
  const {api}=await vite.ssrLoadModule('/src/services/api.ts')
  const {resourceSmokeApi:client}=await vite.ssrLoadModule('/src/services/executionResourceSmoke.ts')
  return {vite,api,client}
}

test('resource contract verifies exact URL owner and explicit prompt source without inventing an instance',async t=>{
  const {api,client}=await setup(t)
  const contract={scope:'resource_smoke',version_id:null,schema_version:1,size_bytes:100,sha256:'a'.repeat(64),target_fingerprint:'b'.repeat(64),context_fields:[],input_forms:{},subject_categories: [{value:'character',label:'人物'},{value:'scene',label:'场景'},{value:'prop',label:'道具'}], language_form: unusedLanguageForm(),
    definition:{version_id:null,configuration:{...selection,deployment_id:'deployment',workflow_version_id:null}}}
  const calls=[]
  api.post=async(...args)=>{calls.push(args);return {data:contract}}
  const result=await client.contract(context)
  assert.equal(result.version_id,null)
  assert.deepEqual(calls,[['/model-deployments/deployment/smoke-contract/',selection]])
  for(const change of [{prompt_mode:'profile'},{deployment_id:'different'},{workflow_version_id:'unselected'}]) {
    api.post=async()=>({data:{...contract,definition:{...contract.definition,configuration:{...contract.definition.configuration,...change}}}})
    await assert.rejects(client.contract(context),/资源选择不一致/)
  }
  api.post=async()=>({data:{...contract,frozen:true}})
  await assert.rejects(client.contract(context),/冒充/)
})

test('resource creation reuses original Smoke URLs and returns the exact null-instance preparation',async t=>{
  const {api,client,vite}=await setup(t),calls=[]
  api.post=async(...args)=>{calls.push(args);return {data:created}}
  const result=await client.create(context,request,'same-key')
  assert.equal(result.preparation.id,'prep');assert.equal(result.preparation.executionInstanceVersion,null)
  assert.deepEqual(calls[0],['/model-deployments/deployment/smoke-test/',{
    selection,expected_definition_sha256:request.expectedDefinitionSha256,fixture:request.fixture,field_values:[],allow_external_calls:false},
    {headers:{'Idempotency-Key':'same-key'}}])
  await client.create(context,request,'same-key')
  assert.deepEqual(calls[1],calls[0])
  await assert.rejects(client.create(context,{...request,reuse:{}},'same-key'),/不能借用/)
  await assert.rejects(client.create(context,request,''),/幂等键/)
  assert.equal(calls.length,2)
  const m=await vite.ssrLoadModule('/src/services/executionPreparation.ts')
  assert.throws(()=>m.adaptExecutionSmokeCreated(created),/关联不一致/)
  for(const change of [{purpose:'instance_smoke'},{execution_instance_version:'fake'},{deployment_smoke:'other'}])
    assert.throws(()=>m.adaptResourceSmokeCreated({...created,preparation:{...preparation,...change}}),/关联不一致/)
  api.post=async()=>({data:{...created,attempt_kind:'workflow_smoke',preparation:{...preparation,workflow_smoke:'attempt',deployment_smoke:null}}})
  await assert.rejects(client.create(context,request,'same-key'),/资源类型/)
})

test('resource field source uses exact hashes and cannot accept an instance field batch',async t=>{
  const {api,client}=await setup(t),calls=[]
  const source={execution_instance_version_id:null,definition_sha256:'a'.repeat(64),input_key:'fields',profile_version_id:'profile',profile_contract_sha256:'b'.repeat(64),
    rules:[{field_key:'direction',generation_instruction:'Describe supplied facts',language_policy:'en',generation_context_selectors:[],max_chars:7000}]}
  api.post=async(...args)=>{calls.push(args);return {data:source}}
  assert.deepEqual(await client.readFields(context,'profile'),source)
  assert.deepEqual(calls[0],['/model-deployments/deployment/smoke-field-source/',{selection,profile_version_id:'profile'}])
  const items=[{field_key:'direction',source_text:'A parcel.',source_language:'en',target_language:'en',context_facts:{},protected_tokens:[]}]
  const built={...source,source:{kind:'literal',value:{items}}}
  api.post=async(...args)=>{calls.push(args);return {data:built}}
  assert.deepEqual(await client.buildFields(context,source,items),built)
  assert.deepEqual(calls[1][1],{selection,profile_version_id:'profile',expected_definition_sha256:source.definition_sha256,expected_profile_contract_sha256:source.profile_contract_sha256,items})
  api.post=async()=>({data:{...source,execution_instance_version_id:'instance'}})
  await assert.rejects(client.readFields(context,'profile'),/来源响应不完整/)
})

test('resource selector requires explicit choices and freezes the selected context until deliberate replacement',async t=>{
  const {client,vite}=await setup(t)
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  client.choices=async()=>({target:{operations:[{value:'script.generate',label:'剧本生成',output_parser_keys:[]}],modes:[],model_family:'text'},prompt_modes:[{value:'manual',label:'人工提示词'},{value:'profile',label:'策略组装'}]})
  platformApi.listPromptProfileVersions=async()=>[{id:'profile',status:'published',modelFamily:'text',mode:'',name:'Exact',version:2}]
  const {default:Form}=await vite.ssrLoadModule('/src/components/ResourceSmokeCreateForm.vue')
  let state
  await renderToString(createSSRApp({setup(){state=Form.setup(reactive({target:context.target}),{expose:()=>{},emit:()=>{}});return()=>h('div')}}))
  await state.load()
  assert.equal(state.operation.value,'');assert.equal(state.promptMode.value,'')
  state.choose();assert.equal(state.context.value,null)
  state.operation.value='script.generate';state.promptMode.value='profile';state.choose()
  assert.equal(state.context.value,null)
  state.profileId.value='profile';state.choose()
  assert.equal(state.context.value.selection.prompt_profile_version_id,'profile')
  state.promptMode.value='manual';state.choose()
  assert.equal(state.context.value.selection.prompt_mode,'profile')
  state.childBusy.value=true;state.changeSelection();assert.ok(state.context.value)
  state.childBusy.value=false;state.changeSelection();assert.equal(state.context.value,null)
  state.choose();assert.equal(state.context.value.selection.prompt_profile_version_id,null)
})

test('generic resource selection freezes selected output and rejects absent or incompatible output',async t=>{
  const {vite,client}=await setup(t),{platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  client.choices=async()=>({target:{operations:[{value:'text.structured',label:'通用结构化文本',output_parser_keys:['structured_json.v1']}],modes:[],model_family:'text'},prompt_modes:[{value:'manual',label:'人工提示词'}]})
  platformApi.listPromptProfileVersions=async()=>[]
  const {default:Form}=await vite.ssrLoadModule('/src/components/ResourceSmokeCreateForm.vue')
  let state
  await renderToString(createSSRApp({setup(){state=Form.setup(reactive({target:context.target}),{expose(){},emit(){}});return()=>h('div')}}))
  await state.load();state.operation.value='text.structured';state.promptMode.value='manual'
  state.choose();assert.equal(state.context.value,null);assert.match(state.error.value,/输出版本/)
  state.chooseOutput({id:'wrong',status:'published',parser_key:'visual_rules.v2'});assert.equal(state.selectedOutput.value,null)
  state.chooseOutput({id:'a',status:'published',parser_key:'structured_json.v1'});state.choose()
  assert.equal(state.context.value.selection.output_version_id,'a')
  state.chooseOutput({id:'b',status:'published',parser_key:'structured_json.v1'});assert.equal(state.context.value.selection.output_version_id,'a')
})
