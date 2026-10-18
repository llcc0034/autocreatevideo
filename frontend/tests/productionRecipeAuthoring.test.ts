import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'

const root=fileURLToPath(new URL('..',import.meta.url)), sha='a'.repeat(64)
const shotSources=[{value:'upstream',label:'本轮上游素材结果',upstreamStageKey:'bible_image_generation',selectionOptions:[{value:'manual',label:'人工选择'},{value:'unique_setting',label:'唯一确切设定，否则人工接管'}]},
  ...['existing','upload'].map(value=>({value,label:value,upstreamStageKey:null,selectionOptions:[{value:'manual',label:'人工选择'}]}))]
const options={shotReferenceSourceOptions:shotSources,definitionSchemaVersion:3,effectiveLimits:{maxRoutingRoutes:3,maxVariants:4},stageOptions:[
  {value:'script_generate',label:'script_generate',repeatable:false,candidateEvaluatorOptions:[],modes:['ai'],requiresRouting:true,routingModes:['ai'],decisionPolicy:null},
  {value:'script_review',label:'script_review',repeatable:false,candidateEvaluatorOptions:[],modes:['review'],requiresRouting:false,routingModes:[],decisionPolicy:'review_registered_v1'},
],interactionModeOptions:[{value:'assisted',label:'人工辅助'},{value:'unattended',label:'无人值守'}],
approvalOptions:['auto','required','policy'].map(value=>({value,label:value})),routingModeOptions:[{value:'fixed',label:'固定'}],
materialSourceOptions:['generate','existing','upload'].map(value=>({value,label:value})),materialSelectionOptions:['manual','unique_version'].map(value=>({value,label:value})),fieldReviewPolicyOptions:[],sourceKindOptions:[],structureNodeOptions:[],textOutputOptions:[],referenceSourceOptions:[],parameterPolicyOptions:[],parameterSourceOptions:[],scopeOptions:[],presets:[]}
const base={schema_version:3,interaction_mode:'assisted',stages:[{key:'script_generate',mode:'ai',approval:'auto'}]}

test('shot rules require explicit source strategy and permission and never infer a later upstream',()=>modules(async m=>{
  const rule={source:'upstream',source_stage_key:'bible_image_generation',selection:'unique_setting',allow_override:false}
  const stage={key:'storyboard_review',mode:'review',approval:'required',shot_reference_selection:rule}
  const prior=[{key:'bible_image_generation',mode:'existing_pipeline',approval:'auto'}]
  const before=structuredClone(stage)
  assert.deepEqual(m.recipeShotReferenceErrors(stage,prior,options),[])
  for(const change of [{source:'latest'},{selection:'latest'},{allow_override:0},{allow_override:null},{source_stage_key:'future'},{extra:1}])
    assert.notDeepEqual(m.recipeShotReferenceErrors({...stage,shot_reference_selection:{...rule,...change}},prior,options),[])
  assert.notDeepEqual(m.recipeShotReferenceErrors(stage,[],options),[])
  assert.notDeepEqual(m.recipeShotReferenceErrors(stage,[{...prior[0],mode:'manual'}],options),[])
  for(const source of ['existing','upload']) {
    assert.deepEqual(m.recipeShotReferenceErrors({...stage,shot_reference_selection:{...rule,source,source_stage_key:null,selection:'manual'}},[],options),[])
    assert.notDeepEqual(m.recipeShotReferenceErrors({...stage,shot_reference_selection:{...rule,source}},prior,options),[])
  }
  assert.deepEqual(stage,before)
}))

test('real shot source component resets dependent fields only on explicit source change and honors readonly',()=>modules(async (_,vite)=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeShotReferencePolicy.vue')
  const props=reactive({options,disabled:false,prior:[{key:'bible_image_generation',mode:'existing_pipeline'}],modelValue:undefined as any})
  const writes:any[]=[];let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(event,value)=>{writes.push(structuredClone(value));props.modelValue=value}});return()=>h('div')}})
  try {
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await nextTick()
    assert.equal(writes.length,0)
    state.changeSource({target:{value:'upstream'}});await nextTick()
    assert.deepEqual(writes[0],{source:'upstream',source_stage_key:null,selection:'',allow_override:null})
    assert.equal(state.upstream.value.length,1)
    state.update({source_stage_key:'bible_image_generation',selection:'unique_setting',allow_override:false});await nextTick()
    state.changeSource({target:{value:'upload'}});await nextTick()
    assert.deepEqual(props.modelValue,{source:'upload',source_stage_key:null,selection:'',allow_override:false})
    assert.equal(state.source.value.selectionOptions.length,1)
    props.disabled=true;await nextTick()
    const count=writes.length;state.update({allow_override:true});state.changeSource({target:{value:'existing'}})
    assert.equal(writes.length,count)
  } finally {app.unmount()}
}))

test('shot source component displays missing upstream, explicit limitations and readonly controls',()=>modules(async (_,vite)=>{
  const {createSSRApp}=await import('vue'),{renderToString}=await import('@vue/server-renderer')
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeShotReferencePolicy.vue')
  const value={source:'upstream',source_stage_key:'bible_image_generation',selection:'unique_setting',allow_override:false}
  const html=await renderToString(createSSRApp(Component,{modelValue:value,options,prior:[],disabled:true}))
  assert.match(html,/前面没有可用的素材结果阶段/);assert.match(html,/运行详情.*逐镜选材/)
  assert.doesNotMatch(html,/执行接线尚未完成/)
  assert.match(html,/允许替换已有选择/);assert.equal((html.match(/<select[^>]* disabled/g)||[]).length,4)
  const absent=await renderToString(createSSRApp(Component,{modelValue:undefined,options:{...options,shotReferenceSourceOptions:[]},prior:[]}))
  assert.match(absent,/来源目录未就绪/);assert.equal(absent.includes('<select'),false)
}))

test('result source authoring preserves the other role and removes only explicitly replaced generation routes',()=>modules(async m=>{
  const stage={key:'bible_image_generation',mode:'existing_pipeline',approval:'auto',routing:{sentinel:1},variant_routing:{sentinel:2}}
  const original=structuredClone(stage)
  const primary=m.withRecipeMaterialSource(stage,'primary','existing',options)
  assert.deepEqual(primary.material_sources,{primary:'existing',variant:'generate'})
  assert.equal(primary.routing,undefined)
  assert.deepEqual(primary.variant_routing,stage.variant_routing)
  assert.deepEqual(m.recipeRoutingGroups(primary),[{key:'variant_routing',label:'变体'}])
  const both=m.withRecipeMaterialSource(primary,'variant','upload',options)
  assert.deepEqual(m.recipeRoutingGroups(both),[])
  assert.deepEqual(stage,original)
  assert.throws(()=>m.withRecipeMaterialSource(stage,'primary','upstream',options))
  assert.throws(()=>m.withRecipeMaterialSource(base.stages[0],'primary','existing',options))
  assert.deepEqual(m.recipeRoutingGroups(m.withRecipeMaterialSource(both,'primary','generate',options)),[{key:'routing',label:'主图'}])
}))

