import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { createRenderer, reactive, nextTick, createSSRApp, ssrContextKey } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createPinia, setActivePinia } from 'pinia'
const root=fileURLToPath(new URL('..',import.meta.url)),sha='a'.repeat(64)
const instanceId='12345678-1234-4123-8123-123456789abc'
async function modules(fn){const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true}});try{await fn(vite)}finally{await vite.close()}}
const preview=()=>({schema_version:1,status:'inputs_validated',run_plan_sha256:sha,definition_sha256:sha,stage_run_id:'stage',shot_id:'shot',review:null,
  candidate_number:1,route_index:0,execution_instance_version_id:'instance',input_amendment:null,effective_input_sources:{},
  excluded_reference_keys:['unused'],notice:'未调用模型',value_inputs:[],inputs:[{key:'image',kind:'image',minimum:0,maximum:2,
    items:[{item_key:'one',name:'角色图片',purpose:null,order:0,paired_to:null,subject_ref:null,asset_id:'asset'}]}]})
test('preview requires matching plan/stage/shot and ordered complete semantic facts',()=>modules(async vite=>{
  const {referenceMappingPreview:adapt}=await vite.ssrLoadModule('/src/services/productionReferenceMapping.ts'),expected={plan:sha,stage:'stage',shot:'shot',candidate:1}
  assert.equal(adapt(preview(),expected).inputs[0].items[0].name,'角色图片')
  for(const change of [{status:'succeeded'},{shot_id:'other'},{candidate_number:2},{route_index:-1},{execution_instance_version_id:null},{run_plan_sha256:'b'.repeat(64)},{inputs:null},{notice:null}])assert.throws(()=>adapt({...preview(),...change},expected))
  for(const change of [{order:1},{purpose:undefined},{paired_to:'missing'},{subject_ref:{id:'unknown'}},{item_key:''}]){
    const row=preview();Object.assign(row.inputs[0].items[0],change);assert.throws(()=>adapt(row,expected))
  }
  const repeated=preview();repeated.inputs[0].items.push({...repeated.inputs[0].items[0],order:1});assert.throws(()=>adapt(repeated,expected))
}))

test('structured preview requires original input evidence and does not hide missing values',()=>modules(async vite=>{
  const {referenceMappingPreview:adapt}=await vite.ssrLoadModule('/src/services/productionReferenceMapping.ts'),expected={plan:sha,stage:'stage',shot:'shot',candidate:1}
  const value={...preview(),value_inputs:[{key:'units',kind:'json',value:{ordered_units:[{unit_key:'shot:shot'}]},
    provenance:{input_key:'units',kind:'structured',value_sha256:sha,leaves:[]}}]}
  assert.deepEqual(adapt(value,expected).value_inputs[0].value,value.value_inputs[0].value)
  for(const change of [{value_inputs:undefined},{value_inputs:[{...value.value_inputs[0],key:'image'}]},
    {value_inputs:[{...value.value_inputs[0],provenance:{}}]},{value_inputs:[{key:'units',kind:'json',provenance:value.value_inputs[0].provenance}]}]){
    assert.throws(()=>adapt({...value,...change},expected))
  }
}))
function mountSetup(Component,props,emit=()=>{}){
  let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit});return()=>null}});app.provide(ssrContextKey,{});app.mount({});return {state,unmount:()=>app.unmount()}
}

