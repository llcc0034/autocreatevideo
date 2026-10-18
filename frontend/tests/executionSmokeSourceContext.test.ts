import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createRenderer, h, reactive, nextTick, ssrContextKey } from 'vue'
import { unusedLanguageForm } from './fixtures/executionLanguage.ts'

const row=()=>({run_id:'run',project_id:'project',stage_key:'media_generation',project_title:'项目',run_created_at:'2026-09-22T00:00:00Z',run_plan_sha256:'a'.repeat(64)})
const page=(rows=[row()])=>({count:rows.length,results:rows,next:null,previous:null})

test('smoke context API is paged and exact context belongs only to the independent request',async t=>{
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom'})
  t.after(()=>vite.close())
  const {adaptSmokeSourceContexts:adapt}=await vite.ssrLoadModule('/src/services/executionSmokeSourceContext.ts')
  assert.equal(adapt(page()).results[0].project_id,'project')
  assert.throws(()=>adapt(page([{...row(),run_plan_sha256:''} as any])))
  const {api}=await vite.ssrLoadModule('/src/services/api.ts')
  const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const calls:any[]=[];api.get=async(...args)=>{calls.push(args);return {data:page()}}
  await executionRegistryApi.smokeSourceContexts('version',2)
  assert.deepEqual(calls,[['/model-execution-instance-versions/version/smoke-source-contexts/',{params:{page:2,page_size:20}}]])
  const {executionSmokePayload}=await vite.ssrLoadModule('/src/services/executionPreparation.ts')
  const context={run_id:'run',stage_key:'media_generation',run_plan_sha256:'a'.repeat(64)}
  const payload=executionSmokePayload({expectedDefinitionSha256:'b'.repeat(64),fixture:{inputs:{}},fieldValues:[],allowExternalCalls:false,reviewedSourceContext:context})
  assert.deepEqual(payload.reviewed_source_context,context)
  assert.equal(payload.fixture.production_run_id,undefined)
  assert.equal(payload.approve,undefined)
})

test('context selector requires an explicit choice and invalidates on reload, error and owner change',async t=>{
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom'})
  t.after(()=>vite.close())
  const Component=(await vite.ssrLoadModule('/src/components/ExecutionSmokeSourceContext.vue')).default
  const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  let calls=0,state:any
  executionRegistryApi.smokeSourceContexts=async()=>{calls++;return page()}
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const props=reactive({versionId:'version',modelValue:null})
  const Probe={...Component,setup(p,ctx){state=Component.setup(p,ctx);return()=>null}}
  const app=renderer.createApp({render:()=>h(Probe,props)})
  t.after(()=>app.unmount());app.provide(ssrContextKey,{});app.mount({})
  assert.equal(calls,0);await state.load();assert.equal(calls,1);assert.equal(state.model.value,null)
  state.select('run|media_generation');assert.equal(state.model.value.run_id,'run')
  state.select('forged|media_generation');assert.equal(state.model.value,null)
  state.select('run|media_generation')
  executionRegistryApi.smokeSourceContexts=async()=>{throw new Error('unavailable')}
  await state.load();assert.equal(state.model.value,null);assert.equal(state.rows.value.length,0);assert.match(state.error.value,/unavailable/)
  let finish:any
  executionRegistryApi.smokeSourceContexts=()=>new Promise(resolve=>{finish=resolve})
  const pending=state.load();props.versionId='changed';await nextTick();finish(page());await pending
  assert.equal(state.model.value,null);assert.equal(state.rows.value.length,0);assert.equal(state.loaded.value,false)
})

test('Smoke form requires context and sends source identity without creating a production request',async t=>{
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom'})
  t.after(()=>vite.close())
  const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const selector='reviewed_visual_rules.continuity_summary', calls:any[]=[]
  const input={key:selector,kind:'text',required:true,min_items:1,max_items:1,position:0,
    semantic_contract:{schema_version:1,allowed_scope_kinds:['project'],allowed_subject_kinds:[],allowed_purposes:[],paired_with_input_key:null,allow_repeated_asset:false},
    targets:[{target_kind:'profile_input',target_key:selector,is_primary:true}]}
  const raw={version_id:'version',revision:1,frozen:true,sha256:'a'.repeat(64),subject_categories: [{value:'character',label:'人物'},{value:'scene',label:'场景'},{value:'prop',label:'道具'}], language_form: unusedLanguageForm(),input_forms:{},
    context_fields:[{selector_key:selector,kind:'text',scope_kind:'project',revision_kind:'visual_rules_approval'}],
    definition:{version_id:'version',configuration:{deployment_id:'deployment',operation:'visual_rules.generate',mode:'',workflow_version_id:null,prompt_mode:'profile',prompt_profile_version_id:'profile',inputs:[input],parameters:[],auxiliaries:[]},
      interface:{output:{kind:'text'},parameters:{},input_purposes:{},profile_inputs:{[selector]:{kind:'text',source_kinds:['context_field'],context_selectors:[selector]}},prompt:{mode:'profile',channels:{}}}}}
  executionRegistryApi.contract=async()=>raw
  executionRegistryApi.smoke=async(...args)=>{calls.push(args);return {preparation:{id:'prep',executionInstanceVersion:'version'}}}
  const Component=(await vite.ssrLoadModule('/src/components/ExecutionSmokeCreateForm.vue')).default
  let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({...Component,setup(p,ctx){state=Component.setup(p,ctx);return()=>null}},{versionId:'version'})
  t.after(()=>app.unmount());app.provide(ssrContextKey,{});app.mount({});await nextTick();await nextTick()
  assert.equal(state.error.value,'');assert.equal(state.usesReviewedSources.value,true)
  state.draft.value.contexts[selector]={selectorKey:selector,projectId:'project',revisionId:'decision'}
  await nextTick();state.reviewed.value=true;await state.submit();assert.equal(calls.length,0)
  state.sourceContext.value=row();await nextTick();assert.equal(state.reviewed.value,false)
  state.reviewed.value=true;await state.submit();assert.equal(calls.length,1)
  const submitted=calls[0][1]
  assert.deepEqual(submitted.reviewedSourceContext,{run_id:'run',stage_key:'media_generation',run_plan_sha256:'a'.repeat(64)})
  assert.equal(submitted.fixture.inputs[selector].content_revision_id,'decision')
  assert.equal(submitted.fixture.production_run_id,undefined)
})