test('automatic result policy is explicit, role-local and never a generation candidate evaluator',()=>modules(async m=>{
  const stage={key:'bible_image_generation',mode:'existing_pipeline',approval:'auto',material_sources:{primary:'existing',variant:'upload'}}
  const primary=m.withRecipeMaterialSelectionPolicy(stage,'primary','unique_version',options)
  const both=m.withRecipeMaterialSelectionPolicy(primary,'variant','unique_version',options)
  assert.equal(m.recipeMaterialSelectionPolicy(stage,'primary'),'manual')
  assert.equal(m.recipeMaterialSelectionPolicy(both,'variant'),'unique_version')
  assert.equal(stage.material_selection,undefined)
  assert.deepEqual(m.withRecipeMaterialSource(both,'primary','generate',options).material_selection,{variant:'unique_version'})
  assert.throws(()=>m.withRecipeMaterialSelectionPolicy({...stage,material_sources:{primary:'generate',variant:'upload'}},'primary','unique_version',options))
  assert.throws(()=>m.withRecipeMaterialSelectionPolicy(stage,'primary','latest',options))
  assert.throws(()=>m.withRecipeMaterialSelectionPolicy(stage,'primary','unique_version',{...options,materialSelectionOptions:[]}))
  const opts={...options,stageOptions:[{value:'bible_image_generation',label:'素材',repeatable:false,candidateEvaluatorOptions:[],modes:['existing_pipeline'],routingModes:['existing_pipeline']}]}
  const draft={schema_version:3,interaction_mode:'assisted',stages:[both]}
  assert.deepEqual(m.recipeAuthoringErrors(draft,opts),[])
  for(const policies of [null,{},[],{primary:'latest'},{unknown:'manual'}]) assert.notDeepEqual(m.recipeAuthoringErrors({...draft,stages:[{...both,material_selection:policies}]},opts),[])
}))

test('malformed or contradictory material sources cannot be saved as a valid draft',()=>modules(async m=>{
  const opts={...options,stageOptions:[{value:'bible_image_generation',label:'素材',repeatable:false,candidateEvaluatorOptions:[],modes:['existing_pipeline'],routingModes:['existing_pipeline']}]}
  const stage={key:'bible_image_generation',mode:'existing_pipeline',approval:'auto',material_sources:{primary:'existing',variant:'upload'}}
  const draft={schema_version:3,interaction_mode:'assisted',stages:[stage]}
  assert.deepEqual(m.recipeAuthoringErrors(draft,opts),[])
  for(const bad of [{...stage,material_sources:{}},{...stage,routing:{}},{...stage,material_sources:{primary:'existing',variant:'automatic'}}])
    assert.notDeepEqual(m.recipeAuthoringErrors({...draft,stages:[bad]},opts),[])
}))

test('actual source editor confirms only the affected role and cancels stale or readonly changes',()=>modules(async (_,vite)=>{
  const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const previous=executionRegistryApi.versions
  executionRegistryApi.versions=async()=>({results:[],next:null,previous:null,count:0})
  const {default:Component}=await vite.ssrLoadModule('/src/components/ProductionRecipeEditor.vue')
  const stage={key:'bible_image_generation',mode:'existing_pipeline',approval:'auto',
    routing:{mode:'fixed',variant_count:1,evaluator:null,routes:[]},variant_routing:{mode:'fixed',variant_count:3,evaluator:null,routes:[]}}
  const props=reactive({disabled:false,modelValue:{schema_version:3,interaction_mode:'assisted',stages:[stage]},
    options:{...options,stageOptions:[{value:'bible_image_generation',label:'素材',repeatable:false,candidateEvaluatorOptions:[],modes:['existing_pipeline'],routingModes:['existing_pipeline']}]}})
  const writes:any[]=[];let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(event,value)=>{
    if(event==='update:modelValue'){writes.push(JSON.parse(JSON.stringify(value)));props.modelValue=value}
  }});return()=>h('div')}})
  try{
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await Promise.resolve();await nextTick()
    const input={value:'existing'}
    state.requestMaterialSource(0,'primary',{target:input})
    assert.equal(input.value,'generate');assert.equal(writes.length,0)
    state.pendingMaterialSource.value=null // Shared modal close/cancel action.
    state.confirmMaterialSource();assert.equal(writes.length,0)
    state.setMaterialSource(0,'primary','existing');state.confirmMaterialSource();await nextTick()
    assert.equal(writes.length,1);assert.equal(writes[0].stages[0].routing,undefined)
    assert.deepEqual(writes[0].stages[0].variant_routing,stage.variant_routing)
    state.setMaterialSource(0,'variant','upload')
    props.modelValue={...props.modelValue,stages:[{...props.modelValue.stages[0],approval:'required'}]}
    state.confirmMaterialSource();assert.equal(writes.length,1,'stale confirmation never removes changed routes')
    await nextTick();state.setMaterialSource(0,'variant','upload');props.disabled=true
    state.confirmMaterialSource();assert.equal(writes.length,1)
    await nextTick();assert.equal(state.pendingMaterialSource.value,null)
    props.disabled=false;await nextTick();state.setMaterialSource(0,'variant','upload');state.confirmMaterialSource();await nextTick()
    assert.equal(writes[1].stages[0].variant_routing,undefined)
    state.setMaterialSource(0,'primary','generate');await nextTick()
    assert.equal(writes[2].stages[0].routing,undefined,'returning to generation does not restore discarded inputs')
    assert.deepEqual(writes[2].stages[0].material_sources,{primary:'generate',variant:'upload'})
  } finally {app.unmount();executionRegistryApi.versions=previous}
}))

test('material sources require the explicit variant context, exact image port and consumed text',()=>modules(async m=>{
  const choices={...options,sourceKindOptions:['material_primary','material_difference','literal'].map(value=>({value,label:value}))}
  const contract={contextFields:[],sourceConstraints:{difference:{kinds:['literal'],selectors:null}}}
  const image={key:'image',kind:'image',min_items:1,max_items:1,media_source_policy:{allowed_sources:['asset_library'],default_source:'asset_library',allow_override:false},semantic_contract:{allowed_scope_kinds:['request'],paired_with_input_key:null}}
  const text={key:'difference',kind:'text',semantic_contract:{allowed_scope_kinds:['request']}}
  const material={primary_inputs:[{input_key:'image',target_key:'actual.image'}],difference_sources:[{kind:'logical_input',key:'difference'}]}
  assert.deepEqual(m.recipeSourceKindOptions(contract,image,choices,['bible_review']).map(row=>row.value),[])
  assert.deepEqual(m.recipeSourceKindOptions(contract,image,choices,[],material).map(row=>row.value),[])
  assert.deepEqual(m.recipeSourceKindOptions(contract,image,choices,['bible_review'],material).map(row=>row.value),['material_primary'])
  assert.deepEqual(m.recipeSourceKindOptions(contract,{...image,key:'other'},choices,['bible_review'],material),[])
  assert.deepEqual(m.recipeSourceKindOptions(contract,{...image,media_source_policy:{...image.media_source_policy,default_source:'asset_upload'}},choices,['bible_review'],material),[])
  assert.deepEqual(m.recipeSourceKindOptions(contract,text,choices,['bible_review'],material).map(row=>row.value),['material_difference','literal'])
  assert.match(m.recipeInputSourceIssue(contract,image,choices,['bible_review'],'material_primary'),/保存的来源/)
}))