test('project text amendment reuses source confirmation without offering media conversion',()=>modules(async vite=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeStageInputAmendment.vue')
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),original=platformApi.amendProductionStageInputs
  const props=reactive({runId:'run',stageId:'text-stage',planSha:sha,projectText:true,disabled:false,sources:[{index:0,sources:{script:{kind:'context_field',selector_key:'script.content',scope:{kind:'project'}}}}]})
  const calls:any[]=[],mounted=mountSetup(Component,props),s=mounted.state
  platformApi.amendProductionStageInputs=async(run,input)=>{calls.push(input);return {status:input.action==='apply'?'applied':'proposed',sha256:sha,evidence:{run_plan_sha256:sha,policy:input.policy}}}
  try{
    assert.equal(s.policy.value,'explicit_project_text_input_sources_v1')
    const html=await renderToString(createSSRApp(Component,props));assert.match(html,/项目文本/);assert.doesNotMatch(html,/对白缺少画外音标记/)
    s.selected.value='0';s.add();s.drafts.value[0].text=JSON.stringify({kind:'selected_output',stage_key:'script_generate',output_key:'generated_script.script.content',scope:{kind:'project'},selection_policy:'reviewed_selected'})
    await s.request('preview');assert.equal(calls.length,1);assert.equal(calls[0].policy,'explicit_project_text_input_sources_v1')
    s.confirmed.value=true;props.disabled=true;await nextTick();await s.request('apply');assert.equal(calls.length,1);assert.equal(s.result.value,null)
    props.disabled=false;await s.request('preview');s.confirmed.value=true;await s.request('apply');assert.equal(calls.at(-1).expected_amendment_sha256,sha)
    props.stageId='new-stage';await nextTick();assert.equal(s.drafts.value.length,0);assert.equal(s.confirmed.value,false)
  }finally{platformApi.amendProductionStageInputs=original;mounted.unmount()}
}))

test('stage source validation presents the bounded API error instead of only a code',()=>modules(async vite=>{
  const {productionIssuePresentation}=await vite.ssrLoadModule('/src/services/productionRunPresentation.ts')
  assert.match(productionIssuePresentation('stage_validation_failed',"{'input_sources.script': ['本轮未选择已确认剧本']}",'blocked').summary,/input_sources.script.*未选择已确认剧本/)
  assert.doesNotMatch(productionIssuePresentation('stage_validation_failed','<script>secret</script>','blocked').summary,/secret/)
}))

test('dialogue amendment requires explicit confirmation and preserves immutable plan identity',()=>modules(async vite=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeStageInputAmendment.vue')
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),original=platformApi.amendProductionStageInputs,calls=[]
  const props=reactive({runId:'run',stageId:'stage',planSha:sha}),events=[]
  platformApi.amendProductionStageInputs=async(run,input)=>{calls.push(input);return {status:input.action==='apply'?'applied':'proposed',sha256:sha,
    evidence:{run_plan_sha256:props.planSha,policy:'dialogue_missing_voiceover_is_false_v1',changes:[]},id:input.action==='apply'?'audit':null,notice:'未调用模型'}}
  const mounted=mountSetup(Component,props,(event)=>events.push(event)),s=mounted.state
  try{
    await s.request('apply');assert.equal(calls.length,0)
    await s.request('preview');assert.equal(calls[0].action,'preview');assert.equal(events.length,0)
    await s.request('apply');assert.equal(calls.length,1)
    s.confirmed.value=true;await s.request('apply')
    assert.deepEqual(calls[1],{stage_run_id:'stage',expected_run_plan_sha256:sha,action:'apply',policy:'dialogue_missing_voiceover_is_false_v1',expected_amendment_sha256:sha})
    assert.deepEqual(events,['applied']);assert.equal(s.result.value.status,'applied')
    const html=await renderToString(createSSRApp(Component,props));assert.match(html,/不会改写剧本/);assert.match(html,/核对对白转换修订/)
    props.stageId='another';await nextTick();assert.equal(s.result.value,null);assert.equal(s.confirmed.value,false)
  }finally{platformApi.amendProductionStageInputs=original;mounted.unmount()}
}))

test('dialogue amendment rejects stale response after stage change',()=>modules(async vite=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeStageInputAmendment.vue')
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),original=platformApi.amendProductionStageInputs
  let finish:any;platformApi.amendProductionStageInputs=()=>new Promise(resolve=>finish=resolve)
  const props=reactive({runId:'run',stageId:'stage',planSha:sha}),mounted=mountSetup(Component,props),s=mounted.state
  try{const pending=s.request('preview');props.stageId='other';await nextTick();finish({status:'proposed',sha256:sha,evidence:{}});await pending
    assert.equal(s.result.value,null);assert.equal(s.error.value,'')
  }finally{platformApi.amendProductionStageInputs=original;mounted.unmount()}
}))

