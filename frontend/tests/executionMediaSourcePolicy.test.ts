import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {createSSRApp,h,createRenderer,ssrContextKey,reactive,nextTick} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {unusedLanguageForm} from './fixtures/executionLanguage.ts'

const assetId='12345678-1234-1234-1234-123456789012'
const choices=()=>({schema_version:1,allowed_sources:['asset_library','asset_upload','selected_output'],default_source:'asset_library',allow_override:true,order:'provided'})
const input=()=>({key:'pictures',kind:'image',position:0,required:true,min_items:1,max_items:2,media_source_policy:choices(),
  semantic_contract:{schema_version:1,allowed_purposes:[],allowed_subject_kinds:[],allowed_scope_kinds:['request'],paired_with_input_key:null,allow_repeated_asset:false},
  targets:[{target_kind:'workflow_slot',target_key:'pictures',is_primary:true}]})
const raw=()=>({subject_categories:[{value:'character',label:'角色'}],language_form:unusedLanguageForm(),input_forms:{},context_fields:[],version_id:'version',revision:1,frozen:true,sha256:'a'.repeat(64),definition:{version_id:'version',
  configuration:{deployment_id:'deployment',operation:'video.reference_to_video',mode:'mode',workflow_version_id:'workflow',prompt_mode:'none',prompt_profile_version_id:null,inputs:[input()],parameters:[],auxiliaries:[]},
  interface:{output:{contract:{primary:{kind:'video'}}},input_purposes:{image:[]},parameters:{},prompt:{mode:'none',channels:{}}}}})
async function modules(t:any){const vite=await createServer({logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false},optimizeDeps:{noDiscovery:true}});t.after(()=>vite.close());return {vite,m:await vite.ssrLoadModule('/src/services/executionRequestForm.ts')}}
const row=()=>({itemKey:'explicit-use',assetId,purpose:null,pairedTo:null,subjectId:null})

test('instance configuration retains compact source choices and derives constraints once',async t=>{
  const {vite,m}=await modules(t),p=await vite.ssrLoadModule('/src/services/executionMediaSourcePolicy.ts')
  const contract=m.executionRequestFormContract(raw())
  assert.deepEqual(contract.inputs[0].media_source_policy,choices())
  const resolved=p.executionMediaPolicy(contract.inputs[0])
  assert.equal(resolved.min_items,1);assert.equal(resolved.media_kind,'image')
  contract.inputs[0].max_items=1;assert.equal(p.executionMediaPolicy(contract.inputs[0]).max_items,1)
  for(const invalid of [{...choices(),order:'name'},{...choices(),schema_version:2},{...choices(),max_items:8}]){
    const value=raw();value.definition.configuration.inputs[0].media_source_policy=invalid as any
    assert.throws(()=>m.executionRequestFormContract(value))
  }
})

test('request modes are explicit, replace the entire selection and never mix readers',async t=>{
  const {m}=await modules(t),contract=m.executionRequestFormContract(raw()),draft=m.emptyExecutionRequestDraft(contract)
  assert.equal(draft.mediaSourceModes.pictures,'asset_library');assert.deepEqual(draft.media.pictures,[])
  draft.media.pictures=[row()]
  const base=m.buildExecutionRequestFixture(contract,draft).inputs.pictures
  assert.equal(base.selection.source,'asset_library');assert.equal(base.override,undefined)
  draft.mediaSourceModes.pictures='asset_upload'
  const override=m.buildExecutionRequestFixture(contract,draft).inputs.pictures
  assert.deepEqual(override.selection,{source:'asset_library',items:[]})
  assert.equal(override.override.source,'asset_upload');assert.equal(override.override.items[0].asset_id,assetId)
  contract.inputs[0].media_source_policy.allow_override=false
  assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/不允许/)
  contract.inputs[0].media_source_policy.allow_override=true
  draft.mediaSourceModes.pictures='selected_output'
  assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/不一致/)
  delete draft.mediaSourceModes.pictures
  assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/明确选择/)
})

test('upload-only and upstream modes show only applicable existing selection controls',async t=>{
  const {vite,m}=await modules(t),Component=(await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')).default
  const contract=m.executionRequestFormContract(raw()),draft=m.emptyExecutionRequestDraft(contract)
  draft.media.pictures=[row()];draft.mediaSourceModes.pictures='asset_upload'
  let html=await renderToString(createSSRApp({render:()=>h(Component,{contract,modelValue:draft})}))
  assert.match(html,/本次素材来源/);assert.match(html,/上传本地文件并入库/);assert.doesNotMatch(html,/>选择已有素材</)
  draft.mediaSourceModes.pictures='selected_output'
  html=await renderToString(createSSRApp({render:()=>h(Component,{contract,modelValue:draft})}))
  assert.match(html,/此入口没有提供上游/);assert.doesNotMatch(html,/上传本地文件并入库/)
})

test('author editor reuses shared control and emits no duplicate quantity or purpose',async t=>{
  const {vite}=await modules(t),service=await vite.ssrLoadModule('/src/services/inputSourcePolicies.ts')
  service.inputSourcePolicies.options=async()=>({schema_version:1,sources:[],orders:[],configuration_owner:'instance',description:'fixture'})
  const Component=(await vite.ssrLoadModule('/src/components/ExecutionMediaSourcePolicyEditor.vue')).default
  const props=reactive({input:input(),purposes:[],disabled:false}),events:any[]=[];let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit(...value:any[]){events.push(value)}});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({});t.after(()=>app.unmount());await nextTick();await nextTick()
  assert.ok(state.catalog.value);assert.equal(events.length,0)
  state.update({...choices(),media_kind:'image',min_items:99,max_items:100,allowed_purposes:['not another owner']})
  assert.deepEqual(events[0],['update:policy',choices()])
  props.disabled=true;state.enable();state.update({...choices()});assert.equal(events.length,1)
})