test('material difference structure node is text only and cannot carry replacement prose',()=>modules(async (_,vite)=>{
  const m=await vite.ssrLoadModule('/src/services/recipeStructureMapping.ts')
  assert.deepEqual(m.initializeMapping('material_difference',{type:'string'}),{kind:'material_difference'})
  assert.deepEqual(m.mappingErrors({kind:'material_difference'},{type:'string'},'difference'),[])
  assert.match(m.mappingErrors({kind:'material_difference'},{type:'number'},'difference').join(' '),/类型/)
  assert.match(m.mappingErrors({kind:'material_difference',value:'override'},{type:'string'},'difference').join(' '),/正文/)
  assert.equal(m.mappingHasMaterialDifference({kind:'object',fields:{change:{kind:'material_difference'}}}),true)
  assert.equal(m.mappingHasMaterialDifference({kind:'literal',value:{kind:'material_difference'}}),false)
  const component=readFileSync(new URL('../src/components/RecipeStructureMappingEditor.vue',import.meta.url),'utf8')
  assert.match(component,/单元贡献字段/)
  assert.match(component,/单元贡献须使用“当前制作单元标识”/)
}))

test('recipe source choices intersect the exact protocol, implemented producers and prior reviewed scope',()=>modules(async m=>{
  const choices={...options,sourceKindOptions:['literal','context_field','selected_output','structured','reviewed_references','project_snapshot'].map(value=>({value,label:value})),
    textOutputOptions:[{outputKey:'rules.text',stageKey:'visual_rules_generate',kind:'text',scopeKind:'project',label:'Rules'}]}
  const input={key:'project.logline',kind:'text',semantic_contract:{allowed_scope_kinds:['project']}}
  const contract={contextFields:[],sourceConstraints:{'project.logline':{kinds:['project_snapshot'],selectors:null}}}
  const before=structuredClone({input,contract,choices})
  assert.deepEqual(m.recipeSourceKindOptions(contract,input,choices,[]).map(row=>row.value),['project_snapshot'])
  assert.equal(m.recipeInputSourceIssue(contract,input,choices,[],''),'')
  for(const kind of ['literal','context_field','selected_output'])assert.match(m.recipeInputSourceIssue(contract,input,choices,[],kind),/保存的来源不满足/)
  assert.deepEqual({input,contract,choices},before,'reading choices must not rewrite a saved source or contract')
  const general={...contract,sourceConstraints:{'project.logline':{kinds:['literal','context_field','selected_output','structured'],selectors:null}}}
  assert.deepEqual(m.recipeSourceKindOptions(general,input,choices,[]).map(row=>row.value),['literal'])
  assert.deepEqual(m.recipeSourceKindOptions(general,input,choices,['visual_rules_generate']).map(row=>row.value),['literal','selected_output'])
  assert.deepEqual(m.recipeSourceKindOptions(general,{...input,kind:'json'},choices,[]).map(row=>row.value),['literal','structured'])
  assert.deepEqual(m.recipeSourceKindOptions(general,{...input,kind:'image'},choices,[]).map(row=>row.value),['reviewed_references'])
  assert.match(m.recipeInputSourceIssue({...contract,sourceConstraints:{}},input,choices,[],''),/没有适用于/)
}))

test('recipe context menu obeys the exact input kind, scope and protocol instead of guessing a field',()=>modules(async m=>{
  const fields=[{selector_key:'production.entity.description',kind:'text',scope_kind:'request',revision_kind:'bible_approval'},
    {selector_key:'production.entity.content',kind:'json',scope_kind:'request',revision_kind:'bible_approval'},
    {selector_key:'script.content',kind:'text',scope_kind:'project',revision_kind:'script_revision'}]
  const contract={contextFields:fields,sourceConstraints:{brief:{kinds:['literal','context_field'],selectors:null}}}
  const input={key:'brief',kind:'text',semantic_contract:{allowed_scope_kinds:['request']}}
  const original=structuredClone({contract,input})
  assert.deepEqual(m.recipeContextFieldOptions(contract,input,['bible_review']),[fields[0]])
  assert.deepEqual(m.recipeContextFieldOptions(contract,{...input,kind:'json'},['bible_review']),[fields[1]])
  assert.deepEqual(m.recipeContextFieldOptions(contract,input,[]),[])
  assert.deepEqual(m.recipeContextFieldOptions({...contract,sourceConstraints:{}},input,['bible_review']),[])
  assert.deepEqual(m.recipeContextFieldOptions({...contract,sourceConstraints:{brief:{kinds:['literal'],selectors:null}}},input,['bible_review']),[])
  assert.deepEqual(m.recipeContextFieldOptions({...contract,sourceConstraints:{brief:{kinds:['context_field'],selectors:['script.content']}}},input,['bible_review']),[])
  assert.deepEqual({contract,input},original)
}))

const readinessFixture=()=>({schema_version:2,request_checked:false,status:'blocked',reason_codes:['execution_pipeline_incomplete'],recipes:[{
  id:'recipe-id',recipe_key:'fixture',version:1,state:'blocked',reason_codes:['execution_pipeline_incomplete'],validation_errors:[],capabilities:{script:{
    scope:'configuration_only',request_checked:false,state:'ready',required:true,reason_codes:[],
    warning_codes:['execution_capacity_unknown','execution_smoke_expired'],instance_count:1,viable_instance_count:1,
  }},
}]})

test('readiness parses explicit check scope and rejects old, missing or coerced facts',()=>modules(async (_,vite)=>{
  const {adaptProductionReadiness}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const current=readinessFixture(), parsed=adaptProductionReadiness(current)
  assert.equal(parsed.requestChecked,false);assert.equal(parsed.recipes[0].capabilities.script.instanceCount,1)
  assert.deepEqual(parsed.recipes[0].capabilities.script.warningCodes,['execution_capacity_unknown','execution_smoke_expired'])
  for(const changes of [{schema_version:1},{request_checked:true},{request_checked:undefined},{recipes:null},{reason_codes:[1]}]){
    assert.throws(()=>adaptProductionReadiness({...current,...changes}))
  }
  for(const changes of [{scope:'runtime'},{request_checked:true},{instance_count:'1'},{warning_codes:undefined},{state:'success'}]){
    const changed=readinessFixture();Object.assign(changed.recipes[0].capabilities.script,changes)
    assert.throws(()=>adaptProductionReadiness(changed))
  }
  const unknown=readinessFixture();Object.assign(unknown.recipes[0].capabilities.script,{instance_count:null,viable_instance_count:null})
  assert.equal(adaptProductionReadiness(unknown).recipes[0].capabilities.script.instanceCount,null)
}))