test('explicit source edits are bounded to selected inputs; any draft edit invalidates confirmation',()=>modules(async vite=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeStageInputAmendment.vue')
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),before=platformApi.amendProductionStageInputs
  const source={kind:'structured',template:{kind:'literal',value:{direction:'Walk'}}}
  const props=reactive({runId:'run',stageId:'stage',planSha:sha,sources:[{index:0,sources:{core:source,images:{kind:'reviewed_references'}}}]})
  const calls:any[]=[],mounted=mountSetup(Component,props),s=mounted.state
  platformApi.amendProductionStageInputs=async(run,input)=>{calls.push(input);return {status:'proposed',sha256:sha,evidence:{run_plan_sha256:sha,policy:input.policy}}}
  try{
    s.policy.value='explicit_media_input_sources_v1';await nextTick()
    await s.request('preview');assert.equal(calls.length,0);assert.match(s.error.value,/选择并修改/)
    assert.equal(s.options.value.length,1);s.selected.value='0';s.add();s.add();assert.equal(s.drafts.value.length,1)
    s.drafts.value[0].text='{';await s.request('preview');assert.equal(calls.length,0);assert.ok(s.error.value)
    s.drafts.value[0].text=JSON.stringify({...source,template:{kind:'unit_map',value:source.template}})
    await s.request('preview');assert.equal(calls.length,1);assert.equal(calls[0].source_changes[0].input_key,'core')
    s.confirmed.value=true;s.drafts.value[0].text+=' ';assert.equal(s.result.value,null);assert.equal(s.confirmed.value,false)
    await s.request('apply');assert.equal(calls.length,1)
    await s.request('preview');s.policy.value='dialogue_missing_voiceover_is_false_v1';assert.equal(s.result.value,null)
    const html=await renderToString(createSSRApp(Component,props));assert.match(html,/每阶段仅允许一份/)
  }finally{platformApi.amendProductionStageInputs=before;mounted.unmount()}
}))

