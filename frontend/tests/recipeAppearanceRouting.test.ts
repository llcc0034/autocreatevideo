import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
import {createRenderer,h,nextTick,reactive,ssrContextKey} from 'vue'

const id='11111111-1111-4111-8111-111111111111', sha='a'.repeat(64)
const route={execution_instance_version_id:id,expected_definition_sha256:sha,input_sources:{},parameter_policies:[]}
const group=(count:number)=>({mode:'manual_choice',variant_count:count,evaluator:null,routes:[route,{...route}]})
const definition=()=>({schema_version:3,interaction_mode:'assisted',stages:[{key:'bible_image_generation',mode:'existing_pipeline',approval:'required',routing:group(2),variant_routing:group(3)}]})
const options:any={definitionSchemaVersion:3,stageOptions:[{value:'bible_image_generation',label:'素材生成',repeatable:false,modes:['existing_pipeline'],routingModes:['existing_pipeline'],candidateEvaluatorOptions:[]}],
  interactionModeOptions:[{value:'assisted'}],approvalOptions:[{value:'required'}],routingModeOptions:[{value:'manual_choice'}]}
async function modules(fn){const vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try{await fn(vite)}finally{await vite.close()}}

test('role authoring and frozen overview keep separate routes and never inherit a missing variant',()=>modules(async vite=>{
  const {recipeAuthoringErrors,recipeRoutingGroups}=await vite.ssrLoadModule('/src/services/productionRecipeAuthoring.ts')
  const {recipeExecutionOverview}=await vite.ssrLoadModule('/src/services/recipeExecutionOverview.ts')
  const value:any=definition(),original=structuredClone(value)
  assert.deepEqual(recipeAuthoringErrors(value,options),[])
  assert.deepEqual(recipeRoutingGroups(value.stages[0]).map(row=>row.key),['routing','variant_routing'])
  assert.equal(recipeExecutionOverview(value).stages[0].variantRouting.routes.length,2)
  value.stages[0].variant_routing.variant_count=0
  assert.ok(recipeAuthoringErrors(value,options).length)
  delete value.stages[0].variant_routing
  assert.equal(recipeExecutionOverview(value).stages[0].variantRouting,undefined)
  const wrong=structuredClone(original);wrong.stages[0].key='media_generation'
  assert.ok(recipeAuthoringErrors(wrong,options).length)
  assert.match(recipeExecutionOverview(wrong).error,/只有素材主图/)
}))

test('formal route choices emit exact role and render independently confirmed selections',()=>modules(async vite=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeExecutionRouteChoices.vue')
  const props:any=reactive({busy:false,run:{id:'isolated',planSource:'recipe',status:'awaiting_approval',executionPlan:{sha256:sha,value:{recipe:{definition:definition()}}},
    stages:[{id:'stage',stageKey:'bible_image_generation',status:'pending',selectedRouteIndex:1,selectedVariantRouteIndex:null}]}})
  const events:any[]=[];let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(...args)=>events.push(args)});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()})
  try{app.mount({});await nextTick()
    assert.equal(state.stages.value.length,2)
    assert.equal(state.visibleStages.value.length,2)
    assert.equal(state.selections.value['bible_image_generation:primary'],1)
    assert.equal(state.selections.value['bible_image_generation:variant'],null)
    state.selections.value['bible_image_generation:variant']=0
    props.stageKeys=['media_generation'];await nextTick()
    assert.equal(state.visibleStages.value.length,0)
    assert.equal(state.selections.value['bible_image_generation:variant'],0)
    props.stageKeys=['bible_image_generation'];await nextTick()
    assert.equal(state.visibleStages.value.length,2)
    assert.equal(state.selections.value['bible_image_generation:primary'],1)
    state.select('bible_image_generation:variant')
    assert.deepEqual(events,[['select',{stageKey:'bible_image_generation',routeIndex:0,appearanceRole:'variant'}]])
    props.busy=true;state.select('bible_image_generation:primary');assert.equal(events.length,1)
    props.run.stages[0].selectedVariantRouteIndex=0;await nextTick()
    assert.equal(state.stages.value[0].selectedIndex,1);assert.equal(state.stages.value[1].selectedIndex,0)
  }finally{app.unmount()}
}))

test('formal action payload and Stage adapter keep variant choice distinct from primary',()=>modules(async vite=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const {api}=await vite.ssrLoadModule('/src/services/api.ts')
  const {adaptProductionRun}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const sent:any[]=[]
  api.post=async(url,body)=>{sent.push({url,body});throw new Error('Isolated transport, no real request')}
  await assert.rejects(()=>platformApi.selectProductionRoute('isolated-run',{
    stageKey:'bible_image_generation',routeIndex:0,appearanceRole:'variant'}),/Isolated transport/)
  assert.deepEqual(sent,[{url:'/production-runs/isolated-run/select-route/',body:{
    stage_key:'bible_image_generation',route_index:0,appearance_role:'variant'}}])
  const run=adaptProductionRun({plan_source:'recipe',recipe_version:'isolated-recipe',stages:[{id:'stage',stage_key:'bible_image_generation',
    selected_route_index:1,selected_variant_route_index:0}]})
  assert.equal(run.stages[0].selectedRouteIndex,1)
  assert.equal(run.stages[0].selectedVariantRouteIndex,0)
}))