test('actual recipe details show check limits and warnings and reject stale success after read failure',()=>modules(async (_,vite)=>{
  const {createSSRApp}=await import('vue'), {renderToString}=await import('@vue/server-renderer')
  const {default:Component}=await vite.ssrLoadModule('/src/components/ProductionRecipeFlow.vue')
  const {adaptProductionReadiness}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const readiness=adaptProductionReadiness(readinessFixture()).recipes[0]
  const props={recipe:{definition:base,definitionSha256:sha},recipeEditorOptions:options,readiness,readinessReportState:'loaded'}
  const rendered=await renderToString(createSSRApp(Component,props))
  for(const text of ['配置与证据核对','不代表本次执行已获准','剧本（script）','关联实例 1 项','容量需在实际执行准备时核对','组合验收已过期'])assert.ok(rendered.includes(text),text)
  assert.equal(rendered.includes('未识别媒体类型'),false)
  const failed=await renderToString(createSSRApp(Component,{...props,readinessReportState:'error'}))
  assert.ok(failed.includes('读取失败'));assert.equal(failed.includes('关联实例 1 项'),false)
}))
test('recipe validation details preserve exact identity and original field errors, never stale details after failure',()=>modules(async (_,vite)=>{
  const {createSSRApp}=await import('vue'), {renderToString}=await import('@vue/server-renderer')
  const {default:Component}=await vite.ssrLoadModule('/src/components/ProductionRecipeFlow.vue')
  const {adaptProductionReadiness}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const fixture=readinessFixture(), error={phase:'definition',field_path:'route.expected_definition_sha256',
    message:'执行定义已变化，请重新读取并确认',code:null,stage_index:1,stage_key:'exact-step-two',stage_kind:'structured_text',routing_key:'routing',route_index:0}
  fixture.recipes[0].validation_errors=[error] as any
  const readiness=adaptProductionReadiness(fixture).recipes[0]
  assert.equal(readiness.validationErrors[0].stageKey,'exact-step-two')
  assert.equal(readiness.validationErrors[0].fieldPath,error.field_path)
  const props={recipe:{definition:base,definitionSha256:sha},recipeEditorOptions:options,readiness,readinessReportState:'loaded'}
  const html=await renderToString(createSSRApp(Component,props))
  for(const text of ['exact-step-two','route.expected_definition_sha256',error.message,'查看此配方版本的合同错误'])assert.ok(html.includes(text),text)
  const failed=await renderToString(createSSRApp(Component,{...props,readinessReportState:'error'}))
  assert.equal(failed.includes(error.message),false)
  for(const changes of [{phase:'runtime'},{stage_index:-1},{stage_key:undefined},{route_index:'0'},{message:undefined},{field_path:''}]){
    const invalid=structuredClone(fixture);Object.assign(invalid.recipes[0].validation_errors[0],changes)
    assert.throws(()=>adaptProductionReadiness(invalid))
  }
  const whole=structuredClone(fixture);Object.assign(whole.recipes[0].validation_errors[0],{stage_index:null,stage_key:null,stage_kind:null,routing_key:null,route_index:null})
  assert.equal(adaptProductionReadiness(whole).recipes[0].validationErrors[0].stageIndex,null)
}))
async function modules(fn) {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true}})
  try {await fn(await vite.ssrLoadModule('/src/services/productionRecipeAuthoring.ts'),vite)} finally {await vite.close()}
}

async function stageEditor(vite, definition, fn) {
  const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const previous=executionRegistryApi.versions
  executionRegistryApi.versions=async()=>({results:[],next:null,previous:null,count:0})
  const {default:Component}=await vite.ssrLoadModule('/src/components/ProductionRecipeEditor.vue')
  const props=reactive({disabled:false,modelValue:structuredClone(definition),options:{...options,stageOptions:[...options.stageOptions,
    ...['assembly','delivery'].map(value=>({value,label:value,repeatable:false,candidateEvaluatorOptions:[],modes:['existing_pipeline','manual'],routingModes:[]}))]}})
  const writes:any[]=[];let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(event,value)=>{
    if(event==='update:modelValue'){writes.push(JSON.parse(JSON.stringify(value)));props.modelValue=value}
  }});return()=>h('div')}})
  try {
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await Promise.resolve();await nextTick()
    await fn(state,props,writes)
  } finally {app.unmount();executionRegistryApi.versions=previous}
}

test('stage removal requires shared confirmation and preserves unrelated raw inputs and references',()=>modules(async (_,vite)=>{
  const definition={...base,stages:[{...base.stages[0],routing:{mode:'fixed',routes:[],raw:'  原文\n'}},{key:'script_review',mode:'review',approval:'required'}]}
  await stageEditor(vite,definition,async(state,props,writes)=>{
    state.removeStage(0)
    assert.equal(writes.length,0);assert.deepEqual(JSON.parse(JSON.stringify(props.modelValue)),definition)
    assert.match(state.pendingStageEdit.value.message,/引用不会自动删除/)
    state.pendingStageEdit.value=null // Shared modal close / cancel.
    state.confirmStageEdit();assert.equal(writes.length,0)
    state.removeStage(1);state.confirmStageEdit();state.confirmStageEdit();await nextTick()
    assert.equal(writes.length,1);assert.deepEqual(writes[0],{...definition,stages:[definition.stages[0]]})
    assert.equal(state.pendingStageEdit.value,null)
  })
}))

test('stage confirmation rejects changed definitions, reorder, catalog and readonly state before watcher flush',()=>modules(async (_,vite)=>{
  const definition={...base,stages:[base.stages[0],{key:'script_review',mode:'review',approval:'required'}]}
  await stageEditor(vite,definition,async(state,props,writes)=>{
    const mutate=[
      ()=>{props.modelValue.interaction_mode='unattended'},
      ()=>{props.modelValue.stages.reverse()},
      ()=>{props.options={...props.options,approvalOptions:[]}},
      ()=>{props.disabled=true},
    ]
    for(const change of mutate) {
      props.disabled=false;props.modelValue=structuredClone(definition);await nextTick()
      state.removeStage(0);assert.ok(state.pendingStageEdit.value)
      change();state.confirmStageEdit()
      assert.equal(writes.length,0);await nextTick();assert.equal(state.pendingStageEdit.value,null)
    }
    props.disabled=false;await nextTick();state.removeStage(0)
    props.modelValue={...props.modelValue,interaction_mode:'unattended'};await nextTick()
    assert.equal(state.pendingStageEdit.value,null,'changed draft closes visible confirmation')
    props.disabled=true;await nextTick();state.removeStage(0);state.changeKind(0,'delivery');state.updateStage(0,{mode:'manual'})
    assert.equal(writes.length,0);assert.equal(state.pendingStageEdit.value,null)
  })
}))

