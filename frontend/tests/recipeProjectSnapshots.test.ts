import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { unusedLanguageForm } from './fixtures/executionLanguage.ts'

const sha='a'.repeat(64), id='11111111-1111-4111-8111-111111111111', key='project.logline'
const raw:any={subject_categories: [{value:'character',label:'人物'},{value:'scene',label:'场景'},{value:'prop',label:'道具'}], language_form: unusedLanguageForm(),version_id:id,revision:1,frozen:true,sha256:sha,context_fields:[],
  input_forms:{[key]:{label:'项目梗概',suggestions:[{key,label:'预览梗概',requires_project:true}]}},definition:{version_id:id,
    configuration:{deployment_id:'deployment',operation:'image.text_to_image',mode:'',workflow_version_id:null,
      prompt_mode:'profile',prompt_profile_version_id:'profile',parameters:[],auxiliaries:[],inputs:[{key,kind:'text',position:1,required:true,min_items:1,max_items:1,
        semantic_contract:{schema_version:1,allowed_purposes:[],allowed_subject_kinds:[],allowed_scope_kinds:['project'],paired_with_input_key:null,allow_repeated_asset:false},
        targets:[{target_kind:'profile_input',target_key:key,is_primary:true}]}]},
    interface:{output:{kind:'image'},parameters:{},input_purposes:{},profile_inputs:{[key]:{kind:'text',required:true,min_items:1,max_items:1,
      source_kinds:['project_snapshot'],source_reader:{protocol:'project_snapshot/v1',selector:key}}},prompt:{mode:'profile',channels:{}}}}}
const route={execution_instance_version_id:id,expected_definition_sha256:sha,input_sources:{[key]:{kind:'project_snapshot'}},parameter_policies:[]}
const recipe=()=>({id:'recipe',definitionSha256:'b'.repeat(64),definition:{schema_version:3,interaction_mode:'assisted',stages:[
  {key:'visual_rules_generate',routing:{routes:[structuredClone(route)]}}, {key:'bible_image_generation',mode:'existing_pipeline',routing:{routes:[structuredClone(route)]},variant_routing:{routes:[structuredClone(route)]}}]}})
const tick=async()=>{await Promise.resolve();await nextTick();await Promise.resolve();await nextTick()}

async function modules(fn){const vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try{await fn(vite)}finally{await vite.close()}}

test('recipe snapshots deduplicate exact consumers, require explicit confirmation and discard stale project responses',()=>modules(async vite=>{
  const {executionRegistryApi:api}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  let reads=0, pending:any=null
  api.contract=async()=>{reads++;return pending?await new Promise(resolve=>pending=resolve):structuredClone(raw)}
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeProjectSourceConfirmations.vue')
  const props:any=reactive({recipe:recipe(),projectId:'project-a',disabled:false}),events:any[]=[];let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(event,value)=>events.push([event,value])});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()})
  try{
    app.mount({});await tick()
    assert.equal(state.error.value,'');assert.equal(reads,1);assert.equal(state.entries.value.length,1)
    assert.equal(state.ready.value,false);assert.equal(state.entries.value[0].uses.length,3)
    state.entries.value[0].row={enabled:true,value:'Read preview',reference:{token:'signed',content:'Read preview',label:'梗概'}}
    await tick();assert.equal(state.ready.value,true)
    assert.deepEqual(events.filter(([e])=>e==='update:modelValue').at(-1)[1],[{executionInstanceVersionId:id,inputKey:key,reference:'signed'}])
    state.busy.value={entry:true};await tick();assert.equal(state.ready.value,false)
    state.busy.value={};state.entries.value[0].row.value='Edited';await tick();assert.equal(state.ready.value,false)
    pending=true;props.projectId='project-b';await tick()
    assert.equal(state.ready.value,false);assert.equal(state.entries.value.length,0)
    const finish=pending;pending=null
    props.recipe={...recipe(),definition:{stages:[]}};await tick();assert.equal(state.ready.value,true)
    finish(structuredClone(raw));await tick();assert.equal(state.entries.value.length,0,'old contract response cannot restore prior project receipts')
    assert.deepEqual(events.filter(([e])=>e==='update:modelValue').at(-1)[1],[])
    props.recipe=recipe();props.projectId=undefined;await tick();assert.equal(state.ready.value,false,'new project must be saved before preview')
    props.projectId='project-c';api.contract=async()=>{throw new Error('contract read failed')};await tick()
    assert.match(state.error.value,/contract read failed/);assert.equal(state.ready.value,false)
    api.contract=async()=>({...structuredClone(raw),sha256:'c'.repeat(64)})
    await state.load();await tick();assert.match(state.error.value,/不一致/)
  }finally{app.unmount()}
}))

test('production payload freezes only exact preview receipts and rejects duplicate or injected bodies',()=>modules(async vite=>{
  const {snapshotProductionRunInput}=await vite.ssrLoadModule('/src/services/productionOnboarding.ts')
  const {buildProductionRunRequestPayload:build}=await vite.ssrLoadModule('/src/services/productionRunPayload.ts')
  const input:any={planSource:'recipe',projectId:'project',recipeVersionId:'recipe',idempotencyKey:'intent',scriptMode:'confirmed_revision',scriptRevisionId:'revision',
    projectSourceConfirmations:[{executionInstanceVersionId:id,inputKey:key,reference:'signed'}]}
  const frozen=snapshotProductionRunInput(input)
  input.projectSourceConfirmations[0].reference='edited'
  assert.deepEqual(build(frozen).project_source_confirmations,[{execution_instance_version_id:id,input_key:key,reference:'signed'}])
  assert.throws(()=>build({...input,projectSourceConfirmations:[...input.projectSourceConfirmations,...input.projectSourceConfirmations]}),/重复确认/)
  assert.throws(()=>build({...input,projectSourceConfirmations:[{...input.projectSourceConfirmations[0],value:'injection'}]}),/不接收正文/)
}))