test('real mapping form adds catalog groups, reorders explicitly and does not rewrite purposes',()=>modules(async vite=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeReferenceMappingEditor.vue')
  const props=reactive({modelValue:{kind:'reviewed_references',stage_key:'',scope:{kind:''},groups:[]},
    input:{key:'images',kind:'image',semantic_contract:{paired_with_input_key:null,allowed_purposes:['identity'],allowed_subject_kinds:['character']}},
    priorStages:[{key:'review-check'}],options:{referenceSourceOptions:[
      {value:'characters',label:'已审核人物图',kind:'image',scopeKind:'shot',stageKey:'review-check',paired:false},
      {value:'props',label:'已审核道具图',kind:'image',scopeKind:'shot',stageKey:'review-check',paired:false},
      {value:'sound',label:'配套音频',kind:'audio',scopeKind:'shot',stageKey:'review-check',paired:true}]},
    purposeOptions:[{value:'identity',label:'主体一致性',description:'保持身份'}]})
  const setup=mountSetup(Component,props,(event,value)=>{if(event==='update:modelValue')props.modelValue=value})
  try{setup.state.add('characters');setup.state.add('props');await nextTick()
    assert.equal(props.modelValue.stage_key,'review-check');assert.deepEqual(props.modelValue.groups.map(row=>row.source_key),['characters','props'])
    setup.state.move(1,-1);assert.deepEqual(props.modelValue.groups.map(row=>row.source_key),['props','characters'])
    setup.state.descriptions(1,{target:{value:'自定义原文'}})
    assert.deepEqual(props.modelValue.groups[1].purposes,['自定义原文']);assert.equal(Object.hasOwn(props.modelValue.groups[1],'purpose'),false)
    setup.state.purpose(1,{target:{value:'identity'}})
    assert.equal(props.modelValue.groups[1].purpose,'identity')
    assert.deepEqual(props.modelValue.groups[1].purposes,['自定义原文'])
    const html=await renderToString(createSSRApp(Component,props));assert.ok(html.includes('主体一致性'));assert.ok(html.includes('组内保持审核顺序'));assert.equal(html.includes('value="sound"'),false)
    props.priorStages=[];await nextTick();assert.equal(setup.state.choices.value.length,0)
  }finally{setup.unmount()}
}))
test('actual preview discards late results, resets on new selection and exposes read errors',()=>modules(async vite=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeReferenceMappingPreview.vue')
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const before=platformApi.previewProductionReferenceMapping
  const props=reactive({run:{id:'run',planSource:'recipe',executionPlan:{sha256:sha,value:{recipe:{definition:{stages:[{key:'media_generation',routing:{routes:[{input_sources:{image:{kind:'reviewed_references'}}}]}}]}}}},stages:[{id:'stage',stageKey:'media_generation',outputSnapshotSha256:'x'}]}})
  const setup=mountSetup(Component,props)
  try{setup.state.stageId.value='stage';setup.state.shotId.value='shot';await nextTick()
    props.run.executionPlan.value.recipe.definition.stages[0].routing.routes[0].input_sources={units:{kind:'structured'}}
    assert.equal(setup.state.stages.value.length,1)
    setup.state.candidate.value=1
    let finish:any;platformApi.previewProductionReferenceMapping=()=>new Promise(resolve=>finish=resolve)
    const pending=setup.state.preview();setup.state.shotId.value='another';finish(preview());await pending
    assert.equal(setup.state.result.value,null)
    setup.state.shotId.value='shot';platformApi.previewProductionReferenceMapping=async()=>preview();await setup.state.preview();assert.equal(setup.state.result.value.status,'inputs_validated')
    platformApi.previewProductionReferenceMapping=async()=>{throw new Error('review changed')};await setup.state.preview();assert.equal(setup.state.result.value,null);assert.match(setup.state.error.value,/review changed/)
    const html=await renderToString(createSSRApp(Component,props));assert.ok(html.includes('逐镜输入映射'))
    const expanded=await renderToString(createSSRApp({...Component,setup(props,context){
      const state=Component.setup(props,context);state.open.value=true;return state
    }},props))
    assert.ok(expanded.includes('预览当前映射'));assert.ok(expanded.includes('已有准备的输入不会随上游更新'))
  }finally{platformApi.previewProductionReferenceMapping=before;setup.unmount()}
}))

test('route choice reads frozen instance routes, allows future stages and never auto-selects',()=>modules(async vite=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeExecutionRouteChoices.vue')
  const instance='12345678-1234-4123-8123-123456789abc'
  const route={execution_instance_version_id:instance,expected_definition_sha256:sha,input_sources:{},parameter_policies:[]}
  const props=reactive({busy:false,run:{id:'run',planSource:'recipe',status:'awaiting_approval',currentStageKey:'storyboard_review',
    executionPlan:{sha256:sha,value:{recipe:{definition:{schema_version:3,interaction_mode:'assisted',stages:[{key:'media_generation',mode:'existing_pipeline',approval:'required',
      routing:{mode:'manual_choice',variant_count:2,evaluator:null,routes:[route,{...route}]}}]}}}},
    stages:[{id:'stage',stageKey:'media_generation',status:'pending',selectedRouteIndex:null,routing:{routes:[{driver_type:'must-not-be-used'}]}}]}})
  const events:any[]=[],setup=mountSetup(Component,props,(name,value)=>events.push([name,value]))
  try{
    assert.equal(setup.state.selections.value['media_generation:primary'],null)
    setup.state.select('media_generation:primary');assert.equal(events.length,0)
    setup.state.selections.value['media_generation:primary']=1;setup.state.select('media_generation:primary')
    assert.deepEqual(events,[['select',{stageKey:'media_generation',routeIndex:1,appearanceRole:'primary'}]])
    const html=await renderToString(createSSRApp(Component,props))
    assert.ok(html.includes(instance));assert.ok(html.includes('参考素材审核前选择'));assert.equal(html.includes('must-not-be-used'),false)
    props.run.stages[0].status='running';setup.state.select('media_generation:primary');assert.equal(events.length,1)
    props.run.stages[0].status='pending';props.run.status='cancelled';setup.state.select('media_generation:primary');assert.equal(events.length,1)
    props.run.status='running';props.busy=true;setup.state.select('media_generation:primary');assert.equal(events.length,1)
  }finally{setup.unmount()}
}))