test('kind selector stays on saved duty until confirmation and never carries another duty route',()=>modules(async (m,vite)=>{
  const original={...base.stages[0],routing:{mode:'fixed',routes:[],raw:' unchanged '},variant_routing:{routes:[]}}
  await stageEditor(vite,{...base,stages:[original]},async(state,props,writes)=>{
    const select={value:'script_review'}
    state.requestKind(0,{target:select});assert.equal(select.value,'script_generate');assert.equal(writes.length,0)
    state.pendingStageEdit.value=null;state.confirmStageEdit();assert.equal(writes.length,0)
    select.value='script_review';state.requestKind(0,{target:select});state.confirmStageEdit();await nextTick()
    assert.deepEqual(writes[0].stages,[m.recipeStageWithKind(original,'script_review',props.options,()=>{throw new Error('singleton must not generate key')})])
    assert.equal(writes[0].stages[0].routing,undefined)
    state.updateStage(0,{approval:'required'});await nextTick()
    assert.equal(writes.length,2,'non-destructive edits still update immediately')
    assert.equal(state.pendingStageEdit.value,null)
  })
}))

test('assembly and delivery mode changes retain exact mappings until explicit confirmation',()=>modules(async (_,vite)=>{
  for(const key of ['assembly','delivery']) {
    const fields=key==='assembly'?['assembly_output','assembly_timing','assembly_media']:['delivery_metadata']
    const stage={key,mode:'existing_pipeline',approval:'auto',...Object.fromEntries(fields.map(field=>[field,{source_text:`  ${field}\n原文`,mapping:{schema_version:1,node:{kind:'literal',value:{title:'kept'}}}}]))}
    await stageEditor(vite,{...base,stages:[stage]},async(state,props,writes)=>{
      const select={value:'manual'}
      state.requestMode(0,{target:select});assert.equal(select.value,'existing_pipeline')
      assert.equal(writes.length,0);assert.deepEqual(JSON.parse(JSON.stringify(props.modelValue.stages[0])),stage)
      state.pendingStageEdit.value=null;state.confirmStageEdit();assert.equal(writes.length,0)
      select.value='manual';state.requestMode(0,{target:select});state.confirmStageEdit();await nextTick()
      assert.equal(writes.length,1);assert.deepEqual(writes[0].stages,[{key,mode:'manual',approval:'auto'}])
    })
  }
}))

test('stage editor uses in-page confirmation for destructive edits instead of native dialogs',()=>{
  const source=readFileSync(new URL('../src/components/ProductionRecipeEditor.vue',import.meta.url),'utf8')
  assert.doesNotMatch(source,/window\.confirm/)
  assert.match(source,/<BaseModal :open="Boolean\(pendingStageEdit\)"/)
  assert.match(source,/@close="pendingStageEdit=null"/)
  assert.match(source,/@click="confirmStageEdit"/)
  assert.match(source,/仅修改当前编辑草稿，确认后仍需保存/)
})
test('schema3 authoring distinguishes incomplete draft from wrong schema and old resource pairs',()=>modules(async m=>{
  assert.deepEqual(m.recipeAuthoringErrors(base,options),[])
  for(const version of [1,2,true,null])assert.match(m.recipeAuthoringErrors({...base,schema_version:version},options).join(' '),/schema3/)
  const stage={...base.stages[0],routing:{mode:'fixed',variant_count:1,evaluator:null,routes:[{model_deployment_id:'old',workflow_version_id:'old'}]}}
  assert.match(m.recipeAuthoringErrors({...base,stages:[stage]},options).join(' '),/实例配置只接受/)
  stage.routing.routes=[]
  assert.deepEqual(m.recipeAuthoringErrors({...base,stages:[stage]},options),[])
}))
test('selected exact contract never copies workflow, profile, smoke fixture or parameters into a new route',()=>modules(async m=>{
  const route=m.recipeRouteForContract({versionId:'exact-version',sha256:sha,parameters:[{key:'seed',default:100}],inputs:[{key:'ref'}]})
  assert.deepEqual(route,{execution_instance_version_id:'exact-version',expected_definition_sha256:sha,input_sources:{},parameter_policies:[]})
  assert.equal(Object.hasOwn(route,'prompt_profile_binding_id'),false)
}))

test('parameter policy controls keep source and value exclusive and use only catalog choices',()=>modules(async (_,vite)=>{
  const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const previous=executionRegistryApi.contract
  let reads=0
  executionRegistryApi.contract=async()=>{reads++;throw new Error('isolated transport unavailable')}
  const {default:Component}=await vite.ssrLoadModule('/src/components/ProductionRecipeRouteEditor.vue')
  const source={selectorKey:'project.width',label:'项目宽度',scopeKind:'project',type:'integer',unit:'px',sourceKind:'context_scalar',parameterKey:null,stageKey:null,description:'原始单位'}
  const props=reactive({modelValue:{execution_instance_version_id:'exact',expected_definition_sha256:sha,
    input_sources:{},parameter_policies:[{key:'width',policy:'locked_value',value:0}]},
    options:{...options,parameterSourceOptions:[source]},priorStages:[]})
  const writes=[],validities=[];let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(event,value)=>{
    if(event==='update:modelValue'){writes.push(JSON.parse(JSON.stringify(value)));props.modelValue=value}
    if(event==='validity'){
      validities.push(value)
      props.priorStages=props.priorStages.map(row=>({...row}))
    }
  }});return()=>h('div')}})
  try{
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await Promise.resolve();await nextTick()
    assert.match(state.error.value,/unavailable/);assert.equal(state.contract.value,null)
    state.setPolicyKind('width','default',{default:512});assert.equal(writes.length,0,'unavailable contract must not permit parameter writes')
    state.error.value=''
    state.contract.value={parameters:[{key:'width',rule:{type:'integer',required:true,default:512}}],inputs:[],contextFields:[],sourceConstraints:{},purposes:{}}
    assert.deepEqual(state.parameterSources('integer'),[source]);assert.deepEqual(state.parameterSources('number'),[source])
    assert.deepEqual(state.parameterSources('string'),[]);assert.deepEqual(state.parameterSources('boolean'),[])
    state.setPolicyKind('width','default',{default:512});assert.equal(writes.at(-1).parameter_policies[0].value,0)
    state.setPolicyKind('width','locked_source',{default:512})
    assert.deepEqual(writes.at(-1).parameter_policies,[{key:'width',policy:'locked_source',source:{kind:'context_scalar',selector_key:'',scope:{kind:'project'}}}])
    state.setParameterSource('width',source.selectorKey)
    assert.equal(writes.at(-1).parameter_policies[0].source.selector_key,'project.width')
    const count=writes.length;state.setParameterSource('width','$.arbitrary')
    assert.equal(writes.length,count)
    state.setParameterSource('width','');assert.equal(writes.at(-1).parameter_policies[0].source.selector_key,'')
    state.setPolicyKind('width','locked_value',{const:0,default:512})
    assert.deepEqual(writes.at(-1).parameter_policies,[{key:'width',policy:'locked_value',value:0}])
    state.setPolicyKind('width','',{});assert.deepEqual(writes.at(-1).parameter_policies,[])
    assert.equal(writes.at(-1).expected_definition_sha256,sha)
    state.updateSource('brief',{kind:'literal',value:'Approved story'})
    await nextTick();await Promise.resolve()
    assert.equal(reads,1,'editing sources and policies must not reload the same contract')
    props.modelValue={...props.modelValue,expected_definition_sha256:'b'.repeat(64)}
    await nextTick();await Promise.resolve()
    assert.equal(reads,2,'a changed contract digest must reload')
    props.modelValue={...props.modelValue,execution_instance_version_id:'another-exact'}
    await nextTick();await Promise.resolve()
    assert.equal(reads,3,'a changed version identity must reload')
    const derived={selectorKey:'shot.duration_ms',label:'按已审核镜头时长计算帧数',scopeKind:'shot',type:'integer',unit:'frames',sourceKind:'workflow_frames',parameterKey:'video.frames',stageKey:'media_generation',description:'审核来源和计算结果首次冻结'}
    props.options.parameterSourceOptions.push(derived)
    assert.deepEqual(state.parameterSources('integer','video.frames'),[source])
    props.stageKey='media_generation'
    state.error.value=''
    state.contract.value={framePolicy:{fps:24},parameters:[{key:'video.frames',rule:{type:'integer'}}]}
    assert.deepEqual(state.parameterSources('integer','video.frames'),[source,derived])
    assert.deepEqual(state.parameterSources('integer','seed'),[source])
    state.setParameterSource('video.frames','shot.duration_ms')
    assert.deepEqual(writes.at(-1).parameter_policies,[{key:'video.frames',policy:'locked_source',source:{kind:'workflow_frames',selector_key:'shot.duration_ms',scope:{kind:'shot'}}}])
    const beforeInvalid=writes.length
    state.setParameterSource('seed','shot.duration_ms');assert.equal(writes.length,beforeInvalid)
    state.contract.value={framePolicy:null,parameters:[]}
    assert.deepEqual(state.parameterSources('integer','video.frames'),[source])
    const stored=JSON.parse(JSON.stringify(props.modelValue)), writeCount=writes.length
    state.contract.value={inputs:[{key:'brief',kind:'text',semantic_contract:{allowed_scope_kinds:['project']}}],
      contextFields:[],sourceConstraints:{brief:{kinds:['project_snapshot'],selectors:null}},parameters:[]}
    await nextTick()
    assert.match(validities.at(-1).join(' '),/brief.*保存的来源不满足/)
    assert.deepEqual(props.modelValue,stored);assert.equal(writes.length,writeCount)
    assert.deepEqual(state.sourceChoices(state.contract.value.inputs[0]),[])
    state.updateSource('brief');await nextTick()
    assert.equal(validities.at(-1).some(message=>message.includes('保存的来源')),false,'unconfigured draft remains saveable')
    assert.match(state.sourceIssue(state.contract.value.inputs[0]),/没有适用于/)
    const validityCount=validities.length
    props.priorStages=[...props.priorStages];await nextTick();await nextTick()
    assert.equal(validities.length,validityCount,'equivalent parent render props must not trigger a validity feedback loop')
    const view=readFileSync(new URL('../src/components/ProductionRecipeRouteEditor.vue',import.meta.url),'utf8')
    assert.ok(view.includes('source.unit'));assert.ok(view.includes('不会改用默认值'))
    assert.ok(view.includes('parameter.rule.seed_policy'))
  }finally{app.unmount();executionRegistryApi.contract=previous}
}))
test('stage catalog owns routing requirements and automatic review is explicit',()=>modules(async m=>{
  const routing={mode:'fixed',variant_count:1,evaluator:null,routes:[]}
  assert.match(m.recipeAuthoringErrors({...base,stages:[{key:'script_review',mode:'review',approval:'required',routing}]},options).join(' '),/不能配置执行实例/)
  for (const key of ['script_normalize', 'script_normalize_review', 'prompt_compile']) {
    assert.ok(m.recipeAuthoringErrors({...base,stages:[{key,mode:'manual',approval:'required'}]},options).length)
  }
  const unattended={...base,interaction_mode:'unattended',stages:[{key:'script_review',mode:'review',approval:'policy',decision_policy:'review_registered_v1'}]}
  assert.deepEqual(m.recipeAuthoringErrors(unattended,options),[])
  assert.match(m.recipeAuthoringErrors({...unattended,stages:[{...unattended.stages[0],decision_policy:'field_approval'}]},options).join(' '),/需要明确/)
  assert.match(m.recipeAuthoringErrors({...unattended,interaction_mode:'assisted'},options).join(' '),/仅适用于无人值守/)
}))
test('out of order, duplicate stages and invalid fixed route counts remain visible',()=>modules(async m=>{
  assert.match(m.recipeAuthoringErrors({...base,stages:[base.stages[0],base.stages[0]]},options).join(' '),/重复/)
  const route=m.recipeRouteForContract({versionId:'id',sha256:sha})
  assert.match(m.recipeAuthoringErrors({...base,stages:[{...base.stages[0],routing:{mode:'fixed',variant_count:1,routes:[route,route]}}]},options).join(' '),/只能配置一个/)
}))
test('editing and semantic readonly details retain exact instance references without legacy graph expansion',()=>{
  const read=path=>readFileSync(new URL('../src/'+path,import.meta.url),'utf8')
  const editor=read('components/ProductionRecipeEditor.vue'), route=read('components/ProductionRecipeRouteEditor.vue')
  assert.ok(editor.includes('executionRegistryApi.versions'))
  assert.ok(editor.includes('recipeRouteForContract(contract)'))
  assert.ok(route.includes('executionRegistryApi.contract'))
  assert.ok(route.includes('value.sha256 !== props.modelValue.expected_definition_sha256'))
  assert.ok(route.includes('executionRuleDescription(parameter.rule)'))
  const flow=read('components/ProductionRecipeFlow.vue')
  assert.ok(flow.includes('<ProductionRecipeDefinitionView'))
  assert.equal(flow.includes('<ProductionRecipeEditor'),false)
  assert.ok(read('components/ProductionRecipeDefinitionView.vue').includes("inspect('execution', route.execution_instance_version_id"))
  for(const old of ['model_deployment_id','workflowOptions','hydrateRecipeWorkflowDetails'])assert.equal(editor.includes(old),false)
})