test('route choice never substitutes Stage routing when RunPlan is missing',()=>modules(async vite=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeExecutionRouteChoices.vue')
  const props={busy:false,run:{id:'run',planSource:'recipe',status:'running',stages:[{stageKey:'media_generation',routing:{routes:[{}]}}]}}
  const html=await renderToString(createSSRApp(Component,props))
  assert.ok(html.includes('role="alert"'));assert.equal(html.includes('确认执行实例'),false)
}))

test('polling preserves preview disclosure, selections and result; real context changes invalidate only result',()=>modules(async vite=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeReferenceMappingPreview.vue')
  const props=reactive({run:{id:'run',executionPlan:{sha256:sha},stages:[{id:'s',stageKey:'storyboard_review',status:'succeeded',outputSnapshotSha256:sha}]}})
  const setup=mountSetup(Component,props)
  try{
    setup.state.open.value=true;setup.state.stageId.value='s';setup.state.shotId.value='shot';await nextTick();setup.state.candidate.value=1;await nextTick()
    setup.state.result.value=preview();setup.state.targets.value=[{label:'shot'}]
    props.run=JSON.parse(JSON.stringify(props.run));await nextTick()
    assert.equal(setup.state.open.value,true);assert.equal(setup.state.shotId.value,'shot');assert.equal(setup.state.candidate.value,1);assert.equal(setup.state.result.value.status,'inputs_validated');assert.equal(setup.state.targets.value.length,1)
    props.run.stages[0].outputSnapshotSha256='b'.repeat(64);await nextTick()
    assert.equal(setup.state.result.value,null);assert.equal(setup.state.open.value,true);assert.equal(setup.state.shotId.value,'shot');assert.match(setup.state.contextNotice.value,/失效/)
    props.run.id='different';await nextTick();assert.equal(setup.state.open.value,false);assert.equal(setup.state.shotId.value,'');assert.equal(setup.state.targets.value.length,0)
  }finally{setup.unmount()}
}))

test('unapproved storyboard preview does not call the approved-input API; unmount rejects pending result',()=>modules(async vite=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeReferenceMappingPreview.vue')
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),before=platformApi.previewProductionReferenceMapping
  const props=reactive({run:{id:'run',currentStageKey:'storyboard_review',executionPlan:{sha256:sha},stages:[{id:'review',stageKey:'storyboard_review',status:'awaiting_approval'}]}})
  const setup=mountSetup(Component,props)
  let calls=0,finish:any
  platformApi.previewProductionReferenceMapping=()=>{calls++;return new Promise(resolve=>finish=resolve)}
  try{
    setup.state.stageId.value='stage';setup.state.shotId.value='shot';await nextTick();setup.state.candidate.value=1
    await setup.state.preview();assert.equal(calls,0);assert.match(setup.state.error.value,/仍待审核/)
    props.run.stages[0].status='succeeded';await nextTick();const pending=setup.state.preview();assert.equal(calls,1)
    setup.unmount();finish(preview());await pending;assert.equal(setup.state.result.value,null)
  }finally{platformApi.previewProductionReferenceMapping=before}
}))