test('readonly recipe editor renders a schema3 draft without models or generating a hidden route',()=>modules(async (m,vite)=>{
  const {createSSRApp}=await import('vue'), {renderToString}=await import('@vue/server-renderer')
  const {default:component}=await vite.ssrLoadModule('/src/components/ProductionRecipeEditor.vue')
  const input=structuredClone(base)
  const html=await renderToString(createSSRApp(component,{modelValue:input,
    options:{...options,approvalOptions:[{value:'auto',label:'目录中的自动审核'},{value:'required',label:'目录中的人工审核'}]},disabled:true}))
  assert.match(html,/制作方式/)
  assert.match(html,/精确执行实例版本/)
  assert.match(html,/目录中的自动审核/)
  assert.match(html,/目录中的人工审核/)
  assert.equal(html.includes('未识别模式（auto）'),false)
  assert.equal(html.includes('添加上方选中的实例版本'),false)
  assert.deepEqual(input,base)
}))

test('recipe catalog owns numeric limits without requiring Comfy conditioning modes',()=>modules(async (m,vite)=>{
  const {adaptProductionRecipeEditorOptions:adapt}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const raw={definition_schema_version:3,effective_limits:{max_routing_routes:2,max_variants:7,max_generation_tasks:100},
    assembly_output_schema:{type:'object',properties:{width:{type:'integer'},height:{type:'integer'},fps:{type:'integer'}}},
    assembly_timing_schema:{type:'object',properties:{episodes:{type:'array'}}},
    assembly_media_schema:{type:'object',properties:{episodes:{type:'array'}}},
    delivery_metadata_schema:{type:'object',properties:{title:{type:'string'},notes:{type:'string'}}},
    shot_reference_source_options:shotSources.map(row=>({value:row.value,label:row.label,upstream_stage_key:row.upstreamStageKey,selection_options:row.selectionOptions})),
    stage_options:options.stageOptions.map(row=>({value:row.value,modes:row.modes,requires_routing:row.requiresRouting,
      routing_modes:row.routingModes,decision_policy:row.decisionPolicy,label:row.label,repeatable:row.repeatable,candidate_evaluator_options:row.candidateEvaluatorOptions})),
    approval_options:options.approvalOptions,interaction_mode_options:options.interactionModeOptions,
    material_source_options:options.materialSourceOptions,material_selection_options:options.materialSelectionOptions,field_review_policy_options:[],source_kind_options:[{value:'literal',label:'固定内容'}],structure_node_options:[{value:'literal',label:'明确固定值'}],text_output_options:[],reference_source_options:[],
    parameter_policy_options:[{value:'default',label:'默认'}],parameter_source_options:[],scope_options:[{value:'project',label:'项目'}],
    routing_mode_options:options.routingModeOptions,presets:[{value:'draft',label:'草稿',stages:base.stages}],conditioning_modes:[]}
  assert.deepEqual(adapt(raw).effectiveLimits,{maxRoutingRoutes:2,maxVariants:7,maxGenerationTasks:100})
  assert.deepEqual(adapt(raw).materialSelectionOptions,options.materialSelectionOptions)
  assert.deepEqual(adapt(raw).shotReferenceSourceOptions,shotSources)
  assert.deepEqual(adapt(raw).assemblyOutputSchema,raw.assembly_output_schema)
  assert.deepEqual(adapt(raw).assemblyTimingSchema,raw.assembly_timing_schema)
  assert.deepEqual(adapt(raw).assemblyMediaSchema,raw.assembly_media_schema)
  assert.throws(()=>adapt({...raw,assembly_media_schema:undefined}),/assembly_media_schema/)
  assert.deepEqual(adapt(raw).deliveryMetadataSchema,raw.delivery_metadata_schema)
  assert.throws(()=>adapt({...raw,delivery_metadata_schema:undefined}),/delivery_metadata_schema/)
  assert.throws(()=>adapt({...raw,assembly_timing_schema:undefined}),/assembly_timing_schema/)
  assert.throws(()=>adapt({...raw,assembly_output_schema:undefined}),/assembly_output_schema/)
  assert.throws(()=>adapt({...raw,shot_reference_source_options:undefined}),/shot_reference_source_options/)
  assert.throws(()=>adapt({...raw,shot_reference_source_options:[{value:'upstream',label:'来源'}]}),/来源目录/)
  assert.throws(()=>adapt({...raw,material_selection_options:undefined}),/material_selection_options/)
  for (const change of [{repeatable:undefined},{repeatable:'true'},{label:''},{candidate_evaluator_options:undefined}]) {
    assert.throws(()=>adapt({...raw,stage_options:[{...raw.stage_options[0],...change}]}))
  }
  assert.throws(()=>adapt({...raw,effective_limits:{max_routing_routes:2,max_variants:7}}),/effective_limits/)
  const output={output_key:'generated_script.script.content',stage_key:'script_generate',kind:'text',scope_kind:'project',label:'已采用剧本全文'}
  assert.deepEqual(adapt({...raw,text_output_options:[output]}).textOutputOptions,[{
    outputKey:output.output_key,stageKey:output.stage_key,kind:output.kind,scopeKind:output.scope_kind,label:output.label}])
  assert.throws(()=>adapt({...raw,text_output_options:undefined}),/text_output_options/)
  assert.throws(()=>adapt({...raw,reference_source_options:undefined}),/reference_source_options/)
  const reference={value:'videos',label:'已审核视频',kind:'video',scope_kind:'shot',stage_key:'storyboard_review',paired:false}
  assert.equal(adapt({...raw,reference_source_options:[reference]}).referenceSourceOptions[0].stageKey,'storyboard_review')
  assert.throws(()=>adapt({...raw,reference_source_options:[{...reference,paired:'false'}]}),/参考来源/)
  assert.throws(()=>adapt({...raw,text_output_options:[{...output,output_key:null}]}),/目录/)
  const source={selector_key:'project.width',label:'项目宽度',scope_kind:'project',type:'integer',unit:'px',source_kind:'context_scalar',parameter_key:null,stage_key:null,description:'原始单位'}
  assert.deepEqual(adapt({...raw,parameter_source_options:[source]}).parameterSourceOptions,[{
    selectorKey:source.selector_key,label:source.label,scopeKind:source.scope_kind,type:source.type,unit:source.unit,sourceKind:'context_scalar',parameterKey:null,stageKey:null,description:'原始单位'}])
  assert.throws(()=>adapt({...raw,parameter_source_options:undefined}),/parameter_source_options/)
  for(const key of Object.keys(source).filter(key=>!['parameter_key','stage_key'].includes(key)))assert.throws(()=>adapt({...raw,parameter_source_options:[{...source,[key]:null}]}),/来源目录/)
  for(const key of ['parameter_key','stage_key'])assert.throws(()=>adapt({...raw,parameter_source_options:[{...source,[key]:undefined}]}),/来源目录/)
  for(const key of ['max_variants','max_routing_routes'])for(const value of [undefined,null,0,-1,1.5,true,'7']) {
    assert.throws(()=>adapt({...raw,effective_limits:{...raw.effective_limits,[key]:value}}),/effective_limits/)
  }
  assert.throws(()=>adapt({...raw,effective_limits:undefined}),/effective_limits/)
}))

test('media author only belongs to assembly and preserves exact raw fields',()=>modules(async m=>{
  const opts={...options,stageOptions:[{value:'assembly',label:'装配',repeatable:false,candidateEvaluatorOptions:[],modes:['existing_pipeline'],routingModes:[],requiresRouting:false}]}
  const clip={output_version_id:'00000000-0000-4000-8000-000000000001',expected_contract_sha256:sha,
    source_text:' {"片段":{"episodes":[]},"说明":"  原文  "} ',mapping:{schema_version:1,node:{kind:'path',scope:'root',path:['片段']}}}
  // Shape/placement only: actual target values must still pass the authoritative backend projection.
  const value={schema_version:3,interaction_mode:'assisted',stages:[{key:'assembly',mode:'existing_pipeline',approval:'auto',assembly_media:clip}]}
  const original=structuredClone(value);assert.deepEqual(m.recipeAuthoringErrors(value,opts),[]);assert.deepEqual(value,original)
  assert.notDeepEqual(m.recipeAuthoringErrors({...value,stages:[{...base.stages[0],assembly_media:clip}]},options),[])
}))

test('delivery author validates placement and parent edits preserve raw fields and readonly state',()=>modules(async (m,vite)=>{
  const opts={...options,stageOptions:[{value:'delivery',label:'交付',repeatable:false,candidateEvaluatorOptions:[],modes:['existing_pipeline'],routingModes:[],requiresRouting:false}]}
  const author={output_version_id:'00000000-0000-4000-8000-000000000001',expected_contract_sha256:sha,
    source_text:'{ "原题":"  雨夜 🌧  ", "说明":"原文\\r\\nΔ", "custom":false }',
    mapping:{schema_version:1,node:{kind:'object',fields:{title:{kind:'path',scope:'root',path:['原题']},notes:{kind:'path',scope:'root',path:['说明']}}}}}
  const value={schema_version:3,interaction_mode:'assisted',stages:[{key:'delivery',mode:'existing_pipeline',approval:'auto',delivery_metadata:author}]}
  assert.deepEqual(m.recipeAuthoringErrors(value,opts),[])
  assert.notDeepEqual(m.recipeAuthoringErrors({...value,stages:[{...base.stages[0],delivery_metadata:author}]},options),[])
  const {default:Component}=await vite.ssrLoadModule('/src/components/ProductionRecipeEditor.vue')
  const props=reactive({modelValue:structuredClone(value),options:opts,disabled:false})
  let state:any; const writes:any[]=[]
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(event,next)=>{
    if(event==='update:modelValue'){props.modelValue=next;writes.push(JSON.parse(JSON.stringify(next)))}}
  });return()=>h('div')}})
  try {
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await nextTick()
    state.setAssemblyOutput(0,author,'delivery_metadata');await nextTick()
    assert.deepEqual(writes.at(-1).stages[0].delivery_metadata,author)
    props.disabled=true;await nextTick();const count=writes.length
    state.setAssemblyOutput(0,undefined,'delivery_metadata');assert.equal(writes.length,count)
    props.disabled=false;await nextTick();state.setAssemblyOutput(0,undefined,'delivery_metadata');await nextTick()
    assert.equal(writes.at(-1).stages[0].delivery_metadata,undefined)
  }finally{app.unmount()}
}))

test('creation overview reads frozen exact instances and renders without mutable selectors',()=>modules(async (_,vite)=>{
  const {recipeExecutionOverview:read}=await vite.ssrLoadModule('/src/services/recipeExecutionOverview.ts')
  const route={execution_instance_version_id:'11111111-1111-4111-8111-111111111111',expected_definition_sha256:sha,
    input_sources:{story:{kind:'literal',value:'既定故事'}},parameter_policies:[{key:'seed',policy:'fixed',value:0}]}
  const definition={...base,stages:[{...base.stages[0],routing:{mode:'fixed',variant_count:1,evaluator:null,routes:[route]}},
    {key:'script_review',mode:'review',approval:'required'}]}
  const original=structuredClone(definition)
  assert.deepEqual(read(definition),{error:'',stages:[{
    key:'script_generate',mode:'ai',approval:'auto',routingMode:'fixed',routes:[{
      versionId:route.execution_instance_version_id,definitionSha256:sha,inputKeys:['story'],parameterCount:1,
    }],
  },{key:'script_review',mode:'review',approval:'required',routingMode:null,routes:[]}]})
  const {createSSRApp}=await import('vue'),{renderToString}=await import('@vue/server-renderer')
  const {default:component}=await vite.ssrLoadModule('/src/components/RecipeExecutionSummary.vue')
  const html=await renderToString(createSSRApp(component,{recipe:{definition}}))
  assert.ok(html.includes(route.execution_instance_version_id))
  assert.ok(html.includes(sha))
  assert.ok(html.includes('不代表执行准入或烟测已通过'))
  assert.equal(html.includes('<select'),false)
  assert.deepEqual(definition,original)
}))

test('creation overview never converts legacy or broken definitions into a usable route',()=>modules(async (_,vite)=>{
  const {recipeExecutionOverview:read}=await vite.ssrLoadModule('/src/services/recipeExecutionOverview.ts')
  const route={execution_instance_version_id:'11111111-1111-4111-8111-111111111111',expected_definition_sha256:sha,input_sources:{},parameter_policies:[]}
  const definitionFor=routes=>({...base,stages:[{...base.stages[0],routing:{mode:'fixed',routes}}]})
  const invalid=[null,{}, {...base,schema_version:2},{...base,interaction_mode:undefined},{...base,interaction_mode:'unknown'},
    {...base,stages:[]},{...base,stages:[base.stages[0],base.stages[0]]},definitionFor([])]
  for(const [key,value] of [['execution_instance_version_id','latest'],['expected_definition_sha256','wrong'],['input_sources',null],['parameter_policies',{}],['workflow_version_id','old']]) {
    invalid.push(definitionFor([{...route,[key]:value}]))
  }
  for(const definition of invalid) {
    const result=read(definition)
    assert.notEqual(result.error,'')
    assert.deepEqual(result.stages,[])
  }
}))
